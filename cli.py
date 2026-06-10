#!/usr/bin/env python3
"""
AUGUR CLI — Command-line interface for the Wealth Intelligence System.

Usage:
    python cli.py <command> [subcommand] [options]

Examples:
    python cli.py quote AAPL
    python cli.py portfolio list
    python cli.py smart-money score AAPL
    python cli.py ml-forecast AAPL
    python cli.py congress --days 60
"""

import argparse
import json
import sys
import os
import time
from datetime import datetime

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database as db
db.init_db()


# ── Output Helpers ───────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
WHITE  = "\033[97m"
MAGENTA = "\033[35m"

def _c(text, color):
    return f"{color}{text}{RESET}"

def _pnl_color(val):
    if val is None: return DIM   # None > 0 raises TypeError in py3
    if val > 0: return GREEN
    if val < 0: return RED
    return DIM

def _signal_color(sig):
    s = (sig or "").upper()
    if "BUY" in s or "BULLISH" in s or "STRONG BUY" in s:
        return GREEN
    if "SELL" in s or "BEARISH" in s or "AVOID" in s:
        return RED
    if "CAUTION" in s:
        return YELLOW
    return DIM

def banner(text):
    w = max(len(text) + 4, 50)
    print(_c("═" * w, CYAN))
    print(_c(f"  {text}", WHITE + BOLD))
    print(_c("═" * w, CYAN))

def header(text):
    print(f"\n{_c(text, CYAN + BOLD)}")
    print(_c("─" * len(text), DIM))

def table(rows, headers, col_widths=None, alignments=None):
    """Print a simple formatted table."""
    if not rows:
        print(_c("  (no data)", DIM))
        return
    if col_widths is None:
        col_widths = []
        for i, h in enumerate(headers):
            w = len(h)
            for r in rows:
                val = str(r[i]) if i < len(r) else ""
                w = max(w, len(val))
            col_widths.append(min(w + 2, 40))
    if alignments is None:
        alignments = ["<"] * len(headers)

    hdr = ""
    for i, h in enumerate(headers):
        hdr += f"{h:{alignments[i]}{col_widths[i]}}"
    print(_c(f"  {hdr}", DIM))
    print(_c("  " + "─" * sum(col_widths), DIM))
    for r in rows:
        line = ""
        for i, val in enumerate(r):
            s = str(val) if val is not None else ""
            if i < len(col_widths):
                line += f"{s:{alignments[i]}{col_widths[i]}}"
            else:
                line += s
        print(f"  {line}")

def fmt_price(val):
    if val is None: return "N/A"
    return f"${val:,.2f}"

def fmt_pct(val):
    if val is None: return "N/A"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"

def fmt_number(val):
    if val is None: return "N/A"
    if abs(val) >= 1e12: return f"${val/1e12:.2f}T"
    if abs(val) >= 1e9: return f"${val/1e9:.2f}B"
    if abs(val) >= 1e6: return f"${val/1e6:.1f}M"
    return f"${val:,.0f}"

def print_json(data):
    print(json.dumps(data, indent=2, default=str))

def _elapsed(start):
    return f"{(time.time() - start):.1f}s"


# ══════════════════════════════════════════════════════════════════════════════
# COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

# ── Quote ─────────────────────────────────────────────────────────────────────

def cmd_quote(args):
    import fetcher
    for sym in args.symbols:
        q = fetcher.get_quote(sym.upper())
        if not q or "error" in q:
            print(_c(f"  {sym}: no data", RED))
            continue
        # `or 0` (not get(...,0)) because the API may emit the key with an
        # explicit None, which would crash the f'{change:+.2f}' / _pnl_color below.
        price = q.get("price") or 0
        change = q.get("change") or 0
        pct = q.get("change_pct") or 0
        col = _pnl_color(change)
        name = q.get("name", sym)
        mcap = fmt_number(q.get("market_cap"))
        vol = f"{q.get('volume', 0):,.0f}" if q.get("volume") else "N/A"
        hi52 = fmt_price(q.get("fifty_two_week_high"))
        lo52 = fmt_price(q.get("fifty_two_week_low"))

        banner(f"{sym.upper()} — {name}")
        print(f"  Price:    {_c(fmt_price(price), WHITE + BOLD)}  {_c(fmt_pct(pct), col)} ({_c(f'{change:+.2f}', col)})")
        print(f"  Mkt Cap:  {mcap}    Volume: {vol}")
        print(f"  52w:      {lo52} — {hi52}")
        print()


# ── Fundamentals ──────────────────────────────────────────────────────────────

def cmd_fundamentals(args):
    import fetcher
    sym = args.symbol.upper()
    banner(f"FUNDAMENTALS — {sym}")
    data = fetcher.get_fundamentals(sym)
    if not data or "error" in data:
        print(_c("  No data available", RED))
        return
    rows = []
    key_metrics = [
        ("trailingPE", "P/E (TTM)"), ("forwardPE", "P/E (Fwd)"), ("pegRatio", "PEG"),
        ("priceToSalesTrailing12Months", "P/S"), ("priceToBook", "P/B"),
        ("profitMargins", "Profit Margin"), ("revenueGrowth", "Revenue Growth"),
        ("returnOnEquity", "ROE"), ("returnOnAssets", "ROA"),
        ("debtToEquity", "Debt/Equity"), ("currentRatio", "Current Ratio"),
        ("dividendYield", "Div Yield"), ("beta", "Beta"),
        ("targetMeanPrice", "Analyst Target"), ("recommendationKey", "Recommendation"),
    ]
    for key, label in key_metrics:
        val = data.get(key)
        if val is None:
            continue
        if isinstance(val, float):
            if "margin" in key.lower() or "growth" in key.lower() or "yield" in key.lower() or "return" in key.lower():
                rows.append((label, fmt_pct(val * 100)))
            elif "price" in key.lower() or "target" in key.lower():
                rows.append((label, fmt_price(val)))
            else:
                rows.append((label, f"{val:.2f}"))
        else:
            rows.append((label, str(val)))
    table(rows, ["Metric", "Value"], [22, 20])


# ── Chart (text sparkline) ───────────────────────────────────────────────────

def cmd_chart(args):
    import fetcher
    sym = args.symbol.upper()
    data = fetcher.get_chart_data(sym, period=args.period, interval=args.interval)
    if not data:
        print(_c("No chart data", RED))
        return
    closes = [d["close"] for d in data if d.get("close")]
    if not closes:
        print(_c("No close prices", RED))
        return

    banner(f"CHART — {sym}  ({args.period} / {args.interval})")
    lo, hi = min(closes), max(closes)
    rng = hi - lo or 1
    width = 60
    height = 16
    grid = [[" "] * width for _ in range(height)]
    step = max(1, len(closes) // width)
    sampled = closes[::step][:width]
    for x, c in enumerate(sampled):
        y = int((c - lo) / rng * (height - 1))
        grid[height - 1 - y][x] = _c("█", GREEN if c >= closes[0] else RED)

    print(f"  {_c(fmt_price(hi), DIM)}  ┐")
    for row in grid:
        print(f"  {''.join(row)}  │")
    print(f"  {_c(fmt_price(lo), DIM)}  ┘")
    print(f"  Start: {fmt_price(closes[0])}  →  End: {fmt_price(closes[-1])}  ({fmt_pct((closes[-1]/closes[0]-1)*100)})")

    # Technical indicators
    indicators = fetcher.compute_indicators(data)
    if indicators:
        header("INDICATORS")
        ind_rows = []
        for key in ["rsi", "macd", "macd_signal", "bb_upper", "bb_lower", "atr", "sma20", "sma50", "sma200", "ema12", "ema26"]:
            val = indicators.get(key)
            if val is not None:
                label = key.upper().replace("_", " ")
                ind_rows.append((label, f"{val:.2f}"))
        table(ind_rows, ["Indicator", "Current"], [16, 14])


# ── News ──────────────────────────────────────────────────────────────────────

def cmd_news(args):
    import fetcher
    sym = args.symbol.upper()
    news = fetcher.get_news(sym, limit=args.limit)
    if not news:
        print(_c("No news found", DIM))
        return
    banner(f"NEWS — {sym}")
    for i, n in enumerate(news, 1):
        title = n.get("title", "")
        source = n.get("publisher", n.get("source", ""))
        dt = n.get("published", "")
        print(f"  {_c(f'[{i}]', CYAN)} {_c(title, WHITE)}")
        if source or dt:
            print(f"      {_c(source, DIM)}  {_c(dt, DIM)}")
        link = n.get("link", n.get("url", ""))
        if link:
            print(f"      {_c(link, DIM)}")
        print()


# ── Search ────────────────────────────────────────────────────────────────────

def cmd_search(args):
    import fetcher
    results = fetcher.search_symbol(args.query)
    if not results:
        print(_c("No results", DIM))
        return
    rows = [(r.get("symbol",""), r.get("name",""), r.get("type",""), r.get("exchange","")) for r in results]
    table(rows, ["Symbol", "Name", "Type", "Exchange"], [10, 35, 10, 12])


# ── Market Overview ───────────────────────────────────────────────────────────

def cmd_market(args):
    import fetcher
    sub = args.market_sub
    if sub == "indices":
        banner("MARKET INDICES")
        data = fetcher.get_market_indices()
        if not data: return
        rows = []
        for idx in data:
            name = idx.get("name", idx.get("symbol",""))
            price = idx.get("price", 0)
            chg = idx.get("change_pct", 0)
            rows.append((name, fmt_price(price), _c(fmt_pct(chg), _pnl_color(chg))))
        table(rows, ["Index", "Price", "Change"], [28, 14, 12])

    elif sub == "sectors":
        banner("SECTOR PERFORMANCE")
        data = fetcher.get_sector_performance()
        if not data: return
        rows = []
        for s in data:
            name = s.get("name", s.get("symbol",""))
            chg = s.get("change_pct") or 0   # `or 0`: a present-but-None value would crash int(abs(chg)*3)
            bar_len = int(abs(chg) * 3)
            bar = ("█" * min(bar_len, 20))
            rows.append((name, _c(fmt_pct(chg), _pnl_color(chg)), _c(bar, _pnl_color(chg))))
        table(rows, ["Sector", "Change", ""], [24, 10, 22])

    elif sub == "movers":
        banner("TOP MOVERS")
        data = fetcher.get_top_movers()
        if not data: return
        for cat in ["gainers", "losers"]:
            items = data.get(cat, [])
            if items:
                header(cat.upper())
                rows = [(m.get("symbol",""), fmt_price(m.get("price",0)),
                         _c(fmt_pct(m.get("change_pct",0)), _pnl_color(m.get("change_pct",0))))
                        for m in items]
                table(rows, ["Symbol", "Price", "Change"], [10, 14, 12])


# ── Portfolio ─────────────────────────────────────────────────────────────────

def cmd_portfolio(args):
    import fetcher
    sub = args.portfolio_sub

    if sub == "list" or sub is None:
        holdings = db.get_portfolio()
        if not holdings:
            print(_c("Portfolio is empty. Use: cli.py portfolio add AAPL 10 150.00", YELLOW))
            return
        banner("PORTFOLIO")
        # Get live prices
        stock_syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"]
        crypto_syms = [h["symbol"] for h in holdings if h["asset_type"] == "crypto"]
        prices = {}
        if stock_syms:
            prices.update(fetcher.get_quotes_batch(stock_syms))
        if crypto_syms:
            crypto_yf = [s + "-USD" for s in crypto_syms]
            cp = fetcher.get_quotes_batch(crypto_yf)
            for s in crypto_syms:
                key = (s + "-USD").upper()
                if key in cp:
                    prices[s] = cp[key]

        total_val = 0
        total_cost = 0
        rows = []
        for h in holdings:
            sym = h["symbol"]
            shares = h["shares"]
            avg = h["avg_cost"]
            q = prices.get(sym.upper(), {})
            price = q.get("price", avg)
            val = shares * price
            cost = shares * avg
            pnl = val - cost
            pnl_pct = (pnl / cost * 100) if cost else 0
            total_val += val
            total_cost += cost
            col = _pnl_color(pnl)
            rows.append((
                sym, h["asset_type"][:5], f"{shares:.2f}", fmt_price(avg),
                fmt_price(price), fmt_price(val),
                _c(f"{pnl:+,.2f}", col), _c(fmt_pct(pnl_pct), col)
            ))

        table(rows,
              ["Symbol", "Type", "Shares", "Avg Cost", "Price", "Value", "P&L", "P&L%"],
              [8, 7, 10, 12, 12, 14, 14, 10],
              ["<", "<", ">", ">", ">", ">", ">", ">"])

        total_pnl = total_val - total_cost
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0
        print()
        print(f"  {_c('Total Value:', DIM)}  {_c(fmt_price(total_val), WHITE + BOLD)}  "
              f"P&L: {_c(f'{total_pnl:+,.2f}', _pnl_color(total_pnl))} "
              f"({_c(fmt_pct(total_pnl_pct), _pnl_color(total_pnl))})")

    elif sub == "add":
        sym = args.symbol.upper()
        shares = args.shares
        cost = args.cost
        atype = args.type or "stock"
        name = args.name or sym
        rid = db.add_position(sym, name, shares, cost, asset_type=atype, notes=args.notes or "")
        print(_c(f"  ✓ Added {shares} shares of {sym} @ {fmt_price(cost)} (id={rid})", GREEN))

    elif sub == "remove":
        db.delete_position(args.id)
        print(_c(f"  ✓ Removed position #{args.id}", GREEN))

    elif sub == "update":
        db.update_position(args.id, shares=args.shares, avg_cost=args.cost, notes=args.notes)
        print(_c(f"  ✓ Updated position #{args.id}", GREEN))

    elif sub == "export":
        holdings = db.get_portfolio()
        if not holdings:
            print(_c("Portfolio is empty", DIM))
            return
        fname = args.output or "portfolio_export.csv"
        import csv
        with open(fname, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Symbol", "Name", "Shares", "Avg Cost", "Asset Type", "Sector", "Notes"])
            for h in holdings:
                w.writerow([h["symbol"], h.get("name",""), h["shares"], h["avg_cost"],
                           h["asset_type"], h.get("sector",""), h.get("notes","")])
        print(_c(f"  ✓ Exported {len(holdings)} positions to {fname}", GREEN))


# ── Watchlist ─────────────────────────────────────────────────────────────────

def cmd_watchlist(args):
    import fetcher
    sub = args.watchlist_sub

    if sub == "list" or sub is None:
        items = db.get_watchlist()
        if not items:
            print(_c("Watchlist is empty. Use: cli.py watchlist add AAPL", YELLOW))
            return
        banner("WATCHLIST")
        syms = [w["symbol"] for w in items]
        prices = fetcher.get_quotes_batch(syms)
        rows = []
        for w in items:
            q = prices.get(w["symbol"].upper(), {})
            price = q.get("price", 0)
            chg = q.get("change_pct", 0)
            alert_hi = w.get("alert_high") or ""
            alert_lo = w.get("alert_low") or ""
            rows.append((
                w["symbol"], fmt_price(price), _c(fmt_pct(chg), _pnl_color(chg)),
                alert_hi if alert_hi else "—", alert_lo if alert_lo else "—"
            ))
        table(rows, ["Symbol", "Price", "Change", "Alert ↑", "Alert ↓"], [10, 12, 10, 10, 10])

    elif sub == "add":
        sym = args.symbol.upper()
        db.add_to_watchlist(sym, args.name or "", alert_high=args.alert_high, alert_low=args.alert_low)
        print(_c(f"  ✓ Added {sym} to watchlist", GREEN))

    elif sub == "remove":
        db.delete_from_watchlist(args.id)
        print(_c(f"  ✓ Removed watchlist item #{args.id}", GREEN))


# ── Transactions ──────────────────────────────────────────────────────────────

def cmd_transactions(args):
    sub = args.txn_sub

    if sub == "list" or sub is None:
        txns = db.get_transactions(symbol=args.symbol, limit=args.limit or 50)
        if not txns:
            print(_c("No transactions found", DIM))
            return
        banner("TRANSACTIONS")
        rows = []
        for t in txns:
            action = t["action"]
            col = GREEN if action.upper() == "BUY" else RED
            rows.append((
                t.get("date", "")[:10], t["symbol"], _c(action.upper(), col),
                f"{t['shares']:.2f}", fmt_price(t["price"]),
                fmt_price(t.get("fees", 0)), t.get("notes", "")[:20]
            ))
        table(rows, ["Date", "Symbol", "Action", "Shares", "Price", "Fees", "Notes"],
              [12, 8, 8, 10, 12, 10, 22])

    elif sub == "add":
        sym = args.symbol.upper()
        db.add_transaction(sym, args.action, args.shares, args.price,
                          fees=args.fees or 0, date=args.date, notes=args.notes or "")
        print(_c(f"  ✓ Logged {args.action.upper()} {args.shares} {sym} @ {fmt_price(args.price)}", GREEN))

    elif sub == "export":
        txns = db.get_transactions(limit=9999)
        if not txns:
            print(_c("No transactions", DIM))
            return
        fname = args.output or "transactions_export.csv"
        import csv
        with open(fname, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Symbol", "Action", "Shares", "Price", "Fees", "Notes"])
            for t in txns:
                w.writerow([t.get("date",""), t["symbol"], t["action"],
                           t["shares"], t["price"], t.get("fees",0), t.get("notes","")])
        print(_c(f"  ✓ Exported {len(txns)} transactions to {fname}", GREEN))


# ── Alerts ────────────────────────────────────────────────────────────────────

def cmd_alerts(args):
    sub = args.alert_sub

    if sub == "list" or sub is None:
        alerts = db.get_price_alerts(include_triggered=args.all if hasattr(args, "all") else False)
        if not alerts:
            print(_c("No alerts", DIM))
            return
        banner("PRICE ALERTS")
        rows = []
        for a in alerts:
            status = _c("TRIGGERED", YELLOW) if a.get("triggered") else _c("ACTIVE", GREEN)
            rows.append((a["id"], a["symbol"], a["alert_type"].upper(), fmt_price(a["price"]), status))
        table(rows, ["ID", "Symbol", "Type", "Price", "Status"], [6, 8, 8, 12, 12])

    elif sub == "add":
        db.add_price_alert(args.symbol.upper(), args.type, args.price)
        print(_c(f"  ✓ Alert: {args.symbol.upper()} {args.type} {fmt_price(args.price)}", GREEN))

    elif sub == "remove":
        db.delete_price_alert(args.id)
        print(_c(f"  ✓ Removed alert #{args.id}", GREEN))

    elif sub == "clear":
        db.clear_triggered_alerts()
        print(_c("  ✓ Cleared all triggered alerts", GREEN))


# ── Analytics ─────────────────────────────────────────────────────────────────

def cmd_analytics(args):
    import fetcher
    sub = args.analytics_sub

    if sub == "risk":
        symbols = args.symbols or [h["symbol"] for h in db.get_portfolio()]
        if not symbols:
            print(_c("No symbols — pass symbols or add portfolio positions", YELLOW))
            return
        banner("RISK METRICS")
        data = fetcher.get_risk_metrics(symbols, period=args.period or "1y")
        if not data:
            print(_c("No data", RED))
            return
        for sym, m in data.items():
            header(sym)
            rows = [
                ("Ann. Return", fmt_pct(m.get("annualized_return", 0) * 100)),
                ("Ann. Volatility", fmt_pct(m.get("annualized_volatility", 0) * 100)),
                ("Sharpe Ratio", f"{m.get('sharpe_ratio', 0):.2f}"),
                ("Sortino Ratio", f"{m.get('sortino_ratio', 0):.2f}"),
                ("Max Drawdown", fmt_pct(m.get("max_drawdown", 0) * 100)),
                ("Calmar Ratio", f"{m.get('calmar_ratio', 0):.2f}"),
                ("VaR 95%", fmt_pct(m.get("var_95", 0) * 100)),
            ]
            table(rows, ["Metric", "Value"], [20, 14])

    elif sub == "correlation":
        symbols = args.symbols or [h["symbol"] for h in db.get_portfolio()]
        if len(symbols) < 2:
            print(_c("Need at least 2 symbols", YELLOW))
            return
        banner("CORRELATION MATRIX")
        data = fetcher.get_correlation_matrix(symbols, period=args.period or "1y")
        if not data:
            print(_c("No data", RED))
            return
        syms = data.get("symbols", symbols)
        matrix = data.get("matrix", [])
        # Print header
        hdr = "         " + "  ".join(f"{s:>7}" for s in syms)
        print(_c(hdr, DIM))
        for i, row in enumerate(matrix):
            line = f"  {syms[i]:<7}"
            for val in row:
                if val is None: val = 0
                col = GREEN if val > 0.5 else (RED if val < -0.3 else DIM)
                line += f"  {_c(f'{val:7.3f}', col)}"
            print(line)

    elif sub == "stress":
        holdings = db.get_portfolio()
        if not holdings:
            print(_c("Portfolio is empty", YELLOW))
            return
        drop = args.drop or 20
        banner(f"STRESS TEST — {drop}% DROP")
        data = fetcher.get_portfolio_stress_test(holdings, custom_drop_pct=drop)
        if not data:
            print(_c("No data", RED))
            return
        positions = data.get("positions", [])
        rows = []
        for p in positions:
            rows.append((
                p.get("symbol",""), fmt_price(p.get("current_value",0)),
                _c(fmt_price(p.get("stressed_value",0)), RED),
                _c(fmt_price(p.get("loss",0)), RED)
            ))
        table(rows, ["Symbol", "Current", "Stressed", "Loss"], [10, 14, 14, 14])
        total = data.get("total", {})
        print(f"\n  {_c('Portfolio Impact:', DIM)} {_c(fmt_price(total.get('total_loss',0)), RED)}")

    elif sub == "benchmark":
        import fetcher
        banner(f"BENCHMARK COMPARISON — {args.benchmark or 'SPY'}")
        data = fetcher.get_benchmark_history(args.benchmark or "SPY", period=args.period or "1y")
        if not data:
            print(_c("No data", RED))
            return
        print(f"  Benchmark: {args.benchmark or 'SPY'}")
        print(f"  Period: {args.period or '1y'}")
        pts = data if isinstance(data, list) else data.get("history", [])
        if pts:
            first = pts[0].get("value", pts[0].get("close", 100))
            last = pts[-1].get("value", pts[-1].get("close", 100))
            ret = (last / first - 1) * 100 if first else 0
            print(f"  Return: {_c(fmt_pct(ret), _pnl_color(ret))}")


# ── Options ───────────────────────────────────────────────────────────────────

def cmd_options(args):
    import fetcher
    sub = args.options_sub

    if sub == "dates":
        sym = args.symbol.upper()
        dates = fetcher.get_options_dates(sym)
        if not dates:
            print(_c("No options dates", DIM))
            return
        banner(f"OPTIONS EXPIRATIONS — {sym}")
        for i, d in enumerate(dates, 1):
            print(f"  {_c(f'[{i}]', CYAN)} {d}")

    elif sub == "chain":
        sym = args.symbol.upper()
        data = fetcher.get_option_chain(sym, args.expiry)
        if not data:
            print(_c("No chain data", RED))
            return
        banner(f"OPTIONS CHAIN — {sym} exp {args.expiry}")
        for side in ["calls", "puts"]:
            items = data.get(side, [])
            if not items:
                continue
            header(side.upper())
            rows = []
            for o in items[:20]:
                rows.append((
                    f"{o.get('strike',0):.1f}",
                    f"{o.get('lastPrice',0):.2f}",
                    f"{o.get('bid',0):.2f}",
                    f"{o.get('ask',0):.2f}",
                    f"{o.get('volume',0):,.0f}",
                    f"{o.get('openInterest',0):,.0f}",
                    f"{o.get('impliedVolatility',0)*100:.1f}%",
                ))
            table(rows, ["Strike", "Last", "Bid", "Ask", "Vol", "OI", "IV"],
                  [10, 8, 8, 8, 10, 10, 8], [">",">",">",">",">",">",">"])

    elif sub == "flow":
        import fetcher
        sym = args.symbol.upper() if args.symbol else None
        if sym:
            banner(f"UNUSUAL OPTIONS FLOW — {sym}")
            data = fetcher.get_unusual_options_flow(sym)
        else:
            banner("UNUSUAL OPTIONS FLOW — PORTFOLIO SCAN")
            syms = [h["symbol"] for h in db.get_portfolio()]
            if not syms:
                print(_c("Portfolio is empty", YELLOW))
                return
            data = fetcher.scan_unusual_options_portfolio(syms)

        if not data:
            print(_c("No unusual activity detected", DIM))
            return

        contracts = data if isinstance(data, list) else data.get("contracts", data.get("results", []))
        if isinstance(data, dict) and "contracts" not in data and "results" not in data:
            # Single symbol result
            contracts = data.get("unusual", [])

        if not contracts:
            print(_c("No unusual contracts found", DIM))
            return
        rows = []
        for c in contracts[:30]:
            ctype = c.get("type", c.get("option_type", "?")).upper()
            col = GREEN if ctype == "CALL" else RED
            rows.append((
                c.get("symbol", "?"), _c(ctype, col),
                f"{c.get('strike',0):.1f}", c.get("expiry", c.get("expiration",""))[:10],
                f"{c.get('volume',0):,.0f}", f"{c.get('openInterest', c.get('open_interest',0)):,.0f}",
                f"{c.get('vol_oi_ratio', c.get('ratio',0)):.1f}x",
                f"{c.get('impliedVolatility', c.get('iv',0))*100:.0f}%",
            ))
        table(rows, ["Sym", "Type", "Strike", "Expiry", "Vol", "OI", "V/OI", "IV"],
              [7, 6, 8, 12, 10, 10, 7, 7])


# ── Dividends ─────────────────────────────────────────────────────────────────

def cmd_dividends(args):
    import fetcher
    holdings = db.get_portfolio()
    if not holdings:
        print(_c("Portfolio is empty", YELLOW))
        return
    banner("DIVIDEND ANALYSIS")
    total_annual = 0
    rows = []
    for h in holdings:
        sym = h["symbol"]
        data = fetcher.get_dividend_data(sym)
        if not data or not data.get("dividend_rate"):
            continue
        rate = data.get("dividend_rate", 0)
        yld = (data.get("dividend_yield") or 0) * 100
        annual = rate * h["shares"]
        total_annual += annual
        ex_date = data.get("ex_date", "N/A")
        rows.append((sym, f"${rate:.2f}", fmt_pct(yld), fmt_price(annual), ex_date))

    table(rows, ["Symbol", "Rate", "Yield", "Annual $", "Ex-Date"], [10, 8, 8, 12, 12])
    print(f"\n  {_c('Projected Annual Income:', DIM)} {_c(fmt_price(total_annual), GREEN + BOLD)}")


# ── Macro ─────────────────────────────────────────────────────────────────────

def cmd_macro(args):
    import fetcher
    banner("MACRO INDICATORS")
    data = fetcher.get_macro_indicators()
    if not data:
        print(_c("No data", RED))
        return
    if isinstance(data, dict):
        rows = []
        for k, v in data.items():
            if isinstance(v, dict):
                val = v.get("value", v.get("price", "N/A"))
                chg = v.get("change_pct", v.get("change", None))
                label = v.get("name", k.replace("_", " ").title())
                display = str(val)
                if chg is not None:
                    display += f"  {_c(fmt_pct(chg), _pnl_color(chg))}"
                rows.append((label, display))
            else:
                rows.append((k.replace("_", " ").title(), str(v)))
        table(rows, ["Indicator", "Value"], [28, 30])


# ── Earnings ──────────────────────────────────────────────────────────────────

def cmd_earnings(args):
    import earnings as earnings_module
    sub = args.earnings_sub

    if sub == "calendar":
        holdings = db.get_portfolio()
        watchlist = db.get_watchlist()
        syms = list(set([h["symbol"] for h in holdings] + [w["symbol"] for w in watchlist]))
        if args.symbols:
            syms = [s.upper() for s in args.symbols]
        if not syms:
            print(_c("No symbols", YELLOW))
            return
        banner("EARNINGS CALENDAR")
        data = earnings_module.get_earnings_calendar(syms)
        if not data:
            print(_c("No upcoming earnings found", DIM))
            return
        rows = []
        for e in data:
            days = e.get("days_until", "?")
            sym = e.get("symbol", "")
            date = e.get("earnings_date", "")
            eps_est = e.get("eps_estimate", "N/A")
            beat_rate = e.get("beat_rate")
            br = f"{beat_rate:.0%}" if beat_rate is not None else "N/A"
            urgency = _c("SOON", YELLOW) if isinstance(days, (int, float)) and days <= 7 else ""
            rows.append((sym, date, f"{days}d", str(eps_est), br, urgency))
        table(rows, ["Symbol", "Date", "Days", "EPS Est", "Beat%", ""], [10, 14, 7, 10, 8, 8])

    elif sub == "dossier":
        sym = args.symbol.upper()
        banner(f"EARNINGS DOSSIER — {sym}")
        print(_c("  Fetching earnings data, insider activity, options...", DIM))
        data = earnings_module.get_earnings_dossier(sym)
        if not data or "error" in data:
            print(_c(f"  Error: {data.get('error', 'no data')}", RED))
            return
        # Calendar info
        if data.get("earnings_date"):
            print(f"  Earnings Date: {_c(data['earnings_date'], WHITE + BOLD)}  ({data.get('days_until', '?')} days)")
        # EPS estimates
        if data.get("eps_estimate"):
            print(f"  EPS Estimate: {data['eps_estimate']}  |  Revenue Est: {data.get('revenue_estimate', 'N/A')}")
        # History
        history = data.get("history", [])
        if history:
            header("EARNINGS HISTORY")
            rows = []
            for h in history[:8]:
                surprise = h.get("surprise_pct", 0)
                col = _pnl_color(surprise)
                rows.append((
                    h.get("quarter", ""), str(h.get("estimate", "")),
                    str(h.get("actual", "")),
                    _c(fmt_pct(surprise), col),
                    _c("BEAT" if surprise > 0 else "MISS", col)
                ))
            table(rows, ["Quarter", "Est", "Actual", "Surprise", ""], [12, 10, 10, 10, 8])
        # Options implied move
        if data.get("implied_move_pct"):
            print(f"\n  Options Implied Move: {_c(fmt_pct(data['implied_move_pct']), CYAN)}")


# ── SEC Intelligence ──────────────────────────────────────────────────────────

def cmd_intel(args):
    import edgar
    sub = args.intel_sub

    if sub == "filings":
        sym = args.symbol.upper()
        banner(f"SEC FILINGS — {sym}")
        filings = edgar.get_recent_filings(sym, limit=args.limit or 10)
        if not filings:
            print(_c("No filings found", DIM))
            return
        rows = []
        for f in filings:
            form = f.get("form_type", "")
            date = f.get("filing_date", "")[:10]
            desc = (f.get("description", "") or "")[:50]
            rows.append((form, date, desc))
        table(rows, ["Form", "Date", "Description"], [8, 12, 52])

    elif sub == "insiders":
        sym = args.symbol.upper()
        banner(f"INSIDER TRADING — {sym}")
        txns = edgar.get_form4_transactions(sym)
        if not txns:
            print(_c("No insider transactions found", DIM))
            return
        rows = []
        for t in txns[:20]:
            ttype = t.get("transaction_type", "")
            col = GREEN if ttype.upper() in ("P", "A", "BUY") else RED
            rows.append((
                t.get("date", "")[:10],
                (t.get("insider_name", "") or "")[:22],
                (t.get("insider_title", "") or "")[:16],
                _c(ttype, col),
                f"{t.get('shares', 0):,.0f}",
                fmt_price(t.get("price_per_share", 0)),
            ))
        table(rows, ["Date", "Insider", "Title", "Type", "Shares", "Price"],
              [12, 24, 18, 6, 12, 12])

    elif sub == "institutional":
        import edgar
        banner("INSTITUTIONAL HOLDINGS — TRACKED FUNDS")
        funds = getattr(edgar, "TRACKED_FUNDS", {})
        if not funds:
            print(_c("No tracked funds configured", DIM))
            return
        for name, info in funds.items():
            cik = info if isinstance(info, str) else info.get("cik", "")
            header(name)
            holdings = edgar.get_institutional_holdings(cik, name)
            if not holdings:
                print(_c("  No data", DIM))
                continue
            rows = []
            for h in (holdings if isinstance(holdings, list) else holdings.get("holdings", []))[:10]:
                rows.append((
                    (h.get("name", "") or "")[:30],
                    f"{h.get('shares', 0):,.0f}",
                    fmt_number(h.get("value", 0)),
                    f"{h.get('pct_portfolio', 0):.1f}%"
                ))
            table(rows, ["Company", "Shares", "Value", "% Port"], [32, 14, 14, 8])


# ── Smart Money Score ─────────────────────────────────────────────────────────

def cmd_smart_money(args):
    import smart_money
    sub = args.sm_sub

    if sub == "score":
        sym = args.symbol.upper()
        banner(f"SMART MONEY SCORE — {sym}")
        t0 = time.time()
        print(_c("  Computing 7-factor score (insiders, institutional, earnings, options, momentum, SEC, ML)...", DIM))
        data = smart_money.compute_score(sym)
        if data.get("error"):
            print(_c(f"  Error: {data['error']}", RED))
            return
        score = data["score"]
        signal = data["signal"]
        col = _signal_color(signal)

        # Big score display
        print(f"\n  {_c(f'  {score}  ', col + BOLD)} /100   {_c(signal, col + BOLD)}")
        print(f"  {_c(data.get('name', sym), DIM)}  —  {fmt_price(data.get('price'))}")
        print()

        # Component bars
        comps = data.get("components", {})
        for key, c in comps.items():
            pct = round(c.get("score", 0) / c["max"] * 100) if c.get("max") else 0
            bar_filled = int(pct / 5)
            bar = _c("█" * bar_filled, GREEN if pct >= 70 else (YELLOW if pct >= 40 else RED))
            bar += _c("░" * (20 - bar_filled), DIM)
            is_ml = key == "ml_forecast"
            prefix = "⚡" if is_ml else " "
            print(f"  {prefix} {c['label']:<20} {bar} {c['score']:>2}/{c['max']:<2}  {_c(c.get('detail',''), DIM)}")

        # ML detail
        ml_detail = comps.get("ml_forecast", {}).get("ml_detail", {})
        if ml_detail and ml_detail.get("signal") != "N/A":
            header("ML FORECAST DETAIL")
            rf_prob = ml_detail.get("rf_prob_up", 0.5) * 100
            print(f"  RF P(Up 20d):   {_c(f'{rf_prob:.1f}%', _signal_color(ml_detail.get('rf_signal')))}  "
                  f"(accuracy: {ml_detail.get('rf_accuracy', 0)*100:.0f}%)")
            print(f"  Trend:          {_c(ml_detail.get('trend_direction','N/A'), _signal_color(ml_detail.get('trend_direction')))}  "
                  f"(forecast: {ml_detail.get('forecast_pct', 0):+.1f}%)")
            print(f"  Regime:         {_c(ml_detail.get('regime','N/A'), CYAN)}")
            print(f"  Mean Reversion: {_c(ml_detail.get('mr_signal','N/A'), _signal_color(ml_detail.get('mr_signal')))}  "
                  f"(z: {ml_detail.get('mr_zscore', 0):.2f})")
            feats = ml_detail.get("top_features", [])
            if feats:
                print(f"\n  {_c('Top Features:', DIM)}")
                for f in feats[:5]:
                    print(f"    {f['name']:<16} {f['importance']*100:.1f}%")

        print(f"\n  {_c(f'Computed in {_elapsed(t0)}', DIM)}")

    elif sub == "scan":
        symbols = args.symbols
        if not symbols:
            holdings = db.get_portfolio()
            symbols = [h["symbol"] for h in holdings]
        if not symbols:
            print(_c("No symbols — pass symbols or add portfolio positions", YELLOW))
            return
        banner(f"SMART MONEY SCAN — {len(symbols)} symbols")
        print(_c("  Scoring each symbol across 7 dimensions...\n", DIM))
        results = smart_money.compute_scores_bulk(symbols)
        rows = []
        for r in results:
            sc = r["score"]
            sig = r["signal"]
            col = _signal_color(sig)
            ml_c = r.get("components", {}).get("ml_forecast", {})
            ml_s = f"{ml_c.get('score',0)}/{ml_c.get('max',20)}" if ml_c else "N/A"
            rows.append((
                r["symbol"], _c(f"{sc}", col), _c(sig, col),
                fmt_price(r.get("price")), ml_s
            ))
        table(rows, ["Symbol", "Score", "Signal", "Price", "ML"], [10, 8, 14, 14, 8])


# ── ML Forecast ───────────────────────────────────────────────────────────────

def cmd_ml_forecast(args):
    import ml_forecast as mlf
    sym = args.symbol.upper()
    banner(f"ML FORECAST — {sym}")
    t0 = time.time()
    print(_c("  Training models on historical data (RF, trend, regime, mean reversion)...", DIM))

    data = mlf.ml_forecast(sym)
    if "error" in data:
        print(_c(f"  Error: {data['error']}", RED))
        return

    composite = data.get("composite_ml_score", 0.5)
    signal = data.get("composite_signal", "HOLD")
    col = _signal_color(signal)

    print(f"\n  {_c(f'  {signal}  ', col + BOLD)}  Composite: {composite:.3f}")
    print()

    # RF Classifier
    rf = data.get("rf_classifier", {})
    if rf:
        header("RANDOM FOREST CLASSIFIER")
        prob_up = rf.get("prob_up_20d", 0.5) * 100
        prob_dn = rf.get("prob_down_20d", 0.5) * 100
        rfcol = _signal_color(rf.get("signal"))
        bar_up = int(prob_up / 5)
        print(f"  P(Up 20d):   {_c(f'{prob_up:.1f}%', rfcol)}  {'█' * bar_up}{'░' * (20 - bar_up)}")
        print(f"  P(Down 20d): {prob_dn:.1f}%")
        print(f"  Signal:      {_c(rf.get('signal','N/A'), rfcol)}  (confidence: {rf.get('confidence',0):.1f}%)")
        print(f"  Walk-fwd accuracy: {rf.get('accuracy_recent',0)*100:.1f}%  "
              f"(train: {rf.get('train_samples',0)} samples, balance: {rf.get('class_balance',0)*100:.0f}%)")
        feats = rf.get("top_features", [])
        if feats:
            print(f"\n  {_c('Feature Importance:', DIM)}")
            max_imp = max((f["importance"] for f in feats), default=1) or 1  # avoid /0 when all importances are 0
            for f in feats[:8]:
                pct = int(f["importance"] / max_imp * 20)
                print(f"    {f['name']:<16} {_c('█' * pct, CYAN)}{'░' * (20 - pct)} {f['importance']*100:.1f}%")

    # Trend Forecast
    trend = data.get("trend_forecast", {})
    if trend:
        header("TREND FORECAST (30-DAY)")
        cur = trend.get("current_price", 0)
        fc = trend.get("forecast_price", 0)
        fc_pct = trend.get("forecast_pct", 0)
        tcol = _pnl_color(fc_pct)
        print(f"  Current:  {fmt_price(cur)}")
        print(f"  Forecast: {_c(fmt_price(fc), tcol)}  ({_c(fmt_pct(fc_pct), tcol)})")
        print(f"  CI Range: {fmt_price(trend.get('lower_ci',0))} — {fmt_price(trend.get('upper_ci',0))}")
        print(f"  Short trend: {_c(trend.get('trend_short','?'), tcol)}  |  Mid: {trend.get('trend_mid','?')}  |  Aligned: {trend.get('trends_aligned')}")

        # Mini sparkline of forecast path
        path = trend.get("path", [])
        if path:
            prices = [p["price"] for p in path]
            lo, hi = min(prices), max(prices)
            rng = hi - lo or 1
            spark = ""
            chars = "▁▂▃▄▅▆▇█"
            for p in prices:
                idx = int((p - lo) / rng * (len(chars) - 1))
                spark += chars[idx]
            print(f"\n  {_c('Path:', DIM)} {_c(spark, tcol)}")
            print(f"  {_c(fmt_price(prices[0]), DIM)} {'─' * 20} {_c(fmt_price(prices[-1]), tcol)}")

        # Per-regime detail
        regimes = trend.get("regimes", {})
        if regimes:
            rows = []
            for name, r in regimes.items():
                rows.append((
                    name, fmt_pct(r.get("ann_return_pct", 0)),
                    fmt_pct(r.get("forecast_pct", 0)),
                    f"{r.get('residual_std', 0)*100:.1f}%"
                ))
            table(rows, ["Window", "Ann Return", "30d Fcast", "Std Dev"], [10, 14, 12, 10])

    # Regime Detection
    regime = data.get("regime", {})
    if regime:
        header("REGIME DETECTION (K-MEANS)")
        rlabel = regime.get("current_regime", "?")
        print(f"  Current:       {_c(rlabel, CYAN + BOLD)}")
        print(f"  Days in regime: {regime.get('days_in_regime', '?')}")
        dist = regime.get("regime_distribution_60d", {})
        if dist:
            print(f"\n  {_c('60-day regime distribution:', DIM)}")
            total = sum(dist.values())
            for name, count in sorted(dist.items(), key=lambda x: -x[1]):
                pct = count / total * 100 if total else 0
                bar = int(pct / 5)
                print(f"    {name:<20} {_c('█' * bar, CYAN)}{'░' * (20 - bar)} {pct:.0f}% ({count}d)")

        stats = regime.get("regime_stats", {})
        if stats:
            rows = []
            for name, s in stats.items():
                rows.append((name, f"{s.get('vol_20d',0):.1f}%", f"{s.get('rsi',0):.0f}",
                            f"{s.get('momentum',0):+.1f}%", f"{s.get('bb_width',0):.1f}%"))
            table(rows, ["Regime", "Vol", "RSI", "Mom", "BB Width"], [20, 8, 6, 10, 10])

    # Mean Reversion
    mr = data.get("mean_reversion", {})
    if mr:
        header("MEAN REVERSION (OU PROCESS)")
        mrsig = mr.get("signal", "?")
        mrcol = _signal_color(mrsig)
        print(f"  Signal:    {_c(mrsig, mrcol)}")
        print(f"  Z-score:   {mr.get('zscore', 0):.3f}")
        print(f"  Half-life: {mr.get('half_life_days', 0):.1f} days")
        print(f"  MR Prob:   {mr.get('mr_probability', 0)*100:.1f}%")
        print(f"  Direction: {mr.get('reversion_direction', 'N/A')}")
        print(f"  OU θ:      {mr.get('ou_theta', 0):.4f}")

    print(f"\n  {_c(f'Computed in {_elapsed(t0)}', DIM)}")


# ── Congressional Trading ─────────────────────────────────────────────────────

def cmd_congress(args):
    import congress
    sym = args.symbol
    if sym:
        sym = sym.upper()
        banner(f"CONGRESSIONAL TRADES — {sym}")
        trades = congress.get_trades_for_ticker(sym, days=args.days or 180)
        if not trades:
            print(_c("No trades found", DIM))
            return
        _print_congress_trades(trades)
    else:
        banner("CONGRESSIONAL TRADING SUMMARY")
        print(_c(f"  Fetching House PTR filings (last {args.days or 90} days)...", DIM))
        data = congress.get_congress_summary(days=args.days or 90, max_pdfs=args.max_pdfs or 60)
        trades = data.get("trades", [])
        summary = data.get("ticker_summary", [])

        print(f"  Filings parsed: {data.get('filings_parsed', 0)}  |  Trades found: {data.get('total_trades_found', 0)}")

        if summary:
            header("MOST TRADED TICKERS")
            rows = []
            for s in summary[:20]:
                sentiment = s.get("sentiment", "")
                scol = _signal_color(sentiment)
                rows.append((
                    s["ticker"], str(s["total_trades"]),
                    str(s["buys"]), str(s["sells"]),
                    _c(f"{s['buy_pct']}%", scol),
                    _c(sentiment, scol),
                    str(s.get("member_count", 0)),
                    fmt_number(s.get("total_amount", 0))
                ))
            table(rows, ["Ticker", "Trades", "Buys", "Sells", "Buy%", "Sentiment", "Members", "Amount"],
                  [8, 8, 6, 6, 7, 10, 9, 12])

        if trades:
            header("RECENT TRADES")
            _print_congress_trades(trades[:30])


def _print_congress_trades(trades):
    rows = []
    for t in trades:
        ttype = t.get("txn_type", "")
        col = GREEN if "Buy" in ttype else (RED if "Sell" in ttype else DIM)
        rows.append((
            t.get("txn_date", "")[:10],
            (t.get("member_name", "") or "")[:22],
            t.get("ticker", ""),
            _c(ttype, col),
            t.get("amount_str", "") or "N/A",
            t.get("owner", "")
        ))
    table(rows, ["Date", "Member", "Ticker", "Type", "Amount", "Owner"],
          [12, 24, 8, 16, 22, 8])


# ── Crypto ────────────────────────────────────────────────────────────────────

def cmd_crypto(args):
    import fetcher
    sub = args.crypto_sub

    if sub == "market":
        banner("CRYPTO MARKET")
        data = fetcher.get_crypto_market(limit=args.limit or 20)
        if not data:
            print(_c("No data", RED))
            return
        rows = []
        for c in data:
            chg24 = c.get("price_change_percentage_24h", 0) or 0
            chg7d = c.get("price_change_percentage_7d_in_currency", 0) or 0
            rows.append((
                f"#{c.get('market_cap_rank', '?')}",
                c.get("symbol", "").upper(),
                c.get("name", "")[:16],
                fmt_price(c.get("current_price", 0)),
                _c(fmt_pct(chg24), _pnl_color(chg24)),
                _c(fmt_pct(chg7d), _pnl_color(chg7d)),
                fmt_number(c.get("market_cap", 0))
            ))
        table(rows, ["#", "Sym", "Name", "Price", "24h", "7d", "Mkt Cap"],
              [5, 7, 18, 14, 10, 10, 12])

    elif sub == "quote":
        coin = args.coin_id
        banner(f"CRYPTO QUOTE — {coin}")
        data = fetcher.get_crypto_quote(coin)
        if not data:
            print(_c("No data", RED))
            return
        print(f"  Price:    {fmt_price(data.get('price', data.get('current_price', 0)))}")
        print(f"  Mkt Cap:  {fmt_number(data.get('market_cap', 0))}")
        print(f"  Rank:     #{data.get('market_cap_rank', '?')}")
        print(f"  ATH:      {fmt_price(data.get('ath', 0))}")
        print(f"  ATL:      {fmt_price(data.get('atl', 0))}")

    elif sub == "global":
        banner("GLOBAL CRYPTO STATS")
        data = fetcher.get_crypto_global()
        if not data:
            print(_c("No data", RED))
            return
        if isinstance(data, dict):
            d = data.get("data", data)
            print(f"  Total Market Cap:   {fmt_number(d.get('total_market_cap', {}).get('usd', 0))}")
            print(f"  24h Volume:         {fmt_number(d.get('total_volume', {}).get('usd', 0))}")
            print(f"  BTC Dominance:      {(d.get('market_cap_percentage') or {}).get('btc') or 0:.1f}%")
            print(f"  ETH Dominance:      {(d.get('market_cap_percentage') or {}).get('eth') or 0:.1f}%")
            print(f"  Active Coins:       {d.get('active_cryptocurrencies', 0):,}")


# ── Settings ──────────────────────────────────────────────────────────────────

def cmd_settings(args):
    sub = args.settings_sub

    if sub == "list" or sub is None:
        settings = db.get_settings()
        banner("SETTINGS")
        SENSITIVE = ("key", "token", "secret", "password", "credential")
        rows = []
        for k, v in settings.items():
            val = str(v)
            if any(s in k.lower() for s in SENSITIVE) and len(val) > 8:
                val = val[:4] + "…" + val[-4:]
            rows.append((k, val))
        if not rows:
            print(_c("No settings configured", DIM))
            return
        table(rows, ["Key", "Value"], [24, 30])

    elif sub == "set":
        db.set_setting(args.key, args.value)
        print(_c(f"  ✓ {args.key} = {args.value}", GREEN))


# ── AI Analysis ───────────────────────────────────────────────────────────────

def cmd_ai(args):
    import ai_summarizer
    import fetcher
    sub = args.ai_sub

    if sub == "portfolio":
        holdings = db.get_portfolio()
        if not holdings:
            print(_c("Portfolio is empty", YELLOW))
            return
        banner("AI PORTFOLIO ANALYSIS")
        print(_c("  Generating analysis with GPT-4o...", DIM))
        # Build summary
        stock_syms = [h["symbol"] for h in holdings if h["asset_type"] != "crypto"]
        prices = fetcher.get_quotes_batch(stock_syms) if stock_syms else {}
        total_val = 0
        positions = []
        for h in holdings:
            q = prices.get(h["symbol"].upper(), {})
            price = q.get("price", h["avg_cost"])
            val = h["shares"] * price
            total_val += val
            positions.append({**h, "current_price": price, "value": val})

        summary = {
            "total_value": total_val,
            "positions": len(holdings),
        }
        result = ai_summarizer.analyze_portfolio(positions, summary, model=args.model or "gpt-4o")
        if isinstance(result, dict):
            if result.get("analysis"):
                print(f"\n{result['analysis']}")
            elif result.get("error"):
                print(_c(f"  Error: {result['error']}", RED))
        else:
            print(f"\n{result}")

    elif sub == "filing":
        import edgar
        sym = args.symbol.upper()
        banner(f"AI FILING ANALYSIS — {sym}")
        print(_c("  Fetching recent filings...", DIM))
        filings = edgar.get_recent_filings(sym, limit=3)
        if not filings:
            print(_c("No filings found", DIM))
            return
        for f in filings[:1]:
            acc = f.get("accession", "")
            print(_c(f"  Analyzing {f.get('form_type','')} filed {f.get('filing_date','')[:10]}...", DIM))
            # Get filing text and analyze
            cik = edgar.get_cik(sym)
            text = edgar.get_filing_text(cik, acc, f.get("primary_document", ""), f.get("form_type", ""))
            if text:
                result = ai_summarizer.summarize_filing(text, f.get("form_type",""), sym, f.get("description",""))
                if isinstance(result, dict):
                    sig = result.get("signal", "")
                    print(f"\n  Signal: {_c(sig, _signal_color(sig))}")
                    print(f"  Summary: {result.get('summary', 'N/A')}")
                    points = result.get("key_points", [])
                    if points:
                        for p in points:
                            print(f"    • {p}")


# ── Opportunity Scanner ─────────────────────────────────────────────────────

def cmd_scanner(args):
    import opportunity_scanner as scanner
    sub = args.scanner_sub

    if sub == "profile":
        profile = scanner.get_scanner_profile()
        banner("SCANNER PROFILE")
        rows = [(k, str(v)) for k, v in profile.items()]
        table(rows, ["Setting", "Value"], [20, 30])

    elif sub == "scan":
        profile = scanner.get_scanner_profile()
        if not profile.get("strategy"):
            print(_c("  No profile configured. Set one first:", YELLOW))
            print(_c("  augur scanner set --risk moderate --horizon medium --strategy growth", DIM))
            return
        banner("OPPORTUNITY SCANNER")
        print(_c("  Scanning universe — this may take 30-60 seconds...", DIM))
        results = scanner.scan_opportunities(profile, force_refresh=args.force)
        opps = results.get("opportunities", [])
        meta = results.get("meta", {})
        print(f"  Universe: {meta.get('universe_size', '?')} symbols | Strategy: {_c(meta.get('strategy', '?').upper(), GREEN)}")
        print(f"  Found: {_c(str(len(opps)), GREEN)} opportunities\n")
        if not opps:
            print(_c("  No opportunities matched your criteria.", DIM))
            return
        rows = []
        for i, o in enumerate(opps):
            tags = ", ".join(o.get("tags", []))
            tag_color = GREEN if "TOP PICK" in tags else (YELLOW if "EARLY SIGNAL" in tags else DIM)
            comp = o.get("composite", 0)
            comp_color = GREEN if comp >= 70 else (YELLOW if comp >= 50 else DIM)
            rows.append((
                str(i + 1),
                _c(o["symbol"], GREEN),
                (o.get("asset_type", "stock") or "stock").upper(),
                fmt_price(o.get("price")),
                _c(f"{comp:.1f}", comp_color),
                _c(tags, tag_color) if tags else "",
            ))
        table(rows, ["#", "Symbol", "Type", "Price", "Score", "Tags"], [4, 10, 8, 12, 8, 24])

        # Show top catalysts
        for o in opps[:5]:
            catalysts = o.get("catalysts", [])
            if catalysts:
                print(f"  {_c(o['symbol'], GREEN)}: {'; '.join(catalysts[:3])}")

    elif sub == "set":
        profile = scanner.get_scanner_profile()
        if args.risk:
            profile["risk_tolerance"] = args.risk
        if args.horizon:
            profile["horizon"] = args.horizon
        if args.strategy:
            profile["strategy"] = args.strategy
        if args.assets:
            profile["asset_types"] = [a.strip() for a in args.assets.split(",")]
        if args.budget_max is not None:
            profile["budget_max"] = args.budget_max
        scanner.save_scanner_profile(profile)
        print(_c("  ✓ Scanner profile updated", GREEN))

    else:
        banner("OPPORTUNITY SCANNER")
        print(_c("  Subcommands: scan, profile, set", DIM))
        print(_c("  Example: augur scanner scan", DIM))
        print(_c("  Example: augur scanner set --risk aggressive --strategy momentum", DIM))


# ── GEX Flow ─────────────────────────────────────────────────────────────────

def cmd_gex(args):
    import gex_engine
    sym = args.symbol.upper()
    banner(f"GEX FLOW — {sym}")
    print(_c("  Computing gamma exposure...", DIM))
    data = gex_engine.compute_gex(sym)
    if "error" in data:
        print(_c(f"  Error: {data['error']}", RED))
        return
    regime = data.get("gamma_regime", "UNKNOWN")
    regime_color = GREEN if regime == "LONG GAMMA" else (RED if regime == "SHORT GAMMA" else YELLOW)
    print(f"  Net GEX:         {_c(data.get('net_gex_formatted', 'N/A'), regime_color)}")
    print(f"  Gamma Regime:    {_c(regime, regime_color)}")
    flip = data.get("gamma_flip_price")
    print(f"  Gamma Flip:      {fmt_price(flip) if flip else 'N/A'}")
    print(f"  Spot Price:      {fmt_price(data.get('spot_price'))}")
    print(f"  P/C OI Ratio:    {data.get('put_call_oi_ratio', 0):.2f}")
    print(f"  Max Pain:        {fmt_price(data.get('max_pain'))}")
    walls = data.get("top_gamma_walls", [])
    if walls:
        header("TOP GAMMA WALLS")
        rows = []
        for w in walls[:10]:
            wtype = w.get("type", "")
            color = GREEN if wtype == "support" else RED
            rows.append((fmt_price(w.get("strike")), _c(w.get("net_gex_formatted", str(int(w.get("net_gex", 0)))), color), _c(wtype.upper(), color)))
        table(rows, ["Strike", "Net GEX", "Type"], [12, 16, 12])
    hedges = data.get("dealer_hedge_estimates", {})
    if hedges:
        header("DEALER HEDGE ESTIMATES")
        rows = []
        for move, info in sorted(hedges.items()):
            rows.append((move, fmt_price(info.get("price")), fmt_number(abs(info.get("delta_shares", 0)))))
        table(rows, ["Move", "Price", "Delta Shares"], [8, 12, 16])


# ── Contagion Graph ──────────────────────────────────────────────────────────

def cmd_contagion(args):
    import contagion_graph
    sub = args.contagion_sub
    if sub == "graph":
        sym = args.symbol.upper()
        banner(f"CONTAGION GRAPH — {sym}")
        print(_c("  Building dependency graph from SEC filings...", DIM))
        data = contagion_graph.build_graph(sym)
        if "error" in data:
            print(_c(f"  Error: {data['error']}", RED))
            return
        print(f"  Company: {_c(data.get('company_name', sym), GREEN)}")
        print(f"  Sector:  {data.get('sector', 'N/A')}")
        print(f"  Connections: {_c(str(data.get('total_connections', 0)), CYAN)}")
        nodes = data.get("nodes", [])
        if nodes:
            header("CONNECTED COMPANIES")
            rows = []
            for n in nodes[:20]:
                rows.append((_c(n["ticker"], GREEN), n.get("name", "")[:20], n.get("relationship", ""), str(n.get("mention_count", 0))))
            table(rows, ["Ticker", "Name", "Relationship", "Mentions"], [8, 22, 14, 10])
    elif sub == "impact":
        sym = args.symbol.upper()
        event = getattr(args, "event", "earnings_miss")
        banner(f"CONTAGION IMPACT — {sym}")
        print(_c(f"  Assessing {event} contagion...", DIM))
        data = contagion_graph.assess_contagion(sym, event_type=event)
        if "error" in data:
            print(_c(f"  Error: {data['error']}", RED))
            return
        companies = data.get("impacted_companies", [])
        print(f"  Total Impacted: {_c(str(data.get('total_impacted', 0)), YELLOW)}")
        print(f"  High Risk: {_c(str(data.get('high_risk_count', 0)), RED)}")
        if companies:
            header("IMPACTED COMPANIES")
            rows = []
            for c in companies[:15]:
                risk = c.get("contagion_risk", "")
                risk_color = RED if risk == "HIGH" else (YELLOW if risk == "MODERATE" else DIM)
                rows.append((_c(c["ticker"], GREEN), str(c.get("impact_score", 0)), f"{c.get('correlation', 0):.2f}", str(c.get("lag_days", 0)), _c(risk, risk_color)))
            table(rows, ["Ticker", "Impact", "Corr", "Lag(d)", "Risk"], [8, 8, 8, 8, 12])
    else:
        banner("CONTAGION GRAPH")
        print(_c("  Subcommands: graph <symbol>, impact <symbol>", DIM))


# ── Narrative Velocity ───────────────────────────────────────────────────────

def cmd_narrative(args):
    import narrative_engine
    sym = args.symbol.upper()
    banner(f"NARRATIVE VELOCITY — {sym}")
    print(_c("  Analyzing media narrative...", DIM))
    data = narrative_engine.analyze_narrative(sym)
    if "error" in data:
        print(_c(f"  Error: {data['error']}", RED))
        return
    phase = data.get("narrative_phase", "UNKNOWN")
    phase_color = GREEN if phase in ("EMERGENCE", "ACCELERATION") else (YELLOW if phase == "CONSENSUS" else (RED if phase in ("EXHAUSTION", "REVERSAL") else DIM))
    vel = data.get("velocity_score", 0)
    vel_color = GREEN if vel > 20 else (RED if vel < -20 else DIM)
    print(f"  Dominant: {_c(data.get('dominant_narrative', 'N/A'), CYAN)}")
    print(f"  Phase:    {_c(phase, phase_color)}")
    print(f"  Velocity: {_c(f'{vel:+d}', vel_color)} ({data.get('velocity_label', '')})")
    if data.get("contrarian_signal"):
        print(f"  Contrarian: {_c('YES — ' + (data.get('contrarian_detail') or 'high consensus'), RED)}")
    print(f"  Articles: {data.get('article_count', 0)}")
    windows = data.get("windows", {})
    if windows:
        header("WINDOW BREAKDOWN")
        for w in ["7d", "14d", "30d"]:
            wd = windows.get(w, {})
            dom = wd.get("dominant", "N/A")
            pct = wd.get("dominant_pct", 0)
            cnt = wd.get("article_count", 0)
            print(f"  {w:4s}  {_c(dom, CYAN):20s}  {pct:.0f}%  ({cnt} articles)")


# ── Synthetic Insider ────────────────────────────────────────────────────────

def cmd_synthetic_insider(args):
    import synthetic_insider
    sub = args.si_sub
    if sub == "score":
        sym = args.symbol.upper()
        banner(f"SYNTHETIC INSIDER — {sym}")
        print(_c("  Computing composite signal...", DIM))
        data = synthetic_insider.compute_composite(sym)
        if "error" in data:
            print(_c(f"  Error: {data['error']}", RED))
            return
        score = data.get("composite_score", 0)
        score_color = GREEN if score >= 70 else (YELLOW if score >= 50 else DIM)
        alert = data.get("alert_level", "DORMANT")
        alert_color = GREEN if alert == "CRITICAL" else (YELLOW if alert == "CONVERGING" else (CYAN if alert == "AWAKENING" else DIM))
        print(f"  Composite:    {_c(str(score), score_color)}/100")
        print(f"  Alert Level:  {_c(alert, alert_color)}")
        print(f"  Convergence:  {data.get('convergence_count', 0)}/6 channels firing")
        print(f"  Signal:       {data.get('signal', 'N/A')}")
        channels = data.get("channels", {})
        if channels:
            header("CHANNEL BREAKDOWN")
            rows = []
            for name, ch in channels.items():
                s = ch.get("score", 0)
                sc = GREEN if s >= 60 else (YELLOW if s >= 40 else DIM)
                firing = _c("FIRING", GREEN) if ch.get("firing") else _c("quiet", DIM)
                rows.append((name.replace("_", " ").upper(), _c(str(s), sc), firing, ch.get("detail", "")[:40]))
            table(rows, ["Channel", "Score", "Status", "Detail"], [20, 8, 10, 42])
    elif sub == "scan":
        symbols = args.symbols if args.symbols else None
        if not symbols:
            import database as db2
            holdings = db2.get_portfolio()
            symbols = list(set(h["symbol"] for h in holdings if h.get("asset_type") != "crypto"))
        if not symbols:
            print(_c("  No symbols to scan", YELLOW))
            return
        banner("SYNTHETIC INSIDER SCAN")
        print(_c(f"  Scanning {len(symbols)} symbols...", DIM))
        results = synthetic_insider.scan_composite_bulk(symbols[:15])
        rows = []
        for r in results:
            s = r.get("composite_score", 0)
            sc = GREEN if s >= 70 else (YELLOW if s >= 50 else DIM)
            al = r.get("alert_level", "")
            rows.append((_c(r.get("symbol", ""), GREEN), _c(str(s), sc), al, str(r.get("convergence_count", 0))))
        table(rows, ["Symbol", "Score", "Alert", "Conv"], [10, 8, 14, 6])
    else:
        banner("SYNTHETIC INSIDER")
        print(_c("  Subcommands: score <symbol>, scan [symbols...]", DIM))


# ── Reflexivity Detector ─────────────────────────────────────────────────────

def cmd_reflexivity(args):
    import reflexivity_detector
    sym = args.symbol.upper()
    banner(f"REFLEXIVITY DETECTOR — {sym}")
    print(_c("  Detecting feedback loops...", DIM))
    data = reflexivity_detector.detect_loops(sym)
    if "error" in data:
        print(_c(f"  Error: {data['error']}", RED))
        return
    print(f"  Active Loops: {_c(str(data.get('loop_count', 0)), CYAN)}")
    print(f"  Max Strength: {data.get('max_strength', 0)}")
    risk = data.get("overall_risk", "NONE DETECTED")
    risk_color = RED if "HIGH" in risk else (YELLOW if "MODERATE" in risk else DIM)
    print(f"  Overall Risk: {_c(risk, risk_color)}")
    loops = data.get("active_loops", [])
    if loops:
        header("FEEDBACK LOOPS")
        for loop in loops:
            ltype = loop.get("type", "")
            detected = loop.get("detected", False)
            strength = loop.get("strength", 0)
            direction = loop.get("direction", "")
            d_color = GREEN if direction == "VIRTUOUS" else (RED if direction == "VICIOUS" else DIM)
            status = _c("ACTIVE", GREEN) if detected else _c("inactive", DIM)
            print(f"\n  {_c(ltype, CYAN)}  [{status}]")
            if detected:
                print(f"    Strength:  {strength}/100")
                print(f"    Direction: {_c(direction, d_color)}")
                print(f"    Proximity: {loop.get('proximity_to_trigger_pct', 0):.0f}%")
                print(f"    Timeline:  {loop.get('acceleration_timeline', 'N/A')}")
                print(f"    {loop.get('detail', '')}")


# ── Liquidity Monitor ────────────────────────────────────────────────────────

def cmd_liquidity(args):
    import liquidity_monitor
    banner("LIQUIDITY REGIME MONITOR")
    print(_c("  Computing market-wide stress indicators...", DIM))
    data = liquidity_monitor.compute_stress_score()
    if "error" in data:
        print(_c(f"  Error: {data['error']}", RED))
        return
    score = data.get("composite_score", 0)
    regime = data.get("regime", "UNKNOWN")
    regime_color = GREEN if regime == "NORMAL" else (YELLOW if regime == "ELEVATED" else (RED if regime == "WARNING" else MAGENTA))
    print(f"\n  Composite Score:  {_c(str(score), regime_color)}/100")
    print(f"  Regime:           {_c(regime, regime_color)}")
    print(f"  Position Sizing:  {data.get('position_sizing_multiplier', 1.0):.2f}x")
    print(f"  Percentile:       {data.get('percentile_rank', 0):.0f}th")
    rec = data.get("recommendation", "")
    if rec:
        print(f"  Recommendation:   {rec}")
    indicators = data.get("indicators", {})
    if indicators:
        header("STRESS INDICATORS")
        rows = []
        for name, ind in indicators.items():
            s = ind.get("score", 0)
            sc = GREEN if s < 30 else (YELLOW if s < 50 else (RED if s < 70 else MAGENTA))
            w = ind.get("weight", 0)
            rows.append((name.replace("_", " ").upper(), _c(str(s), sc), f"{w:.0%}", ind.get("detail", "")[:45]))
        table(rows, ["Indicator", "Score", "Weight", "Detail"], [22, 8, 8, 46])


# ── Alt-Data Nowcast ─────────────────────────────────────────────────────────

def cmd_alt_data(args):
    import alt_data_engine
    sub = args.ad_sub
    if sub == "nowcast":
        sym = args.symbol.upper()
        banner(f"REVENUE NOWCAST — {sym}")
        print(_c("  Computing alternative data signals...", DIM))
        data = alt_data_engine.nowcast_revenue(sym)
        if "error" in data:
            print(_c(f"  Error: {data['error']}", RED))
            return
        score = data.get("nowcast_score", 0)
        score_color = GREEN if score >= 70 else (YELLOW if score >= 45 else RED)
        prob = data.get("beat_probability", "UNCERTAIN")
        prob_color = GREEN if prob == "LIKELY BEAT" else (RED if prob == "LIKELY MISS" else YELLOW)
        print(f"  Nowcast Score:    {_c(str(score), score_color)}/100")
        print(f"  Beat Probability: {_c(prob, prob_color)}")
        print(f"  Est. Surprise:    {data.get('estimated_surprise_pct', 0):+.1f}%")
        print(f"  Confidence:       {data.get('confidence_level', 'N/A')}")
        print(f"  Signal:           {data.get('signal', 'N/A')}")
        signals = data.get("signals", {})
        if signals:
            header("SIGNAL BREAKDOWN")
            rows = []
            for name, sig in signals.items():
                s = sig.get("score", 0)
                sc = GREEN if s >= 70 else (YELLOW if s >= 45 else DIM)
                w = sig.get("weight", 0)
                rows.append((name.replace("_", " ").upper(), _c(str(s), sc), f"{w:.0%}", sig.get("detail", "")[:40]))
            table(rows, ["Signal", "Score", "Weight", "Detail"], [22, 8, 8, 42])
    elif sub == "scan":
        symbols = args.symbols if args.symbols else None
        if not symbols:
            import database as db2
            holdings = db2.get_portfolio()
            symbols = list(set(h["symbol"] for h in holdings if h.get("asset_type") != "crypto"))
        if not symbols:
            print(_c("  No symbols to scan", YELLOW))
            return
        banner("ALT-DATA SCAN")
        print(_c(f"  Nowcasting {len(symbols)} symbols...", DIM))
        results = alt_data_engine.nowcast_bulk(symbols[:15])
        rows = []
        for r in results:
            s = r.get("nowcast_score", 0)
            sc = GREEN if s >= 70 else (YELLOW if s >= 45 else DIM)
            rows.append((_c(r.get("symbol", ""), GREEN), _c(str(s), sc), r.get("beat_probability", ""), r.get("signal", "")))
        table(rows, ["Symbol", "Score", "Beat Prob", "Signal"], [10, 8, 14, 20])
    else:
        banner("ALT-DATA NOWCAST")
        print(_c("  Subcommands: nowcast <symbol>, scan [symbols...]", DIM))


# ── Serve (start web UI) ─────────────────────────────────────────────────────

def cmd_serve(args):
    port = args.port or 5001
    banner(f"AUGUR WEB UI — http://localhost:{port}")
    from app import app
    app.run(debug=args.debug, host="127.0.0.1", port=port)


# ══════════════════════════════════════════════════════════════════════════════
# ARGUMENT PARSER
# ══════════════════════════════════════════════════════════════════════════════

def build_parser():
    parser = argparse.ArgumentParser(
        prog="augur",
        description="AUGUR — Wealth Intelligence System CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
commands:
  quote             Get live stock quote(s)
  fundamentals      Company fundamentals & ratios
  chart             Price chart with technical indicators
  news              Latest news for a symbol
  search            Search for a symbol by name
  market            Market overview (indices, sectors, movers)
  portfolio         Manage portfolio (list, add, remove, update, export)
  watchlist         Manage watchlist (list, add, remove)
  transactions      Transaction log (list, add, export)
  alerts            Price alerts (list, add, remove, clear)
  analytics         Risk metrics, correlation, stress test, benchmark
  options           Options chain, expiry dates, unusual flow
  dividends         Dividend analysis for portfolio
  macro             Macro economic indicators
  earnings          Earnings calendar & pre-earnings dossier
  intel             SEC filings, insider trades, institutional holdings
  smart-money       Smart Money Convergence Score
  ml-forecast       ML predictive analytics (RF, trend, regime, MR)
  congress          Congressional trading intelligence
  crypto            Crypto market, quotes, global stats
  ai                AI-powered analysis (portfolio, filings)
  scanner           Investment opportunity scanner
  gex               Gamma exposure flow analysis
  contagion         Corporate contagion graph
  narrative         Narrative velocity analysis
  synthetic-insider Synthetic insider composite
  reflexivity       Reflexivity detector
  liquidity         Liquidity regime monitor
  alt-data          Alt-data revenue nowcasting
  settings          View/update app settings
  serve             Start the web UI server
        """,
    )
    sub = parser.add_subparsers(dest="command")

    # quote
    p = sub.add_parser("quote", help="Get stock quote(s)")
    p.add_argument("symbols", nargs="+", help="Ticker symbol(s)")

    # fundamentals
    p = sub.add_parser("fundamentals", help="Company fundamentals")
    p.add_argument("symbol", help="Ticker symbol")

    # chart
    p = sub.add_parser("chart", help="Price chart")
    p.add_argument("symbol", help="Ticker symbol")
    p.add_argument("-p", "--period", default="6mo", help="Period (1mo,3mo,6mo,1y,2y,5y)")
    p.add_argument("-i", "--interval", default="1d", help="Interval (1d,1wk,1mo)")

    # news
    p = sub.add_parser("news", help="Latest news")
    p.add_argument("symbol", help="Ticker symbol")
    p.add_argument("-l", "--limit", type=int, default=10, help="Max articles")

    # search
    p = sub.add_parser("search", help="Search symbols")
    p.add_argument("query", help="Search query")

    # market
    p = sub.add_parser("market", help="Market overview")
    p.add_argument("market_sub", choices=["indices", "sectors", "movers"], help="Subcommand")

    # portfolio
    p = sub.add_parser("portfolio", help="Portfolio management")
    ps = p.add_subparsers(dest="portfolio_sub")
    ps.add_parser("list", help="Show portfolio")
    pa = ps.add_parser("add", help="Add position")
    pa.add_argument("symbol", help="Ticker symbol")
    pa.add_argument("shares", type=float, help="Number of shares")
    pa.add_argument("cost", type=float, help="Average cost per share")
    pa.add_argument("-t", "--type", default="stock", help="Asset type (stock, crypto, etf)")
    pa.add_argument("-n", "--name", help="Company name")
    pa.add_argument("--notes", help="Notes")
    pr = ps.add_parser("remove", help="Remove position")
    pr.add_argument("id", type=int, help="Position ID")
    pu = ps.add_parser("update", help="Update position")
    pu.add_argument("id", type=int, help="Position ID")
    pu.add_argument("--shares", type=float, help="New shares count")
    pu.add_argument("--cost", type=float, help="New avg cost")
    pu.add_argument("--notes", help="Notes")
    pe = ps.add_parser("export", help="Export to CSV")
    pe.add_argument("-o", "--output", help="Output file path")

    # watchlist
    p = sub.add_parser("watchlist", help="Watchlist management")
    ps = p.add_subparsers(dest="watchlist_sub")
    ps.add_parser("list", help="Show watchlist")
    pa = ps.add_parser("add", help="Add to watchlist")
    pa.add_argument("symbol", help="Ticker symbol")
    pa.add_argument("-n", "--name", help="Name")
    pa.add_argument("--alert-high", type=float, help="Alert above price")
    pa.add_argument("--alert-low", type=float, help="Alert below price")
    pr = ps.add_parser("remove", help="Remove from watchlist")
    pr.add_argument("id", type=int, help="Watchlist item ID")

    # transactions
    p = sub.add_parser("transactions", help="Transaction log")
    ps = p.add_subparsers(dest="txn_sub")
    pl = ps.add_parser("list", help="List transactions")
    pl.add_argument("-s", "--symbol", help="Filter by symbol")
    pl.add_argument("-l", "--limit", type=int, default=50, help="Max entries")
    pa = ps.add_parser("add", help="Log transaction")
    pa.add_argument("symbol", help="Ticker symbol")
    pa.add_argument("action", choices=["buy", "sell"], help="Buy or sell")
    pa.add_argument("shares", type=float, help="Number of shares")
    pa.add_argument("price", type=float, help="Price per share")
    pa.add_argument("--fees", type=float, default=0, help="Transaction fees")
    pa.add_argument("--date", help="Date (YYYY-MM-DD)")
    pa.add_argument("--notes", help="Notes")
    pe = ps.add_parser("export", help="Export to CSV")
    pe.add_argument("-o", "--output", help="Output file path")

    # alerts
    p = sub.add_parser("alerts", help="Price alerts")
    ps = p.add_subparsers(dest="alert_sub")
    pl = ps.add_parser("list", help="List alerts")
    pl.add_argument("-a", "--all", action="store_true", help="Include triggered")
    pa = ps.add_parser("add", help="Add alert")
    pa.add_argument("symbol", help="Ticker symbol")
    pa.add_argument("type", choices=["above", "below"], help="Alert type")
    pa.add_argument("price", type=float, help="Trigger price")
    pr = ps.add_parser("remove", help="Remove alert")
    pr.add_argument("id", type=int, help="Alert ID")
    ps.add_parser("clear", help="Clear triggered alerts")

    # analytics
    p = sub.add_parser("analytics", help="Portfolio analytics")
    ps = p.add_subparsers(dest="analytics_sub")
    pr = ps.add_parser("risk", help="Risk metrics")
    pr.add_argument("symbols", nargs="*", help="Symbols (defaults to portfolio)")
    pr.add_argument("-p", "--period", default="1y", help="Period")
    pc = ps.add_parser("correlation", help="Correlation matrix")
    pc.add_argument("symbols", nargs="*", help="Symbols (defaults to portfolio)")
    pc.add_argument("-p", "--period", default="1y", help="Period")
    pt = ps.add_parser("stress", help="Stress test")
    pt.add_argument("-d", "--drop", type=float, default=20, help="Drop percentage")
    pb = ps.add_parser("benchmark", help="Benchmark comparison")
    pb.add_argument("-b", "--benchmark", default="SPY", help="Benchmark symbol")
    pb.add_argument("-p", "--period", default="1y", help="Period")

    # options
    p = sub.add_parser("options", help="Options data")
    ps = p.add_subparsers(dest="options_sub")
    pd = ps.add_parser("dates", help="Expiry dates")
    pd.add_argument("symbol", help="Ticker symbol")
    pc = ps.add_parser("chain", help="Options chain")
    pc.add_argument("symbol", help="Ticker symbol")
    pc.add_argument("expiry", help="Expiry date (YYYY-MM-DD)")
    pf = ps.add_parser("flow", help="Unusual options flow")
    pf.add_argument("symbol", nargs="?", help="Symbol (omit for portfolio scan)")

    # dividends
    sub.add_parser("dividends", help="Dividend analysis")

    # macro
    sub.add_parser("macro", help="Macro indicators")

    # earnings
    p = sub.add_parser("earnings", help="Earnings intelligence")
    ps = p.add_subparsers(dest="earnings_sub")
    pc = ps.add_parser("calendar", help="Upcoming earnings")
    pc.add_argument("symbols", nargs="*", help="Symbols (defaults to portfolio+watchlist)")
    pd = ps.add_parser("dossier", help="Pre-earnings dossier")
    pd.add_argument("symbol", help="Ticker symbol")

    # intel
    p = sub.add_parser("intel", help="SEC intelligence")
    ps = p.add_subparsers(dest="intel_sub")
    pf = ps.add_parser("filings", help="Recent SEC filings")
    pf.add_argument("symbol", help="Ticker symbol")
    pf.add_argument("-l", "--limit", type=int, default=10, help="Max filings")
    pi = ps.add_parser("insiders", help="Insider trading (Form 4)")
    pi.add_argument("symbol", help="Ticker symbol")
    pt = ps.add_parser("institutional", help="Tracked fund holdings")

    # smart-money
    p = sub.add_parser("smart-money", help="Smart Money Convergence Score")
    ps = p.add_subparsers(dest="sm_sub")
    psc = ps.add_parser("score", help="Score one symbol")
    psc.add_argument("symbol", help="Ticker symbol")
    psb = ps.add_parser("scan", help="Scan multiple symbols")
    psb.add_argument("symbols", nargs="*", help="Symbols (defaults to portfolio)")

    # ml-forecast
    p = sub.add_parser("ml-forecast", help="ML predictive analytics")
    p.add_argument("symbol", help="Ticker symbol")

    # congress
    p = sub.add_parser("congress", help="Congressional trading")
    p.add_argument("symbol", nargs="?", help="Filter by ticker")
    p.add_argument("-d", "--days", type=int, default=90, help="Days to look back")
    p.add_argument("--max-pdfs", type=int, default=60, help="Max PDFs to parse")

    # crypto
    p = sub.add_parser("crypto", help="Cryptocurrency data")
    ps = p.add_subparsers(dest="crypto_sub")
    pm = ps.add_parser("market", help="Top coins by market cap")
    pm.add_argument("-l", "--limit", type=int, default=20, help="Number of coins")
    pq = ps.add_parser("quote", help="Coin details")
    pq.add_argument("coin_id", help="CoinGecko coin ID (e.g. bitcoin)")
    ps.add_parser("global", help="Global crypto stats")

    # ai
    p = sub.add_parser("ai", help="AI-powered analysis")
    ps = p.add_subparsers(dest="ai_sub")
    pp = ps.add_parser("portfolio", help="AI portfolio analysis")
    pp.add_argument("-m", "--model", default="gpt-4o", help="OpenAI model")
    pf = ps.add_parser("filing", help="AI filing analysis")
    pf.add_argument("symbol", help="Ticker symbol")

    # settings
    p = sub.add_parser("settings", help="App settings")
    ps = p.add_subparsers(dest="settings_sub")
    ps.add_parser("list", help="Show all settings")
    pset = ps.add_parser("set", help="Update a setting")
    pset.add_argument("key", help="Setting key")
    pset.add_argument("value", help="Setting value")

    # scanner
    p = sub.add_parser("scanner", help="Opportunity scanner")
    ps = p.add_subparsers(dest="scanner_sub")
    psc = ps.add_parser("scan", help="Run opportunity scan")
    psc.add_argument("-f", "--force", action="store_true", help="Force refresh")
    ps.add_parser("profile", help="Show scanner profile")
    pss = ps.add_parser("set", help="Update scanner profile")
    pss.add_argument("--risk", choices=["conservative", "moderate", "aggressive"], help="Risk tolerance")
    pss.add_argument("--horizon", choices=["short", "medium", "long"], help="Investment horizon")
    pss.add_argument("--strategy", choices=["growth", "value", "income", "momentum"], help="Strategy")
    pss.add_argument("--assets", help="Asset types (comma-separated: stocks,etfs,crypto,options)")
    pss.add_argument("--budget-max", type=float, help="Max budget per position")

    # gex
    p = sub.add_parser("gex", help="Gamma exposure flow analysis")
    p.add_argument("symbol", help="Ticker symbol")

    # contagion
    p = sub.add_parser("contagion", help="Corporate contagion graph")
    ps = p.add_subparsers(dest="contagion_sub")
    pg = ps.add_parser("graph", help="Build contagion graph")
    pg.add_argument("symbol", help="Ticker symbol")
    pi = ps.add_parser("impact", help="Assess contagion impact")
    pi.add_argument("symbol", help="Ticker symbol")
    pi.add_argument("-e", "--event", default="earnings_miss", help="Event type")

    # narrative
    p = sub.add_parser("narrative", help="Narrative velocity analysis")
    p.add_argument("symbol", help="Ticker symbol")

    # synthetic-insider
    p = sub.add_parser("synthetic-insider", help="Synthetic insider composite")
    ps = p.add_subparsers(dest="si_sub")
    psc = ps.add_parser("score", help="Score one symbol")
    psc.add_argument("symbol", help="Ticker symbol")
    psb = ps.add_parser("scan", help="Scan multiple symbols")
    psb.add_argument("symbols", nargs="*", help="Symbols (defaults to portfolio)")

    # reflexivity
    p = sub.add_parser("reflexivity", help="Reflexivity detector")
    p.add_argument("symbol", help="Ticker symbol")

    # liquidity
    sub.add_parser("liquidity", help="Liquidity regime monitor")

    # alt-data
    p = sub.add_parser("alt-data", help="Alt-data revenue nowcasting")
    ps = p.add_subparsers(dest="ad_sub")
    psc = ps.add_parser("nowcast", help="Nowcast one symbol")
    psc.add_argument("symbol", help="Ticker symbol")
    psb = ps.add_parser("scan", help="Scan multiple symbols")
    psb.add_argument("symbols", nargs="*", help="Symbols (defaults to portfolio)")

    # serve
    p = sub.add_parser("serve", help="Start web UI server")
    p.add_argument("-p", "--port", type=int, default=5001, help="Port number")
    p.add_argument("--debug", action="store_true", help="Debug mode")

    return parser


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        banner("AUGUR // WEALTH INTELLIGENCE SYSTEM")
        print(_c("  CLI for portfolio tracking, market data, ML forecasts, and more.\n", DIM))
        parser.print_help()
        return

    dispatch = {
        "quote":        cmd_quote,
        "fundamentals": cmd_fundamentals,
        "chart":        cmd_chart,
        "news":         cmd_news,
        "search":       cmd_search,
        "market":       cmd_market,
        "portfolio":    cmd_portfolio,
        "watchlist":    cmd_watchlist,
        "transactions": cmd_transactions,
        "alerts":       cmd_alerts,
        "analytics":    cmd_analytics,
        "options":      cmd_options,
        "dividends":    cmd_dividends,
        "macro":        cmd_macro,
        "earnings":     cmd_earnings,
        "intel":        cmd_intel,
        "smart-money":  cmd_smart_money,
        "ml-forecast":  cmd_ml_forecast,
        "congress":     cmd_congress,
        "crypto":       cmd_crypto,
        "ai":           cmd_ai,
        "scanner":      cmd_scanner,
        "gex":              cmd_gex,
        "contagion":        cmd_contagion,
        "narrative":        cmd_narrative,
        "synthetic-insider": cmd_synthetic_insider,
        "reflexivity":      cmd_reflexivity,
        "liquidity":        cmd_liquidity,
        "alt-data":         cmd_alt_data,
        "settings":     cmd_settings,
        "serve":        cmd_serve,
    }

    func = dispatch.get(args.command)
    if func:
        try:
            func(args)
        except KeyboardInterrupt:
            print(_c("\n  Interrupted.", DIM))
        except Exception as e:
            print(_c(f"\n  Error: {e}", RED))
            if os.environ.get("DEBUG"):
                import traceback
                traceback.print_exc()
    else:
        parser.print_help()


def run_command(command_str):
    """Run a CLI command programmatically and capture output as a string.
    Used by the web terminal API. Returns (output_text, exit_code)."""
    import io
    import contextlib

    argv_backup = sys.argv
    buf = io.StringIO()
    try:
        parts = command_str.strip().split()
        # Handle 'help' as a special case (not an argparse command)
        if parts and parts[0] == "help":
            with contextlib.redirect_stdout(buf):
                parser = build_parser()
                parser.print_help()
            return buf.getvalue(), 0

        sys.argv = ["augur"] + parts
        parser = build_parser()

        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            args = parser.parse_args(parts)

        if not args.command:
            with contextlib.redirect_stdout(buf):
                banner("AUGUR // WEALTH INTELLIGENCE SYSTEM")
                print("  Type a command. Use 'help' for the command list.\n")
                parser.print_help()
            return buf.getvalue(), 0

        dispatch = {
            "quote":        cmd_quote,
            "fundamentals": cmd_fundamentals,
            "chart":        cmd_chart,
            "news":         cmd_news,
            "search":       cmd_search,
            "market":       cmd_market,
            "portfolio":    cmd_portfolio,
            "watchlist":    cmd_watchlist,
            "transactions": cmd_transactions,
            "alerts":       cmd_alerts,
            "analytics":    cmd_analytics,
            "options":      cmd_options,
            "dividends":    cmd_dividends,
            "macro":        cmd_macro,
            "earnings":     cmd_earnings,
            "intel":        cmd_intel,
            "smart-money":  cmd_smart_money,
            "ml-forecast":  cmd_ml_forecast,
            "congress":     cmd_congress,
            "crypto":       cmd_crypto,
            "ai":           cmd_ai,
            "scanner":      cmd_scanner,
            "gex":              cmd_gex,
            "contagion":        cmd_contagion,
            "narrative":        cmd_narrative,
            "synthetic-insider": cmd_synthetic_insider,
            "reflexivity":      cmd_reflexivity,
            "liquidity":        cmd_liquidity,
            "alt-data":         cmd_alt_data,
            "settings":     cmd_settings,
            "serve":        cmd_serve,
        }

        func = dispatch.get(args.command)
        if func:
            with contextlib.redirect_stdout(buf):
                func(args)
        else:
            with contextlib.redirect_stdout(buf):
                parser.print_help()

        return buf.getvalue(), 0
    except SystemExit:
        return buf.getvalue(), 1
    except Exception as e:
        return buf.getvalue() + f"\033[31m  Error: {e}\033[0m\n", 1
    finally:
        sys.argv = argv_backup


if __name__ == "__main__":
    main()
