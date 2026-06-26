"""
forecast_accountability — closes the loop on the Forecast Ensemble.

The ensemble (forecast_ensemble.py) emits a calibrated directional
probability by fusing several engines. On its own it's a black box: are
those probabilities any good? Which engines actually earn the weight we
give them? This module answers both and feeds the answer back in.

The loop:

    forecast  ──►  LOG every ensemble call + each contributing component
                   (research_tracker.signal_forecasts)
                        │
                        ▼
    horizon    ──►  SCORE once the horizon elapses (the existing
    elapses         research_tracker.score_due_forecasts cron prices them)
                        │
                        ▼
    measure    ──►  BRIER score + reliability curve for the ensemble;
                    a per-component LEADERBOARD (hit-rate + Brier + n)
                        │
                        ▼
    adapt      ──►  ADAPTIVE WEIGHTS tilt the next fusion toward the
                    components with a proven edge ──┐
                        └───────────────────────────┘  (back to forecast)

Logging is fire-and-forget: any failure is swallowed so it can never break
a forecast. Reads degrade to empty/neutral when there's no scored history
yet (the common case on a fresh install).

Python 3.9 compatible.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("augur.forecast_accountability")

# The tracker signal_name for the fused headline call.
ENSEMBLE_SIGNAL = "forecast_ensemble"

# Ensemble component keys (must match forecast_ensemble._BASE_WEIGHTS) and
# the tracker signal_name each is logged under. Prefixed so component rows
# never collide with the standalone signal trackers (ml_forecast, narrative…).
COMPONENT_KEYS = (
    "rf_classifier", "ml_composite", "trend",
    "mean_reversion", "bootstrap", "narrative",
)


def _component_signal(key: str) -> str:
    return "ens:" + key


def _dir_label(direction: str) -> str:
    """Map an ensemble UP/DOWN/NEUTRAL into the tracker's tradeable labels
    so research_tracker._direction_matches can score it."""
    d = (direction or "").upper()
    if d == "UP":
        return "BUY"
    if d == "DOWN":
        return "SELL"
    return "NEUTRAL"


# --------------------------------------------------------------------------
# 1. LOG — record an ensemble forecast and its components.
# --------------------------------------------------------------------------

def log_ensemble(symbol: str, horizon_days: int, result: Dict[str, Any]) -> None:
    """Fire-and-forget: persist the ensemble call + each component to the
    research_tracker ledger. issue_price is left None — the scorer backfills
    it from the chart at issue time. The predicted prob_up is stored in
    metadata so Brier scoring can recover it later."""
    try:
        import research_tracker
    except Exception:
        return
    if not isinstance(result, dict):
        return
    ens = result.get("ensemble")
    if not isinstance(ens, dict):
        return

    try:
        cone = ens.get("return_cone") or {}
        er = cone.get("expected_return_pct")
        magnitude = (float(er) / 100.0) if er is not None else None
        research_tracker.log_forecast(
            ENSEMBLE_SIGNAL,
            symbol=symbol,
            horizon_days=horizon_days,
            direction=_dir_label(ens.get("direction")),
            magnitude=magnitude,
            confidence=ens.get("consensus"),
            issue_price=None,
            metadata={
                "prob_up": ens.get("prob_up"),
                "prob_up_raw": ens.get("prob_up_raw"),
                "verdict": ens.get("verdict"),
                "conviction": ens.get("conviction"),
                "consensus": ens.get("consensus"),
                "n_signals": result.get("n_signals"),
            },
        )
    except Exception as e:
        log.debug("log_ensemble headline failed for %s: %s", symbol, e)

    # Each component, scored at the same horizon as the ensemble so the
    # leaderboard compares apples to apples ("how good is this engine *as a
    # contributor to the N-day fused call*").
    for sig in (result.get("signals") or []):
        try:
            key = sig.get("key")
            if key not in COMPONENT_KEYS:
                continue
            research_tracker.log_forecast(
                _component_signal(key),
                symbol=symbol,
                horizon_days=horizon_days,
                direction=_dir_label(sig.get("direction")),
                magnitude=None,
                confidence=None,
                issue_price=None,
                metadata={"prob_up": sig.get("prob_up"), "weight": sig.get("weight")},
            )
        except Exception as e:
            log.debug("log_ensemble component %s failed: %s", sig.get("key"), e)


# --------------------------------------------------------------------------
# 2/3. MEASURE — Brier score, reliability curve, component leaderboard.
# --------------------------------------------------------------------------

def _brier_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Brier score + reliability buckets from scored rows that carry a
    `prob_up` in metadata. Outcome = 1 if realized_return > 0 else 0.

    Brier = mean((prob_up - outcome)^2), lower is better (0 = perfect).
    Reference = base_rate*(1-base_rate), the score of always predicting the
    base rate. Skill = 1 - brier/reference (>0 means beats climatology).
    """
    pairs: List[Tuple[float, int, float]] = []  # (prob_up, outcome, realized)
    for r in rows:
        # Only score directional calls so Brier covers the same population as
        # the hit-rate leaderboard. NEUTRAL/HOLD rows carry hit=None and are
        # excluded from hit-rate; including them here would make brier_skill
        # and hit_rate inconsistent for the same component.
        if r.get("hit") is None:
            continue
        md = r.get("metadata") or {}
        p = md.get("prob_up")
        rr = r.get("realized_return")
        if p is None or rr is None:
            continue
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        outcome = 1 if float(rr) > 0 else 0
        pairs.append((max(0.0, min(1.0, p)), outcome, float(rr)))

    n = len(pairs)
    if n == 0:
        return {"n": 0, "brier": None, "brier_skill": None,
                "base_rate": None, "reliability": [], "verdict": "NO DATA"}

    brier = sum((p - o) ** 2 for p, o, _ in pairs) / n
    base_rate = sum(o for _, o, _ in pairs) / n
    ref = base_rate * (1.0 - base_rate)
    skill = (1.0 - brier / ref) if ref > 1e-9 else None

    # Reliability curve: bin predicted prob into deciles, compare the bucket's
    # mean prediction to the realized frequency of up-moves in that bucket.
    buckets: Dict[int, List[Tuple[float, int]]] = {}
    for p, o, _ in pairs:
        b = min(9, int(p * 10))
        buckets.setdefault(b, []).append((p, o))
    reliability = []
    for b in sorted(buckets):
        bp = buckets[b]
        reliability.append({
            "bucket_low": round(b / 10.0, 2),
            "bucket_high": round((b + 1) / 10.0, 2),
            "predicted_mean": round(sum(p for p, _ in bp) / len(bp), 4),
            "realized_freq": round(sum(o for _, o in bp) / len(bp), 4),
            "count": len(bp),
        })

    if skill is None:
        verdict = "INSUFFICIENT SPREAD"
    elif skill >= 0.15:
        verdict = "WELL CALIBRATED"
    elif skill >= 0.0:
        verdict = "SLIGHT EDGE"
    else:
        verdict = "MISCALIBRATED"

    return {
        "n": n,
        "brier": round(brier, 4),
        "brier_skill": round(skill, 4) if skill is not None else None,
        "base_rate": round(base_rate, 4),
        "reliability": reliability,
        "verdict": verdict,
    }


def ensemble_calibration(since: Optional[str] = None,
                         symbol: Optional[str] = None) -> Dict[str, Any]:
    """Track record + Brier calibration for the fused ensemble signal."""
    try:
        import research_tracker
    except Exception:
        return {"error": "research_tracker unavailable"}
    record = research_tracker.get_track_record(ENSEMBLE_SIGNAL, since=since, symbol=symbol)
    rows = research_tracker.get_scored_rows(ENSEMBLE_SIGNAL, since=since, symbol=symbol)
    cal = _brier_from_rows(rows)
    return {"signal": ENSEMBLE_SIGNAL, "track_record": record, "calibration": cal}


def component_leaderboard(since: Optional[str] = None) -> List[Dict[str, Any]]:
    """For each ensemble component: hit-rate, Brier, and sample size. Ranked
    best-first by Brier skill (then hit-rate). This is the evidence behind the
    adaptive weights — which engines actually earn their seat in the fusion."""
    try:
        import research_tracker
    except Exception:
        return []
    out = []
    for key in COMPONENT_KEYS:
        sig = _component_signal(key)
        rec = research_tracker.get_track_record(sig, since=since)
        rows = research_tracker.get_scored_rows(sig, since=since)
        cal = _brier_from_rows(rows)
        out.append({
            "key": key,
            "signal": sig,
            "n": rec.get("n") or 0,
            "n_directional": rec.get("n_directional") or 0,
            "hit_rate": rec.get("hit_rate"),
            "avg_return": rec.get("avg_return"),
            "brier": cal.get("brier"),
            "brier_skill": cal.get("brier_skill"),
        })

    def _rank(row):
        # Higher brier_skill better; treat None as worst. Tie-break on hit_rate.
        bs = row["brier_skill"]
        hr = row["hit_rate"]
        return (bs if bs is not None else -9.0, hr if hr is not None else -9.0)

    out.sort(key=_rank, reverse=True)
    return out


# --------------------------------------------------------------------------
# 4. ADAPT — feed realized component skill back into the fusion weights.
# --------------------------------------------------------------------------

_MIN_N_FOR_ADAPT = 20      # need this many scored directional calls to trust a tilt
_MAX_TILT = 0.6            # a component's weight can swing at most ±60%
_TILT_GAIN = 2.0           # maps (hit_rate - 0.5) into a multiplier

# Tiny TTL cache so we don't re-query the ledger on every uncached forecast.
_weights_cache: Dict[Any, Tuple[float, Dict[str, float]]] = {}
_weights_cache_lock = threading.Lock()  # adaptive_weights runs under thread pools
_WEIGHTS_TTL = 600


def adaptive_weights(base_weights: Dict[str, float],
                     horizon_days: Optional[int] = None) -> Dict[str, float]:
    """Return component weights tilted toward proven performers.

    For each component with >= _MIN_N_FOR_ADAPT scored directional calls,
    multiply its base weight by clamp(1 + gain*(hit_rate-0.5), 1-MAX, 1+MAX).
    Components without enough history keep their base weight. The result is
    renormalized to sum to the same total as the input, so an all-cold-start
    install behaves exactly like the static-weight ensemble.

    Never raises — on any failure returns base_weights unchanged.
    """
    try:
        import research_tracker
    except Exception:
        return dict(base_weights)

    # WHY (Q9): skill is horizon-specific, so a horizon-5 adaptation must not
    # reuse a cached horizon-60 result — fold horizon_days into the cache key.
    key = ("adaptive", horizon_days, tuple(sorted(base_weights.items())))
    with _weights_cache_lock:
        hit = _weights_cache.get(key)
    if hit is not None and (time.time() - hit[0]) < _WEIGHTS_TTL:
        return dict(hit[1])

    try:
        adjusted: Dict[str, float] = {}
        total_base = sum(base_weights.values()) or 1.0
        for k, w in base_weights.items():
            mult = 1.0
            try:
                # WHY (Q9): filter the track record to this horizon so we tilt
                # on the component's hit-rate AT this horizon, not a pooled
                # average across all horizons.
                rec = research_tracker.get_track_record(
                    _component_signal(k), horizon_days=horizon_days)
                n = rec.get("n_directional") or 0
                hr = rec.get("hit_rate")
                if n >= _MIN_N_FOR_ADAPT and hr is not None:
                    raw = 1.0 + _TILT_GAIN * (float(hr) - 0.5)
                    mult = max(1.0 - _MAX_TILT, min(1.0 + _MAX_TILT, raw))
            except Exception:
                mult = 1.0
            adjusted[k] = w * mult

        total_adj = sum(adjusted.values()) or 1.0
        scale = total_base / total_adj
        out = {k: v * scale for k, v in adjusted.items()}
        with _weights_cache_lock:
            _weights_cache[key] = (time.time(), out)
        return dict(out)
    except Exception as e:
        log.debug("adaptive_weights failed: %s", e)
        return dict(base_weights)


def weights_are_adapted(base_weights: Dict[str, float],
                        horizon_days: Optional[int] = None) -> bool:
    """True if adaptive_weights would meaningfully differ from base (i.e. at
    least one component has crossed the min-sample threshold).

    `horizon_days` must match the horizon the applied weights are computed at,
    so the 'adapted' label agrees with the weights actually used (a component
    can have enough pooled history but too little at a specific horizon, or
    vice-versa). Defaults to pooled (None) for backward compatibility."""
    adj = adaptive_weights(base_weights, horizon_days)
    for k in base_weights:
        if abs(adj.get(k, 0.0) - base_weights.get(k, 0.0)) > 1e-6:
            return True
    return False


# --------------------------------------------------------------------------
# Bundled report for the UI.
# --------------------------------------------------------------------------

def accountability_report(since: Optional[str] = None) -> Dict[str, Any]:
    """One call for the Forecast view's accountability panel."""
    ens = ensemble_calibration(since=since)
    board = component_leaderboard(since=since)
    try:
        import forecast_ensemble
        base = forecast_ensemble._BASE_WEIGHTS
        adapted = weights_are_adapted(base)
        adj = adaptive_weights(base)
        weights = [{
            "key": k,
            "base_weight": round(base[k], 4),
            "effective_weight": round(adj.get(k, base[k]), 4),
        } for k in base]
    except Exception:
        adapted = False
        weights = []
    return {
        "ensemble": ens,
        "leaderboard": board,
        "weights_adapted": adapted,
        "weights": weights,
    }


if __name__ == "__main__":  # manual smoke test
    import json
    import sys
    fn = sys.argv[1] if len(sys.argv) > 1 else "report"
    if fn == "report":
        print(json.dumps(accountability_report(), indent=2, default=str))
    elif fn == "leaderboard":
        print(json.dumps(component_leaderboard(), indent=2, default=str))
