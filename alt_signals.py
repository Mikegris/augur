"""
alt_signals.py — Free, no-auth alternative-data signals.

Sources:
    Reddit (r/wallstreetbets, r/stocks, r/investing)  via .json endpoints
    StockTwits public symbol stream                   via api.stocktwits.com
"""

import logging
import re
import threading
import time
from collections import Counter

import requests

log = logging.getLogger("augur.altsignals")

UA = "AUGUR/1.0 (+local research)"
HEADERS_REDDIT   = {"User-Agent": UA}
HEADERS_STWITS   = {"User-Agent": UA, "Accept": "application/json"}

# ── TTL cache ──────────────────────────────────────────────────────
_cache = {}
_lock = threading.Lock()


def _cget(k):
    with _lock:
        v = _cache.get(k)
        if v and v[1] > time.time():
            return v[0]
        return None


def _cset(k, value, ttl):
    with _lock:
        _cache[k] = (value, time.time() + ttl)
        if len(_cache) > 100:
            now = time.time()
            for kk in [kk for kk, vv in _cache.items() if vv[1] < now][:30]:
                _cache.pop(kk, None)


# ════════════════════════════════════════════════════════════════════
# REDDIT — no auth, hot/new posts as JSON
# ════════════════════════════════════════════════════════════════════
SUBREDDITS_DEFAULT = ("wallstreetbets", "stocks", "investing", "options")
_TICKER_PATTERN = re.compile(r"\$?\b([A-Z]{1,5})\b")
_NOISE = {
    "I", "A", "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "WHO",
    "WHY", "HOW", "ITS", "MY", "ON", "OR", "OF", "TO", "BE", "IS", "AS", "AT",
    "AN", "BY", "IN", "WE", "DO", "GO", "SO", "IT", "NO", "IF", "OK", "DD",
    "YOLO", "WSB", "HODL", "USD", "EU", "US", "AI", "CEO", "CFO", "FED", "GDP",
    "USA", "PSA", "ETF", "IPO", "TLDR", "NSFW", "OP", "OOP", "RIP",
}


def reddit_subreddit_posts(subreddit, *, sort="hot", limit=50):
    """Fetch posts from a subreddit. sort: hot|new|top|rising"""
    cache_key = ("reddit", subreddit, sort, limit)
    hit = _cget(cache_key)
    if hit is not None:
        return hit
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json"
    try:
        r = requests.get(url, headers=HEADERS_REDDIT, params={"limit": min(limit, 100)}, timeout=15)
        r.raise_for_status()
        children = (r.json().get("data") or {}).get("children") or []
        posts = []
        for c in children:
            d = c.get("data") or {}
            posts.append({
                "id": d.get("id"),
                "title": d.get("title"),
                "selftext": d.get("selftext", "")[:600],
                "score": d.get("score") or 0,
                "num_comments": d.get("num_comments") or 0,
                "upvote_ratio": d.get("upvote_ratio"),
                "url": "https://reddit.com" + (d.get("permalink") or ""),
                "created_utc": d.get("created_utc") or 0,
                "subreddit": subreddit,
                "flair": d.get("link_flair_text") or "",
            })
        _cset(cache_key, posts, 300)
        return posts
    except Exception as e:
        log.warning("reddit fetch %s: %s", subreddit, e)
        return []


def reddit_ticker_mentions(*, subreddits=SUBREDDITS_DEFAULT, sort="hot", limit_per=80):
    """Aggregate ticker mention counts across subreddits."""
    counter = Counter()
    score_sum = Counter()
    posts_sample = {}
    for sub in subreddits:
        for p in reddit_subreddit_posts(sub, sort=sort, limit=limit_per):
            text = (p.get("title") or "") + " " + (p.get("selftext") or "")
            seen = set()
            for m in _TICKER_PATTERN.finditer(text):
                t = m.group(1)
                if len(t) < 2 or t in _NOISE:
                    continue
                seen.add(t)
            for t in seen:
                counter[t] += 1
                score_sum[t] += int(p.get("score") or 0)
                if t not in posts_sample:
                    posts_sample[t] = p["url"]
    out = []
    for ticker, mentions in counter.most_common(40):
        out.append({
            "ticker": ticker,
            "mentions": mentions,
            "total_score": score_sum[ticker],
            "sample_url": posts_sample.get(ticker),
        })
    return out


# ════════════════════════════════════════════════════════════════════
# STOCKTWITS — public symbol stream (sentiment)
# ════════════════════════════════════════════════════════════════════
STOCKTWITS_SYMBOL = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"
STOCKTWITS_TRENDING = "https://api.stocktwits.com/api/2/trending/symbols.json"


def stocktwits_symbol_sentiment(symbol):
    """Pull recent messages for a symbol and compute bull/bear ratio."""
    cache_key = ("stwits", symbol.upper())
    hit = _cget(cache_key)
    if hit is not None:
        return hit
    try:
        r = requests.get(STOCKTWITS_SYMBOL.format(sym=symbol.upper()), headers=HEADERS_STWITS, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        msgs = data.get("messages") or []
        bull = bear = 0
        sample_msgs = []
        for m in msgs[:30]:
            ent = (m.get("entities") or {}).get("sentiment") or {}
            basic = ent.get("basic") if isinstance(ent, dict) else None
            if basic == "Bullish":
                bull += 1
            elif basic == "Bearish":
                bear += 1
            if len(sample_msgs) < 5:
                sample_msgs.append({
                    "body": (m.get("body") or "")[:240],
                    "sentiment": basic,
                    "user": (m.get("user") or {}).get("username"),
                    "created_at": m.get("created_at"),
                    "likes": (m.get("likes") or {}).get("total", 0),
                })
        total = bull + bear
        ratio = (bull / total) if total > 0 else None
        out = {
            "symbol": symbol.upper(),
            "messages_total": len(msgs),
            "bullish": bull,
            "bearish": bear,
            "bull_ratio": ratio,
            "sample": sample_msgs,
        }
        _cset(cache_key, out, 300)
        return out
    except Exception as e:
        log.warning("stocktwits %s: %s", symbol, e)
        return None


def stocktwits_trending(limit=20):
    """Trending tickers on StockTwits."""
    hit = _cget(("stwits_trending", limit))
    if hit is not None:
        return hit
    try:
        r = requests.get(STOCKTWITS_TRENDING, headers=HEADERS_STWITS, timeout=15)
        if r.status_code != 200:
            return []
        sym = r.json().get("symbols") or []
        out = [{
            "symbol": s.get("symbol"),
            "title": s.get("title"),
            "id": s.get("id"),
        } for s in sym[:limit]]
        _cset(("stwits_trending", limit), out, 300)
        return out
    except Exception as e:
        log.warning("stocktwits trending: %s", e)
        return []
