"""Offline tests for portfolio_insights (v2.5). Pure math on synthetic series."""
import sys

import portfolio_insights as pi


def test_rsi_bounds_and_extremes():
    assert pi.rsi([1, 2, 3]) is None                      # too short
    up = list(range(1, 40))                                # monotonic up
    assert pi.rsi(up) == 100.0
    down = list(range(40, 1, -1))
    assert pi.rsi(down) is not None and pi.rsi(down) < 30  # oversold
    print("  [OK] rsi: short→None, all-up→100, all-down→oversold")


def test_max_drawdown():
    assert pi.max_drawdown([100, 120, 90, 110]) == -25.0   # 120→90
    assert pi.max_drawdown([100, 101, 102]) == 0.0         # never declined
    assert pi.max_drawdown([5]) is None
    print("  [OK] max_drawdown: -25% peak-to-trough, 0 when only rising")


def test_annualized_vol_zero_for_flat():
    assert pi.annualized_vol([10] * 30) == 0.0
    noisy = [10 + (i % 2) for i in range(30)]
    assert pi.annualized_vol(noisy) > 0
    print("  [OK] annualized_vol: 0 for flat, >0 for noisy")


def test_momentum_score_direction():
    up = [float(i) for i in range(1, 260)]                 # strong uptrend
    down = [float(i) for i in range(260, 1, -1)]
    su, sd = pi.momentum_score(up), pi.momentum_score(down)
    assert su is not None and sd is not None
    assert su > sd and su >= 66 and sd <= 38, (su, sd)
    assert pi.momentum_score([1, 2, 3]) is None            # too short
    print("  [OK] momentum_score: uptrend≥66 > downtrend≤38, short→None")


def test_range_position():
    assert pi.range_position(150, 100, 200) == 50.0
    assert pi.range_position(200, 100, 200) == 100.0
    assert pi.range_position(150, 200, 100) is None        # degenerate band
    print("  [OK] range_position: midpoint=50, top=100, degenerate→None")


def test_sharpe_proxy_sign():
    up = [100 * (1.005 ** i) for i in range(60)]           # steady gains
    assert pi.sharpe_proxy(up) is not None and pi.sharpe_proxy(up) > 0
    assert pi.sharpe_proxy([10, 10]) is None               # zero vol
    print("  [OK] sharpe_proxy: positive for steady gains, None at zero vol")


def test_series_metrics_and_stance():
    # Realistic trends (with oscillation) so RSI isn't pinned at an extreme.
    up = [100 + i + ((-1) ** i) * 2 for i in range(260)]      # net uptrend
    m = pi.series_metrics(up)
    assert set(["rsi14", "max_drawdown_pct", "annualized_vol_pct",
                "momentum_score", "sharpe_proxy", "n_bars"]).issubset(m)
    assert pi._stance(m) == "bull", m
    down = [100 + (260 - i) + ((-1) ** i) * 2 for i in range(260)]  # net downtrend
    assert pi._stance(pi.series_metrics(down)) == "bear", pi.series_metrics(down)
    print("  [OK] series_metrics bundles signals; stance bull/bear correct")


def test_portfolio_health_aggregates(monkeypatch=None):
    import database as db
    import portfolio_insights as p
    db.get_portfolio = lambda: [
        {"symbol": "AAA", "shares": 10, "asset_type": "stock"},
        {"symbol": "BBB", "shares": 10, "asset_type": "stock"},
        {"symbol": "DOGE", "shares": 1, "asset_type": "crypto"},  # excluded
    ]
    import fetcher
    fetcher.get_quotes_batch = lambda syms: {s: {"price": 100.0} for s in syms}
    p.symbol_signal = lambda sym, period="1y": {
        "symbol": sym, "momentum_score": 80 if sym == "AAA" else 30,
        "above_sma50": sym == "AAA", "range_position_52w": 90 if sym == "AAA" else 20,
        "max_drawdown_pct": -10 if sym == "AAA" else -30,
    }
    out = p.portfolio_health()
    assert out["n_holdings"] == 2 and out["analyzed"] == 2, out   # crypto excluded
    assert out["near_52w_high"] == 1 and out["in_deep_drawdown"] == 1, out
    assert out["weighted_momentum"] is not None and out["tone"] in (
        "constructive", "mixed", "defensive")
    print("  [OK] portfolio_health: excludes crypto, aggregates breadth/momentum")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("portfolio_insights — {} tests".format(len(fns)))
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
