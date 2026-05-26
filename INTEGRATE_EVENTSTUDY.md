# INTEGRATE_EVENTSTUDY

How to wire the event-study feature into the existing AUGUR app. None of these
edits are made by the worktree branch (the agent stays file-isolated); the
integrator should apply them.

## 1. `app.py` — new Flask route

Add this route alongside the other `/api/research/...` endpoints (near the
existing `research_xbrl` / `research_wikidata` blocks):

```python
# ── Event study ───────────────────────────────────────────────────
try:
    import research_eventstudy
except Exception as _es_err:
    research_eventstudy = None
    log.warning("research_eventstudy unavailable: %s", _es_err)


@app.route("/api/research/event-study/<symbol>")
def research_event_study(symbol):
    if research_eventstudy is None:
        return jsonify({"error": "research_eventstudy module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    event_type   = request.args.get("event_type", "earnings")
    window_days  = _safe_int(request.args.get("window_days"), 10)
    period_years = _safe_int(request.args.get("period_years"), 10)
    benchmark    = request.args.get("benchmark", "SPY")
    return jsonify(research_eventstudy.event_study(
        symbol.upper(),
        event_type=event_type,
        window_days=window_days,
        period_years=period_years,
        benchmark=benchmark,
    ))
```

Note: `_safe_int` and `_valid_ticker` are already defined in `app.py` (used by
neighboring routes). No new imports needed at module top.

## 2. `templates/index.html` — script include and panel container

Add the JS module alongside the existing script tags near the bottom of
`<body>`:

```html
<script src="/static/js/research_eventstudy.js?v=1"></script>
```

(Place it on the line immediately after `<script src="/static/js/app.js?v=9">`
so it can register `window.renderEventStudy` before any Research view fires.)

No HTML container is required — the JS module injects its own `.panel` into
whatever element you pass it. The container is created at runtime by
`app.js` (see step 3).

## 3. `static/js/app.js` — call from Research view

Two small changes are needed inside `loadResearchFor(symbol)`:

### 3a. Add an Event Study tab button

In the `research-tabs` block (currently CHART / FUNDAMENTALS / NEWS /
OPTIONS / SEC / INTEL — around line 1425), append a new tab button:

```html
<button class="research-tab" id="rtab-eventstudy" onclick="switchResearchTab('eventstudy','${symbol}')">EVENT STUDY</button>
```

### 3b. Add the matching panel

After the other `rpanel-*` divs (around line 1500), add:

```html
<!-- Event Study Tab -->
<div id="rpanel-eventstudy" style="display:none">
  <div id="es-container-${symbol}"></div>
</div>
```

### 3c. Extend the tabs list in `switchResearchTab`

In `switchResearchTab` (line ~3662), include `'eventstudy'` in the `tabs`
array, and add a lazy-loader:

```js
const tabs = ['chart', 'fundamentals', 'news', 'options', 'sec', 'intel', 'eventstudy'];
// ...
} else if (tab === 'eventstudy') {
  const container = document.getElementById('es-container-' + symbol);
  if (container && container.childElementCount === 0 && typeof renderEventStudy === 'function') {
    renderEventStudy(container, symbol, 'earnings');
  }
}
```

That keeps the panel from re-fetching on every tab toggle while still
allowing the user's own selector changes inside the panel to re-fetch.

## 4. `setup_app.py` — bundle inclusion

Add `"research_eventstudy"` to the `LOCAL_MODULES` list (alphabetical
ordering puts it between `reflexivity_detector` and `sec_edgar`):

```python
LOCAL_MODULES = [
    ...,
    "reflexivity_detector",
    "research_eventstudy",
    "sec_edgar",
    ...,
]
```

No additions are required to `EXTRA_PACKAGES` — the module only imports
`pandas`, `numpy`, and `yfinance`, all already listed.

## 5. `requirements.txt`

No new dependencies. The module relies on pandas, numpy, and yfinance,
which are all already in `requirements.txt`.

## 6. Smoke test after integration

```bash
# from repo root, with the venv activated
python - <<'PY'
import research_eventstudy as es
r = es.event_study("AAPL", "earnings", window_days=10, period_years=5)
print("n_events=", r["n_events"], "avg_T0=", r["summary"]["avg_T0_return_pct"])
print("avg_curve len=", len(r["average_curve"]))
PY
```

Expected: `n_events` ≈ 18–20 (about 4 quarters/year × 5 years, minus any
window-truncated at the series edges), `avg_T0_return_pct` non-zero, and
`avg_curve len=21` for the default `window_days=10` (covers T-10..T+10).

Then in the running app:
- Open Research → pick any symbol → click `EVENT STUDY` → the panel should
  render with the average / median / IQR fan and a side panel of KPIs and
  best/worst instances.

## Known limitations / TODOs

- **Earnings history depth.** `yfinance.Ticker.earnings_dates` typically
  goes back ~4 years for actively-traded names. For an event study that
  needs more history (e.g. a 10-year view on a long-listed stock), an
  alternative source (e.g. Nasdaq's data feed, or scraping from
  finviz.com/quote.ashx?t=…&p=earnings) would be required. The current
  implementation is honest about what it has — `n_events` reflects what
  was actually matched.

- **Ex-dividend dates.** We currently use the *payment* dates from
  `fetcher.get_dividend_data(...)['history']` because that's the only
  history-bearing endpoint already in the fetcher. For most names these
  are within a few business days of the true ex-div date, which is fine at
  daily-bar resolution but is not the canonical event timestamp. If a
  separate ex-div endpoint is added to `fetcher.py` later, swap the source
  in `research_eventstudy._ex_div_dates`.

- **FOMC calendar.** Hard-coded list of 2010-01-01..2026-12-31 meeting
  dates. The 2026 entries reflect the Fed's published forward schedule;
  re-check annually and extend the `_FOMC_DATES` list when the next
  year's calendar is published.
