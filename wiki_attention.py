"""
Wikipedia pageviews — retail-attention proxy.

Uses Wikimedia's REST pageview API, which is free, key-less, and lightly
rate-limited (~200/sec per IP). Pageview spikes are a leading indicator
for retail-driven moves (the classic example: $AMC and $GME pageviews
exploded days before the price action in early 2021).

API docs: https://wikimedia.org/api/rest_v1/

Flow per symbol:
  1. Map ticker → Wikipedia article title (via heuristic + small hardcoded
     overrides for ambiguous tickers like "T" → "AT&T", not "Letter T")
  2. Fetch daily pageviews for the article over a configurable window
  3. Return the timeseries + summary stats (mean, latest, % change vs.
     7-day baseline)
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from datetime import datetime, timedelta

import requests

log = logging.getLogger("augur.wiki")

WIKI_BASE = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
HEADERS = {"User-Agent": "AUGUR/1.0 wealth-tracker (research)"}

# Tickers whose plain symbol isn't a useful Wikipedia title.
# Mostly single-letter tickers + brands that don't match the corp name.
# Store titles RAW (real "&", not "%26"): fetch_pageviews runs each title
# through urllib.parse.quote(..., safe=''), which encodes "&" → "%26" exactly
# once. Pre-encoding here made quote() double-encode the "%" into "%2526",
# which 404'd PG / JNJ for the full 12h cache TTL.
TICKER_OVERRIDES = {
    "T":    "AT&T",
    "F":    "Ford_Motor_Company",
    "V":    "Visa_Inc.",
    "C":    "Citigroup",
    "X":    "X_(company)",
    "META": "Meta_Platforms",
    "GOOG": "Alphabet_Inc.",
    "GOOGL":"Alphabet_Inc.",
    "BRK.B":"Berkshire_Hathaway",
    "BRK.A":"Berkshire_Hathaway",
    "JPM":  "JPMorgan_Chase",
    "BAC":  "Bank_of_America",
    "AAPL": "Apple_Inc.",
    "MSFT": "Microsoft",
    "AMZN": "Amazon_(company)",
    "TSLA": "Tesla,_Inc.",
    "NVDA": "Nvidia",
    "AMD":  "AMD",
    "INTC": "Intel",
    "NFLX": "Netflix",
    "DIS":  "The_Walt_Disney_Company",
    "WMT":  "Walmart",
    "COST": "Costco",
    "PG":   "Procter_&_Gamble",
    "KO":   "The_Coca-Cola_Company",
    "PEP":  "PepsiCo",
    "JNJ":  "Johnson_&_Johnson",
    "PFE":  "Pfizer",
    "XOM":  "ExxonMobil",
    "CVX":  "Chevron_Corporation",
    "BA":   "Boeing",
    "GS":   "Goldman_Sachs",
    "MS":   "Morgan_Stanley",
    "GME":  "GameStop",
    "AMC":  "AMC_Theatres",
    "PLTR": "Palantir_Technologies",
    "COIN": "Coinbase",
    "HOOD": "Robinhood_Markets",
    "BTC-USD":  "Bitcoin",
    "ETH-USD":  "Ethereum",
    "SOL-USD":  "Solana_(blockchain_platform)",
    "DOGE-USD": "Dogecoin",
}

# In-process cache — pageviews update once daily on Wikimedia's side.
_CACHE: dict = {}
DEFAULT_TTL = 12 * 3600  # 12 hours


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


def _article_title_for(symbol: str) -> str:
    """Pick a Wikipedia article title for a stock symbol. Falls back to the
    symbol itself when no override exists — caller should check whether the
    fetch returns data and treat 404 as "no useful Wikipedia page"."""
    sym = symbol.upper()
    return TICKER_OVERRIDES.get(sym, sym)


def fetch_pageviews(symbol: str, days: int = 30) -> dict:
    """Return daily pageviews for the symbol's Wikipedia article over the
    last N days, plus summary stats."""
    days = max(7, min(days, 365))  # clamp; Wikimedia API caps at 365
    cache_key = ("wiki", symbol.upper(), days)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    title = _article_title_for(symbol)
    end = datetime.utcnow().date() - timedelta(days=1)  # yesterday — today's data lags
    start = end - timedelta(days=days - 1)
    url = (f"{WIKI_BASE}/en.wikipedia/all-access/all-agents/"
           f"{urllib.parse.quote(title, safe='')}/daily/"
           f"{start.strftime('%Y%m%d')}/{end.strftime('%Y%m%d')}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 404:
            # Cache the 404 so we don't hammer Wikimedia for every page load
            # on tickers without a Wikipedia article (or whose mapped title is
            # a redirect / sub-article that the pageviews endpoint can't serve).
            # Use a shorter TTL than success — article titles get created over
            # time — but long enough to absorb a UI's repeated calls.
            missing = {"symbol": symbol.upper(), "article": title, "points": [],
                       "error": "Wikipedia article not found for this symbol"}
            _cache_set(cache_key, missing, DEFAULT_TTL)
            return missing
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.debug("wiki pageviews fetch failed for %s (%s): %s", symbol, title, e)
        # Cache transient failures briefly so a flapping endpoint doesn't get
        # retried on every UI render. 5 min is short enough to recover quickly.
        err = {"symbol": symbol.upper(), "article": title, "points": [], "error": str(e)}
        _cache_set(cache_key, err, 300)
        return err

    points = []
    for item in data.get("items", []):
        ts = item.get("timestamp", "")  # 'YYYYMMDDHH'
        if len(ts) < 8:
            continue
        date = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        points.append({"date": date, "views": item.get("views", 0)})

    # Summary stats
    if points:
        views = [p["views"] for p in points]
        mean = sum(views) / len(views)
        latest = views[-1]
        # 7-day baseline excluding the most-recent day (so a spike on day N
        # gets measured against the prior week, not against itself)
        baseline_window = views[-8:-1] if len(views) >= 8 else views[:-1]
        baseline = sum(baseline_window) / len(baseline_window) if baseline_window else mean
        spike_pct = ((latest - baseline) / baseline * 100) if baseline else None
        stats = {
            "mean": round(mean, 1),
            "latest": latest,
            "baseline_7d": round(baseline, 1),
            "spike_pct_vs_baseline": round(spike_pct, 2) if spike_pct is not None else None,
            "max": max(views),
            "min": min(views),
        }
    else:
        stats = {}

    out = {
        "symbol": symbol.upper(),
        "article": title,
        "article_url": f"https://en.wikipedia.org/wiki/{title}",
        "points": points,
        "stats": stats,
    }
    _cache_set(cache_key, out, DEFAULT_TTL)
    return out
