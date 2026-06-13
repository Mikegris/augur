"""
Probabilistic forecast — research module for AUGUR.

`ml_forecast.py` emits *point* estimates. This module emits a full
distribution: percentiles (p5–p95), summary moments (mean, std), and a
bundle of "probability of …" numbers — enough to drive a fan / violin
chart in the UI instead of a single number.

Method
------
Block bootstrap of daily log-returns (Politis–Romano, block_size=5).

  1. Pull `fetcher.get_chart_data(symbol, "2y", "1d")`.
  2. Trim to the most recent 252 trading days (≈ 1 year).
  3. Compute daily log-returns ln(P_t / P_{t-1}).
  4. For each of `n_bootstrap` simulations:
       a. Pick random starting indices into the return series.
       b. Concatenate length-5 contiguous blocks until we have at least
          `horizon_days` returns; truncate to exactly `horizon_days`.
       c. Sum the log-returns and exponentiate to get the horizon's
          gross return; subtract 1 for the % return.
  5. Compute percentile/probability summary on the resulting empirical
     distribution.

Why block bootstrap (vs. iid resample) — daily returns aren't quite iid:
volatility clusters and trends persist over a few days at a time.
Sampling contiguous 5-day blocks preserves *some* of that
autocorrelation without locking us into a parametric model (GARCH,
HMM, …).

Output schema is documented on `prob_forecast()`.

Caching
-------
`cache_store.coalesce(("probforecast", symbol, horizon_days), 4*3600,
…)` — 4 h TTL matches the upstream price-history TTL closely enough
that a refresh comes due roughly when new closes are available.

Python 3.9 compatible (no PEP 604 unions, no `match`).
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import time
from datetime import timezone as _timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

log = logging.getLogger("augur.research.probforecast")

# --- optional integrations ---------------------------------------------------
try:
    import cache_store  # type: ignore
except Exception:  # pragma: no cover
    cache_store = None  # type: ignore

try:
    import fetcher  # type: ignore
except Exception:  # pragma: no cover
    fetcher = None  # type: ignore

try:
    import ml_forecast  # type: ignore
except Exception:  # pragma: no cover
    ml_forecast = None  # type: ignore


# Cache TTL (seconds). 4 h — long enough to cover a typical session,
# short enough that a new daily close shows up on the next session.
_CACHE_TTL = 4 * 3600

# Lookback for the resampled return distribution. 252 ≈ one trading year:
# enough samples for the bootstrap to be smooth without dragging in stale
# pre-regime data.
_LOOKBACK_BARS = 252

# Block length for the Politis–Romano stationary-block bootstrap. 5 days
# (one trading week) preserves short-term momentum / vol clustering
# without forcing too much path-dependence on individual draws.
_BLOCK_SIZE = 5

# Need at least this much history to bother running the bootstrap at all.
_MIN_BARS = 60

# Hard cap on n_bootstrap to keep the worst-case allocation bounded.
# 50k × 20 floats stays well under 10 MB.
_MAX_BOOTSTRAP = 50000

# Histogram resolution. ~25 bins is wide enough to show shape, narrow
# enough to be drawn as a sparkline.
_HIST_BINS = 25


# ─── public API ─────────────────────────────────────────────────────────────

def prob_forecast(
    symbol: str,
    horizon_days: int = 20,
    n_bootstrap: int = 2000,
) -> Dict[str, Any]:
    """
    Run a block-bootstrap probabilistic forecast for `symbol`.

    Parameters
    ----------
    symbol : str
        Ticker. Forwarded to fetcher.get_chart_data.
    horizon_days : int
        Forecast horizon in trading days. Capped at 252.
    n_bootstrap : int
        Number of resampled paths. Capped at _MAX_BOOTSTRAP.

    Returns
    -------
    dict with keys:
        symbol, horizon_days, method, distribution{p05..p95,mean,std},
        probabilities{up,up_5pct,up_10pct,down_5pct,down_10pct},
        histogram[{bin_low,bin_high,count}], n_paths, input_window_days,
        as_of.

    If history is missing or too short, the response degrades to
    {"symbol": symbol, "error": "Insufficient history for prob_forecast"}.
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"symbol": "", "error": "symbol required"}
    horizon_days = int(max(1, min(int(horizon_days or 20), 252)))
    n_bootstrap = int(max(100, min(int(n_bootstrap or 2000), _MAX_BOOTSTRAP)))

    def _do():
        return _run_uncached(symbol, horizon_days, n_bootstrap)

    if cache_store is not None:
        key = ("probforecast", symbol, horizon_days)
        try:
            # Bake n_bootstrap into the closure but key on (symbol,
            # horizon) only — re-running with a different n_bootstrap on
            # the same key should still benefit from the cache when
            # there's an active payload. If a caller explicitly wants a
            # bigger run they can clear the cache.
            return cache_store.coalesce(key, _CACHE_TTL, _do)
        except Exception:  # pragma: no cover
            log.exception("cache_store.coalesce failed; running uncached")
    return _do()


def compare_to_point(symbol: str, horizon_days: int = 20) -> Dict[str, Any]:
    """
    Run `prob_forecast` and also extract the matching point estimate
    from `ml_forecast.ml_forecast`. Returns both side-by-side so the UI
    can show a single "here's the point, here's the cone around it"
    panel.

    Output shape:
        {
          "symbol": ...,
          "horizon_days": ...,
          "probabilistic": <prob_forecast payload>,
          "point": {
            "forecast_pct": float or None,
            "prob_up_20d":  float or None,
            "source":       "ml_forecast.trend_forecast" | None,
            "trend":        str or None,
          },
          "spread_vs_median_pct": float or None,
        }
    """
    symbol = (symbol or "").strip().upper()
    prob = prob_forecast(symbol, horizon_days=horizon_days)

    point: Dict[str, Any] = {
        "forecast_pct": None,
        "prob_up_20d": None,
        "source": None,
        "trend": None,
    }
    if ml_forecast is not None:
        try:
            ml = ml_forecast.ml_forecast(symbol)
            if ml and not ml.get("error"):
                tf = (ml.get("trend_forecast") or {})
                if tf:
                    fp = tf.get("forecast_pct")
                    point["forecast_pct"] = (round(float(fp), 2)
                                             if fp is not None else None)
                    # ml_forecast.trend_forecast exposes "trend_short"/"trend_mid"
                    # (not "trend"). Reading the wrong key just returned None
                    # forever; prefer the short-term label, fall back to mid.
                    point["trend"] = tf.get("trend_short") or tf.get("trend_mid")
                    point["source"] = "ml_forecast.trend_forecast"
                rf = (ml.get("rf_classifier") or {})
                if rf:
                    p_up = rf.get("prob_up_20d")
                    point["prob_up_20d"] = (round(float(p_up), 4)
                                            if p_up is not None else None)
        except Exception as e:
            log.debug("ml_forecast call failed for %s: %s", symbol, e)

    spread = None
    if (isinstance(prob, dict)
            and point["forecast_pct"] is not None
            and not prob.get("error")):
        try:
            med = prob["distribution"]["median"]
            # WHY (Q13): point["forecast_pct"] is a 30-TRADING-DAY trend
            # forecast (ml_forecast calls _trend_forecast with days_ahead=30),
            # but the bootstrap median is over `horizon_days`. Subtracting them
            # raw compared mismatched horizons. Linearly rescale the point
            # estimate to the requested horizon (×h/30) before differencing —
            # a first-order approximation of the matching-horizon point return.
            point_scaled = float(point["forecast_pct"]) * (float(horizon_days) / 30.0)
            spread = round(point_scaled - float(med), 2)
        except (KeyError, TypeError, ValueError):
            spread = None

    return {
        "symbol": symbol,
        "horizon_days": int(horizon_days),
        "probabilistic": prob,
        "point": point,
        "spread_vs_median_pct": spread,
    }


# ─── internals ──────────────────────────────────────────────────────────────

def _fetch_log_returns(symbol: str) -> Optional[np.ndarray]:
    """
    Return a 1D np.ndarray of daily log-returns over the last
    _LOOKBACK_BARS trading days, or None if unavailable / too short.
    """
    if fetcher is None:
        return None
    try:
        bars = fetcher.get_chart_data(symbol, "2y", "1d")
    except Exception as e:
        log.debug("fetcher.get_chart_data failed for %s: %s", symbol, e)
        return None
    if not bars or not isinstance(bars, list):
        return None

    # bars: [{"time": epoch, "open":, "high":, "low":, "close":, "volume":}, ...]
    closes_raw: List[float] = []
    for b in bars:
        if not isinstance(b, dict):
            continue
        c = b.get("close")
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c <= 0 or not math.isfinite(c):
            continue
        closes_raw.append(c)
    if len(closes_raw) < _MIN_BARS + 1:  # +1 because returns lose one row
        return None

    closes = np.asarray(closes_raw, dtype=np.float64)
    # Trim to the most recent _LOOKBACK_BARS + 1 closes so we get exactly
    # _LOOKBACK_BARS returns (capped by what's actually available).
    if len(closes) > _LOOKBACK_BARS + 1:
        closes = closes[-(_LOOKBACK_BARS + 1):]
    rets = np.diff(np.log(closes))
    # Drop any non-finite values that might sneak through (split days,
    # zero-volume halts, …).
    rets = rets[np.isfinite(rets)]
    if rets.size < _MIN_BARS:
        return None
    return rets


def _block_bootstrap_paths(returns: np.ndarray, horizon_days: int,
                           n_bootstrap: int,
                           block_size: int = _BLOCK_SIZE,
                           rng: Optional[np.random.Generator] = None
                           ) -> np.ndarray:
    """
    Block-bootstrap `n_bootstrap` paths of length `horizon_days` from
    `returns`. Returns a 1D array of horizon log-returns (sum across the
    block-sampled days), shape (n_bootstrap,).

    Sampling strategy:
      - Number of blocks per path = ceil(horizon_days / block_size).
      - For each block we pick a uniform starting index in
        [0, len(returns) - block_size]. (We never wrap, to avoid
        injecting an artificial Monday-after-Friday jump.)
      - Stitch blocks, truncate to horizon_days, sum.

    Using log-returns lets us sum (associative) instead of compound,
    which is both faster and numerically nicer.
    """
    if rng is None:
        rng = np.random.default_rng()

    T = returns.shape[0]
    # If history is shorter than a single block, fall back to block_size = T.
    block_size = int(max(1, min(block_size, T)))
    n_blocks = int(math.ceil(horizon_days / block_size))

    # Highest legal block-start index. Inclusive of the last full block.
    max_start = T - block_size
    if max_start < 0:
        max_start = 0

    # Sample (n_bootstrap, n_blocks) starting indices in one go.
    starts = rng.integers(0, max_start + 1, size=(n_bootstrap, n_blocks))

    # Build a column-offset matrix so we can vectorise the gather:
    # for each block we want rows [start, start+1, ..., start+block_size-1].
    offsets = np.arange(block_size, dtype=np.int64)
    # Shape: (n_bootstrap, n_blocks, block_size). Broadcast-add.
    idx = starts[:, :, None] + offsets[None, None, :]
    # Flatten the (n_blocks, block_size) inner dims, then truncate to
    # exactly horizon_days. This is the equivalent of stitching blocks
    # left-to-right and chopping the tail.
    idx = idx.reshape(n_bootstrap, n_blocks * block_size)[:, :horizon_days]

    sampled = returns[idx]               # (n_bootstrap, horizon_days)
    horizon_log_rets = sampled.sum(axis=1)  # (n_bootstrap,)
    return horizon_log_rets


def _summarize(horizon_returns_pct: np.ndarray) -> Dict[str, Any]:
    """
    Build distribution + probabilities + histogram blocks from the
    simulated horizon return distribution (units: percent).
    """
    pcts = np.percentile(horizon_returns_pct, [5, 10, 25, 50, 75, 90, 95])
    distribution = {
        "p05":    round(float(pcts[0]), 2),
        "p10":    round(float(pcts[1]), 2),
        "p25":    round(float(pcts[2]), 2),
        "median": round(float(pcts[3]), 2),
        "p75":    round(float(pcts[4]), 2),
        "p90":    round(float(pcts[5]), 2),
        "p95":    round(float(pcts[6]), 2),
        "mean":   round(float(horizon_returns_pct.mean()), 2),
        "std":    round(float(horizon_returns_pct.std(ddof=1))
                        if horizon_returns_pct.size > 1 else 0.0, 2),
    }

    n = horizon_returns_pct.size
    if n <= 0:
        n = 1  # defensive; should be caught upstream
    probabilities = {
        "up":        round(float((horizon_returns_pct > 0).mean()), 4),
        "up_5pct":   round(float((horizon_returns_pct >= 5).mean()), 4),
        "up_10pct":  round(float((horizon_returns_pct >= 10).mean()), 4),
        "down_5pct": round(float((horizon_returns_pct <= -5).mean()), 4),
        "down_10pct":round(float((horizon_returns_pct <= -10).mean()), 4),
    }

    # Histogram. np.histogram returns (counts, edges) with len(edges)
    # = bins+1; we emit one row per bin with [low, high, count].
    counts, edges = np.histogram(horizon_returns_pct, bins=_HIST_BINS)
    histogram: List[Dict[str, Any]] = []
    for i, c in enumerate(counts):
        histogram.append({
            "bin_low":  round(float(edges[i]),     2),
            "bin_high": round(float(edges[i + 1]), 2),
            "count":    int(c),
        })

    return {
        "distribution": distribution,
        "probabilities": probabilities,
        "histogram": histogram,
    }


def _run_uncached(symbol: str, horizon_days: int,
                  n_bootstrap: int) -> Dict[str, Any]:
    t0 = time.time()
    rets = _fetch_log_returns(symbol)
    if rets is None:
        return {"symbol": symbol,
                "error": "Insufficient history for prob_forecast"}

    rng = np.random.default_rng()
    horizon_log_rets = _block_bootstrap_paths(
        rets, horizon_days=horizon_days,
        n_bootstrap=n_bootstrap, block_size=_BLOCK_SIZE, rng=rng,
    )
    # Convert log-returns → simple % returns. expm1 is more accurate
    # than exp() - 1 for the small-return regime we mostly live in.
    horizon_returns_pct = np.expm1(horizon_log_rets) * 100.0

    bundle = _summarize(horizon_returns_pct)

    elapsed = time.time() - t0
    log.info("prob_forecast %s: %d paths × %d days from %d-bar window in %.2fs",
             symbol, n_bootstrap, horizon_days, rets.size, elapsed)

    return {
        "symbol": symbol,
        "horizon_days": int(horizon_days),
        "method": "block_bootstrap_returns",
        "block_size": _BLOCK_SIZE,
        "distribution": bundle["distribution"],
        "probabilities": bundle["probabilities"],
        "histogram": bundle["histogram"],
        "n_paths": int(n_bootstrap),
        "input_window_days": int(rets.size),
        "elapsed_ms": int(elapsed * 1000),
        "as_of": _dt.datetime.now(_timezone.utc).date().isoformat(),
    }


# ─── self-test (only when run as a script) ──────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    import json
    logging.basicConfig(level=logging.INFO)
    out = prob_forecast("AAPL", horizon_days=20, n_bootstrap=2000)
    print(json.dumps({k: v for k, v in out.items()
                      if k not in ("histogram",)}, indent=2))
    print("histogram bins:", len(out.get("histogram", [])))
