"""
jarvis_tools — the tool registry that gives the Jarvis agent access to the
whole app.

Each entry wraps an existing engine behind an OpenAI function-calling schema.
The agent loop in jarvis.py decides which tools to call; this module owns
WHAT exists and HOW it executes:

  • read tools execute immediately and return condensed JSON (responses are
    truncated to keep prompt cost bounded — the model sees a summary, not a
    dump);
  • mutating tools NEVER execute from the agent loop. The loop returns a
    proposal the UI must confirm, and only then does /api/jarvis/act call
    execute_mutating() — a whitelist keyed on this registry, so the LLM can
    only ever request actions defined here, with validated arguments.

Python 3.9 compatible.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("augur.jarvis_tools")

_RESULT_CHAR_BUDGET = 1800
_SYM_RE = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,9}$")


def _sym(args: Dict[str, Any]) -> str:
    s = str(args.get("symbol") or "").strip().upper()
    if not _SYM_RE.match(s):
        raise ValueError("invalid symbol")
    return s


def _portfolio_stock_symbols(max_n: int = 15) -> List[str]:
    """Sorted, de-duplicated non-crypto holdings — the symbol universe the
    analytics tools (risk/correlation) operate on. Capped so condensed
    outputs stay inside the result budget."""
    import database as db
    return sorted({h["symbol"] for h in db.get_portfolio()
                   if h.get("asset_type") != "crypto"})[:max_n]


# ─── read-tool implementations (condensed outputs) ───────────────────────────

def _t_get_quote(args):
    import fetcher
    q = fetcher.get_quote(_sym(args))
    keep = ("symbol", "price", "change", "change_pct", "prev_close", "volume",
            "market_cap", "fifty_two_week_high", "fifty_two_week_low")
    return {k: q.get(k) for k in keep}


def _t_get_portfolio(args):
    import jarvis
    pulse = jarvis._portfolio_pulse()
    if not pulse:
        return {"note": "no positions"}
    out = {k: v for k, v in pulse.items() if k != "holdings"}
    out["top_positions"] = [
        {"symbol": r.get("symbol"), "market_value": r.get("market_value"),
         "weight_pct": r.get("weight_pct"),
         "unrealized_pct": r.get("unrealized_pct"),
         "day_change_pct": r.get("day_change_pct")}
        for r in sorted(pulse["holdings"], key=lambda r: -r["market_value"])[:10]]
    return out


def _t_market_regime(args):
    import jarvis
    return jarvis._market_regime() or {"note": "market data unavailable"}


def _t_forecast(args):
    import forecast_ensemble
    horizon = int(args.get("horizon_days") or 20)
    horizon = max(5, min(horizon, 120))
    f = forecast_ensemble.ensemble_forecast(_sym(args), horizon_days=horizon)
    ens = (f or {}).get("ensemble") or {}
    return {"symbol": f.get("symbol"), "horizon_days": f.get("horizon_days"),
            "n_signals": f.get("n_signals"),
            "prob_up": ens.get("prob_up"), "direction": ens.get("direction"),
            "conviction": ens.get("conviction"), "verdict": ens.get("verdict"),
            "return_cone": ens.get("return_cone"), "notes": f.get("notes"),
            # per-signal breakdown, compacted: the model can explain WHICH
            # components drive the call without the full detail strings.
            "signals": [{"name": s.get("name"), "prob_up": s.get("prob_up"),
                         "weight": s.get("weight")}
                        for s in (f.get("signals") or [])]}


def _t_earnings(args):
    import earnings
    import database as db
    syms = args.get("symbols")
    if isinstance(syms, list) and syms:
        syms = [_sym({"symbol": s}) for s in syms[:10]]
    else:
        syms = [h["symbol"] for h in db.get_portfolio()
                if h.get("asset_type") != "crypto"][:25]
    return earnings.get_earnings_calendar(syms)


def _t_smart_money(args):
    import smart_money
    return smart_money.compute_score(_sym(args))


def _t_gex(args):
    import gex_engine
    return gex_engine.get_gex_summary(_sym(args))


def _t_options_flow(args):
    import fetcher
    return fetcher.get_unusual_options_flow(_sym(args))


def _t_news(args):
    import fetcher
    n = fetcher.get_news(_sym(args), limit=5)
    return [{"title": x.get("title"), "publisher": x.get("publisher"),
             "published": x.get("published") or x.get("providerPublishTime")}
            for x in (n or [])[:5]]


def _t_sector_flow(args):
    import synth_sectorflow
    raw = synth_sectorflow.sector_flow() or {}
    rows = raw.get("sectors") or []
    if not rows:
        return {"error": raw.get("error") or "sector flow unavailable"}
    ranked = sorted(
        (r for r in rows if r.get("composite_flow_score") is not None),
        key=lambda r: -r["composite_flow_score"])
    return {"as_of": raw.get("as_of"),
            "leader_sector": raw.get("leader_sector"),
            "laggard_sector": raw.get("laggard_sector"),
            "sectors": [{"sector": r.get("sector"), "etf": r.get("etf"),
                         "score": r.get("composite_flow_score"),
                         "rs_vs_spy_1mo": r.get("rs_vs_spy_1mo")}
                        for r in ranked]}


def _t_alerts(args):
    import database as db
    return db.get_price_alerts(include_triggered=True)


def _t_watchlist(args):
    import database as db
    return [{"symbol": w.get("symbol"), "name": w.get("name"),
             "alert_high": w.get("alert_high"), "alert_low": w.get("alert_low")}
            for w in db.get_watchlist()]


def _t_narrative(args):
    import narrative_engine
    return narrative_engine.analyze_narrative(_sym(args))


def _t_top_ideas(args):
    import idea_pool_warmer
    return idea_pool_warmer.list_warmed_symbols()[:10]


def _t_stress_test(args):
    import database as db
    import fetcher
    drop = args.get("custom_drop_pct")
    if drop is not None:
        drop = max(-95.0, min(0.0, float(drop)))
    holdings = db.get_portfolio()
    if not holdings:
        return {"note": "no positions"}
    syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"]
    crypto = [h["symbol"] for h in holdings if h["asset_type"] == "crypto"]
    prices: Dict[str, Any] = {}
    if syms:
        prices.update(fetcher.get_quotes_batch(syms))
    if crypto:
        cp = fetcher.get_quotes_batch([s + "-USD" for s in crypto])
        for s in crypto:
            k = (s + "-USD").upper()
            if k in cp:
                prices[s] = cp[k]
    for h in holdings:
        cp_ = prices.get(h["symbol"], {}).get("price")
        cur = cp_ if cp_ is not None else h["avg_cost"]
        h["market_value"] = round(cur * h["shares"], 2)
        h["current_price"] = cur
    return fetcher.get_portfolio_stress_test(holdings, custom_drop_pct=drop)


def _t_portfolio_risk(args):
    import fetcher
    syms = _portfolio_stock_symbols()
    if not syms:
        return {"note": "no stock positions"}
    period = str(args.get("period") or "1y")
    raw = fetcher.get_risk_metrics(syms, period=period)
    if not isinstance(raw, dict) or raw.get("error"):
        return {"error": (raw or {}).get("error") or "risk metrics unavailable"}
    per = {}
    for sym, m in raw.items():
        if sym.startswith("_") or not isinstance(m, dict) or not m:
            continue
        per[sym] = {"vol": m.get("annualized_vol"),
                    "sharpe": m.get("sharpe_ratio"),
                    "max_dd": m.get("max_drawdown"),
                    "var_95": m.get("var_95"),
                    "total_return": m.get("total_return")}
    out = {"period": period, "per_symbol": per}
    if raw.get("_missing_symbols"):
        out["missing_symbols"] = raw["_missing_symbols"]
    return out


def _t_portfolio_correlation(args):
    import fetcher
    syms = _portfolio_stock_symbols()
    if len(syms) < 2:
        return {"note": "need at least 2 stock positions"}
    period = str(args.get("period") or "3mo")
    raw = fetcher.get_correlation_matrix(syms, period=period)
    matrix = (raw or {}).get("matrix") or {}
    cols = list(matrix.keys())
    pairs = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            v = (matrix.get(a) or {}).get(b)
            if v is not None:
                pairs.append((a, b, v))
    if not pairs:
        return {"error": (raw or {}).get("error") or "correlation unavailable"}
    pairs.sort(key=lambda p: -p[2])
    fmt = lambda p: {"pair": "{}/{}".format(p[0], p[1]), "corr": p[2]}
    return {"period": period, "n_symbols": len(cols),
            "avg_pairwise_corr": round(sum(p[2] for p in pairs) / len(pairs), 4),
            "most_correlated": [fmt(p) for p in pairs[:5]],
            "least_correlated": [fmt(p) for p in pairs[-5:][::-1]]}


def _t_dividends(args):
    import fetcher
    d = fetcher.get_dividend_data(_sym(args)) or {}
    keep = ("symbol", "price", "div_rate", "div_yield", "ex_date", "pay_date",
            "frequency", "div_growth_5y", "payout_ratio")
    out = {k: d.get(k) for k in keep}
    if d.get("error"):
        out["error"] = d["error"]
    out["recent_payments"] = (d.get("history") or [])[:4]
    return out


def _t_transactions_summary(args):
    import database as db
    sym = _sym(args) if args.get("symbol") else None
    rows = db.get_transactions(symbol=sym, limit=200) or []
    if not rows:
        return {"note": "no transactions recorded"}
    buys = [r for r in rows if str(r.get("action") or "").upper() == "BUY"]
    sells = [r for r in rows if str(r.get("action") or "").upper() == "SELL"]
    return {"symbol_filter": sym, "count": len(rows),
            "buys": len(buys), "sells": len(sells),
            "bought_usd": round(sum(float(r.get("total") or 0) for r in buys), 2),
            "sold_usd": round(sum(float(r.get("total") or 0) for r in sells), 2),
            "recent": [{"date": r.get("date"), "symbol": r.get("symbol"),
                        "action": r.get("action"), "shares": r.get("shares"),
                        "price": r.get("price")} for r in rows[:8]]}


def _t_liquidity_stress(args):
    import liquidity_monitor
    s = liquidity_monitor.compute_stress_score() or {}
    if s.get("error"):
        return {"error": s["error"]}
    return {"composite_score": s.get("composite_score"),
            "regime": s.get("regime"),
            "regime_detail": s.get("regime_detail"),
            "position_sizing_multiplier": s.get("position_sizing_multiplier"),
            "recommendation": s.get("recommendation"),
            "percentile_rank": s.get("percentile_rank"),
            "indicators": {name: {"score": (v or {}).get("score"),
                                  "detail": (v or {}).get("detail")}
                           for name, v in (s.get("indicators") or {}).items()}}


_CONGRESS_SELL_CODES = ("S", "S (partial)", "SE", "S (Exchange)", "E (Exchange)")


def _t_congress_trades(args):
    import congress
    sym = _sym(args)
    trades = congress.get_trades_for_ticker(sym) or []
    if not trades:
        return {"symbol": sym, "trade_count": 0,
                "note": "no congressional trades found in the lookback window"}
    buys = sum(1 for t in trades if t.get("txn_type_raw") in ("P", "PE"))
    sells = sum(1 for t in trades if t.get("txn_type_raw") in _CONGRESS_SELL_CODES)
    return {"symbol": sym, "trade_count": len(trades),
            "buys": buys, "sells": sells,
            "recent": [{"member": t.get("member_name"), "type": t.get("txn_type"),
                        "date": t.get("txn_date"), "amount": t.get("amount_str")}
                       for t in trades[:6]]}


def _t_reflexivity(args):
    import reflexivity_detector
    r = reflexivity_detector.detect_loops(_sym(args)) or {}
    if r.get("error"):
        return {"error": r["error"]}
    return {"symbol": r.get("symbol"), "overall_risk": r.get("overall_risk"),
            "loop_count": r.get("loop_count"),
            "dominant_loop": r.get("dominant_loop"),
            "max_strength": r.get("max_strength"),
            "loops": [{"type": lp.get("type"), "detected": lp.get("detected"),
                       "strength": lp.get("strength"),
                       "direction": lp.get("direction")}
                      for lp in (r.get("active_loops") or [])]}


def _t_historical_analogs(args):
    import historical_analog
    r = historical_analog.compute_historical_analog(_sym(args)) or {}
    if not r.get("available"):
        return {"available": False,
                "error": r.get("error") or "no analogs available",
                "current_conditions": r.get("current_conditions")}
    return {"available": True, "match_count": r.get("match_count"),
            "current_conditions": r.get("current_conditions"),
            "forward_window_days": r.get("forward_window_days"),
            "forward_returns": r.get("forward_returns"),
            "interpretation": r.get("interpretation")}


def _t_benchmark_compare(args):
    import database as db
    import fetcher
    from datetime import datetime, timedelta
    bench = str(args.get("benchmark") or "SPY").strip().upper()
    if not _SYM_RE.match(bench):
        raise ValueError("invalid benchmark symbol")
    period = str(args.get("period") or "1y")
    days = {"1mo": 30, "3mo": 91, "6mo": 182, "1y": 365, "2y": 730}.get(period)
    if days is None:
        period, days = "1y", 365
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    window = [s for s in (db.get_snapshots() or [])
              if str(s.get("date") or "") >= cutoff and s.get("total_value")]
    if len(window) < 2:
        return {"note": "not enough portfolio history "
                        "(need 2+ daily snapshots inside the period)"}
    first, last = window[0], window[-1]
    port_ret = round((float(last["total_value"]) /
                      float(first["total_value"]) - 1) * 100, 2)
    bars = [b for b in (fetcher.get_benchmark_history(symbol=bench,
                                                      period=period) or [])
            if b.get("value")]
    # Align the benchmark to the first snapshot we actually have, so a short
    # snapshot history isn't compared against a full period of benchmark.
    try:
        start_epoch = int(datetime.strptime(
            str(first["date"])[:10], "%Y-%m-%d").timestamp())
        aligned = [b for b in bars if int(b.get("time") or 0) >= start_epoch]
        if len(aligned) >= 2:
            bars = aligned
    except Exception:
        pass
    bench_ret = (round((float(bars[-1]["value"]) /
                        float(bars[0]["value"]) - 1) * 100, 2)
                 if len(bars) >= 2 else None)
    out = {"period": period, "window_start": first.get("date"),
           "window_end": last.get("date"),
           "portfolio_return_pct": port_ret,
           "benchmark": bench, "benchmark_return_pct": bench_ret}
    if bench_ret is not None:
        out["excess_return_pct"] = round(port_ret - bench_ret, 2)
    return out


# ─── mutating-tool implementations (only via execute_mutating) ───────────────

def _t_add_alert(args):
    import database as db
    sym = _sym(args)
    alert_type = str(args.get("alert_type") or "").lower()
    if alert_type not in ("above", "below"):
        raise ValueError("alert_type must be 'above' or 'below'")
    price = float(args.get("price"))
    if not (0 < price < 10_000_000):
        raise ValueError("price out of range")
    alert_id = db.add_price_alert(sym, alert_type, price)
    return {"status": "created", "id": alert_id,
            "label": "{} {} ${:,.2f}".format(sym, alert_type, price)}


def _t_add_watchlist(args):
    import database as db
    sym = _sym(args)
    db.add_to_watchlist(sym)
    return {"status": "added", "label": "{} added to watchlist".format(sym)}


# ─── registry ────────────────────────────────────────────────────────────────

def _p(props: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}

_SYM_PROP = {"symbol": {"type": "string", "description": "Ticker symbol, e.g. NVDA"}}

TOOLS: Dict[str, Dict[str, Any]] = {
    "get_quote": {
        "fn": _t_get_quote, "mutating": False,
        "description": "Use for 'price of X' / 'how is X doing today': live quote with price, day change, volume, 52-week range.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "get_portfolio": {
        "fn": _t_get_portfolio, "mutating": False,
        "description": "Use FIRST for any question about what the user owns, P&L, or weights: total value, day move, top 10 positions with weight and day change.",
        "parameters": _p({}),
    },
    "get_market_regime": {
        "fn": _t_market_regime, "mutating": False,
        "description": "Use for 'how are markets' or to set context before giving advice: S&P/Nasdaq day moves, VIX level, volatility regime.",
        "parameters": _p({}),
    },
    "forecast": {
        "fn": _t_forecast, "mutating": False,
        "description": "Use when asked for an outlook, prediction or odds on a symbol: calibrated P(up), direction, conviction, return cone, plus the per-signal breakdown driving the call.",
        "parameters": _p(dict(_SYM_PROP, horizon_days={"type": "integer", "description": "Trading-day horizon (5-120, default 20)"}), ["symbol"]),
    },
    "get_earnings_calendar": {
        "fn": _t_earnings, "mutating": False,
        "description": "Use for 'when does X report' or to check upcoming catalysts: earnings dates with beat-rate history. Defaults to the user's holdings if no symbols given.",
        "parameters": _p({"symbols": {"type": "array", "items": {"type": "string"}}}),
    },
    "smart_money_score": {
        "fn": _t_smart_money, "mutating": False,
        "description": "Use when asked what insiders/institutions/Congress are doing in a symbol: fast cached convergence score across those channels. Prefer this over congress_trades for a quick read.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "get_gex": {
        "fn": _t_gex, "mutating": False,
        "description": "Use for options-positioning questions (pinning, gamma squeeze, dealer hedging): dealer gamma exposure summary with key levels.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "get_options_flow": {
        "fn": _t_options_flow, "mutating": False,
        "description": "Use when asked about unusual options activity or large directional bets in a symbol.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "get_news": {
        "fn": _t_news, "mutating": False,
        "description": "Use for 'why is X moving' or 'any news on X': latest 5 headlines for a symbol.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "sector_flow": {
        "fn": _t_sector_flow, "mutating": False,
        "description": "Use for sector-rotation questions ('where is money flowing'): the 11 sector ETFs ranked by composite flow score, with leader and laggard.",
        "parameters": _p({}),
    },
    "get_alerts": {
        "fn": _t_alerts, "mutating": False,
        "description": "Use when asked about the user's price alerts (armed and triggered). Read-only; use add_price_alert to create one.",
        "parameters": _p({}),
    },
    "get_watchlist": {
        "fn": _t_watchlist, "mutating": False,
        "description": "Use when asked what's on the user's watchlist. Read-only; use add_to_watchlist to add.",
        "parameters": _p({}),
    },
    "narrative": {
        "fn": _t_narrative, "mutating": False,
        "description": "Use for hype/story questions ('is X overhyped'): news-narrative phase analysis (emergence/acceleration/exhaustion).",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "top_ideas": {
        "fn": _t_top_ideas, "mutating": False,
        "description": "Use when asked for new investment ideas: instant, pre-scored symbols from the warmed idea pool.",
        "parameters": _p({}),
    },
    "position_review": {
        "fn": lambda args: __import__("jarvis_lens").position_review(_sym(args)),
        "mutating": False,
        "description": "Use for 'is X a good business / should I keep holding X / what am I actually paying for': Buffett-style review — business summary, quality score with reasons, valuation flags, the user's basis/holding period, and a margin-of-safety take.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "temperament_check": {
        "fn": lambda args: __import__("jarvis_lens").temperament_check(),
        "mutating": False,
        "description": "Use for 'how is my trading behavior / am I overtrading / be honest with me': behavioral analysis of the user's REAL transaction history — quick flips, chasing strength, churn rate. Instant and local.",
        "parameters": _p({}),
    },
    "macro_brief": {
        "fn": lambda args: {k: v for k, v in __import__("jarvis_lens").macro_brief().items() if k != "voice"},
        "mutating": False,
        "description": "Use for 'what's the macro picture / what regime are we in / weekly outlook': fused strategist read — vol regime, sector rotation, liquidity stress, crypto risk appetite, one narrative.",
        "parameters": _p({}),
    },
    "stress_test": {
        "fn": _t_stress_test, "mutating": False,
        "description": "Use for 'what if the market crashes' on the user's actual book: replays 2008/COVID/rate-shock scenarios. Optional custom_drop_pct (e.g. -30).",
        "parameters": _p({"custom_drop_pct": {"type": "number", "description": "Optional custom market drop in percent, negative"}}),
    },
    "portfolio_risk": {
        "fn": _t_portfolio_risk, "mutating": False,
        "description": "Use for 'how risky is my portfolio' or 'which holding is riskiest': per-holding volatility, Sharpe, max drawdown and VaR over a period. Cached and cheap.",
        "parameters": _p({"period": {"type": "string", "enum": ["3mo", "6mo", "1y", "2y"], "description": "Lookback period (default 1y)"}}),
    },
    "portfolio_correlation": {
        "fn": _t_portfolio_correlation, "mutating": False,
        "description": "Use for diversification questions ('am I diversified', 'do my holdings move together'): average pairwise correlation plus the most and least correlated pairs. Cached and cheap.",
        "parameters": _p({"period": {"type": "string", "enum": ["1mo", "3mo", "6mo", "1y"], "description": "Lookback period (default 3mo)"}}),
    },
    "get_dividends": {
        "fn": _t_dividends, "mutating": False,
        "description": "Use for income/dividend questions on a symbol: yield, annual rate, ex/pay dates, payout frequency, 5y growth, recent payments.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "transactions_summary": {
        "fn": _t_transactions_summary, "mutating": False,
        "description": "Use for 'what have I bought/sold' or trade-history questions: buy/sell counts, dollars in and out, most recent transactions. Optional symbol filter. Local DB, instant.",
        "parameters": _p({"symbol": {"type": "string", "description": "Optional ticker to filter by"}}),
    },
    "liquidity_stress": {
        "fn": _t_liquidity_stress, "mutating": False,
        "description": "Use for 'is the market stressed / should I de-risk': market-wide liquidity stress composite (0-100) with regime, six sub-indicators, and a position-sizing multiplier. Cached.",
        "parameters": _p({}),
    },
    "congress_trades": {
        "fn": _t_congress_trades, "mutating": False,
        "description": "Congressional trades in one symbol. EXPENSIVE: parses many PDF filings and can take over a minute on a cold cache — call ONLY when the user explicitly asks about Congress/politician trading in a specific name; otherwise use smart_money_score.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "reflexivity": {
        "fn": _t_reflexivity, "mutating": False,
        "description": "Use for squeeze or spiral risk questions ('is X a short squeeze', 'dilution risk'): scans 4 self-reinforcing feedback loops (short squeeze, dilution spiral, index inclusion, equity feedback).",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "historical_analogs": {
        "fn": _t_historical_analogs, "mutating": False,
        "description": "Use for 'what happened after setups like this': finds past dates when the symbol's RSI/volatility/trend matched today and reports forward 30-day return stats.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "benchmark_compare": {
        "fn": _t_benchmark_compare, "mutating": False,
        "description": "Use for 'am I beating the market': the portfolio's return from saved daily snapshots vs a benchmark (default SPY) over a period, with the excess return.",
        "parameters": _p({"benchmark": {"type": "string", "description": "Benchmark ticker (default SPY)"},
                          "period": {"type": "string", "enum": ["1mo", "3mo", "6mo", "1y", "2y"], "description": "Comparison period (default 1y)"}}),
    },
    "list_memories": {
        "fn": lambda args: __import__("database").jarvis_list_memories(),
        "mutating": False,
        "description": "Use when asked what Jarvis remembers about the user: durable cross-session facts.",
        "parameters": _p({}),
    },
    # ── mutating: proposal-only from the agent loop ──────────────────────────
    "remember_fact": {
        "fn": lambda args: {"status": "remembered",
                            "id": __import__("database").jarvis_add_memory(
                                str(args.get("fact") or ""), source="agent"),
                            "label": "Remember: {}".format(str(args.get("fact") or "")[:500])},
        "mutating": True,
        "description": "Store a durable fact about the user (preferences, constraints). The user must confirm before this is saved.",
        "parameters": _p({"fact": {"type": "string", "description": "One concise fact, e.g. 'User is risk-averse'"}}, ["fact"]),
    },
    "add_price_alert": {
        "fn": _t_add_alert, "mutating": True,
        "description": "Create a price alert. The user must confirm before this executes.",
        "parameters": _p(dict(_SYM_PROP,
                              alert_type={"type": "string", "enum": ["above", "below"]},
                              price={"type": "number"}),
                         ["symbol", "alert_type", "price"]),
    },
    "add_to_watchlist": {
        "fn": _t_add_watchlist, "mutating": True,
        "description": "Add a symbol to the user's watchlist. The user must confirm before this executes.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
}


def openai_schemas() -> List[Dict[str, Any]]:
    return [{"type": "function",
             "function": {"name": name,
                          "description": spec["description"],
                          "parameters": spec["parameters"]}}
            for name, spec in TOOLS.items()]


def is_mutating(name: str) -> bool:
    spec = TOOLS.get(name)
    return bool(spec and spec["mutating"])


def proposal_label(name: str, args: Dict[str, Any]) -> str:
    if name == "remember_fact":
        return "Remember: {}".format(str(args.get("fact", ""))[:500])
    if name == "add_price_alert":
        return "Set alert: {} {} ${}".format(
            str(args.get("symbol", "")).upper(), args.get("alert_type"), args.get("price"))
    if name == "add_to_watchlist":
        return "Add {} to watchlist".format(str(args.get("symbol", "")).upper())
    return "{} {}".format(name, json.dumps(args))


def execute_read(name: str, args: Dict[str, Any]) -> str:
    """Run a READ tool; return a JSON string within the char budget."""
    spec = TOOLS.get(name)
    if not spec:
        return json.dumps({"error": "unknown tool"})
    if spec["mutating"]:
        return json.dumps({"error": "tool requires user confirmation"})
    try:
        result = spec["fn"](args or {})
    except Exception as e:
        log.debug("tool %s failed: %s", name, e)
        return json.dumps({"error": str(e)[:200]})
    s = json.dumps(result, default=str)
    if len(s) > _RESULT_CHAR_BUDGET:
        s = s[:_RESULT_CHAR_BUDGET] + '..."(truncated)"}'
    return s


def execute_mutating(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Run a MUTATING tool after explicit user confirmation. Whitelisted on
    this registry — unknown or read tools are rejected."""
    spec = TOOLS.get(name)
    if not spec or not spec["mutating"]:
        return {"error": "not a confirmable action"}
    try:
        return spec["fn"](args or {})
    except Exception as e:
        return {"error": str(e)[:200]}
