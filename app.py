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

from flask import Flask, jsonify, request, render_template, abort, Response
import database as db
import fetcher
import json
import os
import io
import csv
import logging
import re
import threading
import time
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

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")

def _safe_int(val, default):
    """Parse an int from query-string input; return default on bad input."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

def _valid_ticker(symbol):
    return isinstance(symbol, str) and bool(_TICKER_RE.match(symbol.strip().upper()))

def _utc_now():
    return datetime.now(timezone.utc)

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
else:
    log.info("Skipping cache_warmer start in reloader parent process")


# ─── Portfolio Snapshot Background Thread ─────────────────────────────────────

def _snapshot_worker():
    """Takes a portfolio snapshot every 5 minutes but only writes once per day."""
    while True:
        try:
            holdings = db.get_portfolio()
            if holdings:
                stock_syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"]
                crypto_syms = [h["symbol"] for h in holdings if h["asset_type"] == "crypto"]
                prices = {}
                if stock_syms:
                    prices.update(fetcher.get_quotes_batch(stock_syms))
                if crypto_syms:
                    crypto_yf = [s + "-USD" for s in crypto_syms]
                    crypto_prices = fetcher.get_quotes_batch(crypto_yf)
                    for sym in crypto_syms:
                        key = (sym + "-USD").upper()
                        if key in crypto_prices:
                            prices[sym] = crypto_prices[key]

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
                today = _utc_now().strftime("%Y-%m-%d")
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
            prices = prices if 'prices' in locals() else {}

        # ── Check price alerts (reuse already-fetched prices) ──────────────
        try:
            active_alerts = db.get_price_alerts(include_triggered=False)
            if active_alerts:
                alert_syms = list({a["symbol"] for a in active_alerts})
                missing = [s for s in alert_syms if s not in prices]
                if missing:
                    prices.update(fetcher.get_quotes_batch(missing))
                for alert in active_alerts:
                    cur = (prices.get(alert["symbol"]) or {}).get("price")
                    if cur is None:
                        continue
                    hit = (alert["alert_type"] == "above" and cur >= alert["price"]) or \
                          (alert["alert_type"] == "below" and cur <= alert["price"])
                    if hit:
                        db.mark_alert_triggered(alert["id"])
        except Exception:
            log.exception("snapshot worker: alert check failed")

        time.sleep(300)  # 5 minutes


# Guard against Flask reloader double-start
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
    _t = threading.Thread(target=_snapshot_worker, daemon=True)
    _t.start()

# ─── Serve UI ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


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
    symbols = [s for s in symbols if _valid_ticker(s)]
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
    limit = _safe_int(request.args.get("limit"), 15)
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
    limit = _safe_int(request.args.get("limit"), 50)
    return jsonify(fetcher.get_crypto_market(limit))


@app.route("/api/crypto/global")
def crypto_global():
    return jsonify(fetcher.get_crypto_global())


@app.route("/api/crypto/chart/<coin_id>")
def crypto_chart(coin_id):
    days = _safe_int(request.args.get("days"), 30)
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
    holdings = db.get_portfolio(account_id=_safe_int(acct_filter, None) if acct_filter else None)
    if not holdings:
        return jsonify({"holdings": [], "summary": {}})

    # Enrich with live prices
    stock_syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"]
    crypto_syms = [h["symbol"] for h in holdings if h["asset_type"] == "crypto"]

    prices = {}
    if stock_syms:
        prices.update(fetcher.get_quotes_batch(stock_syms))

    # For crypto, use yfinance with -USD suffix fallback
    if crypto_syms:
        crypto_yf = [s + "-USD" for s in crypto_syms]
        crypto_prices = fetcher.get_quotes_batch(crypto_yf)
        for sym in crypto_syms:
            key = (sym + "-USD").upper()
            if key in crypto_prices:
                prices[sym] = crypto_prices[key]

    enriched = []
    total_value = 0
    total_cost = 0

    for h in holdings:
        sym = h["symbol"]
        q = prices.get(sym, {})
        current_price = q.get("price")
        if current_price:
            market_value = current_price * h["shares"]
            cost_basis = h["avg_cost"] * h["shares"]
            unrealized_pnl = market_value - cost_basis
            unrealized_pct = (unrealized_pnl / cost_basis * 100) if cost_basis else 0
            total_value += market_value
            total_cost += cost_basis
            day_chg = q.get("change")
            h.update({
                "current_price": round(current_price, 4),
                "market_value": round(market_value, 2),
                "cost_basis": round(cost_basis, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "unrealized_pct": round(unrealized_pct, 2),
                "day_change": day_chg,
                "day_change_pct": q.get("change_pct"),
                "day_pnl": round(day_chg * h["shares"], 2) if day_chg is not None else None,
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
    acct_id = _safe_int(data.get("account_id"), None) if data.get("account_id") else None
    row_id = db.add_position(
        symbol=data["symbol"],
        name=data.get("name", ""),
        shares=shares,
        avg_cost=avg_cost,
        asset_type=data.get("asset_type", "stock"),
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
        date=data.get("date"),
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
    ok = db.update_position(
        pos_id,
        shares=shares,
        avg_cost=avg_cost,
        notes=data.get("notes"),
        account_id=acct_id,
    )
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
    symbols = [i["symbol"] for i in items]
    prices = fetcher.get_quotes_batch(symbols)
    for item in items:
        q = prices.get(item["symbol"], {})
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
    is_new = db.add_to_watchlist(
        symbol=data["symbol"],
        name=data.get("name", ""),
        asset_type=data.get("asset_type", "stock"),
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
    limit = _safe_int(request.args.get("limit"), 100)
    return jsonify(db.get_transactions(symbol=symbol, limit=limit))


@app.route("/api/transactions/summary")
def transactions_summary():
    """Aggregate KPIs over ALL transactions (no row-limit). Used for the
    Transactions view's headline strip so totals stay accurate when the
    table itself only pages in the latest N rows."""
    # Pull every transaction. The per-row payload is tiny (8 numeric/string
    # fields) so this is fine for realistic personal-portfolio sizes.
    txns = db.get_transactions(limit=10**9)
    total_buy = 0.0
    total_sell = 0.0
    for t in txns:
        try:
            total = float(t.get("total") or 0)
        except (TypeError, ValueError):
            total = 0.0
        action = (t.get("action") or "").upper()
        if action == "BUY":
            total_buy += total
        elif action == "SELL":
            total_sell += total
    return jsonify({
        "total_buy": total_buy,
        "total_sell": total_sell,
        "count": len(txns),
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
    db.add_transaction(
        symbol=data["symbol"],
        action=data["action"],
        shares=shares,
        price=price,
        fees=fees,
        date=data.get("date"),
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
    # Get first snapshot value to normalize benchmark to same starting value
    snapshots = db.get_snapshots()
    base_value = snapshots[0]["total_value"] if snapshots else None
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
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "symbol", "action", "shares", "price", "total", "fees", "notes"])
    for t in txns:
        writer.writerow([
            t.get("date", ""), t.get("symbol", ""), t.get("action", ""), t.get("shares", ""),
            t.get("price", ""), t.get("total", ""), t.get("fees", 0), t.get("notes", ""),
        ])
    output.seek(0)
    return Response(
        output.getvalue(),
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
    headers = [h.strip().strip('"').lower() for h in (reader.fieldnames or [])]

    imported = 0
    skipped = 0
    errors = []

    # Detect format by sniffing headers
    def _get(row, *keys):
        for k in keys:
            for h in row:
                if h.strip().strip('"').lower() == k.lower():
                    val = row[h]
                    if val and str(val).strip() not in ("", "--", "N/A"):
                        return str(val).strip()
        return None

    for i, row in enumerate(reader):
        try:
            # Normalize keys
            norm = {k.strip().strip('"').lower(): v for k, v in row.items()}

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

            if not symbol or not re.match(r"^[A-Z0-9.\-]{1,10}$", symbol):
                skipped += 1
                continue
            if shares is None or shares <= 0:
                skipped += 1
                continue
            if avg_cost is None or avg_cost <= 0:
                avg_cost = 0.0  # Allow import with zero cost

            db.add_position(
                symbol=symbol,
                name=name,
                shares=shares,
                avg_cost=avg_cost,
                asset_type="stock",
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
    stock_syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"]
    crypto_syms = [h["symbol"] for h in holdings if h["asset_type"] == "crypto"]
    prices = {}
    if stock_syms:
        prices.update(fetcher.get_quotes_batch(stock_syms))
    if crypto_syms:
        crypto_yf = [s + "-USD" for s in crypto_syms]
        crypto_prices = fetcher.get_quotes_batch(crypto_yf)
        for sym in crypto_syms:
            key = (sym + "-USD").upper()
            if key in crypto_prices:
                prices[sym] = crypto_prices[key]

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
    for k, v in data.items():
        # The UI echoes masked placeholders back on save — writing one
        # through would clobber the real key with "••••XaQA".
        if k in api_keys.SENSITIVE_SETTINGS and api_keys.is_masked(v):
            continue
        db.set_setting(k, v)
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
    symbols = list({a["symbol"] for a in alerts})
    prices = fetcher.get_quotes_batch(symbols) if symbols else {}
    for a in alerts:
        q = prices.get(a["symbol"], {})
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
    symbol    = (data.get("symbol", "") or "").upper().strip()
    alert_type = data.get("alert_type", "above")   # "above" or "below"
    try:
        price = float(data.get("price", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid price"}), 400
    # price must be a positive number — `not price` rejected a legitimate 0 and
    # let negatives through (creating an alert that can never trigger).
    if not symbol or price <= 0 or alert_type not in ("above", "below"):
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

    # Get live prices for yield-on-cost
    syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"]
    prices = fetcher.get_quotes_batch(syms) if syms else {}

    # Parallelize per-symbol yfinance dividend roundtrips. Without this,
    # 30 positions = 30 sequential network calls. yfinance 1.2.x is
    # thread-safe with fast_info.
    equity_syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"]
    div_map = {}
    if equity_syms:
        # safe_executor.parallel_map preserves input order (like pool.map
        # did) and fills a slot with None if a fetch raises, which the
        # `or {}` below absorbs instead of 500-ing the route.
        for sym, dd in zip(equity_syms,
                           safe_executor.parallel_map(
                               fetcher.get_dividend_data, equity_syms,
                               max_workers=8, thread_name_prefix="div-batch")):
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
            "shares": h["shares"],
            "avg_cost": h["avg_cost"],
            "market_value": round(market_value, 2),
            "annual_income": round(annual_income, 2),
            "yield_on_cost": yoc,
            "income_weight": 0,   # filled below
        })

    # Fill income weights
    for r in results:
        r["income_weight"] = round(r["annual_income"] / total_annual_income * 100, 1) if total_annual_income else 0

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

    holdings = db.get_portfolio()
    if not holdings:
        return jsonify({"error": "No positions in portfolio"}), 400

    # Enrich with live market values
    syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"]
    crypto_syms = [h["symbol"] for h in holdings if h["asset_type"] == "crypto"]
    prices = {}
    if syms:
        prices.update(fetcher.get_quotes_batch(syms))
    if crypto_syms:
        cp = fetcher.get_quotes_batch([s + "-USD" for s in crypto_syms])
        for s in crypto_syms:
            # get_quotes_batch returns UPPER-cased keys; match accordingly so a
            # lowercase-stored crypto symbol doesn't silently miss its live price.
            k = (s + "-USD").upper()
            if k in cp:
                prices[s] = cp[k]

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
    # Filter to stocks only (skip crypto, ETFs without earnings)
    stock_symbols = [
        s for s in symbols
        if not any(h["symbol"] == s and h["asset_type"] == "crypto" for h in holdings)
    ]

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
        return jsonify({"error": str(e), "symbol": symbol}), 500

    # Generate AI brief
    brief = ai_summarizer.generate_earnings_brief(dossier, model=ai_model)
    dossier["brief"] = brief

    # Cache it
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

    # Enrich with live prices (same logic as get_portfolio)
    stock_syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"]
    crypto_syms = [h["symbol"] for h in holdings if h["asset_type"] == "crypto"]
    prices = {}
    if stock_syms:
        prices.update(fetcher.get_quotes_batch(stock_syms))
    if crypto_syms:
        crypto_yf = [s + "-USD" for s in crypto_syms]
        cp = fetcher.get_quotes_batch(crypto_yf)
        for sym in crypto_syms:
            key = (sym + "-USD").upper()
            if key in cp:
                prices[sym] = cp[key]

    total_value = 0
    total_cost = 0
    enriched = []
    for h in holdings:
        sym = h["symbol"]
        q = prices.get(sym, {})
        price = q.get("price")
        cost_basis = h["avg_cost"] * h["shares"]
        market_value = price * h["shares"] if price is not None else cost_basis
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
            "current_price": round(price, 4) if price else None,
            "market_value": round(market_value, 2),
            "cost_basis": round(cost_basis, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_pct": round(unrealized_pct, 2),
            "day_change_pct": q.get("change_pct"),
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

    for symbol in symbols[:15]:  # cap at 15 symbols to avoid long waits
        try:
            filings = edgar.get_recent_filings(
                symbol, forms=["8-K", "10-K", "10-Q", "S-1"], limit=5
            )
        except Exception:
            continue
        for f in filings:
            acc = f["accession"]
            cached = db.get_cached_filing(acc) if not refresh else None
            if cached:
                result.append({
                    "ticker": f["ticker"],
                    "form_type": f["form_type"],
                    "filing_date": f["filing_date"],
                    "description": f["description"],
                    "accession": acc,
                    "signal": cached.get("ai_signal", "NEUTRAL"),
                    "summary": cached.get("ai_summary", ""),
                    "key_points": cached.get("ai_key_points", []),
                    "event_type": cached.get("ai_event_type", ""),
                    "ai_powered": bool(cached.get("ai_powered")),
                    "filing_url": f["document_url"],
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
                ai_result = pair[1] if pair else {}
                result.append({
                        "ticker": f["ticker"],
                        "form_type": f["form_type"],
                        "filing_date": f["filing_date"],
                        "description": f["description"],
                        "accession": f["accession"],
                        "signal": ai_result.get("signal", "NEUTRAL"),
                        "summary": ai_result.get("summary", ""),
                        "key_points": ai_result.get("key_points", []),
                        "event_type": ai_result.get("event_type", ""),
                        "ai_powered": bool(ai_result.get("ai_powered")),
                        "filing_url": f["document_url"],
                    })
        else:
            # No AI requested — return raw filing metadata, empty summary
            # fields. Don't cache here; let an ai_powered=1 call populate
            # the cache later.
            for f in uncached_filings:
                result.append({
                    "ticker": f["ticker"],
                    "form_type": f["form_type"],
                    "filing_date": f["filing_date"],
                    "description": f["description"],
                    "accession": f["accession"],
                    "signal": "NEUTRAL",
                    "summary": "",
                    "key_points": [],
                    "event_type": "",
                    "ai_powered": False,
                    "filing_url": f["document_url"],
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

        # Try to find primary document from index
        index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{acc_nodash}-index.json"
        try:
            import requests as req
            resp = req.get(index_url, headers=edgar.EDGAR_HEADERS, timeout=15)
            idx_data = resp.json()
            primary_doc = None
            for doc in idx_data.get("documents", []):
                if doc.get("type") == form_type or str(doc.get("sequence")) == "1":
                    primary_doc = doc.get("document", "")
                    break
            if not primary_doc and idx_data.get("documents"):
                primary_doc = idx_data["documents"][0].get("document", "")
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
        return jsonify({"error": str(e)}), 500


@app.route("/api/intel/insiders/<symbol>")
def intel_insiders(symbol):
    """Get Form 4 insider transactions for a symbol."""
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
        return jsonify({"error": str(e), "transactions": [], "pattern": {}}), 500


@app.route("/api/intel/institutional")
def intel_institutional():
    """Returns holdings for all tracked funds."""
    holdings = db.get_portfolio()
    portfolio_symbols = set(h["symbol"].upper() for h in holdings)

    result = {}
    for fund_name, fund_cik in edgar.TRACKED_FUNDS.items():
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
                    # Try to match by checking if any portfolio symbol appears in holding name
                    for sym in portfolio_symbols:
                        if sym in h_name or h_name.startswith(sym):
                            overlap.append({**h, "portfolio_symbol": sym})
                            break

                result[fund_name] = {
                    "filing_date": fund_data.get("filing_date", ""),
                    "period_of_report": fund_data.get("period_of_report", fund_data.get("period", "")),
                    "total_value": fund_data.get("total_value", 0),
                    "holdings": fund_data.get("holdings", [])[:50],
                    "overlap_with_portfolio": overlap,
                    "num_holdings": len(fund_data.get("holdings", [])),
                }
            else:
                result[fund_name] = {
                    "error": fund_data.get("error", "Unknown error") if fund_data else "No data",
                    "holdings": [],
                    "overlap_with_portfolio": [],
                }
        except Exception as e:
            result[fund_name] = {"error": str(e), "holdings": [], "overlap_with_portfolio": []}

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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


@app.route("/api/smart-money/scores", methods=["POST"])
def smart_money_scores_bulk():
    """Compute Smart Money scores for multiple symbols."""
    import smart_money
    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols", [])
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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


@app.route("/api/options-flow/scan", methods=["POST"])
def options_flow_scan():
    """Scan unusual options activity for portfolio symbols."""
    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols", [])
    if not symbols:
        holdings = db.get_portfolio()
        symbols = list({h["symbol"] for h in holdings if h["asset_type"] == "stock"})
    if not symbols:
        return jsonify({"results": []})
    try:
        results = fetcher.scan_unusual_options_portfolio(symbols[:15])
        return jsonify({"results": results, "scanned": len(symbols)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Congressional Trading Intelligence ─────────────────────────────────────────

@app.route("/api/congress/trades")
def congress_trades():
    """Get recent congressional trading activity summary."""
    import congress
    days = _safe_int(request.args.get("days"), 90)
    max_pdfs = _safe_int(request.args.get("max_pdfs"), 60)
    try:
        result = congress.get_congress_summary(days=days, max_pdfs=max_pdfs)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/congress/trades/<symbol>")
def congress_trades_symbol(symbol):
    """Get recent congressional trades for a specific ticker."""
    import congress
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    days = _safe_int(request.args.get("days"), 180)
    try:
        trades = congress.get_trades_for_ticker(symbol.upper(), days=days)
        return jsonify({"trades": trades, "symbol": symbol.upper()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/terminal", methods=["POST"])
def terminal_exec():
    """Execute a CLI command and return ANSI output."""
    data = request.get_json(force=True, silent=True) or {}
    command = (data.get("command") or "").strip()
    if not command:
        return jsonify({"output": "", "error": "No command provided"}), 400

    # Block dangerous commands
    blocked = ("serve", "rm ", "sudo", "del ", "os.", "import ", "eval", "exec")
    if any(command.lower().startswith(b) or f" {b}" in command.lower() for b in blocked):
        return jsonify({"output": "\033[31m  Command not allowed in web terminal.\033[0m\n", "exit_code": 1})

    import cli as cli_mod
    try:
        output, exit_code = cli_mod.run_command(command)
        return jsonify({"output": output, "exit_code": exit_code})
    except Exception as e:
        return jsonify({"output": f"\033[31m  Error: {e}\033[0m\n", "exit_code": 1})


@app.route("/api/smart-money/ml-forecast/<symbol>")
def ml_forecast_route(symbol):
    """ML-based predictive analytics for a symbol."""
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
        return jsonify({"error": str(e)}), 500


# ─── GEX (Gamma Exposure) Flow Predictor ────────────────────────────────────

@app.route("/api/gex/<symbol>")
def gex_analysis(symbol):
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
        return jsonify({"error": str(e)}), 500


@app.route("/api/gex/summary/<symbol>")
def gex_summary(symbol):
    import gex_engine
    try:
        result = gex_engine.get_gex_summary(symbol.upper())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Corporate Contagion Graph ──────────────────────────────────────────────

@app.route("/api/contagion/<symbol>")
def contagion_graph_route(symbol):
    import contagion_graph
    try:
        result = contagion_graph.build_graph(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/contagion/impact/<symbol>")
def contagion_impact(symbol):
    import contagion_graph
    event_type = request.args.get("event", "earnings_miss")
    try:
        result = contagion_graph.assess_contagion(symbol.upper(), event_type=event_type)
        if "error" in result:
            return jsonify(result), 400
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Narrative Velocity Engine ──────────────────────────────────────────────

@app.route("/api/narrative/<symbol>")
def narrative_analysis(symbol):
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
        return jsonify({"error": str(e)}), 500


# ─── Synthetic Insider Composite ────────────────────────────────────────────

@app.route("/api/synthetic-insider/<symbol>")
def synthetic_insider_route(symbol):
    import synthetic_insider
    try:
        result = synthetic_insider.compute_composite(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/synthetic-insider/scan", methods=["POST"])
def synthetic_insider_scan():
    import synthetic_insider
    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols", [])
    if not symbols:
        holdings = db.get_portfolio()
        symbols = list(set(h["symbol"] for h in holdings if h.get("asset_type") != "crypto"))
    if not symbols:
        return jsonify({"results": []})
    try:
        results = synthetic_insider.scan_composite_bulk(symbols[:15])
        return Response(json.dumps({"results": results}, default=str), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Reflexivity Detector ──────────────────────────────────────────────────

@app.route("/api/reflexivity/<symbol>")
def reflexivity_detect(symbol):
    import reflexivity_detector
    try:
        result = reflexivity_detector.detect_loops(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


# ─── Alt-Data Revenue Nowcasting ───────────────────────────────────────────

@app.route("/api/alt-data/<symbol>")
def alt_data_nowcast(symbol):
    import alt_data_engine
    try:
        result = alt_data_engine.nowcast_revenue(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/alt-data/scan", methods=["POST"])
def alt_data_scan():
    import alt_data_engine
    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols", [])
    if not symbols:
        holdings = db.get_portfolio()
        symbols = list(set(h["symbol"] for h in holdings if h.get("asset_type") != "crypto"))
    if not symbols:
        return jsonify({"results": []})
    try:
        results = alt_data_engine.nowcast_bulk(symbols[:15])
        return Response(json.dumps({"results": results}, default=str), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
    limit = _safe_int(request.args.get("limit"), 100)
    return jsonify({"symbol": symbol.upper(), "history": db.get_scan_history(symbol=symbol, limit=limit)})


# ════════════════════════════════════════════════════════════════════
#   NEW DATA SOURCES — Tier 1 (no key) + Tier 3 (better wrappers)
# ════════════════════════════════════════════════════════════════════

# ── Congress: Senate + combined ──────────────────────────────────
@app.route("/api/congress/senate")
def congress_senate():
    """Senate STOCK Act trades from senate-stock-watcher-data."""
    symbol = request.args.get("symbol")
    limit = _safe_int(request.args.get("limit"), 200)
    return jsonify({"trades": ds.get_senate_trades(symbol=symbol, limit=limit)})


@app.route("/api/congress/all")
def congress_all():
    """Combined House + Senate trades, sorted newest first."""
    symbol = request.args.get("symbol")
    limit = max(1, _safe_int(request.args.get("limit"), 200))  # clamp: -1 would slice off the last row
    senate = ds.get_senate_trades(symbol=symbol, limit=limit)
    try:
        import congress as house_mod
        house_trades = house_mod.get_congress_summary(days=120, max_pdfs=40)
        house_rows = []
        for member in house_trades.get("members", []):
            for t in member.get("trades", []):
                tk = (t.get("ticker") or "").upper()
                if symbol and tk != symbol.upper():
                    continue
                house_rows.append({
                    "chamber": "House",
                    "name": member.get("name"),
                    "ticker": tk,
                    "asset_description": t.get("asset_description") or "",
                    "type": t.get("transaction_type") or "",
                    "amount": t.get("amount") or "",
                    "date": t.get("transaction_date") or "",
                    "filed": member.get("filing_date") or "",
                    "ptr_link": member.get("pdf_url") or "",
                })
    except Exception as e:
        log.debug("house combined: %s", e)
        house_rows = []
    combined = senate + house_rows
    combined.sort(key=lambda r: (r.get("date") or "", r.get("filed") or ""), reverse=True)
    return jsonify({
        "trades": combined[:limit],
        "senate_count": len(senate),
        "house_count": len(house_rows),
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
    limit = _safe_int(request.args.get("limit"), 20)
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
        return jsonify({"error": "fred_data module not available"}), 500
    return jsonify(fred_data.snapshot())


@app.route("/api/macro/fred/catalog")
def fred_catalog():
    if not fred_data:
        return jsonify({"error": "fred_data module not available"}), 500
    return jsonify({"series": fred_data.catalog()})


@app.route("/api/macro/fred/<series_id>")
def fred_series(series_id):
    if not fred_data:
        return jsonify({"error": "fred_data module not available"}), 500
    obs = _safe_int(request.args.get("observations"), 60)
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
        return jsonify({"error": "cftc_cot module not available"}), 500
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
        return jsonify({"error": "wikidata_meta module not available"}), 500
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
        return jsonify({"error": "finviz_data module not available"}), 500
    return jsonify(finviz_data.sector_heatmap())


@app.route("/api/intel/finviz/insiders")
def finviz_insiders():
    if not finviz_data:
        return jsonify({"error": "finviz_data module not available"}), 500
    option = request.args.get("option", "latest")
    return jsonify(finviz_data.insider_trades(option=option))


@app.route("/api/news/finviz/<symbol>")
def finviz_news(symbol):
    if not finviz_data:
        return jsonify({"error": "finviz_data module not available"}), 500
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
        limit=_safe_int(request.args.get("limit"), 200),
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
    if _sec_v2_unavailable():
        return jsonify(dict(_SEC_V2_DORMANT_ENVELOPE, symbol=symbol.upper())), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    form = request.args.get("form")
    limit = _safe_int(request.args.get("limit"), 20)
    return jsonify({
        "symbol": symbol.upper(),
        "filings": sec_filings_v2.get_company_filings(symbol.upper(), form=form, limit=limit),
    })


@app.route("/api/intel/form4-v2/<symbol>")
def intel_form4_v2(symbol):
    if _sec_v2_unavailable():
        return jsonify(dict(_SEC_V2_DORMANT_ENVELOPE, symbol=symbol.upper())), 503
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
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
    sort = request.args.get("sort", "hot")
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
    if _live(wk) and spike is not None:
        # 0% spike -> 50 (baseline attention), +50% -> 100, -50% -> 0.
        parts.append(min(100.0, max(0.0, 50.0 + float(spike))))
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
        "spike_pct": round(float(spike), 1) if spike is not None else None,
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
            hn = hn_sentiment.fetch_mentions(sym, hours=168)
            out["sources"]["hackernews"] = {
                "status": "error" if hn.get("error") else "live",
                "mention_count": hn.get("mention_count", 0),
                "stats": hn.get("stats", {}),
                "mentions": (hn.get("mentions") or [])[:5],
            }
        except Exception as e:
            out["sources"]["hackernews"] = {"status": "error", "note": str(e)[:140]}

    # Wikipedia attention.
    if wiki_attention:
        try:
            w = wiki_attention.fetch_pageviews(sym, days=30)
            out["sources"]["wikipedia"] = {
                "status": "error" if w.get("error") else "live",
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
        return jsonify({"error": "wiki_attention module not available"}), 500
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
        return jsonify({"error": "hn_sentiment module not available"}), 500
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
    min_score = _safe_int(request.args.get("min_score"), None)
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
        if not idea or idea.get("error"):
            return jsonify(idea or {"error": "Unknown error"}), 500
        return jsonify(idea)
    except Exception as e:
        log.exception("ideas_random failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/ideas/universe")
def ideas_universe():
    """Universe metadata for the picker UI (counts, available sectors/strategies)."""
    discovery_mode = (request.args.get("discovery_mode") or "curated").strip().lower()
    try:
        return jsonify(idea_generator.list_universe(discovery_mode=discovery_mode))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ideas/enrich/<symbol>")
def ideas_enrich(symbol):
    """
    Compute the slow enrichment blocks for a symbol that the user just rolled.
    Two-phase streaming: /api/ideas/random returns fast blocks, then the UI
    fires this endpoint in parallel to fill in the rest.
    """
    asset_class = (request.args.get("asset_class") or "stock").strip().lower()
    strategy = (request.args.get("strategy") or "growth").strip().lower()
    try:
        return jsonify(idea_generator.enrich_idea(symbol.upper(), asset_class, strategy))
    except Exception as e:
        log.exception("ideas_enrich %s failed: %s", symbol, e)
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": "research_backtest module not available"}), 500
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
        return jsonify({"error": str(e)}), 500


# ── 2. Event Study ──────────────────────────────────────────────────
@app.route("/api/research/event-study/<symbol>")
def research_event_study_route(symbol):
    if research_eventstudy is None:
        return jsonify({"error": "research_eventstudy module not available"}), 500
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
        return jsonify({"error": str(e)}), 500


# ── 3. Fama-French Factors ─────────────────────────────────────────
@app.route("/api/research/factors/<symbol>")
def research_factors_symbol(symbol):
    if not research_factors:
        return jsonify({"error": "research_factors module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        years = _safe_int(request.args.get("years"), 5)
        return jsonify(research_factors.factor_exposure(symbol.upper(), years))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/factors/portfolio", methods=["GET", "POST"])
def research_factors_portfolio():
    if not research_factors:
        return jsonify({"error": "research_factors module not available"}), 500
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
        return jsonify({"error": str(e)}), 500


# ── 4. Hypothesis Lab ───────────────────────────────────────────────
@app.route("/api/research/hypothesis/generate", methods=["POST"])
def api_hypothesis_generate():
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 500
    try:
        data = request.get_json(silent=True) or {}
        symbol = (data.get("symbol") or "").strip().upper()
        if not symbol:
            return jsonify({"error": "symbol required"}), 400
        return jsonify(research_hypothesis.generate_hypothesis(symbol))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/hypothesis/save", methods=["POST"])
def api_hypothesis_save():
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 500
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
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/research/hypothesis", methods=["GET"])
def api_hypothesis_list():
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 500
    try:
        status = request.args.get("status")
        symbol = request.args.get("symbol")
        limit = _safe_int(request.args.get("limit"), 50)
        return jsonify(research_hypothesis.list_hypotheses(
            status=status, symbol=symbol, limit=limit,
        ))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/hypothesis/<int:hid>/status", methods=["POST"])
def api_hypothesis_set_status(hid):
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 500
    try:
        data = request.get_json(silent=True) or {}
        status = (data.get("status") or "").strip().upper()
        ok = research_hypothesis.update_hypothesis_status(hid, status)
        return jsonify({"ok": ok})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/hypothesis/<int:hid>/score", methods=["POST"])
def api_hypothesis_score(hid):
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 500
    try:
        return jsonify(research_hypothesis.score_hypothesis(hid))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/hypothesis/stats", methods=["GET"])
def api_hypothesis_stats():
    if not research_hypothesis:
        return jsonify({"error": "research_hypothesis module not available"}), 500
    try:
        return jsonify(research_hypothesis.stats())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 5. IV Risk-Neutral Density ─────────────────────────────────────
@app.route("/api/research/rnd/<symbol>")
def research_rnd_default(symbol):
    if not research_iv_density:
        return jsonify({"error": "research_iv_density module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        return jsonify(research_iv_density.risk_neutral_density(symbol.upper()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/rnd/<symbol>/<expiry>")
def research_rnd_with_expiry(symbol, expiry):
    if not research_iv_density:
        return jsonify({"error": "research_iv_density module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", expiry or ""):
        return jsonify({"error": "Expiry must be YYYY-MM-DD"}), 400
    try:
        return jsonify(research_iv_density.risk_neutral_density(symbol.upper(), expiry))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
    stock_syms = [h["symbol"] for h in rows if h.get("asset_type") != "crypto"]
    crypto_syms = [h["symbol"] for h in rows if h.get("asset_type") == "crypto"]
    prices = {}
    if stock_syms:
        try:
            prices.update(fetcher.get_quotes_batch(stock_syms))
        except Exception:
            pass
    if crypto_syms:
        try:
            cp = fetcher.get_quotes_batch([s + "-USD" for s in crypto_syms])
            for s in crypto_syms:
                v = cp.get((s + "-USD").upper())
                if v:
                    prices[s] = v
        except Exception:
            pass
    out = []
    for h in rows:
        q = prices.get(h["symbol"]) or {}
        px = q.get("price")
        if not px:
            continue
        mv = px * h["shares"]
        if mv <= 0:
            continue
        sym = h["symbol"] + "-USD" if h.get("asset_type") == "crypto" else h["symbol"]
        out.append({"symbol": sym, "market_value": round(mv, 2)})
    return out


@app.route("/api/research/montecarlo", methods=["POST"])
def research_montecarlo_route():
    if not _mc_mod:
        return jsonify({"error": "research_montecarlo module not available"}), 500
    try:
        data = request.get_json(force=True, silent=True) or {}
        holdings = data.get("holdings") or []
        if not isinstance(holdings, list) or not holdings:
            return jsonify({"error": "holdings: non-empty list required"}), 400
        horizon = _safe_int(data.get("horizon_days"), 365)
        n_paths = _safe_int(data.get("n_paths"), 10000)
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
            except (TypeError, ValueError):
                pass
        return jsonify(sim)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/montecarlo/portfolio", methods=["GET"])
def research_montecarlo_portfolio():
    if not _mc_mod:
        return jsonify({"error": "research_montecarlo module not available"}), 500
    try:
        acct = request.args.get("account_id")
        holdings = _mc_holdings_from_portfolio(
            account_id=_safe_int(acct, None) if acct else None
        )
        if not holdings:
            return jsonify({"error": "No portfolio holdings available"}), 400
        horizon = _safe_int(request.args.get("horizon_days"), 365)
        n_paths = _safe_int(request.args.get("n_paths"), 10000)
        method = request.args.get("method") or "historical_bootstrap"
        sim = _mc_mod.simulate_portfolio(
            holdings, n_paths=n_paths, horizon_days=horizon, method=method,
        )
        return jsonify(sim)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 7. Multi-Horizon Forecast ──────────────────────────────────────
@app.route("/api/research/horizons/<symbol>")
def research_horizons(symbol):
    if not research_multihorizon:
        return jsonify({"error": "research_multihorizon module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        return jsonify(research_multihorizon.multi_horizon_forecast(symbol.upper()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 8. Portfolio Optimizer ─────────────────────────────────────────
@app.route("/api/research/optimize", methods=["POST"])
def research_optimize():
    if not research_optimizer:
        return jsonify({"error": "research_optimizer module not available"}), 500
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
        return jsonify({"error": str(e)}), 500


# ── 9. Probabilistic Forecast ──────────────────────────────────────
@app.route("/api/research/probforecast/<symbol>", methods=["GET"])
def research_probforecast(symbol):
    if not _pf_mod:
        return jsonify({"error": "research_probforecast module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        horizon = _safe_int(request.args.get("horizon"), 20)
        n_boot = _safe_int(request.args.get("n"), 2000)
        return jsonify(_pf_mod.prob_forecast(symbol.upper(), horizon_days=horizon, n_bootstrap=n_boot))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/probforecast/<symbol>/vs-point", methods=["GET"])
def research_probforecast_vs_point(symbol):
    if not _pf_mod:
        return jsonify({"error": "research_probforecast module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        horizon = _safe_int(request.args.get("horizon"), 20)
        return jsonify(_pf_mod.compare_to_point(symbol.upper(), horizon_days=horizon))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/forecast/ensemble/<symbol>", methods=["GET"])
def forecast_ensemble_route(symbol):
    """Calibrated meta-forecast fusing all available forecasting signals
    into one directional probability + return cone."""
    if not _ens_mod:
        return jsonify({"error": "forecast_ensemble module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        horizon = _safe_int(request.args.get("horizon"), 20)
        return jsonify(_ens_mod.ensemble_forecast(symbol.upper(), horizon_days=horizon))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/forecast/accountability", methods=["GET"])
def forecast_accountability_route():
    """Realized track record + Brier calibration for the ensemble, plus the
    per-component leaderboard and current adaptive weights."""
    try:
        import forecast_accountability
    except Exception as e:
        return jsonify({"error": "forecast_accountability unavailable: {}".format(e)}), 500
    try:
        since = request.args.get("since")
        return jsonify(forecast_accountability.accountability_report(since=since))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Jarvis: unified assistant layer ────────────────────────────────
@app.route("/api/jarvis/act", methods=["POST"])
def jarvis_act_route():
    """Execute a user-CONFIRMED mutating action proposed by the Jarvis agent.
    Whitelisted against jarvis_tools' registry — read tools and unknown names
    are rejected, so this can only ever do what the registry defines."""
    try:
        import jarvis_tools
    except Exception as e:
        return jsonify({"error": "jarvis_tools unavailable: {}".format(e)}), 500
    data = request.get_json(silent=True) or {}
    tool = data.get("tool")
    args = data.get("args")
    if not isinstance(tool, str) or not isinstance(args, dict):
        return jsonify({"error": "expected {tool: str, args: object}"}), 400
    r = jarvis_tools.execute_mutating(tool, args)
    return (jsonify(r), 400) if r.get("error") else jsonify(r)


@app.route("/api/jarvis/briefing", methods=["GET"])
def jarvis_briefing_route():
    """Prioritized daily briefing: portfolio pulse, alerts, earnings,
    market regime, notable moves, concentration, fresh ideas."""
    try:
        import jarvis
    except Exception as e:
        return jsonify({"error": "jarvis unavailable: {}".format(e)}), 500
    try:
        force = request.args.get("refresh") == "1"
        return jsonify(jarvis.get_briefing(force_refresh=force))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/jarvis/activity", methods=["GET"])
def jarvis_activity_route():
    """In-memory snapshot of background machinery for the activity panel."""
    try:
        import jarvis
        return jsonify(jarvis.activity_snapshot())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": "jarvis unavailable: {}".format(e)}), 500

    def generate():
        import time as _time
        import json as _json
        started = _time.time()
        last_sig = None
        last_yield = _time.time()
        sent_any = False
        while _time.time() - started < 600:  # hard stop after 10 min
            try:
                snap = jarvis.activity_snapshot()
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
                # One bad snapshot must not kill the stream — heartbeat so
                # the very first chunk still arrives promptly even on error.
                # (GeneratorExit is BaseException: client disconnects still
                # close the generator normally.)
                if _time.time() - last_yield >= 15 or not sent_any:
                    last_yield = _time.time()
                    sent_any = True
                    yield ": hb\n\n"
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
        return jsonify({"error": "jarvis unavailable: {}".format(e)}), 500
    if not view or len(view) > 40 or not view.replace("-", "").isalnum():
        return jsonify({"error": "invalid view"}), 400
    symbol = request.args.get("symbol")
    if symbol and not _valid_ticker(symbol):
        symbol = None
    try:
        return jsonify(jarvis.view_context(view, symbol))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/jarvis/ask", methods=["POST"])
def jarvis_ask_route():
    """Natural-language Q&A routed to local engines (no API key needed)."""
    try:
        import jarvis
    except Exception as e:
        return jsonify({"error": "jarvis unavailable: {}".format(e)}), 500
    data = request.get_json(silent=True) or {}
    query = data.get("query") if isinstance(data, dict) else None
    if not query or not isinstance(query, str):
        return jsonify({"error": "expected JSON body with a 'query' string"}), 400
    if len(query) > 500:
        return jsonify({"error": "query too long (max 500 chars)"}), 400
    history = data.get("history")  # optional conversation turns; jarvis clamps shape
    try:
        return jsonify(jarvis.ask(query, history=history))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 10. Signal Tracker ─────────────────────────────────────────────
@app.route("/api/research/track/<signal_name>")
def research_track_record(signal_name):
    if not research_tracker:
        return jsonify({"error": "research_tracker module not available"}), 500
    if not signal_name or not signal_name.replace("_", "").isalnum():
        return jsonify({"error": "Invalid signal name"}), 400
    try:
        since = request.args.get("since")
        symbol = request.args.get("symbol")
        return jsonify(research_tracker.get_track_record(
            signal_name, since=since, symbol=symbol,
        ))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/track/<signal_name>/calls")
def research_track_calls(signal_name):
    if not research_tracker:
        return jsonify({"error": "research_tracker module not available"}), 500
    if not signal_name or not signal_name.replace("_", "").isalnum():
        return jsonify({"error": "Invalid signal name"}), 400
    try:
        limit = _safe_int(request.args.get("limit"), 20)
        calls = research_tracker.get_recent_calls(signal_name, limit=limit)
        return jsonify({"calls": calls, "signal_name": signal_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/track/_score", methods=["POST"])
def research_track_score_now():
    if not research_tracker:
        return jsonify({"error": "research_tracker module not available"}), 500
    try:
        return jsonify(research_tracker.score_due_forecasts())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": "synth_bayessmart module not available"}), 500
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
        return jsonify({"error": str(e)}), 500


# ── 2. Catalyst Timeline ────────────────────────────────────────────
@app.route("/api/synth/catalyst", methods=["GET"])
def synth_catalyst_get():
    if synth_catalyst is None:
        return jsonify({"error": "synth_catalyst module not available"}), 500
    days_ahead = _safe_int(request.args.get("days_ahead"), 60)
    try:
        return jsonify(synth_catalyst.catalyst_timeline(symbols=None, days_ahead=days_ahead))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/synth/catalyst", methods=["POST"])
def synth_catalyst_post():
    if synth_catalyst is None:
        return jsonify({"error": "synth_catalyst module not available"}), 500
    body = request.get_json(silent=True) or {}
    syms = body.get("symbols") or []
    if not isinstance(syms, list):
        return jsonify({"error": "`symbols` must be an array"}), 400
    syms = [str(s).strip().upper() for s in syms if str(s).strip()]
    syms = [s for s in syms if _valid_ticker(s)][:25]
    days_ahead = _safe_int(body.get("days_ahead"), 60)
    try:
        return jsonify(synth_catalyst.catalyst_timeline(symbols=syms, days_ahead=days_ahead))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 3. Cluster Scan ─────────────────────────────────────────────────
@app.route("/api/synth/cluster-scan", methods=["GET"])
def synth_cluster_scan_get():
    if not synth_cluster:
        return jsonify({"error": "synth_cluster module not available"}), 500
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
        return jsonify({"error": str(e)}), 500


@app.route("/api/synth/cluster-scan", methods=["POST"])
def synth_cluster_scan_post():
    if not synth_cluster:
        return jsonify({"error": "synth_cluster module not available"}), 500
    body = request.get_json(silent=True) or {}
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
        return jsonify({"error": str(e)}), 500


# ── 4. Cross-source Consensus ───────────────────────────────────────
@app.route("/api/synth/consensus/<symbol>")
def synth_consensus_route(symbol):
    if not synth_consensus:
        return jsonify({"error": "synth_consensus module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        return jsonify(synth_consensus.consensus_score(symbol.upper()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 5. Divergence Map ───────────────────────────────────────────────
@app.route("/api/synth/divergence-map", methods=["GET", "POST"])
def synth_divergence_map_route():
    if not synth_divmap:
        return jsonify({"error": "synth_divmap module not available"}), 500
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        universe = body.get("universe")
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
        return jsonify({"error": str(e)}), 500


# ── 6. Pattern-Grounded Hypothesis ──────────────────────────────────
@app.route("/api/synth/grounded-hypothesis/<symbol>", methods=["POST"])
def synth_grounded_hypothesis_route(symbol):
    if synth_groundhyp is None:
        return jsonify({"error": "synth_groundhyp module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        return jsonify(synth_groundhyp.grounded_hypothesis(symbol.upper()))
    except Exception as e:
        log.exception("grounded_hypothesis failed")
        return jsonify({"error": str(e)}), 500


# ── 7. Cross-Asset Macro Translator ─────────────────────────────────
@app.route("/api/synth/macrotranslate/releases")
def synth_macrotranslate_catalog():
    if not synth_macrotranslate:
        return jsonify({"error": "synth_macrotranslate module not available"}), 500
    try:
        return jsonify({"releases": synth_macrotranslate.supported_releases()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/synth/macrotranslate/<release_id>")
def synth_macrotranslate_get(release_id):
    if not synth_macrotranslate:
        return jsonify({"error": "synth_macrotranslate module not available"}), 500
    surprise = request.args.get("surprise")
    try:
        surprise_pct = float(surprise) if surprise not in (None, "") else None
    except (TypeError, ValueError):
        surprise_pct = None
    try:
        return jsonify(synth_macrotranslate.macro_translate(release_id, surprise_pct=surprise_pct))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/synth/macrotranslate/<release_id>/portfolio", methods=["POST"])
def synth_macrotranslate_portfolio(release_id):
    if not synth_macrotranslate:
        return jsonify({"error": "synth_macrotranslate module not available"}), 500
    body = request.get_json(silent=True) or {}
    holdings = body.get("holdings") or []
    surprise_pct = body.get("surprise_pct")
    if not holdings:
        # Fall back to the current portfolio so a no-body POST works.
        try:
            rows = db.get_portfolio() or []
            holdings = []
            for h in rows:
                mv = (h.get("shares") or 0) * (h.get("avg_cost") or 0)
                if mv > 0:
                    holdings.append({"symbol": h["symbol"], "market_value": float(mv)})
        except Exception:
            holdings = []
    try:
        surprise_pct = float(surprise_pct) if surprise_pct not in (None, "") else None
    except (TypeError, ValueError):
        surprise_pct = None
    try:
        return jsonify(synth_macrotranslate.macro_translate(
            release_id, surprise_pct=surprise_pct, portfolio_holdings=holdings))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 8. Peer Divergence ──────────────────────────────────────────────
@app.route("/api/synth/peerdiv/<symbol>")
def synth_peerdiv_route(symbol):
    if not synth_peerdiv:
        return jsonify({"error": "synth_peerdiv module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    n = _safe_int(request.args.get("n"), 5)
    n = max(1, min(n, 12))
    try:
        return jsonify(synth_peerdiv.peer_divergence(symbol.upper(), n))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 9. Sector Flow ──────────────────────────────────────────────────
@app.route("/api/synth/sectorflow")
def synth_sectorflow_route():
    if synth_sectorflow is None:
        return jsonify({"error": "synth_sectorflow module not available"}), 500
    try:
        return jsonify(synth_sectorflow.sector_flow())
    except Exception as e:
        log.exception("sectorflow failure")
        return jsonify({"error": str(e)}), 500


# ── 10. Portfolio What-If ───────────────────────────────────────────
@app.route("/api/synth/whatif", methods=["POST"])
def synth_whatif_post():
    if not synth_whatif:
        return jsonify({"error": "synth_whatif module not available"}), 500
    body = request.get_json(silent=True) or {}
    current = body.get("current_holdings") or []
    candidate = body.get("candidate") or {}
    if not isinstance(current, list) or not isinstance(candidate, dict):
        return jsonify({"error": "expected JSON {current_holdings:[...], candidate:{...}}"}), 400
    try:
        return jsonify(synth_whatif.whatif(current, candidate))
    except Exception as e:
        log.exception("synth_whatif failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/synth/whatif", methods=["GET"])
def synth_whatif_get():
    """Convenience GET that pulls current_holdings from the DB and accepts
    candidate parameters via query string (?symbol=...&market_value=...&action=...)."""
    if not synth_whatif:
        return jsonify({"error": "synth_whatif module not available"}), 500
    sym = (request.args.get("symbol") or "").strip().upper()
    try:
        mv = float(request.args.get("market_value") or 0.0)
    except (TypeError, ValueError):
        mv = 0.0
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
        return jsonify({"error": str(e)}), 500


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
