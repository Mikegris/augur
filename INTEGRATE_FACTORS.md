# Integrating the Fama-French factor module

Three new files ship with this enhancement:

- `research_factors.py` — factor data loader + OLS engine
- `static/js/research_factors.js` — UI renderers
- `data/ff5_momentum_cache.csv` — offline cache of the merged factor frame
  (created on first successful download from the Tuck/Dartmouth library;
  the module also auto-creates it if missing)

No new Python dependencies are required (numpy ships its own linalg; the
stdlib supplies `urllib.request` and `zipfile`).

## 1. `app.py`

Add the module guard and two routes alongside the existing
`/api/research/...` endpoints:

```python
# ── Fama-French factor exposure ──────────────────────────────────
try:
    import research_factors
except Exception as _ff_err:
    research_factors = None
    log.warning("research_factors unavailable: %s", _ff_err)


@app.route("/api/research/factors/<symbol>")
def research_factors_symbol(symbol):
    if not research_factors:
        return jsonify({"error": "research_factors module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    years = _safe_int(request.args.get("years"), 5)
    return jsonify(research_factors.factor_exposure(symbol.upper(), years))


@app.route("/api/research/factors/portfolio", methods=["GET", "POST"])
def research_factors_portfolio():
    if not research_factors:
        return jsonify({"error": "research_factors module not available"}), 500
    years = _safe_int(request.args.get("years"), 5)
    holdings = []
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        holdings = body.get("holdings") or []
    if not holdings:
        # Pull current portfolio from the DB so a plain GET works too.
        try:
            holdings = [
                {"symbol": h["symbol"], "market_value": h.get("market_value", 0)}
                for h in database.get_holdings_with_values()
            ]
        except Exception:
            holdings = []
    return jsonify(research_factors.portfolio_factor_exposure(holdings, years))
```

The portfolio route handles both `GET` (server pulls current book from the
DB) and `POST` (caller supplies an arbitrary holdings list — useful for
what-if analysis).

## 2. `templates/index.html`

In the per-symbol **Research** view, add a panel slot. Anywhere inside the
existing research tab markup, near the Intel / SEC panels:

```html
<section id="factor-panel" class="card">
  <div id="factor-exposure-{{ symbol }}"></div>
</section>
```

If the markup is generated entirely from JS (as `loadResearchFor` does in
the current build), drop a plain anchor:

```html
<div id="factor-exposure-host" class="card"></div>
```

In **Analytics**, add a portfolio-level slot:

```html
<section class="card">
  <div id="portfolio-factor-exposure"></div>
</section>
```

Load the script after `app.js`:

```html
<script src="{{ url_for('static', filename='js/research_factors.js') }}"></script>
```

## 3. `static/js/app.js`

Hook into the existing entry points without editing the rest of the file.

In `loadResearchFor(symbol)`, append:

```js
const el = document.getElementById('factor-exposure-host')
        || document.getElementById('factor-exposure-' + symbol);
if (el && window.renderFactorExposure) {
  window.renderFactorExposure(el, symbol, { years: 3 });
}
```

In `loadAnalyticsView()`, after holdings are loaded:

```js
const el = document.getElementById('portfolio-factor-exposure');
if (el && window.renderPortfolioFactors) {
  // `State.holdings` is the existing portfolio array in this app.
  window.renderPortfolioFactors(el, State.holdings || [], { years: 3 });
}
```

## 4. `setup_app.py`

Add `"research_factors"` to the `LOCAL_MODULES` list so py2app bundles it
into the Mac build:

```python
LOCAL_MODULES = [
    "app",
    "ai_summarizer",
    # ...
    "reflexivity_detector",
    "research_factors",         # ← new
    "sec_edgar",
    # ...
]
```

The `data/ff5_momentum_cache.csv` file should also be added to the
py2app `data_files` block if you want offline-first behaviour in the
shipped DMG; otherwise the first run will fetch the CSVs from the Tuck
site as usual.

## 5. `requirements.txt`

No changes — numpy is already in the stack, and the factor download uses
stdlib `urllib.request` + `zipfile`.

## 6. Verification

```python
import research_factors as f
print(f.factor_exposure("SPY", 3)["exposures"])
# Expect Mkt-RF beta ~ 1.0, all others |beta| < 0.1
```

Sample tested output (2026-05 run):

```
Mkt-RF beta=0.997  t=321  p=0.000   ← market beta as expected
SMB    beta=-0.08  t=-17  p=0.000
HML    beta=-0.01  t=-1.2 p=0.230
RMW    beta= 0.04  t= 6.3 p=0.000
CMA    beta= 0.02  t= 3.4 p=0.001
Mom    beta=-0.02  t=-5.3 p=0.000
R²=0.9946, alpha_annual=-1.5%
```

## 7. Notes / TODOs

- The Ken French CSVs lag the live market by roughly 2 months — so the
  most recent ~40 trading days of a stock's price history will be
  dropped from the regression (no factor row to align with). For a
  3-year period this leaves ~700 valid daily observations, which is
  still plenty.
- Add a small "as-of" disclaimer in the UI if the gap matters to your
  users (the `as_of` field on the response already exposes it).
- The portfolio aggregator currently re-runs the per-symbol regression
  for every holding on each portfolio call (results are cached for 24h,
  so the cost only hits the first call per day per ticker). If you ever
  want a faster path, factor out `_load_factor_data` + `_stock_log_returns`
  into a single in-memory frame and run one big joint regression.
- The HAC/Newey-West standard-error fix would tighten the t-stats on
  factors with autocorrelated residuals (especially momentum). Out of
  scope for v1.
