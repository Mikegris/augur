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
import math
import warnings
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

    WHY (#224): T0 is anchored to the trading day on-or-before the event date,
    but most reporters announce AFTER the close (~16:30 ET) — anchoring T0 to
    the announcement's calendar date measures the close 30 min BEFORE the
    reaction and mislabels the real move as post-event drift. yfinance's index
    carries the announcement time (tz-aware ET), so when the timestamp is at
    or after the 16:00 close we shift the event date to the next calendar day;
    _event_window_curve's "on or before" lookup then lands T0 on the first
    close AFTER the announcement. Timestamps with no time component (midnight
    — time unknown) are left as-is: pre-reaction T0 for those is documented
    behavior, not a claim we can't back.

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
        # AMC reporters: reaction lands on the NEXT session (#224, see above).
        try:
            if getattr(idx, "hour", 0) >= 16:
                idx = idx + pd.Timedelta(days=1)
        except Exception:
            pass
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


# Market-model estimation window: ~250 trading days ending W+1 days before T0.
_EST_WINDOW = 250
_EST_MIN = 60  # need at least this many overlapping est-window obs for OLS


def _t_p_value_two_sided(t_stat: float, dof: int) -> float:
    """Two-sided p-value for a t-statistic under Student-t with `dof` degrees
    of freedom.

    WHY (#222): the cross-sectional CAR t-stat can be built from as few as
    n_mm=5 events (4 dof); a normal-approx erfc p-value there is ~10x too
    small (t=2.8, 4 dof: true p≈0.049 vs erfc≈0.005), flagging borderline
    results as significant. Use the exact Student-t CDF via scipy (bundled —
    sklearn depends on it); only if scipy is somehow unavailable fall back to
    the old normal approximation, which is anti-conservative for tiny dof but
    preserves the previous behavior rather than dropping the stat entirely."""
    if dof < 1:
        return float("nan")
    try:
        from scipy import stats as _scipy_stats
        return float(2.0 * _scipy_stats.t.sf(abs(t_stat), dof))
    except Exception:
        return math.erfc(abs(t_stat) / math.sqrt(2.0))


def _market_model_ar_curve(sym_closes: pd.Series, bench_closes: pd.Series,
                           event_dt: datetime.date,
                           window_days: int) -> Optional[np.ndarray]:
    """Abnormal-return curve for ONE event under the market model.

    WHY (B2): the old "symbol mean − benchmark mean" abnormal return assumes
    β=1, α=0 for every name — that's not an abnormal return, it's a raw
    benchmark-relative difference. Here we estimate (α,β) by OLS of the
    stock's daily simple returns on the benchmark's, over a pre-event
    estimation window [-(W+1)-EST .. -(W+1)] (so the event window itself is
    NOT in the estimation sample), then form

        AR_t = R_sym,t − (α̂ + β̂·R_mkt,t)

    for each day t in [-W, +W] and return the *cumulative* AR curve (CAR
    path) so it is directly comparable to the existing cumulative-return
    curves. Returns None when there isn't enough clean estimation history.
    """
    if (sym_closes is None or sym_closes.empty
            or bench_closes is None or bench_closes.empty):
        return None
    ts = pd.Timestamp(event_dt)
    sidx = sym_closes.index
    pos = sidx.searchsorted(ts, side="right") - 1
    if pos < 0 or pos >= len(sym_closes):
        return None
    lo = pos - window_days
    hi = pos + window_days
    if lo < 0 or hi >= len(sym_closes):
        return None

    # Estimation window: the EST_WINDOW trading days ending just before lo.
    est_hi = lo - 1
    est_lo = est_hi - _EST_WINDOW
    if est_lo < 1:  # need a prior close to form the first return
        return None

    # Build aligned (sym, bench) simple-return frames on shared dates over the
    # union of estimation + event windows.
    # WHY (#221): pct_change()'s default fill_method pads NaNs forward, which
    # fabricates 0% benchmark returns on days the benchmark has no bar (the
    # NaNs created by the reindex below). Those fake zeros survive dropna(),
    # bias β toward 0 and corrupt the event-day ARs — pass fill_method=None so
    # missing days stay NaN and are removed by the existing df.dropna().
    seg_sym = sym_closes.iloc[est_lo - 1: hi + 1]
    sym_ret = seg_sym.pct_change(fill_method=None).dropna()
    # Re-align the benchmark onto the symbol's trading days.
    bench_on_sym = bench_closes.reindex(sym_closes.index).iloc[est_lo - 1: hi + 1]
    bench_ret = bench_on_sym.pct_change(fill_method=None)
    df = pd.DataFrame({"sym": sym_ret, "mkt": bench_ret}).dropna()
    if df.empty:
        return None

    # Split into estimation (dates strictly before the event window) and event.
    # WHY (#220): the event window holds the 2W returns close[-W]→close[+W] —
    # matching the raw cumulative curves, which anchor at the close of day -W.
    # Including the row AT event_start_ts (the return close[-W-1]→close[-W])
    # made the CAR span 2W+1 returns, shifting every point one return early
    # relative to the raw curves it must be directly comparable to. That
    # boundary return belongs to neither window (estimation already ends at
    # est_hi = lo-1), which is fine — it sits inside the exclusion gap.
    event_start_ts = sym_closes.index[lo]
    est = df[df.index < event_start_ts]
    evt = df[df.index > event_start_ts]
    if len(est) < _EST_MIN or len(evt) != (2 * window_days):
        return None

    x = est["mkt"].to_numpy(dtype=float)
    y = est["sym"].to_numpy(dtype=float)
    try:
        Xe = np.column_stack([np.ones_like(x), x])
        coef, *_ = np.linalg.lstsq(Xe, y, rcond=None)
        alpha, beta = float(coef[0]), float(coef[1])
        if not (np.isfinite(alpha) and np.isfinite(beta)):
            return None
    except Exception:
        return None

    r_sym = evt["sym"].to_numpy(dtype=float)
    r_mkt = evt["mkt"].to_numpy(dtype=float)
    ar = r_sym - (alpha + beta * r_mkt)  # 2W daily abnormal returns
    # Prepend the 0.0 baseline at day -W (#220) so the CAR path has length
    # 2W+1 and starts at 0 exactly like the raw cumulative-return curves.
    car = np.concatenate([[0.0], np.cumsum(ar)])
    return car


def _drop_overlapping_events(pairs, min_gap_days: int):
    """Greedily select events whose windows don't overlap (Q12).

    `pairs` is a list of (date_str, curve). Returns the subset, earliest-first,
    keeping an event only if its date is at least `min_gap_days` CALENDAR days
    after the last kept event's date. Events with unparseable dates are kept
    (conservative — we can't prove overlap). Stable / deterministic.
    """
    parsed = []
    for ds, curve in pairs:
        d = _to_date(ds)
        parsed.append((d, ds, curve))
    # Sort by date; undated rows sink to the end and are appended as-is.
    dated = sorted((p for p in parsed if p[0] is not None), key=lambda p: p[0])
    undated = [p for p in parsed if p[0] is None]
    kept = []
    last_kept = None
    for d, ds, curve in dated:
        if last_kept is None or (d - last_kept).days >= min_gap_days:
            kept.append((ds, curve))
            last_kept = d
    for _, ds, curve in undated:
        kept.append((ds, curve))
    return kept


def _summary_stats(curves: np.ndarray, dates: List[str],
                   window_days: int) -> dict:
    """Compute the headline summary scalars described in the spec docstring."""
    # Index of T0 in a [-W..+W] curve.
    t0 = window_days
    if curves.size == 0:
        return {}
    # WHY (Q10): curves[:, t0] is the CUMULATIVE return from T-W to T0 — i.e.
    # the pre-event run-up over the whole left window, not the event-day move.
    # The actual T0 one-day return is the step from T0-1 to T0:
    #   (1 + c_t0) / (1 + c_{t0-1}) - 1
    # which isolates the event-day reaction. (c_{t0-1} exists since t0 = W ≥ 1.)
    if t0 >= 1:
        c_t0 = curves[:, t0]
        c_prev = curves[:, t0 - 1]
        event_day_ret = (1.0 + c_t0) / (1.0 + c_prev) - 1.0
        avg_T0 = float(np.nanmean(event_day_ret)) * 100.0
    else:
        avg_T0 = float(np.nanmean(curves[:, t0])) * 100.0

    # Post-event 5d performance: T+5 vs T0. The +5 horizon is only fully
    # observed when the right window reaches T+5 (window_days >= 5). For
    # smaller windows min() would clamp post_idx to the last column and the
    # "5d" fields would silently report a shorter horizon — so flag those as
    # unavailable (None) rather than mislabel them.
    full_post5 = (t0 + 5) <= (curves.shape[1] - 1)
    post_idx = min(t0 + 5, curves.shape[1] - 1)
    rel = curves[:, post_idx] - curves[:, t0]
    avg_post5 = float(np.nanmean(rel)) * 100.0 if full_post5 else None
    # NaN-aware hit rate. `np.mean(rel > 0)` treats `nan > 0` as False, so
    # any windows with missing data depress the rate. Filter NaNs first so
    # the rate reflects only events with usable post-event returns.
    # WHY (#225): like avg_post5/avg_abs_move above, gate on full_post5 — a
    # window_days < 5 study would otherwise report the T+window hit rate as
    # if it were the standard T+5 stat (the exact mislabeling the sibling
    # fields were set to None for).
    if full_post5:
        finite_rel = rel[np.isfinite(rel)]
        hit = float(np.mean(finite_rel > 0)) if finite_rel.size else 0.0
    else:
        hit = None

    # WHY (S13): magnitude consumers (e.g. synth_catalyst) want the typical
    # ABSOLUTE move around the event. Averaging signed returns and then taking
    # |.| collapses toward 0 for symmetric outcomes (half +8%, half -8% → ~0),
    # badly understating the realized volatility. Instead average the absolute
    # PER-EPISODE move from just-before-event (T0-1) through T+5, so each
    # episode's magnitude counts before any sign cancellation.
    base_idx = t0 - 1 if t0 >= 1 else t0
    if full_post5:
        per_episode_total = (1.0 + curves[:, post_idx]) / (1.0 + curves[:, base_idx]) - 1.0
        finite_total = per_episode_total[np.isfinite(per_episode_total)]
        avg_abs_move = (float(np.mean(np.abs(finite_total))) * 100.0
                        if finite_total.size else None)
    else:
        # T+5 not fully observed (window_days < 5) — don't report a magnitude
        # measured over a silently shortened horizon.
        avg_abs_move = None

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
        "avg_post_event_5d_pct": round(avg_post5, 3) if avg_post5 is not None else None,
        "avg_abs_event_move_pct": round(avg_abs_move, 3) if avg_abs_move is not None else None,
        "hit_rate_positive_post": round(hit, 3) if hit is not None else None,
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
    mm_car_curves = []  # per-event market-model CAR paths (B2), or None
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
        # Market-model abnormal-return path for this single event (B2).
        mm_car = None
        if has_bench:
            try:
                mm_car = _market_model_ar_curve(sym_closes, bench_closes,
                                                d, window_days)
            except Exception as e:
                logger.debug("event_study: market-model AR failed for %s @ %s: %s",
                             symbol, ds, e)
                mm_car = None
        sym_curves.append(c_sym)
        bench_curves.append(c_bench if c_bench is not None
                            else np.full(2 * window_days + 1, np.nan))
        mm_car_curves.append(mm_car)
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
    # Compute BOTH averages over the SAME subset of events (those that have a
    # benchmark window) so abnormal = sym_avg - bench_avg is a like-for-like
    # difference rather than differencing two non-matching event populations.
    bench_mask = has_bench and np.array(
        [np.any(np.isfinite(row)) for row in bench_arr], dtype=bool)
    if has_bench and np.any(bench_mask):
        # An all-NaN column within the matched subset makes nanmean emit a
        # benign "Mean of empty slice" RuntimeWarning and yield NaN; silence
        # it (the NaN is the intended "no data here" sentinel).
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            bench_avg = np.nanmean(bench_arr[bench_mask], axis=0)
            sym_avg_matched = np.nanmean(sym_arr[bench_mask], axis=0)
        abnormal = sym_avg_matched - bench_avg
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

    # ── Market-model abnormal returns + CAR significance (B2) ───────────────
    # Aggregate the per-event market-model CAR paths and test H0: CAR=0 at the
    # end of the window with a cross-sectional t-stat. These are ADDED keys;
    # the legacy `abnormal_return_curve` (sym_avg − bench_avg) is preserved.
    mm_valid_pairs = [(ds, c) for ds, c in zip(kept_dates, mm_car_curves)
                      if c is not None and np.all(np.isfinite(c))]
    mm_valid = [c for _, c in mm_valid_pairs]

    # WHY (Q12): the cross-sectional CAR t-stat assumes the per-event terminal
    # CARs are independent. Two events whose [-W, +W] windows overlap share
    # return days, so their CARs are correlated — counting both as i.i.d.
    # observations shrinks the standard error and OVERSTATES significance. Drop
    # overlapping events (greedy, earliest-first) so the surviving set has
    # non-overlapping windows before computing the t-stat. The curve average
    # (mm_abnormal_curve) still uses every valid event; only the inference set
    # is de-overlapped. Window half-width is `window_days` TRADING days; we
    # approximate the calendar span as window_days * 7/5 days between event
    # dates to guarantee non-overlap.
    min_gap_days = math.ceil(2 * window_days * 7.0 / 5.0)
    indep_pairs = _drop_overlapping_events(mm_valid_pairs, min_gap_days)
    car_indep = [c for _, c in indep_pairs]

    market_model: dict = {
        "available": False,
        "n_events": len(mm_valid),
        "n_events_independent": len(car_indep),
        "method": "market_model_ols_estimation_window",
        "estimation_window_days": _EST_WINDOW,
    }
    mm_abnormal_curve = np.full_like(avg, np.nan)
    if len(mm_valid) >= _MIN_EVENTS:
        car_mat = np.array(mm_valid, dtype=float)  # (n_valid, 2W+1)
        mm_abnormal_curve = np.nanmean(car_mat, axis=0)  # curve over ALL events
        # Inference uses only the non-overlapping (independent) subset so the
        # cross-sectional SE isn't shrunk by correlated overlapping windows.
        # Fall back to the full set only if de-overlapping left too few events.
        infer = car_indep if len(car_indep) >= _MIN_EVENTS else mm_valid
        car_end = np.array([c[-1] for c in infer], dtype=float)
        n_mm = car_end.size
        mean_car = float(np.mean(car_end))
        # Cross-sectional t-stat for H0: mean CAR = 0 over independent events.
        sd = float(np.std(car_end, ddof=1)) if n_mm > 1 else 0.0
        se = sd / math.sqrt(n_mm) if sd > 0 and n_mm > 1 else float("nan")
        t_stat = mean_car / se if (se == se and se > 0) else float("nan")
        # Two-sided Student-t p-value with n_mm-1 dof (#222) — the normal
        # approximation massively understates p at the small n this runs at.
        p_value = (_t_p_value_two_sided(t_stat, n_mm - 1)
                   if t_stat == t_stat else float("nan"))
        market_model.update({
            "available": True,
            "n_events_used_for_tstat": int(n_mm),
            "mean_car_pct": round(mean_car * 100.0, 4),
            "car_std_pct": round(sd * 100.0, 4),
            "car_t_stat": round(t_stat, 3) if t_stat == t_stat else None,
            "car_p_value": round(p_value, 4) if p_value == p_value else None,
            "significant_5pct": bool(p_value < 0.05) if p_value == p_value else None,
        })

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
        # NEW (B2): proper market-model abnormal-return / CAR analytics.
        "market_model_abnormal_return_curve": _round_pct_list(mm_abnormal_curve),
        "market_model": market_model,
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
        res = cache_store.coalesce(
            key, _CACHE_TTL,
            lambda: _compute(symbol, et, window_days, period_years, benchmark),
        )
        # WHY (#223): _compute's error envelope carries 6 keys, so
        # cache_store's failure heuristic ('error' present AND <=3 keys)
        # doesn't catch it and coalesce caches a transient failure (e.g. a
        # yfinance 429 during the bars fetch) for the full 24h TTL. Evict it
        # immediately so the next request recomputes once the feed recovers —
        # mirroring fetcher.get_dividend_data's explicit guard.
        if isinstance(res, dict) and res.get("error"):
            try:
                cache_store.cache_delete(key)
            except Exception:
                pass
        return res
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
