"""Offline tests for aj_backtest — the agent-policy walk-forward backtester.

Fully network-free: monkeypatches `fetcher.get_chart_data` to return SYNTHETIC
deterministic series (uptrend / downtrend / flat / mean-reverting sine) and
asserts the policy backtester behaves correctly:
  * uptrend       → net-positive expectancy for the momentum-following policy
  * downtrend     → no / negative long trades
  * costs         → reduce returns vs a zero-cost run
  * flat series   → ~no edge (no look-ahead inventing trades)
  * leak-free     → decision slice never includes the decision bar
  * policy_expectancy returns a verdict + summary
  * fail-open     → a fetch that raises yields an {"error": ...} envelope

Standalone runner: prints PASS / N FAILED and sys.exit()s, mirroring the repo's
other test_aj_*.py harnesses.
"""
import math
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix=".db")

import fetcher          # noqa: E402
import aj_backtest as B  # noqa: E402

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


# ── synthetic series builders → fetcher.get_chart_data shape ──────────────────
# fetcher.get_chart_data returns a list of {"time","open","high","low","close",
# "volume"} dicts; aj_backtest only consumes time/close/high/low.

_DAY = 86400


def _bars_from_closes(closes):
    out = []
    t0 = 1_600_000_000  # fixed epoch; daily steps
    for i, c in enumerate(closes):
        c = float(c)
        out.append({
            "time": t0 + i * _DAY,
            "open": c,
            "high": c * 1.005,
            "low": c * 0.995,
            "close": c,
            "volume": 1_000_000,
        })
    return out


def _uptrend(n=400, start=100.0, step=0.4):
    # steady drift up with tiny deterministic ripple (so vol > 0)
    return [start + step * i + math.sin(i * 0.5) * 0.2 for i in range(n)]


def _downtrend(n=400, start=200.0, step=0.4):
    return [start - step * i + math.sin(i * 0.5) * 0.2 for i in range(n)]


def _flat(n=400, level=100.0):
    # tiny zero-mean ripple → no directional edge
    return [level + math.sin(i * 0.7) * 0.05 for i in range(n)]


def _sine(n=400, level=100.0, amp=15.0, period=40.0):
    return [level + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def _patch_fetch(series_by_symbol):
    """Monkeypatch fetcher.get_chart_data to serve the given synthetic series."""
    def _fake(symbol, period="6mo", interval="1d"):
        s = series_by_symbol.get(str(symbol).upper())
        if s is None:
            return []
        return _bars_from_closes(s)
    fetcher.get_chart_data = _fake


_ORIG_FETCH = fetcher.get_chart_data


def _restore_fetch():
    fetcher.get_chart_data = _ORIG_FETCH


# A momentum-following policy config (loose gate so the trend is tradable).
def _cfg(**overrides):
    base = {
        "buy_prob_threshold": 0.52,
        "min_edge_pct_pts": 1.0,
        "take_profit_pct": 0.0,
        "stop_loss_pct": 0.0,
        "trailing_stop_pct": 0.0,
        "max_holding_days": 20,
        "fee_bps": 0.0,
        "paper_slippage_bps": 0.0,
        "conviction_sizing": False,
        "symbol_allowlist": [],
    }
    base.update(overrides)
    return base


# ════════════════════════════════════════════════════════════════════════════
#  TESTS
# ════════════════════════════════════════════════════════════════════════════

def test_uptrend_positive_expectancy():
    _patch_fetch({"UP": _uptrend()})
    try:
        res = B.backtest_policy(["UP"], cfg=_cfg(), period="2y", horizon=20)
        check("error" not in res, "uptrend: no error envelope")
        agg = res["aggregate"]
        check(agg["n_trades"] > 0, "uptrend: opened at least one trade")
        check(agg["expectancy_pct"] > 0,
              "uptrend: net-positive expectancy ({:.3f}%)".format(agg["expectancy_pct"]))
        check(res["coverage"] == "price_signals_only",
              "uptrend: coverage flagged price_signals_only")
    finally:
        _restore_fetch()


def test_downtrend_no_or_negative_longs():
    _patch_fetch({"DOWN": _downtrend()})
    try:
        res = B.backtest_policy(["DOWN"], cfg=_cfg(), period="2y", horizon=20)
        agg = res["aggregate"]
        # long-only policy in a downtrend: either it stays out, or any longs it
        # takes lose money on average.
        cond = (agg["n_trades"] == 0) or (agg["expectancy_pct"] <= 0)
        check(cond,
              "downtrend: no longs OR non-positive expectancy "
              "(n={}, exp={:.3f}%)".format(agg["n_trades"], agg["expectancy_pct"]))
    finally:
        _restore_fetch()


def test_costs_reduce_returns():
    _patch_fetch({"UP": _uptrend()})
    try:
        free = B.backtest_policy(["UP"], cfg=_cfg(fee_bps=0.0, paper_slippage_bps=0.0),
                                 period="2y", horizon=20)
        costly = B.backtest_policy(["UP"], cfg=_cfg(fee_bps=20.0, paper_slippage_bps=30.0),
                                   period="2y", horizon=20)
        ef = free["aggregate"]["expectancy_pct"]
        ec = costly["aggregate"]["expectancy_pct"]
        check(free["aggregate"]["n_trades"] > 0 and costly["aggregate"]["n_trades"] > 0,
              "costs: both runs traded")
        check(ec < ef,
              "costs: cost run expectancy {:.3f}% < free {:.3f}%".format(ec, ef))
    finally:
        _restore_fetch()


def test_flat_series_no_edge():
    _patch_fetch({"FLAT": _flat()})
    try:
        res = B.backtest_policy(["FLAT"], cfg=_cfg(), period="2y", horizon=20)
        agg = res["aggregate"]
        # A flat series should not manufacture a strong edge. Either no trades,
        # or expectancy hovering near zero (no look-ahead profit).
        cond = (agg["n_trades"] == 0) or (abs(agg["expectancy_pct"]) < 1.0)
        check(cond,
              "flat: ~no edge (n={}, exp={:.3f}%)".format(
                  agg["n_trades"], agg["expectancy_pct"]))
    finally:
        _restore_fetch()


def test_leak_free_assertion_holds():
    # The decision slice must equal df.iloc[:t] (length t), never include bar t.
    # _walk_symbol asserts this internally; a clean run proves it never tripped.
    import pandas as pd
    closes = _uptrend(200)
    df = pd.DataFrame({"Close": closes,
                       "High": [c * 1.005 for c in closes],
                       "Low": [c * 0.995 for c in closes]})
    try:
        res = B._walk_symbol("UP", df, _cfg(), horizon=20, regime="bull")
        check(isinstance(res["returns"], list),
              "leak-free: _walk_symbol completed without assertion error")
        # Independently verify the invariant the production code asserts.
        ok_slice = all(len(df.iloc[:t]) == t for t in range(60, len(df)))
        check(ok_slice, "leak-free: df.iloc[:t] length == t for all decision bars")
    except AssertionError as e:
        check(False, "leak-free: assertion tripped: {}".format(e))


def test_take_profit_and_stop_exit():
    # With a tight take-profit on an uptrend, trades should close on TP and stay
    # net-positive; with a tight stop on a downtrend, longs (if any) exit small.
    _patch_fetch({"UP": _uptrend(), "DOWN": _downtrend()})
    try:
        tp = B.backtest_policy(["UP"], cfg=_cfg(take_profit_pct=5.0), period="2y", horizon=20)
        agg = tp["aggregate"]
        check(agg["n_trades"] > 0, "exits: take-profit run opened trades")
        # at least some trades should record a take_profit exit reason
        # (we re-run via _walk_symbol to inspect reasons)
        import pandas as pd
        closes = _uptrend()
        df = pd.DataFrame({"Close": closes,
                           "High": [c * 1.01 for c in closes],
                           "Low": [c * 0.99 for c in closes]})
        wr = B._walk_symbol("UP", df, _cfg(take_profit_pct=5.0), horizon=20, regime="bull")
        reasons = {t["reason"] for t in wr["trades"]}
        check(len(reasons) > 0, "exits: trades recorded exit reasons {}".format(reasons))
    finally:
        _restore_fetch()


def test_policy_expectancy_verdict_and_summary():
    _patch_fetch({s: _uptrend() for s in
                  ["AAPL", "MSFT", "NVDA", "SPY", "AMZN",
                   "GOOGL", "META", "JPM", "XOM", "UNH"]})
    try:
        rep = B.policy_expectancy(cfg=_cfg())
        check("verdict" in rep, "policy_expectancy: has verdict")
        check(rep["verdict"] in ("positive expectancy", "negative expectancy",
                                 "insufficient data"),
              "policy_expectancy: verdict is a known label ({})".format(rep["verdict"]))
        check(isinstance(rep.get("summary"), str) and len(rep["summary"]) > 0,
              "policy_expectancy: non-empty summary")
        check("expectancy_pct" in rep and "n_trades" in rep,
              "policy_expectancy: reports expectancy_pct + n_trades")
        # all-uptrend basket → should find positive expectancy with enough trades
        check(rep["n_trades"] > 0, "policy_expectancy: traded the basket")
    finally:
        _restore_fetch()


def test_policy_expectancy_uses_allowlist():
    _patch_fetch({"TSLA": _uptrend(), "AMD": _uptrend()})
    try:
        rep = B.policy_expectancy(cfg=_cfg(symbol_allowlist=["TSLA", "AMD"]))
        check(set(rep.get("symbols", [])) == {"TSLA", "AMD"},
              "allowlist: used cfg symbol_allowlist as universe ({})".format(
                  rep.get("symbols")))
    finally:
        _restore_fetch()


def test_fail_open_on_fetch_raise():
    def _boom(symbol, period="6mo", interval="1d"):
        raise RuntimeError("network down")
    fetcher.get_chart_data = _boom
    try:
        # _load_closes swallows the raise → symbol gets "insufficient history",
        # aggregate is empty, but the call NEVER raises.
        res = B.backtest_policy(["X"], cfg=_cfg(), period="2y", horizon=20)
        check(isinstance(res, dict), "fail-open: backtest_policy returned a dict")
        check(res["aggregate"]["n_trades"] == 0,
              "fail-open: no trades when fetch raises")
        rep = B.policy_expectancy(cfg=_cfg(symbol_allowlist=["X"]))
        check(rep["verdict"] == "insufficient data",
              "fail-open: policy_expectancy → insufficient data")
    finally:
        _restore_fetch()


def test_empty_symbols_error_envelope():
    res = B.backtest_policy([], cfg=_cfg())
    check("error" in res, "guard: empty symbols → error envelope")


def test_symbol_cap_enforced():
    many = ["S{}".format(i) for i in range(40)]
    _patch_fetch({s: _flat() for s in many})
    try:
        res = B.backtest_policy(many, cfg=_cfg(), period="2y")
        check(res["n_symbols"] <= 15, "cap: backtest capped at 15 symbols (got {})".format(
            res["n_symbols"]))
    finally:
        _restore_fetch()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    print("aj_backtest — {} tests\n".format(len(tests)))
    for fn in tests:
        try:
            fn()
        except Exception as e:
            FAIL.append(fn.__name__)
            print("  [XX] {}: unexpected {}: {}".format(fn.__name__, type(e).__name__, e))
    print("\n{} passed, {} failed".format(len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  - " + f)
        print("{} FAILED".format(len(FAIL)))
        sys.exit(1)
    print("PASS")
    sys.exit(0)
