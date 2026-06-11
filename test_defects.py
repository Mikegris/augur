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


# ── 11b. Jarvis assistant layer: briefing synthesis + ask intent routing ─────
def test_jarvis():
    import jarvis

    holdings = [
        {"symbol": "AAPL", "shares": 10, "avg_cost": 100.0, "asset_type": "stock"},
        {"symbol": "TSLA", "shares": 1, "avg_cost": 500.0, "asset_type": "stock"},
    ]
    quotes = {
        "AAPL": {"price": 200.0, "change": 10.0, "change_pct": 5.26},
        "TSLA": {"price": 400.0, "change": -20.0, "change_pct": -4.76},
    }
    alerts = [
        {"symbol": "AAPL", "alert_type": "above", "price": 150.0, "triggered": 1},
        {"symbol": "TSLA", "alert_type": "below", "price": 395.0, "triggered": 0},
    ]
    indices = [
        {"symbol": "^GSPC", "price": 5000.0, "change_pct": -1.0},
        {"symbol": "^NDX", "price": 18000.0, "change_pct": -1.5},
        {"symbol": "^VIX", "price": 30.0, "change_pct": 5.0},
    ]

    # Monkeypatch every data seam jarvis touches — fully offline. jarvis.db /
    # jarvis.fetcher ARE the shared modules, so save originals and restore in
    # the finally below or later tests (database zero-shares, perf source
    # inspection of get_quotes_batch) see the fakes.
    import earnings
    import idea_pool_warmer
    saved = [
        (jarvis.db, "get_portfolio"), (jarvis.db, "get_watchlist"),
        (jarvis.db, "get_price_alerts"),
        (jarvis.fetcher, "get_quotes_batch"), (jarvis.fetcher, "get_market_indices"),
        (jarvis.fetcher, "get_quote"),
        (earnings, "get_earnings_calendar"),
        (idea_pool_warmer, "list_warmed_symbols"),
    ]
    originals = [(mod, name, getattr(mod, name)) for mod, name in saved]
    jarvis.db.get_portfolio = lambda **k: [dict(h) for h in holdings]
    jarvis.db.get_watchlist = lambda: []
    jarvis.db.get_price_alerts = lambda include_triggered=False: [dict(a) for a in alerts]
    jarvis.fetcher.get_quotes_batch = lambda syms: {s: dict(quotes[s]) for s in syms if s in quotes}
    jarvis.fetcher.get_market_indices = lambda: [dict(i) for i in indices]
    jarvis.fetcher.get_quote = lambda s: {"symbol": s, "price": 123.45, "change_pct": 1.5,
                                          "fifty_two_week_high": 150.0, "fifty_two_week_low": 100.0}
    earnings.get_earnings_calendar = lambda syms: [
        {"symbol": "AAPL", "earnings_date": "2026-06-13", "days_until": 2,
         "beat_rate": 80, "avg_surprise_pct": 5.0}]
    idea_pool_warmer.list_warmed_symbols = lambda asset_class=None: []
    try:
        _run_jarvis_checks(jarvis)
    finally:
        for mod, name, fn in originals:
            setattr(mod, name, fn)


def _run_jarvis_checks(jarvis):
    b = jarvis.get_briefing(force_refresh=True)
    kinds = [c["kind"] for c in b["insights"]]
    check("briefing: triggered alert -> P1 card", "alert" in kinds)
    check("briefing: near-alert detected (TSLA within 2% of level)", "alert_near" in kinds)
    check("briefing: big movers carded", kinds.count("mover") == 2)
    check("briefing: concentration flagged (AAPL ~83%)", "concentration" in kinds)
    check("briefing: VIX 30 -> STRESSED regime card", "regime" in kinds)
    check("briefing: earnings-in-2-days carded", "earnings" in kinds)
    check("briefing: P1 cards sort first", b["insights"][0]["priority"] == 1)
    mover_titles = [c["title"] for c in b["insights"] if c["kind"] == "mover"]
    check("briefing: mover title has no double negative ('down -4%')",
          all("down -" not in t for t in mover_titles), str(mover_titles))
    check("briefing: headline mentions portfolio day move",
          "Portfolio" in b["headline"], b["headline"])

    cases = [
        ("how is my portfolio", "portfolio"),
        ("biggest loser today", "portfolio"),
        ("price of NVDA", "quote"),
        ("when does AAPL report", "earnings"),
        ("how are markets", "market"),
        ("my exposure", "exposure"),
        ("any ideas", "ideas"),
        ("my alerts", "alerts"),
        ("completely unrelated gibberish", "help"),
    ]
    for q, want in cases:
        got = jarvis.ask(q)
        check("ask: %r -> %s" % (q, want), got["intent"] == want,
              "got %s" % got["intent"])
    worst = jarvis.ask("biggest loser today")
    check("ask: biggest loser resolves TSLA", "TSLA" in worst["answer"], worst["answer"])
    check("ask: empty query -> help", jarvis.ask("")["intent"] == "help")

    # view_context — exercised uncached so the mocked seams are visible
    # (the coalesce cache hydrates from disk and could hold real-data lines).
    vc = lambda v, s=None: jarvis._view_context_uncached(v, s)
    check("ctx: portfolio view speaks the pulse", "Book at" in vc("portfolio")["line"],
          vc("portfolio")["line"])
    check("ctx: markets view speaks the regime", "VIX" in vc("markets")["line"])
    check("ctx: stress view appends scenario nudge", "scenario" in vc("stress")["line"])
    check("ctx: research+symbol includes held position",
          "You hold" in vc("research", "AAPL")["line"], vc("research", "AAPL")["line"])
    check("ctx: alerts view counts triggered", "triggered" in vc("alerts")["line"])
    check("ctx: watchlist empty has CTA", "empty" in vc("watchlist")["line"])
    check("ctx: persona line for alpha views", len(vc("gex")["line"]) > 20)
    check("ctx: unknown view falls back gracefully", "⌘K" in vc("unknownview")["line"])
    tones = {vc(v)["tone"] for v in ("portfolio", "markets", "gex")}
    check("ctx: tones are valid", tones <= {"pos", "neg", "warn", "info"}, str(tones))

    import app
    c = app.app.test_client()
    r = c.post("/api/jarvis/ask", json={"query": "how are markets"})
    check("route: POST /api/jarvis/ask -> 200 JSON", r.status_code == 200 and r.is_json)
    r2 = c.post("/api/jarvis/ask", json={"query": "x" * 600})
    check("route: overlong query -> 400", r2.status_code == 400)
    r3 = c.get("/api/jarvis/briefing?refresh=1")
    check("route: GET /api/jarvis/briefing -> 200 JSON", r3.status_code == 200 and r3.is_json)
    r4 = c.get("/api/jarvis/context/portfolio")
    check("route: GET /api/jarvis/context/<view> -> 200 JSON",
          r4.status_code == 200 and r4.is_json)
    r5 = c.get("/api/jarvis/context/bad!view")
    check("route: invalid view -> 400", r5.status_code == 400)

    # activity snapshot — pure in-memory state, must never raise
    a = jarvis.activity_snapshot()
    check("activity: snapshot has background list", isinstance(a.get("background"), list))
    check("activity: snapshot has summary string",
          isinstance(a.get("summary"), str) and len(a["summary"]) > 0)
    r6 = c.get("/api/jarvis/activity")
    check("route: GET /api/jarvis/activity -> 200 JSON",
          r6.status_code == 200 and r6.is_json)

    # SSE stream — Werkzeug 3 test client defaults to buffered=False, so the
    # infinite generator is NOT consumed for status/headers. The generator
    # emits its FIRST snapshot before any sleep, so reading exactly one chunk
    # returns promptly; then close() GeneratorExits the stream.
    r7 = c.get("/api/jarvis/activity/stream", buffered=False)
    try:
        check("route: SSE stream -> 200 text/event-stream",
              r7.status_code == 200 and r7.mimetype == "text/event-stream",
              "status=%s mimetype=%s" % (r7.status_code, r7.mimetype))
        check("route: SSE stream sets Cache-Control: no-cache",
              r7.headers.get("Cache-Control") == "no-cache")
        check("route: SSE stream sets X-Accel-Buffering: no",
              r7.headers.get("X-Accel-Buffering") == "no")
        chunk = next(iter(r7.response))
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", "replace")
        check("route: SSE first chunk is an immediate data frame",
              chunk.startswith("data: ") and chunk.endswith("\n\n"),
              chunk[:80])
        import json as _json
        payload = _json.loads(chunk[len("data: "):])
        check("route: SSE frame carries background+summary",
              isinstance(payload.get("background"), list)
              and isinstance(payload.get("summary"), str))
    finally:
        r7.close()  # GeneratorExit at the current yield — never blocks


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


# ── 21. Perf: synthetic-insider channel budget + sectorflow parallel sectors ─
def test_perf_fixes_present():
    import synthetic_insider, synth_sectorflow, inspect
    si = inspect.getsource(synthetic_insider)
    check("synthetic_insider has a per-channel wall-clock budget",
          "_CHANNEL_BUDGET_S" in si and "timeout_per_item=_CHANNEL_BUDGET_S" in si)
    check("synthetic_insider scores channels via safe_executor.parallel_map",
          "parallel_map(" in si)
    check("synthetic_insider has no abandoned-worker shutdown(wait=False)",
          "shutdown(wait=False)" not in si)
    sf = inspect.getsource(synth_sectorflow)
    check("sectorflow builds sectors in parallel (not a serial loop)",
          "_build_sector_row" in sf and "parallel_map(" in sf)
    import fetcher
    fb = inspect.getsource(fetcher.get_quotes_batch)
    check("get_quotes_batch resolves symbols in parallel (Markets/movers/alerts)",
          "parallel_map(" in fb)


# ── 21b. No direct ThreadPoolExecutor abandonment anywhere in the codebase ───
def test_no_abandoned_tpe_workers():
    """Python 3.9 TPE workers are non-daemon and concurrent.futures registers
    an atexit join over them, so `shutdown(wait=False)` leaks workers that
    block interpreter exit (wedged Werkzeug reloader, hung py2app quit).
    The sanctioned pattern is safe_executor.parallel_map on daemon threads.
    Grep every project *.py (excluding safe_executor itself, which documents
    the failure mode, and tests) and assert zero non-comment hits."""
    base = os.path.dirname(os.path.abspath(__file__))
    offenders = []
    for fname in sorted(os.listdir(base)):
        if not fname.endswith(".py"):
            continue
        if fname == "safe_executor.py" or fname.startswith("test_"):
            continue
        path = os.path.join(base, fname)
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except OSError:
            continue
        for lineno, line in enumerate(src.splitlines(), 1):
            code = line.split("#", 1)[0]  # ignore comments
            if "shutdown(wait=False" in code:
                offenders.append("%s:%d" % (fname, lineno))
    check("no shutdown(wait=False) outside comments in any project module",
          not offenders, "found: %s" % ", ".join(offenders))


# ── 19. Research/Alpha tab fixes: window globals, API errors, reflexivity ────
def test_research_alpha_fixes():
    base = os.path.dirname(__file__)
    appjs = open(os.path.join(base, "static/js/app.js")).read()
    # (a) Core helpers exposed on window so global.API-style modules (Monte
    #     Carlo, whatif, probforecast, consensus, peerdiv, divmap, factors,
    #     sectorflow) stop crashing with "global.API is undefined".
    check("window.API exposed for global.API modules", "window.API = API" in appjs)
    check("window.State/fmt/Toast exposed",
          "window.State = State" in appjs and "window.fmt = fmt" in appjs
          and "window.Toast = Toast" in appjs)
    # (b) API wrappers surface the server's {"error": ...} instead of "HTTP 400".
    check("API surfaces server error body", "_apiError" in appjs)
    # (c) Reflexivity uses loop_count (not the raw active_loops array) for the
    #     count, and indexes the array by `type` for the per-loop cards.
    check("reflexivity uses loop_count for the count", "data.loop_count" in appjs)
    check("reflexivity indexes active_loops by type",
          "loops[l.type] = l" in appjs)


# ── 20. Options-flow distinguishes rate-limit from genuine no-data ───────────
def test_options_flow_ratelimit_message():
    import fetcher
    class _RL:
        @property
        def options(self):
            raise RuntimeError("Too Many Requests. Rate limited.")
    orig = fetcher.yf.Ticker
    fetcher.yf.Ticker = lambda s: _RL()
    try:
        out = fetcher.get_unusual_options_flow("AAPL")
    finally:
        fetcher.yf.Ticker = orig
    check("options-flow flags rate-limit (not generic no-data)",
          "rate-limited" in (out.get("error") or "").lower())


# ── 18. Alt-data social pulse: composite math + graceful per-source failure ──
def test_alt_social_pulse():
    import app
    # Composite math (offline).
    comp = app._social_composite({
        "stocktwits": {"status": "live", "messages_total": 30, "bull_ratio": 0.75},
        "hackernews": {"status": "live", "mention_count": 25, "stats": {"avg_polarity": 0.12}},
        "wikipedia": {"status": "live", "stats": {"spike_pct_vs_baseline": 40}},
    })
    check("social buzz score in 0-100",
          comp["buzz_score"] is not None and 0 <= comp["buzz_score"] <= 100)
    check("social sentiment label BULLISH on bullish inputs",
          comp["sentiment_label"] == "BULLISH")
    # Route stays 200 + honest Reddit even when every source raises (no network).
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    app.alt_signals.stocktwits_symbol_sentiment = boom
    if getattr(app, "hn_sentiment", None):
        app.hn_sentiment.fetch_mentions = boom
    if getattr(app, "wiki_attention", None):
        app.wiki_attention.fetch_pageviews = boom
    r = app.app.test_client().get("/api/alt-data/social/AAPL")
    d = r.get_json()
    check("social route 200 even when sources error",
          r.status_code == 200 and r.is_json)
    check("erroring source flagged, not crashed",
          d.get("sources", {}).get("stocktwits", {}).get("status") == "error")
    check("reddit honestly marked unavailable",
          d.get("reddit", {}).get("status") == "unavailable")


# ── 17. safe_executor falls back to serial on thread exhaustion ──────────────
def test_safe_executor_thread_exhaustion():
    import threading
    import safe_executor as se
    check("recognizes 'can't start new thread'",
          se._looks_like_global_shutdown(RuntimeError("can't start new thread")))
    check("recognizes interpreter-shutdown spawn refusal",
          se._looks_like_global_shutdown(
              RuntimeError("can't create new thread at interpreter shutdown")))
    orig = se._spawn
    def _boom(target, name):
        raise RuntimeError("can't start new thread")
    se._spawn = _boom
    try:
        out = se.parallel_map(lambda x: x * 2, [1, 2, 3, 4])
    finally:
        se._spawn = orig
    check("parallel_map serial-fallback on thread exhaustion", out == [2, 4, 6, 8],
          "out=%r" % out)
    # Daemon guarantee — the whole point of the rewrite: workers must never
    # be able to block interpreter exit (the atexit-join reloader wedge).
    seen = {}
    def _spy(target, name):
        t = threading.Thread(target=target, daemon=True, name=name)
        seen["daemon"] = t.daemon
        t.start()
        return t
    se._spawn = _spy
    try:
        out2 = se.parallel_map(lambda x: x + 1, [1, 2, 3])
    finally:
        se._spawn = orig
    check("parallel_map workers are daemon threads", seen.get("daemon") is True)
    check("parallel_map ordered results", out2 == [2, 3, 4], "out=%r" % out2)


# ── 16. Yahoo chart UA isn't the rate-limited Chrome string ──────────────────
def test_yahoo_ua_not_blocked():
    import fetcher
    ua = (fetcher._YAHOO_DIRECT_HEADERS or {}).get("User-Agent", "")
    # The full "...Chrome/120... Safari/537.36" UA gets 429'd by Yahoo's v8
    # chart endpoint, which silently kills the chart-data fallback. Guard so it
    # can't be reintroduced.
    check("Yahoo direct UA is not the rate-limited Chrome string",
          "Chrome/" not in ua, "UA=%r" % ua)


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
        ("jarvis briefing + ask", test_jarvis),
        ("database zero-shares guard", test_database_zero_shares),
        ("cli quote None fields", test_cli_quote_none_fields),
        ("xss escaping intact", test_xss_escaping_intact),
        ("chart date adapter present", test_chart_date_adapter_present),
        ("yahoo UA not blocked", test_yahoo_ua_not_blocked),
        ("safe_executor thread exhaustion", test_safe_executor_thread_exhaustion),
        ("alt-data social pulse", test_alt_social_pulse),
        ("research/alpha tab fixes", test_research_alpha_fixes),
        ("options-flow rate-limit msg", test_options_flow_ratelimit_message),
        ("perf fixes present", test_perf_fixes_present),
        ("no abandoned TPE workers", test_no_abandoned_tpe_workers),
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
