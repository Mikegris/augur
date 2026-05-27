"""synth_consensus.py — Cross-Source Consensus Score for AUGUR.

A single 0..100 bullish/bearish score per symbol that folds EVERY signal
AUGUR collects into one number, with a contributor-level drill-down.

Design summary
--------------
For each input source we collect a raw value, map it to ``normalized`` in
[-1, +1] (-1 = max bearish, +1 = max bullish), and combine via a weighted
average:

    weighted_sum = Σ w_i · n_i
    sum_w        = Σ w_i              (only contributors that produced a value)

If a contributor's live track record (via :mod:`research_tracker`) has
``n >= 20`` directional calls, its static weight is scaled by
``hit_rate / 0.5`` clipped to [0.5x, 2x] so persistent winners get more
voice and persistent losers get demoted but never silenced.

The final score is the weighted average squashed through a logistic-ish
transform to a 0..100 scale: ``50 + 50 · tanh(weighted_avg · 2)``.  This
keeps the score from clipping aggressively at ±1 and gives the middle of
the distribution good dynamic range.

Each contributor that errors or returns no usable data ends up in the
``missing`` list, never crashing the whole call.

API
---
:func:`consensus_score` ``(symbol) -> dict``

    {
      "symbol", "score", "label", "n_contributors",
      "contributors": [
          {"name", "value", "normalized", "weight",
           "hit_rate", "n_calls", "static_weight"}, ...
      ],
      "missing": [...],
      "as_of": "...ISO...",
      "computed_in_ms": ...,
    }

Cached via ``cache_store.coalesce`` with a 10-minute TTL.

Python 3.9 compatible.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Soft dependencies — every import is guarded.  Missing one just removes
# that contributor from the score; the rest still works.
# ---------------------------------------------------------------------------
try:
    import cache_store  # type: ignore
except Exception:  # pragma: no cover
    cache_store = None

try:
    import fetcher  # type: ignore
except Exception:  # pragma: no cover
    fetcher = None

try:
    import ml_forecast  # type: ignore
except Exception:  # pragma: no cover
    ml_forecast = None

try:
    import smart_money  # type: ignore
except Exception:  # pragma: no cover
    smart_money = None

try:
    import gex_engine  # type: ignore
except Exception:  # pragma: no cover
    gex_engine = None

try:
    import narrative_engine  # type: ignore
except Exception:  # pragma: no cover
    narrative_engine = None

try:
    import sec_edgar  # type: ignore
except Exception:  # pragma: no cover
    sec_edgar = None

try:
    import congress  # type: ignore
except Exception:  # pragma: no cover
    congress = None

try:
    import research_factors  # type: ignore
except Exception:  # pragma: no cover
    research_factors = None

try:
    import alt_signals  # type: ignore
except Exception:  # pragma: no cover
    alt_signals = None

try:
    import reflexivity_detector  # type: ignore
except Exception:  # pragma: no cover
    reflexivity_detector = None

try:
    import research_tracker  # type: ignore
except Exception:  # pragma: no cover
    research_tracker = None


# ---------------------------------------------------------------------------
# Static weights & helpers
# ---------------------------------------------------------------------------

STATIC_WEIGHTS: Dict[str, float] = {
    "ml_forecast_20d":  0.15,
    "smart_money":      0.20,
    "gex_regime":       0.05,
    "narrative_phase":  0.08,
    "insider_form4_net":0.10,
    "congress_net_60d": 0.05,
    "factor_alpha":     0.08,
    "alt_signals_avg":  0.05,
    "reflexivity":      0.04,
    "price_momentum":   0.20,
}

# Map signal_name in synth -> signal_name registered in research_tracker
# (only signals that the existing tracker logs are mapped; the rest fall
# back to their static weight).
_TRACKER_NAMES: Dict[str, str] = {
    "ml_forecast_20d":  "ml_forecast",
    "smart_money":      "smart_money",
    "gex_regime":       "gex",
    "narrative_phase":  "narrative",
}

_CACHE_TTL = 600  # seconds


def _clip(x: float, lo: float, hi: float) -> float:
    # None means "no signal" — return the midpoint of [lo, hi] (neutral),
    # not `lo`, which would otherwise bias the consensus aggregate maximally
    # bearish whenever a defensive caller forwarded a None through.
    if x is None:
        return (lo + hi) / 2.0
    return max(lo, min(hi, x))


def _label_for_score(score: float) -> str:
    """Map 0..100 score onto a 5-bucket label, by quintiles of the scale."""
    if score >= 80:
        return "VERY BULLISH"
    if score >= 60:
        return "BULLISH"
    if score >= 40:
        return "NEUTRAL"
    if score >= 20:
        return "BEARISH"
    return "VERY BEARISH"


def _tanh(x: float) -> float:
    # math.tanh is fine in 3.9 but be defensive against extreme inputs.
    try:
        return math.tanh(x)
    except Exception:
        return 0.0


def _dynamic_weight(name: str, static_w: float) -> Tuple[float, Optional[float], int]:
    """Look up track record. Returns (weight, hit_rate_or_None, n_calls).

    Override rule: if n_directional >= 20, weight = static * (hit_rate/0.5)
    clipped to [0.5*static, 2*static]. Otherwise return static unchanged.
    """
    if research_tracker is None:
        return static_w, None, 0
    tracker_name = _TRACKER_NAMES.get(name)
    if not tracker_name:
        return static_w, None, 0
    try:
        rec = research_tracker.get_track_record(tracker_name)
    except Exception as e:
        log.debug("tracker lookup failed for %s: %s", tracker_name, e)
        return static_w, None, 0
    n = int(rec.get("n_directional") or 0)
    hit = rec.get("hit_rate")
    if hit is None or n < 20:
        return static_w, hit, n
    try:
        ratio = float(hit) / 0.5
    except Exception:
        return static_w, hit, n
    ratio = _clip(ratio, 0.5, 2.0)
    return static_w * ratio, float(hit), n


# ---------------------------------------------------------------------------
# Contributor functions — each returns (value, normalized) or None.
#
# Convention: anything that returns None means "no data available"; the
# contributor is appended to ``missing`` and contributes neither weight
# nor score to the average.
# ---------------------------------------------------------------------------

def _c_ml_forecast(symbol: str) -> Optional[Tuple[Any, float]]:
    if ml_forecast is None:
        return None
    try:
        res = ml_forecast.ml_forecast(symbol)
    except Exception as e:
        log.debug("ml_forecast failed for %s: %s", symbol, e)
        return None
    if not isinstance(res, dict) or res.get("error"):
        return None
    rf = res.get("rf_classifier") or {}
    p = rf.get("prob_up_20d")
    if p is None:
        return None
    try:
        p = float(p)
    except Exception:
        return None
    # Map probability in [0, 1] to [-1, +1] linearly, then a small dead-zone
    # to keep neutral signals from dominating.
    n = (p - 0.5) * 2.0
    return round(p, 4), _clip(n, -1.0, 1.0)


def _c_smart_money(symbol: str) -> Optional[Tuple[Any, float]]:
    if smart_money is None:
        return None
    fn = getattr(smart_money, "compute_score", None)
    if not callable(fn):
        return None
    try:
        res = fn(symbol)
    except Exception as e:
        log.debug("smart_money.compute_score failed for %s: %s", symbol, e)
        return None
    if not isinstance(res, dict) or res.get("error"):
        return None
    score = res.get("score")
    if score is None:
        return None
    try:
        score = float(score)
    except Exception:
        return None
    # 0..100 → -1..+1 centered at 50
    n = (score - 50.0) / 50.0
    return int(score), _clip(n, -1.0, 1.0)


def _c_gex_regime(symbol: str) -> Optional[Tuple[Any, float]]:
    if gex_engine is None:
        return None
    fn = getattr(gex_engine, "get_gex_summary", None) or getattr(gex_engine, "compute_gex", None)
    if not callable(fn):
        return None
    try:
        res = fn(symbol)
    except Exception as e:
        log.debug("gex_engine failed for %s: %s", symbol, e)
        return None
    if not isinstance(res, dict) or res.get("error"):
        return None
    regime = res.get("gamma_regime") or res.get("regime")
    if not regime:
        return None
    r = str(regime).upper()
    # LONG GAMMA → dampened, pinning, usually a bullish stability tilt.
    # SHORT GAMMA → reflexive/trending; we call that mildly bearish on the
    # assumption that flow demand from dealer hedging amplifies down moves.
    # NEUTRAL → 0.
    if "LONG" in r or "POSITIVE" in r:
        n = +0.3
    elif "SHORT" in r or "NEGATIVE" in r:
        n = -0.3
    else:
        n = 0.0
    return regime, n


def _c_narrative(symbol: str) -> Optional[Tuple[Any, float]]:
    if narrative_engine is None:
        return None
    fn = getattr(narrative_engine, "analyze_narrative", None)
    if not callable(fn):
        return None
    try:
        res = fn(symbol)
    except Exception as e:
        log.debug("narrative_engine failed for %s: %s", symbol, e)
        return None
    if not isinstance(res, dict) or res.get("error"):
        return None
    phase = res.get("narrative_phase") or res.get("phase")
    velocity = res.get("velocity_score")
    if not phase:
        return None
    phase = str(phase).upper()
    # Phase scoring: ACCELERATION/EMERGENCE = bullish breakout, REVERSAL
    # = contrarian buy on capitulation, CONSENSUS/EXHAUSTION = crowded
    # contrarian SELL. DEVELOPING = neutral.
    PHASE_MAP = {
        "ACCELERATION": +0.5,
        "EMERGENCE":    +0.3,
        "BREAKOUT":     +0.5,
        "REVERSAL":     +0.2,
        "DEVELOPING":   0.0,
        "CONSENSUS":   -0.3,
        "EXHAUSTION":  -0.5,
    }
    n = PHASE_MAP.get(phase, 0.0)
    # Bend by velocity if available (clip ±0.2 contribution)
    try:
        v = float(velocity) if velocity is not None else 0.0
        n = _clip(n + max(-0.2, min(0.2, v / 100.0)), -1.0, 1.0)
    except Exception:
        pass
    return phase, n


def _c_insider_form4(symbol: str) -> Optional[Tuple[Any, float]]:
    if sec_edgar is None:
        return None
    fn = getattr(sec_edgar, "get_form4_transactions", None)
    if not callable(fn):
        return None
    try:
        txns = fn(symbol, limit=20)
    except Exception as e:
        log.debug("sec_edgar.get_form4_transactions failed for %s: %s", symbol, e)
        return None
    if not isinstance(txns, list) or not txns:
        return None
    buy_val = 0.0
    sell_val = 0.0
    for t in txns:
        if not isinstance(t, dict):
            continue
        try:
            v = float(t.get("value") or 0.0)
        except Exception:
            v = 0.0
        ttype = (t.get("transaction_type") or "").upper()
        if ttype == "BUY":
            buy_val += v
        elif ttype == "SELL":
            sell_val += v
    total = buy_val + sell_val
    if total <= 0:
        # No usable signed dollar volume.  Still report 0 to indicate we
        # found filings but no net direction — counts toward n_contributors.
        return ("0.00 ratio", 0.0)
    ratio = (buy_val - sell_val) / total  # -1 = all sells, +1 = all buys
    # ratio is already in [-1, +1]; pass through.
    return round(ratio, 3), _clip(ratio, -1.0, 1.0)


def _c_congress(symbol: str) -> Optional[Tuple[Any, float]]:
    if congress is None:
        return None
    fn = getattr(congress, "get_trades_for_ticker", None)
    if not callable(fn):
        return None
    try:
        trades = fn(symbol, days=60)
    except Exception as e:
        log.debug("congress.get_trades_for_ticker failed for %s: %s", symbol, e)
        return None
    if not isinstance(trades, list) or not trades:
        return None
    # Each trade has txn_type ("Purchase"/"Sale"/etc) and amount_val (estimate).
    # txn_dt comes back as a naive datetime (congress.py strptime'd without
    # tz), so anchor cutoff to UTC and drop tzinfo so the comparison is
    # naive-vs-naive.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=60)
    net = 0.0
    for t in trades:
        if not isinstance(t, dict):
            continue
        # Filter by txn_dt if available
        dt = t.get("txn_dt")
        if isinstance(dt, datetime) and dt < cutoff:
            continue
        if isinstance(dt, str):
            try:
                d = datetime.strptime(dt, "%Y-%m-%d")
                if d < cutoff:
                    continue
            except Exception:
                pass
        try:
            val = float(t.get("amount_val") or 0.0)
        except Exception:
            val = 0.0
        txn = (t.get("txn_type") or "").upper()
        raw = (t.get("txn_type_raw") or "").upper()
        # txn labels via congress.TXN_LABELS: "Buy", "Sell", "Sell (Partial)",
        # "Purchase (Exercise)", "Sale (Exercise)", "Exchange". Raw codes:
        # "P", "S", "PE", "SE", "S (partial)", "S (Exchange)", "E (Exchange)".
        # Use prefix matches so partials/exchanges fall on the right side.
        if (
            "PURCHASE" in txn
            or txn.startswith("BUY")
            or raw == "P"
            or raw.startswith("PE")
        ):
            net += val
        elif (
            "SALE" in txn
            or txn.startswith("SELL")
            or raw.startswith("S")
        ):
            net -= val
    if net == 0:
        return None
    # Saturate at ±$100k as the "full" signal level for retail-sized congresscritter trades.
    n = _clip(net / 100_000.0, -1.0, 1.0)
    return int(net), n


def _c_factor_alpha(symbol: str) -> Optional[Tuple[Any, float]]:
    if research_factors is None:
        return None
    fn = getattr(research_factors, "factor_exposure", None)
    if not callable(fn):
        return None
    try:
        res = fn(symbol, period_years=3)
    except Exception as e:
        log.debug("research_factors.factor_exposure failed for %s: %s", symbol, e)
        return None
    if not isinstance(res, dict) or res.get("error"):
        return None
    alpha = res.get("alpha_annual_pct")
    if alpha is None:
        return None
    try:
        a = float(alpha)
    except Exception:
        return None
    # Saturate alpha at ±15%/yr → ±1.
    n = _clip(a / 15.0, -1.0, 1.0)
    return round(a, 2), n


def _c_alt_signals(symbol: str) -> Optional[Tuple[Any, float]]:
    if alt_signals is None:
        return None
    fn = getattr(alt_signals, "stocktwits_symbol_sentiment", None)
    if not callable(fn):
        return None
    try:
        res = fn(symbol)
    except Exception as e:
        log.debug("alt_signals.stocktwits_symbol_sentiment failed for %s: %s", symbol, e)
        return None
    if not isinstance(res, dict):
        return None
    ratio = res.get("bull_ratio")
    if ratio is None:
        return None
    try:
        r = float(ratio)
    except Exception:
        return None
    # r in [0,1] → [-1, +1]
    n = (r - 0.5) * 2.0
    return round(r, 3), _clip(n, -1.0, 1.0)


def _c_reflexivity(symbol: str) -> Optional[Tuple[Any, float]]:
    if reflexivity_detector is None:
        return None
    fn = getattr(reflexivity_detector, "detect_loops", None)
    if not callable(fn):
        return None
    try:
        res = fn(symbol)
    except Exception as e:
        log.debug("reflexivity_detector.detect_loops failed for %s: %s", symbol, e)
        return None
    if not isinstance(res, dict) or res.get("error"):
        return None
    risk = (res.get("overall_risk") or "").upper()
    dominant = res.get("dominant_loop")
    strength = res.get("max_strength") or 0
    try:
        s = float(strength) / 100.0
    except Exception:
        s = 0.0
    # Loop direction: SHORT_SQUEEZE / INDEX_INCLUSION / EQUITY_FEEDBACK = bullish reflexive,
    # DILUTION_SPIRAL = bearish reflexive. Use loop type + strength.
    bullish_loops = {"SHORT_SQUEEZE", "INDEX_INCLUSION"}
    bearish_loops = {"DILUTION_SPIRAL"}
    if dominant in bullish_loops:
        n = +s
    elif dominant in bearish_loops:
        n = -s
    else:
        # EQUITY_FEEDBACK can go either way — treat as 0; mark MUTED.
        n = 0.0
    value = dominant or risk or "MUTED"
    return value, _clip(n, -1.0, 1.0)


def _c_price_momentum(symbol: str) -> Optional[Tuple[Any, float]]:
    """Price momentum from quote: change_pct over the most recent session
    plus 52-week range positioning.  Cheap fallback signal that always has
    data when get_quote works.
    """
    if fetcher is None:
        return None
    try:
        q = fetcher.get_quote(symbol)
    except Exception as e:
        log.debug("fetcher.get_quote failed for %s: %s", symbol, e)
        return None
    if not isinstance(q, dict) or q.get("error"):
        return None
    pct = q.get("change_pct")
    hi = q.get("fifty_two_week_high")
    lo = q.get("fifty_two_week_low")
    price = q.get("price")
    pieces = []
    try:
        if pct is not None:
            pieces.append(_clip(float(pct) / 5.0, -1.0, 1.0))
    except Exception:
        pass
    try:
        if price is not None and hi is not None and lo is not None and float(hi) > float(lo):
            pos = (float(price) - float(lo)) / (float(hi) - float(lo))  # 0..1
            pieces.append(_clip((pos - 0.5) * 2.0, -1.0, 1.0))
    except Exception:
        pass
    if not pieces:
        return None
    n = sum(pieces) / len(pieces)
    label = "{:+.2f}% / {:.0%} 52w" .format(
        float(pct) if pct is not None else 0.0,
        ((float(price) - float(lo)) / (float(hi) - float(lo))) if (price and hi and lo and float(hi) > float(lo)) else 0.5,
    )
    return label, _clip(n, -1.0, 1.0)


# Ordered list of (name, fn) — keeps output stable.
_CONTRIBUTORS: List[Tuple[str, Any]] = [
    ("ml_forecast_20d",   _c_ml_forecast),
    ("smart_money",       _c_smart_money),
    ("gex_regime",        _c_gex_regime),
    ("narrative_phase",   _c_narrative),
    ("insider_form4_net", _c_insider_form4),
    ("congress_net_60d",  _c_congress),
    ("factor_alpha",      _c_factor_alpha),
    ("alt_signals_avg",   _c_alt_signals),
    ("reflexivity",       _c_reflexivity),
    ("price_momentum",    _c_price_momentum),
]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _compute(symbol: str) -> Dict[str, Any]:
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return {"error": "missing symbol"}

    t0 = time.time()
    contributors: List[Dict[str, Any]] = []
    missing: List[str] = []
    weighted_sum = 0.0
    sum_w = 0.0

    for name, fn in _CONTRIBUTORS:
        static_w = STATIC_WEIGHTS.get(name, 0.05)
        try:
            out = fn(symbol)
        except Exception as e:
            log.debug("contributor %s raised %s", name, e)
            out = None
        if out is None:
            missing.append(name)
            continue
        try:
            value, normalized = out
            normalized = float(normalized)
        except Exception:
            missing.append(name)
            continue
        if normalized != normalized:  # NaN
            missing.append(name)
            continue
        weight, hit_rate, n_calls = _dynamic_weight(name, static_w)
        weighted_sum += weight * normalized
        sum_w += weight
        contributors.append({
            "name": name,
            "value": value,
            "normalized": round(normalized, 4),
            "weight": round(weight, 4),
            "static_weight": round(static_w, 4),
            "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
            "n_calls": int(n_calls),
        })

    if sum_w <= 0 or not contributors:
        return {
            "symbol": symbol,
            "score": None,
            "label": "INSUFFICIENT DATA",
            "n_contributors": 0,
            "contributors": [],
            "missing": missing,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "computed_in_ms": round((time.time() - t0) * 1000),
        }

    avg = weighted_sum / sum_w  # in roughly [-1, +1]
    # Tanh squash gives nice midrange spread; 50 + 50*tanh(2*avg) maps the
    # high-conviction tails (|avg|≈1) to 88..96 and keeps the middle band
    # wide. The factor of 2 was chosen so |avg|=0.5 lands at ~88.
    score = 50.0 + 50.0 * _tanh(avg * 1.6)
    score = round(_clip(score, 0.0, 100.0), 1)

    # Sort contributors by absolute contribution (|weight*normalized|) desc
    # so the UI lists the most-influential signals first.
    contributors.sort(
        key=lambda c: abs((c.get("weight") or 0.0) * (c.get("normalized") or 0.0)),
        reverse=True,
    )

    return {
        "symbol": symbol,
        "score": score,
        "label": _label_for_score(score),
        "n_contributors": len(contributors),
        "contributors": contributors,
        "missing": missing,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "computed_in_ms": round((time.time() - t0) * 1000),
    }


def consensus_score(symbol: str) -> Dict[str, Any]:
    """Top-level: compute (or fetch cached) consensus for ``symbol``."""
    sym = (symbol or "").upper().strip()
    if not sym:
        return {"error": "missing symbol"}
    if cache_store is None:
        return _compute(sym)
    try:
        return cache_store.coalesce(("consensus", sym), _CACHE_TTL, lambda: _compute(sym))
    except Exception as e:
        log.warning("cache_store.coalesce(consensus) failed for %s: %s", sym, e)
        return _compute(sym)


if __name__ == "__main__":  # quick smoke test
    import json
    for s in ("AAPL", "NVDA", "TLT"):
        r = consensus_score(s)
        print(json.dumps({
            "symbol": r.get("symbol"),
            "score": r.get("score"),
            "label": r.get("label"),
            "n_contributors": r.get("n_contributors"),
            "missing": r.get("missing"),
            "top_3": [c["name"] for c in (r.get("contributors") or [])[:3]],
        }, indent=2))
