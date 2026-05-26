# Wiring `research_iv_density` into AUGUR

Self-contained module — extracts the options-implied risk-neutral density
(Breeden-Litzenberger) from a symbol's call chain at a chosen expiry.
Pairs naturally with probabilistic forecasts: divergence between the
model's predictive distribution and the market-implied PDF is a tradable
view.

Three editing touchpoints are required to integrate it. None of them
should conflict with the other parallel research streams (each stream
owns its own routes/JS module).

---

## 1. `app.py` — register two Flask routes

Add the import + routes near the other `/api/research/*` endpoints (look
for the `research_wikidata` / `research_xbrl` block ~line 1954-2025).

```python
# ── Options-implied risk-neutral density ──────────────────────────
# Breeden-Litzenberger: f(K) = e^(rT) · ∂²C/∂K² extracted from the
# call chain at a chosen expiry. The default route picks the first
# expiry >7d out; the /<expiry> variant accepts any ISO date the chain
# exposes (validate via /api/options/<symbol>/dates).
try:
    import research_iv_density
except Exception as _rnd_err:
    research_iv_density = None
    log.warning("research_iv_density unavailable: %s", _rnd_err)


@app.route("/api/research/rnd/<symbol>")
def research_rnd_default(symbol):
    if not research_iv_density:
        return jsonify({"error": "research_iv_density module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    return jsonify(research_iv_density.risk_neutral_density(symbol.upper()))


@app.route("/api/research/rnd/<symbol>/<expiry>")
def research_rnd_with_expiry(symbol, expiry):
    if not research_iv_density:
        return jsonify({"error": "research_iv_density module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    # `expiry` is path-segmented as YYYY-MM-DD; reject anything that doesn't
    # look like an ISO date to keep the URL surface tight.
    import re
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", expiry):
        return jsonify({"error": "Expiry must be YYYY-MM-DD"}), 400
    return jsonify(research_iv_density.risk_neutral_density(symbol.upper(), expiry))
```

`_valid_ticker` already lives in `app.py` — no extra helpers needed.

---

## 2. `templates/index.html` — load the JS module

Add one `<script>` tag in the same block that loads the other front-end
modules (search for `<script src="static/js/charts.js"></script>` or the
final cluster of `<script>` tags before `</body>`):

```html
<script src="static/js/research_iv_density.js"></script>
```

No CSS additions needed — the module uses the existing `.panel`,
`.data-table`, `.form-select`, `.form-input`, and color CSS variables
from the AUGUR theme.

---

## 3. `static/js/app.js` — hook into the Research view

The Research view already has tab buttons defined around line 1426:

```js
<button class="research-tab" id="rtab-options" onclick="switchResearchTab('options','${symbol}')">OPTIONS</button>
```

Two minimally-invasive ways to expose the new panel:

### Option A (preferred) — new tab "IV DENSITY"

Inject a new tab button alongside `OPTIONS`:

```js
<button class="research-tab" id="rtab-rnd" onclick="switchResearchTab('rnd','${symbol}')">IV DENSITY</button>
```

Add the corresponding `<div id="rpanel-rnd">…</div>` next to the other
`rpanel-*` divs:

```html
<!-- IV Density Tab -->
<div id="rpanel-rnd" style="display:none">
  <div id="rnd-body-${symbol}">
    <div class="loading"><div class="spinner"></div> Loading implied density…</div>
  </div>
</div>
```

Extend the `switchResearchTab` function (~line 3662) to include `'rnd'`
in the tab list and call into the new module on activation:

```js
function switchResearchTab(tab, symbol) {
  const tabs = ['chart', 'fundamentals', 'news', 'options', 'rnd', 'sec', 'intel'];
  // … existing toggle code …
  if (tab === 'rnd') {
    const body = document.getElementById('rnd-body-' + symbol);
    if (body && body.dataset.loaded !== '1') {
      body.dataset.loaded = '1';
      ResearchIVDensity.render(body, symbol);
    }
  }
  // … existing else-if branches …
}
```

The lazy-load (`body.dataset.loaded === '1'` guard) matches the
existing pattern used for the INTEL tab — keeps the chart from
recomputing on every tab switch.

### Option B — append below the existing Options chain panel

Inside the existing `rpanel-options` div, append a second `<div
id="rnd-body-${symbol}">` and call `ResearchIVDensity.render(body,
symbol)` inside `loadOptionsForSymbol` after the chain loads. This
co-locates the chain table and the implied density on the same tab.

Option A is cleaner and keeps the existing options-chain rendering
unchanged. Use B only if you want the density visible without an
extra click.

---

## 4. `setup_app.py` — include in the py2app bundle

Add `"research_iv_density"` to the `LOCAL_MODULES` list near line 21:

```python
LOCAL_MODULES = [
    "app",
    "ai_summarizer",
    # …
    "research_iv_density",   # ← new
    "sec_edgar",
    # …
]
```

---

## 5. `requirements.txt` — no change needed

`scipy` is already pulled in transitively (via `scikit-learn`), and the
module's `from scipy.interpolate import CubicSpline` /
`scipy.integrate.cumulative_trapezoid` imports both work against the
shipped 1.13.1 wheel. `setup_app.py` already lists `scipy` in
`EXTRA_PACKAGES` so the py2app bundle pulls the full scipy tree.

---

## Smoke test after wiring

```bash
# Backend:
curl -s http://localhost:5000/api/research/rnd/SPY | python -m json.tool | head -40
curl -s http://localhost:5000/api/research/rnd/NVDA/2026-06-21 | python -m json.tool | head -40

# Quick Python sanity:
./venv/bin/python -c "
import research_iv_density as rnd
r = rnd.risk_neutral_density('SPY')
print('n_strikes:', r['n_strikes'], 'spot:', r['spot'])
print('p_above+10%:', r['probabilities']['p_above_spot_10pct'])
print('moments:', r['implied_moments'])
print('integral:', r['diagnostics']['density_integral'])
"
```

Front-end smoke: open AUGUR, navigate to Research → any liquid ticker
(SPY/QQQ/NVDA), click `IV DENSITY` tab. You should see a bell-shaped
curve peaking near ATM, the orange dashed spot line, populated moments
+ tail probabilities, and a working probability calculator.

---

## Caveats

* The module uses a *raw* finite-difference Breeden-Litzenberger
  estimator on cubic-spline-smoothed call prices. For very illiquid
  expiries or names with sparse strikes, the second derivative can be
  noisy — the response includes `diagnostics.illiquid_flag` and
  `density_integral` so the UI can warn the user. The frontend already
  surfaces `LOW-LIQ` in the status line when triggered.
* Deep-ITM strikes (K << spot) with near-zero time value are dropped
  before differentiation; otherwise quote noise on essentially-intrinsic
  calls dominates the curvature estimate.
* For longer-dated expiries (≥60 DTE) the implied mean can drift
  meaningfully from spot due to chain truncation — the chain doesn't
  span the full real line, so mass beyond `K_max` or below the cutoff
  is missed.
* The risk-free rate is pulled from `^TNX` (10Y treasury yield) via
  `fetcher.get_quote`, falling back to 4.5% if unavailable. For more
  precise pricing at short tenors a money-market rate would be better,
  but the difference is sub-percent and well within the noise floor of
  the RND estimate.
* No put-call parity merging — calls only, per MVP. Extending to merge
  in OTM puts via parity would improve the left-tail estimate.
