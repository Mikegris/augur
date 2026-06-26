"""AJTA — Analyst Council: shared analyst utilities (merge plan §5.1).

This module holds the small, now-PUBLIC helpers that the analyst adapters
(aj_analysts) and the persona analysts (aj_personas) both rely on. It was
extracted to resolve a private-import smell: aj_personas previously reached into
aj_analysts' module privates (_safe/_fmt/_neutral/CallFn/default_call). Both
modules now import these from here instead.

The helpers themselves are byte-for-byte identical to their original
definitions — this is a pure refactor. Names are kept (the leading-underscore
ones are re-exported under public aliases too) so any external importer keeps
working.

Contents:
  * CallFn / default_call — the analyst completion type + real router-backed call.
  * _safe (safe)          — fail-open engine invocation.
  * _fmt  (fmt)           — compact evidence into a bounded JSON snippet.
  * _neutral (neutral)    — neutral, low-confidence AnalystReport fallback.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

import aj_routing
from aj_schemas import AnalystReport

log = logging.getLogger("augur.aj_analyst_util")

# call(tier, role, system, prompt) -> Optional[str]
CallFn = Callable[[str, str, str, str], Optional[str]]


def default_call(tier: str, role: str, system: str, prompt: str) -> Optional[str]:
    """Real completion via the privacy-aware router. Public sensitivity: an
    analyst prompt is market evidence about ONE ticker — no holdings/P&L — so it
    may use a cloud model. (The operator's portfolio is never injected here.)"""
    try:
        r = aj_routing.complete_tiered(prompt, tier=tier, system=system, role=role,
                                       sensitivity=aj_routing.PUBLIC)
        return r.get("text") if r and r.get("ok") else None
    except Exception:
        log.exception("default_call failed for role=%s", role)
        return None


def _safe(fn, *a, **k):
    """Call an engine fail-open; return its result or None on any error."""
    try:
        return fn(*a, **k)
    except Exception as e:
        log.debug("evidence engine %s failed: %s", getattr(fn, "__name__", fn), e)
        return None


def _fmt(obj: Any, keep: Optional[List[str]] = None, limit: int = 1500) -> str:
    """Compact a dict/list of evidence into a bounded JSON snippet for a prompt."""
    if obj is None:
        return ""
    try:
        if isinstance(obj, dict) and keep:
            # Compute each value once; drop missing (None) and NaN floats — NaN
            # would otherwise serialize as the literal token 'NaN', which is
            # invalid JSON in the evidence snippet the model is shown.
            vals = ((k, obj.get(k)) for k in keep)
            obj = {k: v for k, v in vals
                   if v is not None and not (isinstance(v, float) and v != v)}
        s = json.dumps(obj, default=str, separators=(",", ":"))
        return s[:limit]
    except Exception:
        return str(obj)[:limit]


def _neutral(analyst: str, why: str = "") -> AnalystReport:
    return AnalystReport(analyst=analyst, band="NEUTRAL", score=5.0, confidence=0.1,
                         key_points=([why] if why else []),
                         narrative=why or "insufficient evidence")


# Public aliases (the leading-underscore names are kept above for byte-for-byte
# compatibility; these expose the same callables without the private-looking
# underscore for new call sites).
safe = _safe
fmt = _fmt
neutral = _neutral
