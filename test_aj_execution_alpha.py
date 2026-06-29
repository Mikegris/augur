"""Offline tests for aj_execution_alpha — execution-alpha helpers.

Standalone runner (not pytest). No network, no LLM. Each helper is exercised
both OFF (default / no-op / fail-open) and ON with canned quotes/positions and
monkeypatched fetcher/earnings/gex_engine.

Run:  ./venv/bin/python test_aj_execution_alpha.py
"""
import os
import sys
import types
import tempfile
import datetime

# Harmless: the module under test does no DB I/O, but the suite convention is a
# throwaway DB path so nothing touches a real AUGUR db.
os.environ.setdefault("AUGUR_DB_PATH", tempfile.mktemp(suffix=".db"))

import aj_execution_alpha as X  # noqa: E402

PASS = []
FAIL = []


def ok(msg):
    PASS.append(msg)
    print("  [OK] " + msg)


def check(cond, msg):
    if cond:
        ok(msg)
    else:
        FAIL.append(msg)
        print("  [XX] " + msg)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ── 1. entry_price ────────────────────────────────────────────────────────

def test_entry_price():
    q = {"price": 100.0, "bid": 99.9, "ask": 100.1}  # mid = 100.0

    # OFF -> market
    r = X.entry_price("AAA", "buy", q, {})
    check(r["order_type"] == "market" and r["limit_price"] is None,
          "1 entry_price OFF -> market")

    # ON buy -> limit below mid
    cfg = {"limit_entry": True, "limit_entry_offset_bps": 10}
    rb = X.entry_price("AAA", "buy", q, cfg)
    check(rb["order_type"] == "limit" and rb["limit_price"] < 100.0,
          "1 entry_price ON buy -> limit below mid ({} < 100)".format(rb["limit_price"]))
    check(approx(rb["limit_price"], 100.0 * (1 - 10 / 10000.0), 1e-4),
          "1 entry_price buy limit == mid*(1-10bps)")

    # ON sell -> limit above mid (symmetric)
    rs = X.entry_price("AAA", "sell", q, cfg)
    check(rs["order_type"] == "limit" and rs["limit_price"] > 100.0,
          "1 entry_price ON sell -> limit above mid")

    # Bad quote with flag ON -> safe default market
    check(X.entry_price("AAA", "buy", {}, cfg)["order_type"] == "market",
          "1 entry_price bad quote -> market (fail-open)")
    check(X.entry_price("AAA", "buy", None, cfg)["order_type"] == "market",
          "1 entry_price None quote -> market (fail-open)")

    # No bid/ask -> mid falls back to last price
    rno = X.entry_price("AAA", "buy", {"price": 50.0}, cfg)
    check(rno["order_type"] == "limit" and rno["limit_price"] < 50.0,
          "1 entry_price uses last price when bid/ask absent")


# ── 2. cost_gate ──────────────────────────────────────────────────────────

def test_cost_gate():
    q = {"price": 100.0, "bid": 99.95, "ask": 100.05}  # spread = 0.10% of mid

    # OFF -> always ok
    check(X.cost_gate(0.001, "AAA", q, {})["ok"] is True,
          "2 cost_gate OFF -> ok even on tiny edge")

    cfg = {"cost_gate": True, "fee_bps": 0, "cost_edge_multiple": 1.5}
    # round_trip = 0.10% spread; required = 0.10 * 1.5 = 0.15%
    tiny = X.cost_gate(0.05, "AAA", q, cfg)
    check(tiny["ok"] is False, "2 cost_gate blocks tiny edge (0.05% < 0.15%)")
    check(approx(tiny["cost_pct"], 0.15, 1e-3),
          "2 cost_gate reports cost_pct ~0.15 ({})".format(tiny["cost_pct"]))

    big = X.cost_gate(5.0, "AAA", q, cfg)
    check(big["ok"] is True, "2 cost_gate passes large edge (5% >> 0.15%)")

    # Fees factor in: 50bps/leg -> +1.0% round trip
    cfg_fee = {"cost_gate": True, "fee_bps": 50, "cost_edge_multiple": 1.0}
    # required = (0.10 + 2*0.50) * 1.0 = 1.10%
    rf = X.cost_gate(1.0, "AAA", q, cfg_fee)
    check(rf["ok"] is False and approx(rf["cost_pct"], 1.10, 1e-3),
          "2 cost_gate folds fees into cost ({})".format(rf["cost_pct"]))

    # No bid/ask -> assumed_spread_bps used, still computes
    cfg_as = {"cost_gate": True, "assumed_spread_bps": 20, "cost_edge_multiple": 1.0}
    ra = X.cost_gate(0.10, "AAA", {"price": 100.0}, cfg_as)
    check(ra["ok"] is False and approx(ra["cost_pct"], 0.20, 1e-3),
          "2 cost_gate uses assumed_spread_bps when bid/ask absent ({})".format(ra["cost_pct"]))

    # Fail-open on garbage
    check(X.cost_gate("not-a-number", "AAA", q, cfg)["ok"] is True,
          "2 cost_gate bad edge -> ok (fail-open)")
    check(X.cost_gate(1.0, "AAA", None, cfg)["ok"] in (True, False),
          "2 cost_gate None quote does not raise")


# ── 3. time_stop ──────────────────────────────────────────────────────────

def test_time_stop():
    now = datetime.datetime(2026, 6, 20, tzinfo=datetime.timezone.utc)
    old = (now - datetime.timedelta(days=10)).isoformat()
    recent = (now - datetime.timedelta(days=2)).isoformat()

    # OFF -> no exit
    pos = {"opened_at": old, "avg_cost": 100.0, "mark": 100.0}
    check(X.time_stop(pos, {}, now=now)["exit"] is False, "3 time_stop OFF -> no exit")

    cfg = {"time_stop_days": 5, "time_stop_min_gain_pct": 5}

    # Held > window, flat -> exit
    flat = {"opened_at": old, "avg_cost": 100.0, "mark": 101.0}  # +1% < 5%
    check(X.time_stop(flat, cfg, now=now)["exit"] is True,
          "3 time_stop fires past window when not up enough")

    # Held > window but up enough -> hold
    winner = {"opened_at": old, "avg_cost": 100.0, "mark": 120.0}  # +20% >= 5%
    check(X.time_stop(winner, cfg, now=now)["exit"] is False,
          "3 time_stop holds a winner past the window")

    # Not yet past window -> no exit
    young = {"opened_at": recent, "avg_cost": 100.0, "mark": 100.0}
    check(X.time_stop(young, cfg, now=now)["exit"] is False,
          "3 time_stop does not fire before the window")

    # opened_at via arg + mark via arg
    r = X.time_stop({"avg_cost": 100.0}, cfg, now=now, mark=100.5, opened_at=old)
    check(r["exit"] is True, "3 time_stop accepts opened_at/mark as args")

    # Missing entry time -> hold (fail-open)
    check(X.time_stop({"avg_cost": 100.0, "mark": 100.0}, cfg, now=now)["exit"] is False,
          "3 time_stop missing entry time -> hold")

    # Garbage -> hold
    check(X.time_stop(None, cfg, now=now)["exit"] is False,
          "3 time_stop None position -> hold (fail-open)")


# ── 4. profit_ladder ──────────────────────────────────────────────────────

def test_profit_ladder():
    # OFF -> []
    check(X.profit_ladder({"avg_cost": 100, "mark": 200}, {}) == [],
          "4 profit_ladder OFF -> []")

    cfg = {"profit_ladder": True}  # default rungs 15/0.33, 30/0.5

    # +20% -> first rung triggered, second not
    pos = {"avg_cost": 100.0, "mark": 120.0}
    rungs = X.profit_ladder(pos, cfg)
    check(len(rungs) == 2, "4 profit_ladder returns 2 default rungs")
    check(rungs[0]["triggered"] is True and rungs[1]["triggered"] is False,
          "4 profit_ladder marks rung1 triggered, rung2 not at +20%")

    # +35% -> both triggered
    both = X.profit_ladder({"avg_cost": 100.0, "mark": 135.0}, cfg)
    check(both[0]["triggered"] and both[1]["triggered"],
          "4 profit_ladder marks both rungs at +35%")

    # custom rungs
    cfg2 = {"profit_ladder": True,
            "profit_ladder_rungs": [{"gain_pct": 10, "trim_frac": 0.25},
                                    {"gain_pct": 50, "trim_frac": 1.0}]}
    cr = X.profit_ladder({"avg_cost": 100.0, "mark": 112.0}, cfg2)
    check(len(cr) == 2 and cr[0]["gain_pct"] == 10 and cr[0]["triggered"] is True
          and cr[1]["triggered"] is False,
          "4 profit_ladder honours custom rungs")

    # unmeasurable gain -> untriggered advisory rungs
    nogain = X.profit_ladder({"qty": 5}, cfg)
    check(len(nogain) == 2 and all(not r["triggered"] for r in nogain),
          "4 profit_ladder no cost/mark -> rungs untriggered (advisory)")

    # garbage -> []
    check(X.profit_ladder(None, cfg) == [], "4 profit_ladder None pos -> [] (fail-open)")


# ── 5. event_blackout ─────────────────────────────────────────────────────

def _patch_earnings(monkey_date):
    """Install a fake `earnings` module returning a fixed next date (or None)."""
    fake = types.ModuleType("earnings")

    def get_earnings_calendar(symbols):
        if monkey_date is None:
            return []
        return [{"earnings_date": monkey_date, "days_until": 0}]
    fake.get_earnings_calendar = get_earnings_calendar
    sys.modules["earnings"] = fake


def test_event_blackout():
    today = datetime.date(2026, 6, 20)

    # OFF -> not blocked (no earnings module needed)
    check(X.event_blackout("AAA", {}, now=today)["blocked"] is False,
          "5 event_blackout OFF -> not blocked")

    cfg = {"event_blackout_days": 7}
    try:
        # earnings in 3 days -> blocked
        _patch_earnings("2026-06-23")
        check(X.event_blackout("AAA", cfg, now=today)["blocked"] is True,
              "5 event_blackout blocks within window (3d <= 7d)")

        # earnings in 30 days -> not blocked
        _patch_earnings("2026-07-20")
        check(X.event_blackout("AAA", cfg, now=today)["blocked"] is False,
              "5 event_blackout allows outside window (30d > 7d)")

        # unknown date -> not blocked (fail-open)
        _patch_earnings(None)
        check(X.event_blackout("AAA", cfg, now=today)["blocked"] is False,
              "5 event_blackout unknown date -> allow (fail-open)")

        # past date -> not blocked
        _patch_earnings("2026-06-01")
        check(X.event_blackout("AAA", cfg, now=today)["blocked"] is False,
              "5 event_blackout past earnings -> allow")
    finally:
        sys.modules.pop("earnings", None)

    # broken earnings module -> fail-open
    broken = types.ModuleType("earnings")
    def boom(symbols):
        raise RuntimeError("yfinance down")
    broken.get_earnings_calendar = boom
    sys.modules["earnings"] = broken
    try:
        check(X.event_blackout("AAA", cfg, now=today)["blocked"] is False,
              "5 event_blackout broken earnings module -> allow (fail-open)")
    finally:
        sys.modules.pop("earnings", None)


# ── 6. gex_entry_lean ─────────────────────────────────────────────────────

def _patch_gex(payload):
    fake = types.ModuleType("gex_engine")
    fake.compute_gex = lambda symbol: payload
    sys.modules["gex_engine"] = fake


def test_gex_entry_lean():
    # OFF -> adjust 0
    check(X.gex_entry_lean("AAA", "buy", {})["adjust"] == 0.0,
          "6 gex_entry_lean OFF -> adjust 0")

    cfg = {"gex_timing": True, "gex_timing_step": 0.5}
    try:
        # spot above flip + positive gamma -> favourable (positive lean) for buy
        _patch_gex({"spot_price": 105.0, "gamma_flip_price": 100.0, "net_gex": 5e9})
        rb = X.gex_entry_lean("AAA", "buy", cfg)
        check(rb["adjust"] > 0, "6 gex_entry_lean +gamma above flip -> positive buy lean")

        # mirrored for sell
        rs = X.gex_entry_lean("AAA", "sell", cfg)
        check(rs["adjust"] < 0, "6 gex_entry_lean mirrors sign for sell")

        # negative gamma / below flip -> cautious (negative lean) for buy
        _patch_gex({"spot_price": 95.0, "gamma_flip_price": 100.0, "net_gex": -3e9})
        check(X.gex_entry_lean("AAA", "buy", cfg)["adjust"] < 0,
              "6 gex_entry_lean negative-gamma -> cautious buy lean")

        # gex error -> no nudge (fail-open)
        _patch_gex({"error": "no options"})
        check(X.gex_entry_lean("AAA", "buy", cfg)["adjust"] == 0.0,
              "6 gex_entry_lean gex error -> adjust 0 (fail-open)")

        # incomplete gex -> no nudge
        _patch_gex({"spot_price": 100.0})
        check(X.gex_entry_lean("AAA", "buy", cfg)["adjust"] == 0.0,
              "6 gex_entry_lean incomplete gex -> adjust 0")
    finally:
        sys.modules.pop("gex_engine", None)

    # broken gex module -> fail-open
    broken = types.ModuleType("gex_engine")
    def boom(symbol):
        raise RuntimeError("gex blew up")
    broken.compute_gex = boom
    sys.modules["gex_engine"] = broken
    try:
        check(X.gex_entry_lean("AAA", "buy", cfg)["adjust"] == 0.0,
              "6 gex_entry_lean broken gex module -> adjust 0 (fail-open)")
    finally:
        sys.modules.pop("gex_engine", None)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_execution_alpha — {} test groups\n".format(len(tests)))
    for fn in tests:
        try:
            fn()
        except Exception as e:
            FAIL.append(fn.__name__)
            print("  [XX] {}: unexpected {}: {}".format(fn.__name__, type(e).__name__, e))
    print("\n{} passed, {} failed".format(len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
    sys.exit(1 if FAIL else 0)
