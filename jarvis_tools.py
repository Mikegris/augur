"""
jarvis_tools — the tool registry that gives the Jarvis agent access to the
whole app.

Each entry wraps an existing engine behind an OpenAI function-calling schema.
The agent loop in jarvis.py decides which tools to call; this module owns
WHAT exists and HOW it executes:

  • read tools execute immediately and return condensed JSON (responses are
    truncated to keep prompt cost bounded — the model sees a summary, not a
    dump);
  • mutating tools NEVER execute from the agent loop. The loop returns a
    proposal the UI must confirm, and only then does /api/jarvis/act call
    execute_mutating() — a whitelist keyed on this registry, so the LLM can
    only ever request actions defined here, with validated arguments.

Python 3.9 compatible.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("augur.jarvis_tools")

_RESULT_CHAR_BUDGET = 1800
_SYM_RE = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,9}$")


def _sym(args: Dict[str, Any]) -> str:
    s = str(args.get("symbol") or "").strip().upper()
    if not _SYM_RE.match(s):
        raise ValueError("invalid symbol")
    return s


# ─── read-tool implementations (condensed outputs) ───────────────────────────

def _t_get_quote(args):
    import fetcher
    q = fetcher.get_quote(_sym(args))
    keep = ("symbol", "price", "change", "change_pct", "prev_close", "volume",
            "market_cap", "fifty_two_week_high", "fifty_two_week_low")
    return {k: q.get(k) for k in keep}


def _t_get_portfolio(args):
    import jarvis
    pulse = jarvis._portfolio_pulse()
    if not pulse:
        return {"note": "no positions"}
    out = {k: v for k, v in pulse.items() if k != "holdings"}
    out["top_positions"] = sorted(
        pulse["holdings"], key=lambda r: -r["market_value"])[:10]
    return out


def _t_market_regime(args):
    import jarvis
    return jarvis._market_regime() or {"note": "market data unavailable"}


def _t_forecast(args):
    import forecast_ensemble
    horizon = int(args.get("horizon_days") or 20)
    horizon = max(5, min(horizon, 120))
    f = forecast_ensemble.ensemble_forecast(_sym(args), horizon_days=horizon)
    ens = (f or {}).get("ensemble") or {}
    return {"symbol": f.get("symbol"), "horizon_days": f.get("horizon_days"),
            "n_signals": f.get("n_signals"),
            "prob_up": ens.get("prob_up"), "direction": ens.get("direction"),
            "conviction": ens.get("conviction"), "verdict": ens.get("verdict"),
            "return_cone": ens.get("return_cone"), "notes": f.get("notes")}


def _t_earnings(args):
    import earnings
    import database as db
    syms = args.get("symbols")
    if isinstance(syms, list) and syms:
        syms = [_sym({"symbol": s}) for s in syms[:10]]
    else:
        syms = [h["symbol"] for h in db.get_portfolio()
                if h.get("asset_type") != "crypto"][:25]
    return earnings.get_earnings_calendar(syms)


def _t_smart_money(args):
    import smart_money
    return smart_money.compute_score(_sym(args))


def _t_gex(args):
    import gex_engine
    return gex_engine.get_gex_summary(_sym(args))


def _t_options_flow(args):
    import fetcher
    return fetcher.get_unusual_options_flow(_sym(args))


def _t_news(args):
    import fetcher
    n = fetcher.get_news(_sym(args), limit=5)
    return [{"title": x.get("title"), "publisher": x.get("publisher"),
             "published": x.get("published") or x.get("providerPublishTime")}
            for x in (n or [])[:5]]


def _t_sector_flow(args):
    import synth_sectorflow
    return synth_sectorflow.sector_flow()


def _t_alerts(args):
    import database as db
    return db.get_price_alerts(include_triggered=True)


def _t_watchlist(args):
    import database as db
    return [{"symbol": w.get("symbol"), "name": w.get("name"),
             "alert_high": w.get("alert_high"), "alert_low": w.get("alert_low")}
            for w in db.get_watchlist()]


def _t_narrative(args):
    import narrative_engine
    return narrative_engine.analyze_narrative(_sym(args))


def _t_top_ideas(args):
    import idea_pool_warmer
    return idea_pool_warmer.list_warmed_symbols()[:10]


def _t_stress_test(args):
    import database as db
    import fetcher
    drop = args.get("custom_drop_pct")
    if drop is not None:
        drop = max(-95.0, min(0.0, float(drop)))
    holdings = db.get_portfolio()
    if not holdings:
        return {"note": "no positions"}
    syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"]
    crypto = [h["symbol"] for h in holdings if h["asset_type"] == "crypto"]
    prices: Dict[str, Any] = {}
    if syms:
        prices.update(fetcher.get_quotes_batch(syms))
    if crypto:
        cp = fetcher.get_quotes_batch([s + "-USD" for s in crypto])
        for s in crypto:
            k = (s + "-USD").upper()
            if k in cp:
                prices[s] = cp[k]
    for h in holdings:
        cp_ = prices.get(h["symbol"], {}).get("price")
        cur = cp_ if cp_ is not None else h["avg_cost"]
        h["market_value"] = round(cur * h["shares"], 2)
        h["current_price"] = cur
    return fetcher.get_portfolio_stress_test(holdings, custom_drop_pct=drop)


# ─── mutating-tool implementations (only via execute_mutating) ───────────────

def _t_add_alert(args):
    import database as db
    sym = _sym(args)
    alert_type = str(args.get("alert_type") or "").lower()
    if alert_type not in ("above", "below"):
        raise ValueError("alert_type must be 'above' or 'below'")
    price = float(args.get("price"))
    if not (0 < price < 10_000_000):
        raise ValueError("price out of range")
    alert_id = db.add_price_alert(sym, alert_type, price)
    return {"status": "created", "id": alert_id,
            "label": "{} {} ${:,.2f}".format(sym, alert_type, price)}


def _t_add_watchlist(args):
    import database as db
    sym = _sym(args)
    db.add_to_watchlist(sym)
    return {"status": "added", "label": "{} added to watchlist".format(sym)}


# ─── registry ────────────────────────────────────────────────────────────────

def _p(props: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}

_SYM_PROP = {"symbol": {"type": "string", "description": "Ticker symbol, e.g. NVDA"}}

TOOLS: Dict[str, Dict[str, Any]] = {
    "get_quote": {
        "fn": _t_get_quote, "mutating": False,
        "description": "Live quote: price, day change, volume, 52-week range.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "get_portfolio": {
        "fn": _t_get_portfolio, "mutating": False,
        "description": "The user's portfolio: total value, P&L, day move, top positions with weights.",
        "parameters": _p({}),
    },
    "get_market_regime": {
        "fn": _t_market_regime, "mutating": False,
        "description": "Market snapshot: S&P/Nasdaq day moves, VIX level and volatility regime.",
        "parameters": _p({}),
    },
    "forecast": {
        "fn": _t_forecast, "mutating": False,
        "description": "Calibrated ensemble forecast for a symbol: P(up), direction, conviction, return cone.",
        "parameters": _p(dict(_SYM_PROP, horizon_days={"type": "integer", "description": "Trading-day horizon (5-120, default 20)"}), ["symbol"]),
    },
    "get_earnings_calendar": {
        "fn": _t_earnings, "mutating": False,
        "description": "Upcoming earnings dates. Defaults to the user's holdings if no symbols given.",
        "parameters": _p({"symbols": {"type": "array", "items": {"type": "string"}}}),
    },
    "smart_money_score": {
        "fn": _t_smart_money, "mutating": False,
        "description": "Smart-money convergence score for a symbol (insiders, institutions, options flow, congress).",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "get_gex": {
        "fn": _t_gex, "mutating": False,
        "description": "Dealer gamma exposure summary for a symbol (pinning/amplification levels).",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "get_options_flow": {
        "fn": _t_options_flow, "mutating": False,
        "description": "Unusual options activity for a symbol.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "get_news": {
        "fn": _t_news, "mutating": False,
        "description": "Latest headlines for a symbol (max 5).",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "sector_flow": {
        "fn": _t_sector_flow, "mutating": False,
        "description": "Where capital is rotating across the 11 sector ETFs.",
        "parameters": _p({}),
    },
    "get_alerts": {
        "fn": _t_alerts, "mutating": False,
        "description": "The user's price alerts, armed and triggered.",
        "parameters": _p({}),
    },
    "get_watchlist": {
        "fn": _t_watchlist, "mutating": False,
        "description": "The user's watchlist symbols.",
        "parameters": _p({}),
    },
    "narrative": {
        "fn": _t_narrative, "mutating": False,
        "description": "News-narrative phase analysis for a symbol (emergence/acceleration/exhaustion).",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
    "top_ideas": {
        "fn": _t_top_ideas, "mutating": False,
        "description": "Top pre-scored investment ideas from the warmed pool.",
        "parameters": _p({}),
    },
    "stress_test": {
        "fn": _t_stress_test, "mutating": False,
        "description": "Replay historical crash scenarios (2008, COVID, rate shocks) against the user's actual portfolio. Optional custom_drop_pct (e.g. -30).",
        "parameters": _p({"custom_drop_pct": {"type": "number", "description": "Optional custom market drop in percent, negative"}}),
    },
    "list_memories": {
        "fn": lambda args: __import__("database").jarvis_list_memories(),
        "mutating": False,
        "description": "Durable facts Jarvis remembers about the user across sessions.",
        "parameters": _p({}),
    },
    # ── mutating: proposal-only from the agent loop ──────────────────────────
    "remember_fact": {
        "fn": lambda args: {"status": "remembered",
                            "id": __import__("database").jarvis_add_memory(
                                str(args.get("fact") or ""), source="agent"),
                            "label": "Remember: {}".format(str(args.get("fact") or "")[:120])},
        "mutating": True,
        "description": "Store a durable fact about the user (preferences, constraints). The user must confirm before this is saved.",
        "parameters": _p({"fact": {"type": "string", "description": "One concise fact, e.g. 'User is risk-averse'"}}, ["fact"]),
    },
    "add_price_alert": {
        "fn": _t_add_alert, "mutating": True,
        "description": "Create a price alert. The user must confirm before this executes.",
        "parameters": _p(dict(_SYM_PROP,
                              alert_type={"type": "string", "enum": ["above", "below"]},
                              price={"type": "number"}),
                         ["symbol", "alert_type", "price"]),
    },
    "add_to_watchlist": {
        "fn": _t_add_watchlist, "mutating": True,
        "description": "Add a symbol to the user's watchlist. The user must confirm before this executes.",
        "parameters": _p(dict(_SYM_PROP), ["symbol"]),
    },
}


def openai_schemas() -> List[Dict[str, Any]]:
    return [{"type": "function",
             "function": {"name": name,
                          "description": spec["description"],
                          "parameters": spec["parameters"]}}
            for name, spec in TOOLS.items()]


def is_mutating(name: str) -> bool:
    spec = TOOLS.get(name)
    return bool(spec and spec["mutating"])


def proposal_label(name: str, args: Dict[str, Any]) -> str:
    if name == "remember_fact":
        return "Remember: {}".format(str(args.get("fact", ""))[:120])
    if name == "add_price_alert":
        return "Set alert: {} {} ${}".format(
            str(args.get("symbol", "")).upper(), args.get("alert_type"), args.get("price"))
    if name == "add_to_watchlist":
        return "Add {} to watchlist".format(str(args.get("symbol", "")).upper())
    return "{} {}".format(name, json.dumps(args))


def execute_read(name: str, args: Dict[str, Any]) -> str:
    """Run a READ tool; return a JSON string within the char budget."""
    spec = TOOLS.get(name)
    if not spec:
        return json.dumps({"error": "unknown tool"})
    if spec["mutating"]:
        return json.dumps({"error": "tool requires user confirmation"})
    try:
        result = spec["fn"](args or {})
    except Exception as e:
        log.debug("tool %s failed: %s", name, e)
        return json.dumps({"error": str(e)[:200]})
    s = json.dumps(result, default=str)
    if len(s) > _RESULT_CHAR_BUDGET:
        s = s[:_RESULT_CHAR_BUDGET] + '..."(truncated)"}'
    return s


def execute_mutating(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Run a MUTATING tool after explicit user confirmation. Whitelisted on
    this registry — unknown or read tools are rejected."""
    spec = TOOLS.get(name)
    if not spec or not spec["mutating"]:
        return {"error": "not a confirmable action"}
    try:
        return spec["fn"](args or {})
    except Exception as e:
        return {"error": str(e)[:200]}
