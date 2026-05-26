# INTEGRATE_SYNTH_CLUSTER.md

How to wire `synth_cluster.py` + `static/js/synth_cluster.js` into the
live AUGUR app. The module is self-contained — it reads from the existing
research modules (`sec_edgar`, `congress`, `ml_forecast`, `narrative_engine`,
`smart_money`, `alt_signals`, `fetcher`, `wiki_attention`,
`reflexivity_detector`) and never writes to them or to the SQLite. The
integration is the standard pattern: a route in `app.py`, a JS include,
a top-level view, and an optional warmer cadence.

This worktree could not edit `app.py`, `static/js/app.js`,
`templates/index.html`, `setup_app.py`, or `cache_warmer.py`. The
integrating pass applies the edits below.

---

## 1. `app.py` — add the import and the two routes

Pick the import block where other research modules are wired in. Add:

```python
try:
    import synth_cluster
except Exception as _sc_err:
    synth_cluster = None
    log.warning("synth_cluster unavailable: %s", _sc_err)
```

Then drop these route handlers in alongside the other `/api/...`
endpoints (anywhere near `research_*` or near `smart_money` works fine):

```python
@app.route("/api/synth/cluster-scan")
def synth_cluster_scan():
    """Multi-source signal cluster scan. Returns symbols where N+
    independent sources agree on a direction. Heavy on cold cache —
    expect 30-90s for the full SP500 top-100 universe."""
    if not synth_cluster:
        return jsonify({"error": "synth_cluster module not available"}), 500
    direction = (request.args.get("direction") or "bullish").lower()
    if direction not in ("bullish", "bearish"):
        direction = "bullish"
    min_sources = _safe_int(request.args.get("min_sources"), 4)
    universe_param = request.args.get("universe") or "sp500_top100"
    # Allow comma-separated tickers in the universe query param
    universe = universe_param if universe_param == "sp500_top100" else \
               [s.strip().upper() for s in universe_param.split(",") if s.strip()]
    try:
        return jsonify(synth_cluster.cluster_scan(
            universe=universe,
            direction=direction,
            min_sources=min_sources,
        ))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/synth/cluster-scan", methods=["POST"])
def synth_cluster_scan_post():
    """POST variant — accepts a custom universe in the JSON body to dodge
    URL-length limits on big watchlist exports. Body shape:
        {"direction": "bullish", "min_sources": 4, "universe": ["AAPL","MSFT",...]}
    """
    if not synth_cluster:
        return jsonify({"error": "synth_cluster module not available"}), 500
    body = request.get_json(silent=True) or {}
    direction = (body.get("direction") or "bullish").lower()
    if direction not in ("bullish", "bearish"):
        direction = "bullish"
    min_sources = _safe_int(body.get("min_sources"), 4)
    universe = body.get("universe")  # list[str] or None
    if universe is not None and not isinstance(universe, list):
        return jsonify({"error": "universe must be a list of symbols"}), 400
    try:
        return jsonify(synth_cluster.cluster_scan(
            universe=universe,
            direction=direction,
            min_sources=min_sources,
        ))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

---

## 2. `templates/index.html` — include the JS

In the block where the other research JS files are loaded (search for
`research_multihorizon.js` or similar), add:

```html
<script src="{{ url_for('static', filename='js/synth_cluster.js') }}"></script>
```

---

## 3. `templates/index.html` — add the top-level view

Add a new view container near the other `<div class="view" id="view-...">`
blocks. Place it under Signals or Research depending on where it fits
best in the IA — recommended under Signals as a peer of "Smart Money":

```html
<div class="view" id="view-cluster" style="display:none">
  <div class="view-header">
    <h2>CLUSTER SCAN</h2>
    <p class="view-subtitle">
      Symbols where multiple independent signal sources are firing in the
      same direction.
    </p>
  </div>
  <div id="cluster-panel"></div>
</div>
```

And add a corresponding nav button in the tab strip (next to the existing
buttons that select views):

```html
<button class="nav-tab" data-view="cluster">CLUSTER</button>
```

---

## 4. `static/js/app.js` — wire the view switch

Find where the view-switch logic dispatches per `data-view` and add:

```javascript
if (viewName === 'cluster') {
  const el = document.getElementById('cluster-panel');
  if (el && typeof window.renderClusterScan === 'function') {
    // Render with defaults; the panel has its own controls inside it
    window.renderClusterScan(el, {direction: 'bullish', min_sources: 4});
  }
}
```

`renderClusterScan` is idempotent — re-calling on the same container
re-initializes the controls. If the existing view dispatcher uses a
switch on `viewName`, just add a `case 'cluster': … break;` arm.

---

## 5. `setup_app.py` — add to `LOCAL_MODULES`

```python
LOCAL_MODULES = [
    ...
    "synth_cluster",
    ...
]
```

So py2app pulls it into the macOS bundle.

---

## 6. `cache_warmer.py` — optional warm cadence (recommended)

A full SP500-top-100 scan takes 30-90s on cold cache. Warm one in the
background every 12h so the first user click is instant. Inside
`_loop()` add a constant + cycle:

```python
CLUSTER_INTERVAL = 12 * 3600   # 12 hours

# ...

if now - _last_cycle.get("cluster_bull", 0) >= CLUSTER_INTERVAL:
    try:
        import synth_cluster
        _safe("cluster_bull", synth_cluster.cluster_scan,
              universe=None, direction="bullish", min_sources=4)
        _last_cycle["cluster_bull"] = time.time()
    except Exception as e:
        log.debug("cluster_bull skipped: %s", e)
    time.sleep(INTER_REQUEST_DELAY)

if now - _last_cycle.get("cluster_bear", 0) >= CLUSTER_INTERVAL:
    try:
        import synth_cluster
        _safe("cluster_bear", synth_cluster.cluster_scan,
              universe=None, direction="bearish", min_sources=4)
        _last_cycle["cluster_bear"] = time.time()
    except Exception as e:
        log.debug("cluster_bear skipped: %s", e)
    time.sleep(INTER_REQUEST_DELAY)
```

The warmer offsets the two scans so EDGAR / yfinance see them at slightly
different times — minimizes the "scan blast" effect.

---

## 7. Smoke test

After integration:

```bash
# Hit the GET endpoint with a small universe
curl 'http://localhost:5000/api/synth/cluster-scan?direction=bullish&min_sources=2&universe=AAPL,MSFT,NVDA,META,GOOGL'

# Or POST with a custom universe
curl -X POST 'http://localhost:5000/api/synth/cluster-scan' \
  -H 'Content-Type: application/json' \
  -d '{"direction":"bullish","min_sources":2,"universe":["NVDA","TSLA"]}'
```

Either should return JSON matching the spec — sorted clusters with per-source
fired/value/note breakdowns. If you get back `clusters: []` with low
`n_sources_firing` values, that's expected on a cold cache because EDGAR
and yfinance both rate-limit aggressively; let the cache warmer prime
once and the second scan will return real clusters.

---

## Bugs & TODOs (worked-around in this worktree)

- **EDGAR `/files/company_tickers.json` 429s under load.** When sec_edgar
  hits this, the insider_form4 component naturally falls back to
  `fired=False, value="no data"`. No fix needed in synth_cluster, but
  the EDGAR module could benefit from a longer back-off + persistent
  CIK cache.

- **`13f_institutional` is a yfinance proxy, not a true 13F sum.** The
  spec wanted "most recent 13F shows positive net flow", but
  `sec_edgar.get_institutional_holdings` takes a *fund* CIK, not a
  ticker — i.e. it reads one fund's holdings, not all funds holding a
  given stock. Aggregating across all 13Fs filing on a ticker would
  need a fresh ETL pipeline. For the MVP we proxy via
  `heldPercentInstitutions` + `shortPercentOfFloat`. Future improvement:
  build a `sec_edgar.get_institutional_flow_for_ticker(symbol)` that
  scans recent 13F filings via the Form Type filter on EDGAR's submissions
  endpoint.

- **`narrative` mapping.** The spec asked for phases ∈ {BREAKOUT, BUILDING}
  but `narrative_engine` emits {ACCELERATION, EMERGENCE, CONSENSUS,
  EXHAUSTION, REVERSAL, DEVELOPING}. We map ACCELERATION→BREAKOUT and
  EMERGENCE→BUILDING, which preserves spec intent. We additionally
  require the dominant bucket to be in the bullish set
  (GROWTH/PROFITABILITY/TURNAROUND/M_AND_A) so an "ACCELERATION/MACRO"
  doesn't fire as bullish.

- **`wiki_attention` direction.** Attention spikes correlate with bullish
  retail moves more often than bearish — but they can also accompany
  scandals. Conservative current behavior: z>1 for bullish, z<-1 for
  bearish (negative-z = drying interest). A future improvement would
  combine with the narrative's dominant bucket to disambiguate.

- **Universe staleness.** `SP500_TOP100` is hardcoded as a May 2026
  snapshot. Refresh ~quarterly. A future improvement is to scrape the
  S&P composition + market caps daily.

- **Cold-cache cluster results.** Because so many upstreams rate-limit
  aggressively, a "first" cluster scan run from a totally cold cache
  returns mostly empty clusters. The warmer cadence in §6 is what makes
  this feature usable in practice.
