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
AJ_SCHEMA_TARGET = 11
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

# Migration step 3 (Analyst Council layer, merge plan §5.6). Additive only —
# new advisory tables; no existing table is altered. Every council artifact is
# also folded into the aj_audit hash chain via audit(); these tables are the
# queryable/UI copy.
_DDL_V3 = [
    """CREATE TABLE IF NOT EXISTS aj_council_runs (
        id            INTEGER PRIMARY KEY,
        ts            TEXT NOT NULL,
        cycle_id      TEXT,
        symbol        TEXT NOT NULL,
        policy        TEXT,
        status        TEXT,
        rating        TEXT,
        action        TEXT,
        conviction    REAL,
        thesis        TEXT,
        price_target  REAL,
        time_horizon  TEXT,
        stop_hint     REAL,
        dissent       TEXT,
        cost_usd      REAL DEFAULT 0,
        latency_ms    INTEGER DEFAULT 0,
        n_calls       INTEGER DEFAULT 0,
        decision_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aj_council_runs_sym ON aj_council_runs(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_aj_council_runs_cycle ON aj_council_runs(cycle_id)",
    """CREATE TABLE IF NOT EXISTS aj_analyst_reports (
        id              INTEGER PRIMARY KEY,
        council_run_id  INTEGER NOT NULL,
        ts              TEXT NOT NULL,
        analyst         TEXT NOT NULL,
        band            TEXT,
        score           REAL,
        confidence      REAL,
        narrative       TEXT,
        report_json     TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aj_analyst_reports_run ON aj_analyst_reports(council_run_id)",
    """CREATE TABLE IF NOT EXISTS aj_debate_turns (
        id              INTEGER PRIMARY KEY,
        council_run_id  INTEGER NOT NULL,
        ts              TEXT NOT NULL,
        debate          TEXT NOT NULL CHECK (debate IN ('research','risk')),
        role            TEXT NOT NULL,
        round           INTEGER,
        content         TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aj_debate_turns_run ON aj_debate_turns(council_run_id)",
    """CREATE TABLE IF NOT EXISTS aj_reflections (
        id              INTEGER PRIMARY KEY,
        ts              TEXT NOT NULL,
        symbol          TEXT NOT NULL,
        situation_hash  TEXT,
        raw_return      REAL,
        alpha_return    REAL,
        benchmark       TEXT,
        lesson          TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aj_reflections_sym ON aj_reflections(symbol)",
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
    v = None
    try:
        v = get_setting_raw(_SCHEMA_KEY)
        return int(v) if v is not None else 0
    except Exception:
        # A corrupt non-int schema version would silently re-run every
        # migration (IF NOT EXISTS-safe) and overwrite the version, masking the
        # corruption. Surface it so it's observable rather than swallowed.
        log.warning("_applied_version: corrupt %s value %r; treating as 0",
                    _SCHEMA_KEY, v)
        return 0


def aj_migrate() -> int:
    """Idempotent, forward-only migration. Creates the base schema at step 1
    and records the applied level in settings. Returns the level now in force.

    A migration that ALTERS an existing table MUST take a backup first (§6);
    the base creation here only adds new `aj_*` tables, so no backup is needed
    for step 1. Future column-add steps should call backup_db() before running.
    """
    # Take the (only) backup BEFORE acquiring the write lock. The sole step that
    # ALTERs an existing table is step 4 (options columns); backup_db() opens its
    # own connections and copies the whole DB via the Online Backup API, which
    # can take a while — holding _write_lock across it would block EVERY other
    # writer (audit, orders, fills) for the duration. Decide whether step 4 will
    # run from the pre-lock applied version and snapshot first, outside the lock.
    pre = _applied_version()
    if pre < 4 <= AJ_SCHEMA_TARGET:
        try:
            backup_db()
        except Exception:
            log.debug("aj_migrate: pre-lock backup_db failed (continuing)", exc_info=True)
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
        # Step 3 — analyst-council advisory tables (additive only).
        if current < 3:
            cur = conn.cursor()
            for stmt in _DDL_V3:
                cur.execute(stmt)
            conn.commit()
            current = 3
        # Step 4 — options support: additive option columns on proposals/orders.
        # This ALTERs existing tables, so back up first (§6). ADD COLUMN is
        # safe/idempotent-guarded (we check existing columns before each add).
        if current < 4:
            # Backup was already taken BEFORE the lock (see top of aj_migrate) to
            # avoid holding _write_lock across the whole-DB copy. ADD COLUMN is
            # idempotent-guarded (we check existing columns before each add).
            for table in ("aj_proposals", "aj_orders"):
                have = {r["name"] for r in conn.execute(
                    "PRAGMA table_info({})".format(table)).fetchall()}
                adds = [
                    ("instrument_type", "TEXT NOT NULL DEFAULT 'stock'"),
                    ("underlying", "TEXT"),
                    ("option_type", "TEXT"),
                    ("strike", "REAL"),
                    ("expiry", "TEXT"),
                    ("contract_multiplier", "INTEGER DEFAULT 100"),
                ]
                for col, decl in adds:
                    if col not in have:
                        try:
                            conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, col, decl))
                        except sqlite3.OperationalError as e:
                            # a concurrent migrator (app startup racing a CLI
                            # aj_init) added it between our PRAGMA check and the
                            # ALTER — the column exists, which is all we need
                            if "duplicate column" not in str(e).lower():
                                raise
            conn.commit()
            current = 4
        # Step 5 — screener quote cache (global-best ranking across cycles).
        if current < 5:
            conn.execute("""CREATE TABLE IF NOT EXISTS aj_screen_cache (
                symbol      TEXT PRIMARY KEY,
                ts          TEXT NOT NULL,
                price       REAL,
                volume      REAL,
                market_cap  REAL,
                change_pct  REAL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_screen_cache_ts ON aj_screen_cache(ts)")
            conn.commit()
            current = 5
        if current < 6:
            # Per-cycle decision funnel + scan snapshot — powers the Insights
            # visualizations (decision funnel, activity timeline, selectivity
            # scatter). Pure observability; never read by the trading path.
            conn.execute("""CREATE TABLE IF NOT EXISTS aj_cycle_stats (
                cycle_id     TEXT PRIMARY KEY,
                ts           TEXT NOT NULL,
                mode         TEXT,
                session      TEXT,
                scanned      INTEGER,
                with_signal  INTEGER,
                executed     INTEGER,
                exits        INTEGER,
                result_json  TEXT,
                scan_json    TEXT
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_cycle_stats_ts ON aj_cycle_stats(ts)")
            conn.commit()
            current = 6
        if current < 7:
            # Hot-path indexes: the metrics/gate range scans and the trades/day +
            # TTL cap queries were doing full table scans (substr() filters can't
            # use an index; COUNT(*) over aj_cycles per order was O(orders×cycles)).
            # All additive + idempotent (CREATE INDEX IF NOT EXISTS).
            for stmt in (
                "CREATE INDEX IF NOT EXISTS idx_aj_orders_mode ON aj_orders(mode)",
                "CREATE INDEX IF NOT EXISTS idx_aj_orders_created ON aj_orders(created_at)",
                "CREATE INDEX IF NOT EXISTS idx_aj_risk_events_created ON aj_risk_events(created_at)",
                "CREATE INDEX IF NOT EXISTS idx_aj_routing_ts ON aj_routing(ts)",
                "CREATE INDEX IF NOT EXISTS idx_aj_cycles_started_status ON aj_cycles(started_at, status)",
            ):
                conn.execute(stmt)
            conn.commit()
            current = 7
        if current < 8:
            # Meta-labeling training store: one labeled row per CLOSED trade —
            # features captured at decision time + whether the trade was net
            # profitable. Pure ML training data; never read by the live gate.
            conn.execute("""CREATE TABLE IF NOT EXISTS aj_trade_labels (
                id                INTEGER PRIMARY KEY,
                symbol            TEXT NOT NULL,
                side              TEXT,
                opened_at         TEXT,
                closed_at         TEXT,
                holding_days      REAL,
                regime            TEXT,
                features_json     TEXT NOT NULL,
                realized_return_pct REAL,
                realized_pnl_usd  REAL,
                label             INTEGER,
                created_at        TEXT NOT NULL
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_trade_labels_sym ON aj_trade_labels(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_trade_labels_closed ON aj_trade_labels(closed_at)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_aj_trade_labels_uniq ON aj_trade_labels(symbol, opened_at, closed_at)")
            conn.commit()
            current = 8
        if current < 9:
            # Benchmark-relative labels: was the trade's return over its holding
            # window better than the market's (alpha)? Lets the meta-label model
            # learn P(beats the benchmark) instead of only P(profit). Additive.
            # Idempotency comes from the column-existence check (like step 4) —
            # a blanket except here would stamp version 9 even when a transient
            # SQLITE_BUSY/disk error left the columns missing, permanently
            # disabling alpha-labeling with no re-run path.
            have = {r["name"] for r in conn.execute(
                "PRAGMA table_info(aj_trade_labels)").fetchall()}
            for col, decl in (("benchmark_return_pct", "REAL"),
                              ("alpha_pct", "REAL"),
                              ("beat_benchmark", "INTEGER")):
                if col not in have:
                    try:
                        conn.execute("ALTER TABLE aj_trade_labels ADD COLUMN {} {}".format(col, decl))
                    except sqlite3.OperationalError as e:
                        # concurrent migrator won the race — fine
                        if "duplicate column" not in str(e).lower():
                            raise
            conn.commit()
            current = 9
        if current < 10:
            # v3.16 enhancements groundwork:
            #  - aj_journal: dated reflections/briefings journal (browsable +
            #    weekly rollups) replacing per-day settings blobs.
            #  - aj_signal_scores: per-adapter forecast ledger for the signal
            #    scorecard (log at forecast time, score after the horizon).
            #  - aj_proposals.features_json: decision-time features persisted
            #    at proposal creation so ML labels come from exact entry state
            #    instead of reconstructed cycle snapshots.
            #  - append-only triggers on aj_audit: tampering is PREVENTED
            #    in-band (RAISE ABORT), not just detected by the hash chain.
            conn.execute("""CREATE TABLE IF NOT EXISTS aj_journal (
                id           INTEGER PRIMARY KEY,
                kind         TEXT NOT NULL,
                date         TEXT NOT NULL,
                ts           TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(kind, date)
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_journal_kind_date "
                         "ON aj_journal(kind, date)")
            conn.execute("""CREATE TABLE IF NOT EXISTS aj_signal_scores (
                id           INTEGER PRIMARY KEY,
                ts           TEXT NOT NULL,
                symbol       TEXT NOT NULL,
                adapter      TEXT NOT NULL,
                prob_up      REAL,
                confidence   REAL,
                horizon_days INTEGER,
                price_at     REAL,
                scored_at    TEXT,
                realized_up  INTEGER,
                hit          INTEGER
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_signal_scores_due "
                         "ON aj_signal_scores(scored_at, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_signal_scores_adapter "
                         "ON aj_signal_scores(adapter)")
            have = {r["name"] for r in conn.execute(
                "PRAGMA table_info(aj_proposals)").fetchall()}
            if "features_json" not in have:
                try:
                    conn.execute("ALTER TABLE aj_proposals ADD COLUMN features_json TEXT")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            # Guarded by the __aj_audit_unlock settings flag so explicit
            # maintenance (test resets, retention) can lift it via
            # audit_maintenance(); everything else is blocked in-band. An
            # attacker with raw SQL could set the flag — but they could drop
            # the trigger too; the hash chain remains the detection layer.
            for trg, op in (("trg_aj_audit_no_update", "UPDATE"),
                            ("trg_aj_audit_no_delete", "DELETE")):
                conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS {} BEFORE {} ON aj_audit "
                    "WHEN COALESCE((SELECT value FROM settings WHERE "
                    "key='__aj_audit_unlock'), '') <> '1' "
                    "BEGIN SELECT RAISE(ABORT, 'aj_audit is append-only'); END".format(trg, op))
            conn.commit()
            current = 10
        if current < 11:
            # v3.20 Research Scientist (aj_lab): the experiment queue/ledger —
            # every hypothesis, its K-fold evidence, and the verdict — plus
            # promotion records so live config changes stay traceable to the
            # experiment that earned them AND auto-demotable when live
            # performance falls short of the promoted expectation.
            conn.execute("""CREATE TABLE IF NOT EXISTS aj_lab_experiments (
                id           INTEGER PRIMARY KEY,
                created_at   TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'queued'
                             CHECK (status IN ('queued','running','done','error')),
                kind         TEXT,
                hypothesis   TEXT,
                rationale    TEXT,
                params_json  TEXT NOT NULL,
                evidence_json TEXT,
                verdict      TEXT CHECK (verdict IN
                             ('promoted','rejected','inconclusive', NULL)),
                started_at   TEXT,
                finished_at  TEXT
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_lab_exp_status "
                         "ON aj_lab_experiments(status, id)")
            conn.execute("""CREATE TABLE IF NOT EXISTS aj_lab_promotions (
                id            INTEGER PRIMARY KEY,
                experiment_id INTEGER,
                promoted_at   TEXT NOT NULL,
                key           TEXT NOT NULL,
                old_value     TEXT,
                new_value     TEXT,
                expectation_json TEXT,
                status        TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','demoted','superseded')),
                demoted_at    TEXT,
                demote_reason TEXT
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_aj_lab_prom_status "
                         "ON aj_lab_promotions(status)")
            conn.commit()
            current = 11
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

# ── simulation clock (aj_replay) ──────────────────────────────────────────────
# When set, EVERY aj_* component lives at the simulated instant: sessions,
# day-P&L windows, order timestamps, position aging, journal keys. Only ever
# set inside a dedicated replay process (isolated AUGUR_DB_PATH) — never in
# the live app. Module-global (not thread-local) on purpose: the replay
# process is single-purpose and the operator cycle spawns worker threads that
# must all see the same simulated 'now'.
_SIM_CLOCK: Optional[datetime] = None


def set_sim_clock(dt: Optional[datetime]) -> None:
    """Freeze utc_now() at `dt` (tz-aware, UTC) for this process; None clears.
    aj_replay drives this one trading day at a time."""
    global _SIM_CLOCK
    if dt is not None and dt.tzinfo is None:
        raise ValueError("sim clock must be timezone-aware")
    _SIM_CLOCK = dt.astimezone(timezone.utc) if dt is not None else None


def utc_now() -> datetime:
    if _SIM_CLOCK is not None:
        return _SIM_CLOCK
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
        return days[n - 1] if len(days) >= n else None

    def _last_weekday(month, weekday):
        c = _cal.Calendar()
        days = [d for d in c.itermonthdates(year, month)
                if d.month == month and d.weekday() == weekday]
        return days[-1] if days else None

    def _good_friday():
        # Western Easter via the anonymous Gregorian computus; Good Friday is
        # Easter Sunday minus 2 days. Good Friday is a full NYSE close.
        a = year % 19
        b = year // 100
        c = year % 100
        d = b // 4
        e = b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i = c // 4
        k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31
        day = ((h + l - 7 * m + 114) % 31) + 1
        return (datetime(year, month, day) - timedelta(days=2)).date()

    hols = set()
    hols.add(_good_friday())                        # Good Friday (NYSE closed)
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
        new_val = "{}:{}".format(now, os.getpid())
        with db._write_lock:
            # MUST read the raw row: get_settings() hides `__` keys, so reading
            # the lease through it always returned None and the lock never
            # excluded anything (fail-open). Read directly.
            cur = get_setting_raw(key)
            conn = db.get_conn()
            if not cur:
                # No lease row at all — claim it with a guarded INSERT. A
                # concurrent process that inserted first makes this raise on the
                # PRIMARY KEY (or no-op under OR IGNORE) so only one wins.
                try:
                    c = conn.execute(
                        "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
                        (key, new_val))
                    conn.commit()
                    if c.rowcount != 1:
                        return False
                except Exception:
                    return False
                self._have = True
                self._lease_val = new_val
                return True
            # A lease row exists. If it is still within TTL, we lost.
            try:
                held_at = float(str(cur).split(":")[0])
                if now - held_at < self.ttl_s:
                    return False
            except Exception:
                # Unparseable lease — treat as stale and try to take it via CAS.
                pass
            # Compare-and-set: only steal the lease if the row STILL holds the
            # exact stale value we just read. A second process that swapped it
            # between our read and this UPDATE changes the value, so its rowcount
            # is 1 and ours is 0 — we are told we lost (fixes the cross-process
            # TOCTOU; the in-process _write_lock alone never excluded siblings).
            c = conn.execute(
                "UPDATE settings SET value=? WHERE key=? AND value=?",
                (new_val, key, cur))
            conn.commit()
            if c.rowcount != 1:
                return False
        self._have = True
        self._lease_val = new_val
        return True

    def release(self) -> None:
        if not self._have:
            return
        if self._fh:
            try:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            # Always close the handle even if the unlock above raised, so the
            # lock file descriptor never leaks.
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        try:
            with db._write_lock:
                conn = db.get_conn()
                # Release only OUR lease (guard on the value we wrote): if a
                # long run overshot the TTL and another process legitimately
                # stole the lease, an unconditional DELETE would free the
                # thief's lease and let a THIRD instance run concurrently.
                val = getattr(self, "_lease_val", None)
                if val is not None:
                    conn.execute("DELETE FROM settings WHERE key=? AND value=?",
                                 ("__aj_lease_{}".format(self.name), val))
                else:
                    conn.execute("DELETE FROM settings WHERE key=?",
                                 ("__aj_lease_{}".format(self.name),))
                conn.commit()
        except Exception:
            pass
        self._have = False
        self._lease_val = None

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


def claim_stale_cycle(cycle_id: str) -> bool:
    """Atomically claim a stale 'running' cycle for crash recovery: flip it to
    'crashed' ONLY if it is still 'running' (compare-and-set). Returns True iff
    THIS caller won the claim (rowcount == 1). Two recovery passes racing the
    same stale cycle must not both reconcile it — the loser sees rowcount 0 and
    skips it. Closes the find-then-mark TOCTOU in recover_crashed_cycles."""
    with db._write_lock:
        conn = db.get_conn()
        cur = conn.execute(
            "UPDATE aj_cycles SET status='crashed' WHERE cycle_id=? AND status='running'",
            (cycle_id,))
        conn.commit()
        return cur.rowcount == 1


# ── append-only audit + hash chain (§8) ──────────────────────────────────────

import contextlib


@contextlib.contextmanager
def audit_maintenance():
    """Scoped unlock of aj_audit's append-only triggers (schema v10) for
    explicit maintenance — test resets, retention pruning. In-band app code
    can never UPDATE/DELETE audit rows outside this context, and the hash
    chain still detects anything mutated under it."""
    with db._write_lock:
        conn = db.get_conn()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) "
                     "VALUES ('__aj_audit_unlock','1')")
        conn.commit()
    try:
        yield
    finally:
        try:
            with db._write_lock:
                conn = db.get_conn()
                conn.execute("DELETE FROM settings WHERE key='__aj_audit_unlock'")
                conn.commit()
            db._invalidate_settings_cache()
        except Exception:
            log.exception("audit_maintenance: relock failed")


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
        # Keep the stored audit body valid, canonical JSON even when the payload
        # can't be serialized, so it stays parseable/reproducible instead of an
        # unstable, dict-ordering-dependent str().
        try:
            return json.dumps({"_unserializable": str(payload)},
                              sort_keys=True, separators=(",", ":"))
        except Exception:
            return json.dumps({"_unserializable": "<unrepresentable>"},
                              sort_keys=True, separators=(",", ":"))


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
        # BEGIN IMMEDIATE takes the cross-PROCESS write lock before reading the
        # chain tip. The in-process RLock alone lets a second process (Flask app
        # vs CLI operator) interleave read-tip/insert, forking the hash chain —
        # a false "prev_hash linkage broken" tamper verdict that never
        # self-heals. Tolerate an already-open transaction (legacy isolation
        # mode opens one implicitly after DML).
        took_txn = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            took_txn = True
        except sqlite3.OperationalError:
            pass
        try:
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
        except Exception:
            if took_txn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        return int(cur.lastrowid)


# Incremental-verify cache: the last clean (id, row_hash, total rows) we
# verified up to. The hot path (status/health ticks) only re-verifies rows
# APPENDED since, chaining from the cached row_hash — re-hashing the entire
# append-only log on every tick was O(rows) per call and dominated those paths.
_CHAIN_CACHE_LOCK = threading.Lock()
_chain_cache: Dict[str, Any] = {"last_id": 0, "last_row_hash": None, "rows": 0}


def _verify_chain_rows(rows, prev_row_hash) -> Dict[str, Any]:
    """Verify `rows` (ordered by id ASC), linking the FIRST row's prev_hash to
    `prev_row_hash` (None when `rows` begins the chain). Returns the standard
    result dict; on success includes the last (id, row_hash) verified."""
    last_id = None
    last_hash = prev_row_hash
    for i, r in enumerate(rows):
        stored_prev = r["prev_hash"] or None
        if prev_row_hash is None and i == 0:
            # genuine head of the chain must start cleanly (null prev_hash)
            if stored_prev is not None:
                return {"ok": False, "broken_at": r["id"],
                        "reason": "chain does not start cleanly (prefix deleted?)"}
        elif stored_prev != last_hash:
            return {"ok": False, "broken_at": r["id"], "reason": "prev_hash linkage broken"}
        material = "{}|{}|{}|{}|{}|{}".format(
            stored_prev or "", r["ts"], r["kind"],
            r["ref_id"] if r["ref_id"] is not None else "",
            r["actor"] or "", r["payload_json"])
        expect = hashlib.sha256(material.encode("utf-8")).hexdigest()
        if expect != r["row_hash"]:
            return {"ok": False, "broken_at": r["id"], "reason": "row_hash mismatch"}
        last_hash = r["row_hash"]
        last_id = r["id"]
    return {"ok": True, "last_id": last_id, "last_row_hash": last_hash}


def verify_audit_chain(quick: bool = False) -> Dict[str, Any]:
    """Verify the aj_audit hash chain. A clean chain means no row was edited or
    deleted out of band.

    DEFAULT is a FULL re-scan from the chain head — the authoritative integrity
    check (catches tampering ANYWHERE in the log). Pass quick=True for the hot
    status/health path: it re-verifies only rows APPENDED since the last clean
    verification (chaining from the cached row_hash), confirming the cached
    anchor row is still present + unchanged before trusting the prefix. The
    quick path is O(new rows); the full path is O(all rows). A stale anchor /
    detected break falls back to a full re-scan."""
    conn = db.get_conn()
    if quick:
        with _CHAIN_CACHE_LOCK:
            c = dict(_chain_cache)
        if c["last_id"] and c["last_row_hash"]:
            # Confirm the cached anchor row is still present + unchanged (a
            # delete/edit inside the verified prefix invalidates the cache).
            anchor = conn.execute(
                "SELECT row_hash FROM aj_audit WHERE id=?", (c["last_id"],)).fetchone()
            if anchor and anchor["row_hash"] == c["last_row_hash"]:
                new_rows = conn.execute(
                    "SELECT id, ts, kind, ref_id, actor, payload_json, prev_hash, row_hash "
                    "FROM aj_audit WHERE id > ? ORDER BY id ASC", (c["last_id"],)).fetchall()
                res = _verify_chain_rows(new_rows, c["last_row_hash"])
                if res["ok"]:
                    last_id = res.get("last_id") or c["last_id"]
                    last_hash = res.get("last_row_hash") or c["last_row_hash"]
                    total = c["rows"] + len(new_rows)
                    with _CHAIN_CACHE_LOCK:
                        _chain_cache.update(last_id=last_id, last_row_hash=last_hash, rows=total)
                    return {"ok": True, "rows": total}
                # break in the tail — report it directly (no need to full-scan).
                return {"ok": res["ok"], "broken_at": res.get("broken_at"),
                        "reason": res.get("reason")}
            # cache stale / anchor gone -> fall through to a full re-scan.
    # Full scan from the head.
    rows = conn.execute(
        "SELECT id, ts, kind, ref_id, actor, payload_json, prev_hash, row_hash "
        "FROM aj_audit ORDER BY id ASC").fetchall()
    res = _verify_chain_rows(rows, None)
    if res["ok"]:
        with _CHAIN_CACHE_LOCK:
            _chain_cache.update(last_id=res.get("last_id") or 0,
                                last_row_hash=res.get("last_row_hash"),
                                rows=len(rows))
        return {"ok": True, "rows": len(rows)}
    return {"ok": res["ok"], "broken_at": res.get("broken_at"), "reason": res.get("reason")}


# ── generic row helpers for aj_* tables ──────────────────────────────────────

import re as _re

# Tables these generic helpers are allowed to touch. Values are parameterized,
# but identifiers (table/column names) are interpolated into the SQL text, so we
# whitelist tables and validate column identifiers to close the injection vector
# for any path where a name might be derived from config/external input.
_ALLOWED_TABLES = frozenset({
    "aj_proposals", "aj_orders", "aj_fills", "aj_risk_events", "aj_routing",
    "aj_recon", "aj_cycles", "aj_equity", "aj_cycle_log", "aj_position_state",
    # Analyst Council (step 3) — advisory artifacts, also audit-chained.
    "aj_council_runs", "aj_analyst_reports", "aj_debate_turns", "aj_reflections",
    # screener quote cache (step 5)
    "aj_screen_cache",
    # per-cycle decision funnel + scan snapshot (step 6, observability)
    "aj_cycle_stats",
    # meta-labeling training store (step 8, ML training data)
    "aj_trade_labels",
    # journal + adapter scorecard (step 10)
    "aj_journal", "aj_signal_scores",
    # Research Scientist experiment queue/ledger + promotions (step 11)
    "aj_lab_experiments", "aj_lab_promotions",
})
_IDENT_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_table(table: str) -> str:
    # aj_audit is append-only and hash-chained: force callers through audit()
    # so a generic insert can't write a chain-bypassing row (breaks tamper-
    # evidence). Any non-whitelisted / malformed table name is rejected too.
    if table == "aj_audit":
        raise ValueError("aj_audit is append-only; use audit()")
    if table not in _ALLOWED_TABLES:
        raise ValueError("disallowed table: {!r}".format(table))
    return table


def _check_cols(*names: str) -> None:
    for n in names:
        if not isinstance(n, str) or not _IDENT_RE.match(n):
            raise ValueError("invalid column identifier: {!r}".format(n))


def insert(table: str, **cols) -> int:
    _check_table(table)
    _check_cols(*cols.keys())
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
    _check_table(table)
    _check_cols(*cols.keys())
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
    _check_table(table)
    _check_cols(where_col, *cols.keys())
    sets = ",".join("{}=?".format(k) for k in cols)
    with db._write_lock:
        conn = db.get_conn()
        cur = conn.execute(
            "UPDATE {} SET {} WHERE id=? AND {}=?".format(table, sets, where_col),
            tuple(cols.values()) + (row_id, where_val))
        conn.commit()
        return cur.rowcount > 0


def get_row(table: str, row_id: int) -> Optional[Dict[str, Any]]:
    _check_table(table)
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
    try:
        d = sqlite3.connect(dest)
    except Exception:
        # If opening the destination fails, the source FD would otherwise leak.
        s.close()
        raise
    try:
        with d:
            s.backup(d)
    finally:
        s.close()
        d.close()
    log.info("aj backup -> %s", dest)
    return dest
