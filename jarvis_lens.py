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

import database as db
import fetcher

_REVIEW_TTL = 3600
_BRIEF_TTL = 1800
_TEMPERAMENT_TTL = 900

_QUICK_FLIP_DAYS = 21
_CHASE_PCT = 20.0          # buying >20% above your own prior avg cost
_FLIPS_FLAG = 2            # quick flips in 90d that earn a card


def _safe_pct(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # yfinance mixes 0.18 and 18.0 conventions; normalize ratios to percent
    return f * 100 if -1.5 < f < 1.5 else f


# ─── 1. Position review (Buffett lens) ───────────────────────────────────────

def _quality_score(f: Dict[str, Any]) -> Dict[str, Any]:
    """Score business quality 0-100 from returns, margins, leverage, growth,
    cash generation. Each check explains itself — the reasons ARE the value."""
    score = 50.0
    reasons: List[str] = []

    roe = _safe_pct(f.get("return_on_equity"))
    if roe is not None:
        if roe >= 20:
            score += 15; reasons.append("ROE {:.0f}% — excellent returns on owners' capital".format(roe))
        elif roe >= 12:
            score += 8; reasons.append("ROE {:.0f}% — solid returns".format(roe))
        elif roe < 6:
            score -= 12; reasons.append("ROE {:.0f}% — weak returns on equity".format(roe))

    om = _safe_pct(f.get("operating_margin"))
    if om is not None:
        if om >= 25:
            score += 10; reasons.append("operating margin {:.0f}% suggests pricing power".format(om))
        elif om < 8:
            score -= 8; reasons.append("thin operating margin ({:.0f}%)".format(om))

    de = f.get("debt_equity")
    try:
        de = float(de) if de is not None else None
    except (TypeError, ValueError):
        de = None
    if de is not None:
        de_ratio = de / 100 if de > 10 else de  # yfinance reports 154.5 meaning 1.545
        if de_ratio <= 0.5:
            score += 8; reasons.append("conservative balance sheet (D/E {:.1f})".format(de_ratio))
        elif de_ratio > 2.0:
            score -= 10; reasons.append("heavy leverage (D/E {:.1f})".format(de_ratio))

    fcf = f.get("free_cashflow")
    if isinstance(fcf, (int, float)):
        if fcf > 0:
            score += 7; reasons.append("generates free cash")
        else:
            score -= 10; reasons.append("burns cash (negative FCF)")

    rg = _safe_pct(f.get("revenue_growth"))
    if rg is not None:
        if rg >= 10:
            score += 6; reasons.append("revenue growing {:.0f}%".format(rg))
        elif rg < 0:
            score -= 8; reasons.append("revenue shrinking ({:.0f}%)".format(rg))

    score = max(0, min(100, score))
    verdict = "QUALITY" if score >= 70 else ("MIXED" if score >= 45 else "WEAK")
    return {"score": round(score), "verdict": verdict, "reasons": reasons}


def _valuation_flags(f: Dict[str, Any]) -> Dict[str, Any]:
    pe = f.get("pe_ratio")
    peg = f.get("peg_ratio")
    pb = f.get("pb_ratio")
    flags: List[str] = []
    tone = "unclear"
    try:
        pe = float(pe) if pe else None
        peg = float(peg) if peg else None
        pb = float(pb) if pb else None
    except (TypeError, ValueError):
        pe = peg = pb = None
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
    dy = _safe_pct(f.get("dividend_yield"))
    if dy:
        flags.append("pays {:.1f}% dividend".format(dy))
    return {"pe": pe, "peg": peg, "pb": pb, "tone": tone, "flags": flags}


def _ownership(symbol: str, price: Optional[float]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"held": False}
    try:
        for h in db.get_portfolio():
            if h["symbol"].upper() == symbol:
                out.update({
                    "held": True, "shares": h["shares"], "avg_cost": h["avg_cost"],
                    "unrealized_pct": round((price - h["avg_cost"]) / h["avg_cost"] * 100, 1)
                    if (price and h["avg_cost"]) else None,
                })
                break
        txns = db.get_transactions(symbol=symbol, limit=100)
        buys = [t for t in txns if (t.get("action") or "").upper() == "BUY"]
        if buys:
            first = min(t.get("date") or t.get("created_at", "") for t in buys)
            out["first_buy"] = first[:10]
            try:
                days = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(first[:10]).replace(tzinfo=timezone.utc)).days
                out["holding_days"] = max(0, days)
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
    f = {}
    try:
        f = fetcher.get_fundamentals(symbol) or {}
    except Exception as e:
        log.debug("fundamentals(%s) failed: %s", symbol, e)
    q = {}
    try:
        q = fetcher.get_quote(symbol) or {}
    except Exception:
        pass
    price = q.get("price")
    desc = (f.get("description") or "").strip()
    summary = desc.split(". ")[0][:280] + "." if desc else "No business description available."

    quality = _quality_score(f)
    valuation = _valuation_flags(f)
    own = _ownership(symbol, price)
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


def _temperament_uncached() -> Dict[str, Any]:
    try:
        txns = db.get_transactions(limit=500) or []
    except Exception:
        txns = []
    if not txns:
        return {"observations": [], "stats": {"n_transactions": 0},
                "verdict": "No transaction history yet — temperament unmeasured.",
                "as_of": datetime.now(timezone.utc).isoformat()}

    now = datetime.now()
    dated = [(t, _parse_day(t)) for t in txns]
    dated = [(t, d) for t, d in dated if d is not None]
    recent = [(t, d) for t, d in dated if (now - d).days <= 90]

    observations: List[Dict[str, str]] = []

    # Quick flips: BUY then SELL of the same symbol within N days.
    by_sym: Dict[str, List] = {}
    for t, d in sorted(dated, key=lambda td: td[1]):
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
                if held <= _QUICK_FLIP_DAYS:
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
            price = t.get("price") or 0
            sh = t.get("shares") or 0
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
        sym, p, avg = chases[-1]
        observations.append({
            "kind": "chasing", "tone": "warn",
            "text": "{} add{} made >20% above your own average cost (latest: {} at ${:,.2f} "
                    "vs ${:,.2f} basis). Averaging UP needs a stronger thesis than averaging down.".format(
                        len(chases), "s" if len(chases) != 1 else "", sym, p, avg)})

    # Churn trend: activity accelerating?
    n_recent = len(recent)
    monthly = n_recent / 3.0
    if monthly >= 8:
        observations.append({
            "kind": "churn", "tone": "warn",
            "text": "{:.0f} trades/month over the last quarter. Buffett: 'lethargy bordering "
                    "on sloth remains the cornerstone of our investment style.'".format(monthly)})
    elif monthly <= 2 and n_recent > 0:
        observations.append({
            "kind": "patience", "tone": "pos",
            "text": "~{:.0f} trade{}/month — patient pace. The portfolio is doing the work.".format(
                monthly, "s" if monthly != 1 else "")})

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

    regime = None
    try:
        regime = jarvis._market_regime()
    except Exception:
        pass

    sectors_top: List[Dict[str, Any]] = []
    sectors_bottom: List[Dict[str, Any]] = []
    try:
        sectors = fetcher.get_sector_performance() or []
        ranked = sorted([s for s in sectors if s.get("change_pct") is not None],
                        key=lambda s: -s["change_pct"])
        sectors_top = [{"sector": s.get("sector") or s.get("name"),
                        "change_pct": s["change_pct"]} for s in ranked[:3]]
        sectors_bottom = [{"sector": s.get("sector") or s.get("name"),
                           "change_pct": s["change_pct"]} for s in ranked[-3:]]
    except Exception:
        pass

    liquidity = None
    try:
        import liquidity_monitor
        ls = liquidity_monitor.compute_stress_score() or {}
        liquidity = {"score": ls.get("composite_score"), "regime": ls.get("regime")}
    except Exception:
        pass

    crypto = None
    try:
        cg = fetcher.get_crypto_global() or {}
        crypto = {"mcap": cg.get("total_market_cap_usd"),
                  "btc_dominance": cg.get("btc_dominance"),
                  "mcap_change_24h": cg.get("market_cap_change_24h")}
    except Exception:
        pass

    # Rule-based strategist paragraph — every clause guarded.
    bits: List[str] = []
    if regime:
        bits.append("Equities are in a {} vol regime (VIX {:.1f}), S&P {} on the day".format(
            (regime.get("regime") or "—").lower(), regime.get("vix") or 0,
            jarvis._fmt_pct(regime.get("spx_pct"))))
    if sectors_top and sectors_bottom:
        bits.append("rotation favors {} over {}".format(
            sectors_top[0]["sector"], sectors_bottom[-1]["sector"]))
    if liquidity and liquidity.get("regime"):
        bits.append("liquidity conditions read {} ({}/100)".format(
            str(liquidity["regime"]).lower(), liquidity.get("score")))
    if crypto and crypto.get("btc_dominance") is not None:
        chg = crypto.get("mcap_change_24h")
        bits.append("crypto risk appetite is {} (BTC dominance {:.0f}%{})".format(
            "expanding" if (chg or 0) > 0 else "contracting",
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
