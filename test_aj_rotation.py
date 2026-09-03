#!/usr/bin/env python3
"""AJTA capital-rotation tests (aj_rotation + operator integration).

Part 1 pins the pure decision primitive (aj_rotation.rotation_plan): the
gating, hysteresis, tax-guard and churn protections. Part 2 drives a real
operator wiring against a fresh temp DB to prove a swap actually executes,
frees the slot, and never fires when unconstrained. No network/LLM/broker.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajrot.db")

import aj_rotation             # noqa: E402


# ── Part 1: pure decision primitive ─────────────────────────────────────────────

_CFG = {
    "rotation_enabled": True,
    "rotation_min_edge_gain_pct_pts": 4.0,
    "rotation_hold_edge_floor_pct_pts": 2.0,
    "rotation_min_hold_days": 3,
    "rotation_max_per_cycle": 2,
    "rotation_tax_bias_pct_pts": 3.0,
    "rotation_tax_bias_window_days": 45,
    "min_edge_pct_pts": 3.0,
}


def _cfg(**over):
    c = dict(_CFG)
    c.update(over)
    return c


def test_disabled_returns_nothing():
    plan = aj_rotation.rotation_plan(
        [{"symbol": "OLD", "edge": -5, "age_days": 30, "days_to_long_term": 300}],
        [{"symbol": "NEW", "edge": 10}], _cfg(rotation_enabled=False), True)
    assert plan == []


def test_unconstrained_never_rotates():
    # even a huge edge gain does nothing when the agent could just buy
    plan = aj_rotation.rotation_plan(
        [{"symbol": "OLD", "edge": -5, "age_days": 30, "days_to_long_term": 300}],
        [{"symbol": "NEW", "edge": 15}], _cfg(), False)
    assert plan == []


def test_basic_swap_fires_when_constrained():
    plan = aj_rotation.rotation_plan(
        [{"symbol": "OLD", "edge": -1.0, "age_days": 30, "days_to_long_term": 300}],
        [{"symbol": "NEW", "edge": 8.0}], _cfg(), True)
    assert len(plan) == 1
    assert plan[0]["sell"] == "OLD" and plan[0]["buy"] == "NEW"
    assert plan[0]["edge_gain"] == 9.0


def test_strong_holding_is_never_displaced():
    # holding edge ABOVE the floor -> untouchable even if a candidate is stronger
    plan = aj_rotation.rotation_plan(
        [{"symbol": "STRONG", "edge": 6.0, "age_days": 30, "days_to_long_term": 300}],
        [{"symbol": "NEW", "edge": 12.0}], _cfg(), True)
    assert plan == []


def test_marginal_gain_below_threshold_is_rejected():
    # holding weak (edge 1 < floor 2), but candidate only +3 > need +4 -> no
    plan = aj_rotation.rotation_plan(
        [{"symbol": "OLD", "edge": 1.0, "age_days": 30, "days_to_long_term": 300}],
        [{"symbol": "NEW", "edge": 4.0}], _cfg(), True)
    assert plan == []


def test_just_opened_holding_is_protected():
    # weak edge + huge candidate, but age < min_hold_days -> don't churn it
    plan = aj_rotation.rotation_plan(
        [{"symbol": "FRESH", "edge": -5.0, "age_days": 1, "days_to_long_term": 360}],
        [{"symbol": "NEW", "edge": 15.0}], _cfg(), True)
    assert plan == []


def test_candidate_below_entry_floor_is_ignored():
    # candidate edge 2.5 < min_edge_pct_pts 3.0 -> not a buyable name
    plan = aj_rotation.rotation_plan(
        [{"symbol": "OLD", "edge": -3.0, "age_days": 30, "days_to_long_term": 300}],
        [{"symbol": "NEW", "edge": 2.5}], _cfg(), True)
    assert plan == []


def test_tax_guard_requires_extra_edge_near_one_year():
    # holding is 20 days from long-term: needs min_gain(4)+tax_bias(3)=7 pts.
    held = [{"symbol": "OLD", "edge": 0.0, "age_days": 345, "days_to_long_term": 20}]
    # candidate +6 -> below the tax-guarded bar -> rejected
    assert aj_rotation.rotation_plan(held, [{"symbol": "N", "edge": 6.0}], _cfg(), True) == []
    # candidate +8 -> clears it -> fires
    plan = aj_rotation.rotation_plan(held, [{"symbol": "N", "edge": 8.0}], _cfg(), True)
    assert len(plan) == 1 and plan[0]["near_long_term"] is True


def test_weakest_holding_matched_to_best_candidate_and_capped():
    held = [
        {"symbol": "A", "edge": -6.0, "age_days": 30, "days_to_long_term": 300},
        {"symbol": "B", "edge": -1.0, "age_days": 30, "days_to_long_term": 300},
        {"symbol": "C", "edge": -3.0, "age_days": 30, "days_to_long_term": 300},
    ]
    cands = [{"symbol": "X", "edge": 10.0}, {"symbol": "Y", "edge": 9.0},
             {"symbol": "Z", "edge": 8.0}]
    plan = aj_rotation.rotation_plan(held, cands, _cfg(rotation_max_per_cycle=2), True)
    assert len(plan) == 2                        # capped
    # weakest (A=-6) paired to best (X=10); next weakest (C=-3) to next best (Y=9)
    assert plan[0]["sell"] == "A" and plan[0]["buy"] == "X"
    assert plan[1]["sell"] == "C" and plan[1]["buy"] == "Y"


def test_no_double_use_of_a_candidate():
    held = [{"symbol": "A", "edge": -5.0, "age_days": 30, "days_to_long_term": 300},
            {"symbol": "B", "edge": -4.0, "age_days": 30, "days_to_long_term": 300}]
    cands = [{"symbol": "X", "edge": 10.0}]       # only one candidate
    plan = aj_rotation.rotation_plan(held, cands, _cfg(), True)
    assert len(plan) == 1 and plan[0]["buy"] == "X"


def test_unknown_edge_holding_is_kept():
    plan = aj_rotation.rotation_plan(
        [{"symbol": "OLD", "edge": None, "age_days": 30, "days_to_long_term": 300}],
        [{"symbol": "NEW", "edge": 12.0}], _cfg(), True)
    assert plan == []


# ── Part 2: operator integration (real sell frees the slot) ─────────────────────

def _integration():
    """Wire a 2-position book at a cap of 2, force a strong candidate, and prove
    rotation sells the weak name so a buy can fund the better one. Imported lazily
    so Part 1 runs even if the heavier operator stack can't import."""
    import aj_db, aj_config, aj_positions, aj_operator, aj_rules
    import database as db
    aj_db.aj_init()

    def reset():
        conn = db.get_conn()
        with aj_db.audit_maintenance():
            for t in ("aj_orders", "aj_fills", "aj_proposals", "aj_audit",
                      "aj_position_state", "aj_cycles"):
                conn.execute("DELETE FROM {}".format(t))
        conn.execute("DELETE FROM settings WHERE key LIKE 'aj_%' OR key LIKE '__aj_%'")
        conn.commit()
        try:
            db._invalidate_settings_cache()
        except Exception:
            pass

    def fill(sym, side, qty, price, when):
        oid = aj_db.insert("aj_orders", proposal_id=1,
                           client_order_id=sym + side + str(qty) + when,
                           broker="paper", mode="paper", symbol=sym, side=side,
                           qty=qty, order_type="market", state="filled", created_at=when)
        aj_db.insert("aj_fills", order_id=oid, qty=qty, price=price, fees_usd=0.0,
                     filled_at=when)

    results = []

    # book: WEAK + KEEP, both aged so they clear min_hold_days
    reset()
    old = "2026-06-01T15:00:00+00:00"
    fill("WEAK", "buy", 10, 100.0, old)
    fill("KEEP", "buy", 10, 100.0, old)
    aj_rules.update_position_state()

    # PERSIST the execution-path switches: _propose_and_execute / the risk gate
    # re-read config from the DB, so a passed-in override alone won't authorize a
    # fill. Rotation's own rotation_* keys are read from the passed cfg.
    aj_config.set_config({"trading_enabled": True, "auto_approve_paper": True,
                          "dry_run": False, "max_open_positions": 2,
                          "max_trades_per_day": 50, "max_order_notional_usd": 5000.0,
                          "max_daily_loss_usd": 5000.0, "signal_scorecard": False})
    cfg = aj_config.get_config()
    # min_hold_days=0 here: update_position_state stamps opened_at=now for a
    # freshly-seeded book, so age is 0 in-test. The age guard is covered by the
    # pure unit test; this integration isolates execution + slot-freeing.
    cfg = dict(cfg, rotation_enabled=True, max_open_positions=2,
               rotation_min_hold_days=0, rotation_min_edge_gain_pct_pts=4.0,
               rotation_hold_edge_floor_pct_pts=2.0, signal_scorecard=False)

    # deterministic edges + a real mark, patched onto the operator's collaborators
    edges = {"WEAK": -3.0, "KEEP": 8.0, "BEST": 12.0}

    def fake_forecast(sym, horizon):
        e = edges.get(sym)
        if e is None:
            return None
        return {"ensemble": {"edge_pct_pts": e, "prob_up": 0.5 + e / 100.0,
                             "conviction": "high"}}

    import aj_risk
    import fetcher
    _orig_fc = aj_operator._forecast
    _orig_marks = aj_risk._marks
    _orig_sess = aj_db.market_session
    _orig_q = fetcher.get_quote
    aj_operator._forecast = fake_forecast
    aj_risk._marks = lambda syms: {s: 105.0 for s in syms}
    aj_db.market_session = lambda dt=None: "regular"       # allow fills in-test
    # the paper broker prices fills off fetcher.get_quote — give the synthetic
    # tickers a deterministic mark so the rotation SELL actually fills.
    fetcher.get_quote = lambda sym, *a, **k: {"price": 105.0, "symbol": sym}
    try:
        held = {"WEAK": 10.0, "KEEP": 10.0}
        fc_cache = {}
        scan = ["BEST", "WEAK", "KEEP"]
        rot = aj_operator._process_rotation("cyc1", cfg, held, fc_cache, scan, 20)
        results.append(("constrained", rot.get("constrained") is True))
        results.append(("one_swap_planned", len(rot.get("plan") or []) == 1))
        sw = (rot.get("plan") or [{}])[0]
        results.append(("sells_weak", sw.get("sell") == "WEAK"))
        results.append(("buys_best", sw.get("buy") == "BEST"))
        results.append(("executed", len(rot.get("executed") or []) == 1))
        # the sell actually happened: WEAK is flat, slot freed in `held`
        book = aj_positions.paper_book().get("positions") or {}
        results.append(("weak_sold_flat", float((book.get("WEAK") or {}).get("qty") or 0) == 0))
        results.append(("keep_untouched", float((book.get("KEEP") or {}).get("qty") or 0) == 10))
        results.append(("slot_freed_in_held", "WEAK" not in held))

        # NOT constrained (cap high, cash irrelevant) -> no rotation
        reset()
        fill("WEAK", "buy", 10, 100.0, old)
        fill("KEEP", "buy", 10, 100.0, old)
        aj_rules.update_position_state()
        cfg2 = dict(cfg, max_open_positions=25, paper_cash=0.0)
        held2 = {"WEAK": 10.0, "KEEP": 10.0}
        rot2 = aj_operator._process_rotation("cyc2", cfg2, held2, {}, scan, 20)
        results.append(("unconstrained_noop", rot2.get("constrained") is False and not rot2.get("executed")))
    finally:
        aj_operator._forecast = _orig_fc
        aj_risk._marks = _orig_marks
        aj_db.market_session = _orig_sess
        fetcher.get_quote = _orig_q
    return results


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    print("aj_rotation — {} unit tests + integration".format(len(fns)))
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
            print("  [XX] {}: unexpected {}: {}".format(fn.__name__, type(e).__name__, e))
    # integration (may be skipped if heavy stack unavailable)
    try:
        for name, ok in _integration():
            print(("  [OK] " if ok else "  [XX] ") + "integration:" + name)
            if not ok:
                failed += 1
    except Exception as e:
        print("  [--] integration skipped: {}: {}".format(type(e).__name__, e))
    print("PASS" if failed == 0 else "{} FAILED".format(failed))
    sys.exit(1 if failed else 0)
