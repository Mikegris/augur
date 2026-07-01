"""AJTA — benchmark the agent's performance vs the leading market indexes.

Builds the agent's daily NAV series (funded base + cumulative realized+unrealized
P&L, from aj_equity) and compares its day-over-day and cumulative return to SPY /
QQQ / DIA / IWM over the same trading dates: cumulative return each, alpha
(agent − index), and the fraction of days the agent beat each index.

Read-only, fail-open. Returns an {"error"/"verdict":"insufficient data"} envelope
until at least two equity snapshots exist. Powers the Insights "Benchmark vs
Indexes" panel.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional, Tuple

import aj_config

log = logging.getLogger("augur.aj_benchmark")

# The leading indexes, tracked via their liquid ETFs.
_INDEXES = [("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"),
            ("DIA", "Dow Jones"), ("IWM", "Russell 2000")]

_PERIOD_FOR_DAYS = [(35, "1mo"), (95, "3mo"), (190, "6mo"), (370, "1y")]


def _period_for(days: int) -> str:
    for d, p in _PERIOD_FOR_DAYS:
        if days <= d:
            return p
    return "2y"


def _agent_nav_series(cfg: Dict[str, Any], days: int) -> List[Tuple[str, float]]:
    """[(date, NAV)] ascending. NAV = funded base + cumulative P&L (equity_usd)."""
    try:
        import aj_analytics
        base = float(cfg.get("compound_base_equity_usd") or 10000.0) or 10000.0
        rows = aj_analytics.equity_curve(days=days) or []
        by_date: Dict[str, float] = {}
        for r in rows:
            d = r.get("date")
            eq = r.get("equity_usd")
            if d is None or eq is None:
                continue
            try:
                by_date[str(d)] = base + float(eq)
            except (TypeError, ValueError):
                continue
        return sorted(by_date.items())
    except Exception:
        log.debug("agent nav series failed", exc_info=True)
        return []


def _index_closes(symbol: str, period: str) -> Dict[str, float]:
    """{date(ET) -> close} for an index ETF over `period`. Empty on any error."""
    try:
        import fetcher
        rows = fetcher.get_chart_data(symbol, period, "1d") or []
    except Exception:
        log.debug("index chart fetch failed for %s", symbol, exc_info=True)
        return {}
    out: Dict[str, float] = {}
    for r in rows:
        t = r.get("time")
        c = r.get("close")
        if t is None or c is None:
            continue
        try:
            dt = datetime.datetime.fromtimestamp(int(t), tz=datetime.timezone.utc)
            try:
                from zoneinfo import ZoneInfo
                dt = dt.astimezone(ZoneInfo("America/New_York"))
            except Exception:
                pass
            out[dt.strftime("%Y-%m-%d")] = float(c)
        except Exception:
            continue
    return out


def _cum_series(dates: List[str], value_on) -> List[Optional[float]]:
    """Cumulative % return from the first date, given a date->value lookup."""
    base = None
    series: List[Optional[float]] = []
    for d in dates:
        v = value_on(d)
        if v is None or v <= 0:
            series.append(None)
            continue
        if base is None:
            base = v
        series.append(round((v / base - 1.0) * 100.0, 3) if base else 0.0)
    return series


def _round_series(s: List[Optional[float]]) -> List[Optional[float]]:
    return [round(x, 3) if x is not None else None for x in s]


def return_between(symbol: str, start_date: str, end_date: str,
                   period: str = "1y") -> Optional[float]:
    """% return of a benchmark ETF between two dates (close-on-or-before each).
    Used to label a closed trade's alpha vs the market over its holding window.
    None if unavailable. Never raises."""
    try:
        if not start_date or not end_date:
            return None
        closes = _index_closes(symbol, period)
        if not closes:
            return None
        sd = sorted(closes)
        s = str(start_date)[:10]
        e = str(end_date)[:10]

        def on(d):
            if d in closes:
                return closes[d]
            prev = [x for x in sd if x <= d]
            return closes[prev[-1]] if prev else None

        p0, p1 = on(s), on(e)
        if p0 is None or p1 is None or p0 <= 0:
            return None
        return (p1 / p0 - 1.0) * 100.0
    except Exception:
        log.debug("return_between failed", exc_info=True)
        return None


def benchmark(cfg: Optional[Dict[str, Any]] = None, days: int = 90) -> Dict[str, Any]:
    """Agent vs leading indexes over the agent's equity dates. Never raises."""
    try:
        cfg = cfg or aj_config.get_config()
        nav = _agent_nav_series(cfg, days)
        if len(nav) < 2:
            return {"verdict": "insufficient data", "n_days": len(nav),
                    "note": "need at least two daily equity snapshots to compare"}
        dates = [d for d, _ in nav]
        navmap = dict(nav)
        base_equity = float(cfg.get("compound_base_equity_usd") or 10000.0) or 10000.0
        period = _period_for(days)

        agent_series = _cum_series(dates, lambda d: navmap.get(d))
        agent_cum = next((x for x in reversed(agent_series) if x is not None), 0.0)

        benchmarks: Dict[str, Any] = {}
        vs: Dict[str, Any] = {}
        for sym, name in _INDEXES:
            closes = _index_closes(sym, period)
            if not closes:
                continue
            sorted_dates = sorted(closes)

            def price_on(d, _c=closes, _sd=sorted_dates):
                if d in _c:
                    return _c[d]
                prev = [x for x in _sd if x <= d]   # last close on/before d
                return _c[prev[-1]] if prev else None

            idx_series = _cum_series(dates, price_on)
            idx_cum = next((x for x in reversed(idx_series) if x is not None), None)
            if idx_cum is None:
                continue
            benchmarks[sym] = {"name": name, "cumulative_return_pct": round(idx_cum, 2),
                               "series": _round_series(idx_series)}
            # per-day: did the agent's daily move beat the index's?
            beaten = total = 0
            for i in range(1, len(dates)):
                a0, a1 = agent_series[i - 1], agent_series[i]
                b0, b1 = idx_series[i - 1], idx_series[i]
                if None in (a0, a1, b0, b1):
                    continue
                total += 1
                if (a1 - a0) > (b1 - b0):
                    beaten += 1
            vs[sym] = {"name": name, "alpha_pct": round(agent_cum - idx_cum, 2),
                       "days_beaten": beaten, "days_total": total,
                       "beat_rate": round(beaten / total, 3) if total else None}

        spy_alpha = (vs.get("SPY") or {}).get("alpha_pct")
        verdict = ("outperforming" if (spy_alpha or 0) > 0 else "underperforming") \
            if spy_alpha is not None else "n/a"

        return {
            "as_of": dates[-1],
            "n_days": len(dates),
            "base_equity": base_equity,
            "dates": dates,
            "agent": {"cumulative_return_pct": round(agent_cum, 2),
                      "series": _round_series(agent_series)},
            "benchmarks": benchmarks,
            "vs": vs,
            "verdict": verdict,
        }
    except Exception:
        log.debug("benchmark failed", exc_info=True)
        return {"verdict": "insufficient data", "error": "benchmark unavailable"}
