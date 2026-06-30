"""Portfolio & symbol insight signals (v2.5).

New, self-contained technical/positioning signals that aren't already covered
by /api/analytics/{risk,correlation,portfolio}. The math (rsi, drawdown, vol,
momentum, sharpe proxy, range position) is pure and import-free so it unit-tests
against synthetic series; the data-fetching wrappers (symbol_signal,
portfolio_health) pull closes via fetcher.get_chart_data and are best-effort /
fail-open. Everything degrades gracefully on short or missing series.
"""
import logging
import math
from typing import Any, Dict, List, Optional

log = logging.getLogger("augur.portfolio_insights")


# ── pure math (newest-last close series) ─────────────────────────────────────

def daily_returns(closes: List[float]) -> List[float]:
    out = []
    for i in range(1, len(closes)):
        p0 = closes[i - 1]
        if p0:
            out.append(closes[i] / p0 - 1.0)
    return out


def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI over the last `period` deltas. None if too short."""
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def max_drawdown(closes: List[float]) -> Optional[float]:
    """Worst peak-to-trough decline as a negative pct (e.g. -23.4). None if <2."""
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = 0.0
    for c in closes:
        if c > peak:
            peak = c
        if peak:
            dd = c / peak - 1.0
            if dd < worst:
                worst = dd
    return round(worst * 100.0, 1)


def annualized_vol(closes: List[float]) -> Optional[float]:
    """Annualized volatility (%) from daily returns. None if too short."""
    rets = daily_returns(closes)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round(math.sqrt(var) * math.sqrt(252) * 100.0, 1)


def sma(closes: List[float], n: int) -> Optional[float]:
    if len(closes) < n or n <= 0:
        return None
    return sum(closes[-n:]) / n


def sharpe_proxy(closes: List[float]) -> Optional[float]:
    """Annualized mean return / annualized vol (rf=0). A rough risk-adjusted
    momentum read, not a true Sharpe. None if too short."""
    rets = daily_returns(closes)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    sd = (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
    if sd == 0:
        return None
    return round((mean * 252) / (sd * math.sqrt(252)), 2)


def range_position(price: float, low: float, high: float) -> Optional[float]:
    """Where price sits in the [low, high] band, 0..100. None if degenerate."""
    if price is None or low is None or high is None or high <= low:
        return None
    return round(max(0.0, min(1.0, (price - low) / (high - low))) * 100.0, 0)


def momentum_score(closes: List[float]) -> Optional[int]:
    """Blended 0..100 momentum: price vs 50d MA, 50d vs 200d MA, ~60d return.
    Falls back gracefully when the series is too short for the longer MAs."""
    if len(closes) < 10:
        return None
    score = 50.0
    price = closes[-1]
    s50 = sma(closes, 50)
    s200 = sma(closes, 200)
    if s50 is not None:
        score += 15 if price >= s50 else -15
    if s50 is not None and s200 is not None:
        score += 15 if s50 >= s200 else -15
    look = closes[-60] if len(closes) >= 60 else closes[0]
    if look:
        ret = price / look - 1.0
        score += max(-20.0, min(20.0, ret * 100.0))  # ±20 cap
    return int(max(0, min(100, round(score))))


def series_metrics(closes: List[float]) -> Dict[str, Any]:
    """Bundle the pure signals for a close series."""
    return {
        "rsi14": rsi(closes),
        "max_drawdown_pct": max_drawdown(closes),
        "annualized_vol_pct": annualized_vol(closes),
        "momentum_score": momentum_score(closes),
        "sharpe_proxy": sharpe_proxy(closes),
        "above_sma50": (sma(closes, 50) is not None and closes[-1] >= sma(closes, 50))
        if closes else None,
        "n_bars": len(closes),
    }


def _stance(m: Dict[str, Any]) -> str:
    """Coarse bull/bear/neutral from the metric bundle."""
    mom = m.get("momentum_score")
    rsi_v = m.get("rsi14")
    if mom is None:
        return "neutral"
    # RSI only vetoes at true blow-off extremes; healthy trends sit at 70-80.
    if mom >= 66 and (rsi_v is None or rsi_v < 85):
        return "bull"
    if mom <= 38 and (rsi_v is None or rsi_v > 15):
        return "bear"
    return "neutral"


# ── data-backed wrappers (best-effort) ───────────────────────────────────────

def _closes(symbol: str, period: str = "1y") -> List[float]:
    import fetcher
    bars = fetcher.get_chart_data(symbol, period, "1d") or []
    return [b["close"] for b in bars if isinstance(b, dict) and b.get("close") is not None]


def symbol_signal(symbol: str, period: str = "1y") -> Dict[str, Any]:
    """Technical/positioning signal bundle for one symbol. Fail-open. Cached
    (coalesced) 10 min — chart history barely moves intraday and the breadth
    fan-out calls this per holding, so caching dedupes the heavy chart pulls."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"error": "symbol required"}
    try:
        import cache_store
        return cache_store.coalesce(("symbol_signal", symbol, period), 600,
                                    lambda: _symbol_signal_compute(symbol, period))
    except Exception:
        return _symbol_signal_compute(symbol, period)


def _symbol_signal_compute(symbol: str, period: str = "1y") -> Dict[str, Any]:
    out: Dict[str, Any] = {"symbol": symbol}
    try:
        closes = _closes(symbol, period)
        if len(closes) < 10:
            return {"symbol": symbol, "error": "insufficient history"}
        m = series_metrics(closes)
        out.update(m)
        out["stance"] = _stance(m)
        # 52-week range position from the live quote.
        try:
            import fetcher
            q = fetcher.get_quote(symbol) or {}
            out["range_position_52w"] = range_position(
                q.get("price"), q.get("fifty_two_week_low"),
                q.get("fifty_two_week_high"))
            out["price"] = q.get("price")
        except Exception:
            pass
    except Exception as e:
        log.debug("symbol_signal failed for %s: %s", symbol, e)
        return {"symbol": symbol, "error": "signal unavailable"}
    return out


def portfolio_health(max_symbols: int = 25) -> Dict[str, Any]:
    """Aggregate breadth/positioning across equity holdings: weighted momentum,
    how many are above their 50d MA, near 52w highs, or in a deep drawdown, plus
    concentration (HHI). Fail-open; uses safe_executor for the fan-out."""
    try:
        import database as db
        holdings = db.get_portfolio() or []
    except Exception:
        return {"error": "no portfolio"}
    eq = [h for h in holdings if (h.get("asset_type") or "stock") != "crypto"
          and h.get("symbol")]
    if not eq:
        return {"n_holdings": 0, "note": "No equity holdings to analyze."}

    # Value weights from live quotes (best-effort).
    weights: Dict[str, float] = {}
    try:
        import fetcher
        syms = list({h["symbol"].upper() for h in eq})
        quotes = fetcher.get_quotes_batch(syms) or {}
        total = 0.0
        vals: Dict[str, float] = {}
        for h in eq:
            s = h["symbol"].upper()
            px = (quotes.get(s) or {}).get("price")
            if px:
                v = float(h.get("shares") or 0) * float(px)
                vals[s] = vals.get(s, 0.0) + v
                total += v
        if total > 0:
            weights = {s: v / total for s, v in vals.items()}
    except Exception:
        pass

    symbols = list({h["symbol"].upper() for h in eq})[:max_symbols]

    def _one(sym):
        try:
            return symbol_signal(sym, period="1y")
        except Exception:
            return {"symbol": sym, "error": "x"}

    try:
        import safe_executor
        sigs = safe_executor.parallel_map(_one, symbols, max_workers=6,
                                          timeout_per_item=20,
                                          thread_name_prefix="insights")
    except Exception:
        sigs = [_one(s) for s in symbols]

    good = [s for s in sigs if s and not s.get("error") and s.get("momentum_score") is not None]
    if not good:
        return {"n_holdings": len(eq), "analyzed": 0,
                "note": "Couldn't pull enough history to gauge breadth right now."}

    above_50 = sum(1 for s in good if s.get("above_sma50"))
    near_high = sum(1 for s in good if (s.get("range_position_52w") or 0) >= 85)
    deep_dd = sum(1 for s in good if (s.get("max_drawdown_pct") or 0) <= -25)
    # Weighted momentum — use ONE weighting scheme consistently, not a mix of
    # value-weights (for symbols present in `weights`) and 1/len (for absent
    # ones), which skewed the average toward priced-but-light names. When value
    # weights exist, weight ONLY the symbols that have one (the wsum/wtot ratio
    # renormalizes over exactly that subset); otherwise fall back to a pure
    # equal-weight average over all analyzed symbols.
    wsum = 0.0
    wtot = 0.0
    if weights:
        for s in good:
            w = weights.get(s["symbol"])
            if w is None:
                continue
            wsum += w * s["momentum_score"]
            wtot += w
    if wtot:
        weighted_mom = round(wsum / wtot)
    elif good:
        # Equal-weight fallback (no usable value weights for the analyzed set).
        weighted_mom = round(sum(s["momentum_score"] for s in good) / len(good))
    else:
        weighted_mom = None
    hhi = round(sum(w * w for w in weights.values()), 3) if weights else None

    breadth = round(100.0 * above_50 / len(good))
    if weighted_mom is None:
        tone = "mixed"
    elif weighted_mom >= 62 and breadth >= 60:
        tone = "constructive"
    elif weighted_mom <= 40 or breadth <= 35:
        tone = "defensive"
    else:
        tone = "mixed"

    return {
        "n_holdings": len(eq),
        "analyzed": len(good),
        "weighted_momentum": weighted_mom,
        "breadth_above_50dma_pct": breadth,
        "near_52w_high": near_high,
        "in_deep_drawdown": deep_dd,
        "concentration_hhi": hhi,
        "tone": tone,
    }
