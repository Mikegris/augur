"""
historical_analog.py — Find historical days where a stock had similar conditions
to today (RSI band, volatility regime, position vs 50/200-day MAs), then compute
forward 30-day return statistics.

Used by the "random investment idea" dossier to answer: "Last time this stock
had similar conditions, what happened over the next 30 days?"
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

from fetcher import _cached, _set_cache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache config
# ---------------------------------------------------------------------------
_CACHE_TTL = 3600  # 1 hour

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
_LOOKBACK_PERIOD = "5y"
_LOOKBACK_YEARS = 5
_MIN_HISTORY_DAYS = 250
_FORWARD_WINDOW = 30
_MIN_MATCHES = 5
_RSI_PERIOD = 14
_VOL_WINDOW = 20
_SMA_FAST = 50
_SMA_SLOW = 200


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Standard 14-day Wilder-style RSI (using simple rolling means of gains/losses).

    Handles the degenerate cases properly:
      - No losses over the window (loss == 0) -> RSI = 100 (max overbought)
      - No gains over the window  (gain == 0) -> RSI = 0   (max oversold)
    Previously this returned NaN in both cases because we divided by NaN.
    """
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    # Standard RSI handles the zero-denominator cases as RSI=100 / RSI=0.
    rsi = pd.Series(np.nan, index=close.index, dtype=float)
    valid = gain.notna() & loss.notna()
    no_loss = valid & (loss == 0) & (gain > 0)
    no_gain = valid & (gain == 0)
    normal = valid & (loss > 0) & (gain > 0)
    rs = gain[normal] / loss[normal]
    rsi.loc[normal] = 100 - (100 / (1 + rs))
    rsi.loc[no_loss] = 100.0
    rsi.loc[no_gain] = 0.0
    # Both zero (perfectly flat window): leave as NaN -> caller treats as undefined
    return rsi


def _rsi_band(rsi: float) -> Optional[str]:
    if rsi is None or pd.isna(rsi):
        return None
    if rsi < 30:
        return "OVERSOLD"
    if rsi < 45:
        return "WEAK"
    if rsi < 55:
        return "NEUTRAL"
    if rsi <= 70:
        return "STRONG"
    return "OVERBOUGHT"


def _trend_position(close: float, sma50: float, sma200: float) -> Optional[str]:
    if any(v is None or pd.isna(v) for v in (close, sma50, sma200)):
        return None
    above50 = close > sma50
    above200 = close > sma200
    if above50 and above200:
        return "ABOVE_50_AND_200"
    if above50 and not above200:
        return "ABOVE_50_BELOW_200"
    if not above50 and above200:
        return "BELOW_50_ABOVE_200"
    return "BELOW_50_AND_200"


def _build_features(hist: pd.DataFrame) -> pd.DataFrame:
    """Augment OHLCV with RSI, annualized vol, SMAs, trend position, and forward return."""
    df = hist.copy()
    close = df["Close"]

    df["rsi_14"] = _compute_rsi(close, _RSI_PERIOD)

    # Annualised vol from rolling std of daily log returns
    log_ret = np.log(close / close.shift(1))
    df["vol_ann"] = log_ret.rolling(_VOL_WINDOW).std() * np.sqrt(252)

    df["sma_50"] = close.rolling(_SMA_FAST).mean()
    df["sma_200"] = close.rolling(_SMA_SLOW).mean()

    # Forward 30-day return (% change to close 30 trading days later)
    df["fwd_return_pct"] = (close.shift(-_FORWARD_WINDOW) / close - 1.0) * 100.0

    return df


def _classify_rows(df: pd.DataFrame, vol_low_thresh: float, vol_high_thresh: float) -> pd.DataFrame:
    """Add categorical band columns based on absolute thresholds for vol_band."""
    out = df.copy()

    out["rsi_band"] = out["rsi_14"].apply(_rsi_band)

    def _vol_band(v):
        if v is None or pd.isna(v):
            return None
        if v <= vol_low_thresh:
            return "LOW"
        if v >= vol_high_thresh:
            return "HIGH"
        return "MID"

    out["vol_band"] = out["vol_ann"].apply(_vol_band)

    out["trend_position"] = [
        _trend_position(c, s50, s200)
        for c, s50, s200 in zip(out["Close"], out["sma_50"], out["sma_200"])
    ]

    return out


# ---------------------------------------------------------------------------
# Match search + forward-return stats
# ---------------------------------------------------------------------------

def _find_matches(
    candidates: pd.DataFrame,
    rsi_band: str,
    vol_band: str,
    trend_position: str,
    require_trend: bool = True,
    require_vol: bool = True,
) -> pd.DataFrame:
    """Filter `candidates` to rows matching the requested bands."""
    mask = candidates["rsi_band"] == rsi_band
    if require_vol:
        mask &= candidates["vol_band"] == vol_band
    if require_trend:
        mask &= candidates["trend_position"] == trend_position
    return candidates[mask]


def _summarise_returns(matches: pd.DataFrame) -> dict:
    rets = matches["fwd_return_pct"].dropna().values
    if len(rets) == 0:
        return {
            "mean_pct": 0.0,
            "median_pct": 0.0,
            "hit_rate_pct": 0.0,
            "p25_pct": 0.0,
            "p75_pct": 0.0,
            "best_pct": 0.0,
            "worst_pct": 0.0,
        }
    return {
        "mean_pct": round(float(np.mean(rets)), 2),
        "median_pct": round(float(np.median(rets)), 2),
        "hit_rate_pct": round(float((rets > 0).mean() * 100.0), 1),
        "p25_pct": round(float(np.percentile(rets, 25)), 2),
        "p75_pct": round(float(np.percentile(rets, 75)), 2),
        "best_pct": round(float(np.max(rets)), 2),
        "worst_pct": round(float(np.min(rets)), 2),
    }


def _sample_dates(matches: pd.DataFrame, n: int = 5) -> list:
    """Return up to n example matches, evenly spaced across history."""
    if matches.empty:
        return []
    valid = matches.dropna(subset=["fwd_return_pct"])
    if valid.empty:
        return []
    n = min(n, len(valid))
    if len(valid) <= n:
        picks = valid
    else:
        # Evenly spaced indices across the matched rows
        idxs = np.linspace(0, len(valid) - 1, n).round().astype(int)
        picks = valid.iloc[idxs]
    out = []
    for ts, row in picks.iterrows():
        try:
            date_str = pd.Timestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            date_str = str(ts)[:10]
        out.append({
            "date": date_str,
            "forward_pct": round(float(row["fwd_return_pct"]), 2),
        })
    return out


def _interpret(stats: dict, match_count: int, loosened: Optional[str]) -> str:
    hit = stats["hit_rate_pct"]
    mean = stats["mean_pct"]
    positives = int(round(match_count * hit / 100.0))
    if hit >= 60:
        lead = "Bullish"
    elif hit <= 40:
        lead = "Bearish"
    else:
        lead = "Neutral"
    sign = "+" if mean >= 0 else ""
    base = (
        f"{lead}: {positives} of {match_count} historical analogs returned "
        f"positive over 30d, avg {sign}{mean:.1f}%"
    )
    if loosened:
        base += f" (loosened: {loosened})"
    return base + "."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_historical_analog(symbol: str) -> dict:
    """
    Find historical days where `symbol` had similar conditions to today, then
    compute forward 30-day return statistics. See module docstring for shape.
    """
    if not symbol:
        return {"available": False, "error": "No symbol provided"}

    symbol = symbol.upper().strip()
    cache_key = ("historical_analog", symbol)

    cached = _cached(cache_key, ttl=_CACHE_TTL)
    if cached is not None:
        return cached

    try:
        import fetcher
        bars = fetcher.get_chart_data(symbol, period=_LOOKBACK_PERIOD, interval="1d")
        if not bars:
            hist = pd.DataFrame()
        else:
            hist = pd.DataFrame(bars)
            hist["Date"] = pd.to_datetime(hist["time"], unit="s")
            hist = hist.set_index("Date")
            hist = hist.rename(columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume",
            })
    except Exception as e:
        logger.warning("historical_analog: failed to fetch %s: %s", symbol, e)
        return {"available": False, "error": "Failed to fetch history: %s" % e}

    if hist is None or hist.empty or len(hist) < _MIN_HISTORY_DAYS:
        result = {"available": False, "error": "Insufficient history"}
        _set_cache(cache_key, result, ttl=_CACHE_TTL)
        return result

    try:
        feats = _build_features(hist)
    except Exception as e:
        logger.warning("historical_analog: feature build failed for %s: %s", symbol, e)
        return {"available": False, "error": "Feature engineering failed: %s" % e}

    # Vol-band thresholds from the symbol's own historical vol distribution
    vol_series = feats["vol_ann"].dropna()
    if len(vol_series) < 30:
        result = {"available": False, "error": "Insufficient volatility history"}
        _set_cache(cache_key, result, ttl=_CACHE_TTL)
        return result

    vol_low_thresh = float(np.percentile(vol_series, 33))
    vol_high_thresh = float(np.percentile(vol_series, 67))

    classified = _classify_rows(feats, vol_low_thresh, vol_high_thresh)

    # Current conditions = the most recent fully-formed row
    latest_valid = classified.dropna(subset=["rsi_band", "vol_band", "trend_position"])
    if latest_valid.empty:
        result = {"available": False, "error": "Could not classify current conditions"}
        _set_cache(cache_key, result, ttl=_CACHE_TTL)
        return result

    current = latest_valid.iloc[-1]
    current_rsi = float(current["rsi_14"]) if not pd.isna(current["rsi_14"]) else None
    current_rsi_band = current["rsi_band"]
    current_vol_band = current["vol_band"]
    current_trend = current["trend_position"]

    current_conditions = {
        "rsi_14": round(current_rsi, 2) if current_rsi is not None else None,
        "vol_band": current_vol_band,
        "trend_position": current_trend,
        "rsi_band": current_rsi_band,
    }

    # Candidate pool: exclude the most recent _FORWARD_WINDOW bars (no fwd return)
    # and require all classification + forward return present.
    candidates = classified.iloc[:-_FORWARD_WINDOW] if len(classified) > _FORWARD_WINDOW else classified.iloc[0:0]
    candidates = candidates.dropna(subset=["rsi_band", "vol_band", "trend_position", "fwd_return_pct"])

    if candidates.empty:
        result = {"available": False, "error": "No usable candidate history"}
        _set_cache(cache_key, result, ttl=_CACHE_TTL)
        return result

    # Tier 1: strict match on all three
    matches = _find_matches(
        candidates, current_rsi_band, current_vol_band, current_trend,
        require_trend=True, require_vol=True,
    )
    loosened = None

    # Tier 2: drop trend_position
    if len(matches) < _MIN_MATCHES:
        matches = _find_matches(
            candidates, current_rsi_band, current_vol_band, current_trend,
            require_trend=False, require_vol=True,
        )
        loosened = "dropped trend_position"

    # Tier 3: drop vol_band too (RSI band only)
    if len(matches) < _MIN_MATCHES:
        matches = _find_matches(
            candidates, current_rsi_band, current_vol_band, current_trend,
            require_trend=False, require_vol=False,
        )
        loosened = "dropped trend_position + vol_band"

    if matches.empty:
        result = {
            "available": False,
            "error": "No historical analogs found",
            "current_conditions": current_conditions,
        }
        _set_cache(cache_key, result, ttl=_CACHE_TTL)
        return result

    stats = _summarise_returns(matches)
    samples = _sample_dates(matches, n=5)
    interpretation = _interpret(stats, len(matches), loosened)

    result = {
        "available": True,
        "current_conditions": current_conditions,
        "match_count": int(len(matches)),
        "lookback_years": _LOOKBACK_YEARS,
        "forward_window_days": _FORWARD_WINDOW,
        "forward_returns": stats,
        "sample_dates": samples,
        "interpretation": interpretation,
        "error": None,
    }

    _set_cache(cache_key, result, ttl=_CACHE_TTL)
    return result
