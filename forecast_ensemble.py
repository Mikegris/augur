"""
forecast_ensemble — a calibrated meta-forecast that fuses AUGUR's siloed
forecasting engines into a single directional probability + return cone.

Motivation
----------
AUGUR ships ~10 independent forecasting signals (RandomForest classifier,
trend regression, mean-reversion, block-bootstrap return distribution,
narrative-phase momentum, …). Each lives in its own module and its own UI
view. A user wanting "so what is the actual forecast?" has to eyeball half a
dozen panels and reconcile them in their head.

This module does that reconciliation rigorously:

1. Collect each available signal as a directional probability `prob_up`
   in [0, 1] with a confidence weight.
2. Fuse them with a weighted average into a raw ensemble probability.
3. Measure cross-signal DISAGREEMENT (weighted std of the per-signal
   probabilities). High disagreement => the signals contradict each other
   => the ensemble edge is less trustworthy.
4. CALIBRATE by shrinking the edge toward 0.5 in proportion to
   disagreement. A 60/40 call that every signal agrees on survives; a
   60/40 call the signals are split on collapses toward neutral. This is
   the key step that makes the meta-forecast better than naive averaging —
   it refuses to express conviction the underlying signals don't support.
5. Anchor the RETURN forecast on the block-bootstrap distribution (the only
   signal that produces a real, fat-tailed return cone), tilted slightly by
   the ensemble's directional lean.

Everything degrades gracefully: any signal that errors or is missing is
dropped, weights renormalize over what's left, and the response reports how
many signals actually contributed.

Python 3.9 compatible (no PEP 604 unions).
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import safe_executor

log = logging.getLogger("augur.forecast_ensemble")

try:
    import cache_store
except Exception:  # pragma: no cover
    cache_store = None

_CACHE_TTL = 900  # 15 min — the underlying engines cache for ~1h each

# Default trust weights per signal. Renormalized over whatever is available.
# The two real statistical models (RF classifier, bootstrap) carry the most
# weight; the soft/narrative signals are tie-breakers.
_BASE_WEIGHTS = {
    "rf_classifier":   0.28,
    "bootstrap":       0.26,
    "ml_composite":    0.18,
    "trend":           0.12,
    "mean_reversion":  0.08,
    "narrative":       0.08,
}

# Disagreement (weighted std of per-signal prob_up) at which we trust ~none
# of the edge. 0.22 ≈ signals spanning roughly [0.28, 0.72]: a genuine split.
_MAX_DISAGREEMENT = 0.22

# Map a news narrative phase to a directional probability lean.
_NARRATIVE_PHASE_PROB = {
    "EMERGENCE":     0.62,
    "ACCELERATION":  0.66,
    "CONSENSUS":     0.52,   # everyone's already in — limited upside
    "EXHAUSTION":    0.40,
    "REVERSAL":      0.35,
    "DEVELOPING":    0.50,
    "NEUTRAL":       0.50,
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Individual signal extractors. Each returns either a dict
#   {"prob_up": float in [0,1], "detail": str, "expected_return_pct": float?}
# or None if the signal is unavailable. They never raise — failures map to None.
# --------------------------------------------------------------------------

def _sig_ml(symbol: str) -> Dict[str, Optional[Dict[str, Any]]]:
    """Pull RF classifier, ML composite, trend, and mean-reversion out of a
    single ml_forecast call (one fetch, four signals)."""
    out: Dict[str, Optional[Dict[str, Any]]] = {
        "rf_classifier": None, "ml_composite": None,
        "trend": None, "mean_reversion": None,
    }
    try:
        import ml_forecast
        ml = ml_forecast.ml_forecast(symbol)
    except Exception as e:
        log.debug("ml_forecast failed for %s: %s", symbol, e)
        return out
    if not ml or ml.get("error"):
        return out

    rf = ml.get("rf_classifier") or {}
    if rf.get("prob_up_20d") is not None:
        p = _clamp(float(rf["prob_up_20d"]))
        out["rf_classifier"] = {
            "prob_up": p,
            "detail": "RandomForest P(up,20d)={:.0%}".format(p),
        }

    comp = ml.get("composite_ml_score")
    if comp is not None:
        out["ml_composite"] = {
            "prob_up": _clamp(float(comp)),
            "detail": "ML composite {} ({:.0%})".format(
                ml.get("composite_signal", "?"), float(comp)),
        }

    tf = ml.get("trend_forecast") or {}
    fp = tf.get("forecast_pct")
    if fp is not None:
        fp = float(fp)
        # Map a 30d trend % into a probability-like lean, same scaling the
        # ML module uses internally (±20% -> ±0.4 around 0.5).
        p = _clamp(0.5 + max(-0.4, min(0.4, fp / 20.0)))
        out["trend"] = {
            "prob_up": p,
            "expected_return_pct": round(fp, 2),
            "detail": "Trend forecast {:+.1f}% / 30d".format(fp),
        }

    mr = ml.get("mean_reversion") or {}
    direction = mr.get("reversion_direction")
    mrp = mr.get("mr_probability")
    if direction and mrp is not None:
        mrp = float(mrp)
        if direction == "UP":
            p = _clamp(0.5 + mrp * 0.3)
        elif direction == "DOWN":
            p = _clamp(0.5 - mrp * 0.3)
        else:
            p = 0.5
        out["mean_reversion"] = {
            "prob_up": p,
            "detail": "Mean-reversion {} ({:.0%})".format(direction, mrp),
        }

    return out


def _sig_bootstrap(symbol: str, horizon_days: int) -> Optional[Dict[str, Any]]:
    """Block-bootstrap return distribution → P(up) + the return cone that
    anchors the ensemble's return forecast."""
    try:
        import research_probforecast
        pf = research_probforecast.prob_forecast(symbol, horizon_days=horizon_days)
    except Exception as e:
        log.debug("prob_forecast failed for %s: %s", symbol, e)
        return None
    if not pf or pf.get("error"):
        return None
    probs = pf.get("probabilities") or {}
    dist = pf.get("distribution") or {}
    up = probs.get("up")
    if up is None:
        return None
    return {
        "prob_up": _clamp(float(up)),
        "expected_return_pct": dist.get("mean"),
        "distribution": dist,
        "detail": "Bootstrap P(up,{}d)={:.0%}, median {:+.1f}%".format(
            horizon_days, float(up), float(dist.get("median", 0.0))),
    }


def _sig_narrative(symbol: str) -> Optional[Dict[str, Any]]:
    """News narrative phase → directional lean. A soft, slow signal."""
    try:
        import narrative_engine
        nar = narrative_engine.analyze_narrative(symbol)
    except Exception as e:
        log.debug("analyze_narrative failed for %s: %s", symbol, e)
        return None
    if not nar or nar.get("error"):
        return None
    if not nar.get("article_count"):
        return None  # no news -> no signal, don't dilute with a 0.5 vote
    phase = (nar.get("narrative_phase") or "NEUTRAL").upper()
    p = _NARRATIVE_PHASE_PROB.get(phase, 0.5)
    # Contrarian flag flips an over-extended narrative.
    if nar.get("contrarian_signal"):
        p = _clamp(1.0 - p) if p != 0.5 else 0.5
    return {
        "prob_up": _clamp(p),
        "detail": "Narrative phase {} ({} articles)".format(
            phase, nar.get("article_count")),
    }


# --------------------------------------------------------------------------
# Fusion
# --------------------------------------------------------------------------

def _verdict(prob_up: float, conviction: str) -> str:
    if conviction == "NONE":
        return "NO EDGE"
    if prob_up >= 0.62:
        return "STRONG BUY" if conviction == "HIGH" else "BUY"
    if prob_up >= 0.545:
        return "BUY" if conviction in ("HIGH", "MODERATE") else "LEAN LONG"
    if prob_up > 0.455:
        return "NEUTRAL"
    if prob_up > 0.38:
        return "SELL" if conviction in ("HIGH", "MODERATE") else "LEAN SHORT"
    return "STRONG SELL" if conviction == "HIGH" else "SELL"


def _conviction(edge: float, consensus: float, n: int) -> str:
    """Edge = |calibrated prob - 0.5| in [0,0.5]. Consensus in [0,1]."""
    if n < 2 or abs(edge) < 0.015:
        return "NONE"
    score = abs(edge) * 2.0 * consensus  # 0..1
    if score >= 0.28 and n >= 3:
        return "HIGH"
    if score >= 0.12:
        return "MODERATE"
    return "LOW"


def _build_return_cone(bootstrap: Optional[Dict[str, Any]],
                       trend_ret: Optional[float],
                       tilt: float) -> Optional[Dict[str, Any]]:
    """Return forecast cone. Prefer the bootstrap distribution (real,
    fat-tailed). Tilt is the ensemble directional lean in [-0.5,0.5]; it
    nudges the central estimate toward the consensus direction without
    distorting the dispersion."""
    if bootstrap and bootstrap.get("distribution"):
        d = bootstrap["distribution"]
        median = float(d.get("median", 0.0))
        # Shift the whole cone by up to ±1/3 of its own half-width toward the
        # ensemble lean, so a bullish consensus lifts the expected path.
        half_width = (float(d.get("p75", median)) - float(d.get("p25", median))) / 2.0
        shift = tilt * 0.66 * abs(half_width)
        return {
            "source": "bootstrap",
            "expected_return_pct": round(median + shift, 2),
            "p05": d.get("p05"), "p25": d.get("p25"),
            "median": d.get("median"), "p75": d.get("p75"),
            "p95": d.get("p95"),
            "tilt_applied_pct": round(shift, 2),
        }
    if trend_ret is not None:
        return {
            "source": "trend",
            "expected_return_pct": round(float(trend_ret), 2),
            "p05": None, "p25": None, "median": round(float(trend_ret), 2),
            "p75": None, "p95": None, "tilt_applied_pct": 0.0,
        }
    return None


def _run_uncached(symbol: str, horizon_days: int) -> Dict[str, Any]:
    t0 = time.time()

    # Run the (independently-cached) engines concurrently. ml_forecast yields
    # four signals from one call; bootstrap and narrative are one each.
    jobs = [
        ("ml", lambda: _sig_ml(symbol)),
        ("bootstrap", lambda: _sig_bootstrap(symbol, horizon_days)),
        ("narrative", lambda: _sig_narrative(symbol)),
    ]
    results = safe_executor.parallel_map(
        lambda job: job[1](), jobs, max_workers=3,
        thread_name_prefix="augur-ensemble",
    )
    by_name = {jobs[i][0]: results[i] for i in range(len(jobs))}

    ml_signals = by_name.get("ml") or {}
    bootstrap = by_name.get("bootstrap")
    narrative = by_name.get("narrative")

    # Assemble the candidate signal list.
    raw_signals: Dict[str, Dict[str, Any]] = {}
    for key in ("rf_classifier", "ml_composite", "trend", "mean_reversion"):
        if ml_signals.get(key):
            raw_signals[key] = ml_signals[key]
    if bootstrap:
        raw_signals["bootstrap"] = bootstrap
    if narrative:
        raw_signals["narrative"] = narrative

    labels = {
        "rf_classifier": "RandomForest Classifier",
        "ml_composite": "ML Composite",
        "trend": "Trend Regression",
        "mean_reversion": "Mean Reversion",
        "bootstrap": "Bootstrap Distribution",
        "narrative": "Narrative Phase",
    }

    if not raw_signals:
        return {
            "symbol": symbol, "horizon_days": horizon_days,
            "as_of": _now_iso(), "n_signals": 0,
            "error": "No forecasting signals available for this symbol.",
            "signals": [], "ensemble": None,
        }

    # Renormalize weights over the available signals.
    total_w = sum(_BASE_WEIGHTS.get(k, 0.1) for k in raw_signals)
    weighted_sum = 0.0
    signal_rows: List[Dict[str, Any]] = []
    for key, sig in raw_signals.items():
        w = _BASE_WEIGHTS.get(key, 0.1) / total_w
        p = float(sig["prob_up"])
        weighted_sum += p * w
        signal_rows.append({
            "key": key,
            "name": labels.get(key, key),
            "prob_up": round(p, 4),
            "weight": round(w, 4),
            "expected_return_pct": sig.get("expected_return_pct"),
            "detail": sig.get("detail", ""),
        })

    prob_up_raw = _clamp(weighted_sum)

    # Weighted disagreement = sqrt(weighted variance of per-signal probs).
    var = 0.0
    for row in signal_rows:
        var += row["weight"] * (row["prob_up"] - prob_up_raw) ** 2
    disagreement = math.sqrt(max(0.0, var))
    consensus = _clamp(1.0 - disagreement / _MAX_DISAGREEMENT)

    # CALIBRATION: shrink the edge toward 0.5 by how much the signals split.
    # Keep at least 35% of the raw edge even under total disagreement, so a
    # unanimous-direction-but-noisy set still leans correctly.
    keep = 0.35 + 0.65 * consensus
    prob_up_cal = _clamp(0.5 + (prob_up_raw - 0.5) * keep)
    edge = prob_up_cal - 0.5

    conviction = _conviction(edge, consensus, len(signal_rows))
    direction = "UP" if prob_up_cal > 0.52 else "DOWN" if prob_up_cal < 0.48 else "NEUTRAL"
    verdict = _verdict(prob_up_cal, conviction)

    # Annotate each signal as agreeing/disagreeing with the ensemble.
    for row in signal_rows:
        s_dir = "UP" if row["prob_up"] > 0.52 else "DOWN" if row["prob_up"] < 0.48 else "NEUTRAL"
        row["agrees"] = (s_dir == direction) if direction != "NEUTRAL" else None
        row["direction"] = s_dir
    signal_rows.sort(key=lambda r: r["weight"], reverse=True)

    trend_ret = (ml_signals.get("trend") or {}).get("expected_return_pct")
    cone = _build_return_cone(bootstrap, trend_ret, tilt=edge)

    notes: List[str] = []
    n_agree = sum(1 for r in signal_rows if r.get("agrees") is True)
    n_disagree = sum(1 for r in signal_rows if r.get("agrees") is False)
    if disagreement >= _MAX_DISAGREEMENT * 0.8:
        notes.append(
            "Signals strongly disagree ({} vs {}); edge shrunk toward neutral."
            .format(n_agree, n_disagree))
    elif consensus >= 0.8 and conviction in ("HIGH", "MODERATE"):
        notes.append("Broad signal agreement reinforces the {} call.".format(direction))
    if len(signal_rows) < 3:
        notes.append("Thin signal coverage ({}); treat conviction cautiously."
                     .format(len(signal_rows)))
    if bootstrap is None:
        notes.append("No bootstrap distribution — return cone is a point trend estimate.")

    return {
        "symbol": symbol,
        "horizon_days": horizon_days,
        "as_of": _now_iso(),
        "n_signals": len(signal_rows),
        "ensemble": {
            "prob_up": round(prob_up_cal, 4),
            "prob_up_raw": round(prob_up_raw, 4),
            "direction": direction,
            "edge_pct_pts": round(edge * 100, 1),
            "conviction": conviction,
            "consensus": round(consensus, 3),
            "disagreement": round(disagreement, 4),
            "agree_count": n_agree,
            "disagree_count": n_disagree,
            "verdict": verdict,
            "return_cone": cone,
        },
        "signals": signal_rows,
        "notes": notes,
        "computed_in_ms": round((time.time() - t0) * 1000),
    }


def ensemble_forecast(symbol: str, horizon_days: int = 20) -> Dict[str, Any]:
    """
    Public entry point. Fuse all available forecasting signals for `symbol`
    into one calibrated directional probability + return cone.

    Returns a dict (see _run_uncached for the schema). Never raises — on
    catastrophic failure returns {"symbol", "error", ...}.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"symbol": "", "error": "symbol required", "ensemble": None,
                "signals": [], "n_signals": 0}
    horizon_days = int(max(1, min(int(horizon_days or 20), 252)))

    def _do():
        return _run_uncached(symbol, horizon_days)

    if cache_store is not None:
        key = ("forecast_ensemble", symbol, horizon_days)
        try:
            return cache_store.coalesce(key, _CACHE_TTL, _do)
        except Exception:  # pragma: no cover
            log.exception("cache_store.coalesce failed; running uncached")
    return _do()


if __name__ == "__main__":  # manual smoke test
    import json
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(json.dumps(ensemble_forecast(sym), indent=2, default=str))
