"""
CFTC Commitments of Traders — weekly futures positioning data.

Pulled from publicreporting.cftc.gov via Socrata Open Data — JSON, keyless,
no rate limits worth worrying about for personal use. Two datasets:

  - srt6-5q2f  "Legacy_All"   — commodity contracts (oil, gold, wheat...)
  - udgc-27he  "TFF_All"      — financial futures (S&P, UST, USD, FX)

Each weekly row breaks down open interest across trader categories:
  commercial / non-commercial / non-reportable (legacy)
  dealer / asset manager / leveraged / other reportable (TFF)

Net-long delta vs prior week is the headline signal: when leveraged funds
flip from net-short to net-long Treasuries, that's a meaningful shift.

Curated contracts of interest live in `WATCHED_CONTRACTS` — same idea as
fred_data.SERIES_CATALOG: a manageable set with good signal.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

import requests

log = logging.getLogger("augur.cftc")

CFTC_BASE = "https://publicreporting.cftc.gov/resource"
HEADERS = {"User-Agent": "AUGUR/1.0 wealth-tracker", "Accept": "application/json"}

# (dataset_id, contract_name_pattern, friendly_label)
# Pattern matches CFTC's `contract_market_name` field via SoQL LIKE.
WATCHED_CONTRACTS = [
    # Financial futures (TFF — Traders in Financial Futures)
    ("udgc-27he", "E-MINI S&P 500",          "S&P 500 E-mini",     "equity"),
    ("udgc-27he", "NASDAQ-100 E-MINI",       "Nasdaq E-mini",      "equity"),
    ("udgc-27he", "UST 10Y NOTE",            "10Y Treasury",       "rates"),
    ("udgc-27he", "UST 2Y NOTE",             "2Y Treasury",        "rates"),
    ("udgc-27he", "UST BOND",                "30Y Treasury Bond",  "rates"),
    ("udgc-27he", "USD INDEX",               "USD Index",          "fx"),
    ("udgc-27he", "EURO FX",                 "EUR/USD",            "fx"),
    ("udgc-27he", "JAPANESE YEN",            "JPY/USD",            "fx"),
    # Legacy (commodity) contracts
    ("srt6-5q2f", "CRUDE OIL, LIGHT SWEET",  "WTI Crude",          "energy"),
    ("srt6-5q2f", "GOLD",                    "Gold",               "metals"),
    ("srt6-5q2f", "SILVER",                  "Silver",             "metals"),
    ("srt6-5q2f", "WHEAT-SRW",               "Wheat",              "ag"),
    ("srt6-5q2f", "CORN",                    "Corn",               "ag"),
    ("srt6-5q2f", "COPPER- HIGH GRADE",      "Copper",             "metals"),
    ("srt6-5q2f", "NAT GAS NYME",            "Natural Gas",        "energy"),
]

_CACHE: dict = {}
DEFAULT_TTL = 12 * 3600  # weekly data — 12h is plenty


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


def _safe_int(x) -> Optional[int]:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _fetch_latest_two(dataset_id: str, contract_pattern: str) -> list:
    """Return the two most-recent weekly rows for a contract — used to
    compute week-over-week deltas."""
    cache_key = ("cftc", dataset_id, contract_pattern)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        # SoQL: filter on contract name + order by date desc, limit 2.
        # Escape single quotes in `contract_pattern` (SoQL string literal
        # escape is doubling the quote, same as ANSI SQL). Patterns are
        # hardcoded today, but defending the injection vector now means a
        # future caller threading user input through here won't break the
        # query or open a SoQL injection.
        safe_pattern = contract_pattern.replace("'", "''")
        url = f"{CFTC_BASE}/{dataset_id}.json"
        params = {
            "$where": f"contract_market_name like '%{safe_pattern}%'",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 2,
        }
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        rows = resp.json()
        if isinstance(rows, dict) and rows.get("error"):
            log.debug("cftc returned error for %s: %s", contract_pattern, rows.get("message"))
            return []
        _cache_set(cache_key, rows, DEFAULT_TTL)
        return rows
    except Exception as e:
        log.debug("cftc fetch failed for %s/%s: %s", dataset_id, contract_pattern, e)
        return []


def _net_position(row: dict, dataset_id: str):
    """Compute the headline net long-short position for a row, picking the
    right trader category based on which dataset it came from. Field
    names differ between datasets — TFF uses `_positions_long` (no _all),
    Legacy uses `_positions_long_all`."""
    if dataset_id == "udgc-27he":  # TFF — financial futures
        # Leveraged funds (hedge funds) — the most informative cohort
        long_  = _safe_int(row.get("lev_money_positions_long"))
        short_ = _safe_int(row.get("lev_money_positions_short"))
        category = "leveraged_funds"
    else:  # Legacy — commodities
        long_  = _safe_int(row.get("noncomm_positions_long_all"))
        short_ = _safe_int(row.get("noncomm_positions_short_all"))
        category = "non_commercial"
    if long_ is None or short_ is None:
        return None, None, category
    return long_ - short_, (long_, short_), category


def snapshot() -> dict:
    """Return positioning summary across every WATCHED_CONTRACTS row, with
    week-over-week delta. Used to power the Macro view's CFTC panel.

    Fetches each contract in parallel — Socrata round-trips are ~1-2s each,
    so a serial loop over 15 contracts blocked the macro endpoint for
    ~20-30s on cold cache. Bounded concurrency keeps us comfortably under
    Socrata's per-IP throttle.
    """
    from concurrent.futures import ThreadPoolExecutor

    def _one(entry):
        dataset_id, pattern, label, category = entry
        rows = _fetch_latest_two(dataset_id, pattern)
        if not rows:
            return None
        latest = rows[0]
        prior  = rows[1] if len(rows) > 1 else None

        net_l, breakdown_l, cohort = _net_position(latest, dataset_id)
        net_p, _,            _      = _net_position(prior, dataset_id) if prior else (None, None, None)
        delta = (net_l - net_p) if (net_l is not None and net_p is not None) else None
        oi = _safe_int(latest.get("open_interest_all"))

        return {
            "contract": pattern,
            "label": label,
            "category": category,
            "trader_cohort": cohort,
            "report_date": (latest.get("report_date_as_yyyy_mm_dd") or "")[:10],
            "open_interest": oi,
            "long": breakdown_l[0] if breakdown_l else None,
            "short": breakdown_l[1] if breakdown_l else None,
            "net": net_l,
            "net_prior": net_p,
            "net_delta_wow": delta,
            "net_pct_of_oi": round(net_l / oi * 100, 2) if (net_l is not None and oi) else None,
        }

    # Preserve WATCHED_CONTRACTS order in the output so the UI grid stays
    # stable across reloads.
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_one, WATCHED_CONTRACTS))
    out = [r for r in results if r is not None]

    return {
        "snapshot": out,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "source": "publicreporting.cftc.gov",
    }
