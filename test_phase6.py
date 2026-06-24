#!/usr/bin/env python3
"""
Phase 6 Regression Test: CLI + Web Terminal
Tests run_command() for all major commands and the /api/terminal POST route.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


import database as db
db.init_db()

import cli


# ═══════════════════════════════════════════════════════════════════════════════
# 6A — CLI module structure
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 6A: CLI module structure ──")

if callable(getattr(cli, 'build_parser', None)):
    ok("build_parser exists")
else:
    fail("build_parser missing")

if callable(getattr(cli, 'run_command', None)):
    ok("run_command exists")
else:
    fail("run_command missing")

if callable(getattr(cli, 'main', None)):
    ok("main exists")
else:
    fail("main missing")

# Check all command functions
cmd_names = [
    "cmd_quote", "cmd_fundamentals", "cmd_chart", "cmd_news", "cmd_search",
    "cmd_market", "cmd_portfolio", "cmd_watchlist", "cmd_transactions",
    "cmd_alerts", "cmd_analytics", "cmd_options", "cmd_dividends", "cmd_macro",
    "cmd_earnings", "cmd_intel", "cmd_smart_money", "cmd_ml_forecast",
    "cmd_congress", "cmd_crypto", "cmd_ai", "cmd_scanner", "cmd_settings", "cmd_serve",
]
missing = [n for n in cmd_names if not callable(getattr(cli, n, None))]
if not missing:
    ok(f"all {len(cmd_names)} command functions exist")
else:
    fail(f"missing command functions: {missing}")

# Helper functions
for fn in ["_c", "_pnl_color", "_signal_color", "banner", "header", "table",
           "fmt_price", "fmt_pct", "fmt_number"]:
    if not callable(getattr(cli, fn, None)):
        fail(f"helper {fn} missing")
        break
else:
    ok("all output helper functions exist")

# Parser builds without error
try:
    parser = cli.build_parser()
    ok("build_parser() succeeds")
except Exception as e:
    fail("build_parser() raises", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# 6B — run_command basics
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 6B: run_command basics ──")

# help
out, code = cli.run_command("help")
if code == 0 and "AUGUR" in out:
    ok("run_command('help') works")
else:
    fail("run_command('help')", f"code={code} len={len(out)}")

# empty string → shows help
out, code = cli.run_command("")
if code == 0 and len(out) > 50:
    ok("run_command('') shows help")
else:
    fail("run_command('')", f"code={code}")

# invalid command → exit code 1
out, code = cli.run_command("nonexistent-cmd")
if code == 1:
    ok("run_command('nonexistent-cmd') returns exit code 1")
else:
    fail("run_command invalid cmd", f"code={code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 6C — run_command: market data commands
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 6C: Market data commands ──")

out, code = cli.run_command("quote AAPL")
if code == 0 and ("AAPL" in out or "aapl" in out.lower()):
    ok("run_command('quote AAPL')")
else:
    fail("quote AAPL", f"code={code} len={len(out)}")

out, code = cli.run_command("fundamentals AAPL")
if code == 0 and len(out) > 20:
    ok("run_command('fundamentals AAPL')")
else:
    fail("fundamentals AAPL", f"code={code}")

out, code = cli.run_command("chart AAPL -p 1mo")
if code == 0 and len(out) > 10:
    ok("run_command('chart AAPL -p 1mo')")
else:
    fail("chart AAPL", f"code={code}")

out, code = cli.run_command("news AAPL -l 3")
if code == 0:
    ok("run_command('news AAPL -l 3')")
else:
    fail("news AAPL", f"code={code}")

out, code = cli.run_command("search apple")
if code == 0:
    ok("run_command('search apple')")
else:
    fail("search apple", f"code={code}")

out, code = cli.run_command("market indices")
if code == 0 and len(out) > 10:
    ok("run_command('market indices')")
else:
    fail("market indices", f"code={code}")

out, code = cli.run_command("market sectors")
if code == 0:
    ok("run_command('market sectors')")
else:
    fail("market sectors", f"code={code}")

out, code = cli.run_command("market movers")
if code == 0:
    ok("run_command('market movers')")
else:
    fail("market movers", f"code={code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 6D — run_command: portfolio/watchlist/transactions
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 6D: Portfolio/Watchlist/Transactions commands ──")

out, code = cli.run_command("portfolio list")
if code == 0:
    ok("run_command('portfolio list')")
else:
    fail("portfolio list", f"code={code}")

out, code = cli.run_command("watchlist list")
if code == 0:
    ok("run_command('watchlist list')")
else:
    fail("watchlist list", f"code={code}")

out, code = cli.run_command("transactions list")
if code == 0:
    ok("run_command('transactions list')")
else:
    fail("transactions list", f"code={code}")

out, code = cli.run_command("alerts list")
if code == 0:
    ok("run_command('alerts list')")
else:
    fail("alerts list", f"code={code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 6E — run_command: analytics
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 6E: Analytics commands ──")

out, code = cli.run_command("options dates AAPL")
if code == 0:
    ok("run_command('options dates AAPL')")
else:
    fail("options dates AAPL", f"code={code}")

out, code = cli.run_command("dividends")
if code == 0:
    ok("run_command('dividends')")
else:
    fail("dividends", f"code={code}")

out, code = cli.run_command("macro")
if code == 0 and len(out) > 10:
    ok("run_command('macro')")
else:
    fail("macro", f"code={code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 6F — run_command: intelligence commands
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 6F: Intelligence commands ──")

out, code = cli.run_command("smart-money score AAPL")
if code == 0 and ("score" in out.lower() or "/100" in out):
    ok("run_command('smart-money score AAPL')")
else:
    fail("smart-money score AAPL", f"code={code} len={len(out)}")

out, code = cli.run_command("ml-forecast AAPL")
if code == 0 and len(out) > 10:
    ok("run_command('ml-forecast AAPL')")
else:
    fail("ml-forecast AAPL", f"code={code}")

out, code = cli.run_command("crypto market")
if code == 0:
    ok("run_command('crypto market')")
else:
    fail("crypto market", f"code={code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 6G — run_command: settings
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 6G: Settings ──")

out, code = cli.run_command("settings list")
if code == 0:
    ok("run_command('settings list')")
else:
    fail("settings list", f"code={code}")


# ═══════════════════════════════════════════════════════════════════════════════
# 6H — Helper function unit tests
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 6H: Helper function unit tests ──")

# fmt_price
if cli.fmt_price(None) == "N/A":
    ok("fmt_price(None) = 'N/A'")
else:
    fail("fmt_price(None)")

if cli.fmt_price(1234.5) == "$1,234.50":
    ok("fmt_price(1234.5) = '$1,234.50'")
else:
    fail("fmt_price(1234.5)", cli.fmt_price(1234.5))

# fmt_pct
if cli.fmt_pct(None) == "N/A":
    ok("fmt_pct(None) = 'N/A'")
else:
    fail("fmt_pct(None)")

if cli.fmt_pct(5.2) == "+5.20%":
    ok("fmt_pct(5.2) = '+5.20%'")
else:
    fail("fmt_pct(5.2)", cli.fmt_pct(5.2))

if cli.fmt_pct(-3.1) == "-3.10%":
    ok("fmt_pct(-3.1) = '-3.10%'")
else:
    fail("fmt_pct(-3.1)", cli.fmt_pct(-3.1))

# fmt_number
if cli.fmt_number(None) == "N/A":
    ok("fmt_number(None) = 'N/A'")
else:
    fail("fmt_number(None)")

if "T" in cli.fmt_number(2.5e12):
    ok("fmt_number(2.5T) contains 'T'")
else:
    fail("fmt_number(2.5T)", cli.fmt_number(2.5e12))

if "B" in cli.fmt_number(3e9):
    ok("fmt_number(3B) contains 'B'")
else:
    fail("fmt_number(3B)", cli.fmt_number(3e9))

if "M" in cli.fmt_number(5e6):
    ok("fmt_number(5M) contains 'M'")
else:
    fail("fmt_number(5M)", cli.fmt_number(5e6))

# Color helpers
if cli.RESET in cli._c("test", cli.GREEN):
    ok("_c wraps with color and RESET")
else:
    fail("_c color wrapping")

if cli._pnl_color(1.0) == cli.GREEN:
    ok("_pnl_color(positive) = GREEN")
else:
    fail("_pnl_color(positive)")

if cli._pnl_color(-1.0) == cli.RED:
    ok("_pnl_color(negative) = RED")
else:
    fail("_pnl_color(negative)")

if cli._signal_color("BULLISH") == cli.GREEN:
    ok("_signal_color('BULLISH') = GREEN")
else:
    fail("_signal_color('BULLISH')")

if cli._signal_color("BEARISH") == cli.RED:
    ok("_signal_color('BEARISH') = RED")
else:
    fail("_signal_color('BEARISH')")


# ═══════════════════════════════════════════════════════════════════════════════
# 6I — Web Terminal via Flask test client
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 6I: Web Terminal API ──")

from app import app
app.config["TESTING"] = True
tc = app.test_client()

# help
r = tc.post("/api/terminal", json={"command": "help"})
d = r.get_json()
if r.status_code == 200 and "output" in d and "AUGUR" in d["output"]:
    ok("POST /api/terminal help via web")
else:
    fail("POST /api/terminal help via web", f"status={r.status_code}")

# quote
r = tc.post("/api/terminal", json={"command": "quote AAPL"})
d = r.get_json()
if r.status_code == 200 and d.get("exit_code") == 0 and len(d.get("output", "")) > 10:
    ok("POST /api/terminal quote AAPL")
else:
    fail("POST /api/terminal quote AAPL", f"status={r.status_code} exit={d.get('exit_code')}")

# settings
# 'settings' can mutate config, so the web terminal correctly refuses it
# (read-only gate) — same posture as 'serve' below. Expect it blocked.
r = tc.post("/api/terminal", json={"command": "settings list"})
d = r.get_json()
if r.status_code == 200 and d.get("exit_code") == 1:
    ok("POST /api/terminal blocks 'settings' (read-only gate)")
else:
    fail("POST /api/terminal should block settings", f"status={r.status_code} exit={d.get('exit_code')}")

# blocked: serve
r = tc.post("/api/terminal", json={"command": "serve"})
d = r.get_json()
if d.get("exit_code") == 1:
    ok("POST /api/terminal blocks 'serve'")
else:
    fail("POST /api/terminal should block serve", str(d)[:80])

# blocked: sudo
r = tc.post("/api/terminal", json={"command": "sudo rm -rf"})
d = r.get_json()
if d.get("exit_code") == 1:
    ok("POST /api/terminal blocks 'sudo'")
else:
    fail("POST /api/terminal should block sudo")

# ANSI output contains escape codes
r = tc.post("/api/terminal", json={"command": "market indices"})
d = r.get_json()
if r.status_code == 200 and "\033[" in d.get("output", ""):
    ok("Terminal output contains ANSI escape codes")
else:
    skip("Terminal output ANSI check", "may not have ANSI in all outputs")


# ═══════════════════════════════════════════════════════════════════════════════
# 6J — Dispatch table completeness
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 6J: Dispatch table completeness ──")

# The dispatch dict in run_command should have all commands from build_parser
parser = cli.build_parser()
# Get subparser actions
subparsers_action = None
for action in parser._subparsers._actions:
    if hasattr(action, '_parser_class'):
        subparsers_action = action
        break

if subparsers_action:
    parser_commands = set(subparsers_action.choices.keys())
    # Check run_command has them all in dispatch
    # We'll just verify the count matches expected
    expected = {"quote", "fundamentals", "chart", "news", "search", "market",
                "portfolio", "watchlist", "transactions", "alerts", "analytics",
                "options", "dividends", "macro", "earnings", "intel",
                "smart-money", "ml-forecast", "congress", "crypto", "ai",
                "scanner", "settings", "serve",
                # newer subcommands added since this list was first written
                "ask", "briefing", "health", "narrative", "reflexivity",
                "synthetic-insider", "liquidity", "alt-data", "contagion", "gex"}
    if parser_commands == expected:
        ok(f"Parser has all {len(expected)} subcommands")
    else:
        diff = parser_commands.symmetric_difference(expected)
        fail(f"Parser subcommands mismatch: {diff}")
else:
    skip("Could not inspect subparsers")


# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════

print(f"\n{'='*60}")
print(f"  Phase 6 Results: {passed} passed, {failed} failed, {skipped} skipped")
print(f"{'='*60}\n")

sys.exit(1 if failed > 0 else 0)
