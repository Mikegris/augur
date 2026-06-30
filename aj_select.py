"""aj_select — relative opportunity selection + regime-conditional signal trust.

This module turns the agent from "trade anything above an absolute edge bar"
into "trade the BEST relative opportunities", and makes how much we trust the
ensemble's components depend on the prevailing market regime.

Design rules (all functions here obey them):
  * PURE + DETERMINISTIC: no I/O, no network, no DB, no trading, no globals
    mutated. Given the same inputs you get the same outputs.
  * NEVER RAISE: degenerate inputs (None, NaN, missing keys, wrong types) are
    coerced or dropped, never propagated as exceptions.
  * NO NEW DEPS: stdlib only (math).
  * THE ABSOLUTE EDGE FLOOR IS NEVER BYPASSED: relative ranking selects *among*
    names that already clear `min_edge_pct_pts`; it cannot promote a sub-floor
    name into a trade.

A "forecast" is the dict produced by forecast_ensemble.ensemble_forecast and
consumed by aj_operator: {"ensemble": {"prob_up": float, "edge_pct_pts": float,
...}, ...}. The operator also surfaces top-level prob_up/edge on its decisions,
so we read defensively from both shapes.

Regime labels come from aj_alpha.detect_regime(): "bull" | "bear" | "chop".
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "composite_edge",
    "cross_sectional_rank",
    "select_top_n",
    "regime_signal_weights",
]


# --- regime tilt table -------------------------------------------------------
# Bounded multipliers applied to the base component weights before renormalizing
# back to the original total. A trending regime (bull/bear) leans into momentum
# /trend and away from mean-reversion; a range/neutral regime (chop) does the
# reverse. Components not listed are left at ×1.0 (renormalization still keeps
# the total constant). Multipliers are intentionally small (×1.3 / ×0.7) so no
# single regime can dominate or zero-out a signal.
_TILT_UP = 1.3
_TILT_DOWN = 0.7

# Component families. Anything matching a "momentum/trend" key gets the trend
# tilt; anything matching a "mean reversion" key gets the opposite tilt.
_TREND_KEYS = ("trend", "momentum", "narrative", "smart_money", "smart-money")
_MEANREV_KEYS = ("mean_reversion", "mean-reversion", "meanreversion", "reversion")


def _to_float(x: Any) -> Optional[float]:
    """Coerce to a finite float, else None. Never raises."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def composite_edge(forecast: Any) -> float:
    """Single comparable score for a forecast: SIGNED edge in percentage points
    (positive = bullish, negative = bearish).

    Prefers the calibrated `edge_pct_pts`; falls back to (prob_up - 0.5) * 100
    only when the calibrated edge is genuinely absent. Reads from the nested
    ``ensemble`` block first, then top-level keys (the operator surfaces both).
    Returns 0.0 for anything missing/unparseable so it sorts as "no conviction"
    rather than crashing or polluting the ranking with NaN.
    """
    if not isinstance(forecast, dict):
        return 0.0
    ens = forecast.get("ensemble")
    if not isinstance(ens, dict):
        ens = {}

    # 1) calibrated signed edge in pp (preferred), from ensemble then top-level.
    for src in (ens, forecast):
        if "edge_pct_pts" in src and src.get("edge_pct_pts") is not None:
            e = _to_float(src.get("edge_pct_pts"))
            if e is not None:
                return e
    # The operator also exposes a plain "edge" on its decision dicts.
    for src in (ens, forecast):
        if "edge" in src and src.get("edge") is not None:
            e = _to_float(src.get("edge"))
            if e is not None:
                return e

    # 2) fall back to prob_up only when no edge field was present.
    for src in (ens, forecast):
        if src.get("prob_up") is not None:
            p = _to_float(src.get("prob_up"))
            if p is not None:
                return (p - 0.5) * 100.0

    return 0.0


def _signed_score_key(item: Tuple[Any, Any]):
    """NaN-safe sort key: rank by SIGNED composite_edge DESCENDING so the most
    BULLISH name ranks first (this feeds a LONG-entry flow — a bearish/negative
    edge must NOT be promoted to the top by its magnitude). Degenerate scores
    (NaN/inf) sort as 0.0 conviction (neutral), sinking below any real positive
    edge while still outranking real negatives. Negate so a plain ascending sort
    yields most-bullish-first.
    """
    try:
        fv = float(composite_edge(item[1]))
    except (TypeError, ValueError):
        fv = 0.0
    if math.isnan(fv) or math.isinf(fv):
        fv = 0.0
    return -fv


def cross_sectional_rank(candidates: Any, cfg: Optional[Dict[str, Any]] = None) -> List[Tuple[Any, Any]]:
    """Rank (symbol, forecast) tuples by SIGNED composite_edge descending (most
    BULLISH first). This feeds a long-entry flow, so a strongly NEGATIVE (bearish)
    edge ranks LAST, not first — ranking by magnitude would wrongly fill the
    top-N with shorts the agent can't take. NaN/None/garbage edges score 0.0
    (neutral) and sit between positive and negative edges; the sort never crashes
    on them. Returns a NEW list; the input is not mutated. Empty or non-iterable
    input returns []. `cfg` is accepted for signature symmetry.
    """
    if not candidates:
        return []
    try:
        items = list(candidates)
    except TypeError:
        return []
    # Keep only well-formed (symbol, forecast) pairs; tolerate the rest by
    # dropping them rather than raising.
    pairs: List[Tuple[Any, Any]] = []
    for c in items:
        try:
            sym, fc = c[0], c[1]
        except (TypeError, IndexError, KeyError):
            continue
        pairs.append((sym, fc))
    # Stable sort preserves original order among equal-conviction names.
    return sorted(pairs, key=_signed_score_key)


def select_top_n(candidates: Any, cfg: Optional[Dict[str, Any]] = None) -> List[Tuple[Any, Any]]:
    """Select the best LONG-entry candidates: rank by SIGNED edge (most bullish
    first), drop any non-bullish (edge <= 0) name, then take the strongest
    `cross_sectional_top_n` (default 5) of those that also clear the absolute edge
    floor `min_edge_pct_pts` (default 0).

    This feeds a long-only entry flow, so a BEARISH name is never selected no
    matter how large its magnitude — ranking/selecting by |edge| would wrongly
    promote shorts the agent can't take. The floor is applied to the (positive)
    edge magnitude AFTER the bullish filter, and the relative top-N decides which
    of the floor-clearing bullish names to trade. Empty/garbage input returns []
    safely.
    """
    cfg = cfg or {}
    ranked = cross_sectional_rank(candidates, cfg)
    if not ranked:
        return []

    try:
        top_n = int(cfg.get("cross_sectional_top_n", 5))
    except (TypeError, ValueError):
        top_n = 5
    if top_n < 0:
        top_n = 0

    min_edge = _to_float(cfg.get("min_edge_pct_pts", 0))
    if min_edge is None:
        min_edge = 0.0
    min_edge = abs(min_edge)

    # Filter to BULLISH names clearing the absolute floor FIRST (ranked is already
    # most-bullish-first), THEN take the top-N — so a dropped bearish/sub-floor
    # name doesn't consume a top-N slot a tradable bullish name should have.
    eligible: List[Tuple[Any, Any]] = []
    for sym, fc in ranked:
        edge = composite_edge(fc)
        if edge > 0 and edge >= min_edge:
            eligible.append((sym, fc))
    return eligible[:top_n]


def _classify(key: Any) -> int:
    """+1 if the component is a momentum/trend signal, -1 if mean-reversion,
    0 otherwise. Case/separator-insensitive, never raises."""
    k = str(key).strip().lower()
    for mr in _MEANREV_KEYS:
        if mr in k:
            return -1
    for tr in _TREND_KEYS:
        if tr in k:
            return 1
    return 0


def regime_signal_weights(base_weights: Any, regime: Any, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """Re-weight the ensemble's component weights for the current regime, then
    renormalize so the TOTAL weight is preserved.

    Tilt table (bounded multipliers, then renormalize):
        regime "bull"/"bear" (trending)  -> trend/momentum x1.3, mean_reversion x0.7
        regime "chop"        (range)     -> trend/momentum x0.7, mean_reversion x1.3
        unknown regime                   -> base unchanged

    Gated by cfg["regime_conditional_weights"] (default False); when off, returns
    base_weights unchanged. Always returns a NEW dict (input not mutated) on the
    re-weighting path. Robust to non-numeric / missing weights.
    """
    cfg = cfg or {}
    # Normalize base into a clean {str: float>=0} dict regardless of input shape.
    clean: Dict[str, float] = {}
    if isinstance(base_weights, dict):
        for k, v in base_weights.items():
            f = _to_float(v)
            clean[str(k)] = f if (f is not None and f >= 0) else 0.0

    # Flag off -> return base unchanged (the original object, as documented).
    if not cfg.get("regime_conditional_weights", False):
        return base_weights

    if not clean:
        return dict(clean)

    reg = str(regime).strip().lower() if regime is not None else ""
    if reg in ("bull", "bear"):
        trend_mult, mr_mult = _TILT_UP, _TILT_DOWN
    elif reg == "chop":
        trend_mult, mr_mult = _TILT_DOWN, _TILT_UP
    else:
        # Unknown regime: do not tilt. Return a copy of the cleaned base.
        return dict(clean)

    tilted: Dict[str, float] = {}
    for k, w in clean.items():
        cls = _classify(k)
        if cls > 0:
            tilted[k] = w * trend_mult
        elif cls < 0:
            tilted[k] = w * mr_mult
        else:
            tilted[k] = w

    orig_total = sum(clean.values())
    new_total = sum(tilted.values())
    if new_total <= 0 or orig_total <= 0:
        # Degenerate (all-zero) weights: nothing meaningful to renormalize.
        return dict(clean)

    scale = orig_total / new_total
    return {k: w * scale for k, w in tilted.items()}
