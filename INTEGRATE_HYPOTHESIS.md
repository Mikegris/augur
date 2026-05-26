# Integrating the Research Lab (AI Hypothesis Generator)

This module adds a synthesis layer that sits on top of every other research
panel (smart_money, ml_forecast, narrative_engine, gex_engine, sec_edgar)
and produces testable, falsifiable hypotheses that get auto-scored at
their horizon. Files added by this PR:

- `research_hypothesis.py`
- `static/js/research_hypothesis.js`

The module **does not** touch `database.py`. It self-creates its
`hypotheses` table in the same SQLite file (`AUGUR_DB_PATH` or
`wealth.db`) via `init_hypothesis_db()`, which is invoked on import.

## 1. `app.py` — Flask routes

Add this block alongside the other `/api/research/*` routes:

```python
import research_hypothesis

@app.route("/api/research/hypothesis/generate", methods=["POST"])
def api_hypothesis_generate():
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    return jsonify(research_hypothesis.generate_hypothesis(symbol))

@app.route("/api/research/hypothesis/save", methods=["POST"])
def api_hypothesis_save():
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    hyp = data.get("hypothesis") or {}
    if not symbol or not isinstance(hyp, dict):
        return jsonify({"error": "symbol and hypothesis required"}), 400
    try:
        hid = research_hypothesis.save_hypothesis(hyp, symbol,
                                                  source=data.get("source", "ai"))
        return jsonify({"id": hid})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/research/hypothesis", methods=["GET"])
def api_hypothesis_list():
    status = request.args.get("status")
    symbol = request.args.get("symbol")
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return jsonify(research_hypothesis.list_hypotheses(
        status=status, symbol=symbol, limit=limit))

@app.route("/api/research/hypothesis/<int:hid>/status", methods=["POST"])
def api_hypothesis_set_status(hid):
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().upper()
    try:
        ok = research_hypothesis.update_hypothesis_status(hid, status)
        return jsonify({"ok": ok})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/research/hypothesis/<int:hid>/score", methods=["POST"])
def api_hypothesis_score(hid):
    return jsonify(research_hypothesis.score_hypothesis(hid))

@app.route("/api/research/hypothesis/stats", methods=["GET"])
def api_hypothesis_stats():
    return jsonify(research_hypothesis.stats())
```

## 2. `templates/index.html`

### 2a. Add the script tag (before the closing `</body>`)

```html
<script src="{{ url_for('static', filename='js/research_hypothesis.js') }}"></script>
```

### 2b. Add a top-level nav item

Find the nav bar and add:

```html
<a href="#" class="nav-item" data-view="research-lab">RESEARCH LAB</a>
```

### 2c. Add the view container

```html
<section id="view-research-lab" class="view" style="display:none">
  <div id="research-lab-root"></div>
</section>
```

## 3. `static/js/app.js`

Add a loader function and register it in the view-switcher:

```js
function loadHypothesisLab() {
  const root = document.getElementById('research-lab-root');
  if (root && typeof renderHypothesisLab === 'function') {
    renderHypothesisLab(root);
  }
}

// In the existing showView() / view-router, add:
//   case 'research-lab': loadHypothesisLab(); break;
```

## 4. `cache_warmer.py` — daily scoring

Append a once-per-day scoring task to the warmer loop:

```python
HYPOTHESIS_SCORE_INTERVAL = 24 * 3600   # once per day

def _warm_hypotheses():
    try:
        import research_hypothesis
        research_hypothesis.score_due_hypotheses()
    except Exception as e:
        log.debug("warmer hypothesis-score failed: %s", e)

# In the cadence loop, alongside the other interval checks:
if now - _last_cycle.get("hypothesis_score", 0) >= HYPOTHESIS_SCORE_INTERVAL:
    _safe("hypothesis_score", _warm_hypotheses)
```

## 5. `setup_app.py`

Add `"research_hypothesis"` to `LOCAL_MODULES`:

```python
LOCAL_MODULES = [
    ...
    "research_hypothesis",
    ...
]
```

The JS file is auto-picked up via the `static/js/` glob.

## 6. Verification (no API key required)

```python
import research_hypothesis as rh
rh.init_hypothesis_db()
print(rh.generate_hypothesis("AAPL"))   # {"error": "OpenAI key not configured"}
h = {"title":"test","conditions":{},
     "prediction":{"direction":"UP","magnitude_pct":3.0,
                   "horizon_days":20,"confidence":0.6},
     "rationale":"x","tags":[]}
hid = rh.save_hypothesis(h, "AAPL", source="user")
print(rh.list_hypotheses(symbol="AAPL"))
```

With an OpenAI key configured (env `OPENAI_API_KEY` or DB setting
`openai_api_key`), `generate_hypothesis("AAPL")` will return a full
schema-conformant dict with `title`, `conditions`, `prediction`,
`rationale`, `tags`, plus a `_context_snapshot` that records the
research signals used at issue time.

## API surface

| Method | Path | Body / Query | Returns |
|--------|------|--------------|---------|
| POST | `/api/research/hypothesis/generate` | `{symbol}` | hypothesis dict or `{error}` |
| POST | `/api/research/hypothesis/save`     | `{symbol, hypothesis, source?}` | `{id}` |
| GET  | `/api/research/hypothesis`          | `?status&symbol&limit`         | list of rows |
| POST | `/api/research/hypothesis/<id>/status` | `{status}` | `{ok: bool}` |
| POST | `/api/research/hypothesis/<id>/score`  | (none)     | scoring result |
| GET  | `/api/research/hypothesis/stats`       | (none)     | aggregate stats |

`status` must be one of: `OPEN`, `CONFIRMED`, `REJECTED`, `INVALIDATED`.
