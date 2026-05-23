# INTEGRATE_MULTIHORIZON.md

How to wire `research_multihorizon.py` + `research_multihorizon.js` into the
AUGUR app. These steps require edits to files this worktree intentionally did
NOT touch (`app.py`, `static/js/app.js`, `templates/index.html`,
`setup_app.py`), so the integrating agent / human applies them.

## 1. `app.py` — add the API route

Pick any free import block near the other `research_*` routes (look for
`research_wikidata` around line 1954). Add an import + a route:

```python
try:
    import research_multihorizon
except Exception as _mh_err:
    research_multihorizon = None
    log.warning("research_multihorizon unavailable: %s", _mh_err)


@app.route("/api/research/horizons/<symbol>")
def research_horizons(symbol):
    if not research_multihorizon:
        return jsonify({"error": "research_multihorizon module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    return jsonify(research_multihorizon.multi_horizon_forecast(symbol.upper()))
```

No DB schema changes. No new environment variables. Caching is handled inside
the module via `cache_store.coalesce(("multihorizon", symbol), 6*3600, ...)`.

## 2. `templates/index.html` — load the JS

In the `<head>` (or wherever the other module JS files are pulled in next to
`app.js`/`charts.js`):

```html
<script src="/static/js/research_multihorizon.js"></script>
```

Then, inside the existing Research view layout, add a panel slot under the
Forecast section. The container ID below is what the loader in step 3 reads.

```html
<div class="panel" id="mh-panel-wrapper">
  <div class="panel-header">
    <div class="panel-title">MULTI-HORIZON FORECAST</div>
  </div>
  <div class="panel-body" id="mh-panel"></div>
</div>
```

If the Research view is built dynamically in `app.js` (it is — see
`switchResearchTab` / the `view-research` HTML template), you can either:

- inject the panel HTML into the same dynamic string that builds the Forecast
  tab (preferred — see step 3), or
- mount it as a sibling panel after Forecast in whichever container holds the
  Research panels.

## 3. `static/js/app.js` — call the renderer

The cleanest hook is wherever the Research-view's Forecast tab is rendered.
Around the existing `loadResearchFor(symbol)` flow, after the panel DOM is in
place, call:

```javascript
if (window.renderMultiHorizon) {
  const mhEl = document.getElementById('mh-panel');
  if (mhEl) window.renderMultiHorizon(mhEl, symbol);
}
```

A safe minimal placement is inside the same handler that already calls the
existing ML forecast fetch — that way the panel only loads when the user is
viewing a specific ticker's research page, not on every navigation.

If you'd rather wire it to a dedicated tab button (e.g. an `HORIZONS` tab next
to `CHART` / `FUNDAMENTALS`), add a button + a tab handler that calls
`renderMultiHorizon(document.getElementById('mh-panel'), State.researchSymbol)`
when activated.

## 4. `setup_app.py` — bundle the module

Add `"research_multihorizon"` to `LOCAL_MODULES` so py2app pulls it into the
macOS bundle:

```python
LOCAL_MODULES = [
    ...
    "ml_forecast",
    "narrative_engine",
    "opportunity_scanner",
    "reflexivity_detector",
    "research_multihorizon",   # ← add this
    "sec_edgar",
    ...
]
```

No changes needed to `EXTRA_PACKAGES`; this module only depends on `numpy`,
`pandas`, and the existing AUGUR modules (`fetcher`, `cache_store`,
`ml_forecast`), all of which are already in the bundle.

## 5. Smoke test (without the UI)

```bash
python3 -c "
import research_multihorizon as mh
r = mh.multi_horizon_forecast('AAPL')
for h, v in r['horizons'].items():
    print(h, v['prob_up'], v['expected_return_pct'], v['method'])
print('consensus:', r['consensus'], 'divergence:', r['divergence_flag'])
"
```

You should see four populated horizons with different `prob_up` values and a
consensus block. First call takes ~3-5s (ml_forecast trains its RF model);
subsequent calls within 6h return from cache instantly.

## 6. Endpoint smoke test (once route is wired)

```
curl -s http://127.0.0.1:5000/api/research/horizons/AAPL | jq '{horizons,consensus,divergence_flag}'
```

## Notes & caveats

- The h5 method is deliberately mean-reversion biased; it is supposed to
  disagree with the long-term trend when price is stretched. That's the
  whole point of the divergence flag.
- h20 reuses `ml_forecast.ml_forecast(symbol)` so it inherits that module's
  ~1h cache. The multi-horizon module's own 6h cache means the RF model is
  retrained at most every 6h per symbol.
- If `cache_store` isn't importable for some reason (e.g. running outside the
  Flask app), `multi_horizon_forecast` still works — it falls through to a
  direct compute.
- If fewer than 200 daily bars are available (recent IPO, illiquid ticker)
  the module returns an error envelope and the UI shows a friendly message.
