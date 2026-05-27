"""
research_backtest.py — Walk-forward backtest framework for AUGUR signals.

Provides a reusable harness that replays any signal-producing function over a
historical price series and reports performance metrics. The point is to turn
the app's many "trust me, this is a good signal" outputs into measured,
auditable historical hit rates.

Design
------
A *Signal* is any callable matching ::

    (history_df_up_to_t: pd.DataFrame, params: dict) -> dict | None

where the returned dict has shape::

    {"direction": "BUY" | "SELL" | "FLAT",
     "confidence": float in [0, 1],
     "horizon_days": int}

Returning ``None`` (or a FLAT dict) means "no signal today" and the day is
skipped. The harness, ``run_backtest``, iterates one bar at a time. At each
day ``t`` the signal sees only bars ``[0, t)`` (strictly past), and a fired
signal is scored on the realised close-to-close return from ``t`` to
``t + horizon_days``.

Look-ahead caveat for the ml_forecast adapter
---------------------------------------------
The ``adapter_momentum`` and ``adapter_mean_reversion`` adapters operate on the
windowed slice only — they are leak-free. ``adapter_ml_forecast`` is best
effort: ``ml_forecast.ml_forecast`` pulls its own 2y of history via the
fetcher, so it cannot easily be replayed at an arbitrary point in time. The
MVP calls it on the current symbol regardless of the walk-forward cursor and
re-uses that single forecast across all bars in the backtest — meaning the
"hit rate" measured for the ml_forecast adapter is essentially "how often does
the model's *latest* call match the realised direction at each bar t". This is
useful as a smoke test but is NOT a leak-free out-of-sample evaluation. A
future revision should add an ``as_of`` parameter to ``ml_forecast.ml_forecast``
so the adapter can replay the model at each bar with point-in-time inputs.

Metrics
-------
- ``n_signals``           number of non-FLAT signals fired
- ``hit_rate``            fraction of signals whose realised return matched
                          their direction (BUY positive, SELL negative)
- ``avg_return_per_signal``  mean realised pct return per signal (sign
                          flipped for SELLs so positive = profitable)
- ``total_return_pct``    compounded equity-curve return (sum of pnl-per-bar
                          where each bar holds the position established by the
                          most recent signal, FLAT in gaps)
- ``sharpe``              annualised Sharpe of the per-bar pnl, ``sqrt(252)``
                          scaling. Returns 0.0 if too few bars.
- ``max_drawdown_pct``    worst peak-to-trough drawdown of the equity curve
- ``equity_curve``        list of ``{date, nav}`` (nav starts at 1.0)
- ``signals``             per-signal log of
                          ``{issued_at, direction, confidence,
                            realized_return}``

Cache: results are cached by ``(symbol, signal_name, start, end, params_hash)``
via ``cache_store.coalesce`` for 6 hours.

Python 3.9 compatible. No new third-party deps; only numpy / pandas which the
rest of AUGUR already requires.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol

import numpy as np
import pandas as pd

log = logging.getLogger("augur.backtest")

_CACHE_TTL = 6 * 3600
_TRADING_DAYS_PER_YEAR = 252


# ───────────────────────────── Signal protocol ──────────────────────────────

class Signal(Protocol):
    """Callable contract for a signal-producing function.

    A Signal sees ``history`` (an OHLCV ``DataFrame`` indexed by date,
    containing only bars strictly *before* the decision point) and a free-form
    ``params`` dict. It returns either ``None`` for no-signal or a dict with
    ``direction`` ('BUY'|'SELL'|'FLAT'), ``confidence`` in [0, 1], and
    ``horizon_days`` (positive int).
    """

    __name__: str

    def __call__(
        self,
        history: pd.DataFrame,
        params: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        ...


# ───────────────────────────── data plumbing ────────────────────────────────

_MIN_BARS_FOR_BACKTEST = 60   # need at least this many *total* bars
_MIN_LOOKBACK = 30            # bars required before we start asking signals


def _load_history(symbol: str) -> Optional[pd.DataFrame]:
    """Pull 5y of daily bars and normalise to a sorted ``DataFrame``.

    Returns ``None`` if the fetcher can't produce a usable series. Five years
    is the longest fetcher.get_chart_data supports cleanly while still keeping
    one request light enough that the cache warmer can prefetch it.
    """
    try:
        import fetcher
    except Exception as e:
        log.warning("fetcher import failed: %s", e)
        return None

    try:
        bars = fetcher.get_chart_data(symbol, period="5y", interval="1d")
    except Exception as e:
        log.warning("get_chart_data(%s) failed: %s", symbol, e)
        return None

    if not bars:
        return None

    df = pd.DataFrame(bars)
    if "time" not in df.columns or "close" not in df.columns:
        return None
    df["Date"] = pd.to_datetime(df["time"], unit="s").dt.normalize()
    df = df.set_index("Date").rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    df = df[df["Close"] > 0]
    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()
    return df


def _slice_window(
    df: pd.DataFrame,
    start: Optional[str],
    end: Optional[str],
) -> pd.DataFrame:
    """Restrict ``df`` to the [start, end] inclusive window if provided."""
    out = df
    if start:
        try:
            ts = pd.to_datetime(start).normalize()
            out = out[out.index >= ts]
        except Exception:
            pass
    if end:
        try:
            ts = pd.to_datetime(end).normalize()
            out = out[out.index <= ts]
        except Exception:
            pass
    return out


# ───────────────────────────── signal adapters ──────────────────────────────

def adapter_momentum(
    history: pd.DataFrame,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """20-day momentum signal — pure & leak-free.

    BUY when the most recent 20-day return is in the top quartile of the
    trailing 252-day distribution of 20-day rolling returns; SELL when in the
    bottom quartile; FLAT otherwise.

    Returns ``None`` if there isn't enough history to compute the distribution.
    """
    params = params or {}
    lookback = int(params.get("lookback_days", 252))
    window = int(params.get("window_days", 20))
    horizon = int(params.get("horizon_days", 20))

    close = history.get("Close")
    if close is None or len(close) < (lookback + window + 1):
        return None

    # Trailing distribution of `window`-day returns over `lookback` days.
    rolling = close.pct_change(window).dropna()
    if len(rolling) < lookback:
        return None
    dist = rolling.iloc[-lookback:]
    current = float(rolling.iloc[-1])
    q25 = float(dist.quantile(0.25))
    q75 = float(dist.quantile(0.75))

    if not (math.isfinite(current) and math.isfinite(q25) and math.isfinite(q75)):
        return None

    if current >= q75:
        direction = "BUY"
        # Confidence: how far above q75 vs the q75..max range
        spread = max(float(dist.max()) - q75, 1e-9)
        conf = float(np.clip((current - q75) / spread, 0.0, 1.0))
    elif current <= q25:
        direction = "SELL"
        spread = max(q25 - float(dist.min()), 1e-9)
        conf = float(np.clip((q25 - current) / spread, 0.0, 1.0))
    else:
        return {"direction": "FLAT", "confidence": 0.0, "horizon_days": horizon}

    # Floor confidence so the worst-quartile-but-just-barely signals still log
    # at a measurable level; we don't want everything bunched at 0.
    conf = float(np.clip(0.2 + 0.8 * conf, 0.2, 0.99))
    return {
        "direction": direction,
        "confidence": round(conf, 3),
        "horizon_days": horizon,
    }


def adapter_mean_reversion(
    history: pd.DataFrame,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Mean-reversion via z-score of close vs 50d MA — pure & leak-free.

    BUY when z < -1.5 (price stretched below average), SELL when z > 1.5
    (stretched above), FLAT otherwise. Confidence scales with the absolute
    z-score past the threshold.
    """
    params = params or {}
    ma_window = int(params.get("ma_window", 50))
    z_threshold = float(params.get("z_threshold", 1.5))
    horizon = int(params.get("horizon_days", 10))

    close = history.get("Close")
    if close is None or len(close) < (ma_window + 5):
        return None

    window_slice = close.iloc[-ma_window:]
    ma = float(window_slice.mean())
    sd = float(window_slice.std())
    if sd <= 1e-9:
        return None

    last = float(close.iloc[-1])
    z = (last - ma) / sd
    if not math.isfinite(z):
        return None

    abs_z = abs(z)
    if abs_z < z_threshold:
        return {"direction": "FLAT", "confidence": 0.0, "horizon_days": horizon}

    # Reversion: high z → SELL (expect drop), low z → BUY (expect bounce)
    direction = "SELL" if z > 0 else "BUY"
    # Cap z-score influence at 4 sigma — anything beyond that is more likely
    # data error than a tradable extreme.
    conf = float(np.clip((abs_z - z_threshold) / (4.0 - z_threshold), 0.0, 1.0))
    conf = float(np.clip(0.2 + 0.8 * conf, 0.2, 0.99))
    return {
        "direction": direction,
        "confidence": round(conf, 3),
        "horizon_days": horizon,
    }


# Module-level cache for the per-symbol ml_forecast call so the adapter, which
# is invoked once per bar in the backtest, doesn't retrain the RF model on
# every iteration.
_ML_FORECAST_CACHE: Dict[str, Dict[str, Any]] = {}


def adapter_ml_forecast(
    history: pd.DataFrame,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """ml_forecast.ml_forecast wrapper — BEST EFFORT, NOT leak-free.

    ``ml_forecast.ml_forecast(symbol)`` pulls its own 2y of history via the
    fetcher, so we can't easily replay it at a point-in-time. The adapter
    derives the symbol from the ``params['symbol']`` field passed in by
    ``run_backtest`` and calls ``ml_forecast`` once, then reuses that single
    forecast across every bar of the backtest. The realised direction matched
    against this single forecast is informative but not a leak-free
    out-of-sample evaluation.

    A future version of ``ml_forecast`` would need an ``as_of=ts`` argument so
    each bar could ask the model what it would have predicted that day.
    """
    params = params or {}
    symbol = params.get("symbol")
    if not symbol:
        return None
    prob_threshold = float(params.get("prob_threshold", 0.55))
    horizon = int(params.get("horizon_days", 20))

    cached = _ML_FORECAST_CACHE.get(symbol)
    if cached is None:
        try:
            import ml_forecast
            cached = ml_forecast.ml_forecast(symbol) or {}
        except Exception as e:
            log.warning("ml_forecast(%s) failed in adapter: %s", symbol, e)
            cached = {}
        _ML_FORECAST_CACHE[symbol] = cached

    rf = (cached or {}).get("rf_classifier") or {}
    prob_up = rf.get("prob_up_20d")
    if prob_up is None:
        return None
    try:
        prob_up = float(prob_up)
    except Exception:
        return None

    if prob_up >= prob_threshold:
        direction = "BUY"
        conf = float(np.clip((prob_up - 0.5) * 2.0, 0.0, 1.0))
    elif prob_up <= (1.0 - prob_threshold):
        direction = "SELL"
        conf = float(np.clip((0.5 - prob_up) * 2.0, 0.0, 1.0))
    else:
        return {"direction": "FLAT", "confidence": 0.0, "horizon_days": horizon}

    conf = float(np.clip(0.2 + 0.8 * conf, 0.2, 0.99))
    return {
        "direction": direction,
        "confidence": round(conf, 3),
        "horizon_days": horizon,
    }


# Friendly names for the API + cache key. Maps name → callable.
ADAPTERS: Dict[str, Signal] = {
    "momentum":        adapter_momentum,
    "mean_reversion":  adapter_mean_reversion,
    "ml_forecast":     adapter_ml_forecast,
}


def get_adapter(name: str) -> Optional[Signal]:
    """Return the adapter callable for ``name`` or ``None`` if unknown."""
    return ADAPTERS.get((name or "").lower().strip())


# ─────────────────────────────── metrics ────────────────────────────────────

def _compute_metrics(
    pnl_by_bar: List[float],
    dates: List[pd.Timestamp],
    signal_log: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Roll up per-bar pnl + signal log into the canonical metrics dict."""
    n_signals = len(signal_log)
    realised = [s.get("realized_return") for s in signal_log
                if isinstance(s.get("realized_return"), (int, float))
                and math.isfinite(s["realized_return"])]

    # Hit rate: BUY wins when realized > 0, SELL wins when realized < 0. We
    # normalise to "directional pnl" (positive when the signal called it right)
    # by sign-flipping SELL returns when computing the realised series.
    directional_pnls: List[float] = []
    for s in signal_log:
        r = s.get("realized_return")
        if r is None or not isinstance(r, (int, float)) or not math.isfinite(r):
            continue
        if s.get("direction") == "SELL":
            directional_pnls.append(-r)
        else:
            directional_pnls.append(r)

    hit_rate = (
        sum(1 for p in directional_pnls if p > 0) / len(directional_pnls)
        if directional_pnls else 0.0
    )
    avg_return_per_signal = (
        float(np.mean(directional_pnls)) if directional_pnls else 0.0
    )

    # Equity curve from per-bar pnl. nav_t = nav_{t-1} * (1 + pnl_t).
    nav = 1.0
    equity_curve: List[Dict[str, Any]] = []
    nav_series: List[float] = []
    for i, p in enumerate(pnl_by_bar):
        nav = nav * (1.0 + (p if isinstance(p, (int, float)) and math.isfinite(p) else 0.0))
        nav_series.append(nav)
        d = dates[i] if i < len(dates) else None
        equity_curve.append({
            "date": d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d),
            "nav":  round(float(nav), 6),
        })

    total_return_pct = (nav - 1.0) * 100.0 if nav_series else 0.0

    # Sharpe on per-bar pnl (these are daily realised returns when holding;
    # zeros when flat — that's fine, it just lowers vol).
    if pnl_by_bar and len(pnl_by_bar) > 2:
        arr = np.asarray(pnl_by_bar, dtype=float)
        arr = arr[np.isfinite(arr)]
        if arr.size > 2 and float(arr.std()) > 1e-12:
            sharpe = float(arr.mean() / arr.std() * math.sqrt(_TRADING_DAYS_PER_YEAR))
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    # Max drawdown of nav series.
    max_dd = 0.0
    if nav_series:
        peak = nav_series[0]
        for n in nav_series:
            if n > peak:
                peak = n
            dd = (n - peak) / peak * 100.0
            if dd < max_dd:
                max_dd = dd

    return {
        "n_signals":            int(n_signals),
        "hit_rate":             round(float(hit_rate), 4),
        "avg_return_per_signal": round(float(avg_return_per_signal) * 100.0, 4),
        "total_return_pct":     round(float(total_return_pct), 4),
        "sharpe":               round(float(sharpe), 4) if math.isfinite(sharpe) else 0.0,
        "max_drawdown_pct":     round(float(max_dd), 4),
        "equity_curve":         equity_curve,
        "signals":              signal_log,
    }


# ──────────────────────────── core walk-forward ─────────────────────────────

def _params_hash(params: Optional[Dict[str, Any]]) -> str:
    """Stable short hash for ``params`` so the cache key stays compact."""
    if not params:
        return "default"
    try:
        # Drop symbol from the hash — already in the cache key.
        p = {k: v for k, v in params.items() if k != "symbol"}
        if not p:
            return "default"
        blob = json.dumps(p, sort_keys=True, default=str)
        return hashlib.md5(blob.encode("utf-8")).hexdigest()[:10]
    except Exception:
        return "custom"


def _resolve_signal_name(signal_fn: Callable) -> str:
    """Best-effort human-readable name for the signal callable."""
    for name, fn in ADAPTERS.items():
        if fn is signal_fn:
            return name
    return getattr(signal_fn, "__name__", "signal")


def _run(
    signal_fn: Signal,
    symbol: str,
    start: Optional[str],
    end: Optional[str],
    horizon_override: Optional[int],
    params: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """The actual backtest loop; wrapped by ``run_backtest`` for caching."""
    df_full = _load_history(symbol)
    if df_full is None or len(df_full) < _MIN_BARS_FOR_BACKTEST:
        return {
            "symbol": symbol,
            "signal": _resolve_signal_name(signal_fn),
            "error": "Insufficient history for backtest (need 60+ daily bars).",
            "n_signals": 0,
            "hit_rate": 0.0,
            "avg_return_per_signal": 0.0,
            "total_return_pct": 0.0,
            "sharpe": 0.0,
            "max_drawdown_pct": 0.0,
            "equity_curve": [],
            "signals": [],
        }

    df = _slice_window(df_full, start, end)
    if len(df) < _MIN_BARS_FOR_BACKTEST:
        return {
            "symbol": symbol,
            "signal": _resolve_signal_name(signal_fn),
            "error": "Window has fewer than 60 bars after applying start/end.",
            "n_signals": 0,
            "hit_rate": 0.0,
            "avg_return_per_signal": 0.0,
            "total_return_pct": 0.0,
            "sharpe": 0.0,
            "max_drawdown_pct": 0.0,
            "equity_curve": [],
            "signals": [],
        }

    # Inject symbol into params so adapters that need it (ml_forecast) can read
    # it without us changing the protocol signature.
    sig_params: Dict[str, Any] = dict(params or {})
    sig_params.setdefault("symbol", symbol)

    closes = df["Close"].to_numpy(dtype=float)
    dates = list(df.index)
    n = len(df)

    # position[t] is the direction held between close[t] and close[t+1].
    # +1 = long, -1 = short, 0 = flat. We accumulate by overlaying every
    # active signal's lifetime; if multiple signals fire we let the most
    # recent one dominate (typical backtest convention for non-position-sized
    # signals).
    position = np.zeros(n - 1, dtype=float)
    signal_log: List[Dict[str, Any]] = []

    for t in range(_MIN_LOOKBACK, n):
        # The signal sees strictly past bars only — slice up to (but not
        # including) bar t. This is the leak-prevention invariant.
        hist_slice = df.iloc[:t]

        try:
            out = signal_fn(hist_slice, sig_params)
        except Exception as e:
            log.warning("signal_fn raised at t=%s: %s", t, e)
            continue

        if out is None:
            continue
        direction = (out.get("direction") or "").upper()
        if direction not in ("BUY", "SELL"):
            continue

        try:
            horizon = int(horizon_override if horizon_override else out.get("horizon_days", 20))
        except Exception:
            horizon = 20
        if horizon < 1:
            horizon = 1

        confidence = out.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except Exception:
            confidence = 0.0

        # Realised return: close[t-1] is the most recent bar the signal saw;
        # we enter at close[t-1] and exit at close[min(t-1+horizon, n-1)].
        # The reason for using t-1 as entry: at the moment the signal fires
        # using hist_slice = df[:t], the most recent observable bar is t-1.
        # Executing at *that* close is the standard "decision at close, fill
        # at close" convention.
        entry_idx = t - 1
        exit_idx = min(entry_idx + horizon, n - 1)
        if exit_idx <= entry_idx:
            continue
        entry_px = closes[entry_idx]
        exit_px  = closes[exit_idx]
        if entry_px <= 0 or not math.isfinite(entry_px) or not math.isfinite(exit_px):
            continue
        realized = (exit_px / entry_px) - 1.0

        signal_log.append({
            "issued_at":       dates[entry_idx].strftime("%Y-%m-%d"),
            "direction":       direction,
            "confidence":      round(float(confidence), 3),
            "horizon_days":    int(horizon),
            "entry_price":     round(float(entry_px), 4),
            "exit_price":      round(float(exit_px), 4),
            "exit_date":       dates[exit_idx].strftime("%Y-%m-%d"),
            "realized_return": round(float(realized), 6),
        })

        # Mark the position vector for the held interval. Overwrite (rather
        # than accumulate) so a new signal cleanly replaces an older one.
        pos_val = 1.0 if direction == "BUY" else -1.0
        for k in range(entry_idx, exit_idx):
            if k < len(position):
                position[k] = pos_val

    # Per-bar pnl = position[t] * (close[t+1]/close[t] - 1). Build this once
    # the position vector is fully populated.
    pnl_by_bar: List[float] = [0.0]   # day 0 has no prior return
    for t in range(n - 1):
        p = position[t]
        if p == 0.0:
            pnl_by_bar.append(0.0)
            continue
        prev = closes[t]
        nxt  = closes[t + 1]
        if prev <= 0 or not math.isfinite(prev) or not math.isfinite(nxt):
            pnl_by_bar.append(0.0)
            continue
        r = (nxt / prev) - 1.0
        pnl_by_bar.append(float(p) * r)

    metrics = _compute_metrics(pnl_by_bar, dates, signal_log)

    # Trim the signals list to keep response payload small but still useful.
    # The top 5 / bottom 5 are what the UI shows; full list capped at 200.
    full_signals = metrics["signals"]
    if len(full_signals) > 200:
        # Keep the 100 most recent (chronological) and the 100 most extreme
        # by realized_return.
        by_extremity = sorted(
            full_signals,
            key=lambda s: abs(s.get("realized_return", 0.0)),
            reverse=True,
        )[:100]
        recent = full_signals[-100:]
        merged: Dict[str, Dict[str, Any]] = {}
        for s in recent + by_extremity:
            merged[s["issued_at"]] = s
        metrics["signals"] = sorted(merged.values(), key=lambda s: s["issued_at"])
        metrics["signals_truncated_to"] = 200

    return {
        "symbol":   symbol,
        "signal":   _resolve_signal_name(signal_fn),
        "start":    start or (dates[0].strftime("%Y-%m-%d") if dates else None),
        "end":      end   or (dates[-1].strftime("%Y-%m-%d") if dates else None),
        "params":   {k: v for k, v in sig_params.items() if k != "symbol"},
        "as_of":    datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        **metrics,
    }


def run_backtest(
    signal_fn: Signal,
    symbol: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    horizon_override: Optional[int] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Walk-forward backtest of ``signal_fn`` over ``symbol`` in [start, end].

    See the module docstring for the metrics returned. Results are cached for
    6h via ``cache_store.coalesce`` keyed by
    ``("backtest", symbol, signal_name, start, end, params_hash)``. Pass a
    distinct ``params`` dict to invalidate.
    """
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return {
            "symbol":   "",
            "signal":   _resolve_signal_name(signal_fn),
            "error":    "empty symbol",
            "n_signals": 0, "hit_rate": 0.0, "avg_return_per_signal": 0.0,
            "total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0,
            "equity_curve": [], "signals": [],
        }

    name = _resolve_signal_name(signal_fn)
    ph = _params_hash(params)

    def _do():
        return _run(signal_fn, symbol, start, end, horizon_override, params)

    try:
        import cache_store
        return cache_store.coalesce(
            ("backtest", symbol, name, start or "_", end or "_",
             str(horizon_override or "_"), ph),
            _CACHE_TTL,
            _do,
        )
    except Exception as e:
        log.warning("cache_store unavailable for backtest %s/%s: %s", symbol, name, e)
        return _do()


__all__ = [
    "Signal",
    "ADAPTERS",
    "adapter_momentum",
    "adapter_mean_reversion",
    "adapter_ml_forecast",
    "get_adapter",
    "run_backtest",
]
