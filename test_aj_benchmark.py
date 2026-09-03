#!/usr/bin/env python3
"""Benchmark-vs-indexes tests. Network-free: equity curve + index closes injected."""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_bmk.db")

import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_analytics             # noqa: E402
import fetcher                  # noqa: E402
import aj_benchmark as BM       # noqa: E402

aj_db.aj_init()


def _cfg():
    aj_config.set_config({"compound_base_equity_usd": 10000.0})
    return aj_config.get_config()


def _mk_epoch(dstr):
    import datetime
    dt = datetime.datetime.strptime(dstr, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp()) + 20 * 3600   # ~market close, ET date == dstr


def _patch_indexes(prices_by_sym):
    # prices_by_sym: {SYM: {date: close}}
    def _chart(sym, period, interval="1d"):
        p = prices_by_sym.get(sym, {})
        return [{"time": _mk_epoch(d), "close": c} for d, c in sorted(p.items())]
    fetcher.get_chart_data = _chart


def test_insufficient_history():
    aj_analytics.equity_curve = lambda days=90: [{"date": "2026-06-30", "equity_usd": 0.0}]
    r = BM.benchmark(_cfg())
    assert r["verdict"] == "insufficient data", r


def test_agent_outperforms_when_pnl_beats_index():
    # agent NAV 10000 -> 10100 -> 10300 => +3% cumulative
    aj_analytics.equity_curve = lambda days=90: [
        {"date": "2026-06-26", "equity_usd": 0.0},
        {"date": "2026-06-29", "equity_usd": 100.0},
        {"date": "2026-06-30", "equity_usd": 300.0}]
    # SPY flat -> +1% ; agent (+3%) should have +2% alpha and be "outperforming"
    _patch_indexes({"SPY": {"2026-06-26": 100.0, "2026-06-29": 100.0, "2026-06-30": 101.0},
                    "QQQ": {"2026-06-26": 200.0, "2026-06-29": 210.0, "2026-06-30": 220.0}})
    r = BM.benchmark(_cfg())
    assert r["verdict"] == "outperforming", r
    assert abs(r["agent"]["cumulative_return_pct"] - 3.0) < 1e-6, r
    assert abs(r["vs"]["SPY"]["alpha_pct"] - 2.0) < 0.01, r["vs"]["SPY"]
    # QQQ went +10% -> agent underperforms QQQ (negative alpha)
    assert r["vs"]["QQQ"]["alpha_pct"] < 0, r["vs"]["QQQ"]
    assert r["n_days"] == 3 and len(r["agent"]["series"]) == 3


def test_days_beaten_counts_daily_wins():
    aj_analytics.equity_curve = lambda days=90: [
        {"date": "2026-06-26", "equity_usd": 0.0},
        {"date": "2026-06-29", "equity_usd": 200.0},   # +2% day
        {"date": "2026-06-30", "equity_usd": 210.0}]   # ~+0.1% day
    _patch_indexes({"SPY": {"2026-06-26": 100.0, "2026-06-29": 101.0, "2026-06-30": 105.0}})
    #   day1: agent +2% vs SPY +1% -> agent wins ; day2: agent +0.1% vs SPY ~+4% -> agent loses
    r = BM.benchmark(_cfg())
    v = r["vs"]["SPY"]
    assert v["days_total"] == 2 and v["days_beaten"] == 1, v


def test_days_beaten_uses_true_daily_returns_after_drift():
    # Once the agent NAV has drifted far from its base, differencing the
    # cumulative-% series overstated its daily move (a +1% day on a doubled NAV
    # showed as +2 cumulative points). The beat test must use TRUE daily
    # returns off consecutive raw values.
    aj_analytics.equity_curve = lambda days=90: [
        {"date": "2026-06-26", "equity_usd": 0.0},       # NAV 10000
        {"date": "2026-06-29", "equity_usd": 10000.0},   # NAV 20000: +100% day
        {"date": "2026-06-30", "equity_usd": 10200.0}]   # NAV 20200: +1% day
    _patch_indexes({"SPY": {"2026-06-26": 100.0, "2026-06-29": 100.0,
                            "2026-06-30": 101.5}})       # day2: +1.5%
    r = BM.benchmark(_cfg())
    v = r["vs"]["SPY"]
    # day1: +100% vs 0% -> win. day2: +1% vs +1.5% -> LOSS (the old cum-diff
    # math scored it +2.0 vs +1.5 points and wrongly counted a win).
    assert v["days_total"] == 2 and v["days_beaten"] == 1, v


def test_fail_open_on_bad_index_data():
    aj_analytics.equity_curve = lambda days=90: [
        {"date": "2026-06-29", "equity_usd": 100.0},
        {"date": "2026-06-30", "equity_usd": 200.0}]
    fetcher.get_chart_data = lambda s, p, i="1d": (_ for _ in ()).throw(RuntimeError("net down"))
    r = BM.benchmark(_cfg())
    # agent series still computed; no benchmarks -> verdict n/a, no crash
    assert r.get("agent") and r["verdict"] in ("n/a", "insufficient data"), r


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_benchmark — {} tests".format(len(fns)))
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
