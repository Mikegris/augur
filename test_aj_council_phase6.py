#!/usr/bin/env python3
"""Phase 6 tests — personas, optional FinGPT prior, council report.

Network-free: engines + completion injected.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajp6.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_analysts              # noqa: E402
import aj_council               # noqa: E402
import aj_personas              # noqa: E402
from aj_schemas import AnalystReport, CouncilDecision, Rating, Action  # noqa: E402

aj_db.aj_init()


def test_personas_empty_unless_enabled():
    assert aj_personas.persona_analysts({"personas_enabled": False}) == {}
    ps = aj_personas.persona_analysts({"personas_enabled": True})
    assert set(ps.keys()) >= {"persona:value", "persona:growth", "persona:contrarian"}


def test_persona_analyst_produces_report():
    import fetcher
    saved = (getattr(fetcher, "get_fundamentals", None), getattr(fetcher, "get_chart_data", None),
             getattr(fetcher, "compute_indicators", None))
    fetcher.get_fundamentals = lambda s: {"pe_ratio": 12, "return_on_equity": 0.3}
    fetcher.get_chart_data = lambda s, p="6mo", i="1d": [{"close": 100}]
    fetcher.compute_indicators = lambda bars: {"rsi": 45, "current_price": 100}
    canned = '{"band":"BULLISH","score":7.5,"confidence":0.6,"key_points":["cheap+quality"],"narrative":"value buy"}'
    call = lambda tier, role, system, prompt: canned
    try:
        fn = aj_personas.persona_analysts({"personas_enabled": True})["persona:value"]
        rep = fn("AAPL", call=call, cfg={})
        assert rep.analyst == "persona:value"
        assert rep.score == 7.5 and rep.confidence == 0.6
    finally:
        fetcher.get_fundamentals, fetcher.get_chart_data, fetcher.compute_indicators = saved


def test_persona_neutral_when_no_evidence():
    import fetcher
    saved = getattr(fetcher, "get_fundamentals", None)
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    fetcher.get_fundamentals = boom
    sc = getattr(fetcher, "get_chart_data", None)
    fetcher.get_chart_data = boom
    called = {"n": 0}
    call = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "{}"
    try:
        fn = aj_personas.persona_analysts({"personas_enabled": True})["persona:value"]
        rep = fn("AAPL", call=call, cfg={})
        assert rep.confidence <= 0.2 and called["n"] == 0
    finally:
        fetcher.get_fundamentals = saved
        if sc:
            fetcher.get_chart_data = sc


def test_fingpt_sentiment_fails_open_without_stack():
    # No optional finbert/fingpt helper is installed, so the dependency-free
    # lexicon scorer is the functional default. Contract: never raises, never a
    # hard dependency, source is "lexicon" (not "fingpt"), and it returns None
    # when there are no headlines / no polarity cue.
    import fetcher
    saved = getattr(fetcher, "get_news", None)
    try:
        # no headlines -> None
        fetcher.get_news = lambda s, n=10: []
        assert aj_personas.fingpt_sentiment("AAPL") is None
        # headline with no polarity cue -> None (never fabricates a number)
        fetcher.get_news = lambda s, n=10: [{"title": "Company holds annual meeting"}]
        assert aj_personas.fingpt_sentiment("AAPL") is None
        # clearly bearish headlines -> a real lexicon-scored BEARISH prior
        fetcher.get_news = lambda s, n=10: [
            {"title": "Shares plunge as company misses estimates and warns on guidance"},
            {"title": "Analyst downgrade triggers selloff"}]
        out = aj_personas.fingpt_sentiment("AAPL")
        assert out is not None and out["source"] == "lexicon"
        assert out["label"] == "BEARISH" and out["score"] <= 4
    finally:
        if saved is not None:
            fetcher.get_news = saved


def test_council_includes_personas_when_enabled():
    # base analysts + 5 personas, all injected to avoid engines
    def base(symbol, call=None, cfg=None):
        return AnalystReport(analyst="technical", band="NEUTRAL", score=6, confidence=0.6)
    aj_analysts.ANALYSTS = {"technical": base}
    # personas reuse fetcher; mock it so they don't hit network
    import fetcher
    saved = (getattr(fetcher, "get_fundamentals", None), getattr(fetcher, "get_chart_data", None),
             getattr(fetcher, "compute_indicators", None))
    fetcher.get_fundamentals = lambda s: {"pe_ratio": 10}
    fetcher.get_chart_data = lambda s, p="6mo", i="1d": [{"close": 1}]
    fetcher.compute_indicators = lambda bars: {"rsi": 50}
    aj_config.set_config({"max_research_rounds": 0, "personas_enabled": True,
                          "council_analyst_news": False, "council_analyst_sentiment": False,
                          "council_analyst_fundamentals": False})
    aj_council.clear_cache()
    call = lambda tier, role, system, prompt: '{"band":"BULLISH","score":7,"confidence":0.5,"key_points":["x"]}'
    try:
        dec = aj_council.run("AAPL", call=call)
        conn = db.get_conn()
        rid = conn.execute("SELECT id FROM aj_council_runs WHERE symbol='AAPL' ORDER BY id DESC LIMIT 1").fetchone()["id"]
        analysts = [r["analyst"] for r in conn.execute(
            "SELECT analyst FROM aj_analyst_reports WHERE council_run_id=?", (rid,)).fetchall()]
        assert any(a.startswith("persona:") for a in analysts), analysts
    finally:
        fetcher.get_fundamentals, fetcher.get_chart_data, fetcher.compute_indicators = saved
        aj_config.set_config({"personas_enabled": False, "council_analyst_news": True,
                              "council_analyst_sentiment": True, "council_analyst_fundamentals": True})


def test_personas_use_shared_util_not_analysts_privates():
    # S033 refactor: aj_personas must pull the shared helpers from
    # aj_analyst_util, NOT reach into aj_analysts' module privates. We assert on
    # the source so the private-import smell can't silently come back.
    import inspect
    src = inspect.getsource(aj_personas)
    assert "from aj_analyst_util import" in src
    assert "from aj_analysts import" not in src
    # The helpers resolve to the SAME objects across both modules + the util
    # module (pure refactor — byte-for-byte identical behavior).
    import aj_analyst_util
    for name in ("CallFn", "default_call", "_safe", "_fmt", "_neutral"):
        assert getattr(aj_personas, name) is getattr(aj_analyst_util, name)
        # aj_analysts re-exports for backward compat.
        assert getattr(aj_analysts, name) is getattr(aj_analyst_util, name)


def test_council_report_renders():
    decs = [CouncilDecision(symbol="AAPL", rating=Rating.BUY, action=Action.BUY,
                            conviction=0.8, thesis="momentum", dissent="valuation")]
    md = aj_personas.council_report(decs)
    assert "AAPL" in md and "BUY" in md and "momentum" in md
    assert aj_personas.council_report([]).strip().endswith("_no council decisions_")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_council_phase6 — {} tests".format(len(fns)))
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
