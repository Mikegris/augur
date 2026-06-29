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

    # Poor coverage dampens the tilt toward neutral.
    with _Patch((synthetic_insider, "compute_composite",
                 lambda sym: {"composite_score": 90, "coverage": 1.0,
                              "convergence_count": 5})):
        full = S.insider_signal("AAA")
    with _Patch((synthetic_insider, "compute_composite",
                 lambda sym: {"composite_score": 90, "coverage": 0.17,
                              "convergence_count": 1})):
        thin = S.insider_signal("AAA")
    check(thin["prob_up"] < full["prob_up"] and thin["confidence"] < full["confidence"],
          "insider poor coverage -> prob closer to neutral + lower confidence")

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
