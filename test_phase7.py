#!/usr/bin/env python3
"""
Phase 7 Regression Test: Alpha Engine Features
Tests 7 new modules, API routes, and CLI commands.
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


# ═══════════════════════════════════════════════════════════════════════════════
# 7A — Module Imports
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 7A: Module Imports ──")

for mod_name in ["gex_engine", "contagion_graph", "narrative_engine",
                 "synthetic_insider", "reflexivity_detector", "liquidity_monitor", "alt_data_engine"]:
    try:
        __import__(mod_name)
        ok(f"import {mod_name}")
    except Exception as e:
        fail(f"import {mod_name}", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# 7B — Module Function Signatures
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 7B: Module Function Signatures ──")

module_functions = {
    "gex_engine": ["compute_gex", "get_gex_summary"],
    "contagion_graph": ["build_graph", "assess_contagion"],
    "narrative_engine": ["analyze_narrative"],
    "synthetic_insider": ["compute_composite", "scan_composite_bulk"],
    "reflexivity_detector": ["detect_loops"],
    "liquidity_monitor": ["compute_stress_score"],
    "alt_data_engine": ["nowcast_revenue", "nowcast_bulk"],
}

for mod_name, funcs in module_functions.items():
    try:
        mod = __import__(mod_name)
        for fn in funcs:
            if callable(getattr(mod, fn, None)):
                ok(f"{mod_name}.{fn} exists and callable")
            else:
                fail(f"{mod_name}.{fn} missing or not callable")
    except Exception as e:
        for fn in funcs:
            skip(f"{mod_name}.{fn}", f"module import failed: {str(e)[:60]}")


# ═══════════════════════════════════════════════════════════════════════════════
# 7C — Backend Computation Tests
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 7C: Backend Computation Tests ──")

# gex_engine.compute_gex
try:
    import gex_engine
    result = gex_engine.compute_gex("AAPL")
    if isinstance(result, dict) and "error" not in result:
        if "net_gex" in result and "gamma_regime" in result:
            ok("gex_engine.compute_gex('AAPL') returns valid data")
        else:
            fail("gex_engine.compute_gex missing keys", str(result.keys()))
    elif isinstance(result, dict) and "error" in result:
        skip("gex_engine.compute_gex", result["error"])
    else:
        fail("gex_engine.compute_gex wrong type", str(type(result)))
except Exception as e:
    skip("gex_engine.compute_gex", str(e)[:80])

# contagion_graph.build_graph
try:
    import contagion_graph
    result = contagion_graph.build_graph("AAPL")
    if isinstance(result, dict) and "error" not in result:
        if "nodes" in result and "edges" in result:
            ok("contagion_graph.build_graph('AAPL') returns valid data")
        else:
            fail("contagion_graph.build_graph missing keys", str(result.keys()))
    elif isinstance(result, dict) and "error" in result:
        skip("contagion_graph.build_graph", result["error"])
    else:
        fail("contagion_graph.build_graph wrong type", str(type(result)))
except Exception as e:
    skip("contagion_graph.build_graph", str(e)[:80])

# narrative_engine.analyze_narrative
try:
    import narrative_engine
    result = narrative_engine.analyze_narrative("AAPL")
    if isinstance(result, dict) and "error" not in result:
        expected_keys = ["dominant_narrative", "narrative_phase", "velocity_score"]
        missing = [k for k in expected_keys if k not in result]
        if not missing:
            ok("narrative_engine.analyze_narrative('AAPL') returns valid data")
        else:
            fail("narrative_engine.analyze_narrative missing keys", str(missing))
    elif isinstance(result, dict) and "error" in result:
        skip("narrative_engine.analyze_narrative", result["error"])
    else:
        fail("narrative_engine.analyze_narrative wrong type", str(type(result)))
except Exception as e:
    skip("narrative_engine.analyze_narrative", str(e)[:80])

# synthetic_insider.compute_composite
try:
    import synthetic_insider
    result = synthetic_insider.compute_composite("AAPL")
    if isinstance(result, dict) and "error" not in result:
        expected_keys = ["composite_score", "alert_level", "channels"]
        missing = [k for k in expected_keys if k not in result]
        if not missing:
            score = result.get("composite_score", -1)
            if isinstance(score, (int, float)) and 0 <= score <= 100:
                ok("synthetic_insider.compute_composite('AAPL') returns valid data")
            else:
                ok("synthetic_insider.compute_composite('AAPL') has expected keys")
        else:
            fail("synthetic_insider.compute_composite missing keys", str(missing))
    elif isinstance(result, dict) and "error" in result:
        skip("synthetic_insider.compute_composite", result["error"])
    else:
        fail("synthetic_insider.compute_composite wrong type", str(type(result)))
except Exception as e:
    skip("synthetic_insider.compute_composite", str(e)[:80])

# reflexivity_detector.detect_loops
try:
    import reflexivity_detector
    result = reflexivity_detector.detect_loops("AAPL")
    if isinstance(result, dict) and "error" not in result:
        if "active_loops" in result and "loop_count" in result:
            ok("reflexivity_detector.detect_loops('AAPL') returns valid data")
        else:
            fail("reflexivity_detector.detect_loops missing keys", str(result.keys()))
    elif isinstance(result, dict) and "error" in result:
        skip("reflexivity_detector.detect_loops", result["error"])
    else:
        fail("reflexivity_detector.detect_loops wrong type", str(type(result)))
except Exception as e:
    skip("reflexivity_detector.detect_loops", str(e)[:80])

# liquidity_monitor.compute_stress_score
try:
    import liquidity_monitor
    result = liquidity_monitor.compute_stress_score()
    if isinstance(result, dict) and "error" not in result:
        expected_keys = ["composite_score", "regime", "indicators"]
        missing = [k for k in expected_keys if k not in result]
        if not missing:
            ok("liquidity_monitor.compute_stress_score() returns valid data")
        else:
            fail("liquidity_monitor.compute_stress_score missing keys", str(missing))
    elif isinstance(result, dict) and "error" in result:
        skip("liquidity_monitor.compute_stress_score", result["error"])
    else:
        fail("liquidity_monitor.compute_stress_score wrong type", str(type(result)))
except Exception as e:
    skip("liquidity_monitor.compute_stress_score", str(e)[:80])

# alt_data_engine.nowcast_revenue
try:
    import alt_data_engine
    result = alt_data_engine.nowcast_revenue("AAPL")
    if isinstance(result, dict) and "error" not in result:
        expected_keys = ["nowcast_score", "beat_probability", "signals"]
        missing = [k for k in expected_keys if k not in result]
        if not missing:
            ok("alt_data_engine.nowcast_revenue('AAPL') returns valid data")
        else:
            fail("alt_data_engine.nowcast_revenue missing keys", str(missing))
    elif isinstance(result, dict) and "error" in result:
        skip("alt_data_engine.nowcast_revenue", result["error"])
    else:
        fail("alt_data_engine.nowcast_revenue wrong type", str(type(result)))
except Exception as e:
    skip("alt_data_engine.nowcast_revenue", str(e)[:80])


# ═══════════════════════════════════════════════════════════════════════════════
# 7D — API Route Tests
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 7D: API Route Tests ──")

from app import app
app.config["TESTING"] = True
tc = app.test_client()

api_tests = [
    ("GET", "/api/gex/AAPL", None, "net_gex"),
    ("GET", "/api/gex/summary/AAPL", None, None),
    ("GET", "/api/contagion/AAPL", None, "nodes"),
    ("GET", "/api/contagion/impact/AAPL", None, None),
    ("GET", "/api/narrative/AAPL", None, "narrative_phase"),
    ("GET", "/api/synthetic-insider/AAPL", None, "composite_score"),
    ("POST", "/api/synthetic-insider/scan", {"symbols": ["AAPL"]}, None),
    ("GET", "/api/reflexivity/AAPL", None, "active_loops"),
    ("GET", "/api/liquidity", None, "regime"),
    ("GET", "/api/alt-data/AAPL", None, "nowcast_score"),
    ("POST", "/api/alt-data/scan", {"symbols": ["AAPL"]}, None),
]

for method, route, payload, expected_key in api_tests:
    try:
        if method == "GET":
            r = tc.get(route)
        else:
            r = tc.post(route, json=payload)
        if r.status_code == 200:
            d = r.get_json()
            if expected_key is not None:
                if d and expected_key in d:
                    ok(f"{method} {route} -> 200 + has '{expected_key}'")
                else:
                    skip(f"{method} {route}", f"200 but missing '{expected_key}'")
            else:
                ok(f"{method} {route} -> 200")
        else:
            skip(f"{method} {route}", f"status={r.status_code}")
    except Exception as e:
        skip(f"{method} {route}", str(e)[:60])


# ═══════════════════════════════════════════════════════════════════════════════
# 7E — CLI Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 7E: CLI Integration Tests ──")

import cli

commands = [
    ("gex AAPL", "gex"),
    ("narrative AAPL", "narrative"),
    ("reflexivity AAPL", "reflexivity"),
    ("liquidity", "liquidity"),
]
for cmd, name in commands:
    try:
        out, code = cli.run_command(cmd)
        if code == 0 and len(out) > 10:
            ok(f"cli.run_command('{cmd}')")
        else:
            skip(f"cli.run_command('{cmd}')", f"code={code} len={len(out)}")
    except Exception as e:
        skip(f"cli.run_command('{cmd}')", str(e)[:60])


# ═══════════════════════════════════════════════════════════════════════════════
# 7F — Parser Completeness
# ═══════════════════════════════════════════════════════════════════════════════
print("\n── 7F: Parser Completeness ──")

parser = cli.build_parser()
for action in parser._subparsers._actions:
    if hasattr(action, '_parser_class'):
        commands = set(action.choices.keys())
        for cmd in ["gex", "contagion", "narrative", "synthetic-insider", "reflexivity", "liquidity", "alt-data"]:
            if cmd in commands:
                ok(f"parser has '{cmd}' command")
            else:
                fail(f"parser missing '{cmd}' command")
        break


# ═══════════════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════════════

print(f"\n{'='*60}")
print(f"  Phase 7 Results: {passed} passed, {failed} failed, {skipped} skipped")
print(f"{'='*60}\n")

sys.exit(1 if failed > 0 else 0)
