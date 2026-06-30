"""Standalone tests for aj_select. Run: ./venv/bin/python test_aj_select.py

No DB/network — pure dicts. Prints PASS / FAILED and sys.exit()s with a status
code so it can gate a regression run.
"""

import math
import sys

import aj_select


_failures = []


def check(cond, msg):
    if cond:
        print("  ok: " + msg)
    else:
        print("  FAIL: " + msg)
        _failures.append(msg)


def fc(edge=None, prob=None, **extra):
    """Build an ensemble forecast dict like forecast_ensemble emits."""
    ens = {}
    if edge is not None:
        ens["edge_pct_pts"] = edge
    if prob is not None:
        ens["prob_up"] = prob
    ens.update(extra)
    return {"ensemble": ens}


# --- composite_edge ----------------------------------------------------------
def test_composite_edge():
    print("composite_edge:")
    check(aj_select.composite_edge(fc(edge=7.5)) == 7.5, "reads signed edge_pct_pts")
    check(aj_select.composite_edge(fc(edge=-4.0)) == -4.0, "preserves bearish sign")
    # falls back to (prob-0.5)*100 when no edge present
    check(abs(aj_select.composite_edge(fc(prob=0.6)) - 10.0) < 1e-9, "prob_up fallback")
    # graceful on junk
    check(aj_select.composite_edge(None) == 0.0, "None -> 0.0")
    check(aj_select.composite_edge({}) == 0.0, "empty dict -> 0.0")
    check(aj_select.composite_edge({"ensemble": None}) == 0.0, "None ensemble -> 0.0")
    check(aj_select.composite_edge(fc(edge=float("nan"))) == 0.0, "NaN edge -> 0.0 (no crash)")
    # top-level edge (operator decision shape)
    check(aj_select.composite_edge({"edge": 3.3}) == 3.3, "reads top-level edge")


# --- cross_sectional_rank ----------------------------------------------------
def test_rank():
    print("cross_sectional_rank:")
    # Signed ranking: most BULLISH first. A bearish (negative-edge) name sinks to
    # the bottom even when its MAGNITUDE is the largest — this feeds a long flow.
    cands = [
        ("LOW", fc(edge=1.0)),
        ("HIGH", fc(edge=-9.0)),   # strongest by magnitude but BEARISH -> last
        ("MID", fc(edge=4.0)),
    ]
    ranked = aj_select.cross_sectional_rank(cands, {})
    syms = [s for s, _ in ranked]
    check(syms == ["MID", "LOW", "HIGH"], "orders by SIGNED edge desc (bearish last): " + str(syms))

    # NaN / None edges become 0.0 conviction (neutral) — they sit BELOW positive
    # edges but ABOVE real negatives; the sort never crashes on them.
    cands2 = [
        ("NAN", fc(edge=float("nan"))),   # -> 0.0 conviction
        ("GOOD", fc(edge=5.0)),
        ("NONE", fc(edge=None)),          # -> 0.0 conviction
        ("MID", fc(edge=2.0)),
    ]
    ranked2 = aj_select.cross_sectional_rank(cands2, {})
    syms2 = [s for s, _ in ranked2]
    check(syms2[0] == "GOOD" and syms2[1] == "MID", "strong (bullish) names first: " + str(syms2))
    check(set(syms2[2:]) == {"NAN", "NONE"}, "zero-conviction (NaN/None) names sink below real edges: " + str(syms2))

    # Malformed forecasts (non-dict / missing ensemble) score 0.0 and must sink
    # below real edges without crashing.
    cands_raw = [("BAD1", 3.0), ("REAL", fc(edge=9.0)), ("BAD2", {"no": "edge"})]
    ranked_raw = aj_select.cross_sectional_rank(cands_raw, {})
    check([s for s, _ in ranked_raw][0] == "REAL", "real edge outranks malformed forecasts: "
          + str([s for s, _ in ranked_raw]))
    check(set([s for s, _ in ranked_raw][1:]) == {"BAD1", "BAD2"}, "malformed forecasts sink")

    # empty / garbage
    check(aj_select.cross_sectional_rank([], {}) == [], "empty -> []")
    check(aj_select.cross_sectional_rank(None, {}) == [], "None -> []")
    # input not mutated
    original = list(cands)
    aj_select.cross_sectional_rank(cands, {})
    check(cands == original, "input list not mutated")


# --- select_top_n ------------------------------------------------------------
def test_select_top_n():
    print("select_top_n:")
    cands = [
        ("A", fc(edge=8.0)),
        ("B", fc(edge=6.0)),
        ("C", fc(edge=4.0)),
        ("D", fc(edge=2.0)),
        ("E", fc(edge=1.0)),
        ("F", fc(edge=0.5)),
    ]
    # top_n caps the count
    top = aj_select.select_top_n(cands, {"cross_sectional_top_n": 3, "min_edge_pct_pts": 0})
    check([s for s, _ in top] == ["A", "B", "C"], "respects top_n=3")

    # absolute floor is NOT bypassed by ranking: with floor 3.0, D/E/F drop even
    # though top_n would allow them.
    top2 = aj_select.select_top_n(cands, {"cross_sectional_top_n": 5, "min_edge_pct_pts": 3.0})
    check([s for s, _ in top2] == ["A", "B", "C"], "floor drops sub-edge names: " + str([s for s, _ in top2]))

    # long-only selection: a BEARISH name is never selected, no matter how large
    # its magnitude. WEAK (bullish but sub-floor) is also dropped -> empty.
    cands3 = [("BEAR", fc(edge=-7.0)), ("WEAK", fc(edge=1.0))]
    top3 = aj_select.select_top_n(cands3, {"cross_sectional_top_n": 5, "min_edge_pct_pts": 4.0})
    check([s for s, _ in top3] == [], "bearish name never selected for long flow: " + str([s for s, _ in top3]))
    # a bullish name clearing the floor IS selected, bearish dropped
    cands3b = [("BEAR", fc(edge=-7.0)), ("BULL", fc(edge=5.0))]
    top3b = aj_select.select_top_n(cands3b, {"cross_sectional_top_n": 5, "min_edge_pct_pts": 4.0})
    check([s for s, _ in top3b] == ["BULL"], "bullish clears floor, bearish dropped: " + str([s for s, _ in top3b]))

    # default top_n = 5
    top4 = aj_select.select_top_n(cands, {"min_edge_pct_pts": 0})
    check(len(top4) == 5, "default top_n is 5")

    # empty input
    check(aj_select.select_top_n([], {}) == [], "empty -> []")
    check(aj_select.select_top_n(None, {}) == [], "None -> []")


# --- regime_signal_weights ---------------------------------------------------
BASE = {
    "rf_classifier": 0.28,
    "bootstrap": 0.26,
    "ml_composite": 0.18,
    "trend": 0.12,
    "mean_reversion": 0.08,
    "narrative": 0.08,
}


def test_regime_weights():
    print("regime_signal_weights:")
    base_total = sum(BASE.values())

    # flag off -> base unchanged (same object)
    off = aj_select.regime_signal_weights(BASE, "bull", {"regime_conditional_weights": False})
    check(off is BASE, "flag off returns base unchanged")
    check(aj_select.regime_signal_weights(BASE, "bull", {}) is BASE, "default off returns base")

    # bull: trend/narrative up, mean_reversion down; total preserved
    bull = aj_select.regime_signal_weights(BASE, "bull", {"regime_conditional_weights": True})
    check(abs(sum(bull.values()) - base_total) < 1e-9, "bull renormalizes to original total")
    check(bull["trend"] / BASE["trend"] > bull["mean_reversion"] / BASE["mean_reversion"],
          "bull tilts trend up relative to mean_reversion")
    check(bull["mean_reversion"] < BASE["mean_reversion"], "bull cuts mean_reversion (absolute)")

    # bear behaves like a trending regime too
    bear = aj_select.regime_signal_weights(BASE, "bear", {"regime_conditional_weights": True})
    check(abs(sum(bear.values()) - base_total) < 1e-9, "bear renormalizes to original total")
    check(bear["trend"] > BASE["trend"], "bear tilts trend up")

    # chop: opposite tilt
    chop = aj_select.regime_signal_weights(BASE, "chop", {"regime_conditional_weights": True})
    check(abs(sum(chop.values()) - base_total) < 1e-9, "chop renormalizes to original total")
    check(chop["mean_reversion"] > BASE["mean_reversion"], "chop tilts mean_reversion up")
    check(chop["trend"] < BASE["trend"], "chop cuts trend")

    # bounded: no weight blows up beyond the multiplier * scale
    for k, v in bull.items():
        check(v >= 0, "weight " + k + " stays non-negative in bull")

    # unknown regime -> base unchanged (value-equal copy)
    unk = aj_select.regime_signal_weights(BASE, "moon", {"regime_conditional_weights": True})
    check(abs(sum(unk.values()) - base_total) < 1e-9, "unknown regime preserves total")
    check(unk == BASE, "unknown regime returns base values unchanged")
    none_reg = aj_select.regime_signal_weights(BASE, None, {"regime_conditional_weights": True})
    check(none_reg == BASE, "None regime returns base values unchanged")

    # robustness: junk weights don't crash
    junk = aj_select.regime_signal_weights({"trend": None, "mean_reversion": "x"}, "bull",
                                           {"regime_conditional_weights": True})
    check(isinstance(junk, dict), "junk weights -> dict (no crash)")
    empty = aj_select.regime_signal_weights({}, "bull", {"regime_conditional_weights": True})
    check(empty == {}, "empty weights -> {}")


def main():
    test_composite_edge()
    test_rank()
    test_select_top_n()
    test_regime_weights()
    print()
    if _failures:
        print("FAILED ({} check(s)):".format(len(_failures)))
        for f in _failures:
            print("  - " + f)
        sys.exit(1)
    print("PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
