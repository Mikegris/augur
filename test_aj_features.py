"""Offline tests for aj_features — the meta-labeling feature/label store.

Fully isolated: fresh temp DB, aj_positions.realized_trades monkeypatched with
canned closed trades, a couple of seeded aj_cycle_stats scan snapshots, regime
detection stubbed. No network, no LLM. Standalone runner: PASS/FAILED + exit().
"""
import os
import sys
import json
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix=".db")

import database  # noqa: E402
database.init_db()
import aj_db  # noqa: E402
aj_db.aj_migrate()

import aj_positions  # noqa: E402
import aj_alpha  # noqa: E402
import aj_features as F  # noqa: E402

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


# ── fixtures ───────────────────────────────────────────────────────────────────

def _seed_scan(symbol, ts, edge, conviction, prob_up):
    """Insert one aj_cycle_stats row whose scan_json carries one symbol entry."""
    cid = "cyc_" + symbol + "_" + ts.replace(":", "").replace("-", "")
    scan = json.dumps([{"symbol": symbol, "result": "executed",
                        "edge": edge, "conviction": conviction,
                        "prob_up": prob_up}])
    with database._write_lock:
        conn = database.get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO aj_cycle_stats "
            "(cycle_id, ts, mode, session, scanned, with_signal, executed, "
            " exits, result_json, scan_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, ts, "paper", "regular", 1, 1, 1, 0, "{}", scan))
        conn.commit()


# Canned closed round-trips. AAA: profitable long; BBB: losing long; CCC: a
# profitable long for a symbol with NO scan snapshot (features must fall back).
_CANNED = [
    {"symbol": "AAA", "side": "sell", "qty": 10,
     "entry_price": 100.0, "exit_price": 110.0,
     "opened_at": "2026-06-10T15:00:00+00:00",
     "closed_at": "2026-06-12T15:00:00+00:00",
     "realized": 95.0},                       # net pnl > 0 -> label 1
    {"symbol": "BBB", "side": "sell", "qty": 5,
     "entry_price": 50.0, "exit_price": 45.0,
     "opened_at": "2026-06-11T15:00:00+00:00",
     "closed_at": "2026-06-13T15:00:00+00:00",
     "realized": -27.0},                      # net pnl < 0 -> label 0
    {"symbol": "CCC", "side": "sell", "qty": 3,
     "entry_price": 20.0, "exit_price": 22.0,
     "opened_at": "2026-06-14T15:00:00+00:00",
     "closed_at": "2026-06-15T15:00:00+00:00",
     "realized": 6.0},                        # no snapshot -> neutral features
]


def _install_fixtures():
    aj_positions.realized_trades = lambda mode="paper": list(_CANNED)
    aj_alpha.detect_regime = lambda: "bull"
    # AAA: an earlier (stale) and a nearer snapshot before the open — nearest
    # prior must win (edge=0.9, conviction=high). BBB has one prior snapshot.
    _seed_scan("AAA", "2026-06-01T15:00:00+00:00", 0.1, "low", 0.4)
    _seed_scan("AAA", "2026-06-10T14:00:00+00:00", 0.9, "high", 0.8)
    _seed_scan("BBB", "2026-06-11T14:00:00+00:00", 0.3, "med", 0.55)
    # CCC: intentionally no snapshot.


# ── tests ──────────────────────────────────────────────────────────────────────

def test_build_labels_writes_correct_rows():
    res = F.build_labels(lookback_days=365)
    check("error" not in res, "build_labels did not error")
    check(res.get("built") == 3, "build_labels built 3 new rows (got %r)" % res.get("built"))
    check(res.get("total") == 3, "build_labels total == 3")
    # base rate: AAA + CCC profitable, BBB not -> 2/3.
    check(abs(res.get("base_rate", 0) - 2.0 / 3.0) < 1e-9,
          "build_labels base_rate == 2/3 (got %r)" % res.get("base_rate"))

    rows = {r["symbol"]: r for r in aj_db.query(
        "SELECT * FROM aj_trade_labels")}
    check(rows["AAA"]["label"] == 1, "AAA labeled 1 (pnl>0)")
    check(rows["BBB"]["label"] == 0, "BBB labeled 0 (pnl<0)")
    check(rows["CCC"]["label"] == 1, "CCC labeled 1 (pnl>0)")
    check(abs(rows["AAA"]["realized_pnl_usd"] - 95.0) < 1e-9, "AAA pnl stored")
    # long return pct: (110-100)/100*100 = 10
    check(abs(rows["AAA"]["realized_return_pct"] - 10.0) < 1e-6,
          "AAA realized_return_pct == 10 (got %r)" % rows["AAA"]["realized_return_pct"])
    # holding_days AAA = 2 days
    check(abs(rows["AAA"]["holding_days"] - 2.0) < 1e-6, "AAA holding_days == 2")
    check(rows["AAA"]["regime"] == "bull", "AAA regime captured")


def test_recovers_and_falls_back_features():
    rows = {r["symbol"]: r for r in aj_db.query("SELECT * FROM aj_trade_labels")}
    aaa = json.loads(rows["AAA"]["features_json"])
    # nearest PRIOR snapshot (06-10 14:00) recovered, not the stale 06-01 one.
    check(abs(aaa["edge"] - 0.9) < 1e-9, "AAA edge recovered from nearest snapshot")
    check(abs(aaa["conviction"] - 1.0) < 1e-9, "AAA conviction high->1.0")
    check(abs(aaa["prob_up"] - 0.8) < 1e-9, "AAA prob_up recovered")
    bbb = json.loads(rows["BBB"]["features_json"])
    check(abs(bbb["conviction"] - 0.5) < 1e-9, "BBB conviction med->0.5")
    ccc = json.loads(rows["CCC"]["features_json"])
    # no snapshot -> edge/conviction/prob_up absent (None) in features_json
    check(ccc.get("edge") is None, "CCC edge falls back to None (no snapshot)")
    check(ccc.get("conviction") is None, "CCC conviction None (no snapshot)")


def test_idempotent_rerun():
    res2 = F.build_labels(lookback_days=365)
    check(res2.get("built") == 0, "re-run built 0 new rows (idempotent)")
    check(res2.get("total") == 3, "re-run total still 3 (no duplicates)")
    cnt = aj_db.query("SELECT COUNT(*) AS n FROM aj_trade_labels")[0]["n"]
    check(cnt == 3, "still exactly 3 rows after re-run")


def test_training_set_aligned():
    X, y, names = F.training_set()
    check(names == F.FEATURE_NAMES, "feature_names matches the canonical contract")
    check(len(X) == 3 and len(y) == 3, "training_set has 3 aligned rows")
    width = len(names)
    check(all(len(v) == width for v in X), "every X row is the canonical width")
    check(all(isinstance(c, float) for v in X for c in v), "all features are floats")
    check(set(y) == {0, 1}, "labels are 0/1")
    check(sum(y) == 2, "two profitable labels in training set")
    # CCC row imputes missing numeric features to neutral values.
    idx = {n: i for i, n in enumerate(names)}
    # find CCC's row: it's the last by closed_at ordering.
    ccc_vec = X[-1]
    check(ccc_vec[idx["edge"]] == 0.0, "CCC edge imputed to neutral 0.0")
    check(ccc_vec[idx["conviction"]] == 0.5, "CCC conviction imputed to neutral 0.5")
    check(ccc_vec[idx["prob_up"]] == 0.5, "CCC prob_up imputed to neutral 0.5")
    # one-hot regime bull set for all (regime=bull)
    check(all(v[idx["regime_bull"]] == 1.0 for v in X), "regime_bull one-hot set")
    check(all(v[idx["regime_bear"]] == 0.0 for v in X), "regime_bear zero")


def test_label_stats():
    st = F.label_stats()
    check("error" not in st, "label_stats did not error")
    check(st["n"] == 3, "label_stats n == 3")
    check(abs(st["base_rate"] - 2.0 / 3.0) < 1e-9, "label_stats base_rate == 2/3")
    check("bull" in st["by_regime"], "by_regime has bull")
    check(st["by_regime"]["bull"]["n"] == 3, "all 3 in bull regime")
    check(abs(st["by_regime"]["bull"]["base_rate"] - 2.0 / 3.0) < 1e-9,
          "bull base_rate == 2/3")
    check(st["oldest"] == "2026-06-12T15:00:00+00:00", "oldest = earliest close")
    check(st["newest"] == "2026-06-15T15:00:00+00:00", "newest = latest close")


def test_fail_open_on_bad_source():
    """A raising realized_trades must not blow up build_labels (fail-open)."""
    orig = aj_positions.realized_trades
    try:
        def boom(mode="paper"):
            raise RuntimeError("boom")
        aj_positions.realized_trades = boom
        res = F.build_labels()
        # _round_trips catches internally -> 0 trades, no new rows, no raise.
        check(isinstance(res, dict) and "error" not in res,
              "build_labels fail-open when source raises")
        check(res.get("built") == 0, "no rows built from a broken source")
    finally:
        aj_positions.realized_trades = orig


if __name__ == "__main__":
    _install_fixtures()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_features — {} test groups\n".format(len(tests)))
    for fn in tests:
        try:
            fn()
        except Exception as e:
            FAIL.append(fn.__name__)
            print("  [XX] {}: unexpected {}: {}".format(
                fn.__name__, type(e).__name__, e))
    print("\n{} passed, {} failed".format(len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print("PASS")
    sys.exit(0)
