# INTEGRATE — Pattern-Grounded Hypotheses (`synth_groundhyp`)

This module adds **retrieval-augmented hypothesis generation** to AUGUR.
Before calling the LLM, it extracts the current symbol's signal pattern
as a normalized feature vector, retrieves the closest historical
analogs from `signal_forecasts` (owned by `research_tracker`), summarises
their realized outcomes, and only then asks `gpt-4o-mini` to refine a
falsifiable hypothesis grounded in those cases.

The module is **read-only** with respect to every existing file. The only
DB write it owns is an idempotent `ALTER TABLE` that adds an optional
`pattern_vector_json` column to `signal_forecasts` (see
**Schema extension** below).

---

## 1. Files shipped

| File | Purpose |
|---|---|
| `synth_groundhyp.py` | Core engine. Public: `grounded_hypothesis(symbol) -> dict`, `attach_pattern_to_forecast(forecast_id, vector) -> bool`. |
| `static/js/synth_groundhyp.js` | UI. Exposes `window.renderGroundedHypothesis(containerEl, symbol)`. |
| `INTEGRATE_SYNTH_GROUNDHYP.md` | This file. |

No edits to existing files are required for the engine to work; the steps
below describe how to wire it into the app surface.

---

## 2. Backend integration (`app.py`)

Add the module to the optional-import block at the top of `app.py`,
matching the pattern used by `research_hypothesis`:

```python
try:
    import synth_groundhyp
except Exception as _sgh_err:
    synth_groundhyp = None
    log.warning("synth_groundhyp unavailable: %s", _sgh_err)
```

Then add a single route. Use `POST` because the handler may call OpenAI:

```python
@app.route("/api/synth/grounded-hypothesis/<symbol>", methods=["POST"])
def synth_grounded_hypothesis_route(symbol):
    if synth_groundhyp is None:
        return jsonify({"error": "synth_groundhyp module not available"}), 500
    try:
        return jsonify(synth_groundhyp.grounded_hypothesis(symbol))
    except Exception as e:
        log.exception("grounded_hypothesis failed")
        return jsonify({"error": str(e)}), 500
```

No tracker hook-ups need updating — the module reads `signal_forecasts`
as-is and gracefully tolerates an empty table (`n_analogs = 0`).

### Optional richer logging (recommended once stable)

To improve retrieval quality, callers that already log to
`research_tracker` can also stash a pattern vector via:

```python
fid = research_tracker.log_smart_money(symbol, sm_result)
if fid is not None:
    # Best-effort feature snapshot at issue time.
    vec, _raw = synth_groundhyp._extract_pattern(symbol)
    synth_groundhyp.attach_pattern_to_forecast(fid, vec)
```

This step is **not required** — the module falls back to parsing
`metadata` for older rows.

---

## 3. Frontend integration

### 3a. Load the JS module

In `templates/index.html`, alongside the other research module imports:

```html
<script src="/static/js/synth_groundhyp.js?v=1"></script>
```

### 3b. Mount the view

The simplest option is to add a sub-tab under the existing Research Lab.
Following the pattern of `loadHypothesisLab()` in `static/js/app.js`,
introduce a sibling tab and renderer:

```js
function loadGroundedHypothesis() {
  const root = document.getElementById('grounded-hyp-root');
  if (!root) return;
  if (typeof window.renderGroundedHypothesis === 'function') {
    // Symbol arg is optional; if you have a "current symbol" in app state,
    // pass it through so the panel runs on mount.
    try {
      window.renderGroundedHypothesis(root, (window.State && State.symbol) || '');
    } catch (e) {
      root.innerHTML = '<div style="color:var(--red);padding:24px">' +
        (e.message || e) + '</div>';
    }
  } else {
    root.innerHTML = '<div style="padding:24px;color:#e15a5a">' +
      'synth_groundhyp.js not loaded</div>';
  }
}
```

And a matching `<div id="grounded-hyp-root">` mount point in `index.html`,
plus a tab entry pointing at `loadGroundedHypothesis()`.

If a top-level view is preferred instead of a sub-tab, mount the same
`<div>` in a new view container; the renderer is view-agnostic.

---

## 4. Schema extension (self-managed)

The module needs an issue-time pattern vector per `signal_forecasts` row
for cosine retrieval. Rather than editing `database.py` or
`research_tracker.py`, it self-initialises an extra column on first use:

```sql
-- Run automatically by synth_groundhyp._ensure_pattern_column()
ALTER TABLE signal_forecasts ADD COLUMN pattern_vector_json TEXT;
```

The call is wrapped in try/except so a missing parent table (tracker
hasn't been imported yet) won't crash; the next call retries. Older rows
with `pattern_vector_json IS NULL` fall back to features parsed out of
their existing `metadata` blob via `_vec_from_metadata`.

If you ever decide to fold this into `database.py` for cleanliness, just
add the column there and remove `_ensure_pattern_column`; the parsing
fallback still works for legacy rows.

---

## 5. Packaging (`setup_app.py`)

Add `"synth_groundhyp"` to `LOCAL_MODULES`:

```python
LOCAL_MODULES = [
    ...,
    "synth_groundhyp",
    ...,
]
```

No new third-party packages are needed — `openai` is already listed.

---

## 6. Behaviour matrix

| Condition | Output |
|---|---|
| No symbol | `{"error": "missing symbol"}` |
| Empty `signal_forecasts` | Pattern vector + `anchor_cases=[]` + `base_rate_summary.n_analogs=0` + LLM-only hypothesis (if key configured) |
| `OPENAI_API_KEY` unset | Analogs + base rate, `hypothesis=null`, `llm_status="no_api_key"` |
| Daily AI cap reached | Analogs + base rate, `hypothesis=null`, `llm_status="daily_cap_exceeded"` |
| Valid pipeline | Full payload with hypothesis schema identical to `research_hypothesis.generate_hypothesis` (so it's interchangeable in the "save hypothesis" endpoint) |

Cached via `cache_store.coalesce(("groundhyp", symbol), 3 * 3600, …)`.

---

## 7. Test plan

1. **Empty ledger** — call `grounded_hypothesis("AAPL")` against a fresh
   DB. Confirm: `n_analogs == 0`, `current_pattern` populated for at
   least some features, no crash.
2. **Seeded ledger** — insert ~10 synthetic rows into `signal_forecasts`
   with varied `metadata` JSON (e.g. `{"prob_up_20d": 0.62}` for
   `signal_name="ml_forecast"`) and `scored_at` set. Verify that
   `anchor_cases` lists the 5 closest by `pattern_distance`.
3. **No OpenAI** — unset `OPENAI_API_KEY`. Confirm `hypothesis` is
   `null` and the rest of the payload still renders.
4. **Save round-trip** — click "SAVE HYPOTHESIS" in the UI. Confirm a
   row appears in `hypotheses` (the table owned by `research_hypothesis`)
   without schema mismatch errors.
5. **Cache** — back-to-back calls within 3h should return the same
   `as_of` timestamp (served from `cache_store`).

---

## 8. Known limitations / TODOs

- **Cold ledger** — until `signal_forecasts` has dozens of scored calls,
  the analog set is sparse. Consider seeding it via a one-shot script
  that replays historical signal_forecasts from `idea_pool_warmer`.
- **Cosine over partial vectors** — vectors with very few shared
  dimensions can produce misleadingly small distances. The current
  guard is "must share ≥ 1 dimension"; tightening to ≥ 3 might be
  worthwhile once the ledger is dense.
- **Feature drift** — the normalisation recipe is hard-coded to mirror
  `synth_consensus`. If that module's recipe changes, the two will
  silently diverge. A shared `_normalise` helper would be safer.
