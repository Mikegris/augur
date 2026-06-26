"""
jarvis_lens — investor-grade judgment layers for Jarvis.

Three engines modeled on how a great investor would actually use a copilot:

1. position_review(symbol)  — the Buffett lens. Not a price chart: what the
   BUSINESS is, whether it's a quality business (returns, margins, debt,
   cash generation), what you're paying for it (valuation flags), and your
   own relationship with it (basis, weight, holding period from the real
   transaction log). Ends with a margin-of-safety-flavored take.

2. temperament_check()      — the guardrail Buffett says matters more than
   IQ. Reads the user's actual transaction history for the classic
   self-defeating patterns: quick flips, chasing strength, churn creep.
   Phrased as observations, never as advice.

3. macro_brief()            — the Visser lens. One regime narrative fusing
   VIX regime, sector rotation, liquidity stress and crypto risk appetite,
   written like a strategist's Sunday note. Rule-based core; optional LLM
   polish rides jarvis's fail-open helpers.

Everything degrades gracefully and works keyless. Python 3.9 compatible.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("augur.jarvis_lens")

try:
    import cache_store
except Exception:  # pragma: no cover
    cache_store = None

try:
    import safe_executor
except Exception:  # pragma: no cover
    safe_executor = None

import database as db
import fetcher

_REVIEW_TTL = 3600
_BRIEF_TTL = 1800
_TEMPERAMENT_TTL = 900

_QUICK_FLIP_DAYS = 21
_CHASE_PCT = 20.0          # buying >20% above your own prior avg cost
_FLIPS_FLAG = 2            # quick flips in 90d that earn a card


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Reject nan/inf — they propagate into formatted strings as "nan"/"inf".
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _pct_ratio(v: Any) -> Optional[float]:
    """ROE / margins / growth → percent. yfinance gives a ratio (0.30, and
    for buyback-heavy names ROE can be 1.41 = 141%); Finviz gives percent
    (30). Multiply only when the magnitude is ratio-scaled (<2), so 1.6
    becomes 160% instead of being read as a weak 1.6%."""
    f = _num(v)
    if f is None:
        return None
    return f * 100 if -2.0 < f < 2.0 else f


def _pct_yield(v: Any) -> Optional[float]:
    """Dividend yield is already percent-form from both sources in the
    versions we run (yfinance 1.2.0: 0.37 == 0.37%; Finviz 'Dividend %':
    2.5). Never multiply — that was the '37% dividend' bug."""
    return _num(v)


def _debt_ratio(v: Any) -> Optional[float]:
    """Normalize debt/equity to a true ratio. yfinance reports percent-form
    (79.5 == 0.795, and 8.0 == 0.08 nearly debt-free); Finviz reports the
    ratio directly (0.79). Divide by 100 only for clearly percent-scaled
    values (>3) so a near-debt-free yfinance 8.0 isn't read as heavy
    leverage."""
    f = _num(v)
    if f is None:
        return None
    return f / 100 if f > 3 else f


# Back-compat alias: a couple of call sites still read margins generically.
_safe_pct = _pct_ratio


# ─── 1. Position review (Buffett lens) ───────────────────────────────────────

def _quality_score(f: Dict[str, Any]) -> Dict[str, Any]:
    """Score business quality 0-100 from returns, margins, leverage, growth,
    cash generation. Each check explains itself — the reasons ARE the value."""
    score = 50.0
    reasons: List[str] = []

    roe = _pct_ratio(f.get("return_on_equity"))
    if roe is not None:
        if roe >= 20:
            score += 15; reasons.append("ROE {:.0f}% — excellent returns on owners' capital".format(roe))
        elif roe >= 12:
            score += 8; reasons.append("ROE {:.0f}% — solid returns".format(roe))
        elif roe < 6:
            score -= 12; reasons.append("ROE {:.0f}% — weak returns on equity".format(roe))

    om = _pct_ratio(f.get("operating_margin"))
    if om is not None:
        if om >= 25:
            score += 10; reasons.append("operating margin {:.0f}% suggests pricing power".format(om))
        elif om < 8:
            score -= 8; reasons.append("thin operating margin ({:.0f}%)".format(om))

    de_ratio = _debt_ratio(f.get("debt_equity"))
    if de_ratio is not None:
        if de_ratio <= 0.5:
            score += 8; reasons.append("conservative balance sheet (D/E {:.2f})".format(de_ratio))
        elif de_ratio > 2.0:
            score -= 10; reasons.append("heavy leverage (D/E {:.1f})".format(de_ratio))

    burns_cash = False
    fcf = f.get("free_cashflow")
    if isinstance(fcf, (int, float)):
        if fcf > 0:
            score += 7; reasons.append("generates free cash")
        else:
            burns_cash = True
            score -= 10; reasons.append("burns cash (negative FCF)")

    rg = _pct_ratio(f.get("revenue_growth"))
    if rg is not None:
        if rg >= 10:
            score += 6; reasons.append("revenue growing {:.0f}%".format(rg))
        elif rg < 0:
            score -= 8; reasons.append("revenue shrinking ({:.0f}%)".format(rg))

    score = max(0, min(100, score))
    # A cash-burner is not a "genuinely good business" no matter how good the
    # other lines look — the Buffett lens is sold on cash generation. Cap the
    # verdict below QUALITY when free cash flow is negative.
    if burns_cash and score >= 70:
        score = 69
    verdict = "QUALITY" if score >= 70 else ("MIXED" if score >= 45 else "WEAK")
    return {"score": round(score), "verdict": verdict, "reasons": reasons}


def _valuation_flags(f: Dict[str, Any]) -> Dict[str, Any]:
    pe = f.get("pe_ratio")
    peg = f.get("peg_ratio")
    pb = f.get("pb_ratio")
    flags: List[str] = []
    tone = "unclear"
    # Parse each independently — one unparsable field ("N/A") must not blank
    # the other two.
    pe = _num(pe)
    peg = _num(peg)
    pb = _num(pb)
    if pe is not None:
        if pe < 0:
            tone = "speculative"; flags.append("negative earnings — you're paying for a story")
        elif pe <= 15 and (peg is None or peg <= 1.2):
            tone = "reasonable"; flags.append("P/E {:.0f} is in value territory".format(pe))
        elif pe >= 35 or (peg is not None and peg >= 2.5):
            tone = "expensive"; flags.append("P/E {:.0f}{} — priced for a lot to go right".format(
                pe, ", PEG {:.1f}".format(peg) if peg else ""))
        else:
            tone = "fair"; flags.append("P/E {:.0f} — neither bargain nor bubble".format(pe))
    dy = _pct_yield(f.get("dividend_yield"))
    if dy:
        flags.append("pays {:.2f}% dividend".format(dy))
    return {"pe": pe, "peg": peg, "pb": pb, "tone": tone, "flags": flags}


def _held_record(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        for h in db.get_portfolio():
            if h["symbol"].upper() == symbol:
                return h
    except Exception:
        pass
    return None


def _ownership(symbol: str, price: Optional[float], held: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"held": False}
    try:
        h = held if held is not None else _held_record(symbol)
        if h is not None:
            avg_cost = h.get("avg_cost")
            out.update({
                "held": True, "shares": h.get("shares"), "avg_cost": avg_cost,
                "unrealized_pct": round((price - avg_cost) / avg_cost * 100, 1)
                if (price and avg_cost) else None,
            })
        txns = db.get_transactions(symbol=symbol, limit=100)
        buys = [t for t in txns if (t.get("action") or "").upper() == "BUY"]
        if buys:
            # Coerce to string and drop falsy/None so a BUY row with a null
            # date/created_at can't make min() compare None to str (TypeError),
            # which would otherwise blank the whole ownership block.
            dates = [str(t.get("date") or t.get("created_at") or "") for t in buys]
            dates = [d for d in dates if d]
            first = min(dates) if dates else None
        if buys and first:
            out["first_buy"] = first[:10]
            try:
                days = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(first[:10]).replace(tzinfo=timezone.utc)).days
                # A future-dated buy (clock skew / bad import) yields negative
                # days. Don't clamp to 0 — that falsely reads as "owned 0 days,
                # too early to judge". Leave holding_days unset on bad data.
                if days >= 0:
                    out["holding_days"] = days
            except Exception:
                pass
        out["n_transactions"] = len(txns)
    except Exception as e:
        log.debug("ownership(%s) failed: %s", symbol, e)
    return out


def _buffett_take(quality: Dict[str, Any], valuation: Dict[str, Any],
                  own: Dict[str, Any], name: str) -> str:
    bits: List[str] = []
    if quality["verdict"] == "QUALITY":
        bits.append("{} looks like a genuinely good business".format(name))
    elif quality["verdict"] == "MIXED":
        bits.append("{} is a decent-but-not-great business".format(name))
    else:
        bits.append("{} doesn't screen as a high-quality business".format(name))
    tone = valuation.get("tone")
    if tone == "reasonable":
        bits.append("and the price asks little of it — that's the margin-of-safety setup")
    elif tone == "expensive":
        bits.append("but the price already assumes the rosy version of the future — "
                    "the margin of safety is in the buyer's imagination")
    elif tone == "speculative":
        bits.append("and without earnings, any price is a guess about a story")
    elif tone == "fair":
        bits.append("at a price that's fair rather than generous")
    # First two bits form ONE sentence ("…good business, but the price…");
    # anything after stands alone.
    sentence = ", ".join(bits[:2]) if len(bits) >= 2 else bits[0]
    sentences = [sentence[0].upper() + sentence[1:]]
    if own.get("held") and own.get("holding_days") is not None:
        d = own["holding_days"]
        if d > 365:
            sentences.append("You've owned it {} years — the holding period is doing its work".format(
                round(d / 365, 1)))
        elif d < 60:
            sentences.append("You've owned it {} days — too early for the thesis to have proven anything".format(d))
    return ". ".join(sentences) + "."


def _position_review_uncached(symbol: str) -> Dict[str, Any]:
    held = _held_record(symbol)
    is_crypto = bool(held and held.get("asset_type") == "crypto")
    # Crypto holdings must be priced off SYM-USD, not the same-ticker equity
    # (an ETF or unrelated stock) — otherwise BTC reviews as a trust at -100%.
    quote_sym = symbol + "-USD" if is_crypto else symbol

    # Fundamentals and the quote hit independent upstreams; for an equity we
    # need both, so fetch them concurrently instead of one-then-the-other.
    # parallel_map returns results in input order with None for any thunk that
    # raised — same guarded contract the serial try/excepts gave (a dead feed
    # leaves that dict empty, never crashes the review). Crypto skips
    # fundamentals entirely, so the parallel hop earns nothing there.
    f = {}
    q = {}
    if not is_crypto and safe_executor is not None:
        f_res, q_res = safe_executor.parallel_map(
            lambda thunk: thunk(),
            [lambda: fetcher.get_fundamentals(symbol),
             lambda: fetcher.get_quote(quote_sym)],
            max_workers=2, thread_name_prefix="lens-review")
        f = f_res or {}
        q = q_res or {}
    else:
        if not is_crypto:
            try:
                f = fetcher.get_fundamentals(symbol) or {}
            except Exception as e:
                log.debug("fundamentals(%s) failed: %s", symbol, e)
        try:
            q = fetcher.get_quote(quote_sym) or {}
        except Exception:
            pass
    price = q.get("price")
    own = _ownership(symbol, price, held=held)

    # A crypto asset isn't a "business" — give it an honest, non-equity take.
    if is_crypto:
        return {
            "symbol": symbol, "is_crypto": True,
            "business": {"name": q.get("name") or symbol, "sector": "Crypto",
                         "industry": None,
                         "summary": "{} is a crypto asset — no business fundamentals to "
                                    "review. Judge it on adoption, liquidity and your own "
                                    "risk tolerance, not earnings.".format(symbol)},
            "quality": {"score": None, "verdict": "N/A", "reasons": []},
            "valuation": {"tone": "n/a", "flags": []},
            "ownership": own, "price": price,
            "take": "{} is a crypto position, not a business — the Buffett lens doesn't "
                    "apply. Size it like the volatile asset it is.".format(symbol),
            "as_of": datetime.now(timezone.utc).isoformat(),
        }

    # No fundamentals at all (bad ticker, dead feed): don't fabricate a verdict.
    metric_keys = ("pe_ratio", "return_on_equity", "operating_margin", "market_cap",
                   "revenue", "free_cashflow")
    has_data = bool(price) or any(f.get(k) is not None for k in metric_keys)
    if not has_data:
        return {
            "symbol": symbol, "no_data": True,
            "business": {"name": f.get("name") or symbol, "sector": None,
                         "industry": None,
                         "summary": "No data available for {} — it may be an invalid "
                                    "ticker or the data feed is down.".format(symbol)},
            "quality": {"score": None, "verdict": "NO DATA", "reasons": []},
            "valuation": {"tone": "unclear", "flags": []},
            "ownership": own, "price": price,
            "take": "I couldn't pull anything on {} — can't review what I can't see.".format(symbol),
            "as_of": datetime.now(timezone.utc).isoformat(),
        }

    desc = (f.get("description") or "").strip()
    if desc:
        first = desc.split(". ")[0]
        if len(first) > 280:
            # Truncate at a word boundary with an ellipsis, not mid-token.
            first = first[:280].rsplit(" ", 1)[0] + "…"
        else:
            first = first + "."
        summary = first
    else:
        summary = "No business description available."
    quality = _quality_score(f)
    valuation = _valuation_flags(f)
    name = f.get("name") or symbol

    return {
        "symbol": symbol,
        "business": {"name": name, "sector": f.get("sector"),
                     "industry": f.get("industry"), "summary": summary},
        "quality": quality,
        "valuation": valuation,
        "ownership": own,
        "price": price,
        "take": _buffett_take(quality, valuation, own, name),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def position_review(symbol: str) -> Dict[str, Any]:
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"error": "symbol required"}
    if cache_store is None:
        return _position_review_uncached(symbol)
    return cache_store.coalesce(("lens_review", symbol), _REVIEW_TTL,
                                lambda: _position_review_uncached(symbol))


# ─── 2. Temperament check (the behavioral guardrail) ─────────────────────────

def _parse_day(t: Dict[str, Any]) -> Optional[datetime]:
    raw = (t.get("date") or t.get("created_at") or "")[:10]
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _parse_ts(t: Dict[str, Any]) -> Optional[datetime]:
    """Full timestamp for ordering. created_at carries time-of-day, which is
    what disambiguates same-DAY round trips (day-trades) — sorting by day
    alone left SELL before BUY and the flip pairing never matched."""
    raw = (t.get("created_at") or t.get("date") or "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except Exception:
            continue
    return None


def _temperament_uncached() -> Dict[str, Any]:
    try:
        txns = db.get_transactions(limit=500) or []
    except Exception:
        txns = []
    if not txns:
        return {"observations": [], "stats": {"n_transactions": 0},
                "verdict": "No transaction history yet — temperament unmeasured.",
                "as_of": datetime.now(timezone.utc).isoformat()}

    # Naive local "now" to match the naive transaction date parsing here
    # (_ownership uses UTC against UTC-tagged dates — different lens, same
    # day-count intent; this keeps the temperament window internally
    # consistent rather than mixing a tz-aware now with naive dates).
    now = datetime.now()
    # Order by full timestamp so same-day BUY→SELL pairs resolve correctly.
    dated = [(t, _parse_day(t), _parse_ts(t)) for t in txns]
    dated = [(t, d, ts) for t, d, ts in dated if d is not None]
    # Sort by DAY first, then full timestamp as tiebreak. Sorting on the
    # timestamp alone let rows with a missing ts (which fall back to the day,
    # i.e. midnight) jump ahead of same-day rows that DO carry a time, and
    # ties preserved input order — which arrives newest-first — reversing the
    # intended chronology. (day, ts) keeps days monotonic and only uses the
    # time to order within a day.
    dated.sort(key=lambda x: (x[1], x[2] or x[1]))
    recent_count = sum(1 for t, d, ts in dated if (now - d).days <= 90)

    observations: List[Dict[str, str]] = []

    # Quick flips: BUY then SELL of the same symbol within N days. Counted
    # over the last 90 days only — the card says "recently", so it must be.
    by_sym: Dict[str, List] = {}
    for t, d, ts in dated:
        by_sym.setdefault(t["symbol"].upper(), []).append((t, d))
    flips = []
    for sym, rows in by_sym.items():
        last_buy = None
        for t, d in rows:
            act = (t.get("action") or "").upper()
            if act == "BUY":
                last_buy = d
            elif act == "SELL" and last_buy is not None:
                held = (d - last_buy).days
                if held <= _QUICK_FLIP_DAYS and (now - d).days <= 90:
                    flips.append((sym, held))
                last_buy = None
    if len(flips) >= _FLIPS_FLAG:
        worst = min(flips, key=lambda x: x[1])
        observations.append({
            "kind": "quick_flips", "tone": "warn",
            "text": "{} round-trips inside {} days recently (fastest: {} in {}d). "
                    "Trading costs and taxes compound too — against you.".format(
                        len(flips), _QUICK_FLIP_DAYS, worst[0], worst[1])})

    # Chasing: buying meaningfully above your own established average cost.
    chases = []
    for sym, rows in by_sym.items():
        cost_shares = 0.0
        cost_total = 0.0
        for t, d in rows:
            act = (t.get("action") or "").upper()
            # Some import paths store price/shares as text; coerce defensively
            # so a single string row can't raise TypeError and abort the whole
            # temperament check (this loop is not wrapped in try/except).
            price = _num(t.get("price"))
            sh = _num(t.get("shares"))
            if price is None or sh is None:
                continue
            if act == "BUY":
                if cost_shares > 0 and price > (cost_total / cost_shares) * (1 + _CHASE_PCT / 100):
                    chases.append((sym, price, cost_total / cost_shares))
                cost_total += price * sh
                cost_shares += sh
            elif act == "SELL" and cost_shares > 0:
                avg = cost_total / cost_shares
                cost_shares = max(0.0, cost_shares - sh)
                cost_total = avg * cost_shares
    if chases:
        # Surface the WORST chase (largest premium over basis), not just the
        # most recent — that's the one worth seeing, like quick_flips does.
        sym, p, avg = max(chases, key=lambda c: (c[1] - c[2]) / c[2] if c[2] else 0)
        observations.append({
            "kind": "chasing", "tone": "warn",
            "text": "{} add{} made >20% above your own average cost (worst: {} at ${:,.2f} "
                    "vs ${:,.2f} basis). Averaging UP needs a stronger thesis than averaging down.".format(
                        len(chases), "s" if len(chases) != 1 else "", sym, p, avg)})

    # Churn trend: activity accelerating?
    n_recent = recent_count
    monthly = n_recent / 3.0
    if monthly >= 8:
        observations.append({
            "kind": "churn", "tone": "warn",
            "text": "{:.0f} trades/month over the last quarter. Buffett: 'lethargy bordering "
                    "on sloth remains the cornerstone of our investment style.'".format(monthly)})
    elif monthly <= 2 and n_recent > 0:
        # Round first, then pluralize on the rounded display value so the
        # grammar matches what's shown ("~1 trade", not "~1 trades").
        shown = round(monthly)
        observations.append({
            "kind": "patience", "tone": "pos",
            "text": "~{} trade{}/month — patient pace. The portfolio is doing the work.".format(
                shown, "s" if shown != 1 else "")})

    if not observations:
        observations.append({"kind": "clean", "tone": "pos",
                             "text": "No self-defeating patterns in the recent record."})

    warn = sum(1 for o in observations if o["tone"] == "warn")
    verdict = ("Temperament check: {} flag{} worth a look.".format(warn, "s" if warn != 1 else "")
               if warn else "Temperament check: clean. Keep sitting on your hands.")
    return {
        "observations": observations,
        "stats": {"n_transactions": len(txns), "last_90d": n_recent,
                  "per_month_90d": round(monthly, 1), "quick_flips": len(flips)},
        "verdict": verdict,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


def temperament_check() -> Dict[str, Any]:
    if cache_store is None:
        return _temperament_uncached()
    return cache_store.coalesce(("lens_temperament",), _TEMPERAMENT_TTL,
                                _temperament_uncached)


# ─── 3. Macro brief (Visser lens) ────────────────────────────────────────────

def _macro_brief_uncached() -> Dict[str, Any]:
    import jarvis  # late import to avoid cycles

    # The four reads (regime, sectors, liquidity, crypto) hit independent
    # upstreams, so run them concurrently. Each thunk keeps its own guard and
    # returns the exact value the serial block produced (or its empty default
    # on failure), so parallelism changes timing only, never the output.
    def _regime_thunk():
        try:
            return jarvis._market_regime()
        except Exception:
            return None

    def _sectors_thunk():
        try:
            sectors = fetcher.get_sector_performance() or []
            ranked = sorted([s for s in sectors if s.get("change_pct") is not None],
                            key=lambda s: -s["change_pct"])
            # Disjoint top/bottom slices — with <6 sectors reporting, ranked[:3]
            # and ranked[-3:] would overlap and produce "X over X".
            half = len(ranked) // 2
            # With a single sector reporting, still surface it as leading
            # (top_n=1) so a one-sector feed isn't dropped entirely. The bottom
            # slice is only populated when there are ≥2 to keep top/bottom
            # disjoint.
            top_n = min(3, max(1, half)) if len(ranked) >= 1 else 0
            bottom_n = top_n if len(ranked) >= 2 else 0
            top = [{"sector": s.get("sector") or s.get("name"),
                    "change_pct": s["change_pct"]} for s in ranked[:top_n]]
            bottom = [{"sector": s.get("sector") or s.get("name"),
                       "change_pct": s["change_pct"]} for s in ranked[len(ranked) - bottom_n:]] if bottom_n else []
            return (top, bottom)
        except Exception:
            return None

    def _liquidity_thunk():
        try:
            import liquidity_monitor
            ls = liquidity_monitor.compute_stress_score() or {}
            return {"score": ls.get("composite_score"), "regime": ls.get("regime")}
        except Exception:
            return None

    def _crypto_thunk():
        try:
            cg = fetcher.get_crypto_global() or {}
            return {"mcap": cg.get("total_market_cap_usd"),
                    "btc_dominance": cg.get("btc_dominance"),
                    "mcap_change_24h": cg.get("market_cap_change_24h")}
        except Exception:
            return None

    thunks = [_regime_thunk, _sectors_thunk, _liquidity_thunk, _crypto_thunk]
    if safe_executor is not None:
        regime, sectors_res, liquidity, crypto = safe_executor.parallel_map(
            lambda thunk: thunk(), thunks, max_workers=len(thunks),
            thread_name_prefix="lens-macro")
    else:  # fail-open: serial with the same per-thunk guards
        regime, sectors_res, liquidity, crypto = (t() for t in thunks)

    sectors_top: List[Dict[str, Any]] = []
    sectors_bottom: List[Dict[str, Any]] = []
    if sectors_res:
        sectors_top, sectors_bottom = sectors_res

    # Rule-based strategist paragraph — every clause guarded.
    bits: List[str] = []
    if regime and regime.get("vix") is not None and regime.get("regime"):
        bits.append("Equities are in a {} vol regime (VIX {:.1f}), S&P {} on the day".format(
            regime["regime"].lower(), regime["vix"],
            jarvis._fmt_pct(regime.get("spx_pct"))))
    elif regime and regime.get("spx_pct") is not None:
        bits.append("S&P {} on the day".format(jarvis._fmt_pct(regime["spx_pct"])))
    if (sectors_top and sectors_bottom
            and sectors_top[0]["sector"] != sectors_bottom[-1]["sector"]):
        bits.append("rotation favors {} over {}".format(
            sectors_top[0]["sector"], sectors_bottom[-1]["sector"]))
    if liquidity and liquidity.get("regime"):
        score = liquidity.get("score")
        if score is not None:
            bits.append("liquidity conditions read {} ({:.0f}/100)".format(
                str(liquidity["regime"]).lower(), score))
        else:
            bits.append("liquidity conditions read {}".format(
                str(liquidity["regime"]).lower()))
    if crypto and crypto.get("btc_dominance") is not None:
        chg = crypto.get("mcap_change_24h")
        # Only assert a direction when we actually have a 24h change; with
        # dominance present but mcap_change missing, say "mixed" rather than
        # falsely asserting "contracting" with no evidence.
        appetite = ("expanding" if chg and chg > 0
                    else ("contracting" if chg is not None and chg < 0 else "mixed"))
        bits.append("crypto risk appetite is {} (BTC dominance {:.0f}%{})".format(
            appetite,
            crypto["btc_dominance"],
            ", mcap {:+.1f}%/24h".format(chg) if chg is not None else ""))
    narrative = ("; ".join(bits) + "." if bits
                 else "Macro feeds are quiet — no regime read available right now.")
    narrative = narrative[0].upper() + narrative[1:]

    out = {
        "regime": regime,
        "sectors": {"leading": sectors_top, "lagging": sectors_bottom},
        "liquidity": liquidity,
        "crypto": crypto,
        "narrative": narrative,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }

    # Optional strategist voice — fail-open like every other LLM touch.
    try:
        import jarvis as _j
        if _j._llm_available():
            polished = _j._llm_complete(
                [{"role": "system", "content":
                  "You are a macro strategist writing the opening of a Sunday client "
                  "note. Rewrite this regime read in 3-4 confident sentences connecting "
                  "the dots. Keep every number exactly. No advice."},
                 {"role": "user", "content": narrative}],
                max_tokens=180, temperature=0.5)
            if polished:
                out["voice"] = polished
    except Exception:
        pass
    return out


def macro_brief() -> Dict[str, Any]:
    if cache_store is None:
        return _macro_brief_uncached()
    return cache_store.coalesce(("lens_macro_brief",), _BRIEF_TTL,
                                _macro_brief_uncached)
