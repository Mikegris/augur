"""AJTA — the "100x" alpha & intelligence layer (enhancements 1-20).

First-principles, compounding-growth machinery layered on the fail-closed core.
EVERYTHING here is opt-in: with every 100x config key at its 0/false default,
every function returns a neutral value (multiplier 1.0, no block, no extra exit)
and the agent behaves exactly as before. Each helper is fail-OPEN — an enhancement
must never crash a cycle, so on any error it returns the neutral result.

Layout:
  • sizing intelligence (1-5)  → sizing_multiplier()
  • entry alpha       (6-11,18) → entry_block_reason()
  • exit intelligence (12-15)   → extra_exit_signals()
  • adaptive brain    (16-17)   → effective_config()
  • scorecard         (19)      → scorecard_record() / scorecard()
  • opportunity radar (20)      → rank_universe()

Data access is funnelled through the small `_*` wrappers at the top so tests can
monkeypatch them and exercise the pure logic with no network.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

import aj_db

log = logging.getLogger("augur.aj_alpha")

_SCORECARD_KEY = "__aj_scorecard"   # control-plane settings key (hidden)
_ENTRY_CONVICTION_KEY = "__aj_entry_conviction"
_TP_LADDER_KEY = "__aj_tp_ladder_fired"   # {symbol: [fired rung numbers]}


# ── data wrappers (monkeypatchable in tests) ──────────────────────────────────

def _closes(symbol: str, period: str = "1y") -> List[float]:
    """Daily closes for a symbol, oldest→newest. [] when unavailable."""
    try:
        import portfolio_insights as pi
        return list(pi._closes(symbol, period) or [])
    except Exception:
        log.debug("closes failed for %s", symbol, exc_info=True)
        return []


def _bars(symbol: str, period: str = "6mo") -> List[Dict[str, Any]]:
    """OHLC bars (need high/low for ATR). [] when unavailable."""
    try:
        import fetcher
        return list(fetcher.get_chart_data(symbol, period, "1d") or [])
    except Exception:
        log.debug("bars failed for %s", symbol, exc_info=True)
        return []


def _market_closes(period: str = "1y") -> List[float]:
    """Benchmark (SPY) closes for relative-strength / regime."""
    return _closes("SPY", period)


def _paper_book() -> Dict[str, Any]:
    try:
        import aj_positions
        return aj_positions.paper_book()
    except Exception:
        return {"positions": {}}


def _realized_trades() -> List[Dict[str, Any]]:
    try:
        import aj_positions
        return list(aj_positions.realized_trades("paper") or [])
    except Exception:
        return []


def _equity_curve() -> List[Dict[str, Any]]:
    try:
        import aj_analytics
        return list(aj_analytics.equity_curve(180) or [])
    except Exception:
        return []


def _current_equity() -> float:
    """Realized + open unrealized, the agent's paper equity above zero."""
    try:
        import aj_metrics
        return float((aj_metrics.cumulative_pnl() or {}).get("total") or 0.0)
    except Exception:
        return 0.0


def _paper_cash() -> float:
    """Configured paper cash balance (aj_paper_cash), or 0.0. This is the cash
    leg of account equity used to base weight/sector caps on EQUITY rather than
    invested notional. 0.0 (the default) makes equity == positions market value,
    preserving the prior notional-based behaviour for an unfunded book."""
    try:
        import database as _db
        raw = _db.get_settings().get("aj_paper_cash")
        return float(aj_db.money(raw)) if raw is not None else 0.0
    except Exception:
        return 0.0


def account_equity(positions: Optional[Dict[str, Any]] = None,
                   marks: Optional[Dict[str, Optional[float]]] = None) -> float:
    """Account equity = cash (aj_paper_cash) + current market value of all open
    paper positions. The correct base for per-symbol / sector weight caps (a
    first position is then sized against equity, not always ~100% of an empty
    'book'). Falls back to the paper book / live marks when not passed in.
    Fail-OPEN: any error yields just the positions market value (>= 0)."""
    try:
        if positions is None:
            positions = (_paper_book().get("positions") or {})
        syms = list(positions.keys())
        if marks is None:
            try:
                import aj_risk
                marks = aj_risk._marks(syms) if syms else {}
            except Exception:
                marks = {}
        mv = 0.0
        for s, p in positions.items():
            mk = marks.get(s)
            px = mk if mk is not None else float(p.get("avg_cost") or 0)
            mv += float(p.get("qty") or 0) * float(px or 0)
        return max(0.0, _paper_cash() + mv)
    except Exception:
        log.debug("account_equity failed -> 0", exc_info=True)
        return 0.0


def _earnings_days_away(symbol: str) -> Optional[int]:
    """Calendar days until the next earnings date, or None if unknown."""
    try:
        import earnings
        cal = earnings.get_earnings_calendar([symbol]) or []
        for row in cal:
            d = row.get("days_until") if isinstance(row, dict) else None
            if isinstance(d, (int, float)):
                return int(d)
            date_str = (row.get("date") or row.get("earnings_date")) if isinstance(row, dict) else None
            if date_str:
                dt = aj_db.parse_iso(str(date_str)) or _parse_date(str(date_str))
                if dt:
                    # calendar-day difference (not floor-divided seconds, which
                    # rounds negatives toward -inf and mislabels boundary days)
                    return (dt.date() - aj_db.utc_now().date()).days
    except Exception:
        log.debug("earnings lookup failed for %s", symbol, exc_info=True)
    return None


def _parse_date(s: str):
    from datetime import datetime, timezone
    s = str(s or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _sector_of(symbol: str) -> Optional[str]:
    try:
        import synth_sectorflow
        return synth_sectorflow._ticker_sector(symbol)
    except Exception:
        return None


# ── technical primitives (reuse portfolio_insights where possible) ────────────

def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI: seed with the first `period` deltas, then smooth the
    average gain/loss recursively (avg = (prev*(p-1)+cur)/p). True Wilder
    smoothing rather than a flat trailing-window mean. None if too short."""
    try:
        if not closes or len(closes) <= period or period < 1:
            return None
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        if len(deltas) < period:
            return None
        gains = [d if d > 0 else 0.0 for d in deltas]
        losses = [-d if d < 0 else 0.0 for d in deltas]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return round(100.0 - (100.0 / (1.0 + rs)), 1)
    except Exception:
        return None


def _gk_annualized_vol(bars: List[Dict[str, Any]]) -> Optional[float]:
    """Garman-Klass annualized volatility (%) from OHLC bars — uses the high/low
    range and the open→close move, ~5x more efficient than close-to-close.
    GK daily variance = 0.5*(ln(H/L))^2 - (2ln2-1)*(ln(C/O))^2. None if the bars
    lack usable OHLC (caller falls back to close-to-close)."""
    try:
        terms: List[float] = []
        for b in bars:
            o = float(b.get("open"))
            h = float(b.get("high"))
            lo = float(b.get("low"))
            c = float(b.get("close"))
            if o <= 0 or h <= 0 or lo <= 0 or c <= 0 or h < lo:
                continue
            hl = math.log(h / lo)
            co = math.log(c / o)
            terms.append(0.5 * hl * hl - (2.0 * math.log(2.0) - 1.0) * co * co)
        if len(terms) < 2:
            return None
        daily_var = sum(terms) / len(terms)
        if daily_var <= 0:
            return None
        return round(math.sqrt(daily_var) * math.sqrt(252) * 100.0, 1)
    except Exception:
        return None


def _ann_vol(closes: List[float], bars: Optional[List[Dict[str, Any]]] = None) -> Optional[float]:
    """Annualized volatility (%). Prefers the Garman-Klass OHLC estimator when
    bars are available (more efficient), falling back to close-to-close."""
    try:
        if bars:
            gk = _gk_annualized_vol(bars)
            if gk is not None and gk > 0:
                return gk
        import portfolio_insights as pi
        return pi.annualized_vol(closes)
    except Exception:
        return None


def _sma(closes: List[float], n: int) -> Optional[float]:
    try:
        import portfolio_insights as pi
        return pi.sma(closes, n)
    except Exception:
        return None


def _returns(closes: List[float]) -> List[float]:
    out = []
    for i in range(1, len(closes)):
        p0 = closes[i - 1]
        if p0:
            out.append(closes[i] / p0 - 1.0)
    return out


def _pct_return(closes: List[float], lookback: int) -> Optional[float]:
    if len(closes) < lookback + 1:
        return None
    p0 = closes[-lookback - 1]
    if not p0:
        return None
    return (closes[-1] / p0 - 1.0) * 100.0


def _correlation(a: List[float], b: List[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < 10:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


def _atr(bars: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    """Wilder's Average True Range from OHLC bars: seed with the mean of the
    first `period` true ranges, then smooth recursively (ATR = (prev*(p-1)+TR)/p).
    True Wilder smoothing rather than a flat trailing mean. None if insufficient."""
    if len(bars) < period + 1 or period < 1:
        return None
    trs: List[float] = []
    for i in range(1, len(bars)):
        try:
            hi = float(bars[i].get("high"))
            lo = float(bars[i].get("low"))
            pc = float(bars[i - 1].get("close"))
        except (TypeError, ValueError):
            continue
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score interval LOWER bound for a binomial proportion. Used to
    require statistically-supported evidence before a small-sample veto/adapt:
    with few trades the lower bound stays near 0 even at a high raw win-rate, so
    a 1/3 or 2/5 streak can't trigger a confident action. n<=0 -> 0.0."""
    if n <= 0:
        return 0.0
    phat = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = phat + z2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z2 / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


# ════════════════════════════════════════════════════════════════════════════
#  SIZING INTELLIGENCE (1-5)  → one combined multiplier on the order target
# ════════════════════════════════════════════════════════════════════════════

def _per_symbol_record(symbol: str) -> Tuple[int, int, float]:
    """(wins, losses, net_realized) for a symbol from the paper trade journal."""
    wins = losses = 0
    net = 0.0
    for t in _realized_trades():
        if str(t.get("symbol", "")).upper() != symbol.upper():
            continue
        r = float(t.get("realized") or 0)
        net += r
        if r > 0:
            wins += 1
        elif r < 0:
            losses += 1
    return wins, losses, aj_db.money(net)


def _kelly_factor(cfg: Dict[str, Any], decision: Optional[Dict[str, Any]]) -> float:
    """1: fractional-Kelly. f* = p - (1-p)/b from realized win-rate & payoff;
    falls back to the forecast edge when there's too little history."""
    if not cfg.get("kelly_sizing"):
        return 1.0
    frac = _clamp(float(cfg.get("kelly_fraction") or 0.5), 0.05, 1.0)
    trades = _realized_trades()
    wins = [t for t in trades if float(t.get("realized") or 0) > 0]
    losses = [t for t in trades if float(t.get("realized") or 0) < 0]
    # Realized payoff b = avg-win/avg-loss whenever BOTH legs have history (used
    # by the cold-start branch too, instead of assuming 1:1).
    if wins and losses:
        avg_win = sum(float(t.get("realized") or 0) for t in wins) / len(wins)
        avg_loss = -sum(float(t.get("realized") or 0) for t in losses) / len(losses)
        b_realized = (avg_win / avg_loss) if avg_loss > 0 else 1.0
    else:
        b_realized = None
    if len(trades) >= 5 and wins and losses:
        p = len(wins) / len(trades)
        b = b_realized or 1.0
    else:
        # cold start: imply p from the forecast edge. `edge_pts` is already
        # (prob_up_cal - 0.5) x 100 — an ALREADY-calibrated edge in points — so
        # p = 0.5 + edge_pts/100 (NOT /200, which would re-halve a calibrated
        # edge). Clamp to a sane band. Derive payoff from realized win/loss when
        # any history exists, else assume 1:1.
        edge = abs(float((decision or {}).get("edge_pts") or 0))
        p = _clamp(0.5 + edge / 100.0, 0.5, 0.85)   # 10pp edge → p≈0.60
        b = b_realized if (b_realized and b_realized > 0) else 1.0
    kelly = p - (1.0 - p) / b if b > 0 else 0.0
    bet = _clamp(frac * kelly, 0.0, 1.0)
    # 0.20 bet fraction is the "neutral" reference (=1.0x base sizing). Floor at
    # 0.0 so a non-positive Kelly (zero/negative edge) shrinks size toward zero
    # rather than being held at quarter-size — fractional-Kelly intent.
    return _clamp(bet / 0.20, 0.0, 2.5)


def _vol_target_factor(symbol: str, cfg: Dict[str, Any]) -> float:
    """2: scale inversely to the symbol's daily volatility so each position
    contributes ~equal risk."""
    target = float(cfg.get("volatility_target_pct") or 0)
    if target <= 0:
        return 1.0
    # Prefer the Garman-Klass OHLC estimator when bars are available (more
    # efficient than close-to-close); fall back to the close series otherwise.
    av = None
    try:
        av = _gk_annualized_vol(_bars(symbol, "6mo"))
    except Exception:
        av = None
    if not av or av <= 0:
        av = _ann_vol(_closes(symbol, "1y"))
    if not av or av <= 0:
        return 1.0
    # portfolio_insights.annualized_vol always returns a PERCENT (e.g. 25.0, and
    # 4.0 for a genuinely quiet name). A magnitude-based fraction/percent guess
    # mis-detected real low-vol percents (<5.0) as fractions and inflated them
    # 100x, collapsing the factor to its floor — so treat `av` as percent here.
    av_pct = av
    daily_vol_pct = av_pct / math.sqrt(252)
    if daily_vol_pct <= 0:
        return 1.0
    return _clamp(target / daily_vol_pct, 0.25, 2.5)


def _compound_factor(cfg: Dict[str, Any]) -> float:
    """3: grow order size with the agent's equity (compounding)."""
    if not cfg.get("compound_sizing"):
        return 1.0
    base = float(cfg.get("compound_base_equity_usd") or 0)
    if base <= 0:
        return 1.0
    equity = base + _current_equity()      # equity above the notional base
    return _clamp(equity / base, 0.5, 5.0)


def _symbol_perf_factor(symbol: str, cfg: Dict[str, Any]) -> float:
    """4: lean into proven winners, fade chronic losers (0.0 = skip)."""
    if not cfg.get("symbol_performance_weighting"):
        return 1.0
    wins, losses, net = _per_symbol_record(symbol)
    n = wins + losses
    if n < 2:
        return 1.0
    wr = wins / n
    # Chronic-loser veto: require a Wilson UPPER bound on the win-rate below 0.5
    # (so we're statistically confident the name is a loser) AND n>=4 — not just
    # a fragile 1/3-style raw streak. A small sample keeps the upper bound high
    # and the name is throttled (below) rather than hard-skipped.
    if n >= 4 and net < 0:
        z2 = 1.96 * 1.96
        denom = 1.0 + z2 / n
        centre = wr + z2 / (2 * n)
        margin = 1.96 * math.sqrt((wr * (1 - wr) + z2 / (4 * n)) / n)
        wilson_upper = (centre + margin) / denom
        if wilson_upper < 0.5:
            return 0.0                      # chronic loser — skip the name
    if net > 0 and wr >= 0.5:
        return _clamp(1.0 + wr * 0.6, 1.0, 1.3)
    if net < 0:
        return _clamp(0.4 + wr, 0.5, 1.0)
    return 1.0


def current_drawdown_pct() -> float:
    """Peak-to-current drawdown of the paper equity curve, in %."""
    curve = _equity_curve()
    if not curve:
        return 0.0
    eqs = [float(r.get("equity_usd") or 0) for r in curve]
    peak = max(eqs) if eqs else 0.0
    last = eqs[-1] if eqs else 0.0
    if peak <= 0:
        return 0.0
    dd = (peak - last) / peak * 100.0
    return max(0.0, dd)


def _drawdown_factor(cfg: Dict[str, Any]) -> float:
    """5: throttle size as drawdown deepens; fully off at the threshold."""
    thr = float(cfg.get("drawdown_throttle_pct") or 0)
    if thr <= 0:
        return 1.0
    dd = current_drawdown_pct()
    return _clamp(1.0 - dd / thr, 0.0, 1.0)


def _correlation_budget_factor(symbol: str, cfg: Dict[str, Any]) -> float:
    """Portfolio correlation/vol budget (opt-in): when the candidate is highly
    correlated with the existing book, scale the aggregate target DOWN so two
    correlated names don't each get full size. A BOUNDED multiplier in
    [floor, 1.0] — it can only REDUCE risk, never increase it. Off (1.0) unless
    `correlation_size_budget` (the avg-correlation above which throttling
    begins) is set > 0. Reuses the existing `_correlation` helper."""
    budget = float(cfg.get("correlation_size_budget") or 0)
    if budget <= 0:
        return 1.0
    try:
        positions = (_paper_book().get("positions") or {})
        others = [s for s in positions if str(s).upper() != symbol.upper()]
        if not others:
            return 1.0
        sym_ret = _returns(_closes(symbol, "6mo"))
        corrs = []
        for s in others:
            c = _correlation(sym_ret, _returns(_closes(s, "6mo")))
            if c is not None:
                corrs.append(max(0.0, c))      # only positive corr adds risk
        if not corrs:
            return 1.0
        avg_c = sum(corrs) / len(corrs)
        if avg_c <= budget:
            return 1.0
        # Linearly throttle from 1.0 at the budget down toward a floor at corr=1.
        span = max(1e-6, 1.0 - budget)
        frac = _clamp((avg_c - budget) / span, 0.0, 1.0)
        floor = _clamp(float(cfg.get("correlation_size_floor") or 0.25), 0.05, 1.0)
        return _clamp(1.0 - frac * (1.0 - floor), floor, 1.0)
    except Exception:
        log.debug("correlation budget factor failed -> 1.0", exc_info=True)
        return 1.0


def sizing_multiplier(symbol: str, side: str, cfg: Dict[str, Any],
                      decision: Optional[Dict[str, Any]] = None) -> float:
    """Combined 1-5 multiplier on the base order target. 1.0 when all off.
    Returns 0.0 to veto the order (chronic loser / drawdown halt). Buys only —
    sells are risk-reducing and never up/down-sized here."""
    try:
        if side != "buy":
            return 1.0
        factors = [
            _kelly_factor(cfg, decision),
            _vol_target_factor(symbol, cfg),
            _compound_factor(cfg),
            _symbol_perf_factor(symbol, cfg),
            _drawdown_factor(cfg),
            _correlation_budget_factor(symbol, cfg),
        ]
        if any(f <= 0 for f in factors):
            return 0.0
        # Combine in LOG-space (sum of logs -> exp) rather than raw multiplication
        # so five independent factors don't compound fragilely into an extreme
        # product; mathematically equal to the product but numerically gentler.
        log_sum = sum(math.log(f) for f in factors)
        m = math.exp(log_sum)
        if not math.isfinite(m):
            return 1.0
        return _clamp(m, 0.0, 3.0)
    except Exception:
        log.exception("sizing_multiplier failed -> neutral 1.0")
        return 1.0


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY ALPHA (6-11, 18)  → block reason or None
# ════════════════════════════════════════════════════════════════════════════

def _adds_since_open(symbol: str) -> int:
    """Count of filled BUY orders for a symbol since its position opened."""
    rows = aj_db.query("SELECT opened_at FROM aj_position_state WHERE symbol=?",
                       (symbol.upper(),))
    opened = rows[0].get("opened_at") if rows else None
    if not opened:
        return 0
    cnt = aj_db.query(
        "SELECT COUNT(*) AS n FROM aj_orders WHERE symbol=? AND side='buy' "
        "AND state IN ('filled','partially_filled') "
        "AND COALESCE(submitted_at, created_at) >= ?", (symbol.upper(), opened))
    return int(cnt[0]["n"]) if cnt else 0


def entry_block_reason(symbol: str, side: str, qty: float, price: float,
                       cfg: Dict[str, Any]) -> Optional[str]:
    """Alpha/diversification entry filters (6-11) + pyramiding discipline (18).
    Buys only. Returns a block reason or None. Fail-OPEN: an error never blocks
    (these are edges, not the fail-closed core rail)."""
    try:
        if side != "buy":
            return None
        symbol = symbol.upper()
        book = _paper_book()
        positions = book.get("positions") or {}
        held = float((positions.get(symbol) or {}).get("qty") or 0)
        # Separate per-window caches: gates 6/7/8 use a 1y series, gate 9
        # (correlation vs the book) needs a 6mo series. Sharing one `closes`
        # made the window order-dependent and frequently the wrong length.
        closes_1y: Optional[List[float]] = None
        closes_6mo: Optional[List[float]] = None

        # 6: momentum confirmation — price must be above the N-day SMA
        mdays = int(cfg.get("momentum_filter_days") or 0)
        if mdays > 0:
            closes_1y = closes_1y if closes_1y is not None else _closes(symbol, "1y")
            sma = _sma(closes_1y, mdays)
            if sma is not None and price < sma:
                return "momentum: {:.2f} below {}d SMA {:.2f}".format(price, mdays, sma)

        # 7: mean-reversion guard — don't chase overbought (RSI cap)
        rmax = float(cfg.get("mean_reversion_rsi_max") or 0)
        if rmax > 0:
            closes_1y = closes_1y if closes_1y is not None else _closes(symbol, "1y")
            r = _rsi(closes_1y, 14)
            if r is not None and r > rmax:
                return "overbought: RSI {:.0f} > {:g}".format(r, rmax)

        # 8: relative strength — must outperform SPY over the lookback
        if cfg.get("relative_strength_filter"):
            lb = int(cfg.get("relative_strength_lookback_days") or 20)
            closes_1y = closes_1y if closes_1y is not None else _closes(symbol, "1y")
            sym_ret = _pct_return(closes_1y, lb)
            mkt_ret = _pct_return(_market_closes("1y"), lb)
            if sym_ret is not None and mkt_ret is not None and sym_ret < mkt_ret:
                return "weak RS: {:+.1f}% vs SPY {:+.1f}% ({}d)".format(sym_ret, mkt_ret, lb)

        # 9: correlation gate — block if too correlated with the existing book
        ccap = float(cfg.get("max_book_correlation") or 0)
        if ccap > 0 and positions:
            closes_6mo = closes_6mo if closes_6mo is not None else _closes(symbol, "6mo")
            sym_ret_series = _returns(closes_6mo)
            corrs = []
            for s in positions:
                if s == symbol:
                    continue
                c = _correlation(sym_ret_series, _returns(_closes(s, "6mo")))
                if c is not None:
                    corrs.append(c)
            if corrs:
                avg_c = sum(corrs) / len(corrs)
                if avg_c > ccap:
                    return "corr {:.2f} > {:g} with book".format(avg_c, ccap)

        # 10: earnings blackout — avoid binary event risk
        eb = int(cfg.get("earnings_blackout_days") or 0)
        if eb > 0:
            d = _earnings_days_away(symbol)
            if d is not None and 0 <= d <= eb:
                return "earnings in {}d (blackout {}d)".format(d, eb)

        # 11: sector concentration cap — projected sector weight after the buy
        scap = float(cfg.get("max_sector_weight_pct") or 0)
        if scap > 0:
            sec = _sector_of(symbol)
            if sec:
                import aj_risk
                syms = list(positions.keys())
                marks = aj_risk._marks(syms) if syms else {}
                # resolve each held symbol's sector once (avoids O(positions)
                # repeated lookups and keeps None-handling consistent)
                sectors = {s: _sector_of(s) for s in positions}
                sec_val = 0.0
                for s, p in positions.items():
                    mk = marks.get(s)
                    px = mk if mk is not None else float(p.get("avg_cost") or 0)
                    mv = float(p.get("qty") or 0) * px
                    if sectors.get(s) == sec:
                        sec_val += mv
                add = qty * price
                sec_val += add
                # Weight against ACCOUNT EQUITY (cash + positions MV incl. the
                # add), not just invested notional — so the cap is blind neither
                # to cash nor to leverage. With no configured cash (default),
                # equity == positions MV and behaviour is unchanged.
                equity = account_equity(positions, marks) + add
                if equity > 0 and sec_val / equity * 100.0 > scap:
                    return "{} sector {:.0f}% > {:g}% cap".format(sec, sec_val / equity * 100.0, scap)

        # 18: pyramiding discipline — when ON, only add to a winner, capped
        if cfg.get("pyramiding") and held > 0:
            avg = float((positions.get(symbol) or {}).get("avg_cost") or 0)
            gain = (price - avg) / avg * 100.0 if avg > 0 else 0.0
            # "winner only": an unconfigured min-gain falls back to +1% rather
            # than 0, so a flat position isn't pyramided into.
            min_gain = float(cfg.get("pyramid_min_gain_pct") or 0) or 1.0
            if gain < min_gain:
                return "pyramid: {} only +{:.1f}% (need +{:g}%)".format(symbol, gain, min_gain)
            # max_adds <= 0 means "unlimited adds", not "zero allowed" — enabling
            # pyramiding without tuning the cap shouldn't block every add.
            max_adds = int(cfg.get("pyramid_max_adds") or 0)
            adds = _adds_since_open(symbol)
            if max_adds > 0 and adds >= max_adds:
                return "pyramid: {} at max adds ({})".format(symbol, adds)
        return None
    except Exception:
        log.exception("entry_block_reason failed -> allow (fail-open)")
        return None


# ════════════════════════════════════════════════════════════════════════════
#  EXIT INTELLIGENCE (12-15)  → extra exit signals merged with aj_rules
# ════════════════════════════════════════════════════════════════════════════

def extra_exit_signals(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Time stop (12), profit ratchet (13), TP ladder (14), ATR stop (15).
    Returns [{symbol, qty, reason, mark}] (qty may be partial for the ladder)."""
    out: List[Dict[str, Any]] = []
    try:
        mhd = int(cfg.get("max_holding_days") or 0)
        ratchet = float(cfg.get("profit_ratchet_pct") or 0)
        lock = float(cfg.get("profit_ratchet_lock_pct") or 0)
        ladder = bool(cfg.get("tp_ladder"))
        tp = float(cfg.get("take_profit_pct") or 0)
        atr_mult = float(cfg.get("atr_stop_mult") or 0)
        if not (mhd > 0 or ratchet > 0 or (ladder and tp > 0) or atr_mult > 0):
            return []
        book = _paper_book()
        positions = book.get("positions") or {}
        if not positions:
            return []
        import aj_risk
        marks = aj_risk._marks(list(positions.keys()))
        state = {r["symbol"]: r for r in aj_db.query("SELECT * FROM aj_position_state")}
        ladder_fired = _tp_ladder_fired() if (ladder and tp > 0) else {}
        ladder_dirty = False
        if ladder and tp > 0:
            # prune fired-rung memory for symbols no longer held (re-entry resets)
            held_syms = {str(s).upper() for s in positions}
            for s in list(ladder_fired.keys()):
                if s not in held_syms:
                    del ladder_fired[s]
                    ladder_dirty = True
        for sym, p in positions.items():
            qty = float(p.get("qty") or 0)
            avg = float(p.get("avg_cost") or 0)
            if qty <= 0 or avg <= 0:
                continue
            mark = marks.get(sym)
            if mark is None:
                continue
            gain = (mark - avg) / avg * 100.0
            st = state.get(sym) or {}

            # 12: time-based stop
            if mhd > 0:
                age = _age_days(st)
                if age is not None and age >= mhd:
                    out.append(_exit(sym, qty, "max-holding {}d (age {:.0f}d)".format(mhd, age), mark))
                    continue

            # 13: profit ratchet — ran up past `ratchet`, gave back to `lock`
            if ratchet > 0:
                peak = float(st.get("peak_mark") or 0)
                peak_gain = (peak - avg) / avg * 100.0 if (peak > 0 and avg > 0) else 0.0
                if peak_gain >= ratchet and gain <= lock:
                    out.append(_exit(sym, qty, "profit-ratchet: peaked +{:.1f}%, locked +{:.1f}%".format(peak_gain, gain), mark))
                    continue

            # 14: take-profit ladder — scale out a third at tp / 1.5tp / 2tp.
            # Each rung fires at most once per open position (tracked in
            # _TP_LADDER_KEY, reset on going flat) so repeat cycles past a rung
            # don't keep dumping a fraction of the shrinking remainder.
            if ladder and tp > 0:
                key = str(sym).upper()
                fired = set(ladder_fired.get(key) or [])
                if gain >= 2 * tp and 3 not in fired:
                    out.append(_exit(sym, qty, "tp-ladder rung-3 +{:.1f}%".format(gain), mark))
                    fired.update({1, 2, 3})
                    ladder_fired[key] = sorted(fired)
                    ladder_dirty = True
                    continue
                if gain >= 1.5 * tp and 2 not in fired:
                    out.append(_exit(sym, round(qty / 2, 6), "tp-ladder rung-2 +{:.1f}%".format(gain), mark))
                    # Only mark rung-2 fired: rung-1 wasn't sold here, so a later
                    # cycle may still scale out its third per the thirds ladder.
                    fired.add(2)
                    ladder_fired[key] = sorted(fired)
                    ladder_dirty = True
                    continue
                if gain >= tp and 1 not in fired:
                    out.append(_exit(sym, round(qty / 3, 6), "tp-ladder rung-1 +{:.1f}%".format(gain), mark))
                    fired.add(1)
                    ladder_fired[key] = sorted(fired)
                    ladder_dirty = True
                    continue

            # 15: ATR volatility stop — loss beyond mult x ATR from entry
            if atr_mult > 0:
                atr = _atr(_bars(sym, "6mo"), int(cfg.get("atr_period") or 14))
                if atr is not None and atr > 0 and mark <= avg - atr_mult * atr:
                    out.append(_exit(sym, qty, "atr-stop {:.1f}x ATR".format(atr_mult), mark))
                    continue
        if ladder and tp > 0 and ladder_dirty:
            _set_tp_ladder_fired(ladder_fired)
    except Exception:
        log.exception("extra_exit_signals failed -> none")
    return out


def _tp_ladder_fired() -> Dict[str, List[int]]:
    """Which TP-ladder rungs have already fired per symbol (persisted)."""
    try:
        raw = aj_db.get_setting_raw(_TP_LADDER_KEY)
        d = json.loads(raw) if raw else {}
        return {str(k).upper(): list(v or []) for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:
        return {}


def _set_tp_ladder_fired(d: Dict[str, List[int]]) -> None:
    try:
        aj_db.set_setting_raw(_TP_LADDER_KEY, json.dumps(d))
    except Exception:
        log.debug("tp-ladder state write failed", exc_info=True)


def _exit(symbol: str, qty: float, reason: str, mark: float) -> Dict[str, Any]:
    return {"symbol": symbol, "qty": qty, "reason": reason, "mark": mark}


def _age_days(state_row: Dict[str, Any]) -> Optional[float]:
    opened = state_row.get("opened_at")
    if not opened:
        return None
    dt = aj_db.parse_iso(opened)
    if not dt:
        return None
    return (aj_db.utc_now() - dt).total_seconds() / 86400.0


# ════════════════════════════════════════════════════════════════════════════
#  ADAPTIVE BRAIN (16-17)  → effective_config()
# ════════════════════════════════════════════════════════════════════════════

def detect_regime() -> str:
    """'bull' | 'bear' | 'chop' from SPY vs its 50d/200d SMA and trend."""
    closes = _market_closes("1y")
    if len(closes) < 60:
        return "chop"
    price = closes[-1]
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200) if len(closes) >= 200 else sma50
    if sma50 is None:
        return "chop"
    if price > sma50 and (sma200 is None or sma50 >= sma200):
        return "bull"
    if price < sma50 and (sma200 is None or sma50 <= sma200):
        return "bear"
    return "chop"


def recent_hit_rate_detail(n: int = 20) -> Optional[Tuple[float, int, int]]:
    """(win_rate, wins, decisive_n) over the most recent n realized paper trades,
    or None when too few decisive trades. Scratch trades (realized == 0) are
    excluded. Exposes wins/n so callers can apply a Wilson confidence bound
    before adapting on a small sample."""
    trades = _realized_trades()[-n:]
    decisive = [t for t in trades if float(t.get("realized") or 0) != 0]
    if len(decisive) < 5:
        return None
    wins = sum(1 for t in decisive if float(t.get("realized") or 0) > 0)
    return wins / len(decisive), wins, len(decisive)


def recent_hit_rate(n: int = 20) -> Optional[float]:
    """Win-rate over the most recent n realized paper trades. Scratch trades
    (realized == 0) are excluded from the denominator so they don't depress the
    rate and over-tighten the adaptive thresholds."""
    d = recent_hit_rate_detail(n)
    return d[0] if d is not None else None


def effective_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a cfg copy with adaptive (16) + regime (17) threshold nudges
    applied. A no-op when both are off, so callers can always route through it."""
    eff = dict(cfg)
    try:
        # 16: adaptive thresholds — tune to recent realized hit-rate, but only
        # on statistically-supported evidence: tighten when the Wilson UPPER
        # bound is still < 0.40 (confident the streak is bad), loosen when the
        # Wilson LOWER bound is > 0.55 (confident it's good). A small sample
        # keeps the bounds wide and the thresholds untouched (no fragile nudge
        # off a 3-of-5 streak).
        if cfg.get("adaptive_thresholds"):
            det = recent_hit_rate_detail(20)
            if det is not None:
                _hr, _wins, _ntr = det
                lo = _wilson_lower_bound(_wins, _ntr)
                z2 = 1.96 * 1.96
                _denom = 1.0 + z2 / _ntr
                _centre = _hr + z2 / (2 * _ntr)
                _margin = 1.96 * math.sqrt((_hr * (1 - _hr) + z2 / (4 * _ntr)) / _ntr)
                hi = (_centre + _margin) / _denom
                if hi < 0.40:           # confidently bleeding — demand more edge
                    eff["buy_prob_threshold"] = _clamp(float(eff.get("buy_prob_threshold") or 0.55) + 0.03, 0.5, 0.75)
                    eff["min_edge_pct_pts"] = float(eff.get("min_edge_pct_pts") or 0) + 1.0
                elif lo > 0.55:          # confident hot hand — capture a touch more
                    eff["buy_prob_threshold"] = _clamp(float(eff.get("buy_prob_threshold") or 0.55) - 0.02, 0.50, 0.70)
        # 17: regime-adaptive profile
        if cfg.get("regime_adaptive"):
            regime = detect_regime()
            eff["_regime"] = regime
            if regime == "bull":
                eff["buy_prob_threshold"] = _clamp(float(eff.get("buy_prob_threshold") or 0.55) - 0.03, 0.50, 0.70)
                eff["min_edge_pct_pts"] = max(0.0, float(eff.get("min_edge_pct_pts") or 0) - 0.5)
            elif regime == "bear":
                eff["buy_prob_threshold"] = _clamp(float(eff.get("buy_prob_threshold") or 0.55) + 0.05, 0.50, 0.80)
                eff["sell_prob_threshold"] = _clamp(float(eff.get("sell_prob_threshold") or 0.45) - 0.03, 0.30, 0.50)
                eff["min_edge_pct_pts"] = float(eff.get("min_edge_pct_pts") or 0) + 1.5
    except Exception:
        log.exception("effective_config failed -> base cfg")
        return dict(cfg)
    return eff


# ════════════════════════════════════════════════════════════════════════════
#  SIGNAL SCORECARD (19)
# ════════════════════════════════════════════════════════════════════════════

def scorecard_record(conviction: str, realized_pnl: float) -> None:
    """Tally a closed trade's outcome under its originating conviction bucket."""
    try:
        bucket = (conviction or "none").lower()
        raw = aj_db.get_setting_raw(_SCORECARD_KEY)
        data = json.loads(raw) if raw else {}
        b = data.get(bucket) or {"n": 0, "wins": 0, "pnl": 0.0}
        b["n"] += 1
        if realized_pnl > 0:
            b["wins"] += 1
        b["pnl"] = aj_db.money(b["pnl"] + realized_pnl)
        data[bucket] = b
        aj_db.set_setting_raw(_SCORECARD_KEY, json.dumps(data))
    except Exception:
        log.debug("scorecard_record failed", exc_info=True)


def note_entry_conviction(symbol: str, conviction: str) -> None:
    """Remember the conviction a position was opened on, so its eventual close
    can be scored under the right bucket (19)."""
    try:
        raw = aj_db.get_setting_raw(_ENTRY_CONVICTION_KEY)
        d = json.loads(raw) if raw else {}
        d[symbol.upper()] = (conviction or "none").lower()
        aj_db.set_setting_raw(_ENTRY_CONVICTION_KEY, json.dumps(d))
    except Exception:
        log.debug("note_entry_conviction failed", exc_info=True)


def pop_entry_conviction(symbol: str) -> str:
    """The conviction a position was opened on (default 'none')."""
    try:
        raw = aj_db.get_setting_raw(_ENTRY_CONVICTION_KEY)
        d = json.loads(raw) if raw else {}
        return d.get(symbol.upper(), "none")
    except Exception:
        return "none"


def scorecard() -> Dict[str, Any]:
    """Per-conviction realized win-rate & net P&L, plus per-signal hit-rates
    from the forecast accountability ledger when available."""
    out: Dict[str, Any] = {"by_conviction": {}, "signals": []}
    try:
        raw = aj_db.get_setting_raw(_SCORECARD_KEY)
        data = json.loads(raw) if raw else {}
        for bucket, b in data.items():
            n = int(b.get("n") or 0)
            out["by_conviction"][bucket] = {
                "n": n, "wins": int(b.get("wins") or 0),
                "win_rate": round(b["wins"] / n, 3) if n else None,
                "net_pnl": aj_db.money(b.get("pnl") or 0)}
    except Exception:
        log.debug("scorecard read failed", exc_info=True)
    try:
        import forecast_accountability as fa
        if hasattr(fa, "signal_scoreboard"):
            out["signals"] = fa.signal_scoreboard() or []
        elif hasattr(fa, "adaptive_weights"):
            w = fa.adaptive_weights() or {}
            out["signals"] = [{"name": k, "weight": v} for k, v in w.items()]
    except Exception:
        log.debug("signal scoreboard unavailable", exc_info=True)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  OPPORTUNITY RADAR (20)
# ════════════════════════════════════════════════════════════════════════════

def _radar_score(symbol: str) -> float:
    """Cheap composite setup score: momentum + relative strength + not-overbought."""
    try:
        import portfolio_insights as pi
        closes = _closes(symbol, "1y")
        if len(closes) < 30:
            return 0.0
        score = 0.0
        ms = pi.momentum_score(closes)            # 0-100
        if ms is not None:
            score += float(ms)
        rs = _pct_return(closes, 20)
        mkt = _pct_return(_market_closes("1y"), 20)
        if rs is not None and mkt is not None:
            score += (rs - mkt) * 2.0             # reward outperformance
        r = _rsi(closes, 14)
        if r is not None and r > 75:
            score -= 20.0                         # penalise overbought
        return score
    except Exception:
        return 0.0


def rank_universe(symbols: List[str], cfg: Dict[str, Any]) -> List[str]:
    """When opportunity_radar is on, rank candidates by setup score and return
    only the top-K. Otherwise return the input unchanged."""
    try:
        if not cfg.get("opportunity_radar"):
            return list(symbols)
        k = max(1, int(cfg.get("opportunity_radar_top_k") or 5))

        def _score_key(t):
            v = t[1]
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return float("inf")        # unscorable -> sink to the bottom
            if math.isnan(fv):
                return float("inf")        # NaN -> sink (keeps sort stable)
            return -fv
        scored = sorted(((s, _radar_score(s)) for s in symbols), key=_score_key)
        return [s for s, _ in scored[:k]]
    except Exception:
        log.exception("rank_universe failed -> unchanged")
        return list(symbols)
