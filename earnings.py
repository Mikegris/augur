"""
Earnings Catalyst Intelligence — pre-earnings dossier builder.

Aggregates: next earnings date, EPS consensus, beat/miss history,
post-earnings price moves, options implied move, insider activity context.
"""

import datetime
import logging
import time
import numpy as np
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(v):
    try:
        f = float(v)
        return None if (f != f) else f  # NaN check
    except (TypeError, ValueError):
        return None


def _date_str(d):
    """Convert various date types to YYYY-MM-DD string."""
    if d is None:
        return None
    if hasattr(d, "date"):
        return d.date().isoformat()
    s = str(d)
    return s[:10]


# ── Core functions ─────────────────────────────────────────────────────────────

def _calendar_for_one(symbol):
    """Fetch the calendar slice for a single symbol with cache_store caching.

    Earnings dates move at most once per quarter; a per-symbol 6h cache
    means the daily portfolio sweep only hits Yahoo when the previous
    answer has actually had time to change. The previous implementation
    had no caching at all and re-hit yfinance for every symbol on every
    /api/earnings/calendar request, which routinely tripped Yahoo's rate
    limit on portfolios with >20 tickers.
    """
    try:
        import cache_store
        ck = ("earnings_cal_one", symbol.upper())
        hit = cache_store.cache_get(ck, ttl=6 * 3600)
        if hit is not None:
            return hit
    except Exception:
        cache_store = None
        ck = None

    today = datetime.date.today()
    try:
        t = yf.Ticker(symbol)
        cal = t.calendar
        if not cal:
            return None

        dates_list = cal.get("Earnings Date", [])
        if not dates_list:
            return None

        next_earnings = None
        for d in dates_list:
            d_date = d.date() if hasattr(d, "date") else datetime.date.fromisoformat(str(d)[:10])
            if d_date >= today:
                next_earnings = d_date
                break

        if next_earnings is None:
            return None

        days_until = (next_earnings - today).days

        beat_rate = None
        avg_surprise = None
        try:
            eh = t.earnings_history
            if eh is not None and not eh.empty:
                # Pair actual & estimate per row first. Dropping NaNs from each
                # column independently and zip-ing positionally misaligns
                # quarters whenever one column has a gap, and divides by the
                # wrong (un-truncated) count.
                paired = eh[["epsActual", "epsEstimate"]].dropna()
                if len(paired) >= 2:
                    beats = int((paired["epsActual"] > paired["epsEstimate"]).sum())
                    beat_rate = round(beats / len(paired) * 100)
                    # Align surprises to the same quarters as beat_rate (paired
                    # index) instead of the full, independently-NaN'd column.
                    surprises = eh.loc[paired.index, "surprisePercent"].dropna().tolist()
                    if surprises:
                        avg_surprise = round(float(np.mean(surprises)) * 100, 2)
        except Exception:
            pass

        entry = {
            "symbol": symbol.upper(),
            "earnings_date": next_earnings.isoformat(),
            "days_until": days_until,
            "eps_estimate": _safe_float(cal.get("Earnings Average")),
            "eps_low": _safe_float(cal.get("Earnings Low")),
            "eps_high": _safe_float(cal.get("Earnings High")),
            "beat_rate": beat_rate,
            "avg_surprise_pct": avg_surprise,
        }
        if cache_store is not None and ck is not None:
            try:
                cache_store.cache_set(ck, entry, ttl=6 * 3600)
            except Exception:
                pass
        return entry
    except Exception as e:
        logger.debug("calendar skip %s: %s", symbol, e)
        return None


def get_earnings_calendar(symbols):
    """
    For a list of symbols, return upcoming earnings dates with basic metadata.
    Skips symbols with no upcoming earnings or no options (ETFs, etc).
    Returns list sorted by earnings date ascending.

    Per-symbol results are cached for 6h via `_calendar_for_one`, so a
    portfolio of N tickers normally costs ~0 Yahoo calls between refreshes
    rather than 4N (calendar+earnings_history per symbol).
    """
    results = []
    cache_misses = 0

    for symbol in symbols:
        # Only sleep between *upstream* calls (cache misses). The old
        # unconditional 0.15s/symbol stalled the whole request by N*150ms
        # even when every entry was already cached.
        try:
            import cache_store
            cached = cache_store.cache_get(("earnings_cal_one", symbol.upper()))
        except Exception:
            cached = None
        if cached is None:
            if cache_misses > 0:
                time.sleep(0.15)  # light rate limiting between upstream hits
            cache_misses += 1

        entry = _calendar_for_one(symbol)
        if entry is not None:
            results.append(entry)

    results.sort(key=lambda x: x["earnings_date"])
    return results


def get_earnings_dossier(symbol):
    """
    Full pre-earnings dossier for one symbol. Assembles:
      - Calendar: next date, EPS consensus
      - History: beat/miss rate, avg surprise, last 8 actuals vs estimates
      - Post-earnings moves: last 8 quarters (calculated from price history)
      - Options implied move: ATM straddle on first expiry after earnings
      - Insider activity: net buy/sell summary (last 60 days)

    Returns a dict ready for AI brief generation.
    """
    today = datetime.date.today()
    symbol = symbol.upper()
    t = yf.Ticker(symbol)

    dossier = {
        "symbol": symbol,
        "generated_at": today.isoformat(),
    }

    # ── 1. Calendar ──────────────────────────────────────────────────────────
    try:
        cal = t.calendar or {}
        dates_list = cal.get("Earnings Date", [])
        next_earnings = None
        for d in dates_list:
            d_date = d.date() if hasattr(d, "date") else datetime.date.fromisoformat(str(d)[:10])
            if d_date >= today:
                next_earnings = d_date
                break

        dossier["earnings_date"] = next_earnings.isoformat() if next_earnings else None
        dossier["days_until"] = (next_earnings - today).days if next_earnings else None
        dossier["eps_estimate"] = _safe_float(cal.get("Earnings Average"))
        dossier["eps_low"] = _safe_float(cal.get("Earnings Low"))
        dossier["eps_high"] = _safe_float(cal.get("Earnings High"))
        dossier["revenue_estimate"] = _safe_float(cal.get("Revenue Average"))
    except Exception as e:
        logger.error("dossier calendar %s: %s", symbol, e)
        dossier["earnings_date"] = None

    # ── 2. Earnings history ───────────────────────────────────────────────────
    history_rows = []
    beat_rate = None
    avg_surprise = None
    avg_beat_magnitude = None

    try:
        ed = t.earnings_dates
        if ed is not None and not ed.empty:
            past = ed[ed["Reported EPS"].notna()].copy()
            if not past.empty:
                beats = int((past["Reported EPS"] > past["EPS Estimate"]).sum())
                total_q = len(past)
                beat_rate = round(beats / total_q * 100) if total_q else None

                surprises = past["Surprise(%)"].dropna()
                if not surprises.empty:
                    avg_surprise = round(float(surprises.mean()), 2)
                    beat_only = surprises[surprises > 0]
                    avg_beat_magnitude = round(float(beat_only.mean()), 2) if not beat_only.empty else None

                # Last 8 quarters for table
                for idx, row in past.head(8).iterrows():
                    _actual = _safe_float(row["Reported EPS"])
                    _est = _safe_float(row["EPS Estimate"])
                    history_rows.append({
                        "date": _date_str(idx),
                        "estimate": _est,
                        "actual": _actual,
                        "surprise_pct": _safe_float(row["Surprise(%)"]),
                        # Parenthesize the comparison — the previous expression
                        # `bool(actual or 0 > estimate or 0)` parsed as
                        # `bool(actual or (0 > estimate) or 0)`, which is True
                        # whenever actual is truthy regardless of the estimate.
                        "beat": (_actual or 0) > (_est or 0),
                    })
    except Exception as e:
        logger.error("dossier history %s: %s", symbol, e)

    dossier["beat_rate"] = beat_rate
    dossier["avg_surprise_pct"] = avg_surprise
    dossier["avg_beat_magnitude"] = avg_beat_magnitude
    dossier["history"] = history_rows

    # ── 3. Post-earnings price moves ──────────────────────────────────────────
    post_moves = []
    avg_abs_move = None

    try:
        # Route price history through fetcher so we inherit the Yahoo
        # direct-chart fallback when yfinance's crumb auth breaks.
        import fetcher as _f
        import pandas as _pd
        _bars = _f.get_chart_data(symbol, period="3y", interval="1d")
        if _bars:
            price_hist = _pd.DataFrame(_bars)
            price_hist["Date"] = _pd.to_datetime(price_hist["time"], unit="s")
            price_hist = price_hist.set_index("Date")
            price_hist = price_hist.rename(columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume",
            })
        else:
            price_hist = _pd.DataFrame()
        if not price_hist.empty:
            # Use earnings_dates index for past dates
            ed2 = t.earnings_dates
            if ed2 is not None and not ed2.empty:
                past_dates = ed2[ed2["Reported EPS"].notna()].index.tolist()
                for dt in past_dates[:8]:
                    try:
                        # Convert to tz-naive for comparison
                        dt_naive = dt.tz_localize(None) if dt.tzinfo else dt
                        idx = price_hist.index.tz_localize(None).searchsorted(dt_naive)
                        # Measure the POST-earnings reaction (announcement day ->
                        # next session), not the run-up into it. The old idx-1 ->
                        # idx window ended on the earnings day itself.
                        if 0 <= idx and idx + 1 < len(price_hist):
                            before = float(price_hist["Close"].iloc[idx])
                            after = float(price_hist["Close"].iloc[idx + 1])
                            move_pct = round((after - before) / before * 100, 2) if before else None
                            post_moves.append({
                                "date": _date_str(dt),
                                "move_pct": move_pct,
                                "direction": "UP" if move_pct >= 0 else "DOWN",
                            })
                    except Exception:
                        continue

        if post_moves:
            avg_abs_move = round(float(np.mean([abs(m["move_pct"]) for m in post_moves])), 2)
    except Exception as e:
        logger.error("dossier moves %s: %s", symbol, e)

    dossier["post_earnings_moves"] = post_moves
    dossier["avg_abs_move_pct"] = avg_abs_move

    # ── 4. Options implied move ───────────────────────────────────────────────
    implied_move_pct = None
    options_expiry_used = None
    straddle_cost = None
    current_price = None

    try:
        fast = t.fast_info
        current_price = _safe_float(fast.last_price)
        expiries = t.options

        if expiries and current_price and dossier.get("earnings_date"):
            earnings_dt = dossier["earnings_date"]
            # Find first expiry on or after earnings date
            target_expiry = None
            for e in expiries:
                if e >= earnings_dt:
                    target_expiry = e
                    break

            if target_expiry:
                chain = t.option_chain(target_expiry)
                calls = chain.calls
                puts = chain.puts

                # ATM = closest strike to current price
                atm_call = calls.iloc[(calls["strike"] - current_price).abs().argsort()[:1]]
                atm_put = puts.iloc[(puts["strike"] - current_price).abs().argsort()[:1]]

                c_price = _safe_float(atm_call["lastPrice"].iloc[0])
                p_price = _safe_float(atm_put["lastPrice"].iloc[0])

                if c_price and p_price and current_price:
                    straddle_cost = round(c_price + p_price, 2)
                    implied_move_pct = round(straddle_cost / current_price * 100, 2)
                    options_expiry_used = target_expiry
    except Exception as e:
        logger.error("dossier options %s: %s", symbol, e)

    dossier["current_price"] = current_price
    dossier["implied_move_pct"] = implied_move_pct
    dossier["straddle_cost"] = straddle_cost
    dossier["options_expiry_used"] = options_expiry_used

    # IV vs historical: positive = options overpricing move, negative = underpricing
    if implied_move_pct and avg_abs_move:
        dossier["iv_vs_historical"] = round(implied_move_pct - avg_abs_move, 2)
    else:
        dossier["iv_vs_historical"] = None

    # ── 5. Insider activity (last 60 days) ────────────────────────────────────
    insider_summary = {"buys": 0, "sells": 0, "net_buy_value": 0, "signal": "NEUTRAL"}
    try:
        import sec_edgar as edgar
        transactions = edgar.get_form4_transactions(symbol, limit=20)
        cutoff = (today - datetime.timedelta(days=60)).isoformat()
        recent = [tx for tx in transactions if tx.get("date", "") >= cutoff]

        buys = [tx for tx in recent if tx.get("transaction_type") == "BUY"]
        sells = [tx for tx in recent if tx.get("transaction_type") == "SELL"]
        buy_val = sum(tx.get("value", 0) or 0 for tx in buys)
        sell_val = sum(tx.get("value", 0) or 0 for tx in sells)

        insider_summary = {
            "buys": len(buys),
            "sells": len(sells),
            "buy_value": round(buy_val, 0),
            "sell_value": round(sell_val, 0),
            "net_buy_value": round(buy_val - sell_val, 0),
            "signal": "BULLISH" if buy_val > sell_val * 1.5 and len(buys) >= 1
                      else "BEARISH" if sell_val > buy_val * 2
                      else "NEUTRAL",
        }
    except Exception as e:
        logger.debug("insider data unavailable for %s: %s", symbol, e)

    dossier["insider_activity"] = insider_summary

    # ── 6. Company name ──────────────────────────────────────────────────────
    try:
        dossier["name"] = t.fast_info.get("shortName") or t.info.get("shortName") or symbol
    except Exception:
        dossier["name"] = symbol

    return dossier
