# Integrating the Sector-Flow multi-signal heatmap

Three new files ship with this enhancement:

- `synth_sectorflow.py` — multi-source aggregator and composite scorer
- `static/js/synth_sectorflow.js` — sortable table + leader/laggard side cards
- `INTEGRATE_SYNTH_SECTORFLOW.md` — this file

No new Python dependencies. All data sources are existing AUGUR modules
(`fetcher`, `finviz_data`, `narrative_engine`, `congress`, `alt_signals`,
`wiki_attention`, `hn_sentiment`, `research_factors`). Any source can fail
silently and the row still renders with its remaining signals.

## 1. `app.py`

Add a guarded import alongside the other `synth_*` / research module blocks:

```python
try:
    import synth_sectorflow
except Exception as _sf_err:
    synth_sectorflow = None
    log.warning("synth_sectorflow unavailable: %s", _sf_err)
```

Then register the single route:

```python
@app.route("/api/synth/sectorflow")
def synth_sectorflow_route():
    if synth_sectorflow is None:
        return jsonify({"error": "synth_sectorflow module not available"}), 500
    try:
        return jsonify(synth_sectorflow.sector_flow())
    except Exception as e:
        log.exception("sectorflow failure")
        return jsonify({"error": str(e)}), 500
```

The `sector_flow()` call is internally cached via
`cache_store.coalesce(("sectorflow",), 600, ...)` so a typical request
costs zero upstream API calls.

## 2. `templates/index.html`

Add the new view container alongside the others:

```html
<!-- SECTOR FLOW (multi-signal rotation) -->
<div id="view-sectorflow" class="view">
  <div class="loading"><div class="spinner"></div></div>
</div>
```

Load the renderer script after `app.js`:

```html
<script src="{{ url_for('static', filename='js/synth_sectorflow.js') }}"></script>
```

## 3. `static/js/app.js`

Register the new view in the MARKETS group:

```js
markets: {
  label: 'MARKETS',
  items: [
    { view: 'markets',     label: 'Markets' },
    { view: 'sectorflow',  label: 'Sector Flow' },   // ← new
    { view: 'crypto',      label: 'Crypto' },
    { view: 'macro',       label: 'Macro' },
    { view: 'stress',      label: 'Stress Test' },
    { view: 'liquidity',   label: 'Liquidity' },
    { view: 'news',        label: 'News' },
  ],
},
```

And one entry in the `navigate(view)` switch:

```js
case 'sectorflow':    loadSectorFlow(); break;
```

`loadSectorFlow` is already attached to `window` by `synth_sectorflow.js`,
so no extra registration is needed beyond making sure the script tag is
present.

## 4. `setup_app.py`

Add `"synth_sectorflow"` to `LOCAL_MODULES` so py2app bundles it into the
Mac build:

```python
LOCAL_MODULES = [
    "app",
    "ai_summarizer",
    # ...
    "smart_money",
    "synthetic_insider",
    "synth_sectorflow",          # ← new
]
```

## 5. `requirements.txt`

No changes — every upstream this module talks to is already in the lock.

## 6. Verification

```python
import synth_sectorflow as s
out = s.sector_flow()
assert len(out["sectors"]) == 11
print("leader:",  out["leader_sector"])
print("laggard:", out["laggard_sector"])
```

A representative row from a healthy run looks like:

```json
{
  "sector": "Technology",
  "etf": "XLK",
  "price_1d_pct": 0.81,
  "price_5d_pct": 2.14,
  "price_1mo_pct": 5.40,
  "rs_vs_spy_5d": 0.31,
  "rs_vs_spy_1mo": 1.82,
  "narrative_phase": "BULLISH",
  "narrative_velocity": 1.21,
  "factor_1d_return_pct": 0.42,
  "insider_buy_ratio_30d": 0.62,
  "congress_net_30d_usd": 124000.0,
  "reddit_mention_delta_pct": 15.2,
  "hn_polarity": 0.42,
  "wiki_attention_z": 0.81,
  "options_pcr": 0.61,
  "composite_flow_score": 1.42
}
```

Fields that fail to populate land as `null` — the UI treats them as
"unknown" and skips them in both the composite and the colour scale.

## 7. Notes / TODOs

- **Price fields will be `null` when Yahoo rate-limits the daily-chart
  endpoint.** The module gracefully degrades but the composite leans
  heavily on price/RS so it'll be noisy in those moments. The cache
  warmer + the per-key coalesce stampede guard already deal with this on
  warm boots; first-launch cold-starts may see a few empty rows.
- **Reddit mention delta is currently a crude proxy** — count of hot
  posts in a sector-themed subreddit minus a fixed baseline (25 posts
  per subreddit). It's enough to colour-sort sectors against each other,
  but it isn't a true delta vs a trailing window. Replace with a
  rolling-window mention count once `alt_signals` exposes one.
- **Congress sector lookup goes through Finviz fundament HTML.** This is
  one HTTP request per unique ticker, cached for 24h in-process. The
  first call on a busy day can hit a few dozen tickers; subsequent calls
  reuse the cache. If Finviz starts rate-limiting we may need a smaller
  static mapping for the most common congressional holdings.
- **Composite weights are hand-tuned**, not regressed. Pick a window of
  historical rotation events (e.g. 2020 Q2 risk-off, 2022 H1 inflation
  rotation, 2023 AI tech rip) and check whether the score actually
  ranked the leader / laggard correctly before relying on it.
- **Factor return is shared across all sectors** but applied with a
  per-sector tilt weight (see `SECTOR_FACTOR_TILT`). Heuristic — replace
  with the regressed ETF-to-factor beta if you want a tighter signal.
- Sortable headers work in-place without re-fetching. The "as-of" stamp
  reflects the cached snapshot, not the time of the click.
