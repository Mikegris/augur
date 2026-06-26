"""
jarvis_counterfactual — the counterfactual coach.

"What if I hadn't sold X?" answered with real dollar numbers from the user's
own transaction log, not vibes. Two entry points:

1. analyze(symbol=None, limit=10) — replays every recorded SELL against
   today's price. For each sell: delta_usd = (price_now − sell_price) × shares.
   Positive delta means the position kept climbing after the exit (regret);
   negative means the sell dodged a drawdown (relief); anything inside ±1% of
   the sale proceeds is a wash — too small for the market to have rendered a
   verdict. Ends with one dry, coach-voiced paragraph. Observations only,
   never advice — the coach narrates, the user decides.

2. holding_discipline() — average holding period and quick-flip count from
   BUY→SELL pairs, FIFO-matched per symbol (approximation documented on the
   function).

Everything fails open: no transactions, dead quote feeds, malformed rows —
the worst outcome is an empty-but-well-shaped dict. Python 3.9 compatible.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import database as db
import fetcher

log = logging.getLogger("augur.jarvis_counterfactual")

# A sell whose subsequent move is under 1% of the sale proceeds isn't a
# decision the market has graded yet — calling it regret or relief would be
# reading tea leaves. Band it as "wash".
_WASH_FRACTION = 0.01

# Round trips at or under this many days count as quick flips — same
# threshold jarvis_lens uses for its temperament check, kept in lockstep so
# the two coaches never disagree about what "quick" means.
_QUICK_FLIP_DAYS = 21

# How deep into the transaction log we read. 200 covers years of retail
# activity; anything older has long since stopped being a coaching moment.
_TXN_LIMIT = 200

_EMPTY_SUMMARY = "No sells on record — nothing to second-guess."


# ─── small helpers ────────────────────────────────────────────────────────────

def _num(v: Any) -> Optional[float]:
    """Defensive float coercion — rejects None, junk strings, and nan/inf
    (which would otherwise leak into formatted summaries as 'nan')."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _parse_day(s: Any) -> Optional[datetime]:
    """Transaction dates are stored as 'YYYY-MM-DD' (add_transaction) but
    older imports sometimes carry full ISO timestamps — slice to the day."""
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _usd(v: float) -> str:
    """$1,840 for real money, cents only when the amount is small enough
    that whole-dollar rounding would erase it. Always unsigned — the
    surrounding sentence carries the direction."""
    a = abs(v)
    return "${:,.2f}".format(a) if a < 100 else "${:,.0f}".format(a)


def _crypto_symbols() -> set:
    """Symbols currently held as crypto, per the portfolio table. These must
    be priced off SYM-USD — the bare ticker is often an unrelated equity or
    trust (the BTC-reviews-at-minus-100% bug, same lesson as jarvis_lens)."""
    try:
        return {
            (p.get("symbol") or "").upper()
            for p in (db.get_portfolio() or [])
            if (p.get("asset_type") or "").lower() == "crypto"
        }
    except Exception as e:
        log.debug("portfolio read failed, crypto set empty: %s", e)
        return set()


def _price_now(symbol: str, crypto_syms: set, cache: Dict[str, Optional[float]]) -> Optional[float]:
    """Today's price for a symbol the user once sold.

    Resolution order:
      • known crypto (in the portfolio's crypto set, or already -USD-suffixed)
        → quote SYM-USD straight away;
      • everything else → bare symbol first, then SYM-USD as a fallback for
        formerly-held crypto that's no longer in the portfolio (so the
        asset_type hint is gone). The fallback can mis-price an equity whose
        ticker collides with a coin, but only when the equity feed itself
        returned nothing — acceptable for a coaching number.

    None when no candidate yields a price; the caller skips that row rather
    than inventing a delta. Memoized per analyze() call so a symbol sold five
    times costs one fetch.
    """
    if symbol in cache:
        return cache[symbol]

    if symbol.endswith("-USD") or symbol in crypto_syms:
        sym_usd = symbol if symbol.endswith("-USD") else symbol + "-USD"
        candidates = [sym_usd]
    else:
        candidates = [symbol, symbol + "-USD"]

    price = None
    for cand in candidates:
        try:
            q = fetcher.get_quote(cand) or {}
        except Exception as e:
            log.debug("quote(%s) raised: %s", cand, e)
            continue
        p = _num(q.get("price"))
        if p is not None and p > 0:
            price = p
            break

    cache[symbol] = price
    return price


def _grouped_sells(txns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse SELL rows into per-(symbol, date) lots.

    Brokers report partial fills as separate rows; coaching the user about
    'three sells of SMCI' that were one order executed in three prints is
    noise. Shares are summed, price is share-weighted. Output sorted newest
    first (ISO dates sort lexically). Action case is normalized — older
    imports carry lowercase 'sell'.
    """
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for t in txns:
        if (t.get("action") or "").upper() != "SELL":
            continue
        shares = _num(t.get("shares"))
        price = _num(t.get("price"))
        if not shares or shares <= 0 or price is None:
            continue  # malformed row — nothing coachable in it
        sym = (t.get("symbol") or "").upper()
        day = str(t.get("date") or t.get("created_at") or "")[:10]
        if not sym or not day:
            continue
        key = (sym, day)
        g = groups.get(key)
        if g is None:
            groups[key] = {"symbol": sym, "date": day,
                           "shares": shares, "cost": shares * price}
        else:
            g["shares"] += shares
            g["cost"] += shares * price

    out = []
    for g in groups.values():
        out.append({
            "symbol": g["symbol"],
            "date": g["date"],
            "shares": g["shares"],
            "sell_price": g["cost"] / g["shares"],  # share-weighted fill
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def _coach_summary(rows: List[Dict[str, Any]], net: float) -> str:
    """One paragraph, steady and dry. Names the single biggest regret and
    the single biggest relief (the extremes are what the user remembers
    anyway), then the net. No advice to act — ever."""
    if not rows:
        return _EMPTY_SUMMARY

    regrets = [r for r in rows if r["verdict"] == "regret"]
    reliefs = [r for r in rows if r["verdict"] == "relief"]

    bits: List[str] = []
    if regrets:
        worst = max(regrets, key=lambda r: r["delta_usd"])
        bits.append("selling {} cost {} of subsequent upside".format(
            worst["symbol"], _usd(worst["delta_usd"])))
    if reliefs:
        best = min(reliefs, key=lambda r: r["delta_usd"])
        bits.append("the {} trim saved {}".format(
            best["symbol"], _usd(best["delta_usd"])))

    if bits:
        lead = "; ".join(bits)
        lead = lead[0].upper() + lead[1:] + "."
    else:
        # Every graded sell landed inside the wash band.
        lead = ("Every sell on record sits within a percent of today's "
                "price — washes, all of them.")

    # The net verdict. Under a dollar either way, don't pretend precision.
    # When every graded sell was a wash, force the wash tail regardless of the
    # net: summing many sub-band deltas can drift past a dollar in aggregate,
    # which would otherwise contradict the "washes, all of them" lead.
    if not regrets and not reliefs:
        tail = "Net, it comes out a wash — the market hasn't graded these yet."
    elif net > 1.0:
        tail = ("Net, the urge to sell has cost {} — worth remembering the "
                "next time the finger hovers.".format(_usd(net)))
    elif net < -1.0:
        tail = ("Net, the exits have saved {} — for what it's worth, "
                "patience cuts both ways.".format(_usd(net)))
    else:
        tail = "Net, it comes out a wash — the market hasn't graded these yet."

    return lead + " " + tail


# ─── 1. analyze — the counterfactual replay ──────────────────────────────────

def analyze(symbol: Optional[str] = None, limit: int = 10) -> Dict[str, Any]:
    """Replay recorded sells against today's prices.

    Returns:
        {"sells":  [{"symbol", "date", "shares", "sell_price", "price_now",
                     "delta_usd", "verdict"}…],   # newest first
         "net_usd": float,   # sum of delta_usd; positive = selling cost money
         "summary": str,     # one coach-voiced paragraph
         "n_sells": int}

    delta_usd = (price_now − sell_price) × shares. verdict is "regret" when
    the position kept rising, "relief" when the sell saved money, "wash" when
    |delta| < 1% of sale proceeds. `limit` caps the most recent N sells after
    same-day partial fills are grouped. Rows whose current price can't be
    resolved are skipped, never guessed. Fails open to the empty shape.
    """
    empty = {"sells": [], "net_usd": 0, "n_sells": 0, "summary": _EMPTY_SUMMARY}

    try:
        txns = db.get_transactions(symbol=symbol, limit=_TXN_LIMIT) or []
    except Exception as e:
        log.warning("transaction read failed: %s", e)
        return empty

    lots = _grouped_sells(txns)
    if not lots:
        return empty
    if limit and limit > 0:
        lots = lots[:limit]

    crypto_syms = _crypto_symbols()
    quote_cache: Dict[str, Optional[float]] = {}

    sells: List[Dict[str, Any]] = []
    net = 0.0
    for lot in lots:
        price_now = _price_now(lot["symbol"], crypto_syms, quote_cache)
        if price_now is None:
            continue  # no price, no counterfactual — skip, don't fabricate

        delta = (price_now - lot["sell_price"]) * lot["shares"]
        proceeds = lot["sell_price"] * lot["shares"]
        if proceeds <= 0:
            continue  # zero/penny proceeds — no band to grade against, skip
        band = _WASH_FRACTION * abs(proceeds)
        if delta == 0 or abs(delta) < band:
            verdict = "wash"
        elif delta > 0:
            verdict = "regret"   # it kept going up without you
        else:
            verdict = "relief"   # the sell dodged the drawdown

        net += delta
        sells.append({
            "symbol": lot["symbol"],
            "date": lot["date"],
            "shares": round(lot["shares"], 6),
            "sell_price": round(lot["sell_price"], 4),
            "price_now": round(price_now, 4),
            "delta_usd": round(delta, 2),
            "verdict": verdict,
        })

    if not sells:
        # Sells exist but none could be priced (feeds down, delisted tickers).
        return {"sells": [], "net_usd": 0, "n_sells": 0,
                "summary": "Sells are on record, but none could be priced "
                           "against today's market — no verdict for now."}

    net = round(net, 2)
    return {
        "sells": sells,
        "net_usd": net,
        "summary": _coach_summary(sells, net),
        "n_sells": len(sells),
        # The 200-row read is GLOBAL across all symbols, not per-symbol — for a
        # very active log, older sells can fall outside the window, so flag when
        # the cap was hit so callers know the verdict may be partial.
        "partial": len(txns) >= _TXN_LIMIT,
    }


# ─── 2. holding_discipline — how long do positions actually live ─────────────

def holding_discipline() -> Dict[str, Any]:
    """Average holding period and quick-flip count from the transaction log.

    Approximation (FIFO-ish, documented on purpose): per symbol, BUY rows are
    queued oldest-first and each SELL consumes shares from the front of the
    queue. Each SELL becomes one round trip whose holding period is the
    share-weighted age of the buy lots it consumed. Sold shares that exceed
    recorded buys (positions imported without their entry, transfers in) are
    ignored rather than guessed at, and fills aren't matched to intraday
    timestamps — day granularity only. Good enough for a temperament
    read-out; not a tax lot engine.

    Returns {"avg_hold_days": float|None, "quick_flips": int, "note": str}.
    avg_hold_days is share-weighted across all matched shares; quick_flips
    counts round trips of ≤ 21 days. Fails open.
    """
    try:
        txns = db.get_transactions(limit=500) or []
    except Exception as e:
        log.warning("transaction read failed: %s", e)
        return {"avg_hold_days": None, "quick_flips": 0,
                "note": "No transaction history available."}

    # Per-symbol, in chronological order. get_transactions returns newest
    # first; FIFO matching needs oldest first.
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for t in txns:
        sym = (t.get("symbol") or "").upper()
        if sym:
            by_symbol.setdefault(sym, []).append(t)

    weighted_days = 0.0   # Σ (shares × hold_days) over matched shares
    matched_shares = 0.0
    round_trips = 0
    quick_flips = 0

    for sym, rows in by_symbol.items():
        rows = sorted(rows, key=lambda t: str(t.get("date") or t.get("created_at") or ""))
        open_lots: List[Tuple[datetime, float]] = []  # (buy_day, shares remaining)

        for t in rows:
            action = (t.get("action") or "").upper()
            shares = _num(t.get("shares"))
            day = _parse_day(t.get("date") or t.get("created_at"))
            if not shares or shares <= 0 or day is None:
                continue

            if action == "BUY":
                open_lots.append((day, shares))
            elif action == "SELL":
                # Consume open buy lots front-to-back; the unmatched
                # remainder (no recorded entry) is dropped, per the docstring.
                remaining = shares
                trip_days = 0.0   # share-weighted days for THIS sell
                trip_shares = 0.0
                while remaining > 1e-9 and open_lots:
                    buy_day, lot_shares = open_lots[0]
                    take = min(lot_shares, remaining)
                    held = (day - buy_day).days
                    if held >= 0:  # future-dated buys are bad data, not lots
                        trip_days += take * held
                        trip_shares += take
                    if take >= lot_shares - 1e-9:
                        open_lots.pop(0)
                    else:
                        open_lots[0] = (buy_day, lot_shares - take)
                    remaining -= take

                if trip_shares > 0:
                    round_trips += 1
                    if (trip_days / trip_shares) <= _QUICK_FLIP_DAYS:
                        quick_flips += 1
                    weighted_days += trip_days
                    matched_shares += trip_shares

    if matched_shares <= 0:
        return {"avg_hold_days": None, "quick_flips": 0,
                "note": "No completed buy-to-sell round trips to measure yet."}

    avg = round(weighted_days / matched_shares, 1)
    note = ("Average hold of {} days across {} round trip{}; {} quick "
            "flip{} (in and out inside {} days).".format(
                avg, round_trips, "" if round_trips == 1 else "s",
                quick_flips, "" if quick_flips == 1 else "s",
                _QUICK_FLIP_DAYS))
    return {"avg_hold_days": avg, "quick_flips": quick_flips, "note": note}
