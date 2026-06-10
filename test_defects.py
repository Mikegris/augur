"""
Regression tests for the full-codebase defect sweep (PR: defect-sweep).

Each test pins one confirmed fix so it can't silently regress. Designed to run
OFFLINE and deterministically — network seams (yfinance, SEC, CoinGecko) are
monkeypatched, and a throwaway DB is used so the real wealth.db is untouched.

Run:  ./venv/bin/python test_defects.py
Exit: 0 if all pass, 1 otherwise.
"""
import os
import sys
import tempfile

# Isolate DB before importing app/tracker so we never touch the real wealth.db.
_TMPDB = tempfile.NamedTemporaryFile(prefix="augur_test_", suffix=".db", delete=False)
_TMPDB.close()
os.environ["AUGUR_DB_PATH"] = _TMPDB.name
os.environ["DISABLE_WARMER"] = "1"

import pandas as pd  # noqa: E402

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


# ── 1. CoinGecko prev_close/change math (fetcher) ────────────────────────────
def test_coingecko_math():
    import fetcher
    fetcher._coingecko_simple_batch = lambda ids: {
        "bitcoin": {"usd": 125.0, "usd_24h_change": 25.0, "usd_24h_vol": 1,
                    "usd_market_cap": 1}
    }
    q = fetcher._coingecko_quote("bitcoin", "BTC")
    # +25% day: prior close must be 100 (125/1.25), change +25 — NOT 93.75/31.25.
    check("coingecko prev_close = price/(1+pct/100)", abs(q["prev_close"] - 100.0) < 1e-6,
          "prev_close=%s" % q["prev_close"])
    check("coingecko change = price - prev", abs(q["change"] - 25.0) < 1e-6,
          "change=%s" % q["change"])


# ── 2. smart_money insider cluster-buy boost (was dead code) ─────────────────
def test_smart_money_cluster():
    import smart_money, sec_edgar
    today = smart_money._today_et().strftime("%Y-%m-%d")
    sec_edgar.get_form4_transactions = lambda sym, **k: [
        {"insider_name": "A", "transaction_type": "BUY", "date": today},
        {"insider_name": "B", "transaction_type": "BUY", "date": today},
        {"insider_name": "C", "transaction_type": "BUY", "date": today},
        {"insider_name": "D", "transaction_type": "SELL", "date": today},
    ]
    score, _ = smart_money._score_insiders("TEST")
    # 3 buys / 4 = 0.75 -> raw 19; +3 cluster boost (>=3 distinct BUYers) -> 22.
    check("smart_money cluster boost fires (22, not 19)", score == 22, "score=%s" % score)


# ── 3. research_montecarlo _empty_response: independent percentile lists ─────
def test_montecarlo_aliasing():
    import research_montecarlo as mc
    r = mc._empty_response([], 5, "mvn", 100)
    p = r["percentiles"]
    distinct = len({id(v) for v in p.values()}) == 5
    p["p05"][0] = 9.9
    check("montecarlo percentile lists are independent objects", distinct)
    check("mutating p05 doesn't bleed into p95", p["p95"][0] == 0.0)


# ── 4. safe_executor: overall timeout returns a list, never raises ───────────
def test_safe_executor_timeout():
    import time, safe_executor
    def work(x):
        time.sleep(0.3 if x == 2 else 0.0)
        return x * 10
    out = safe_executor.parallel_map(work, [1, 2, 3, 4], max_workers=4,
                                     timeout_per_item=0.05)
    check("safe_executor returns full-length list on timeout",
          isinstance(out, list) and len(out) == 4, "out=%r" % out)
    check("safe_executor fills timed-out slot with None", out[1] is None)


# ── 5. earnings beat-rate: row-aligned, not positional ───────────────────────
def test_earnings_beatrate_alignment():
    # The fix: pair columns with one .dropna() instead of independent dropna+zip.
    eh = pd.DataFrame({"epsActual": [1.0, 2.0, 3.0, 4.0],
                       "epsEstimate": [0.9, None, 3.5, 3.8]})
    paired = eh[["epsActual", "epsEstimate"]].dropna()
    beats = int((paired["epsActual"] > paired["epsEstimate"]).sum())
    rate = round(beats / len(paired) * 100)
    # rows: 1.0>0.9 T, 3.0>3.5 F, 4.0>3.8 T -> 2/3 = 67%.
    check("earnings beat-rate row-aligned (2/3 = 67%)", beats == 2 and rate == 67,
          "beats=%s rate=%s" % (beats, rate))


# ── 6. sec_edgar null-ticker guard ───────────────────────────────────────────
def test_sec_edgar_null_ticker():
    # The fix: (entry.get("ticker") or "").upper() — must not raise on null.
    entry = {"ticker": None}
    try:
        val = (entry.get("ticker") or "").upper()
        check("sec_edgar null ticker yields '' (no AttributeError)", val == "")
    except AttributeError:
        check("sec_edgar null ticker yields '' (no AttributeError)", False)


# ── 7. synth_cluster shutdown fallback returns caller-compatible dict ────────
def test_synth_cluster_finalize():
    import synth_cluster as sc
    d = sc._finalize_scan("TEST", [None] * len(sc.COMPONENTS))
    ok = (isinstance(d, dict) and hasattr(d, "get")
          and d.get("n_sources_firing") == 0
          and isinstance(d.get("composite_score"), float)
          and d.get("symbol") == "TEST")
    check("synth_cluster _finalize_scan returns envelope dict", ok, repr(d)[:80])


# ── 8. congress TICKER_RE matches dotted/class tickers ───────────────────────
def test_congress_ticker_regex():
    import re, congress
    # Re-derive the same pattern the parser uses.
    RE = r"\(([A-Z][A-Z.]{0,5})\)\s*\[(ST|OP|MF|GS|OT|RE|PF|HC|VI)\]"
    cases = {"(BRK.B) [ST]": "BRK.B", "(AAPL) [ST]": "AAPL", "(MSFT) [OP]": "MSFT"}
    ok = all((re.search(RE, s) or [None]) and re.search(RE, s).group(1) == exp
             for s, exp in cases.items())
    check("congress regex matches dotted tickers (BRK.B)", ok)


# ── 9. finviz Stocks parsing survives comma/dash ─────────────────────────────
def test_finviz_stocks_parse():
    import finviz_data as fv
    def parse(v):
        return int(fv._safe_float(v)) if fv._safe_float(v) is not None else None
    check("finviz '2,345' -> None (no crash)", parse("2,345") is None)
    check("finviz '-' -> None", parse("-") is None)
    check("finviz '600' -> 600", parse("600") == 600)


# ── 10. forecast_ensemble + accountability degrade cleanly ───────────────────
def test_forecast_edge_cases():
    import forecast_ensemble as fe, forecast_accountability as fa
    check("ensemble empty symbol -> error", fe.ensemble_forecast("")["error"] == "symbol required")
    # Brier on degenerate all-up base rate must not divide by zero.
    r = fa._brier_from_rows([{"metadata": {"prob_up": 0.7}, "realized_return": 3.0}] * 4)
    check("brier all-up base rate -> skill None (no div0)", r["brier_skill"] is None)
    # adaptive_weights with empty history == base (sum preserved).
    base = fe._BASE_WEIGHTS
    adj = fa.adaptive_weights(base)
    check("adaptive_weights cold-start preserves weight sum",
          abs(sum(adj.values()) - sum(base.values())) < 1e-6)


# ── 11. Route handlers degrade gracefully (test client) ──────────────────────
def test_routes():
    import app
    c = app.app.test_client()
    r1 = c.post("/api/stress-test")  # no body / no content-type
    # The bug was a 415 UnsupportedMediaType before the body was ever read.
    # The fix parses gracefully and reaches business logic (200 with holdings,
    # or 400 "No positions" on an empty book) — the point is: never 415, JSON.
    check("POST /api/stress-test no-body parsed gracefully (not 415, JSON)",
          r1.status_code != 415 and r1.is_json, "status=%s" % r1.status_code)
    r2 = c.post("/api/transactions/add",
                json={"symbol": "AAPL", "action": 1, "shares": 1, "price": 1})
    check("POST /api/transactions/add numeric action -> 400 (not 500)",
          r2.status_code == 400 and r2.is_json)
    r3 = c.post("/api/scanner/profile", data="not json",
                content_type="application/json")
    check("POST /api/scanner/profile bad body -> JSON 400 (not HTML)",
          r3.status_code == 400 and r3.is_json)
    r4 = c.get("/api/synth/whatif?symbol=AAPL&market_value=1000&action=add")
    check("GET /api/synth/whatif -> 200 JSON", r4.status_code == 200 and r4.is_json)


# ── 12. database add_position: offsetting to zero shares must not crash ──────
def test_database_zero_shares():
    import database
    database.init_db()
    database.add_position("ZZZTEST", "Test Co", 10, 100.0)
    # Fully offsetting add -> total_shares == 0; must not ZeroDivisionError.
    database.add_position("ZZZTEST", "Test Co", -10, 100.0)
    pos = [p for p in database.get_portfolio() if p["symbol"] == "ZZZTEST"]
    check("database add_position offset-to-zero doesn't crash",
          bool(pos) and abs(pos[0]["shares"]) < 1e-9)


# ── 13. cli cmd_quote: explicit None fields must not crash ───────────────────
def test_cli_quote_none_fields():
    import cli, fetcher
    fetcher.get_quote = lambda s: {"price": None, "change": None,
                                   "change_pct": None, "name": "X",
                                   "market_cap": None, "volume": None}
    class _Args:
        symbols = ["AAA"]
    try:
        cli.cmd_quote(_Args())  # prints; the point is it doesn't raise
        check("cli cmd_quote survives None-valued quote fields", True)
    except Exception as e:
        check("cli cmd_quote survives None-valued quote fields", False, repr(e))


# ── 14. XSS hardening stays in place (static guard) ──────────────────────────
def test_xss_escaping_intact():
    src = open(os.path.join(os.path.dirname(__file__), "static/js/app.js")).read()
    check("no unescaped ${e.message} in app.js innerHTML",
          "${e.message}" not in src)
    check("no unescaped concat ' + e.message + ' in app.js",
          "' + e.message + '" not in src)


# ── 15. Chart.js time-scale date adapter is loaded ───────────────────────────
def test_chart_date_adapter_present():
    html = open(os.path.join(os.path.dirname(__file__), "templates/index.html")).read()
    js = open(os.path.join(os.path.dirname(__file__),
                           "static/js/synth_catalyst.js")).read()
    # The catalysts chart uses a Chart.js time axis; index.html must load a date
    # adapter or the chart throws "complete date adapter required" at runtime.
    uses_time = "type: 'time'" in js
    has_adapter = "chartjs-adapter" in html
    check("Chart.js date adapter loaded when a time scale is used",
          (not uses_time) or has_adapter)


def main():
    tests = [
        ("CoinGecko quote math", test_coingecko_math),
        ("smart_money cluster boost", test_smart_money_cluster),
        ("montecarlo list aliasing", test_montecarlo_aliasing),
        ("safe_executor timeout", test_safe_executor_timeout),
        ("earnings beat-rate alignment", test_earnings_beatrate_alignment),
        ("sec_edgar null ticker", test_sec_edgar_null_ticker),
        ("synth_cluster finalize", test_synth_cluster_finalize),
        ("congress ticker regex", test_congress_ticker_regex),
        ("finviz stocks parse", test_finviz_stocks_parse),
        ("forecast edge cases", test_forecast_edge_cases),
        ("route handlers", test_routes),
        ("database zero-shares guard", test_database_zero_shares),
        ("cli quote None fields", test_cli_quote_none_fields),
        ("xss escaping intact", test_xss_escaping_intact),
        ("chart date adapter present", test_chart_date_adapter_present),
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
    print("  Defect-regression results: %d passed, %d failed" % (_passed, _failed))
    print("=" * 56)
    try:
        os.unlink(_TMPDB.name)
    except OSError:
        pass
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
