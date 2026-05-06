import sqlite3
import json
import threading
from datetime import datetime, timezone

DB_PATH = "wealth.db"

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
        _local.conn = conn
    return conn


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

    # ── Indexes for performance ──
    c.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_symbol ON portfolio(symbol)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_transactions_symbol ON transactions(symbol)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON price_alerts(symbol)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_triggered ON price_alerts(triggered)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_filings_cache_ticker ON sec_filings_cache(ticker)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_insider_cache_ticker ON insider_transactions_cache(ticker)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_insider_cache_ticker_date ON insider_transactions_cache(ticker, cached_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_institutional_cache_cik ON institutional_cache(fund_cik)")

    # ── Migrations: add account_id columns to existing tables ──
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

    conn.commit()
    # connection reused (thread-local pool)


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
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO accounts (name, account_type, institution, notes, color) VALUES (?,?,?,?,?)",
        (name, account_type, institution, notes, color)
    )
    conn.commit()
    return cur.lastrowid


def update_account(account_id, name=None, account_type=None, institution=None, notes=None, color=None):
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
    conn = get_conn()
    # Nullify portfolio/transaction references first
    conn.execute("UPDATE portfolio SET account_id = NULL WHERE account_id = ?", (account_id,))
    conn.execute("UPDATE transactions SET account_id = NULL WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()


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
            total_cost = (existing["shares"] * existing["avg_cost"]) + (shares * avg_cost)
            new_avg = total_cost / total_shares
            conn.execute(
                "UPDATE portfolio SET shares = ?, avg_cost = ?, name = ? WHERE id = ?",
                (total_shares, new_avg, name or existing["name"], existing["id"])
            )
            row_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO portfolio (symbol, name, shares, avg_cost, asset_type, sector, currency, notes, account_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (symbol.upper(), name, shares, avg_cost, asset_type, sector, currency, notes, account_id)
            )
            row_id = cur.lastrowid
        conn.commit()
    return row_id


def update_position(pos_id, shares=None, avg_cost=None, notes=None, account_id=None):
    conn = get_conn()
    pos = conn.execute("SELECT * FROM portfolio WHERE id = ?", (pos_id,)).fetchone()
    if not pos:
        return False
    new_shares = shares if shares is not None else pos["shares"]
    new_cost = avg_cost if avg_cost is not None else pos["avg_cost"]
    new_notes = notes if notes is not None else pos["notes"]
    new_acct = account_id if account_id is not None else pos["account_id"]
    conn.execute(
        "UPDATE portfolio SET shares = ?, avg_cost = ?, notes = ?, account_id = ? WHERE id = ?",
        (new_shares, new_cost, new_notes, new_acct, pos_id)
    )
    conn.commit()
    return True


def delete_position(pos_id):
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
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO watchlist (symbol, name, asset_type, alert_high, alert_low, notes) VALUES (?,?,?,?,?,?)",
            (symbol.upper(), name, asset_type, alert_high, alert_low, notes)
        )
        conn.commit()
        result = True
    except sqlite3.IntegrityError:
        # Update alerts if already exists
        conn.execute(
            "UPDATE watchlist SET alert_high = ?, alert_low = ?, notes = ? WHERE symbol = ?",
            (alert_high, alert_low, notes, symbol.upper())
        )
        conn.commit()
        result = False
    # connection reused (thread-local pool)
    return result


def delete_from_watchlist(wl_id):
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
    rows = conn.execute(base + where + " ORDER BY t.date DESC, t.created_at DESC LIMIT ?", params).fetchall()
    return [dict(r) for r in rows]


def add_transaction(symbol, action, shares, price, fees=0, date=None, notes="", account_id=None):
    conn = get_conn()
    total = shares * price
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO transactions (symbol, action, shares, price, total, fees, date, notes, account_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (symbol.upper(), action.upper(), shares, price, total, fees, date, notes, account_id)
    )
    conn.commit()


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
    conn = get_conn()
    conn.execute("DELETE FROM price_alerts WHERE id=?", (alert_id,))
    conn.commit()
    # connection reused (thread-local pool)


def mark_alert_triggered(alert_id):
    conn = get_conn()
    conn.execute(
        "UPDATE price_alerts SET triggered=1, triggered_at=CURRENT_TIMESTAMP WHERE id=?",
        (alert_id,)
    )
    conn.commit()
    # connection reused (thread-local pool)


def clear_triggered_alerts():
    conn = get_conn()
    conn.execute("DELETE FROM price_alerts WHERE triggered=1")
    conn.commit()
    # connection reused (thread-local pool)


# ── Settings ───────────────────────────────────────────────────────────────────

def get_settings():
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    # connection reused (thread-local pool)
    return {r["key"]: r["value"] for r in rows}


def set_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    # connection reused (thread-local pool)


# ── Portfolio Snapshots ────────────────────────────────────────────────────────

def save_snapshot(date, total_value, total_cost, total_pnl, total_pnl_pct, positions_json):
    with _write_lock:
        conn = get_conn()
        conn.execute(
            """INSERT OR IGNORE INTO portfolio_snapshots
               (date, total_value, total_cost, total_pnl, total_pnl_pct, positions_json)
               VALUES (?,?,?,?,?,?)""",
            (date, total_value, total_cost, total_pnl, total_pnl_pct, positions_json)
        )
        conn.commit()


def get_snapshots():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM portfolio_snapshots ORDER BY date ASC"
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
           AND datetime(cached_at) > datetime('now', '-24 hours')""",
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
           AND datetime(cached_at) > datetime('now', ?)
           ORDER BY date DESC""",
        (ticker.upper(), f"-{days} days")
    ).fetchall()
    # connection reused (thread-local pool)
    return [dict(r) for r in rows]


def cache_insider_transactions(ticker, transactions):
    """Bulk insert insider transactions, ignoring duplicates."""
    if not transactions:
        return
    conn = get_conn()
    for t in transactions:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO insider_transactions_cache
                   (ticker, accession, insider_name, title, transaction_type, security,
                    shares, price, value, shares_after, date, form_url, cached_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    t.get("ticker", ticker).upper(),
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
           AND datetime(cached_at) > datetime('now', '-7 days')
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
        return d
    return None


def cache_institutional(fund_name, fund_cik, data):
    """Save fund holdings data to cache."""
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
            data.get("total_value", 0),
            json.dumps(holdings),
        )
    )
    conn.commit()
    # connection reused (thread-local pool)


# ── Earnings Cache ─────────────────────────────────────────────────────────────

def get_cached_earnings_dossier(symbol, max_age_hours=6):
    """Return cached earnings dossier or None."""
    conn = get_conn()
    row = conn.execute(
        """SELECT dossier_json FROM earnings_cache
           WHERE symbol = ?
           AND datetime(cached_at) > datetime('now', ?)""",
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
        rows = [
            (
                scanned_at_iso, profile_hash, strategy,
                (opp.get("symbol") or "").upper(),
                opp.get("asset_class"),
                float(opp.get("composite") or 0),
                opp.get("badge"),
                rank,
            )
            for rank, opp in enumerate(opportunities, start=1)
            if opp.get("symbol")
        ]
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
           WHERE datetime(scanned_at) > datetime('now', ?)
           GROUP BY symbol
           ORDER BY appearances DESC, max_score DESC
           LIMIT ?""",
        (f"-{int(days)} days", limit)
    ).fetchall()
    return [dict(r) for r in rows]
