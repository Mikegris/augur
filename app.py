"""
AUGUR — Wealth Intelligence System
Local personal stock & wealth tracker.
"""

# Silence known-benign warnings BEFORE third-party imports trigger them.
# Filters must be installed by message-pattern (not category) here because
# importing the warning class itself would already trigger the noisy import.
import warnings as _warnings
import os as _early_os

# urllib3 v2 + macOS LibreSSL: one-time NotOpenSSLWarning at urllib3 import.
_warnings.filterwarnings("ignore", message=r".*OpenSSL 1\.1\.1\+.*")
# joblib/loky worker pool teardown fires this every batch — sklearn n_jobs=-1
# starts new pools constantly during the warm pass, producing log spam.
_warnings.filterwarnings(
    "ignore",
    message=r"resource_tracker:.*process died unexpectedly.*",
)
# sklearn matmul instability — RandomForest/LinearRegression on sparse or
# extreme-value feature matrices throws overflow/divide-by-zero warnings.
# We've clamped what we can (see ml_forecast.py); silence the rest.
_warnings.filterwarnings(
    "ignore",
    category=RuntimeWarning,
    module=r"sklearn\..*",
)

# yfinance logs per-ticker "possibly delisted" + "Invalid Crumb" directly to
# its logger; we already log meaningful failures ourselves at WARNING.
import logging as _early_logging
_early_logging.getLogger("yfinance").setLevel(_early_logging.CRITICAL)

from flask import Flask, jsonify, request, render_template, abort, Response, make_response
import database as db
import fetcher
import json
import os
import io
import csv
import logging
import re
import math
import threading
import time
import itertools
from collections import deque
from datetime import datetime, timezone

import safe_executor
import sec_edgar as edgar
import ai_summarizer
import earnings as earnings_module
import data_sources as ds
import alt_signals
import idea_generator
try:
    import sec_filings_v2
except Exception:
    sec_filings_v2 = None
try:
    import calendar_v2
except Exception:
    calendar_v2 = None
try:
    import crypto_exchanges
except Exception:
    crypto_exchanges = None

log = logging.getLogger("augur")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Single source of truth for the app version — surfaced at /api/version and
# in jarvis.health_snapshot().
APP_VERSION = "3.20.0"

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")

def _safe_int(val, default):
    """Parse an int from query-string input; return default on bad input."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

def _valid_ticker(symbol):
    return isinstance(symbol, str) and bool(_TICKER_RE.match(symbol.strip().upper()))

# Crypto pricing keys on `asset_type == "crypto"` throughout the app, so a typo
# like "Crypto"/"CRYPTO" stored verbatim would make a coin be priced as an
# equity (no -USD pair) and never get a live quote. Normalize to a known set.
_KNOWN_ASSET_TYPES = ("stock", "crypto", "etf", "option")

def _normalize_asset_type(value, default="stock"):
    at = (value or default)
    at = at.strip().lower() if isinstance(at, str) else default
    return at if at in _KNOWN_ASSET_TYPES else default

def _normalize_txn_date(raw):
    """Normalize a client-supplied transaction date to ISO YYYY-MM-DD.

    db.get_transactions orders by the stored date STRING, so a non-ISO value
    (e.g. "07/10/2026") would sort among ISO "2026-…" rows completely wrong,
    permanently. Returns (iso_or_None, ok): None/"" -> (None, True) so the DB
    default applies; unparseable -> (None, False) so callers can 400.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, True
    if not isinstance(raw, str):
        return None, False
    s = raw.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d"), True
        except ValueError:
            pass
    return None, False

def _err(e, msg="internal error", status=500):
    """Log an engine/DB/upstream exception server-side and return a generic
    JSON error so raw str(e) (SQL, file paths, upstream URLs, stack detail)
    never leaks to the client. Use for unexpected exceptions only — keep
    validation 400s with their specific user-facing messages."""
    log.exception(e)
    return jsonify({"error": msg}), status

def _utc_now():
    return datetime.now(timezone.utc)

def _market_date():
    """Calendar date in the US market zone (America/New_York). The daily
    portfolio snapshot is bucketed once per local trading day — keying it by
    UTC would file an afternoon-ET snapshot under the next calendar day (UTC
    rolls over ~19:00-20:00 ET), distorting the history/benchmark series.
    Falls back to UTC if the tz database is unavailable."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return _utc_now().strftime("%Y-%m-%d")

def _portfolio_live_prices(holdings):
    """Live quotes for a holdings list in ONE batched fetch.

    Stocks and crypto used to be fetched as two sequential
    fetcher.get_quotes_batch calls per request (two full network waves —
    the second blocked on the first). Combining them into a single batch
    lets fetcher's internal thread pool resolve everything in one wave.

    Returns the same dict the old per-route code built: UPPER-cased stock
    symbols from get_quotes_batch, plus each crypto holding's original
    symbol mapped from its -USD Yahoo pair when a quote came back.
    """
    stock_syms = [h["symbol"] for h in holdings
                  if h.get("asset_type") != "crypto" and isinstance(h.get("symbol"), str)]
    crypto_syms = [h["symbol"] for h in holdings
                   if h.get("asset_type") == "crypto" and isinstance(h.get("symbol"), str)]
    query = stock_syms + [s + "-USD" for s in crypto_syms]
    if not query:
        return {}
    batch = fetcher.get_quotes_batch(query)
    prices = dict(batch)
    for sym in crypto_syms:
        # get_quotes_batch returns UPPER-cased keys; match accordingly so a
        # lowercase-stored crypto symbol doesn't silently miss its live price.
        key = (sym + "-USD").upper()
        if key in batch:
            prices[sym] = batch[key]
    return prices

# When bundled by py2app, app.py lives inside a .zip and Flask's default
# template/static lookup (relative to __file__) breaks. py2app exports
# RESOURCEPATH pointing at Contents/Resources/ where data_files land, so
# prefer that when available; otherwise fall back to the source layout.
_BASE = os.environ.get("RESOURCEPATH") or os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(_BASE, "templates"),
    static_folder=os.path.join(_BASE, "static"),
)
app.config["JSON_SORT_KEYS"] = False

# Gzip compression — reduces JSON response sizes by 60-80%
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass

db.init_db()


# Release this request thread's thread-local SQLite connection when the request
# context tears down. Flask's threaded server can spawn a fresh thread per
# request; without this each such thread opened a connection on first DB access
# and never closed it, leaking file descriptors until GC. close_thread_conn()
# commits-then-closes and is a no-op when no connection was opened.
@app.teardown_appcontext
def _release_db_conn(_exc=None):
    try:
        db.close_thread_conn()
    except Exception:
        pass


# AJTA trading-agent schema (additive aj_* tables + forward-only migration).
# Safe + idempotent; never alters existing AUGUR tables.
try:
    import aj_db as _aj_db
    _aj_db.aj_migrate()
except Exception as _aj_err:
    log.warning("aj_migrate failed (trading-agent tables): %s", _aj_err)

# Hydrate the persistent API cache from disk so the first navigation after
# launch reads from the cache instead of hammering Yahoo/Finviz cold. The
# warmer thread is then responsible for keeping the cache fresh in the
# background — see cache_warmer.py for the cadence policy.
try:
    import cache_store
    cache_store.init()
except Exception as _cache_err:
    log.warning("cache_store init failed: %s", _cache_err)

# Guard against Flask reloader double-start: when running `python app.py`
# with debug=True, Werkzeug spawns a reloader child. Both parent and child
# import this module, and `cache_warmer.start()` would run in BOTH processes
# — they'd hammer SQLite and upstream APIs in parallel. Detect the reloader
# parent (running as __main__ with WERKZEUG_RUN_MAIN unset) and skip there.
# When imported by desktop.py / py2app, __name__ != "__main__" so we still
# start normally.
_IS_RELOADER_PARENT = (
    __name__ == "__main__"
    and os.environ.get("WERKZEUG_RUN_MAIN") != "true"
)
if not _IS_RELOADER_PARENT:
    try:
        import cache_warmer
        cache_warmer.start()
    except Exception as _warmer_err:
        log.warning("cache_warmer start failed: %s", _warmer_err)
    # AJTA auto-run scheduler: the daemon timer that actually ticks the agent's
    # cycles on auto_run_interval_min. Self-gates on auto_run_enabled (default
    # OFF) + market session, so it is a cheap no-op until the operator enables
    # auto-run. Without this, the "Auto-run every N minutes" toggle is inert.
    try:
        import aj_autonomy
        aj_autonomy.start_scheduler()
    except Exception as _sched_err:
        log.warning("aj scheduler start failed: %s", _sched_err)
else:
    log.info("Skipping cache_warmer start in reloader parent process")


# ─── Portfolio Snapshot Background Thread ─────────────────────────────────────

# Jarvis watch evaluation rides the snapshot loop's tick but is gated by this
# module-level timestamp so the cadence stays ≥5 minutes even if the loop's
# sleep is ever shortened.
_JARVIS_WATCH_EVAL_LAST = 0.0
_JARVIS_WATCH_EVAL_INTERVAL = 300  # seconds
# The cadence gate below is a check-then-set on a module global. The snapshot
# worker is the only caller today, but guard it so a second caller (or a future
# reload) can't both pass the interval check and double-run the evaluation.
_JARVIS_WATCH_EVAL_LOCK = threading.Lock()
# In-progress flag so the cadence gate can prevent a concurrent double-run
# WITHOUT advancing the success timestamp before the work completes.
_JARVIS_WATCH_EVAL_RUNNING = False


def _evaluate_jarvis_watches():
    """Run jarvis_watches.evaluate_all() and log an insight card per newly
    triggered watch so the briefing/digest surface it. Fully guarded — the
    module may not exist yet, and nothing here may kill the worker loop."""
    global _JARVIS_WATCH_EVAL_LAST, _JARVIS_WATCH_EVAL_RUNNING
    now = time.time()
    with _JARVIS_WATCH_EVAL_LOCK:
        # Skip if a run is already in flight (double-run guard) or the cadence
        # interval hasn't elapsed since the last SUCCESSFUL evaluation.
        if _JARVIS_WATCH_EVAL_RUNNING:
            return
        if now - _JARVIS_WATCH_EVAL_LAST < _JARVIS_WATCH_EVAL_INTERVAL:
            return
        _JARVIS_WATCH_EVAL_RUNNING = True
    try:
        try:
            import jarvis_watches
        except Exception:
            return  # module in flight — quietly skip until it lands
        try:
            triggered = jarvis_watches.evaluate_all() or []
            cards = []
            for w in triggered:
                if not isinstance(w, dict):
                    continue
                cards.append({
                    "kind": "watch",
                    "tone": "warn",
                    "priority": 1,
                    "title": "Watch triggered: {}".format(w.get("name") or "unnamed"),
                    "detail": w.get("description") or "",
                    "action": {"view": "alerts"},
                })
            if cards:
                db.jarvis_log_insights(cards)
            # Only advance the cadence timestamp after a successful run, so a
            # transient failure doesn't silently skip the next tick.
            with _JARVIS_WATCH_EVAL_LOCK:
                _JARVIS_WATCH_EVAL_LAST = time.time()
        except Exception:
            log.exception("snapshot worker: jarvis watch evaluation failed")
    finally:
        with _JARVIS_WATCH_EVAL_LOCK:
            _JARVIS_WATCH_EVAL_RUNNING = False


def _snapshot_worker():
    """Takes a portfolio snapshot every 5 minutes but only writes once per day."""
    while True:
        # Bind prices unconditionally — the alert block below reads it, and a
        # watchlist-only user (no holdings) would otherwise hit NameError every
        # cycle and never get an alert evaluated.
        prices = {}
        try:
            holdings = db.get_portfolio()
            if holdings:
                prices = _portfolio_live_prices(holdings)

                total_value = 0
                total_cost = 0
                positions = []
                for h in holdings:
                    sym = h["symbol"]
                    q = prices.get(sym, {})
                    current_price = q.get("price")
                    cost_basis = h["avg_cost"] * h["shares"]
                    total_cost += cost_basis
                    if current_price:
                        mv = current_price * h["shares"]
                        total_value += mv
                        positions.append({"symbol": sym, "market_value": round(mv, 2)})
                    else:
                        total_value += cost_basis
                        positions.append({"symbol": sym, "market_value": round(cost_basis, 2)})

                total_pnl = total_value - total_cost
                total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0
                today = _market_date()
                db.save_snapshot(
                    date=today,
                    total_value=round(total_value, 2),
                    total_cost=round(total_cost, 2),
                    total_pnl=round(total_pnl, 2),
                    total_pnl_pct=round(total_pnl_pct, 2),
                    positions_json=json.dumps(positions),
                )
        except Exception:
            log.exception("snapshot worker: portfolio snapshot failed")

        # ── Check price alerts (reuse already-fetched prices) ──────────────
        try:
            active_alerts = db.get_price_alerts(include_triggered=False)
            if active_alerts:
                # Crypto alert symbols must be fetched as their -USD Yahoo pair
                # (a bare 'BTC' resolves to an equity and never returns a price),
                # mirroring _portfolio_live_prices. Infer crypto-ness from the
                # holdings/watchlist asset_type since price_alerts has none.
                try:
                    crypto_set = {
                        h["symbol"].upper()
                        for h in locals().get("holdings", []) or []
                        if h.get("asset_type") == "crypto"
                    }
                except Exception:
                    crypto_set = set()
                try:
                    for w in db.get_watchlist():
                        if w.get("asset_type") == "crypto":
                            crypto_set.add(str(w["symbol"]).upper())
                except Exception:
                    pass
                # Normalize to upper: prices/get_quotes_batch key on UPPER, so a
                # legacy lower/mixed-case alert symbol would never match and the
                # alert would silently never fire (defense-in-depth — inserts
                # already uppercase).
                alert_syms = list({str(a["symbol"]).upper() for a in active_alerts})
                missing = [s for s in alert_syms if s not in prices]
                if missing:
                    # Build the fetch query, mapping crypto symbols to -USD, and
                    # remember the mapping so we can key the result back to the
                    # bare alert symbol.
                    query = []
                    crypto_query = {}  # bare symbol -> -USD pair (upper)
                    for s in missing:
                        if s.upper() in crypto_set:
                            pair = (s + "-USD").upper()
                            crypto_query[s] = pair
                            query.append(pair)
                        else:
                            query.append(s)
                    batch = fetcher.get_quotes_batch(query)
                    prices.update(batch)
                    for bare, pair in crypto_query.items():
                        if pair in batch:
                            prices[bare] = batch[pair]
                for alert in active_alerts:
                    cur = (prices.get(str(alert["symbol"]).upper()) or {}).get("price")
                    if cur is None:
                        continue
                    ap = alert["price"]
                    if ap is None:
                        continue
                    hit = (alert["alert_type"] == "above" and cur >= alert["price"]) or \
                          (alert["alert_type"] == "below" and cur <= alert["price"])
                    if hit:
                        db.mark_alert_triggered(alert["id"])
        except Exception:
            log.exception("snapshot worker: alert check failed")

        # ── Evaluate Jarvis watches (≥5-min cadence, timestamp-gated) ──────
        _evaluate_jarvis_watches()

        time.sleep(300)  # 5 minutes


# Guard against Flask reloader double-start — reuse the same reloader-parent
# detection cache_warmer uses so we start the snapshot worker in exactly one
# process (app.debug is False at import time, so checking it always started in
# BOTH the reloader parent and child). A module-level once-flag (under a lock)
# additionally makes this block idempotent, so an importlib.reload or repeated
# execution can't accumulate parallel snapshot threads each re-fetching quotes
# and racing on the per-day INSERT OR REPLACE.
_snapshot_started = False
_snapshot_start_lock = threading.Lock()


def _start_snapshot_worker_once():
    global _snapshot_started
    if _IS_RELOADER_PARENT:
        return
    with _snapshot_start_lock:
        if _snapshot_started:
            return
        _snapshot_started = True
        _t = threading.Thread(target=_snapshot_worker, daemon=True)
        _t.start()


_start_snapshot_worker_once()

# ─── Serve UI ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # Render the version into the page (status bar) so it can never go stale,
    # and send no-store: the desktop WKWebView persists an on-disk cache keyed
    # by bundle id, so without this an old index.html survived app updates —
    # showing a stale version label AND loading old JS (no new features). The
    # versioned ?v= asset URLs it references still handle their own busting.
    resp = make_response(render_template("index.html", app_version=APP_VERSION))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


# ─── Market Data ──────────────────────────────────────────────────────────────

@app.route("/api/quote/<symbol>")
def quote(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    return jsonify(fetcher.get_quote(symbol.upper()))


@app.route("/api/quotes")
def quotes_batch():
    symbols_param = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in symbols_param.split(",") if s.strip()]
    symbols = [s for s in symbols if _valid_ticker(s)][:50]
    if not symbols:
        return jsonify({"error": "No valid symbols provided"}), 400
    return jsonify(fetcher.get_quotes_batch(symbols))


@app.route("/api/fundamentals/<symbol>")
def fundamentals(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    return jsonify(fetcher.get_fundamentals(symbol.upper()))


@app.route("/api/chart/<symbol>")
def chart(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    period = request.args.get("period", "6mo")
    interval = request.args.get("interval", "1d")
    data = fetcher.get_chart_data(symbol.upper(), period=period, interval=interval)
    indicators = fetcher.compute_indicators(data) if data else {}
    return jsonify({"symbol": symbol.upper(), "data": data, "indicators": indicators})


@app.route("/api/news/<symbol>")
def news(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    limit = min(max(_safe_int(request.args.get("limit"), 15), 1), 100)
    return jsonify(fetcher.get_news(symbol.upper(), limit=limit))


@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify([])
    return jsonify(fetcher.search_symbol(query))


@app.route("/api/market/indices")
def market_indices():
    return jsonify(fetcher.get_market_indices())


@app.route("/api/market/sectors")
def market_sectors():
    return jsonify(fetcher.get_sector_performance())


@app.route("/api/market/movers")
def market_movers():
    """Top gainers/losers within a curated ~36-name large-cap universe.

    Not a market-wide screen — see fetcher.get_top_movers for the list.
    """
    return jsonify(fetcher.get_top_movers())


# ─── Crypto ───────────────────────────────────────────────────────────────────

@app.route("/api/crypto/market")
def crypto_market():
    limit = min(max(_safe_int(request.args.get("limit"), 50), 1), 100)
    return jsonify(fetcher.get_crypto_market(limit))


@app.route("/api/crypto/global")
def crypto_global():
    return jsonify(fetcher.get_crypto_global())


@app.route("/api/crypto/chart/<coin_id>")
def crypto_chart(coin_id):
    days = min(max(_safe_int(request.args.get("days"), 30), 1), 365)
    return jsonify(fetcher.get_crypto_chart(coin_id, days=days))


@app.route("/api/crypto/quote/<coin_id>")
def crypto_quote(coin_id):
    return jsonify(fetcher.get_crypto_quote(coin_id))


# ─── Accounts ─────────────────────────────────────────────────────────────────

@app.route("/api/accounts")
def list_accounts():
    accounts = db.get_accounts()
    return jsonify(accounts)


@app.route("/api/accounts", methods=["POST"])
def create_account():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not data.get("name"):
        return jsonify({"error": "Account name is required"}), 400
    row_id = db.add_account(
        name=data["name"],
        account_type=data.get("account_type", "brokerage"),
        institution=data.get("institution", ""),
        notes=data.get("notes", ""),
        color=data.get("color", ""),
    )
    return jsonify({"id": row_id, "status": "created"})


@app.route("/api/accounts/<int:account_id>", methods=["PUT"])
def update_account(account_id):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400
    ok = db.update_account(
        account_id,
        name=data.get("name"),
        account_type=data.get("account_type"),
        institution=data.get("institution"),
        notes=data.get("notes"),
        color=data.get("color"),
    )
    return jsonify({"status": "updated" if ok else "not_found"})


@app.route("/api/accounts/<int:account_id>", methods=["DELETE"])
def delete_account(account_id):
    db.delete_account(account_id)
    return jsonify({"status": "deleted"})


# ─── Portfolio ────────────────────────────────────────────────────────────────

@app.route("/api/portfolio")
def get_portfolio():
    acct_filter = request.args.get("account_id")
    acct_id = None
    if acct_filter:
        acct_id = _safe_int(acct_filter, None)
        if acct_id is None:
            # A malformed id must not silently drop the filter and return the
            # ENTIRE portfolio as if it were that account's — 400 like the PUT.
            return jsonify({"error": "account_id must be an integer"}), 400
    holdings = db.get_portfolio(account_id=acct_id)
    if not holdings:
        return jsonify({"holdings": [], "summary": {}})

    # Enrich with live prices (stocks + crypto -USD pairs in one wave)
    prices = _portfolio_live_prices(holdings)

    enriched = []
    total_value = 0
    total_cost = 0

    for h in holdings:
        sym = h["symbol"]
        q = prices.get(sym, {})
        current_price = q.get("price")
        # Fall back to avg_cost when the live quote is missing (None) so a
        # transiently-missing quote can't make the position vanish from the
        # summary totals — mirrors stress_test / dividends / ai_analysis. A
        # real 0.0 is a valid price and counted as such.
        cur = current_price if current_price is not None else h["avg_cost"]
        market_value = cur * h["shares"]
        cost_basis = h["avg_cost"] * h["shares"]
        unrealized_pnl = market_value - cost_basis
        unrealized_pct = (unrealized_pnl / cost_basis * 100) if cost_basis else 0
        total_value += market_value
        total_cost += cost_basis
        day_chg = q.get("change")
        h.update({
            "current_price": round(cur, 4),
            "market_value": round(market_value, 2),
            "cost_basis": round(cost_basis, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_pct": round(unrealized_pct, 2),
            "day_change": day_chg if current_price is not None else None,
            # Gate the percent on the same condition as the absolute change
            # so the UI never shows a percent move beside a None day P&L.
            "day_change_pct": (q.get("change_pct") if day_chg is not None else None) if current_price is not None else None,
            "day_pnl": round(day_chg * h["shares"], 2) if (current_price is not None and day_chg is not None) else None,
        })
        enriched.append(h)

    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0

    return jsonify({
        "holdings": enriched,
        "summary": {
            "total_value": round(total_value, 2),
            "total_cost": round(total_cost, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "num_positions": len(enriched),
        },
    })


@app.route("/api/portfolio/add", methods=["POST"])
def add_position():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400
    required = ["symbol", "shares", "avg_cost"]
    if not all(k in data for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    if not _valid_ticker(data["symbol"]):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        shares = float(data["shares"])
        avg_cost = float(data["avg_cost"])
        fees = float(data.get("fees", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "shares, avg_cost, fees must be numeric"}), 400
    if not (math.isfinite(shares) and math.isfinite(avg_cost) and math.isfinite(fees)):
        return jsonify({"error": "shares, avg_cost, fees must be finite"}), 400
    if shares <= 0:
        return jsonify({"error": "shares must be > 0"}), 400
    if avg_cost < 0 or fees < 0:
        return jsonify({"error": "avg_cost and fees must be >= 0"}), 400
    txn_date, date_ok = _normalize_txn_date(data.get("date"))
    if not date_ok:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    acct_id = _safe_int(data.get("account_id"), None) if data.get("account_id") else None
    asset_type = _normalize_asset_type(data.get("asset_type"))
    row_id = db.add_position(
        symbol=data["symbol"],
        name=data.get("name", ""),
        shares=shares,
        avg_cost=avg_cost,
        asset_type=asset_type,
        sector=data.get("sector", ""),
        currency=data.get("currency", "USD"),
        notes=data.get("notes", ""),
        account_id=acct_id,
    )
    # Also log as transaction
    db.add_transaction(
        symbol=data["symbol"],
        action="BUY",
        shares=shares,
        price=avg_cost,
        fees=fees,
        date=txn_date,
        notes=data.get("notes", ""),
        account_id=acct_id,
    )
    return jsonify({"id": row_id, "status": "added"})


@app.route("/api/portfolio/<int:pos_id>", methods=["PUT"])
def update_position(pos_id):
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400
    acct_id = data.get("account_id")
    if acct_id == "" or acct_id is None:
        acct_id = None
    else:
        try:
            acct_id = int(acct_id)
        except (TypeError, ValueError):
            return jsonify({"error": "account_id must be an integer"}), 400
    # Coerce numerics (mirror add_position) — a non-numeric shares/avg_cost was
    # written to the DB verbatim and later crashed avg_cost*shares arithmetic.
    shares = data.get("shares")
    avg_cost = data.get("avg_cost")
    try:
        if shares is not None:
            shares = float(shares)
        if avg_cost is not None:
            avg_cost = float(avg_cost)
    except (TypeError, ValueError):
        return jsonify({"error": "shares and avg_cost must be numeric"}), 400
    if shares is not None and (not math.isfinite(shares) or shares <= 0):
        return jsonify({"error": "shares must be a finite number > 0"}), 400
    if avg_cost is not None and (not math.isfinite(avg_cost) or avg_cost < 0):
        return jsonify({"error": "avg_cost must be a finite number >= 0"}), 400
    # Only forward account_id when the client actually sent the key, so an
    # omitted account_id keeps the existing assignment while an explicit null/""
    # clears it (un-assigns the position from its account). Passing it
    # unconditionally would wipe the assignment on every PUT that omits it.
    kwargs = dict(shares=shares, avg_cost=avg_cost, notes=data.get("notes"))
    if "account_id" in data:
        kwargs["account_id"] = acct_id
    ok = db.update_position(pos_id, **kwargs)
    return jsonify({"status": "updated" if ok else "not_found"})


@app.route("/api/portfolio/<int:pos_id>", methods=["DELETE"])
def delete_position(pos_id):
    db.delete_position(pos_id)
    return jsonify({"status": "deleted"})


# ─── Watchlist ────────────────────────────────────────────────────────────────

@app.route("/api/watchlist")
def get_watchlist():
    items = db.get_watchlist()
    if not items:
        return jsonify([])
    # Crypto watchlist symbols must be fetched as their -USD Yahoo pair — a
    # bare 'BTC' resolves to an EQUITY (Grayscale's ETF), showing the wrong
    # instrument's price. Mirrors _portfolio_live_prices / snapshot worker.
    query = []
    pair_of = {}  # bare symbol -> -USD pair (UPPER, matching batch keys)
    for i in items:
        sym = i["symbol"]
        if i.get("asset_type") == "crypto" and isinstance(sym, str):
            pair = (sym + "-USD").upper()
            pair_of[sym] = pair
            query.append(pair)
        else:
            query.append(sym)
    prices = fetcher.get_quotes_batch(query)
    for item in items:
        key = pair_of.get(item["symbol"], item["symbol"])
        q = prices.get(key, {})
        item.update({
            "price": q.get("price"),
            "change": q.get("change"),
            "change_pct": q.get("change_pct"),
            "day_high": q.get("day_high"),
            "day_low": q.get("day_low"),
        })
    return jsonify(items)


@app.route("/api/watchlist/add", methods=["POST"])
def add_watchlist():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or "symbol" not in data:
        return jsonify({"error": "symbol required"}), 400
    if not _valid_ticker(data["symbol"]):
        return jsonify({"error": "Invalid symbol"}), 400
    # Coerce alert thresholds to float-or-None — a string would be stored and
    # then break the numeric alert comparison downstream.
    def _opt_float(v):
        return None if v in (None, "") else float(v)
    try:
        alert_high = _opt_float(data.get("alert_high"))
        alert_low = _opt_float(data.get("alert_low"))
    except (TypeError, ValueError):
        return jsonify({"error": "alert_high/alert_low must be numeric"}), 400
    # float("nan")/float("inf") parse cleanly; a stored NaN later serializes as
    # the invalid-JSON literal NaN and breaks the whole watchlist render.
    for v in (alert_high, alert_low):
        if v is not None and not math.isfinite(v):
            return jsonify({"error": "alert_high/alert_low must be finite"}), 400
    is_new = db.add_to_watchlist(
        symbol=data["symbol"],
        name=data.get("name", ""),
        asset_type=_normalize_asset_type(data.get("asset_type")),
        alert_high=alert_high,
        alert_low=alert_low,
        notes=data.get("notes", ""),
    )
    return jsonify({"status": "added" if is_new else "updated"})


@app.route("/api/watchlist/<int:wl_id>", methods=["DELETE"])
def delete_watchlist(wl_id):
    db.delete_from_watchlist(wl_id)
    return jsonify({"status": "deleted"})


# ─── Transactions ─────────────────────────────────────────────────────────────

@app.route("/api/transactions")
def get_transactions():
    symbol = request.args.get("symbol")
    # Clamp to a positive minimum — a negative limit becomes SQL `LIMIT -1`,
    # which SQLite reads as "no limit" (all rows), the opposite of intent.
    limit = max(1, _safe_int(request.args.get("limit"), 100))
    return jsonify(db.get_transactions(symbol=symbol, limit=limit))


@app.route("/api/transactions/summary")
def transactions_summary():
    """Aggregate KPIs over ALL transactions (no row-limit). Used for the
    Transactions view's headline strip so totals stay accurate when the
    table itself only pages in the latest N rows."""
    # Aggregate in SQL (SUM/COUNT GROUP BY action) instead of loading every
    # row into Python just to sum it — keeps the headline strip O(1) on the
    # DB side regardless of table size.
    summary = db.get_transaction_summary()
    return jsonify({
        "total_buy": summary["total_buy"],
        "total_sell": summary["total_sell"],
        "count": summary["count"],
    })


@app.route("/api/transactions/add", methods=["POST"])
def add_transaction():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400
    required = ["symbol", "action", "shares", "price"]
    if not all(k in data for k in required):
        return jsonify({"error": "Missing required fields"}), 400
    if not _valid_ticker(data["symbol"]):
        return jsonify({"error": "Invalid symbol"}), 400
    # action must be a string — db.add_transaction calls action.upper(), which
    # raises AttributeError (→ 500) on a numeric/JSON-object action.
    if not isinstance(data["action"], str):
        return jsonify({"error": "action must be a string"}), 400
    try:
        shares = float(data["shares"])
        price = float(data["price"])
        fees = float(data.get("fees", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "shares, price, fees must be numeric"}), 400
    if not (math.isfinite(shares) and math.isfinite(price) and math.isfinite(fees)):
        return jsonify({"error": "shares, price, fees must be finite"}), 400
    # Sign guards (mirror add_position): a negative shares/price is recorded
    # verbatim and silently corrupts the buy/sell totals in
    # /api/transactions/summary. Sells are modeled via the action field.
    if shares <= 0:
        return jsonify({"error": "shares must be > 0"}), 400
    if price < 0 or fees < 0:
        return jsonify({"error": "price and fees must be >= 0"}), 400
    txn_date, date_ok = _normalize_txn_date(data.get("date"))
    if not date_ok:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    db.add_transaction(
        symbol=data["symbol"],
        action=data["action"],
        shares=shares,
        price=price,
        fees=fees,
        date=txn_date,
        notes=data.get("notes", ""),
    )
    return jsonify({"status": "recorded"})


# ─── Portfolio History & Benchmark ───────────────────────────────────────────

@app.route("/api/portfolio/history")
def portfolio_history():
    snapshots = db.get_snapshots()
    return jsonify(snapshots)


@app.route("/api/portfolio/benchmark")
def portfolio_benchmark():
    symbol = (request.args.get("symbol") or "SPY").upper().strip()
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    period = request.args.get("period", "1y")
    # Allowlist the period — an unrecognized value reaches yfinance and comes
    # back as a silent empty series (looks like "no data" to the UI). Reject
    # with a 400 so the caller knows the input was bad.
    _VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y",
                      "10y", "ytd", "max"}
    if period not in _VALID_PERIODS:
        return jsonify({"error": "Invalid period",
                        "valid_periods": sorted(_VALID_PERIODS)}), 400
    # Get first snapshot value to normalize benchmark to same starting value.
    # Use the earliest snapshot with a POSITIVE total_value — normalizing to a
    # zero/near-zero base (portfolio was empty when first snapshotted) distorts
    # the benchmark series wildly.
    snapshots = db.get_snapshots()
    base_value = next((s["total_value"] for s in snapshots if s.get("total_value")), None)
    data = fetcher.get_benchmark_history(symbol=symbol, period=period, base_value=base_value)
    return jsonify({"symbol": symbol, "data": data})


# ─── Options Chain ─────────────────────────────────────────────────────────────

@app.route("/api/options/<symbol>/dates")
def options_dates(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    dates = fetcher.get_options_dates(symbol.upper())
    return jsonify(dates)


@app.route("/api/options/<symbol>/chain")
def options_chain(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    date = request.args.get("date")
    chain = fetcher.get_option_chain(symbol.upper(), date=date)
    return jsonify(chain)


# ─── Analytics: Correlation & Risk ───────────────────────────────────────────

@app.route("/api/analytics/correlation")
def analytics_correlation():
    period = request.args.get("period", "3mo")
    holdings = db.get_portfolio()
    symbols = list(set(h["symbol"] for h in holdings if h["asset_type"] != "crypto"))
    if not symbols:
        return jsonify({"symbols": [], "matrix": {}})
    result = fetcher.get_correlation_matrix(symbols, period=period)
    return jsonify(result)


@app.route("/api/analytics/risk")
def analytics_risk():
    period = request.args.get("period", "1y")
    holdings = db.get_portfolio()
    symbols = list(set(h["symbol"] for h in holdings if h["asset_type"] != "crypto"))
    if not symbols:
        return jsonify({})
    result = fetcher.get_risk_metrics(symbols, period=period)
    return jsonify(result)


@app.route("/api/insights/symbol/<symbol>")
def insights_symbol(symbol):
    """v2.5 technical/positioning signal bundle for one symbol."""
    try:
        import portfolio_insights
        return jsonify(portfolio_insights.symbol_signal(symbol))
    except Exception as e:
        return _err(e)


@app.route("/api/insights/portfolio")
def insights_portfolio():
    """v2.5 technical breadth/health across equity holdings."""
    try:
        import portfolio_insights
        return jsonify(portfolio_insights.portfolio_health())
    except Exception as e:
        return _err(e)


# ─── Portfolio CSV Export / Import ────────────────────────────────────────────

@app.route("/api/portfolio/export")
def portfolio_export():
    holdings = db.get_portfolio()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["symbol", "name", "shares", "avg_cost", "asset_type", "sector", "currency", "notes", "account_name", "account_type"])
    for h in holdings:
        writer.writerow([
            h["symbol"], h.get("name", ""), h["shares"], h["avg_cost"],
            h.get("asset_type", "stock"), h.get("sector", ""),
            h.get("currency", "USD"), h.get("notes", ""),
            h.get("account_name", ""), h.get("acct_type", ""),
        ])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=portfolio_export.csv"},
    )


@app.route("/api/transactions/export")
def transactions_export():
    txns = db.get_transactions(limit=10000)

    # Stream rows out via a generator instead of materializing the whole CSV
    # in memory before responding — keeps peak memory flat regardless of how
    # many rows are exported.
    def _generate():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["date", "symbol", "action", "shares", "price", "total", "fees", "notes"])
        yield buf.getvalue()
        for t in txns:
            buf.seek(0)
            buf.truncate(0)
            writer.writerow([
                t.get("date", ""), t.get("symbol", ""), t.get("action", ""), t.get("shares", ""),
                t.get("price", ""), t.get("total", ""), t.get("fees", 0), t.get("notes", ""),
            ])
            yield buf.getvalue()

    return Response(
        _generate(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=transactions_export.csv"},
    )


@app.route("/api/portfolio/import", methods=["POST"])
def portfolio_import():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    try:
        content = f.read().decode("utf-8-sig")  # Handle BOM
    except (UnicodeDecodeError, AttributeError):
        return jsonify({"error": "File must be UTF-8 encoded CSV"}), 400
    reader = csv.DictReader(io.StringIO(content))

    imported = 0
    skipped = 0
    errors = []

    for i, row in enumerate(reader):
        try:
            # Normalize keys. Skip DictReader's restkey (None) — a row with
            # more cells than headers would otherwise crash on None.strip()
            # and the row would land in errors instead of importing.
            norm = {k.strip().strip('"').lower(): v for k, v in row.items()
                    if isinstance(k, str)}

            # Detect broker format and extract fields
            symbol = None
            shares = None
            avg_cost = None
            name = ""

            # Generic (our format) / Robinhood / Fidelity / Schwab
            symbol = (
                norm.get("symbol") or
                norm.get("\"symbol\"")
            )
            if symbol:
                symbol = symbol.strip().strip('"').upper()

            # Shares / Quantity
            shares_raw = (
                norm.get("shares") or
                norm.get("quantity") or
                norm.get("\"quantity\"")
            )
            if shares_raw:
                try:
                    shares = float(str(shares_raw).replace(",", ""))
                except ValueError:
                    pass

            # Avg cost
            cost_raw = (
                norm.get("avg_cost") or
                norm.get("average cost") or
                norm.get("average cost basis") or
                norm.get("\"average cost\"") or
                norm.get("cost basis") or
                norm.get("price")
            )
            if cost_raw:
                try:
                    avg_cost = float(str(cost_raw).replace("$", "").replace(",", ""))
                except ValueError:
                    pass

            name = norm.get("name") or norm.get("description") or norm.get("\"description\"") or ""
            name = str(name).strip('"').strip() if name else ""

            if not symbol or not _valid_ticker(symbol):
                skipped += 1
                continue
            # Reject non-finite values — float("inf")/float("nan") parse cleanly
            # and slip past the <=0 guard (NaN comparisons are always False), so
            # an "inf"/"nan" cell would otherwise be written to the DB and later
            # poison avg_cost*shares arithmetic.
            if shares is not None and not math.isfinite(shares):
                skipped += 1
                continue
            if avg_cost is not None and not math.isfinite(avg_cost):
                skipped += 1
                continue
            if shares is None or shares <= 0:
                skipped += 1
                continue
            if avg_cost is None or avg_cost <= 0:
                avg_cost = 0.0  # Allow import with zero cost

            # Round-trip our own exporter's columns: asset_type especially —
            # hardcoding "stock" re-imported crypto as an equity, so BTC was
            # then quoted as the NYSE ticker 'BTC' (Grayscale's ETF).
            db.add_position(
                symbol=symbol,
                name=name,
                shares=shares,
                avg_cost=avg_cost,
                asset_type=_normalize_asset_type(norm.get("asset_type")),
                sector=str(norm.get("sector") or "").strip(),
                currency=str(norm.get("currency") or "").strip() or "USD",
                notes=str(norm.get("notes") or "").strip(),
            )
            imported += 1
        except Exception as e:
            errors.append(f"Row {i+2}: {str(e)}")

    return jsonify({"imported": imported, "skipped": skipped, "errors": errors})


# ─── Analytics ────────────────────────────────────────────────────────────────

@app.route("/api/analytics/portfolio")
def portfolio_analytics():
    """Performance vs benchmark, allocation breakdown, etc."""
    holdings = db.get_portfolio()
    if not holdings:
        return jsonify({})

    # Live prices for market-value-weighted allocation (cost basis is the
    # fallback when a quote is missing).
    prices = _portfolio_live_prices(holdings)

    alloc_type = {}
    alloc_sector = {}
    alloc_position = {}

    for h in holdings:
        cur = (prices.get(h["symbol"]) or {}).get("price")
        mv = (cur if cur is not None else h["avg_cost"]) * h["shares"]
        alloc_type[h["asset_type"]] = alloc_type.get(h["asset_type"], 0) + mv
        sector = (h.get("sector") or "").strip() or ("Crypto" if h["asset_type"] == "crypto" else "Unclassified")
        alloc_sector[sector] = alloc_sector.get(sector, 0) + mv
        alloc_position[h["symbol"]] = alloc_position.get(h["symbol"], 0) + mv

    total = sum(alloc_type.values())

    def to_pct(d):
        return {k: round(v / total * 100, 2) for k, v in d.items()} if total else {}

    return jsonify({
        "allocation_by_type": to_pct(alloc_type),
        "allocation_by_type_values": {k: round(v, 2) for k, v in alloc_type.items()},
        "allocation_by_sector": to_pct(alloc_sector),
        "allocation_by_sector_values": {k: round(v, 2) for k, v in alloc_sector.items()},
        "allocation_by_position": to_pct(alloc_position),
        "total_value": round(total, 2),
    })


# ─── Settings ─────────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def get_settings():
    # Key material never leaves the server — sensitive values are masked
    # to •••• + last 4. (This route previously leaked the full OpenAI key.)
    import api_keys
    return jsonify(api_keys.mask_settings(db.get_settings()))


@app.route("/api/settings", methods=["POST"])
def update_settings():
    import api_keys
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400
    # Two-phase: validate EVERY key first and only commit once all pass, so a
    # later validation failure can't leave settings half-applied (the route
    # would otherwise have already committed earlier db.set_setting writes).
    plan = []  # list of ("setting", k, v) or ("provider", provider, key)
    for k, v in data.items():
        # The UI echoes masked placeholders back on save — writing one
        # through would clobber the real key with "••••XaQA".
        if k in api_keys.SENSITIVE_SETTINGS and api_keys.is_masked(v):
            continue
        # Sensitive keys must pass the provider's format gate, so a typo
        # ("garbage") can't flip _llm_available() true and fail at call time.
        if k in api_keys.SENSITIVE_SETTINGS and v:
            provider = next((p for p, s in api_keys.PROVIDERS.items()
                             if s["setting"] == k), None)
            if provider:
                key = v if isinstance(v, str) else str(v)
                # Mirror api_keys.save_key's format validation WITHOUT writing,
                # so we can fail the whole request before committing anything.
                spec = api_keys.PROVIDERS.get(provider) or {}
                fmt = spec.get("format")
                if api_keys.is_masked(key):
                    return jsonify({"error": "that's the masked placeholder, not a key"}), 400
                if fmt and not re.fullmatch(fmt, key.strip()):
                    return jsonify({"error": "key doesn't look like a {} key ({})".format(
                        spec.get("label", provider), spec.get("hint", ""))}), 400
                plan.append(("provider", provider, key))
                continue
        # Non-string values for non-sensitive keys: coerce primitives, reject
        # nested structures (str(dict) used to store "{'x': 1}").
        if v is not None and not isinstance(v, (str, int, float, bool)):
            return jsonify({"error": "invalid value for '{}'".format(k)}), 400
        plan.append(("setting", k, v))
    # Commit phase — all validations passed.
    for kind, a, b in plan:
        if kind == "provider":
            res = api_keys.save_key(a, b)
            if res.get("error"):
                return jsonify({"error": res["error"]}), 400
        else:
            db.set_setting(a, b)
    return jsonify({"status": "saved"})


# ─── API key console ──────────────────────────────────────────────────────────

@app.route("/api/keys", methods=["GET"])
def list_api_keys():
    import api_keys
    return jsonify({"providers": api_keys.list_providers()})


@app.route("/api/keys/<provider>", methods=["POST"])
def save_api_key(provider):
    import api_keys
    data = request.get_json(silent=True) or {}
    r = api_keys.save_key(provider, data.get("key", ""))
    return (jsonify(r), 400) if r.get("error") else jsonify(r)


@app.route("/api/keys/<provider>", methods=["DELETE"])
def delete_api_key(provider):
    import api_keys
    r = api_keys.delete_key(provider)
    return (jsonify(r), 400) if r.get("error") else jsonify(r)


@app.route("/api/keys/<provider>/test", methods=["POST"])
def test_api_key(provider):
    import api_keys
    return jsonify(api_keys.test_provider(provider))


# ─── Price Alerts ─────────────────────────────────────────────────────────────

@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    include_triggered = request.args.get("include_triggered", "false").lower() == "true"
    alerts = db.get_price_alerts(include_triggered=include_triggered)
    # Enrich with current prices
    symbols = list({a["symbol"] for a in alerts
                    if isinstance(a.get("symbol"), str) and a["symbol"]})
    # Crypto alert symbols must be fetched as their -USD Yahoo pair (a bare
    # 'BTC' resolves to an equity) — same inference the alert-trigger worker
    # uses, or distance_pct is computed against the wrong instrument.
    # Advisory enrichment: fail open to the unmapped fetch on any DB hiccup.
    crypto_set = set()
    try:
        for h in db.get_portfolio() or []:
            if h.get("asset_type") == "crypto":
                crypto_set.add(str(h["symbol"]).upper())
        for w in db.get_watchlist() or []:
            if w.get("asset_type") == "crypto":
                crypto_set.add(str(w["symbol"]).upper())
    except Exception:
        crypto_set = set()
    query = []
    pair_of = {}  # bare alert symbol -> -USD pair (UPPER, matches batch keys)
    for s in symbols:
        if s.upper() in crypto_set:
            pair_of[s] = (s + "-USD").upper()
            query.append(pair_of[s])
        else:
            query.append(s)
    prices = fetcher.get_quotes_batch(query) if query else {}
    for a in alerts:
        sym = a.get("symbol")
        q = prices.get(pair_of.get(sym, sym), {})
        cur = q.get("price")
        a["current_price"] = cur
        if cur is not None and cur != 0 and a["price"] is not None:
            a["distance_pct"] = round((a["price"] - cur) / cur * 100, 2)
        else:
            a["distance_pct"] = None
    return jsonify(alerts)


@app.route("/api/alerts", methods=["POST"])
def add_alert():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400
    sym_raw   = data.get("symbol", "")
    symbol    = sym_raw.strip().upper() if isinstance(sym_raw, str) else ""
    alert_type = data.get("alert_type", "above")   # "above" or "below"
    try:
        price = float(data.get("price", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid price"}), 400
    # price must be a positive FINITE number — `not price` rejected a legitimate
    # 0 and let negatives through, and NaN slips past `price <= 0` (all NaN
    # comparisons are False) creating an alert that can never trigger.
    if not symbol or not math.isfinite(price) or price <= 0 or alert_type not in ("above", "below"):
        return jsonify({"error": "symbol, valid alert_type, and price > 0 required"}), 400
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    row_id = db.add_price_alert(symbol, alert_type, price)
    return jsonify({"id": row_id, "status": "created"})


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
def delete_alert(alert_id):
    db.delete_price_alert(alert_id)
    return jsonify({"status": "deleted"})


@app.route("/api/alerts/clear-triggered", methods=["POST"])
def clear_triggered():
    db.clear_triggered_alerts()
    return jsonify({"status": "cleared"})


@app.route("/api/alerts/triggered")
def get_triggered_alerts():
    """Polled by frontend to show notifications for newly triggered alerts."""
    conn_alerts = db.get_price_alerts(include_triggered=True)
    triggered = [a for a in conn_alerts if a.get("triggered")]
    return jsonify(triggered)


# ─── Dividends ────────────────────────────────────────────────────────────────

@app.route("/api/dividends/portfolio")
def portfolio_dividends():
    """Dividend data for all portfolio positions."""
    holdings = db.get_portfolio()
    if not holdings:
        return jsonify({"positions": [], "summary": {}})

    # Live prices (for yield-on-cost) + per-symbol yfinance dividend
    # roundtrips, all in ONE parallel wave. The quotes batch is independent
    # of the dividend fetches, so it rides the same pool (slot 0) instead of
    # running as its own serial phase before them. Without parallelism,
    # 30 positions = 30 sequential network calls. yfinance 1.2.x is
    # thread-safe with fast_info.
    # Cap the fan-out: one dividend fetch per holding is unbounded, so a very
    # large portfolio could spawn hundreds of network calls. 60 covers any
    # realistic personal portfolio; holdings beyond it just render with their
    # avg_cost fallback (no dividend roundtrip).
    equity_syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"][:60]
    prices = {}
    div_map = {}
    if equity_syms:
        # Unique private sentinel for the quotes-batch slot — using None would
        # collide with a corrupt holding whose symbol is literally None,
        # mis-running the batch in that worker and corrupting div_map alignment.
        _QUOTES = object()

        def _div_task(item):
            if item is _QUOTES:  # sentinel slot: the live-quotes batch
                return fetcher.get_quotes_batch(equity_syms)
            return fetcher.get_dividend_data(item)

        # safe_executor.parallel_map preserves input order (like pool.map
        # did) and fills a slot with None if a fetch raises, which the
        # `or {}` below absorbs instead of 500-ing the route.
        wave = safe_executor.parallel_map(
            _div_task, [_QUOTES] + equity_syms,
            max_workers=9, thread_name_prefix="div-batch")
        prices = wave[0] or {}
        for sym, dd in zip(equity_syms, wave[1:]):
            div_map[sym] = dd or {}

    # Walk holdings in original order so the response ordering is stable.
    results = []
    total_annual_income = 0
    total_portfolio_value = 0

    for h in holdings:
        if h["asset_type"] == "crypto":
            continue
        div_data = div_map.get(h["symbol"], {})
        _px = prices.get(h["symbol"], {}).get("price")
        cur_price = _px if _px is not None else h["avg_cost"]  # real 0 isn't missing
        market_value = cur_price * h["shares"]
        total_portfolio_value += market_value

        annual_income = (div_data.get("div_rate") or 0) * h["shares"]
        total_annual_income += annual_income

        # Yield on cost = annual div rate / avg cost paid
        yoc = None
        if div_data.get("div_rate") and h["avg_cost"]:
            yoc = round(div_data["div_rate"] / h["avg_cost"] * 100, 3)

        results.append({
            **div_data,
            # Guarantee the keys the UI attributes rows by, even when the
            # per-symbol dividend fetch failed (div_data == {}).
            "symbol": h["symbol"],
            "name": div_data.get("name") or h.get("name") or h["symbol"],
            "div_rate": div_data.get("div_rate"),
            "div_yield": div_data.get("div_yield"),
            "shares": h["shares"],
            "avg_cost": h["avg_cost"],
            "market_value": round(market_value, 2),
            "annual_income": round(annual_income, 2),
            "yield_on_cost": yoc,
            "income_weight": 0,   # filled below
        })

    # Fill income weights
    for r in results:
        r["income_weight"] = round(r["annual_income"] / total_annual_income * 100, 1) if total_annual_income > 0 else 0

    results.sort(key=lambda x: x["annual_income"], reverse=True)

    # Monthly income estimate (annual / 12)
    monthly_income = total_annual_income / 12

    return jsonify({
        "positions": results,
        "summary": {
            "total_annual_income": round(total_annual_income, 2),
            "monthly_income": round(monthly_income, 2),
            "portfolio_yield": round(total_annual_income / total_portfolio_value * 100, 3) if total_portfolio_value else 0,
            "dividend_positions": len([r for r in results if r.get("div_rate")]),
            "total_positions": len(results),
        },
    })


# ─── Macro Conditions Dashboard ───────────────────────────────────────────────

@app.route("/api/macro")
def macro_dashboard():
    data = fetcher.get_macro_indicators()
    return jsonify(data)


# ─── Portfolio Stress Test ────────────────────────────────────────────────────

@app.route("/api/stress-test", methods=["POST"])
def stress_test():
    # get_json(silent=True) degrades to None (then {}) on a missing/wrong
    # Content-Type or malformed body; request.json would raise 415/400 instead.
    body = request.get_json(silent=True) or {}
    custom_drop = body.get("custom_drop_pct")   # e.g. -30.0
    # Coerce + finiteness-guard (mirror add_alert): a string "-30" (HTML number
    # inputs serialize as strings) crashes the fetcher's format-string with an
    # unhandled 500, and NaN poisons every position's new_value into invalid JSON.
    if custom_drop is not None:
        try:
            custom_drop = float(custom_drop)
        except (TypeError, ValueError):
            return jsonify({"error": "custom_drop_pct must be numeric"}), 400
        if not math.isfinite(custom_drop):
            return jsonify({"error": "custom_drop_pct must be finite"}), 400

    holdings = db.get_portfolio()
    if not holdings:
        return jsonify({"error": "No positions in portfolio"}), 400

    # Enrich with live market values (stocks + crypto in one batched wave)
    prices = _portfolio_live_prices(holdings)

    enriched = []
    for h in holdings:
        _cp = prices.get(h["symbol"], {}).get("price")
        cur = _cp if _cp is not None else h["avg_cost"]  # a real 0 price isn't "missing"
        h["market_value"] = round(cur * h["shares"], 2)
        h["current_price"] = cur
        enriched.append(h)

    result = fetcher.get_portfolio_stress_test(enriched, custom_drop_pct=custom_drop)
    return jsonify(result)


# ─── Earnings Catalyst Intelligence ──────────────────────────────────────────

@app.route("/api/earnings/calendar")
def earnings_calendar():
    """
    Upcoming earnings for all portfolio + watchlist symbols.
    Returns sorted list of symbols with next earnings date and quick stats.
    """
    holdings = db.get_portfolio()
    watchlist = db.get_watchlist()
    symbols = list({h["symbol"] for h in holdings} | {w["symbol"] for w in watchlist})
    # Filter to stocks only (skip crypto, ETFs without earnings). Crypto can
    # come from EITHER source — a watchlist-only BTC would otherwise reach the
    # earnings fetcher as the unrelated equity ticker BTC.
    crypto_syms = (
        {h["symbol"] for h in holdings if h.get("asset_type") == "crypto"}
        | {w["symbol"] for w in watchlist if w.get("asset_type") == "crypto"}
    )
    stock_symbols = [s for s in symbols if s not in crypto_syms]

    calendar = earnings_module.get_earnings_calendar(stock_symbols)
    return jsonify(calendar)


@app.route("/api/earnings/dossier/<symbol>")
def earnings_dossier(symbol):
    """
    Full pre-earnings dossier for one symbol with AI brief.
    Cached for 6 hours per symbol.
    """
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    symbol = symbol.upper()
    refresh = request.args.get("refresh", "false").lower() == "true"
    ai_model = request.args.get("model", "gpt-4o")

    # Check cache
    if not refresh:
        cached = db.get_cached_earnings_dossier(symbol)
        if cached and cached.get("brief"):
            return jsonify(cached)

    # Build dossier
    try:
        dossier = earnings_module.get_earnings_dossier(symbol)
    except Exception as e:
        log.exception(e)
        return jsonify({"error": "internal error", "symbol": symbol}), 500

    # Generate AI brief
    brief = ai_summarizer.generate_earnings_brief(dossier, model=ai_model)
    dossier["brief"] = brief

    # Cache only a genuinely successful brief — a failed/error envelope (no key,
    # rate-limited, daily cap) would otherwise be cached for 6h and keep serving
    # the bad brief even after the key is fixed.
    if brief and not (isinstance(brief, dict) and brief.get("error")):
        db.cache_earnings_dossier(symbol, dossier)

    return jsonify(dossier)


# ─── Portfolio AI Analysis ────────────────────────────────────────────────────

@app.route("/api/portfolio/ai-analysis", methods=["POST"])
def portfolio_ai_analysis():
    """
    Run GPT-4o analysis across the entire portfolio.
    Accepts optional { model: "gpt-4o" } in POST body to let UI choose model.
    """
    body = request.get_json(silent=True) or {}
    model = body.get("model", "gpt-4o")

    holdings = db.get_portfolio()
    if not holdings:
        return jsonify({"error": "No positions in portfolio"}), 400

    # Enrich with live prices (same logic as get_portfolio — one batched wave)
    prices = _portfolio_live_prices(holdings)

    total_value = 0
    total_cost = 0
    enriched = []
    for h in holdings:
        sym = h["symbol"]
        q = prices.get(sym, {})
        price = q.get("price")
        cost_basis = h["avg_cost"] * h["shares"]
        # Fall back to cost basis only when there is no live price at all;
        # a real 0.0 quote is a valid price (mirror stress_test/dividends).
        cur = price if price is not None else h["avg_cost"]
        market_value = cur * h["shares"]
        unrealized_pnl = market_value - cost_basis
        unrealized_pct = (unrealized_pnl / cost_basis * 100) if cost_basis else 0
        total_value += market_value
        total_cost += cost_basis
        enriched.append({
            "symbol": sym,
            "name": h.get("name", sym),
            "asset_type": h["asset_type"],
            "shares": h["shares"],
            "avg_cost": h["avg_cost"],
            "current_price": round(price, 4) if price is not None else None,
            "market_value": round(market_value, 2),
            "cost_basis": round(cost_basis, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_pct": round(unrealized_pct, 2),
            # Gate the percent on the absolute change being present, matching
            # get_portfolio, so the model/UI never sees a percent with no move.
            "day_change_pct": q.get("change_pct") if q.get("change") is not None else None,
            "weight_pct": 0,  # filled below
        })

    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0
    for pos in enriched:
        pos["weight_pct"] = round(pos["market_value"] / total_value * 100, 2) if total_value else 0

    summary = {
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "num_positions": len(enriched),
    }

    result = ai_summarizer.analyze_portfolio(enriched, summary, model=model)
    return jsonify(result)


# ─── SEC Intelligence Hub ─────────────────────────────────────────────────────

@app.route("/api/intel/feed")
def intel_feed():
    """Filing feed for all portfolio + watchlist symbols.

    By default returns raw filing metadata (no AI summarization). Pass
    `ai_powered=1` (or `true`) to run summaries on uncached filings —
    those are parallelized with a thread pool because cache_store.coalesce
    (used by ai_summarizer) is thread-safe.
    """
    refresh = request.args.get("refresh", "false").lower() == "true"
    ai_flag = (request.args.get("ai_powered") or "").lower()
    ai_powered = ai_flag in ("1", "true", "yes")

    holdings = db.get_portfolio()
    watchlist = db.get_watchlist()

    portfolio_syms = list(set(
        h["symbol"] for h in holdings if h.get("asset_type") not in ("crypto",)
    ))
    watchlist_syms = list(set(
        w["symbol"] for w in watchlist if w.get("asset_type") not in ("crypto",)
    ))
    symbols = list(set(portfolio_syms + watchlist_syms))

    if not symbols:
        # Default to some well-known symbols if no portfolio/watchlist
        symbols = ["AAPL", "MSFT", "NVDA"]

    # ── Pass 1: collect filings + cache hits, defer uncached for batch ──
    result = []
    uncached_filings = []  # list of (filing_dict,) entries needing AI

    # Fetch each symbol's recent filings in parallel instead of serially —
    # 15 cold symbols used to mean 15 back-to-back EDGAR roundtrips.
    # sec_edgar bounds its own concurrency (module-level semaphore of 8),
    # so 8 workers here can't exceed SEC's 10 req/s budget. parallel_map
    # preserves input order and returns None where a fetch raised, which
    # `or ()` skips — exactly what the old per-symbol `continue` did.
    def _recent_filings(sym):
        return edgar.get_recent_filings(
            sym, forms=["8-K", "10-K", "10-Q", "S-1"], limit=5
        )

    filing_lists = safe_executor.parallel_map(
        _recent_filings, symbols[:15],  # cap at 15 symbols to avoid long waits
        max_workers=8, thread_name_prefix="intel-filings",
    )

    # Flatten all filings, then resolve cache hits in ONE batch query instead
    # of a per-filing single-row get_cached_filing (N+1). When refreshing we
    # skip the cache entirely.
    all_filings = []
    for filings in filing_lists:
        for f in filings or ():
            if f.get("accession"):
                all_filings.append(f)

    cache_map = {}
    if not refresh:
        cache_map = db.get_cached_filings([f["accession"] for f in all_filings])

    for f in all_filings:
        acc = f.get("accession")
        if not acc:
            continue
        cached = cache_map.get(acc) if not refresh else None
        if cached:
            result.append({
                "ticker": f.get("ticker", ""),
                "form_type": f.get("form_type", ""),
                "filing_date": f.get("filing_date", ""),
                "description": f.get("description", ""),
                "accession": acc,
                "signal": cached.get("ai_signal", "NEUTRAL"),
                "summary": cached.get("ai_summary", ""),
                "key_points": cached.get("ai_key_points", []),
                "event_type": cached.get("ai_event_type", ""),
                "ai_powered": bool(cached.get("ai_powered")),
                "filing_url": f.get("document_url", ""),
            })
        else:
            uncached_filings.append(f)

    # ── Pass 2: handle uncached filings ──
    if uncached_filings:
        if ai_powered:
            # Parallel AI summarize — caches each result and emits an entry.
            def _summarize_and_cache(f):
                try:
                    ai_result = ai_summarizer.summarize_filing(
                        "", f["form_type"], f["ticker"], f["description"],
                    )
                    db.cache_filing(
                        f["accession"], f["ticker"], f["form_type"],
                        f["filing_date"], f["description"], "", ai_result,
                    )
                    return (f, ai_result)
                except Exception as exc:
                    log.warning(
                        "intel_feed: summarize %s/%s failed: %s",
                        f.get("ticker"), f.get("accession"), exc,
                    )
                    return (f, {})

            # parallel_map preserves input order like pool.map did;
            # _summarize_and_cache catches its own exceptions, so a None
            # slot (work fn raised) just falls back to empty AI fields.
            summarized = safe_executor.parallel_map(
                _summarize_and_cache, uncached_filings,
                max_workers=8, thread_name_prefix="intel-ai",
            )
            for f, pair in zip(uncached_filings, summarized):
                ai_result = pair[1] if isinstance(pair, (list, tuple)) and len(pair) > 1 else {}
                result.append({
                        "ticker": f.get("ticker", ""),
                        "form_type": f.get("form_type", ""),
                        "filing_date": f.get("filing_date", ""),
                        "description": f.get("description", ""),
                        "accession": f.get("accession", ""),
                        "signal": ai_result.get("signal", "NEUTRAL"),
                        "summary": ai_result.get("summary", ""),
                        "key_points": ai_result.get("key_points", []),
                        "event_type": ai_result.get("event_type", ""),
                        "ai_powered": bool(ai_result.get("ai_powered")),
                        "filing_url": f.get("document_url", ""),
                    })
        else:
            # No AI requested — return raw filing metadata, empty summary
            # fields. Don't cache here; let an ai_powered=1 call populate
            # the cache later.
            for f in uncached_filings:
                result.append({
                    "ticker": f.get("ticker", ""),
                    "form_type": f.get("form_type", ""),
                    "filing_date": f.get("filing_date", ""),
                    "description": f.get("description", ""),
                    "accession": f.get("accession", ""),
                    "signal": "NEUTRAL",
                    "summary": "",
                    "key_points": [],
                    "event_type": "",
                    "ai_powered": False,
                    "filing_url": f.get("document_url", ""),
                })

    # Sort by filing_date descending
    result.sort(key=lambda x: x.get("filing_date", ""), reverse=True)
    return jsonify(result[:50])


@app.route("/api/intel/filing/<path:accession>")
def intel_filing_detail(accession):
    """Fetch full filing text and run AI analysis."""
    ticker = request.args.get("ticker", "")
    form_type = request.args.get("form_type", "")
    description = request.args.get("description", "")

    # Check cache
    cached = db.get_cached_filing(accession)
    if cached and cached.get("filing_text"):
        return jsonify({
            "signal": cached.get("ai_signal", "NEUTRAL"),
            "summary": cached.get("ai_summary", ""),
            "key_points": cached.get("ai_key_points", []),
            "event_type": cached.get("ai_event_type", ""),
            "ai_powered": bool(cached.get("ai_powered")),
        })

    # Need to fetch filing text
    try:
        # Get filing info to find cik and primary_document
        cik = edgar.get_cik(ticker) if ticker else None
        if not cik:
            return jsonify({"error": "Cannot resolve CIK"}), 400

        cik_int = str(int(cik))
        acc_nodash = accession.replace("-", "")

        # Try to find primary document from index. EDGAR does not serve a
        # `-index.json` with a top-level `documents` list; reuse the EDGAR
        # helper that parses the real filing index page.
        try:
            primary_doc, _doc_type = edgar._best_document_from_index(
                cik_int, acc_nodash, form_type or ""
            )
        except Exception:
            primary_doc = None

        if not primary_doc:
            return jsonify({"error": "Cannot find primary document"}), 400

        filing_text = edgar.get_filing_text(cik_int, accession, primary_doc)
        ai_result = ai_summarizer.summarize_filing(filing_text, form_type, ticker, description)

        # Update cache with full text
        db.cache_filing(accession, ticker, form_type, "", description, filing_text, ai_result)

        return jsonify({
            "signal": ai_result.get("signal", "NEUTRAL"),
            "summary": ai_result.get("summary", ""),
            "key_points": ai_result.get("key_points", []),
            "event_type": ai_result.get("event_type", ""),
            "ai_powered": bool(ai_result.get("ai_powered")),
        })
    except Exception as e:
        return _err(e)


@app.route("/api/intel/insiders/<symbol>")
def intel_insiders(symbol):
    """Get Form 4 insider transactions for a symbol."""
    if not _valid_ticker(symbol):
        return jsonify({"error": "invalid symbol"}), 400
    symbol = symbol.upper()
    refresh = request.args.get("refresh", "false").lower() == "true"

    # Check cache
    if not refresh:
        cached = db.get_cached_insiders(symbol, days=1)
        if cached:
            pattern = ai_summarizer.analyze_insider_pattern(cached, symbol)
            return jsonify({"transactions": cached, "pattern": pattern, "from_cache": True})

    # Fetch from EDGAR
    try:
        transactions = edgar.get_form4_transactions(symbol, limit=30)
        if transactions:
            db.cache_insider_transactions(symbol, transactions)
        pattern = ai_summarizer.analyze_insider_pattern(transactions, symbol)
        return jsonify({"transactions": transactions, "pattern": pattern, "from_cache": False})
    except Exception as e:
        log.exception(e)
        return jsonify({"error": "internal error", "transactions": [], "pattern": {}}), 500


@app.route("/api/intel/institutional")
def intel_institutional():
    """Returns holdings for all tracked funds."""
    holdings = db.get_portfolio()
    portfolio_symbols = set(h["symbol"].upper() for h in holdings)

    # Cold-cache EDGAR fetches for each tracked fund used to run serially on
    # the request thread (N funds = N back-to-back roundtrips). Parallelize:
    # each fund's cache-check + maybe-fetch + overlap-compute is independent,
    # and db.cache_institutional serializes its own write under _write_lock.
    # sec_edgar bounds its own concurrency, so 6 workers stay within budget.
    result = {}

    def _process_fund(item):
        fund_name, fund_cik = item
        try:
            # Check cache
            cached = db.get_cached_institutional(fund_cik)
            if cached and cached.get("holdings"):
                fund_data = cached
            else:
                fund_data = edgar.get_institutional_holdings(fund_cik, fund_name)
                if fund_data and not fund_data.get("error") and fund_data.get("holdings"):
                    db.cache_institutional(fund_name, fund_cik, fund_data)

            if fund_data and not fund_data.get("error"):
                # Find overlap with portfolio
                overlap = []
                for h in fund_data.get("holdings", []):
                    h_name = h.get("name", "").upper()
                    h_ticker = (h.get("ticker") or "").upper()
                    # Try to match by the holding's own ticker when present,
                    # else a word-boundary match against the holding name.
                    # A bare substring test produced false positives for short
                    # tickers (e.g. 'A' matched every name, 'ON' matched 'EXXON').
                    for sym in portfolio_symbols:
                        if (h_ticker and sym == h_ticker) or (
                            h_name and re.search(r"\b" + re.escape(sym) + r"\b", h_name)
                        ):
                            overlap.append({**h, "portfolio_symbol": sym})
                            break

                return (fund_name, {
                    "filing_date": fund_data.get("filing_date", ""),
                    "period_of_report": fund_data.get("period_of_report", fund_data.get("period", "")),
                    "total_value": fund_data.get("total_value", 0),
                    "holdings": fund_data.get("holdings", [])[:50],
                    "overlap_with_portfolio": overlap,
                    "num_holdings": len(fund_data.get("holdings", [])),
                })
            return (fund_name, {
                "error": fund_data.get("error", "Unknown error") if fund_data else "No data",
                "holdings": [],
                "overlap_with_portfolio": [],
            })
        except Exception as e:
            return (fund_name, {"error": str(e), "holdings": [], "overlap_with_portfolio": []})

    entries = safe_executor.parallel_map(
        _process_fund, list(edgar.TRACKED_FUNDS.items()),
        max_workers=6, thread_name_prefix="intel-inst",
    )
    for entry in entries:
        if entry:  # parallel_map fills None for a worker that raised
            fund_name, fund_entry = entry
            result[fund_name] = fund_entry

    return jsonify(result)


@app.route("/api/intel/institutional/<path:fund_name>")
def intel_institutional_fund(fund_name):
    """Returns holdings for one specific fund."""
    fund_cik = edgar.TRACKED_FUNDS.get(fund_name)
    if not fund_cik:
        # Try case-insensitive match
        for name, cik in edgar.TRACKED_FUNDS.items():
            if name.lower() == fund_name.lower():
                fund_cik = cik
                fund_name = name
                break
    if not fund_cik:
        return jsonify({"error": f"Unknown fund: {fund_name}"}), 404

    cached = db.get_cached_institutional(fund_cik)
    if cached and cached.get("holdings"):
        return jsonify(cached)

    try:
        fund_data = edgar.get_institutional_holdings(fund_cik, fund_name)
        if fund_data and not fund_data.get("error"):
            db.cache_institutional(fund_name, fund_cik, fund_data)
        return jsonify(fund_data)
    except Exception as e:
        return _err(e)


# ── Smart Money Convergence Score ─────────────────────────────────────────────

@app.route("/api/smart-money/score/<symbol>")
def smart_money_score(symbol):
    """Compute Smart Money Convergence Score for one symbol."""
    import smart_money
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        result = smart_money.compute_score(symbol.upper())
        # Best-effort tracker log; never let a tracker hiccup break the panel.
        try:
            if 'research_tracker' in globals() and research_tracker:
                research_tracker.log_smart_money(symbol.upper(), result)
        except Exception:
            pass
        return jsonify(result)
    except Exception as e:
        return _err(e)


@app.route("/api/smart-money/scores", methods=["POST"])
def smart_money_scores_bulk():
    """Compute Smart Money scores for multiple symbols."""
    import smart_money
    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols", [])
    # A JSON STRING is iterable — "NVDA" would be scanned per character as the
    # real tickers N/V/D/A, returning plausible-but-wrong results. Require a
    # list and keep only valid tickers.
    if symbols and not isinstance(symbols, list):
        return jsonify({"error": "symbols must be a list"}), 400
    symbols = [str(s).strip().upper() for s in symbols if _valid_ticker(s)]
    if not symbols:
        # Default: portfolio symbols
        holdings = db.get_portfolio()
        symbols = list({h["symbol"] for h in holdings if h["asset_type"] == "stock"})
    if not symbols:
        return jsonify({"scores": []})
    try:
        scores = smart_money.compute_scores_bulk(symbols[:20])
        return jsonify({"scores": scores})
    except Exception as e:
        return _err(e)


# ── Unusual Options Activity ────────────────────────────────────────────────────

@app.route("/api/options-flow/<symbol>")
def options_flow_symbol(symbol):
    """Scan unusual options activity for a single symbol."""
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        result = fetcher.get_unusual_options_flow(symbol.upper())
        return jsonify(result)
    except Exception as e:
        return _err(e)


@app.route("/api/options-flow/scan", methods=["POST"])
def options_flow_scan():
    """Scan unusual options activity for portfolio symbols."""
    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols", [])
    # See smart_money_scores_bulk: a JSON string would be scanned per character.
    if symbols and not isinstance(symbols, list):
        return jsonify({"error": "symbols must be a list"}), 400
    symbols = [str(s).strip().upper() for s in symbols if _valid_ticker(s)]
    if not symbols:
        holdings = db.get_portfolio()
        symbols = list({h["symbol"] for h in holdings if h["asset_type"] == "stock"})
    if not symbols:
        return jsonify({"results": []})
    scanned = symbols[:15]
    try:
        results = fetcher.scan_unusual_options_portfolio(scanned)
        # Report what was ACTUALLY scanned — claiming len(symbols) told the
        # user all N names were screened when everything past 15 was skipped.
        return jsonify({"results": results, "scanned": len(scanned),
                        "truncated": len(symbols) > len(scanned)})
    except Exception as e:
        return _err(e)


# ── Congressional Trading Intelligence ─────────────────────────────────────────

@app.route("/api/congress/trades")
def congress_trades():
    """Get recent congressional trading activity summary."""
    import congress
    # Clamp unbounded params — a huge max_pdfs/days would fan out to hundreds of
    # slow PDF fetches and peg the request thread (DoS).
    days = min(max(_safe_int(request.args.get("days"), 90), 1), 365)
    max_pdfs = min(max(_safe_int(request.args.get("max_pdfs"), 60), 1), 200)
    try:
        result = congress.get_congress_summary(days=days, max_pdfs=max_pdfs)
        return jsonify(result)
    except Exception as e:
        return _err(e)


@app.route("/api/congress/trades/<symbol>")
def congress_trades_symbol(symbol):
    """Get recent congressional trades for a specific ticker."""
    import congress
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    days = min(max(_safe_int(request.args.get("days"), 180), 1), 365)
    try:
        trades = congress.get_trades_for_ticker(symbol.upper(), days=days)
        return jsonify({"trades": trades, "symbol": symbol.upper()})
    except Exception as e:
        return _err(e)


@app.route("/api/terminal", methods=["POST"])
def terminal_exec():
    """Execute a CLI command and return ANSI output."""
    data = request.get_json(force=True, silent=True) or {}
    command = (data.get("command") or "").strip()
    if not command:
        return jsonify({"output": "", "error": "No command provided"}), 400

    # Allowlist the CLI subcommand. The old substring denylist was bypassable
    # (e.g. ";rm", a tab-prefixed "\trm", or "serve" reached via an alias) — and
    # cli.run_command shells the string into argparse, so anything not on the
    # known-safe READ-ONLY list must be rejected. Mutating/admin commands
    # (portfolio, watchlist, transactions, alerts, settings) and the server
    # launcher (serve) are deliberately excluded.
    _ALLOWED_CLI = {
        "help", "quote", "fundamentals", "chart", "news", "search", "market",
        "analytics", "options", "dividends", "macro", "earnings", "intel",
        "smart-money", "ml-forecast", "congress", "crypto", "ai", "scanner",
        "gex", "contagion", "narrative", "synthetic-insider", "reflexivity",
        "liquidity", "alt-data",
        # Jarvis read-only commands: "ask" runs with persist=False (no portfolio
        # mutation), "briefing"/"health" are pure read snapshots. "settings"
        # stays excluded (mutating) as does "serve" (server launcher).
        "ask", "briefing", "health",
    }
    first = command.split()[0].lower() if command.split() else ""
    if first not in _ALLOWED_CLI:
        return jsonify({
            "output": "\033[31m  Command '{}' not allowed in web terminal. "
                      "Only read-only commands are permitted.\033[0m\n".format(first),
            "exit_code": 1,
        })

    import cli as cli_mod
    try:
        output, exit_code = cli_mod.run_command(command)
        return jsonify({"output": output, "exit_code": exit_code})
    except Exception as e:
        return jsonify({"output": f"\033[31m  Error: {e}\033[0m\n", "exit_code": 1})


@app.route("/api/smart-money/ml-forecast/<symbol>")
def ml_forecast_route(symbol):
    """ML-based predictive analytics for a symbol."""
    if not _valid_ticker(symbol):
        return jsonify({"error": "invalid symbol"}), 400
    import ml_forecast as mlf
    try:
        result = mlf.ml_forecast(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        try:
            if 'research_tracker' in globals() and research_tracker:
                research_tracker.log_ml_forecast(symbol.upper(), result)
        except Exception:
            pass
        # Convert numpy/datetime types for JSON serialization
        return Response(
            json.dumps(result, default=str),
            mimetype="application/json"
        )
    except Exception as e:
        return _err(e)


# ─── GEX (Gamma Exposure) Flow Predictor ────────────────────────────────────

@app.route("/api/gex/<symbol>")
def gex_analysis(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "invalid symbol"}), 400
    import gex_engine
    try:
        result = gex_engine.compute_gex(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        try:
            if 'research_tracker' in globals() and research_tracker:
                research_tracker.log_gex(symbol.upper(), result)
        except Exception:
            pass
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return _err(e)


@app.route("/api/gex/summary/<symbol>")
def gex_summary(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "invalid symbol"}), 400
    import gex_engine
    try:
        result = gex_engine.get_gex_summary(symbol.upper())
        return jsonify(result)
    except Exception as e:
        return _err(e)


# ─── Corporate Contagion Graph ──────────────────────────────────────────────

@app.route("/api/contagion/<symbol>")
def contagion_graph_route(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "invalid symbol"}), 400
    import contagion_graph
    try:
        result = contagion_graph.build_graph(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return _err(e)


@app.route("/api/contagion/impact/<symbol>")
def contagion_impact(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "invalid symbol"}), 400
    import contagion_graph
    event_type = request.args.get("event", "earnings_miss")
    try:
        result = contagion_graph.assess_contagion(symbol.upper(), event_type=event_type)
        if "error" in result:
            return jsonify(result), 400
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return _err(e)


# ─── Narrative Velocity Engine ──────────────────────────────────────────────

@app.route("/api/narrative/<symbol>")
def narrative_analysis(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "invalid symbol"}), 400
    import narrative_engine
    try:
        result = narrative_engine.analyze_narrative(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        try:
            if 'research_tracker' in globals() and research_tracker:
                research_tracker.log_narrative(symbol.upper(), result)
        except Exception:
            pass
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return _err(e)


# ─── Synthetic Insider Composite ────────────────────────────────────────────

@app.route("/api/synthetic-insider/<symbol>")
def synthetic_insider_route(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "invalid symbol"}), 400
    import synthetic_insider
    try:
        result = synthetic_insider.compute_composite(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return _err(e)


@app.route("/api/synthetic-insider/scan", methods=["POST"])
def synthetic_insider_scan():
    import synthetic_insider
    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols", [])
    # See smart_money_scores_bulk: a JSON string would be scanned per character.
    if symbols and not isinstance(symbols, list):
        return jsonify({"error": "symbols must be a list"}), 400
    symbols = [str(s).strip().upper() for s in symbols if _valid_ticker(s)]
    if not symbols:
        holdings = db.get_portfolio()
        symbols = list(set(h["symbol"] for h in holdings if h.get("asset_type") != "crypto"))
    if not symbols:
        return jsonify({"results": []})
    try:
        results = synthetic_insider.scan_composite_bulk(symbols[:15])
        return Response(json.dumps({"results": results}, default=str), mimetype="application/json")
    except Exception as e:
        return _err(e)


# ─── Reflexivity Detector ──────────────────────────────────────────────────

@app.route("/api/reflexivity/<symbol>")
def reflexivity_detect(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "invalid symbol"}), 400
    import reflexivity_detector
    try:
        result = reflexivity_detector.detect_loops(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return _err(e)


# ─── Liquidity Regime Monitor ──────────────────────────────────────────────

@app.route("/api/liquidity")
def liquidity_monitor_route():
    import liquidity_monitor as lm
    try:
        result = lm.compute_stress_score()
        if "error" in result:
            return jsonify(result), 400
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return _err(e)


# ─── Alt-Data Revenue Nowcasting ───────────────────────────────────────────

@app.route("/api/alt-data/<symbol>")
def alt_data_nowcast(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "invalid symbol"}), 400
    import alt_data_engine
    try:
        result = alt_data_engine.nowcast_revenue(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return _err(e)


@app.route("/api/alt-data/scan", methods=["POST"])
def alt_data_scan():
    import alt_data_engine
    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols", [])
    # See smart_money_scores_bulk: a JSON string would be scanned per character.
    if symbols and not isinstance(symbols, list):
        return jsonify({"error": "symbols must be a list"}), 400
    symbols = [str(s).strip().upper() for s in symbols if _valid_ticker(s)]
    if not symbols:
        holdings = db.get_portfolio()
        symbols = list(set(h["symbol"] for h in holdings if h.get("asset_type") != "crypto"))
    if not symbols:
        return jsonify({"results": []})
    try:
        results = alt_data_engine.nowcast_bulk(symbols[:15])
        return Response(json.dumps({"results": results}, default=str), mimetype="application/json")
    except Exception as e:
        return _err(e)


# ─── Opportunity Scanner ──────────────────────────────────────────────────────

@app.route("/api/scanner/profile", methods=["GET"])
def scanner_profile_get():
    """Return the user's saved scanner profile (or defaults)."""
    import opportunity_scanner as scanner
    return jsonify(scanner.get_scanner_profile())


@app.route("/api/scanner/profile", methods=["POST"])
def scanner_profile_save():
    """Save/update the scanner profile."""
    import opportunity_scanner as scanner
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400
    # The profile must be a JSON object — a list/scalar would break
    # save_scanner_profile's dict.update() and corrupt the saved config that
    # scan_opportunities reads back.
    if not isinstance(data, dict):
        return jsonify({"error": "profile must be a JSON object"}), 400
    scanner.save_scanner_profile(data)
    return jsonify({"status": "ok", "profile": data})


@app.route("/api/scanner/scan", methods=["POST"])
def scanner_scan():
    """Run a full opportunity scan using the saved profile."""
    import opportunity_scanner as scanner
    force = request.get_json(silent=True) or {}
    force_refresh = force.get("force", False)
    profile = scanner.get_scanner_profile()
    try:
        results = scanner.scan_opportunities(profile, force_refresh=force_refresh)
        return Response(
            json.dumps(results, default=str),
            mimetype="application/json"
        )
    except Exception as e:
        return _err(e)


@app.route("/api/scanner/results")
def scanner_results():
    """Return cached scan results (or empty)."""
    import opportunity_scanner as scanner
    cached = scanner.get_cached_scan()
    if cached:
        return Response(
            json.dumps(cached, default=str),
            mimetype="application/json"
        )
    return jsonify({"opportunities": [], "meta": {"cached": False}})


@app.route("/api/scanner/watchlist")
def scanner_watchlist():
    """Symbols that have appeared in recent scans, ranked by appearance + max score."""
    # clamp positive — a negative limit becomes SQL LIMIT -1 (all rows) and a
    # negative days builds a "--N days" modifier that yields an empty window.
    days = max(1, _safe_int(request.args.get("days"), 30))
    limit = max(1, _safe_int(request.args.get("limit"), 20))
    return jsonify({"watchlist": db.get_scanner_watchlist(limit=limit, days=days)})


@app.route("/api/scanner/history/<symbol>")
def scanner_history(symbol):
    """Score timeline for a symbol across scan runs."""
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    # Clamp positive — a negative limit becomes SQL `LIMIT -1` (all rows).
    limit = max(1, _safe_int(request.args.get("limit"), 100))
    return jsonify({"symbol": symbol.upper(), "history": db.get_scan_history(symbol=symbol, limit=limit)})


# ════════════════════════════════════════════════════════════════════
#   NEW DATA SOURCES — Tier 1 (no key) + Tier 3 (better wrappers)
# ════════════════════════════════════════════════════════════════════

# ── Congress: Senate + combined ──────────────────────────────────
@app.route("/api/congress/senate")
def congress_senate():
    """Senate STOCK Act trades from senate-stock-watcher-data."""
    symbol = request.args.get("symbol")
    if symbol and not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    limit = max(1, _safe_int(request.args.get("limit"), 200))  # clamp: negative limit truncates results
    return jsonify({"trades": ds.get_senate_trades(symbol=symbol, limit=limit)})


@app.route("/api/congress/all")
def congress_all():
    """Combined House + Senate trades, sorted newest first."""
    symbol = request.args.get("symbol")
    if symbol and not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    limit = max(1, _safe_int(request.args.get("limit"), 200))  # clamp: -1 would slice off the last row
    # Over-fetch senate so the post-merge slice can surface the true newest
    # `limit` combined rows even when senate dominates — otherwise senate is
    # pre-truncated to its own newest `limit` before House rows interleave.
    senate = ds.get_senate_trades(symbol=symbol, limit=limit * 2)
    try:
        import congress as house_mod
        house_trades = house_mod.get_congress_summary(days=120, max_pdfs=40)
        house_rows = []
        # get_congress_summary returns a FLAT trades list (there is no
        # "members" grouping) whose rows use congress.py's field names
        # (member_name/txn_type/txn_date/amount_str/notif_date/pdf_url) —
        # map them into the senate-row schema built above.
        for t in house_trades.get("trades", []) or []:
            if not isinstance(t, dict):
                continue
            tk = (t.get("ticker") or "").upper()
            if symbol and tk != symbol.upper():
                continue
            house_rows.append({
                "chamber": "House",
                "name": t.get("member_name") or "",
                "ticker": tk,
                "asset_description": t.get("asset_type") or "",
                "type": t.get("txn_type") or "",
                "amount": t.get("amount_str") or "",
                "date": t.get("txn_date") or "",
                "filed": t.get("notif_date") or "",
                "ptr_link": t.get("pdf_url") or "",
            })
    except Exception as e:
        log.debug("house combined: %s", e)
        house_rows = []
    combined = senate + house_rows

    # Sort on PARSED dates, newest first. Both chambers report MM/DD/YYYY,
    # which sorts wrong lexicographically across year boundaries
    # ("12/05/2025" > "01/15/2026") — the top-`limit` slice would then drop
    # the genuinely newest trades entirely.
    def _cg_dt(s):
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(s or "").strip(), fmt)
            except ValueError:
                pass
        return None

    combined.sort(key=lambda r: (_cg_dt(r.get("date")) or datetime.min,
                                 _cg_dt(r.get("filed")) or datetime.min),
                  reverse=True)
    sliced = combined[:limit]
    # Report counts actually included in the returned slice, not pre-truncation.
    return jsonify({
        "trades": sliced,
        "senate_count": sum(1 for r in sliced if r.get("chamber") != "House"),
        "house_count": sum(1 for r in sliced if r.get("chamber") == "House"),
    })


# ── GDELT news / narrative ────────────────────────────────────────
@app.route("/api/news/gdelt/<symbol>")
def news_gdelt(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    days = _safe_int(request.args.get("days"), 3)
    timespan = f"{max(min(days, 30), 1)}d"
    arts = ds.gdelt_articles(symbol.upper(), max_records=25, timespan=timespan)
    return jsonify({"symbol": symbol.upper(), "articles": arts})


@app.route("/api/news/gdelt-tone/<symbol>")
def news_gdelt_tone(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    return jsonify({
        "symbol": symbol.upper(),
        "tone": ds.gdelt_tone_timeline(symbol.upper(), timespan="2w"),
    })


# ── DefiLlama crypto / DeFi ───────────────────────────────────────
@app.route("/api/crypto/defi")
def crypto_defi():
    return jsonify(ds.defillama_tvl_summary())


@app.route("/api/crypto/stablecoins")
def crypto_stablecoins():
    return jsonify({"stablecoins": ds.defillama_stablecoins()})


@app.route("/api/crypto/yields")
def crypto_yields():
    limit = max(1, _safe_int(request.args.get("limit"), 20))
    return jsonify({"pools": ds.defillama_top_yields(limit=limit)})


# ── mempool.space BTC ─────────────────────────────────────────────
@app.route("/api/crypto/btc/mempool")
def crypto_btc_mempool():
    return jsonify(ds.mempool_btc_stats())


# ── ccxt cross-exchange prices ────────────────────────────────────
@app.route("/api/crypto/cross-exchange")
def crypto_cross_exchange():
    if crypto_exchanges is None:
        return jsonify({"error": "ccxt unavailable"}), 503
    pair = request.args.get("pair") or "BTC/USDT"
    return jsonify(crypto_exchanges.cross_exchange_prices(pair))


# ── Treasury yield curve ──────────────────────────────────────────
@app.route("/api/macro/treasury-curve")
def macro_treasury_curve():
    return jsonify(ds.treasury_yield_curve())


# ── CBOE / VIX history ────────────────────────────────────────────
@app.route("/api/macro/vix")
def macro_vix():
    return jsonify(ds.cboe_vix_history() or {})


# ── FRED (Federal Reserve Economic Data) ──────────────────────────
# Free, no-auth CSV endpoints for 800k+ macro series. We expose three
# routes: a snapshot dashboard, the curated catalog, and per-series detail.
try:
    import fred_data
except Exception as _fred_err:
    fred_data = None
    log.warning("fred_data unavailable: %s", _fred_err)


@app.route("/api/macro/fred/snapshot")
def fred_snapshot():
    if not fred_data:
        return jsonify({"error": "fred_data module not available"}), 503
    return jsonify(fred_data.snapshot())


@app.route("/api/macro/fred/catalog")
def fred_catalog():
    if not fred_data:
        return jsonify({"error": "fred_data module not available"}), 503
    return jsonify({"series": fred_data.catalog()})


@app.route("/api/macro/fred/<series_id>")
def fred_series(series_id):
    if not fred_data:
        return jsonify({"error": "fred_data module not available"}), 503
    obs = min(max(_safe_int(request.args.get("observations"), 60), 1), 5000)
    return jsonify(fred_data.fetch_series(series_id, observations=obs))


# ── CFTC Commitments of Traders ───────────────────────────────────
# Weekly futures positioning data, no auth required. Drives a Macro
# panel showing the net long/short shift of leveraged funds across
# S&P/Treasuries/USD/commodities.
try:
    import cftc_cot
except Exception as _cftc_err:
    cftc_cot = None
    log.warning("cftc_cot unavailable: %s", _cftc_err)


@app.route("/api/macro/cftc/snapshot")
def cftc_snapshot():
    if not cftc_cot:
        return jsonify({"error": "cftc_cot module not available"}), 503
    return jsonify(cftc_cot.snapshot())


# ── Wikidata corporate metadata ───────────────────────────────────
# SPARQL endpoint, keyless. Returns HQ/inception/CEO/parent/employees
# for any ticker that has a Wikidata entity.
try:
    import wikidata_meta
except Exception as _wd_err:
    wikidata_meta = None
    log.warning("wikidata_meta unavailable: %s", _wd_err)


@app.route("/api/research/wikidata/<symbol>")
def research_wikidata(symbol):
    if not wikidata_meta:
        return jsonify({"error": "wikidata_meta module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    return jsonify(wikidata_meta.fetch_facts(symbol.upper()))


# ── Finviz scraping (via finvizfinance library on GitHub) ─────────
# Sector performance/valuation heatmap + insider trades + per-ticker news.
try:
    import finviz_data
except Exception as _fv_err:
    finviz_data = None
    log.warning("finviz_data unavailable: %s", _fv_err)


@app.route("/api/market/finviz/sectors")
def finviz_sectors():
    if not finviz_data:
        return jsonify({"error": "finviz_data module not available"}), 503
    return jsonify(finviz_data.sector_heatmap())


@app.route("/api/intel/finviz/insiders")
def finviz_insiders():
    if not finviz_data:
        return jsonify({"error": "finviz_data module not available"}), 503
    option = request.args.get("option", "latest")
    if option not in {"latest", "latest buys", "latest sales", "top week", "top owner trade"}:
        return jsonify({"error": "invalid option"}), 400
    return jsonify(finviz_data.insider_trades(option=option))


@app.route("/api/news/finviz/<symbol>")
def finviz_news(symbol):
    if not finviz_data:
        return jsonify({"error": "finviz_data module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    return jsonify(finviz_data.stock_news(symbol.upper()))


# ── FinanceDatabase universe ──────────────────────────────────────
@app.route("/api/screener/facets")
def screener_facets():
    asset = request.args.get("asset", "equities")
    return jsonify(ds.financedb_facets(asset=asset))


@app.route("/api/screener/universe")
def screener_universe():
    asset = request.args.get("asset", "equities")
    out = ds.financedb_filter(
        asset=asset,
        country=request.args.get("country"),
        sector=request.args.get("sector"),
        industry=request.args.get("industry"),
        exchange=request.args.get("exchange"),
        limit=min(max(_safe_int(request.args.get("limit"), 200), 1), 1000),
    )
    return jsonify({"asset": asset, "results": out, "count": len(out)})


# ── SEC XBRL fundamentals ─────────────────────────────────────────
@app.route("/api/research/xbrl/<symbol>")
def research_xbrl(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    cik = edgar.get_cik(symbol.upper())
    if not cik:
        return jsonify({"error": f"No CIK for {symbol}"}), 404
    return jsonify({"symbol": symbol.upper(), "cik": cik, "metrics": ds.xbrl_key_metrics(cik)})


# ── edgartools-backed filings + Form 4 ────────────────────────────
# `sec_filings_v2` depends on the optional `edgartools` PyPI package which we
# intentionally don't ship in requirements.txt (heavy dep; the lightweight
# sec_edgar.py covers the same ground). When unavailable we return a clear
# 503 with an explanatory error envelope instead of an empty list — see
# sec_filings_v2.is_available().
_SEC_V2_DORMANT_ENVELOPE = {
    "error": "sec_filings_v2 module unavailable — uses optional edgartools dep"
}


def _sec_v2_unavailable():
    return sec_filings_v2 is None or not sec_filings_v2.is_available()


@app.route("/api/intel/filings-v2/<symbol>")
def intel_filings_v2(symbol):
    # Validate the symbol before the capability check so an invalid symbol gets
    # a 400 (consistent error semantics) rather than a 503 'unavailable'.
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    if _sec_v2_unavailable():
        return jsonify(dict(_SEC_V2_DORMANT_ENVELOPE, symbol=symbol.upper())), 503
    form = request.args.get("form")
    limit = _safe_int(request.args.get("limit"), 20)
    return jsonify({
        "symbol": symbol.upper(),
        "filings": sec_filings_v2.get_company_filings(symbol.upper(), form=form, limit=limit),
    })


@app.route("/api/intel/form4-v2/<symbol>")
def intel_form4_v2(symbol):
    # Validate before the capability check (see intel_filings_v2).
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    if _sec_v2_unavailable():
        return jsonify(dict(_SEC_V2_DORMANT_ENVELOPE, symbol=symbol.upper())), 503
    limit = _safe_int(request.args.get("limit"), 30)
    return jsonify({
        "symbol": symbol.upper(),
        "transactions": sec_filings_v2.get_form4_transactions(symbol.upper(), limit=limit),
    })


# ── NASDAQ calendar (earnings/IPO/dividends) ──────────────────────
@app.route("/api/calendar/earnings")
def calendar_earnings_v2():
    if calendar_v2 is None:
        return jsonify({"error": "finance_calendars unavailable"}), 503
    date = request.args.get("date")
    rows = calendar_v2.earnings_for(date) if date else calendar_v2.earnings_today()
    return jsonify({"date": date, "earnings": rows or []})


@app.route("/api/calendar/ipos")
def calendar_ipos():
    if calendar_v2 is None:
        return jsonify({"error": "finance_calendars unavailable"}), 503
    return jsonify({
        "priced": calendar_v2.ipos_this_month() or [],
        "upcoming": calendar_v2.upcoming_ipos() or [],
    })


@app.route("/api/calendar/dividends")
def calendar_dividends():
    if calendar_v2 is None:
        return jsonify({"error": "finance_calendars unavailable"}), 503
    return jsonify({"dividends": calendar_v2.dividends_today() or []})


@app.route("/api/calendar/splits")
def calendar_splits():
    if calendar_v2 is None:
        return jsonify({"error": "finance_calendars unavailable"}), 503
    return jsonify({"splits": calendar_v2.splits_today() or []})


# ── Alt-data: Reddit + StockTwits ─────────────────────────────────
@app.route("/api/alt-data/reddit/mentions")
def alt_reddit_mentions():
    return jsonify({"mentions": alt_signals.reddit_ticker_mentions()})


@app.route("/api/alt-data/reddit/<subreddit>")
def alt_reddit_sub(subreddit):
    if not re.fullmatch(r"[A-Za-z0-9_]{1,30}", subreddit or ""):
        return jsonify({"error": "invalid subreddit"}), 400
    sort = request.args.get("sort", "hot")
    if sort not in ("hot", "new", "top", "rising"):
        sort = "hot"
    limit = _safe_int(request.args.get("limit"), 25)
    return jsonify({
        "subreddit": subreddit,
        "posts": alt_signals.reddit_subreddit_posts(subreddit, sort=sort, limit=limit),
    })


@app.route("/api/alt-data/stocktwits/<symbol>")
def alt_stocktwits(symbol):
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    return jsonify(alt_signals.stocktwits_symbol_sentiment(symbol.upper()) or {})


@app.route("/api/alt-data/stocktwits-trending")
def alt_stocktwits_trending():
    return jsonify({"trending": alt_signals.stocktwits_trending(limit=20)})


def _social_composite(sources):
    """Blend the working social sources into a single 0-100 buzz score and a
    -1..1 sentiment, tolerating any source being missing/rate-limited."""
    st = sources.get("stocktwits") or {}
    hn = sources.get("hackernews") or {}
    wk = sources.get("wikipedia") or {}

    def _live(s):
        return s.get("status") == "live"

    # Buzz components, each normalized to ~0-100.
    parts = []
    if _live(st):
        parts.append(min(100.0, (st.get("messages_total") or 0) / 30.0 * 100.0))
    if _live(hn):
        parts.append(min(100.0, (hn.get("mention_count") or 0) / 50.0 * 100.0))
    spike = ((wk.get("stats") or {}).get("spike_pct_vs_baseline"))
    # spike originates from an external Wikipedia module and may be cached as a
    # numeric string; coerce defensively so a bad value can't 500 the panel.
    try:
        spike_val = float(spike) if spike is not None else None
    except (TypeError, ValueError):
        spike_val = None
    if _live(wk) and spike_val is not None:
        # 0% spike -> 50 (baseline attention), +50% -> 100, -50% -> 0.
        parts.append(min(100.0, max(0.0, 50.0 + spike_val)))
    buzz = round(sum(parts) / len(parts)) if parts else None

    # Sentiment direction in -1..1 from StockTwits bull ratio + HN polarity.
    sent = []
    br = st.get("bull_ratio")
    if _live(st) and br is not None:
        sent.append((float(br) - 0.5) * 2.0)
    ap = (hn.get("stats") or {}).get("avg_polarity")
    if _live(hn) and ap is not None:
        sent.append(max(-1.0, min(1.0, float(ap) * 5.0)))
    sentiment = round(sum(sent) / len(sent), 2) if sent else None
    if sentiment is None:
        label = "—"
    elif sentiment > 0.15:
        label = "BULLISH"
    elif sentiment < -0.15:
        label = "BEARISH"
    else:
        label = "NEUTRAL"

    n_live = sum(1 for s in (st, hn, wk) if _live(s))
    return {
        "buzz_score": buzz,
        "sentiment": sentiment,
        "sentiment_label": label,
        # Only surface spike_pct when Wikipedia is actually live — a stale/non-live
        # 'stats' blob would otherwise leak a spike figure that didn't feed buzz.
        "spike_pct": round(spike_val, 1) if (_live(wk) and spike_val is not None) else None,
        "sources_live": n_live,
    }


@app.route("/api/alt-data/social/<symbol>")
def alt_social_pulse(symbol):
    """Aggregate per-symbol social signals (StockTwits sentiment, Hacker News
    mentions, Wikipedia attention) into one 'social pulse' with a composite buzz
    score. Each source is captured independently so a rate-limited/erroring one
    degrades gracefully instead of blanking the panel. Reddit is reported as
    unavailable — its public JSON API now 403s unauthenticated apps."""
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    sym = symbol.upper()
    out = {
        "symbol": sym,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "sources": {},
        "reddit": {
            "status": "unavailable",
            "note": "Reddit no longer serves its public JSON API to apps (HTTP 403) — "
                    "even OAuth's data host is IP-blocked. StockTwits/HN/Wiki used instead.",
        },
    }

    # StockTwits per-symbol sentiment.
    try:
        st = alt_signals.stocktwits_symbol_sentiment(sym)
        if st and (st.get("messages_total") or 0) > 0:
            st = dict(st)
            st["status"] = "live"
            out["sources"]["stocktwits"] = st
        else:
            # None = cached failure / rate-limit; dict with 0 = genuinely quiet.
            out["sources"]["stocktwits"] = {
                "status": "ratelimited" if st is None else "quiet",
                "messages_total": (st or {}).get("messages_total", 0),
            }
    except Exception as e:
        out["sources"]["stocktwits"] = {"status": "error", "note": str(e)[:140]}

    # Hacker News mentions.
    if hn_sentiment:
        try:
            # A None return (failure/short-circuit) must not AttributeError, and
            # 'live' requires positive evidence — not merely the absence of an
            # 'error' key.
            hn = hn_sentiment.fetch_mentions(sym, hours=168) or {}
            hn_live = (not hn.get("error")) and bool(hn.get("mention_count"))
            out["sources"]["hackernews"] = {
                "status": "live" if hn_live else "error" if hn.get("error") else "quiet",
                "mention_count": hn.get("mention_count", 0),
                "stats": hn.get("stats", {}),
                "mentions": (hn.get("mentions") or [])[:5],
            }
        except Exception as e:
            out["sources"]["hackernews"] = {"status": "error", "note": str(e)[:140]}

    # Wikipedia attention.
    if wiki_attention:
        try:
            # A None return must not AttributeError; 'live' requires actual
            # data (non-empty stats or points), not just a missing 'error' key.
            w = wiki_attention.fetch_pageviews(sym, days=30) or {}
            w_live = (not w.get("error")) and bool(w.get("stats") or w.get("points"))
            out["sources"]["wikipedia"] = {
                "status": "live" if w_live else "error" if w.get("error") else "quiet",
                "stats": w.get("stats", {}),
                "article": w.get("article"),
                "article_url": w.get("article_url"),
                "points": w.get("points", []),
            }
        except Exception as e:
            out["sources"]["wikipedia"] = {"status": "error", "note": str(e)[:140]}

    out["composite"] = _social_composite(out["sources"])
    return jsonify(out)


# ── Wikipedia pageviews — retail-attention proxy ──────────────────
# Free, key-less. Daily pageview series for a stock's article + spike
# detection vs. a 7-day baseline.
try:
    import wiki_attention
except Exception as _wiki_err:
    wiki_attention = None
    log.warning("wiki_attention unavailable: %s", _wiki_err)


@app.route("/api/alt-data/wiki/<symbol>")
def alt_wiki_pageviews(symbol):
    if not wiki_attention:
        return jsonify({"error": "wiki_attention module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    days = _safe_int(request.args.get("days"), 30)
    return jsonify(wiki_attention.fetch_pageviews(symbol.upper(), days=days))


# ── Hacker News mentions — tech sentiment signal ──────────────────
# Algolia-backed search, no auth. Returns recent stories/comments
# mentioning the ticker or company name, with coarse polarity scores.
try:
    import hn_sentiment
except Exception as _hn_err:
    hn_sentiment = None
    log.warning("hn_sentiment unavailable: %s", _hn_err)


@app.route("/api/alt-data/hackernews/<symbol>")
def alt_hackernews(symbol):
    if not hn_sentiment:
        return jsonify({"error": "hn_sentiment module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    hours = _safe_int(request.args.get("hours"), 168)
    return jsonify(hn_sentiment.fetch_mentions(symbol.upper(), hours=hours))


# ──────────────────────────────────────────────────────────────────────────────
# IDEAS — random investment idea generator
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/api/ideas/random")
def ideas_random():
    """
    Generate a single random investment idea dossier.
    Query params (all optional):
      - asset_class: stock | crypto | etf | any
      - sector:     equity sector filter
      - strategy:   growth | value | income | momentum
      - min_score:  reject picks below this composite (0-100), re-roll up to 3x
      - exclude:    comma-separated list of symbols to skip
    """
    asset_class = (request.args.get("asset_class") or "").strip().lower() or None
    sector = (request.args.get("sector") or "").strip() or None
    strategy = (request.args.get("strategy") or "").strip().lower() or None
    # min_score may be fractional (e.g. 7.5); _safe_int would drop it to None
    # and silently disable the filter. Parse as float and clamp to 0-100.
    min_score = None
    _ms_raw = request.args.get("min_score")
    if _ms_raw is not None and str(_ms_raw).strip() != "":
        try:
            _ms = float(_ms_raw)
            # min/max pass NaN straight through the clamp — a NaN threshold
            # disables the filter AND serializes as invalid-JSON literal NaN.
            min_score = min(max(_ms, 0.0), 100.0) if math.isfinite(_ms) else None
        except (TypeError, ValueError):
            min_score = None
    discovery_mode = (request.args.get("discovery_mode") or "curated").strip().lower()
    exclude_raw = (request.args.get("exclude") or "").strip()
    exclude = [s.strip().upper() for s in exclude_raw.split(",") if s.strip()] if exclude_raw else None

    if asset_class == "any":
        asset_class = None

    try:
        idea = idea_generator.generate_random_idea(
            asset_class=asset_class,
            sector=sector,
            strategy=strategy,
            min_score=min_score,
            exclude=exclude,
            discovery_mode=discovery_mode,
        )
        # No idea matched the filters is a normal empty result, not a server
        # fault — return 404 with the generator's own message so the UI can say
        # "nothing matched, loosen your filters" rather than showing an error.
        if not idea or idea.get("error"):
            msg = (idea or {}).get("error") or "no idea matched the given filters"
            return jsonify({"error": msg}), 404
        return jsonify(idea)
    except Exception as e:
        log.exception("ideas_random failed: %s", e)
        return _err(e)


@app.route("/api/ideas/universe")
def ideas_universe():
    """Universe metadata for the picker UI (counts, available sectors/strategies)."""
    discovery_mode = (request.args.get("discovery_mode") or "curated").strip().lower()
    try:
        return jsonify(idea_generator.list_universe(discovery_mode=discovery_mode))
    except Exception as e:
        return _err(e)


@app.route("/api/ideas/enrich/<symbol>")
def ideas_enrich(symbol):
    """
    Compute the slow enrichment blocks for a symbol that the user just rolled.
    Two-phase streaming: /api/ideas/random returns fast blocks, then the UI
    fires this endpoint in parallel to fill in the rest.
    """
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    asset_class = (request.args.get("asset_class") or "stock").strip().lower()
    strategy = (request.args.get("strategy") or "growth").strip().lower()
    try:
        return jsonify(idea_generator.enrich_idea(symbol.upper(), asset_class, strategy))
    except Exception as e:
        log.exception("ideas_enrich %s failed: %s", symbol, e)
        return _err(e)


@app.route("/api/ideas/warmer/status")
def ideas_warmer_status():
    """Diagnostic — show the pre-warmer's state for the UI badge."""
    try:
        import idea_pool_warmer
        status = idea_pool_warmer.warmer_status() or {}
        # `warmer_status()` doesn't currently return the thread-liveness it
        # already computes internally; surface the actual thread state from
        # the warmer's module-level _thread so callers can distinguish
        # "started but dead" from "running normally" without forcing an
        # edit to idea_pool_warmer.py.
        try:
            t = getattr(idea_pool_warmer, "_thread", None)
            status["thread_alive"] = bool(t and t.is_alive())
            status["started"] = bool(getattr(idea_pool_warmer, "_thread_started", False))
        except Exception:
            status["thread_alive"] = None
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e), "running": False, "warmed_total": 0,
                        "started": False, "thread_alive": False}), 200


@app.route("/api/system/cache/stats")
def cache_stats():
    """Persistent-cache + warmer-thread diagnostics. Powers the future
    Settings → Cache panel; also handy for ad-hoc curl debugging."""
    out = {}
    try:
        import cache_store
        out["cache"] = cache_store.stats()
    except Exception as e:
        out["cache"] = {"error": str(e)}
    try:
        import cache_warmer
        status = cache_warmer.status() or {}
        # `status()` reports started=True forever once the boot path ran,
        # even if the thread later died (e.g. an exception escaped _loop).
        # Surface the actual thread liveness so the UI / curl probes can
        # distinguish "scheduled to run" from "still running".
        try:
            t = getattr(cache_warmer, "_thread", None)
            status["thread_alive"] = bool(t and t.is_alive())
        except Exception:
            status["thread_alive"] = None
        out["warmer"] = status
    except Exception as e:
        out["warmer"] = {"error": str(e)}
    return jsonify(out)


@app.route("/api/system/cache/clear", methods=["POST"])
def cache_clear():
    """Manual cache flush (used by a future Settings button)."""
    try:
        import cache_store
        n = cache_store.clear()
        return jsonify({"cleared": n})
    except Exception as e:
        return _err(e)


# ─── Research modules (v0.2.0) ──────────────────────────────────────
# All routes below live under /api/research/* and back the 10 new research
# panels added in v0.2.0. Each module is loaded in a guarded try/except so
# a single broken import can't take down the entire route table. See the
# INTEGRATE_*.md files at repo root for the spec each route implements.

try:
    import research_backtest
except Exception as _bt_err:
    research_backtest = None
    log.warning("research_backtest unavailable: %s", _bt_err)

try:
    import research_eventstudy
except Exception as _es_err:
    research_eventstudy = None
    log.warning("research_eventstudy unavailable: %s", _es_err)

try:
    import research_factors
except Exception as _ff_err:
    research_factors = None
    log.warning("research_factors unavailable: %s", _ff_err)

try:
    import research_hypothesis
except Exception as _rh_err:
    research_hypothesis = None
    log.warning("research_hypothesis unavailable: %s", _rh_err)

try:
    import research_iv_density
except Exception as _rnd_err:
    research_iv_density = None
    log.warning("research_iv_density unavailable: %s", _rnd_err)

try:
    import research_montecarlo as _mc_mod
except Exception as _mc_err:
    _mc_mod = None
    log.warning("research_montecarlo unavailable: %s", _mc_err)

try:
    import research_multihorizon
except Exception as _mh_err:
    research_multihorizon = None
    log.warning("research_multihorizon unavailable: %s", _mh_err)

try:
    import research_optimizer
except Exception as _opt_err:
    research_optimizer = None
    log.warning("research_optimizer unavailable: %s", _opt_err)

try:
    import research_probforecast as _pf_mod
except Exception as _pf_err:
    _pf_mod = None
    log.warning("research_probforecast unavailable: %s", _pf_err)

try:
    import forecast_ensemble as _ens_mod
except Exception as _ens_err:
    _ens_mod = None
    log.warning("forecast_ensemble unavailable: %s", _ens_err)

try:
    import research_tracker
    research_tracker.init_tracker_db()  # idempotent — creates signal_forecasts table
except Exception as _rt_err:
    research_tracker = None
    log.warning("research_tracker unavailable: %s", _rt_err)


# ── 1. Backtest ─────────────────────────────────────────────────────
@app.route("/api/research/backtest")
def research_backtest_route():
    if research_backtest is None:
        return jsonify({"error": "research_backtest module not available"}), 503
    symbol = (request.args.get("symbol") or "").upper()
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    signal_name = (request.args.get("signal") or "momentum").strip()
    signal_fn = research_backtest.get_adapter(signal_name)
    if signal_fn is None:
        return jsonify({
            "error": "Unknown signal '%s'. Available: %s" % (
                signal_name, ", ".join(research_backtest.ADAPTERS.keys()),
            ),
        }), 400
    start = request.args.get("start") or None
    end = request.args.get("end") or None
    horizon_override = _safe_int(request.args.get("horizon_override"), 0) or None
    try:
        result = research_backtest.run_backtest(
            signal_fn, symbol,
            start=start, end=end, horizon_override=horizon_override,
        )
        return jsonify(result)
    except Exception as e:
        log.exception("backtest failure")
        return _err(e)


# ── 2. Event Study ──────────────────────────────────────────────────
@app.route("/api/research/event-study/<symbol>")
def research_event_study_route(symbol):
    if research_eventstudy is None:
        return jsonify({"error": "research_eventstudy module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        event_type = request.args.get("event_type", "earnings")
        window_days = _safe_int(request.args.get("window_days"), 10)
        period_years = _safe_int(request.args.get("period_years"), 10)
        benchmark = request.args.get("benchmark", "SPY")
        return jsonify(research_eventstudy.event_study(
            symbol.upper(),
            event_type=event_type,
            window_days=window_days,
            period_years=period_years,
            benchmark=benchmark,
        ))
    except Exception as e:
        return _err(e)


# ── 3. Fama-French Factors ─────────────────────────────────────────
@app.route("/api/research/factors/<symbol>")
def research_factors_symbol(symbol):
    if not research_factors:
        return jsonify({"error": "research_factors module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        years = _safe_int(request.args.get("years"), 5)
        return jsonify(research_factors.factor_exposure(symbol.upper(), years))
    except Exception as e:
        return _err(e)


@app.route("/api/research/factors/portfolio", methods=["GET", "POST"])
def research_factors_portfolio():
    if not research_factors:
        return jsonify({"error": "research_factors module not available"}), 503
    try:
        years = _safe_int(request.args.get("years"), 5)
        holdings = []
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            holdings = body.get("holdings") or []
        if not holdings:
            try:
                rows = db.get_portfolio() or []
                # `db.get_portfolio()` doesn't compute market_value — that
                # only lives on /api/portfolio's enriched response. Use the
                # cost basis (shares * avg_cost) as the weighting fallback;
                # otherwise every holding would get weight=0 and the factor
                # model would return NaN/empty exposures.
                holdings = []
                for h in rows:
                    # The Fama-French model only prices equities/ETFs — exclude
                    # crypto/options so they don't skew the weights or contaminate
                    # the exposure estimate (mirrors the scan routes).
                    if (h.get("asset_type") or "stock") not in ("stock", "etf"):
                        continue
                    mv = (h.get("shares") or 0) * (h.get("avg_cost") or 0)
                    if mv > 0:
                        holdings.append({
                            "symbol": h["symbol"],
                            "market_value": float(mv),
                        })
            except Exception:
                holdings = []
        return jsonify(research_factors.portfolio_factor_exposure(holdings, years))
    except Exception as e:
        return _err(e)


# ── 4. Hypothesis Lab ───────────────────────────────────────────────
@app.route("/api/research/hypothesis/generate", methods=["POST"])
def api_hypothesis_generate():
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 503
    try:
        data = request.get_json(silent=True) or {}
        symbol = (data.get("symbol") or "").strip().upper()
        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        return jsonify(research_hypothesis.generate_hypothesis(symbol))
    except Exception as e:
        return _err(e)


@app.route("/api/research/hypothesis/save", methods=["POST"])
def api_hypothesis_save():
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 503
    try:
        data = request.get_json(silent=True) or {}
        symbol = (data.get("symbol") or "").strip().upper()
        hyp = data.get("hypothesis") or {}
        if not symbol or not isinstance(hyp, dict):
            return jsonify({"error": "symbol and hypothesis required"}), 400
        hid = research_hypothesis.save_hypothesis(
            hyp, symbol, source=data.get("source", "ai"),
        )
        return jsonify({"id": hid})
    except ValueError as e:
        # Validation failures keep their specific message as a client 400.
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        # Server faults (DB locked, disk full) must not masquerade as client
        # errors nor leak raw driver detail — same policy as sibling routes.
        return _err(e)


@app.route("/api/research/hypothesis", methods=["GET"])
def api_hypothesis_list():
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 503
    try:
        status = request.args.get("status")
        symbol = request.args.get("symbol")
        limit = _safe_int(request.args.get("limit"), 50)
        return jsonify(research_hypothesis.list_hypotheses(
            status=status, symbol=symbol, limit=limit,
        ))
    except Exception as e:
        return _err(e)


@app.route("/api/research/hypothesis/<int:hid>/status", methods=["POST"])
def api_hypothesis_set_status(hid):
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 503
    try:
        data = request.get_json(silent=True) or {}
        status = (data.get("status") or "").strip().upper()
        ok = research_hypothesis.update_hypothesis_status(hid, status)
        return jsonify({"ok": ok})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return _err(e)


@app.route("/api/research/hypothesis/<int:hid>/score", methods=["POST"])
def api_hypothesis_score(hid):
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 503
    try:
        return jsonify(research_hypothesis.score_hypothesis(hid))
    except Exception as e:
        return _err(e)


@app.route("/api/research/hypothesis/stats", methods=["GET"])
def api_hypothesis_stats():
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 503
    try:
        return jsonify(research_hypothesis.stats())
    except Exception as e:
        return _err(e)


# ── 5. IV Risk-Neutral Density ─────────────────────────────────────
@app.route("/api/research/rnd/<symbol>")
def research_rnd_default(symbol):
    if not research_iv_density:
        return jsonify({"error": "research_iv_density module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        return jsonify(research_iv_density.risk_neutral_density(symbol.upper()))
    except Exception as e:
        return _err(e)


@app.route("/api/research/rnd/<symbol>/<expiry>")
def research_rnd_with_expiry(symbol, expiry):
    if not research_iv_density:
        return jsonify({"error": "research_iv_density module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", expiry or ""):
        return jsonify({"error": "Expiry must be YYYY-MM-DD"}), 400
    try:
        return jsonify(research_iv_density.risk_neutral_density(symbol.upper(), expiry))
    except Exception as e:
        return _err(e)


# ── 6. Monte Carlo ──────────────────────────────────────────────────
def _mc_holdings_from_portfolio(account_id=None):
    """Return [{symbol, market_value}, ...] freshly priced from the DB."""
    try:
        rows = db.get_portfolio(account_id=account_id) if account_id is not None \
            else db.get_portfolio()
    except TypeError:
        rows = db.get_portfolio() or []
    if not rows:
        return []
    try:
        prices = _portfolio_live_prices(rows)
    except Exception:
        prices = {}
    out = []
    for h in rows:
        sym0 = h.get("symbol")
        if not sym0:
            continue
        q = prices.get(sym0) or {}
        px = q.get("price")
        # Fall back to cost basis when there's no live quote, mirroring the rest
        # of the app, instead of silently dropping the position (which understates
        # exposure). A genuinely non-positive value is still skipped.
        cur = px if px is not None else h.get("avg_cost")
        if not cur or cur <= 0:
            continue
        sh = h.get("shares")
        if not sh or sh <= 0:
            continue
        mv = cur * sh
        if mv <= 0:
            continue
        sym = sym0 + "-USD" if h.get("asset_type") == "crypto" else sym0
        out.append({"symbol": sym, "market_value": round(mv, 2)})
    return out


@app.route("/api/research/montecarlo", methods=["POST"])
def research_montecarlo_route():
    if not _mc_mod:
        return jsonify({"error": "research_montecarlo module not available"}), 503
    try:
        data = request.get_json(force=True, silent=True) or {}
        holdings = data.get("holdings") or []
        if not isinstance(holdings, list) or not holdings:
            return jsonify({"error": "holdings: non-empty list required"}), 400
        # Validate/normalize each entry before it reaches the simulation engine —
        # a stray non-dict or a non-numeric/negative market_value would otherwise
        # surface as an opaque 500 or silently skew the sim.
        clean = []
        for h in holdings:
            if not (isinstance(h, dict) and isinstance(h.get("symbol"), str) and h["symbol"].strip()):
                continue
            try:
                mv = float(h.get("market_value"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(mv) or mv <= 0:
                continue
            clean.append({"symbol": h["symbol"].strip().upper(), "market_value": mv})
        if not clean:
            return jsonify({"error": "holdings must be objects with symbol and positive market_value"}), 400
        holdings = clean
        # Clamp unbounded sim params — an attacker-supplied n_paths=1e9 would
        # peg a worker thread for minutes (DoS).
        horizon = min(max(_safe_int(data.get("horizon_days"), 252), 1), 3650)
        n_paths = min(max(_safe_int(data.get("n_paths"), 10000), 100), 50000)
        method = (data.get("method") or "historical_bootstrap").strip()
        seed = data.get("seed")
        if seed is not None:
            try:
                seed = int(seed)
            except (TypeError, ValueError):
                seed = None
        sim = _mc_mod.simulate_portfolio(
            holdings, n_paths=n_paths, horizon_days=horizon,
            method=method, seed=seed,
        )
        target = data.get("target_nav")
        if target is not None:
            try:
                pt = _mc_mod.prob_of_target(
                    holdings, float(target), horizon_days=horizon,
                    n_paths=n_paths, method=method, seed=seed,
                )
                sim["prob_of_target"] = pt
            except Exception as _pt_err:
                # The optional target-probability add-on runs a second full
                # simulation; any failure there must not discard the primary
                # `sim` the user actually requested.
                log.warning("prob_of_target failed: %s", _pt_err)
        return jsonify(sim)
    except Exception as e:
        return _err(e)


@app.route("/api/research/montecarlo/portfolio", methods=["GET"])
def research_montecarlo_portfolio():
    if not _mc_mod:
        return jsonify({"error": "research_montecarlo module not available"}), 503
    try:
        acct = request.args.get("account_id")
        holdings = _mc_holdings_from_portfolio(
            account_id=_safe_int(acct, None) if acct else None
        )
        if not holdings:
            return jsonify({"error": "No portfolio holdings available"}), 400
        horizon = min(max(_safe_int(request.args.get("horizon_days"), 252), 1), 3650)
        n_paths = min(max(_safe_int(request.args.get("n_paths"), 10000), 100), 50000)
        method = request.args.get("method") or "historical_bootstrap"
        sim = _mc_mod.simulate_portfolio(
            holdings, n_paths=n_paths, horizon_days=horizon, method=method,
        )
        return jsonify(sim)
    except Exception as e:
        return _err(e)


# ── 7. Multi-Horizon Forecast ──────────────────────────────────────
@app.route("/api/research/horizons/<symbol>")
def research_horizons(symbol):
    if not research_multihorizon:
        return jsonify({"error": "research_multihorizon module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        return jsonify(research_multihorizon.multi_horizon_forecast(symbol.upper()))
    except Exception as e:
        return _err(e)


# ── 8. Portfolio Optimizer ─────────────────────────────────────────
@app.route("/api/research/optimize", methods=["POST"])
def research_optimize():
    if not research_optimizer:
        return jsonify({"error": "research_optimizer module not available"}), 503
    try:
        body = request.get_json(silent=True) or {}
        symbols = [
            s.strip().upper() for s in (body.get("symbols") or [])
            if isinstance(s, str) and s.strip()
        ]
        if not symbols:
            return jsonify({"error": "symbols list is required"}), 400
        objective = (body.get("objective") or "max_sharpe").strip().lower()
        constraints = body.get("constraints") or {}
        current_weights = body.get("current_weights") or {}

        if objective == "risk_parity":
            result = research_optimizer.risk_parity(
                symbols, period=constraints.get("period", "2y"),
            )
        elif objective == "black_litterman":
            views = body.get("views") or []
            view_conf = body.get("view_confidence") or []
            mkt_caps = body.get("mkt_caps") or {}
            result = research_optimizer.black_litterman(
                symbols=symbols,
                mkt_caps=mkt_caps,
                views=views,
                view_confidence=view_conf,
                period=constraints.get("period", "2y"),
                risk_aversion=float(constraints.get("risk_aversion", 2.5)),
                tau=float(constraints.get("tau", 0.05)),
            )
        else:
            result = research_optimizer.markowitz_optimize(
                symbols, objective=objective, constraints=constraints,
            )

        if isinstance(result, dict) and "error" in result:
            return jsonify({"error": result["error"]}), 422

        if isinstance(current_weights, dict) and current_weights:
            try:
                cmp_out = research_optimizer.compare_to_current(
                    current_weights={k.upper(): float(v) for k, v in current_weights.items()},
                    optimal_weights=result["weights"],
                    period=constraints.get("period", "2y"),
                )
            except Exception as ce:
                log.warning("compare_to_current failed: %s", ce)
                cmp_out = {"delta": {}, "tracking_error_pct": None, "symbols": []}
        else:
            cmp_out = {"delta": {}, "tracking_error_pct": None, "symbols": []}

        return jsonify({"optimal": result, "compare": cmp_out})
    except Exception as e:
        log.exception("optimize failed")
        return _err(e)


# ── 9. Probabilistic Forecast ──────────────────────────────────────
@app.route("/api/research/probforecast/<symbol>", methods=["GET"])
def research_probforecast(symbol):
    if not _pf_mod:
        return jsonify({"error": "research_probforecast module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        # Clamp unbounded params — a huge n bootstrap or horizon would peg the
        # request thread (DoS).
        horizon = min(max(_safe_int(request.args.get("horizon"), 20), 1), 3650)
        n_boot = min(max(_safe_int(request.args.get("n"), 2000), 100), 20000)
        return jsonify(_pf_mod.prob_forecast(symbol.upper(), horizon_days=horizon, n_bootstrap=n_boot))
    except Exception as e:
        return _err(e)


@app.route("/api/research/probforecast/<symbol>/vs-point", methods=["GET"])
def research_probforecast_vs_point(symbol):
    if not _pf_mod:
        return jsonify({"error": "research_probforecast module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        horizon = min(max(_safe_int(request.args.get("horizon"), 20), 1), 3650)
        return jsonify(_pf_mod.compare_to_point(symbol.upper(), horizon_days=horizon))
    except Exception as e:
        return _err(e)


@app.route("/api/forecast/ensemble/<symbol>", methods=["GET"])
def forecast_ensemble_route(symbol):
    """Calibrated meta-forecast fusing all available forecasting signals
    into one directional probability + return cone."""
    if not _ens_mod:
        return jsonify({"error": "forecast_ensemble module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        horizon = min(max(_safe_int(request.args.get("horizon"), 20), 1), 3650)
        return jsonify(_ens_mod.ensemble_forecast(symbol.upper(), horizon_days=horizon))
    except Exception as e:
        return _err(e)


@app.route("/api/forecast/accountability", methods=["GET"])
def forecast_accountability_route():
    """Realized track record + Brier calibration for the ensemble, plus the
    per-component leaderboard and current adaptive weights."""
    try:
        import forecast_accountability
    except Exception as e:
        return jsonify({"error": "forecast_accountability unavailable"}), 503
    try:
        since = request.args.get("since")
        return jsonify(forecast_accountability.accountability_report(since=since))
    except Exception as e:
        return _err(e)


# ── Jarvis: unified assistant layer ────────────────────────────────
@app.route("/api/jarvis/lens/review/<symbol>", methods=["GET"])
def jarvis_lens_review_route(symbol):
    """Buffett-lens position review: business quality, valuation, basis."""
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        import jarvis_lens
        return jsonify(jarvis_lens.position_review(symbol.upper()))
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/lens/temperament", methods=["GET"])
def jarvis_lens_temperament_route():
    try:
        import jarvis_lens
        return jsonify(jarvis_lens.temperament_check())
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/lens/macro", methods=["GET"])
def jarvis_lens_macro_route():
    try:
        import jarvis_lens
        return jsonify(jarvis_lens.macro_brief())
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/tts", methods=["POST"])
def jarvis_tts_route():
    """Premium voice: synthesize speech through the user's OpenAI key.
    404 when keyless/capped/failed — the frontend falls back to browser
    speechSynthesis, so this endpoint is strictly an upgrade."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400
    text = data.get("text")
    if not text or not isinstance(text, str):
        return jsonify({"error": "expected JSON body with a 'text' string"}), 400
    # Cap text — OpenAI's TTS endpoint hard-rejects >4096 chars (and bills per
    # character), so reject oversized input rather than pay-then-fail.
    if len(text) > 4096:
        return jsonify({"error": "text too long (max 4096 characters)"}), 400
    try:
        import ai_summarizer
        _OK_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
        client_voice = data.get("voice")
        voice = client_voice or db.get_settings().get("jarvis_tts_voice") or "onyx"
        voice = str(voice).lower()[:20]
        if voice not in _OK_VOICES:
            if client_voice:
                # The client explicitly asked for an unknown voice → 400 (distinct
                # from the keyless 404 the frontend treats as "fall back to browser").
                return jsonify({"error": "unknown voice '{}'".format(voice)}), 400
            # The bad voice came from settings (a stale/non-allowlisted DB value):
            # fall back to the default instead of 400-ing every TTS call.
            voice = "onyx"
        audio = ai_summarizer.tts_speech(text, voice=voice)
        if not audio:
            return jsonify({"error": "tts unavailable"}), 404
        return Response(audio, mimetype="audio/mpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return _err(e)


@app.route("/api/version", methods=["GET"])
def api_version_route():
    """The running app version — lets UIs and health checks confirm what
    they're talking to."""
    return jsonify({"version": APP_VERSION})


# ─── AJTA trading agent (AJTA-SPEC-1.0) ──────────────────────────────────────
# Read routes + a local control plane. The control routes (kill/rearm/run/
# config/approve) are localhost operator actions, NOT model-driven; every
# order still passes the fail-closed risk gate (§11). Paper-first: all trade
# switches default false, live never auto-executes.

@app.route("/api/aj/status", methods=["GET"])
def aj_status_route():
    try:
        import aj_metrics
        return jsonify(aj_metrics.status())
    except Exception as e:
        return _err(e)


@app.route("/api/aj/config", methods=["GET", "POST"])
def aj_config_route():
    try:
        import aj_config
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            if not isinstance(data, dict):
                return jsonify({"error": "expected JSON object"}), 400
            return jsonify(aj_config.set_config(data))
        return jsonify(aj_config.get_config())
    except Exception as e:
        return _err(e)


def _aj_replays_base(create=False):
    """The replay artifacts dir, resolved robustly: env override first, then
    repo-relative, then beside the (resolved) DB — the packaged desktop app
    runs from a frozen bundle where __file__ is NOT the working repo."""
    import os as _os
    # An explicit env override wins UNCONDITIONALLY — filtering it through the
    # isdir check below silently redirected artifacts beside the DB whenever
    # the configured dir didn't exist yet, so tools watching AJ_REPLAYS_DIR
    # saw nothing.
    env_dir = _os.environ.get("AJ_REPLAYS_DIR") or ""
    if env_dir:
        if create:
            _os.makedirs(env_dir, exist_ok=True)
        return env_dir
    candidates = [
        _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                      "data", "replays"),
    ]
    try:
        import database as _dbm
        candidates.append(_os.path.join(
            _os.path.dirname(_dbm.DB_PATH), "data", "replays"))
        candidates.append(_os.path.join(
            _os.path.dirname(_os.path.realpath(_dbm.DB_PATH)),
            "data", "replays"))
    except Exception:
        pass
    base = next((c for c in candidates if c and _os.path.isdir(c)), None)
    if base is None:
        # prefer beside-the-resolved-DB for a fresh dir (always writable and
        # reachable from both the repo and the bundle); candidates[-1] is the
        # repo-relative path only when the DB lookup failed.
        base = candidates[-1]
        if create:
            _os.makedirs(base, exist_ok=True)
    return base


# One replay launch at a time from the UI; state lives here so /status can
# report progress without a DB round-trip (the replay uses its OWN db anyway).
_AJ_REPLAY_PROC = {"proc": None, "run_id": None, "started": None, "log": None}
# Serializes the poll-check + Popen + state update — two concurrent POSTs
# could otherwise both pass the poll() check, mint the same run_id, truncate
# each other's run.log and corrupt the shared replay.db/result.json.
_AJ_REPLAY_LOCK = threading.Lock()


@app.route("/api/aj/replay/run", methods=["POST"])
def aj_replay_run_route():
    """Launch a historical replay in a fresh, DB-isolated subprocess. The
    running app is never touched — artifacts land in the replays dir and the
    Replay Lab panel picks them up when done."""
    try:
        import os as _os
        import subprocess as _sp
        import re as _re
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        data = request.get_json(silent=True) or {}
        syms = [s.strip().upper() for s in
                str(data.get("symbols") or "").replace(" ", ",").split(",")
                if s.strip()]
        syms = [s for s in syms if _re.match(r"^[A-Z0-9.\-^]{1,10}$", s)]
        if not syms or len(syms) > 30:
            return jsonify({"error": "1-30 valid symbols required"}), 400
        end = str(data.get("end") or (_dt.now(_tz.utc) - _td(days=1)).strftime("%Y-%m-%d"))
        start = str(data.get("start") or (_dt.now(_tz.utc) - _td(days=730)).strftime("%Y-%m-%d"))
        if not (_re.match(r"^\d{4}-\d{2}-\d{2}$", start)
                and _re.match(r"^\d{4}-\d{2}-\d{2}$", end) and start < end):
            return jsonify({"error": "invalid start/end dates"}), 400
        try:
            cash = max(1000.0, min(1e8, float(data.get("cash") or 100000)))
        except (TypeError, ValueError):
            cash = 100000.0
        import aj_replay
        base = _aj_replays_base(create=True)
        # Hold the lock across check + launch + state update so concurrent
        # POSTs can't both pass the poll() check; %f makes run_id unique even
        # for back-to-back launches within the same second.
        with _AJ_REPLAY_LOCK:
            p = _AJ_REPLAY_PROC
            if p["proc"] is not None and p["proc"].poll() is None:
                return jsonify({"error": "a replay is already running",
                                "run_id": p["run_id"]}), 409
            run_id = "ui_" + _dt.now(_tz.utc).strftime("%Y%m%d_%H%M%S_%f")
            prefix, env = aj_replay.spawn_cmd_env(run_id, base)
            cache_dir = _os.path.join(_os.path.dirname(base), "replay_cache")
            cmd = prefix + ["run", "--run-id", run_id, "--out-dir", base,
                            "--cache-dir", cache_dir,
                            "--symbols", ",".join(syms),
                            "--start", start, "--end", end, "--cash", str(cash),
                            "--forecaster",
                            ("ensemble" if data.get("forecaster") == "ensemble"
                             else "pit")]
            log_path = _os.path.join(base, run_id, "run.log")
            logf = open(log_path, "w")
            proc = _sp.Popen(cmd, env=env, stdout=logf, stderr=_sp.STDOUT,
                             cwd=_os.path.dirname(base) or None)
            p.update({"proc": proc, "run_id": run_id, "log": log_path,
                      "started": _dt.now(_tz.utc).isoformat()})
        return jsonify({"launched": True, "run_id": run_id,
                        "symbols": syms, "start": start, "end": end,
                        "cash": cash})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/replay/status", methods=["GET"])
def aj_replay_status_route():
    try:
        import os as _os
        p = _AJ_REPLAY_PROC
        if p["proc"] is None:
            return jsonify({"running": False, "run_id": None})
        rc = p["proc"].poll()
        tail = ""
        try:
            if p["log"] and _os.path.exists(p["log"]):
                with open(p["log"]) as f:
                    tail = f.read()[-600:]
        except Exception:
            pass
        return jsonify({"running": rc is None, "run_id": p["run_id"],
                        "started": p["started"], "returncode": rc,
                        "log_tail": tail})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/lab", methods=["GET"])
def aj_lab_route():
    """Research Scientist status: experiment queue, recent verdicts with
    evidence, promotions/demotions, and the current deflated promotion bar."""
    try:
        import aj_lab
        return jsonify(aj_lab.status())
    except Exception as e:
        return _err(e)


@app.route("/api/aj/replays", methods=["GET"])
def aj_replays_route():
    """Index + latest details of stored replay-engine artifacts (data/replays).
    Read-only file reads — replays themselves always run out-of-process
    against isolated DBs (see aj_replay)."""
    try:
        import os as _os
        import json as _json
        base = _aj_replays_base()
        runs, grids = [], []
        latest, latest_mtime = None, -1.0
        if _os.path.isdir(base):
            for d in sorted(_os.listdir(base)):
                rp = _os.path.join(base, d, "result.json")
                gp = _os.path.join(base, d, "grid.json")
                if _os.path.exists(rp):
                    try:
                        with open(rp) as f:
                            r = _json.load(f)
                    except Exception:
                        continue
                    runs.append({"run_id": d, "start": r.get("start"),
                                 "end": r.get("end"),
                                 "total_return_pct": r.get("total_return_pct"),
                                 "benchmark_return_pct": r.get("benchmark_return_pct"),
                                 "alpha_pct": r.get("alpha_pct"),
                                 "sharpe": r.get("sharpe"),
                                 "max_drawdown_pct": r.get("max_drawdown_pct"),
                                 "trades": (r.get("trades") or {}).get("trades"),
                                 "forecaster": r.get("forecaster")})
                    m = _os.path.getmtime(rp)
                    if m > latest_mtime:
                        latest_mtime = m
                        # decimate the daily series so the payload stays small.
                        # ALWAYS keep the final point — daily[::step] drops it
                        # whenever (len-1) % step != 0, making the chart's
                        # terminal value disagree with total_return_pct.
                        def _decimate(series):
                            step = max(1, len(series) // 400)
                            dec = series[::step]
                            if series and (len(series) - 1) % step != 0:
                                dec.append(series[-1])
                            return dec
                        daily = r.get("daily") or []
                        bench = r.get("benchmark_daily") or []
                        latest = dict(runs[-1])
                        latest.update({
                            "daily": _decimate(daily),
                            "benchmark_daily": _decimate(bench),
                            "gate": r.get("gate"),
                            "metalabel": {"oos_auc": (r.get("metalabel") or {}).get("oos_auc")},
                            "labels_built": (r.get("labels") or {}).get("built"),
                            "win_rate": (r.get("trades") or {}).get("win_rate"),
                            "profit_factor": (r.get("trades") or {}).get("profit_factor"),
                            "caveats": r.get("caveats"),
                        })
                elif _os.path.exists(gp):
                    try:
                        with open(gp) as f:
                            g = _json.load(f)
                    except Exception:
                        continue
                    grids.append({
                        "grid_id": d, "start": g.get("start"), "end": g.get("end"),
                        "params": g.get("params"), "winner": g.get("winner"),
                        "train_frac": g.get("train_frac"),
                        "cells": [{k: c.get(k) for k in
                                   ("run_id", "params", "train_sharpe",
                                    "test_sharpe", "test_return_pct",
                                    "alpha_pct", "max_drawdown_pct")}
                                  for c in (g.get("cells") or [])],
                    })
        return jsonify({"runs": runs, "grids": grids, "latest": latest})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/trades/<path:symbol>", methods=["GET"])
def aj_trades_route(symbol):
    """Per-symbol trade history drill-down: every fill (with the proposal's
    thesis), the FIFO round-trips with realized P&L, and the current position.
    Answers 'did the agent actually buy this, and did the sell add up?'
    without scrolling the recent-orders table."""
    try:
        import re as _re
        import aj_db
        sym = str(symbol or "").upper().strip()
        # Length bound must fit the app's OWN option symbols
        # (OPT:AAPL:20260717:C:150.0 is 25 chars; 4-letter underliers and
        # 4-digit strikes routinely reach 26+) — 24 rejected most options.
        if not _re.match(r"^[A-Z0-9.:\-^_]{1,40}$", sym):
            return jsonify({"error": "invalid symbol"}), 400
        fills = aj_db.query(
            "SELECT f.id AS fill_id, f.order_id, o.side, f.qty, f.price, "
            "f.fees_usd, f.filled_at, o.state, o.proposal_id, p.thesis, "
            "p.risk_reason FROM aj_fills f "
            "JOIN aj_orders o ON f.order_id = o.id "
            "LEFT JOIN aj_proposals p ON o.proposal_id = p.id "
            "WHERE o.symbol = ? AND o.mode = 'paper' "
            "ORDER BY f.filled_at ASC, f.id ASC", (sym,))
        for f in fills:
            f["thesis"] = (f.get("thesis") or f.get("risk_reason") or "")[:160]
            f.pop("risk_reason", None)
        trips = []
        try:
            import aj_features
            for t in aj_features._round_trips("paper"):
                if (t.get("symbol") or "").upper() != sym:
                    continue
                ep, xp = t.get("entry_price"), t.get("exit_price")
                gain = None
                try:
                    if ep and xp and float(ep) > 0:
                        gain = round((float(xp) / float(ep) - 1.0) * 100.0, 2)
                except (TypeError, ValueError):
                    pass
                trips.append({"opened_at": t.get("opened_at"),
                              "closed_at": t.get("closed_at"),
                              "qty": t.get("qty"), "side": t.get("side"),
                              "entry_price": ep, "exit_price": xp,
                              "realized_pnl_usd": t.get("realized_pnl_usd"),
                              "gain_pct": gain})
        except Exception:
            pass
        position = None
        try:
            import aj_positions
            position = (aj_positions.paper_book().get("positions") or {}).get(sym)
        except Exception:
            pass
        return jsonify({"symbol": sym, "position": position,
                        "fills": fills, "round_trips": trips})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/proposals", methods=["GET"])
def aj_proposals_route():
    try:
        import aj_db
        limit = max(1, _safe_int(request.args.get("limit"), 50))
        return jsonify({"proposals": aj_db.query(
            "SELECT * FROM aj_proposals ORDER BY id DESC LIMIT ?", (limit,))})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/orders", methods=["GET"])
def aj_orders_route():
    try:
        import aj_db
        limit = max(1, _safe_int(request.args.get("limit"), 50))
        return jsonify({"orders": aj_db.query(
            "SELECT * FROM aj_orders ORDER BY id DESC LIMIT ?", (limit,))})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/audit", methods=["GET"])
def aj_audit_route():
    try:
        import aj_db
        limit = max(1, _safe_int(request.args.get("limit"), 100))
        rows = aj_db.query("SELECT * FROM aj_audit ORDER BY id DESC LIMIT ?", (limit,))
        return jsonify({"audit": rows, "chain": aj_db.verify_audit_chain()})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/kill", methods=["POST"])
def aj_kill_route():
    try:
        import aj_risk
        data = request.get_json(silent=True) or {}
        reason = str(data.get("reason") or "kill (web)")[:200]
        return jsonify(aj_risk.kill_switch(reason))
    except Exception as e:
        return _err(e)


@app.route("/api/aj/rearm", methods=["POST"])
def aj_rearm_route():
    try:
        import aj_risk
        return jsonify(aj_risk.rearm(actor="web"))
    except Exception as e:
        return _err(e)


@app.route("/api/aj/run", methods=["POST"])
def aj_run_route():
    """Trigger one operator cycle (§19). Paper-first; live proposes+gates only."""
    try:
        import aj_operator
        data = request.get_json(silent=True) or {}
        mode = "live" if str(data.get("mode")) == "live" else "paper"
        _stamps = _jarvis_act_charge(1)
        if not _stamps:
            return jsonify({"ok": False, "reason": "rate limited"}), 429
        try:
            result = aj_operator.run_once(mode)
        except Exception as e:
            # No cycle ran — refund the charged slot so transient run_once
            # failures don't silently drain the shared budget.
            _jarvis_act_refund(_stamps)
            return _err(e)
        # A single-instance lock refusal is not success — surface 409 so the UI
        # doesn't report "cycle complete" when the cycle never ran. Refund the
        # rate-limit slot since no cycle actually ran.
        if isinstance(result, dict) and result.get("ok") is False \
                and "running" in str(result.get("reason", "")):
            _jarvis_act_refund(_stamps)
            return jsonify(result), 409
        return jsonify(result)
    except Exception as e:
        return _err(e)


@app.route("/api/aj/recon", methods=["POST"])
def aj_recon_route():
    try:
        import aj_execution, aj_config
        return jsonify(aj_execution.reconcile(
            venue=aj_config.get_config().get("default_broker")))
    except Exception as e:
        return _err(e)


@app.route("/api/aj/proposals/<int:pid>/approve", methods=["POST"])
def aj_approve_route(pid):
    """Human-in-the-loop approval of a pending proposal (§19). Re-runs the
    risk gate at approval time (config may have changed) before executing —
    the gate, not this route, authorizes the order. Live orders execute only
    through the gated, VERIFY'd broker (which fails closed if not enabled)."""
    try:
        import aj_db, aj_risk, aj_execution
        _stamps = _jarvis_act_charge(1)
        if not _stamps:
            return jsonify({"error": "rate limited"}), 429
        row = aj_db.get_row("aj_proposals", pid)
        if not row:
            _jarvis_act_refund(_stamps)
            return jsonify({"error": "proposal not found"}), 404
        prev_status = row.get("status")
        if prev_status not in ("approved", "proposed"):
            _jarvis_act_refund(_stamps)
            return jsonify({"error": "proposal not approvable (status {})".format(prev_status)}), 400
        # Atomically claim the proposal before executing — two concurrent approval
        # POSTs for the same pid would otherwise both pass the status check and
        # both call execute_trade, double-submitting the order. The compare-and-set
        # on the exact status we just read guarantees exactly one claimer wins.
        if not aj_db.update_if("aj_proposals", pid, "status", prev_status, status="approving"):
            _jarvis_act_refund(_stamps)
            return jsonify({"error": "proposal already being approved"}), 409
        proposal = {"id": pid, "symbol": row["symbol"], "side": row["side"],
                    "qty": row.get("qty"), "notional_usd": row.get("notional_usd"),
                    "order_type": row.get("order_type") or "market",
                    "limit_price": row.get("limit_price"),
                    "account_id": row.get("account_id")}
        # Anything raising between the compare-and-set claim above and
        # completion (evaluate/audit/execute_trade) must release the claim —
        # a proposal stranded in 'approving' has NO recovery path anywhere
        # (it's neither 'approved' nor 'proposed', so every retry 400s
        # forever) and its rate-limit slot would never be refunded.
        try:
            rd = aj_risk.evaluate(proposal)
            if rd.get("decision") != "pass":
                aj_db.update("aj_proposals", pid, status="blocked", risk_reason=rd.get("reason"))
                _jarvis_act_refund(_stamps)
                return jsonify({"ok": False, "decision": rd.get("decision"), "reason": rd.get("reason")}), 400
            aj_db.audit("approval", {"proposal_id": pid, "mode": rd.get("mode"),
                                     "required": True, "decision": "human-approved"},
                        ref_id=pid, actor="human:web")
            ex = aj_execution.execute_trade(proposal, rd, cycle_id=row.get("cycle_id"))
        except Exception as e:
            try:
                aj_db.update_if("aj_proposals", pid, "status", "approving", status=prev_status)
            finally:
                _jarvis_act_refund(_stamps)
            return _err(e)
        # execute_trade flips status to 'executed' on a fill. If nothing filled,
        # release the claim back to its prior status so the proposal isn't left
        # permanently stuck in the intermediate 'approving' state.
        if not ex.get("ok"):
            aj_db.update_if("aj_proposals", pid, "status", "approving", status=prev_status)
        return jsonify({"ok": ex.get("ok", False), "exec": ex})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/analytics", methods=["GET"])
def aj_analytics_route():
    try:
        import aj_analytics
        return jsonify(aj_analytics.summary())
    except Exception as e:
        return _err(e)


@app.route("/api/aj/cycles", methods=["GET"])
def aj_cycles_route():
    """Per-cycle decision funnel + latest scan snapshot (Insights: funnel,
    activity timeline, selectivity scatter)."""
    try:
        import aj_db
        import json as _json
        aj_db.aj_init()
        limit = max(1, _safe_int(request.args.get("limit"), 40))
        rows = aj_db.query("SELECT * FROM aj_cycle_stats ORDER BY ts DESC LIMIT ?", (limit,))
        for r in rows:
            for k in ("result_json", "scan_json"):
                try:
                    r[k] = _json.loads(r.get(k) or ("{}" if k == "result_json" else "[]"))
                except Exception:
                    r[k] = {} if k == "result_json" else []
        latest_scan = rows[0]["scan_json"] if rows else []
        return jsonify({"cycles": rows, "latest_scan": latest_scan})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/signal_skill", methods=["GET"])
def aj_signal_skill_route():
    """Realized skill (IC / Brier-skill / hit-rate / n) + promotion verdict for
    every forecast signal (Insights: signal-skill dashboard)."""
    try:
        import aj_config, aj_ic
        cfg = aj_config.get_config()
        names = ["rf_classifier", "bootstrap", "ml_composite", "trend",
                 "mean_reversion", "narrative",
                 "smart_money", "insider", "congress", "social"]
        out = {}
        for n in names:
            try:
                skill = aj_ic.signal_skill(n)
                promo = aj_ic.signal_promoted(n, cfg)
                out[n] = {"skill": skill, "promoted": promo.get("promoted"),
                          "reason": promo.get("reason")}
            except Exception:
                out[n] = {"skill": None, "promoted": None, "reason": "unavailable"}
        return jsonify({"signals": out,
                        "gate_on": bool(cfg.get("signal_ic_gate", True)),
                        "multi_factor": bool(cfg.get("multi_factor_signals"))})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/allocation", methods=["GET"])
def aj_allocation_route():
    """Current vs target book weights + drift (Insights: exposure treemap)."""
    try:
        import aj_config, aj_allocate
        return jsonify(aj_allocate.rebalance_plan(aj_config.get_config()) or {})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/position/<symbol>", methods=["GET"])
def aj_position_detail_route(symbol):
    """Drill-down for one held name: entry thesis, current edge, stop/target,
    ladder rungs, time-stop status (Insights: position drill-down)."""
    try:
        import aj_db, aj_positions, aj_config
        aj_db.aj_init()
        sym = (symbol or "").upper()
        cfg = aj_config.get_config()
        book = (aj_positions.paper_book().get("positions") or {})
        pos = book.get(sym)
        out = {"symbol": sym, "position": pos}
        try:
            rows = aj_db.query(
                "SELECT created_at, thesis, notional_usd, side FROM aj_proposals "
                "WHERE symbol=? AND side='buy' ORDER BY id DESC LIMIT 1", (sym,))
            out["entry"] = rows[0] if rows else None
        except Exception:
            out["entry"] = None
        try:
            import forecast_ensemble
            fc = forecast_ensemble.ensemble_forecast(sym, int(cfg.get("forecast_horizon_days") or 20))
            ens = (fc or {}).get("ensemble") or {}
            out["forecast"] = {"prob_up": ens.get("prob_up"),
                               "edge_pct_pts": ens.get("edge_pct_pts"),
                               "conviction": ens.get("conviction")}
        except Exception:
            out["forecast"] = None
        if pos:
            try:
                import aj_risk, aj_execution_alpha
                mark = (aj_risk._marks([sym]) or {}).get(sym)
                out["mark"] = mark
                p2 = dict(pos); p2["symbol"] = sym
                out["time_stop"] = aj_execution_alpha.time_stop(p2, cfg, mark=mark,
                                                                opened_at=pos.get("opened_at"))
                out["profit_ladder"] = aj_execution_alpha.profit_ladder(p2, cfg, mark=mark)
                avg = float(pos.get("avg_cost") or 0)
                # pct == 0 is the aj_config default meaning DISABLED — emitting
                # avg*(1±0) would render both a TP and an SL at exactly
                # breakeven for an exit rule that isn't active.
                tp_pct = float(cfg.get("take_profit_pct", 0) or 0)
                sl_pct = float(cfg.get("stop_loss_pct", 0) or 0)
                out["levels"] = {
                    "take_profit": avg * (1 + tp_pct / 100.0) if (avg and tp_pct > 0) else None,
                    "stop_loss": avg * (1 - sl_pct / 100.0) if (avg and sl_pct > 0) else None,
                }
            except Exception:
                pass
        return jsonify(out)
    except Exception as e:
        return _err(e)


@app.route("/api/aj/backtest", methods=["GET"])
def aj_backtest_route():
    """Full-policy walk-forward expectancy report — does the agent's policy have
    a measured out-of-sample edge? (Insights: policy expectancy.)"""
    try:
        import aj_config, aj_backtest
        syms = request.args.get("symbols")
        symbols = [s.strip().upper() for s in syms.split(",") if s.strip()] if syms else None
        return jsonify(aj_backtest.policy_expectancy(aj_config.get_config(), symbols))
    except Exception as e:
        return _err(e)


@app.route("/api/aj/metalabel", methods=["GET"])
def aj_metalabel_route():
    """Meta-label model status: promotion verdict, OOS AUC, label counts/base
    rate (Insights: learned-edge dashboard)."""
    try:
        import aj_config, aj_metalabel
        return jsonify(aj_metalabel.status(aj_config.get_config()))
    except Exception as e:
        return _err(e)


@app.route("/api/aj/risk_governor", methods=["GET"])
def aj_risk_governor_route():
    """Portfolio Risk Governor: current global exposure multiplier G, the
    circuit-breaker flag, the reasons, and the component readings (Insights:
    risk-governor gauge)."""
    try:
        import aj_config, aj_risk_governor
        return jsonify(aj_risk_governor.status(aj_config.get_config()))
    except Exception as e:
        return _err(e)


@app.route("/api/aj/benchmark", methods=["GET"])
def aj_benchmark_route():
    """Agent day-over-day + cumulative return vs the leading indexes (SPY/QQQ/
    DIA/IWM): per-index alpha and days-beaten (Insights: benchmark panel)."""
    try:
        import aj_config, aj_benchmark
        days = max(5, _safe_int(request.args.get("days"), 90))
        return jsonify(aj_benchmark.benchmark(aj_config.get_config(), days))
    except Exception as e:
        return _err(e)


@app.route("/api/aj/council/<symbol>", methods=["GET"])
def aj_council_route(symbol):
    """Run the Analyst Council for one symbol (inspection). Advisory only — this
    never trades. Doubly gated: requires the VERIFY-COUNCIL gate (force=True
    still checks it), so it can't spend on the council without acknowledgement.
    Returns the decision + the per-analyst reports + debate transcript."""
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        import aj_db, aj_council, aj_config
        aj_db.aj_init()
        if not aj_config.council_verify_passed():
            return jsonify({"error": "VERIFY-COUNCIL gate not passed",
                            "hint": "aj_cli verify-set council --force"}), 403
        dec = aj_council.run(symbol.upper(), force=True)
        conn = db.get_conn()
        # S067: bind reports/turns to THIS run via the run_id carried on the
        # returned decision. The old "ORDER BY id DESC" re-query could attach a
        # concurrent run's rows. Fall back to that query only if run_id is None.
        run_id = getattr(dec, "run_id", None)
        if run_id is None:
            run = conn.execute(
                "SELECT id FROM aj_council_runs WHERE symbol=? ORDER BY id DESC LIMIT 1",
                (symbol.upper(),)).fetchone()
            run_id = run["id"] if run else None
        reports, turns = [], []
        if run_id is not None:
            reports = [dict(r) for r in conn.execute(
                "SELECT analyst, band, score, confidence, narrative FROM "
                "aj_analyst_reports WHERE council_run_id=? ORDER BY id", (run_id,)).fetchall()]
            turns = [dict(t) for t in conn.execute(
                "SELECT debate, role, round, content FROM aj_debate_turns "
                "WHERE council_run_id=? ORDER BY id", (run_id,)).fetchall()]
        return jsonify({"decision": dec.to_audit(), "reports": reports, "debate": turns})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/council/recent", methods=["GET"])
def aj_council_recent_route():
    """Recent council decisions (queryable copy) for the UI panel."""
    try:
        import aj_db
        aj_db.aj_init()
        conn = db.get_conn()
        rows = [dict(r) for r in conn.execute(
            "SELECT ts, cycle_id, symbol, status, rating, action, conviction, "
            "thesis, dissent, cost_usd, n_calls FROM aj_council_runs "
            "ORDER BY id DESC LIMIT 50").fetchall()]
        return jsonify({"runs": rows})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/positions", methods=["GET"])
def aj_positions_route():
    """The agent's current paper positions with per-stock analytics."""
    try:
        import aj_analytics
        return jsonify(aj_analytics.positions_detail())
    except Exception as e:
        return _err(e)


@app.route("/api/aj/journal.csv", methods=["GET"])
def aj_journal_route():
    try:
        import aj_analytics
        csv_text = aj_analytics.journal_csv()
        return Response(csv_text, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=aj_trade_journal.csv",
                                 "Cache-Control": "no-store"})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/preset", methods=["POST"])
def aj_preset_route():
    """Apply a risk/strategy preset (conservative|moderate|aggressive)."""
    try:
        import aj_config
        data = request.get_json(silent=True) or {}
        name = str(data.get("name") or "")
        result = aj_config.apply_preset(name)
        if result is None:
            return jsonify({"error": "unknown preset",
                            "presets": list(aj_config.PRESETS.keys())}), 400
        return jsonify({"ok": True, "preset": name, "config": result})
    except Exception as e:
        return _err(e)


@app.route("/api/aj/voice", methods=["POST"])
def aj_voice_route():
    """Spoken trading command (§18). Read intents run; high-risk actions return
    requires_approval and NEVER auto-execute."""
    try:
        import aj_voice
        data = request.get_json(silent=True) or {}
        return jsonify(aj_voice.handle_command(str(data.get("text") or "")[:300]))
    except Exception as e:
        return _err(e)


@app.route("/api/aj/mcp/tools", methods=["GET"])
def aj_mcp_tools_route():
    try:
        import aj_mcp_read
        return jsonify({"tools": aj_mcp_read.schemas(),
                        "contract_ok": aj_mcp_read.contract_ok()})
    except Exception as e:
        return _err(e)


# Rolling-window rate limit for confirmed Jarvis actions: timestamps of the
# last executions. A deque + GIL is enough fidelity for a single-user local
# app — this guards against a runaway client loop, not an adversary.
_JARVIS_ACT_TIMES = deque()
_JARVIS_ACT_MAX = 10     # executions…
_JARVIS_ACT_WINDOW = 60  # …per rolling window, seconds
# Two requests racing the check-prune-append could each read the deque under
# the limit and both append, overshooting MAX. Guard the whole sequence.
_JARVIS_ACT_LOCK = threading.Lock()
# Monotonic counter (held under the lock) used to make every charged stamp
# globally unique, so a refund's remove() can only ever delete the caller's
# own entries even when two charges land on the same time.time() value.
_JARVIS_ACT_SEQ = itertools.count()


def _jarvis_act_charge(n):
    """Atomically prune the rolling window and, if charging n executions keeps
    the total within _JARVIS_ACT_MAX, append n timestamps and return the list of
    appended timestamps (truthy). Otherwise return an empty list (rate limited,
    falsy). Both batch and single callers use this so the boundary (reject when
    total would exceed MAX) is identical. The returned stamps let a caller refund
    its OWN exact entries instead of popping whichever entry happens to be last."""
    now = time.time()
    with _JARVIS_ACT_LOCK:
        while _JARVIS_ACT_TIMES and now - _JARVIS_ACT_TIMES[0] > _JARVIS_ACT_WINDOW:
            _JARVIS_ACT_TIMES.popleft()
        if len(_JARVIS_ACT_TIMES) + n > _JARVIS_ACT_MAX:
            return []
        # Make each stamp globally distinct (now + a monotonic µs-scale offset)
        # so a concurrent refund's remove() can only delete the caller's OWN
        # entries — two charges landing on the same time.time() value would
        # otherwise let one refund delete the other's still-valid slot. The
        # 1e-6 step is large enough to survive float64 precision at epoch-second
        # magnitudes yet far below the rolling window, so the absolute-time
        # pruning at the top of this function is unaffected.
        stamps = [now + next(_JARVIS_ACT_SEQ) * 1e-6 for _ in range(n)]
        for t in stamps:
            _JARVIS_ACT_TIMES.append(t)
        return stamps


def _jarvis_act_refund(stamps):
    """Return previously-charged slots to the rolling window — used when a charged
    action turns out to be a no-op (e.g. the operator was already running), so
    polling a busy 'Run' doesn't burn the budget on 409 refusals. Accepts the list
    of timestamps returned by _jarvis_act_charge and removes those exact entries
    (so concurrent requests can't refund each other's slots). An int is also
    accepted for backward compatibility (pops that many most-recent entries)."""
    with _JARVIS_ACT_LOCK:
        if isinstance(stamps, int):
            for _ in range(stamps):
                if _JARVIS_ACT_TIMES:
                    _JARVIS_ACT_TIMES.pop()
            return
        for t in stamps or []:
            try:
                _JARVIS_ACT_TIMES.remove(t)
            except ValueError:
                pass


@app.route("/api/jarvis/act", methods=["POST"])
def jarvis_act_route():
    """Execute a user-CONFIRMED mutating action proposed by the Jarvis agent.
    Whitelisted against jarvis_tools' registry — read tools and unknown names
    are rejected, so this can only ever do what the registry defines."""
    try:
        import jarvis_tools
    except Exception as e:
        return jsonify({"error": "jarvis_tools unavailable"}), 503
    data = request.get_json(silent=True) or {}
    # A valid-JSON non-object body (array/string) would make `"actions" in
    # data` a substring test and .get() raise AttributeError (unhandled 500).
    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400
    # ── Batch form: {"actions": [{tool, args}, …]} (≤10) ───────────────────
    # Validated for shape here; per-action whitelisting/validation happens in
    # jarvis_tools.execute_mutating_batch so each result carries its own
    # ok/error. The rolling rate limiter charges ONE SLOT PER ACTION — if the
    # whole batch wouldn't fit, 429 before executing anything.
    if "actions" in data:
        actions = data.get("actions")
        if not isinstance(actions, list) or not actions or len(actions) > 10:
            return jsonify(
                {"error": "expected actions: non-empty list of at most "
                          "10 {tool, args} objects"}), 400
        for a in actions:
            if (not isinstance(a, dict) or not isinstance(a.get("tool"), str)
                    or not isinstance(a.get("args"), dict)):
                return jsonify(
                    {"error": "each action must be {tool: str, args: object}"}), 400
        batch_fn = getattr(jarvis_tools, "execute_mutating_batch", None)
        if batch_fn is None:
            return jsonify({"error": "batch execution unavailable"}), 500
        # Charge only actions that will actually EXECUTE (mirroring the single
        # path, which validates before charging). The executor re-checks the
        # exact same whitelist and rejects the rest without executing — a
        # fully-rejected batch must not burn the shared 10/60s budget.
        n_exec = sum(
            1 for a in actions
            if jarvis_tools.is_mutating(a["tool"])
            and jarvis_tools.valid_proposal_args(a["tool"], a["args"])
        )
        _stamps = _jarvis_act_charge(n_exec) if n_exec else []
        if n_exec and not _stamps:
            return jsonify({"error": "rate limited"}), 429
        try:
            r = batch_fn(actions)
        except Exception as e:
            # Nothing executed — refund the charged slots so a transient
            # executor failure doesn't permanently drain the shared budget.
            _jarvis_act_refund(_stamps)
            return _err(e)
        if not isinstance(r, dict):
            return jsonify({"error": "batch executor returned invalid result"}), 500
        return (jsonify(r), 400) if r.get("error") else jsonify(r)
    # ── Single-action form (unchanged) ─────────────────────────────────────
    tool = data.get("tool")
    args = data.get("args")
    if not isinstance(tool, str) or not isinstance(args, dict):
        return jsonify({"error": "expected {tool: str, args: object}"}), 400
    # Re-validate the args server-side — the payload is client-supplied, so
    # don't trust that a well-formed proposal produced it. Combined with the
    # registry whitelist (mutating-only, 3 benign tools) this bounds what a
    # crafted /act request can do.
    if not jarvis_tools.valid_proposal_args(tool, args):
        return jsonify({"error": "invalid or incomplete action arguments"}), 400
    # Rate limit counts EXECUTIONS (checked after validation) so malformed
    # requests can't burn the budget of legitimate confirmations. Same charge
    # helper as the batch path, so the MAX boundary is identical.
    _stamps = _jarvis_act_charge(1)
    if not _stamps:
        return jsonify({"error": "rate limited"}), 429
    try:
        r = jarvis_tools.execute_mutating(tool, args)
    except Exception as e:
        # Nothing executed — refund the charged slot so a transient failure
        # doesn't permanently drain the shared budget.
        _jarvis_act_refund(_stamps)
        return _err(e)
    if not isinstance(r, dict):
        return jsonify({"error": "tool returned invalid result"}), 500
    return (jsonify(r), 400) if r.get("error") else jsonify(r)


@app.route("/api/jarvis/briefing", methods=["GET"])
def jarvis_briefing_route():
    """Prioritized daily briefing: portfolio pulse, alerts, earnings,
    market regime, notable moves, concentration, fresh ideas."""
    try:
        import jarvis
    except Exception as e:
        return jsonify({"error": "jarvis unavailable"}), 503
    try:
        force = request.args.get("refresh") == "1"
        return jsonify(jarvis.get_briefing(force_refresh=force))
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/digest", methods=["GET"])
def jarvis_digest_route():
    """'While you were away' — insights logged since the last visit.
    ?mark_seen=0 peeks without advancing the watermark."""
    try:
        import jarvis
    except Exception as e:
        return jsonify({"error": "jarvis unavailable"}), 503
    mark = request.args.get("mark_seen", "1") != "0"
    try:
        return jsonify(jarvis.away_digest(mark_seen=mark))
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/digest/preview", methods=["GET"])
def jarvis_digest_preview_route():
    """Render the briefing digest (text + HTML) without delivering it."""
    try:
        import jarvis_delivery
    except Exception:
        return jsonify({"error": "delivery unavailable"}), 503
    try:
        return jsonify(jarvis_delivery.render_digest())
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/digest/send", methods=["POST"])
def jarvis_digest_send_route():
    """Deliver the digest now via the requested channels (notify/file/email).
    Body: {"channels": ["file","notify"]} — omit to use the configured set."""
    try:
        import jarvis_delivery
    except Exception:
        return jsonify({"error": "delivery unavailable"}), 503
    try:
        body = request.get_json(silent=True) or {}
        ch = body.get("channels")
        if isinstance(ch, str):
            ch = [c.strip() for c in ch.split(",") if c.strip()]
        return jsonify(jarvis_delivery.deliver_digest(channels=ch or None))
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/health", methods=["GET"])
def jarvis_health_route():
    """Jarvis self-diagnostics: LLM key/budget, warmer liveness, cache size."""
    try:
        import jarvis
        return jsonify(jarvis.health_snapshot())
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/conversation/export", methods=["GET"])
def jarvis_conversation_export_route():
    """Download the active conversation as a Markdown transcript."""
    try:
        conv_id = db.jarvis_active_conversation()
        msgs = db.jarvis_get_messages(conv_id, limit=200)
        lines = ["# JARVIS conversation #{}".format(conv_id), ""]
        for m in msgs:
            who = "**You**" if m.get("role") == "user" else "**JARVIS**"
            ts = (m.get("created_at") or "")[:16]
            lines.append("{} ({}):".format(who, ts))
            lines.append((m.get("content") or "").strip())
            lines.append("")
        resp = Response("\n".join(lines), mimetype="text/markdown")
        resp.headers["Content-Disposition"] = \
            "attachment; filename=jarvis-conversation-{}.md".format(conv_id)
        return resp
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/activity", methods=["GET"])
def jarvis_activity_route():
    """In-memory snapshot of background machinery for the activity panel."""
    try:
        import jarvis
        return jsonify(jarvis.activity_snapshot())
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/activity/stream", methods=["GET"])
def jarvis_activity_stream_route():
    """Server-Sent Events stream of activity_snapshot(). Emits the FIRST
    snapshot immediately (so test clients can read one chunk and close),
    then only on change (stable JSON signature over background+summary).
    Comment heartbeats (`: hb`) flow every ~15s so dead connections get
    reaped, and the generator hard-stops after 10 minutes — the browser's
    EventSource auto-reconnects — so reloads/shutdowns are never blocked
    by an immortal response thread."""
    try:
        import jarvis
    except Exception as e:
        return jsonify({"error": "jarvis unavailable"}), 503

    def generate():
        import time as _time
        import json as _json
        started = _time.time()
        last_sig = None
        last_yield = _time.time()
        sent_any = False
        err_streak = 0
        while _time.time() - started < 600:  # hard stop after 10 min
            try:
                snap = jarvis.activity_snapshot()
                err_streak = 0
                sig = _json.dumps(
                    {"background": snap.get("background"),
                     "summary": snap.get("summary")},
                    sort_keys=True, default=str)
                if sig != last_sig:
                    last_sig = sig
                    last_yield = _time.time()
                    sent_any = True
                    yield "data: {}\n\n".format(_json.dumps(snap, default=str))
                elif _time.time() - last_yield >= 15:
                    last_yield = _time.time()
                    yield ": hb\n\n"
            except Exception:
                # A persistently failing snapshot must not keep re-running (and
                # pinning a worker) for the full 10 min — stop after a short
                # streak; the browser's EventSource simply auto-reconnects.
                err_streak += 1
                # One bad snapshot must not kill the stream. The documented
                # contract is "first chunk is a data frame" — so on a FIRST
                # iteration error, emit an empty-but-valid data frame rather
                # than a bare comment heartbeat. (GeneratorExit is
                # BaseException: client disconnects still close normally.)
                if not sent_any:
                    last_sig = None
                    last_yield = _time.time()
                    sent_any = True
                    yield "data: {}\n\n".format(_json.dumps(
                        {"background": [], "summary": "Background status unavailable."}))
                elif _time.time() - last_yield >= 15:
                    last_yield = _time.time()
                    yield ": hb\n\n"
                if err_streak >= 5:
                    break
            _time.sleep(2)

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/jarvis/context/<view>", methods=["GET"])
def jarvis_context_route(view):
    """One contextual Jarvis line for the active view (?symbol= for research)."""
    try:
        import jarvis
    except Exception as e:
        return jsonify({"error": "jarvis unavailable"}), 503
    if not view or len(view) > 40 or not view.replace("-", "").isalnum():
        return jsonify({"error": "invalid view"}), 400
    symbol = request.args.get("symbol")
    if symbol and not _valid_ticker(symbol):
        symbol = None
    try:
        return jsonify(jarvis.view_context(view, symbol))
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/ask", methods=["POST"])
def jarvis_ask_route():
    """Natural-language Q&A routed to local engines (no API key needed)."""
    try:
        import jarvis
    except Exception as e:
        return jsonify({"error": "jarvis unavailable"}), 503
    data = request.get_json(silent=True) or {}
    query = data.get("query") if isinstance(data, dict) else None
    if not query or not isinstance(query, str):
        return jsonify({"error": "expected JSON body with a 'query' string"}), 400
    if len(query) > 500:
        return jsonify({"error": "query too long (max 500 chars)"}), 400
    history = data.get("history")  # optional explicit turns; jarvis clamps shape
    conversation_id = data.get("conversation_id")
    try:
        return jsonify(jarvis.ask(query, history=history,
                                  conversation_id=conversation_id))
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/ask/stream", methods=["POST"])
def jarvis_ask_stream_route():
    """Streaming ask: SSE-formatted frames over a POST fetch-stream. Emits
    `data: {"type":"status","text":…}` lines while Jarvis works, comment
    heartbeats on quiet stretches, and one terminal
    `data: {"type":"answer","payload":…}` frame. The generator is bounded
    (jarvis.ask_stream hard-deadlines), so no immortal response threads."""
    try:
        import jarvis
    except Exception as e:
        return jsonify({"error": "jarvis unavailable"}), 503
    data = request.get_json(silent=True) or {}
    query = data.get("query") if isinstance(data, dict) else None
    if not query or not isinstance(query, str):
        return jsonify({"error": "expected JSON body with a 'query' string"}), 400
    if len(query) > 500:
        return jsonify({"error": "query too long (max 500 chars)"}), 400
    history = data.get("history")
    conversation_id = data.get("conversation_id")

    def generate():
        import json as _json
        try:
            for ev in jarvis.ask_stream(query, history=history,
                                        conversation_id=conversation_id):
                if ev.get("type") == "hb":
                    yield ": hb\n\n"
                else:
                    yield "data: {}\n\n".format(_json.dumps(ev, default=str))
        except Exception as e:
            # Terminal error frame so the client always gets an answer event.
            yield "data: {}\n\n".format(_json.dumps(
                {"type": "answer",
                 "payload": {"intent": "error", "answer": str(e)}}))

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@app.route("/api/jarvis/conversation", methods=["GET"])
def jarvis_conversation_route():
    """Active conversation id + its recent messages (for palette resume)."""
    try:
        conv_id = db.jarvis_active_conversation()
        return jsonify({"conversation_id": conv_id,
                        "messages": db.jarvis_get_messages(conv_id, limit=12)})
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/conversation/new", methods=["POST"])
def jarvis_new_conversation_route():
    try:
        # Capture the outgoing thread BEFORE rotating so it can be summarized
        # in the background. summarize_thread may not exist yet (in-flight
        # module) — resolved via getattr inside the daemon thread, so the
        # response is never blocked and never fails on its account.
        try:
            old_id = db.jarvis_active_conversation()
        except Exception:
            old_id = None
        new_id = db.jarvis_new_conversation()
        if old_id is not None and old_id != new_id:
            def _summarize_old_thread(cid=old_id):
                try:
                    import jarvis
                    fn = getattr(jarvis, "summarize_thread", None)
                    if callable(fn):
                        fn(cid)
                except Exception:
                    log.exception("jarvis thread summarization failed")
            threading.Thread(target=_summarize_old_thread, daemon=True).start()
        return jsonify({"conversation_id": new_id})
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/memory", methods=["GET"])
def jarvis_memory_route():
    try:
        return jsonify({"memories": db.jarvis_list_memories()})
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/memory/<int:memory_id>", methods=["DELETE"])
def jarvis_memory_delete_route(memory_id):
    try:
        ok = db.jarvis_delete_memory(memory_id)
        return jsonify({"status": "deleted" if ok else "not found"})
    except Exception as e:
        return _err(e)


# ─── Jarvis Watches (standing conditions, evaluated by the snapshot loop) ────

@app.route("/api/jarvis/watches", methods=["GET"])
def jarvis_watches_list_route():
    """All standing watches with their armed/triggered state."""
    try:
        import jarvis_watches
    except Exception as e:
        return jsonify({"error": "jarvis_watches unavailable"}), 503
    try:
        return jsonify({"watches": jarvis_watches.list_watches()})
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/watches", methods=["POST"])
def jarvis_watches_add_route():
    """Create a watch: {name: str, conditions: …} — shape of conditions is
    owned by jarvis_watches; we only insist both fields are present."""
    try:
        import jarvis_watches
    except Exception as e:
        return jsonify({"error": "jarvis_watches unavailable"}), 503
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400
    name = data.get("name")
    conditions = data.get("conditions")
    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "expected a non-empty 'name' string"}), 400
    if conditions is None:
        return jsonify({"error": "expected 'conditions'"}), 400
    try:
        r = jarvis_watches.add_watch(name.strip(), conditions)
    except Exception as e:
        return _err(e)
    if not isinstance(r, dict):
        return jsonify({"error": "add_watch returned invalid result"}), 500
    return (jsonify(r), 400) if r.get("error") else jsonify(r)


@app.route("/api/jarvis/watches/<int:watch_id>", methods=["DELETE"])
def jarvis_watches_delete_route(watch_id):
    try:
        import jarvis_watches
    except Exception as e:
        return jsonify({"error": "jarvis_watches unavailable"}), 503
    try:
        ok = jarvis_watches.delete_watch(watch_id)
    except Exception as e:
        return _err(e)
    if not ok:
        return jsonify({"error": "watch not found"}), 404
    return jsonify({"status": "deleted"})


@app.route("/api/jarvis/watches/<int:watch_id>/rearm", methods=["POST"])
def jarvis_watches_rearm_route(watch_id):
    try:
        import jarvis_watches
    except Exception as e:
        return jsonify({"error": "jarvis_watches unavailable"}), 503
    try:
        ok = jarvis_watches.rearm_watch(watch_id)
    except Exception as e:
        return _err(e)
    if not ok:
        return jsonify({"error": "watch not found"}), 404
    return jsonify({"status": "rearmed"})


# ─── Jarvis Policy (portfolio guardrail rules) ───────────────────────────────

@app.route("/api/jarvis/policy", methods=["GET"])
def jarvis_policy_get_route():
    """Current rules + human description + live violations. Violations are
    individually guarded — a broken portfolio check must not hide the rules."""
    try:
        import jarvis_policy
    except Exception as e:
        return jsonify({"error": "jarvis_policy unavailable"}), 503
    try:
        rules = jarvis_policy.get_rules()
        description = jarvis_policy.describe_rules()
        try:
            violations = jarvis_policy.check_portfolio()
        except Exception:
            log.exception("jarvis_policy.check_portfolio failed")
            violations = []
        return jsonify({"rules": rules, "description": description,
                        "violations": violations})
    except Exception as e:
        return _err(e)


@app.route("/api/jarvis/policy", methods=["POST"])
def jarvis_policy_set_route():
    """Set a rule — either structured {kind, value} or a natural phrase
    {text: …} routed through parse_rule (unparseable → 400)."""
    try:
        import jarvis_policy
    except Exception as e:
        return jsonify({"error": "jarvis_policy unavailable"}), 503
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "expected JSON object"}), 400
    kind = data.get("kind")
    value = data.get("value")
    text = data.get("text")
    if isinstance(text, str) and text.strip():
        try:
            parsed = jarvis_policy.parse_rule(text.strip())
        except Exception as e:
            return _err(e)
        if not isinstance(parsed, dict) or parsed.get("error") \
                or not parsed.get("kind"):
            return jsonify({"error": "could not parse rule from text"}), 400
        kind, value = parsed.get("kind"), parsed.get("value")
    if not isinstance(kind, str) or not kind.strip() or value is None:
        return jsonify(
            {"error": "expected {kind, value} or {text: natural phrase}"}), 400
    try:
        r = jarvis_policy.set_rule(kind.strip(), value)
    except Exception as e:
        return _err(e)
    if not isinstance(r, dict):
        return jsonify({"error": "set_rule returned invalid result"}), 500
    return (jsonify(r), 400) if r.get("error") else jsonify(r)


@app.route("/api/jarvis/policy/<kind>", methods=["DELETE"])
def jarvis_policy_delete_route(kind):
    try:
        import jarvis_policy
    except Exception as e:
        return jsonify({"error": "jarvis_policy unavailable"}), 503
    if not kind or len(kind) > 60:
        return jsonify({"error": "invalid rule kind"}), 400
    try:
        r = jarvis_policy.remove_rule(kind)
    except Exception as e:
        return _err(e)
    if r is False:
        return jsonify({"error": "rule not found"}), 404
    return jsonify({"status": "removed"})


# ── 10. Signal Tracker ─────────────────────────────────────────────
@app.route("/api/research/track/<signal_name>")
def research_track_record(signal_name):
    if not research_tracker:
        return jsonify({"error": "research_tracker module not available"}), 503
    if not signal_name or not signal_name.replace("_", "").isalnum():
        return jsonify({"error": "Invalid signal name"}), 400
    try:
        since = request.args.get("since")
        symbol = request.args.get("symbol")
        return jsonify(research_tracker.get_track_record(
            signal_name, since=since, symbol=symbol,
        ))
    except Exception as e:
        return _err(e)


@app.route("/api/research/track/<signal_name>/calls")
def research_track_calls(signal_name):
    if not research_tracker:
        return jsonify({"error": "research_tracker module not available"}), 503
    if not signal_name or not signal_name.replace("_", "").isalnum():
        return jsonify({"error": "Invalid signal name"}), 400
    try:
        limit = max(1, _safe_int(request.args.get("limit"), 20))
        calls = research_tracker.get_recent_calls(signal_name, limit=limit)
        return jsonify({"calls": calls, "signal_name": signal_name})
    except Exception as e:
        return _err(e)


@app.route("/api/research/track/_score", methods=["POST"])
def research_track_score_now():
    if not research_tracker:
        return jsonify({"error": "research_tracker module not available"}), 503
    try:
        return jsonify(research_tracker.score_due_forecasts())
    except Exception as e:
        return _err(e)


# ─── Synthesis modules (v0.3.0) ─────────────────────────────────────
# Cross-source synthesis layer that fuses signals across the existing
# research / signals / market modules. Each module is read-only with
# respect to existing files; all per-module caching is handled inside
# each `synth_*` module via cache_store.coalesce. Routes are grouped
# under /api/synth/* and guarded so a single broken import can't take
# down the rest of the table.

try:
    import synth_bayessmart
except Exception as _bs_err:
    synth_bayessmart = None
    log.warning("synth_bayessmart unavailable: %s", _bs_err)

try:
    import synth_catalyst
except Exception as _sc_err:
    synth_catalyst = None
    log.warning("synth_catalyst unavailable: %s", _sc_err)

try:
    import synth_cluster
except Exception as _scl_err:
    synth_cluster = None
    log.warning("synth_cluster unavailable: %s", _scl_err)

try:
    import synth_consensus
except Exception as _scs_err:
    synth_consensus = None
    log.warning("synth_consensus unavailable: %s", _scs_err)

try:
    import synth_divmap
except Exception as _dm_err:
    synth_divmap = None
    log.warning("synth_divmap unavailable: %s", _dm_err)

try:
    import synth_groundhyp
except Exception as _sgh_err:
    synth_groundhyp = None
    log.warning("synth_groundhyp unavailable: %s", _sgh_err)

try:
    import synth_macrotranslate
except Exception as _mt_err:
    synth_macrotranslate = None
    log.warning("synth_macrotranslate unavailable: %s", _mt_err)

try:
    import synth_peerdiv
except Exception as _pd_err:
    synth_peerdiv = None
    log.warning("synth_peerdiv unavailable: %s", _pd_err)

try:
    import synth_sectorflow
except Exception as _sf_err:
    synth_sectorflow = None
    log.warning("synth_sectorflow unavailable: %s", _sf_err)

try:
    import synth_whatif
except Exception as _wif_err:
    synth_whatif = None
    log.warning("synth_whatif unavailable: %s", _wif_err)


# ── 1. Bayesian-reweighted Smart-Money composite ────────────────────
@app.route("/api/synth/bayes-smart-money/<symbol>")
def synth_bayes_smart_money_route(symbol):
    if not synth_bayessmart:
        return jsonify({"error": "synth_bayessmart module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        result = synth_bayessmart.bayes_smart_money(symbol.upper())
        try:
            synth_bayessmart.log_bayes_smart_money(symbol.upper(), result)
        except Exception:
            pass
        return jsonify(result)
    except Exception as e:
        return _err(e)


# ── 2. Catalyst Timeline ────────────────────────────────────────────
@app.route("/api/synth/catalyst", methods=["GET"])
def synth_catalyst_get():
    if synth_catalyst is None:
        return jsonify({"error": "synth_catalyst module not available"}), 503
    days_ahead = _safe_int(request.args.get("days_ahead"), 60)
    try:
        return jsonify(synth_catalyst.catalyst_timeline(symbols=None, days_ahead=days_ahead))
    except Exception as e:
        return _err(e)


@app.route("/api/synth/catalyst", methods=["POST"])
def synth_catalyst_post():
    if synth_catalyst is None:
        return jsonify({"error": "synth_catalyst module not available"}), 503
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    syms = body.get("symbols") or []
    if not isinstance(syms, list):
        return jsonify({"error": "`symbols` must be an array"}), 400
    syms = [str(s).strip().upper() for s in syms if str(s).strip()]
    syms = [s for s in syms if _valid_ticker(s)][:25]
    days_ahead = _safe_int(body.get("days_ahead"), 60)
    try:
        return jsonify(synth_catalyst.catalyst_timeline(symbols=syms, days_ahead=days_ahead))
    except Exception as e:
        return _err(e)


# ── 3. Cluster Scan ─────────────────────────────────────────────────
@app.route("/api/synth/cluster-scan", methods=["GET"])
def synth_cluster_scan_get():
    if not synth_cluster:
        return jsonify({"error": "synth_cluster module not available"}), 503
    direction = (request.args.get("direction") or "bullish").lower()
    if direction not in ("bullish", "bearish"):
        direction = "bullish"
    min_sources = _safe_int(request.args.get("min_sources"), 4)
    universe_param = request.args.get("universe") or "sp500_top100"
    if universe_param == "sp500_top100":
        universe = None
    else:
        universe = [s.strip().upper() for s in universe_param.split(",") if s.strip()]
    try:
        return jsonify(synth_cluster.cluster_scan(
            universe=universe, direction=direction, min_sources=min_sources,
        ))
    except Exception as e:
        return _err(e)


@app.route("/api/synth/cluster-scan", methods=["POST"])
def synth_cluster_scan_post():
    if not synth_cluster:
        return jsonify({"error": "synth_cluster module not available"}), 503
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    direction = (body.get("direction") or "bullish").lower()
    if direction not in ("bullish", "bearish"):
        direction = "bullish"
    min_sources = _safe_int(body.get("min_sources"), 4)
    universe = body.get("universe")
    if universe is not None and not isinstance(universe, list):
        return jsonify({"error": "universe must be a list of symbols"}), 400
    try:
        return jsonify(synth_cluster.cluster_scan(
            universe=universe, direction=direction, min_sources=min_sources,
        ))
    except Exception as e:
        return _err(e)


# ── 4. Cross-source Consensus ───────────────────────────────────────
@app.route("/api/synth/consensus/<symbol>")
def synth_consensus_route(symbol):
    if not synth_consensus:
        return jsonify({"error": "synth_consensus module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        return jsonify(synth_consensus.consensus_score(symbol.upper()))
    except Exception as e:
        return _err(e)


# ── 5. Divergence Map ───────────────────────────────────────────────
@app.route("/api/synth/divergence-map", methods=["GET", "POST"])
def synth_divergence_map_route():
    if not synth_divmap:
        return jsonify({"error": "synth_divmap module not available"}), 503
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"error": "expected JSON object"}), 400
        universe = body.get("universe")
        # Mirror the GET branch's string handling — a raw string is treated by
        # synth_divmap._universe_to_list as an unknown label and silently
        # falls back to scanning the full SP500 top-100 (the exact browser-
        # timeout failure documented below).
        if isinstance(universe, str):
            if universe == "sp500_top100":
                universe = None
            else:
                universe = [s.strip().upper() for s in universe.split(",") if s.strip()]
        elif universe is not None and not isinstance(universe, list):
            return jsonify({"error": "universe must be a list of symbols"}), 400
        top_n = _safe_int(body.get("top_n"), 20)
    else:
        # GET accepts the literal "sp500_top100" label OR a comma-separated
        # symbol list. Without this split, `?universe=AAPL,MSFT` is treated
        # as an unknown label inside synth_divmap._universe_to_list and
        # silently falls back to scanning the entire SP500 top-100 — which
        # times out every browser request.
        universe_param = request.args.get("universe") or "sp500_top100"
        if universe_param == "sp500_top100":
            universe = None
        else:
            universe = [s.strip().upper() for s in universe_param.split(",") if s.strip()]
        top_n = _safe_int(request.args.get("top_n"), 20)
    try:
        return jsonify(synth_divmap.divergence_map(universe=universe, top_n=top_n))
    except Exception as e:
        return _err(e)


# ── 6. Pattern-Grounded Hypothesis ──────────────────────────────────
@app.route("/api/synth/grounded-hypothesis/<symbol>", methods=["POST"])
def synth_grounded_hypothesis_route(symbol):
    if synth_groundhyp is None:
        return jsonify({"error": "synth_groundhyp module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        return jsonify(synth_groundhyp.grounded_hypothesis(symbol.upper()))
    except Exception as e:
        log.exception("grounded_hypothesis failed")
        return _err(e)


# ── 7. Cross-Asset Macro Translator ─────────────────────────────────
@app.route("/api/synth/macrotranslate/releases")
def synth_macrotranslate_catalog():
    if not synth_macrotranslate:
        return jsonify({"error": "synth_macrotranslate module not available"}), 503
    try:
        return jsonify({"releases": synth_macrotranslate.supported_releases()})
    except Exception as e:
        return _err(e)


@app.route("/api/synth/macrotranslate/<release_id>")
def synth_macrotranslate_get(release_id):
    if not synth_macrotranslate:
        return jsonify({"error": "synth_macrotranslate module not available"}), 503
    surprise = request.args.get("surprise")
    try:
        surprise_pct = float(surprise) if surprise not in (None, "") else None
    except (TypeError, ValueError):
        surprise_pct = None
    # float() parses 'nan'/'inf'; NaN mis-buckets as a large positive surprise
    # (all comparisons False) and serializes as invalid-JSON literal NaN.
    if surprise_pct is not None and not math.isfinite(surprise_pct):
        return jsonify({"error": "surprise must be a finite number"}), 400
    try:
        return jsonify(synth_macrotranslate.macro_translate(release_id, surprise_pct=surprise_pct))
    except Exception as e:
        return _err(e)


@app.route("/api/synth/macrotranslate/<release_id>/portfolio", methods=["POST"])
def synth_macrotranslate_portfolio(release_id):
    if not synth_macrotranslate:
        return jsonify({"error": "synth_macrotranslate module not available"}), 503
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    holdings = body.get("holdings") or []
    surprise_pct = body.get("surprise_pct")
    if not holdings:
        # Fall back to the current portfolio so a no-body POST works. Weight by
        # LIVE market value (price * shares), not cost basis — macro impact
        # attribution should reflect what positions are worth now, mirroring the
        # Monte Carlo helper. Fall back to avg_cost only when no live quote.
        try:
            rows = db.get_portfolio() or []
            try:
                prices = _portfolio_live_prices(rows)
            except Exception:
                prices = {}
            holdings = []
            for h in rows:
                sym0 = h.get("symbol")
                if not sym0:
                    continue
                px = (prices.get(sym0) or {}).get("price")
                px = px if px is not None else h.get("avg_cost")
                sh = h.get("shares") or 0
                mv = (px or 0) * sh
                if mv > 0:
                    holdings.append({"symbol": sym0, "market_value": float(mv)})
        except Exception:
            holdings = []
    try:
        surprise_pct = float(surprise_pct) if surprise_pct not in (None, "") else None
    except (TypeError, ValueError):
        surprise_pct = None
    # Same NaN/inf hole as the GET route above — reject non-finite values.
    if surprise_pct is not None and not math.isfinite(surprise_pct):
        return jsonify({"error": "surprise_pct must be a finite number"}), 400
    try:
        return jsonify(synth_macrotranslate.macro_translate(
            release_id, surprise_pct=surprise_pct, portfolio_holdings=holdings))
    except Exception as e:
        return _err(e)


# ── 8. Peer Divergence ──────────────────────────────────────────────
@app.route("/api/synth/peerdiv/<symbol>")
def synth_peerdiv_route(symbol):
    if not synth_peerdiv:
        return jsonify({"error": "synth_peerdiv module not available"}), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    n = _safe_int(request.args.get("n"), 5)
    n = max(1, min(n, 12))
    try:
        return jsonify(synth_peerdiv.peer_divergence(symbol.upper(), n))
    except Exception as e:
        return _err(e)


# ── 9. Sector Flow ──────────────────────────────────────────────────
@app.route("/api/synth/sectorflow")
def synth_sectorflow_route():
    if synth_sectorflow is None:
        return jsonify({"error": "synth_sectorflow module not available"}), 503
    try:
        return jsonify(synth_sectorflow.sector_flow())
    except Exception as e:
        log.exception("sectorflow failure")
        return _err(e)


# ── 10. Portfolio What-If ───────────────────────────────────────────
@app.route("/api/synth/whatif", methods=["POST"])
def synth_whatif_post():
    if not synth_whatif:
        return jsonify({"error": "synth_whatif module not available"}), 503
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "expected JSON object"}), 400
    current = body.get("current_holdings") or []
    candidate = body.get("candidate") or {}
    if not isinstance(current, list) or not isinstance(candidate, dict):
        return jsonify({"error": "expected JSON {current_holdings:[...], candidate:{...}}"}), 400
    try:
        return jsonify(synth_whatif.whatif(current, candidate))
    except Exception as e:
        log.exception("synth_whatif failed")
        return _err(e)


@app.route("/api/synth/whatif", methods=["GET"])
def synth_whatif_get():
    """Convenience GET that pulls current_holdings from the DB and accepts
    candidate parameters via query string (?symbol=...&market_value=...&action=...)."""
    if not synth_whatif:
        return jsonify({"error": "synth_whatif module not available"}), 503
    sym = (request.args.get("symbol") or "").strip().upper()
    try:
        mv = float(request.args.get("market_value") or 0.0)
    except (TypeError, ValueError):
        mv = 0.0
    # float() parses 'nan'/'inf'; NaN compares False to every relation so it
    # slips past `mv < 0` and then poisons every downstream weight.
    if not math.isfinite(mv):
        return jsonify({"error": "market_value must be a finite number"}), 400
    action = (request.args.get("action") or "add").lower()
    if not sym or mv < 0:
        return jsonify({"error": "need ?symbol=...&market_value=...&action=add|remove|resize_to"}), 400
    if not _valid_ticker(sym):
        return jsonify({"error": "Invalid symbol"}), 400
    if action not in ("add", "remove", "resize_to"):
        return jsonify({"error": "action must be add|remove|resize_to"}), 400
    acct = request.args.get("account_id")
    try:
        holdings_raw = db.get_portfolio(account_id=int(acct) if acct and acct.isdigit() else None)
        enriched = []
        for h in holdings_raw or []:
            if not h.get("symbol"):
                continue
            mv_h = (h.get("shares") or 0) * (h.get("avg_cost") or 0)
            if mv_h <= 0:
                continue
            enriched.append({
                "symbol": h["symbol"],
                "market_value": float(mv_h),
                "asset_type": h.get("asset_type", "stock"),
                "shares": h.get("shares"),
                "avg_cost": h.get("avg_cost"),
            })
        return jsonify(synth_whatif.whatif(enriched, {
            "symbol": sym, "market_value": mv, "action": action,
        }))
    except Exception as e:
        return _err(e)


def _start_idea_warmer(debug_mode: bool = False):
    """Boot the pre-warmer thread (idempotent). Skip during the Flask
    reloader's parent process — otherwise both parent and child would each
    spawn a warmer and hammer SQLite + upstream APIs in parallel.

    Werkzeug sets WERKZEUG_RUN_MAIN="true" only in the child after a reload;
    in the parent the variable is unset. When not under the reloader at all
    (production / py2app), `debug_mode=False` and we always start.
    """
    if debug_mode and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        # Reloader parent — child will start the warmer when it spawns.
        log.info("Skipping idea-pool warmer start in reloader parent process")
        return
    if os.environ.get("DISABLE_WARMER") == "1":
        log.info("Idea pool warmer disabled via DISABLE_WARMER=1")
        return
    try:
        import idea_pool_warmer
        idea_pool_warmer.start_warmer_thread(target_count=200, interval_seconds=6 * 3600)
        log.info("Idea pool warmer started (target=200, every 6h)")
    except Exception as e:
        log.warning("Idea pool warmer failed to start: %s", e)


if __name__ == "__main__":
    # macOS reserves port 5000 for AirPlay Receiver — use 5001
    port = int(os.environ.get("PORT", 5001))
    print("\n" + "=" * 60)
    print("  AUGUR // WEALTH INTELLIGENCE SYSTEM")
    print(f"  http://localhost:{port}")
    print("=" * 60 + "\n")
    # debug_mode=True here matches the `debug=True` passed to app.run below;
    # the warmer guard inside _start_idea_warmer() needs to know we're under
    # the Werkzeug reloader to suppress the start in the parent process.
    _start_idea_warmer(debug_mode=True)
    app.run(debug=True, host="127.0.0.1", port=port)
