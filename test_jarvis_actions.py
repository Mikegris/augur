"""Offline tests for Jarvis agency (portfolio mutation + UI auto-navigation).

Covers the new mutating tools (add_holding / record_trade), their proposal
validation/labels, the keyless rule intents (_answer_trade / _answer_navigate),
and that execute_mutating writes through to the DB layer. No network, no LLM.
"""
import sys

import jarvis
import jarvis_tools as jt


# ── mutating tool registry + validation ──────────────────────────────────────

def test_tools_registered_mutating():
    for n in ("add_holding", "record_trade"):
        assert n in jt.TOOLS, n
        assert jt.is_mutating(n), n
    print("  [OK] add_holding & record_trade registered as mutating")


def test_valid_proposal_args():
    ok = jt.valid_proposal_args
    assert ok("add_holding", {"symbol": "AAPL", "shares": 10, "price": 150})
    assert ok("add_holding", {"symbol": "BTC-USD", "shares": 0.5, "price": 60000})
    assert not ok("add_holding", {"symbol": "AAPL", "shares": -1, "price": 150})
    assert not ok("add_holding", {"symbol": "AAPL", "shares": 10, "price": -5})
    assert not ok("add_holding", {"symbol": "@@", "shares": 10, "price": 150})
    assert not ok("add_holding", {"symbol": "AAPL", "shares": "many", "price": 150})
    assert ok("record_trade", {"symbol": "TSLA", "side": "SELL", "shares": 3, "price": 250})
    assert ok("record_trade", {"symbol": "TSLA", "side": "buy", "shares": 3, "price": 250})
    assert not ok("record_trade", {"symbol": "TSLA", "side": "HODL", "shares": 3, "price": 250})
    print("  [OK] valid_proposal_args bounds shares/price/symbol/side")


def test_proposal_labels():
    assert jt.proposal_label("add_holding", {"symbol": "aapl", "shares": 10, "price": 150.5}) \
        == "Buy 10 AAPL @ $150.50"
    assert jt.proposal_label("record_trade", {"symbol": "tsla", "side": "sell", "shares": 3, "price": 250}) \
        == "Log SELL 3 TSLA @ $250.00"
    print("  [OK] proposal labels render symbol/shares/price")


def test_execute_mutating_writes_through():
    import database as db
    calls = {"pos": [], "txn": []}
    orig_pos, orig_txn = db.add_position, db.add_transaction
    try:
        db.add_position = lambda **kw: (calls["pos"].append(kw) or 42)
        db.add_transaction = lambda **kw: calls["txn"].append(kw)
        r = jt.execute_mutating("add_holding", {"symbol": "nvda", "shares": 2, "price": 800})
        assert r.get("status") == "added" and r.get("id") == 42, r
        assert calls["pos"][0]["symbol"] == "NVDA" and calls["pos"][0]["shares"] == 2
        assert calls["txn"][0]["action"] == "BUY", calls["txn"]
        r2 = jt.execute_mutating("record_trade", {"symbol": "tsla", "side": "SELL", "shares": 1, "price": 250})
        assert r2.get("status") == "logged", r2
        assert calls["txn"][1]["action"] == "SELL", calls["txn"]
        # a read tool name must be rejected by the mutating gate
        assert jt.execute_mutating("get_quote", {"symbol": "AAPL"}).get("error")
    finally:
        db.add_position, db.add_transaction = orig_pos, orig_txn
    print("  [OK] execute_mutating writes position+BUY / SELL journal, rejects reads")


def test_invalid_symbol_raises_clean_error():
    # execute_mutating wraps exceptions into an error envelope, never raises.
    r = jt.execute_mutating("add_holding", {"symbol": "###", "shares": 1, "price": 1})
    assert isinstance(r, dict) and r.get("error"), r
    print("  [OK] bad symbol -> error envelope, no exception")


# ── keyless trade-capture intent ─────────────────────────────────────────────

def test_answer_trade_buy_and_sell():
    r = jarvis._answer_trade("buy 10 NVDA at 800")
    assert r and r["proposal"]["tool"] == "add_holding"
    assert r["proposal"]["args"] == {"symbol": "NVDA", "shares": 10.0, "price": 800.0}
    r = jarvis._answer_trade("I bought 5 shares of AAPL at 150.25")
    assert r and r["proposal"]["args"]["symbol"] == "AAPL" and r["proposal"]["args"]["shares"] == 5.0
    r = jarvis._answer_trade("sell 3 TSLA at 250")
    assert r and r["proposal"]["tool"] == "record_trade" and r["proposal"]["args"]["side"] == "SELL"
    r = jarvis._answer_trade("log a sell of 2 MSFT @ 400")
    assert r and r["proposal"]["args"]["symbol"] == "MSFT" and r["proposal"]["args"]["shares"] == 2.0
    print("  [OK] _answer_trade parses buy/sell with shares+price")


def test_answer_trade_company_name():
    r = jarvis._answer_trade("buy 10 apple at 195")
    assert r and r["proposal"]["args"]["symbol"] == "AAPL", r
    print("  [OK] _answer_trade resolves company name -> ticker")


def test_answer_trade_rejects_ambiguous():
    # conviction question (no number), watchlist add (no price), and a bare
    # price-less "add" must NOT produce a trade proposal.
    for q in ("should I sell NVDA?", "add NVDA to my watchlist",
              "what if I buy NVDA", "buy NVDA"):
        assert jarvis._answer_trade(q) is None, q
    print("  [OK] _answer_trade ignores price-less / conviction phrasing")


def test_answer_trade_proposal_is_executable():
    # The proposal it emits must itself pass the server-side re-validation.
    r = jarvis._answer_trade("buy 4 GOOGL at 180")
    p = r["proposal"]
    assert jt.valid_proposal_args(p["tool"], p["args"]), p
    print("  [OK] emitted proposal passes server-side valid_proposal_args")


# ── keyless UI auto-navigation intent ────────────────────────────────────────

def test_answer_navigate_views():
    cases = {
        "go to portfolio": ("portfolio", None),
        "take me to settings": ("settings", None),
        "show me my watchlist": ("watchlist", None),
        "navigate to stress test": ("stress", None),
        "open monte carlo": ("montecarlo", None),
    }
    for q, (view, sym) in cases.items():
        r = jarvis._answer_navigate(q)
        assert r and r["action"]["view"] == view, (q, r)
        assert r["action"].get("auto") is True, q
    print("  [OK] _answer_navigate maps phrases -> views with auto=True")


def test_answer_navigate_symbol_views():
    r = jarvis._answer_navigate("open the forecast for NVDA")
    assert r["action"]["view"] == "forecast" and r["action"]["symbol"] == "NVDA", r
    r = jarvis._answer_navigate("open research for AAPL")
    assert r["action"]["view"] == "research" and r["action"]["symbol"] == "AAPL", r
    print("  [OK] _answer_navigate carries symbol for research/forecast")


def test_answer_navigate_ignores_non_nav():
    for q in ("show me my biggest position", "what is my portfolio worth",
              "how is NVDA doing", "buy 10 NVDA at 800"):
        assert jarvis._answer_navigate(q) is None, q
    print("  [OK] _answer_navigate returns None for non-navigation queries")


def test_ask_routes_navigate_and_trade(monkeypatch=None):
    # End-to-end through ask() (persist=False so no DB writes): a nav request
    # returns an auto action; a trade returns a proposal.
    nav = jarvis.ask("go to portfolio", persist=False)
    assert nav.get("intent") == "navigate" and nav["action"]["auto"] is True, nav
    tr = jarvis.ask("buy 10 NVDA at 800", persist=False)
    assert tr.get("proposal", {}).get("tool") == "add_holding", tr
    print("  [OK] ask() routes navigate->auto action and trade->proposal")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("jarvis_actions — {} tests".format(len(fns)))
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
