"""
synth_catalyst.py — Catalyst Calendar with Implied-Move + Historical Overlay.

Builds a unified forward-looking timeline of upcoming catalysts (earnings,
FOMC, monthly OPEX, ex-dividend) for a list of symbols. For each event we
overlay three independent views:

    1. Market-implied move from the options risk-neutral density
       (`research_iv_density.risk_neutral_density`). The implied 1-σ width
       at the nearest expiry on/after the event is normalized by spot to
       give a percent move.

    2. Historical event-study average from
       `research_eventstudy.event_study(symbol, event_type)`: the average
       T0 return and post-event 5-day hit-rate distilled from up to a
       five-year window of comparable past events for the *same symbol*.

    3. Current signal stack going into the event: smart_money score,
       ml_forecast composite probability, narrative_engine phase, and a
       proxy for option skew taken from the RND's third-moment estimate.

A synthesized "edge_score" then expresses how far the historical event
move differs from what the option chain is currently pricing — positive
means the market is *under*-pricing the move relative to history (cheap
straddle), negative means it is over-pricing.

Public API
----------

    catalyst_timeline(symbols=None, days_ahead=60) -> dict

When `symbols` is None the user's portfolio holdings are used
(`database.get_portfolio()`). Otherwise the caller-supplied list is
deduplicated, upper-cased, and capped at 25 names.

The return envelope is exactly::

    {
      "as_of": "YYYY-MM-DD",
      "days_ahead": 60,
      "symbols": ["AAPL", "NVDA", ...],
      "events": [
        {
          "date": "YYYY-MM-DD",
          "type": "earnings" | "fed_fomc" | "opex" | "ex_dividend",
          "symbol": "AAPL",
          "days_until": int,
          "implied_move_pct": float | None,
          "historical_avg_move_pct": float | None,
          "historical_n_events": int | None,
          "historical_hit_rate_positive_post": float | None,
          "current_signals": {
            "smart_money": int | None,
            "ml_prob_up": float | None,
            "narrative_phase": str | None,
            "iv_skew": str | None,
          },
          "edge_score": float | None,
          "note": str,
        },
        ...
      ],
      "errors": [...]    # opportunistic, never blocks output
    }

Cache: result is keyed by a 12-character SHA hash of the sorted symbol
list plus `days_ahead`, TTL = 6h. Inner calls (event studies, RNDs, ML
forecasts, narratives) hit their own caches, so a warm rebuild costs only
the wrapper logic.

Python 3.9 compatible — no PEP-604 unions, no `match` statements.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("augur.synth.catalyst")


# ── Tunables ──────────────────────────────────────────────────────────────

_CACHE_TTL = 6 * 3600      # 6h — events change slowly; signals refresh hourly
_MAX_SYMBOLS = 25          # hard cap on caller-supplied input
_HIST_WINDOW_DAYS = 5      # event_study window for historical overlay
_HIST_PERIOD_YEARS = 5     # how far back to look for historical instances
_FOMC_TICKER_FOR_BARS = "SPY"   # all FOMC events share one implied-move source


# ── Soft imports — every dependency must degrade, never crash ────────────

try:
    import cache_store
except Exception as _e:    # pragma: no cover
    cache_store = None     # type: ignore
    log.warning("cache_store unavailable: %s", _e)

try:
    import database
except Exception as _e:    # pragma: no cover
    database = None        # type: ignore
    log.warning("database unavailable: %s", _e)

try:
    import fetcher
except Exception as _e:    # pragma: no cover
    fetcher = None         # type: ignore
    log.warning("fetcher unavailable: %s", _e)

try:
    import research_eventstudy
except Exception as _e:    # pragma: no cover
    research_eventstudy = None  # type: ignore
    log.warning("research_eventstudy unavailable: %s", _e)

try:
    import research_iv_density
except Exception as _e:    # pragma: no cover
    research_iv_density = None  # type: ignore
    log.warning("research_iv_density unavailable: %s", _e)

try:
    import smart_money
except Exception as _e:    # pragma: no cover
    smart_money = None     # type: ignore
    log.warning("smart_money unavailable: %s", _e)

try:
    import ml_forecast
except Exception as _e:    # pragma: no cover
    ml_forecast = None     # type: ignore
    log.warning("ml_forecast unavailable: %s", _e)

try:
    import narrative_engine
except Exception as _e:    # pragma: no cover
    narrative_engine = None  # type: ignore
    log.warning("narrative_engine unavailable: %s", _e)


# ── Date helpers ─────────────────────────────────────────────────────────

def _today() -> datetime.date:
    return datetime.date.today()


def _to_date(d: Any) -> Optional[datetime.date]:
    if d is None:
        return None
    if isinstance(d, datetime.date) and not isinstance(d, datetime.datetime):
        return d
    if isinstance(d, datetime.datetime):
        return d.date()
    try:
        return datetime.date.fromisoformat(str(d)[:10])
    except Exception:
        return None


def _days_until(d: datetime.date, ref: Optional[datetime.date] = None) -> int:
    ref = ref or _today()
    return (d - ref).days


def _safe_round(v: Any, digits: int = 3) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:   # NaN
        return None
    return round(f, digits)


# ── Symbol normalization ─────────────────────────────────────────────────

def _portfolio_symbols() -> List[str]:
    """Pull symbols from the user's portfolio. Filters out cash/crypto so
    the timeline focuses on equity catalysts. Empty list on failure."""
    if database is None:
        return []
    try:
        rows = database.get_portfolio() or []
    except Exception as e:
        log.warning("catalyst: get_portfolio failed: %s", e)
        return []
    out = []
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        # Skip non-equity rows (cash sleeves, crypto), since they have no
        # earnings/options.
        atype = (r.get("asset_type") or "stock").lower()
        if atype in ("cash", "crypto"):
            continue
        if sym not in out:
            out.append(sym)
    return out


def _normalize_symbols(symbols: Optional[Sequence[str]]) -> List[str]:
    if not symbols:
        return _portfolio_symbols()
    out: List[str] = []
    for s in symbols:
        if not s:
            continue
        s = str(s).strip().upper()
        if s and s not in out:
            out.append(s)
        if len(out) >= _MAX_SYMBOLS:
            break
    return out


# ── Calendar collection ──────────────────────────────────────────────────

def _upcoming_earnings_for(symbol: str, horizon_end: datetime.date,
                           today: datetime.date) -> Optional[datetime.date]:
    """Return the next earnings date for `symbol` if it falls within
    [today, horizon_end]. Otherwise None.

    Uses `yfinance.Ticker(symbol).calendar` first (cheap, one HTTP), and
    falls back to `earnings_dates` (the upper rows are future entries
    where `Reported EPS` is NaN — those are scheduled releases).
    """
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
    except Exception as e:
        log.debug("catalyst: yfinance unavailable for %s: %s", symbol, e)
        return None

    # 1) Ticker.calendar — fastest, returns the single next date.
    try:
        cal = t.calendar
        if cal:
            dates_list = cal.get("Earnings Date") or []
            if not isinstance(dates_list, list):
                dates_list = [dates_list]
            for d in dates_list:
                dt = _to_date(d)
                if dt and today <= dt <= horizon_end:
                    return dt
    except Exception as e:
        log.debug("catalyst: calendar(%s) failed: %s", symbol, e)

    # 2) earnings_dates — covers names where calendar is empty.
    try:
        ed = t.earnings_dates
        if ed is not None and not ed.empty:
            # Future rows are those where Reported EPS is NaN.
            try:
                future = ed[ed["Reported EPS"].isna()] if "Reported EPS" in ed.columns else ed
            except Exception:
                future = ed
            best: Optional[datetime.date] = None
            for idx in future.index:
                dt = _to_date(idx)
                if dt and today <= dt <= horizon_end:
                    if best is None or dt < best:
                        best = dt
            return best
    except Exception as e:
        log.debug("catalyst: earnings_dates(%s) failed: %s", symbol, e)

    return None


def _upcoming_fomc(horizon_end: datetime.date,
                   today: datetime.date) -> List[datetime.date]:
    """Filter the shared FOMC calendar to dates in [today, horizon_end]."""
    if research_eventstudy is None:
        return []
    raw = getattr(research_eventstudy, "CALENDAR", {}).get("fed_fomc") or []
    out = []
    for s in raw:
        d = _to_date(s)
        if d and today <= d <= horizon_end:
            out.append(d)
    return sorted(out)


def _upcoming_opex(horizon_end: datetime.date,
                   today: datetime.date) -> List[datetime.date]:
    """Third Friday of each month between today and horizon_end."""
    out = []
    y, m = today.year, today.month
    while datetime.date(y, m, 1) <= horizon_end:
        first = datetime.date(y, m, 1)
        offset = (4 - first.weekday()) % 7   # 0..6 days until first Friday
        third_friday = first + datetime.timedelta(days=offset + 14)
        if today <= third_friday <= horizon_end:
            out.append(third_friday)
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out


def _upcoming_ex_div(symbol: str, horizon_end: datetime.date,
                     today: datetime.date) -> Optional[datetime.date]:
    """Next ex-dividend date for `symbol` (yfinance Ticker.calendar carries
    a forward-looking `Ex-Dividend Date` for active dividend payers).

    Falls back to None when not available — `fetcher.get_dividend_data()`
    only carries past payments."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        cal = t.calendar or {}
    except Exception:
        return None
    raw = cal.get("Ex-Dividend Date")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raw = [raw]
    for d in raw:
        dt = _to_date(d)
        if dt and today <= dt <= horizon_end:
            return dt
    return None


# ── Per-event enrichment helpers ─────────────────────────────────────────

def _implied_move_pct(symbol: str, event_date: datetime.date) -> Optional[float]:
    """Compute the option-implied 1-σ move % at the nearest expiry
    on/after `event_date`. Returns None when the chain is unavailable.

    The RND module returns absolute implied moments in dollar terms; we
    normalize by spot to get a percentage that's directly comparable to
    the historical avg-T0 figure from `event_study.summary`.
    """
    if research_iv_density is None:
        return None

    # Pick an expiry: ask the fetcher for the chain calendar so we can
    # snap to the next expiry ≥ event_date. The RND module's own
    # _choose_expiry picks the first expiry >7d out, which can sit
    # *before* the event — that would mis-attribute the implied move.
    #
    # NOTE: fetcher exports ``get_options_dates`` (not ``get_option_expiries``),
    # so the original hasattr check was always False and we'd silently fall
    # through to a bare yf.Ticker(symbol).options — which returns () under
    # rate-limit (v0.1.6 audit pattern) without ever attempting the Yahoo /
    # Finviz fallback chain.
    chosen: Optional[str] = None
    expiries: list = []
    try:
        if fetcher is not None and hasattr(fetcher, "get_options_dates"):
            expiries = fetcher.get_options_dates(symbol) or []
    except Exception:
        expiries = []

    ev_iso = event_date.isoformat()
    # sorted() so "first expiry on/after the event" is genuinely the nearest,
    # even if the provider returns expiry dates out of order.
    for exp in sorted(expiries):
        if exp >= ev_iso:
            chosen = exp
            break
    # If nothing on/after the event is listed, the event is past the
    # listed strip — let the RND module pick its own default.

    try:
        rnd = research_iv_density.risk_neutral_density(symbol, expiry=chosen)
    except Exception as e:
        log.debug("catalyst: RND(%s, %s) failed: %s", symbol, chosen, e)
        return None

    if not isinstance(rnd, dict) or rnd.get("error"):
        return None

    spot = rnd.get("spot")
    moments = rnd.get("implied_moments") or {}
    std_implied = moments.get("std_implied")
    if spot is None or std_implied is None:
        return None
    try:
        pct = float(std_implied) / float(spot) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if pct != pct or pct <= 0:
        return None
    return round(pct, 3)


def _implied_skew(symbol: str, event_date: datetime.date) -> Optional[float]:
    """Optional: reuse the RND skew (third moment) as an IV-skew proxy.

    Negative skew → downside is priced richer than upside (put skew),
    positive skew → upside is priced richer (call skew, less common).
    Returns None on failure or no data."""
    if research_iv_density is None:
        return None
    # Re-pick same expiry as the implied move call. We don't bother
    # recomputing — research_iv_density.coalesce caches the full payload
    # so this is essentially free on the second hit.
    try:
        # Route through fetcher.get_options_dates which carries the cache +
        # fallback handling. The earlier hasattr() check was looking for a
        # non-existent ``get_option_expiries`` name so it always fell through
        # to a naked yf.Ticker(symbol).options — which silently returns ()
        # whenever yfinance is rate-limited.
        expiries: list = []
        try:
            if fetcher is not None and hasattr(fetcher, "get_options_dates"):
                expiries = fetcher.get_options_dates(symbol) or []
        except Exception:
            expiries = []
        chosen = None
        ev_iso = event_date.isoformat()
        # WHY (S12): _implied_move_pct picks the nearest on/after expiry via
        # sorted(expiries), but this skew path iterated the RAW provider order.
        # If the provider returns expiries unsorted, skew and move could anchor
        # to DIFFERENT expiries — a silent mismatch. Sort here too so both
        # quantities describe the same option strip.
        for exp in sorted(expiries):
            if exp >= ev_iso:
                chosen = exp
                break
        rnd = research_iv_density.risk_neutral_density(symbol, expiry=chosen)
    except Exception:
        return None
    if not isinstance(rnd, dict) or rnd.get("error"):
        return None
    skew = (rnd.get("implied_moments") or {}).get("skew_implied")
    if skew is None:
        return None
    try:
        return round(float(skew), 3)
    except (TypeError, ValueError):
        return None


def _historical_overlay(symbol: str, event_type: str) -> Dict[str, Any]:
    """Pull historical-event statistics from `research_eventstudy.event_study`.

    Returns a small dict with avg-T0 / hit-rate / n_events / abs-avg-move.
    On insufficient history we return None values rather than raising —
    the timeline still ships, just without a historical overlay for this
    event."""
    if research_eventstudy is None:
        return {"avg_move": None, "hit_rate": None, "n_events": None}
    try:
        r = research_eventstudy.event_study(
            symbol, event_type=event_type,
            window_days=_HIST_WINDOW_DAYS,
            period_years=_HIST_PERIOD_YEARS,
        )
    except Exception as e:
        log.debug("catalyst: event_study(%s, %s) failed: %s",
                  symbol, event_type, e)
        return {"avg_move": None, "hit_rate": None, "n_events": None}
    if not isinstance(r, dict):
        return {"avg_move": None, "hit_rate": None, "n_events": None}

    n_events = r.get("n_events")
    if r.get("error") or not r.get("summary"):
        # Surface n_events even on insufficient-history error envelopes so
        # the UI can render "1/5 events found".
        return {"avg_move": None, "hit_rate": None,
                "n_events": n_events if isinstance(n_events, int) else None}

    s = r["summary"]
    # WHY (S13): we want the typical ABSOLUTE move around the event to compare
    # against implied σ%. The old `abs(mean(T0)) + abs(mean(post5))` averaged
    # SIGNED returns first, so a symmetric history (big moves both ways that
    # cancel in the mean) collapsed toward ~0 and made the premium look cheap.
    # Prefer the event-study's mean-of-absolute-per-episode move; only fall
    # back to the old signed-then-abs combination if it isn't available.
    move_magnitude = s.get("avg_abs_event_move_pct")
    if not isinstance(move_magnitude, (int, float)):
        avg_t0 = s.get("avg_T0_return_pct")
        avg_post = s.get("avg_post_event_5d_pct")
        move_magnitude = None
        if isinstance(avg_t0, (int, float)) and isinstance(avg_post, (int, float)):
            move_magnitude = abs(float(avg_t0)) + abs(float(avg_post))
        elif isinstance(avg_t0, (int, float)):
            move_magnitude = abs(float(avg_t0))
        elif isinstance(avg_post, (int, float)):
            move_magnitude = abs(float(avg_post))

    return {
        "avg_move": _safe_round(move_magnitude, 3),
        "hit_rate": _safe_round(s.get("hit_rate_positive_post"), 3),
        "n_events": n_events if isinstance(n_events, int) else None,
    }


def _current_signals(symbol: str,
                     event_date: datetime.date,
                     signal_cache: Dict[str, Dict[str, Any]],
                     skew_symbol: Optional[str] = None) -> Dict[str, Any]:
    """Memoized per-symbol bundle of current signal stack. Each symbol
    appears multiple times across event types (earnings + FOMC + OPEX),
    so this is cached at the request scope.

    WHY (S11): `skew_symbol` lets the caller pick the underlying whose chain
    drives iv_skew (defaults to `symbol`). Previously the caller would let this
    compute _implied_skew(symbol) and then immediately overwrite it for FOMC,
    paying for the chain pull twice. Passing the right symbol in computes it
    once."""
    skew_symbol = skew_symbol or symbol
    cached = signal_cache.get(symbol)
    if cached is not None:
        # Re-derive iv_skew from the event date (skew varies by expiry).
        out = dict(cached)
        skew = _implied_skew(skew_symbol, event_date)
        out["iv_skew"] = _format_skew(skew)
        return out

    sm_score: Optional[int] = None
    ml_prob: Optional[float] = None
    narr_phase: Optional[str] = None

    if smart_money is not None:
        try:
            sm = smart_money.compute_score(symbol)
            if isinstance(sm, dict):
                v = sm.get("score")
                if isinstance(v, (int, float)):
                    sm_score = int(v)
        except Exception as e:
            log.debug("catalyst: smart_money(%s) failed: %s", symbol, e)

    if ml_forecast is not None:
        try:
            ml = ml_forecast.ml_forecast(symbol)
            if isinstance(ml, dict):
                rf = ml.get("rf_classifier") or {}
                p = rf.get("prob_up_20d") if isinstance(rf, dict) else None
                if isinstance(p, (int, float)):
                    ml_prob = round(float(p), 3)
                else:
                    # Fall back to the composite if RF unavailable.
                    comp = ml.get("composite_ml_score")
                    if isinstance(comp, (int, float)):
                        ml_prob = round(float(comp), 3)
        except Exception as e:
            log.debug("catalyst: ml_forecast(%s) failed: %s", symbol, e)

    if narrative_engine is not None:
        try:
            nv = narrative_engine.analyze_narrative(symbol)
            if isinstance(nv, dict) and not nv.get("error"):
                narr_phase = nv.get("narrative_phase")
        except Exception as e:
            log.debug("catalyst: narrative(%s) failed: %s", symbol, e)

    bundle = {
        "smart_money": sm_score,
        "ml_prob_up": ml_prob,
        "narrative_phase": narr_phase,
    }
    signal_cache[symbol] = bundle

    out = dict(bundle)
    out["iv_skew"] = _format_skew(_implied_skew(skew_symbol, event_date))
    return out


def _format_skew(skew: Optional[float]) -> Optional[str]:
    """Render the RND skew as a signed, two-decimal string. None -> None."""
    if skew is None:
        return None
    sign = "+" if skew >= 0 else ""
    return f"{sign}{skew:.2f}"


# ── Edge synthesis and note generation ───────────────────────────────────

def _edge_score(implied: Optional[float],
                historical: Optional[float]) -> Optional[float]:
    """Edge = (historical - implied) / max(implied, 1).

    Positive ⇒ historical event move is *larger* than what options price
    in — straddle looks cheap. Negative ⇒ implied move richer than history
    — straddle expensive."""
    if implied is None or historical is None:
        return None
    denom = max(abs(implied), 1.0)
    edge = (historical - implied) / denom
    # Cap so a single noisy run doesn't blow out the visualization.
    if edge > 3.0:
        edge = 3.0
    elif edge < -3.0:
        edge = -3.0
    return _safe_round(edge, 3)


def _build_note(event_type: str, implied: Optional[float],
                historical: Optional[float], n_events: Optional[int],
                hit_rate: Optional[float]) -> str:
    """Compose a one-liner that's safe to render in a UI cell."""
    if implied is None and historical is None:
        return "No implied or historical overlay available."
    if implied is None:
        if historical is None:
            return "Historical sample too small."
        return (f"Historical avg move {historical:.2f}% over {n_events or 0} "
                f"past events — implied move unavailable (no liquid chain).")
    if historical is None:
        return (f"Implied move {implied:.2f}%; no comparable historical "
                "sample for this name yet.")

    # Both available — compare.
    if implied <= 0.01:
        return f"Historical avg {historical:.2f}% vs implied ~0 — implied unreliable."
    ratio = historical / implied
    if ratio >= 1.25:
        verdict = f"historical {((ratio-1)*100):.0f}% larger than implied — premium cheap"
    elif ratio <= 0.8:
        verdict = f"implied {((1/ratio-1)*100):.0f}% higher than historical avg — premium expensive"
    else:
        verdict = "implied and historical within ±20% — fairly priced"

    hit_str = ""
    if isinstance(hit_rate, (int, float)):
        hit_str = f" Hit-rate post-event: {hit_rate*100:.0f}%."
    return (f"Implied {implied:.2f}% vs historical {historical:.2f}% "
            f"({n_events or 0} events): {verdict}.{hit_str}")


# ── Event assembly ───────────────────────────────────────────────────────

def _make_event(symbol: str, event_type: str, event_date: datetime.date,
                signal_cache: Dict[str, Dict[str, Any]],
                today: datetime.date) -> Dict[str, Any]:
    """Build the dict for a single event row."""

    # WHY (S10): FOMC is a market-wide event, but the historical overlay below
    # is always built from THIS symbol's own past reactions. Pricing the
    # implied move off SPY while comparing it to the symbol's historical moves
    # injected a pure beta artifact into edge_score (a high-beta name looks
    # "cheap" and a low-beta name "expensive" purely from its beta, not any
    # real mispricing). Use the symbol's OWN chain for the implied move so both
    # sides of the comparison describe the same underlying.
    implied = _implied_move_pct(symbol, event_date)
    skew_sym = symbol

    hist = _historical_overlay(symbol, event_type)
    historical = hist["avg_move"]
    n_events = hist["n_events"]
    hit_rate = hist["hit_rate"]

    # WHY (S11): pass the skew underlying straight into _current_signals so it
    # computes _implied_skew once on the correct symbol. The old code computed
    # _implied_skew(symbol) inside _current_signals and then, for FOMC, threw
    # it away and recomputed _implied_skew(SPY) here — two full chain pulls per
    # symbol per FOMC date. With skew_sym == symbol for every event type now,
    # there's nothing to override.
    signals = _current_signals(symbol, event_date, signal_cache, skew_symbol=skew_sym)

    return {
        "date": event_date.isoformat(),
        "type": event_type,
        "symbol": symbol,
        "days_until": _days_until(event_date, today),
        "implied_move_pct": _safe_round(implied, 3) if implied is not None else None,
        "historical_avg_move_pct": historical,
        "historical_n_events": n_events,
        "historical_hit_rate_positive_post": hit_rate,
        "current_signals": signals,
        "edge_score": _edge_score(implied, historical),
        "note": _build_note(event_type, implied, historical, n_events, hit_rate),
    }


# ── Top-level compute (wrapped by `catalyst_timeline` for caching) ───────

def _compute(symbols: List[str], days_ahead: int) -> Dict[str, Any]:
    today = _today()
    horizon_end = today + datetime.timedelta(days=int(days_ahead))

    # 1. Collect raw event dates per category.
    fomc_dates = _upcoming_fomc(horizon_end, today)
    opex_dates = _upcoming_opex(horizon_end, today)

    # 2. Walk every symbol; assemble events with per-symbol earnings + ex-div.
    events: List[Dict[str, Any]] = []
    errors: List[str] = []
    signal_cache: Dict[str, Dict[str, Any]] = {}

    # Decide whether the portfolio has equity exposure (FOMC otherwise
    # contributes nothing useful). Treat a non-empty symbol list as
    # equity exposure.
    has_equity = bool(symbols)

    # Per-symbol earnings + ex-div each hit upstreams — serially that's
    # O(symbols × latency) and a 20-name book took >60s cold (e2e timeout).
    # Parallelize per symbol; signal_cache entries are keyed per symbol so
    # concurrent workers touch disjoint keys.
    def _symbol_events(sym):
        ev, errs = [], []
        try:
            er = _upcoming_earnings_for(sym, horizon_end, today)
            if er is not None:
                ev.append(_make_event(sym, "earnings", er, signal_cache, today))
        except Exception as e:
            errs.append(f"earnings({sym}): {e}")
            log.debug("catalyst: earnings(%s) failed: %s", sym, e)
        try:
            ed = _upcoming_ex_div(sym, horizon_end, today)
            if ed is not None:
                ev.append(_make_event(sym, "ex_dividend", ed, signal_cache, today))
        except Exception as e:
            errs.append(f"ex_dividend({sym}): {e}")
            log.debug("catalyst: ex_div(%s) failed: %s", sym, e)
        return ev, errs

    try:
        import safe_executor
        per_sym = safe_executor.parallel_map(
            _symbol_events, list(symbols), max_workers=6,
            thread_name_prefix="catalyst")
    except Exception:
        per_sym = [_symbol_events(s) for s in symbols]
    for r in per_sym:
        if not r:
            continue
        ev, errs = r
        events.extend(ev)
        errors.extend(errs)

    # 2c. FOMC applies to every symbol when there is equity exposure.
    if has_equity:
        for fd in fomc_dates:
            for sym in symbols:
                try:
                    events.append(
                        _make_event(sym, "fed_fomc", fd, signal_cache, today)
                    )
                except Exception as e:
                    errors.append(f"fomc({sym},{fd}): {e}")
                    log.debug("catalyst: fomc(%s,%s) failed: %s", sym, fd, e)

    # 2d. OPEX — applies to every symbol (monthly equity-index expiry).
    if has_equity:
        for od in opex_dates:
            for sym in symbols:
                try:
                    events.append(
                        _make_event(sym, "opex", od, signal_cache, today)
                    )
                except Exception as e:
                    errors.append(f"opex({sym},{od}): {e}")
                    log.debug("catalyst: opex(%s,%s) failed: %s", sym, od, e)

    # 3. Sort ascending by date, then by symbol for deterministic output.
    events.sort(key=lambda e: (e["date"], e["symbol"], e["type"]))

    return {
        "as_of": today.isoformat(),
        "days_ahead": int(days_ahead),
        "symbols": list(symbols),
        "events": events,
        "errors": errors,
    }


# ── Public API ───────────────────────────────────────────────────────────

def _cache_key(symbols: List[str], days_ahead: int) -> Tuple[str, str, int]:
    """Stable cache key based on a hash of the sorted symbol list."""
    if not symbols:
        h = "_empty_"
    else:
        joined = ",".join(sorted(s.upper() for s in symbols))
        h = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]
    return ("catalyst", h, int(days_ahead))


def catalyst_timeline(symbols: Optional[Sequence[str]] = None,
                      days_ahead: int = 60) -> Dict[str, Any]:
    """Top-level entry point. See module docstring for the return shape.

    Args:
        symbols: Iterable of tickers to include. If None or empty, the
                 user's portfolio holdings are used.
        days_ahead: Forward horizon in calendar days (clamped to 1..180).
    """
    try:
        days_ahead = int(days_ahead)
    except (TypeError, ValueError):
        days_ahead = 60
    days_ahead = max(1, min(days_ahead, 180))

    syms = _normalize_symbols(symbols)
    if not syms:
        return {
            "as_of": _today().isoformat(),
            "days_ahead": days_ahead,
            "symbols": [],
            "events": [],
            "errors": ["No symbols supplied and portfolio is empty."],
        }

    if cache_store is None:
        return _compute(syms, days_ahead)

    key = _cache_key(syms, days_ahead)
    try:
        return cache_store.coalesce(
            key, _CACHE_TTL, lambda: _compute(syms, days_ahead)
        )
    except Exception as e:
        log.warning("catalyst: cache_store unavailable (%s); uncached", e)
        return _compute(syms, days_ahead)


# ── Debug entrypoint ─────────────────────────────────────────────────────

if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    if len(sys.argv) > 1:
        syms = [s.strip().upper() for s in sys.argv[1].split(",") if s.strip()]
    else:
        syms = ["AAPL", "NVDA", "SPY"]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    out = catalyst_timeline(syms, days)
    summary = {
        "as_of": out["as_of"],
        "days_ahead": out["days_ahead"],
        "symbols": out["symbols"],
        "n_events": len(out["events"]),
        "first_5_events": out["events"][:5],
        "errors": out.get("errors", [])[:5],
    }
    print(json.dumps(summary, indent=2, default=str))
