"""
Tests for jarvis_tools — the Jarvis agent's tool registry.

Pins the widened read-tool surface (portfolio risk/correlation, dividends,
transactions, liquidity, congress, reflexivity, historical analogs, benchmark
comparison) and the deepened condensers (forecast signals, portfolio
day-change, ranked sector flow). Every tool executes against mocked engine
seams: OFFLINE and deterministic — no network, and a throwaway DB so the real
wealth.db is untouched.

Run:  ./venv/bin/python test_jarvis_tools.py
Exit: 0 if all pass, 1 otherwise.
"""
import json
import os
import sys
import tempfile

# Isolate DB before importing app/tracker so we never touch the real wealth.db.
_TMPDB = tempfile.NamedTemporaryFile(prefix="augur_test_", suffix=".db", delete=False)
_TMPDB.close()
os.environ["AUGUR_DB_PATH"] = _TMPDB.name
os.environ["DISABLE_WARMER"] = "1"

import jarvis_tools as jt  # noqa: E402

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  ✓ " + name)
    else:
        _failed += 1
        print("  ✗ " + name + ("  — " + detail if detail else ""))


class _patched:
    """Temporarily set module attributes; always restores."""

    def __init__(self, *triples):
        self.triples = triples  # (module, attr_name, replacement)

    def __enter__(self):
        self.saved = [(m, n, getattr(m, n)) for m, n, _ in self.triples]
        for m, n, repl in self.triples:
            setattr(m, n, repl)

    def __exit__(self, *exc):
        for m, n, orig in self.saved:
            setattr(m, n, orig)


def _read(name, args=None):
    """execute_read + parse: every read tool must return valid JSON."""
    s = jt.execute_read(name, args or {})
    return json.loads(s), s


_NEW_TOOLS = [
    "portfolio_risk", "portfolio_correlation", "get_dividends",
    "transactions_summary", "liquidity_stress", "congress_trades",
    "reflexivity", "historical_analogs", "benchmark_compare",
]


# ── 1. Registry hygiene: schemas, mutability, routing descriptions ──────────
def test_registry_shape():
    schemas = jt.openai_schemas()
    check("every tool schema has valid shape",
          all(t["function"]["name"]
              and t["function"]["parameters"]["type"] == "object"
              for t in schemas))
    for t in schemas:
        fn = t["function"]
        props = fn["parameters"].get("properties", {})
        req = fn["parameters"].get("required", [])
        if not (isinstance(props, dict) and set(req) <= set(props)):
            check("schema %s: required ⊆ properties" % fn["name"], False, str(req))
            return
    check("schema: required ⊆ properties for all tools", True)
    check("descriptions are non-empty routing signals",
          all(len(t["function"]["description"]) >= 20 for t in schemas))
    check("all new tools registered", all(n in jt.TOOLS for n in _NEW_TOOLS),
          str([n for n in _NEW_TOOLS if n not in jt.TOOLS]))
    check("all new tools are read-only (not mutating)",
          all(not jt.TOOLS[n]["mutating"] for n in _NEW_TOOLS))
    desc = jt.TOOLS["congress_trades"]["description"]
    check("expensive congress tool flagged in description",
          "EXPENSIVE" in desc and ("cold cache" in desc or "minute" in desc), desc)
    check("mutating whitelist: execute_read refuses mutating tool",
          json.loads(jt.execute_read("add_price_alert", {}))["error"]
          == "tool requires user confirmation")
    check("mutating whitelist: execute_mutating refuses read tool",
          jt.execute_mutating("portfolio_risk", {}).get("error") is not None)
    check("mutating whitelist: unknown tool rejected",
          jt.execute_mutating("rm_rf", {}).get("error") is not None)


# ── 2. portfolio_risk → fetcher.get_risk_metrics ─────────────────────────────
def test_portfolio_risk():
    import database as db
    import fetcher
    holdings = [{"symbol": "AAPL", "asset_type": "stock"},
                {"symbol": "MSFT", "asset_type": "stock"},
                {"symbol": "BTC", "asset_type": "crypto"}]
    raw = {"AAPL": {"annualized_return": 20.0, "annualized_vol": 25.0,
                    "sharpe_ratio": 1.1, "sortino_ratio": 1.5,
                    "max_drawdown": -18.0, "calmar_ratio": 1.0,
                    "var_95": -2.5, "total_return": 22.0},
           "MSFT": {"annualized_return": 15.0, "annualized_vol": 20.0,
                    "sharpe_ratio": 0.9, "sortino_ratio": 1.2,
                    "max_drawdown": -12.0, "calmar_ratio": 1.2,
                    "var_95": -2.0, "total_return": 16.0},
           "_missing_symbols": ["ZZZ"]}
    seen = {}
    def fake_risk(symbols, period="1y"):
        seen["symbols"], seen["period"] = list(symbols), period
        return dict(raw)
    with _patched((db, "get_portfolio", lambda **k: [dict(h) for h in holdings]),
                  (fetcher, "get_risk_metrics", fake_risk)):
        out, s = _read("portfolio_risk", {"period": "6mo"})
    check("risk: crypto excluded from symbol universe",
          seen["symbols"] == ["AAPL", "MSFT"], str(seen))
    check("risk: period forwarded", seen["period"] == "6mo")
    check("risk: condensed per-symbol metrics",
          out["per_symbol"]["AAPL"]["sharpe"] == 1.1
          and out["per_symbol"]["MSFT"]["max_dd"] == -12.0, s)
    check("risk: _missing_symbols surfaced not crashed",
          out.get("missing_symbols") == ["ZZZ"])
    check("risk: within char budget", len(s) <= jt._RESULT_CHAR_BUDGET)
    with _patched((db, "get_portfolio", lambda **k: [])):
        out2, _ = _read("portfolio_risk")
    check("risk: empty book -> note, no error", "note" in out2, str(out2))


# ── 3. portfolio_correlation → fetcher.get_correlation_matrix ───────────────
def test_portfolio_correlation():
    import database as db
    import fetcher
    holdings = [{"symbol": s, "asset_type": "stock"} for s in ("AAPL", "MSFT", "XOM")]
    matrix = {"AAPL": {"AAPL": 1.0, "MSFT": 0.82, "XOM": 0.11},
              "MSFT": {"AAPL": 0.82, "MSFT": 1.0, "XOM": 0.05},
              "XOM": {"AAPL": 0.11, "MSFT": 0.05, "XOM": 1.0}}
    with _patched((db, "get_portfolio", lambda **k: [dict(h) for h in holdings]),
                  (fetcher, "get_correlation_matrix",
                   lambda symbols, period="3mo": {"symbols": list(symbols),
                                                  "matrix": matrix})):
        out, s = _read("portfolio_correlation")
    check("corr: avg pairwise computed from upper triangle",
          abs(out["avg_pairwise_corr"] - round((0.82 + 0.11 + 0.05) / 3, 4)) < 1e-9,
          str(out))
    check("corr: most-correlated pair ranked first",
          out["most_correlated"][0] == {"pair": "AAPL/MSFT", "corr": 0.82})
    check("corr: least-correlated pair surfaced",
          out["least_correlated"][0] == {"pair": "MSFT/XOM", "corr": 0.05})
    check("corr: no full NxN matrix in output (condensed)", "matrix" not in out)
    check("corr: within char budget", len(s) <= jt._RESULT_CHAR_BUDGET)
    with _patched((db, "get_portfolio",
                   lambda **k: [{"symbol": "AAPL", "asset_type": "stock"}])):
        out2, _ = _read("portfolio_correlation")
    check("corr: single holding -> note, no crash", "note" in out2, str(out2))


# ── 4. get_dividends → fetcher.get_dividend_data ─────────────────────────────
def test_dividends():
    import fetcher
    d = {"symbol": "KO", "name": "Coca-Cola", "price": 60.0, "div_rate": 1.94,
         "div_yield": 3.2, "ex_date": "2026-06-15", "pay_date": "2026-07-01",
         "frequency": "Quarterly", "div_growth_5y": 4.1, "payout_ratio": 0.7,
         "history": [{"date": "2026-0%d-01" % i, "amount": 0.485} for i in range(1, 7)]}
    with _patched((fetcher, "get_dividend_data", lambda sym: dict(d))):
        out, s = _read("get_dividends", {"symbol": "KO"})
    check("dividends: core fields kept",
          out["div_yield"] == 3.2 and out["frequency"] == "Quarterly"
          and out["ex_date"] == "2026-06-15", s)
    check("dividends: history condensed to last 4 payments",
          len(out["recent_payments"]) == 4)
    check("dividends: within char budget", len(s) <= jt._RESULT_CHAR_BUDGET)


# ── 5. transactions_summary → database.get_transactions (real temp DB) ──────
def test_transactions_summary():
    import database as db
    db.init_db()
    db.add_transaction("AAPL", "BUY", 10, 100.0, date="2026-01-05")
    db.add_transaction("AAPL", "SELL", 4, 120.0, date="2026-02-10")
    db.add_transaction("MSFT", "BUY", 2, 300.0, date="2026-03-01")
    out, s = _read("transactions_summary")
    check("txns: counts buys and sells", out["buys"] == 2 and out["sells"] == 1,
          str(out))
    check("txns: dollar totals from `total` column",
          out["bought_usd"] == 1600.0 and out["sold_usd"] == 480.0, str(out))
    check("txns: recent rows condensed",
          len(out["recent"]) == 3 and set(out["recent"][0])
          == {"date", "symbol", "action", "shares", "price"})
    fout, _ = _read("transactions_summary", {"symbol": "msft"})
    check("txns: symbol filter normalizes case",
          fout["count"] == 1 and fout["symbol_filter"] == "MSFT", str(fout))
    bad, _ = _read("transactions_summary", {"symbol": "NOT A TICKER!!"})
    check("txns: invalid symbol -> error JSON, no raise", "error" in bad)
    check("txns: within char budget", len(s) <= jt._RESULT_CHAR_BUDGET)


# ── 6. liquidity_stress → liquidity_monitor.compute_stress_score ────────────
def test_liquidity_stress():
    import liquidity_monitor as lm
    raw = {"composite_score": 62, "regime": "WARNING",
           "regime_detail": "Multiple stress signals active.",
           "position_sizing_multiplier": 0.5,
           "recommendation": "Reduce position sizes to 50% of normal.",
           "percentile_rank": 88.0,
           "indicators": {k: {"score": 50, "value": 1.0,
                              "detail": "detail for " + k, "weight": 0.2}
                          for k in ("vix_regime", "cross_correlation",
                                    "sector_dispersion", "volume_regime",
                                    "credit_stress", "market_breadth")},
           "history": [{"score": i} for i in range(30)]}
    with _patched((lm, "compute_stress_score", lambda: dict(raw))):
        out, s = _read("liquidity_stress")
    check("liquidity: composite + regime + sizing kept",
          out["composite_score"] == 62 and out["regime"] == "WARNING"
          and out["position_sizing_multiplier"] == 0.5, s)
    check("liquidity: six sub-indicators condensed to score+detail",
          len(out["indicators"]) == 6
          and set(out["indicators"]["vix_regime"]) == {"score", "detail"})
    check("liquidity: sparkline history dropped", "history" not in out)
    check("liquidity: within char budget", len(s) <= jt._RESULT_CHAR_BUDGET)


# ── 7. congress_trades → congress.get_trades_for_ticker ─────────────────────
def test_congress_trades():
    import congress
    trades = [{"member_name": "Member %d" % i, "ticker": "NVDA",
               "txn_type": "Purchase" if i % 2 == 0 else "Sale",
               "txn_type_raw": "P" if i % 2 == 0 else "S",
               "txn_date": "05/%02d/2026" % (20 - i),
               "amount_str": "$15,001 - $50,000", "amount_val": 15001}
              for i in range(10)]
    with _patched((congress, "get_trades_for_ticker",
                   lambda ticker, days=180: [dict(t) for t in trades])):
        out, s = _read("congress_trades", {"symbol": "NVDA"})
    check("congress: buy/sell tally from txn_type_raw",
          out["trade_count"] == 10 and out["buys"] == 5 and out["sells"] == 5, s)
    check("congress: recent trades capped at 6 compact rows",
          len(out["recent"]) == 6
          and set(out["recent"][0]) == {"member", "type", "date", "amount"})
    check("congress: within char budget", len(s) <= jt._RESULT_CHAR_BUDGET)
    with _patched((congress, "get_trades_for_ticker", lambda ticker, days=180: [])):
        empty, _ = _read("congress_trades", {"symbol": "ZZZQ"})
    check("congress: no trades -> honest note", empty["trade_count"] == 0
          and "note" in empty, str(empty))


# ── 8. reflexivity → reflexivity_detector.detect_loops ──────────────────────
def test_reflexivity():
    import reflexivity_detector as rd
    raw = {"symbol": "GME", "loop_count": 1, "max_strength": 74,
           "dominant_loop": "SHORT_SQUEEZE", "overall_risk": "HIGH REFLEXIVITY",
           "active_loops": [
               {"type": "SHORT_SQUEEZE", "detected": True, "strength": 74,
                "direction": "UP", "evidence": ["x" * 400]},
               {"type": "DILUTION_SPIRAL", "detected": False, "strength": 5,
                "direction": "NEUTRAL", "evidence": []}],
           "fundamentals_snapshot": {"price": 25.0}, "timestamp": "t"}
    with _patched((rd, "detect_loops", lambda sym: dict(raw))):
        out, s = _read("reflexivity", {"symbol": "GME"})
    check("reflexivity: headline risk fields kept",
          out["overall_risk"] == "HIGH REFLEXIVITY"
          and out["dominant_loop"] == "SHORT_SQUEEZE", s)
    check("reflexivity: loops condensed (no evidence dumps)",
          len(out["loops"]) == 2 and "evidence" not in out["loops"][0])
    check("reflexivity: within char budget", len(s) <= jt._RESULT_CHAR_BUDGET)
    with _patched((rd, "detect_loops", lambda sym: {"error": "no fundamentals"})):
        e, _ = _read("reflexivity", {"symbol": "ZZZQ"})
    check("reflexivity: engine error passed through", e.get("error") == "no fundamentals")


# ── 9. historical_analogs → historical_analog.compute_historical_analog ─────
def test_historical_analogs():
    import historical_analog as ha
    raw = {"available": True,
           "current_conditions": {"rsi_14": 71.2, "vol_band": "HIGH",
                                  "trend_position": "ABOVE_BOTH", "rsi_band": "OVERBOUGHT"},
           "match_count": 42, "lookback_years": 10, "forward_window_days": 30,
           "forward_returns": {"mean": 1.2, "median": 0.8, "win_rate": 57.0,
                               "p25": -3.0, "p75": 5.0},
           "sample_dates": [{"date": "2021-01-0%d" % i} for i in range(1, 6)],
           "interpretation": "Mildly positive skew.", "error": None}
    with _patched((ha, "compute_historical_analog", lambda sym: dict(raw))):
        out, s = _read("historical_analogs", {"symbol": "NVDA"})
    check("analogs: stats + interpretation kept",
          out["match_count"] == 42 and out["forward_returns"]["win_rate"] == 57.0
          and out["interpretation"], s)
    check("analogs: sample_dates dropped from condensed output",
          "sample_dates" not in out)
    check("analogs: within char budget", len(s) <= jt._RESULT_CHAR_BUDGET)
    with _patched((ha, "compute_historical_analog",
                   lambda sym: {"available": False, "error": "Insufficient history"})):
        e, _ = _read("historical_analogs", {"symbol": "ZZZQ"})
    check("analogs: unavailable -> available:false + reason",
          e["available"] is False and "Insufficient" in e["error"], str(e))


# ── 10. benchmark_compare → db snapshots + fetcher.get_benchmark_history ────
def test_benchmark_compare():
    import database as db
    import fetcher
    from datetime import datetime
    snaps = [{"date": "2026-01-02", "total_value": 100000.0},
             {"date": "2026-03-01", "total_value": 104000.0},
             {"date": "2026-06-10", "total_value": 110000.0}]
    def _epoch(d):
        return int(datetime.strptime(d, "%Y-%m-%d").timestamp())
    bars = [{"time": _epoch("2025-07-01"), "value": 90.0},   # pre-window, must be cut
            {"time": _epoch("2026-01-02"), "value": 100.0},
            {"time": _epoch("2026-06-10"), "value": 105.0}]
    seen = {}
    def fake_bench(symbol="SPY", period="1y", base_value=None):
        seen["symbol"], seen["period"] = symbol, period
        return [dict(b) for b in bars]
    with _patched((db, "get_snapshots", lambda: [dict(x) for x in snaps]),
                  (fetcher, "get_benchmark_history", fake_bench)):
        out, s = _read("benchmark_compare", {"period": "1y"})
    check("benchmark: portfolio return from snapshot window",
          out["portfolio_return_pct"] == 10.0, str(out))
    check("benchmark: bars aligned to first snapshot (pre-window bar cut)",
          out["benchmark_return_pct"] == 5.0, str(out))
    check("benchmark: excess return computed",
          out["excess_return_pct"] == 5.0, str(out))
    check("benchmark: defaults to SPY", seen["symbol"] == "SPY")
    check("benchmark: within char budget", len(s) <= jt._RESULT_CHAR_BUDGET)
    with _patched((db, "get_snapshots", lambda: []),
                  (fetcher, "get_benchmark_history", fake_bench)):
        e, _ = _read("benchmark_compare")
    check("benchmark: no snapshots -> note, no crash", "note" in e, str(e))
    bad, _ = _read("benchmark_compare", {"benchmark": "../etc"})
    check("benchmark: invalid benchmark symbol -> error JSON", "error" in bad)


# ── 11. Deepened condenser: forecast carries compact per-signal rows ─────────
def test_forecast_condenser():
    import forecast_ensemble as fe
    raw = {"symbol": "NVDA", "horizon_days": 20, "n_signals": 5,
           "ensemble": {"prob_up": 0.61, "direction": "UP", "conviction": "MODERATE",
                        "verdict": "LEAN BULLISH",
                        "return_cone": {"p10": -6.0, "p50": 2.5, "p90": 9.0}},
           "signals": [{"key": "rf", "name": "RandomForest", "prob_up": 0.66,
                        "weight": 0.3, "detail": "d" * 300, "agrees": True,
                        "direction": "UP", "expected_return_pct": 3.0}
                       for _ in range(5)],
           "notes": ["Broad signal agreement reinforces the UP call."]}
    with _patched((fe, "ensemble_forecast",
                   lambda sym, horizon_days=20: dict(raw))):
        out, s = _read("forecast", {"symbol": "NVDA"})
    check("forecast: per-signal breakdown included",
          len(out["signals"]) == 5
          and out["signals"][0] == {"name": "RandomForest", "prob_up": 0.66,
                                    "weight": 0.3}, s)
    check("forecast: signal detail strings stripped (compact)",
          "detail" not in out["signals"][0])
    check("forecast: ensemble verdict still present",
          out["verdict"] == "LEAN BULLISH" and out["prob_up"] == 0.61)
    check("forecast: within char budget", len(s) <= jt._RESULT_CHAR_BUDGET,
          "len=%d" % len(s))


# ── 12. Deepened condenser: get_portfolio top_positions carry day change ────
def test_portfolio_condenser():
    import jarvis
    holdings = [{"symbol": "P%02d" % i, "market_value": 1000.0 * (20 - i),
                 "unrealized_pnl": 50.0, "unrealized_pct": 5.0,
                 "day_change_pct": round(0.1 * i, 2), "day_pnl": 12.0,
                 "weight_pct": 5.0}
                for i in range(20)]
    pulse = {"total_value": 210000.0, "total_pnl": 1000.0, "total_pnl_pct": 0.5,
             "day_pnl": 240.0, "day_pnl_pct": 0.11, "num_positions": 20,
             "holdings": holdings, "best": holdings[-1], "worst": holdings[0]}
    with _patched((jarvis, "_portfolio_pulse", lambda: dict(pulse))):
        out, s = _read("get_portfolio")
    check("portfolio: top_positions capped at 10", len(out["top_positions"]) == 10)
    check("portfolio: positions sorted by market value",
          out["top_positions"][0]["symbol"] == "P00"
          and out["top_positions"][9]["symbol"] == "P09")
    check("portfolio: each top position carries day_change_pct",
          all("day_change_pct" in r for r in out["top_positions"]), s)
    check("portfolio: rows are compact (no day_pnl/unrealized_pnl dollars)",
          set(out["top_positions"][0]) == {"symbol", "market_value", "weight_pct",
                                           "unrealized_pct", "day_change_pct"})
    check("portfolio: within char budget", len(s) <= jt._RESULT_CHAR_BUDGET,
          "len=%d" % len(s))


# ── 13. Deepened condenser: sector_flow ranks instead of dumping ────────────
def test_sector_flow_condenser():
    import synth_sectorflow as sf
    sectors = []
    for i in range(11):
        sectors.append({"sector": "Sector %02d" % i, "etf": "XL%d" % i,
                        "composite_flow_score": float(i * 10 - 50),
                        "rs_vs_spy_1mo": 0.5,
                        "price_1d_pct": 0.1, "price_5d_pct": 0.2,
                        "price_1mo_pct": 1.0, "narrative_phase": "x" * 80,
                        "reddit_msgs": 9, "hn_hits": 3, "wiki_views": 12345,
                        "options_pcr": 0.9, "insider_buy_ratio_30d": 0.4,
                        "congress_net_30d_usd": 1e6,
                        "factor_1d_return_pct": 0.05})
    raw = {"as_of": "2026-06-11T00:00:00Z", "sectors": sectors,
           "leader_sector": "Sector 10", "laggard_sector": "Sector 00",
           "factor_snapshot": {"smb": 0.1, "hml": -0.2, "mom": 0.3}}
    with _patched((sf, "sector_flow", lambda: dict(raw))):
        out, s = _read("sector_flow")
    check("sectorflow: all 11 sectors kept, ranked by score desc",
          len(out["sectors"]) == 11
          and out["sectors"][0]["sector"] == "Sector 10"
          and out["sectors"][-1]["sector"] == "Sector 00", s)
    check("sectorflow: rows condensed to sector/etf/score/rs",
          set(out["sectors"][0]) == {"sector", "etf", "score", "rs_vs_spy_1mo"})
    check("sectorflow: leader/laggard surfaced",
          out["leader_sector"] == "Sector 10" and out["laggard_sector"] == "Sector 00")
    check("sectorflow: raw dump dropped (no factor_snapshot)",
          "factor_snapshot" not in out)
    check("sectorflow: within char budget (no truncation)",
          len(s) <= jt._RESULT_CHAR_BUDGET, "len=%d" % len(s))
    with _patched((sf, "sector_flow", lambda: {"sectors": [], "error": "boom"})):
        e, _ = _read("sector_flow")
    check("sectorflow: empty engine result -> error JSON", e.get("error") == "boom")


# ── 14. Failure safety: every new tool degrades to {"error": ...} ───────────
def test_new_tools_never_raise():
    import database as db
    import fetcher
    import liquidity_monitor as lm
    import congress
    import reflexivity_detector as rd
    import historical_analog as ha

    def boom(*a, **k):
        raise RuntimeError("engine exploded")

    seams = {
        "portfolio_risk": [(fetcher, "get_risk_metrics", boom),
                           (db, "get_portfolio",
                            lambda **k: [{"symbol": "A", "asset_type": "stock"},
                                         {"symbol": "B", "asset_type": "stock"}])],
        "portfolio_correlation": [(fetcher, "get_correlation_matrix", boom),
                                  (db, "get_portfolio",
                                   lambda **k: [{"symbol": "A", "asset_type": "stock"},
                                                {"symbol": "B", "asset_type": "stock"}])],
        "get_dividends": [(fetcher, "get_dividend_data", boom)],
        "transactions_summary": [(db, "get_transactions", boom)],
        "liquidity_stress": [(lm, "compute_stress_score", boom)],
        "congress_trades": [(congress, "get_trades_for_ticker", boom)],
        "reflexivity": [(rd, "detect_loops", boom)],
        "historical_analogs": [(ha, "compute_historical_analog", boom)],
        "benchmark_compare": [(db, "get_snapshots", boom)],
    }
    args = {"symbol": "NVDA"}
    for name in _NEW_TOOLS:
        try:
            with _patched(*seams[name]):
                out, _ = _read(name, dict(args))
            check("safety: %s exploding engine -> error JSON" % name,
                  out.get("error") == "engine exploded"
                  or "error" in out, str(out))
        except Exception as e:
            check("safety: %s exploding engine -> error JSON" % name, False, repr(e))
    # And invalid symbols on the symbol-taking tools never raise either.
    for name in ("get_dividends", "congress_trades", "reflexivity",
                 "historical_analogs"):
        out, _ = _read(name, {"symbol": "../../etc/passwd"})
        check("safety: %s invalid symbol -> error JSON" % name,
              out.get("error") == "invalid symbol", str(out))


def main():
    tests = [
        ("registry shape + whitelist", test_registry_shape),
        ("portfolio_risk", test_portfolio_risk),
        ("portfolio_correlation", test_portfolio_correlation),
        ("get_dividends", test_dividends),
        ("transactions_summary", test_transactions_summary),
        ("liquidity_stress", test_liquidity_stress),
        ("congress_trades", test_congress_trades),
        ("reflexivity", test_reflexivity),
        ("historical_analogs", test_historical_analogs),
        ("benchmark_compare", test_benchmark_compare),
        ("forecast condenser", test_forecast_condenser),
        ("portfolio condenser", test_portfolio_condenser),
        ("sector_flow condenser", test_sector_flow_condenser),
        ("new tools never raise", test_new_tools_never_raise),
    ]
    for title, fn in tests:
        print("── %s" % title)
        try:
            fn()
        except Exception as e:
            global _failed
            _failed += 1
            print("  ✗ %s raised %s: %s" % (title, type(e).__name__, e))

    print("\n" + "=" * 56)
    print("  jarvis_tools results: %d passed, %d failed" % (_passed, _failed))
    print("=" * 56)
    try:
        os.unlink(_TMPDB.name)
    except OSError:
        pass
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
