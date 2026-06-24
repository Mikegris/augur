"""Offline tests for jarvis.deep_dossier (v2.4) — the engine-fan-out composite.

Monkeypatches the underlying engines so no network/data is touched. Verifies:
synthesis from multiple stances, conviction-shrinking when signals disagree,
contribution of only the engines that succeeded, and no-raise when all fail.
"""
import sys

import jarvis


def _patch(forecast=None, lens=None, smart=None):
    """Replace the three dossier adapters with fixtures. Each value is a
    {name,stance,note} dict, the string 'raise' (adapter throws), or None."""
    def _mk(label, val):
        def _fn(symbol):
            if val == "raise":
                raise RuntimeError("boom")
            return val
        _fn.__name__ = label
        return _fn
    jarvis._DOSSIER_ADAPTERS = [
        _mk("_dossier_forecast", forecast),
        _mk("_dossier_lens", lens),
        _mk("_dossier_smart_money", smart),
    ]


def test_aligned_signals_high_conviction():
    _patch(
        forecast={"name": "Forecast ensemble", "stance": "bull", "note": "63% P(up)"},
        lens={"name": "Investor lens", "stance": "bull", "note": "quality 82/100"},
        smart={"name": "Smart money", "stance": "bull", "note": "score 71/100 — Accumulate"},
    )
    out = jarvis.deep_dossier("NVDA")
    d = out["dossier"]
    assert d["n_signals"] == 3 and not d["disagreement"], d
    assert d["lean"] == "constructive", d
    assert d["conviction"] == "high", d
    assert "NVDA" in out["answer"] and "Forecast ensemble" in out["answer"]
    print("  [OK] aligned bullish signals → constructive, high conviction")


def test_split_signals_shrink_conviction():
    _patch(
        forecast={"name": "Forecast ensemble", "stance": "bull", "note": "58% P(up)"},
        lens={"name": "Investor lens", "stance": "bear", "note": "quality 30/100"},
        smart={"name": "Smart money", "stance": "bear", "note": "score 22/100 — Avoid"},
    )
    out = jarvis.deep_dossier("XYZ")
    d = out["dossier"]
    assert d["disagreement"] is True, d
    assert d["conviction"] == "low", d           # split → conviction shrunk
    assert "disagree" in out["answer"].lower(), out["answer"]
    print("  [OK] split signals → disagreement flagged, conviction shrunk to low")


def test_partial_failure_uses_survivors():
    _patch(
        forecast="raise",                         # adapter throws
        lens=None,                                # adapter returns nothing
        smart={"name": "Smart money", "stance": "bull", "note": "score 66/100 — Buy"},
    )
    out = jarvis.deep_dossier("AAPL")
    d = out["dossier"]
    assert d["n_signals"] == 1, d                 # only the survivor counts
    assert d["conviction"] == "low", d            # thin coverage → low
    assert "Smart money" in out["answer"]
    print("  [OK] partial failure: dossier built from surviving engine only")


def test_all_fail_no_raise():
    _patch(forecast="raise", lens=None, smart="raise")
    out = jarvis.deep_dossier("ZZZZ")
    assert "couldn't pull enough signal" in out["answer"].lower(), out["answer"]
    assert out["symbol"] == "ZZZZ"
    print("  [OK] all engines fail → graceful message, no raise")


def test_empty_symbol():
    out = jarvis.deep_dossier("")
    assert out["symbol"] is None
    print("  [OK] empty symbol handled")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("jarvis.deep_dossier — {} tests".format(len(fns)))
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print("  [XX] {}: {}".format(fn.__name__, e))
        except Exception as e:
            failed += 1
            print("  [XX] {}: unexpected {}: {}".format(fn.__name__, type(e).__name__, e))
    print("PASS" if failed == 0 else "{} FAILED".format(failed))
    sys.exit(1 if failed else 0)
