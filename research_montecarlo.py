"""
Monte Carlo portfolio simulation — research module for AUGUR.

Given a set of current holdings (symbol + market_value), sample N forward
NAV paths over a horizon expressed in TRADING days (the bootstrap samples
daily-return rows, so each step is one trading day, ~252/yr — NOT calendar
days). Typical horizons: 21 (~1mo), 63 (~1qtr), 252 (~1yr) trading days.
Earlier code drew 365 trading-day steps and labeled it "1 year", which is
really ~17 calendar months; use 252 for a true one-year horizon. Methods:

  * historical_bootstrap (default): pull the last ~2y of daily returns for
    each holding, align on a common index, and sample full rows
    (block_size=1) with replacement. Block-bootstrapping by *day* preserves
    the cross-asset correlation embedded in same-day return rows without
    forcing us to estimate a covariance matrix — important when some
    holdings have heavy-tailed or skewed marginals.

  * multivariate_normal: fit mean + cov from the same return matrix, draw
    from `np.random.multivariate_normal`. Faster, smoother, but throws
    away skew / kurtosis. We expose it as a sanity-check / fallback.

Output is a dict containing percentile cones (p5/25/50/75/95) for the NAV
path itself, a terminal distribution summary, and a probability bundle
(prob of loss, drawdown ≥10%, drawdown ≥20%, ≥15% target gain, 1-day 95%
VaR, expected shortfall).

Caching: the full simulation is keyed on
`(montecarlo, portfolio_hash, horizon_days, method, n_paths)` and held for
one hour via `cache_store.coalesce`. The hash is over
`sorted([(symbol_upper, round(market_value, 2)) for ...])` so a small
intraday price drift on the same holdings still hits cache.

Python 3.9 compatible (no PEP 604 unions, no `match` statements).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import time
from datetime import timezone as _timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger("augur.research.montecarlo")

# --- optional integrations ---------------------------------------------------
# All of these are best-effort: the module degrades gracefully if any are
# missing so it can also be exercised from a unit test that doesn't carry
# the full app stack.
try:
    import cache_store  # type: ignore
except Exception:  # pragma: no cover
    cache_store = None  # type: ignore

try:
    import fetcher  # type: ignore
except Exception:  # pragma: no cover
    fetcher = None  # type: ignore


# Cache TTL (seconds). One hour is enough to absorb panel re-renders + the
# usual round of dashboard refreshes without spending another 10k-path
# simulation worth of CPU; well under the ~6h timescale on which holdings
# meaningfully evolve.
_CACHE_TTL = 1 * 3600

# Lookback window for the historical return distribution. ~2y = 504 trading
# days, enough to span at least one full market regime without dragging in
# stale crisis-era data that no longer reflects current vol.
_LOOKBACK_DAYS = 504

# Hard caps to keep the worst-case allocation bounded. A 10k×365×N matrix
# of float64s stays comfortably under 200 MB for any realistic N.
_MAX_PATHS = 20000
_MAX_HORIZON = 365 * 2

# Default RNG when caller passes seed=None — keeps simulations reproducible
# within a single process while still producing fresh draws across runs.
_DEFAULT_RNG = np.random.default_rng()


# ─── public API ─────────────────────────────────────────────────────────────

def simulate_portfolio(
    holdings: List[Dict[str, Any]],
    n_paths: int = 10000,
    horizon_days: int = 252,
    method: str = "historical_bootstrap",
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Run a Monte Carlo simulation of NAV over `horizon_days` and return
    percentile cones + summary stats.

    WHY (Q8): `horizon_days` is in TRADING days (one bootstrapped return row
    per step). The default is 252 (~one calendar year of trading), not 365 —
    365 trading days is ~17 calendar months and was mislabeled "1 year".
    NOTE for orchestrator: the app.py route default still needs updating to
    252 (this module owns only the function default).

    Parameters
    ----------
    holdings : list of dict
        Each dict must have at least `symbol` and `market_value`. Other
        keys are passed through into the response.
    n_paths : int
        Number of MC paths to sample. Capped at _MAX_PATHS.
    horizon_days : int
        Trading-day horizon. Capped at _MAX_HORIZON.
    method : {"historical_bootstrap", "multivariate_normal"}
        Sampling strategy. See module docstring.
    seed : int or None
        Forwarded to `np.random.default_rng(seed)`. Same seed + same
        holdings + same returns ⇒ identical output.
    """
    n_paths = int(max(100, min(n_paths, _MAX_PATHS)))
    horizon_days = int(max(1, min(horizon_days, _MAX_HORIZON)))
    method = (method or "historical_bootstrap").lower()
    if method not in ("historical_bootstrap", "multivariate_normal"):
        method = "historical_bootstrap"

    clean = _clean_holdings(holdings)
    if not clean:
        return _empty_response(holdings, horizon_days, method, n_paths,
                               reason="No valid holdings supplied")

    # Cache lookup. We deliberately *don't* fold seed into the key — a
    # caller that pins a seed for reproducibility usually still wants the
    # cached payload from an unpinned earlier run if it's fresh.
    cache_key = _make_cache_key(clean, horizon_days, method, n_paths)

    def _do():
        # WHY (Q4): _simulate_uncached attaches a raw numpy `_paths` array for
        # prob_of_target's exact computation. simulate_portfolio is the public/
        # cacheable entry point, so strip it here — it isn't JSON-serializable
        # and must not reach the cache or an HTTP response.
        res = _simulate_uncached(clean, n_paths, horizon_days, method, seed)
        res.pop("_paths", None)
        return res

    if cache_store is not None and seed is None:
        try:
            return cache_store.coalesce(cache_key, _CACHE_TTL, _do)
        except Exception:  # pragma: no cover
            log.exception("cache_store.coalesce failed; running uncached")
    return _do()


def prob_of_target(
    holdings: List[Dict[str, Any]],
    target_nav: float,
    horizon_days: int,
    n_paths: int = 10000,
    method: str = "historical_bootstrap",
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Specialized helper. Runs `simulate_portfolio` and returns the
    probability that the *terminal* NAV ≥ target_nav, plus the probability
    of *ever* touching target_nav within the horizon.

    Returns
    -------
    {
        "target_nav": ...,
        "initial_nav": ...,
        "horizon_days": ...,
        "prob_terminal_ge_target": 0.0..1.0,
        "prob_touch_target": 0.0..1.0,
        "implied_return_pct": ...,
    }
    """
    # WHY (Q4): go straight to _simulate_uncached so the raw `_paths` array is
    # guaranteed present. simulate_portfolio routes through the JSON cache,
    # which never carries the numpy paths, so the old code's `_paths` was
    # ALWAYS None — the exact branch was dead and every call fell back to the
    # Gaussian approximation with prob_touch=min(1,prob_term*1.4). That bound
    # is also flat-out wrong for DOWNSIDE targets (a portfolio almost certain
    # to terminate above a low target still has high touch probability, not a
    # small multiple of a tiny terminal prob).
    # Clamp once and reuse so the reported horizon matches the sim that ran.
    hd = int(max(1, min(horizon_days, _MAX_HORIZON)))
    np_clamped = int(max(100, min(n_paths, _MAX_PATHS)))
    clean = _clean_holdings(holdings)
    if not clean:
        sim = _empty_response(holdings, hd, method, np_clamped,
                              reason="No valid holdings supplied")
    else:
        sim = _simulate_uncached(clean, np_clamped, hd,
                                 (method or "historical_bootstrap").lower(), seed)
    initial_nav = sim.get("initial_nav", 0.0) or 0.0
    target_nav = float(target_nav)
    paths = sim.pop("_paths", None)
    # prob_touch from real paths is a discrete-daily approximation (intraday /
    # inter-step crossings aren't modeled, so it can slightly understate the
    # true touch probability). In the empty/failed-sim fallback it collapses
    # onto the terminal probability and is only a rough Normal approximation;
    # flag that case so consumers don't treat prob_touch as exact.
    prob_touch_approximate = False
    if paths is None or getattr(paths, "size", 0) == 0:
        # No paths (empty/failed sim) — approximate via the terminal
        # distribution Normal fit. Touch is then unknowable; report terminal.
        td = sim.get("terminal_distribution", {})
        mu = td.get("mean", initial_nav)
        sd = td.get("std", 0.0) or 1.0
        z = (target_nav - mu) / sd if sd else 0.0
        prob_term = float(0.5 * math.erfc(z / math.sqrt(2)))
        prob_touch = prob_term
        prob_touch_approximate = True
    else:
        terminal = paths[:, -1]
        # Direction matters: an UP-target is "ever ≥ target", a DOWN-target
        # (below the starting NAV) is "ever ≤ target".
        if target_nav >= initial_nav:
            prob_term = float((terminal >= target_nav).mean())
            prob_touch = float((paths >= target_nav).any(axis=1).mean())
        else:
            prob_term = float((terminal <= target_nav).mean())
            prob_touch = float((paths <= target_nav).any(axis=1).mean())

    implied_return_pct = (
        ((target_nav / initial_nav) - 1.0) * 100.0
        if initial_nav and abs(initial_nav) > 1e-6
        else None
    )
    return {
        "target_nav": round(target_nav, 2),
        "initial_nav": round(initial_nav, 2),
        "horizon_days": hd,
        "n_paths": sim.get("n_paths", np_clamped),
        "method": sim.get("method", method),
        "prob_terminal_ge_target": round(prob_term, 4),
        "prob_touch_target": round(prob_touch, 4),
        "prob_touch_approximate": prob_touch_approximate,
        "implied_return_pct": round(implied_return_pct, 2) if implied_return_pct is not None else None,
        "as_of": sim.get("as_of"),
    }


# ─── internals ──────────────────────────────────────────────────────────────

def _clean_holdings(holdings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop holdings without a symbol or market_value, normalise types."""
    if not isinstance(holdings, list):
        return []
    # WHY (Q6): the same symbol can appear in multiple rows (e.g. split lots,
    # multiple accounts). Emitting one cleaned entry per row left duplicate
    # symbols, and the weight vector (mv / total_nav per entry) then assigned
    # that symbol weight TWICE — so a portfolio with one duplicated holding
    # summed to >1.0 (1.5x phantom leverage). Aggregate market_value by symbol.
    agg: Dict[str, float] = {}
    for h in holdings:
        if not isinstance(h, dict):
            continue
        sym = h.get("symbol") or h.get("ticker")
        if not sym:
            continue
        try:
            mv = float(h.get("market_value", 0) or 0)
        except (TypeError, ValueError):
            continue
        if mv <= 0:
            continue
        key = str(sym).upper()
        agg[key] = agg.get(key, 0.0) + mv
    return [{"symbol": k, "market_value": v} for k, v in agg.items()]


def _make_cache_key(clean: List[Dict[str, Any]], horizon_days: int,
                    method: str, n_paths: int) -> Tuple[str, str, int, str, int]:
    h = hashlib.sha1()
    for entry in sorted(clean, key=lambda d: d["symbol"]):
        h.update(entry["symbol"].encode("utf-8"))
        h.update(b"|")
        h.update(f"{round(entry['market_value'], 2)}".encode("utf-8"))
        h.update(b";")
    return ("montecarlo", h.hexdigest()[:20], horizon_days, method, n_paths)


def _empty_response(holdings: List[Dict[str, Any]], horizon_days: int,
                    method: str, n_paths: int,
                    reason: str = "") -> Dict[str, Any]:
    # Independent lists per key — aliasing one `zeros` list into all five
    # would let any in-place edit of one cone silently mutate the others.
    return {
        "holdings": holdings or [],
        "initial_nav": 0.0,
        "n_paths": n_paths,
        "horizon_days": horizon_days,
        "method": method,
        "percentiles": {k: [0.0] * horizon_days for k in ("p05", "p25", "p50", "p75", "p95")},
        "terminal_distribution": {"p05": 0.0, "p25": 0.0, "p50": 0.0, "p75": 0.0, "p95": 0.0,
                                   "mean": 0.0, "std": 0.0},
        "summary": {"prob_loss": 0.0, "prob_drawdown_10pct": 0.0,
                    "prob_drawdown_20pct": 0.0, "prob_target_15pct_gain": 0.0,
                    "value_at_risk_95_pct": 0.0, "expected_shortfall_95_pct": 0.0},
        "dropped_symbols": [],
        "warning": reason,
        "as_of": _dt.datetime.now(_timezone.utc).date().isoformat(),
    }


def _fetch_returns(symbol: str, lookback_days: int) -> Optional[pd.Series]:
    """Daily returns Series indexed by date, or None if unavailable.

    Routes through fetcher.get_chart_data so this benefits from the v0.1.6
    Yahoo-direct fallback chain. A naked yf.Ticker.history call here would
    silently return empty whenever yfinance's crumb auth is broken, even
    though the same data is reachable via Yahoo's v8 chart endpoint.

    Pulls a slightly longer period than asked so weekends / holidays don't
    starve the matrix after we trim NaNs.
    """
    if fetcher is None:
        return None
    period = "2y" if lookback_days >= 400 else "1y"
    try:
        bars = fetcher.get_chart_data(symbol, period=period, interval="1d")
    except Exception as e:
        log.debug("fetcher.get_chart_data failed for %s: %s", symbol, e)
        return None
    if not bars:
        return None
    # bars is a list of {time, open, high, low, close, volume}; build a
    # date-indexed Close series, then convert to daily returns.
    close = pd.Series(
        [float(b["close"]) for b in bars if b.get("close") is not None],
        index=pd.to_datetime(
            [b["time"] for b in bars if b.get("close") is not None],
            unit="s", utc=True,
        ).tz_convert(None),
        name=symbol.upper(),
    )
    if len(close) < 30:
        return None
    rets = close.pct_change().dropna()
    if rets.empty:
        return None
    rets.name = symbol.upper()
    if len(rets) > lookback_days:
        rets = rets.iloc[-lookback_days:]
    return rets


def _build_returns_matrix(symbols: List[str]) -> Tuple[np.ndarray, List[str], List[str]]:
    """
    Returns (R, kept_symbols, dropped_symbols) where R has shape (T, N).
    Symbols whose history is missing or has fewer than 30 aligned days are
    dropped. T = number of dates with full coverage across kept symbols.
    """
    series = []
    dropped = []
    for s in symbols:
        r = _fetch_returns(s, _LOOKBACK_DAYS)
        if r is None or len(r) < 30:
            dropped.append(s)
            continue
        series.append(r)
    if not series:
        return np.zeros((0, 0)), [], dropped

    # Outer join then drop any row with a NaN — guarantees every kept row
    # has a real return for every kept holding, which is what makes the
    # block bootstrap valid.
    df = pd.concat(series, axis=1, join="outer").sort_index().dropna()
    # WHY (Q14): the old fallback re-joined with join="inner" + dropna(), but
    # that is identical to outer+dropna() (both keep only rows with every
    # column present) — a literal no-op. The real cause of a starved matrix is
    # ONE much-newer symbol forcing the common date window tiny. Fix: drop the
    # shortest-history symbol(s) into `dropped` and rebuild until we clear the
    # 30-row floor (or run out of symbols).
    while (df.empty or len(df) < 30) and len(series) > 1:
        # Evict the symbol with the fewest observations — it is the binding
        # constraint on the intersection of dates.
        shortest_i = min(range(len(series)), key=lambda i: len(series[i]))
        dropped.append(str(series[shortest_i].name))
        series.pop(shortest_i)
        df = pd.concat(series, axis=1, join="outer").sort_index().dropna()
    if df.empty or len(df) < 30:
        return np.zeros((0, 0)), [], dropped + [str(s.name) for s in series]

    kept = [str(c) for c in df.columns]
    return df.to_numpy(dtype=np.float64, copy=False), kept, dropped


def _simulate_paths_bootstrap(R: np.ndarray, weights: np.ndarray,
                              n_paths: int, horizon_days: int,
                              rng: np.random.Generator) -> np.ndarray:
    """
    Block bootstrap with block_size=1. Sample `horizon_days * n_paths`
    row indices uniformly from R, slot into a (n_paths, horizon_days, N)
    cube, project onto weights, compound into NAV multipliers.

    Returns
    -------
    nav_paths : np.ndarray of shape (n_paths, horizon_days)
        NAV / initial_NAV at each step. Multiply by initial_nav to get $.
    """
    T = R.shape[0]
    # Flat index pool then reshape — single RNG call is much faster than
    # n_paths separate calls.
    idx = rng.integers(0, T, size=n_paths * horizon_days)
    # (n_paths, horizon_days, N) — but we only need portfolio returns, so
    # collapse the asset axis on the fly with the einsum.
    sampled = R[idx].reshape(n_paths, horizon_days, R.shape[1])
    port_rets = sampled @ weights  # (n_paths, horizon_days)
    # Compound. log1p + cumsum + expm1 is more numerically stable than
    # cumprod for long horizons.
    nav = np.expm1(np.cumsum(np.log1p(port_rets), axis=1)) + 1.0
    return nav


def _simulate_paths_mvn(R: np.ndarray, weights: np.ndarray,
                        n_paths: int, horizon_days: int,
                        rng: np.random.Generator) -> np.ndarray:
    """Multivariate-normal fallback. Same return shape as bootstrap."""
    mu = R.mean(axis=0)
    cov = np.cov(R, rowvar=False)
    # Ensure cov is at least 2D (single-asset case).
    if cov.ndim == 0:
        cov = np.array([[float(cov)]])
    else:
        # np.cov on a (T, N) matrix always yields an (N, N) matrix for N > 1.
        # atleast_2d is a harmless no-op there; it only matters for a stray
        # 1-D variance vector, which it shapes to (1, N) so a malformed input
        # fails loudly in multivariate_normal rather than being silently
        # squashed to (1, 1) (which would drop assets).
        cov = np.atleast_2d(cov)
    # Add a small ridge to keep the covariance positive-definite — without
    # this, near-collinear holdings (e.g. SPY + VOO) blow up Cholesky.
    ridge = 1e-12 * np.eye(cov.shape[0])
    # Suppress harmless BLAS overflow/divide warnings that fire on macOS
    # Accelerate when the (n_paths, horizon, N) draw cube is large.
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        draws = rng.multivariate_normal(mu, cov + ridge,
                                        size=(n_paths, horizon_days))
    # Clip absurd tail draws to keep log1p stable (Gaussian tails can
    # produce ~ -1.0 returns which would log1p -> -inf and break the
    # cumsum). Clip to -50%/+50% daily — anything beyond is noise.
    draws = np.clip(draws, -0.5, 0.5)
    port_rets = draws @ weights
    nav = np.expm1(np.cumsum(np.log1p(port_rets), axis=1)) + 1.0
    return nav


def _summarize(nav_paths: np.ndarray, initial_nav: float, horizon_days: int,
               R: np.ndarray, weights: np.ndarray) -> Dict[str, Any]:
    """Build the percentile cones + summary dict from raw nav_paths."""
    # nav_paths is (n_paths, horizon_days) multipliers; convert to $.
    nav_dollars = nav_paths * initial_nav
    # Percentile cones — axis=0 collapses paths.
    pcts = np.percentile(nav_dollars, [5, 25, 50, 75, 95], axis=0)
    percentiles = {
        "p05": [round(float(v), 2) for v in pcts[0]],
        "p25": [round(float(v), 2) for v in pcts[1]],
        "p50": [round(float(v), 2) for v in pcts[2]],
        "p75": [round(float(v), 2) for v in pcts[3]],
        "p95": [round(float(v), 2) for v in pcts[4]],
    }

    terminal = nav_dollars[:, -1]
    tpcts = np.percentile(terminal, [5, 25, 50, 75, 95])
    terminal_distribution = {
        "p05": round(float(tpcts[0]), 2),
        "p25": round(float(tpcts[1]), 2),
        "p50": round(float(tpcts[2]), 2),
        "p75": round(float(tpcts[3]), 2),
        "p95": round(float(tpcts[4]), 2),
        "mean": round(float(terminal.mean()), 2),
        "std": round(float(terminal.std(ddof=1)) if len(terminal) > 1 else 0.0, 2),
    }

    # Path-level drawdown: for every path, max drawdown from running peak.
    running_peak = np.maximum.accumulate(nav_paths, axis=1)
    drawdowns = (nav_paths / running_peak) - 1.0  # ≤ 0
    max_dd_per_path = drawdowns.min(axis=1)  # most-negative value per path

    prob_loss = float((terminal < initial_nav).mean())
    prob_dd_10 = float((max_dd_per_path <= -0.10).mean())
    prob_dd_20 = float((max_dd_per_path <= -0.20).mean())
    prob_target_15 = float((terminal >= initial_nav * 1.15).mean())

    # 1-day 95% VaR / ES — computed from the *historical* portfolio return
    # distribution, not the simulated terminal, so it's still meaningful
    # even on short horizons. Expressed as positive % loss numbers.
    port_hist = R @ weights  # (T,)
    if port_hist.size:
        var_q = float(np.percentile(port_hist, 5))   # 5th percentile is the 95% VaR tail
        # Expected shortfall = mean of losses worse than the VaR threshold.
        tail = port_hist[port_hist <= var_q]
        es = float(tail.mean()) if tail.size else var_q
        var_pct = round(-var_q * 100.0, 2)
        es_pct = round(-es * 100.0, 2)
    else:
        var_pct = 0.0
        es_pct = 0.0

    summary = {
        "prob_loss": round(prob_loss, 4),
        "prob_drawdown_10pct": round(prob_dd_10, 4),
        "prob_drawdown_20pct": round(prob_dd_20, 4),
        "prob_target_15pct_gain": round(prob_target_15, 4),
        "value_at_risk_95_pct": var_pct,
        "expected_shortfall_95_pct": es_pct,
    }

    return {
        "percentiles": percentiles,
        "terminal_distribution": terminal_distribution,
        "summary": summary,
    }


def _simulate_uncached(clean: List[Dict[str, Any]], n_paths: int,
                       horizon_days: int, method: str,
                       seed: Optional[int]) -> Dict[str, Any]:
    """Actual heavy lifting — separated so cache_store.coalesce can wrap it."""
    t0 = time.time()
    # Guarantee _summarize's terminal statistics (std ddof=1, percentiles) are
    # always well-defined regardless of caller path: need at least 2 paths.
    n_paths = int(max(2, min(n_paths, _MAX_PATHS)))
    horizon_days = int(max(1, min(horizon_days, _MAX_HORIZON)))
    symbols = [h["symbol"] for h in clean]
    R, kept, dropped = _build_returns_matrix(symbols)

    initial_nav = sum(h["market_value"] for h in clean)

    if R.size == 0 or len(kept) == 0:
        resp = _empty_response(clean, horizon_days, method, n_paths,
                               reason="No price history available for any holding")
        resp["dropped_symbols"] = dropped
        resp["initial_nav"] = round(initial_nav, 2)
        return resp

    # Drop holdings without history from the clean list, then renormalise
    # weights against the *surviving* market value. This matches user
    # intuition: "what's my MC if we ignore the assets we can't price?".
    kept_set = set(kept)
    surviving = [h for h in clean if h["symbol"] in kept_set]
    surviving_mv = sum(h["market_value"] for h in surviving)
    if surviving_mv <= 0:
        resp = _empty_response(clean, horizon_days, method, n_paths,
                               reason="All holdings dropped due to missing history")
        resp["dropped_symbols"] = dropped
        resp["initial_nav"] = round(initial_nav, 2)
        return resp

    # Build weight vector in the same order as the columns of R.
    mv_by_sym = {h["symbol"]: h["market_value"] for h in surviving}
    raw_mv = np.array([mv_by_sym.get(s, 0.0) for s in kept], dtype=np.float64)
    # Surface silent weight loss: a kept column that doesn't resolve to a
    # positive market value (e.g. a name mangled by pandas column coercion)
    # would otherwise get 0.0 weight and vanish from the portfolio without
    # ever appearing in dropped_symbols. Record + renormalize over the rest.
    zero_w = [kept[i] for i in range(len(kept)) if raw_mv[i] <= 0.0]
    if zero_w:
        for s in zero_w:
            if s not in dropped:
                dropped.append(s)
    eff_mv = float(raw_mv.sum())
    if eff_mv <= 0:
        resp = _empty_response(clean, horizon_days, method, n_paths,
                               reason="All holdings dropped due to missing history")
        resp["dropped_symbols"] = dropped
        resp["initial_nav"] = round(initial_nav, 2)
        return resp
    weights = raw_mv / eff_mv

    rng = np.random.default_rng(seed) if seed is not None else np.random.default_rng()

    if method == "multivariate_normal":
        nav_paths = _simulate_paths_mvn(R, weights, n_paths, horizon_days, rng)
    else:
        nav_paths = _simulate_paths_bootstrap(R, weights, n_paths, horizon_days, rng)

    bundle = _summarize(nav_paths, surviving_mv, horizon_days, R, weights)

    # Decorate holdings response with weight (relative to the surviving
    # NAV so callers can show the % each position is contributing).
    holdings_out = []
    for h in surviving:
        w = h["market_value"] / surviving_mv if surviving_mv else 0.0
        holdings_out.append({
            "symbol": h["symbol"],
            "market_value": round(h["market_value"], 2),
            "weight": round(w, 4),
        })

    elapsed = time.time() - t0
    log.info("monte carlo: %d paths x %d days, %d holdings (kept=%d, dropped=%d) in %.2fs",
             n_paths, horizon_days, len(clean), len(kept), len(dropped), elapsed)

    return {
        "holdings": holdings_out,
        "initial_nav": round(surviving_mv, 2),
        "n_paths": n_paths,
        "horizon_days": horizon_days,
        "method": method,
        "percentiles": bundle["percentiles"],
        "terminal_distribution": bundle["terminal_distribution"],
        "summary": bundle["summary"],
        "dropped_symbols": dropped,
        "lookback_days_used": int(R.shape[0]),
        "elapsed_ms": int(elapsed * 1000),
        "as_of": _dt.datetime.now(_timezone.utc).date().isoformat(),
        # WHY (Q4): raw dollar NAV paths, attached so prob_of_target can do an
        # EXACT terminal/touch probability instead of a Gaussian approximation.
        # Callers that hit the JSON cache won't see this (np array isn't
        # serialized there) — prob_of_target deliberately calls _simulate_uncached
        # directly to guarantee it's present. Stripped before any HTTP response.
        "_paths": nav_paths * surviving_mv,
    }


# ─── self-test (only run when executed as a script) ─────────────────────────

if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    out = simulate_portfolio(
        [{"symbol": "AAPL", "market_value": 10000},
         {"symbol": "GLD",  "market_value": 10000}],
        n_paths=2000, horizon_days=90, seed=42,
    )
    print(json.dumps({k: v for k, v in out.items()
                      if k in ("initial_nav", "n_paths", "horizon_days",
                               "terminal_distribution", "summary",
                               "dropped_symbols", "as_of")},
                     indent=2))
    print("p50 path length:", len(out["percentiles"]["p50"]))
