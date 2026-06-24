"""AJTA — model routing (AJTA-SPEC-1.0 §10, Appendices A–C).

A thin router over `ai_summarizer` that adds the spec's normative guarantees:
  * EVERY model call emits an `aj_routing` row (telemetry feeds the eval loop).
  * sensitivity='private' (any prompt touching holdings / P&L / account ids)
    MUST route to a LOCAL model and MUST NOT egress. If no local model is
    reachable, the call returns ok=False — it never silently falls to a cloud
    model (fail-closed on privacy).
  * escalation is bounded (≤1 by default).
  * a quality_floor unmet by all available models fails the step (ok=False),
    so the operator proposes NO trade rather than acting on degraded output
    (§20.4 fail-closed on decision quality).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import aj_db

log = logging.getLogger("augur.aj_routing")

PUBLIC = "public"
PRIVATE = "private"


def _ai():
    import ai_summarizer
    return ai_summarizer


def is_private(text: Any) -> bool:
    """Heuristic sensitivity classifier — true if a prompt likely contains
    portfolio holdings, P&L, or account identifiers. Conservative: when in
    doubt, treat as private (keeps it local)."""
    if not text:
        return False
    s = str(text).lower()
    cues = ("portfolio", "holding", "my position", "p&l", "pnl", "account",
            "shares of", "avg cost", "cost basis", "net worth", "my book",
            "unrealized", "realized gain", "day_pnl", "balance")
    return any(c in s for c in cues)


def _messages_text(messages: Any) -> str:
    try:
        return " ".join(str(m.get("content", "")) for m in messages
                        if isinstance(m, dict))
    except Exception:
        return str(messages)


def _local_available() -> bool:
    try:
        return bool(_ai()._ollama_ready())
    except Exception:
        return False


def _meets_floor(text: Optional[str], quality_floor: float) -> bool:
    if not text:
        return False
    if quality_floor <= 0:
        return True
    # We can't truly grade quality offline; use a non-trivial-length proxy.
    return len(text.strip()) >= max(1, int(8 * quality_floor))


def route(messages: List[Dict[str, Any]], role: str = "", complexity: float = 0.5,
          sensitivity: Optional[str] = None, quality_floor: float = 0.0,
          cycle_id: Optional[str] = None, max_escalations: int = 1,
          max_tokens: Optional[int] = None, json_mode: bool = False) -> Dict[str, Any]:
    """Route one completion. Returns
    {ok, text, model, sensitivity, escalated, fallback_used, latency_ms}.
    """
    if sensitivity is None:
        sensitivity = PRIVATE if is_private(_messages_text(messages)) else PUBLIC
    t0 = time.time()
    chosen = "none"
    text: Optional[str] = None
    ok = False
    fallback_used = 0
    escalated = 0

    try:
        if sensitivity == PRIVATE:
            # LOCAL ONLY — never egress a private prompt.
            if _local_available():
                chosen = "ollama"
                text = _ai()._ollama_chat(messages, max_tokens=max_tokens)
                ok = _meets_floor(text, quality_floor)
                # bounded escalation: retry once locally on a quality miss
                if not ok and max_escalations >= 1 and text is not None:
                    escalated = 1
                    text = _ai()._ollama_chat(messages, max_tokens=max_tokens)
                    ok = _meets_floor(text, quality_floor)
            else:
                chosen = "none(local-required)"
                ok = False  # fail-closed: no local model => no private inference
        else:
            chosen = "chat_any"
            text = _ai().chat_any(messages, max_tokens=max_tokens, json_mode=json_mode)
            ok = _meets_floor(text, quality_floor)
            if not ok and max_escalations >= 1:
                escalated = 1
                fallback_used = 1
                text = _ai().chat_any(messages, max_tokens=(max_tokens or 0) + 256,
                                      json_mode=json_mode)
                ok = _meets_floor(text, quality_floor)
    except Exception as e:
        log.exception("route failed for role=%s", role)
        ok = False
        text = None
        chosen = chosen + "(error)"

    latency_ms = int((time.time() - t0) * 1000)
    try:
        aj_db.insert("aj_routing", ts=aj_db.utc_now_iso(), cycle_id=cycle_id,
                     role=role, complexity=complexity, sensitivity=sensitivity,
                     chosen_model=chosen, fallback_used=fallback_used,
                     cost_usd=0.0, latency_ms=latency_ms, ok=1 if ok else 0,
                     escalated=escalated)
    except Exception:
        log.exception("could not record aj_routing")

    return {"ok": ok, "text": text, "model": chosen, "sensitivity": sensitivity,
            "escalated": escalated, "fallback_used": fallback_used,
            "latency_ms": latency_ms}


def complete(prompt: str, system: Optional[str] = None, **kw) -> Dict[str, Any]:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return route(msgs, **kw)
