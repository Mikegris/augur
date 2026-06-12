"""
Idea Pool Warmer — background pre-computation of investment dossiers.

User-triggered "rolls" in the idea generator UI normally trigger a full
fan-out compute (yfinance / SEC EDGAR / CoinGecko / ML forecast / narrative
engine / etc.) that takes 30–55 seconds end-to-end. This module warms a
SQLite-backed cache (`idea_pool`) in a daemon thread so the common case
becomes a sub-millisecond lookup instead of a cold compute.

Integration:
    - On app startup, call `start_warmer_thread()` once.
    - The roll endpoint should call `get_warmed_dossier()` first; on a miss
      or stale row, fall back to the existing `idea_generator._build_dossier`.
    - `list_warmed_symbols()` lets the random picker bias toward symbols we
      already have warm cache for.
    - `warmer_status()` powers a small status widget in the UI.

Schema is created at import time via `init_idea_pool_table()`.

Python 3.9 compatible.
"""

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import database as db
import idea_generator
import opportunity_scanner as scanner
import safe_executor

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

WARMED_TTL_SECONDS = 24 * 3600  # 24h freshness window

# Skip-fresh threshold (2026-06 audit). Every 6h pass used to rebuild ALL
# ~180 dossiers (full fan-out: yfinance / EDGAR / CoinGecko / ML forecast)
# even though rows are served for 24h — i.e. each dossier was regenerated
# 4× per day while its consumers tolerate 24h-old data by design. A pass now
# skips any (symbol, asset_class, strategy) row younger than this threshold.
# Why TTL/2 (12h) and not higher: a row rebuilt at age 12h gets TWO further
# passes (18h, 24h) as retry headroom before it would expire, so a transient
# per-symbol failure can't leave a hole in the pool. Failed targets are never
# persisted, so they stay missing-from-fresh and retry on the very next pass.
# Net effect: ~720 dossier builds/day -> ~360/day (alternating full/no-op
# passes in steady state), with identical worst-case staleness (24h TTL).
REFRESH_AGE_SECONDS = WARMED_TTL_SECONDS // 2

# Internal cap on parallelism — the underlying _build_dossier already fans out
# to ~10 workers per symbol, so keep this modest to avoid thrashing the
# external rate limits (yfinance / SEC / CoinGecko) AND to minimize SQLite
# write contention with concurrent /api/ideas read traffic during a pass.
_WARM_MAX_WORKERS = 2

# Tiny breather after each persisted dossier — gives the SQLite WAL room and
# leaves request-thread reads visibly snappy during a warming pass.
_PERSIST_PAUSE_SECONDS = 0.05

# Boot delay before the first warming pass so app startup isn't penalized.
_BOOT_DELAY_SECONDS = 30

# ── Module state (guarded by _state_lock) ────────────────────────────────────

_state_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_thread_started = False
_running = False
_last_run_started: Optional[str] = None
_last_run_finished: Optional[str] = None
_last_run_count = 0
_last_run_errors = 0
_last_run_skipped = 0
_last_run_epoch: Optional[float] = None
_warm_target = 200
_warm_interval = 6 * 3600


# ── Schema bootstrap ─────────────────────────────────────────────────────────

def init_idea_pool_table() -> None:
    """Create the idea_pool + idea_pool_state tables if they don't exist.
    Holds _write_lock so we don't race the snapshot worker / cache_warmer
    on module import."""
    with db._write_lock:
        conn = db.get_conn()
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS idea_pool (
            symbol TEXT NOT NULL,
            asset_class TEXT NOT NULL,
            strategy TEXT NOT NULL,
            dossier_json TEXT NOT NULL,
            composite_score REAL,
            sector TEXT,
            warmed_at TEXT NOT NULL,
            PRIMARY KEY (symbol, asset_class, strategy)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_idea_pool_warmed ON idea_pool(warmed_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_idea_pool_score ON idea_pool(composite_score)")

        c.execute("""CREATE TABLE IF NOT EXISTS idea_pool_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        conn.commit()


# Run on import so callers can immediately query the table.
init_idea_pool_table()


# ── State key/value helpers ──────────────────────────────────────────────────

def _state_get(key: str) -> Optional[str]:
    """Read a state key. Returns None if the table is missing, the row is
    absent, or the DB is corrupted — the warmer treats this as 'start fresh'
    rather than crashing the daemon."""
    try:
        conn = db.get_conn()
        row = conn.execute(
            "SELECT value FROM idea_pool_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    except Exception as exc:  # noqa: BLE001 — never let state read kill the loop
        logger.warning("idea_pool: state_get(%s) failed: %s", key, exc)
        return None


def _state_set(key: str, value: str) -> None:
    """Persist a state key under the shared _write_lock so we don't race
    the snapshot worker or cache warmer. Best-effort: state is advisory
    (rotation offset, last-run counts) so a write failure is just logged."""
    try:
        conn = db.get_conn()
        with db._write_lock:
            conn.execute(
                "INSERT OR REPLACE INTO idea_pool_state (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("idea_pool: state_set(%s) failed: %s", key, exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl_cutoff_iso() -> str:
    """ISO timestamp marking the oldest still-fresh row."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=WARMED_TTL_SECONDS)
    return cutoff.isoformat()


# ── Public lookup API ────────────────────────────────────────────────────────

def get_warmed_dossier(symbol: str, asset_class: str, strategy: str) -> Optional[Dict[str, Any]]:
    """Return the cached dossier for (symbol, asset_class, strategy) if fresh."""
    conn = db.get_conn()
    row = conn.execute(
        """SELECT dossier_json, warmed_at FROM idea_pool
            WHERE symbol = ? AND asset_class = ? AND strategy = ?""",
        (symbol, asset_class, strategy),
    ).fetchone()
    if not row:
        return None
    if row["warmed_at"] < _ttl_cutoff_iso():
        return None
    try:
        dossier = json.loads(row["dossier_json"])
    except (TypeError, ValueError) as exc:
        logger.warning("idea_pool: failed to decode cached dossier for %s: %s", symbol, exc)
        return None
    dossier["from_warmed_pool"] = True
    return dossier


def list_warmed_symbols(asset_class: Optional[str] = None) -> List[Dict[str, Any]]:
    """List currently fresh warmed rows (used to bias random picks)."""
    conn = db.get_conn()
    cutoff = _ttl_cutoff_iso()
    if asset_class:
        rows = conn.execute(
            """SELECT symbol, asset_class, strategy, composite_score, sector, warmed_at
                FROM idea_pool
                WHERE warmed_at >= ? AND asset_class = ?
                ORDER BY composite_score DESC""",
            (cutoff, asset_class),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT symbol, asset_class, strategy, composite_score, sector, warmed_at
                FROM idea_pool
                WHERE warmed_at >= ?
                ORDER BY composite_score DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def _count_fresh() -> int:
    try:
        conn = db.get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM idea_pool WHERE warmed_at >= ?",
            (_ttl_cutoff_iso(),),
        ).fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def _recently_warmed_keys(max_age_seconds: int) -> set:
    """Set of (symbol, asset_class, strategy) tuples warmed within the last
    `max_age_seconds`. Used by warm_pool to skip rebuilding dossiers whose
    cached row is still young. Best-effort: on any error return an empty set,
    which degrades to the old rebuild-everything behavior (safe, just slower)."""
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        ).isoformat()
        conn = db.get_conn()
        rows = conn.execute(
            "SELECT symbol, asset_class, strategy FROM idea_pool WHERE warmed_at >= ?",
            (cutoff,),
        ).fetchall()
        return {(r["symbol"], r["asset_class"], r["strategy"]) for r in rows}
    except Exception as exc:  # noqa: BLE001
        logger.warning("idea_pool: fresh-key lookup failed (will warm all): %s", exc)
        return set()


def warmer_status() -> Dict[str, Any]:
    """Snapshot of the warmer state for the UI."""
    with _state_lock:
        running = _running
        last_started = _last_run_started
        last_finished = _last_run_finished
        last_count = _last_run_count
        last_errors = _last_run_errors
        last_skipped = _last_run_skipped
        last_epoch = _last_run_epoch
        target = _warm_target
        interval = _warm_interval
        thread_alive = _thread is not None and _thread.is_alive()

    next_in: Optional[int] = None
    if thread_alive and not running and last_epoch is not None:
        elapsed = time.time() - last_epoch
        remaining = int(interval - elapsed)
        next_in = max(remaining, 0)

    # The configured target (default 200) exceeds what one pass can actually
    # produce: the universe is ~160 equities + ~20 crypto and each symbol is
    # warmed under exactly one strategy per pass (PRIMARY KEY symbol/class/
    # strategy), so distinct fresh rows top out around 180. Surface the real
    # ceiling so the UI widget doesn't imply 200 is reachable.
    try:
        ceiling = len(idea_generator.CRYPTO_UNIVERSE) + len(idea_generator.EQUITY_UNIVERSE)
    except Exception:  # noqa: BLE001
        ceiling = None

    return {
        "running": running,
        "last_run_started": last_started,
        "last_run_finished": last_finished,
        "last_run_count": last_count,
        "last_run_errors": last_errors,
        "last_run_skipped": last_skipped,
        "warmed_total": _count_fresh(),
        "next_run_in_seconds": next_in,
        "warm_target": target,
        "warm_target_ceiling": ceiling,
    }


# ── Rotation / selection ─────────────────────────────────────────────────────

_OFFSET_KEY = "equity_rotation_offset"


def _get_rotation_offset() -> int:
    raw = _state_get(_OFFSET_KEY)
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _set_rotation_offset(value: int) -> None:
    _state_set(_OFFSET_KEY, str(value))


def _select_targets(target_count: int) -> List[Dict[str, str]]:
    """
    Build the ordered (symbol, asset_class, strategy) work list for one pass.

    Crypto universe is included whole (small; gives consistent crypto coverage).
    Equity universe is sliced with a wrap-around rotation offset so that
    successive passes cover different equities and the full ~160-symbol
    universe gets touched over a few days.

    NOTE (2026-06 audit): with the default target (200) >= universe size
    (~180), take == n, so the offset advances by (offset + n) % n == offset —
    i.e. rotation is a no-op and every pass selects the full universe with
    the SAME symbol->strategy assignment. That's harmless (full coverage per
    pass) and the stable strategy assignment is what lets warm_pool's
    skip-fresh filter match rows across passes. Rotation only kicks in if
    the target is lowered below the universe size.

    Strategies are assigned round-robin so each pass mixes growth/value/etc.
    """
    crypto = list(idea_generator.CRYPTO_UNIVERSE)
    equities = list(idea_generator.EQUITY_UNIVERSE)
    strategies = list(scanner.STRATEGY_WEIGHTS.keys()) or ["growth"]

    eq_needed = max(target_count - len(crypto), 0)
    eq_slice: List[str] = []
    if equities and eq_needed:
        n = len(equities)
        offset = _get_rotation_offset() % n
        # Cap at universe size — no point warming a symbol twice in one pass.
        take = min(eq_needed, n)
        for i in range(take):
            eq_slice.append(equities[(offset + i) % n])
        # Advance offset for next pass (persisted at end of warm_pool()).
        _set_rotation_offset((offset + take) % n)

    targets: List[Dict[str, str]] = []
    idx = 0
    for sym in crypto:
        targets.append({
            "symbol": sym,
            "asset_class": "crypto",
            "strategy": strategies[idx % len(strategies)],
        })
        idx += 1
    for sym in eq_slice:
        targets.append({
            "symbol": sym,
            "asset_class": "stock",
            "strategy": strategies[idx % len(strategies)],
        })
        idx += 1
    return targets


# ── Persistence ──────────────────────────────────────────────────────────────

def _persist_dossier(symbol: str, asset_class: str, strategy: str, dossier: Dict[str, Any]) -> None:
    composite = None
    sector = None
    try:
        composite = ((dossier.get("strength") or {}).get("composite_score"))
        if composite is not None:
            composite = float(composite)
    except (TypeError, ValueError):
        composite = None
    try:
        sector = (dossier.get("snapshot") or {}).get("sector")
    except AttributeError:
        sector = None

    payload = json.dumps(dossier, default=str)
    conn = db.get_conn()
    with db._write_lock:
        conn.execute(
            """INSERT OR REPLACE INTO idea_pool
                (symbol, asset_class, strategy, dossier_json, composite_score, sector, warmed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (symbol, asset_class, strategy, payload, composite, sector, _now_iso()),
        )
        conn.commit()


def _warm_one(target: Dict[str, str]) -> bool:
    symbol = target["symbol"]
    asset_class = target["asset_class"]
    strategy = target["strategy"]
    try:
        dossier = idea_generator._build_dossier(
            symbol, asset_class, source="warmer", strategy=strategy
        )
        if not dossier:
            logger.warning("idea_pool: empty dossier for %s/%s/%s", symbol, asset_class, strategy)
            return False
        _persist_dossier(symbol, asset_class, strategy, dossier)
        if _PERSIST_PAUSE_SECONDS:
            time.sleep(_PERSIST_PAUSE_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 — one bad symbol must not kill the pass
        logger.exception("idea_pool: failed to warm %s/%s/%s: %s",
                         symbol, asset_class, strategy, exc)
        return False


def _warm_one_with_cleanup(target: Dict[str, str]) -> bool:
    """Worker wrapper that closes thread-local sqlite connections on the
    way out. Without this, each parallel_map worker thread opens (lazily,
    via database.get_conn() / cache_store._conn() inside _build_dossier and
    _persist_dossier) thread-local sqlite handles. The daemon worker pool
    is recreated every warm_pool() call, so its threads terminate when the
    pass drains — but Python's threading.local cleanup runs only when the
    thread object is GC'd, which can be arbitrarily delayed. In the
    meantime the sqlite FDs stay open and a long-lived process accumulates
    them across passes (every 6h × N workers per process lifetime). On
    macOS the default per-process FD soft limit is 256; with two workers
    we'd hit it after ~120 passes."""
    try:
        return _warm_one(target)
    finally:
        try:
            db.close_thread_conn()
        except Exception:
            pass
        try:
            import cache_store
            cache_store.close_thread_conn()
        except Exception:
            pass


# ── Warming pass ─────────────────────────────────────────────────────────────

def warm_pool(target_count: int = 200) -> Dict[str, Any]:
    """Run a single warming pass. Returns counts + elapsed seconds."""
    global _running, _last_run_started, _last_run_finished
    global _last_run_count, _last_run_errors, _last_run_skipped
    global _last_run_epoch, _warm_target

    with _state_lock:
        if _running:
            logger.info("idea_pool: warm_pool() called but a pass is already running")
            return {"warmed": 0, "errors": 0, "elapsed_seconds": 0.0, "skipped": True}
        _running = True
        _warm_target = target_count
        _last_run_started = _now_iso()

    start = time.time()
    warmed = 0
    errors = 0
    skipped_fresh = 0
    try:
        targets = _select_targets(target_count)

        # Drop targets whose cached dossier is still young (< REFRESH_AGE).
        # Inputs (price, signals) feeding a dossier are themselves cached
        # upstream for minutes-to-hours, so rebuilding a 6h-old dossier
        # mostly re-serializes the same inputs — pure churn. See the
        # REFRESH_AGE_SECONDS comment for the staleness/retry reasoning.
        fresh = _recently_warmed_keys(REFRESH_AGE_SECONDS)
        if fresh:
            kept = [t for t in targets
                    if (t["symbol"], t["asset_class"], t["strategy"]) not in fresh]
            skipped_fresh = len(targets) - len(kept)
            targets = kept

        logger.info(
            "idea_pool: starting warm pass — %d targets (%d skipped still-fresh), %d workers",
            len(targets), skipped_fresh, _WARM_MAX_WORKERS)

        # Run on safe_executor's daemon threads. This is the call site that
        # used to wedge interpreter exit: a 200-item pass mid-flight at
        # shutdown left non-daemon TPE workers for concurrent.futures'
        # atexit hook to join. Daemon workers just die with the process.
        # parallel_map also falls back to a serial loop by itself when
        # worker threads can't be spawned, replacing the old bundle-state
        # guards. _warm_one_with_cleanup returns True/False (it logs and
        # swallows per-target exceptions); a None slot from parallel_map
        # (work fn raised) counts as an error like the old fut.result()
        # exception path did.
        outcomes = safe_executor.parallel_map(
            _warm_one_with_cleanup, targets,
            max_workers=_WARM_MAX_WORKERS,
            thread_name_prefix="idea-warm",
        )
        for ok in outcomes:
            if ok:
                warmed += 1
            else:
                errors += 1
    finally:
        elapsed = time.time() - start
        finished_iso = _now_iso()
        with _state_lock:
            _running = False
            _last_run_finished = finished_iso
            _last_run_count = warmed
            _last_run_errors = errors
            _last_run_skipped = skipped_fresh
            _last_run_epoch = time.time()
        # Persist for visibility across restarts.
        try:
            _state_set("last_run_started", _last_run_started or "")
            _state_set("last_run_finished", finished_iso)
            _state_set("last_run_count", str(warmed))
            _state_set("last_run_errors", str(errors))
            _state_set("last_run_skipped", str(skipped_fresh))
        except Exception as exc:  # noqa: BLE001
            logger.warning("idea_pool: failed to persist run state: %s", exc)
        logger.info(
            "idea_pool: warm pass complete — warmed=%d skipped=%d errors=%d elapsed=%.1fs",
            warmed, skipped_fresh, errors, elapsed,
        )
        # Force a WAL checkpoint so the WAL file doesn't grow unbounded across
        # passes and so subsequent reader connections don't spend time over
        # the freshly-written WAL pages. Holds _write_lock so no concurrent
        # writer races against the checkpoint's brief exclusive grab.
        try:
            with db._write_lock:
                db.get_conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("idea_pool: WAL checkpoint failed: %s", exc)

    return {
        "warmed": warmed,
        "errors": errors,
        "skipped_fresh": skipped_fresh,
        "elapsed_seconds": elapsed,
    }


# ── Background thread ────────────────────────────────────────────────────────

def _warmer_loop(target_count: int, interval_seconds: int) -> None:
    logger.info("idea_pool: warmer thread started — boot delay %ds", _BOOT_DELAY_SECONDS)
    time.sleep(_BOOT_DELAY_SECONDS)
    while True:
        try:
            warm_pool(target_count=target_count)
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            logger.exception("idea_pool: warm_pool raised: %s", exc)
        time.sleep(interval_seconds)


def start_warmer_thread(target_count: int = 200, interval_seconds: int = 6 * 3600) -> None:
    """
    Idempotently spawn the background warmer daemon.

    Calling this more than once in the same process is a no-op.
    """
    global _thread, _thread_started, _warm_target, _warm_interval

    with _state_lock:
        if _thread_started and _thread is not None and _thread.is_alive():
            logger.info("idea_pool: warmer thread already running")
            return
        _warm_target = target_count
        _warm_interval = interval_seconds
        _thread = threading.Thread(
            target=_warmer_loop,
            args=(target_count, interval_seconds),
            name="idea-pool-warmer",
            daemon=True,
        )
        _thread_started = True
        _thread.start()
    logger.info(
        "idea_pool: warmer thread launched — target=%d interval=%ds",
        target_count, interval_seconds,
    )
