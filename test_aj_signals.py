"""Offline tests for aj_signals — the orthogonal alpha-signal adapters.

Fully isolated: a temp DB (in case any lazy import touches it) and every source
module (smart_money, synthetic_insider, congress, alt_signals) monkeypatched
with canned data. No network, no LLM. Standalone runner: prints PASS / N FAILED
and exits non-zero on failure, mirroring the repo's other test_aj_*.py.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix=".db")

import aj_signals as S  # noqa: E402
import smart_money  # noqa: E402
import synthetic_insider  # noqa: E402
import congress  # noqa: E402
import alt_signals  # noqa: E402
import fetcher  # noqa: E402
import aj_db  # noqa: E402

aj_db.aj_init()   # the adapter scorecard (section 6) needs aj_signal_scores

PASS = []
FAIL = []


def ok(msg):
    PASS.append(msg)
    print("  [OK] " + msg)


def check(cond, msg):
    if cond:
        ok(msg)
    else:
        FAIL.append(msg)
        print("  [XX] " + msg)


def _approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def _in_unit(p):
    return isinstance(p, float) and 0.0 <= p <= 1.0


class _Patch:
    """Tiny context manager to monkeypatch attrs and always restore them."""

    def __init__(self, *triples):
        # triples: (module, attr, value)
        self.triples = triples
        self._saved = []

    def __enter__(self):
        for mod, attr, val in self.triples:
            self._saved.append((mod, attr, getattr(mod, attr)))
            setattr(mod, attr, val)
        return self

    def __exit__(self, *exc):
        for mod, attr, val in reversed(self._saved):
            setattr(mod, attr, val)
        return False


def _raise(*a, **k):
    raise RuntimeError("boom")


# --------------------------------------------------------------------------- #
# Shared mapping
# --------------------------------------------------------------------------- #

def test_score_to_prob_mapping():
    check(_approx(S._score_to_prob(50), 0.5), "score 50 -> prob 0.5 (neutral)")
    check(S._score_to_prob(100) > 0.5 and S._score_to_prob(100) <= 0.95,
          "score 100 -> prob > 0.5 and clamped <= 0.95")
    check(S._score_to_prob(0) < 0.5 and S._score_to_prob(0) >= 0.05,
          "score 0 -> prob < 0.5 and clamped >= 0.05")
    check(_approx(S._score_to_prob(75), 0.75), "score 75 -> prob 0.75")
    check(_approx(S._score_to_prob(25), 0.25), "score 25 -> prob 0.25")
    # monotone
    check(S._score_to_prob(60) > S._score_to_prob(50) > S._score_to_prob(40),
          "score->prob is monotone increasing")
    check(_approx(S._score_to_confidence(50), 0.0), "confidence 0 at neutral 50")
    check(_approx(S._score_to_confidence(100), 1.0), "confidence 1 at extreme 100")
    # corrupt-data guard: a non-finite score must read NEUTRAL, never
    # max-conviction (NaN used to clamp to prob 0.95 / confidence 1.0)
    check(_approx(S._score_to_prob(float("nan")), 0.5), "NaN score -> neutral prob 0.5")
    check(_approx(S._score_to_confidence(float("nan")), 0.0), "NaN score -> confidence 0")
    check(_approx(S._score_to_prob(float("inf")), 0.5), "inf score -> neutral prob 0.5")


# --------------------------------------------------------------------------- #
# 1. smart_money
# --------------------------------------------------------------------------- #

def test_smart_money_signal():
    with _Patch((smart_money, "compute_score",
                 lambda sym: {"score": 50, "signal": "NEUTRAL"})):
        sig = S.smart_money_signal("AAA")
        check(sig is not None and _approx(sig["prob_up"], 0.5),
              "smart_money score 50 -> prob ~0.5")
        check(sig["source"] == "smart_money", "smart_money source tag set")

    with _Patch((smart_money, "compute_score",
                 lambda sym: {"score": 85, "signal": "STRONG BUY"})):
        sig = S.smart_money_signal("AAA")
        check(sig is not None and sig["prob_up"] > 0.5 and _in_unit(sig["prob_up"]),
              "smart_money high score -> prob > 0.5")
        check(sig["confidence"] > 0.0, "smart_money high score -> confidence > 0")

    with _Patch((smart_money, "compute_score",
                 lambda sym: {"score": 15, "signal": "AVOID"})):
        sig = S.smart_money_signal("AAA")
        check(sig is not None and sig["prob_up"] < 0.5 and _in_unit(sig["prob_up"]),
              "smart_money low score -> prob < 0.5")

    with _Patch((smart_money, "compute_score",
                 lambda sym: {"symbol": sym, "score": None, "error": "no price"})):
        check(S.smart_money_signal("AAA") is None,
              "smart_money None score -> None")

    with _Patch((smart_money, "compute_score", _raise)):
        check(S.smart_money_signal("AAA") is None,
              "smart_money raising source -> None (fail-open)")


# --------------------------------------------------------------------------- #
# 2. insider (synthetic_insider.compute_composite)
# --------------------------------------------------------------------------- #

def test_insider_signal():
    with _Patch((synthetic_insider, "compute_composite",
                 lambda sym: {"composite_score": 50, "coverage": 1.0,
                              "convergence_count": 0, "signal": "QUIET"})):
        sig = S.insider_signal("AAA")
        check(sig is not None and _approx(sig["prob_up"], 0.5),
              "insider composite 50 (full coverage) -> prob ~0.5")

    with _Patch((synthetic_insider, "compute_composite",
                 lambda sym: {"composite_score": 90, "coverage": 1.0,
                              "convergence_count": 5, "signal": "STRONG CONVERGENCE"})):
        sig = S.insider_signal("AAA")
        check(sig is not None and sig["prob_up"] > 0.5 and _in_unit(sig["prob_up"]),
              "insider high composite -> prob > 0.5")

    with _Patch((synthetic_insider, "compute_composite",
                 lambda sym: {"composite_score": 10, "coverage": 1.0,
                              "convergence_count": 0, "signal": "QUIET"})):
        sig = S.insider_signal("AAA")
        check(sig is not None and sig["prob_up"] < 0.5 and _in_unit(sig["prob_up"]),
              "insider low composite -> prob < 0.5")

    # Poor coverage dampens the CONFIDENCE channel ONLY (no double penalty): the
    # prob tilt stays at full strength while confidence is cut.
    with _Patch((synthetic_insider, "compute_composite",
                 lambda sym: {"composite_score": 90, "coverage": 1.0,
                              "convergence_count": 5})):
        full = S.insider_signal("AAA")
    with _Patch((synthetic_insider, "compute_composite",
                 lambda sym: {"composite_score": 90, "coverage": 0.17,
                              "convergence_count": 1})):
        thin = S.insider_signal("AAA")
    check(thin["confidence"] < full["confidence"],
          "insider poor coverage -> lower confidence")
    check(_approx(thin["prob_up"], full["prob_up"]),
          "insider poor coverage -> prob tilt NOT also damped (single penalty)")

    with _Patch((synthetic_insider, "compute_composite",
                 lambda sym: {"error": "compute failed"})):
        check(S.insider_signal("AAA") is None, "insider error dict -> None")

    with _Patch((synthetic_insider, "compute_composite", _raise)):
        check(S.insider_signal("AAA") is None,
              "insider raising source -> None (fail-open)")


# --------------------------------------------------------------------------- #
# 3. congress
# --------------------------------------------------------------------------- #

def _trade(ticker, txn_type, amount_val):
    return {"ticker": ticker, "txn_type": txn_type, "amount_val": amount_val}


def test_congress_signal():
    # All buys -> prob > 0.5
    with _Patch((congress, "get_recent_trades",
                 lambda **k: {"trades": [
                     _trade("AAA", "Buy", 50000),
                     _trade("AAA", "Buy", 15000),
                     _trade("BBB", "Sell", 99999),  # other symbol -> ignored
                 ]})):
        sig = S.congress_signal("AAA")
        check(sig is not None and sig["prob_up"] > 0.5 and _in_unit(sig["prob_up"]),
              "congress net buys -> prob > 0.5")
        check(sig["source"] == "congress", "congress source tag set")

    # All sells -> prob < 0.5
    with _Patch((congress, "get_recent_trades",
                 lambda **k: {"trades": [
                     _trade("AAA", "Sell", 50000),
                     _trade("AAA", "Sell (Partial)", 15000),
                 ]})):
        sig = S.congress_signal("AAA")
        check(sig is not None and sig["prob_up"] < 0.5 and _in_unit(sig["prob_up"]),
              "congress net sells -> prob < 0.5")

    # Balanced $ -> ~neutral
    with _Patch((congress, "get_recent_trades",
                 lambda **k: {"trades": [
                     _trade("AAA", "Buy", 50000),
                     _trade("AAA", "Sell", 50000),
                 ]})):
        sig = S.congress_signal("AAA")
        check(sig is not None and _approx(sig["prob_up"], 0.5, tol=1e-6),
              "congress balanced buy/sell -> prob ~0.5")

    # No trades for symbol -> None
    with _Patch((congress, "get_recent_trades",
                 lambda **k: {"trades": [_trade("ZZZ", "Buy", 1000)]})):
        check(S.congress_signal("AAA") is None,
              "congress no trades for symbol -> None")

    # Empty -> None
    with _Patch((congress, "get_recent_trades", lambda **k: {"trades": []})):
        check(S.congress_signal("AAA") is None, "congress empty -> None")

    # Tolerates a bare list return too.
    with _Patch((congress, "get_recent_trades",
                 lambda **k: [_trade("AAA", "Buy", 1000)])):
        sig = S.congress_signal("AAA")
        check(sig is not None and sig["prob_up"] > 0.5,
              "congress bare-list return tolerated")

    with _Patch((congress, "get_recent_trades", _raise)):
        check(S.congress_signal("AAA") is None,
              "congress raising source -> None (fail-open)")


# --------------------------------------------------------------------------- #
# 4. social
# --------------------------------------------------------------------------- #

def test_social_signal():
    with _Patch((alt_signals, "stocktwits_symbol_sentiment",
                 lambda sym: {"bull_ratio": 0.5, "bullish": 10, "bearish": 10})):
        sig = S.social_signal("AAA")
        check(sig is not None and _approx(sig["prob_up"], 0.5),
              "social balanced ratio -> prob ~0.5")

    with _Patch((alt_signals, "stocktwits_symbol_sentiment",
                 lambda sym: {"bull_ratio": 0.9, "bullish": 27, "bearish": 3})):
        sig = S.social_signal("AAA")
        check(sig is not None and sig["prob_up"] > 0.5 and _in_unit(sig["prob_up"]),
              "social bullish -> prob > 0.5")
        check(sig["confidence"] <= 0.4, "social confidence kept low (<= 0.4)")

    with _Patch((alt_signals, "stocktwits_symbol_sentiment",
                 lambda sym: {"bull_ratio": 0.1, "bullish": 3, "bearish": 27})):
        sig = S.social_signal("AAA")
        check(sig is not None and sig["prob_up"] < 0.5 and _in_unit(sig["prob_up"]),
              "social bearish -> prob < 0.5")

    with _Patch((alt_signals, "stocktwits_symbol_sentiment",
                 lambda sym: {"bull_ratio": None, "bullish": 0, "bearish": 0})):
        check(S.social_signal("AAA") is None, "social no tagged sentiment -> None")

    with _Patch((alt_signals, "stocktwits_symbol_sentiment", lambda sym: None)):
        check(S.social_signal("AAA") is None, "social None upstream -> None")

    with _Patch((alt_signals, "stocktwits_symbol_sentiment", _raise)):
        check(S.social_signal("AAA") is None,
              "social raising source -> None (fail-open)")


# --------------------------------------------------------------------------- #
# 5. all_signals aggregation + resilience
# --------------------------------------------------------------------------- #

def test_all_signals_aggregates_non_none():
    with _Patch(
        (smart_money, "compute_score", lambda sym: {"score": 80, "signal": "BUY"}),
        (synthetic_insider, "compute_composite",
         lambda sym: {"composite_score": 70, "coverage": 1.0, "convergence_count": 3}),
        (congress, "get_recent_trades", lambda **k: {"trades": []}),  # -> None
        (alt_signals, "stocktwits_symbol_sentiment",
         lambda sym: {"bull_ratio": 0.8, "bullish": 16, "bearish": 4}),
    ):
        out = S.all_signals("AAA")
        check(set(out.keys()) == {"smart_money", "insider", "social"},
              "all_signals collects only non-None adapters")
        check(all(_in_unit(v["prob_up"]) for v in out.values()),
              "all_signals: every prob_up in [0,1]")


def test_all_signals_survives_one_raising():
    with _Patch(
        (smart_money, "compute_score", _raise),                       # raises
        (synthetic_insider, "compute_composite",
         lambda sym: {"composite_score": 60, "coverage": 1.0, "convergence_count": 2}),
        (congress, "get_recent_trades",
         lambda **k: {"trades": [_trade("AAA", "Buy", 1000)]}),
        (alt_signals, "stocktwits_symbol_sentiment", lambda sym: None),  # -> None
    ):
        out = S.all_signals("AAA")
        check("smart_money" not in out, "all_signals: raising adapter dropped")
        check("insider" in out and "congress" in out,
              "all_signals: other adapters survive one raising")
        check("social" not in out, "all_signals: None adapter excluded")


# --------------------------------------------------------------------------- #
# 6. Adapter scorecard — ledger, scoring, confidence decay
# --------------------------------------------------------------------------- #

import database as _dbmod  # noqa: E402
from datetime import timedelta  # noqa: E402


def _clear_scores():
    with _dbmod._write_lock:
        conn = _dbmod.get_conn()
        conn.execute("DELETE FROM aj_signal_scores")
        conn.commit()
    S._invalidate_adapter_weights()


def _seed_scored(adapter, n, hit):
    """n already-scored rows for `adapter` with the given hit outcome."""
    now = aj_db.utc_now_iso()
    with _dbmod._write_lock:
        conn = _dbmod.get_conn()
        conn.executemany(
            "INSERT INTO aj_signal_scores (ts, symbol, adapter, prob_up, confidence,"
            " horizon_days, price_at, scored_at, realized_up, hit)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(now, "AAA", adapter, 0.7, 0.5, 5, 100.0, now, int(hit), int(hit))
             for _ in range(n)])
        conn.commit()
    S._invalidate_adapter_weights()


def _seed_open(adapter, days_old, prob_up, price_at=100.0, horizon=5):
    """One unscored (scored_at NULL) row `days_old` calendar days in the past."""
    ts = (aj_db.utc_now() - timedelta(days=days_old)).isoformat()
    with _dbmod._write_lock:
        conn = _dbmod.get_conn()
        cur = conn.execute(
            "INSERT INTO aj_signal_scores (ts, symbol, adapter, prob_up, confidence,"
            " horizon_days, price_at) VALUES (?,?,?,?,?,?,?)",
            (ts, "AAA", adapter, prob_up, 0.5, horizon, price_at))
        conn.commit()
        return cur.lastrowid


_SCORECARD_SOURCES = (
    (smart_money, "compute_score", lambda sym: {"score": 80, "signal": "BUY"}),
    (synthetic_insider, "compute_composite",
     lambda sym: {"composite_score": 70, "coverage": 1.0, "convergence_count": 3}),
    (congress, "get_recent_trades", lambda **k: {"trades": []}),   # -> None
    (alt_signals, "stocktwits_symbol_sentiment",
     lambda sym: {"bull_ratio": 0.8, "bullish": 16, "bearish": 4}),
)


def _score_rows():
    return aj_db.query(
        "SELECT * FROM aj_signal_scores ORDER BY id", ())


def test_scorecard_hit_rate_weight_mapping():
    # documented mapping: clamp((hr - 0.35)/0.15, 0.25, 1.0)
    check(_approx(S._hit_rate_weight(0.5), 1.0), "weight 1.0 at hit-rate 0.5")
    check(_approx(S._hit_rate_weight(0.9), 1.0), "weight clamped to 1.0 above 0.5")
    check(_approx(S._hit_rate_weight(0.44), 0.6), "weight 0.6 at hit-rate 0.44")
    check(_approx(S._hit_rate_weight(0.35), 0.25), "weight floored 0.25 at 0.35")
    check(_approx(S._hit_rate_weight(0.0), 0.25), "weight never below the 0.25 floor")
    check(S._hit_rate_weight(0.48) > S._hit_rate_weight(0.42) > S._hit_rate_weight(0.38),
          "weight mapping is monotone in hit-rate")


def test_scorecard_off_is_todays_behavior():
    _clear_scores()
    with _Patch(*_SCORECARD_SOURCES):
        base = {}
        for name, fn in S._ADAPTERS:
            sig = fn("AAA")
            if sig is not None:
                base[name] = sig
        n_before = len(_score_rows())
        out = S.all_signals("AAA", cfg={})                 # flag absent -> OFF
        out_default = S.all_signals("AAA")                 # real cfg, key not in DEFAULTS -> OFF
    check(out == base, "scorecard OFF: fused signals identical to raw adapters")
    check(out_default == base, "scorecard OFF: default-cfg call path unchanged")
    check(len(_score_rows()) == n_before, "scorecard OFF: nothing logged")


def test_scorecard_on_logs_and_cold_start_neutral():
    _clear_scores()
    cfg = {"adapter_scorecard": True, "forecast_horizon_days": 5}
    with _Patch(*_SCORECARD_SOURCES,
                (fetcher, "get_quotes_batch",
                 lambda syms: {s.upper(): {"price": 100.0} for s in syms})):
        off = S.all_signals("AAA", cfg={})
        on = S.all_signals("AAA", cfg=cfg)
    check(on == off, "scorecard ON + cold start (no scored history): confidences neutral")
    rows = _score_rows()
    check({r["adapter"] for r in rows} == set(on.keys()),
          "scorecard ON: one ledger row per live adapter")
    check(all(r["scored_at"] is None and r["price_at"] == 100.0
              and r["horizon_days"] == 5 for r in rows),
          "scorecard ON: rows logged open (scored_at NULL) with price/horizon")
    check(all(_approx(r["prob_up"], on[r["adapter"]]["prob_up"]) for r in rows),
          "scorecard ON: logged prob_up matches the adapter output")


def test_scorecard_on_applies_decay_weights():
    _clear_scores()
    _seed_scored("social", 40, hit=0)        # 0% hit-rate -> floor weight 0.25
    _seed_scored("smart_money", 40, hit=1)   # 100% hit-rate -> full 1.0
    _seed_scored("insider", 10, hit=0)       # < 30 samples -> cold-start 1.0
    w = S.adapter_weights()
    check(_approx(w["social"], 0.25), "adapter_weights: chronic misser decayed to 0.25")
    check(_approx(w["smart_money"], 1.0), "adapter_weights: proven adapter keeps 1.0")
    check(_approx(w["insider"], 1.0), "adapter_weights: <30 samples stays neutral (cold start)")
    check(_approx(w["congress"], 1.0), "adapter_weights: no history stays neutral")
    cfg = {"adapter_scorecard": True, "forecast_horizon_days": 5}
    with _Patch(*_SCORECARD_SOURCES,
                (fetcher, "get_quotes_batch",
                 lambda syms: {s.upper(): {"price": 100.0} for s in syms})):
        off = S.all_signals("AAA", cfg={})
        on = S.all_signals("AAA", cfg=cfg)
    check(_approx(on["social"]["confidence"], off["social"]["confidence"] * 0.25),
          "fusion: decayed adapter's confidence multiplied by its weight")
    check(_approx(on["smart_money"]["confidence"], off["smart_money"]["confidence"]),
          "fusion: full-weight adapter's confidence untouched")
    check(_approx(on["social"]["prob_up"], off["social"]["prob_up"]),
          "fusion: prob_up never reweighted (confidence channel only)")
    # memoized for 10 min: new (bad) history doesn't move weights until refresh.
    # 60 misses on top of the 40 hits -> trailing hit-rate 0.40 -> weight < 1.0
    _seed_scored_no_invalidate = 60
    now = aj_db.utc_now_iso()
    with _dbmod._write_lock:
        conn = _dbmod.get_conn()
        conn.executemany(
            "INSERT INTO aj_signal_scores (ts, symbol, adapter, prob_up, confidence,"
            " horizon_days, price_at, scored_at, realized_up, hit)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(now, "AAA", "smart_money", 0.7, 0.5, 5, 100.0, now, 0, 0)
             for _ in range(_seed_scored_no_invalidate)])
        conn.commit()
    check(_approx(S.adapter_weights()["smart_money"], 1.0),
          "adapter_weights: memoized for the TTL (fresh rows ignored)")
    S._invalidate_adapter_weights()
    check(S.adapter_weights()["smart_money"] < 1.0,
          "adapter_weights: refreshed after invalidation")


def test_scorecard_scoring_pass():
    _clear_scores()
    id_hit = _seed_open("smart_money", days_old=10, prob_up=0.7)    # due (5*1.5=7.5d)
    id_miss = _seed_open("social", days_old=10, prob_up=0.3)        # due, wrong call
    id_fresh = _seed_open("insider", days_old=1, prob_up=0.9)       # NOT due yet
    with _Patch((fetcher, "get_quotes_batch",
                 lambda syms: {s.upper(): {"price": 110.0} for s in syms})):
        res = S.score_due_adapter_signals()
    check(res.get("scored") == 2, "scoring: exactly the due rows scored")
    rows = {r["id"]: r for r in _score_rows()}
    check(rows[id_hit]["realized_up"] == 1 and rows[id_hit]["hit"] == 1,
          "scoring: bullish call + price up -> realized_up=1, hit=1")
    check(rows[id_miss]["realized_up"] == 1 and rows[id_miss]["hit"] == 0,
          "scoring: bearish call + price up -> hit=0")
    check(rows[id_fresh]["scored_at"] is None,
          "scoring: row inside its horizon left open")
    # quote miss leaves the row open for the next pass (never falsely graded)
    id_nq = _seed_open("congress", days_old=10, prob_up=0.6)
    with _Patch((fetcher, "get_quotes_batch", lambda syms: {})):
        res2 = S.score_due_adapter_signals()
    check(res2.get("scored") == 0 and res2.get("skipped") == 1,
          "scoring: quote miss skipped, not graded")
    check({r["id"]: r for r in _score_rows()}[id_nq]["scored_at"] is None,
          "scoring: quote-miss row still open")
    _clear_scores()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_signals — {} test groups\n".format(len(tests)))
    for fn in tests:
        try:
            fn()
        except Exception as e:
            FAIL.append(fn.__name__)
            print("  [XX] {}: unexpected {}: {}".format(
                fn.__name__, type(e).__name__, e))
    print("\n{} passed, {} failed".format(len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
        print("{} FAILED".format(len(FAIL)))
        sys.exit(1)
    print("PASS")
    sys.exit(0)
