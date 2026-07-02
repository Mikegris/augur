#!/usr/bin/env python3
"""Phase 1 tests — Analyst Council orchestration (aj_council, aj_analysts).

Network-free: analyst engines are monkeypatched and the LLM completion is
injected. Covers consensus mapping, persistence + audit chain, gating
(active/force/VERIFY), the per-cycle budget cap, caching, and one analyst
adapter end-to-end with mocked engines.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajcouncil.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_routing               # noqa: E402
import aj_analysts              # noqa: E402
import aj_council               # noqa: E402
from aj_schemas import AnalystReport, CouncilDecision, Rating, Action  # noqa: E402

aj_db.aj_init()
# Phase 1 tests exercise the analyst-consensus path (no debate). Phase 2's
# debate has its own suite (test_aj_debate.py).
aj_config.set_config({"max_research_rounds": 0})


def _fake_analyst(score, conf, band="NEUTRAL", analyst="x"):
    def fn(symbol, call=None, cfg=None):
        return AnalystReport(analyst=analyst, band=band, score=score,
                             confidence=conf, key_points=[analyst + " point"],
                             narrative=analyst + " view")
    return fn


def _set_analysts(specs):
    """specs: {name: (score, conf)} -> monkeypatch ANALYSTS registry."""
    aj_analysts.ANALYSTS = {n: _fake_analyst(s, c, analyst=n) for n, (s, c) in specs.items()}


_ORIG = dict(aj_analysts.ANALYSTS)
_DUMMY = lambda *a, **k: None   # bypasses gating (call is not None)


def teardown():
    aj_analysts.ANALYSTS = dict(_ORIG)
    aj_council.clear_cache()


def test_consensus_bullish_to_buy():
    _set_analysts({"fundamentals": (9.0, 0.9), "technical": (8.0, 0.8)})
    aj_council.clear_cache()
    dec = aj_council.run("AAPL", call=_DUMMY)
    assert dec.rating is Rating.BUY, dec.rating
    assert dec.action is Action.BUY
    assert dec.conviction > 0.6
    assert dec.status == "ok"
    teardown()


def test_consensus_bearish_to_sell():
    _set_analysts({"fundamentals": (1.5, 0.9), "technical": (2.0, 0.8)})
    aj_council.clear_cache()
    dec = aj_council.run("XYZ", call=_DUMMY)
    assert dec.rating is Rating.SELL, dec.rating
    assert dec.action is Action.SELL
    teardown()


def test_dissent_flagged_on_wide_spread():
    _set_analysts({"fundamentals": (9.0, 0.9), "sentiment": (1.0, 0.9)})
    aj_council.clear_cache()
    dec = aj_council.run("MSFT", call=_DUMMY)
    assert dec.dissent, "expected dissent on a 9-vs-1 split"
    teardown()


def test_persistence_and_audit_chain():
    _set_analysts({"fundamentals": (7.5, 0.8), "news": (7.0, 0.7)})
    aj_council.clear_cache()
    dec = aj_council.run("NVDA", cycle_id="cyc1", call=_DUMMY)
    conn = db.get_conn()
    row = conn.execute("SELECT symbol, rating, status FROM aj_council_runs WHERE symbol='NVDA'").fetchone()
    assert row is not None and row["rating"] == dec.rating.value
    nrep = conn.execute("SELECT COUNT(*) c FROM aj_analyst_reports").fetchone()["c"]
    assert nrep >= 2
    chain = aj_db.verify_audit_chain()
    assert chain["ok"], chain
    teardown()


def test_run_id_set_on_decision():
    # run() stamps the persisted aj_council_runs.id onto the returned decision so
    # callers can fetch this exact run's artifacts without racing "latest run".
    _set_analysts({"fundamentals": (7.5, 0.8), "news": (7.0, 0.7)})
    aj_council.clear_cache()
    dec = aj_council.run("RIDSET", cycle_id="cyc-rid", call=_DUMMY)
    assert dec.run_id is not None
    row = db.get_conn().execute(
        "SELECT id FROM aj_council_runs WHERE symbol='RIDSET' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None and dec.run_id == row["id"]
    teardown()


def test_gating_not_active_returns_skipped():
    # call=None path: council disabled by default => skipped
    aj_config.set_config({"council_enabled": False})
    dec = aj_council.run("AAPL")
    assert dec.status == "skipped"
    teardown()


def test_force_requires_verify_gate():
    db.set_setting("aj_verify_council", "not-passed")
    dec = aj_council.run("AAPL", force=True)
    assert dec.status == "skipped" and "VERIFY" in dec.dissent
    db.set_setting("aj_verify_council", "pass")
    teardown()


def test_budget_cap_blocks_calls():
    b = aj_council.Budget(max_calls=2)
    assert not b.exhausted()
    b.n_calls = 2
    assert b.exhausted() and b.hit_cap
    # _make_call returns None once exhausted (no LLM hit)
    calls = {"n": 0}
    orig = aj_routing.complete_tiered
    aj_routing.complete_tiered = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or {"ok": True, "text": "x"})
    try:
        cfg = aj_config.get_config()
        b2 = aj_council.Budget(max_calls=0)
        call = aj_council._make_call(cfg, b2, None)
        assert call("deep", "r", "s", "p") is None
        assert calls["n"] == 0   # never reached the model
    finally:
        aj_routing.complete_tiered = orig
    teardown()


def test_make_call_threads_json_mode_to_router():
    # ENHANCEMENT A: strict-JSON roles request native JSON output. The council's
    # CallFn takes an OPTIONAL json_mode kwarg (default False — existing callers
    # unchanged); aj_routing.call_json passes json_mode=True when supported and
    # falls back to the plain 4-arg invocation for legacy fakes.
    seen = {}
    orig = aj_routing.complete_tiered
    aj_routing.complete_tiered = lambda *a, **k: (
        seen.update(k) or {"ok": True, "text": "{}", "cost_usd": 0.0})
    try:
        call = aj_council._make_call(aj_config.get_config(), aj_council.Budget(5), None)
        assert aj_routing.call_json(call, "deep", "council.analyst.x", "s", "p") == "{}"
        assert seen.get("json_mode") is True, seen
        # default path (no call_json) keeps json_mode off
        seen.clear()
        assert call("deep", "council.reflect", "s", "p") == "{}"
        assert seen.get("json_mode") is False
    finally:
        aj_routing.complete_tiered = orig
    # legacy 4-positional fakes must be invoked WITHOUT the kwarg (no TypeError)
    legacy = lambda tier, role, system, prompt: "legacy-ok"
    assert aj_routing.call_json(legacy, "deep", "r", "s", "p") == "legacy-ok"
    teardown()


def test_hit_rate_weighting_cold_start_neutral():
    # ENHANCEMENT C: below _HIT_RATE_MIN_SAMPLES resolved samples the skill
    # multiplier is a NEUTRAL 1.0x — consensus must be bit-identical to the
    # historical pure-confidence weighting. With warm (>=10 samples) skill data
    # the proven analyst dominates. Any hit-rate error => fail-open to baseline.
    import time as _t
    reports = [AnalystReport(analyst="fundamentals", band="NEUTRAL", score=8.0,
                             confidence=0.9, key_points=["f"], narrative="f"),
               AnalystReport(analyst="technical", band="NEUTRAL", score=3.0,
                             confidence=0.5, key_points=["t"], narrative="t")]
    memo = aj_council._hit_rate_memo
    try:
        memo["rates"], memo["exp"] = {}, _t.time() + 60
        base = aj_council._consensus(reports)
        assert base[0] is Rating.OVERWEIGHT       # (8*.9+3*.5)/1.4 ≈ 6.21
        # cold start: thin samples (n < 10) => identical rating AND conviction
        memo["rates"] = {"fundamentals": (1.0, 9), "technical": (0.0, 3)}
        cold = aj_council._consensus(reports)
        assert cold[0] is base[0] and abs(cold[1] - base[1]) < 1e-12
        # warm skill data: perfect fundamentals (1.5x) vs coin-toss-worse
        # technical (0.5x) => avg ≈ 7.22 => rating strengthens to BUY
        memo["rates"] = {"fundamentals": (1.0, 30), "technical": (0.0, 30)}
        warm = aj_council._consensus(reports)
        assert warm[0] is Rating.BUY, warm[0]
        # fail-open: a raising hit-rate helper must yield the baseline result
        orig = aj_council._analyst_hit_rates
        aj_council._analyst_hit_rates = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            safe = aj_council._consensus(reports)
            assert safe[0] is base[0] and abs(safe[1] - base[1]) < 1e-12
        finally:
            aj_council._analyst_hit_rates = orig
    finally:
        memo["rates"], memo["exp"] = {}, 0.0      # drop the memo for other tests
    teardown()


def test_cache_key_varies_with_council_models():
    # ENHANCEMENT C2: swapping the deep/quick tier model must produce a fresh
    # cache key so the old model's cached advice is never served for the TTL.
    cfg = dict(aj_config.get_config())
    k_a = aj_council._cache_key("AAPL", dict(cfg, council_deep_model="model-a"))
    k_b = aj_council._cache_key("AAPL", dict(cfg, council_deep_model="model-b"))
    k_q = aj_council._cache_key("AAPL", dict(cfg, council_quick_model="mini-x"))
    assert k_a != k_b and k_a != k_q
    assert k_a == aj_council._cache_key("AAPL", dict(cfg, council_deep_model="model-a"))
    teardown()


def test_cache_returns_same_decision():
    _set_analysts({"fundamentals": (8.0, 0.9)})
    aj_council.clear_cache()
    aj_config.set_config({"council_cache_ttl_min": 360})
    d1 = aj_council.run("CACHE", call=_DUMMY)
    # mutate the analyst; cached decision should be returned unchanged
    _set_analysts({"fundamentals": (1.0, 0.9)})
    d2 = aj_council.run("CACHE", call=_DUMMY)
    assert d1.rating is d2.rating, "cache should return the first decision"
    teardown()


def test_inflight_dedup_coalesces_concurrent_runs():
    # S004: concurrent identical run() calls coalesce — the first computes, the
    # rest wait and read the cache (so analysts run once, not per-thread).
    import threading
    aj_config.set_config({"council_cache_ttl_min": 360})
    aj_council.clear_cache()
    runs = {"n": 0}
    gate = threading.Event()

    def slow_analyst(symbol, call=None, cfg=None):
        runs["n"] += 1
        gate.wait(timeout=5)        # hold the leader until all threads are waiting
        return AnalystReport(analyst="f", band="NEUTRAL", score=8.0, confidence=0.9,
                             key_points=["pt"], narrative="v")
    aj_analysts.ANALYSTS = {"fundamentals": slow_analyst}

    results = []
    def worker():
        results.append(aj_council.run("FLIGHT", call=_DUMMY))
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    import time as _t
    _t.sleep(0.3)                   # let the leader enter the analyst + others wait
    gate.set()                      # release the leader
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 4
    assert runs["n"] == 1, "analysts should run once (coalesced), got {}".format(runs["n"])
    assert all(r.rating is results[0].rating for r in results)
    teardown()


def test_inflight_event_released_on_error():
    # The in-flight Event must always be signaled (finally) so an error in the
    # leader never deadlocks subsequent runs for the same key.
    aj_config.set_config({"council_cache_ttl_min": 360})
    aj_council.clear_cache()

    def boom_analyst(symbol, call=None, cfg=None):
        raise RuntimeError("boom")
    # _run_analysts swallows analyst crashes, but force a harder failure path:
    orig_consensus = aj_council._consensus
    aj_council._consensus = lambda reports: (_ for _ in ()).throw(RuntimeError("boom"))
    aj_analysts.ANALYSTS = {"fundamentals": boom_analyst}
    try:
        raised = False
        try:
            aj_council.run("ERRKEY", call=_DUMMY)
        except RuntimeError:
            raised = True
        assert raised
        # in-flight registry must be empty (Event released in finally)
        assert "ERRKEY" not in "".join(aj_council._inflight.keys())
    finally:
        aj_council._consensus = orig_consensus
    teardown()


def test_technical_analyst_with_mocked_engines():
    import fetcher
    orig_chart = getattr(fetcher, "get_chart_data", None)
    orig_ind = getattr(fetcher, "compute_indicators", None)
    fetcher.get_chart_data = lambda s, p="6mo", i="1d": [{"close": 100, "volume": 1}]
    fetcher.compute_indicators = lambda bars: {"rsi": 55, "macd": 1.2, "current_price": 100,
                                               "price_vs_sma50": 3.0}
    canned = '{"band":"BULLISH","score":8,"confidence":0.7,"key_points":["uptrend"],"narrative":"strong"}'
    call = lambda tier, role, system, prompt: canned
    try:
        rep = aj_analysts.technical("AAPL", call=call, cfg={"forecast_horizon_days": 20})
        assert rep.score == 8.0 and rep.confidence == 0.7
        assert "indicators" in rep.evidence_refs
    finally:
        if orig_chart:
            fetcher.get_chart_data = orig_chart
        if orig_ind:
            fetcher.compute_indicators = orig_ind
    teardown()


def test_analyst_neutral_when_no_evidence():
    # ALL evidence engines raise => fail-open => no evidence => neutral, low conf,
    # and the LLM is never called (nothing to analyze).
    import fetcher, earnings, smart_money
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    saved = (fetcher.get_fundamentals, earnings.get_earnings_dossier, smart_money.compute_score)
    fetcher.get_fundamentals = boom
    earnings.get_earnings_dossier = boom
    smart_money.compute_score = boom
    called = {"n": 0}
    call = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "{}"
    try:
        rep = aj_analysts.fundamentals("AAPL", call=call, cfg={})
        assert rep.confidence <= 0.2, rep.confidence
        assert called["n"] == 0, "no evidence => model must not be called"
    finally:
        fetcher.get_fundamentals, earnings.get_earnings_dossier, smart_money.compute_score = saved
    teardown()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_council — {} tests".format(len(fns)))
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
