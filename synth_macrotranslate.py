"""
synth_macrotranslate.py — Cross-Asset Macro Translator for AUGUR.

When a macro print lands (CPI, NFP, GDP, PCE, FOMC, …) the same question
gets asked: "how did equities / factors / sectors respond to comparable
prints in the past, and what does that imply for *my* book?"

This module answers it without any new data dependencies. Pipeline:

  1. Resolve a `release_id` (a FRED series ID from `fred_data.SERIES_CATALOG`
     or the special token "FOMC") into a list of historical release dates.
     - For FRED series we treat each observation date as a proxy for the
       release date. Daily series (T10Y2Y, DCOILWTICO, …) use the date as-is.
       Monthly/quarterly series have a typical ~14-day reporting lag; we
       shift them forward to land on a real trading day, which is when the
       market actually reacted.
     - For "FOMC" we reuse `research_eventstudy.CALENDAR["fed_fomc"]`.
  2. For every historical release date, compute SPY / sector-ETF / factor
     5-day forward returns starting on the next trading day. We piggy-back
     on `fetcher.get_chart_data` for prices and on `research_factors._load_factor_data`
     for the Ken French FF5+Mom daily frame — no new fetchers, no new
     network endpoints.
  3. Average across episodes → `average_response`. Quartiles across episode
     SPY 5-day returns drive the IQR.
  4. If a portfolio is supplied, get its factor exposure via
     `research_factors.portfolio_factor_exposure` and project
     `expected_5d ≈ Σ exposure[f] * avg_factor_return_5d[f]`.

Public API:

    macro_translate(release_id, surprise_pct=None, portfolio_holdings=None) -> dict

Cache: 6-hour TTL via `cache_store.coalesce`. Stampede-safe like the rest
of the research stack.

Python 3.9 compatible.
"""

from __future__ import annotations

import datetime
import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("augur.macrotranslate")

# Optional imports — module degrades to error envelopes when missing.
try:
    import cache_store  # type: ignore
except Exception as _ce:
    cache_store = None  # type: ignore
    log.warning("cache_store unavailable: %s", _ce)

try:
    import fred_data  # type: ignore
except Exception as _fe:
    fred_data = None  # type: ignore
    log.warning("fred_data unavailable: %s", _fe)

try:
    import fetcher  # type: ignore
except Exception as _fe:
    fetcher = None  # type: ignore
    log.warning("fetcher unavailable: %s", _fe)

try:
    import research_factors  # type: ignore
except Exception as _re:
    research_factors = None  # type: ignore
    log.warning("research_factors unavailable: %s", _re)

try:
    import research_eventstudy  # type: ignore
except Exception as _re:
    research_eventstudy = None  # type: ignore
    log.warning("research_eventstudy unavailable: %s", _re)


# ───────────────────────── constants ──────────────────────────

_CACHE_TTL = 6 * 3600  # 6h — matches FRED snapshot cadence

# Forward window length (trading days) for portfolio projection.
_FWD_DAYS = 5

# Cap the number of historical episodes we keep — avoids 30-year FRED
# series blowing up the payload. Newer episodes are weighted more heavily
# in practice (recency bias is appropriate for translating to *today's*
# market) but we still average uniformly to keep the math interpretable.
_MAX_EPISODES = 24

# Sector ETF universe mirrors `fetcher.get_sector_performance` so the
# panel reads identically to the rest of the macro view.
_SECTOR_ETFS: Dict[str, str] = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLV":  "Healthcare",
    "XLE":  "Energy",
    "XLI":  "Industrials",
    "XLC":  "Comm Services",
    "XLY":  "Consumer Disc",
    "XLP":  "Consumer Staples",
    "XLRE": "Real Estate",
    "XLB":  "Materials",
    "XLU":  "Utilities",
}

# Typical release-lag in calendar days, applied to the FRED observation date
# (which is the START of the REFERENCE period, not the release date) to
# approximate the print's actual market-reaction date.
#
# FRED stamps an observation with the first day of the period it measures:
# March CPI is dated 2024-03-01 but is RELEASED ~mid-April — i.e. ~6 weeks
# after the observation date, NOT 14 days. The old 14-day shift landed the
# "release" inside the very month the data measures, mis-dating the market
# reaction by ~a month and pairing the forward-return window with the wrong
# session. We can't pin the exact BLS/BEA release day without a real release
# calendar (none is bundled), so we use the empirically-typical lag from the
# reference-period start to first publication:
#
#   * monthly   (CPI/PCE/PAYEMS/UNRATE/retail): published the following month
#                → ~6 weeks ≈ 44 days from the 1st of the reference month.
#   * quarterly (GDP advance estimate): ~30 days after quarter-end, and the
#                obs date is the quarter START, so ≈ 120 days.
#   * daily series (yields, WTI): the obs date IS the data date → 0 lag.
#
# A more precise per-series lag (or the actual release calendar) would be
# strictly better; this is documented as an approximation.
_RELEASE_LAG_DAYS = {
    "monthly":   44,
    "quarterly": 120,
    "daily":     0,
    "weekly":    7,
}

# Friendlier display labels for the per-release UI header.
_RELEASE_LABELS = {
    "CPIAUCSL":  "CPI",
    "UNRATE":    "Unemployment Rate",
    "PAYEMS":    "Nonfarm Payrolls",
    "FEDFUNDS":  "Fed Funds Rate",
    "DFII10":    "10Y Real Yield",
    "T10Y2Y":    "10Y-2Y Spread",
    "M2SL":      "M2 Money Supply",
    "GDP":       "Nominal GDP",
    "PCE":       "PCE",
    "RSAFS":     "Retail Sales",
    "UMCSENT":   "UMich Consumer Sent.",
    "DCOILWTICO":"WTI Crude",
    "FOMC":      "FOMC Meeting",
}


# ───────────────────────── helpers ──────────────────────────

def _to_date(s) -> Optional[datetime.date]:
    if s is None:
        return None
    if isinstance(s, datetime.date) and not isinstance(s, datetime.datetime):
        return s
    if isinstance(s, datetime.datetime):
        return s.date()
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _release_meta(release_id: str) -> dict:
    """Return FRED catalog metadata for a release ID, with the "FOMC"
    pseudo-series handled separately so the rest of the pipeline can treat
    it uniformly."""
    rid = (release_id or "").upper().strip()
    if rid == "FOMC":
        return {
            "id": "FOMC",
            "label": "FOMC Meeting",
            "category": "rates",
            "frequency": "irregular",
        }
    if fred_data is None:
        return {"id": rid, "label": rid, "category": None, "frequency": "monthly"}
    for s in getattr(fred_data, "SERIES_CATALOG", []):
        if s.get("id") == rid:
            return {
                "id": rid,
                "label": s.get("label", rid),
                "category": s.get("category"),
                "frequency": s.get("freq", "monthly"),
            }
    # Uncurated series — treat as monthly by default.
    return {"id": rid, "label": rid, "category": None, "frequency": "monthly"}


def _shift_to_release(obs_date: datetime.date, frequency: str) -> datetime.date:
    """Convert a FRED observation date into a best-guess market-reaction
    date by adding the typical reporting lag for that release frequency."""
    lag = _RELEASE_LAG_DAYS.get((frequency or "monthly").lower(), 14)
    return obs_date + datetime.timedelta(days=lag)


def _historical_release_dates(release_id: str) -> List[Tuple[datetime.date, Optional[float]]]:
    """Return (release_date, value_at_release) for every historical instance.

    `value_at_release` is the FRED point value (or None for FOMC since
    rate decisions don't map to a numeric series cleanly in this module).
    Sorted oldest → newest, then trimmed to the most-recent _MAX_EPISODES.
    """
    rid = (release_id or "").upper().strip()

    if rid == "FOMC":
        if research_eventstudy is None:
            return []
        raw = list((research_eventstudy.CALENDAR or {}).get("fed_fomc") or [])
        out: List[Tuple[datetime.date, Optional[float]]] = []
        today = datetime.date.today()
        for s in raw:
            d = _to_date(s)
            if d is None:
                continue
            if d >= today:  # ignore future-scheduled meetings
                continue
            out.append((d, None))
        out.sort(key=lambda kv: kv[0])
        return out[-_MAX_EPISODES:]

    if fred_data is None:
        return []

    # Ask for plenty of history; FRED's CSV is cheap and we trim below.
    try:
        series = fred_data.fetch_series(rid, observations=600)
    except Exception as e:
        log.warning("macrotranslate: FRED fetch failed for %s: %s", rid, e)
        return []
    pts = (series or {}).get("points") or []
    if not pts:
        return []
    freq = (series or {}).get("frequency") or _release_meta(rid)["frequency"]
    today = datetime.date.today()
    rows: List[Tuple[datetime.date, Optional[float]]] = []
    for p in pts:
        obs = _to_date(p.get("date"))
        if obs is None:
            continue
        rd = _shift_to_release(obs, freq)
        if rd >= today:
            continue
        rows.append((rd, p.get("value")))
    rows.sort(key=lambda kv: kv[0])
    return rows[-_MAX_EPISODES:]


# ───────────────────────── factor data ──────────────────────────

class _FactorFrame:
    """Tiny adapter over `research_factors._load_factor_data()` that exposes
    a date → index map plus a numpy matrix for 1-d/5-d forward-sums.

    We don't import numpy at module top level so that if the factor stack
    is unavailable the rest of the module still answers.
    """
    def __init__(self):
        self.dates: List[str] = []
        self.factor_matrix = None  # type: ignore[assignment]
        self.factor_names: List[str] = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"]
        self.index: Dict[str, int] = {}
        self.available = False

        if research_factors is None:
            return
        try:
            dates, fac, _rf = research_factors._load_factor_data()  # type: ignore[attr-defined]
        except Exception as e:
            log.warning("macrotranslate: factor data unavailable: %s", e)
            return
        if not dates:
            return
        self.dates = list(dates)
        self.factor_matrix = fac
        self.index = {d: i for i, d in enumerate(self.dates)}
        self.available = True

    def forward_5d(self, on_or_after: datetime.date) -> Optional[Dict[str, float]]:
        """Sum of daily factor returns over the 5 trading days at/after
        `on_or_after`. Returns None when the date falls outside the frame
        or there aren't 5 forward days available.

        Daily Ken French returns are additive (decimal). For a 5-day
        cumulative log-return view the sum is a tight first-order
        approximation — way under 5 bps of error at these magnitudes.
        """
        if not self.available or self.factor_matrix is None:
            return None
        target = on_or_after.isoformat()
        # First trading day at or after target
        i = None
        for d in self.dates:
            if d >= target:
                i = self.index[d]
                break
        if i is None:
            return None
        end = i + _FWD_DAYS
        if end > len(self.dates):
            return None
        try:
            import numpy as np
            block = self.factor_matrix[i:end]
            sums = np.sum(block, axis=0)
            return {
                name: float(sums[k]) * 100.0  # decimal → percent
                for k, name in enumerate(self.factor_names)
            }
        except Exception as e:
            log.debug("macrotranslate: factor 5d sum failed: %s", e)
            return None


# ───────────────────────── price 5-day windows ──────────────────────────

def _close_series(symbol: str) -> Dict[str, float]:
    """Cache-aware close-by-date dict for an ETF / index symbol.

    Pulls ~10y of daily bars from fetcher.get_chart_data and reduces to a
    dict keyed by ISO date strings, which is friendlier for the
    forward-window lookup that follows.
    """
    if fetcher is None:
        return {}
    try:
        bars = fetcher.get_chart_data(symbol, period="10y", interval="1d") or []
    except Exception as e:
        log.debug("macrotranslate: get_chart_data(%s) failed: %s", symbol, e)
        # Must match the declared return type (Dict[str, float]). The old
        # `return []` was caught by downstream `or {}` truthiness checks but
        # would crash any consumer that called `.keys()` / `.items()` directly.
        return {}
    out: Dict[str, float] = {}
    if not bars:
        return out
    for b in bars:
        c = b.get("close")
        t = b.get("time")
        # Coerce close first — get_chart_data may yield numeric strings, and a
        # `c <= 0` comparison against a str raises TypeError on Python 3.
        try:
            cf = float(c)
        except (TypeError, ValueError):
            continue
        if t is None or cf <= 0:
            continue
        try:
            d = datetime.datetime.utcfromtimestamp(int(t)).strftime("%Y-%m-%d")
        except Exception:
            continue
        out[d] = cf
    return out


def _forward_return(closes: Dict[str, float], on_or_after: datetime.date,
                    days: int) -> Optional[float]:
    """Return percent change from the first close on/after `on_or_after`
    to `days` trading days later. None when either endpoint is missing.

    `days=0` is a no-op (returns 0.0 for live dates) — used as a sanity
    pre-check that the start-date has any price at all.
    """
    if not closes:
        return None
    sorted_dates = sorted(closes.keys())
    target = on_or_after.isoformat()
    start_idx = None
    # bisect-left without importing bisect (tiny lists, cheap)
    lo, hi = 0, len(sorted_dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_dates[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    start_idx = lo
    if start_idx >= len(sorted_dates):
        return None
    end_idx = start_idx + days
    if end_idx >= len(sorted_dates):
        return None
    start_p = closes[sorted_dates[start_idx]]
    end_p = closes[sorted_dates[end_idx]]
    if start_p <= 0:
        return None
    return (end_p / start_p - 1.0) * 100.0


# ───────────────────────── aggregation ──────────────────────────

def _mean(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return sum(xs) / len(xs)


def _quartiles(xs: List[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (Q1, median, Q3). Uses linear interpolation between order
    statistics so the result coincides with numpy.percentile(method='linear')
    on small samples. None when input has < 2 values."""
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if n < 2:
        return (None, None, None)

    def _pct(p: float) -> float:
        # 0 ≤ p ≤ 1; classic linear interpolation
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        w = idx - lo
        return xs[lo] * (1.0 - w) + xs[hi] * w

    return (_pct(0.25), _pct(0.50), _pct(0.75))


def _avg_dict(dicts: List[Optional[Dict[str, float]]]) -> Dict[str, float]:
    """Per-key average across a list of {name: float} dicts (ignoring None)."""
    bucket: Dict[str, List[float]] = {}
    for d in dicts:
        if not d:
            continue
        for k, v in d.items():
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            bucket.setdefault(k, []).append(fv)
    return {k: round(sum(vs) / len(vs), 4) for k, vs in bucket.items() if vs}


# ───────────────────────── core ──────────────────────────

def _build_episode(rd: datetime.date,
                   release_value: Optional[float],
                   spy_closes: Dict[str, float],
                   sector_closes: Dict[str, Dict[str, float]],
                   factor_frame: _FactorFrame) -> Optional[dict]:
    """Assemble one historical episode dict. Skip dates with no SPY window."""
    spy_1d = _forward_return(spy_closes, rd, 1)
    spy_5d = _forward_return(spy_closes, rd, 5)
    if spy_1d is None and spy_5d is None:
        return None

    sector_rets: Dict[str, Optional[float]] = {}
    for etf, label in _SECTOR_ETFS.items():
        closes = sector_closes.get(etf) or {}
        r = _forward_return(closes, rd, 5)
        sector_rets[label] = round(r, 4) if r is not None else None

    fac = factor_frame.forward_5d(rd) if factor_frame.available else None
    fac_rounded = {k: round(v, 4) for k, v in (fac or {}).items()} if fac else {}

    return {
        "date": rd.isoformat(),
        "release_value": release_value,
        "surprise_pct": None,  # historical surprises not modelled (would need consensus history)
        "sp500_1d_pct": round(spy_1d, 4) if spy_1d is not None else None,
        "sp500_5d_pct": round(spy_5d, 4) if spy_5d is not None else None,
        "factor_returns_5d": fac_rounded,
        "sector_returns_5d": sector_rets,
    }


def _portfolio_projection(holdings: List[dict],
                          avg_factors: Dict[str, float],
                          spy_5d_distribution: List[float],
                          episode_factor_returns: Optional[List[Dict[str, float]]] = None
                          ) -> Optional[dict]:
    """Project portfolio 5-day return from factor exposures + the historical
    average per-factor 5d return; bracket with the IQR of the PROJECTED
    QUANTITY ITSELF across episodes.

    `avg_factors` and `spy_5d_distribution` are *percent* values. We
    interpret `exposure[f] * avg_factor_return_5d[f]` as a percentage too.

    `episode_factor_returns` is the per-episode {factor: 5d_return_pct} list;
    when supplied we re-run the same Σ beta·factor projection for every
    historical episode to build the projected-return distribution, then take
    ITS IQR / extremes. The old code took SPY's IQR and merely rescaled it by
    market beta — which ignores the book's SMB/HML/Mom/etc. tilts entirely, so
    a market-neutral but factor-tilted book got a band of ~0. Bracketing with
    the projected quantity's own cross-episode dispersion is the honest band.
    Falls back to the old SPY-IQR×beta path when episode data isn't supplied.
    """
    if not holdings or research_factors is None:
        return None
    try:
        port = research_factors.portfolio_factor_exposure(holdings, period_years=3)
    except Exception as e:
        log.warning("macrotranslate: portfolio_factor_exposure failed: %s", e)
        return {"error": f"portfolio exposure unavailable: {e}"}
    if not port or "error" in port:
        return {"error": (port or {}).get("error", "portfolio exposure unavailable")}

    exposures = (port.get("exposures") or {})
    contrib: Dict[str, float] = {}
    total = 0.0
    for f, blk in exposures.items():
        beta = (blk or {}).get("beta")
        if beta is None:
            continue
        af = avg_factors.get(f)
        if af is None:
            continue
        c = float(beta) * float(af)
        contrib[f] = round(c, 4)
        total += c

    mkt_beta = (exposures.get("Mkt-RF") or {}).get("beta")
    scale = float(mkt_beta) if mkt_beta is not None else 1.0

    # Preferred bracket: the IQR / extremes of the PROJECTED portfolio return
    # across episodes (project each episode through the same factor betas).
    projected_dist: List[float] = []
    if episode_factor_returns:
        for ep_fac in episode_factor_returns:
            if not isinstance(ep_fac, dict):
                continue
            used = 0
            ep_proj = 0.0
            for f, blk in exposures.items():
                beta = (blk or {}).get("beta")
                if beta is None:
                    continue
                rv = ep_fac.get(f)
                if rv is None:
                    continue
                try:
                    ep_proj += float(beta) * float(rv)
                    used += 1
                except (TypeError, ValueError):
                    continue
            if used > 0:
                projected_dist.append(ep_proj)

    iqr = [None, None]
    best_case: Optional[float] = None
    worst_case: Optional[float] = None
    band_basis = "projected_quantity_iqr"
    if len(projected_dist) >= 2:
        q1, _med, q3 = _quartiles(projected_dist)
        if q1 is not None and q3 is not None:
            iqr = [round(q1, 4), round(q3, 4)]
        best_case = round(max(projected_dist), 4)
        worst_case = round(min(projected_dist), 4)
    else:
        # Fallback: SPY 5-day IQR rescaled by market beta (old behaviour).
        band_basis = "spy_iqr_x_market_beta"
        q1, _med, q3 = _quartiles(spy_5d_distribution)
        if q1 is not None and q3 is not None:
            iqr = [round(q1 * scale, 4), round(q3 * scale, 4)]
        if spy_5d_distribution:
            best_case = round(max(spy_5d_distribution) * scale, 4)
            worst_case = round(min(spy_5d_distribution) * scale, 4)

    # Per-holding winners/losers via individual factor exposures.
    per_symbol: List[dict] = []
    for h in port.get("holdings") or []:
        sym = h.get("symbol")
        res = h.get("result") or {}
        if "error" in res:
            continue
        sym_total = 0.0
        used = 0
        for f, blk in (res.get("exposures") or {}).items():
            beta = (blk or {}).get("beta")
            if beta is None:
                continue
            af = avg_factors.get(f)
            if af is None:
                continue
            sym_total += float(beta) * float(af)
            used += 1
        if used == 0:
            continue
        per_symbol.append({"symbol": sym, "projected_5d": round(sym_total, 4)})

    per_symbol.sort(key=lambda x: x["projected_5d"], reverse=True)
    top_winners = per_symbol[:5]
    top_losers = sorted(per_symbol, key=lambda x: x["projected_5d"])[:5]

    out = {
        "expected_5d_pct": round(total, 4),
        "iqr_5d": iqr,
        "iqr_basis": band_basis,
        "best_case": best_case,
        "worst_case": worst_case,
        "factor_contributions": contrib,
        "market_beta": round(scale, 4) if mkt_beta is not None else None,
        "top_winners": top_winners,
        "top_losers": top_losers,
        "n_holdings": port.get("n_holdings"),
    }
    return out


def macro_translate(release_id: str,
                    surprise_pct: Optional[float] = None,
                    portfolio_holdings: Optional[List[dict]] = None) -> dict:
    """Translate a macro release into expected portfolio reaction.

    Pure function on top of cached upstream calls. Cached itself for 6h
    per (release_id, surprise bucket, holdings fingerprint).
    """
    rid_raw = (release_id or "").upper().strip()
    if not rid_raw:
        return {"error": "missing release_id"}

    meta = _release_meta(rid_raw)
    rid = meta["id"]

    # Holdings fingerprint for the cache key — coarse on purpose. Two books
    # that differ only by a small re-balance don't need separate translates.
    fp = ""
    if portfolio_holdings:
        try:
            buckets: Dict[str, float] = {}
            for h in portfolio_holdings:
                s = str(h.get("symbol", "")).upper().strip()
                mv = float(h.get("market_value") or 0.0)
                if s and mv > 0:
                    buckets[s] = buckets.get(s, 0.0) + mv
            tot = sum(buckets.values()) or 1.0
            # symbol → integer percent of book, sorted; collisions are fine.
            fp = ",".join(f"{s}:{int(round(mv/tot*100))}"
                          for s, mv in sorted(buckets.items()))
        except Exception:
            fp = ""

    surprise_bucket: Optional[str] = None
    if surprise_pct is not None:
        try:
            sp = float(surprise_pct)
            # 4-bin signed bucket so similar surprises share a cache entry.
            if   sp <= -0.5: surprise_bucket = "neg2"
            elif sp <  0.0:  surprise_bucket = "neg1"
            elif sp <  0.5:  surprise_bucket = "pos1"
            else:            surprise_bucket = "pos2"
        except (TypeError, ValueError):
            surprise_bucket = None

    cache_key = ("macrotranslate", rid, surprise_bucket or "any", fp)

    def _compute() -> dict:
        episodes_raw = _historical_release_dates(rid)
        if not episodes_raw:
            return {
                "error": f"no historical release dates for {rid}",
                "release": _release_envelope(meta, surprise_pct, None, None),
                "as_of": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        # SPY closes once, sector closes once. Even on a cold cache this
        # block costs ~12 yfinance calls, which is well under typical
        # rate-limit thresholds.
        spy_closes = _close_series("SPY")
        sector_closes = {etf: _close_series(etf) for etf in _SECTOR_ETFS}
        factor_frame = _FactorFrame()

        episodes: List[dict] = []
        for rd, val in episodes_raw:
            ep = _build_episode(rd, val, spy_closes, sector_closes, factor_frame)
            if ep is None:
                continue
            episodes.append(ep)

        if not episodes:
            return {
                "error": "no historical episodes with usable price windows",
                "release": _release_envelope(meta, surprise_pct, None, None),
                "as_of": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

        avg_factors = _avg_dict([e.get("factor_returns_5d") for e in episodes])
        avg_sectors = _avg_dict([e.get("sector_returns_5d") for e in episodes])
        avg_spy_1d = _mean([e.get("sp500_1d_pct") for e in episodes])
        avg_spy_5d = _mean([e.get("sp500_5d_pct") for e in episodes])
        q1, med, q3 = _quartiles([e.get("sp500_5d_pct") for e in episodes if e.get("sp500_5d_pct") is not None])

        average_response = {
            "sp500_1d_pct": round(avg_spy_1d, 4) if avg_spy_1d is not None else None,
            "sp500_5d_pct": round(avg_spy_5d, 4) if avg_spy_5d is not None else None,
            "sp500_5d_iqr": [
                round(q1, 4) if q1 is not None else None,
                round(q3, 4) if q3 is not None else None,
            ],
            "sp500_5d_median": round(med, 4) if med is not None else None,
            "factor_returns_5d": avg_factors,
            "sector_returns_5d": avg_sectors,
        }

        # Latest release pulled from the FRED series if available (gives
        # the UI a "what just printed" header).
        latest_date = None
        latest_value = None
        delta_pct = None
        if rid != "FOMC" and fred_data is not None:
            try:
                s = fred_data.fetch_series(rid, observations=4)
                pts = (s or {}).get("points") or []
                if pts:
                    latest_date = pts[-1].get("date")
                    latest_value = pts[-1].get("value")
                    if len(pts) >= 2:
                        # FRED point values can be numeric strings; coerce so the
                        # subtraction doesn't TypeError (which the bare except
                        # below would swallow, silently losing delta_pct).
                        try:
                            lv = float(latest_value)
                            pv = float(pts[-2].get("value"))
                        except (TypeError, ValueError):
                            lv = pv = None
                        if pv:
                            delta_pct = round((lv - pv) / pv * 100.0, 4)
            except Exception:
                pass

        release_block = _release_envelope(meta, surprise_pct, latest_date, latest_value, delta_pct)

        out = {
            "release": release_block,
            "historical_episodes": episodes,
            "average_response": average_response,
            "as_of": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        if portfolio_holdings:
            spy_5d_dist = [e["sp500_5d_pct"] for e in episodes
                           if e.get("sp500_5d_pct") is not None]
            ep_factor_rets = [e.get("factor_returns_5d") for e in episodes
                              if e.get("factor_returns_5d")]
            proj = _portfolio_projection(portfolio_holdings, avg_factors, spy_5d_dist,
                                         ep_factor_rets)
            if proj is not None:
                out["portfolio_projection"] = proj

        return out

    if cache_store is None:
        return _compute()
    try:
        return cache_store.coalesce(cache_key, _CACHE_TTL, _compute)
    except Exception as e:
        log.warning("cache_store.coalesce(macrotranslate) failed: %s", e)
        return _compute()


def _release_envelope(meta: dict,
                      surprise_pct: Optional[float],
                      latest_date: Optional[str],
                      latest_value: Optional[float],
                      delta_pct: Optional[float] = None) -> dict:
    return {
        "id": meta["id"],
        "name": _RELEASE_LABELS.get(meta["id"], meta.get("label") or meta["id"]),
        "category": meta.get("category"),
        "frequency": meta.get("frequency"),
        "latest_date": latest_date,
        "latest_value": latest_value,
        "delta_pct": delta_pct,
        "surprise_pct": surprise_pct,
    }


def supported_releases() -> List[dict]:
    """Return the catalog of release IDs this module understands.

    Convenience for the UI / route layer so they can populate a dropdown
    without re-importing fred_data themselves.
    """
    out: List[dict] = []
    if fred_data is not None:
        for s in getattr(fred_data, "SERIES_CATALOG", []):
            out.append({
                "id": s["id"],
                "name": s.get("label", s["id"]),
                "category": s.get("category"),
                "frequency": s.get("freq"),
            })
    out.append({"id": "FOMC", "name": "FOMC Meeting",
                "category": "rates", "frequency": "irregular"})
    return out


__all__ = ["macro_translate", "supported_releases"]
