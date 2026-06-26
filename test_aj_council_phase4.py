#!/usr/bin/env python3
"""Phase 4 tests — trader, 3-way risk debate, arbiter, alpha-aware reflection.

Network-free: analysts + completions injected (role-aware fake), memory + DB in
the temp dir.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajp4.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_analysts              # noqa: E402
import aj_council               # noqa: E402
import aj_debate                # noqa: E402
import aj_memory                # noqa: E402
from aj_schemas import (AnalystReport, ResearchPlan, TraderProposal, Rating,  # noqa: E402
                        Action)

aj_db.aj_init()
aj_memory.clear()


def _reports(*specs):
    return [AnalystReport(analyst=n, band="NEUTRAL", score=s, confidence=c,
                          key_points=[n], narrative=n) for n, s, c in specs]


def _full_call(arbiter_rating="OVERWEIGHT", arbiter_conv=0.7):
    """Role-aware fake spanning the whole pipeline."""
    def call(tier, role, system, prompt):
        if "research.bull" in role or "research.bear" in role:
            return role.split(".")[-1] + " argument"
        if "research_manager" in role:
            return '{"recommendation":"BUY","rationale":"bullish","strategic_actions":["buy"]}'
        if "trader" in role:
            return '{"action":"BUY","reasoning":"go","entry_price":100,"stop_loss":90,"position_sizing":"normal"}'
        if "risk.aggressive" in role:
            return '{"assessment":"size up","recommended_action":"BUY"}'
        if "risk.conservative" in role:
            return '{"assessment":"trim risk","recommended_action":"HOLD"}'
        if "risk.neutral" in role:
            return '{"assessment":"balanced","recommended_action":"BUY"}'
        if "arbiter" in role:
            return '{"rating":"%s","action":"BUY","conviction":%s,"thesis":"net constructive","dissent":"conservative wary","time_horizon":"medium"}' % (arbiter_rating, arbiter_conv)
        return None
    return call


# ── trader ────────────────────────────────────────────────────────────────────

def test_trader_builds_proposal():
    plan = ResearchPlan(recommendation=Rating.BUY, rationale="x")
    prop = aj_debate.trader("AAPL", plan, _full_call())
    assert prop is not None and prop.action is Action.BUY
    assert prop.entry_price == 100 and prop.stop_loss == 90
    assert aj_debate.trader("AAPL", plan, lambda *a, **k: None) is None  # no model


# ── risk debate ───────────────────────────────────────────────────────────────

def test_risk_debate_rotation_and_termination():
    prop = TraderProposal(action=Action.BUY, reasoning="x")
    turns = aj_debate.risk_debate("AAPL", prop, "", _full_call(), rounds=1)
    assert [t["role"] for t in turns] == ["aggressive", "conservative", "neutral"]
    assert all(t["debate"] == "risk" for t in turns)
    turns2 = aj_debate.risk_debate("AAPL", prop, "", _full_call(), rounds=2)
    assert len(turns2) == 6


def test_risk_debate_stops_early_and_zero_rounds():
    prop = TraderProposal(action=Action.BUY)
    one = lambda tier, role, s, p: ("ok" if "aggressive" in role else None)
    assert len(aj_debate.risk_debate("AAPL", prop, "", one, rounds=1)) == 1
    assert aj_debate.risk_debate("AAPL", prop, "", _full_call(), rounds=0) == []


# ── arbiter ───────────────────────────────────────────────────────────────────

def test_arbiter_synthesizes_decision():
    plan = ResearchPlan(recommendation=Rating.BUY, rationale="x")
    prop = TraderProposal(action=Action.BUY)
    turns = aj_debate.risk_debate("AAPL", prop, "", _full_call(), rounds=1)
    dec = aj_council._arbiter("AAPL", plan, prop, turns, "", 0.7, _full_call(arbiter_rating="OVERWEIGHT", arbiter_conv=0.8))
    assert dec is not None and dec.rating is Rating.OVERWEIGHT
    assert abs(dec.conviction - 0.8) < 1e-6        # arbiter conviction honored
    assert dec.dissent and dec.time_horizon == "medium"


def test_arbiter_none_without_turns_or_model():
    plan = ResearchPlan(recommendation=Rating.BUY)
    assert aj_council._arbiter("AAPL", plan, None, [], "", 0.5, _full_call()) is None
    prop = TraderProposal(action=Action.BUY)
    assert aj_council._arbiter("AAPL", plan, prop, [{"role": "x", "content": "y"}], "", 0.5, lambda *a, **k: None) is None


# ── reflection ────────────────────────────────────────────────────────────────

def test_reflect_writes_lesson_and_alpha():
    aj_memory.clear()
    out = aj_council.reflect("AAPL", raw_return=0.10, benchmark_return=0.03, benchmark="SPY")
    assert abs(out["alpha_return"] - 0.07) < 1e-9
    # persisted to aj_reflections
    row = db.get_conn().execute(
        "SELECT alpha_return, lesson FROM aj_reflections WHERE symbol='AAPL'").fetchone()
    assert row is not None and abs(row["alpha_return"] - 0.07) < 1e-9
    # available as a cross-symbol lesson in memory recall
    got = aj_memory.recall("OTHER", n_cross=3)
    assert any("AAPL" in g for g in got)
    assert aj_db.verify_audit_chain()["ok"]


def test_reflect_uses_model_lesson_when_available():
    out = aj_council.reflect("MSFT", 0.05, 0.05, call=lambda *a, **k: "Lesson: trim winners earlier.")
    assert out["lesson"].startswith("Lesson:")


# ── full pipeline ─────────────────────────────────────────────────────────────

def _set_analysts(specs):
    def mk(s, c, n):
        return lambda symbol, call=None, cfg=None: AnalystReport(
            analyst=n, band="NEUTRAL", score=s, confidence=c, key_points=[n], narrative=n)
    aj_analysts.ANALYSTS = {n: mk(s, c, n) for n, (s, c) in specs.items()}


def test_full_pipeline_uses_arbiter_and_persists_risk_turns():
    aj_config.set_config({"max_research_rounds": 1, "max_risk_rounds": 1})
    aj_council.clear_cache()
    _set_analysts({"fundamentals": (6.0, 0.7), "technical": (6.0, 0.7)})
    dec = aj_council.run("AAPL", call=_full_call(arbiter_rating="OVERWEIGHT"))
    assert dec.rating is Rating.OVERWEIGHT      # arbiter's call, not raw consensus
    assert dec.status == "ok"
    conn = db.get_conn()
    nrisk = conn.execute("SELECT COUNT(*) c FROM aj_debate_turns WHERE debate='risk'").fetchone()["c"]
    nres = conn.execute("SELECT COUNT(*) c FROM aj_debate_turns WHERE debate='research'").fetchone()["c"]
    assert nrisk >= 3 and nres >= 2
    assert aj_db.verify_audit_chain()["ok"]


def test_reflect_due_dedup_and_alpha():
    import aj_positions, aj_analytics
    aj_config.set_config({"daily_reflection": True})
    aj_memory.clear()
    # a closed council BUY round-trip
    aj_db.insert("aj_council_runs", ts=aj_db.utc_now_iso(), symbol="RDUE",
                 action="BUY", status="ok", rating="BUY", conviction=0.8)
    saved = (aj_council._invested_usd, aj_council._spy_return_since,
             aj_positions.paper_book, aj_analytics.attribution)
    aj_council._invested_usd = lambda s: 1000.0
    aj_council._spy_return_since = lambda ts: 0.02
    aj_positions.paper_book = lambda *a, **k: {"positions": {}}      # closed
    aj_analytics.attribution = lambda: [{"symbol": "RDUE", "realized": 100.0}]
    try:
        first = aj_council.reflect_due(aj_config.get_config())
        assert len(first) == 1
        assert abs(first[0]["alpha_return"] - 0.08) < 1e-9   # 0.10 raw - 0.02 SPY
        second = aj_council.reflect_due(aj_config.get_config())
        assert second == []                                   # dedup by run id
    finally:
        (aj_council._invested_usd, aj_council._spy_return_since,
         aj_positions.paper_book, aj_analytics.attribution) = saved
    aj_config.set_config({"daily_reflection": False})


def test_reflect_due_gated_off_by_default():
    aj_config.set_config({"daily_reflection": False})
    assert aj_council.reflect_due(aj_config.get_config()) == []


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_council_phase4 — {} tests".format(len(fns)))
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
    print("PASS" if failed == 0 else "{} FAILED".format(failed))
    sys.exit(1 if failed else 0)
