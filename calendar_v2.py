"""
calendar_v2.py — NASDAQ public calendar via finance_calendars.
Provides earnings, IPO, dividend, and split calendars for the entire US market
without an API key.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("augur.calendar_v2")


def _today_key() -> str:
    """ISO date string used to invalidate per-day caches at the date roll.

    Without including today's date in the cache key, the static
    ("earn_today",) / ("div_today",) / ("splits_today",) keys would serve
    yesterday's NASDAQ results for up to TTL (30 min) past midnight UTC.
    Anchored to UTC so the roll happens at a predictable moment.
    """
    return datetime.now(timezone.utc).date().isoformat()

_cache = {}
_lock = threading.Lock()


def _cached(key, ttl, builder):
    with _lock:
        v = _cache.get(key)
        if v and v[1] > time.time():
            return v[0]
    try:
        out = builder()
    except Exception as e:
        log.warning("calendar_v2 %s: %s", key, e)
        return None
    with _lock:
        _cache[key] = (out, time.time() + ttl)
    return out


def _df_to_records(df, max_rows=400):
    if df is None:
        return []
    try:
        # finance_calendars returns pandas DataFrames keyed by symbol
        records = []
        for sym, row in df.head(max_rows).iterrows():
            rec = {"symbol": sym}
            for k, v in row.items():
                rec[str(k).lower().replace(" ", "_")] = (
                    None if v is None or (isinstance(v, float) and v != v) else v
                )
            records.append(rec)
        return records
    except Exception as e:
        log.warning("df_to_records: %s", e)
        return []


def earnings_today():
    def _build():
        from finance_calendars import finance_calendars as fc
        return _df_to_records(fc.get_earnings_today())
    return _cached(("earn_today", _today_key()), 1800, _build)


def earnings_for(date_str):
    """date_str: YYYY-MM-DD"""
    def _build():
        from finance_calendars import finance_calendars as fc
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return _df_to_records(fc.get_earnings_by_date(d))
    return _cached(("earn", date_str), 1800, _build)


def ipos_this_month():
    def _build():
        from finance_calendars import finance_calendars as fc
        return _df_to_records(fc.get_priced_ipos_this_month())
    return _cached(("ipos_priced",), 21600, _build)


def upcoming_ipos():
    def _build():
        from finance_calendars import finance_calendars as fc
        return _df_to_records(fc.get_upcoming_ipos_this_month())
    return _cached(("ipos_up",), 21600, _build)


def dividends_today():
    def _build():
        from finance_calendars import finance_calendars as fc
        return _df_to_records(fc.get_dividends_today())
    return _cached(("div_today", _today_key()), 1800, _build)


def splits_today():
    def _build():
        from finance_calendars import finance_calendars as fc
        return _df_to_records(fc.get_splits_today())
    return _cached(("splits_today", _today_key()), 1800, _build)
