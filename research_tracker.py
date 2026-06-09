"""
Live signal-tracking ledger for AUGUR.

Every signal-producing module (smart_money, ml_forecast, gex_engine,
narrative_engine, …) calls `log_forecast(...)` at issue time. A background
scoring job (`score_due_forecasts`) re-prices each call once its horizon
elapses, computes the realized return, and decides whether the direction
"hit". `get_track_record(signal_name)` then returns aggregate statistics
that the UI surfaces as a small inline badge — e.g.

    [●] 58% (n=143)   <- last 100: 58% hit, +2.3% avg

The module owns its own SQLite table (`signal_forecasts`) and never edits
`database.py` so it can ship as a self-contained add-on. It reuses the
same DB file via the `AUGUR_DB_PATH` env var so a single bundled .app
keeps one wealth.db.

Python 3.9 compatible.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("augur.tracker")

# Same DB file as database.py — must read AUGUR_DB_PATH dynamically each call
# so test harnesses that override it after import still hit the right file.
def _db_path() -> str:
    return os.environ.get("AUGUR_DB_PATH", "wealth.db")


# Serialize writes the same way database.py does. SQLite is fine with
# concurrent reads but a compute-then-update race in score_due_forecasts
# would otherwise double-score a row if two warmers fire simultaneously.
_write_lock = threading.RLock()

# Thread-local connection pool — same pattern as database.py.
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != _db_path():
        # Path may have changed (tests). Drop any cached connection.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        conn = sqlite3.connect(_db_path(), check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # Match database.py PRAGMAs so we're a good citizen on the shared file.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
        _local.path = _db_path()
    return conn


# ── schema ──────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signal_forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    horizon_days INTEGER NOT NULL,
    predicted_direction TEXT,
    predicted_magnitude REAL,
    confidence REAL,
    metadata TEXT,
    scored_at TEXT,
    realized_return REAL,
    hit INTEGER,
    issue_price REAL,
    score_price REAL
);
CREATE INDEX IF NOT EXISTS idx_signal_due ON signal_forecasts(scored_at, issued_at);
CREATE INDEX IF NOT EXISTS idx_signal_name ON signal_forecasts(signal_name, scored_at);
"""


def init_tracker_db() -> None:
    """Create the signal_forecasts table + indexes if missing.

    Idempotent — safe to call from app startup, tests, or the warmer.
    """
    with _write_lock:
        conn = _get_conn()
        for stmt in _SCHEMA_SQL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.commit()


# ── core API ────────────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    # ISO with trailing 'Z' so it sorts lexicographically.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_forecast(
    signal_name: str,
    symbol: str,
    horizon_days: int,
    direction: Optional[str],
    magnitude: Optional[float],
    confidence: Optional[float],
    issue_price: Optional[float],
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """Insert a forecast row and return its rowid.

    No-ops (returns None) on missing required fields rather than raising,
    so a buggy signal-producer can't crash the panel that calls it.
    """
    if not signal_name or not symbol or horizon_days is None:
        return None
    try:
        meta_json = json.dumps(metadata, default=str) if metadata else None
    except Exception:
        meta_json = None

    direction_norm: Optional[str] = None
    if direction is not None:
        direction_norm = str(direction).upper().strip() or None

    # Cheap auto-init keeps callers from having to remember to bootstrap
    # the schema before the first log call. CREATE TABLE IF NOT EXISTS is
    # idempotent and trivially fast after the first hit.
    init_tracker_db()

    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            """
            INSERT INTO signal_forecasts
              (signal_name, symbol, issued_at, horizon_days,
               predicted_direction, predicted_magnitude, confidence,
               metadata, issue_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_name,
                symbol.upper(),
                _utc_now_iso(),
                int(horizon_days),
                direction_norm,
                _safe_float(magnitude),
                _safe_float(confidence),
                meta_json,
                _safe_float(issue_price),
            ),
        )
        conn.commit()
        return int(cur.lastrowid) if cur.lastrowid is not None else None


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse our own ISO strings — tolerate the trailing 'Z' and naïve fmts."""
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def _close_price_on_or_before(symbol: str, target: datetime) -> Optional[float]:
    """Fetch the latest daily close at-or-before `target` via fetcher.get_chart_data.

    Returns None if fetcher isn't importable or no data is available.
    """
    try:
        import fetcher  # type: ignore
    except Exception:
        return None
    try:
        # 2y covers any horizon we'd realistically score. Daily bars.
        bars = fetcher.get_chart_data(symbol, "2y", "1d") or []
    except Exception:
        return None
    if not bars:
        return None
    target_ts = int(target.timestamp())
    last_close: Optional[float] = None
    for bar in bars:
        ts = bar.get("time")
        if ts is None:
            continue
        if int(ts) <= target_ts:
            c = bar.get("close")
            if c is not None:
                last_close = float(c)
        else:
            break
    if last_close is not None:
        return last_close
    # Target may pre-date all bars (rare); fall back to the first bar's close.
    first_close = bars[0].get("close")
    return float(first_close) if first_close is not None else None


def _direction_matches(direction: Optional[str], realized_return: Optional[float]) -> Optional[int]:
    """Return 1 if direction matches the sign of realized_return, else 0.

    None if the direction isn't a tradeable BUY/SELL/LONG/SHORT/UP/DOWN (e.g.
    NEUTRAL/HOLD calls are recorded but not counted as hits or misses).
    """
    if direction is None or realized_return is None:
        return None
    d = direction.upper().strip()
    bull = {"BUY", "LONG", "UP", "BULL", "LONG GAMMA", "STRONG BUY"}
    bear = {"SELL", "SHORT", "DOWN", "BEAR", "SHORT GAMMA", "STRONG SELL"}
    if d in bull:
        return 1 if realized_return > 0 else 0
    if d in bear:
        return 1 if realized_return < 0 else 0
    return None  # NEUTRAL / HOLD / unknown — recorded, not scored as hit/miss


def score_due_forecasts(max_rows: int = 200) -> Dict[str, int]:
    """Walk unscored rows whose horizon has elapsed; price each one.

    Returns a small summary: {'scored': n_priced, 'errors': n_failed}.
    Capped at `max_rows` per call so a long backlog can't lock the DB
    for minutes when the warmer first wakes up.
    """
    init_tracker_db()
    scored = 0
    errors = 0
    now = datetime.now(timezone.utc)

    # Read candidates outside the write lock — readers don't block writers.
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT id, signal_name, symbol, issued_at, horizon_days,
               predicted_direction, issue_price
          FROM signal_forecasts
         WHERE scored_at IS NULL
         ORDER BY issued_at ASC
         LIMIT ?
        """,
        (int(max_rows),),
    ).fetchall()

    for row in rows:
        issued = _parse_iso(row["issued_at"])
        if issued is None:
            errors += 1
            continue
        due_at = issued + timedelta(days=int(row["horizon_days"]))
        if due_at > now:
            # NOTE: rows arrive sorted by issued_at, *not* by due_at, so we
            # can't `break` here — a later-issued forecast with a shorter
            # horizon could still be due. Skip just this row and keep going.
            continue

        issue_price = _safe_float(row["issue_price"])
        if issue_price is None:
            # Try to backfill from the chart at issue time.
            issue_price = _close_price_on_or_before(row["symbol"], issued)
        score_price = _close_price_on_or_before(row["symbol"], due_at)

        if not issue_price or not score_price:
            # Don't mark scored — try again next cycle.
            errors += 1
            continue

        realized = (score_price - issue_price) / issue_price
        hit = _direction_matches(row["predicted_direction"], realized)

        with _write_lock:
            try:
                _get_conn().execute(
                    """
                    UPDATE signal_forecasts
                       SET scored_at = ?, realized_return = ?, hit = ?,
                           issue_price = COALESCE(issue_price, ?),
                           score_price = ?
                     WHERE id = ?
                    """,
                    (
                        _utc_now_iso(),
                        float(realized),
                        hit,
                        float(issue_price),
                        float(score_price),
                        int(row["id"]),
                    ),
                )
                _get_conn().commit()
                scored += 1
            except Exception as e:
                log.debug("scoring row %s failed: %s", row["id"], e)
                errors += 1

    return {"scored": scored, "errors": errors}


# ── aggregations for the UI ─────────────────────────────────────────────────

def _stats_for_rows(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    """Compute hit-rate / avg-return / Sharpe-of-signal-returns from scored rows."""
    # Only consider rows where hit is non-null (i.e. directional calls). Avg
    # return uses ALL scored rows, even neutral ones, because a neutral call
    # still has a realized return.
    directional = [r for r in rows if r["hit"] is not None]
    returns = [r["realized_return"] for r in rows if r["realized_return"] is not None]

    n_dir = len(directional)
    hit_rate = (sum(r["hit"] for r in directional) / n_dir) if n_dir else None

    if returns:
        avg = sum(returns) / len(returns)
        if len(returns) > 1:
            var = sum((x - avg) ** 2 for x in returns) / (len(returns) - 1)
            sd = math.sqrt(var) if var > 0 else 0.0
            sharpe = (avg / sd) if sd > 0 else None
        else:
            sharpe = None
    else:
        avg = None
        sharpe = None

    # Last-100 hit rate — use scored_at order. The caller already passed
    # `rows` in chronological order; take the tail directional 100.
    last_100 = directional[-100:]
    n_last = len(last_100)
    last_100_hit = (sum(r["hit"] for r in last_100) / n_last) if n_last else None

    return {
        "n": len(rows),
        "n_directional": n_dir,
        "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
        "avg_return": round(avg, 5) if avg is not None else None,
        "sharpe_of_signal_returns": round(sharpe, 4) if sharpe is not None else None,
        "last_100_hit_rate": round(last_100_hit, 4) if last_100_hit is not None else None,
    }


def get_track_record(
    signal_name: str,
    since: Optional[str] = None,
    symbol: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate stats for a signal. Empty record if nothing's scored yet."""
    init_tracker_db()
    conn = _get_conn()
    query = [
        "SELECT hit, realized_return, scored_at FROM signal_forecasts",
        "WHERE signal_name = ? AND scored_at IS NOT NULL",
    ]
    params: List[Any] = [signal_name]
    if since:
        query.append("AND scored_at >= ?")
        params.append(since)
    if symbol:
        query.append("AND symbol = ?")
        params.append(symbol.upper())
    query.append("ORDER BY scored_at ASC")
    rows = conn.execute(" ".join(query), params).fetchall()
    stats = _stats_for_rows(rows)
    stats["signal_name"] = signal_name
    if symbol:
        stats["symbol"] = symbol.upper()
    return stats


def get_recent_calls(signal_name: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Latest scored calls, newest first. UI table feed."""
    init_tracker_db()
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT id, signal_name, symbol, issued_at, scored_at, horizon_days,
               predicted_direction, predicted_magnitude, confidence,
               realized_return, hit, issue_price, score_price
          FROM signal_forecasts
         WHERE signal_name = ? AND scored_at IS NOT NULL
         ORDER BY scored_at DESC
         LIMIT ?
        """,
        (signal_name, int(limit)),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "signal_name": r["signal_name"],
            "symbol": r["symbol"],
            "issued_at": r["issued_at"],
            "scored_at": r["scored_at"],
            "horizon_days": r["horizon_days"],
            "direction": r["predicted_direction"],
            "predicted_magnitude": r["predicted_magnitude"],
            "confidence": r["confidence"],
            "realized_return": r["realized_return"],
            "hit": r["hit"],
            "issue_price": r["issue_price"],
            "score_price": r["score_price"],
        })
    return out


def get_scored_rows(
    signal_name: str,
    since: Optional[str] = None,
    symbol: Optional[str] = None,
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    """Scored rows for a signal, chronological, with metadata JSON parsed.

    Unlike get_recent_calls (newest-first, no metadata), this returns the
    full scored history with the parsed `metadata` dict, which downstream
    calibration code (forecast_accountability) needs to recover the
    predicted probability for Brier scoring.
    """
    init_tracker_db()
    conn = _get_conn()
    q = [
        "SELECT signal_name, symbol, issued_at, scored_at, horizon_days,",
        "predicted_direction, confidence, realized_return, hit, metadata",
        "FROM signal_forecasts WHERE signal_name = ? AND scored_at IS NOT NULL",
    ]
    params: List[Any] = [signal_name]
    if since:
        q.append("AND scored_at >= ?")
        params.append(since)
    if symbol:
        q.append("AND symbol = ?")
        params.append(symbol.upper())
    q.append("ORDER BY scored_at ASC LIMIT ?")
    params.append(int(limit))
    rows = conn.execute(" ".join(q), params).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        md: Dict[str, Any] = {}
        if r["metadata"]:
            try:
                parsed = json.loads(r["metadata"])
                if isinstance(parsed, dict):
                    md = parsed
            except Exception:
                md = {}
        out.append({
            "signal_name": r["signal_name"],
            "symbol": r["symbol"],
            "issued_at": r["issued_at"],
            "scored_at": r["scored_at"],
            "horizon_days": r["horizon_days"],
            "direction": r["predicted_direction"],
            "confidence": r["confidence"],
            "realized_return": r["realized_return"],
            "hit": r["hit"],
            "metadata": md,
        })
    return out


def prune_old_forecasts(max_age_days: int = 365) -> int:
    """Delete signal_forecasts rows older than `max_age_days`.

    Without this, the live-tracker table grows monotonically — fine for
    a few weeks but unbounded over years of use. 1 year is plenty of
    history for the hit-rate / Sharpe statistics we compute. Called from
    the cache_warmer on a daily cadence.

    Returns the number of rows pruned.
    """
    init_tracker_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM signal_forecasts WHERE issued_at < ?", (cutoff,)
        )
        conn.commit()
        return cur.rowcount or 0


# ── per-signal hook integrators ─────────────────────────────────────────────
# Each is intentionally permissive: it accepts the raw result dict from the
# upstream module, extracts what it needs, and silently no-ops if the shape
# doesn't match (e.g. an error result). This means the integrating agent
# can drop a one-liner into app.py with zero risk of breaking a panel.

def _maybe_log(name: str, **kwargs) -> Optional[int]:
    try:
        return log_forecast(name, **kwargs)
    except Exception as e:
        log.debug("log_forecast(%s) failed: %s", name, e)
        return None


def log_ml_forecast(symbol: str, ml_data: Dict[str, Any]) -> Optional[int]:
    """Hook for the /api/smart-money/ml-forecast/<symbol> response.

    Records the RF classifier's 20-day prob_up as a directional call:
    p > 0.55  -> BUY, p < 0.45 -> SELL, else NEUTRAL.
    """
    if not isinstance(ml_data, dict) or ml_data.get("error"):
        return None
    rf = ml_data.get("rf_classifier") or {}
    p = _safe_float(rf.get("prob_up_20d"))
    if p is None:
        return None
    if p > 0.55:
        direction = "BUY"
    elif p < 0.45:
        direction = "SELL"
    else:
        direction = "NEUTRAL"
    # Magnitude isn't given by the RF classifier in raw form; use the trend
    # forecast's projected pct as a best-effort magnitude when present.
    trend = ml_data.get("trend_forecast") or {}
    magnitude = _safe_float(trend.get("forecast_pct"))
    if magnitude is not None:
        magnitude = magnitude / 100.0  # forecast_pct is already in percent
    issue_price = _safe_float(ml_data.get("price")) or _safe_float(rf.get("price"))
    return _maybe_log(
        "ml_forecast",
        symbol=symbol,
        horizon_days=20,
        direction=direction,
        magnitude=magnitude,
        confidence=abs(p - 0.5) * 2.0,
        issue_price=issue_price,
        metadata={
            "prob_up_20d": p,
            "rf_signal": rf.get("signal"),
            "composite_signal": ml_data.get("composite_signal"),
        },
    )


def log_smart_money(symbol: str, sm_result: Dict[str, Any]) -> Optional[int]:
    """Hook for the smart_money.compute_score result.

    Composite score >70 = BUY, <30 = SELL, otherwise NEUTRAL. 30-day horizon.
    """
    if not isinstance(sm_result, dict) or sm_result.get("error"):
        return None
    score = _safe_float(sm_result.get("score"))
    if score is None:
        return None
    if score > 70:
        direction = "BUY"
    elif score < 30:
        direction = "SELL"
    else:
        direction = "NEUTRAL"
    return _maybe_log(
        "smart_money",
        symbol=symbol,
        horizon_days=30,
        direction=direction,
        magnitude=None,
        confidence=abs(score - 50.0) / 50.0,
        issue_price=_safe_float(sm_result.get("price")),
        metadata={
            "score": score,
            "signal": sm_result.get("signal"),
        },
    )


def log_gex(symbol: str, gex_result: Dict[str, Any]) -> Optional[int]:
    """Hook for the gex_engine result.

    LONG GAMMA -> BUY-like (mean-revert / pinned), SHORT GAMMA -> SELL-like
    (volatile / trending). 10-day horizon.
    """
    if not isinstance(gex_result, dict) or gex_result.get("error"):
        return None
    regime = gex_result.get("gamma_regime") or gex_result.get("regime")
    if not regime:
        return None
    regime_up = str(regime).upper()
    if "LONG" in regime_up:
        direction = "BUY"
    elif "SHORT" in regime_up:
        direction = "SELL"
    else:
        direction = "NEUTRAL"
    net_gex = _safe_float(gex_result.get("net_gex"))
    return _maybe_log(
        "gex",
        symbol=symbol,
        horizon_days=10,
        direction=direction,
        magnitude=None,
        confidence=None,
        issue_price=_safe_float(gex_result.get("spot") or gex_result.get("price")),
        metadata={
            "regime": regime,
            "net_gex": net_gex,
            "flip_price": gex_result.get("flip_price"),
        },
    )


def log_narrative(symbol: str, narrative_result: Dict[str, Any]) -> Optional[int]:
    """Hook for the narrative_engine result.

    Treats REVERSAL phase as a contrarian BUY (panic-bottom rebound), and
    EXHAUSTION / CONSENSUS phases as contrarian SELL (crowded). Everything
    else is logged NEUTRAL. 14-day horizon.
    """
    if not isinstance(narrative_result, dict) or narrative_result.get("error"):
        return None
    phase = narrative_result.get("narrative_phase") or narrative_result.get("phase")
    if not phase:
        return None
    phase_up = str(phase).upper()
    if phase_up == "REVERSAL":
        direction = "BUY"
    elif phase_up in ("EXHAUSTION", "CONSENSUS"):
        direction = "SELL"
    else:
        direction = "NEUTRAL"
    velocity = _safe_float(narrative_result.get("velocity_score") or narrative_result.get("velocity"))
    return _maybe_log(
        "narrative",
        symbol=symbol,
        horizon_days=14,
        direction=direction,
        magnitude=None,
        confidence=None,
        issue_price=None,
        metadata={
            "phase": phase,
            "velocity": velocity,
            "dominant_narrative": narrative_result.get("dominant_narrative") or narrative_result.get("dominant"),
            "contrarian": narrative_result.get("contrarian_signal"),
        },
    )


# Convenience: list signals we know how to track, for the API's discovery
# endpoint and the UI's "which signals have a track record?" lookup.
KNOWN_SIGNALS = ("ml_forecast", "smart_money", "gex", "narrative")


if __name__ == "__main__":
    # Tiny smoke-test: create a synthetic forecast, score it, and dump stats.
    logging.basicConfig(level=logging.INFO)
    init_tracker_db()
    print("Initialized signal_forecasts table at", _db_path())
    rid = log_forecast("smoke_test", "AAPL", 20, "BUY", 0.05, 0.7, 200.0)
    print("Inserted row id:", rid)
    print("Track record (pre-scoring):", get_track_record("smoke_test"))
