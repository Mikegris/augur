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


def test_risk_vote_majority_and_skips_unparseable():
    # ENHANCEMENT B (unit): tolerant RiskStance parsing + strict-majority vote.
    turns = [
        {"debate": "risk", "role": "aggressive", "round": 0,
         "content": '{"assessment":"cut","recommended_action":"SELL"}'},
        {"debate": "risk", "role": "conservative", "round": 0,
         "content": 'Sure! Here is my view: {"assessment":"exit","recommended_action":"SELL"}'},
        {"debate": "risk", "role": "neutral", "round": 0,
         "content": "no json here at all"},                 # unparseable => skipped
    ]
    v = aj_debate.risk_vote(turns)
    assert v["n_turns"] == 3 and v["n_parsed"] == 2
    assert v["votes"] == {"SELL": 2} and v["majority"] == "SELL"
    # 1-1-1 split => no majority; a lone stance => no majority (needs 2 votes)
    split = aj_debate.risk_vote([
        {"content": '{"recommended_action":"BUY"}'},
        {"content": '{"recommended_action":"SELL"}'},
        {"content": '{"recommended_action":"HOLD"}'},
    ])
    assert split["majority"] is None and split["n_parsed"] == 3
    lone = aj_debate.risk_vote([{"content": '{"recommended_action":"BUY"}'}])
    assert lone["majority"] is None
    # parsed JSON with NO explicit action key must not fabricate a HOLD vote
    noact = aj_debate.risk_vote([{"content": '{"assessment":"hmm"}'}] * 3)
    assert noact["n_parsed"] == 0 and noact["majority"] is None
    assert aj_debate.risk_vote([]) == {"votes": {}, "n_parsed": 0, "n_turns": 0,
                                       "majority": None}


def _vote_fallback_call(manager_rec, risk_actions, arbiter_text):
    """Role-aware fake: risk debaters vote per `risk_actions`; the arbiter
    returns `arbiter_text` (prose => unparseable => council falls back)."""
    def call(tier, role, system, prompt):
        if "research.bull" in role or "research.bear" in role:
            return "argument"
        if "research_manager" in role:
            return '{"recommendation":"%s","rationale":"r"}' % manager_rec
        if "trader" in role:
            return '{"action":"%s","reasoning":"t"}' % (
                "BUY" if manager_rec in ("BUY", "OVERWEIGHT") else
                "SELL" if manager_rec in ("SELL", "UNDERWEIGHT") else "HOLD")
        for persp, act in risk_actions.items():
            if "risk." + persp in role:
                return '{"assessment":"a","recommended_action":"%s"}' % act
        if "arbiter" in role:
            return arbiter_text
        return None
    return call


def test_arbiter_fallback_uses_risk_vote_majority():
    # ENHANCEMENT B (pipeline): arbiter output is prose (unparseable) => the
    # council prefers the deterministic 2-of-3 risk-stance majority over the
    # plan-derived action, and persists the tally in the decision audit payload.
    aj_config.set_config({"max_research_rounds": 1, "max_risk_rounds": 1})
    aj_council.clear_cache()
    _set_analysts({"fundamentals": (5.0, 0.7), "technical": (5.0, 0.7)})
    call = _vote_fallback_call(
        "HOLD", {"aggressive": "SELL", "conservative": "SELL", "neutral": "BUY"},
        "On reflection, it is hard to say.")     # prose: extract_json => {}
    dec = aj_council.run("VOTEFB", call=call)
    assert dec.action is Action.SELL, dec.action        # 2-of-3 SELL adopted
    assert "risk-vote fallback" in dec.dissent, dec.dissent
    import json as _j
    row = db.get_conn().execute(
        "SELECT decision_json FROM aj_council_runs WHERE symbol='VOTEFB' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    doc = _j.loads(row["decision_json"])
    assert doc.get("risk_vote", {}).get("majority") == "SELL", doc.get("risk_vote")
    assert doc["risk_vote"]["votes"] == {"SELL": 2, "BUY": 1}
    assert aj_db.verify_audit_chain()["ok"]


def test_arbiter_fallback_vote_never_contradicts_rating():
    # A BUY<->SELL flip against the rating-implied action is incoherent for
    # sizing (signed_score reads the rating) — the vote is recorded but the
    # implied action is kept, mirroring the arbiter's own reconciliation.
    aj_config.set_config({"max_research_rounds": 1, "max_risk_rounds": 1})
    aj_council.clear_cache()
    _set_analysts({"fundamentals": (5.0, 0.7), "technical": (5.0, 0.7)})
    call = _vote_fallback_call(
        "BUY", {"aggressive": "SELL", "conservative": "SELL", "neutral": "SELL"},
        "no json")
    dec = aj_council.run("VOTECON", call=call)
    assert dec.rating is Rating.BUY
    assert dec.action is Action.BUY, dec.action         # implied action kept
    assert "contradicts rating" in dec.dissent, dec.dissent


def test_arbiter_fallback_no_majority_keeps_plan_action():
    # 1-1-1 split => no majority => the existing plan-derived fallback stands.
    aj_config.set_config({"max_research_rounds": 1, "max_risk_rounds": 1})
    aj_council.clear_cache()
    _set_analysts({"fundamentals": (5.0, 0.7), "technical": (5.0, 0.7)})
    call = _vote_fallback_call(
        "HOLD", {"aggressive": "BUY", "conservative": "SELL", "neutral": "HOLD"},
        "no json")
    dec = aj_council.run("VOTESPL", call=call)
    assert dec.action is Action.HOLD, dec.action
    assert "risk-vote fallback" not in dec.dissent


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
    # run_id stamped on the arbiter (`final`) branch too
    assert dec.run_id is not None
    conn = db.get_conn()
    rid = conn.execute("SELECT id FROM aj_council_runs WHERE symbol='AAPL' "
                       "ORDER BY id DESC LIMIT 1").fetchone()["id"]
    assert dec.run_id == rid
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
    saved = (aj_council._closed_round_trip, aj_council._spy_return_since,
             aj_positions.paper_book, aj_analytics.attribution)
    # S006: return is realized P&L over THIS round-trip's own basis (1000), and
    # S007: benchmark spans the round-trip's holding window (first buy→last sell).
    seen = {"end": "sentinel"}
    aj_council._closed_round_trip = lambda s: ({
        "basis_usd": 1000.0, "realized_usd": 100.0,
        "first_buy_ts": "2026-01-01T00:00:00Z", "last_sell_ts": "2026-02-01T00:00:00Z"}
        if s == "RDUE" else None)

    def _spy(ts, end=None):
        seen["end"] = end                       # capture the holding-window end
        return 0.02
    aj_council._spy_return_since = _spy
    aj_positions.paper_book = lambda *a, **k: {"positions": {}}      # closed
    aj_analytics.attribution = lambda: [{"symbol": "RDUE", "realized": 100.0}]
    try:
        first = aj_council.reflect_due(aj_config.get_config())
        assert len(first) == 1
        assert abs(first[0]["alpha_return"] - 0.08) < 1e-9   # 0.10 raw - 0.02 SPY
        assert seen["end"] == "2026-02-01T00:00:00Z"         # S007 horizon passed
        second = aj_council.reflect_due(aj_config.get_config())
        assert second == []                                   # dedup by run id
    finally:
        (aj_council._closed_round_trip, aj_council._spy_return_since,
         aj_positions.paper_book, aj_analytics.attribution) = saved
    aj_config.set_config({"daily_reflection": False})


def test_closed_round_trip_scopes_basis_to_one_round_trip():
    # Two separate round-trips for the SAME symbol. The all-time _invested_usd
    # would sum BOTH buys' basis (double-count); _closed_round_trip must scope to
    # the LAST closed round-trip only (S006).
    sym = "RTRIP"
    p1 = aj_db.insert("aj_proposals", created_at=aj_db.utc_now_iso(), cycle_id="c",
                      symbol=sym, side="buy", order_type="market")
    def _order(side, qty):
        import os as _os
        return aj_db.insert(
            "aj_orders", proposal_id=p1, client_order_id=_os.urandom(6).hex(),
            broker="paper", mode="paper", symbol=sym, side=side, qty=qty,
            order_type="market", state="filled", created_at=aj_db.utc_now_iso())
    def _fill(oid, qty, price, ts):
        aj_db.insert("aj_fills", order_id=oid, qty=qty, price=price,
                     fees_usd=0.0, filled_at=ts)
    # round-trip 1: buy 10@100, sell 10@110  (basis 1000)
    _fill(_order("buy", 10), 10, 100, "2026-01-01T00:00:00Z")
    _fill(_order("sell", 10), 10, 110, "2026-01-10T00:00:00Z")
    # round-trip 2: buy 5@200, sell 5@180    (basis 1000, realized -100)
    _fill(_order("buy", 5), 5, 200, "2026-02-01T00:00:00Z")
    _fill(_order("sell", 5), 5, 180, "2026-02-10T00:00:00Z")
    rt = aj_council._closed_round_trip(sym)
    assert rt is not None
    assert abs(rt["basis_usd"] - 1000.0) < 1e-6       # only RT2's basis, not 2000
    assert abs(rt["realized_usd"] - (-100.0)) < 1e-6  # RT2 P&L only
    assert rt["first_buy_ts"] == "2026-02-01T00:00:00Z"
    assert rt["last_sell_ts"] == "2026-02-10T00:00:00Z"
    # lifetime helper still double-counts (kept for back-compat) — proves the fix
    assert abs(aj_council._invested_usd(sym) - 2000.0) < 1e-6


def test_closed_round_trip_none_when_still_open():
    sym = "OPENPOS"
    p = aj_db.insert("aj_proposals", created_at=aj_db.utc_now_iso(), cycle_id="c",
                     symbol=sym, side="buy", order_type="market")
    import os as _os
    oid = aj_db.insert("aj_orders", proposal_id=p, client_order_id=_os.urandom(6).hex(),
                       broker="paper", mode="paper", symbol=sym, side="buy", qty=10,
                       order_type="market", state="filled", created_at=aj_db.utc_now_iso())
    aj_db.insert("aj_fills", order_id=oid, qty=10, price=100, fees_usd=0.0,
                 filled_at="2026-03-01T00:00:00Z")   # bought, never sold
    assert aj_council._closed_round_trip(sym) is None


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
