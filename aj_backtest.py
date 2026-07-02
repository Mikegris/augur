"""aj_backtest.py — leak-free WALK-FORWARD backtester for the AGENT'S policy.

`research_backtest.py` measures whether an individual *signal* has historical
skill. This module answers the next question: does the AGENT'S COMPLETE
PRICE-DRIVEN POLICY — its judge thresholds, conviction sizing, and exit rules,
charged real transaction costs — actually produce out-of-sample expectancy?

It replays the policy bar by bar over price history. At every bar `t` the
decision sees ONLY bars strictly before `t` (the leak-prevention invariant,
reused from research_backtest's "decision at close[t-1], fill at close" rule),
derives a leak-free price edge (momentum / mean-reversion / trend blended into a
prob_up via a normal-CDF map — mirroring how `forecast_ensemble`/ml_forecast
turn a z-score into a probability), applies the agent's ENTRY gate
(prob_up >= buy_prob_threshold AND |edge| >= min_edge_pct_pts), sizes by
conviction (fixed-fractional like aj_strategy), then manages the OPEN position
with the agent's EXIT rules (take-profit / stop-loss / trailing-stop /
max-holding-days) — charging a round-trip cost of (fee_bps + paper_slippage_bps)
on entry and exit. Each closed trade's NET return is tracked and aggregated.

Scope (honest): this covers the PRICE-DRIVEN core of the policy. The orthogonal
alt-data signals (smart_money / insider / congress / social) have no
point-in-time historical snapshots, so they cannot be replayed leak-free and are
intentionally excluded — every result carries `coverage: "price_signals_only"`.

Guardrails:
  * leak-free — the decision at bar t is computed from df.iloc[:t] only, and an
    assertion guards the entry/fill index relationship.
  * cost-aware — round-trip fee + slippage charged on every closed trade.
  * fail-open — never raises into a caller, never trades; on any error returns
    an `{"error": ...}` envelope.
  * no new deps — numpy / pandas only; reads only.

Public API:
  * backtest_policy(symbols, cfg=None, period="2y", horizon=20) -> dict
  * policy_expectancy(cfg=None, symbols=None) -> dict

Python 3.9 compatible.
"""
from __future__ import annotations

import hashlib
import logging
import math
import random
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

log = logging.getLogger("augur.aj_backtest")

# Built-in liquid basket used ONLY as the last-resort fallback when no symbols,
# allowlist, or screener-cache universe are available. CAVEAT: these are
# TODAY'S mega-cap winners, so any default-basket backtest carries survivorship
# bias — results are stamped with `selection_bias` (see policy_expectancy).
_DEFAULT_BASKET = ["AAPL", "MSFT", "NVDA", "SPY", "AMZN",
                   "GOOGL", "META", "JPM", "XOM", "UNH"]
_DEFAULT_BASKET_CAVEAT = (
    "universe fell back to the built-in basket of today's mega-cap winners; "
    "results overstate expectancy (survivorship bias)")

_MAX_SYMBOLS = 15            # hard cap on universe size (cost guard)
_MIN_BARS = 80               # need enough history to compute the leak-free edge
_LOOKBACK = 60               # bars of context required before the first decision
_VOL_FLOOR = 1e-6

# Bootstrap CI over per-trade returns: a positive MEAN alone is fragile — with
# few/noisy trades it flips sign under resampling, so the verdict would bless
# luck. We report a nonparametric 95% CI of the mean and require its LOWER
# bound to clear zero before calling the policy "positive expectancy".
_BOOT_RESAMPLES = 1000       # bootstrap resamples per CI
_BOOT_MIN_TRADES = 10        # below this the resampled means are pure noise —
                             # skip the bootstrap (ci95=None, matches the
                             # existing insufficient-data cutoff)


# ───────────────────────────── data plumbing ────────────────────────────────

def _load_closes(symbol: str, period: str) -> Optional[pd.DataFrame]:
    """Pull daily bars via the fetcher → sorted DataFrame (Close, High, Low).

    Returns None when the fetcher can't produce a usable series. Never raises.
    """
    try:
        import fetcher
    except Exception as e:  # pragma: no cover - import guard
        log.warning("fetcher import failed: %s", e)
        return None
    try:
        bars = fetcher.get_chart_data(symbol, period=period, interval="1d")
    except Exception as e:
        log.warning("get_chart_data(%s) failed: %s", symbol, e)
        return None
    if not bars:
        return None
    try:
        df = pd.DataFrame(bars)
        if "close" not in df.columns:
            return None
        if "time" in df.columns:
            df["Date"] = pd.to_datetime(df["time"], unit="s", errors="coerce")
            df = df.set_index("Date")
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                "close": "Close", "volume": "Volume"})
        # Open is carried so gap-throughs can fill at the OPEN (an earnings gap
        # 100→80 through a 5% stop really fills near 80, not at the stop level).
        for col in ("High", "Low", "Open"):
            if col not in df.columns:
                df[col] = df["Close"]
        df = df[df["Close"] > 0]
        df = df[~df.index.duplicated(keep="last")]
        df = df.sort_index()
        return df[["Open", "High", "Low", "Close"]]
    except Exception as e:
        log.warning("normalise(%s) failed: %s", symbol, e)
        return None


# ───────────────────────── leak-free price edge ─────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf (stdlib) — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _edge_at(hist: pd.DataFrame) -> Optional[Dict[str, float]]:
    """Leak-free price edge from bars strictly BEFORE the decision point.

    `hist` is df.iloc[:t] — it must contain NO bar at or after the decision
    bar. Blends three leak-free components, each computed only from `hist`:
      * momentum:        20-day return z-scored against its trailing distribution
                         (mirrors research_backtest.adapter_momentum)
      * mean-reversion:  z-score of close vs its 50d MA (sign flipped — a
                         stretched-low price implies a forward bounce)
      * trend:           sign/magnitude of the 50d SMA slope
    The blended z-score maps to prob_up via the normal CDF (the same z→prob idea
    forecast_ensemble uses); `edge_pct_pts` is the expected forward move in
    percentage points (prob deviation from 0.5 scaled to a ~20d move budget).

    Returns None when there isn't enough history.
    """
    close = hist.get("Close")
    if close is None or len(close) < _LOOKBACK:
        return None
    c = close.to_numpy(dtype=float)
    if not np.all(np.isfinite(c)) or np.any(c <= 0):
        c = c[np.isfinite(c) & (c > 0)]
        if len(c) < _LOOKBACK:
            return None

    # Daily returns of the trailing window drive the vol scaling used to turn
    # absolute moves into z-scores (so a trend in a quiet vs noisy name is
    # judged on the same footing).
    ma_win = 50
    daily_all = np.diff(c) / c[:-1]
    daily_all = daily_all[np.isfinite(daily_all)]
    dvol = float(np.std(daily_all[-ma_win:])) if len(daily_all) >= 10 else 0.01
    dvol = max(dvol, _VOL_FLOOR)

    # ── momentum: the recent 20d RETURN, vol-normalized to a z-score. A
    # sustained advance keeps a POSITIVE momentum z (unlike ranking within a
    # tight rolling-return distribution, which decays to ~0 mid-trend). Scaled
    # by sqrt(20) so it's a per-20d move expressed in daily-vol units. ─────────
    window = 20
    if len(c) <= window:
        return None
    mom20 = float(c[-1] / c[-1 - window] - 1.0)
    z_mom = mom20 / (dvol * math.sqrt(window))

    # ── trend: 50d SMA slope over 10 bars, vol-normalized. Positive in an
    # advance, the core momentum-following driver. ─────────────────────────────
    if len(c) >= ma_win + 10:
        sma_now = float(np.mean(c[-ma_win:]))
        sma_prev = float(np.mean(c[-ma_win - 10:-10]))
        z_trend = (sma_now / sma_prev - 1.0) / (dvol * math.sqrt(10))
    else:
        z_trend = 0.0

    # ── mean reversion: z of close vs 50d MA (sign flipped). Kept as a SMALL
    # damper only — in a persistent trend price sits far from its MA, so this
    # term must NOT be allowed to overpower a real trend (hence the low weight
    # and the clip below). ─────────────────────────────────────────────────────
    win = c[-ma_win:]
    ma, msd = float(np.mean(win)), float(np.std(win))
    z_mr_raw = -((float(c[-1]) - ma) / msd) if msd > _VOL_FLOOR else 0.0
    z_mr = float(np.clip(z_mr_raw, -1.0, 1.0))

    # Blend: momentum-led, trend-confirmed, mean-reversion a mild damper.
    z = 0.50 * z_mom + 0.40 * z_trend + 0.10 * z_mr
    z = float(np.clip(z, -4.0, 4.0))
    prob_up = _norm_cdf(z)

    # per-horizon move budget (~20d) from the trailing daily vol, so the edge is
    # expressed in honest percentage points, capped to a sane band.
    horizon_move_budget = min(0.40, max(0.02, dvol * math.sqrt(20.0) * 2.5))
    edge_pct_pts = (prob_up - 0.5) * 2.0 * horizon_move_budget * 100.0

    return {
        "prob_up": float(prob_up),
        "edge_pct_pts": float(edge_pct_pts),
        "z": z,
    }


# ───────────────────────────── metrics roll-up ──────────────────────────────

def _bootstrap_ci95(rs: List[float]) -> Optional[List[float]]:
    """Nonparametric bootstrap 95% CI of the MEAN per-trade net return,
    reported in PERCENT (same units as expectancy_pct): resample the trade
    list with replacement _BOOT_RESAMPLES times, take each sample's mean,
    return the [2.5th, 97.5th] percentiles.

    DETERMINISM: the RNG is a private random.Random seeded from a HASH OF THE
    TRADE LIST itself — no wall clock, no global random state — so the same
    trades always yield the same interval and the verdict cannot flap between
    otherwise-identical runs. md5 (not built-in hash()) because str hashing is
    salted per process and this must be stable across processes/restarts.

    Returns None below _BOOT_MIN_TRADES — too few trades for resampling to
    say anything; callers keep the existing insufficient-data behavior.
    """
    n = len(rs)
    if n < _BOOT_MIN_TRADES:
        return None
    key = ",".join("{:.10e}".format(r) for r in rs).encode("utf-8")
    rng = random.Random(int(hashlib.md5(key).hexdigest()[:16], 16))
    means = []
    for _ in range(_BOOT_RESAMPLES):
        means.append(sum(rng.choices(rs, k=n)) / n)   # resample w/ replacement
    means.sort()
    lo = means[int(round(0.025 * (_BOOT_RESAMPLES - 1)))]
    hi = means[int(round(0.975 * (_BOOT_RESAMPLES - 1)))]
    return [round(lo * 100.0, 4), round(hi * 100.0, 4)]


def _empty_metrics() -> Dict[str, Any]:
    return {
        "n_trades": 0, "hit_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "profit_factor": 0.0, "expectancy_pct": 0.0, "sharpe": 0.0,
        "max_drawdown": 0.0, "turnover": 0.0, "total_return": 0.0,
        "ci95": None,
    }


def _metrics_from_trades(returns: List[float],
                         hold_days: Optional[List[float]] = None) -> Dict[str, Any]:
    """Aggregate a list of per-trade NET fractional returns into metrics.

    `returns` are net of round-trip costs. total_return is the COMPOUNDED equity
    growth of sequentially risking the same fraction on each trade — callers
    must pass the returns in CHRONOLOGICAL exit order for it (and max_drawdown)
    to describe a real equity path. `hold_days` (parallel to `returns`, holding
    period of each trade in bars) drives the Sharpe annualization below.
    """
    rs = [float(r) for r in returns if isinstance(r, (int, float)) and math.isfinite(r)]
    n = len(rs)
    if n == 0:
        return _empty_metrics()
    arr = np.asarray(rs, dtype=float)
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    hit_rate = float(len(wins) / n)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    if gross_loss > _VOL_FLOOR:
        profit_factor = gross_win / gross_loss
    else:
        profit_factor = float("inf") if gross_win > 0 else 0.0
    expectancy = float(arr.mean())
    sd = float(arr.std())
    # Sharpe: mean/sd is the PER-TRADE ratio; annualize by TRADE FREQUENCY —
    # sqrt(252 / avg holding days) trades' worth of independent bets per year.
    # (The old * sqrt(n_trades) scaling was a t-statistic, not a Sharpe: it
    # grows without bound as the sample gets longer, not as the edge improves.)
    hs = [float(h) for h in (hold_days or [])
          if isinstance(h, (int, float)) and math.isfinite(h) and h > 0]
    avg_hold = (sum(hs) / len(hs)) if hs else 0.0
    if sd > _VOL_FLOOR:
        if avg_hold >= 1.0:
            sharpe = float(expectancy / sd * math.sqrt(252.0 / avg_hold))
        else:
            # no holding-period info → report the raw per-trade ratio,
            # NOT a sample-size-inflated t-stat.
            sharpe = float(expectancy / sd)
    else:
        sharpe = 0.0

    # Equity curve: compound each trade's net return; drawdown off that curve.
    nav = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in rs:
        nav *= (1.0 + r)
        if nav > peak:
            peak = nav
        dd = (nav - peak) / peak if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd
    total_return = nav - 1.0

    return {
        "n_trades": int(n),
        "hit_rate": round(hit_rate, 4),
        "avg_win": round(avg_win * 100.0, 4),
        "avg_loss": round(avg_loss * 100.0, 4),
        "profit_factor": (round(profit_factor, 4) if math.isfinite(profit_factor)
                          else float("inf")),
        "expectancy_pct": round(expectancy * 100.0, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(max_dd * 100.0, 4),
        "turnover": float(n),   # each closed trade = one round-trip
        "total_return": round(total_return * 100.0, 4),
        # 95% bootstrap CI of the mean return (%, [lo, hi]); None when n < 10.
        "ci95": _bootstrap_ci95(rs),
    }


# ───────────────────────────── per-symbol walk ──────────────────────────────

def _walk_symbol(
    symbol: str,
    df: pd.DataFrame,
    cfg: Dict[str, Any],
    horizon: int,
    regime: str,
) -> Dict[str, Any]:
    """Replay the agent's price-driven policy over one symbol.

    Returns {"returns": [...net trade returns...], "trades": [...], "regime": ...}.
    Costs: round-trip = 2 * (fee_bps + paper_slippage_bps) charged on each trade.
    """
    buy_thresh = float(cfg.get("buy_prob_threshold", 0.55))
    min_edge = float(cfg.get("min_edge_pct_pts", 3.0))
    tp = float(cfg.get("take_profit_pct", 0.0)) / 100.0
    sl = float(cfg.get("stop_loss_pct", 0.0)) / 100.0
    trail = float(cfg.get("trailing_stop_pct", 0.0)) / 100.0
    max_hold = int(cfg.get("max_holding_days", 0))
    fee_bps = float(cfg.get("fee_bps", 0.0))
    slip_bps = float(cfg.get("paper_slippage_bps", 0.0))
    conviction = bool(cfg.get("conviction_sizing", False))

    # One-way cost fraction; round-trip = entry + exit.
    one_way = max(0.0, (fee_bps + slip_bps)) / 10000.0
    if max_hold <= 0:
        max_hold = horizon  # default holding cap = the forecast horizon

    closes = df["Close"].to_numpy(dtype=float)
    highs = df["High"].to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    # Open is optional (older callers hand in Close/High/Low frames); fall back
    # to Close so the gap-fill clamp below degrades to the plain hi-clamp.
    opens = (df["Open"].to_numpy(dtype=float) if "Open" in df.columns
             else closes)
    dates = list(df.index)
    n = len(closes)

    returns: List[float] = []
    trades: List[Dict[str, Any]] = []
    in_pos = False
    entry_idx = -1
    entry_px = 0.0
    peak_px = 0.0
    weight = 1.0

    def _book_exit(px: float, why: str, exit_i: int) -> None:
        """Close the open trade at `px` on bar `exit_i` and record it."""
        gross = (px / entry_px) - 1.0
        net = (1.0 - one_way) * (px / entry_px) * (1.0 - one_way) - 1.0
        net *= weight  # fixed-fractional position weight
        returns.append(float(net))
        trades.append({
            "symbol": symbol,
            "entry_date": _fmt(dates, entry_idx),
            "exit_date": _fmt(dates, exit_i),
            "entry_px": round(entry_px, 4),
            "exit_px": round(float(px), 4),
            "gross_pct": round(gross * 100.0, 4),
            "net_pct": round(net * 100.0, 4),
            "held_days": int(exit_i - entry_idx),
            "reason": why,
            "weight": round(weight, 3),
            "regime": regime,
        })

    t = _LOOKBACK
    while t < n:
        if not in_pos:
            # DECISION at bar t uses ONLY df.iloc[:t] (strictly past bars).
            hist = df.iloc[:t]
            # Leak-free invariant: the slice's last index must be t-1, never t.
            assert len(hist) == t, "look-ahead: decision slice length != t"
            edge = _edge_at(hist)
            t_step = 1
            if edge is not None:
                prob_up = edge["prob_up"]
                edge_pp = edge["edge_pct_pts"]
                if prob_up >= buy_thresh and abs(edge_pp) >= min_edge:
                    # FILL at close[t-1] (the most recent OBSERVABLE bar at t).
                    entry_idx = t - 1
                    entry_px = float(closes[entry_idx])
                    if entry_px > 0 and math.isfinite(entry_px):
                        in_pos = True
                        peak_px = entry_px
                        # conviction sizing: fixed-fractional scaled by edge
                        # strength (mirrors aj_strategy's edge→size ramp,
                        # full size by ~10 edge pct-pts), else flat 1.0.
                        if conviction:
                            weight = float(np.clip(abs(edge_pp) / 10.0, 0.25, 1.0))
                        else:
                            weight = 1.0
            t += t_step
            continue

        # ── MANAGE the open position. Bar t is now OBSERVABLE (held overnight),
        # so using its OHLC to detect an intrabar exit is causal: we entered at
        # close[entry_idx] and are checking each subsequent bar in sequence. ────
        hi = float(highs[t]) if math.isfinite(highs[t]) else float(closes[t])
        lo = float(lows[t]) if math.isfinite(lows[t]) else float(closes[t])
        cl = float(closes[t])
        op = float(opens[t]) if math.isfinite(opens[t]) and opens[t] > 0 else cl

        held_days = t - entry_idx
        exit_px = None
        reason = None

        # Stop-loss / trailing-stop checked against the bar low (worst case).
        # GAP CLAMP: a stop is a resting order — if the bar OPENED below the
        # stop level (earnings gap 100→80 through a 5% stop), the real fill is
        # ~the open, not the stop price. Fill at min(stop, open), and never
        # above the bar's high, so a gap-through books its true loss.
        if sl > 0:
            sl_px = entry_px * (1.0 - sl)
            if lo <= sl_px:
                exit_px, reason = min(sl_px, op, hi), "stop_loss"
        if exit_px is None and trail > 0:
            # peak_px here is the peak over bars < t ONLY — ratcheting it with
            # THIS bar's high before testing this bar's low would let the stop
            # trail an intrabar move it couldn't have seen (look-ahead).
            tr_px = peak_px * (1.0 - trail)
            if lo <= tr_px:
                # fill at the trail level, gap-clamped like the stop-loss
                exit_px, reason = min(tr_px, op, hi), "trailing_stop"
        # Take-profit checked against the bar high (best case).
        if exit_px is None and tp > 0:
            tp_px = entry_px * (1.0 + tp)
            if hi >= tp_px:
                exit_px, reason = tp_px, "take_profit"
        # Max-holding-days → exit at this bar's close.
        if exit_px is None and held_days >= max_hold:
            exit_px, reason = cl, "max_holding_days"

        # Only AFTER the exit tests may this bar's high ratchet the trail peak
        # (it becomes observable history for bar t+1 onward).
        if hi > peak_px:
            peak_px = hi

        if exit_px is not None and exit_px > 0:
            _book_exit(float(exit_px), reason, t)
            in_pos = False
            entry_idx = -1
        t += 1

    # A position still open when the series ends must be COUNTED, not silently
    # dropped — discarding it biases the stats toward trades that managed to
    # exit. Force-close at the final bar's close.
    if in_pos and n > 0:
        last_cl = float(closes[n - 1])
        if math.isfinite(last_cl) and last_cl > 0:
            _book_exit(last_cl, "end_of_data", n - 1)
            in_pos = False
            entry_idx = -1

    return {"returns": returns, "trades": trades, "regime": regime}


def _fmt(dates: List[Any], idx: int) -> Optional[str]:
    try:
        d = dates[idx]
        return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
    except Exception:
        return None


# ───────────────────────────── public API ───────────────────────────────────

def backtest_policy(
    symbols: List[str],
    cfg: Optional[Dict[str, Any]] = None,
    period: str = "2y",
    horizon: int = 20,
) -> Dict[str, Any]:
    """Walk-forward backtest of the agent's PRICE-DRIVEN policy over `symbols`.

    For each symbol, replays the policy bar by bar (decision at close[t-1], no
    look-ahead), applies the entry gate + conviction sizing + cost-charged exit
    rules from `cfg`, and tracks every closed trade's NET return. Aggregates
    per symbol AND overall, plus a per-regime breakdown of the current regime.

    Returns a dict with `per_symbol`, `aggregate`, `per_regime`, the resolved
    `params`, and `coverage: "price_signals_only"`. Fail-open: returns
    `{"error": ...}` rather than raising.
    """
    try:
        if cfg is None:
            try:
                import aj_config
                cfg = aj_config.get_config()
            except Exception:
                cfg = {}
        cfg = dict(cfg or {})

        syms = []
        for s in (symbols or []):
            u = str(s or "").upper().strip()
            if u and u not in syms:
                syms.append(u)
        syms = syms[:_MAX_SYMBOLS]
        if not syms:
            return {"error": "no symbols to backtest"}

        try:
            horizon = max(1, int(horizon))
        except Exception:
            horizon = 20

        # One regime label for this run (current SPY-derived regime). Per-trade
        # regime is stamped with this label; we cannot reconstruct point-in-time
        # historical regimes leak-free without per-bar SPY history, so the
        # per-regime breakdown is honestly scoped to the run's regime.
        regime = "unknown"
        try:
            import aj_alpha
            regime = aj_alpha.detect_regime()
        except Exception:
            regime = "unknown"

        per_symbol: Dict[str, Any] = {}
        all_trades: List[Dict[str, Any]] = []
        # (exit_date, net_return, held_days) triples — kept together so the
        # aggregate can be re-sorted CHRONOLOGICALLY across symbols below.
        all_rows: List[Any] = []
        per_regime_rows: Dict[str, List[Any]] = {}

        for sym in syms:
            df = _load_closes(sym, period)
            if df is None or len(df) < _MIN_BARS:
                per_symbol[sym] = {
                    "error": "insufficient history",
                    **_empty_metrics(),
                }
                continue
            res = _walk_symbol(sym, df, cfg, horizon, regime)
            rets = res["returns"]
            holds = [tr.get("held_days") for tr in res["trades"]]
            per_symbol[sym] = _metrics_from_trades(rets, hold_days=holds)
            per_symbol[sym]["regime"] = res["regime"]
            all_trades.extend(res["trades"])
            rows = [(tr.get("exit_date") or "", r, tr.get("held_days"))
                    for r, tr in zip(rets, res["trades"])]
            all_rows.extend(rows)
            per_regime_rows.setdefault(res["regime"], []).extend(rows)

        # Sort ALL trades by exit date before compounding the aggregate: the
        # naive per-symbol concatenation replayed symbol A's 2 years, then
        # symbol B's 2 years, ... as ONE sequence, making the aggregate
        # total_return / max_drawdown a fictional equity path. Chronological
        # order gives a real (sequential-capital) path; expectancy / hit-rate /
        # profit-factor are order-independent and unchanged either way.
        all_rows.sort(key=lambda x: x[0])
        aggregate = _metrics_from_trades([x[1] for x in all_rows],
                                         hold_days=[x[2] for x in all_rows])
        per_regime = {}
        for reg, rows in per_regime_rows.items():
            rows.sort(key=lambda x: x[0])
            per_regime[reg] = _metrics_from_trades(
                [x[1] for x in rows], hold_days=[x[2] for x in rows])

        return {
            "coverage": "price_signals_only",
            "n_symbols": len(syms),
            "symbols": syms,
            "period": period,
            "horizon": horizon,
            "regime": regime,
            "params": {
                "buy_prob_threshold": float(cfg.get("buy_prob_threshold", 0.55)),
                "min_edge_pct_pts": float(cfg.get("min_edge_pct_pts", 3.0)),
                "take_profit_pct": float(cfg.get("take_profit_pct", 0.0)),
                "stop_loss_pct": float(cfg.get("stop_loss_pct", 0.0)),
                "trailing_stop_pct": float(cfg.get("trailing_stop_pct", 0.0)),
                "max_holding_days": int(cfg.get("max_holding_days", 0)),
                "fee_bps": float(cfg.get("fee_bps", 0.0)),
                "paper_slippage_bps": float(cfg.get("paper_slippage_bps", 0.0)),
                "conviction_sizing": bool(cfg.get("conviction_sizing", False)),
            },
            "per_symbol": per_symbol,
            "per_regime": per_regime,
            "aggregate": aggregate,
        }
    except Exception as e:  # fail-open
        log.warning("backtest_policy failed: %s", e, exc_info=True)
        return {"error": "backtest_policy failed: {}".format(e)}


def policy_expectancy(
    cfg: Optional[Dict[str, Any]] = None,
    symbols: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Headline 'does the policy have an edge?' report.

    Picks a universe (explicit `symbols`, else cfg `symbol_allowlist` when
    non-empty, else a built-in liquid basket, capped at 15), runs
    `backtest_policy`, and returns the aggregate metrics plus a `verdict`
    ("positive expectancy" / "negative expectancy" / "insufficient data"),
    `expectancy_pct`, `ci95` (bootstrap 95% CI of the mean return, %; the
    positive verdict requires ci95[0] > 0), `n_trades`, and a one-line
    `summary`. Fail-open.
    """
    try:
        if cfg is None:
            try:
                import aj_config
                cfg = aj_config.get_config()
            except Exception:
                cfg = {}
        cfg = dict(cfg or {})

        selection_bias = None
        if symbols:
            universe = [str(s).upper().strip() for s in symbols if str(s).strip()]
        else:
            allow = cfg.get("symbol_allowlist") or []
            if isinstance(allow, list) and allow:
                universe = [str(s).upper().strip() for s in allow if str(s).strip()]
            else:
                # Prefer the screener's recently-seen candidates (a pure DB
                # read of aj_screen_cache — NO network) over the hard-coded
                # basket, so the tested universe matches what the agent
                # actually trades. Ranked by dollar volume, like the screener.
                universe = []
                try:
                    import aj_universe
                    ttl = int(cfg.get("screen_cache_ttl_min", 45) or 45)
                    pool = aj_universe._cache_fresh(ttl) or []
                    pool.sort(key=lambda r: -(float(r.get("price") or 0.0)
                                              * float(r.get("volume") or 0.0)))
                    universe = [str(r.get("symbol") or "").upper().strip()
                                for r in pool if r.get("symbol")]
                except Exception:
                    log.debug("screen-cache universe unavailable", exc_info=True)
                    universe = []
                if not universe:
                    universe = list(_DEFAULT_BASKET)
                    selection_bias = _DEFAULT_BASKET_CAVEAT
        universe = list(dict.fromkeys(universe))[:_MAX_SYMBOLS]

        bt = backtest_policy(universe, cfg=cfg)
        if "error" in bt:
            return {
                "verdict": "insufficient data",
                "expectancy_pct": 0.0,
                "n_trades": 0,
                "summary": "Backtest could not run: {}".format(bt["error"]),
                "error": bt["error"],
            }

        agg = bt.get("aggregate", _empty_metrics())
        n_trades = int(agg.get("n_trades", 0))
        exp = float(agg.get("expectancy_pct", 0.0))
        ci95 = agg.get("ci95")   # [lo, hi] in %, or None below 10 trades

        if n_trades < 10:
            verdict = "insufficient data"
        elif exp > 0 and ci95 is not None and float(ci95[0]) > 0:
            # "positive expectancy" now requires the WHOLE bootstrap CI to
            # clear zero, not just the point estimate — a mean the resamples
            # frequently flip negative is noise, not edge (fail-closed toward
            # "no edge" so nothing sizes up on luck).
            verdict = "positive expectancy"
        else:
            verdict = "negative expectancy"

        summary = (
            "{verdict}: {exp:+.2f}% net expectancy/trade (95% CI {ci}) over "
            "{n} trades ({sym} symbols, hit-rate {hr:.0%}, profit-factor {pf}, "
            "regime={reg}). Scope: price signals only."
        ).format(
            ci=("[{:+.2f}%, {:+.2f}%]".format(float(ci95[0]), float(ci95[1]))
                if ci95 is not None else "n/a"),
            verdict=verdict,
            exp=exp,
            n=n_trades,
            sym=bt.get("n_symbols", 0),
            hr=float(agg.get("hit_rate", 0.0)),
            pf=("inf" if agg.get("profit_factor") == float("inf")
                else "{:.2f}".format(float(agg.get("profit_factor", 0.0)))),
            reg=bt.get("regime", "unknown"),
        )

        return {
            "verdict": verdict,
            "expectancy_pct": round(exp, 4),
            # bootstrap 95% CI of the mean per-trade return (%) — the verdict
            # gate above; None below 10 trades (insufficient data).
            "ci95": ci95,
            "n_trades": n_trades,
            "summary": summary,
            # non-None only when the survivorship-biased default basket ran
            "selection_bias": selection_bias,
            "coverage": bt.get("coverage", "price_signals_only"),
            "aggregate": agg,
            "per_symbol": bt.get("per_symbol", {}),
            "per_regime": bt.get("per_regime", {}),
            "params": bt.get("params", {}),
            "symbols": bt.get("symbols", universe),
        }
    except Exception as e:  # fail-open
        log.warning("policy_expectancy failed: %s", e, exc_info=True)
        return {
            "verdict": "insufficient data",
            "expectancy_pct": 0.0,
            "n_trades": 0,
            "summary": "policy_expectancy failed",
            "error": "policy_expectancy failed: {}".format(e),
        }


__all__ = ["backtest_policy", "policy_expectancy"]
