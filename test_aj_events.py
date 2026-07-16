#!/usr/bin/env python3
"""Event-alpha engine tests (aj_events) — fully offline.

News/filings sources and the LLM router are stubbed; what's under test is the
engine: idempotent ingestion, budget-capped scoring with clamping and
retry-capping, the POINT-IN-TIME signal (sim-clock aware, published_at
filtered, half-life decay), outcome grading, and the adapter contract.
"""
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajevents.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_events                # noqa: E402
import fetcher                  # noqa: E402
import aj_routing               # noqa: E402

aj_db.aj_init()

CFG = {"event_alpha_enabled": True, "event_max_llm_per_cycle": 5,
       "event_symbols_per_cycle": 10}

_NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)


def _reset():
    conn = db.get_conn()
    conn.execute("DELETE FROM aj_events")
    conn.commit()
    aj_db.set_sim_clock(None)


def _stub_sources(news=None, filings=None):
    fetcher.get_news = lambda s, n=8: list(news or [])
    import sec_edgar
    sec_edgar.get_recent_filings = lambda t, forms=None, limit=6: list(filings or [])
    fetcher.get_quote = lambda s: {"price": 100.0}
    fetcher.get_quotes_batch = lambda syms: {s: {"price": 110.0} for s in syms}


def _stub_llm(direction=0.6, confidence=0.7, half_life=5, ok=True, text=None):
    def fake(prompt, system=None, **kw):
        if not ok:
            return {"ok": False}
        t = text if text is not None else json.dumps(
            {"direction": direction, "confidence": confidence,
             "half_life_days": half_life, "magnitude": "medium",
             "rationale": "test"})
        return {"ok": True, "text": t, "cost_usd": 0.001, "chosen_model": "stub"}
    aj_routing.complete = fake


def test_ingest_idempotent_and_scoring_budget():
    _reset()
    _stub_sources(news=[{"title": "Earnings beat", "url": "u1",
                         "pub_date": "2026-03-09T12:00:00+00:00"},
                        {"title": "Guidance raised", "url": "u2",
                         "pub_date": "2026-03-09T13:00:00+00:00"}],
                  filings=[{"form": "8-K", "accession": "acc1",
                            "filing_date": "2026-03-08",
                            "items": "2.02 Results of Operations"}])
    _stub_llm()
    r1 = aj_events.ingest_and_score(["AAPL"], CFG)
    assert r1["ingested"] == 3 and r1["scored"] == 3, r1
    # second pass: nothing new ingested, nothing left to score
    r2 = aj_events.ingest_and_score(["AAPL"], CFG)
    assert r2["ingested"] == 0 and r2["scored"] == 0, r2
    rows = aj_db.query("SELECT * FROM aj_events ORDER BY id")
    assert len(rows) == 3
    assert all(r["scored_at"] for r in rows)
    assert all(abs(float(r["direction"]) - 0.6) < 1e-9 for r in rows)
    assert all(r["price_at"] == 100.0 for r in rows)


def test_scoring_clamps_and_budget_cap():
    _reset()
    news = [{"title": "n{}".format(i), "url": "x{}".format(i),
             "pub_date": "2026-03-09T12:00:00+00:00"} for i in range(8)]
    _stub_sources(news=news)
    _stub_llm(direction=5.0, confidence=9.0, half_life=999)   # out-of-range
    cfg = dict(CFG); cfg["event_max_llm_per_cycle"] = 3
    r = aj_events.ingest_and_score(["MSFT"], cfg)
    assert r["ingested"] == 8 and r["scored"] == 3, r      # budget respected
    row = aj_db.query("SELECT * FROM aj_events WHERE scored_at IS NOT NULL")[0]
    assert float(row["direction"]) == 1.0                   # clamped
    assert float(row["confidence"]) == 1.0
    assert float(row["half_life_days"]) == 30.0


def test_unscorable_retries_then_neutralized():
    _reset()
    _stub_sources(news=[{"title": "garbled", "url": "g1",
                         "pub_date": "2026-03-09T12:00:00+00:00"}])
    _stub_llm(text="not json at all")
    for _ in range(3):
        aj_events.ingest_and_score(["NVDA"], CFG)
    row = aj_db.query("SELECT * FROM aj_events")[0]
    assert row["score_tries"] >= 3 and row["scored_at"] is not None
    assert float(row["confidence"]) == 0.0     # neutralized, never re-scored
    # neutral events produce NO signal
    assert aj_events.event_signal("NVDA") is None


def test_signal_pit_and_decay():
    _reset()
    # two scored events: one fresh strong positive, one ancient (decayed)
    def _put(sym, pub, direction, conf, hl):
        aj_db.insert("aj_events", symbol=sym, event_type="news",
                     source_id="s" + pub, published_at=pub,
                     ingested_at=pub, title="t", summary="",
                     direction=direction, confidence=conf,
                     half_life_days=hl, magnitude="medium",
                     scored_at=pub)
    _put("AAPL", "2026-03-09T12:00:00+00:00", 0.8, 0.8, 5.0)   # 1 day old
    _put("AAPL", "2025-11-01T12:00:00+00:00", -1.0, 1.0, 5.0)  # ~130d: dead
    aj_db.set_sim_clock(_NOW)
    try:
        sig = aj_events.event_signal("AAPL")
        assert sig is not None and sig["source"] == "events"
        assert sig["prob_up"] > 0.55, sig       # fresh positive dominates
        # PIT: a sim clock BEFORE both events sees nothing (no look-ahead)
        aj_db.set_sim_clock(datetime(2025, 10, 1, tzinfo=timezone.utc))
        assert aj_events.event_signal("AAPL") is None
        # a sim clock between them sees only the (now-decayed) old negative
        aj_db.set_sim_clock(datetime(2025, 11, 2, tzinfo=timezone.utc))
        sig2 = aj_events.event_signal("AAPL")
        assert sig2 is not None and sig2["prob_up"] < 0.5, sig2
    finally:
        aj_db.set_sim_clock(None)


def test_outcome_grading_hits_and_neutral_skipped():
    _reset()
    old = (_NOW - timedelta(days=30)).isoformat()
    aj_db.insert("aj_events", symbol="AAPL", event_type="news", source_id="o1",
                 published_at=old, ingested_at=old, title="up call",
                 direction=0.8, confidence=0.7, half_life_days=5.0,
                 magnitude="medium", scored_at=old, price_at=100.0)
    aj_db.insert("aj_events", symbol="AAPL", event_type="news", source_id="o2",
                 published_at=old, ingested_at=old, title="neutral note",
                 direction=0.0, confidence=0.2, half_life_days=5.0,
                 magnitude="small", scored_at=old, price_at=100.0)
    _stub_sources()   # quotes_batch -> 110.0 (+10%)
    aj_db.set_sim_clock(_NOW)
    try:
        r = aj_events.score_due_outcomes()
    finally:
        aj_db.set_sim_clock(None)
    assert r["graded"] == 2, r
    rows = {r["source_id"]: r for r in aj_db.query("SELECT * FROM aj_events")}
    assert rows["o1"]["hit"] == 1                      # predicted up, went up
    assert abs(rows["o1"]["realized_return_pct"] - 10.0) < 1e-6
    assert rows["o2"]["hit"] is None                   # neutral: not graded
    skill = aj_events.event_skill()
    assert skill["skill"]["news"]["n"] == 1
    assert skill["skill"]["news"]["hit_rate"] == 1.0


def test_disabled_flag_is_noop():
    _reset()
    _stub_sources(news=[{"title": "x", "url": "u",
                         "pub_date": "2026-03-09T12:00:00+00:00"}])
    _stub_llm()
    r = aj_events.ingest_and_score(["AAPL"], {"event_alpha_enabled": False})
    assert r == {"ingested": 0, "scored": 0, "cost_usd": 0.0}
    assert aj_db.query("SELECT COUNT(*) n FROM aj_events")[0]["n"] == 0


def test_adapter_registered_in_signals():
    import aj_signals
    names = [n for n, _ in aj_signals._ADAPTERS]
    assert "events" in names
    # adapter fail-open: empty table -> None, and all_signals survives
    _reset()
    out = aj_signals.events_signal("AAPL")
    assert out is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_events — {} tests".format(len(fns)))
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
