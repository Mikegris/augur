"""
Finviz scraper via the `finvizfinance` library (PyPI / GitHub:
lit-llm/finvizfinance). No API key required — it parses Finviz's HTML.

Three integrations:
  - sector_heatmap()      — performance/valuation across all 11 sectors
  - insider_trades()      — Finviz's "Latest Insider Trading" aggregator
  - stock_news(symbol)    — Finviz's per-ticker news feed

This is intentionally a thin layer over finvizfinance — the lib already
handles HTML quirks. We add caching + JSON-friendly shaping.

Caveat: scrapers break. If Finviz changes their HTML, finvizfinance ships
patches reasonably quickly, but expect occasional gaps. Defaults are
defensive (empty list on failure, never raises).
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime

log = logging.getLogger("augur.finviz")

# Lazy-import — if the bundle is missing the lib (or HTML changes break
# imports), we degrade to empty responses instead of crashing.
_LIB = None


def _lib():
    global _LIB
    if _LIB is None:
        try:
            from finvizfinance.group.overview import Overview
            from finvizfinance.insider import Insider
            from finvizfinance.quote import finvizfinance as Quote
            _LIB = {"Overview": Overview, "Insider": Insider, "Quote": Quote, "ok": True}
        except Exception as e:
            log.warning("finvizfinance not importable: %s", e)
            _LIB = {"ok": False, "error": str(e)}
    return _LIB


_CACHE: dict = {}
TTL_SECTOR = 30 * 60   # sector data — 30 min
TTL_INSIDER = 15 * 60  # insider — 15 min
TTL_NEWS = 10 * 60     # news — 10 min


def _cache_get(key):
    hit = _CACHE.get(key)
    if not hit: return None
    val, exp = hit
    if time.time() > exp:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_set(key, val, ttl):
    _CACHE[key] = (val, time.time() + ttl)


def _safe_float(v):
    if v is None: return None
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def sector_heatmap() -> dict:
    """All 11 GICS sectors with valuation + day change. Maps directly to a
    grid layout in the UI. The ``change_1w`` field name is preserved for
    consumer compatibility but the underlying Finviz group-overview "Change"
    column is the current-day percent change (decimal fraction), not a
    weekly performance number."""
    cache_key = ("finviz", "sectors")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    lib = _lib()
    if not lib.get("ok"):
        return {"sectors": [], "error": lib.get("error", "finvizfinance unavailable")}

    try:
        ov = lib["Overview"]()
        df = ov.screener_view(group="Sector", order="Change")
    except Exception as e:
        log.warning("finviz sector fetch failed: %s", e)
        return {"sectors": [], "error": str(e)}

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "name":         r.get("Name"),
            "stocks":       int(r["Stocks"]) if r.get("Stocks") else None,
            "market_cap":   _safe_float(r.get("Market Cap")),
            "dividend":     _safe_float(r.get("Dividend")),
            "pe":           _safe_float(r.get("P/E")),
            "fwd_pe":       _safe_float(r.get("Fwd P/E")),
            "peg":          _safe_float(r.get("PEG")),
            "float_short":  _safe_float(r.get("Float Short")),
            # Despite the field name, this is the current-day change (decimal
            # fraction) from Finviz's sector-overview "Change" column, not a
            # 1-week return. Name retained for UI-consumer compatibility.
            "change_1w":    _safe_float(r.get("Change")),
            "volume":       _safe_float(r.get("Volume")),
            "recom":        _safe_float(r.get("Recom")),
        })
    out = {
        "sectors": rows,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "source": "finviz.com (via finvizfinance)",
    }
    _cache_set(cache_key, out, TTL_SECTOR)
    return out


def insider_trades(option: str = "latest") -> dict:
    """Latest insider activity across the market. `option`:
       'latest', 'latest buys', 'latest sales', 'top week', 'top owner trade'."""
    cache_key = ("finviz", "insider", option)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    lib = _lib()
    if not lib.get("ok"):
        return {"trades": [], "error": lib.get("error", "finvizfinance unavailable")}

    try:
        ins = lib["Insider"](option=option)
        df = ins.get_insider()
    except Exception as e:
        log.warning("finviz insider fetch failed: %s", e)
        return {"trades": [], "error": str(e)}

    if df is None or df.empty:
        return {"trades": []}

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "ticker":       r.get("Ticker"),
            "owner":        r.get("Owner"),
            "relationship": r.get("Relationship"),
            "date":         r.get("Date"),
            "transaction":  r.get("Transaction"),
            "cost":         _safe_float(r.get("Cost")),
            "shares":       _safe_float(r.get("#Shares")),
            "value":        _safe_float(r.get("Value ($)")),
            "shares_total": _safe_float(r.get("#Shares Total")),
            # finvizfinance's insider DataFrame ships two columns:
            #   "SEC Form 4"      -> the timestamp text ("May 22 09:47 PM")
            #   "SEC Form 4 Link" -> the actual sec.gov filing URL
            # The field name implies a link, so use the link column.
            "sec_form4":    r.get("SEC Form 4 Link"),
        })
    out = {
        "option": option,
        "trades": rows[:50],
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }
    _cache_set(cache_key, out, TTL_INSIDER)
    return out


def stock_news(symbol: str) -> dict:
    """Finviz's per-ticker news aggregator — pulls headlines from a dozen
    feed sources Finviz indexes."""
    sym = symbol.upper()
    cache_key = ("finviz", "news", sym)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    lib = _lib()
    if not lib.get("ok"):
        return {"symbol": sym, "items": [], "error": lib.get("error", "finvizfinance unavailable")}

    try:
        q = lib["Quote"](ticker=sym)
        df = q.ticker_news()
    except Exception as e:
        log.debug("finviz news fetch failed for %s: %s", sym, e)
        return {"symbol": sym, "items": [], "error": str(e)}

    if df is None or df.empty:
        return {"symbol": sym, "items": []}

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "date":   str(r.get("Date", "")),
            "title":  r.get("Title"),
            "source": r.get("Source"),
            "url":    r.get("Link"),
        })
    out = {"symbol": sym, "items": rows[:30]}
    _cache_set(cache_key, out, TTL_NEWS)
    return out
