"""
Hacker News mentions — tech-tilted sentiment signal.

HN's audience is heavily tech / VC / engineering — mentions of NVDA,
TSLA, AAPL, PLTR, OpenAI, etc. on HN tend to lead retail attention by
12-24h. Free, no-auth API via the Algolia search backend:

    https://hn.algolia.com/api/v1/search

Two query modes:
  - by ticker symbol (e.g. "NVDA") — exact match, lower noise
  - by company name (e.g. "Nvidia") — broader, more recall

We do both and dedupe by HN story ID.

Polarity is a coarse keyword-based score (no model dependency, no API
costs). It's a "vibe check" not a real sentiment model — but for HN's
discourse style it's surprisingly serviceable.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import requests

log = logging.getLogger("augur.hn")

HN_SEARCH = "https://hn.algolia.com/api/v1/search"
HEADERS = {"User-Agent": "AUGUR/1.0 wealth-tracker"}

# A coarse keyword polarity model — meant for HN comment titles, which are
# typically declarative and concise. Real model would be better but adding
# transformers to the bundle is a lot for a "vibe" signal.
POS = {
    "good", "great", "love", "excellent", "amazing", "beats", "wins",
    "strong", "rallies", "soars", "jumps", "surge", "surges", "growth",
    "beat", "exceeded", "raised", "upgraded", "buy", "bullish", "rally",
    "record", "all-time", "ath", "moat",
}
NEG = {
    "bad", "terrible", "hate", "worst", "miss", "missed", "loss", "losses",
    "weak", "fall", "falls", "plunges", "tanks", "crash", "crashes",
    "downgrade", "downgraded", "sell", "bearish", "decline", "drops",
    "layoffs", "fraud", "scam", "lawsuit", "investigation", "sec",
    "scandal", "bankruptcy", "delisted", "halted",
}

# Hardcoded mappings for common tickers → search terms. The ticker itself
# is also searched, but supplementing with the company name catches mentions
# that don't include the symbol.
TICKER_NAMES = {
    "AAPL": "Apple", "MSFT": "Microsoft", "AMZN": "Amazon",
    "GOOG": "Google", "GOOGL": "Google", "META": "Meta",
    "NVDA": "Nvidia", "TSLA": "Tesla", "NFLX": "Netflix",
    "AMD": "AMD", "INTC": "Intel", "ORCL": "Oracle",
    "CRM": "Salesforce", "ADBE": "Adobe", "PLTR": "Palantir",
    "COIN": "Coinbase", "HOOD": "Robinhood", "SHOP": "Shopify",
    "SNOW": "Snowflake", "DDOG": "Datadog", "NET": "Cloudflare",
    "MDB": "MongoDB", "CRWD": "CrowdStrike", "PANW": "Palo Alto Networks",
    "SPOT": "Spotify", "UBER": "Uber", "LYFT": "Lyft",
    "ABNB": "Airbnb", "RBLX": "Roblox", "U": "Unity Software",
    "RIVN": "Rivian", "LCID": "Lucid Motors",
    "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana",
}

_CACHE: dict = {}
DEFAULT_TTL = 3600  # 1 hour


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


def _score_text(text: str) -> int:
    """Coarse polarity: +1 per positive word, -1 per negative word.
    Returns the net delta."""
    if not text:
        return 0
    words = {w.strip(".,!?'\"()[]:;") for w in text.lower().split()}
    return len(words & POS) - len(words & NEG)


def _algolia_search(query: str, hours: int = 168, hits: int = 50):
    """Search HN stories/comments matching query within the time window."""
    cutoff = int((datetime.utcnow() - timedelta(hours=hours)).timestamp())
    try:
        resp = requests.get(HN_SEARCH, params={
            "query": query,
            "tags": "(story,comment)",
            "numericFilters": f"created_at_i>{cutoff}",
            "hitsPerPage": hits,
        }, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json().get("hits", [])
    except Exception as e:
        log.debug("HN search failed for %s: %s", query, e)
        return []


def fetch_mentions(symbol: str, hours: int = 168) -> dict:
    """Return HN mentions of `symbol` over the last `hours` hours, with
    coarse polarity scoring and per-hit metadata. Default window: 7 days."""
    cache_key = ("hn", symbol.upper(), hours)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    sym = symbol.upper()
    company = TICKER_NAMES.get(sym)

    # Query both — symbol gives us low-noise hits, company name gives recall.
    hits = _algolia_search(sym, hours=hours)
    if company:
        hits += _algolia_search(company, hours=hours)

    # Dedupe by HN object ID
    seen, deduped = set(), []
    for h in hits:
        oid = h.get("objectID")
        if oid and oid not in seen:
            seen.add(oid)
            deduped.append(h)

    # Score each
    scored = []
    for h in deduped:
        text = (h.get("title") or "") + " " + (h.get("comment_text") or "") + " " + (h.get("story_text") or "")
        polarity = _score_text(text)
        scored.append({
            "id": h.get("objectID"),
            "title": h.get("title") or (h.get("story_title") or "")[:120],
            "author": h.get("author"),
            "created_at": h.get("created_at"),
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            "points": h.get("points"),
            "num_comments": h.get("num_comments"),
            "polarity": polarity,
            "kind": "comment" if h.get("comment_text") else "story",
        })

    # Aggregate
    if scored:
        polarities = [s["polarity"] for s in scored]
        net_polarity = sum(polarities)
        avg_polarity = net_polarity / len(polarities)
        positive = sum(1 for p in polarities if p > 0)
        negative = sum(1 for p in polarities if p < 0)
        neutral = sum(1 for p in polarities if p == 0)
    else:
        net_polarity = avg_polarity = positive = negative = neutral = 0

    out = {
        "symbol": sym,
        "company_searched": company,
        "window_hours": hours,
        "mention_count": len(scored),
        "stats": {
            "net_polarity": net_polarity,
            "avg_polarity": round(avg_polarity, 2),
            "positive_count": positive,
            "negative_count": negative,
            "neutral_count": neutral,
        },
        "mentions": scored[:25],  # cap response size
    }
    _cache_set(cache_key, out, DEFAULT_TTL)
    return out
