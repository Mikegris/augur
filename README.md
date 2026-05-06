# AUGUR

**Wealth Intelligence System** — a local, self-hosted personal stock & crypto tracker styled after a Bloomberg Terminal. Reads omens from a constellation of public signals to forecast investment outcomes.

> *An augur in Roman tradition was a priest who interpreted omens to forecast events. AUGUR does the same — reading insider filings, options flow, social sentiment, narrative phase, ML forecasts, and historical analogs to surface investable ideas.*

```
╔══════════════════════════════════════════════════╗
║   AUGUR // WEALTH INTELLIGENCE SYSTEM            ║
╚══════════════════════════════════════════════════╝
```

---

## Features

- **Random Idea Generator** — One-click "roll the dice" surfaces a random investment idea from a curated 180-name pool or the full ~10,000 SEC-registered ticker universe + ~500 top crypto. Each idea comes with a complete dossier:
  - Composite scanner score across 8 sub-signals (smart money, ML forecast, momentum, fundamentals, insider, congress, options flow, dividend)
  - 30-day ML price forecast with regime detection + mean-reversion signal
  - Narrative phase analysis (emergence / acceleration / consensus / exhaustion / reversal)
  - StockTwits + Reddit sentiment composite
  - Form 4 insider activity (60d window)
  - Congressional trading activity (180d window)
  - Unusual options flow (volume / OI ratio scan)
  - Peer comparison against same-sector comparables (best-in-class to laggard verdict)
  - Historical analog ("last time RSI / vol / trend matched this, returns were…")
  - Position sizing tiers based on your portfolio + idea volatility
  - Portfolio correlation analysis (diversifies / concentrates verdict)
  - Auto-generated trade plan (entry zone, stop, two profit targets, R:R quality)
  - AI-generated thesis (bull case / bear case / conviction / catalyst / suggested action)
- **Portfolio tracking** — multi-account positions, transaction log, P&L, dividend income projection
- **Watchlist** with price alerts
- **Markets dashboard** — indices, sectors, top movers
- **Crypto** — top 250 coins with charts
- **Earnings calendar + AI pre-earnings briefs**
- **SEC intel feed** — 8-K / 10-K / 10-Q / Form 4 with AI summaries
- **Smart-money tracking** — institutional 13F holdings + congressional trades
- **Options flow** — unusual activity scanner
- **Macro dashboard** — VIX, yields, dollar index
- **Stress tests** — portfolio impact under historical drawdowns
- **Pre-warmed dossier pool** — background thread keeps ~200 dossiers ready, so most rolls return instantly
- **Aesthetic** — dark monospaced Bloomberg-Terminal × blockchain-explorer styling

## Screenshots

_(Add screenshots here after first run.)_

## Stack

- **Backend**: Python 3.9+, Flask, SQLite (WAL mode)
- **Data**: yfinance (equities), CoinGecko free API (crypto), SEC EDGAR (filings + Form 4), Reddit + StockTwits (social), no paid APIs required
- **ML**: scikit-learn (RandomForest, regime clustering, mean reversion)
- **Frontend**: vanilla JS SPA, TradingView Lightweight Charts, Chart.js
- **Optional**: OpenAI API key for AI-generated theses & filing summaries (rule-based fallback works without)

## Installation

### Prerequisites

- **Python 3.9 or newer** ([download](https://www.python.org/downloads/)) — the installer will tell you if it's missing and how to install it
- **~500 MB free disk space** for the virtualenv + dependencies
- An internet connection (for the data feeds)
- *(Optional)* an OpenAI API key for AI-generated theses

### Quick install — paste one line

**macOS / Linux** (Terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/Mikegris/augur/main/install.sh | bash
```

**Windows** (PowerShell):

```powershell
iwr -useb https://raw.githubusercontent.com/Mikegris/augur/main/install.ps1 | iex
```

The installer will:
1. Verify Python 3.9+ (and tell you exactly how to install it if missing)
2. Clone the repo into `~/augur` (or `$HOME\augur` on Windows) — set `AUGUR_DIR` to override
3. Create a virtual environment and install dependencies (~2-5 min)
4. Initialize a fresh local database
5. Optionally start the app and open your browser

The app opens at **http://localhost:5001**.

> **Want to read the script before running it?** Just paste the URL into your browser, or run `curl -fsSL https://raw.githubusercontent.com/Mikegris/augur/main/install.sh` (without `| bash`) to print it.

### Manual install

If you'd rather clone yourself:

**macOS / Linux:**

```bash
git clone https://github.com/Mikegris/augur.git
cd augur
./setup.sh        # one-time install (creates venv, installs deps, inits DB)
./run.sh          # starts the app and opens your browser to localhost:5001
```

**Windows:**

```bat
git clone https://github.com/Mikegris/augur.git
cd augur
setup.bat
run.bat
```

### Configuration (optional)

Copy `.env.example` to `.env` and uncomment any of:

```bash
OPENAI_API_KEY=sk-...    # enables AI-powered theses & summaries
PORT=5001                # change the local port (macOS reserves 5000 for AirPlay)
DISABLE_WARMER=1         # disable the background dossier pre-warmer
```

You can also paste an OpenAI key into the in-app **Settings** page — same effect, persisted to the local DB.

## First-time tour

1. Open `http://localhost:5001`
2. Click **PORTFOLIO** → add a position to seed your book
3. Click **IDEAS** → hit **⚄ GENERATE IDEA** to roll a random investment idea with a full dossier
4. Toggle **POOL** between `CURATED (~180)` and `FULL UNIVERSE (~10k+)` to widen the random search space
5. Adjust **STRATEGY LENS** (growth / value / momentum / income) and **MIN SCORE** to bias picks
6. Open **SETTINGS** to add your OpenAI API key (optional, enables AI theses)

## Data & privacy

**All data stays on your machine.** AUGUR is a single Flask process serving `127.0.0.1:5001`; nothing is sent to external servers except the public data APIs it reads from (yfinance, CoinGecko, SEC EDGAR, Reddit, StockTwits) and OpenAI when you've configured a key for AI summaries.

The local SQLite database at `wealth.db` holds your portfolio, transactions, watchlist, alerts, and any settings (including your OpenAI key). It is **gitignored** — running `git status` after using AUGUR will not show it as a candidate to commit. If you ever fork or share this repo, your `wealth.db` stays on your machine.

## Development

```bash
# Smoke tests
./venv/bin/python test_phase5.py
./venv/bin/python test_phase6.py
./venv/bin/python test_phase7.py

# CLI (alternative to the web UI)
./venv/bin/python cli.py quote AAPL
./venv/bin/python cli.py scanner scan
./venv/bin/python cli.py --help
```

## License

[MIT](LICENSE) — do whatever you want, no warranty.

## Disclaimer

AUGUR is a personal research tool. Nothing it surfaces is investment advice. Random idea generation is for *idea generation* — every roll should be your starting point for research, not a trade signal. Past performance, ML forecasts, and historical analogs are illustrative and frequently wrong.

You are responsible for your own trades.
