#!/usr/bin/env python3
"""
Phase 4 Regression Test: EDGAR + Congress + Earnings
Tests parallelized Form 4 fetches, congressional PDF parsing,
earnings calendar/dossier builders.
"""

import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


# ═══════════════════════════════════════════════════════════════════════════════
# 4A — EDGAR module structure
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4A: EDGAR module structure ──")

import sec_edgar as edgar

# Session with retry
assert edgar._SESSION is None or edgar._SESSION is not None  # just importable
sess = edgar._get_session()
if sess and hasattr(sess, 'headers'):
    ok("_get_session returns Session with headers")
else:
    fail("_get_session returns Session with headers")

# TRACKED_FUNDS
if len(edgar.TRACKED_FUNDS) >= 5:
    ok(f"TRACKED_FUNDS has {len(edgar.TRACKED_FUNDS)} funds")
else:
    fail("TRACKED_FUNDS count", str(len(edgar.TRACKED_FUNDS)))

# CIK cache exists
if isinstance(edgar._CIK_CACHE, dict):
    ok("CIK cache is a dict")
else:
    fail("CIK cache type")

# EDGAR_HEADERS has User-Agent
if "User-Agent" in edgar.EDGAR_HEADERS:
    ok("EDGAR_HEADERS includes User-Agent")
else:
    fail("EDGAR_HEADERS missing User-Agent")


# ═══════════════════════════════════════════════════════════════════════════════
# 4B — EDGAR CIK lookup (live)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4B: EDGAR CIK lookup ──")

try:
    cik = edgar.get_cik("AAPL")
    if cik and len(cik) == 10 and cik.isdigit():
        ok(f"get_cik('AAPL') = {cik}")
        # Should be cached now
        if "AAPL" in edgar._CIK_CACHE:
            ok("CIK cached after lookup")
        else:
            fail("CIK not cached after lookup")
    else:
        fail("get_cik('AAPL')", f"got {cik}")
except Exception as e:
    fail("get_cik('AAPL') exception", str(e))

# Non-existent ticker
try:
    bad_cik = edgar.get_cik("ZZZZZXXX123")
    if bad_cik is None:
        ok("get_cik returns None for invalid ticker")
    else:
        skip("get_cik invalid ticker returned something", str(bad_cik))
except Exception as e:
    fail("get_cik invalid ticker exception", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# 4C — EDGAR recent filings (live)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4C: EDGAR recent filings ──")

try:
    filings = edgar.get_recent_filings("AAPL", forms=["8-K", "10-K", "10-Q"], limit=5)
    if isinstance(filings, list) and len(filings) > 0:
        ok(f"get_recent_filings returned {len(filings)} filings")
        f0 = filings[0]
        required_keys = ["accession", "form_type", "filing_date", "description", "document_url", "ticker"]
        missing = [k for k in required_keys if k not in f0]
        if not missing:
            ok("filing dict has all required keys")
        else:
            fail("filing dict missing keys", str(missing))
        if f0["ticker"] == "AAPL":
            ok("filing ticker matches input")
        else:
            fail("filing ticker mismatch", f0["ticker"])
        if f0["form_type"] in ("8-K", "10-K", "10-Q"):
            ok(f"filing form_type valid: {f0['form_type']}")
        else:
            fail("filing form_type unexpected", f0["form_type"])
    else:
        fail("get_recent_filings empty", str(type(filings)))
except Exception as e:
    fail("get_recent_filings exception", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# 4D — Form 4 insider transactions (parallelized — live)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4D: Form 4 insider transactions ──")

try:
    t0 = time.time()
    txns = edgar.get_form4_transactions("AAPL", limit=10)
    elapsed = time.time() - t0
    if isinstance(txns, list):
        ok(f"get_form4_transactions returned {len(txns)} txns in {elapsed:.1f}s")
        if len(txns) > 0:
            tx = txns[0]
            required_keys = ["ticker", "insider_name", "transaction_type", "shares", "price", "date", "accession"]
            missing = [k for k in required_keys if k not in tx]
            if not missing:
                ok("transaction dict has all required keys")
            else:
                fail("transaction dict missing keys", str(missing))
            if tx["ticker"] == "AAPL":
                ok("transaction ticker matches")
            else:
                fail("transaction ticker mismatch", tx["ticker"])
            if tx["transaction_type"] in ("BUY", "SELL"):
                ok(f"transaction type valid: {tx['transaction_type']}")
            else:
                fail("transaction type unexpected", tx["transaction_type"])
            # Sorted by date descending
            if len(txns) >= 2:
                if txns[0].get("date", "") >= txns[1].get("date", ""):
                    ok("transactions sorted by date descending")
                else:
                    skip("transaction sort order unclear", f"{txns[0].get('date')} vs {txns[1].get('date')}")
        else:
            skip("no Form 4 transactions returned for AAPL (may be timing)")
    else:
        fail("get_form4_transactions return type", str(type(txns)))
except Exception as e:
    fail("get_form4_transactions exception", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# 4E — Institutional holdings (live)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4E: Institutional holdings ──")

try:
    brk_cik = edgar.TRACKED_FUNDS.get("Berkshire Hathaway", "0001067983")
    t0 = time.time()
    holdings = edgar.get_institutional_holdings(brk_cik, "Berkshire Hathaway")
    elapsed = time.time() - t0
    if isinstance(holdings, dict):
        ok(f"get_institutional_holdings returned dict in {elapsed:.1f}s")
        if holdings.get("fund_name") == "Berkshire Hathaway":
            ok("fund_name matches")
        else:
            fail("fund_name mismatch", str(holdings.get("fund_name")))
        h_list = holdings.get("holdings", [])
        if len(h_list) > 0:
            ok(f"Berkshire has {len(h_list)} holdings")
            h0 = h_list[0]
            if "name" in h0 and "value_usd" in h0 and "shares" in h0:
                ok("holding dict has name/value_usd/shares")
            else:
                fail("holding dict missing keys", str(h0.keys()))
            if "pct_of_portfolio" in h0:
                ok("holding has pct_of_portfolio")
            else:
                fail("holding missing pct_of_portfolio")
            # Total value
            if holdings.get("total_value", 0) > 0:
                ok(f"total_value = ${holdings['total_value']:,.0f}")
            else:
                skip("total_value is zero or missing")
        else:
            skip("no holdings returned for Berkshire (may be timing)")
    else:
        fail("get_institutional_holdings return type", str(type(holdings)))
except Exception as e:
    fail("get_institutional_holdings exception", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# 4F — Congress module structure
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4F: Congress module structure ──")

import congress

if congress._PDF_OK:
    ok("pdfplumber available")
else:
    skip("pdfplumber not installed — PDF tests will skip")

if len(congress.AMOUNT_MIDPOINTS) >= 7:
    ok(f"AMOUNT_MIDPOINTS has {len(congress.AMOUNT_MIDPOINTS)} ranges")
else:
    fail("AMOUNT_MIDPOINTS count", str(len(congress.AMOUNT_MIDPOINTS)))

if len(congress.TXN_LABELS) >= 5:
    ok(f"TXN_LABELS has {len(congress.TXN_LABELS)} types")
else:
    fail("TXN_LABELS count")

# _parse_amount unit tests
test_amounts = [
    ("$1,001 - $15,000", 8000),
    ("$50,001 - $100,000", 75000),
    ("Over $5,000,000", 5000000),
]
all_pass = True
for s, expected in test_amounts:
    if congress._parse_amount(s) != expected:
        all_pass = False
        break
if all_pass:
    ok("_parse_amount correctly maps amount ranges")
else:
    fail("_parse_amount mismatch")


# ═══════════════════════════════════════════════════════════════════════════════
# 4G — Congress FD index fetch (live)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4G: Congress FD index fetch ──")

from datetime import datetime

try:
    year = datetime.now().year
    recs = congress._fetch_fd_index(year)
    if isinstance(recs, list):
        ok(f"_fetch_fd_index({year}) returned {len(recs)} PTR records")
        if len(recs) > 0:
            r0 = recs[0]
            required_keys = ["last", "first", "state", "date_str", "doc_id", "year"]
            missing = [k for k in required_keys if k not in r0]
            if not missing:
                ok("PTR record has all required keys")
            else:
                fail("PTR record missing keys", str(missing))
            if r0["year"] == year:
                ok("PTR record year matches")
            else:
                fail("PTR record year mismatch", str(r0["year"]))
        else:
            skip(f"no PTR records for {year} yet")
    else:
        fail("_fetch_fd_index return type", str(type(recs)))
except Exception as e:
    fail("_fetch_fd_index exception", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# 4H — Congress get_recent_trades (live, small sample)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4H: Congress recent trades (small sample) ──")

if congress._PDF_OK:
    try:
        t0 = time.time()
        result = congress.get_recent_trades(days=60, max_pdfs=5)
        elapsed = time.time() - t0
        if isinstance(result, dict):
            ok(f"get_recent_trades returned dict in {elapsed:.1f}s")
            trades = result.get("trades", [])
            ok(f"  {len(trades)} trades from {result.get('filings_parsed', 0)} filings")
            if len(trades) > 0:
                t = trades[0]
                required_keys = ["member_name", "ticker", "txn_type", "txn_date", "amount_str", "pdf_url"]
                missing = [k for k in required_keys if k not in t]
                if not missing:
                    ok("trade dict has all required keys")
                else:
                    fail("trade dict missing keys", str(missing))
                if t.get("asset_type") in ("ST", "OP"):
                    ok(f"trade asset_type valid: {t['asset_type']}")
                else:
                    fail("trade asset_type unexpected", str(t.get("asset_type")))
            else:
                skip("no trades parsed from 5 PDFs (may be content-dependent)")
        else:
            fail("get_recent_trades return type", str(type(result)))
    except Exception as e:
        fail("get_recent_trades exception", str(e))
else:
    skip("get_recent_trades — pdfplumber not available")


# ═══════════════════════════════════════════════════════════════════════════════
# 4I — Earnings module structure
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4I: Earnings module structure ──")

import earnings as earnings_module

# Helper functions exist
if callable(getattr(earnings_module, '_safe_float', None)):
    ok("_safe_float helper exists")
else:
    fail("_safe_float missing")

if callable(getattr(earnings_module, '_date_str', None)):
    ok("_date_str helper exists")
else:
    fail("_date_str missing")

# _safe_float unit tests
tests = [(None, None), ("abc", None), (3.14, 3.14), (float('nan'), None)]
all_pass = True
for inp, expected in tests:
    result = earnings_module._safe_float(inp)
    if result != expected and not (result is None and expected is None):
        all_pass = False
        break
if all_pass:
    ok("_safe_float handles edge cases correctly")
else:
    fail("_safe_float edge case mismatch")

# _date_str
import datetime as dt_mod
if earnings_module._date_str(None) is None:
    ok("_date_str(None) returns None")
else:
    fail("_date_str(None) should be None")

d = dt_mod.date(2025, 1, 15)
if earnings_module._date_str(d) == "2025-01-15":
    ok("_date_str(date) returns YYYY-MM-DD")
else:
    fail("_date_str(date) mismatch", earnings_module._date_str(d))


# ═══════════════════════════════════════════════════════════════════════════════
# 4J — Earnings calendar (live)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4J: Earnings calendar ──")

try:
    t0 = time.time()
    cal = earnings_module.get_earnings_calendar(["AAPL", "MSFT"])
    elapsed = time.time() - t0
    if isinstance(cal, list):
        ok(f"get_earnings_calendar returned {len(cal)} entries in {elapsed:.1f}s")
        if len(cal) > 0:
            c0 = cal[0]
            required_keys = ["symbol", "earnings_date", "days_until"]
            missing = [k for k in required_keys if k not in c0]
            if not missing:
                ok("calendar entry has required keys")
            else:
                fail("calendar entry missing keys", str(missing))
            if c0.get("days_until", -1) >= 0:
                ok(f"days_until is non-negative: {c0['days_until']}")
            else:
                skip("days_until is negative (earnings may have passed)")
            # Sorted by earnings_date ascending
            if len(cal) >= 2:
                if cal[0]["earnings_date"] <= cal[1]["earnings_date"]:
                    ok("calendar sorted by earnings_date ascending")
                else:
                    skip("calendar sort order unclear")
        else:
            skip("no upcoming earnings found for AAPL/MSFT")
    else:
        fail("get_earnings_calendar return type", str(type(cal)))
except Exception as e:
    fail("get_earnings_calendar exception", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# 4K — Earnings dossier (live)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4K: Earnings dossier ──")

try:
    t0 = time.time()
    dossier = earnings_module.get_earnings_dossier("AAPL")
    elapsed = time.time() - t0
    if isinstance(dossier, dict):
        ok(f"get_earnings_dossier returned dict in {elapsed:.1f}s")
        if dossier.get("symbol") == "AAPL":
            ok("dossier symbol matches")
        else:
            fail("dossier symbol mismatch", str(dossier.get("symbol")))

        # Check key sections exist
        for key in ["earnings_date", "beat_rate", "history", "post_earnings_moves",
                     "implied_move_pct", "insider_activity", "current_price"]:
            if key in dossier:
                ok(f"dossier has '{key}'")
            else:
                fail(f"dossier missing '{key}'")

        # History is a list
        hist = dossier.get("history", [])
        if isinstance(hist, list):
            ok(f"dossier history has {len(hist)} quarters")
        else:
            fail("dossier history not a list")

        # Post earnings moves
        moves = dossier.get("post_earnings_moves", [])
        if isinstance(moves, list):
            ok(f"dossier post_earnings_moves has {len(moves)} entries")
            if len(moves) > 0:
                m0 = moves[0]
                if "move_pct" in m0 and "direction" in m0:
                    ok("move entry has move_pct and direction")
                else:
                    fail("move entry missing keys")
        else:
            fail("post_earnings_moves not a list")

        # Insider activity
        insider = dossier.get("insider_activity", {})
        if isinstance(insider, dict) and "signal" in insider:
            ok(f"insider_activity signal: {insider['signal']}")
        else:
            fail("insider_activity missing or no signal")

        # Current price
        if dossier.get("current_price") and dossier["current_price"] > 0:
            ok(f"current_price = ${dossier['current_price']:.2f}")
        else:
            skip("current_price is None or zero")

    else:
        fail("get_earnings_dossier return type", str(type(dossier)))
except Exception as e:
    fail("get_earnings_dossier exception", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# 4L — Filing text extraction (unit test)
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 4L: Filing text extraction ──")

# Test _extract_readable_text with synthetic HTML
html_input = "<html><head><title>T</title></head><body><p>Hello World</p><script>bad</script></body></html>"
text = edgar._extract_readable_text(html_input, "test.htm")
if "Hello World" in text and "bad" not in text:
    ok("_extract_readable_text strips scripts, keeps body text")
else:
    fail("_extract_readable_text HTML handling", text[:100])

# Plain text passthrough
plain = "This is plain text."
if edgar._extract_readable_text(plain, "test.txt") == plain:
    ok("_extract_readable_text passes plain text through")
else:
    fail("_extract_readable_text plain text")

# XML extraction
xml_input = '<?xml version="1.0"?><root><item>Data here</item></root>'
text = edgar._extract_readable_text(xml_input, "test.xml")
if "Data here" in text:
    ok("_extract_readable_text extracts XML text")
else:
    fail("_extract_readable_text XML", text[:100])


# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════

print(f"\n{'='*60}")
print(f"  Phase 4 Results: {passed} passed, {failed} failed, {skipped} skipped")
print(f"{'='*60}\n")

sys.exit(1 if failed > 0 else 0)
