#!/usr/bin/env python3
"""Conditional performance-slice tests — aj_analytics.performance_slices."""
import json
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_perfslice.db")

import aj_db                    # noqa: E402
import aj_analytics as A        # noqa: E402

aj_db.aj_init()

_FAILS = []


def check(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        _FAILS.append(msg)


def _seed(symbol, regime, conviction, holding, ret, pnl, i):
    aj_db.insert("aj_trade_labels", symbol=symbol, side="buy",
                 opened_at="2026-01-{:02d}T00:00:00+00:00".format((i % 27) + 1),
                 closed_at="2026-02-{:02d}T00:00:00+00:00".format((i % 27) + 1),
                 holding_days=float(holding), regime=regime,
                 features_json=json.dumps({"conviction": conviction, "edge": 10.0}),
                 realized_return_pct=float(ret), realized_pnl_usd=float(pnl),
                 label=1 if ret > 0 else 0, created_at="2026-02-01T00:00:00+00:00")


def test_instrument_derivation():
    check(A._instrument_of("OPT:AAPL:20260101:C:100.0") == "option", "OPT: -> option")
    check(A._instrument_of("BTC-USD") == "crypto", "-USD -> crypto")
    check(A._instrument_of("AAPL") == "stock", "bare ticker -> stock")


def test_slice_stats_math():
    # 2 wins (+10), 3 losses (-5): win_rate 0.4, payoff 2.0, breakeven 1/3,
    # expectancy = (20-15)/5 = +1.0
    s = A._slice_stats([10, 10, -5, -5, -5], [10, 10, -5, -5, -5])
    check(s["win_rate"] == 0.4, "win_rate 2/5 = 0.4")
    check(s["payoff_ratio"] == 2.0, "payoff 10/5 = 2.0")
    check(abs(s["breakeven_win_rate"] - 0.333) < 0.01, "breakeven 1/(1+2) = 0.33")
    check(abs(s["expectancy_pct"] - 1.0) < 1e-9, "expectancy +1.0%")
    # win_rate 0.4 > breakeven 0.33 and expectancy>0 -> NOT losing
    check(s["structurally_losing"] is False, "positive-expectancy slice not flagged")


def test_structural_loser_and_net_guard():
    # Case A: real loser — net<0, expectancy<0, win<breakeven.
    loser = A._slice_stats([-8, -8, -8, -8, 5], [-8, -8, -8, -8, 5])
    check(loser["structurally_losing"] is True, "net-negative losing slice IS flagged")
    # Case B: net-POSITIVE in $ but slightly negative mean% — must NOT flag
    # (one big $ winner outweighs small % losers). rets mean<0 but pnl sum>0.
    mixed = A._slice_stats([-1, -1, -1, -1, 3], [-1, -1, -1, -1, 100])
    check(mixed["expectancy_pct"] < 0 and mixed["net_pnl_usd"] > 0,
          "mixed slice: expectancy<0 but net$>0")
    check(mixed["structurally_losing"] is False,
          "net-positive slice NOT flagged despite negative mean% (net$ guard)")


def test_grouping_and_flags():
    # crypto: 4 trades, all losers -> flagged. stock: net-positive -> not flagged.
    for i in range(4):
        _seed("ETH-USD", "bull", 0.5, 3, -15, -150, i)
    for i in range(6):
        _seed("AAPL", "chop", 0.0, 10, 8 if i < 3 else -2, 80 if i < 3 else -20, 10 + i)
    rep = A.performance_slices()
    inst = {s["slice"]: s for s in rep["by"]["instrument"]}
    check("crypto" in inst and inst["crypto"]["structurally_losing"] is True,
          "crypto slice flagged structurally losing")
    check("stock" in inst and inst["stock"]["structurally_losing"] is False,
          "net-positive stock slice not flagged")
    dims = set(rep["by"].keys())
    check(dims == {"regime", "instrument", "conviction", "holding", "edge"},
          "all five dimensions present")
    check(any(f["dimension"] == "instrument" and f["slice"] == "crypto"
              for f in rep["flags"]), "crypto appears in the flagged shortlist")


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        print(t.__name__)
        t()
    print("\n{} checks failed".format(len(_FAILS)) if _FAILS
          else "\nALL PASSED ({} tests)".format(len(tests)))
    sys.exit(1 if _FAILS else 0)


if __name__ == "__main__":
    main()
