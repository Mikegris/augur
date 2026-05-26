# INTEGRATE_SYNTH_BAYESSMART.md

How to wire `synth_bayessmart.py` + `static/js/synth_bayessmart.js` into
the live AUGUR app.

The module is self-contained: it relies on the existing
`smart_money.compute_score` for raw component scores and on the existing
`research_tracker.get_track_record` for live hit-rate data — it never
edits either file. Integration is a single Flask route, one `<script>`
include, a panel mount inside the existing Signals view (or a new
sub-tab in Research), and one line in `setup_app.py`'s `LOCAL_MODULES`.

This worktree could not edit `app.py`, `static/js/app.js`,
`templates/index.html`, or `setup_app.py`. The integrating pass applies
the edits below.

---

## 1. `app.py` — add the route

Pick the import block where the other `research_*` modules are imported.
Add (anywhere alongside them is fine):

```python
try:
    import synth_bayessmart
except Exception as _bs_err:
    synth_bayessmart = None
    log.warning("synth_bayessmart unavailable: %s", _bs_err)
```

Then add the route alongside the other `/api/synth/*` or `/api/research/*`
endpoints (the prefix `/api/synth/` keeps it cleanly grouped with
`synthetic_insider`):

```python
@app.route("/api/synth/bayes-smart-money/<symbol>")
def synth_bayes_smart_money(symbol):
    """Bayesian-reweighted Smart-Money composite — uses live hit-rates
    from research_tracker to dynamically scale each component's weight."""
    if not synth_bayessmart:
        return jsonify({"error": "synth_bayessmart module not available"}), 500
    sym = (symbol or "").upper().strip()
    if not sym or not sym.replace(".", "").replace("-", "").isalnum():
        return jsonify({"error": "Invalid symbol"}), 400
    try:
        result = synth_bayessmart.bayes_smart_money(sym)
        if "error" in result and result.get("static_score") is None:
            return jsonify(result), 200  # surface error to UI, don't 500
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
```

Optional: also log every Bayes call into the tracker so the next run has
even more component history to update from. Drop this **inside** the
route after `synth_bayessmart.bayes_smart_money(sym)`:

```python
    try:
        synth_bayessmart.log_bayes_smart_money(sym, result)
    except Exception:
        pass
```

## 2. `templates/index.html` — load the JS

In the script block where the existing research JS files load (around
line 608, alongside `research_tracker.js`), add one line:

```html
<script src="/static/js/research_tracker.js?v=1"></script>
<script src="/static/js/synth_bayessmart.js?v=1"></script>
```

(Order doesn't matter — synth_bayessmart.js has no dependencies on
research_tracker.js.)

## 3. UI mount — sub-tab in Research view

Two options; pick one.

### Option A (recommended) — new tab in the Signals view

The existing `view-signals` already renders the static Smart-Money grid.
Add a "BAYES SMART MONEY" toggle to its header in `app.js`'s
`loadSignalsView()`:

```javascript
// Inside loadSignalsView()'s view-header HTML, alongside the SCAN button:
'<button class="btn btn-ghost btn-sm" onclick="toggleBayesSmartPanel()">⊕ BAYES MODE</button>'

// Then somewhere after the signals-grid div:
'<div id="bayes-smart-panel" style="display:none;padding:0 24px 24px"></div>'
```

…and a tiny toggle helper added to `app.js`:

```javascript
function toggleBayesSmartPanel() {
  var panel = document.getElementById('bayes-smart-panel');
  if (!panel) return;
  if (panel.style.display === 'none') {
    panel.style.display = 'block';
    // Default to first symbol in cache, or AAPL.
    var sym = (_signalsCache && _signalsCache[0] && _signalsCache[0].symbol) || 'AAPL';
    if (window.renderBayesSmart) window.renderBayesSmart(panel, sym);
  } else {
    panel.style.display = 'none';
  }
}
```

Also clicking a card in `renderSignalsGrid` can re-target the panel:

```javascript
// Inside the card click handler:
if (panel && panel.style.display === 'block' && window.renderBayesSmart) {
  window.renderBayesSmart(panel, s.symbol);
}
```

### Option B — new sub-tab in the Research view

If the Research view has the sub-tab nav (`#sub-nav`) the existing
research modules use, register a new `BAYES SMART MONEY` tab that mounts
into a fresh `<div id="research-bayes-root"></div>`:

```javascript
// In the Research view's sub-tab handler:
{
  id: 'bayes-smart',
  label: 'BAYES SMART MONEY',
  mount: function (containerEl, ctx) {
    if (window.renderBayesSmart) {
      window.renderBayesSmart(containerEl, (ctx && ctx.symbol) || 'AAPL');
    }
  }
}
```

The exact registration shape depends on how the existing 10 research
modules register — pick whichever pattern they use.

## 4. `setup_app.py` — bundle the module

Add to `LOCAL_MODULES`:

```python
LOCAL_MODULES = [
    "app",
    ...
    "synth_bayessmart",
    ...
]
```

`static/js/synth_bayessmart.js` is picked up automatically by
`_walk_tree(HERE / "static", "static")`.

---

## 5. Pre-populate the tracker so the Bayes weights actually move

Out of the box, `research_tracker` has zero scored history for the
per-component signals (`smart_money_insider_activity`, etc.). With no
data, `bayes_smart_money(symbol)` returns `bayes_score ≈ static_score`
because every multiplier collapses to 1.0 — exactly as designed.

To verify dynamic weighting works end-to-end, seed synthetic history:

```python
import synth_bayessmart as bs
print(bs.seed_synthetic_history())
```

This inserts ~265 fake `signal_forecasts` rows tagged
`metadata = {"synthetic": true}`, distributed so that:

- `insider_activity` and `ml_forecast` have 60-65% hit-rates → upweighted
- `earnings_quality` and `sec_sentiment` have 38-42% hit-rates → downweighted
- the rest sit near 50% → barely move

Then run:

```python
import synth_bayessmart as bs
out = bs.bayes_smart_money("AAPL")
print(out["static_score"], out["bayes_score"])
for s in out["weight_shift_summary"]:
    print(s)
```

You should see `bayes_score` diverge from `static_score` by several
points and `weight_shift_summary` flag insider+ml as biggest gainers,
earnings+sec as biggest losers.

To remove the synthetic rows (idempotent — seed re-inserts every time):

```sql
DELETE FROM signal_forecasts
WHERE metadata LIKE '%"synthetic": true%';
```

In production, the natural flow is:

1. The route's optional `log_bayes_smart_money(...)` line records one
   directional call per component per symbol every time the panel opens.
2. `cache_warmer.py` already runs `research_tracker.score_due_forecasts()`
   every 6h (per INTEGRATE_TRACKER.md step 5), which scores those calls
   30 days later.
3. After ~30-60 days of organic use, real hit-rates accrue and the
   Bayes weights start to move.

---

## 6. Caching

`bayes_smart_money(symbol)` is wrapped in
`cache_store.coalesce(("bayessmart", sym), 600, …)` so the 10-min TTL
matches the cadence of the other slow signal panels. The underlying
`smart_money.compute_score` call retains its own internal caching.

---

## 7. API contract

`GET /api/synth/bayes-smart-money/<symbol>` returns:

```json
{
  "symbol": "AAPL",
  "name": "Apple Inc.",
  "price": 213.42,
  "static_score": 65.0,
  "static_signal": "BUY",
  "bayes_score": 71.2,
  "bayes_signal": "BUY",
  "score_delta": 6.2,
  "components": [
    {
      "name": "insider_activity",
      "label": "Insider Activity",
      "score": 14,
      "max": 20,
      "detail": "5 recent Form 4 txns",
      "raw_value": 0.7,
      "normalized": 0.4,
      "static_weight": 0.20,
      "track_record": { "n": 47, "n_directional": 47, "hit_rate": 0.61, "avg_return": 0.018, "last_100_hit_rate": 0.61 },
      "posterior_mean": 0.604,
      "weight_multiplier": 1.33,
      "posterior_weight": 0.265,
      "contribution": 0.106
    }
    // ... one per dimension (7 in total)
  ],
  "weight_shift_summary": [
    { "name": "insider_activity", "label": "Insider Activity",
      "shift": 0.065, "static_weight": 0.20, "posterior_weight": 0.265 },
    { "name": "earnings_quality", "label": "Earnings Quality",
      "shift": -0.041, "static_weight": 0.15, "posterior_weight": 0.109 },
    { "name": "ml_forecast", "label": "ML Forecast",
      "shift": 0.038, "static_weight": 0.20, "posterior_weight": 0.238 }
  ],
  "prior": { "alpha": 20.0, "beta": 20.0, "mean": 0.5, "min_n": 20, "weight_exponent": 1.5 },
  "as_of": "2026-05-26T14:32:11Z"
}
```

Error shape (when `smart_money.compute_score` itself fails):

```json
{
  "symbol": "AAPL",
  "error": "No price data",
  "static_score": null,
  "bayes_score": null,
  "components": [],
  "weight_shift_summary": [],
  "as_of": "2026-05-26T14:32:11Z"
}
```

---

## 8. Verification (3-minute smoke test)

```python
import os
os.environ.setdefault("AUGUR_DB_PATH", "wealth.db")

import synth_bayessmart as bs

# 1) No history yet — bayes_score should hug static_score (±2 typical)
res = bs.bayes_smart_money("AAPL")
print("STATIC:", res["static_score"], "  BAYES:", res["bayes_score"])
print("delta :", res["score_delta"])
assert abs(res["bayes_score"] - res["static_score"]) < 20, "low-history divergence too large"

# 2) Seed synthetic per-component history and rerun.
print(bs.seed_synthetic_history())

# Bust the 10-min cache (cache key is ("bayessmart", "AAPL"))
import cache_store
cache_store.invalidate(("bayessmart", "AAPL")) if hasattr(cache_store, "invalidate") else None

res2 = bs.bayes_smart_money("AAPL")
print("STATIC:", res2["static_score"], "  BAYES:", res2["bayes_score"])
print("delta :", res2["score_delta"])
for s in res2["weight_shift_summary"]:
    print(" ", s["name"], "shift=", s["shift"])

# 3) Hit the API end-to-end:
#    curl http://localhost:5000/api/synth/bayes-smart-money/AAPL
```

If `cache_store` has no `invalidate` helper, simply pick a different
symbol (e.g. `MSFT`) or wait 10 minutes for the cache to expire.

---

## 9. Known caveats / TODO

- **Cold-start = static score.** Until the per-component tracker
  signals have ≥ 20 scored directional calls, the Bayes weights equal
  the static weights and `bayes_score ≈ static_score`. This is correct
  behaviour (we have no evidence to favour any component yet) but the
  UI's "BAYES MODE" panel will look identical to the static panel.
  `seed_synthetic_history()` exists precisely to demonstrate the
  dynamic-weighting path before real data accrues.

- **Reverse-engineered weights.** The static weights baked in to
  `_STATIC_RAW_WEIGHTS` (20/15/15/10/10/10/20) are read straight from
  the docstring at the top of `smart_money.py`. If that module's
  per-component max-scores ever change, this file's weights need to be
  updated in lockstep. A safer long-term fix is to expose them from
  `smart_money.py` as a constant — out of scope for this worktree
  (file-isolation rules).

- **Normalisation choice.** A component's raw output is mapped to
  `[-1..+1]` via `(score - max/2) / (max/2)`. Components whose neutral
  baseline isn't max/2 (e.g. ML's "10 = neutral, 20 = strong bull")
  effectively use the right normalisation already because their
  scoring function centres at max/2. If a future component breaks that
  convention, `_normalize_component` should be made more component-aware.

- **Direction logging for components.** The optional
  `log_bayes_smart_money` writes ONE row per *component* per call. With
  7 components × N panel views, the `signal_forecasts` table grows ~7×
  faster than the existing single-row-per-call signals. Adjust the
  warmer's `max_rows` cap or the call cadence if growth becomes a
  problem.

- **Weight multiplier clamp.** Multiplier is hard-clamped to
  `[0.25, 3.0]` to avoid degenerate cases where one component dominates.
  A component with a true 80% hit-rate over 200 calls would otherwise
  push the multiplier to ~4.0 — empirically rare for stock-market
  signals but worth knowing.
