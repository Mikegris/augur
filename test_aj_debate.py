#!/usr/bin/env python3
"""Phase 2 tests — research debate, research manager, memory log.

Network-free: analysts injected, completions injected (role-aware fake), memory
log redirected to the temp DB dir.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajdebate.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_analysts              # noqa: E402
import aj_council               # noqa: E402
import aj_debate                # noqa: E402
import aj_memory                # noqa: E402
from aj_schemas import AnalystReport, Rating  # noqa: E402

aj_db.aj_init()
aj_memory.clear()


def _reports(*specs):
    return [AnalystReport(analyst=n, band="NEUTRAL", score=s, confidence=c,
                          key_points=[n + " pt"], narrative=n) for n, s, c in specs]


def _role_call(manager_rec="BUY"):
    """Role-aware fake completion: bull/bear return arguments, manager returns
    a JSON ResearchPlan."""
    def call(tier, role, system, prompt):
        if "research.bull" in role:
            return "Bull: strong momentum and a beat."
        if "research.bear" in role:
            return "Bear: stretched valuation, rebutting the bull."
        if "research_manager" in role:
            return '{"recommendation":"%s","rationale":"debate net bullish","strategic_actions":["scale in"]}' % manager_rec
        return None
    return call


# ── memory log ────────────────────────────────────────────────────────────────

def test_memory_append_and_recall_same_symbol():
    aj_memory.clear()
    assert aj_memory.append("AAPL", "decision", "BUY conviction=80%")
    assert aj_memory.append("AAPL", "decision", "HOLD next day")
    got = aj_memory.recall("AAPL", n_same=5)
    assert len(got) == 2
    assert "HOLD next day" in got[0]   # reverse-chronological (most recent first)


def test_memory_cross_symbol_lessons_only():
    aj_memory.clear()
    aj_memory.append("AAPL", "decision", "aapl decision")
    aj_memory.append("MSFT", "lesson", "msft lesson learned")
    aj_memory.append("NVDA", "decision", "nvda decision (not a lesson)")
    got = aj_memory.recall("AAPL", n_same=5, n_cross=3)
    joined = "\n".join(got)
    assert "msft lesson learned" in joined        # cross-symbol lesson included
    assert "nvda decision" not in joined          # cross-symbol non-lesson excluded


def test_memory_empty_when_absent():
    aj_memory.clear()
    assert aj_memory.recall("ZZZZ") == []
    assert aj_memory.recall_text("ZZZZ") == ""


# ── debate ────────────────────────────────────────────────────────────────────

def test_debate_hard_termination_by_rounds():
    reports = _reports(("technical", 7, 0.8))
    turns = aj_debate.research_debate("AAPL", reports, "", _role_call(), rounds=2)
    assert len(turns) == 4                         # 2 rounds * 2 sides
    assert [t["role"] for t in turns] == ["bull", "bear", "bull", "bear"]


def test_debate_stops_early_on_none():
    reports = _reports(("technical", 7, 0.8))
    only_bull = lambda tier, role, system, prompt: ("arg" if "bull" in role else None)
    turns = aj_debate.research_debate("AAPL", reports, "", only_bull, rounds=2)
    assert len(turns) == 1 and turns[0]["role"] == "bull"   # bear None => stop


def test_debate_zero_rounds_no_turns():
    assert aj_debate.research_debate("AAPL", _reports(("t", 7, 0.8)), "", _role_call(), rounds=0) == []


def test_manager_returns_plan_and_none_without_turns():
    reports = _reports(("technical", 7, 0.8))
    turns = aj_debate.research_debate("AAPL", reports, "", _role_call("OVERWEIGHT"), rounds=1)
    plan = aj_debate.research_manager("AAPL", reports, turns, _role_call("OVERWEIGHT"))
    assert plan is not None and plan.recommendation is Rating.OVERWEIGHT
    assert aj_debate.research_manager("AAPL", reports, [], _role_call()) is None


# ── council with debate ───────────────────────────────────────────────────────

def _set_analysts(specs):
    def mk(s, c, n):
        return lambda symbol, call=None, cfg=None: AnalystReport(
            analyst=n, band="NEUTRAL", score=s, confidence=c, key_points=[n], narrative=n)
    aj_analysts.ANALYSTS = {n: mk(s, c, n) for n, (s, c) in specs.items()}


def test_council_uses_debate_plan_over_consensus():
    aj_config.set_config({"max_research_rounds": 1})
    aj_council.clear_cache()
    # analysts are mildly bearish (consensus would be HOLD/UNDERWEIGHT) but the
    # debate manager says BUY -> the council adopts the manager's stance.
    _set_analysts({"fundamentals": (4.5, 0.7), "technical": (4.0, 0.7)})
    dec = aj_council.run("AAPL", call=_role_call("BUY"))
    assert dec.rating is Rating.BUY, dec.rating
    assert dec.status == "ok"
    # debate turns persisted
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) c FROM aj_debate_turns").fetchone()["c"]
    assert n >= 2
    assert aj_db.verify_audit_chain()["ok"]


def test_council_falls_back_to_consensus_when_debate_unavailable():
    aj_config.set_config({"max_research_rounds": 1})
    aj_council.clear_cache()
    _set_analysts({"fundamentals": (9.0, 0.9), "technical": (8.5, 0.9)})
    # call returns None for everything (no model) -> debate can't run
    dec = aj_council.run("NVDA", call=lambda *a, **k: None)
    assert dec.rating is Rating.BUY            # consensus preserved
    assert dec.status == "degraded"            # debate requested but couldn't run


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_debate — {} tests".format(len(fns)))
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
