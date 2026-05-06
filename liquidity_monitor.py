"""
liquidity_monitor.py — Liquidity Regime Monitor

Market-wide early warning system that detects pre-crisis conditions 1-3 days
before dislocations.  Synthesizes 6 sub-indicators into a composite stress
score with regime labels (NORMAL / ELEVATED / WARNING / CRITICAL) and
automatic position-sizing recommendations.

Sub-indicators (0-100 each):
  VIX Regime              25%
  Cross-Asset Correlation 20%
  Sector Dispersion       15%
  Volume Regime           15%
  Credit Stress Proxy     15%
  Market Breadth          10%
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

import fetcher

log = logging.getLogger(__name__)

# ── Module-level cache (300 s TTL — refresh frequently for market data) ──────

_cache = {}       # type: Dict[str, object]
_cache_ts = {}    # type: Dict[str, float]
_CACHE_TTL = 300


def _cached_get(key):
    # type: (str) -> Optional[object]
    ts = _cache_ts.get(key)
    if ts is not None and (time.time() - ts) < _CACHE_TTL:
        return _cache.get(key)
    return None


def _cached_set(key, value):
    _cache[key] = value
    _cache_ts[key] = time.time()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clamp(val, lo=0, hi=100):
    # type: (float, float, float) -> int
    return int(max(lo, min(hi, val)))


def _safe_chart(symbol, period, interval="1d"):
    # type: (str, str, str) -> Optional[List[dict]]
    """Fetch chart data, return None on failure."""
    try:
        data = fetcher.get_chart_data(symbol, period=period, interval=interval)
        if data and isinstance(data, list) and len(data) > 0:
            return data
    except Exception as exc:
        log.warning("Chart fetch failed for %s: %s", symbol, exc)
    return None


def _closes(chart_data):
    # type: (List[dict]) -> np.ndarray
    return np.array([d["close"] for d in chart_data if d.get("close") is not None], dtype=float)


def _volumes(chart_data):
    # type: (List[dict]) -> np.ndarray
    return np.array([d["volume"] for d in chart_data if d.get("volume") is not None], dtype=float)


def _rate_of_change(arr, lookback=20):
    # type: (np.ndarray, int) -> float
    """Percentage change over *lookback* periods."""
    if len(arr) < lookback + 1:
        return 0.0
    old = arr[-(lookback + 1)]
    new = arr[-1]
    if old == 0:
        return 0.0
    return ((new - old) / abs(old)) * 100.0


def _neutral_indicator(name):
    # type: (str) -> dict
    return {"score": 50, "value": 0.0, "detail": "{} data unavailable".format(name)}


# ── Sub-indicator 1: VIX Regime (25%) ────────────────────────────────────────

def _compute_vix_regime():
    # type: () -> dict
    chart = _safe_chart("^VIX", "6mo")
    if chart is None:
        return _neutral_indicator("VIX")

    closes = _closes(chart)
    if len(closes) < 20:
        return _neutral_indicator("VIX")

    level = float(closes[-1])
    percentile = float(np.percentile(closes, 0))  # placeholder; compute real
    # Real percentile: fraction of observations <= current level
    percentile = float(np.sum(closes <= level) / len(closes) * 100.0)

    # Base score from absolute level
    if level > 35:
        score = 95
    elif level > 30:
        score = 85
    elif level > 25:
        score = 70
    elif level > 20:
        score = 50
    elif level > 15:
        score = 25
    else:
        score = 10

    # Rate-of-change spike check: 5-day avg vs 20-day avg
    avg5 = float(np.mean(closes[-5:])) if len(closes) >= 5 else level
    avg20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else level
    if avg20 > 0 and ((avg5 - avg20) / avg20) > 0.20:
        score += 10

    score = _clamp(score)
    detail = "VIX at {:.1f} ({:.0f}th percentile, 6mo)".format(level, percentile)
    return {"score": score, "value": round(level, 2), "detail": detail}


# ── Sub-indicator 2: Cross-Asset Correlation (20%) ──────────────────────────

def _compute_cross_correlation():
    # type: () -> dict
    try:
        result = fetcher.get_correlation_matrix(
            ["SPY", "TLT", "GLD", "USO", "UUP"], period="1mo"
        )
    except Exception as exc:
        log.warning("Correlation matrix failed: %s", exc)
        return _neutral_indicator("Cross-asset correlation")

    matrix = result.get("matrix", {})
    if not matrix:
        return _neutral_indicator("Cross-asset correlation")

    # Extract unique pairwise absolute correlations
    syms = list(matrix.keys())
    pairs = []
    for i, s1 in enumerate(syms):
        for s2 in syms[i + 1:]:
            val = None
            row = matrix.get(s1, {})
            if isinstance(row, dict):
                val = row.get(s2)
            if val is not None:
                pairs.append(abs(float(val)))

    if not pairs:
        return _neutral_indicator("Cross-asset correlation")

    avg_corr = float(np.mean(pairs))

    if avg_corr > 0.75:
        score = 95
    elif avg_corr > 0.60:
        score = 75
    elif avg_corr > 0.45:
        score = 55
    elif avg_corr > 0.30:
        score = 35
    else:
        score = 15

    detail = "Avg cross-asset correlation: {:.2f}".format(avg_corr)
    return {"score": score, "value": round(avg_corr, 4), "detail": detail}


# ── Sub-indicator 3: Sector Dispersion (15%) ─────────────────────────────────

def _compute_sector_dispersion():
    # type: () -> dict
    try:
        sectors = fetcher.get_sector_performance()
    except Exception as exc:
        log.warning("Sector performance failed: %s", exc)
        return _neutral_indicator("Sector dispersion")

    if not sectors or not isinstance(sectors, list):
        return _neutral_indicator("Sector dispersion")

    changes = []
    for s in sectors:
        val = s.get("change_pct") if isinstance(s, dict) else None
        if val is not None:
            changes.append(float(val))

    if len(changes) < 3:
        return _neutral_indicator("Sector dispersion")

    std = float(np.std(changes, ddof=1)) if len(changes) > 1 else 0.0

    # Inverted: low dispersion = high stress (herding)
    if std < 0.3:
        score = 85
    elif std < 0.5:
        score = 65
    elif std < 1.0:
        score = 45
    elif std < 2.0:
        score = 25
    else:
        score = 10

    detail = "Sector return dispersion: {:.2f}%".format(std)
    return {"score": score, "value": round(std, 4), "detail": detail}


# ── Sub-indicator 4: Volume Regime (15%) ─────────────────────────────────────

def _compute_volume_regime():
    # type: () -> dict
    chart = _safe_chart("SPY", "3mo")
    if chart is None or len(chart) < 20:
        return _neutral_indicator("Volume regime")

    closes = _closes(chart)
    vols = _volumes(chart)

    if len(vols) < 20 or len(closes) < 20:
        return _neutral_indicator("Volume regime")

    avg5_vol = float(np.mean(vols[-5:]))
    avg20_vol = float(np.mean(vols[-20:]))
    vol_ratio = avg5_vol / avg20_vol if avg20_vol > 0 else 1.0

    # Price direction over last 5 days
    price_change = (closes[-1] - closes[-6]) / closes[-6] if closes[-6] != 0 else 0.0
    price_up = price_change > 0.002
    price_down = price_change < -0.002
    vol_rising = vol_ratio > 1.05
    vol_declining = vol_ratio < 0.95

    if price_up and vol_declining:
        score = 75
        direction = "rising on declining volume (distribution)"
    elif price_down and vol_rising:
        score = 80
        direction = "falling on surging volume (panic)"
    elif price_up and vol_rising:
        score = 15
        direction = "rising on rising volume (healthy)"
    elif not price_up and not price_down and vol_declining:
        score = 40
        direction = "flat on declining volume"
    else:
        score = 30
        direction = "mixed"

    detail = "SPY volume {:.1f}x normal, price {}".format(vol_ratio, direction)
    return {"score": score, "value": round(vol_ratio, 2), "detail": detail}


# ── Sub-indicator 5: Credit Stress Proxy (15%) ──────────────────────────────

def _compute_credit_stress():
    # type: () -> dict
    hyg = _safe_chart("HYG", "3mo")
    tlt = _safe_chart("TLT", "3mo")
    if hyg is None or tlt is None:
        return _neutral_indicator("Credit stress")

    hyg_c = _closes(hyg)
    tlt_c = _closes(tlt)
    min_len = min(len(hyg_c), len(tlt_c))
    if min_len < 21:
        return _neutral_indicator("Credit stress")

    # Align to same length (take last min_len entries)
    hyg_c = hyg_c[-min_len:]
    tlt_c = tlt_c[-min_len:]

    # Ratio of HYG / TLT
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(tlt_c != 0, hyg_c / tlt_c, np.nan)

    # Filter out NaNs
    ratio = ratio[~np.isnan(ratio)]
    if len(ratio) < 21:
        return _neutral_indicator("Credit stress")

    change = _rate_of_change(ratio, lookback=20)

    if change < -5:
        score = 90
    elif change < -3:
        score = 70
    elif change < -1:
        score = 50
    elif change <= 1:
        score = 25
    else:
        score = 10

    detail = "HYG/TLT ratio change: {:+.1f}% (20d)".format(change)
    return {"score": score, "value": round(change, 2), "detail": detail}


# ── Sub-indicator 6: Market Breadth (10%) ────────────────────────────────────

def _compute_market_breadth():
    # type: () -> dict
    rsp = _safe_chart("RSP", "3mo")
    if rsp is None:
        # Fallback symbol
        rsp = _safe_chart("RSPE", "3mo")
    spy = _safe_chart("SPY", "3mo")

    if rsp is None or spy is None:
        return _neutral_indicator("Market breadth")

    rsp_c = _closes(rsp)
    spy_c = _closes(spy)
    min_len = min(len(rsp_c), len(spy_c))
    if min_len < 21:
        return _neutral_indicator("Market breadth")

    rsp_c = rsp_c[-min_len:]
    spy_c = spy_c[-min_len:]

    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(spy_c != 0, rsp_c / spy_c, np.nan)
    ratio = ratio[~np.isnan(ratio)]
    if len(ratio) < 21:
        return _neutral_indicator("Market breadth")

    change = _rate_of_change(ratio, lookback=20)

    if change < -3:
        score = 85
        direction = "narrowing"
    elif change < -1:
        score = 60
        direction = "narrowing"
    elif change <= 1:
        score = 35
        direction = "stable"
    else:
        score = 10
        direction = "broadening"

    detail = "Market breadth {}: RSP/SPY ratio \u0394{:+.1f}%".format(direction, change)
    return {"score": score, "value": round(change, 2), "detail": detail}


# ── History sparkline (approximate) ──────────────────────────────────────────

def _compute_history_sparkline():
    # type: () -> List[dict]
    """
    Approximate the last 30 daily composite scores using VIX + SPY volume,
    the two most impactful and readily available indicators.
    """
    vix_chart = _safe_chart("^VIX", "6mo")
    spy_chart = _safe_chart("SPY", "3mo")
    if vix_chart is None or spy_chart is None:
        return []

    vix_closes = _closes(vix_chart)
    spy_vols = _volumes(spy_chart)
    spy_closes = _closes(spy_chart)

    # We need at least 50 data points to look back 20 days from each of 30 points
    if len(vix_closes) < 50 or len(spy_vols) < 50 or len(spy_closes) < 50:
        return []

    history = []
    n_days = min(30, len(vix_closes) - 20, len(spy_vols) - 20)
    if n_days <= 0:
        return []

    # Get dates from VIX chart
    vix_dates = []
    for d in vix_chart:
        ts = d.get("date")
        if ts is not None:
            try:
                vix_dates.append(datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d"))
            except Exception:
                vix_dates.append("")
        else:
            vix_dates.append("")

    for offset in range(n_days, 0, -1):
        idx = len(vix_closes) - offset

        # VIX score at this point
        vix_val = float(vix_closes[idx])
        if vix_val > 35:
            vix_s = 95
        elif vix_val > 30:
            vix_s = 85
        elif vix_val > 25:
            vix_s = 70
        elif vix_val > 20:
            vix_s = 50
        elif vix_val > 15:
            vix_s = 25
        else:
            vix_s = 10

        # Volume score at corresponding index (approximate)
        spy_idx = min(idx, len(spy_vols) - 1)
        if spy_idx >= 20:
            avg5 = float(np.mean(spy_vols[spy_idx - 4:spy_idx + 1]))
            avg20 = float(np.mean(spy_vols[spy_idx - 19:spy_idx + 1]))
            vol_ratio = avg5 / avg20 if avg20 > 0 else 1.0
        else:
            vol_ratio = 1.0

        # Simplified volume score
        if vol_ratio > 1.3:
            vol_s = 70
        elif vol_ratio > 1.1:
            vol_s = 40
        else:
            vol_s = 20

        # Approximate composite: VIX weight 0.55, volume weight 0.45
        approx_score = int(vix_s * 0.55 + vol_s * 0.45)
        approx_score = max(0, min(100, approx_score))

        date_str = ""
        if idx < len(vix_dates):
            date_str = vix_dates[idx]

        history.append({"date": date_str, "score": approx_score})

    return history


# ── Main entry point ─────────────────────────────────────────────────────────

def compute_stress_score():
    # type: () -> dict
    """
    Compute the market-wide liquidity stress composite score.

    Returns a dict with composite_score, regime, indicators, position sizing
    recommendation, and historical sparkline.  Never raises.
    """
    ck = "stress_score"
    hit = _cached_get(ck)
    if hit is not None:
        return hit

    try:
        return _compute_stress_score_inner()
    except Exception as exc:
        log.exception("compute_stress_score failed")
        return {"error": str(exc)}


def _compute_stress_score_inner():
    # type: () -> dict

    # ── Compute all 6 sub-indicators ──────────────────────────────────
    vix = _compute_vix_regime()
    correlation = _compute_cross_correlation()
    dispersion = _compute_sector_dispersion()
    volume = _compute_volume_regime()
    credit = _compute_credit_stress()
    breadth = _compute_market_breadth()

    # ── Weighted composite ────────────────────────────────────────────
    composite = int(
        vix["score"] * 0.25
        + correlation["score"] * 0.20
        + dispersion["score"] * 0.15
        + volume["score"] * 0.15
        + credit["score"] * 0.15
        + breadth["score"] * 0.10
    )
    composite = max(0, min(100, composite))

    # ── Regime classification ─────────────────────────────────────────
    if composite > 70:
        regime = "CRITICAL"
        multiplier = 0.25
    elif composite > 50:
        regime = "WARNING"
        multiplier = 0.50
    elif composite >= 30:
        regime = "ELEVATED"
        multiplier = 0.75
    else:
        regime = "NORMAL"
        multiplier = 1.0

    # ── Regime detail (human-readable) ────────────────────────────────
    regime_details = {
        "NORMAL": "Markets calm; no unusual stress detected across indicators.",
        "ELEVATED": "Some stress signals emerging; monitor closely.",
        "WARNING": "Multiple stress signals active; consider reducing risk exposure.",
        "CRITICAL": "Severe stress across multiple indicators; significant dislocation risk.",
    }
    regime_detail = regime_details[regime]

    # ── Position sizing recommendation ────────────────────────────────
    rec_map = {
        "NORMAL": "Full position sizes appropriate (1.0x).",
        "ELEVATED": "Reduce position sizes to 75% of normal.",
        "WARNING": "Reduce position sizes to 50% of normal.",
        "CRITICAL": "Reduce position sizes to 25% of normal; consider hedging.",
    }
    recommendation = rec_map[regime]

    # ── History sparkline ─────────────────────────────────────────────
    history = _compute_history_sparkline()

    # ── Percentile rank (current vs last 60 approx scores) ───────────
    percentile_rank = 50.0  # default
    if history and len(history) >= 5:
        hist_scores = np.array([h["score"] for h in history], dtype=float)
        percentile_rank = float(np.sum(hist_scores <= composite) / len(hist_scores) * 100.0)

    # ── Assemble result ───────────────────────────────────────────────
    result = {
        "composite_score": composite,
        "regime": regime,
        "regime_detail": regime_detail,
        "position_sizing_multiplier": multiplier,
        "indicators": {
            "vix_regime": {
                "score": vix["score"],
                "value": vix["value"],
                "detail": vix["detail"],
                "weight": 0.25,
            },
            "cross_correlation": {
                "score": correlation["score"],
                "value": correlation["value"],
                "detail": correlation["detail"],
                "weight": 0.20,
            },
            "sector_dispersion": {
                "score": dispersion["score"],
                "value": dispersion["value"],
                "detail": dispersion["detail"],
                "weight": 0.15,
            },
            "volume_regime": {
                "score": volume["score"],
                "value": volume["value"],
                "detail": volume["detail"],
                "weight": 0.15,
            },
            "credit_stress": {
                "score": credit["score"],
                "value": credit["value"],
                "detail": credit["detail"],
                "weight": 0.15,
            },
            "market_breadth": {
                "score": breadth["score"],
                "value": breadth["value"],
                "detail": breadth["detail"],
                "weight": 0.10,
            },
        },
        "percentile_rank": round(percentile_rank, 1),
        "history": history[-30:],  # last 30 data points
        "recommendation": recommendation,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    _cached_set("stress_score", result)
    return result
