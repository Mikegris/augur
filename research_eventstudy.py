"""
research_eventstudy.py — Event study framework for AUGUR.

For a (symbol, event_type) pair, build the empirical distribution of the
stock's price path in a [-N, +N] trading-day window around every historical
instance of that event type. Useful both backward ("how does AAPL typically
behave around earnings?") and forward ("earnings is in 3 days — here's the
historical distribution of moves").

Supported event types:
  - "earnings":     historical reporting dates from yfinance.Ticker.earnings_dates
                    (yfinance is consulted directly because the existing
                    `earnings.get_earnings_calendar` only returns the next
                    upcoming date, not the past history).
  - "fed_fomc":     hard-coded FOMC meeting calendar published by the Fed.
                    Covers 2010-01-01 .. 2026-12-31. Maintained inside this
                    module so the dependency surface stays small.
  - "opex":         third Friday of every month from 2010-01-01 forward
                    (standard monthly equity-index option expiration).
  - "ex_dividend":  per-symbol, sourced from fetcher.get_dividend_data()
                    ["history"] when present.

Public API:

    event_study(symbol, event_type, window_days=10,
                period_years=10, benchmark="SPY") -> dict

Result shape (see project spec):

    {
      "symbol": "AAPL",
      "event_type": "earnings",
      "window_days": 10,
      "n_events": 38,
      "events": [
        {"date": "...", "return_curve": [...2*window+1 values],
         "cumulative_return_at_end": 2.3},
        ...
      ],
      "average_curve": [...],
      "median_curve":  [...],
      "p25_curve":     [...],
      "p75_curve":     [...],
      "abnormal_return_curve": [...],   # symbol mean - benchmark mean
      "summary": {
        "avg_T0_return_pct": ...,
        "avg_post_event_5d_pct": ...,
        "hit_rate_positive_post": ...,
        "best_window":  {"date": "...", "return": ...},
        "worst_window": {"date": "...", "return": ...},
      },
      "as_of": "YYYY-MM-DD"
    }

Insufficient-event handling: when fewer than 5 usable events are matched,
the function returns an error envelope (still a dict, with `"error"` set).

Python 3.9 compatible.
"""

import datetime
import logging
from typing import List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── tunables ───────────────────────────────────────────────────────────────
_MIN_EVENTS = 5
_CACHE_TTL = 24 * 3600        # 24h — historical event studies barely change
_BENCHMARK_DEFAULT = "SPY"

# ── FOMC meeting calendar (Fed-published) ─────────────────────────────────
# Source: federalreserve.gov/monetarypolicy/fomccalendars.htm
# Dates correspond to the *last* (typically Wednesday) day of each meeting,
# which is when the policy statement is released and markets react.
# Entries from 2026 onward use the published forward schedule.
_FOMC_DATES: List[str] = [
    # 2010
    "2010-01-27", "2010-03-16", "2010-04-28", "2010-06-23", "2010-08-10",
    "2010-09-21", "2010-11-03", "2010-12-14",
    # 2011
    "2011-01-26", "2011-03-15", "2011-04-27", "2011-06-22", "2011-08-09",
    "2011-09-21", "2011-11-02", "2011-12-13",
    # 2012
    "2012-01-25", "2012-03-13", "2012-04-25", "2012-06-20", "2012-08-01",
    "2012-09-13", "2012-10-24", "2012-12-12",
    # 2013
    "2013-01-30", "2013-03-20", "2013-05-01", "2013-06-19", "2013-07-31",
    "2013-09-18", "2013-10-30", "2013-12-18",
    # 2014
    "2014-01-29", "2014-03-19", "2014-04-30", "2014-06-18", "2014-07-30",
    "2014-09-17", "2014-10-29", "2014-12-17",
    # 2015
    "2015-01-28", "2015-03-18", "2015-04-29", "2015-06-17", "2015-07-29",
    "2015-09-17", "2015-10-28", "2015-12-16",
    # 2016
    "2016-01-27", "2016-03-16", "2016-04-27", "2016-06-15", "2016-07-27",
    "2016-09-21", "2016-11-02", "2016-12-14",
    # 2017
    "2017-02-01", "2017-03-15", "2017-05-03", "2017-06-14", "2017-07-26",
    "2017-09-20", "2017-11-01", "2017-12-13",
    # 2018
    "2018-01-31", "2018-03-21", "2018-05-02", "2018-06-13", "2018-08-01",
    "2018-09-26", "2018-11-08", "2018-12-19",
    # 2019
    "2019-01-30", "2019-03-20", "2019-05-01", "2019-06-19", "2019-07-31",
    "2019-09-18", "2019-10-30", "2019-12-11",
    # 2020 (note: emergency 2020-03-03, 2020-03-15 cuts included)
    "2020-01-29", "2020-03-03", "2020-03-15", "2020-04-29", "2020-06-10",
    "2020-07-29", "2020-09-16", "2020-11-05", "2020-12-16",
    # 2021
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16", "2021-07-28",
    "2021-09-22", "2021-11-03", "2021-12-15",
    # 2022
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15", "2022-07-27",
    "2022-09-21", "2022-11-02", "2022-12-14",
    # 2023
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26",
    "2023-09-20", "2023-11-01", "2023-12-13",
    # 2024
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31",
    "2024-09-18", "2024-11-07", "2024-12-18",
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
    "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026 (per Fed forward schedule)
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
    "2026-09-16", "2026-10-28", "2026-12-09",
]


# In-module event-calendar registry. Public so callers can introspect
# (e.g. for hard-coded test fixtures) without re-parsing.
CALENDAR = {
    "fed_fomc": _FOMC_DATES,
}


# ── helpers ────────────────────────────────────────────────────────────────

def _to_date(d) -> Optional[datetime.date]:
    """Coerce many date-like inputs to a python date or None."""
    if d is None:
        return None
    if isinstance(d, datetime.date) and not isinstance(d, datetime.datetime):
        return d
    if isinstance(d, datetime.datetime):
        return d.date()
    try:
        if hasattr(d, "date"):
            return d.date()
    except Exception:
        pass
    s = str(d)[:10]
    try:
        return datetime.date.fromisoformat(s)
    except Exception:
        return None


def _opex_dates(start: datetime.date, end: datetime.date) -> List[str]:
    """Return list of ISO-date strings — third Fridays in [start, end]."""
    out = []
    # Walk month-by-month
    y, m = start.year, start.month
    while datetime.date(y, m, 1) <= end:
        # Locate the first Friday of (y, m): weekday() Friday == 4.
        first = datetime.date(y, m, 1)
        offset = (4 - first.weekday()) % 7   # 0..6 days until Friday
        third_friday = first + datetime.timedelta(days=offset + 14)
        if start <= third_friday <= end:
            out.append(third_friday.isoformat())
        # next month
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out


def _ex_div_dates(symbol: str) -> List[str]:
    """Pull ex-dividend dates via the existing fetcher.get_dividend_data
    helper. That function returns {"history": [{"date": "...", "amount": ...}]}
    (newest first); the dates there are payment dates as recorded by Yahoo,
    which for most issuers are within a few days of the ex-div date and are
    close enough to drive a stock-price-reaction study at daily resolution.

    If get_dividend_data is unavailable or yields no history, returns []."""
    try:
        import fetcher
        data = fetcher.get_dividend_data(symbol) or {}
    except Exception as e:
        logger.debug("event_study: get_dividend_data(%s) failed: %s", symbol, e)
        return []
    hist = (data or {}).get("history") or []
    out = []
    for row in hist:
        d = _to_date(row.get("date") if isinstance(row, dict) else None)
        if d:
            out.append(d.isoformat())
    return out


def _earnings_dates(symbol: str) -> List[str]:
    """Past earnings reporting dates for a single symbol.

    We use ``yfinance.Ticker(symbol).earnings_dates`` rather than the project's
    own ``earnings.get_earnings_calendar`` because the latter is built to
    surface the next *upcoming* earnings date per symbol — it does not return
    historical announcements. The yfinance DataFrame index carries past dates
    where ``Reported EPS`` is non-null, which is the canonical 'announced'
    marker.

    Limitation: yfinance can throttle / break under bot detection. Callers
    should handle an empty list (this function never raises)."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        ed = t.earnings_dates
    except Exception as e:
        logger.debug("event_study: yfinance earnings_dates(%s) failed: %s", symbol, e)
        return []
    if ed is None:
        return []
    try:
        if ed.empty:
            return []
    except Exception:
        return []

    # Keep only rows that look like announced (Reported EPS present).
    try:
        past = ed[ed["Reported EPS"].notna()] if "Reported EPS" in ed.columns else ed
    except Exception:
        past = ed

    out = []
    for idx in past.index:
        d = _to_date(idx)
        if d:
            out.append(d.isoformat())
    # Deduplicate (yfinance sometimes lists pre-/after-market rows on same day)
    return sorted(set(out))


def _event_dates_for(symbol: str, event_type: str,
                     start: datetime.date, end: datetime.date) -> List[str]:
    """Return event-date strings filtered to [start, end] for given type."""
    et = (event_type or "").lower()
    if et == "earnings":
        raw = _earnings_dates(symbol)
    elif et == "fed_fomc":
        raw = list(_FOMC_DATES)
    elif et == "opex":
        raw = _opex_dates(start, end)
    elif et == "ex_dividend":
        raw = _ex_div_dates(symbol)
    else:
        return []
    out = []
    for s in raw:
        d = _to_date(s)
        if d and start <= d <= end:
            out.append(d.isoformat())
    return sorted(set(out))


def _load_bars_df(symbol: str, period_years: int) -> pd.DataFrame:
    """Fetch daily bars via fetcher.get_chart_data and return a tz-naive
    DataFrame indexed by date with a 'Close' column. Empty DF on failure.

    We use period strings that fetcher understands. yfinance period strings
    above '10y' aren't honored; we cap at '10y' and let the slice handle
    the user-requested window."""
    period = f"{int(max(1, min(period_years, 10)))}y"
    try:
        import fetcher
        bars = fetcher.get_chart_data(symbol, period=period, interval="1d")
    except Exception as e:
        logger.debug("event_study: get_chart_data(%s) failed: %s", symbol, e)
        return pd.DataFrame()

    if not bars:
        return pd.DataFrame()

    try:
        df = pd.DataFrame(bars)
        if "time" not in df.columns or "close" not in df.columns:
            return pd.DataFrame()
        df["Date"] = pd.to_datetime(df["time"], unit="s").dt.tz_localize(None).dt.normalize()
        df = df.set_index("Date").sort_index()
        df["Close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["Close"])
        return df[["Close"]]
    except Exception as e:
        logger.debug("event_study: bars frame build failed for %s: %s", symbol, e)
        return pd.DataFrame()


def _event_window_curve(closes: pd.Series, event_dt: datetime.date,
                        window_days: int) -> Optional[np.ndarray]:
    """Return a length-(2*window+1) array of cumulative returns relative
    to the price at offset -window_days. Returns None when the event sits
    too close to either end of the series for a clean window."""
    if closes is None or closes.empty:
        return None
    ts = pd.Timestamp(event_dt)

    # Locate the *trading day on or before* event_dt — handles weekends/holidays.
    idx_arr = closes.index
    pos = idx_arr.searchsorted(ts, side="right") - 1
    if pos < 0 or pos >= len(closes):
        return None

    lo = pos - window_days
    hi = pos + window_days
    if lo < 0 or hi >= len(closes):
        return None

    seg = closes.iloc[lo : hi + 1].to_numpy(dtype=float)
    if len(seg) != 2 * window_days + 1:
        return None
    base = seg[0]
    if not np.isfinite(base) or base <= 0:
        return None
    curve = (seg / base) - 1.0
    return curve


def _summary_stats(curves: np.ndarray, dates: List[str],
                   window_days: int) -> dict:
    """Compute the headline summary scalars described in the spec docstring."""
    # Index of T0 in a [-W..+W] curve.
    t0 = window_days
    if curves.size == 0:
        return {}
    # Return at T0 (== cumulative return from T-W to T0)
    avg_T0 = float(np.nanmean(curves[:, t0])) * 100.0

    # Post-event 5d performance: T+5 vs T0
    post_idx = min(t0 + 5, curves.shape[1] - 1)
    rel = curves[:, post_idx] - curves[:, t0]
    avg_post5 = float(np.nanmean(rel)) * 100.0
    # NaN-aware hit rate. `np.mean(rel > 0)` treats `nan > 0` as False, so
    # any windows with missing data depress the rate. Filter NaNs first so
    # the rate reflects only events with usable post-event returns.
    finite_rel = rel[np.isfinite(rel)]
    hit = float(np.mean(finite_rel > 0)) if finite_rel.size else 0.0

    # Best/worst individual end-of-window cumulative
    end_rets = curves[:, -1]
    if np.all(np.isnan(end_rets)):
        best = worst = None
    else:
        b_i = int(np.nanargmax(end_rets))
        w_i = int(np.nanargmin(end_rets))
        best = {"date": dates[b_i], "return": round(float(end_rets[b_i]) * 100, 2)}
        worst = {"date": dates[w_i], "return": round(float(end_rets[w_i]) * 100, 2)}

    return {
        "avg_T0_return_pct": round(avg_T0, 3),
        "avg_post_event_5d_pct": round(avg_post5, 3),
        "hit_rate_positive_post": round(hit, 3),
        "best_window": best,
        "worst_window": worst,
    }


def _round_pct_list(arr) -> List[float]:
    """Convert a curve (decimal returns) to a JSON-friendly list of percent
    values rounded to 4 decimal places."""
    out = []
    for v in arr:
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            out.append(None)
        else:
            out.append(round(float(v) * 100.0, 4))
    return out


# ── primary API ────────────────────────────────────────────────────────────

def _compute(symbol: str, event_type: str, window_days: int,
             period_years: int, benchmark: str) -> dict:
    """Core computation, wrapped by event_study() for caching."""
    symbol = (symbol or "").upper().strip()
    benchmark = (benchmark or _BENCHMARK_DEFAULT).upper().strip()
    today = datetime.datetime.now(datetime.timezone.utc).date()
    # Build the lookback start defensively — naively subtracting `period_years`
    # from `today.year` crashes on Feb 29 of a leap year because the target
    # year usually isn't a leap year ("day is out of range for month"). Bump
    # the day down to Feb 28 in that case rather than letting the route error.
    try:
        start = datetime.date(today.year - int(period_years), today.month, today.day)
    except ValueError:
        start = datetime.date(today.year - int(period_years), today.month, 28)

    # 1. Bars
    sym_df = _load_bars_df(symbol, period_years)
    if sym_df.empty:
        return {
            "symbol": symbol, "event_type": event_type, "window_days": window_days,
            "n_events": 0, "error": "Insufficient price history for symbol",
            "as_of": today.isoformat(),
        }

    bench_df = _load_bars_df(benchmark, period_years)
    has_bench = not bench_df.empty

    # Restrict candidate event window to the price-history span (avoid
    # silently dropping events that pre-date the symbol's listing).
    bars_first = sym_df.index.min().date()
    bars_last = sym_df.index.max().date()
    eff_start = max(start, bars_first)
    eff_end = min(today, bars_last)

    # 2. Event dates
    dates = _event_dates_for(symbol, event_type, eff_start, eff_end)
    if not dates:
        return {
            "symbol": symbol, "event_type": event_type, "window_days": window_days,
            "n_events": 0,
            "error": "No historical events found for this type / window",
            "as_of": today.isoformat(),
        }

    # 3. Curves
    sym_curves = []
    bench_curves = []
    kept_dates = []
    sym_closes = sym_df["Close"]
    bench_closes = bench_df["Close"] if has_bench else None

    for ds in dates:
        d = _to_date(ds)
        if not d:
            continue
        c_sym = _event_window_curve(sym_closes, d, window_days)
        if c_sym is None:
            continue
        c_bench = (_event_window_curve(bench_closes, d, window_days)
                   if has_bench else None)
        sym_curves.append(c_sym)
        bench_curves.append(c_bench if c_bench is not None
                            else np.full(2 * window_days + 1, np.nan))
        kept_dates.append(ds)

    n = len(sym_curves)
    if n < _MIN_EVENTS:
        return {
            "symbol": symbol, "event_type": event_type, "window_days": window_days,
            "n_events": n,
            "error": (f"Only {n} usable events found "
                      f"(min {_MIN_EVENTS} required)"),
            "as_of": today.isoformat(),
        }

    sym_arr = np.array(sym_curves, dtype=float)
    bench_arr = np.array(bench_curves, dtype=float)

    avg = np.nanmean(sym_arr, axis=0)
    med = np.nanmedian(sym_arr, axis=0)
    p25 = np.nanpercentile(sym_arr, 25, axis=0)
    p75 = np.nanpercentile(sym_arr, 75, axis=0)

    # Abnormal return: symbol avg curve minus benchmark avg curve. When the
    # benchmark history is missing (e.g. SPY rate-limited), surface NaNs so
    # the UI can hide the band rather than show a misleading zero-line.
    if has_bench and np.any(np.isfinite(bench_arr)):
        bench_avg = np.nanmean(bench_arr, axis=0)
        abnormal = avg - bench_avg
    else:
        abnormal = np.full_like(avg, np.nan)

    # Per-event payload (last-bar cumulative return = curve[-1])
    events = []
    for ds, curve in zip(kept_dates, sym_arr):
        events.append({
            "date": ds,
            "return_curve": _round_pct_list(curve),
            "cumulative_return_at_end": round(float(curve[-1]) * 100.0, 3),
        })

    summary = _summary_stats(sym_arr, kept_dates, window_days)

    return {
        "symbol": symbol,
        "event_type": event_type,
        "window_days": int(window_days),
        "period_years": int(period_years),
        "benchmark": benchmark,
        "n_events": n,
        "events": events,
        "average_curve": _round_pct_list(avg),
        "median_curve": _round_pct_list(med),
        "p25_curve": _round_pct_list(p25),
        "p75_curve": _round_pct_list(p75),
        "abnormal_return_curve": _round_pct_list(abnormal),
        "summary": summary,
        "as_of": today.isoformat(),
    }


def event_study(symbol: str, event_type: str = "earnings",
                window_days: int = 10, period_years: int = 10,
                benchmark: str = _BENCHMARK_DEFAULT) -> dict:
    """Build an event study for ``symbol`` × ``event_type``.

    See module docstring for the return envelope. Cached for 24h via
    ``cache_store.coalesce`` so a noisy UI doesn't refetch on every tab
    switch."""
    symbol = (symbol or "").upper().strip()
    if not symbol:
        return {"error": "No symbol provided"}

    try:
        window_days = int(window_days)
    except Exception:
        window_days = 10
    window_days = max(1, min(window_days, 60))

    try:
        period_years = int(period_years)
    except Exception:
        period_years = 10
    period_years = max(1, min(period_years, 10))

    benchmark = (benchmark or _BENCHMARK_DEFAULT).upper().strip()
    et = (event_type or "earnings").lower()

    key = ("eventstudy", symbol, et, int(window_days),
           int(period_years), benchmark)

    try:
        import cache_store
        return cache_store.coalesce(
            key, _CACHE_TTL,
            lambda: _compute(symbol, et, window_days, period_years, benchmark),
        )
    except Exception as e:
        # If the cache layer is unavailable for any reason, do the work
        # uncached rather than failing the request.
        logger.debug("event_study: cache_store unavailable (%s) — uncached", e)
        return _compute(symbol, et, window_days, period_years, benchmark)


# ── debug entrypoint ──────────────────────────────────────────────────────
if __name__ == "__main__":  # pragma: no cover
    import json
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    et = sys.argv[2] if len(sys.argv) > 2 else "earnings"
    r = event_study(sym, et, window_days=10, period_years=5)
    # Trim per-event list for readability.
    if "events" in r:
        r_summary = dict(r)
        r_summary["events"] = f"<{len(r['events'])} events truncated>"
        print(json.dumps(r_summary, indent=2, default=str))
    else:
        print(json.dumps(r, indent=2, default=str))
