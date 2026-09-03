"""AJTA — momentum / mean-reversion signal primitives (pure functions).

The SINGLE source of the two complementary entry signals + the regime switch
between them. Both the replay engine (aj_replay's deterministic forecasters)
and the live pipeline call these, so the signal that gets validated in the
Replay Lab is byte-identical to the one that trades — no train/serve drift.

Validated finding (6 walk-forward folds, 40 large-caps, 2022-2025):
  * MOMENTUM (buy strength) wins in strong trends and bear markets — it
    avoids buying dips that keep dipping (2022 H2 bear, 2023 H1 recovery).
  * MEAN-REVERSION (buy the dip on a name in a healthy longer-term trend)
    wins in choppy / range-bound tape with a far steadier win rate (67-83%
    vs 15-38%), and loses less in drawdowns.
  * They are COMPLEMENTARY, not competitors: a regime SWITCH (momentum when
    trending/bear, mean-reversion when chop) is what the evidence supports —
    won Sharpe in more folds than either pure signal.

Every function is pure (no I/O), deterministic, and Python 3.9 compatible.
Output is a prob_up in (0,1); callers wrap it into the ensemble shape.
"""
from __future__ import annotations

import math
from typing import List, Optional

# Minimum bars each signal needs (mean-reversion looks back 60d for the trend
# gate, so it needs more history than momentum).
_MIN_MOMENTUM_BARS = 40
_MIN_MEANREV_BARS = 65


def _clean(closes: List[float]) -> List[float]:
    out = []
    for c in closes or []:
        try:
            v = float(c)
        except (TypeError, ValueError):
            continue
        if v == v and v > 0:      # drop NaN / non-positive
            out.append(v)
    return out


def _vol(px: List[float], lookback: int = 60) -> float:
    rets = [px[i] / px[i - 1] - 1.0 for i in range(max(1, len(px) - lookback), len(px))]
    if not rets:
        return 0.01
    mu = sum(rets) / len(rets)
    v = (sum((r - mu) ** 2 for r in rets) / max(1, len(rets) - 1)) ** 0.5
    return v or 0.01


def _rsi14(px: List[float]) -> float:
    deltas = [px[i] - px[i - 1] for i in range(len(px) - 14, len(px))]
    gains = sum(d for d in deltas if d > 0)
    losses = sum(-d for d in deltas if d < 0)
    return 100.0 * gains / (gains + losses) if (gains + losses) > 0 else 50.0


def momentum_prob(closes: List[float]) -> Optional[float]:
    """Buy STRENGTH: rising 20d/5d return, RSI above midline, price above the
    20d SMA -> higher prob_up. None on insufficient history. Identical to the
    replay's validated momentum forecaster."""
    px = _clean(closes)
    if len(px) < _MIN_MOMENTUM_BARS:
        return None
    r5 = px[-1] / px[-6] - 1.0
    r20 = px[-1] / px[-21] - 1.0
    rsi = _rsi14(px)
    sma20 = sum(px[-20:]) / 20.0
    sma_dist = (px[-1] / sma20 - 1.0) if sma20 > 0 else 0.0
    vol = _vol(px)
    z = (0.5 * (r20 / (vol * (20 ** 0.5))) + 0.3 * (r5 / (vol * (5 ** 0.5)))
         + 0.2 * ((rsi - 50.0) / 20.0) + 0.15 * (sma_dist / (vol * (20 ** 0.5))))
    return min(0.95, max(0.05, 1.0 / (1.0 + math.exp(-1.2 * z))))


def meanrev_prob(closes: List[float]) -> Optional[float]:
    """Buy the DIP on quality: short-term oversold (low 14d RSI, below the 20d
    SMA, recent 5d drop) -> higher prob_up, but ONLY when the 60d trend is
    still positive (don't catch a falling knife). None on insufficient
    history. Identical to the replay's validated mean-reversion forecaster."""
    px = _clean(closes)
    if len(px) < _MIN_MEANREV_BARS:
        return None
    r5 = px[-1] / px[-6] - 1.0
    r60 = px[-1] / px[-61] - 1.0
    rsi = _rsi14(px)
    sma20 = sum(px[-20:]) / 20.0
    sma_dist = (px[-1] / sma20 - 1.0) if sma20 > 0 else 0.0
    vol = _vol(px)
    oversold = (0.4 * ((50.0 - rsi) / 20.0)
                + 0.35 * (-sma_dist / (vol * (20 ** 0.5)))
                + 0.25 * (-r5 / (vol * (5 ** 0.5))))
    trend_ok = 1.0 if r60 > 0 else 0.2       # gate the dip on a healthy uptrend
    z = oversold * trend_ok
    return min(0.95, max(0.05, 1.0 / (1.0 + math.exp(-1.2 * z))))


def blended_prob(closes: List[float], regime: str) -> Optional[float]:
    """The regime SWITCH the 6-fold validation supports: mean-reversion in
    'chop', momentum otherwise ('bull'/'bear'/unknown). Falls back to the
    other signal if the preferred one lacks history. This is the deployable
    dual-signal logic — one function so replay and live can't diverge."""
    if str(regime).lower() == "chop":
        p = meanrev_prob(closes)
        return p if p is not None else momentum_prob(closes)
    p = momentum_prob(closes)
    return p if p is not None else meanrev_prob(closes)


def prob_to_ensemble(prob_up: Optional[float], source: str) -> dict:
    """Wrap a prob_up into the forecast_ensemble output shape callers expect."""
    if prob_up is None:
        return {"ensemble": None, "signals": [], "n_signals": 0,
                "error": "insufficient history"}
    edge = (prob_up - 0.5) * 100.0
    conviction = ("high" if abs(edge) >= 12 else
                  "medium" if abs(edge) >= 6 else "low")
    return {"ensemble": {"prob_up": round(prob_up, 4),
                         "edge_pct_pts": round(edge, 3), "conviction": conviction},
            "signals": [{"name": source, "prob_up": prob_up}], "n_signals": 1}
