# INTEGRATE_TRACKER.md

How to wire `research_tracker.py` + `static/js/research_tracker.js` into
the live AUGUR app. The tracker module is intentionally self-contained —
it owns its own SQLite table and never edits `database.py` — so the
integration is just route hooks, a JS include, a few one-line log calls
in the existing signal panels, and a cadence in the warmer.

This worktree could not edit `app.py`, `static/js/app.js`,
`templates/index.html`, `setup_app.py`, or `cache_warmer.py`. The
integrating pass applies the edits below.

---

## 1. `app.py` — add the two routes (and one optional debug route)

Pick the import block near the other `research_*` modules (after the six
just-merged ones; look for `research_multihorizon` / similar). Add:

```python
try:
    import research_tracker
    research_tracker.init_tracker_db()  # idempotent — creates signal_forecasts table
except Exception as _rt_err:
    research_tracker = None
    log.warning("research_tracker unavailable: %s", _rt_err)
```

Then drop these route handlers in alongside the other `/api/research/...`
endpoints:

```python
@app.route("/api/research/track/<signal_name>")
def research_track_record(signal_name):
    """Aggregate stats for a tracked signal — powers the inline badge."""
    if not research_tracker:
        return jsonify({"error": "research_tracker module not available"}), 500
    # Whitelist signal_name to alphanum + underscore so a query-param hop
    # can't smuggle a SQL fragment in via the URL.
    if not signal_name or not signal_name.replace("_", "").isalnum():
        return jsonify({"error": "Invalid signal name"}), 400
    since = request.args.get("since")
    symbol = request.args.get("symbol")
    try:
        return jsonify(research_tracker.get_track_record(signal_name, since=since, symbol=symbol))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/track/<signal_name>/calls")
def research_track_calls(signal_name):
    """Latest scored calls for a tracked signal — powers the recent-calls table."""
    if not research_tracker:
        return jsonify({"error": "research_tracker module not available"}), 500
    if not signal_name or not signal_name.replace("_", "").isalnum():
        return jsonify({"error": "Invalid signal name"}), 400
    limit = _safe_int(request.args.get("limit"), 20)
    try:
        calls = research_tracker.get_recent_calls(signal_name, limit=limit)
        return jsonify({"calls": calls, "signal_name": signal_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/research/track/_score", methods=["POST"])
def research_track_score_now():
    """Debug endpoint — force the scoring loop to run synchronously.
    Useful after backdating a row for testing. Safe in production too;
    score_due_forecasts() is idempotent and capped at 200 rows/call."""
    if not research_tracker:
        return jsonify({"error": "research_tracker module not available"}), 500
    try:
        return jsonify(research_tracker.score_due_forecasts())
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

## 2. Four one-liner hook calls in `app.py`

Each existing signal-producing endpoint already has a `result = …compute_x(…)`
line. Add a single best-effort `log_…` call AFTER that line, BEFORE the
`return jsonify(result)`. Wrap each in `try/except` so a tracker hiccup
never breaks the parent panel.

### 2a. Smart Money — `smart_money_score` (currently ~line 1432)

```python
@app.route("/api/smart-money/score/<symbol>")
def smart_money_score(symbol):
    import smart_money
    try:
        result = smart_money.compute_score(symbol.upper())
        if research_tracker:
            try: research_tracker.log_smart_money(symbol.upper(), result)
            except Exception: pass
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

### 2b. ML Forecast — `ml_forecast_route` (currently ~line 1539)

```python
@app.route("/api/smart-money/ml-forecast/<symbol>")
def ml_forecast_route(symbol):
    import ml_forecast as mlf
    try:
        result = mlf.ml_forecast(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        if research_tracker:
            try: research_tracker.log_ml_forecast(symbol.upper(), result)
            except Exception: pass
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

### 2c. GEX — `gex_analysis` (currently ~line 1558)

```python
@app.route("/api/gex/<symbol>")
def gex_analysis(symbol):
    import gex_engine
    try:
        result = gex_engine.compute_gex(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        if research_tracker:
            try: research_tracker.log_gex(symbol.upper(), result)
            except Exception: pass
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

### 2d. Narrative — `narrative_analysis` (currently ~line 1609)

```python
@app.route("/api/narrative/<symbol>")
def narrative_analysis(symbol):
    import narrative_engine
    try:
        result = narrative_engine.analyze_narrative(symbol.upper())
        if "error" in result:
            return jsonify(result), 400
        if research_tracker:
            try: research_tracker.log_narrative(symbol.upper(), result)
            except Exception: pass
        return Response(json.dumps(result, default=str), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

Each `log_*` call is a single insert (~0.3ms locally) and silently no-ops
on bad payloads, so the wrapping `try/except: pass` is paranoia, not
necessity.

## 3. `templates/index.html` — load the JS

In the `<head>`/script block where `app.js` and `charts.js` are pulled
in (around line 608), add ONE line AFTER `app.js`:

```html
<script src="/static/js/charts.js?v=2"></script>
<script src="/static/js/app.js?v=9"></script>
<script src="/static/js/research_tracker.js?v=1"></script>
```

Order matters — `research_tracker.js` only needs the DOM, but loading it
last keeps it cleanly isolated.

The signal views (`#view-smart-money`, `#view-gex`, `#view-narrative`,
the ML forecast tile inside the smart-money card) are populated
dynamically by `app.js`, so the badge DOM hooks have to be inserted
INSIDE those render functions (step 4). No static element IDs to add in
the template — the panels mount and unmount as the user switches views.

If a fixed badge slot is desired in the template anyway (e.g. an
always-visible "today's signal scoreboard"), the simplest spot is to
append this row inside the existing dashboard panel:

```html
<div id="signal-scoreboard" style="display:flex;gap:12px;font-size:11px">
  <span>Smart Money: <span id="track-badge-smart_money"></span></span>
  <span>ML Forecast: <span id="track-badge-ml_forecast"></span></span>
  <span>GEX: <span id="track-badge-gex"></span></span>
  <span>Narrative: <span id="track-badge-narrative"></span></span>
</div>
```

Then call `window.renderTrackBadge(document.getElementById('track-badge-XYZ'), 'XYZ')`
once per signal on dashboard load (see step 4).

## 4. `static/js/app.js` — call the renderer after each panel render

Find the existing per-signal render functions (the cleanest hooks all
already exist in `app.js`). For each, append a one-liner that mounts the
badge into a freshly-inserted `<span>` placeholder.

### 4a. `loadSmartMoneyView` / `renderSmartMoneyCard` (~line 4880)

Inside `renderSmartMoneyCard(s, …)`, in the header `<div>` block where
the symbol + signal pill render (~line 4889 — the `<span class="signal-badge ...">`
line), add a sibling placeholder:

```javascript
// existing:
//   <span class="signal-badge ${signalColors[s.signal] || ''}" ...>${s.signal}</span>
// new — append AFTER the signal-badge span:
//   <span class="sm-track-badge" data-signal="smart_money"></span>
```

Then, once `cardEl.innerHTML = …` has run (i.e. the new HTML is in the
DOM), find every `.sm-track-badge` and call `renderTrackBadge`:

```javascript
container.querySelectorAll('.sm-track-badge').forEach(function(el) {
  if (window.renderTrackBadge) window.renderTrackBadge(el, 'smart_money');
});
```

A safe place for that block is at the bottom of `loadSmartMoneyView`
right before the function returns.

### 4b. ML forecast — `toggleMLPanel` / `renderMLForecastPanel` (~line 4939)

Append a track-badge slot to the panel's header:

```javascript
// In the HTML emitted by renderMLForecastPanel, near the top:
'<div style="display:flex;align-items:center;gap:8px">'
+   '<span style="font-weight:700">ML Forecast</span>'
+   '<span id="track-badge-ml_forecast"></span>'
+ '</div>'
```

After the panel is in the DOM (end of `toggleMLPanel`'s success branch):

```javascript
if (window.renderTrackBadge) {
  window.renderTrackBadge(document.getElementById('track-badge-ml_forecast'), 'ml_forecast');
}
```

### 4c. GEX — `analyzeGex` (~line 6024)

After the GEX results HTML is written to `gex-results` (~line 6028):

```javascript
results.innerHTML = '<span id="track-badge-gex" style="float:right"></span>' + results.innerHTML;
if (window.renderTrackBadge) {
  window.renderTrackBadge(document.getElementById('track-badge-gex'), 'gex');
}
```

### 4d. Narrative — `analyzeNarrative` (~line 6236)

After `results.innerHTML = html;` (~line 6310):

```javascript
var badgeSlot = document.createElement('span');
badgeSlot.id = 'track-badge-narrative';
badgeSlot.style.marginLeft = '8px';
results.querySelector('.panel-header')?.appendChild(badgeSlot);
if (window.renderTrackBadge) {
  window.renderTrackBadge(badgeSlot, 'narrative');
}
```

### Optional: full panel under a signal view

Any time you want the big version (sparkline + recent-20 table — e.g.
under a "Track Record" sub-tab), drop a `<div id="track-record-X">` into
the view and call:

```javascript
window.renderTrackRecord(document.getElementById('track-record-X'), 'smart_money');
```

## 5. `cache_warmer.py` — add a 6h scoring cadence

After the existing `CHART_INTERVAL` block in `_loop()`, add:

```python
# ── score-due forecasts cadence: every 6h ────────────────────────────
# Picks up the queued forecasts whose horizon has elapsed and prices
# each one against fetcher.get_chart_data. Fully self-contained — the
# tracker module owns its own DB connection. Capped at 200 rows/cycle
# so a long backlog can't pin the warmer for minutes.
if now - _last_cycle.get("tracker_score", 0) >= SCORE_INTERVAL:
    try:
        import research_tracker
        _safe("tracker_score", research_tracker.score_due_forecasts)
    except Exception as e:
        log.debug("tracker_score skipped: %s", e)
    time.sleep(INTER_REQUEST_DELAY)
```

…and a constant at the top of the file next to the other intervals:

```python
SCORE_INTERVAL = 6 * 3600         # tracker scoring loop
```

Also surface it in `status()`:

```python
"score_interval": SCORE_INTERVAL,
```

## 6. `setup_app.py` — bundle the module

Add one entry to `LOCAL_MODULES`:

```python
LOCAL_MODULES = [
    "app",
    ...
    "research_tracker",
    ...
]
```

(The `static/js/research_tracker.js` is already picked up by the
`_walk_tree(HERE / "static", "static")` call, so nothing changes there.)

---

## Verification (5-minute smoke test)

After applying the edits above, run the bundled smoke test described in
the task spec:

```python
import os, sqlite3, datetime
os.environ.setdefault("AUGUR_DB_PATH", "wealth.db")
import research_tracker as rt

rt.init_tracker_db()

# Insert 5 fake forecasts
rt.log_forecast("test_signal", "AAPL", 20, "BUY",  0.05, 0.7, 200.0)
rt.log_forecast("test_signal", "MSFT", 20, "BUY",  0.04, 0.7, 400.0)
rt.log_forecast("test_signal", "TSLA", 20, "SELL", -0.03, 0.7, 250.0)
rt.log_forecast("test_signal", "NVDA", 20, "BUY",  0.10, 0.8, 120.0)
rt.log_forecast("test_signal", "SPY",  20, "NEUTRAL", 0.0, 0.5, 480.0)

# Backdate so the horizon has elapsed
conn = sqlite3.connect(os.environ["AUGUR_DB_PATH"])
backdated = (datetime.datetime.now(datetime.timezone.utc) -
             datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
conn.execute("UPDATE signal_forecasts SET issued_at = ? WHERE signal_name='test_signal'", (backdated,))
conn.commit()

print(rt.score_due_forecasts())          # {'scored': 5, 'errors': 0}
print(rt.get_track_record("test_signal"))
print(rt.get_recent_calls("test_signal", limit=5))
```

Then hit `/api/research/track/test_signal` in a browser — JSON should
match the `get_track_record` output above. `/static/js/research_tracker.js`
loads fine in isolation; you can verify the badge renders by opening
DevTools and running:

```javascript
var s = document.createElement('span');
document.body.appendChild(s);
renderTrackBadge(s, 'test_signal');
```

---

## Known caveats

- **NEUTRAL calls are logged but excluded from hit-rate** (their `hit`
  column is left NULL). They DO contribute to `avg_return` because
  having no skill at picking direction still produces a measurable
  realized return.
- **Backfill window is 2 years** — `_close_price_on_or_before` requests
  `2y` of daily bars from `fetcher.get_chart_data`. Forecasts older than
  that with a missing `issue_price` won't score.
- **Scorer is idempotent and capped** at 200 rows per call. If you've
  accumulated more than that, hit the `/api/research/track/_score`
  endpoint a few times in a row, or just wait for the warmer to drain
  the queue over the next few 6h cycles.
- **Direction vocabulary** — `_direction_matches` understands BUY/SELL,
  LONG/SHORT, UP/DOWN, BULL/BEAR, LONG GAMMA / SHORT GAMMA, STRONG BUY,
  STRONG SELL. Anything else is treated as NEUTRAL (logged, not scored).
