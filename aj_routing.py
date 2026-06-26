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


import re as _re
# Dollar amounts ("$1,200") and share quantities ("10 shares") strongly imply
# position/P&L context — treat as private.
_MONEY_RE = _re.compile(r"\$\s?\d")
_SHARES_RE = _re.compile(r"\b\d[\d,]*\s+(?:shares?|units?|contracts?)\b")
_FIRST_PERSON_HOLD_RE = _re.compile(
    r"\bi\s+(own|hold|bought|sold|have|bought|am holding)\b|\bmy\s+"
    r"(position|holding|stake|exposure|book|portfolio|trade)")

_PRIVATE_CUES = (
    "portfolio", "holding", "my position", "p&l", "pnl", "account",
    "shares of", "avg cost", "cost basis", "net worth", "my book",
    "unrealized", "realized gain", "day_pnl", "balance", "exposure",
    "rebalance", "trim my", "position size", "buying power", "cash balance",
    "should i sell", "should i buy", "should i trim", "should i hold")


def is_private(text: Any) -> bool:
    """Heuristic sensitivity classifier — true if a prompt likely references
    portfolio holdings, P&L, dollar amounts, share quantities, or account
    identifiers. Errs toward PRIVATE (routes local, no egress) when any
    position/money signal is present — the safe direction for a leak."""
    if not text:
        return False
    s = str(text).lower()
    if _MONEY_RE.search(s) or _SHARES_RE.search(s) or _FIRST_PERSON_HOLD_RE.search(s):
        return True
    return any(c in s for c in _PRIVATE_CUES)


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
                # bounded escalation: retry once locally on a quality miss OR
                # on a None first result (a transient connection blip to a
                # server that may have just recovered — the cached 60s probe
                # can lag a brief outage). Fail-closed semantics hold either
                # way: a still-None/short retry leaves ok=False.
                # A near-deterministic local model given identical input would
                # just reproduce the failing output, so bump the token budget
                # (mirrors the public path) to give the retry a genuine chance
                # to cross the floor rather than burning latency for nothing.
                if not ok and max_escalations >= 1:
                    escalated = 1
                    retry_tokens = (max_tokens + 256) if max_tokens is not None else None
                    text = _ai()._ollama_chat(messages, max_tokens=retry_tokens)
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
                # NOTE: this retry re-calls the same chat_any routing (no
                # distinct fallback backend is selected), so leave
                # fallback_used=0 — setting it would feed misleading
                # "a fallback model was used" telemetry to the eval loop.
                # Don't turn an intended-unbounded (None) completion into a hard
                # 256-token cap on the retry — that truncates the very output
                # that failed the floor for being too short.
                retry_tokens = (max_tokens + 256) if max_tokens is not None else None
                text = _ai().chat_any(messages, max_tokens=retry_tokens,
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


# ── two-tier abstraction (merge plan §5.5) ────────────────────────────────────
# TradingAgents' single biggest cost lever is a deep-think / quick-think split:
# heavy reasoning on a strong model, cheap summarization/tool-digestion on a fast
# one. ai_summarizer.chat_any/_ollama_chat do not yet expose per-call model
# selection, so for now the tier maps to call PARAMETERS (token budget + quality
# floor + temperature) and is recorded in telemetry via the role suffix. When
# ai_summarizer gains a model-override the physical model split slots in here
# without changing callers. The tier abstraction is what the council depends on.

DEEP = "deep"
QUICK = "quick"

_TIER_DEFAULTS = {
    # tier: (default_max_tokens, default_quality_floor)
    DEEP:  (1024, 1.0),
    QUICK: (512, 0.0),
}


def complete_tiered(prompt: str, tier: str = DEEP, system: Optional[str] = None,
                    role: str = "", max_tokens: Optional[int] = None,
                    quality_floor: Optional[float] = None, **kw) -> Dict[str, Any]:
    """Route a completion at a named reasoning tier (DEEP|QUICK). Resolves the
    token budget / quality floor from the tier when not given, and tags the
    telemetry role with the tier so the eval loop can attribute cost by tier."""
    t = tier if tier in _TIER_DEFAULTS else DEEP
    dt, df = _TIER_DEFAULTS[t]
    if max_tokens is None:
        max_tokens = dt
    if quality_floor is None:
        quality_floor = df
    tagged_role = "{}#{}".format(role or "council", t)
    return complete(prompt, system=system, role=tagged_role, max_tokens=max_tokens,
                    quality_floor=quality_floor, **kw)
