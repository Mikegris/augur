#!/usr/bin/env python3
"""Research Scientist tests (aj_lab) — fully offline.

The replay-fold runner is stubbed (no subprocesses); what's under test is the
science: hypothesis generation + dedup, the constitution (whitelist/bounds),
K-fold verdict logic with the deflated promotion margin, promotion mechanics
(config change + expectation record + audit), and the demotion watchdog.
"""
import json
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="ajlab_"), "replaylab.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_lab                   # noqa: E402

aj_db.aj_init()


def _reset():
    conn = db.get_conn()
    for t in ("aj_lab_experiments", "aj_lab_promotions", "aj_equity"):
        conn.execute("DELETE FROM " + t)
    conn.execute("DELETE FROM settings WHERE key LIKE 'aj_%'")
    conn.commit()
    try:
        db._invalidate_settings_cache()
    except Exception:
        pass
    aj_config.set_config({"lab_enabled": True, "buy_prob_threshold": 0.60,
                          "stop_loss_pct": 8.0, "take_profit_pct": 15.0})


class _StubFolds:
    """Monkeypatch seam: decide each fold's metrics from the candidate's sets."""

    def __init__(self, base_sharpe=1.0, cand_sharpe=1.3, trades=30,
                 none_for_candidate=False):
        self.base_sharpe = base_sharpe
        self.cand_sharpe = cand_sharpe
        self.trades = trades
        self.none_for_candidate = none_for_candidate
        self.calls = []

    def __call__(self, sets, start, end, universe, run_id, cash=100000.0):
        self.calls.append((tuple(sets), start, end, run_id))
        cfg = aj_config.get_config()
        is_candidate = any(
            s.split("=")[0] in aj_lab._TUNABLE_BOUNDS and
            str(cfg.get(s.split("=")[0])) != s.split("=", 1)[1]
            for s in sets)
        if is_candidate and self.none_for_candidate:
            return None
        sharpe = self.cand_sharpe if is_candidate else self.base_sharpe
        return {"sharpe": sharpe, "total_return_pct": 10.0, "alpha_pct": 1.0,
                "max_drawdown_pct": 10.0, "trades": {"trades": self.trades}}


def _queue_exp(key, value):
    return aj_db.insert("aj_lab_experiments", created_at=aj_db.utc_now_iso(),
                        status="queued", kind="param",
                        hypothesis="{} -> {}".format(key, value),
                        rationale="test", params_json=json.dumps(
                            {"set": {key: value}}, sort_keys=True))


def test_propose_respects_bounds_and_dedups():
    _reset()
    r1 = aj_lab.propose(max_new=50)
    assert r1["queued"] > 0, r1
    rows = aj_db.query("SELECT params_json FROM aj_lab_experiments")
    for r in rows:
        chg = json.loads(r["params_json"])["set"]
        for k, v in chg.items():
            lo, hi = aj_lab._TUNABLE_BOUNDS[k]
            assert lo - 1e-9 <= float(v) <= hi + 1e-9, (k, v)
    # re-propose: everything already queued -> nothing new
    r2 = aj_lab.propose(max_new=50)
    assert r2["queued"] == 0, r2


def test_promotion_on_clear_win():
    _reset()
    _queue_exp("take_profit_pct", 20.0)
    stub = _StubFolds(base_sharpe=1.0, cand_sharpe=1.4)
    orig = aj_lab._run_replay_fold
    aj_lab._run_replay_fold = stub
    try:
        res = aj_lab.run_next()
    finally:
        aj_lab._run_replay_fold = orig
    assert res["ran"] and res["verdict"] == "promoted", res
    assert abs(aj_config.get_config()["take_profit_pct"] - 20.0) < 1e-9
    promos = aj_db.query("SELECT * FROM aj_lab_promotions WHERE status='active'")
    assert len(promos) == 1 and promos[0]["key"] == "take_profit_pct"
    exp = json.loads(promos[0]["expectation_json"])
    assert exp["baseline_sharpe_mean"] == 1.0
    assert res["evidence"]["wins"] == 3 and res["evidence"]["judged"] == 3


def test_rejected_below_deflated_margin():
    _reset()
    # seed prior experiments so the margin bar is up
    for i in range(15):
        aj_db.insert("aj_lab_experiments", created_at=aj_db.utc_now_iso(),
                     status="done", kind="param", hypothesis="h", rationale="r",
                     params_json=json.dumps({"set": {"x": i}}), verdict="rejected")
    _queue_exp("take_profit_pct", 20.0)
    # a win, but tiny: below the deflated margin
    stub = _StubFolds(base_sharpe=1.0, cand_sharpe=1.05)
    orig = aj_lab._run_replay_fold
    aj_lab._run_replay_fold = stub
    try:
        res = aj_lab.run_next()
    finally:
        aj_lab._run_replay_fold = orig
    assert res["verdict"] == "rejected", res
    assert res["evidence"]["margin"] > 0.08          # deflation raised the bar
    assert abs(aj_config.get_config()["take_profit_pct"] - 15.0) < 1e-9  # unchanged


def test_margin_rises_with_search_volume():
    assert aj_lab._margin(0) == 0.08
    assert aj_lab._margin(1) > 0.08
    assert aj_lab._margin(31) > aj_lab._margin(7) > aj_lab._margin(1)


def test_constitution_blocks_out_of_scope():
    _reset()
    # non-whitelisted key
    _queue_exp("max_daily_loss_usd", 999999)
    res = aj_lab.run_next()
    assert res["verdict"] == "rejected" and "constitution" in \
        res["evidence"]["reason"], res
    # out-of-bounds value for a whitelisted key
    _queue_exp("stop_loss_pct", 50.0)
    res = aj_lab.run_next()
    assert res["verdict"] == "rejected" and "bounds" in \
        res["evidence"]["reason"], res
    assert abs(aj_config.get_config()["stop_loss_pct"] - 8.0) < 1e-9


def test_inconclusive_fold_blocks_promotion():
    _reset()
    _queue_exp("take_profit_pct", 20.0)
    stub = _StubFolds(none_for_candidate=True)
    orig = aj_lab._run_replay_fold
    aj_lab._run_replay_fold = stub
    try:
        res = aj_lab.run_next()
    finally:
        aj_lab._run_replay_fold = orig
    assert res["verdict"] == "inconclusive", res
    assert abs(aj_config.get_config()["take_profit_pct"] - 15.0) < 1e-9


def test_watchdog_demotes_and_reverts():
    _reset()
    # a live promotion with a healthy expectation
    aj_config.set_config({"take_profit_pct": 20.0})
    aj_db.insert("aj_lab_promotions", experiment_id=1,
                 promoted_at="2026-01-01T00:00:00+00:00",
                 key="take_profit_pct", old_value="15.0", new_value="20.0",
                 expectation_json=json.dumps({"baseline_sharpe_mean": 1.0,
                                              "mean_delta": 0.3}))
    # live equity since promotion: a steady bleed (clearly negative sharpe)
    eq = 100000.0
    for i in range(25):
        eq *= 0.995
        aj_db.insert("aj_equity", ts="2026-02-{:02d}T21:00:00+00:00".format(i + 1),
                     date="2026-02-{:02d}".format(i + 1), realized_usd=0,
                     unrealized_usd=0, equity_usd=eq, day_pnl_usd=0)
    out = aj_lab.check_promotions(min_days=15)
    assert out["demoted"] == ["take_profit_pct"], out
    assert abs(aj_config.get_config()["take_profit_pct"] - 15.0) < 1e-9
    row = aj_db.query("SELECT * FROM aj_lab_promotions")[0]
    assert row["status"] == "demoted" and "live sharpe" in row["demote_reason"]


def test_run_due_gates_on_flag():
    _reset()
    aj_config.set_config({"lab_enabled": False})
    res = aj_lab.run_due()
    assert res["ran"] is False and "disabled" in res["reason"]


def test_status_shape():
    _reset()
    _queue_exp("take_profit_pct", 20.0)
    s = aj_lab.status()
    assert s["enabled"] is True and len(s["queue"]) == 1
    assert "current_margin" in s and "experiments_total" in s


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_lab — {} tests".format(len(fns)))
    failed = 0
    for fn in fns:
        try:
            fn()
            print("  [OK] {}".format(fn.__name__))
        except AssertionError as e:
            failed += 1
            print("  [XX] {}: {}".format(fn.__name__, e))
        except Exception as e:
            failed += 1
            print("  [XX] {}: unexpected {}: {}".format(
                fn.__name__, type(e).__name__, e))
    print("PASS" if failed == 0 else "{} FAILED".format(failed))
    sys.exit(1 if failed else 0)
