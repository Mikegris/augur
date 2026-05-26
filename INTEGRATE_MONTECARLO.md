# Integrating `research_montecarlo`

This document describes the **minimal edits** needed in the existing
AUGUR files (`app.py`, `templates/index.html`, `static/js/app.js`,
`setup_app.py`) to surface the Monte Carlo portfolio-simulation panel
shipped in this branch. All other code is self-contained in
`research_montecarlo.py` and `static/js/research_montecarlo.js`.

No new Python or JS dependencies are introduced — numpy, pandas,
yfinance, and Chart.js are already part of the stack.

---

## 1. `app.py` — two new routes

Drop the following block somewhere alongside the other
`/api/research/...` handlers (e.g. just after the `research_xbrl` route
around line 2025):

```python
# ── Monte Carlo portfolio simulation ──────────────────────────────
try:
    import research_montecarlo as mc
except Exception as _mc_err:  # pragma: no cover
    mc = None
    log.warning("research_montecarlo unavailable: %s", _mc_err)


def _holdings_from_portfolio(account_id=None):
    """Return [{symbol, market_value}, ...] from the user's current
    portfolio, freshly priced. Mirrors the enrichment in get_portfolio()
    but only keeps the two fields the Monte Carlo engine needs."""
    rows = db.get_portfolio(account_id=account_id)
    if not rows:
        return []
    stock_syms  = [h["symbol"] for h in rows if h["asset_type"] != "crypto"]
    crypto_syms = [h["symbol"] for h in rows if h["asset_type"] == "crypto"]
    prices = {}
    if stock_syms:
        prices.update(fetcher.get_quotes_batch(stock_syms))
    if crypto_syms:
        cp = fetcher.get_quotes_batch([s + "-USD" for s in crypto_syms])
        for s in crypto_syms:
            v = cp.get((s + "-USD").upper())
            if v:
                prices[s] = v
    out = []
    for h in rows:
        q = prices.get(h["symbol"]) or {}
        px = q.get("price")
        if not px:
            continue
        mv = px * h["shares"]
        if mv <= 0:
            continue
        # For crypto, MC uses the yfinance "<SYM>-USD" symbol because
        # that's the only feed it can pull history from.
        sym = h["symbol"] + "-USD" if h["asset_type"] == "crypto" else h["symbol"]
        out.append({"symbol": sym, "market_value": round(mv, 2)})
    return out


@app.route("/api/research/montecarlo", methods=["POST"])
def research_montecarlo():
    if not mc:
        return jsonify({"error": "research_montecarlo module not available"}), 500
    data = request.get_json(force=True, silent=True) or {}
    holdings = data.get("holdings") or []
    if not isinstance(holdings, list) or not holdings:
        return jsonify({"error": "holdings: non-empty list required"}), 400
    horizon = _safe_int(data.get("horizon_days"), 365)
    n_paths = _safe_int(data.get("n_paths"), 10000)
    method  = (data.get("method") or "historical_bootstrap").strip()
    seed    = data.get("seed")
    if seed is not None:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            seed = None

    sim = mc.simulate_portfolio(
        holdings, n_paths=n_paths, horizon_days=horizon,
        method=method, seed=seed,
    )

    # Optional: piggy-back a prob_of_target result if the client supplied one.
    target = data.get("target_nav")
    if target is not None:
        try:
            pt = mc.prob_of_target(
                holdings, float(target), horizon_days=horizon,
                n_paths=n_paths, method=method, seed=seed,
            )
            sim["prob_of_target"] = pt
        except (TypeError, ValueError):
            pass
    return jsonify(sim)


@app.route("/api/research/montecarlo/portfolio", methods=["GET"])
def research_montecarlo_portfolio():
    """Convenience: derive holdings from the user's current portfolio."""
    if not mc:
        return jsonify({"error": "research_montecarlo module not available"}), 500
    acct = request.args.get("account_id")
    holdings = _holdings_from_portfolio(
        account_id=_safe_int(acct, None) if acct else None
    )
    if not holdings:
        return jsonify({"error": "No portfolio holdings available"}), 400
    horizon = _safe_int(request.args.get("horizon_days"), 365)
    n_paths = _safe_int(request.args.get("n_paths"), 10000)
    method  = request.args.get("method") or "historical_bootstrap"
    sim = mc.simulate_portfolio(
        holdings, n_paths=n_paths, horizon_days=horizon, method=method,
    )
    return jsonify(sim)
```

Both routes accept either explicit holdings (POST) or auto-derived from
the live portfolio (GET).

---

## 2. `templates/index.html` — script tag + view container

**Add a `<script>` tag** near the other JS includes (after `app.js` is
included, since `research_montecarlo.js` uses `API`, `Toast`, `fmt`,
`State`):

```html
<script src="/static/js/research_montecarlo.js"></script>
```

(If `app.js` is the only existing script tag in `<head>` / before
`</body>`, place this one **immediately after it** so the IIFE has the
globals it needs.)

**Add the view container** in the `<main id="main">` block — for
example just below the `<!-- STRESS TEST -->` panel:

```html
<!-- MONTE CARLO -->
<div id="view-montecarlo" class="view">
  <div class="loading"><div class="spinner"></div></div>
</div>
```

---

## 3. `static/js/app.js` — register the route

### 3a. Add it to `NAV_GROUPS.research`

Inside the `research` group's `items` array (currently `Research`,
`Analytics`, `Intel`, …), add an entry. Suggested placement: right after
"Analytics" so the Monte Carlo cone sits next to the portfolio risk
metrics.

```js
{ view: 'montecarlo', label: 'Monte Carlo' },
```

### 3b. Add a case in `navigate()`'s switch

```js
case 'montecarlo':       loadMonteCarlo(); break;
```

### 3c. Add a `VIEW_LOADERS` entry *(optional)*

So the panel quietly auto-refreshes along with the rest of the
dashboard:

```js
montecarlo:   () => loadMonteCarlo(),
```

Skip 3c if you'd rather the 10k-path simulation only run when the user
explicitly clicks the "RUN" button — the panel always offers that.

No new function declarations are needed: `loadMonteCarlo()` is exposed
by `research_montecarlo.js` as a window global.

---

## 4. `setup_app.py` — bundle for py2app

Add `"research_montecarlo"` to the `LOCAL_MODULES` list (alphabetical
placement, between `reflexivity_detector` and `sec_edgar`):

```python
LOCAL_MODULES = [
    ...
    "reflexivity_detector",
    "research_montecarlo",
    "sec_edgar",
    ...
]
```

`numpy`, `pandas`, `yfinance` are already listed in `EXTRA_PACKAGES`,
so no further changes are needed there.

---

## 5. Verification

After applying the edits above, the following commands should work
end-to-end:

```bash
# 1. Backend round-trip
curl -s -X POST http://127.0.0.1:5050/api/research/montecarlo \
    -H 'Content-Type: application/json' \
    -d '{"holdings":[{"symbol":"AAPL","market_value":10000},{"symbol":"GLD","market_value":10000}],"horizon_days":90,"n_paths":2000}' | jq '.terminal_distribution,.summary'

# 2. Auto-portfolio variant
curl -s 'http://127.0.0.1:5050/api/research/montecarlo/portfolio?horizon_days=90'

# 3. Python sanity check
python -c 'import research_montecarlo as mc; print(mc.simulate_portfolio([{"symbol":"AAPL","market_value":10000},{"symbol":"GLD","market_value":10000}], n_paths=2000, horizon_days=90, seed=42)["summary"])'
```

In the UI: open `RESEARCH → Monte Carlo`, pick a horizon (30/90/365),
click `▶ RUN`. The center pane fills with a percentile-cone chart; the
right pane shows the terminal distribution + probability summary +
"probability of hitting target" calculator.

---

## TODOs / known limitations

- **Crypto symbols**: the `_holdings_from_portfolio` helper rewrites
  crypto symbols to the `XYZ-USD` yfinance format so the historical
  return fetch works. If you'd rather route crypto history through
  CoinGecko (the path already used for spot quotes in `fetcher.py`),
  extend `research_montecarlo._fetch_returns` with a CG branch keyed on
  the raw symbol.
- **Caching ignores `seed`**: the cache key intentionally excludes
  `seed` so repeated panel re-renders hit cache. Callers that pin a
  seed for reproducibility will bypass the cache (`simulate_portfolio`
  detects this).
- **No CSP / nonce**: the script tag is a plain local include — if you
  later add a Content-Security-Policy, allow `'self'` for scripts.
- **Block size = 1 day**. We bootstrap individual days to preserve
  cross-asset correlation but lose any *serial* correlation (volatility
  clustering, trend persistence). A future enhancement could expose
  a `block_size` parameter (5–20 days) for users who want fatter tails.
