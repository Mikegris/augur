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
    db.get_conn().execute("DELETE FROM aj_screen_cache"); db.get_conn().commit()
    aj_db.set_setting_raw("__aj_crypto_lastgood", "")
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
    db.get_conn().execute("DELETE FROM aj_screen_cache"); db.get_conn().commit()
    _mock_population(["A", "B", "C", "D", "E"], cryptos=())
    _mock_quotes({s: {"price": 10, "volume": 1e7, "market_cap": 1e9, "change_pct": i}
                  for i, s in enumerate(["A", "B", "C", "D", "E"])})
    aj_config.set_config({"screen_max": 2, "include_crypto": False})
    out = aj_universe.screen()
    assert len([s for s in out if not s.endswith("-USD")]) == 2


def test_screen_fail_closed_on_quote_error():
    db.get_conn().execute("DELETE FROM aj_screen_cache"); db.get_conn().commit()
    aj_db.set_setting_raw("__aj_crypto_lastgood", "")
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
    # _rotate no longer persists the cursor itself: the caller commits via
    # _advance_cursor only after the slice's quotes succeed (so a 429/empty
    # batch retries the same slice instead of skipping it).
    s1, n1 = aj_universe._rotate(list(pop), 3)
    aj_universe._advance_cursor(n1)                       # quotes "succeeded"
    s2, n2 = aj_universe._rotate(list(pop), 3)
    assert s1 == ["0", "1", "2"] and s2 == ["3", "4", "5"]   # swept forward
    # a failed batch (cursor NOT committed) must re-serve the same slice
    s3, _ = aj_universe._rotate(list(pop), 3)
    assert s3 == s2, "uncommitted cursor must retry the same slice"


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


def test_screen_global_cache_ranks_across_cycles():
    # population of 4, batch 2 -> cycle1 sees AAA/BBB, cycle2 sees CCC/DDD.
    # AAA is the strongest mover; it must still rank into cycle2's shortlist
    # from the cache even though it's NOT in cycle2's slice.
    db.get_conn().execute("DELETE FROM aj_screen_cache"); db.get_conn().commit()
    aj_db.set_setting_raw("__aj_screen_cursor", "0")
    _mock_population(["AAA", "BBB", "CCC", "DDD"], cryptos=())
    _mock_quotes({
        "AAA": {"price": 100, "volume": 1e7, "market_cap": 1e10, "change_pct": 9.0},
        "BBB": {"price": 50, "volume": 1e7, "market_cap": 1e10, "change_pct": 1.0},
        "CCC": {"price": 20, "volume": 1e7, "market_cap": 1e10, "change_pct": 2.0},
        "DDD": {"price": 30, "volume": 1e7, "market_cap": 1e10, "change_pct": 3.0},
    })
    aj_config.set_config({"screen_scan_batch": 2, "screen_max": 5, "include_crypto": False,
                          "screen_cache_ttl_min": 60})
    c1 = aj_universe.screen()
    assert "AAA" in c1
    c2 = aj_universe.screen()                  # slice is CCC/DDD now
    assert "AAA" in c2, "strong name from a prior slice must persist via the cache"
    assert c2[0] == "AAA"                      # still the top mover globally


def test_crypto_lastgood_fallback():
    import idea_generator as ig
    aj_db.set_setting_raw("__aj_crypto_lastgood", "")
    big = [{"symbol": "C%02d" % i} for i in range(30)]
    ig.get_full_crypto_universe = lambda top_n=60: big
    got1 = aj_universe._crypto_population({"include_crypto": True, "crypto_universe_top": 60})
    assert len(got1) == 30 and got1[0] == "C00-USD"
    # now the fetch fails -> must reuse the last-good 30, not collapse
    ig.get_full_crypto_universe = lambda top_n=60: (_ for _ in ()).throw(RuntimeError("429"))
    got2 = aj_universe._crypto_population({"include_crypto": True, "crypto_universe_top": 60})
    assert len(got2) == 30, "should reuse last-good crypto list on fetch failure"


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


def test_junk_tickers_excluded_and_telemetry_writes():
    db.get_conn().execute("DELETE FROM aj_screen_cache"); db.get_conn().commit()
    aj_db.set_setting_raw("__aj_crypto_lastgood", "")
    aj_db.set_setting_raw("__aj_screen_telemetry", "")
    _mock_population(["GOOD", "CORZW", "BODYW", "REAL"], cryptos=())
    _mock_quotes({
        "GOOD":  {"price": 40, "volume": 5e6, "market_cap": 2e9, "change_pct": 6.0},
        "CORZW": {"price": 12, "volume": 5e6, "market_cap": 2e9, "change_pct": 9.0},  # warrant
        "BODYW": {"price": 15, "volume": 5e6, "market_cap": 2e9, "change_pct": 8.0},  # warrant
        "REAL":  {"price": 30, "volume": 5e6, "market_cap": 2e9, "change_pct": 4.0},
    })
    aj_config.set_config({"universe_mode": "market_screen", "screen_min_price": 1.0,
                          "screen_min_dollar_volume": 1_000_000, "screen_max": 10,
                          "include_crypto": False})
    out = aj_universe.screen()
    # warrants hard-excluded even though they were the strongest movers
    assert "CORZW" not in out and "BODYW" not in out, out
    assert "GOOD" in out and "REAL" in out, out
    # telemetry actually persisted (the aj_db NameError regression)
    import json
    tel = aj_db.get_setting_raw("__aj_screen_telemetry")
    assert tel, "screen telemetry never wrote"
    t = json.loads(tel)
    assert t["shortlist"] == len(out) and "quote_hit_rate" in t


def test_is_junk_ticker_unit():
    for j in ("CORZW", "BODYW", "BLUWW", "SPAC.WS", "FOO-WT", "BAR-UN"):
        assert aj_universe._is_junk_ticker(j), j
    for ok in ("AAPL", "NVDA", "BRK-B", "BTC-USD", "GOOGL", ""):
        assert not aj_universe._is_junk_ticker(ok) or ok == "", ok


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
