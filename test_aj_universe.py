#!/usr/bin/env python3
"""Phase A/B tests — market-screener universe + scan/gate agreement + crypto.

Network-free: idea_generator (population) and fetcher (quotes) are monkeypatched.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajuniv.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_universe              # noqa: E402
import aj_operator              # noqa: E402

aj_db.aj_init()


def _mock_population(equities, cryptos=("BTC", "ETH")):
    import idea_generator as ig
    ig.get_full_equity_universe = lambda: list(equities)
    ig.get_full_crypto_universe = lambda top_n=60: [{"symbol": c} for c in cryptos]


def _mock_quotes(qmap):
    import fetcher
    fetcher.get_quotes_batch = lambda syms: {s: qmap[s] for s in syms if s in qmap}


def test_screen_filters_and_ranks():
    _mock_population(["AAA", "BBB", "CCC", "PENNY", "ILLIQ"])
    _mock_quotes({
        "AAA": {"price": 100, "volume": 1e6, "market_cap": 5e9, "change_pct": 5.0},   # strong mover
        "BBB": {"price": 50, "volume": 2e6, "market_cap": 3e9, "change_pct": 1.0},
        "CCC": {"price": 20, "volume": 5e6, "market_cap": 2e9, "change_pct": 8.0},     # strongest mover
        "PENNY": {"price": 0.40, "volume": 1e8, "market_cap": 1e7, "change_pct": 50},  # sub-$1 -> dropped
        "ILLIQ": {"price": 80, "volume": 100, "market_cap": 1e9, "change_pct": 9.0},   # low $vol -> dropped
        "BTC-USD": {"price": 60000, "volume": 1e5, "market_cap": 1e12, "change_pct": 2.0},
        "ETH-USD": {"price": 3000, "volume": 1e5, "market_cap": 4e11, "change_pct": 3.0},
    })
    aj_config.set_config({"universe_mode": "market_screen", "screen_min_price": 1.0,
                          "screen_min_dollar_volume": 1_000_000, "screen_max": 10,
                          "include_crypto": True})
    out = aj_universe.screen()
    assert "PENNY" not in out and "ILLIQ" not in out, out      # filtered
    # equities ranked by momentum: CCC(8) > AAA(5) > BBB(1)
    eq = [s for s in out if not s.endswith("-USD")]
    assert eq[:3] == ["CCC", "AAA", "BBB"], eq
    # crypto always included
    assert "BTC-USD" in out and "ETH-USD" in out


def test_screen_respects_screen_max():
    _mock_population(["A", "B", "C", "D", "E"], cryptos=())
    _mock_quotes({s: {"price": 10, "volume": 1e7, "market_cap": 1e9, "change_pct": i}
                  for i, s in enumerate(["A", "B", "C", "D", "E"])})
    aj_config.set_config({"screen_max": 2, "include_crypto": False})
    out = aj_universe.screen()
    assert len([s for s in out if not s.endswith("-USD")]) == 2


def test_screen_fail_closed_on_quote_error():
    _mock_population(["A", "B"], cryptos=("BTC",))
    import fetcher
    fetcher.get_quotes_batch = lambda syms: (_ for _ in ()).throw(RuntimeError("net down"))
    aj_config.set_config({"include_crypto": True})
    out = aj_universe.screen()
    # equities can't be screened (no quotes) -> dropped; crypto still included
    assert "A" not in out and "B" not in out
    assert "BTC-USD" in out


def test_rotation_advances_cursor():
    aj_db.set_setting_raw("__aj_screen_cursor", "0")
    pop = [str(i) for i in range(10)]
    s1 = aj_universe._rotate(list(pop), 3)
    s2 = aj_universe._rotate(list(pop), 3)
    assert s1 == ["0", "1", "2"] and s2 == ["3", "4", "5"]   # swept forward


def test_scan_dispatch_market_screen():
    aj_config.set_config({"universe_mode": "market_screen", "symbol_allowlist": ["AAPL"]})
    _orig = aj_universe.screen
    aj_universe.screen = lambda cfg=None: ["NVDA", "TSLA", "BTC-USD"]
    try:
        out = aj_operator._scan_universe()
        assert out[0] == "AAPL"                       # allowlist included first
        assert "NVDA" in out and "BTC-USD" in out     # screened names present
    finally:
        aj_universe.screen = _orig                    # restore (don't pollute siblings)


def test_scan_dispatch_allowlist_failclosed():
    aj_config.set_config({"universe_mode": "allowlist", "allow_any_symbol": False,
                          "symbol_allowlist": ["AAPL", "NVDA"]})
    out = aj_operator._scan_universe()
    assert sorted(out) == ["AAPL", "NVDA"]
    assert aj_config.is_open_universe() is False  # gate stays fail-closed too


def test_open_universe_helper_agrees():
    aj_config.set_config({"universe_mode": "market_screen", "allow_any_symbol": False})
    assert aj_config.is_open_universe() is True
    aj_config.set_config({"universe_mode": "open"})
    assert aj_config.is_open_universe() is True
    aj_config.set_config({"universe_mode": "allowlist", "allow_any_symbol": False})
    assert aj_config.is_open_universe() is False
    # legacy back-compat: allow_any_symbol forces open even in allowlist mode
    aj_config.set_config({"allow_any_symbol": True})
    assert aj_config.is_open_universe() is True
    aj_config.set_config({"universe_mode": "market_screen", "allow_any_symbol": False})


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_universe — {} tests".format(len(fns)))
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
