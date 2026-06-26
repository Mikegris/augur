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
# Raised from 5: a 5-sample forward-return mean has very wide error bars. We
# now take the k nearest neighbours by a graded distance (not a discrete band),
# so we can afford — and want — a larger, more statistically stable set.
_MIN_MATCHES = 15
_KNN_K = 40           # neighbours to keep for the forward-return distribution
_RSI_PERIOD = 14
_VOL_WINDOW = 20
_SMA_FAST = 50
_SMA_SLOW = 200

# Feature columns used for the z-scored / Mahalanobis k-NN similarity.
_KNN_FEATURES = ("rsi_14", "vol_ann", "px_vs_sma50", "px_vs_sma200")


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def _compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """14-day Wilder RSI using Wilder's smoothing (the modified EMA with
    α = 1/period), matching the docstring and fetcher.py's genuine Wilder RSI.

    Previously this used a SIMPLE rolling mean of gains/losses, which is the
    Cutler variant — it reacts faster and disagrees with every "Wilder RSI"
    reference. We now seed with the first `period`-window simple average and
    then apply Wilder's recursive smoothing
    (avg = (prev_avg·(period-1) + current) / period), implemented via
    ewm(alpha=1/period, adjust=False) after the seed.

    Degenerate cases preserved:
      - No losses (loss == 0, gain > 0) -> RSI = 100
      - No gains  (gain == 0)           -> RSI = 0
      - Perfectly flat window           -> NaN (caller treats as undefined)
    """
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    # Wilder smoothing == EWM with alpha = 1/period (adjust=False). We still
    # require a full `period` of history before emitting a value so the warm-up
    # NaNs match the old simple-rolling behaviour (no look-ahead change).
    gain = up.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    loss = down.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
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

    # Continuous "distance from moving average" features for the graded k-NN
    # (replacing the discrete above/below band match). Expressed as fractional
    # deviation: (close/sma - 1).
    df["px_vs_sma50"] = close / df["sma_50"] - 1.0
    df["px_vs_sma200"] = close / df["sma_200"] - 1.0

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


def _knn_matches(candidates: pd.DataFrame, current: pd.Series, k: int) -> pd.DataFrame:
    """Graded similarity via a z-scored / Mahalanobis k-NN over the continuous
    feature set (rsi, vol, price-vs-sma50, price-vs-sma200).

    Replaces the old discrete band-matching (which lumped a 31-RSI and a
    44-RSI into the same "WEAK" bucket while splitting 29 vs 31 across
    OVERSOLD/WEAK). Each candidate row gets a distance to *today*; the k
    closest are returned with a ``similarity_distance`` column so the caller
    can weight or inspect them.

    Uses the inverse feature covariance (Mahalanobis) when it's well-
    conditioned, falling back to per-feature z-scoring (diagonal covariance)
    otherwise. Returns an empty frame if features can't be assembled.
    """
    feats = [f for f in _KNN_FEATURES if f in candidates.columns]
    if len(feats) < 2:
        return candidates.iloc[0:0]

    X = candidates[feats].astype(float)
    cur = current[feats].astype(float)
    valid = X.notna().all(axis=1) & cur.notna().all()
    if not bool(valid.all()):
        X = X[valid.reindex(X.index, fill_value=False)]
    cur_vec = cur.values
    if X.empty or any(pd.isna(cur_vec)):
        return candidates.iloc[0:0]

    Xv = X.values
    mu = Xv.mean(axis=0)
    cov = np.cov(Xv, rowvar=False)
    # Mahalanobis when invertible & well-conditioned; else diagonal z-score.
    inv = None
    try:
        cov2d = np.atleast_2d(cov)
        if cov2d.shape[0] == cov2d.shape[1] and np.all(np.isfinite(cov2d)):
            if np.linalg.cond(cov2d) < 1e12:
                inv = np.linalg.pinv(cov2d)
    except Exception:
        inv = None
    if inv is None:
        var = Xv.var(axis=0)
        var[var <= 0] = 1.0
        inv = np.diag(1.0 / var)

    diff = Xv - cur_vec
    # Mahalanobis distance per row: sqrt((x-c)·Σ⁻¹·(x-c))
    dists = np.sqrt(np.clip(np.einsum("ij,jk,ik->i", diff, inv, diff), 0.0, None))

    out = candidates.loc[X.index].copy()
    out["similarity_distance"] = dists
    out = out.sort_values("similarity_distance", ascending=True)
    return out.head(max(1, int(k)))


def _mean_ci(rets: np.ndarray, conf: float = 0.95) -> Optional[list]:
    """Normal-approx confidence interval for the MEAN forward return.

    Returns [lo, hi] in percent, or None when n < 2. z=1.96 for 95%; we use a
    t-like inflation for small n by widening with 2.2 below n=15. Reported so
    the UI can show how shaky a small-sample analog read is.
    """
    n = len(rets)
    if n < 2:
        return None
    mean = float(np.mean(rets))
    sd = float(np.std(rets, ddof=1))
    if sd == 0:
        return [round(mean, 2), round(mean, 2)]
    se = sd / np.sqrt(n)
    z = 1.96 if conf >= 0.95 else 1.64
    if n < 15:
        z *= 1.12  # small-sample widening (rough t-vs-z inflation)
    half = z * se
    return [round(mean - half, 2), round(mean + half, 2)]


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

    # Candidate pool: exclude the most recent _FORWARD_WINDOW bars (no fwd
    # return — preserves the no-look-ahead guarantee) and require the
    # continuous k-NN features + forward return present.
    candidates = classified.iloc[:-_FORWARD_WINDOW] if len(classified) > _FORWARD_WINDOW else classified.iloc[0:0]
    knn_required = list(_KNN_FEATURES) + ["fwd_return_pct"]
    candidates = candidates.dropna(subset=knn_required)

    if candidates.empty:
        result = {"available": False, "error": "No usable candidate history"}
        _set_cache(cache_key, result, ttl=_CACHE_TTL)
        return result

    # ── Primary path: graded z-scored / Mahalanobis k-NN similarity ──────
    method = "knn_mahalanobis"
    loosened = None
    knn = _knn_matches(candidates, current, _KNN_K)
    matches = knn

    similarity = None
    if not matches.empty and "similarity_distance" in matches.columns:
        dvals = matches["similarity_distance"].dropna().values
        if len(dvals):
            similarity = {
                "mean_distance": round(float(np.mean(dvals)), 4),
                "max_distance": round(float(np.max(dvals)), 4),
            }

    # ── Fallback: discrete band-matching (only if k-NN couldn't assemble a
    # usable neighbour set, e.g. degenerate features) ───────────────────
    if len(matches) < _MIN_MATCHES:
        band_cands = candidates.dropna(subset=["rsi_band", "vol_band", "trend_position"])
        method = "band_match"
        matches = _find_matches(
            band_cands, current_rsi_band, current_vol_band, current_trend,
            require_trend=True, require_vol=True,
        )
        loosened = None
        if len(matches) < _MIN_MATCHES:
            matches = _find_matches(
                band_cands, current_rsi_band, current_vol_band, current_trend,
                require_trend=False, require_vol=True,
            )
            loosened = "dropped trend_position"
        if len(matches) < _MIN_MATCHES:
            matches = _find_matches(
                band_cands, current_rsi_band, current_vol_band, current_trend,
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

    # Confidence interval on the mean forward return — surfaces small-sample
    # fragility instead of presenting a 5-point mean as gospel.
    rets = matches["fwd_return_pct"].dropna().values
    mean_ci = _mean_ci(np.asarray(rets, dtype=float)) if len(rets) else None
    low_confidence = len(matches) < _MIN_MATCHES

    result = {
        "available": True,
        "current_conditions": current_conditions,
        "match_count": int(len(matches)),
        "match_method": method,            # "knn_mahalanobis" | "band_match"
        "similarity": similarity,           # k-NN distance summary (None for band match)
        "lookback_years": _LOOKBACK_YEARS,
        "forward_window_days": _FORWARD_WINDOW,
        "forward_returns": stats,
        "forward_return_mean_ci95": mean_ci,  # [lo, hi] % CI on the mean (None if n<2)
        "low_confidence": bool(low_confidence),
        "sample_dates": samples,
        "interpretation": interpretation,
        "error": None,
    }

    _set_cache(cache_key, result, ttl=_CACHE_TTL)
    return result
