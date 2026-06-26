"""
jarvis_watches — composite alert conditions for Jarvis.

The existing price-alert table is single-condition ("NVDA above 200").
Real requests are conjunctions: "tell me if NVDA breaks $200 AND VIX is
over 25". A *watch* is a named list of 1-4 conditions with AND semantics,
evaluated by a background worker via evaluate_all(); the first cycle on
which every condition holds stamps triggered_at and disarms the watch so
it fires exactly once (rearm_watch() resets it).

Supported metrics:
  price        — live price of a symbol         (fetcher.get_quotes_batch)
  change_pct   — symbol's day change in percent (same batch call)
  vix          — VIX level                      (jarvis._market_regime)
  book_day_pct — portfolio day move in percent  (jarvis._portfolio_pulse)

Design rules, mirrored from the rest of the Jarvis stack:
  * lazy schema — CREATE TABLE IF NOT EXISTS on first touch, so importing
    this module never writes to the DB.
  * fail-open everywhere — evaluate_all() runs inside a worker loop and
    must never raise; a condition whose data source is down evaluates
    FALSE (no data is never treated as a trigger).
  * one network batch — symbols across ALL watches are collected and
    resolved with a single get_quotes_batch call per evaluation cycle.

Python 3.9 compatible.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import database as db

log = logging.getLogger("augur.jarvis_watches")

_METRICS = ("price", "change_pct", "vix", "book_day_pct")
_SYMBOL_METRICS = ("price", "change_pct")   # these require a symbol
_OPS = ("gt", "lt")

# A plausible ticker: 1-6 letters/digits, optional .X or -USD suffix (BRK.B,
# BTC-USD). Without this, _normalize_condition accepts any non-empty string
# as a symbol, so a typo'd "my favorite stock" becomes an un-quotable watch.
import re as _re
_SYM_RE = _re.compile(r"^[A-Z][A-Z0-9]{0,5}([.\-][A-Z0-9]{1,4})?$")

_MAX_CONDITIONS = 4
_MAX_NAME_LEN = 80
_MAX_ARMED = 25


# ─── schema ──────────────────────────────────────────────────────────────────

def _ensure_table() -> None:
    """Lazy CREATE TABLE — called by every public entry point."""
    with db._write_lock:
        conn = db.get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS jarvis_watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            conditions_json TEXT NOT NULL,
            armed INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            triggered_at TEXT,
            last_eval TEXT
        )""")
        conn.commit()


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─── validation / normalization ──────────────────────────────────────────────

def _normalize_condition(cond: Any) -> Union[Dict[str, Any], str]:
    """Validate one condition; return a normalized dict or an error string."""
    if not isinstance(cond, dict):
        return "each condition must be an object"

    metric = cond.get("metric")
    if metric not in _METRICS:
        return "unknown metric {!r} (expected one of {})".format(
            metric, ", ".join(_METRICS))

    op = cond.get("op")
    if op not in _OPS:
        return "unknown op {!r} (expected 'gt' or 'lt')".format(op)

    value = cond.get("value")
    # bool is an int subclass — reject it explicitly, "value": true is
    # always a caller bug, not a threshold.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "value must be a number"
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return "value must be finite"

    symbol = cond.get("symbol")
    if metric in _SYMBOL_METRICS:
        if not isinstance(symbol, str) or not symbol.strip():
            return "metric {!r} requires a symbol".format(metric)
        symbol = symbol.strip().upper()
        if not _SYM_RE.match(symbol):
            return "{!r} is not a valid ticker symbol".format(symbol)
    else:
        # vix / book_day_pct are global metrics — a symbol, if sent,
        # is meaningless; drop it rather than reject the whole watch.
        symbol = None

    return {"metric": metric, "symbol": symbol, "op": op, "value": value}


def _normalize_conditions(conditions: Any) -> Union[List[Dict[str, Any]], str]:
    """Validate the full list; return normalized list or an error string."""
    if not isinstance(conditions, (list, tuple)) or not conditions:
        return "conditions must be a non-empty list"
    if len(conditions) > _MAX_CONDITIONS:
        return "too many conditions (max {})".format(_MAX_CONDITIONS)
    out = []
    for i, cond in enumerate(conditions):
        norm = _normalize_condition(cond)
        if isinstance(norm, str):
            return "condition {}: {}".format(i + 1, norm)
        out.append(norm)
    return out


# ─── describe ────────────────────────────────────────────────────────────────

def _fmt_num(v: Any) -> str:
    # %g trims trailing zeros: 200.0 -> "200", 2.5 -> "2.5".
    try:
        return "{:g}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


def _describe_condition(cond: Dict[str, Any]) -> str:
    op = ">" if cond.get("op") == "gt" else "<"
    val = _fmt_num(cond.get("value"))
    metric = cond.get("metric")
    sym = cond.get("symbol") or "?"
    if metric == "price":
        return "{} price {} ${}".format(sym, op, val)
    if metric == "change_pct":
        return "{} day change {} {}%".format(sym, op, val)
    if metric == "vix":
        return "VIX {} {}".format(op, val)
    if metric == "book_day_pct":
        return "portfolio day move {} {}%".format(op, val)
    return "{} {} {}".format(metric, op, val)


def describe(watch_or_conditions: Union[Dict[str, Any], List[Dict[str, Any]]]) -> str:
    """Human-readable summary: 'NVDA price > $200 AND VIX > 25'."""
    try:
        if isinstance(watch_or_conditions, dict):
            conditions = watch_or_conditions.get("conditions") or []
        else:
            conditions = watch_or_conditions or []
        return " AND ".join(_describe_condition(c) for c in conditions
                            if isinstance(c, dict))
    except Exception:
        return ""


# ─── CRUD ────────────────────────────────────────────────────────────────────

def add_watch(name: Any, conditions: Any) -> Dict[str, Any]:
    """Create a watch. Returns {"ok": True, "id": n} or {"error": str}."""
    try:
        if not isinstance(name, str) or not name.strip():
            return {"error": "name is required"}
        name = name.strip()
        if len(name) > _MAX_NAME_LEN:
            return {"error": "name too long (max {} chars)".format(_MAX_NAME_LEN)}

        norm = _normalize_conditions(conditions)
        if isinstance(norm, str):
            return {"error": norm}

        _ensure_table()
        with db._write_lock:
            conn = db.get_conn()
            # Cap on ARMED watches only — triggered (disarmed) history rows
            # shouldn't block the user from adding new watches.
            armed_count = conn.execute(
                "SELECT COUNT(*) FROM jarvis_watches WHERE armed = 1"
            ).fetchone()[0]
            if armed_count >= _MAX_ARMED:
                return {"error": "watch limit reached ({} armed)".format(_MAX_ARMED)}
            cur = conn.execute(
                "INSERT INTO jarvis_watches (name, conditions_json) VALUES (?, ?)",
                (name, json.dumps(norm)))
            conn.commit()
            return {"ok": True, "id": cur.lastrowid}
    except Exception as e:
        log.warning("add_watch failed: %s", e)
        return {"error": "could not save watch"}


def list_watches() -> List[Dict[str, Any]]:
    """All watches (armed and triggered), conditions parsed + described."""
    try:
        _ensure_table()
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT * FROM jarvis_watches ORDER BY id").fetchall()
        out = []
        for r in rows:
            w = dict(r)
            try:
                w["conditions"] = json.loads(w.pop("conditions_json"))
            except Exception:
                w["conditions"] = []
            w["description"] = describe(w)
            out.append(w)
        return out
    except Exception as e:
        log.warning("list_watches failed: %s", e)
        return []


def delete_watch(watch_id: Any) -> bool:
    try:
        _ensure_table()
        with db._write_lock:
            conn = db.get_conn()
            cur = conn.execute(
                "DELETE FROM jarvis_watches WHERE id = ?", (watch_id,))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        log.warning("delete_watch failed: %s", e)
        return False


def rearm_watch(watch_id: Any) -> bool:
    """Reset a triggered watch so it can fire again."""
    try:
        _ensure_table()
        with db._write_lock:
            conn = db.get_conn()
            cur = conn.execute(
                "UPDATE jarvis_watches SET armed = 1, triggered_at = NULL "
                "WHERE id = ?", (watch_id,))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        log.warning("rearm_watch failed: %s", e)
        return False


# ─── evaluation ──────────────────────────────────────────────────────────────

def _crypto_symbols() -> set:
    """Symbols held as crypto in the user's book — quoted as SYM-USD."""
    try:
        return {h["symbol"].upper() for h in db.get_portfolio()
                if isinstance(h, dict) and h.get("asset_type") == "crypto"
                and h.get("symbol")}
    except Exception:
        return set()


def _fetch_quotes(symbols: List[str], crypto: set) -> Dict[str, Dict[str, Any]]:
    """One batched quote fetch, keyed back to the user-facing symbol.

    Crypto holdings are stored bare (BTC) but yfinance quotes them as
    BTC-USD, the same mapping _portfolio_pulse uses.
    """
    if not symbols:
        return {}
    fetch_map = {}  # fetch symbol -> user symbol
    for sym in symbols:
        fetch_sym = sym + "-USD" if sym in crypto else sym
        fetch_map[fetch_sym.upper()] = sym
    try:
        import fetcher  # lazy — keeps module import light and fail-open
        raw = fetcher.get_quotes_batch(list(fetch_map.keys())) or {}
    except Exception as e:
        log.warning("watch quote fetch failed: %s", e)
        return {}
    out = {}
    for fetch_sym, user_sym in fetch_map.items():
        q = raw.get(fetch_sym)
        if isinstance(q, dict):
            out[user_sym] = q

    # -USD fallback for non-held coins. _crypto_symbols() only knows symbols
    # currently in the book, so a watch on a coin the user doesn't hold (DOGE)
    # quotes the bare ticker — often an unrelated equity/trust (the same
    # lesson as jarvis_counterfactual._price_now). For any symbol whose bare
    # quote came back missing or non-positive, retry it as SYM-USD.
    retry = {}  # fetch symbol (SYM-USD) -> user symbol
    for sym in symbols:
        if sym in crypto or sym.upper().endswith("-USD"):
            continue
        q = out.get(sym)
        price = (q or {}).get("price") if isinstance(q, dict) else None
        chg = (q or {}).get("change_pct") if isinstance(q, dict) else None
        bad_price = (not isinstance(price, (int, float))
                     or isinstance(price, bool) or price <= 0)
        # A bare ticker can resolve to an unrelated equity that returns a price
        # but no/garbled change_pct (e.g. a coin the user doesn't hold). Retry
        # SYM-USD when EITHER price OR change_pct is missing so change_pct
        # watches don't silently evaluate against the wrong asset.
        bad_chg = not isinstance(chg, (int, float)) or isinstance(chg, bool)
        if bad_price or bad_chg:
            retry[(sym + "-USD").upper()] = sym
    if retry:
        try:
            raw2 = fetcher.get_quotes_batch(list(retry.keys())) or {}
        except Exception as e:
            log.warning("watch -USD fallback fetch failed: %s", e)
            raw2 = {}
        for fetch_sym, user_sym in retry.items():
            q = raw2.get(fetch_sym)
            qp = (q or {}).get("price") if isinstance(q, dict) else None
            if not (isinstance(qp, (int, float)) and not isinstance(qp, bool)
                    and qp > 0):
                continue
            existing = out.get(user_sym)
            ep = (existing or {}).get("price") if isinstance(existing, dict) else None
            existing_price_ok = (isinstance(ep, (int, float))
                                 and not isinstance(ep, bool) and ep > 0)
            if existing_price_ok:
                # The bare quote already has a valid positive price — only the
                # change_pct was missing. Don't overwrite a legitimate equity
                # quote with a possibly-unrelated -USD instrument; just fill the
                # missing change_pct field from the retry.
                ec = existing.get("change_pct")
                if not (isinstance(ec, (int, float)) and not isinstance(ec, bool)):
                    rc = q.get("change_pct")
                    if isinstance(rc, (int, float)) and not isinstance(rc, bool):
                        existing["change_pct"] = rc
            else:
                out[user_sym] = q
    return out


def _fetch_vix() -> Optional[float]:
    try:
        import jarvis  # lazy
        regime = jarvis._market_regime() or {}
        return regime.get("vix")
    except Exception as e:
        log.warning("watch vix fetch failed: %s", e)
        return None


def _fetch_book_day_pct() -> Optional[float]:
    try:
        import jarvis  # lazy
        pulse = jarvis._portfolio_pulse() or {}
        return pulse.get("day_pnl_pct")
    except Exception as e:
        log.warning("watch pulse fetch failed: %s", e)
        return None


def _condition_holds(cond: Dict[str, Any],
                     quotes: Dict[str, Dict[str, Any]],
                     vix: Optional[float],
                     book_day_pct: Optional[float]) -> bool:
    """True only when the metric resolved AND the comparison holds.

    Missing/None data is FALSE by design: a dead feed must never wake the
    user up ("no data" is not the same as "NVDA broke $200").
    """
    metric = cond.get("metric")
    if metric == "price":
        actual = (quotes.get(cond.get("symbol")) or {}).get("price")
    elif metric == "change_pct":
        actual = (quotes.get(cond.get("symbol")) or {}).get("change_pct")
    elif metric == "vix":
        actual = vix
    elif metric == "book_day_pct":
        actual = book_day_pct
    else:
        return False
    try:
        if actual is None:
            return False
        actual = float(actual)
        value = float(cond.get("value"))
    except (TypeError, ValueError):
        return False
    return actual > value if cond.get("op") == "gt" else actual < value


def evaluate_all() -> List[Dict[str, Any]]:
    """Evaluate every ARMED watch; return the ones that just triggered.

    Runs inside a background worker loop — never raises. Each watch that
    fully matches is stamped triggered_at (UTC) and disarmed so it fires
    once; every armed watch gets last_eval updated either way.
    """
    try:
        _ensure_table()
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT * FROM jarvis_watches WHERE armed = 1 ORDER BY id"
        ).fetchall()
        if not rows:
            return []

        # Parse up front so we can plan ONE batched quote fetch across all
        # watches and only touch VIX / portfolio pulse if actually needed.
        watches = []
        symbols, need_vix, need_book = [], False, False
        for r in rows:
            w = dict(r)
            try:
                w["conditions"] = json.loads(w.pop("conditions_json"))
            except Exception:
                # Corrupt row: keep it visible in last_eval, never trigger.
                w["conditions"] = None
            watches.append(w)
            for c in (w["conditions"] or []):
                if not isinstance(c, dict):
                    continue
                metric = c.get("metric")
                if metric in _SYMBOL_METRICS and c.get("symbol"):
                    if c["symbol"] not in symbols:
                        symbols.append(c["symbol"])
                elif metric == "vix":
                    need_vix = True
                elif metric == "book_day_pct":
                    need_book = True

        quotes = _fetch_quotes(symbols, _crypto_symbols()) if symbols else {}
        vix = _fetch_vix() if need_vix else None
        book_day_pct = _fetch_book_day_pct() if need_book else None

        now = _now_utc()
        triggered: List[Dict[str, Any]] = []
        with db._write_lock:
            for w in watches:
                conds = w["conditions"]
                hit = bool(conds) and all(
                    isinstance(c, dict)
                    and _condition_holds(c, quotes, vix, book_day_pct)
                    for c in conds)
                try:
                    if hit:
                        # Guard on armed = 1 so a watch deleted/rearmed between
                        # the unlocked SELECT and here isn't (re-)triggered, and
                        # two evaluators can't double-fire the same watch.
                        cur = conn.execute(
                            "UPDATE jarvis_watches SET armed = 0, "
                            "triggered_at = ?, last_eval = ? "
                            "WHERE id = ? AND armed = 1",
                            (now, now, w["id"]))
                        if cur.rowcount > 0:
                            w["armed"] = 0
                            w["triggered_at"] = now
                            w["last_eval"] = now
                            w["conditions"] = conds or []
                            w["description"] = describe(w)
                            triggered.append(w)
                    else:
                        conn.execute(
                            "UPDATE jarvis_watches SET last_eval = ? "
                            "WHERE id = ?", (now, w["id"]))
                except Exception as e:
                    log.warning("watch %s update failed: %s", w.get("id"), e)
            conn.commit()
        return triggered
    except Exception as e:
        log.warning("evaluate_all failed: %s", e)
        return []
