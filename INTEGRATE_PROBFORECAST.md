# Integrating `research_probforecast`

This document describes the **minimal edits** needed in the existing
AUGUR files (`app.py`, `templates/index.html`, `static/js/app.js`,
`setup_app.py`) to surface the probabilistic-forecast panel shipped in
this branch. All other code is self-contained in
`research_probforecast.py` and `static/js/research_probforecast.js`.

No new Python or JS dependencies are introduced — numpy, fetcher, and
Chart.js are already part of the stack.

---

## 1. `app.py` — two new routes

Drop the following block alongside the other `/api/research/...`
handlers (e.g. just after `research_montecarlo`):

```python
# ── Probabilistic forecast (block-bootstrap horizon distribution) ──
try:
    import research_probforecast as pf
except Exception as _pf_err:  # pragma: no cover
    pf = None
    log.warning("research_probforecast unavailable: %s", _pf_err)


@app.route("/api/research/probforecast/<symbol>", methods=["GET"])
def research_probforecast(symbol):
    if not pf:
        return jsonify({"error": "research_probforecast module not available"}), 500
    horizon = _safe_int(request.args.get("horizon"), 20)
    n_boot  = _safe_int(request.args.get("n"), 2000)
    out = pf.prob_forecast(symbol, horizon_days=horizon, n_bootstrap=n_boot)
    return jsonify(out)


@app.route("/api/research/probforecast/<symbol>/vs-point", methods=["GET"])
def research_probforecast_vs_point(symbol):
    if not pf:
        return jsonify({"error": "research_probforecast module not available"}), 500
    horizon = _safe_int(request.args.get("horizon"), 20)
    out = pf.compare_to_point(symbol, horizon_days=horizon)
    return jsonify(out)
```

`_safe_int(value, default)` is already defined further up in `app.py`
(it’s used by the Monte Carlo handler). If your branch renames it,
substitute the local equivalent.

---

## 2. `templates/index.html` — script tag + view container

**Add a `<script>` tag** near the other research JS includes (after
`app.js` so `renderProbForecast` is registered on `window` before any
caller in `app.js` fires):

```html
<script src="/static/js/research_probforecast.js"></script>
```

**Add a subsection inside the existing Research view** (the
`#view-research` container). Place it under whatever sibling subsection
already exists for ML/forecast content:

```html
<!-- PROBABILISTIC FORECAST -->
<section class="panel mb-8">
  <div class="panel-header">
    <span class="panel-title">PROBABILISTIC FORECAST</span>
    <div class="flex gap-8 align-center">
      <span style="font-size:10px;color:var(--text-dim)">HORIZON:</span>
      <select class="form-select" data-pf-horizon
              style="font-size:11px;padding:2px 6px">
        <option value="5">5D</option>
        <option value="20" selected>20D</option>
        <option value="60">60D</option>
        <option value="120">120D</option>
      </select>
    </div>
  </div>
  <div id="probforecast-panel" class="panel-body"></div>
</section>
```

(The `data-pf-horizon` `<select>` is hooked up in step 3b. If you'd
rather hard-code a single horizon, drop the select and pass it directly
in step 3a.)

---

## 3. `static/js/app.js` — call from the research view loader

### 3a. Invoke `renderProbForecast` when the research view loads

Find the function that paints the Research view (typical name in this
codebase: `loadResearchView()`, `showResearch()`, or a `case 'research'`
branch in `navigate()`). At the bottom of that flow, after the rest of
the research panels have rendered, add:

```js
const pfHost = document.getElementById('probforecast-panel');
if (pfHost && window.renderProbForecast) {
  const sel = document.querySelector('[data-pf-horizon]');
  const horizon = sel ? parseInt(sel.value, 10) || 20 : 20;
  // State.activeSymbol / State.selectedSymbol / State.symbol — whichever
  // the rest of the research view already uses.
  const sym = (State && (State.activeSymbol || State.symbol)) || 'AAPL';
  window.renderProbForecast(pfHost, sym, horizon);
}
```

### 3b. Re-render on horizon change

Either delegate from `<body>` or attach directly once:

```js
document.addEventListener('change', (e) => {
  if (!e.target || !e.target.matches('[data-pf-horizon]')) return;
  const pfHost = document.getElementById('probforecast-panel');
  if (!pfHost || !window.renderProbForecast) return;
  const sym = (State && (State.activeSymbol || State.symbol)) || 'AAPL';
  const horizon = parseInt(e.target.value, 10) || 20;
  window.renderProbForecast(pfHost, sym, horizon);
});
```

### 3c. (Optional) `VIEW_LOADERS` entry

If the research view uses a `VIEW_LOADERS` map for auto-refresh, no new
entry is needed — `renderProbForecast` lives inside the existing
research loader and gets re-fired whenever the view repaints.

---

## 4. `setup_app.py` — bundle for py2app

Add `"research_probforecast"` to the `LOCAL_MODULES` list (alphabetical
placement, between `research_optimizer` / `research_multihorizon` and
`sec_edgar` — wherever the other `research_*` modules live in your tree):

```python
LOCAL_MODULES = [
    ...
    "reflexivity_detector",
    "research_eventstudy",
    "research_factors",
    "research_iv_density",
    "research_montecarlo",
    "research_multihorizon",
    "research_optimizer",
    "research_probforecast",          # ← add this
    "sec_edgar",
    ...
]
```

`numpy` and `fetcher` are already in scope; no `EXTRA_PACKAGES` change.

---

## 5. Verification

After applying the edits above, the following commands should work
end-to-end:

```bash
# 1. Backend round-trip
curl -s 'http://127.0.0.1:5050/api/research/probforecast/AAPL?horizon=20' \
  | jq '.distribution, .probabilities, (.histogram|length)'

# 2. Point-comparison variant
curl -s 'http://127.0.0.1:5050/api/research/probforecast/AAPL/vs-point?horizon=20' \
  | jq '.point, .spread_vs_median_pct, .probabilistic.distribution'

# 3. Python sanity check (matches the MIN VIABLE SUCCESS spec)
python -c 'import research_probforecast as pf; \
r = pf.prob_forecast("AAPL", 20); \
print(r["distribution"], r["probabilities"]); \
assert r["distribution"]["p10"] < r["distribution"]["median"] < r["distribution"]["p90"]'
```

In the UI: open `RESEARCH → Research`, the "PROBABILISTIC FORECAST"
panel appears. It shows a horizontal density bar (outer band = p5–p95,
inner band = p25–p75, white line = median), a "Probability of …"
table, and a small histogram below.

---

## TODOs / known limitations

- **Cache key is `(symbol, horizon)`** — `n_bootstrap` is intentionally
  *not* part of the key so a user toggling between defaults still hits
  cache. If a caller needs a larger run, clear the cache via
  `cache_store.clear()` or wait the 4 h TTL.
- **Block size is fixed at 5.** The Politis–Romano stationary
  bootstrap with a random geometric block length is a natural next
  step; the current implementation uses fixed-length blocks for speed
  and clarity. Exposing `block_size` as a kwarg would be a one-line
  change.
- **Heavy-tail risk in short windows.** A 60-bar minimum is enforced;
  with fewer than ~6 months of history the tail percentiles (p05/p95)
  are statistically noisy. The UI doesn't currently flag this — a
  future enhancement could add a confidence dot when
  `input_window_days < 120`.
- **No CSP / nonce**: the script tag is a plain local include — if you
  later add a Content-Security-Policy, allow `'self'` for scripts.
- **`compare_to_point` re-runs `ml_forecast`** which has its own cache;
  it does *not* duplicate the work, but on a cold-start the first call
  is slower than the plain `prob_forecast` route.
