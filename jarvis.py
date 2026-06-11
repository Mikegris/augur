"""
jarvis — TERMINUS's unified assistant layer.

The app ships ~40 views and ~30 analysis engines, but the user has to know
where to look. This module is the orchestration layer that makes the app
feel like a personal analyst:

1. `get_briefing()` — a prioritized daily briefing: portfolio pulse,
   triggered/near alerts, upcoming earnings, market regime, notable holding
   moves, concentration risk, and fresh ideas from the warmed pool. Every
   section degrades gracefully — a dead upstream drops the section, never
   the briefing.

2. `ask(query)` — a local natural-language Q&A endpoint. A rule-based
   intent parser routes questions ("how's my portfolio", "forecast NVDA",
   "when does AAPL report") to the existing engines and phrases a direct
   answer plus a deep-link action the frontend can navigate to. No API key
   required; everything runs on local data.

Insight cards: {priority (1=urgent..3=fyi), kind, tone (pos|neg|warn|info),
title, detail, action: {view, symbol?}}.

Python 3.9 compatible (no PEP 604 unions).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

log = logging.getLogger("augur.jarvis")

try:
    import cache_store
except Exception:  # pragma: no cover
    cache_store = None

import database as db
import fetcher

_BRIEFING_TTL = 240  # seconds — underlying quotes cache for 30-120s anyway

# VIX regime bands (CBOE convention-ish)
_VIX_REGIMES = [
    (15, "CALM", "Volatility is low — markets are complacent."),
    (20, "NORMAL", "Volatility is in its normal range."),
    (28, "ELEVATED", "Volatility is elevated — expect wider swings."),
    (999, "STRESSED", "Volatility is in stress territory — risk-off conditions."),
]

_NEAR_ALERT_PCT = 2.0   # within 2% of an alert level counts as "approaching"
_BIG_MOVE_PCT = 3.0     # holding day-move that earns its own insight card
_CONCENTRATION_PCT = 35.0  # single position above this % of portfolio


# ─── helpers ─────────────────────────────────────────────────────────────────

def _et_now() -> datetime:
    """Eastern-time now without external tz deps (UTC-5/-4 close enough for
    greeting + market-phase wording; exact DST handled via stdlib check)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc) - timedelta(hours=5)


def _market_phase(now_et: datetime) -> str:
    if now_et.weekday() >= 5:
        return "closed for the weekend"
    mins = now_et.hour * 60 + now_et.minute
    if 240 <= mins < 570:
        return "in pre-market"
    if 570 <= mins < 960:
        return "open"
    if 960 <= mins < 1200:
        return "in after-hours"
    return "closed"


def _greeting(now_local: Optional[datetime] = None) -> str:
    h = (now_local or datetime.now()).hour
    if h < 5:
        return "Burning the midnight oil"
    if h < 12:
        return "Good morning"
    if h < 17:
        return "Good afternoon"
    return "Good evening"


def _card(priority: int, kind: str, tone: str, title: str, detail: str,
          view: Optional[str] = None, symbol: Optional[str] = None) -> Dict[str, Any]:
    action: Optional[Dict[str, Any]] = None
    if view:
        action = {"view": view}
        if symbol:
            action["symbol"] = symbol
    return {"priority": priority, "kind": kind, "tone": tone,
            "title": title, "detail": detail, "action": action}


def _fmt_usd(v: Optional[float]) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1e6:
        return "{}${:,.2f}M".format(sign, v / 1e6)
    return "{}${:,.0f}".format(sign, v)


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return "{}{:.2f}%".format("+" if v >= 0 else "", v)


# ─── briefing sections (each fully guarded) ──────────────────────────────────

def _portfolio_pulse() -> Optional[Dict[str, Any]]:
    """Live-priced portfolio summary + per-holding day moves."""
    holdings = db.get_portfolio()
    if not holdings:
        return None

    stock_syms = [h["symbol"] for h in holdings if h.get("asset_type") != "crypto"]
    crypto_syms = [h["symbol"] for h in holdings if h.get("asset_type") == "crypto"]
    prices: Dict[str, Dict[str, Any]] = {}
    if stock_syms:
        prices.update(fetcher.get_quotes_batch(stock_syms))
    if crypto_syms:
        cq = fetcher.get_quotes_batch([s + "-USD" for s in crypto_syms])
        for sym in crypto_syms:
            q = cq.get((sym + "-USD").upper())
            if q:
                prices[sym] = q

    total_value = 0.0
    total_cost = 0.0
    day_pnl = 0.0
    day_pnl_known = False
    rows: List[Dict[str, Any]] = []
    for h in holdings:
        q = prices.get(h["symbol"], {})
        price = q.get("price")
        if not price:
            continue
        mv = price * h["shares"]
        cost = h["avg_cost"] * h["shares"]
        total_value += mv
        total_cost += cost
        chg = q.get("change")
        if chg is not None:
            day_pnl += chg * h["shares"]
            day_pnl_known = True
        rows.append({
            "symbol": h["symbol"],
            "market_value": round(mv, 2),
            "unrealized_pnl": round(mv - cost, 2),
            "unrealized_pct": round((mv - cost) / cost * 100, 2) if cost else 0,
            "day_change_pct": q.get("change_pct"),
            "day_pnl": round(chg * h["shares"], 2) if chg is not None else None,
        })

    if not rows:
        return None
    for r in rows:
        r["weight_pct"] = round(r["market_value"] / total_value * 100, 2) if total_value else 0

    movers = [r for r in rows if r.get("day_change_pct") is not None]
    movers.sort(key=lambda r: r["day_change_pct"])
    total_pnl = total_value - total_cost
    prev_value = total_value - day_pnl
    return {
        "total_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / total_cost * 100, 2) if total_cost else 0,
        "day_pnl": round(day_pnl, 2) if day_pnl_known else None,
        "day_pnl_pct": round(day_pnl / prev_value * 100, 2) if (day_pnl_known and prev_value) else None,
        "num_positions": len(rows),
        "holdings": rows,
        "best": movers[-1] if movers else None,
        "worst": movers[0] if movers else None,
    }


def _market_regime() -> Optional[Dict[str, Any]]:
    indices = fetcher.get_market_indices()
    if not indices:
        return None
    by_sym = {i.get("symbol"): i for i in indices}
    spx = by_sym.get("^GSPC") or {}
    ndx = by_sym.get("^NDX") or {}
    vix = by_sym.get("^VIX") or {}
    regime_label = None
    regime_note = None
    vix_level = vix.get("price")
    if vix_level is not None:
        for bound, label, note in _VIX_REGIMES:
            if vix_level < bound:
                regime_label, regime_note = label, note
                break
    return {
        "spx_pct": spx.get("change_pct"),
        "ndx_pct": ndx.get("change_pct"),
        "vix": vix_level,
        "vix_pct": vix.get("change_pct"),
        "regime": regime_label,
        "regime_note": regime_note,
    }


def _alert_insights(quotes_hint: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    alerts = db.get_price_alerts(include_triggered=True)
    if not alerts:
        return cards

    triggered = [a for a in alerts if a.get("triggered")]
    active = [a for a in alerts if not a.get("triggered")]
    for a in triggered[:5]:
        cards.append(_card(
            1, "alert", "warn",
            "{} alert triggered".format(a["symbol"]),
            "{} crossed {} ${:,.2f}.".format(
                a["symbol"], "above" if a.get("alert_type") == "above" else "below",
                a.get("price") or 0),
            view="alerts"))

    # Near-trigger detection on active alerts (quotes are cheap: 30s cache)
    if active:
        syms = sorted({a["symbol"] for a in active})[:25]
        quotes = quotes_hint or {}
        missing = [s for s in syms if s not in quotes]
        if missing:
            try:
                quotes = dict(quotes)
                quotes.update(fetcher.get_quotes_batch(missing))
            except Exception:
                pass
        for a in active:
            q = quotes.get(a["symbol"]) or {}
            price = q.get("price")
            level = a.get("price")
            if not price or not level:
                continue
            dist_pct = (level - price) / price * 100
            near = (a.get("alert_type") == "above" and 0 < dist_pct <= _NEAR_ALERT_PCT) or \
                   (a.get("alert_type") == "below" and 0 < -dist_pct <= _NEAR_ALERT_PCT)
            if near:
                cards.append(_card(
                    2, "alert_near", "info",
                    "{} approaching alert level".format(a["symbol"]),
                    "{} is at ${:,.2f}, within {:.1f}% of your ${:,.2f} {} alert.".format(
                        a["symbol"], price, abs(dist_pct), level, a.get("alert_type", "")),
                    view="alerts"))
    return cards


def _earnings_insights(symbols: List[str]) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    if not symbols:
        return cards
    import earnings
    cal = earnings.get_earnings_calendar(symbols[:25])
    for e in cal:
        days = e.get("days_until")
        if days is None or days > 7:
            continue
        when = "today" if days == 0 else ("tomorrow" if days == 1 else "in {} days".format(days))
        extra = ""
        if e.get("beat_rate") is not None:
            extra = " Historical beat rate: {}%.".format(e["beat_rate"])
        cards.append(_card(
            2 if days <= 2 else 3, "earnings", "info",
            "{} reports earnings {}".format(e["symbol"], when),
            "Next earnings date {}.{}".format(e.get("earnings_date", "—"), extra),
            view="earnings", symbol=e["symbol"]))
    return cards


def _idea_insights() -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    try:
        import idea_pool_warmer
        pool = idea_pool_warmer.list_warmed_symbols()
    except Exception:
        return cards
    top = [r for r in pool if r.get("composite_score") is not None][:3]
    if not top:
        return cards
    names = ", ".join("{} ({:.0f})".format(r["symbol"], r["composite_score"]) for r in top)
    cards.append(_card(
        3, "ideas", "pos",
        "{} fresh ideas ready in the pool".format(len(pool)),
        "Top-scored right now: {}.".format(names),
        view="ideas"))
    return cards


def _build_headline(pulse: Optional[Dict[str, Any]],
                    regime: Optional[Dict[str, Any]],
                    n_urgent: int) -> str:
    bits: List[str] = []
    if pulse:
        if pulse.get("day_pnl") is not None:
            bits.append("Portfolio {} {} ({}) today at {}".format(
                "up" if pulse["day_pnl"] >= 0 else "down",
                _fmt_usd(abs(pulse["day_pnl"])),
                _fmt_pct(pulse.get("day_pnl_pct")),
                _fmt_usd(pulse["total_value"])))
        else:
            bits.append("Portfolio at {}".format(_fmt_usd(pulse["total_value"])))
    if regime and regime.get("vix") is not None:
        bits.append("VIX {:.1f} ({})".format(regime["vix"], regime.get("regime") or "—"))
    if n_urgent:
        bits.append("{} item{} need{} your attention".format(
            n_urgent, "s" if n_urgent != 1 else "", "" if n_urgent != 1 else "s"))
    return ". ".join(bits) + "." if bits else "All quiet. No portfolio, alerts, or market flags right now."


def _run_briefing_uncached() -> Dict[str, Any]:
    now_et = _et_now()
    cards: List[Dict[str, Any]] = []

    try:
        pulse = _portfolio_pulse()
    except Exception as e:
        log.warning("briefing: portfolio pulse failed: %s", e)
        pulse = None

    try:
        regime = _market_regime()
    except Exception as e:
        log.warning("briefing: market regime failed: %s", e)
        regime = None

    # Cards from each section, all guarded
    try:
        cards.extend(_alert_insights())
    except Exception as e:
        log.warning("briefing: alerts failed: %s", e)

    if pulse:
        # Notable single-position day moves
        for r in pulse["holdings"]:
            cp = r.get("day_change_pct")
            if cp is None or abs(cp) < _BIG_MOVE_PCT:
                continue
            tone = "pos" if cp > 0 else "neg"
            cards.append(_card(
                2, "mover", tone,
                "{} {} {:.2f}% today".format(r["symbol"], "up" if cp > 0 else "down", abs(cp)),
                "{} moved {} — {} day P&L on your {} position.".format(
                    r["symbol"], _fmt_pct(cp), _fmt_usd(r.get("day_pnl")),
                    _fmt_usd(r["market_value"])),
                view="research", symbol=r["symbol"]))
        # Concentration risk
        for r in pulse["holdings"]:
            if r.get("weight_pct", 0) >= _CONCENTRATION_PCT:
                cards.append(_card(
                    2, "concentration", "warn",
                    "{} is {:.0f}% of your portfolio".format(r["symbol"], r["weight_pct"]),
                    "Single-position concentration above {:.0f}% amplifies drawdowns. "
                    "Consider the optimizer or a stress test.".format(_CONCENTRATION_PCT),
                    view="stress"))

        try:
            held = [h["symbol"] for h in db.get_portfolio() if h.get("asset_type") != "crypto"]
            cards.extend(_earnings_insights(held))
        except Exception as e:
            log.warning("briefing: earnings failed: %s", e)

    if regime and regime.get("regime") in ("ELEVATED", "STRESSED"):
        cards.append(_card(
            1 if regime["regime"] == "STRESSED" else 2, "regime", "warn",
            "Volatility regime: {}".format(regime["regime"]),
            regime.get("regime_note") or "",
            view="macro"))

    try:
        cards.extend(_idea_insights())
    except Exception as e:
        log.debug("briefing: ideas failed: %s", e)

    cards.sort(key=lambda c: c["priority"])
    n_urgent = sum(1 for c in cards if c["priority"] == 1)

    return {
        "greeting": "{}. Markets are {}.".format(_greeting(), _market_phase(now_et)),
        "headline": _build_headline(pulse, regime, n_urgent),
        "insights": cards[:12],
        "portfolio": pulse and {k: v for k, v in pulse.items() if k != "holdings"},
        "market": regime,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_briefing(force_refresh: bool = False) -> Dict[str, Any]:
    if cache_store is None or force_refresh:
        return _run_briefing_uncached()
    return cache_store.coalesce(("jarvis_briefing",), _BRIEFING_TTL, _run_briefing_uncached)


# ─── ask: local natural-language Q&A ─────────────────────────────────────────

_STOPWORDS = {
    "A", "I", "IS", "IT", "MY", "THE", "OF", "TO", "IN", "ON", "UP", "DO", "ME",
    "HOW", "WHAT", "WHEN", "WILL", "AND", "OR", "FOR", "ARE", "AM", "BE", "GO",
    "VS", "ETF", "USD", "CEO", "AI", "PE", "EPS", "YTD", "ALL", "ANY", "NOW",
    "BUY", "SELL", "HOLD", "WHY", "WHO", "CAN", "GET", "OUT", "WAS", "HAS",
    "DAY", "NEW", "TOP", "BIG", "LOW", "OFF", "SO", "AT", "IF",
}


def _known_symbols() -> set:
    syms = set()
    try:
        syms.update(h["symbol"].upper() for h in db.get_portfolio())
    except Exception:
        pass
    try:
        syms.update(w["symbol"].upper() for w in db.get_watchlist())
    except Exception:
        pass
    return syms


def _extract_symbol(query: str) -> Optional[str]:
    """$SYM beats known portfolio/watchlist symbols beats bare ALL-CAPS token."""
    m = re.search(r"\$([A-Za-z][A-Za-z.\-]{0,9})", query)
    if m:
        return m.group(1).upper()
    tokens = re.findall(r"\b[A-Za-z][A-Za-z.\-]{0,9}\b", query)
    upper = [t.upper() for t in tokens]
    known = _known_symbols()
    for t in upper:
        if t in known:
            return t
    # bare all-caps token the user typed in caps (intentional ticker)
    for t in tokens:
        if t.isupper() and 1 <= len(t) <= 5 and t not in _STOPWORDS:
            return t
    return None


def _answer_quote(symbol: str) -> Dict[str, Any]:
    q = fetcher.get_quote(symbol)
    if not q or q.get("error") or q.get("price") is None:
        return {"answer": "I couldn't get a quote for {} right now.".format(symbol)}
    parts = ["{} is trading at ${:,.2f}".format(symbol, q["price"])]
    if q.get("change_pct") is not None:
        parts.append("{} on the day".format(_fmt_pct(q["change_pct"])))
    hi, lo = q.get("fifty_two_week_high"), q.get("fifty_two_week_low")
    if hi and lo:
        span = hi - lo
        pos = (q["price"] - lo) / span * 100 if span else None
        if pos is not None:
            parts.append("sitting at {:.0f}% of its 52-week range".format(pos))
    return {"answer": ", ".join(parts) + ".",
            "action": {"view": "research", "symbol": symbol}}


def _answer_forecast(symbol: str) -> Dict[str, Any]:
    import forecast_ensemble
    f = forecast_ensemble.ensemble_forecast(symbol)
    ens = (f or {}).get("ensemble") or {}
    if not ens:
        return {"answer": "The forecast ensemble has no read on {} right now.".format(symbol)}
    prob = ens.get("prob_up")
    answer = "{}: ensemble of {} signals puts P(up) at {:.0f}% over {} trading days — {} conviction {}.".format(
        symbol, f.get("n_signals", 0), (prob or 0.5) * 100, f.get("horizon_days", 20),
        (ens.get("conviction") or "").lower(), ens.get("direction") or "")
    notes = f.get("notes") or []
    return {"answer": answer,
            "detail": notes[0] if notes else None,
            "action": {"view": "forecast", "symbol": symbol}}


def _answer_portfolio(q_lower: str) -> Dict[str, Any]:
    pulse = _portfolio_pulse()
    if not pulse:
        return {"answer": "You don't have any portfolio positions yet. Add holdings to get portfolio intelligence.",
                "action": {"view": "portfolio"}}
    if "worst" in q_lower or "loser" in q_lower or "losing" in q_lower:
        w = pulse.get("worst")
        if w:
            return {"answer": "Your weakest position today is {} at {} ({} day P&L).".format(
                        w["symbol"], _fmt_pct(w["day_change_pct"]), _fmt_usd(w["day_pnl"])),
                    "action": {"view": "research", "symbol": w["symbol"]}}
    if "best" in q_lower or "winner" in q_lower or "winning" in q_lower:
        b = pulse.get("best")
        if b:
            return {"answer": "Your strongest position today is {} at {} ({} day P&L).".format(
                        b["symbol"], _fmt_pct(b["day_change_pct"]), _fmt_usd(b["day_pnl"])),
                    "action": {"view": "research", "symbol": b["symbol"]}}
    day = ""
    if pulse.get("day_pnl") is not None:
        day = " Today you're {} {} ({}).".format(
            "up" if pulse["day_pnl"] >= 0 else "down",
            _fmt_usd(abs(pulse["day_pnl"])), _fmt_pct(pulse.get("day_pnl_pct")))
    return {"answer": "Portfolio value is {} across {} positions, {} {} ({}) all-time.{}".format(
                _fmt_usd(pulse["total_value"]), pulse["num_positions"],
                "up" if pulse["total_pnl"] >= 0 else "down",
                _fmt_usd(abs(pulse["total_pnl"])), _fmt_pct(pulse["total_pnl_pct"]), day),
            "action": {"view": "portfolio"}}


def _answer_exposure() -> Dict[str, Any]:
    pulse = _portfolio_pulse()
    if not pulse:
        return {"answer": "No positions yet, so no exposure to report.", "action": {"view": "portfolio"}}
    rows = sorted(pulse["holdings"], key=lambda r: -r["market_value"])[:5]
    tops = "; ".join("{} {:.0f}%".format(r["symbol"], r["weight_pct"]) for r in rows)
    heavy = [r for r in pulse["holdings"] if r["weight_pct"] >= _CONCENTRATION_PCT]
    warn = " ⚠ {} exceeds {:.0f}% concentration.".format(
        heavy[0]["symbol"], _CONCENTRATION_PCT) if heavy else ""
    return {"answer": "Largest exposures: {}.{} Full sector breakdown is in Analytics.".format(tops, warn),
            "action": {"view": "analytics"}}


def _answer_earnings(symbol: Optional[str]) -> Dict[str, Any]:
    import earnings
    if symbol:
        cal = earnings.get_earnings_calendar([symbol])
        if cal:
            e = cal[0]
            return {"answer": "{} reports earnings on {} ({} days away).".format(
                        e["symbol"], e["earnings_date"], e["days_until"]),
                    "action": {"view": "earnings", "symbol": symbol}}
        return {"answer": "No upcoming earnings date found for {}.".format(symbol)}
    held = []
    try:
        held = [h["symbol"] for h in db.get_portfolio() if h.get("asset_type") != "crypto"]
    except Exception:
        pass
    cal = earnings.get_earnings_calendar(held[:25]) if held else []
    soon = [e for e in cal if (e.get("days_until") or 99) <= 14]
    if not soon:
        return {"answer": "No portfolio earnings in the next two weeks.",
                "action": {"view": "earnings"}}
    listing = ", ".join("{} on {}".format(e["symbol"], e["earnings_date"]) for e in soon[:5])
    return {"answer": "Upcoming portfolio earnings: {}.".format(listing),
            "action": {"view": "earnings"}}


def _answer_market() -> Dict[str, Any]:
    regime = _market_regime()
    if not regime:
        return {"answer": "Market data is unavailable right now."}
    bits = []
    if regime.get("spx_pct") is not None:
        bits.append("S&P 500 {}".format(_fmt_pct(regime["spx_pct"])))
    if regime.get("ndx_pct") is not None:
        bits.append("Nasdaq-100 {}".format(_fmt_pct(regime["ndx_pct"])))
    if regime.get("vix") is not None:
        bits.append("VIX at {:.1f} ({})".format(regime["vix"], regime.get("regime") or "—"))
    note = " {}".format(regime.get("regime_note")) if regime.get("regime_note") else ""
    return {"answer": ", ".join(bits) + ".{}".format(note),
            "action": {"view": "markets"}}


def _answer_alerts() -> Dict[str, Any]:
    alerts = db.get_price_alerts(include_triggered=True)
    trig = [a for a in alerts if a.get("triggered")]
    active = [a for a in alerts if not a.get("triggered")]
    if not alerts:
        return {"answer": "No price alerts set. Create alerts to get notified on key levels.",
                "action": {"view": "alerts"}}
    t = "{} triggered".format(len(trig)) if trig else "none triggered"
    return {"answer": "You have {} active alert{} ({}).".format(
                len(active), "s" if len(active) != 1 else "", t),
            "action": {"view": "alerts"}}


def _answer_ideas() -> Dict[str, Any]:
    try:
        import idea_pool_warmer
        pool = idea_pool_warmer.list_warmed_symbols()
    except Exception:
        pool = []
    if not pool:
        return {"answer": "The idea pool is still warming up — check the Ideas view shortly.",
                "action": {"view": "ideas"}}
    top = pool[:3]
    listing = ", ".join("{} (score {:.0f})".format(r["symbol"], r.get("composite_score") or 0) for r in top)
    return {"answer": "Top-scored ideas right now: {}.".format(listing),
            "action": {"view": "ideas"}}


_HELP_ANSWER = (
    "I can answer things like: “how's my portfolio”, “biggest loser today”, "
    "“price of NVDA”, “forecast TSLA”, “when does AAPL report”, "
    "“how are markets”, “my exposure”, “any ideas”, or “my alerts”."
)


def ask(query: str) -> Dict[str, Any]:
    """Route a natural-language question to the right engine. Local-only."""
    q = (query or "").strip()
    if not q:
        return {"intent": "help", "answer": _HELP_ANSWER}
    ql = q.lower()
    symbol = _extract_symbol(q)

    def done(intent: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload["intent"] = intent
        payload.setdefault("symbol", symbol)
        return payload

    try:
        if any(w in ql for w in ("forecast", "predict", "outlook", "go up", "go down", "prob")) and symbol:
            return done("forecast", _answer_forecast(symbol))
        if any(w in ql for w in ("earnings", "report", "reports", "reporting")):
            return done("earnings", _answer_earnings(symbol))
        if any(w in ql for w in ("exposure", "allocation", "concentrat", "diversif", "weight")):
            return done("exposure", _answer_exposure())
        if "alert" in ql:
            return done("alerts", _answer_alerts())
        if any(w in ql for w in ("idea", "opportunit", "what should i buy", "what to buy")):
            return done("ideas", _answer_ideas())
        if any(w in ql for w in ("portfolio", "am i up", "am i down", "my position",
                                 "p&l", "pnl", "net worth", "winner", "loser",
                                 "best position", "worst position", "how am i doing")):
            return done("portfolio", _answer_portfolio(ql))
        if any(w in ql for w in ("market", "vix", "s&p", "spx", "nasdaq", "fear", "regime", "volatil")):
            return done("market", _answer_market())
        if symbol:
            return done("quote", _answer_quote(symbol))
    except Exception as e:
        log.warning("jarvis.ask failed for %r: %s", q, e)
        return {"intent": "error",
                "answer": "Something went wrong answering that — the data source may be rate-limited. Try again in a moment."}

    return {"intent": "help", "answer": _HELP_ANSWER}
