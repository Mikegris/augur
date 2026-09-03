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


def _meets_min_length(text: Optional[str], quality_floor: float) -> bool:
    """Completeness/length heuristic — NOT a quality grade. Returns True iff the
    model produced a non-empty output of at least a minimum length. We cannot
    truly grade answer quality offline, so `quality_floor` is interpreted as a
    minimum-output-length bar (≈ 8 chars per floor unit): it only catches empty
    or trivially short (truncated/failed) completions, not low-quality ones."""
    if not text:
        return False
    if quality_floor <= 0:
        return True
    # Non-trivial-length proxy — a completeness check, not a quality grade.
    return len(text.strip()) >= max(1, int(8 * quality_floor))


def route(messages: List[Dict[str, Any]], role: str = "", complexity: float = 0.5,
          sensitivity: Optional[str] = None, quality_floor: float = 0.0,
          cycle_id: Optional[str] = None, max_escalations: int = 1,
          max_tokens: Optional[int] = None, json_mode: bool = False,
          model: Optional[str] = None) -> Dict[str, Any]:
    """Route one completion. Returns
    {ok, text, model, sensitivity, escalated, fallback_used, latency_ms,
     cost_usd}.

    `model` (when set, non-empty) overrides the cloud model for a PUBLIC call —
    this is how the deep/quick tiers select a stronger/cheaper model. It is
    DELIBERATELY ignored on the PRIVATE path: a private prompt must stay on the
    local model and must never carry a cloud model name (fail-closed on privacy).
    """
    if sensitivity is None:
        sensitivity = PRIVATE if is_private(_messages_text(messages)) else PUBLIC
    model = (str(model).strip() if model else "") or None
    t0 = time.time()
    chosen = "none"
    text: Optional[str] = None
    ok = False
    fallback_used = 0
    escalated = 0
    cost_usd = 0.0

    try:
        if sensitivity == PRIVATE:
            # LOCAL ONLY — never egress a private prompt. Note: `model` is NOT
            # forwarded here — a private call always uses the configured local
            # model, never a (possibly cloud) tier model name.
            if _local_available():
                chosen = "ollama"
                text = _ai()._ollama_chat(messages, max_tokens=max_tokens)
                ok = _meets_min_length(text, quality_floor)
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
                    ok = _meets_min_length(text, quality_floor)
            else:
                chosen = "none(local-required)"
                ok = False  # fail-closed: no local model => no private inference
        else:
            chosen = "chat_any" if not model else "chat_any:" + model
            text, c = _ai().chat_any(messages, max_tokens=max_tokens,
                                     json_mode=json_mode, model=model,
                                     return_cost=True)
            cost_usd += c or 0.0
            ok = _meets_min_length(text, quality_floor)
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
                text, c = _ai().chat_any(messages, max_tokens=retry_tokens,
                                         json_mode=json_mode, model=model,
                                         return_cost=True)
                cost_usd += c or 0.0
                ok = _meets_min_length(text, quality_floor)
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
                     cost_usd=cost_usd, latency_ms=latency_ms, ok=1 if ok else 0,
                     escalated=escalated)
    except Exception:
        log.exception("could not record aj_routing")

    return {"ok": ok, "text": text, "model": chosen, "sensitivity": sensitivity,
            "escalated": escalated, "fallback_used": fallback_used,
            "latency_ms": latency_ms, "cost_usd": cost_usd}


def complete(prompt: str, system: Optional[str] = None, **kw) -> Dict[str, Any]:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return route(msgs, **kw)


# ── two-tier abstraction (merge plan §5.5) ────────────────────────────────────
# TradingAgents' single biggest cost lever is a deep-think / quick-think split:
# heavy reasoning on a strong model, cheap summarization/tool-digestion on a fast
# one. The tier maps to call PARAMETERS (token budget + quality floor) AND to a
# physical model selected from aj_config (council_deep_model / council_quick_model
# — empty => the provider default). The model override only takes effect on a
# PUBLIC route; a PRIVATE prompt always stays on the local model (route() ignores
# the cloud model name on the private path — fail-closed on privacy). The tier
# abstraction is what the council depends on, and route() now returns cost_usd so
# the council's budget guard can account for spend.

DEEP = "deep"
QUICK = "quick"

_TIER_DEFAULTS = {
    # tier: (default_max_tokens, default_quality_floor)
    DEEP:  (1024, 1.0),
    QUICK: (512, 0.0),
}

# tier -> aj_config key holding the model name for that tier ("" => default)
_TIER_MODEL_KEY = {
    DEEP:  "council_deep_model",
    QUICK: "council_quick_model",
}


def _tier_model(tier: str) -> Optional[str]:
    """Resolve the configured model name for a tier, or None for the provider
    default. Any config read error => None (safe: provider default). Never
    raises."""
    key = _TIER_MODEL_KEY.get(tier)
    if not key:
        return None
    try:
        import aj_config
        m = str(aj_config.get(key) or "").strip()
        return m or None
    except Exception:
        return None


def complete_tiered(prompt: str, tier: str = DEEP, system: Optional[str] = None,
                    role: str = "", max_tokens: Optional[int] = None,
                    quality_floor: Optional[float] = None,
                    model: Optional[str] = None, **kw) -> Dict[str, Any]:
    """Route a completion at a named reasoning tier (DEEP|QUICK). Resolves the
    token budget / quality floor AND the model (from aj_config) for the tier when
    not given, tags the telemetry role with the tier so the eval loop can
    attribute cost by tier, and threads model selection + cost_usd through."""
    t = tier if tier in _TIER_DEFAULTS else DEEP
    dt, df = _TIER_DEFAULTS[t]
    if max_tokens is None:
        max_tokens = dt
    if quality_floor is None:
        quality_floor = df
    if model is None:
        model = _tier_model(t)            # "" / unset => None => provider default
    tagged_role = "{}#{}".format(role or "council", t)
    return complete(prompt, system=system, role=tagged_role, max_tokens=max_tokens,
                    quality_floor=quality_floor, model=model, **kw)


# ── strict-JSON call adapter (council CallFn path) ────────────────────────────
# Council roles that demand STRICT JSON (analysts, research manager, trader,
# risk debaters, arbiter) should request NATIVE JSON output from the backend
# (chat_any(json_mode=True)) instead of relying on prompt discipline alone.
# The council threads an OPTIONAL `json_mode` keyword through its injected
# call(tier, role, system, prompt) function; legacy 4-positional callables
# (injected test fakes, aj_analyst_util.default_call) MUST keep working, so
# support is detected by SIGNATURE — never by calling and catching TypeError,
# which would double-invoke (and double-spend) whenever a TypeError originated
# INSIDE the call body.

def _accepts_json_mode(call: Any) -> bool:
    """True iff `call` can take a json_mode keyword (an explicit parameter or a
    **kwargs catch-all). Fail-closed to False on uninspectable callables — the
    plain 4-arg invocation is always safe."""
    try:
        import inspect
        params = inspect.signature(call).parameters
        return ("json_mode" in params or
                any(p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in params.values()))
    except (TypeError, ValueError):
        return False


def call_json(call: Any, tier: str, role: str, system: str,
              prompt: str) -> Optional[str]:
    """Invoke a council completion for a STRICT-JSON role, passing
    json_mode=True when the callable supports it (backward compatible: the
    default stays False everywhere else, so existing callers are unchanged)."""
    if _accepts_json_mode(call):
        return call(tier, role, system, prompt, json_mode=True)
    return call(tier, role, system, prompt)
