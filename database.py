import os
import sqlite3
import json
import threading
import time
from datetime import datetime, timezone

# Default to a relative path so existing CLI / dev workflows keep working;
# the desktop bundle overrides this via AUGUR_DB_PATH to point at the user's
# Application Support directory (which is the only writable place once the
# app is launched as a .app bundle from /Applications).
#
# Resolve to an absolute path at import time. If anything in-process later
# calls os.chdir() (some plotting / data-science libs do this in test
# fixtures, and pywebview backends sometimes shift cwd on macOS), subsequent
# threads opening a fresh connection would otherwise create a SECOND
# wealth.db in the new working directory — silent data fork. The thread-local
# connection pool means the original thread keeps writing to the first file
# while new threads write to the second; portfolios appear to vanish.
_DB_PATH_RAW = os.environ.get("AUGUR_DB_PATH", "wealth.db")
DB_PATH = os.path.abspath(_DB_PATH_RAW)

# Thread-local connection pool — reuse connections per thread instead of
# opening/closing on every call. Saves ~1-5ms per DB operation.
_local = threading.local()

# Serializes read-modify-write sequences (e.g. add_position averages an
# existing row). SQLite's WAL handles single-statement writes, but the
# Python-side compute-then-update pattern here can still race.
_write_lock = threading.RLock()


def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # faster concurrent reads
        conn.execute("PRAGMA synchronous=NORMAL")  # safe + faster writes
        conn.execute("PRAGMA busy_timeout=5000")  # 5s before SQLITE_BUSY
        # SQLite ships with foreign_keys = OFF per-connection. Our schema
        # declares ON DELETE SET NULL on portfolio.account_id and
        # transactions.account_id — those clauses are silently no-ops without
        # this PRAGMA, so dangling account_ids could survive an account row
        # being deleted by anything other than delete_account(). Enable it.
        conn.execute("PRAGMA foreign_keys = ON")
        _local.conn = conn
    return conn


def close_thread_conn():
    """Explicitly close this thread's connection. Short-lived worker
    threads (the snapshot worker on each cycle, a one-shot prune call from
    a CLI script, etc.) should call this before exiting so the FD is
    released immediately rather than waiting for GC. No-op if no
    connection was opened on this thread.

    Commits before close: sqlite3.Connection.close() ROLLS BACK any open
    implicit transaction rather than committing it. If a code path
    finished a successful sequence of writes but skipped the explicit
    commit (e.g., an early return in a future helper), close_thread_conn
    would silently discard those writes. Defensive commit ensures we
    persist whatever was staged before releasing the FD.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.commit()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        try:
            del _local.conn
        except AttributeError:
            pass


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        account_type TEXT NOT NULL DEFAULT 'brokerage',
        institution TEXT,
        notes TEXT,
        color TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS portfolio (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        name TEXT,
        shares REAL NOT NULL,
        avg_cost REAL NOT NULL,
        asset_type TEXT DEFAULT 'stock',
        sector TEXT,
        currency TEXT DEFAULT 'USD',
        notes TEXT,
        account_id INTEGER,
        added_date TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL UNIQUE,
        name TEXT,
        asset_type TEXT DEFAULT 'stock',
        alert_high REAL,
        alert_low REAL,
        notes TEXT,
        added_date TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        action TEXT NOT NULL,
        shares REAL NOT NULL,
        price REAL NOT NULL,
        total REAL NOT NULL,
        fees REAL DEFAULT 0,
        date TEXT,
        notes TEXT,
        account_id INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS price_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        alert_type TEXT NOT NULL,
        price REAL NOT NULL,
        triggered INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        triggered_at TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        date TEXT UNIQUE,
        total_value REAL,
        total_cost REAL,
        total_pnl REAL,
        total_pnl_pct REAL,
        positions_json TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS sec_filings_cache (
        accession TEXT PRIMARY KEY,
        ticker TEXT,
        form_type TEXT,
        filing_date TEXT,
        description TEXT,
        filing_text TEXT,
        ai_signal TEXT,
        ai_summary TEXT,
        ai_key_points TEXT,
        ai_event_type TEXT,
        ai_powered INTEGER DEFAULT 0,
        cached_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS insider_transactions_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT,
        accession TEXT,
        insider_name TEXT,
        title TEXT,
        transaction_type TEXT,
        security TEXT,
        shares REAL,
        price REAL,
        value REAL,
        shares_after REAL,
        date TEXT,
        form_url TEXT,
        cached_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(ticker, accession, insider_name, date)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS institutional_cache (
        fund_name TEXT,
        fund_cik TEXT,
        filing_date TEXT,
        period TEXT,
        total_value_usd REAL,
        holdings_json TEXT,
        cached_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (fund_cik, filing_date)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS earnings_cache (
        symbol TEXT PRIMARY KEY,
        dossier_json TEXT,
        cached_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS scanner_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scanned_at TEXT NOT NULL,
        profile_hash TEXT,
        strategy TEXT,
        symbol TEXT NOT NULL,
        asset_class TEXT,
        composite_score REAL,
        badge TEXT,
        rank_in_scan INTEGER
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scanner_history_symbol ON scanner_history(symbol)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_scanner_history_scanned_at ON scanner_history(scanned_at)")

    # Per-day OpenAI call counter — backs the daily cap in ai_summarizer.
    # Persisted in SQLite so the cap survives process restarts (Flask reloads,
    # desktop bundle relaunches). Keyed on the local YYYY-MM-DD date string.
    c.execute("""CREATE TABLE IF NOT EXISTS ai_call_log (
        date TEXT PRIMARY KEY,
        count INTEGER NOT NULL DEFAULT 0
    )""")

    # ── Jarvis statefulness (v1.4): conversations + durable memory ──
    c.execute("""CREATE TABLE IF NOT EXISTS jarvis_conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT DEFAULT CURRENT_TIMESTAMP,
        archived INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS jarvis_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL REFERENCES jarvis_conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        symbol TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS jarvis_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact TEXT NOT NULL,
        source TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS jarvis_insights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day TEXT NOT NULL,
        kind TEXT NOT NULL,
        tone TEXT,
        priority INTEGER,
        title TEXT NOT NULL,
        detail TEXT,
        view TEXT,
        symbol TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(day, kind, title)
    )""")

    # ── Indexes for performance ──
    c.execute("CREATE INDEX IF NOT EXISTS idx_jarvis_messages_conv ON jarvis_messages(conversation_id, id)")
    # jarvis_insights_since filters on created_at (the digest's "while you
    # were away" query) and jarvis_log_insights prunes on it daily — both
    # were full-table scans without this.
    c.execute("CREATE INDEX IF NOT EXISTS idx_jarvis_insights_created ON jarvis_insights(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_symbol ON portfolio(symbol)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_transactions_symbol ON transactions(symbol)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON price_alerts(symbol)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_triggered ON price_alerts(triggered)")
    # `get_price_alerts` always sorts by `created_at DESC` (and the trigger-loop
    # variant pre-filters on triggered=0 then orders by created_at). Without
    # this index SQLite does a full table scan + filesort every alert refresh.
    c.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_created ON price_alerts(created_at)")
    # Composite index for the alert-polling hot path: `WHERE triggered=0
    # ORDER BY created_at DESC` (every 5 min from the frontend + trigger
    # loop). The single-column indexes above force SQLite to pick one and
    # then sort with a temp B-tree (verified via EXPLAIN QUERY PLAN); this
    # composite serves both the filter and the ordering in one pass.
    c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_triggered_created "
              "ON price_alerts(triggered, created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_filings_cache_ticker ON sec_filings_cache(ticker)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_insider_cache_ticker ON insider_transactions_cache(ticker)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_insider_cache_ticker_date ON insider_transactions_cache(ticker, cached_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_institutional_cache_cik ON institutional_cache(fund_cik)")

    # ── Migrations: add account_id columns to existing tables ──
    # Serialize the racy ALTER TABLE migrations and the settings seeding under
    # the same lock every other writer uses. start() is reachable from several
    # boot paths, so the cache warmer / a request thread may write concurrently;
    # without this an ALTER could interleave with another writer and a parallel
    # get_conn() could observe a half-migrated schema. _write_lock is reentrant,
    # so _migrate_settings's nested acquisition below is safe.
    with _write_lock:
        try:
            c.execute("ALTER TABLE portfolio ADD COLUMN account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            c.execute("ALTER TABLE transactions ADD COLUMN account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL")
        except sqlite3.OperationalError:
            pass  # column already exists

        c.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_account ON portfolio(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id)")

        # Default settings
        defaults = {
            "refresh_interval": "60",
            "currency": "USD",
            "theme": "terminal-green",
            "show_crypto": "true",
            "benchmark": "SPY",
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

        # ── Settings schema version ──
        # Tracks which migration pass has run against the settings table. Old
        # releases stored typo'd or now-renamed keys (e.g. `show_crypto`
        # accidentally stored as `showcrypto`); without a version sentinel we
        # have no way to know whether a given key in the table is a stale
        # leftover or a legitimate user override. Bump SCHEMA_VERSION below
        # and add a corresponding case in _migrate_settings() to retire keys.
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                  ("__schema_version__", "1"))

        conn.commit()
    # Run settings migrations after defaults so we can prune renamed keys.
    try:
        _migrate_settings(conn)
    except Exception as e:
        # Migration errors must never block app startup — log and continue
        # with the un-migrated schema. The app reads settings with
        # `dict.get(..., default)` so missing keys degrade gracefully.
        import logging as _logging
        _logging.getLogger("augur.db").warning("settings migration failed: %s", e)
    # Defaults/migrations may have changed rows under a previously-cached
    # (possibly empty) snapshot — drop it so the next read sees fresh data.
    _invalidate_settings_cache()
    # connection reused (thread-local pool)


SCHEMA_VERSION = 1
# List of settings keys retired in past releases. We delete them on init so
# stale values don't poison new lookups. Append to this when renaming.
_RETIRED_SETTING_KEYS = []  # type: list


def _migrate_settings(conn):
    """Run idempotent settings migrations. Called at the end of init_db()."""
    cur = conn.execute("SELECT value FROM settings WHERE key='__schema_version__'")
    row = cur.fetchone()
    # A corrupt/non-numeric sentinel from an older build should be treated as
    # "needs migration" (0) rather than aborting the migration entirely.
    try:
        current = int(row["value"]) if row else 0
    except (TypeError, ValueError):
        current = 0
    if current >= SCHEMA_VERSION:
        # Even at the latest version, sweep retired keys (cheap + safe).
        if _RETIRED_SETTING_KEYS:
            with _write_lock:
                conn.executemany(
                    "DELETE FROM settings WHERE key=?",
                    [(k,) for k in _RETIRED_SETTING_KEYS],
                )
                conn.commit()
        return
    # Future per-version steps go here. Example shape:
    #   if current < 2: ... rename / coerce ...
    # For now we just stamp the version forward.
    with _write_lock:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("__schema_version__", str(SCHEMA_VERSION)),
        )
        if _RETIRED_SETTING_KEYS:
            conn.executemany(
                "DELETE FROM settings WHERE key=?",
                [(k,) for k in _RETIRED_SETTING_KEYS],
            )
        conn.commit()


# ── Accounts ──────────────────────────────────────────────────────────────────

ACCOUNT_TYPES = [
    "brokerage", "401k", "ira", "roth_ira", "hsa",
    "529", "trust", "crypto", "other",
]


def get_accounts():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def get_account(account_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def add_account(name, account_type="brokerage", institution="", notes="", color=""):
    with _write_lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO accounts (name, account_type, institution, notes, color) VALUES (?,?,?,?,?)",
            (name, account_type, institution, notes, color)
        )
        conn.commit()
        return cur.lastrowid


def update_account(account_id, name=None, account_type=None, institution=None, notes=None, color=None):
    with _write_lock:
        conn = get_conn()
        acct = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not acct:
            return False
        conn.execute(
            "UPDATE accounts SET name=?, account_type=?, institution=?, notes=?, color=? WHERE id=?",
            (
                name if name is not None else acct["name"],
                account_type if account_type is not None else acct["account_type"],
                institution if institution is not None else acct["institution"],
                notes if notes is not None else acct["notes"],
                color if color is not None else acct["color"],
                account_id,
            )
        )
        conn.commit()
        return True


def delete_account(account_id):
    with _write_lock:
        conn = get_conn()
        try:
            # Nullify portfolio/transaction references first. All three
            # statements must land together — a failure after the UPDATEs but
            # before the DELETE (or vice versa) would leave the account half
            # detached. Roll back so nothing is committed on partial failure.
            conn.execute("UPDATE portfolio SET account_id = NULL WHERE account_id = ?", (account_id,))
            conn.execute("UPDATE transactions SET account_id = NULL WHERE account_id = ?", (account_id,))
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ── Portfolio ──────────────────────────────────────────────────────────────────

def get_portfolio(account_id=None):
    conn = get_conn()
    if account_id is not None:
        rows = conn.execute(
            "SELECT p.*, a.name AS account_name, a.account_type AS acct_type, a.institution, a.color AS account_color "
            "FROM portfolio p LEFT JOIN accounts a ON p.account_id = a.id "
            "WHERE p.account_id = ? ORDER BY p.asset_type, p.symbol",
            (account_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT p.*, a.name AS account_name, a.account_type AS acct_type, a.institution, a.color AS account_color "
            "FROM portfolio p LEFT JOIN accounts a ON p.account_id = a.id "
            "ORDER BY p.asset_type, p.symbol"
        ).fetchall()
    return [dict(r) for r in rows]


def add_position(symbol, name, shares, avg_cost, asset_type="stock", sector="", currency="USD", notes="", account_id=None):
    with _write_lock:
        conn = get_conn()
        # Check if exists — if so, average down/up (same symbol + asset_type + account)
        if account_id is not None:
            existing = conn.execute(
                "SELECT * FROM portfolio WHERE symbol = ? AND asset_type = ? AND account_id = ?",
                (symbol.upper(), asset_type, account_id)
            ).fetchone()
        else:
            existing = conn.execute(
                "SELECT * FROM portfolio WHERE symbol = ? AND asset_type = ? AND account_id IS NULL",
                (symbol.upper(), asset_type)
            ).fetchone()
        if existing:
            total_shares = existing["shares"] + shares
            # Snap float-dust residuals (e.g. 1e-13 left by fractional-share
            # sells) to exactly 0 so the position reads flat instead of
            # carrying a tiny non-zero count that would yield a garbage
            # weighted-average cost basis below.
            if abs(total_shares) < 1e-9:
                total_shares = 0.0
            if shares < 0:
                # A partial sell: the cost basis (avg_cost) is unchanged by a
                # sale — only the share count drops. The old weighted-average
                # math used the SELL price as if it were a buy, which corrupted
                # basis (and could drive avg_cost to 0/negative). Reject an
                # over-sell (would make holdings negative) rather than silently
                # flip the position short.
                if total_shares < 0:
                    raise ValueError(
                        f"Cannot sell {abs(shares)} shares of {symbol.upper()}; "
                        f"only {existing['shares']} held"
                    )
                new_avg = existing["avg_cost"]
                if total_shares == 0:
                    # Fully closed: basis is meaningless, leave it at the prior
                    # avg so history reads sanely; shares==0 marks it flat.
                    new_avg = existing["avg_cost"]
            else:
                total_cost = (existing["shares"] * existing["avg_cost"]) + (shares * avg_cost)
                new_avg = (total_cost / total_shares) if total_shares else 0.0
            conn.execute(
                "UPDATE portfolio SET shares = ?, avg_cost = ?, name = ? WHERE id = ?",
                (total_shares, new_avg, name or existing["name"], existing["id"])
            )
            row_id = existing["id"]
        else:
            # No existing position: a SELL (shares<0) here would insert a
            # phantom short with the sell price as basis. Reject it — the
            # over-sell protection above only covers the average-down path.
            if shares < 0:
                raise ValueError(
                    f"Cannot sell {abs(shares)} shares of {symbol.upper()}; "
                    f"no position held"
                )
            cur = conn.execute(
                "INSERT INTO portfolio (symbol, name, shares, avg_cost, asset_type, sector, currency, notes, account_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (symbol.upper(), name, shares, avg_cost, asset_type, sector, currency, notes, account_id)
            )
            row_id = cur.lastrowid
        conn.commit()
    return row_id


# Sentinel distinguishing "argument omitted → keep existing value" from an
# explicit None (e.g. account_id=None means "unassign this position from its
# account"). Using None as the default would make it impossible to clear a
# field via update_position.
_UNSET = object()


def update_position(pos_id, shares=None, avg_cost=None, notes=None, account_id=_UNSET):
    with _write_lock:
        conn = get_conn()
        pos = conn.execute("SELECT * FROM portfolio WHERE id = ?", (pos_id,)).fetchone()
        if not pos:
            return False
        new_shares = shares if shares is not None else pos["shares"]
        new_cost = avg_cost if avg_cost is not None else pos["avg_cost"]
        new_notes = notes if notes is not None else pos["notes"]
        new_acct = pos["account_id"] if account_id is _UNSET else account_id
        conn.execute(
            "UPDATE portfolio SET shares = ?, avg_cost = ?, notes = ?, account_id = ? WHERE id = ?",
            (new_shares, new_cost, new_notes, new_acct, pos_id)
        )
        conn.commit()
        return True


def delete_position(pos_id):
    with _write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM portfolio WHERE id = ?", (pos_id,))
        conn.commit()


# ── Watchlist ──────────────────────────────────────────────────────────────────

def get_watchlist():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM watchlist ORDER BY symbol").fetchall()
    # connection reused (thread-local pool)
    return [dict(r) for r in rows]


def add_to_watchlist(symbol, name="", asset_type="stock", alert_high=None, alert_low=None, notes=""):
    sym = symbol.upper()
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO watchlist (symbol, name, asset_type, alert_high, alert_low, notes) VALUES (?,?,?,?,?,?)",
                (sym, name, asset_type, alert_high, alert_low, notes)
            )
            conn.commit()
            result = True
        except sqlite3.IntegrityError:
            # Only treat this as a duplicate if a row with that symbol actually
            # exists; any other constraint failure (e.g. NOT NULL) must surface
            # rather than be swallowed as a no-op "update".
            existing = conn.execute(
                "SELECT 1 FROM watchlist WHERE symbol = ?", (sym,)
            ).fetchone()
            if not existing:
                raise
            # On a re-add, only overwrite fields the caller explicitly passed.
            # The old blanket UPDATE wiped alert_high/low/notes to None/'' even
            # when the caller didn't supply them (e.g. a plain `watch AAPL`
            # erased alerts set earlier). Pass-through None/'' means "leave it".
            conn.execute(
                """UPDATE watchlist SET
                       alert_high = COALESCE(?, alert_high),
                       alert_low  = COALESCE(?, alert_low),
                       notes      = CASE WHEN ? <> '' THEN ? ELSE notes END,
                       name       = CASE WHEN ? <> '' THEN ? ELSE name END
                   WHERE symbol = ?""",
                (alert_high, alert_low, notes, notes, name, name, sym)
            )
            conn.commit()
            result = False
        # connection reused (thread-local pool)
        return result


def delete_from_watchlist(wl_id):
    with _write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM watchlist WHERE id = ?", (wl_id,))
        conn.commit()
        # connection reused (thread-local pool)


# ── Transactions ───────────────────────────────────────────────────────────────

def get_transactions(symbol=None, limit=100, account_id=None):
    conn = get_conn()
    base = ("SELECT t.*, a.name AS account_name, a.account_type AS acct_type "
            "FROM transactions t LEFT JOIN accounts a ON t.account_id = a.id")
    conditions = []
    params = []
    if symbol:
        conditions.append("t.symbol = ?")
        params.append(symbol.upper())
    if account_id is not None:
        conditions.append("t.account_id = ?")
        params.append(account_id)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    rows = conn.execute(base + where + " ORDER BY COALESCE(t.date, t.created_at) DESC, t.created_at DESC LIMIT ?", params).fetchall()
    return [dict(r) for r in rows]


def add_transaction(symbol, action, shares, price, fees=0, date=None, notes="", account_id=None):
    with _write_lock:
        conn = get_conn()
        total = shares * price
        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO transactions (symbol, action, shares, price, total, fees, date, notes, account_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (symbol.upper(), (action or "").upper(), shares, price, total, fees, date, notes, account_id)
        )
        conn.commit()


def get_transaction_stats():
    """Aggregate trading-activity stats in a single query — feeds Jarvis
    context blocks, where one round trip matters more than normalization.
    Actions are stored upper-cased by add_transaction, but upper() in the
    SQL tolerates rows imported by older builds / external tools.
    Returns zeros/Nones (never raises) so callers can render unconditionally."""
    try:
        conn = get_conn()
        row = conn.execute(
            """SELECT COUNT(*) AS total_trades,
                      COALESCE(SUM(upper(trim(action)) = 'BUY'), 0) AS buys,
                      COALESCE(SUM(upper(trim(action)) = 'SELL'), 0) AS sells,
                      MIN(date) AS first_trade_date,
                      MAX(date) AS last_trade_date,
                      (SELECT symbol FROM transactions
                       GROUP BY symbol
                       ORDER BY COUNT(*) DESC, symbol ASC
                       LIMIT 1) AS most_traded_symbol
               FROM transactions"""
        ).fetchone()
        return {
            "total_trades": int(row["total_trades"] or 0),
            "buys": int(row["buys"] or 0),
            "sells": int(row["sells"] or 0),
            "first_trade_date": row["first_trade_date"],
            "last_trade_date": row["last_trade_date"],
            "most_traded_symbol": row["most_traded_symbol"],
        }
    except Exception:
        return {"total_trades": 0, "buys": 0, "sells": 0,
                "first_trade_date": None, "last_trade_date": None,
                "most_traded_symbol": None}


# ── Price Alerts ──────────────────────────────────────────────────────────────

def get_price_alerts(include_triggered=False):
    conn = get_conn()
    if include_triggered:
        rows = conn.execute(
            "SELECT * FROM price_alerts ORDER BY triggered ASC, created_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM price_alerts WHERE triggered=0 ORDER BY created_at DESC"
        ).fetchall()
    # connection reused (thread-local pool)
    return [dict(r) for r in rows]


def add_price_alert(symbol, alert_type, price, note=""):
    with _write_lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO price_alerts (symbol, alert_type, price, triggered) VALUES (?,?,?,0)",
            (symbol.upper(), alert_type, price)
        )
        row_id = cur.lastrowid
        conn.commit()
        # connection reused (thread-local pool)
        return row_id


def delete_price_alert(alert_id):
    with _write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM price_alerts WHERE id=?", (alert_id,))
        conn.commit()
        # connection reused (thread-local pool)


def mark_alert_triggered(alert_id):
    with _write_lock:
        conn = get_conn()
        conn.execute(
            "UPDATE price_alerts SET triggered=1, triggered_at=CURRENT_TIMESTAMP WHERE id=?",
            (alert_id,)
        )
        conn.commit()
        # connection reused (thread-local pool)


def clear_triggered_alerts():
    with _write_lock:
        conn = get_conn()
        conn.execute("DELETE FROM price_alerts WHERE triggered=1")
        conn.commit()
        # connection reused (thread-local pool)


# ── Jarvis statefulness: conversations + durable memory (v1.4) ─────────────────

_JARVIS_TURN_LIMIT = 200  # per-conversation message cap (prune oldest beyond)


def jarvis_active_conversation():
    """Id of the latest non-archived conversation, creating one if none."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM jarvis_conversations WHERE archived=0 "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        return row["id"]
    return jarvis_new_conversation()


def jarvis_new_conversation():
    """Archive open threads and start a fresh one."""
    with _write_lock:
        conn = get_conn()
        conn.execute("UPDATE jarvis_conversations SET archived=1 WHERE archived=0")
        cur = conn.execute("INSERT INTO jarvis_conversations DEFAULT VALUES")
        conn.commit()
        return cur.lastrowid


def jarvis_add_message(conversation_id, role, content, symbol=None):
    with _write_lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO jarvis_messages (conversation_id, role, content, symbol) "
            "VALUES (?,?,?,?)",
            (conversation_id, role, (content or "")[:4000], symbol))
        # Cap the thread so a long-running install can't grow unbounded.
        conn.execute(
            "DELETE FROM jarvis_messages WHERE conversation_id=? AND id NOT IN "
            "(SELECT id FROM jarvis_messages WHERE conversation_id=? "
            " ORDER BY id DESC LIMIT ?)",
            (conversation_id, conversation_id, _JARVIS_TURN_LIMIT))
        conn.commit()


def jarvis_add_exchange(conversation_id, question, answer, symbol=None):
    """Write a user message and its assistant reply ATOMICALLY (one lock, one
    commit). Two ask() calls racing on the same conversation otherwise
    interleave as U1,U2,A1,A2 — and the Q/A pairing then mismatches answers
    to questions. Writing the pair together keeps them adjacent."""
    with _write_lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO jarvis_messages (conversation_id, role, content, symbol) "
                "VALUES (?,?,?,?)",
                (conversation_id, "user", (question or "")[:4000], symbol))
            conn.execute(
                "INSERT INTO jarvis_messages (conversation_id, role, content, symbol) "
                "VALUES (?,?,?,?)",
                (conversation_id, "assistant", (answer or "")[:4000], symbol))
            conn.execute(
                "DELETE FROM jarvis_messages WHERE conversation_id=? AND id NOT IN "
                "(SELECT id FROM jarvis_messages WHERE conversation_id=? "
                " ORDER BY id DESC LIMIT ?)",
                (conversation_id, conversation_id, _JARVIS_TURN_LIMIT))
            conn.commit()
        except Exception:
            # Roll back so we never commit a lone user message (or a U/A pair
            # without the trim) — a partial write here is exactly what corrupts
            # the Q/A adjacency this function exists to protect.
            conn.rollback()
            raise


def jarvis_get_messages(conversation_id, limit=40):
    """Last `limit` messages, oldest→newest."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT role, content, symbol, created_at FROM jarvis_messages "
        "WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
        (conversation_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def jarvis_list_memories():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, fact, source, created_at FROM jarvis_memory ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def jarvis_add_memory(fact, source="user"):
    fact = (fact or "").strip()[:500]
    if not fact:
        return None
    with _write_lock:
        conn = get_conn()
        # Idempotent on duplicates, case-insensitive — "I prefer ETFs" and
        # "i prefer etfs" are the same fact and shouldn't both occupy a slot
        # in the 12-fact prompt window.
        row = conn.execute(
            "SELECT id FROM jarvis_memory WHERE lower(fact)=lower(?)", (fact,)).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO jarvis_memory (fact, source) VALUES (?,?)", (fact, source))
        conn.commit()
        return cur.lastrowid


def jarvis_log_insights(cards):
    """Persist briefing insight cards as the day's history. UNIQUE(day, kind,
    title) makes re-logging the same card a no-op, so this can run on every
    briefing cache-miss without duplicating. Returns the number of NEW rows.
    Prunes anything older than 14 days while it's here."""
    if not cards:
        return 0
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new = 0
    with _write_lock:
        conn = get_conn()
        for card in cards:
            title = (card.get("title") or "").strip()[:200]
            if not title:
                continue
            action = card.get("action") or {}
            # Tolerate a non-numeric priority (e.g. 'high' from an LLM card):
            # fall back to the default 3 rather than raising out of the whole
            # batch and losing every insight for this briefing.
            try:
                prio = int(card.get("priority") or 3)
            except (TypeError, ValueError):
                prio = 3
            cur = conn.execute(
                "INSERT OR IGNORE INTO jarvis_insights "
                "(day, kind, tone, priority, title, detail, view, symbol) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (day, str(card.get("kind") or "info")[:40],
                 str(card.get("tone") or "info")[:10],
                 prio, title,
                 (card.get("detail") or "")[:500],
                 (action.get("view") or None), (action.get("symbol") or None)))
            new += cur.rowcount
        conn.execute("DELETE FROM jarvis_insights WHERE created_at < datetime('now', '-14 days')")
        conn.commit()
    return new


def jarvis_insights_since(since_iso, limit=20):
    """Insights logged strictly after `since_iso` (UTC 'YYYY-MM-DD HH:MM:SS'
    or ISO — SQLite compares lexicographically, both work). None → empty."""
    if not since_iso:
        return []
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM jarvis_insights WHERE created_at > ? "
        "ORDER BY (priority IS NULL), priority ASC, id DESC LIMIT ?",
        (str(since_iso)[:32], max(1, min(int(limit), 50)))).fetchall()
    return [dict(r) for r in rows]


def jarvis_conversation_exists(conversation_id):
    # Only NON-archived threads are adoptable. After a NEW THREAD in one UI
    # surface archives the old thread, the other surface still holds the old
    # id; rejecting archived ids makes ask() fall back to the active thread so
    # both surfaces converge instead of forking (one writing to a dead thread).
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT 1 FROM jarvis_conversations WHERE id=? AND archived=0",
            (int(conversation_id),)
        ).fetchone()
        return row is not None
    except (OverflowError, ValueError, TypeError):
        return False


def jarvis_delete_memory(memory_id):
    try:
        memory_id = int(memory_id)
        if not (0 < memory_id < 2**62):  # SQLite INTEGER bound — see OverflowError
            return False
    except (ValueError, TypeError):
        return False
    with _write_lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM jarvis_memory WHERE id=?", (memory_id,))
        conn.commit()
        return cur.rowcount > 0


# ── Settings ───────────────────────────────────────────────────────────────────
#
# get_settings() sits on the per-request hot path: api_keys.get_api_key()
# resolves credentials through it for nearly every API route, and several
# views call it directly. Each call was a full-table SELECT on its own —
# cheap individually, but it's the single most frequent query in the app.
# A tiny in-process TTL cache (5s) collapses those reads to ~1 SQLite hit
# per 5 seconds per process. Writes (set_setting / init_db migrations)
# invalidate synchronously, so a set-then-get in the same process is always
# coherent. Cross-process writers (a CLI poking the DB while the server
# runs) are visible after at most _SETTINGS_CACHE_TTL — acceptable for
# settings data. The cache is keyed on DB_PATH so tests or tools that
# repoint the module at a different database never see stale rows.

_SETTINGS_CACHE_TTL = 5.0
_settings_cache = {"data": None, "ts": 0.0, "path": None}
_settings_cache_lock = threading.Lock()


def _invalidate_settings_cache():
    with _settings_cache_lock:
        _settings_cache["data"] = None
        _settings_cache["ts"] = 0.0
        _settings_cache["path"] = None


def get_settings():
    now = time.time()
    with _settings_cache_lock:
        cached = _settings_cache["data"]
        if (cached is not None
                and _settings_cache["path"] == DB_PATH
                and now - _settings_cache["ts"] < _SETTINGS_CACHE_TTL):
            # Shallow copy so callers mutating the dict (the /api/settings
            # masking pass, for instance) can't poison the shared snapshot.
            return dict(cached)
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    # connection reused (thread-local pool)
    # Hide internal metadata keys (anything starting with __) from callers —
    # the frontend settings form would otherwise round-trip them and a stray
    # JS handler could clobber the schema version on save.
    data = {r["key"]: r["value"] for r in rows if not r["key"].startswith("__")}
    with _settings_cache_lock:
        _settings_cache["data"] = dict(data)
        _settings_cache["ts"] = now
        _settings_cache["path"] = DB_PATH
    return data


def set_setting(key, value):
    with _write_lock:
        conn = get_conn()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        # connection reused (thread-local pool)
    # Invalidate AFTER the commit so no reader can re-prime the cache with
    # pre-write rows between the invalidation and the data landing on disk.
    _invalidate_settings_cache()


# ── AI Call Log (daily counter) ───────────────────────────────────────────────
#
# Backs the AUGUR_AI_DAILY_CAP enforcement in ai_summarizer. Persisted so the
# cap survives Flask reloads / desktop relaunches — a fresh process must not
# silently reset the budget.

def _today_str():
    # Keyed in UTC so the cap-counter date aligns with prune_ai_call_log's
    # date('now', ...) (SQLite evaluates that in UTC) and the rest of the
    # module's UTC dating; local time would roll/double-bucket at a different
    # instant for users west of UTC.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_ai_call_count(date=None):
    """Return today's AI call count (or count for explicit date)."""
    conn = get_conn()
    d = date or _today_str()
    row = conn.execute("SELECT count FROM ai_call_log WHERE date=?", (d,)).fetchone()
    return int(row["count"]) if row else 0


def increment_ai_call_count(date=None, by=1):
    """Atomically bump today's counter and return the new value."""
    with _write_lock:
        conn = get_conn()
        d = date or _today_str()
        # UPSERT keeps this race-safe under threaded callers.
        conn.execute(
            "INSERT INTO ai_call_log(date, count) VALUES(?, ?) "
            "ON CONFLICT(date) DO UPDATE SET count = count + ?",
            (d, by, by),
        )
        conn.commit()
        row = conn.execute("SELECT count FROM ai_call_log WHERE date=?", (d,)).fetchone()
        return int(row["count"]) if row else 0


# ── Portfolio Snapshots ────────────────────────────────────────────────────────

def save_snapshot(date, total_value, total_cost, total_pnl, total_pnl_pct, positions_json):
    """Persist a portfolio snapshot for `date`. Uses INSERT OR REPLACE so a
    later snapshot in the same calendar day OVERWRITES the earlier one
    instead of being silently ignored — the snapshot worker fires every
    5 min and we want the most recent end-of-day picture, not whatever
    happened to be the first quote-fetch success of the morning.

    Previously used INSERT OR IGNORE which froze the row at the first
    write of the day; if that first write happened while half the
    upstreams were rate-limited, the saved value undercount persisted
    until midnight."""
    # A NULL/empty date defeats the UNIQUE-conflict dedupe (SQLite allows
    # multiple NULLs in a UNIQUE column), so every such snapshot would insert
    # a new row and the once-per-day contract / prune logic would break.
    # Fall back to today's UTC date so REPLACE can dedupe.
    if not date:
        date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    with _write_lock:
        conn = get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO portfolio_snapshots
               (date, total_value, total_cost, total_pnl, total_pnl_pct, positions_json)
               VALUES (?,?,?,?,?,?)""",
            (date, total_value, total_cost, total_pnl, total_pnl_pct, positions_json)
        )
        conn.commit()


def get_snapshots():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM portfolio_snapshots WHERE date IS NOT NULL ORDER BY date ASC"
    ).fetchall()
    # connection reused (thread-local pool)
    return [dict(r) for r in rows]


# ── SEC Intelligence Cache ─────────────────────────────────────────────────────

def get_cached_filing(accession):
    """Return cached filing dict or None (valid within 24h)."""
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM sec_filings_cache
           WHERE accession = ?
           AND cached_at > datetime('now', '-24 hours')""",
        (accession,)
    ).fetchone()
    # connection reused (thread-local pool)
    if row:
        d = dict(row)
        if d.get("ai_key_points"):
            try:
                d["ai_key_points"] = json.loads(d["ai_key_points"])
            except Exception:
                d["ai_key_points"] = []
        return d
    return None


def cache_filing(accession, ticker, form_type, filing_date, description, filing_text, ai_result):
    """Cache a filing with its AI analysis."""
    # A failed AI step may pass None; coerce so the .get() calls below don't
    # raise AttributeError mid-write (which would roll back the cache insert).
    ai_result = ai_result or {}
    with _write_lock:
        conn = get_conn()
        key_points = ai_result.get("key_points", [])
        conn.execute(
            """INSERT OR REPLACE INTO sec_filings_cache
               (accession, ticker, form_type, filing_date, description, filing_text,
                ai_signal, ai_summary, ai_key_points, ai_event_type, ai_powered, cached_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (
                accession, ticker, form_type, filing_date, description,
                filing_text[:5000] if filing_text else "",
                ai_result.get("signal", "NEUTRAL"),
                ai_result.get("summary", ""),
                json.dumps(key_points),
                ai_result.get("event_type", ""),
                1 if ai_result.get("ai_powered") else 0,
            )
        )
        conn.commit()
        # connection reused (thread-local pool)


def get_cached_insiders(ticker, days=90):
    """Return insider transactions cached within N days."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM insider_transactions_cache
           WHERE ticker = ?
           AND cached_at > datetime('now', ?)
           ORDER BY date DESC""",
        (ticker.upper(), f"-{days} days")
    ).fetchall()
    # connection reused (thread-local pool)
    return [dict(r) for r in rows]


def cache_insider_transactions(ticker, transactions):
    """Bulk insert insider transactions, ignoring duplicates."""
    if not transactions:
        return
    with _write_lock:
        conn = get_conn()
        for t in transactions:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO insider_transactions_cache
                       (ticker, accession, insider_name, title, transaction_type, security,
                        shares, price, value, shares_after, date, form_url, cached_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                    (
                        (t.get("ticker") or ticker).upper(),
                        t.get("accession", ""),
                        t.get("insider_name", ""),
                        t.get("title", ""),
                        t.get("transaction_type", ""),
                        t.get("security", ""),
                        t.get("shares"),
                        t.get("price"),
                        t.get("value"),
                        t.get("shares_after"),
                        t.get("date", ""),
                        t.get("form_url", ""),
                    )
                )
            except Exception:
                pass
        conn.commit()
        # connection reused (thread-local pool)


def get_cached_institutional(fund_cik):
    """Return cached institutional holdings dict or None (valid within 7 days)."""
    conn = get_conn()
    row = conn.execute(
        """SELECT * FROM institutional_cache
           WHERE fund_cik = ?
           AND cached_at > datetime('now', '-7 days')
           ORDER BY filing_date DESC LIMIT 1""",
        (str(fund_cik),)
    ).fetchone()
    # connection reused (thread-local pool)
    if row:
        d = dict(row)
        if d.get("holdings_json"):
            try:
                d["holdings"] = json.loads(d["holdings_json"])
            except Exception:
                d["holdings"] = []
        d["total_value"] = d.get("total_value_usd", 0)
        # Drop the raw JSON blob now that it's parsed into d["holdings"] — it
        # only doubles the payload size when this dict is jsonified to the
        # frontend; no caller reads holdings_json.
        d.pop("holdings_json", None)
        return d
    return None


def cache_institutional(fund_name, fund_cik, data):
    """Save fund holdings data to cache."""
    with _write_lock:
        conn = get_conn()
        holdings = data.get("holdings", [])
        conn.execute(
            """INSERT OR REPLACE INTO institutional_cache
               (fund_name, fund_cik, filing_date, period, total_value_usd, holdings_json, cached_at)
               VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (
                fund_name,
                str(fund_cik),
                data.get("filing_date", ""),
                data.get("period_of_report", ""),
                data.get("total_value", data.get("total_value_usd", 0)),
                json.dumps(holdings),
            )
        )
        conn.commit()
        # connection reused (thread-local pool)


# ── Earnings Cache ─────────────────────────────────────────────────────────────

def get_cached_earnings_dossier(symbol, max_age_hours=6):
    """Return cached earnings dossier or None."""
    if not symbol:
        return None
    conn = get_conn()
    row = conn.execute(
        """SELECT dossier_json FROM earnings_cache
           WHERE symbol = ?
           AND cached_at > datetime('now', ?)""",
        (symbol.upper(), f"-{max_age_hours} hours")
    ).fetchone()
    # connection reused (thread-local pool)
    if row:
        try:
            return json.loads(row["dossier_json"])
        except Exception:
            return None
    return None


def cache_earnings_dossier(symbol, dossier):
    """Cache an earnings dossier dict."""
    if not symbol:
        return
    with _write_lock:
        conn = get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO earnings_cache (symbol, dossier_json, cached_at) VALUES (?,?,CURRENT_TIMESTAMP)",
            (symbol.upper(), json.dumps(dossier, default=str))
        )
        conn.commit()
        # connection reused (thread-local pool)


# ── Scanner History ────────────────────────────────────────────────────────────

def save_scan_history(opportunities, profile_hash, strategy, scanned_at_iso):
    """Persist a snapshot of scanner top results so we can chart score over time."""
    if not opportunities:
        return
    with _write_lock:
        conn = get_conn()
        rows = []
        for rank, opp in enumerate(opportunities, start=1):
            if not opp.get("symbol"):
                continue
            # Store NULL (not 0.0) for a missing/non-numeric composite so a
            # real zero stays distinct and AVG/MAX stats ignore the gap; also
            # keeps one bad row from raising and aborting the whole batch.
            raw = opp.get("composite")
            try:
                composite = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                composite = None
            rows.append(
                (
                    scanned_at_iso, profile_hash, strategy,
                    (opp.get("symbol") or "").upper(),
                    opp.get("asset_class"),
                    composite,
                    opp.get("badge"),
                    rank,
                )
            )
        if not rows:
            return
        conn.executemany(
            """INSERT INTO scanner_history
               (scanned_at, profile_hash, strategy, symbol, asset_class, composite_score, badge, rank_in_scan)
               VALUES (?,?,?,?,?,?,?,?)""",
            rows
        )
        conn.commit()


def get_scan_history(symbol=None, limit=100):
    """Return scanner history rows. If symbol given, returns that symbol's score timeline."""
    conn = get_conn()
    if symbol:
        rows = conn.execute(
            """SELECT scanned_at, composite_score, rank_in_scan, badge, strategy
               FROM scanner_history WHERE symbol = ?
               ORDER BY scanned_at DESC LIMIT ?""",
            (symbol.upper(), limit)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT symbol, scanned_at, composite_score, rank_in_scan, badge, strategy, asset_class
               FROM scanner_history
               ORDER BY scanned_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_scanner_watchlist(limit=20, days=30):
    """Return symbols that have appeared in recent scans, with score stats."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT symbol,
                  COUNT(*) AS appearances,
                  MAX(composite_score) AS max_score,
                  AVG(composite_score) AS avg_score,
                  MAX(scanned_at) AS last_seen,
                  MIN(rank_in_scan) AS best_rank
           FROM scanner_history
           WHERE scanned_at > datetime('now', ?)
           GROUP BY symbol
           ORDER BY appearances DESC, max_score DESC
           LIMIT ?""",
        (f"-{int(days)} days", limit)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Prune helpers ──────────────────────────────────────────────────────────────
#
# Every table below grows monotonically with usage. The warmer (cache_warmer.py)
# calls these on a daily cadence so an active user's wealth.db doesn't balloon
# past a gigabyte over a few months.

def prune_scanner_history(days=90):
    """Drop scanner_history rows older than `days`. Returns rows deleted."""
    with _write_lock:
        conn = get_conn()
        cur = conn.execute(
            "DELETE FROM scanner_history WHERE datetime(scanned_at) < datetime('now', ?)",
            (f"-{int(days)} days",)
        )
        conn.commit()
        return cur.rowcount or 0


def prune_portfolio_snapshots(keep_daily_days=365):
    """Downsample portfolio_snapshots: keep every daily row within the last
    `keep_daily_days` days, but for rows older than that keep only the LAST
    snapshot per calendar month. Returns rows deleted."""
    with _write_lock:
        conn = get_conn()
        # For each (year-month) older than the cutoff, keep only the row with
        # the maximum date. Delete everything else in that month.
        cur = conn.execute(
            """DELETE FROM portfolio_snapshots
               WHERE date < date('now', ?)
                 AND date NOT IN (
                     SELECT MAX(date) FROM portfolio_snapshots
                     WHERE date < date('now', ?) AND date IS NOT NULL
                     GROUP BY substr(date, 1, 7)
                 )""",
            (f"-{int(keep_daily_days)} days", f"-{int(keep_daily_days)} days")
        )
        conn.commit()
        return cur.rowcount or 0


def prune_insider_cache(days=90):
    """Drop insider_transactions_cache rows older than `days` by cached_at."""
    with _write_lock:
        conn = get_conn()
        cur = conn.execute(
            "DELETE FROM insider_transactions_cache WHERE cached_at < datetime('now', ?)",
            (f"-{int(days)} days",)
        )
        conn.commit()
        return cur.rowcount or 0


def prune_institutional_cache(days=30):
    """Drop institutional_cache rows older than `days` by cached_at.
    The lookup helper already filters to <=7d; older rows are dead weight."""
    with _write_lock:
        conn = get_conn()
        cur = conn.execute(
            "DELETE FROM institutional_cache WHERE cached_at < datetime('now', ?)",
            (f"-{int(days)} days",)
        )
        conn.commit()
        return cur.rowcount or 0


def prune_sec_filings_cache(days=30):
    """Drop sec_filings_cache rows older than `days` by cached_at.
    The lookup helper filters to <=24h; older AI-analyzed rows are dead weight."""
    with _write_lock:
        conn = get_conn()
        cur = conn.execute(
            "DELETE FROM sec_filings_cache WHERE cached_at < datetime('now', ?)",
            (f"-{int(days)} days",)
        )
        conn.commit()
        return cur.rowcount or 0


def prune_earnings_cache(hours=48):
    """Drop earnings_cache rows older than `hours` by cached_at.
    Lookup helper filters to <=6h; this is a safety net for stale rows."""
    with _write_lock:
        conn = get_conn()
        cur = conn.execute(
            "DELETE FROM earnings_cache WHERE cached_at < datetime('now', ?)",
            (f"-{int(hours)} hours",)
        )
        conn.commit()
        return cur.rowcount or 0


def prune_idea_pool(keep_n=500):
    """Keep only the most recently warmed `keep_n` idea_pool rows."""
    with _write_lock:
        conn = get_conn()
        # idea_pool may not exist yet on a brand-new DB; tolerate the miss.
        try:
            cur = conn.execute(
                """DELETE FROM idea_pool
                   WHERE rowid NOT IN (
                       SELECT rowid FROM idea_pool
                       ORDER BY warmed_at DESC LIMIT ?
                   )""",
                (int(keep_n),)
            )
            conn.commit()
            return cur.rowcount or 0
        except sqlite3.OperationalError:
            return 0


def prune_api_cache(grace_days=3):
    """Drop api_cache rows whose TTL expired more than `grace_days` ago.

    api_cache is cache_store's write-through table and is by far the
    biggest thing in wealth.db (measured: 110 MB of a 116 MB file, ~95%
    of it SEC EDGAR response bodies). cache_store only purges expired
    rows inside its init() — i.e. at process boot — so on a long-running
    server dead rows pile up until the next restart. This daily sweep
    keeps the table at its true working set between restarts.

    The grace window matters: cache_store.cache_get_stale() serves
    recently-expired entries as a fallback when an upstream is down
    (default max staleness 24h), and init() reloads disk rows into the
    in-memory tier on boot. Deleting only rows expired >3 days keeps
    both behaviors intact while still reclaiming everything genuinely
    dead. The table is created by cache_store, not init_db, so tolerate
    its absence on fresh databases.
    """
    with _write_lock:
        conn = get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM api_cache WHERE expiry < strftime('%s','now') - ?",
                (int(grace_days) * 86400,)
            )
            conn.commit()
            return cur.rowcount or 0
        except sqlite3.OperationalError:
            return 0


def prune_jarvis_conversations(days=90):
    """Drop ARCHIVED jarvis conversations older than `days` (by started_at),
    along with their messages. Active (archived=0) threads are never touched.

    The schema declares ON DELETE CASCADE on jarvis_messages.conversation_id
    and get_conn enables PRAGMA foreign_keys, but a wealth.db created by a
    build that predates the cascade clause keeps its original table DDL
    (CREATE TABLE IF NOT EXISTS never rewrites it) — so delete the messages
    explicitly first. On cascade-enabled DBs that's a cheap no-op overlap.
    Returns conversations deleted."""
    with _write_lock:
        conn = get_conn()
        cutoff = (f"-{int(days)} days",)
        try:
            conn.execute(
                "DELETE FROM jarvis_messages WHERE conversation_id IN ("
                "  SELECT id FROM jarvis_conversations"
                "  WHERE archived=1 AND datetime(started_at) < datetime('now', ?))",
                cutoff
            )
            cur = conn.execute(
                "DELETE FROM jarvis_conversations "
                "WHERE archived=1 AND datetime(started_at) < datetime('now', ?)",
                cutoff
            )
            conn.commit()
            return cur.rowcount or 0
        except Exception:
            # Roll back so we don't orphan messages whose parent conversation
            # delete failed (on a pre-cascade DB those rows would never be
            # reclaimed otherwise).
            conn.rollback()
            raise


def prune_ai_call_log(days=90):
    """Drop ai_call_log rows older than `days`."""
    with _write_lock:
        conn = get_conn()
        cur = conn.execute(
            "DELETE FROM ai_call_log WHERE date < date('now', ?)",
            (f"-{int(days)} days",)
        )
        conn.commit()
        return cur.rowcount or 0


def vacuum_db():
    """Reclaim disk pages freed by DELETEs. Returns the new file size in bytes
    (or -1 if VACUUM was skipped / the DB path can't be stat'd).

    Deliberately does NOT hold _write_lock. The old version did, which froze
    every app-wide write for the (potentially many-second) VACUUM; worse, the
    warmer's 45s soft timeout would "abandon" the call and move on while the
    orphaned thread still held the lock, leaving writes wedged. SQLite
    serializes VACUUM against other writers internally, and the dedicated
    connection's busy_timeout absorbs contention — so the lock buys us nothing
    but the freeze. We use a fresh short-lived connection (not the thread-local
    one) so VACUUM runs outside any implicit transaction and we can close it
    immediately afterward."""
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")  # wait out a transient writer
        conn.isolation_level = None  # autocommit: VACUUM can't run in a txn
        conn.execute("VACUUM")
    except sqlite3.OperationalError as e:
        # Some SQLite builds reject VACUUM under WAL mode if a reader/writer is
        # still active — that's a benign skip. But this branch also catches
        # genuinely serious failures (malformed image, disk I/O error, disk
        # full); surface those at WARNING so a corrupt/full DB isn't silently
        # swallowed as a routine skip. Still return -1 to preserve the
        # skip-by-return-value contract callers rely on.
        msg = str(e).lower()
        if not ("cannot vacuum" in msg or "locked" in msg or "database is locked" in msg):
            import logging as _logging
            _logging.getLogger("augur.db").warning("VACUUM failed: %s", e)
        return -1
    except Exception:
        return -1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    try:
        return os.path.getsize(DB_PATH)
    except OSError:
        return -1


def run_daily_prune():
    """Run all prune helpers in one go. Returns a dict of rows deleted by table."""
    out = {}
    try:
        out["scanner_history"] = prune_scanner_history(90)
    except Exception:
        out["scanner_history"] = -1
    try:
        out["portfolio_snapshots"] = prune_portfolio_snapshots(365)
    except Exception:
        out["portfolio_snapshots"] = -1
    try:
        out["insider_transactions_cache"] = prune_insider_cache(90)
    except Exception:
        out["insider_transactions_cache"] = -1
    try:
        out["institutional_cache"] = prune_institutional_cache(30)
    except Exception:
        out["institutional_cache"] = -1
    try:
        out["sec_filings_cache"] = prune_sec_filings_cache(30)
    except Exception:
        out["sec_filings_cache"] = -1
    try:
        out["earnings_cache"] = prune_earnings_cache(48)
    except Exception:
        out["earnings_cache"] = -1
    try:
        out["idea_pool"] = prune_idea_pool(500)
    except Exception:
        out["idea_pool"] = -1
    try:
        out["ai_call_log"] = prune_ai_call_log(90)
    except Exception:
        out["ai_call_log"] = -1
    try:
        out["api_cache"] = prune_api_cache(3)
    except Exception:
        out["api_cache"] = -1
    try:
        out["jarvis_conversations"] = prune_jarvis_conversations(90)
    except Exception:
        out["jarvis_conversations"] = -1
    return out
