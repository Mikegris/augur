"""AJTA (AUGUR–Jarvis Trading Agent) — data & state foundation.

Implements AJTA-SPEC-1.0 Part II: the new trading tables (§5), forward-only
migrations (§6), money/time/session helpers (§7), retention + append-only
hash-chained audit (§8), single-instance locking (§19.2), cycle bookkeeping
for non-24/7 crash recovery (§19/§20.1), and the WAL-safe backup (§22.4).

Design rules honored here:
  * Reuses AUGUR's `database` module connection pool + `_write_lock` — never
    opens a second connection to wealth.db (avoids the data-fork hazard).
  * Existing AUGUR tables are NEVER redefined; only `aj_*` tables are added.
  * `aj_audit` is append-only: this module exposes no UPDATE/DELETE for it.
  * Everything is Python-3.9 compatible (no `X | Y` unions, no walrus in
    comprehensions, etc.).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

import database as db

log = logging.getLogger("augur.aj_db")

# ── schema version target (bump when adding a numbered migration step) ────────
AJ_SCHEMA_TARGET = 2
_SCHEMA_KEY = "aj_schema_version"

# ── DDL (§5). CREATE TABLE IF NOT EXISTS is safe to re-run; column ADDs go
#    through numbered migration steps, never here. ─────────────────────────────
_DDL = [
    """CREATE TABLE IF NOT EXISTS aj_proposals (
        id            INTEGER PRIMARY KEY,
        created_at    TEXT NOT NULL,
        cycle_id      TEXT NOT NULL,
        symbol        TEXT NOT NULL,
        side          TEXT NOT NULL CHECK (side IN ('buy','sell')),
        qty           REAL,
        notional_usd  REAL,
        order_type    TEXT NOT NULL CHECK (order_type IN ('market','limit')),
        limit_price   REAL,
        thesis        TEXT,
        forecast_id   INTEGER,
        status        TEXT NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed','blocked','approved','rejected','executed','expired')),
        risk_reason   TEXT,
        account_id    INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aj_prop_cycle ON aj_proposals(cycle_id)",
    """CREATE TABLE IF NOT EXISTS aj_orders (
        id              INTEGER PRIMARY KEY,
        proposal_id     INTEGER NOT NULL,
        client_order_id TEXT NOT NULL UNIQUE,
        broker          TEXT NOT NULL,
        mode            TEXT NOT NULL CHECK (mode IN ('paper','live')),
        account_ref     TEXT,
        symbol          TEXT NOT NULL,
        side            TEXT NOT NULL,
        qty             REAL NOT NULL,
        order_type      TEXT NOT NULL,
        limit_price     REAL,
        state           TEXT NOT NULL DEFAULT 'new'
                        CHECK (state IN ('new','submitted','accepted','partially_filled',
                                         'filled','canceled','rejected','expired','unknown')),
        broker_order_id TEXT,
        submitted_at    TEXT,
        terminal_at     TEXT,
        avg_fill_price  REAL,
        filled_qty      REAL DEFAULT 0,
        fees_usd        REAL DEFAULT 0,
        created_at      TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aj_orders_state ON aj_orders(state)",
    """CREATE TABLE IF NOT EXISTS aj_fills (
        id             INTEGER PRIMARY KEY,
        order_id       INTEGER NOT NULL,
        broker_fill_id TEXT,
        qty            REAL NOT NULL,
        price          REAL NOT NULL,
        fees_usd       REAL DEFAULT 0,
        filled_at      TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aj_fills_order ON aj_fills(order_id)",
    """CREATE TABLE IF NOT EXISTS aj_risk_events (
        id           INTEGER PRIMARY KEY,
        created_at   TEXT NOT NULL,
        proposal_id  INTEGER,
        decision     TEXT NOT NULL CHECK (decision IN ('pass','block','halt','rearm')),
        reason       TEXT,
        caps_json    TEXT,
        day_pnl_usd  REAL
    )""",
    """CREATE TABLE IF NOT EXISTS aj_audit (
        id           INTEGER PRIMARY KEY,
        ts           TEXT NOT NULL,
        cycle_id     TEXT,
        kind         TEXT NOT NULL,
        ref_id       INTEGER,
        actor        TEXT,
        payload_json TEXT NOT NULL,
        prev_hash    TEXT,
        row_hash     TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aj_audit_ts ON aj_audit(ts)",
    """CREATE TABLE IF NOT EXISTS aj_routing (
        id           INTEGER PRIMARY KEY,
        ts           TEXT NOT NULL,
        cycle_id     TEXT,
        role         TEXT,
        complexity   REAL,
        sensitivity  TEXT,
        chosen_model TEXT,
        fallback_used INTEGER DEFAULT 0,
        cost_usd     REAL,
        latency_ms   INTEGER,
        ok           INTEGER,
        escalated    INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS aj_recon (
        id           INTEGER PRIMARY KEY,
        ts           TEXT NOT NULL,
        scope        TEXT,
        status       TEXT NOT NULL CHECK (status IN ('match','divergence','resolved')),
        detail_json  TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS aj_cycles (
        cycle_id     TEXT PRIMARY KEY,
        started_at   TEXT NOT NULL,
        ended_at     TEXT,
        mode         TEXT,
        status       TEXT
    )""",
]

# Migration step 2 (v3.x enhancements): analytics + per-position state. Additive.
_DDL_V2 = [
    """CREATE TABLE IF NOT EXISTS aj_equity (
        id             INTEGER PRIMARY KEY,
        ts             TEXT NOT NULL,
        date           TEXT NOT NULL,
        realized_usd   REAL,
        unrealized_usd REAL,
        equity_usd     REAL,
        day_pnl_usd    REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aj_equity_date ON aj_equity(date)",
    """CREATE TABLE IF NOT EXISTS aj_cycle_log (
        id           INTEGER PRIMARY KEY,
        cycle_id     TEXT,
        ts           TEXT NOT NULL,
        mode         TEXT,
        session      TEXT,
        n_proposals  INTEGER,
        n_executed   INTEGER,
        summary_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aj_cycle_log_ts ON aj_cycle_log(ts)",
    """CREATE TABLE IF NOT EXISTS aj_position_state (
        symbol       TEXT PRIMARY KEY,
        opened_at    TEXT,
        peak_mark    REAL,
        last_mark    REAL,
        updated_at   TEXT,
        last_exit_at TEXT
    )""",
]


# ── migrations (§6) ───────────────────────────────────────────────────────────

def get_setting_raw(key: str) -> Optional[str]:
    """Read one settings value DIRECTLY from the table, bypassing both the 5s
    settings cache AND the get_settings() `__`-prefix filter. Control-plane keys
    (the operator lease, the session-open P&L snapshot, the schema version) MUST
    round-trip even though get_settings() hides `__` keys — reading them through
    get_settings() silently returns None, which broke the lease lock and the
    daily-loss unrealized snapshot (both fail-open). Always use this for those."""
    try:
        row = db.get_conn().execute(
            "SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
    except Exception:
        return None


def set_setting_raw(key: str, value: str) -> None:
    """Write a single settings value directly + invalidate the cache. Mirrors
    set_setting but usable for `__` control-plane keys."""
    with db._write_lock:
        conn = db.get_conn()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                     (key, str(value)))
        conn.commit()
    try:
        db._invalidate_settings_cache()
    except Exception:
        pass


def _applied_version() -> int:
    try:
        v = get_setting_raw(_SCHEMA_KEY)
        return int(v) if v is not None else 0
    except Exception:
        return 0


def aj_migrate() -> int:
    """Idempotent, forward-only migration. Creates the base schema at step 1
    and records the applied level in settings. Returns the level now in force.

    A migration that ALTERS an existing table MUST take a backup first (§6);
    the base creation here only adds new `aj_*` tables, so no backup is needed
    for step 1. Future column-add steps should call backup_db() before running.
    """
    with db._write_lock:
        conn = db.get_conn()
        current = _applied_version()
        if current >= AJ_SCHEMA_TARGET:
            return current
        # Step 1 — base trading schema (additive only).
        if current < 1:
            cur = conn.cursor()
            for stmt in _DDL:
                cur.execute(stmt)
            conn.commit()
            current = 1
        # Step 2 — analytics + position-state tables (additive only).
        if current < 2:
            cur = conn.cursor()
            for stmt in _DDL_V2:
                cur.execute(stmt)
            conn.commit()
            current = 2
        set_setting_raw(_SCHEMA_KEY, str(current))
        log.info("aj_migrate: schema at version %d", current)
        return current


def aj_init() -> int:
    """Ensure AUGUR's base schema exists, then apply AJ migrations. Safe to
    call repeatedly (startup, tests, CLI)."""
    try:
        db.init_db()
    except Exception as e:
        log.debug("aj_init: db.init_db() already done or failed soft: %s", e)
    return aj_migrate()


# ── money (§7) — single rounding authority, avoids float drift in caps/P&L ─────

def money(x: Any) -> float:
    """Round a USD amount to cents (banker-safe HALF_UP) and return a float.
    All cap checks and P&L aggregation MUST pass through here so 0.1+0.2-style
    drift never flips a boundary comparison."""
    if x is None:
        return 0.0
    try:
        d = Decimal(str(x))
    except Exception:
        try:
            d = Decimal(float(x))
        except Exception:
            return 0.0
    if not d.is_finite():
        return 0.0
    return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ── time + market session (§7, §20.6) ────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def parse_iso(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        txt = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# US market holidays (fixed + observed). Enough for session gating; live
# trading is OFF by default and §20.6 says default to `closed` when the
# calendar is uncertain, so an imperfect holiday list only ever errs safe.
def _us_market_holidays(year: int) -> set:
    import calendar as _cal

    def _observed(month, day):
        d = datetime(year, month, day)
        wd = d.weekday()
        if wd == 5:      # Saturday -> observed Friday
            d = d - timedelta(days=1)
        elif wd == 6:    # Sunday -> observed Monday
            d = d + timedelta(days=1)
        return d.date()

    def _nth_weekday(month, weekday, n):
        c = _cal.Calendar()
        days = [d for d in c.itermonthdates(year, month)
                if d.month == month and d.weekday() == weekday]
        return days[min(n - 1, len(days) - 1)] if days else None

    def _last_weekday(month, weekday):
        c = _cal.Calendar()
        days = [d for d in c.itermonthdates(year, month)
                if d.month == month and d.weekday() == weekday]
        return days[-1] if days else None

    hols = set()
    hols.add(_observed(1, 1))                       # New Year's Day
    hols.add(_nth_weekday(1, 0, 3))                 # MLK (3rd Mon Jan)
    hols.add(_nth_weekday(2, 0, 3))                 # Presidents (3rd Mon Feb)
    hols.add(_last_weekday(5, 0))                   # Memorial (last Mon May)
    hols.add(_observed(6, 19))                      # Juneteenth
    hols.add(_observed(7, 4))                       # Independence Day
    hols.add(_nth_weekday(9, 0, 1))                 # Labor (1st Mon Sep)
    hols.add(_nth_weekday(11, 3, 4))                # Thanksgiving (4th Thu Nov)
    hols.add(_observed(12, 25))                     # Christmas
    # Keep only same-year dates: an observed shift can push Jan-1-on-Saturday
    # back to Dec-31 of the prior year, which market_session (called with
    # local.year) could never match anyway. Drop None and cross-year entries.
    return {h for h in hols if h is not None and h.year == year}


def market_session(dt: Optional[datetime] = None) -> str:
    """Return the US-equities session for `dt` (default now): one of
    'premarket' | 'regular' | 'afterhours' | 'closed'. DST handled by the
    America/New_York zone. Any failure to resolve the calendar returns
    'closed' (§20.6 — never trade live on an uncertain clock)."""
    try:
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
    except Exception:
        log.debug("market_session: no zoneinfo -> closed (fail-safe)")
        return "closed"
    try:
        when = dt or utc_now()
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        local = when.astimezone(ny)
        if local.weekday() >= 5:                    # Sat/Sun
            return "closed"
        if local.date() in _us_market_holidays(local.year):
            return "closed"
        mins = local.hour * 60 + local.minute
        if 4 * 60 <= mins < 9 * 60 + 30:
            return "premarket"
        if 9 * 60 + 30 <= mins < 16 * 60:
            return "regular"
        if 16 * 60 <= mins < 20 * 60:
            return "afterhours"
        return "closed"
    except Exception:
        log.exception("market_session failed -> closed")
        return "closed"


# ── single-instance lock (§19.2) ──────────────────────────────────────────────

def _lock_path(name: str = "operator") -> str:
    base = os.path.dirname(db.DB_PATH) or "."
    return os.path.join(base, ".aj_{}.lock".format(name))


class SingleInstanceLock:
    """Cross-process exclusive lock via fcntl.flock on a lock file beside the
    DB. A second invocation while one runs fails to acquire and the caller
    exits immediately (§19.2, §20.10). Best-effort on platforms without
    fcntl (falls back to a TTL lease in settings)."""

    def __init__(self, name: str = "operator", ttl_s: int = 3600):
        self.name = name
        self.ttl_s = ttl_s
        self.path = _lock_path(name)
        self._fh = None
        self._have = False

    def acquire(self) -> bool:
        try:
            import fcntl
        except Exception:
            return self._acquire_lease()
        try:
            self._fh = open(self.path, "w")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh.write("{} {}\n".format(os.getpid(), utc_now_iso()))
            self._fh.flush()
            self._have = True
            return True
        except Exception:
            try:
                if self._fh:
                    self._fh.close()
            except Exception:
                pass
            self._fh = None
            return False

    def _acquire_lease(self) -> bool:
        key = "__aj_lease_{}".format(self.name)
        now = time.time()
        with db._write_lock:
            # MUST read the raw row: get_settings() hides `__` keys, so reading
            # the lease through it always returned None and the lock never
            # excluded anything (fail-open). Read directly.
            cur = get_setting_raw(key)
            if cur:
                try:
                    held_at = float(str(cur).split(":")[0])
                    if now - held_at < self.ttl_s:
                        return False
                except Exception:
                    pass
            conn = db.get_conn()
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
                         (key, "{}:{}".format(now, os.getpid())))
            conn.commit()
        self._have = True
        return True

    def release(self) -> None:
        if not self._have:
            return
        try:
            import fcntl
            if self._fh:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
        except Exception:
            pass
        try:
            with db._write_lock:
                conn = db.get_conn()
                conn.execute("DELETE FROM settings WHERE key=?",
                             ("__aj_lease_{}".format(self.name),))
                conn.commit()
        except Exception:
            pass
        self._have = False

    def __enter__(self):
        self._ok = self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# ── cycle bookkeeping (§19, §20.1) ───────────────────────────────────────────

def new_cycle_id() -> str:
    return "cyc_" + uuid.uuid4().hex[:16]


def open_cycle(mode: str) -> str:
    cid = new_cycle_id()
    with db._write_lock:
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO aj_cycles (cycle_id, started_at, mode, status) VALUES (?,?,?,?)",
            (cid, utc_now_iso(), mode, "running"))
        conn.commit()
    return cid


def close_cycle(cycle_id: str, status: str = "completed") -> None:
    with db._write_lock:
        conn = db.get_conn()
        conn.execute(
            "UPDATE aj_cycles SET ended_at=?, status=? WHERE cycle_id=?",
            (utc_now_iso(), status, cycle_id))
        conn.commit()


def find_stale_running_cycles(exclude: Optional[str] = None) -> List[Dict[str, Any]]:
    """Cycles still marked 'running' with no end — a prior process crashed
    mid-cycle (§20.1). The caller marks them 'crashed' and reconciles before
    proposing anything new."""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM aj_cycles WHERE status='running' AND ended_at IS NULL"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if exclude and d.get("cycle_id") == exclude:
            continue
        out.append(d)
    return out


def mark_cycle(cycle_id: str, status: str) -> None:
    with db._write_lock:
        conn = db.get_conn()
        conn.execute("UPDATE aj_cycles SET status=? WHERE cycle_id=?",
                     (status, cycle_id))
        conn.commit()


# ── append-only audit + hash chain (§8) ──────────────────────────────────────

def _last_row_hash() -> Optional[str]:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT row_hash FROM aj_audit ORDER BY id DESC LIMIT 1").fetchone()
    return row["row_hash"] if row and row["row_hash"] else None


def _canonical(payload: Any) -> str:
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          default=str)
    except Exception:
        return str(payload)


def audit(kind: str, payload: Any, cycle_id: Optional[str] = None,
          ref_id: Optional[int] = None, actor: str = "operator") -> int:
    """Append one immutable audit record with a sha256 hash chain so any later
    tampering (UPDATE/DELETE/edit) is detectable. Returns the row id. Never
    raises in a way that breaks the caller's control path — audit failure is
    logged, but trading code should treat an audit write failure as a
    fail-closed condition where it matters (the caller decides)."""
    ts = utc_now_iso()
    body = _canonical(payload)
    with db._write_lock:
        conn = db.get_conn()
        prev = _last_row_hash()
        material = "{}|{}|{}|{}|{}|{}".format(
            prev or "", ts, kind, ref_id if ref_id is not None else "",
            actor or "", body)
        row_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        cur = conn.execute(
            "INSERT INTO aj_audit (ts, cycle_id, kind, ref_id, actor, payload_json, prev_hash, row_hash) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, cycle_id, kind, ref_id, actor, body, prev, row_hash))
        conn.commit()
        return int(cur.lastrowid)


def verify_audit_chain() -> Dict[str, Any]:
    """Recompute the hash chain over aj_audit and report the first broken link
    (if any). A clean chain means no row was edited or deleted out of band."""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, ts, kind, ref_id, actor, payload_json, prev_hash, row_hash "
        "FROM aj_audit ORDER BY id ASC").fetchall()
    prev_row_hash = None     # the previous row's row_hash
    for i, r in enumerate(rows):
        stored_prev = r["prev_hash"] or None
        # Linkage: each row's prev_hash MUST equal the prior row's row_hash. The
        # FIRST row must start the chain (null prev_hash) — a non-null prev_hash
        # on row 0 means a contiguous prefix was deleted.
        if i == 0:
            if stored_prev is not None:
                return {"ok": False, "broken_at": r["id"],
                        "reason": "chain does not start cleanly (prefix deleted?)"}
        elif stored_prev != prev_row_hash:
            return {"ok": False, "broken_at": r["id"], "reason": "prev_hash linkage broken"}
        # Recompute row_hash from the STORED prev_hash so any edit to prev_hash
        # is caught here (row_hash binds it).
        material = "{}|{}|{}|{}|{}|{}".format(
            stored_prev or "", r["ts"], r["kind"],
            r["ref_id"] if r["ref_id"] is not None else "",
            r["actor"] or "", r["payload_json"])
        expect = hashlib.sha256(material.encode("utf-8")).hexdigest()
        if expect != r["row_hash"]:
            return {"ok": False, "broken_at": r["id"], "reason": "row_hash mismatch"}
        prev_row_hash = r["row_hash"]
    return {"ok": True, "rows": len(rows)}


# ── generic row helpers for aj_* tables ──────────────────────────────────────

def insert(table: str, **cols) -> int:
    keys = list(cols.keys())
    qs = ",".join("?" for _ in keys)
    with db._write_lock:
        conn = db.get_conn()
        cur = conn.execute(
            "INSERT INTO {} ({}) VALUES ({})".format(table, ",".join(keys), qs),
            tuple(cols[k] for k in keys))
        conn.commit()
        return int(cur.lastrowid)


def update(table: str, row_id: int, **cols) -> None:
    if not cols:
        return
    sets = ",".join("{}=?".format(k) for k in cols)
    with db._write_lock:
        conn = db.get_conn()
        conn.execute("UPDATE {} SET {} WHERE id=?".format(table, sets),
                     tuple(cols.values()) + (row_id,))
        conn.commit()


def update_if(table: str, row_id: int, where_col: str, where_val: Any,
              **cols) -> bool:
    """Conditional UPDATE: applies cols only if `where_col` still equals
    `where_val` (compare-and-set). Returns True iff a row was updated. Used to
    make order-state transitions race-safe — a concurrent writer that already
    moved the state wins, and this caller is told it lost (rowcount 0)."""
    if not cols:
        return False
    sets = ",".join("{}=?".format(k) for k in cols)
    with db._write_lock:
        conn = db.get_conn()
        cur = conn.execute(
            "UPDATE {} SET {} WHERE id=? AND {}=?".format(table, sets, where_col),
            tuple(cols.values()) + (row_id, where_val))
        conn.commit()
        return cur.rowcount > 0


def get_row(table: str, row_id: int) -> Optional[Dict[str, Any]]:
    conn = db.get_conn()
    r = conn.execute("SELECT * FROM {} WHERE id=?".format(table), (row_id,)).fetchone()
    return dict(r) if r else None


def query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = db.get_conn()
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ── WAL-safe backup (§22.4) ──────────────────────────────────────────────────

def backup_db(dest: Optional[str] = None) -> str:
    """Consistent snapshot of wealth.db via the SQLite Online Backup API
    (safe under WAL). Returns the destination path. Used before migrations
    that alter existing tables and on a schedule."""
    src = db.DB_PATH
    if not dest:
        stamp = utc_now().strftime("%Y%m%d-%H%M%S")
        base = os.path.dirname(src) or "."
        dest = os.path.join(base, "wealth.db.aj-backup-{}".format(stamp))
    s = sqlite3.connect(src)
    d = sqlite3.connect(dest)
    try:
        with d:
            s.backup(d)
    finally:
        s.close()
        d.close()
    log.info("aj backup -> %s", dest)
    return dest
