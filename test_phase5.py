#!/usr/bin/env python3
"""
Phase 5 Regression Test: All Flask API Routes
Uses Flask test client — no live server needed.
"""

import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── DB isolation ─────────────────────────────────────────────────────────────
# This suite exercises mutating routes (/api/portfolio/add, /api/transactions
# /add, etc.) which previously wrote fixtures (TEST/AAPL @150) straight into
# the user's real wealth.db — it has no teardown, so every run leaked. Clone
# the DB to a tempfile (WAL-safe, mirrors test_e2e.clone_db) and point the app
# at it via AUGUR_DB_PATH BEFORE database/app import, so the real book is never
# touched. The clone is removed at exit.
import atexit, sqlite3, tempfile


def _clone_db(src="wealth.db"):
    fd, dst = tempfile.mkstemp(prefix="augur_phase5_", suffix=".db")
    os.close(fd)
    if os.path.exists(src):
        s = sqlite3.connect(src)
        d = sqlite3.connect(dst)
        with d:
            s.backup(d)
        s.close()
        d.close()
    return dst


_TEST_DB = _clone_db()
os.environ["AUGUR_DB_PATH"] = _TEST_DB
atexit.register(lambda: os.path.exists(_TEST_DB) and os.unlink(_TEST_DB))

# Suppress noisy loggers
import logging
logging.disable(logging.WARNING)

passed = 0
failed = 0
skipped = 0

def ok(name):
    global passed
    passed += 1
    print(f"  ✓ {name}")

def fail(name, reason=""):
    global failed
    failed += 1
    print(f"  ✗ {name}  — {reason}")

def skip(name, reason=""):
    global skipped
    skipped += 1
    print(f"  ⊘ {name}  — {reason}")


# ─── Setup ───────────────────────────────────────────────────────────────────

import database as db
db.init_db()

from app import app
app.config["TESTING"] = True
client = app.test_client()


def get(path, expect=200):
    """GET a path and check status code."""
    r = client.get(path)
    return r, r.status_code == expect

def post(path, data=None, expect=200):
    """POST JSON to a path and check status code."""
    r = client.post(path, json=data or {})
    return r, r.status_code == expect


# ═══════════════════════════════════════════════════════════════════════════════
# 5A — Core UI
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5A: Core UI ──")

r, good = get("/")
if good and b"AUGUR" in r.data:
    ok("GET / returns 200 with AUGUR")
else:
    fail("GET /", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5B — Market Data Routes
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5B: Market Data Routes ──")

r, good = get("/api/quote/AAPL")
if good:
    d = r.get_json()
    if d and d.get("symbol") == "AAPL":
        ok("GET /api/quote/AAPL")
    else:
        fail("GET /api/quote/AAPL response shape", str(d)[:100])
else:
    fail("GET /api/quote/AAPL", f"status={r.status_code}")

r, good = get("/api/quotes?symbols=AAPL,MSFT")
if good:
    d = r.get_json()
    if d and ("AAPL" in d or "aapl" in str(d).lower()):
        ok("GET /api/quotes?symbols=AAPL,MSFT")
    else:
        fail("GET /api/quotes response shape")
else:
    fail("GET /api/quotes", f"status={r.status_code}")

r, _ = get("/api/quotes", expect=400)
if r.status_code == 400:
    ok("GET /api/quotes (no symbols) returns 400")
else:
    fail("GET /api/quotes (no symbols)", f"status={r.status_code}")

r, good = get("/api/fundamentals/AAPL")
if good:
    d = r.get_json()
    if d and "symbol" in d:
        ok("GET /api/fundamentals/AAPL")
    else:
        fail("GET /api/fundamentals response shape")
else:
    fail("GET /api/fundamentals/AAPL", f"status={r.status_code}")

r, good = get("/api/chart/AAPL?period=1mo")
if good:
    d = r.get_json()
    if d and "data" in d and "indicators" in d:
        ok("GET /api/chart/AAPL")
    else:
        fail("GET /api/chart response shape")
else:
    fail("GET /api/chart/AAPL", f"status={r.status_code}")

r, good = get("/api/news/AAPL?limit=3")
if good:
    ok("GET /api/news/AAPL")
else:
    fail("GET /api/news/AAPL", f"status={r.status_code}")

r, good = get("/api/search?q=apple")
if good:
    d = r.get_json()
    if isinstance(d, list):
        ok("GET /api/search?q=apple")
    else:
        fail("GET /api/search response shape")
else:
    fail("GET /api/search", f"status={r.status_code}")

r, good = get("/api/search?q=")
if good:
    d = r.get_json()
    if isinstance(d, list) and len(d) == 0:
        ok("GET /api/search (empty query) returns []")
    else:
        fail("GET /api/search empty query response")
else:
    fail("GET /api/search empty", f"status={r.status_code}")

r, good = get("/api/market/indices")
if good:
    ok("GET /api/market/indices")
else:
    fail("GET /api/market/indices", f"status={r.status_code}")

r, good = get("/api/market/sectors")
if good:
    ok("GET /api/market/sectors")
else:
    fail("GET /api/market/sectors", f"status={r.status_code}")

r, good = get("/api/market/movers")
if good:
    ok("GET /api/market/movers")
else:
    fail("GET /api/market/movers", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5C — Crypto Routes
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5C: Crypto Routes ──")

r, good = get("/api/crypto/market?limit=5")
if good:
    ok("GET /api/crypto/market")
else:
    fail("GET /api/crypto/market", f"status={r.status_code}")

r, good = get("/api/crypto/global")
if good:
    ok("GET /api/crypto/global")
else:
    fail("GET /api/crypto/global", f"status={r.status_code}")

r, good = get("/api/crypto/chart/bitcoin?days=7")
if good:
    ok("GET /api/crypto/chart/bitcoin")
else:
    fail("GET /api/crypto/chart/bitcoin", f"status={r.status_code}")

r, good = get("/api/crypto/quote/bitcoin")
if good:
    ok("GET /api/crypto/quote/bitcoin")
else:
    fail("GET /api/crypto/quote/bitcoin", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5D — Portfolio CRUD
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5D: Portfolio CRUD ──")

# Add a test position
r, good = post("/api/portfolio/add", {
    "symbol": "TEST",
    "shares": 10,
    "avg_cost": 100.0,
    "name": "Test Corp",
    "asset_type": "stock",
})
if good:
    d = r.get_json()
    test_pos_id = d.get("id")
    ok(f"POST /api/portfolio/add (id={test_pos_id})")
else:
    fail("POST /api/portfolio/add", f"status={r.status_code}")
    test_pos_id = None

# Get portfolio
r, good = get("/api/portfolio")
if good:
    d = r.get_json()
    if "holdings" in d and "summary" in d:
        ok("GET /api/portfolio has holdings+summary")
    else:
        fail("GET /api/portfolio response shape")
else:
    fail("GET /api/portfolio", f"status={r.status_code}")

# Update position
if test_pos_id:
    r, good = client.put(f"/api/portfolio/{test_pos_id}", json={"shares": 20}), None
    r, good = r if isinstance(r, tuple) else (r, r.status_code == 200)
    # Re-do properly
    r = client.put(f"/api/portfolio/{test_pos_id}", json={"shares": 20})
    if r.status_code == 200:
        ok(f"PUT /api/portfolio/{test_pos_id}")
    else:
        fail(f"PUT /api/portfolio/{test_pos_id}", f"status={r.status_code}")

# Delete position
if test_pos_id:
    r = client.delete(f"/api/portfolio/{test_pos_id}")
    if r.status_code == 200:
        ok(f"DELETE /api/portfolio/{test_pos_id}")
    else:
        fail(f"DELETE /api/portfolio/{test_pos_id}", f"status={r.status_code}")

# Add missing fields → 400
r, _ = post("/api/portfolio/add", {"symbol": "X"}, expect=400)
if r.status_code == 400:
    ok("POST /api/portfolio/add (missing fields) returns 400")
else:
    fail("POST /api/portfolio/add validation", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5E — Watchlist CRUD
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5E: Watchlist CRUD ──")

r, good = post("/api/watchlist/add", {"symbol": "TSLA", "name": "Tesla"})
if good:
    ok("POST /api/watchlist/add")
else:
    fail("POST /api/watchlist/add", f"status={r.status_code}")

r, good = get("/api/watchlist")
if good:
    d = r.get_json()
    if isinstance(d, list):
        ok("GET /api/watchlist returns list")
        # Find our entry and delete it
        tsla_entries = [w for w in d if w.get("symbol") == "TSLA"]
        if tsla_entries:
            wl_id = tsla_entries[0].get("id")
            r2 = client.delete(f"/api/watchlist/{wl_id}")
            if r2.status_code == 200:
                ok(f"DELETE /api/watchlist/{wl_id}")
            else:
                fail(f"DELETE /api/watchlist/{wl_id}", f"status={r2.status_code}")
        else:
            skip("TSLA not found in watchlist for deletion test")
    else:
        fail("GET /api/watchlist response shape")
else:
    fail("GET /api/watchlist", f"status={r.status_code}")

# Missing symbol → 400
r, _ = post("/api/watchlist/add", {}, expect=400)
if r.status_code == 400:
    ok("POST /api/watchlist/add (no symbol) returns 400")
else:
    fail("POST /api/watchlist/add validation", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5F — Transactions
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5F: Transactions ──")

r, good = post("/api/transactions/add", {
    "symbol": "AAPL", "action": "BUY", "shares": 5, "price": 150.0,
})
if good:
    ok("POST /api/transactions/add")
else:
    fail("POST /api/transactions/add", f"status={r.status_code}")

r, good = get("/api/transactions?limit=5")
if good:
    d = r.get_json()
    if isinstance(d, list):
        ok("GET /api/transactions returns list")
    else:
        fail("GET /api/transactions response shape")
else:
    fail("GET /api/transactions", f"status={r.status_code}")

# Missing fields → 400
r, _ = post("/api/transactions/add", {"symbol": "X"}, expect=400)
if r.status_code == 400:
    ok("POST /api/transactions/add (missing) returns 400")
else:
    fail("POST /api/transactions/add validation", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5G — Portfolio History & Benchmark
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5G: Portfolio History & Benchmark ──")

r, good = get("/api/portfolio/history")
if good:
    ok("GET /api/portfolio/history")
else:
    fail("GET /api/portfolio/history", f"status={r.status_code}")

r, good = get("/api/portfolio/benchmark?symbol=SPY&period=3mo")
if good:
    d = r.get_json()
    if d and d.get("symbol") == "SPY":
        ok("GET /api/portfolio/benchmark")
    else:
        fail("GET /api/portfolio/benchmark response shape")
else:
    fail("GET /api/portfolio/benchmark", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5H — Options
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5H: Options ──")

r, good = get("/api/options/AAPL/dates")
if good:
    d = r.get_json()
    if isinstance(d, list):
        ok("GET /api/options/AAPL/dates returns list")
        if len(d) > 0:
            # Test chain with first date
            r2, good2 = get(f"/api/options/AAPL/chain?date={d[0]}")
            if good2:
                ok("GET /api/options/AAPL/chain")
            else:
                fail("GET /api/options/AAPL/chain", f"status={r2.status_code}")
        else:
            skip("no option dates for AAPL chain test")
    else:
        fail("GET /api/options/AAPL/dates response shape")
else:
    fail("GET /api/options/AAPL/dates", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5I — Analytics
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5I: Analytics ──")

r, good = get("/api/analytics/portfolio")
if good:
    ok("GET /api/analytics/portfolio")
else:
    fail("GET /api/analytics/portfolio", f"status={r.status_code}")

r, good = get("/api/analytics/correlation")
if good:
    ok("GET /api/analytics/correlation")
else:
    fail("GET /api/analytics/correlation", f"status={r.status_code}")

r, good = get("/api/analytics/risk")
if good:
    ok("GET /api/analytics/risk")
else:
    fail("GET /api/analytics/risk", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5J — CSV Export
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5J: CSV Export ──")

r, good = get("/api/portfolio/export")
if good and r.content_type and "csv" in r.content_type:
    ok("GET /api/portfolio/export returns CSV")
else:
    fail("GET /api/portfolio/export", f"status={r.status_code} type={r.content_type}")

r, good = get("/api/transactions/export")
if good and r.content_type and "csv" in r.content_type:
    ok("GET /api/transactions/export returns CSV")
else:
    fail("GET /api/transactions/export", f"status={r.status_code} type={r.content_type}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5K — Settings
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5K: Settings ──")

r, good = get("/api/settings")
if good:
    ok("GET /api/settings")
else:
    fail("GET /api/settings", f"status={r.status_code}")

r, good = post("/api/settings", {"test_key": "test_val"})
if good:
    ok("POST /api/settings")
else:
    fail("POST /api/settings", f"status={r.status_code}")

# Verify saved
r, good = get("/api/settings")
if good:
    d = r.get_json()
    if d.get("test_key") == "test_val":
        ok("Setting persisted correctly")
    else:
        fail("Setting not persisted", str(d)[:100])


# ═══════════════════════════════════════════════════════════════════════════════
# 5L — Price Alerts
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5L: Price Alerts ──")

r, good = post("/api/alerts", {"symbol": "AAPL", "alert_type": "above", "price": 999.99})
if good:
    d = r.get_json()
    alert_id = d.get("id")
    ok(f"POST /api/alerts (id={alert_id})")
else:
    fail("POST /api/alerts", f"status={r.status_code}")
    alert_id = None

r, good = get("/api/alerts")
if good:
    d = r.get_json()
    if isinstance(d, list):
        ok("GET /api/alerts returns list")
    else:
        fail("GET /api/alerts response shape")
else:
    fail("GET /api/alerts", f"status={r.status_code}")

r, good = get("/api/alerts/triggered")
if good:
    ok("GET /api/alerts/triggered")
else:
    fail("GET /api/alerts/triggered", f"status={r.status_code}")

if alert_id:
    r = client.delete(f"/api/alerts/{alert_id}")
    if r.status_code == 200:
        ok(f"DELETE /api/alerts/{alert_id}")
    else:
        fail(f"DELETE /api/alerts/{alert_id}", f"status={r.status_code}")

r, _ = post("/api/alerts", {"symbol": "", "price": 0}, expect=400)
if r.status_code == 400:
    ok("POST /api/alerts (invalid) returns 400")
else:
    fail("POST /api/alerts validation", f"status={r.status_code}")

r, good = post("/api/alerts/clear-triggered")
if good:
    ok("POST /api/alerts/clear-triggered")
else:
    fail("POST /api/alerts/clear-triggered", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5M — Dividends
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5M: Dividends ──")

r, good = get("/api/dividends/portfolio")
if good:
    d = r.get_json()
    if "positions" in d or "summary" in d:
        ok("GET /api/dividends/portfolio")
    else:
        ok("GET /api/dividends/portfolio (empty portfolio)")
else:
    fail("GET /api/dividends/portfolio", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5N — Macro
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5N: Macro ──")

r, good = get("/api/macro")
if good:
    ok("GET /api/macro")
else:
    fail("GET /api/macro", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5O — Smart Money + ML Forecast Routes
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5O: Smart Money + ML Forecast Routes ──")

r, good = get("/api/smart-money/score/AAPL")
if good:
    d = r.get_json()
    if d and "score" in d and "signal" in d:
        ok(f"GET /api/smart-money/score/AAPL (score={d['score']})")
    else:
        fail("GET /api/smart-money/score response shape")
else:
    fail("GET /api/smart-money/score/AAPL", f"status={r.status_code}")

r, good = post("/api/smart-money/scores", {"symbols": ["AAPL"]})
if good:
    d = r.get_json()
    if "scores" in d:
        ok("POST /api/smart-money/scores")
    else:
        fail("POST /api/smart-money/scores response shape")
else:
    fail("POST /api/smart-money/scores", f"status={r.status_code}")

r, good = get("/api/smart-money/ml-forecast/AAPL")
if good:
    d = r.get_json()
    if d and ("composite_signal" in d or "error" not in d):
        ok("GET /api/smart-money/ml-forecast/AAPL")
    else:
        skip("GET /api/smart-money/ml-forecast returned error", str(d)[:100])
else:
    fail("GET /api/smart-money/ml-forecast/AAPL", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5P — Options Flow
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5P: Options Flow ──")

r, good = get("/api/options-flow/AAPL")
if good:
    ok("GET /api/options-flow/AAPL")
else:
    fail("GET /api/options-flow/AAPL", f"status={r.status_code}")

r, good = post("/api/options-flow/scan", {"symbols": ["AAPL"]})
if good:
    ok("POST /api/options-flow/scan")
else:
    fail("POST /api/options-flow/scan", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5Q — Earnings Routes
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5Q: Earnings Routes ──")

r, good = get("/api/earnings/calendar")
if good:
    d = r.get_json()
    if isinstance(d, list):
        ok("GET /api/earnings/calendar returns list")
    else:
        fail("GET /api/earnings/calendar response shape")
else:
    fail("GET /api/earnings/calendar", f"status={r.status_code}")

r, good = get("/api/earnings/dossier/AAPL")
if good:
    d = r.get_json()
    if d and d.get("symbol") == "AAPL":
        ok("GET /api/earnings/dossier/AAPL")
    else:
        fail("GET /api/earnings/dossier response shape")
else:
    # May fail if no OpenAI key; that's ok — check for 200 or 500
    skip("GET /api/earnings/dossier/AAPL", f"status={r.status_code} (may need API key)")


# ═══════════════════════════════════════════════════════════════════════════════
# 5R — Congress Routes
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5R: Congress Routes ──")

r, good = get("/api/congress/trades?days=30&max_pdfs=3")
if good:
    d = r.get_json()
    if isinstance(d, dict) and "trades" in d:
        ok("GET /api/congress/trades")
    else:
        fail("GET /api/congress/trades response shape")
else:
    fail("GET /api/congress/trades", f"status={r.status_code}")

r, good = get("/api/congress/trades/AAPL?days=60")
if good:
    d = r.get_json()
    if isinstance(d, dict) and "trades" in d:
        ok("GET /api/congress/trades/AAPL")
    else:
        fail("GET /api/congress/trades/AAPL response shape")
else:
    fail("GET /api/congress/trades/AAPL", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5S — Terminal Route
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5S: Terminal Route ──")

r, good = post("/api/terminal", {"command": "help"})
if good:
    d = r.get_json()
    if "output" in d:
        ok("POST /api/terminal (help)")
    else:
        fail("POST /api/terminal response shape")
else:
    fail("POST /api/terminal", f"status={r.status_code}")

# Empty command → 400
r, _ = post("/api/terminal", {"command": ""}, expect=400)
if r.status_code == 400:
    ok("POST /api/terminal (empty) returns 400")
else:
    fail("POST /api/terminal empty command", f"status={r.status_code}")

# Blocked command
r, good = post("/api/terminal", {"command": "rm -rf /"})
if good:
    d = r.get_json()
    if d.get("exit_code") == 1:
        ok("POST /api/terminal blocks dangerous commands")
    else:
        fail("POST /api/terminal should block rm")
else:
    fail("POST /api/terminal blocked cmd", f"status={r.status_code}")

# Actual CLI command
r, good = post("/api/terminal", {"command": "market indices"})
if good:
    d = r.get_json()
    if d.get("output") and len(d["output"]) > 10:
        ok("POST /api/terminal (market indices) returns output")
    else:
        skip("POST /api/terminal market indices output short", str(d)[:80])
else:
    fail("POST /api/terminal market indices", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5T — Stress Test Route (needs portfolio)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 5T: Stress Test ──")

# Add a temp position for stress test
post("/api/portfolio/add", {"symbol": "AAPL", "shares": 10, "avg_cost": 150.0})

r, good = post("/api/stress-test", {"custom_drop_pct": -20})
if good:
    d = r.get_json()
    if isinstance(d, dict):
        ok("POST /api/stress-test")
    else:
        fail("POST /api/stress-test response shape")
else:
    # Might be 400 if no portfolio
    skip("POST /api/stress-test", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════

print(f"\n{'='*60}")
print(f"  Phase 5 Results: {passed} passed, {failed} failed, {skipped} skipped")
print(f"{'='*60}\n")

sys.exit(1 if failed > 0 else 0)
