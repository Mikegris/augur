"""
FRED (Federal Reserve Economic Data) — macro indicator client.

Hits fred.stlouisfed.org/graph/fredgraph.csv directly, which serves any
series as CSV with no auth required. Avoids the official FRED API and its
~$0/yr key-registration friction — same data, no setup.

Series IDs of interest:
    CPIAUCSL   — Consumer Price Index (urban, SA, monthly)
    UNRATE     — Unemployment rate (monthly)
    M2SL       — M2 money supply (monthly, $B)
    GDP        — Nominal GDP (quarterly, $B)
    FEDFUNDS   — Effective federal funds rate (monthly)
    RSAFS      — Retail and food services sales (monthly)
    PCE        — Personal consumption expenditures (monthly)
    PAYEMS     — All-employees, nonfarm payrolls (monthly)
    DFII10     — 10y TIPS real yield (daily)
    T10Y2Y     — 10y minus 2y treasury spread (daily, recession indicator)
    DCOILWTICO — WTI crude oil price (daily)
    UMCSENT    — UMich consumer sentiment (monthly)

Browse the full catalog at https://fred.stlouisfed.org/categories.
"""

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime

import requests

log = logging.getLogger("augur.fred")

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
HEADERS = {"User-Agent": "AUGUR/1.0 wealth-tracker"}

# In-process cache — these series update at most daily, often monthly.
_CACHE: dict = {}
DEFAULT_TTL = 6 * 3600  # 6 hours

# Curated list with display labels — exposed at /api/macro/fred/series-list
SERIES_CATALOG = [
    {"id": "CPIAUCSL",   "label": "CPI",                  "category": "inflation",  "freq": "monthly"},
    {"id": "UNRATE",     "label": "Unemployment Rate",    "category": "labor",      "freq": "monthly"},
    {"id": "PAYEMS",     "label": "Nonfarm Payrolls",     "category": "labor",      "freq": "monthly"},
    {"id": "FEDFUNDS",   "label": "Fed Funds Rate",       "category": "rates",      "freq": "monthly"},
    {"id": "DFII10",     "label": "10Y Real Yield",       "category": "rates",      "freq": "daily"},
    {"id": "T10Y2Y",     "label": "10Y-2Y Spread",        "category": "rates",      "freq": "daily"},
    {"id": "M2SL",       "label": "M2 Money Supply",      "category": "monetary",   "freq": "monthly"},
    {"id": "GDP",        "label": "Nominal GDP",          "category": "growth",     "freq": "quarterly"},
    {"id": "PCE",        "label": "PCE",                  "category": "consumer",   "freq": "monthly"},
    {"id": "RSAFS",      "label": "Retail Sales",         "category": "consumer",   "freq": "monthly"},
    {"id": "UMCSENT",    "label": "UMich Consumer Sent.", "category": "consumer",   "freq": "monthly"},
    {"id": "DCOILWTICO", "label": "WTI Crude",            "category": "commodity",  "freq": "daily"},
]

ALLOWED_IDS = {s["id"] for s in SERIES_CATALOG}


def _cache_get(key):
    hit = _CACHE.get(key)
    if not hit:
        return None
    val, expires = hit
    if time.time() > expires:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_set(key, val, ttl):
    _CACHE[key] = (val, time.time() + ttl)


def fetch_series(series_id: str, observations: int = 60) -> dict:
    """Fetch a FRED series as a list of {date, value} points.

    `observations`: how many recent points to return (default 60 — five
    years of monthly data, two months of daily). FRED returns the full
    history; we trim client-side.
    """
    series_id = series_id.upper()
    if series_id not in ALLOWED_IDS:
        # Allow arbitrary series IDs but log it — FRED has ~800k of them.
        log.info("fetching uncurated FRED series: %s", series_id)

    cache_key = ("fred", series_id, observations)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            FRED_CSV_URL, params={"id": series_id}, headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        log.warning("FRED fetch failed for %s: %s", series_id, e)
        return {"series_id": series_id, "points": [], "error": str(e)}

    points: list[dict] = []
    reader = csv.reader(io.StringIO(resp.text))
    header = next(reader, None)
    if not header or len(header) < 2:
        return {"series_id": series_id, "points": [], "error": "empty CSV"}

    for row in reader:
        if len(row) < 2:
            continue
        date_str, val_str = row[0], row[1]
        # FRED uses '.' for missing observations
        if val_str in (".", "", "NA"):
            continue
        try:
            points.append({"date": date_str, "value": float(val_str)})
        except ValueError:
            continue

    # Keep the most-recent `observations` points (FRED returns oldest-first)
    points = points[-observations:]
    meta = _series_meta(series_id)
    out = {
        "series_id": series_id,
        "label": meta.get("label", series_id),
        "category": meta.get("category"),
        "frequency": meta.get("freq"),
        "points": points,
        "latest": points[-1] if points else None,
        "latest_date": points[-1]["date"] if points else None,
        "latest_value": points[-1]["value"] if points else None,
    }
    _cache_set(cache_key, out, DEFAULT_TTL)
    return out


def _series_meta(series_id: str) -> dict:
    for s in SERIES_CATALOG:
        if s["id"] == series_id:
            return s
    return {}


def snapshot() -> dict:
    """Return a quick-glance snapshot of every curated series — useful for
    a macro dashboard panel. Latest value + delta vs. previous observation.

    Series fetch in parallel: serially, a cold cache paid up to
    15s × len(catalog) and blew the page/e2e budget; the slowest single
    series now bounds the whole snapshot."""
    def _one(spec):
        try:
            return fetch_series(spec["id"], observations=4)
        except Exception:
            return {}

    try:
        import safe_executor
        fetched = safe_executor.parallel_map(
            _one, list(SERIES_CATALOG), max_workers=6,
            thread_name_prefix="fred-snap")
    except Exception:
        fetched = [_one(s) for s in SERIES_CATALOG]

    out = []
    for s, series in zip(SERIES_CATALOG, fetched):
        series = series or {}
        pts = series.get("points") or []
        latest = pts[-1] if pts else None
        prev = pts[-2] if len(pts) >= 2 else None
        delta = (latest["value"] - prev["value"]) if (latest and prev) else None
        delta_pct = (delta / prev["value"] * 100) if (delta is not None and prev and prev["value"]) else None
        out.append({
            "id": s["id"],
            "label": s["label"],
            "category": s["category"],
            "frequency": s["freq"],
            "latest_value": latest["value"] if latest else None,
            "latest_date": latest["date"] if latest else None,
            "delta": round(delta, 4) if delta is not None else None,
            "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
        })
    return {"snapshot": out, "fetched_at": datetime.utcnow().isoformat() + "Z"}


def catalog() -> list:
    """Public catalog of curated series."""
    return list(SERIES_CATALOG)
