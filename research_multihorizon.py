"""
research_multihorizon.py — Multi-horizon forecast suite.

Produces 5d / 20d / 60d / 120d directional forecasts simultaneously, each using
horizon-appropriate features. Surfaces when short-term mean-reversion and
long-term trend disagree (a useful signal in itself).

Method per horizon (kept deliberately simple — we don't reinvent the ML stack):
  - h5   short_mom    RSI-14 + 5d ROC + recent vol; biased toward mean reversion
                      when price is stretched.
  - h20  rf_classifier reuses ml_forecast.ml_forecast(symbol)'s
                      ``rf_classifier.prob_up_20d`` so it reflects the trained RF.
  - h60  medium_trend 60d / 200d MA crossover + 60d ROC + 60d vol regime.
  - h120 long_trend   linear regression slope on the last 120 closes + long-term
                      volatility regime.

Output is a single dict (see ``multi_horizon_forecast``) suitable for direct
JSON serialisation by the Flask route. All four horizons populate independently
so a failure in one (e.g. ml_forecast can't fetch news) doesn't poison the rest.

Cache: ``cache_store.coalesce(("multihorizon", symbol), 6*3600, ...)`` —
6h matches the rough cadence at which any of the inputs meaningfully move on a
daily-bar timeframe.

Python 3.9 compatible. No new third-party deps; only numpy / pandas which the
rest of AUGUR already requires.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

log = logging.getLogger("augur.multihorizon")

_CACHE_TTL = 6 * 3600       # 6 hours
_MIN_BARS = 200             # need at least ~200 daily bars to compute MA200


def _phi(x: float) -> float:
    """Standard-normal CDF Φ(x). scipy if available, else math.erf. Returns
    0.5 on non-finite input. Never raises."""
    try:
        xf = float(x)
        if not math.isfinite(xf):
            return 0.5
    except (TypeError, ValueError):
        return 0.5
    try:
        from scipy.stats import norm
        return float(norm.cdf(xf))
    except Exception:
        return 0.5 * (1.0 + math.erf(xf / math.sqrt(2.0)))


def _cdf_prob(expected_move_frac: float, horizon_dispersion: float) -> float:
    """Vol-normalized directional probability P(up) = Φ(E[move]/dispersion).

    `expected_move_frac` is the expected horizon return as a fraction (e.g.
    0.03 = +3%); `horizon_dispersion` is the horizon return std (σ_daily·√h).
    Clamped to [0.05, 0.95]. Degenerate dispersion → neutral 0.5."""
    if horizon_dispersion is None or horizon_dispersion <= 1e-9:
        return 0.5
    return float(min(0.95, max(0.05, _phi(expected_move_frac / horizon_dispersion))))


# ──────────────────────────── data plumbing ───────────────────────────────

def _load_history(symbol: str) -> Optional[pd.DataFrame]:
    """Pull 2y of daily bars via fetcher.get_chart_data and shape into a frame.

    Returns None if insufficient data so the caller can return an error envelope.
    """
    try:
        import fetcher
    except Exception as e:
        log.warning("fetcher import failed: %s", e)
        return None

    try:
        bars = fetcher.get_chart_data(symbol, period="2y", interval="1d")
    except Exception as e:
        log.warning("get_chart_data(%s) failed: %s", symbol, e)
        return None

    if not bars or len(bars) < _MIN_BARS:
        return None

    df = pd.DataFrame(bars)
    if "time" not in df.columns or "close" not in df.columns:
        return None
    df["Date"] = pd.to_datetime(df["time"], unit="s")
    df = df.set_index("Date").rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    df = df[df["Close"] > 0]
    # Sort + de-duplicate timestamps (matches research_backtest._load_history):
    # unordered/duplicate bars would corrupt rolling stats and iloc[-1] reads.
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if len(df) < _MIN_BARS:
        return None
    return df


# ────────────────────────── per-horizon methods ───────────────────────────

def _h5_short_mom(hist: pd.DataFrame) -> Dict[str, Any]:
    """5-day short-momentum / mean-reversion blend.

    The intent is to disagree with the long-term trend when price is stretched.
    Stretched up (high RSI, high recent vol) → bias prob_up *down* (expect snap
    back). Stretched down → bias prob_up up. When unstretched we fall back to a
    mild trend-follower on the 5d ROC.
    """
    close = hist["Close"]
    if len(close) < 30:
        return _bail("h5", 5, "short_mom", "insufficient bars")

    # RSI-14
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])
    if not math.isfinite(rsi):
        rsi = 50.0

    # 5d ROC
    roc5 = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) > 6 else 0.0

    # Recent realized vol (5d std of daily returns)
    rets = close.pct_change()
    vol5 = float(rets.iloc[-5:].std()) if len(rets) >= 5 else 0.0
    vol60 = float(rets.iloc[-60:].std()) if len(rets) >= 60 else (vol5 if vol5 > 0 else 0.01)
    vol_ratio = (vol5 / vol60) if vol60 > 1e-9 else 1.0

    # Stretch score: -1 (deep oversold) … +1 (overbought)
    stretch = (rsi - 50.0) / 50.0  # [-1, 1]

    # Horizon dispersion = daily-vol × √5 (the 5-day return std).
    dispersion5 = max(vol5, 0.005) * math.sqrt(5)

    # A5: build an EXPECTED 5-day MOVE (a real return), then map it to a
    # probability via the normal CDF — instead of fabricating prob_up from a
    # hand-tuned 0.25 gain. The minus sign is the whole point of this horizon:
    # a stretched-up RSI implies an expected pull-BACK (mean reversion). The
    # move is sized as a fraction of the 5-day dispersion so it's vol-aware.
    expected_move = -0.6 * stretch * dispersion5
    # Mild 5d-ROC momentum tilt when not stretched (|stretch| < 0.4).
    if abs(stretch) < 0.4:
        expected_move += float(np.clip(roc5, -0.05, 0.05)) * 0.5

    prob_up = _cdf_prob(expected_move, dispersion5)

    # Expected return in % over the horizon (the actual move that drives prob_up).
    expected_pct = expected_move * 100.0

    # Confidence: stronger when both stretch and vol agree
    confidence = float(np.clip(
        abs(prob_up - 0.5) * 2.0 * (1.0 / (1.0 + max(vol_ratio - 1.0, 0))),
        0.05, 0.95
    ))

    return {
        "horizon_days": 5,
        "prob_up": round(prob_up, 3),
        "expected_return_pct": round(expected_pct, 2),
        "confidence": round(confidence, 3),
        "method": "short_mom",
        "features": {
            "rsi_14": round(rsi, 1),
            "roc_5d_pct": round(roc5 * 100, 2),
            "vol_5d": round(vol5, 4),
            "vol_60d": round(vol60, 4),
            "vol_ratio": round(vol_ratio, 2),
            "stretch": round(stretch, 3),
        },
    }


def _h20_rf(symbol: str, hist: pd.DataFrame) -> Dict[str, Any]:
    """20-day horizon: defer to ml_forecast's trained RF classifier.

    We call ml_forecast.ml_forecast(symbol) which has its own cache; we do NOT
    modify that module. If the RF model isn't available (e.g. < 120 samples)
    we degrade gracefully to a momentum-based estimate so the panel still shows
    something.
    """
    try:
        import ml_forecast
    except Exception as e:
        log.warning("ml_forecast import failed: %s", e)
        return _h20_fallback(hist)

    try:
        mlf = ml_forecast.ml_forecast(symbol)
    except Exception as e:
        log.warning("ml_forecast(%s) failed: %s", symbol, e)
        return _h20_fallback(hist)

    rf = (mlf or {}).get("rf_classifier")
    if not rf or rf.get("prob_up_20d") is None:
        return _h20_fallback(hist)

    prob_up = float(rf["prob_up_20d"])
    accuracy = rf.get("accuracy_recent")
    # Trend forecast's blended 30d % — closest thing to a 20d expected return.
    trend = (mlf or {}).get("trend_forecast") or {}
    forecast_pct = trend.get("forecast_pct")
    if forecast_pct is None:
        # Estimate via vol * direction
        rets = hist["Close"].pct_change().dropna()
        vol20 = float(rets.iloc[-20:].std()) if len(rets) >= 20 else 0.01
        expected_pct = (prob_up - 0.5) * 2.0 * max(vol20, 0.005) * math.sqrt(20) * 100.0
    else:
        # Scale 30d -> 20d
        expected_pct = float(forecast_pct) * (20.0 / 30.0)

    # Confidence: blend prob distance from 0.5 with the model's walk-forward accuracy.
    base_conf = abs(prob_up - 0.5) * 2.0
    if accuracy is not None:
        confidence = float(np.clip(0.6 * base_conf + 0.4 * float(accuracy), 0.05, 0.95))
    else:
        confidence = float(np.clip(base_conf, 0.05, 0.95))

    return {
        "horizon_days": 20,
        "prob_up": round(prob_up, 3),
        "expected_return_pct": round(expected_pct, 2),
        "confidence": round(confidence, 3),
        "method": "rf_classifier",
        "features": {
            "rf_accuracy_recent": accuracy,
            "rf_train_samples": rf.get("train_samples"),
            "trend_forecast_pct_30d": forecast_pct,
            "rf_signal": rf.get("signal"),
        },
    }


def _h20_fallback(hist: pd.DataFrame) -> Dict[str, Any]:
    """Lightweight 20d estimate when ml_forecast can't produce a number."""
    close = hist["Close"]
    if len(close) < 60:
        return _bail("h20", 20, "rf_classifier", "ml_forecast unavailable")
    rets = close.pct_change().dropna()
    vol20 = float(rets.iloc[-20:].std()) if len(rets) >= 20 else 0.01
    roc20 = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) >= 21 else 0.0
    # Momentum tilt
    prob_up = float(np.clip(0.5 + np.tanh(roc20 * 5.0) * 0.2, 0.05, 0.95))
    expected_pct = (prob_up - 0.5) * 2.0 * max(vol20, 0.005) * math.sqrt(20) * 100.0
    confidence = float(np.clip(abs(prob_up - 0.5) * 2.0, 0.05, 0.6))
    return {
        "horizon_days": 20,
        "prob_up": round(prob_up, 3),
        "expected_return_pct": round(expected_pct, 2),
        "confidence": round(confidence, 3),
        "method": "rf_classifier_fallback",
        "features": {
            "roc_20d_pct": round(roc20 * 100, 2),
            "vol_20d": round(vol20, 4),
            "note": "ml_forecast unavailable; momentum estimate",
        },
    }


def _h60_medium_trend(hist: pd.DataFrame) -> Dict[str, Any]:
    """60d horizon: MA60 vs MA200 crossover + 60d ROC + vol regime."""
    close = hist["Close"]
    if len(close) < 200:
        return _bail("h60", 60, "medium_trend", "need 200+ bars")

    ma60 = float(close.rolling(60).mean().iloc[-1])
    ma200 = float(close.rolling(200).mean().iloc[-1])
    cross = (ma60 / ma200 - 1.0) if ma200 > 0 else 0.0  # >0 = golden, <0 = death

    roc60 = float(close.iloc[-1] / close.iloc[-61] - 1) if len(close) >= 61 else 0.0

    rets = close.pct_change().dropna()
    vol60 = float(rets.iloc[-60:].std()) if len(rets) >= 60 else 0.01
    vol200 = float(rets.iloc[-200:].std()) if len(rets) >= 200 else vol60 or 0.01
    vol_regime = vol60 / vol200 if vol200 > 1e-9 else 1.0  # >1 = expanding

    # A5: build an EXPECTED 60-day MOVE from the trend signals, then map it to
    # a probability via the normal CDF (vol-normalized) instead of a hand-tuned
    # tanh→0.25 gain. The crossover dominates direction; the 60d ROC adds a
    # momentum component. Both are already returns, so they combine directly.
    dispersion60 = max(vol60, 0.005) * math.sqrt(60)
    # Damp the raw signals toward a horizon-scaled expectation: a golden cross
    # of size `cross` and recent momentum `roc60` jointly imply continuation.
    expected_move = 0.6 * cross + 0.4 * roc60
    prob_up = _cdf_prob(expected_move, dispersion60)

    expected_pct = expected_move * 100.0

    base_conf = abs(prob_up - 0.5) * 2.0
    # Tax high vol_regime; reward when crossover and ROC agree in sign.
    sign_agree = 1.0 if (cross * roc60) > 0 else 0.7
    confidence = float(np.clip(base_conf * sign_agree / max(vol_regime, 0.7), 0.05, 0.95))

    return {
        "horizon_days": 60,
        "prob_up": round(prob_up, 3),
        "expected_return_pct": round(expected_pct, 2),
        "confidence": round(confidence, 3),
        "method": "medium_trend",
        "features": {
            "ma60": round(ma60, 2),
            "ma200": round(ma200, 2),
            "ma60_vs_ma200_pct": round(cross * 100, 2),
            "roc_60d_pct": round(roc60 * 100, 2),
            "vol_60d": round(vol60, 4),
            "vol_regime": round(vol_regime, 2),
        },
    }


def _h120_long_trend(hist: pd.DataFrame) -> Dict[str, Any]:
    """120d horizon: linear regression on log-prices of last 120 closes."""
    close = hist["Close"]
    if len(close) < 120:
        return _bail("h120", 120, "long_trend", "need 120+ bars")

    y = np.log(close.iloc[-120:].values)
    x = np.arange(len(y))
    try:
        slope, intercept = np.polyfit(x, y, 1)
    except Exception as e:
        return _bail("h120", 120, "long_trend", f"polyfit error: {e}")

    fitted = np.polyval([slope, intercept], x)
    residuals = y - fitted
    res_std = float(np.std(residuals))  # log-space residual std
    # R^2 — how well does the line fit?
    ss_res = float(np.sum(residuals ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0

    # Forward 120d return (in pct, log-space then re-exp)
    forward_log = slope * 120.0
    expected_pct = float((math.exp(forward_log) - 1.0) * 100.0)

    rets = close.pct_change().dropna()
    vol120 = float(rets.iloc[-120:].std()) if len(rets) >= 120 else 0.02
    vol250 = float(rets.iloc[-250:].std()) if len(rets) >= 250 else vol120 or 0.02
    vol_regime_long = vol120 / vol250 if vol250 > 1e-9 else 1.0

    # A5: vol-normalized normal-CDF probability from the regression's expected
    # 120d move and its dispersion, instead of a hand-tuned tanh(slope*800)*0.4
    # gain. `expected_pct` (computed above) IS the model's expected 120d return;
    # express it as a fraction and divide by the 120d return std. R² scales the
    # *signal strength*: a noisy upward drift (low R²) shouldn't claim a strong
    # edge, so we shrink the expected move toward 0 by R².
    dispersion120 = max(vol120, 0.005) * math.sqrt(120)
    expected_move120 = (expected_pct / 100.0) * max(0.2, r2)
    prob_up = _cdf_prob(expected_move120, dispersion120)

    # Confidence: prob distance × R^2 × vol-regime moderation
    confidence = float(np.clip(
        abs(prob_up - 0.5) * 2.0 * (0.4 + 0.6 * r2) / max(vol_regime_long, 0.7),
        0.05, 0.95
    ))

    return {
        "horizon_days": 120,
        "prob_up": round(prob_up, 3),
        "expected_return_pct": round(expected_pct, 2),
        "confidence": round(confidence, 3),
        "method": "long_trend",
        "features": {
            "slope_daily_log": round(float(slope), 6),
            "ann_return_pct": round((math.exp(slope * 252) - 1.0) * 100.0, 1),
            "r_squared": round(r2, 3),
            "residual_std_log": round(res_std, 4),
            "vol_120d": round(vol120, 4),
            "vol_regime_long": round(vol_regime_long, 2),
        },
    }


def _bail(key: str, hd: int, method: str, reason: str) -> Dict[str, Any]:
    """Return a neutral placeholder when a horizon's inputs are missing.

    Using 0.5 (perfectly neutral) means an unavailable horizon won't drag the
    consensus toward either direction; ``confidence`` of 0 keeps it out of
    divergence detection too."""
    return {
        "horizon_days": hd,
        "prob_up": 0.5,
        "expected_return_pct": 0.0,
        "confidence": 0.0,
        "method": method,
        "features": {"error": reason},
    }


# ─────────────────────────── consensus + divergence ───────────────────────

def _consensus(horizons: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Summarise agreement across horizons.

    direction: UP / DOWN / MIXED based on majority-with-confidence weighting.
    agreement_score: fraction of horizons that agree with the majority,
                     weighted by confidence so a confident dissenter pulls
                     this lower than a low-confidence one.
    """
    items = [h for h in horizons.values() if h.get("confidence", 0) > 0]
    if not items:
        return {"direction": "UNKNOWN", "agreement_score": 0.0}

    # Bullish / bearish weight in [0, sum(confidences)]
    bull_w = sum(h["confidence"] for h in items if h["prob_up"] > 0.5)
    bear_w = sum(h["confidence"] for h in items if h["prob_up"] < 0.5)
    total = bull_w + bear_w

    if total <= 0:
        return {"direction": "NEUTRAL", "agreement_score": 0.0}

    if bull_w > bear_w * 1.2:
        direction = "UP"
        agreement_score = bull_w / total
    elif bear_w > bull_w * 1.2:
        direction = "DOWN"
        agreement_score = bear_w / total
    else:
        direction = "MIXED"
        agreement_score = max(bull_w, bear_w) / total

    return {
        "direction": direction,
        "agreement_score": round(float(agreement_score), 3),
    }


def _divergence(horizons: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Flag short-vs-long disagreement.

    Returns one of:
      - "SHORT_BEARISH_LONG_BULLISH" when h5 prob_up < 0.4 and h120 prob_up > 0.6
      - "SHORT_BULLISH_LONG_BEARISH" inverse
      - None otherwise
    """
    h5 = horizons.get("h5") or {}
    h120 = horizons.get("h120") or {}
    p5 = h5.get("prob_up", 0.5)
    p120 = h120.get("prob_up", 0.5)
    c5 = h5.get("confidence", 0.0)
    c120 = h120.get("confidence", 0.0)

    # Require minimum confidence in both ends to flag a divergence — otherwise
    # neutral-by-default placeholders would trigger false positives.
    if c5 < 0.1 or c120 < 0.1:
        return None

    if p5 < 0.4 and p120 > 0.6:
        return "SHORT_BEARISH_LONG_BULLISH"
    if p5 > 0.6 and p120 < 0.4:
        return "SHORT_BULLISH_LONG_BEARISH"
    return None


# ─────────────────────────── public API ───────────────────────────────────

def _compute(symbol: str) -> Dict[str, Any]:
    """The actual work; wrapped by multi_horizon_forecast() with caching."""
    hist = _load_history(symbol)
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if hist is None:
        return {
            "symbol": symbol,
            "as_of": as_of,
            "error": "Insufficient history (need 200+ daily bars)",
            "horizons": {},
            "consensus": {"direction": "UNKNOWN", "agreement_score": 0.0},
            "divergence_flag": None,
        }

    horizons: Dict[str, Dict[str, Any]] = {}
    # Each horizon is wrapped so one failure can't poison the rest.
    try:
        horizons["h5"] = _h5_short_mom(hist)
    except Exception as e:
        log.warning("h5 failed for %s: %s", symbol, e)
        horizons["h5"] = _bail("h5", 5, "short_mom", str(e))

    try:
        horizons["h20"] = _h20_rf(symbol, hist)
    except Exception as e:
        log.warning("h20 failed for %s: %s", symbol, e)
        horizons["h20"] = _bail("h20", 20, "rf_classifier", str(e))

    try:
        horizons["h60"] = _h60_medium_trend(hist)
    except Exception as e:
        log.warning("h60 failed for %s: %s", symbol, e)
        horizons["h60"] = _bail("h60", 60, "medium_trend", str(e))

    try:
        horizons["h120"] = _h120_long_trend(hist)
    except Exception as e:
        log.warning("h120 failed for %s: %s", symbol, e)
        horizons["h120"] = _bail("h120", 120, "long_trend", str(e))

    return {
        "symbol": symbol,
        "as_of": as_of,
        "horizons": horizons,
        "consensus": _consensus(horizons),
        "divergence_flag": _divergence(horizons),
    }


def multi_horizon_forecast(symbol: str) -> Dict[str, Any]:
    """Compute 5d / 20d / 60d / 120d forecasts for ``symbol``.

    Cached via cache_store.coalesce for 6h. Returns the canonical dict shape
    documented at the top of this file.
    """
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return {
            "symbol": "",
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "error": "empty symbol",
            "horizons": {},
            "consensus": {"direction": "UNKNOWN", "agreement_score": 0.0},
            "divergence_flag": None,
        }

    try:
        import cache_store
        return cache_store.coalesce(
            ("multihorizon", symbol),
            _CACHE_TTL,
            lambda: _compute(symbol),
        )
    except Exception as e:
        # If cache machinery is wedged we still want a result for the UI.
        log.warning("cache_store unavailable for multihorizon %s: %s", symbol, e)
        return _compute(symbol)


__all__ = ["multi_horizon_forecast"]
