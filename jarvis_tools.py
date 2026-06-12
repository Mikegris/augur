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
# At most ONE interior separator (BRK.B, RDS-A are valid; "A.", "X-",
# "A.B.C.D" are not). Total length capped at ~10.
_SYM_RE = re.compile(r"^[A-Za-z0-9]{1,9}(?:[.\-][A-Za-z0-9]{1,4})?$")


def _sym(args: Dict[str, Any]) -> str:
    s = str(args.get("symbol") or "").strip().upper()
    if not _SYM_RE.match(s):
        raise ValueError("invalid symbol")
    return s


def _num_arg(v: Any) -> Optional[float]:
    """Coerce a model-supplied number that may arrive as '5000', '-30%', etc.
    Returns None if it isn't a finite number."""
    if v is None:
        return None
    try:
        f = float(str(v).strip().replace("%", "").replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


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
    q = fetcher.get_quote(_sym(args)) or {}
    keep = ("symbol", "price", "change", "change_pct", "prev_close", "volume",
            "market_cap", "fifty_two_week_high", "fifty_two_week_low")
    if not q.get("price"):
        return {"note": "no quote available for {}".format(_sym(args))}
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


def _t_portfolio_attribution(args):
    import jarvis
    return jarvis.portfolio_attribution() or {
        "note": "no priced positions with day-change data right now"}


def _t_away_digest(args):
    import jarvis
    d = jarvis.away_digest(mark_seen=False)
    return {"since": d.get("since"), "away_minutes": d.get("away_minutes"),
            "count": d.get("count"),
            "insights": [{"title": r.get("title"), "kind": r.get("kind"),
                          "detail": (r.get("detail") or "")[:150]}
                         for r in (d.get("insights") or [])[:8]]}


def _t_recent_transactions(args):
    import database as db
    n = _num_arg(args.get("limit"))
    n = int(max(1, min(n, 25))) if n else 10
    txns = db.get_transactions(limit=n)
    if not txns:
        return {"note": "no transactions on record"}
    return {"transactions": [
        {"date": t.get("date"), "symbol": t.get("symbol"),
         "action": t.get("action"), "shares": t.get("shares"),
         "price": t.get("price"), "total": t.get("total")} for t in txns]}


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
    # The model sometimes sends a comma-string instead of a list.
    if isinstance(syms, str):
        syms = [s for s in re.split(r"[,\s]+", syms) if s]
    if isinstance(syms, list) and syms:
        # Skip invalid entries rather than aborting the whole batch.
        clean = []
        for s in syms[:10]:
            try:
                clean.append(_sym({"symbol": s}))
            except ValueError:
                continue
        syms = clean or [h["symbol"] for h in db.get_portfolio()
                         if h.get("asset_type") != "crypto"][:25]
    else:
        syms = [h["symbol"] for h in db.get_portfolio()
                if h.get("asset_type") != "crypto"][:25]
    return earnings.get_earnings_calendar(syms)


def _t_smart_money(args):
    import smart_money
    return smart_money.compute_score(_sym(args)) or {"note": "no smart-money data"}


def _t_gex(args):
    import gex_engine
    return gex_engine.get_gex_summary(_sym(args)) or {"note": "no GEX data"}


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
    return narrative_engine.analyze_narrative(_sym(args)) or {"note": "no narrative data"}


def _t_top_ideas(args):
    import idea_pool_warmer
    return idea_pool_warmer.list_warmed_symbols()[:10]


def _t_stress_test(args):
    import database as db
    import fetcher
    drop = _num_arg(args.get("custom_drop_pct"))  # tolerates "-30%" / "30"
    if drop is not None:
        # The model often sends 30 for "a 30% drop" — a positive magnitude.
        # Treat any positive value as the loss size rather than clamping it
        # to a no-op 0%.
        if drop > 0:
            drop = -drop
        drop = max(-95.0, min(-0.1, drop))
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
    # Skip non-numeric totals rather than aborting the whole summary.
    _tot = lambda rs: round(sum((_num_arg(r.get("total")) or 0) for r in rs), 2)
    return {"symbol_filter": sym, "count": len(rows),
            "buys": len(buys), "sells": len(sells),
            "bought_usd": _tot(buys),
            "sold_usd": _tot(sells),
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


def _t_what_if(args):
    import synth_whatif
    import database as db
    import fetcher
    sym = _sym(args)
    action = str(args.get("action") or "").lower()
    if action not in ("add", "remove", "resize_to"):
        raise ValueError("action must be add, remove or resize_to")
    mv = 0.0
    note = None
    if action == "remove":
        if args.get("market_value"):
            note = "market_value ignored for a full remove"
    else:
        mv = _num_arg(args.get("market_value")) or 0
        if not (0 < mv < 100_000_000):
            raise ValueError("market_value required for add/resize_to")
    holdings = db.get_portfolio()
    if not holdings:
        return {"note": "no positions to simulate against"}
    prices = fetcher.get_quotes_batch([h["symbol"] for h in holdings])
    cur = []
    for h in holdings:
        p = (prices.get(h["symbol"]) or {}).get("price")
        cur.append({"symbol": h["symbol"],
                    "market_value": round((p if p else h["avg_cost"]) * h["shares"], 2),
                    "shares": h["shares"], "avg_cost": h["avg_cost"],
                    "asset_type": h.get("asset_type")})
    r = synth_whatif.whatif(cur, {"symbol": sym, "action": action, "market_value": mv})
    # Condense to the actual whatif shape: delta (risk/return shifts) +
    # optimizer verdict. The old code copied keys that don't exist, so the
    # tool returned nothing but an echo of its inputs.
    out = {"symbol": sym, "action": action, "market_value": mv}
    if note:
        out["note"] = note
    delta = r.get("delta")
    if isinstance(delta, dict):
        out["delta"] = {k: delta.get(k) for k in (
            "expected_return_pct", "expected_vol_pct", "sharpe_ratio_delta",
            "concentration_hhi", "ml_forecast_weighted_return_delta_pct", "nav_delta")
            if delta.get(k) is not None}
        shifts = delta.get("scenario_loss_shifts")
        if isinstance(shifts, (dict, list)) and len(str(shifts)) < 400:
            out["delta"]["scenario_loss_shifts"] = shifts
    opt = r.get("optimizer_says")
    if isinstance(opt, dict):
        out["optimizer"] = {k: opt.get(k) for k in (
            "verdict", "current_optimal_size_pct", "your_proposed_size_pct",
            "optimizer_sharpe") if opt.get(k) is not None}
    mc = r.get("monte_carlo_delta")
    if isinstance(mc, dict) and len(str(mc)) < 400:
        out["monte_carlo_delta"] = mc
    if r.get("errors"):
        out["partial"] = True
    return out


def _t_compare(args):
    import jarvis_lens
    a = _sym({"symbol": args.get("symbol_a")})
    b = _sym({"symbol": args.get("symbol_b")})

    def slim(r):
        return {"symbol": r.get("symbol"),
                "name": (r.get("business") or {}).get("name"),
                "sector": (r.get("business") or {}).get("sector"),
                "quality": r.get("quality"),
                "valuation": {k: (r.get("valuation") or {}).get(k) for k in ("tone", "pe", "peg")},
                "ownership": {k: (r.get("ownership") or {}).get(k)
                              for k in ("held", "unrealized_pct", "holding_days")},
                "take": r.get("take")}
    return {"a": slim(jarvis_lens.position_review(a)),
            "b": slim(jarvis_lens.position_review(b))}


def _t_price_history(args):
    """Condensed window stats — 'how did X do this month' wants four numbers,
    not 60 OHLCV bars burning the result budget."""
    import fetcher
    import time as _t
    from datetime import datetime as _dt
    sym = _sym(args)
    n = _num_arg(args.get("days"))
    days = int(max(5, min(n, 365))) if n else 30
    # Fetch the smallest cached period that covers the window — get_chart_data
    # is the same heavily-cached path the /api/chart routes ride.
    period = ("1mo" if days <= 31 else
              "3mo" if days <= 93 else
              "6mo" if days <= 186 else "1y")
    bars = [b for b in (fetcher.get_chart_data(sym, period=period, interval="1d") or [])
            if b.get("close")]
    cutoff = _t.time() - days * 86400
    window = [b for b in bars if (b.get("time") or 0) >= cutoff] or bars
    if len(window) < 2:
        return {"note": "no daily history available for {}".format(sym)}
    start, end = window[0]["close"], window[-1]["close"]
    lows = [(b.get("low") or b["close"]) for b in window]
    highs = [(b.get("high") or b["close"]) for b in window]
    _day = lambda b: _dt.utcfromtimestamp(int(b.get("time") or 0)).strftime("%Y-%m-%d")
    return {"symbol": sym, "days": days, "bars": len(window),
            "start_date": _day(window[0]), "end_date": _day(window[-1]),
            "start": round(start, 2), "end": round(end, 2),
            "low": round(min(lows), 2), "high": round(max(highs), 2),
            "return_pct": round((end / start - 1) * 100, 2) if start else None}


# The headline macro series — FRED ids → prompt-friendly labels. CPI is an
# index level, so the YoY change (the number people mean by "inflation") is
# computed alongside it.
_FRED_HEADLINE = (
    ("CPIAUCSL", "cpi"),
    ("UNRATE", "unemployment_rate_pct"),
    ("FEDFUNDS", "fed_funds_rate_pct"),
    ("DFII10", "ten_year_real_yield_pct"),
    ("T10Y2Y", "yield_curve_10y_minus_2y_pct"),
)


def _t_macro_snapshot(args):
    try:
        import fred_data
    except Exception as e:
        return {"note": "macro data unavailable: {}".format(str(e)[:100])}
    out: Dict[str, Any] = {}
    for sid, label in _FRED_HEADLINE:
        # Per-series guard: one dead series must not blank the snapshot.
        try:
            s = fred_data.fetch_series(sid, observations=13)
            pts = s.get("points") or []
            if not pts:
                continue
            entry: Dict[str, Any] = {"value": pts[-1]["value"], "date": pts[-1]["date"]}
            if sid == "CPIAUCSL" and len(pts) >= 13 and pts[-13].get("value"):
                entry["yoy_pct"] = round(
                    (pts[-1]["value"] / pts[-13]["value"] - 1) * 100, 2)
            out[label] = entry
        except Exception:
            continue
    if not out:
        return {"note": "macro feed unavailable right now"}
    return out


def _t_crypto_market(args):
    import fetcher
    g = fetcher.get_crypto_global() or {}
    mcap = _num_arg(g.get("total_market_cap_usd"))
    if mcap is None:
        return {"note": "crypto global data unavailable right now"}
    rnd = lambda v, p=2: round(v, p) if _num_arg(v) is not None else None
    return {"total_market_cap_usd": round(mcap),
            "total_market_cap_t": round(mcap / 1e12, 2),
            "btc_dominance_pct": rnd(g.get("btc_dominance"), 1),
            "eth_dominance_pct": rnd(g.get("eth_dominance"), 1),
            "market_cap_change_24h_pct": rnd(g.get("market_cap_change_24h")),
            "total_volume_usd": rnd(g.get("total_volume_usd"), 0),
            "active_coins": g.get("active_coins")}


def _t_factor_exposure(args):
    try:
        import research_factors
    except Exception as e:
        return {"note": "factor engine unavailable: {}".format(str(e)[:100])}
    if args.get("symbol"):
        r = research_factors.factor_exposure(_sym(args)) or {}
    else:
        import database as db
        import fetcher
        holdings = [h for h in db.get_portfolio()
                    if h.get("asset_type") != "crypto"]
        if not holdings:
            return {"note": "no stock positions to decompose"}
        prices = fetcher.get_quotes_batch([h["symbol"] for h in holdings])
        hl = []
        for h in holdings:
            p = (prices.get(h["symbol"]) or {}).get("price")
            hl.append({"symbol": h["symbol"],
                       "market_value": (p if p else h["avg_cost"]) * h["shares"]})
        # Each holding costs a regression; cap at the 15 largest positions.
        hl = sorted(hl, key=lambda x: -x["market_value"])[:15]
        r = research_factors.portfolio_factor_exposure(hl) or {}
    if r.get("error"):
        return {"error": str(r["error"])[:200]}
    factors = {}
    for name, v in (r.get("exposures") or {}).items():
        factors[name] = v.get("beta") if isinstance(v, dict) else v
    out = {"factors": factors, "r_squared": r.get("r_squared"),
           "alpha_annual_pct": r.get("alpha_annual_pct"),
           "period_years": r.get("period_years"),
           "n_obs": r.get("n_obs"), "as_of": r.get("as_of"),
           "idiosyncratic_vol_pct": r.get("idiosyncratic_vol_pct")}
    if r.get("symbol"):
        out["symbol"] = r["symbol"]
    if r.get("n_holdings"):
        out["scope"] = "portfolio"
        out["n_holdings"] = r["n_holdings"]
    return out


_FORM_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-/ ]{0,11}$")


def _t_filing_summary(args):
    sym = _sym(args)
    form = str(args.get("form_type") or "").strip().upper()
    if form and not _FORM_RE.match(form):
        raise ValueError("invalid form_type")
    try:
        import sec_edgar
    except Exception as e:
        return {"note": "SEC filings unavailable: {}".format(str(e)[:100])}
    filings = sec_edgar.get_recent_filings(
        sym, forms=[form] if form else None, limit=1) or []
    if not filings:
        return {"note": "no recent {} filings found for {}".format(
            form or "SEC", sym)}
    f = filings[0]
    out = {"symbol": sym, "form": f.get("form_type"),
           "filing_date": f.get("filing_date"),
           "description": f.get("description")}
    cached = None
    try:
        import database as db
        cached = db.get_cached_filing(f.get("accession"))
    except Exception:
        pass
    if cached and (cached.get("ai_summary") or cached.get("ai_signal")):
        out["signal"] = cached.get("ai_signal")
        out["summary"] = cached.get("ai_summary")
        out["key_points"] = (cached.get("ai_key_points") or [])[:4]
        return out
    # Same summarizer the intel feed rides (rule-based when no API key);
    # read-only here — no db.cache_filing, the feed owns the cache.
    try:
        import ai_summarizer
        ai = ai_summarizer.summarize_filing(
            "", f.get("form_type") or "", sym, f.get("description") or "") or {}
        out["signal"] = ai.get("signal")
        out["summary"] = ai.get("summary")
        out["key_points"] = (ai.get("key_points") or [])[:4]
    except Exception as e:
        out["note"] = "summary unavailable: {}".format(str(e)[:100])
    return out


def _t_track_record(args):
    try:
        import forecast_accountability
    except Exception as e:
        return {"note": "accountability ledger unavailable: {}".format(str(e)[:100])}
    rep = forecast_accountability.accountability_report() or {}
    ens = rep.get("ensemble") or {}
    tr = ens.get("track_record") or {}
    cal = ens.get("calibration") or {}
    out = {"ensemble": {"n_scored": tr.get("n"),
                        "n_directional": tr.get("n_directional"),
                        "hit_rate": tr.get("hit_rate"),
                        "avg_return": tr.get("avg_return")},
           "weights_adapted": rep.get("weights_adapted")}
    if cal.get("brier") is not None:
        out["ensemble"]["brier"] = cal.get("brier")
        out["ensemble"]["brier_skill"] = cal.get("brier_skill")
    board = rep.get("leaderboard") or []
    if board:
        b = board[0]
        out["top_component"] = {"key": b.get("key"), "n": b.get("n"),
                                "hit_rate": b.get("hit_rate"),
                                "brier": b.get("brier")}
    if not tr.get("n") and not board:
        return {"note": "no scored forecasts yet — the ledger needs "
                        "forecasts old enough to grade"}
    return out


def _t_counterfactual_review(args):
    try:
        import jarvis_counterfactual
    except Exception:
        return {"note": "counterfactual engine unavailable"}
    sym = _sym(args) if args.get("symbol") else None
    n = _num_arg(args.get("limit"))
    limit = int(max(1, min(n, 10))) if n else 10
    r = jarvis_counterfactual.analyze(symbol=sym, limit=limit) or {}
    if r.get("error"):
        return {"error": str(r["error"])[:200]}
    if not r.get("n_sells"):
        return {"note": r.get("summary") or "no sells to review"}
    return {"n_sells": r.get("n_sells"), "net_usd": r.get("net_usd"),
            "summary": r.get("summary"),
            "sells": (r.get("sells") or [])[:5]}


def _t_check_policy(args):
    try:
        import jarvis_policy
    except Exception:
        return {"note": "policy engine unavailable"}
    out: Dict[str, Any] = {}
    try:
        chk = jarvis_policy.check_portfolio() or {}
        vio = chk.get("violations") or []
        out["n_rules"] = chk.get("n")
        out["n_violations"] = len(vio)
        out["violations"] = [
            {"kind": v.get("kind"), "severity": v.get("severity"),
             "detail": str(v.get("detail") or "")[:150]}
            for v in vio[:6] if isinstance(v, dict)]
    except Exception as e:
        out["error"] = str(e)[:200]
    try:
        desc = jarvis_policy.describe_rules()
        out["rules"] = desc if isinstance(desc, list) else str(desc)[:600]
    except Exception:
        pass
    if not out:
        return {"note": "policy engine returned nothing"}
    return out


def _t_get_watches(args):
    try:
        import jarvis_watches
    except Exception:
        return {"note": "watch engine unavailable"}
    ws = jarvis_watches.list_watches() or []
    if not ws:
        return {"note": "no watches set — offer add_watch to create one"}
    return {"count": len(ws),
            "watches": [{"id": w.get("id"), "name": w.get("name"),
                         "description": w.get("description"),
                         "armed": w.get("armed"),
                         "triggered_at": w.get("triggered_at")}
                        for w in ws[:15]]}


def _t_recall_memory(args):
    q = str(args.get("query") or "").strip()
    if not q:
        raise ValueError("query required")
    try:
        import jarvis_semmem
        hits = jarvis_semmem.recall(q, k=5) or []
    except Exception:
        return {"note": "semantic memory unavailable"}
    if not hits:
        return {"note": "no related memories found"}
    return {"matches": [{"text": str(h.get("text") or "")[:200],
                         "score": h.get("score")}
                        for h in hits[:5] if isinstance(h, dict)]}


# ─── mutating-tool implementations (only via execute_mutating) ───────────────

def _t_remember(args):
    import database as db
    fact = str(args.get("fact") or "").strip()
    if not fact:
        return {"error": "no fact to remember"}
    mid = db.jarvis_add_memory(fact, source="agent")
    if mid is None:
        return {"error": "could not store that"}
    return {"status": "remembered", "id": mid, "label": "Remember: {}".format(fact[:500])}


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


# Watch condition vocabulary — mirrors jarvis_watches, but kept local so
# proposal validation/labels still work when that module can't import.
_WATCH_METRICS = ("price", "change_pct", "vix", "book_day_pct")
_WATCH_SYMBOL_METRICS = ("price", "change_pct")
_WATCH_OPS = ("gt", "lt")


def _watch_label(args: Dict[str, Any]) -> str:
    """'Create watch "NVDA breakout": NVDA price > $200 AND VIX > 25' —
    prefers jarvis_watches.describe but must render without it."""
    name = str(args.get("name") or "").strip()
    conds = args.get("conditions") if isinstance(args.get("conditions"), list) else []
    desc = ""
    try:
        import jarvis_watches
        desc = jarvis_watches.describe({"conditions": conds}) or ""
    except Exception:
        desc = ""
    if not desc:
        parts = []
        for c in conds:
            if not isinstance(c, dict):
                continue
            op = ">" if c.get("op") == "gt" else "<"
            bits = [str(x) for x in (c.get("symbol"), c.get("metric"),
                                     op, c.get("value")) if x is not None]
            parts.append(" ".join(bits))
        desc = " AND ".join(parts)
    if desc:
        return 'Create watch "{}": {}'.format(name, desc)
    return 'Create watch "{}"'.format(name)


def _t_add_watch(args):
    try:
        import jarvis_watches
    except Exception:
        return {"error": "watch engine unavailable"}
    r = jarvis_watches.add_watch(args.get("name"), args.get("conditions")) or {}
    if r.get("error"):
        return {"error": str(r["error"])[:200]}
    return {"status": "created", "id": r.get("id"), "label": _watch_label(args)}


def _policy_kinds() -> Optional[tuple]:
    """Valid policy-rule kinds when jarvis_policy exposes them; None when
    unknown (module missing or no constant) — callers then defer to
    set_rule's own validation at execution time."""
    try:
        import jarvis_policy
    except Exception:
        return None
    for attr in ("KINDS", "RULE_KINDS", "POLICY_KINDS", "VALID_KINDS", "_KINDS"):
        v = getattr(jarvis_policy, attr, None)
        if isinstance(v, (list, tuple, set, frozenset, dict)) and v:
            return tuple(v)
    return None


def _t_set_policy_rule(args):
    try:
        import jarvis_policy
    except Exception:
        return {"error": "policy engine unavailable"}
    kind = str(args.get("kind") or "").strip()
    value = _num_arg(args.get("value"))
    if not kind:
        # Natural-language variant — resolved HERE, at execution time.
        text = str(args.get("text") or "").strip()
        rule = jarvis_policy.parse_rule(text) if text else None
        if not isinstance(rule, dict):
            return {"error": "could not parse a policy rule from: {}".format(
                text[:140] or "(empty)")}
        kind = str(rule.get("kind") or "").strip()
        value = _num_arg(rule.get("value"))
    if not kind or value is None:
        return {"error": "a policy rule needs a kind and a numeric value"}
    r = jarvis_policy.set_rule(kind, value)
    if not isinstance(r, dict):
        r = {"ok": bool(r)}
    if r.get("error") or not r.get("ok"):
        return {"error": str(r.get("error") or "could not set rule")[:200]}
    return {"status": "set",
            "label": "Set policy rule: {} = {:g}".format(kind, value)}


# ─── registry ────────────────────────────────────────────────────────────────

def _p(props: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}

_SYM_PROP = {"symbol": {"type": "string", "description": "Ticker symbol, e.g. NVDA"}}

TOOLS: Dict[str, Dict[str, Any]] = {
    # ── OPEN-WORLD research (no ticker / portfolio needed) ──────────────────
    "web_research": {
        "fn": lambda args: __import__("jarvis_research").web_research(str(args.get("query") or "")),
        "mutating": False,
        "description": "Use for ANY question your other tools can't answer from local data: private companies (SpaceX, Stripe, OpenAI), upcoming IPOs, Fed/macro events, breaking news, regulations, themes, 'what's happening with X', or anything requiring CURRENT outside information. Returns a researched answer with source citations. This is your catch-all — reach for it instead of saying you can't help.",
        "parameters": _p({"query": {"type": "string", "description": "The research question, in plain English"}}, ["query"]),
    },
    "search_news": {
        "fn": lambda args: __import__("jarvis_research").search_news(
            str(args.get("query") or ""), int(args.get("days") or 14)),
        "mutating": False,
        "description": "Use for recent NEWS on any topic, company (public OR private), person, or theme — no ticker required. Returns headlines with sources and dates.",
        "parameters": _p({"query": {"type": "string"},
                          "days": {"type": "integer", "description": "Lookback days (default 14)"}}, ["query"]),
    },
    "news_sentiment": {
        "fn": lambda args: __import__("jarvis_research").news_sentiment(str(args.get("query") or "")),
        "mutating": False,
        "description": "Use for 'how is sentiment / coverage trending on X': average news tone and improving/deteriorating trend for any topic.",
        "parameters": _p({"query": {"type": "string"}}, ["query"]),
    },
    "search_sec_filings": {
        "fn": lambda args: __import__("jarvis_research").search_sec_filings(
            str(args.get("query") or ""), args.get("forms")),
        "mutating": False,
        "description": "Use to find SEC filings for ANY company by NAME (not ticker) — S-1/IPO prospectuses, 10-Ks, 8-Ks. Works for newly-filing or pre-IPO companies. Optional forms filter (e.g. 'S-1').",
        "parameters": _p({"query": {"type": "string", "description": "Company name or phrase"},
                          "forms": {"type": "string", "description": "Optional form type, e.g. S-1"}}, ["query"]),
    },
    "search_hacker_news": {
        "fn": lambda args: __import__("jarvis_research").search_hacker_news(str(args.get("query") or "")),
        "mutating": False,
        "description": "Use for tech/startup/crypto buzz and retail chatter on a topic — Hacker News discussion with points and comment counts.",
        "parameters": _p({"query": {"type": "string"}}, ["query"]),
    },
    "get_quote": {
        "fn": _t_get_quote, "mutating": False,
        "description": "Use for 'price of X' / 'how is X doing today': live quote with price, day change, volume, 52-week range.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "portfolio_attribution": {
        "fn": _t_portfolio_attribution, "mutating": False,
        "description": "Use for 'why am I up/down today' or 'what's driving my portfolio': per-holding contribution to TODAY's P&L — top contributors, top detractors, each position's share of the net move, plus S&P context to separate tape from positioning.",
        "parameters": _p({}),
    },
    "get_portfolio": {
        "fn": _t_get_portfolio, "mutating": False,
        "description": "Use FIRST for any question about what the user owns, P&L, or weights: total value, day move, top 10 positions with weight and day change.",
        "parameters": _p({}),
    },
    "get_away_digest": {
        "fn": _t_away_digest, "mutating": False,
        "description": "Use for 'what did I miss' / 'what happened while I was away': the insight history since the user's last visit — triggered alerts, big holding moves, regime changes, 52-week extremes.",
        "parameters": _p({}),
    },
    "recent_transactions": {
        "fn": _t_recent_transactions, "mutating": False,
        "description": "Use for 'what did I buy/sell recently' or any question about the user's own trading behavior: most recent fills with date, action, shares, and price.",
        "parameters": _p({"limit": {"type": "integer", "description": "How many fills (default 10, max 25)"}}),
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
    "get_price_history": {
        "fn": _t_price_history, "mutating": False,
        "description": "Use for 'how did X do this month/quarter/year': condensed price window for a symbol — start, end, low, high, and total return over the last N days. Cheaper and more direct than reasoning from a quote's 52-week range.",
        "parameters": _p(dict(_SYM_PROP, days={"type": "integer", "description": "Lookback window in calendar days (5-365, default 30)"}), ["symbol"]),
    },
    "get_macro_snapshot": {
        "fn": _t_macro_snapshot, "mutating": False,
        "description": "Use for 'where is inflation / unemployment / rates': latest cached FRED readings — CPI (with YoY inflation), unemployment, fed funds, 10Y real yield, 10Y-2Y curve — each with its observation date.",
        "parameters": _p({}),
    },
    "get_crypto_market": {
        "fn": _t_crypto_market, "mutating": False,
        "description": "Use for 'how is crypto doing overall': global crypto market overview — total market cap, 24h change, BTC/ETH dominance, total volume. Market-wide, not a single coin.",
        "parameters": _p({}),
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
    "what_if": {
        "fn": lambda args: _t_what_if(args),
        "mutating": False,
        "description": "Use for 'what if I add/trim/sell X' BEFORE any trade: simulates the portfolio impact (risk, concentration, factor tilts, stress) of adding, removing, or resizing a position. Purely hypothetical — changes nothing.",
        "parameters": _p(dict(_SYM_PROP,
                              action={"type": "string", "enum": ["add", "remove", "resize_to"],
                                      "description": "add = buy more, remove = sell all, resize_to = set position to market_value"},
                              market_value={"type": "number",
                                            "description": "Dollar amount for add/resize_to (ignored for remove)"}),
                         ["symbol", "action"]),
    },
    "compare_positions": {
        "fn": lambda args: _t_compare(args),
        "mutating": False,
        "description": "Use for 'compare X vs Y / which is the better business': side-by-side Buffett-lens reviews — quality scores, valuation tones, and the user's ownership of each.",
        "parameters": _p({"symbol_a": {"type": "string"}, "symbol_b": {"type": "string"}},
                         ["symbol_a", "symbol_b"]),
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
    "factor_exposure": {
        "fn": _t_factor_exposure, "mutating": False,
        "description": "Use for 'what's really driving my returns / is X a value or momentum bet': Fama-French 5 + momentum factor loadings (beta per factor) with alpha and R-squared. Give a symbol for one stock; omit it to decompose the whole portfolio.",
        "parameters": _p({"symbol": {"type": "string", "description": "Optional ticker — omit for the whole portfolio"}}),
    },
    "filing_summary": {
        "fn": _t_filing_summary, "mutating": False,
        "description": "Use for 'what did X just file / summarize the latest 10-K': the symbol's most recent SEC filing with date, form, analyst-style summary, signal (BULLISH/BEARISH/NEUTRAL/MATERIAL) and key points. Optional form_type filter (8-K, 10-K, 10-Q, S-1).",
        "parameters": _p(dict(_SYM_PROP, form_type={"type": "string", "description": "Optional form filter, e.g. 10-K"}), ["symbol"]),
    },
    "get_track_record": {
        "fn": _t_track_record, "mutating": False,
        "description": "Use for 'how accurate are your forecasts / should I trust the model': the ensemble's scored hit rate and sample size, Brier calibration, and which component signal has earned the most skill. Cite this before leaning on a forecast.",
        "parameters": _p({}),
    },
    "counterfactual_review": {
        "fn": _t_counterfactual_review, "mutating": False,
        "description": "Use for 'was selling X a mistake / how have my sells aged': replays past sells against today's prices — what each exited position would be worth now, the net dollar impact, and a verdict. Optional symbol filter.",
        "parameters": _p({"symbol": {"type": "string", "description": "Optional ticker to review sells of"},
                          "limit": {"type": "integer", "description": "How many sells to analyze (max 10)"}}),
    },
    "check_policy": {
        "fn": _t_check_policy, "mutating": False,
        "description": "Use for 'am I breaking my own rules / hold me accountable': checks the live portfolio against the user's standing policy rules and lists violations with severity. Run this before endorsing a new buy.",
        "parameters": _p({}),
    },
    "get_watches": {
        "fn": _t_get_watches, "mutating": False,
        "description": "Use when asked what conditional watches are standing ('what are you watching for me'): each watch with its trigger conditions, whether it's still armed, and when it fired. Read-only; use add_watch to create one.",
        "parameters": _p({}),
    },
    "recall_memory": {
        "fn": _t_recall_memory, "mutating": False,
        "description": "Use for 'what did we discuss about X / didn't I mention Y once': semantic search over Jarvis's long-term memory — past conversations, notes and facts ranked by relevance. Broader than list_memories, which only lists saved facts.",
        "parameters": _p({"query": {"type": "string", "description": "What to look for, in plain English"}}, ["query"]),
    },
    # ── mutating: proposal-only from the agent loop ──────────────────────────
    "remember_fact": {
        "fn": lambda args: _t_remember(args),
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
    "add_watch": {
        "fn": _t_add_watch, "mutating": True,
        "description": "Create a conditional watch that fires when ALL its conditions hold at once — e.g. 'NVDA price > 200 AND VIX > 25'. 1-4 conditions; metrics: price and change_pct (both need a symbol), vix, book_day_pct (portfolio day move). The user must confirm before this executes.",
        "parameters": _p({
            "name": {"type": "string", "description": "Short watch name, e.g. 'NVDA breakout'"},
            "conditions": {
                "type": "array",
                "description": "1-4 conditions, ALL must hold",
                "items": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string", "enum": ["price", "change_pct", "vix", "book_day_pct"]},
                        "symbol": {"type": "string", "description": "Ticker — required for price/change_pct"},
                        "op": {"type": "string", "enum": ["gt", "lt"]},
                        "value": {"type": "number"},
                    },
                    "required": ["metric", "op", "value"],
                },
            },
        }, ["name", "conditions"]),
    },
    "set_policy_rule": {
        "fn": _t_set_policy_rule, "mutating": True,
        "description": "Set or update one of the user's standing portfolio policy rules (the discipline check_policy enforces). Pass kind + numeric value, OR a natural-language text like 'max position 10%'. The user must confirm before this executes.",
        "parameters": _p({
            "kind": {"type": "string", "description": "Policy rule kind (use with value)"},
            "value": {"type": "number", "description": "Numeric threshold for the rule"},
            "text": {"type": "string", "description": "Alternative: the rule in plain English, e.g. 'max position 10%'"},
        }),
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


def valid_proposal_args(name: str, args: Dict[str, Any]) -> bool:
    """Whether a mutating proposal's args are well-formed enough to offer the
    user a CONFIRM. Malformed model args ('Set alert:  None $None') should be
    rejected before they ever reach the confirmation UI."""
    if not isinstance(args, dict):
        return False
    if name == "remember_fact":
        return bool(str(args.get("fact") or "").strip())
    if name == "add_price_alert":
        if str(args.get("alert_type") or "").lower() not in ("above", "below"):
            return False
        if not str(args.get("symbol") or "").strip():
            return False
        try:
            return float(args.get("price")) > 0
        except (TypeError, ValueError):
            return False
    if name == "add_to_watchlist":
        return bool(str(args.get("symbol") or "").strip())
    if name == "add_watch":
        if not str(args.get("name") or "").strip():
            return False
        conds = args.get("conditions")
        if not isinstance(conds, list) or not (1 <= len(conds) <= 4):
            return False
        for c in conds:
            if not isinstance(c, dict):
                return False
            if c.get("metric") not in _WATCH_METRICS:
                return False
            if c.get("op") not in _WATCH_OPS:
                return False
            if _num_arg(c.get("value")) is None:
                return False
            sym = str(c.get("symbol") or "").strip().upper()
            if c.get("metric") in _WATCH_SYMBOL_METRICS:
                if not sym or not _SYM_RE.match(sym):
                    return False
            elif sym and not _SYM_RE.match(sym):
                return False
        return True
    if name == "set_policy_rule":
        kind = str(args.get("kind") or "").strip()
        if not kind:
            # Text variant — parse_rule resolves it at EXECUTION time; a
            # non-empty phrase is enough to offer a confirm.
            return bool(str(args.get("text") or "").strip())
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]{1,39}$", kind):
            return False
        if _num_arg(args.get("value")) is None:
            return False
        kinds = _policy_kinds()
        if kinds and kind not in kinds:
            return False
        return True
    return True


def proposal_label(name: str, args: Dict[str, Any]) -> str:
    if name == "remember_fact":
        return "Remember: {}".format(str(args.get("fact", ""))[:500])
    if name == "add_price_alert":
        return "Set alert: {} {} ${}".format(
            str(args.get("symbol", "")).upper(), args.get("alert_type"), args.get("price"))
    if name == "add_to_watchlist":
        return "Add {} to watchlist".format(str(args.get("symbol", "")).upper())
    if name == "add_watch":
        return _watch_label(args if isinstance(args, dict) else {})
    if name == "set_policy_rule":
        kind = str(args.get("kind") or "").strip()
        if kind:
            v = _num_arg(args.get("value"))
            return "Set policy rule: {} = {}".format(
                kind, "{:g}".format(v) if v is not None else args.get("value"))
        return 'Set policy rule: "{}"'.format(
            str(args.get("text") or "").strip()[:140])
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
        # Keep VALID JSON, but DON'T drop whole keys — the substantive data
        # (per_symbol, flows, sectors) is usually the last-inserted key, so
        # popping keys sacrificed exactly what the model needs. Instead shrink
        # the largest LIST value (keeping every key), and only as a last
        # resort drop trailing list items.
        try:
            if isinstance(result, list):
                trimmed = list(result)
                while trimmed and len(json.dumps(trimmed, default=str)) > _RESULT_CHAR_BUDGET - 60:
                    trimmed.pop()
                return json.dumps({"items": trimmed, "truncated": True,
                                   "total": len(result)}, default=str)
            if isinstance(result, dict):
                trimmed = dict(result)
                trimmed["truncated"] = True
                # Repeatedly halve the longest list-valued field until it fits.
                for _ in range(40):
                    if len(json.dumps(trimmed, default=str)) <= _RESULT_CHAR_BUDGET:
                        break
                    longest_k, longest_len = None, 0
                    for k, v in trimmed.items():
                        if isinstance(v, list) and len(v) > longest_len:
                            longest_k, longest_len = k, len(v)
                    if longest_k is None or longest_len == 0:
                        break
                    trimmed[longest_k] = trimmed[longest_k][:max(1, longest_len // 2)]
                return json.dumps(trimmed, default=str)
        except Exception:
            pass
        return json.dumps({"truncated": True, "note": "result too large to inline"})
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


def execute_mutating_batch(actions: Any) -> Dict[str, Any]:
    """Run up to 10 confirmed mutating actions in order. Each item is
    {"tool": name, "args": {...}} and is individually re-checked against the
    whitelist (is_mutating + valid_proposal_args) before executing through
    execute_mutating — one malformed or failing item never stops the rest."""
    results: List[Dict[str, Any]] = []
    n_ok = 0
    items = actions if isinstance(actions, list) else []
    for item in items[:10]:
        if not isinstance(item, dict):
            results.append({"tool": None, "ok": False,
                            "error": "malformed action (expected an object)"})
            continue
        tool = str(item.get("tool") or "")
        targs = item.get("args") if isinstance(item.get("args"), dict) else {}
        if not is_mutating(tool):
            results.append({"tool": tool, "ok": False,
                            "error": "not a confirmable action"})
            continue
        if not valid_proposal_args(tool, targs):
            results.append({"tool": tool, "ok": False,
                            "error": "invalid arguments"})
            continue
        r = execute_mutating(tool, targs)
        if isinstance(r, dict) and not r.get("error"):
            results.append({"tool": tool, "ok": True,
                            "label": r.get("label") or proposal_label(tool, targs)})
            n_ok += 1
        else:
            results.append({"tool": tool, "ok": False,
                            "error": str((r or {}).get("error") or "failed")[:200]})
    if isinstance(actions, list) and len(actions) > 10:
        results.append({"tool": None, "ok": False,
                        "error": "batch capped at 10 actions; "
                                 "{} dropped".format(len(actions) - 10)})
    return {"results": results, "n_ok": n_ok}
