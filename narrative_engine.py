"""
Narrative Velocity Engine -- tracks rate-of-change of media narrative
around a stock.  Detects narrative phases:
EMERGENCE -> ACCELERATION -> CONSENSUS -> EXHAUSTION -> REVERSAL

Not just sentiment: narrative *velocity*.  When financial media shifts
from "growth darling" to "profitability questions", the transition
itself is the signal.
"""

import re
import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import fetcher

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------
_cache = {}
_cache_ts = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 1800  # seconds

# ---------------------------------------------------------------------------
# Narrative bucket keyword definitions
# Each keyword list is compiled into a single regex for fast matching.
# ---------------------------------------------------------------------------
_BUCKET_KEYWORDS = {
    "GROWTH": [
        "revenue growth", "expanding", "new market", "beat estimates",
        "record sales", "strong demand", "accelerating", "upside",
        "top-line growth", "market expansion", "outperform",
    ],
    "PROFITABILITY": [
        "margin", "profit", "cost cutting", "efficiency", "earnings beat",
        "bottom line", "cash flow", "free cash flow", "cost reduction",
        "operating income", "net income",
    ],
    "REGULATORY": [
        "regulation", "antitrust", "sec ", "lawsuit", "compliance",
        "fine", "penalty", "investigation", "regulatory", "legal action",
        "enforcement",
    ],
    "SCANDAL": [
        "fraud", "resignation", "whistleblower", "misconduct",
        "accounting", "restatement", "scandal", "allegation",
        "cover-up", "criminal",
    ],
    "TURNAROUND": [
        "restructuring", "new ceo", "pivot", "recovery", "rebound",
        "comeback", "transformation", "turnaround", "revamp",
        "strategic review",
    ],
    "M_AND_A": [
        "acquisition", "merger", "buyout", "takeover", "deal", "bid",
        "target", "acquir", "merged", "divestiture",
    ],
    "MACRO": [
        "interest rate", "inflation", "recession", "fed ", "tariff",
        "trade war", "gdp", "unemployment", "monetary policy",
        "central bank", "economic slowdown",
    ],
}

# Pre-compile regex patterns for each bucket (word-boundary aware where useful)
_BUCKET_PATTERNS = {}
for _bucket, _keywords in _BUCKET_KEYWORDS.items():
    # Escape keywords for regex safety, join with alternation
    _escaped = [re.escape(kw) for kw in _keywords]
    _BUCKET_PATTERNS[_bucket] = re.compile("|".join(_escaped), re.IGNORECASE)

# Ordered list so tie-breaking favours earlier buckets
_BUCKET_ORDER = [
    "GROWTH", "PROFITABILITY", "REGULATORY", "SCANDAL",
    "TURNAROUND", "M_AND_A", "MACRO",
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_date(date_val):
    """Parse a date value that may be a Unix timestamp or ISO string.

    Returns a datetime (UTC-aware) or None if unparseable.
    """
    if date_val is None:
        return None
    # Unix timestamp (int or float, or string of digits)
    if isinstance(date_val, (int, float)):
        try:
            return datetime.fromtimestamp(date_val, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(date_val, str):
        # Check if it looks like a pure numeric timestamp
        stripped = date_val.strip()
        if stripped.replace(".", "", 1).isdigit():
            try:
                return datetime.fromtimestamp(float(stripped), tz=timezone.utc)
            except (OSError, ValueError, OverflowError):
                return None
        # Try common ISO formats
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(stripped, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
        # Last resort: try fromisoformat (handles many variants)
        try:
            dt = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, AttributeError):
            pass
    return None


def _classify_article(title, summary):
    """Classify a single article into a narrative bucket.

    Combines title + summary, lowercased, then counts keyword hits per
    bucket.  Returns the bucket with the most hits.  Ties broken by
    bucket order.  No hits -> NEUTRAL.
    """
    text = (title or "").lower() + " " + (summary or "").lower()
    best_bucket = "NEUTRAL"
    best_count = 0
    for bucket in _BUCKET_ORDER:
        matches = _BUCKET_PATTERNS[bucket].findall(text)
        count = len(matches)
        if count > best_count:
            best_count = count
            best_bucket = bucket
    return best_bucket


def _build_window(articles, now, days):
    """Build bucket distribution for a time window.

    Returns a dict with buckets, dominant, dominant_pct, article_count.
    """
    cutoff = now - timedelta(days=days)
    window_articles = [a for a in articles if a["dt"] >= cutoff]
    total = len(window_articles)
    if total < 3:
        return {
            "buckets": {},
            "dominant": "INSUFFICIENT DATA",
            "dominant_pct": 0.0,
            "article_count": total,
        }
    buckets = {}
    for a in window_articles:
        b = a["bucket"]
        buckets[b] = buckets.get(b, 0) + 1
    # Tie-break deterministically: highest count, then earliest in _BUCKET_ORDER.
    def _dominant_key(b):
        idx = _BUCKET_ORDER.index(b) if b in _BUCKET_ORDER else len(_BUCKET_ORDER)
        return (buckets[b], -idx)

    dominant = max(buckets, key=_dominant_key)
    dominant_pct = round(buckets[dominant] / total * 100, 1)
    return {
        "buckets": buckets,
        "dominant": dominant,
        "dominant_pct": dominant_pct,
        "article_count": total,
    }


def _compute_velocity(windows):
    """Compute narrative velocity score from window data.

    Compares 7d dominant share vs 14d dominant share.  If dominant bucket
    changed between windows, that signals reversal (negative velocity).

    Returns int clamped to [-100, +100].
    """
    w7 = windows.get("7d", {})
    w14 = windows.get("14d", {})

    # Can't compute if either window has insufficient data
    if w7.get("dominant") == "INSUFFICIENT DATA" or w14.get("dominant") == "INSUFFICIENT DATA":
        return 0

    dominant_7d = w7.get("dominant", "NEUTRAL")
    dominant_14d = w14.get("dominant", "NEUTRAL")
    pct_7d = w7.get("dominant_pct", 0.0)
    pct_14d = w14.get("dominant_pct", 0.0)

    if dominant_7d != dominant_14d:
        # A genuine narrative-to-narrative shift gives negative velocity
        # (the old story is collapsing). A NEUTRAL -> narrative or
        # narrative -> NEUTRAL transition is *emergence/dissipation*, not
        # reversal, so we treat it as positive (a new story building up)
        # or zero (a narrative fading into background) instead.
        if dominant_14d == "NEUTRAL" and dominant_7d != "NEUTRAL":
            velocity = int(pct_7d * 1.0)
        elif dominant_7d == "NEUTRAL":
            velocity = 0
        else:
            velocity = -int(pct_7d * 1.0)
    else:
        velocity = int((pct_7d - pct_14d) * 2)

    # Clamp
    return max(-100, min(100, velocity))


def _detect_phase(windows, velocity, total_count):
    """Detect narrative phase.

    Returns (phase_name, detail_string).
    """
    w7 = windows.get("7d", {})
    w14 = windows.get("14d", {})
    w30 = windows.get("30d", {})

    dom_7 = w7.get("dominant", "INSUFFICIENT DATA")
    dom_14 = w14.get("dominant", "INSUFFICIENT DATA")
    dom_30 = w30.get("dominant", "INSUFFICIENT DATA")
    pct_7 = w7.get("dominant_pct", 0.0)
    pct_14 = w14.get("dominant_pct", 0.0)
    cnt_7 = w7.get("article_count", 0)
    cnt_14 = w14.get("article_count", 0)

    # If main windows are insufficient, default
    if dom_7 == "INSUFFICIENT DATA":
        return ("DEVELOPING", "Insufficient recent articles to determine narrative phase.")

    # REVERSAL: 7d dominant differs from 14d dominant *and* the prior
    # 14d dominant was a real narrative (not NEUTRAL). A NEUTRAL-to-real
    # transition is emergence, not reversal -- bucketing it as REVERSAL
    # incorrectly fires the contrarian "crowded trade" warning when in
    # fact the trade has *just started* getting attention.
    if (
        dom_14 != "INSUFFICIENT DATA"
        and dom_14 != "NEUTRAL"
        and dom_7 != "NEUTRAL"
        and dom_7 != dom_14
    ):
        return (
            "REVERSAL",
            "Narrative shifted from {} (14d) to {} (7d).".format(dom_14, dom_7),
        )

    # CONSENSUS: dominant 7d bucket >55% of articles
    is_consensus = pct_7 > 55

    # EXHAUSTION: consensus + declining volume
    if is_consensus:
        # Normalize 14d count to a 7-day equivalent
        normalized_14d = cnt_14 / 2.0 if cnt_14 > 0 else 0
        if normalized_14d > 0 and cnt_7 < normalized_14d:
            return (
                "EXHAUSTION",
                "{} narrative dominates at {:.0f}% but article volume is declining "
                "({} articles in 7d vs {:.0f} expected).".format(
                    dom_7, pct_7, cnt_7, normalized_14d
                ),
            )
        return (
            "CONSENSUS",
            "{} narrative dominates at {:.0f}% of recent coverage.".format(dom_7, pct_7),
        )

    # ACCELERATION: dominant 7d share growing >5pp vs 14d
    if dom_14 != "INSUFFICIENT DATA" and dom_7 == dom_14 and (pct_7 - pct_14) > 5:
        return (
            "ACCELERATION",
            "{} narrative accelerating: {:.0f}% (7d) vs {:.0f}% (14d).".format(
                dom_7, pct_7, pct_14
            ),
        )

    # EMERGENCE: a bucket appears in 7d that wasn't dominant in 30d, <25% share
    if dom_30 != "INSUFFICIENT DATA" and dom_7 != dom_30 and pct_7 < 25:
        return (
            "EMERGENCE",
            "{} narrative emerging at {:.0f}% (previously {} dominated).".format(
                dom_7, pct_7, dom_30
            ),
        )

    return (
        "DEVELOPING",
        "{} narrative at {:.0f}% -- no strong phase signal yet.".format(dom_7, pct_7),
    )


def _velocity_label(velocity):
    """Convert numeric velocity to human-readable label."""
    if velocity >= 30:
        return "ACCELERATING"
    if velocity <= -30:
        if velocity <= -60:
            return "REVERSING"
        return "DECELERATING"
    return "STABLE"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_narrative(symbol):
    """Analyze narrative velocity for a stock symbol.

    Returns a dict with narrative phase, velocity, windows, and article
    history.  On error returns {"error": str}.
    """
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return {"error": "No symbol provided."}

    # Check cache
    cache_key = "narrative_{}".format(symbol)
    now_ts = time.time()
    with _cache_lock:
        if cache_key in _cache and (now_ts - _cache_ts.get(cache_key, 0)) < _CACHE_TTL:
            logger.debug("Returning cached narrative for %s", symbol)
            return _cache[cache_key]

    try:
        raw_articles = fetcher.get_news(symbol, limit=50)
    except Exception as e:
        logger.error("Failed to fetch news for %s: %s", symbol, e)
        return {"error": "Failed to fetch news: {}".format(str(e))}

    if not raw_articles:
        return {
            "symbol": symbol,
            "dominant_narrative": "NEUTRAL",
            "narrative_phase": "DEVELOPING",
            "velocity_score": 0,
            "velocity_label": "STABLE",
            "contrarian_signal": False,
            "contrarian_detail": None,
            "article_count": 0,
            "windows": {},
            "phase_detail": "No news articles available for analysis.",
            "narrative_history": [],
            "sentiment_distribution": {},
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }

    # Parse and classify
    now = datetime.now(tz=timezone.utc)
    articles = []
    overall_buckets = {}
    for item in raw_articles:
        title = item.get("title", "") or ""
        summary = item.get("summary", "") or ""
        source = item.get("source", "") or ""
        published = item.get("published")
        dt = _parse_date(published)
        if dt is None:
            # Skip undated articles — stamping them "now" put them in every
            # window and inflated recent counts / flipped the phase.
            continue

        bucket = _classify_article(title, summary)
        overall_buckets[bucket] = overall_buckets.get(bucket, 0) + 1
        articles.append({
            "dt": dt,
            "bucket": bucket,
            "title": title,
            "summary": summary,
            "source": source,
            "date_str": dt.strftime("%Y-%m-%d"),
        })

    # Sort newest first
    articles.sort(key=lambda a: a["dt"], reverse=True)
    total_count = len(articles)

    # Build windows
    windows = {
        "7d": _build_window(articles, now, 7),
        "14d": _build_window(articles, now, 14),
        "30d": _build_window(articles, now, 30),
    }

    # Velocity
    velocity = _compute_velocity(windows)
    vlabel = _velocity_label(velocity)

    # Phase detection
    phase, phase_detail = _detect_phase(windows, velocity, total_count)

    # Contrarian signal
    contrarian = phase in ("CONSENSUS", "EXHAUSTION")
    contrarian_detail = None
    if contrarian:
        dom = windows["7d"].get("dominant", "NEUTRAL")
        pct = windows["7d"].get("dominant_pct", 0)
        if phase == "EXHAUSTION":
            contrarian_detail = (
                "High consensus ({} at {:.0f}%) with declining volume -- "
                "narrative exhaustion often precedes reversal.".format(dom, pct)
            )
        else:
            contrarian_detail = (
                "Strong consensus ({} at {:.0f}%) -- when everyone agrees, "
                "the trade may already be crowded.".format(dom, pct)
            )

    # Dominant narrative (7d)
    dominant = windows["7d"].get("dominant", "NEUTRAL")

    # Build narrative history
    history = [
        {
            "date": a["date_str"],
            "bucket": a["bucket"],
            "title": a["title"],
            "source": a["source"],
        }
        for a in articles
    ]

    result = {
        "symbol": symbol,
        "dominant_narrative": dominant,
        "narrative_phase": phase,
        "velocity_score": velocity,
        "velocity_label": vlabel,
        "contrarian_signal": contrarian,
        "contrarian_detail": contrarian_detail,
        "article_count": total_count,
        "windows": windows,
        "phase_detail": phase_detail,
        "narrative_history": history,
        "sentiment_distribution": overall_buckets,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }

    # Cache result
    with _cache_lock:
        _cache[cache_key] = result
        _cache_ts[cache_key] = time.time()

    logger.info(
        "Narrative analysis for %s: phase=%s velocity=%d dominant=%s",
        symbol, phase, velocity, dominant,
    )
    return result
