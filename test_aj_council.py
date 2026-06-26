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
