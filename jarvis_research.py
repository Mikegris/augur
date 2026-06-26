"""
jarvis_research — Jarvis's OPEN-WORLD research layer.

The rest of Jarvis is closed-world: every tool keys on the user's portfolio
or a tradeable ticker. That makes it blind to anything without a ticker — a
private company (SpaceX), an upcoming IPO, a Fed decision, a theme, a crypto
project pre-listing, "what's happening with X". This module gives Jarvis the
ability to look OUTSIDE its own database:

  • web_research(query)        — native OpenAI web search (Responses API,
                                 through the user's key). Synthesized answer
                                 + source citations. The catch-all.
  • search_news(query)         — GDELT global news by topic/entity (no key,
                                 no ticker needed).
  • news_sentiment(query)      — GDELT tone timeline for a topic.
  • search_sec_filings(query)  — SEC EDGAR full-text search by COMPANY NAME
                                 (finds S-1s / prospectuses for any filer).
  • search_hacker_news(query)  — HN search — tech/retail buzz, IPO chatter.

Everything is cached, fail-open (returns a note, never raises), and the
LLM-backed web_research counts against the daily AI cap. Python 3.9
compatible.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger("augur.jarvis_research")

try:
    import cache_store
except Exception:  # pragma: no cover
    cache_store = None

_WEB_TTL = 1800       # 30 min — news/rumor questions move, but not by the second
_NEWS_TTL = 900
_SEC_TTL = 3600
_HN_TTL = 1800


def _cache(key, ttl, fn):
    if cache_store is None:
        return fn()
    return cache_store.coalesce(key, ttl, fn)


# ─── web_research — the catch-all (OpenAI native web search) ──────────────────

def _web_research_uncached(query: str) -> Dict[str, Any]:
    import ai_summarizer
    key = ai_summarizer.get_openai_key()
    if not key:
        return {"note": "web search needs an OpenAI key (Settings → API Keys). "
                        "Try search_news for keyless topic news instead."}
    if ai_summarizer._cap_exceeded():
        return {"note": "daily AI budget reached — web search paused until reset."}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=40)
        # Responses API with the web_search tool — synthesized answer + cited
        # sources. Instruct it to stay current and grounded.
        # Route through the shared governor so this shares the process-wide
        # concurrency cap + RPM pacing + cooperative 429/5xx backoff that the
        # rest of the app's OpenAI calls are protected by.
        resp = ai_summarizer.governed_call(lambda: client.responses.create(
            model=ai_summarizer.MODEL_HEAVY,
            tools=[{"type": "web_search"}],
            input=(
                "You are JARVIS, an investing research copilot. Research this "
                "and answer in 2-4 tight sentences, grounded in what you find, "
                "current as of today. No investment advice — facts and framing "
                "only. Question: " + query),
        ))
        # The request was billed the moment governed_call returned, regardless
        # of downstream parsing (empty text, missing citations). Record it BEFORE
        # extracting output so an empty/partial response can't under-count spend
        # against the daily cap (mirrors jarvis_semmem.embed's ordering).
        ai_summarizer._record_ai_call()
        text = (getattr(resp, "output_text", None) or "").strip()
        citations = _extract_citations(resp)
        if not text:
            return {"note": "web search returned nothing usable."}
        return {"answer": text, "citations": citations[:6]}
    except Exception as e:
        log.debug("web_research failed for %r: %s", query, e)
        msg = str(e)
        if "web_search" in msg or "tool" in msg.lower():
            return {"note": "web search isn't available on this OpenAI account/model."}
        # Network / rate-limit is transient — flag it so cache_store doesn't
        # freeze the failure for the full TTL.
        return {"error": "web_transient",
                "note": "web search failed (network or rate limit) — try again shortly."}


def _extract_citations(resp: Any) -> List[Dict[str, str]]:
    """Pull url_citation annotations out of a Responses API result."""
    out: List[Dict[str, str]] = []
    seen = set()
    try:
        for item in (getattr(resp, "output", None) or []):
            for content in (getattr(item, "content", None) or []):
                for ann in (getattr(content, "annotations", None) or []):
                    url = getattr(ann, "url", None)
                    if url and url not in seen:
                        seen.add(url)
                        out.append({"title": (getattr(ann, "title", "") or url)[:120],
                                    "url": url})
    except Exception:
        pass
    return out


def web_research(query: str) -> Dict[str, Any]:
    query = (query or "").strip()[:300]
    if not query:
        return {"note": "empty query"}
    return _cache(("jarvis_web", query.lower()), _WEB_TTL,
                  lambda: _web_research_uncached(query))


# ─── search_news — GDELT topic/entity news (keyless) ─────────────────────────

def search_news(query: str, days: int = 14) -> Dict[str, Any]:
    query = (query or "").strip()[:200]
    if not query:
        return {"note": "empty query"}
    days = max(1, min(int(days or 14), 60))

    def _fetch():
        try:
            import data_sources
            arts = data_sources.gdelt_articles(query, max_records=12,
                                               timespan="{}d".format(days))
        except Exception as e:
            log.debug("search_news failed: %s", e)
            return {"error": "news_unavailable",
                    "note": "news search unavailable right now."}
        if not arts:
            return {"query": query, "articles": [],
                    "note": "no recent news matched — try a broader phrasing."}
        return {"query": query,
                "articles": [{"title": a.get("title"), "domain": a.get("domain"),
                              "url": a.get("url"), "date": a.get("seendate"),
                              "tone": a.get("tone")} for a in arts[:10]]}

    return _cache(("jarvis_news", query.lower(), days), _NEWS_TTL, _fetch)


def news_sentiment(query: str) -> Dict[str, Any]:
    query = (query or "").strip()[:200]
    if not query:
        return {"note": "empty query"}

    def _fetch():
        try:
            import data_sources
            series = data_sources.gdelt_tone_timeline(query, timespan="3w")
        except Exception:
            series = []
        if not series:
            return {"query": query, "note": "no sentiment data for that topic."}
        # Sort chronologically by the GDELT date field (zero-padded
        # YYYYMMDD... sorts lexically = chronologically) so the trend isn't
        # inverted if the feed returns newest-first.
        ordered = sorted((s for s in series if s.get("value") is not None),
                         key=lambda s: str(s.get("date") or ""))
        vals = [s["value"] for s in ordered]
        avg = round(sum(vals) / len(vals), 2) if vals else None
        trend = None
        if len(vals) >= 4:
            first, last = vals[0], vals[-1]
            trend = ("improving" if last > first
                     else "deteriorating" if last < first else "flat")
        return {"query": query, "avg_tone": avg, "trend": trend,
                "points": len(vals),
                "note": "GDELT tone: positive = upbeat coverage, negative = downbeat."}

    return _cache(("jarvis_tone", query.lower()), _NEWS_TTL, _fetch)


# ─── search_sec_filings — EDGAR full-text search by company NAME ──────────────

_EFTS = "https://efts.sec.gov/LATEST/search-index"


def search_sec_filings(query: str, forms: Optional[str] = None) -> Dict[str, Any]:
    query = (query or "").strip()[:200]
    if not query:
        return {"note": "empty query"}

    def _fetch():
        import requests
        params = {"q": '"{}"'.format(query)}
        if forms:
            params["forms"] = forms
        try:
            # SEC fair-use requires a routable contact UA; the bare
            # jarvis@augur.local (.local is mDNS-reserved) gets filtered and
            # 429-throttled. Mirror sec_edgar.py / sec_filings_v2.py's UA.
            ua = os.environ.get(
                "AUGUR_SEC_UA", "AUGUR Research Bot research@augur-app.org")
            headers = {"User-Agent": ua}
            r = requests.get(_EFTS, params=params, headers=headers, timeout=15)
            if r.status_code != 200:
                # Transient: carry an "error" key (and keep the envelope to
                # <=3 keys) so cache_store's negative guard won't freeze this
                # as a success for the full TTL.
                return {"error": "edgar_status",
                        "note": "EDGAR returned {} — it rate-limits; try again.".format(r.status_code)}
            hits = (r.json().get("hits", {}) or {}).get("hits", []) or []
        except Exception as e:
            log.debug("search_sec_filings failed: %s", e)
            return {"error": "sec_unavailable",
                    "note": "SEC search unavailable right now."}
        out = []
        for h in hits[:8]:
            src = h.get("_source", {}) or {}
            disp = src.get("display_names") or []
            out.append({
                # file_type is the exhibit type and root_form doesn't exist
                # on these hits; the actual form lives under "form" with
                # "root_forms" (plural, a list) as the fallback.
                "form": src.get("form") or (src.get("root_forms") or [None])[0],
                "filer": disp[0] if disp else None,
                "filed": src.get("file_date"),
                "id": h.get("_id"),
            })
        if not out:
            return {"query": query, "filings": [],
                    "note": "no SEC filings match — the company may be private / "
                            "not yet a filer (e.g. pre-IPO)."}
        return {"query": query, "filings": out}

    return _cache(("jarvis_sec", query.lower(), forms or ""), _SEC_TTL, _fetch)


# ─── search_hacker_news — tech/retail buzz ───────────────────────────────────

def search_hacker_news(query: str) -> Dict[str, Any]:
    query = (query or "").strip()[:200]
    if not query:
        return {"note": "empty query"}

    def _fetch():
        try:
            import hn_sentiment
            hits = hn_sentiment._algolia_search(query, hours=720, hits=20)
        except Exception as e:
            log.debug("search_hacker_news failed: %s", e)
            return {"error": "hn_unavailable",
                    "note": "HN search unavailable right now."}
        stories = [h for h in hits if h.get("title")][:8]
        if not stories:
            return {"query": query, "stories": [], "note": "no recent HN discussion."}
        return {"query": query,
                "stories": [{"title": h.get("title"),
                             "points": h.get("points"),
                             "comments": h.get("num_comments"),
                             "url": "https://news.ycombinator.com/item?id={}".format(h.get("objectID"))}
                            for h in stories]}

    return _cache(("jarvis_hn", query.lower()), _HN_TTL, _fetch)
