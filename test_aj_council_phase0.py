#!/usr/bin/env python3
"""Phase 0 tests — Analyst Council scaffolding (schemas, config, DB, routing).

Covers: schema tolerant parsing + clamping + audit/render; config council keys
(defaults OFF, policy enum sanitize, double-gating via VERIFY-COUNCIL); aj_db V3
migration idempotency + council tables present + generic insert allowed; routing
deep/quick tier tagging. No network, no LLM, fresh temp DB.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajp0.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_routing               # noqa: E402
import aj_schemas as S          # noqa: E402

# Ensure base + council schema exists for every test regardless of run order.
aj_db.aj_init()


def test_rating_action_band_parse():
    assert S.parse_rating("buy") is S.Rating.BUY
    assert S.parse_rating("Strong Sell") is S.Rating.SELL
    assert S.parse_rating("garbage") is S.Rating.HOLD          # tolerant default
    assert S.parse_action("LONG") is S.Action.BUY
    assert S.parse_action("reduce") is S.Action.SELL
    assert S.parse_band("very_bullish") is S.SentimentBand.VERY_BULLISH


def test_analyst_report_from_messy_llm():
    txt = "Here you go:\n```json\n{\"band\":\"BULLISH\",\"score\":99,\"confidence\":2,\"key_points\":[\"a\",\"b\"]}\n```"
    r = S.AnalystReport.from_llm("technical", txt, band_is_sentiment=True)
    assert r.score == 10.0           # clamped 99 -> 10
    assert r.confidence == 1.0       # clamped 2 -> 1
    assert r.band == "BULLISH"
    assert r.key_points == ["a", "b"]
    assert "technical" in r.render()
    assert r.to_audit()["analyst"] == "technical"


def test_analyst_report_non_json_falls_back_to_narrative():
    r = S.AnalystReport.from_llm("news", "no json here, just prose about catalysts")
    assert r.narrative.startswith("no json")
    assert 0.0 <= r.score <= 10.0


def test_research_plan_and_council_decision_derivation():
    plan = S.ResearchPlan.from_llm('{"recommendation":"BUY","rationale":"momentum","strategic_actions":["scale in"]}')
    assert plan.recommendation is S.Rating.BUY
    dec = S.CouncilDecision.from_plan("aapl", plan, confidence=1.0)
    assert dec.symbol == "AAPL"
    assert dec.action is S.Action.BUY
    assert dec.conviction == 1.0           # BUY mag(1.0) * conf(1.0)
    assert dec.signed_score() > 0.99
    # HOLD => low conviction
    hold = S.CouncilDecision.from_plan("aapl", S.ResearchPlan(recommendation=S.Rating.HOLD), confidence=1.0)
    assert hold.conviction == 0.0
    assert hold.action is S.Action.HOLD


def test_council_decision_clamps_and_audit_roundtrip():
    d = S.CouncilDecision(symbol="msft", rating="OVERWEIGHT", action="buy",
                          conviction=5.0, price_target=-3, stop_hint=0, cost_usd=-1)
    assert d.conviction == 1.0
    assert d.price_target is None        # negative rejected
    assert d.stop_hint is None
    assert d.cost_usd == 0.0
    a = d.to_audit()
    assert a["rating"] == "OVERWEIGHT" and a["action"] == "BUY"


def test_config_council_keys_default_off():
    cfg = aj_config.get_config()
    assert cfg["council_enabled"] is False
    assert cfg["council_policy"] == "advisory"
    assert cfg["council_topk"] == 3
    assert cfg["council_max_calls_per_cycle"] == 40
    # analyst toggles default on (only matter when council is enabled)
    assert cfg["council_analyst_fundamentals"] is True


def test_config_policy_enum_sanitizes():
    aj_config.set_config({"council_policy": "YOLO"})
    assert aj_config.get_config()["council_policy"] == "advisory"   # unknown => safe
    aj_config.set_config({"council_policy": "coequal"})
    assert aj_config.get_config()["council_policy"] == "coequal"
    aj_config.set_config({"council_policy": "advisory"})


def test_council_double_gating():
    # enabled but VERIFY not passed => not active (fail-closed)
    aj_config.set_config({"council_enabled": True})
    assert aj_config.council_verify_passed() is False
    assert aj_config.council_active() is False
    # close the gate => active
    db.set_setting("aj_verify_council", "pass")
    assert aj_config.council_active() is True
    # disable config => inactive even with gate
    aj_config.set_config({"council_enabled": False})
    assert aj_config.council_active() is False
    aj_config.set_config({"council_enabled": True})


def test_db_v3_migration_idempotent_and_tables_present():
    aj_db.aj_init()
    assert aj_db._applied_version() >= 3
    # idempotent re-run
    assert aj_db.aj_migrate() >= 3
    conn = db.get_conn()
    for t in ("aj_council_runs", "aj_analyst_reports", "aj_debate_turns", "aj_reflections"):
        n = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        assert n is not None, "missing table " + t


def test_db_insert_allowed_for_council_tables():
    rid = aj_db.insert("aj_council_runs", ts=aj_db.utc_now_iso(), symbol="AAPL",
                       policy="advisory", status="ok", rating="BUY", action="BUY",
                       conviction=0.8)
    assert rid > 0
    aj_db.insert("aj_reflections", ts=aj_db.utc_now_iso(), symbol="AAPL",
                 raw_return=0.05, alpha_return=0.02, benchmark="SPY", lesson="ok")
    # aj_audit still rejected via generic insert
    try:
        aj_db.insert("aj_audit", ts="x", kind="y", payload_json="{}")
        assert False, "aj_audit should be rejected by generic insert"
    except ValueError:
        pass


def test_routing_tier_tags_role():
    calls = {}
    orig = aj_routing.route
    def fake_route(messages, **kw):
        calls.update(kw)
        return {"ok": True, "text": "ok", "model": "stub"}
    aj_routing.route = fake_route
    try:
        aj_routing.complete_tiered("hi", tier=aj_routing.DEEP, role="analyst.fundamentals")
        assert calls["role"].endswith("#deep")
        assert calls["max_tokens"] == 1024
        aj_routing.complete_tiered("hi", tier=aj_routing.QUICK, role="x")
        assert calls["role"].endswith("#quick")
        assert calls["max_tokens"] == 512
        # unknown tier falls back to deep
        aj_routing.complete_tiered("hi", tier="bogus", role="x")
        assert calls["role"].endswith("#deep")
    finally:
        aj_routing.route = orig


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_council_phase0 — {} tests".format(len(fns)))
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
