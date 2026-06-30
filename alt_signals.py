"""
alt_signals.py — Free, no-auth alternative-data signals.

Sources:
    Reddit (r/wallstreetbets, r/stocks, r/investing)  via .json endpoints
    StockTwits public symbol stream                   via api.stocktwits.com
"""

import logging
import random
import re
import threading
import time
from collections import Counter

import requests

log = logging.getLogger("augur.altsignals")


def _retry_after_ttl(resp, base_ttl, lo=30.0, hi=3600.0):
    """Pick a negative-cache TTL for a rate-limited response.

    Honors the upstream `Retry-After` header (seconds, or an HTTP-date),
    CLAMPED into [lo, hi] so a hostile/huge value can't pin us out for hours
    and a tiny one can't cause a retry storm. Adds +/-15% jitter so parallel
    callers that all 429'd at once don't re-hit upstream in lockstep when the
    window expires. Falls back to `base_ttl` when no usable header is present.
    """
    ttl = base_ttl
    try:
        ra = resp.headers.get("Retry-After") if resp is not None else None
        if ra:
            ra = ra.strip()
            if ra.isdigit():
                ttl = float(ra)
            else:
                # HTTP-date form.
                try:
                    from email.utils import parsedate_to_datetime
                    when = parsedate_to_datetime(ra)
                    if when is not None:
                        delta = when.timestamp() - time.time()
                        if delta > 0:
                            ttl = delta
                except Exception:
                    ttl = base_ttl
    except Exception:
        ttl = base_ttl
    ttl = max(lo, min(hi, ttl))
    ttl *= random.uniform(0.85, 1.15)  # jitter
    return ttl

UA = "AUGUR/1.0 (+local research)"
HEADERS_REDDIT   = {"User-Agent": UA}
HEADERS_STWITS   = {"User-Agent": UA, "Accept": "application/json"}

# ── TTL cache ──────────────────────────────────────────────────────
_cache = {}
_lock = threading.Lock()

# Sentinel for a NEGATIVE cache entry ("we already tried and got nothing").
# A bare None can't be cached usefully: _cget couldn't tell a cached-None from
# a plain miss, so it returned None either way and every caller re-hit the
# (rate-limited) upstream regardless — the negative cache was inert. Callers
# store this sentinel instead and translate it back to None on read.
_NEG = {"__neg_cache__": True}

# Distinct sentinel for a FETCH FAILURE (rate-limit / network error) as opposed
# to a genuine empty/no-data result. Keeping these separate lets callers/tests
# tell "upstream is throttling us" apart from "we asked and there really was
# nothing", and lets us pick a Retry-After-aware TTL only for the failure case.
_FAIL = {"__fetch_failed__": True}


def _cget(k):
    """Return the cached value, the _NEG sentinel for a cached negative, or
    None on a genuine miss/expiry. Callers that negative-cache must map _NEG
    back to their own no-data return value."""
    with _lock:
        v = _cache.get(k)
        if v is not None and v[1] > time.time():
            return v[0]
        # Evict an expired entry on read so the map stays bounded by liveness,
        # not only by the size-triggered sweep in _cset. Keeps one-shot keys
        # (and expired _NEG sentinels) from lingering indefinitely.
        if v is not None:
            _cache.pop(k, None)
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
# Distinguish "$F" / "$T" style cashtags (always a ticker, even single-letter)
# from bare letters in a sentence (almost never a ticker).
_CASHTAG_PATTERN = re.compile(r"\$([A-Z]{1,5})\b")
_TICKER_PATTERN = re.compile(r"\b([A-Z]{2,5})\b")
# Single-letter NYSE tickers actually traded; allowed only via the $-cashtag
# path so we don't get false positives from sentence starts.
_SINGLE_LETTER_TICKERS = {"F", "T", "V", "C", "M", "X", "K", "O", "B", "D", "S", "Z"}
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
    if hit is _FAIL:
        return []  # cached fetch-failure — surface no-data without re-hitting
    if hit is not None:
        return hit
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json"
    try:
        r = requests.get(url, headers=HEADERS_REDDIT, params={"limit": min(limit, 100)}, timeout=15)
        # 403/404 = subreddit went private or was banned; 429 = rate limited.
        # Cache the result so we don't hammer Reddit on every idea generator /
        # dashboard tick — they all run reddit_ticker_mentions which sweeps the
        # full default subreddit list in a tight loop. A 429 is a FETCH FAILURE
        # (store _FAIL, honor Retry-After + jitter); 403/404/451 are genuine
        # "this subreddit has no data for us" results (store []).
        if r.status_code == 429:
            _cset(cache_key, _FAIL, _retry_after_ttl(r, 1800))
            return []
        if r.status_code in (403, 404, 451):
            _cset(cache_key, [], 3600)
            return []
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
        # Cache the FAILURE (distinct from no-data) for a short, jittered window
        # so transient network blips don't lock us out and parallel callers
        # don't all retry in lockstep.
        _cset(cache_key, _FAIL, _retry_after_ttl(None, 120, lo=30.0, hi=300.0))
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
            # 2-5 letter all-caps tokens with word boundaries
            for m in _TICKER_PATTERN.finditer(text):
                t = m.group(1)
                if t in _NOISE:
                    continue
                seen.add(t)
            # Cashtag form ($F, $T) catches single-letter NYSE tickers that
            # the bare-token regex deliberately skips to avoid sentence-start
            # false positives.
            for m in _CASHTAG_PATTERN.finditer(text):
                t = m.group(1)
                if t in _NOISE:
                    continue
                if len(t) == 1 and t not in _SINGLE_LETTER_TICKERS:
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
    if hit is _NEG or hit is _FAIL:
        return None  # cached negative/failure — don't re-hit the rate-limited API
    if hit is not None:
        return hit
    try:
        r = requests.get(STOCKTWITS_SYMBOL.format(sym=symbol.upper()), headers=HEADERS_STWITS, timeout=15)
        if r.status_code != 200:
            # Stocktwits aggressively rate-limits (~200/hr without auth).
            # Cache the failure so idea_generator's parallel pool calls
            # don't retry on every page load — that's how we end up locked
            # out for hours at a time. A 429 is a FETCH FAILURE (store _FAIL,
            # honor Retry-After + jitter); other non-200s store _NEG.
            if r.status_code == 429:
                _cset(cache_key, _FAIL, _retry_after_ttl(r, 1800))
            else:
                _cset(cache_key, _NEG, 600)
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
        _cset(cache_key, _FAIL, _retry_after_ttl(None, 120, lo=30.0, hi=300.0))
        return None


def stocktwits_trending(limit=20):
    """Trending tickers on StockTwits."""
    cache_key = ("stwits_trending", limit)
    hit = _cget(cache_key)
    if hit is _FAIL:
        return []
    if hit is not None:
        return hit
    try:
        r = requests.get(STOCKTWITS_TRENDING, headers=HEADERS_STWITS, timeout=15)
        if r.status_code != 200:
            if r.status_code == 429:
                _cset(cache_key, _FAIL, _retry_after_ttl(r, 1800))
            else:
                _cset(cache_key, [], 600)
            return []
        sym = r.json().get("symbols") or []
        out = [{
            "symbol": s.get("symbol"),
            "title": s.get("title"),
            "id": s.get("id"),
        } for s in sym[:limit]]
        _cset(cache_key, out, 300)
        return out
    except Exception as e:
        log.warning("stocktwits trending: %s", e)
        _cset(cache_key, _FAIL, _retry_after_ttl(None, 120, lo=30.0, hi=300.0))
        return []
