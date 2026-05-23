# Portfolio Optimizer — Wiring Guide

Drop-in instructions to surface `research_optimizer.py` + `static/js/research_optimizer.js`
in AUGUR (`app.py` v0.1.14+).

The optimizer module is self-contained — it pulls daily bars via
`fetcher.get_chart_data(symbol, period="2y", interval="1d")` and solves with
`scipy.optimize.minimize`. No new requirements.

---

## 1. `app.py` — register the route

Near the other `/api/research/*` handlers (e.g. just below the `research_wikidata`
block around line 1960), add:

```python
# ── Portfolio optimizer (Markowitz, Black-Litterman, Risk Parity) ─────
try:
    import research_optimizer
except Exception as _opt_err:
    research_optimizer = None
    log.warning("research_optimizer unavailable: %s", _opt_err)


@app.route("/api/research/optimize", methods=["POST"])
def research_optimize():
    if not research_optimizer:
        return jsonify({"error": "research_optimizer module not available"}), 500

    body = request.get_json(silent=True) or {}
    symbols = [s.strip().upper() for s in (body.get("symbols") or []) if isinstance(s, str) and s.strip()]
    if not symbols:
        return jsonify({"error": "symbols list is required"}), 400

    objective = (body.get("objective") or "max_sharpe").strip().lower()
    constraints = body.get("constraints") or {}
    current_weights = body.get("current_weights") or {}

    try:
        if objective == "risk_parity":
            result = research_optimizer.risk_parity(
                symbols, period=constraints.get("period", "2y"),
            )
        elif objective == "black_litterman":
            views = body.get("views") or []
            view_conf = body.get("view_confidence") or []
            mkt_caps = body.get("mkt_caps") or {}
            result = research_optimizer.black_litterman(
                symbols=symbols,
                mkt_caps=mkt_caps,
                views=views,
                view_confidence=view_conf,
                period=constraints.get("period", "2y"),
                risk_aversion=float(constraints.get("risk_aversion", 2.5)),
                tau=float(constraints.get("tau", 0.05)),
            )
        else:
            result = research_optimizer.markowitz_optimize(
                symbols, objective=objective, constraints=constraints,
            )
    except Exception as e:
        log.exception("optimize failed")
        return jsonify({"error": str(e)}), 500

    if "error" in result:
        return jsonify({"error": result["error"]}), 422

    # Always compute the current-vs-optimal comparison if a current allocation
    # is supplied; otherwise return a stub so the UI logic stays uniform.
    if isinstance(current_weights, dict) and current_weights:
        try:
            cmp_out = research_optimizer.compare_to_current(
                current_weights={k.upper(): float(v) for k, v in current_weights.items()},
                optimal_weights=result["weights"],
                period=constraints.get("period", "2y"),
            )
        except Exception as e:
            log.warning("compare_to_current failed: %s", e)
            cmp_out = {"delta": {}, "tracking_error_pct": None, "symbols": []}
    else:
        cmp_out = {"delta": {}, "tracking_error_pct": None, "symbols": []}

    return jsonify({"optimal": result, "compare": cmp_out})
```

That's the only `app.py` edit needed.

---

## 2. `templates/index.html` — add the view + script tag

### 2a. Add the view div

Alongside the other `<div id="view-*">` blocks (around line 136 where
`view-analytics` lives), insert:

```html
<!-- PORTFOLIO OPTIMIZER -->
<div id="view-optimizer" class="view">
  <div class="loading"><div class="spinner"></div></div>
</div>
```

### 2b. Load the JS module

Near the existing `<script src="{{ url_for('static', filename='js/app.js') }}">`
tag at the bottom of the body, add:

```html
<script src="{{ url_for('static', filename='js/research_optimizer.js') }}"></script>
```

The script must load *before* `app.js` doesn't call `renderOptimizer`, or just
after — order doesn't actually matter because the global is read on click. Just
make sure it loads before the user clicks the nav item.

---

## 3. `static/js/app.js` — register the view

### 3a. Add to the `research` group in `NAV_GROUPS`

Around line 348 replace:

```js
research: {
  label: 'RESEARCH',
  items: [
    { view: 'research',  label: 'Research' },
    { view: 'analytics', label: 'Analytics' },
    ...
  ],
},
```

with (adds the new `optimizer` item right after `analytics`):

```js
research: {
  label: 'RESEARCH',
  items: [
    { view: 'research',  label: 'Research' },
    { view: 'analytics', label: 'Analytics' },
    { view: 'optimizer', label: 'Optimizer' },
    { view: 'intel',     label: 'Intel' },
    { view: 'earnings',  label: 'Earnings' },
    { view: 'screener',  label: 'Screener' },
    { view: 'narrative', label: 'Narrative' },
    { view: 'alt-data',  label: 'Alt Data' },
  ],
},
```

### 3b. Add the lazy-load case

In the `switch (view)` block inside `navigate()` (around line 428), add:

```js
case 'optimizer': loadOptimizer(); break;
```

### 3c. Add the `loadOptimizer` function

Add anywhere alongside the other `load*View` functions:

```js
async function loadOptimizer() {
  const view = document.getElementById('view-optimizer');
  if (!view) return;
  if (typeof window.renderOptimizer !== 'function') {
    view.innerHTML = '<div style="padding:24px;color:#e15a5a">research_optimizer.js not loaded</div>';
    return;
  }

  // Seed the universe + current weights from the live portfolio if available.
  let seedSymbols = [];
  let currentWeights = {};
  try {
    const port = State.portfolio || await API.get('/api/portfolio');
    if (port && Array.isArray(port.holdings)) {
      const totalVal = port.holdings.reduce((acc, h) => acc + (Number(h.market_value) || 0), 0);
      port.holdings.forEach((h) => {
        if (!h || !h.symbol) return;
        seedSymbols.push(String(h.symbol).toUpperCase());
        if (totalVal > 0) {
          currentWeights[String(h.symbol).toUpperCase()] = (Number(h.market_value) || 0) / totalVal;
        }
      });
    }
  } catch (e) {
    console.warn('loadOptimizer: portfolio seed failed', e);
  }
  if (!seedSymbols.length) seedSymbols = ['AAPL', 'MSFT', 'GLD', 'SPY', 'TLT'];

  window.renderOptimizer(view, { symbols: seedSymbols, currentWeights });
}
```

---

## 4. `setup_app.py` — bundle the module

Add `"research_optimizer"` to the `LOCAL_MODULES` list (around line 21), e.g.
between `reflexivity_detector` and `sec_edgar`:

```python
LOCAL_MODULES = [
    ...
    "reflexivity_detector",
    "research_optimizer",
    "sec_edgar",
    ...
]
```

scipy is already in `EXTRA_PACKAGES` (line ~71), so py2app will pull it in.

---

## 5. Smoke test

```bash
# Backend
python -c "
import research_optimizer as ro
r = ro.markowitz_optimize(['AAPL','MSFT','GLD','SPY','TLT'], objective='max_sharpe')
print({s: round(w,3) for s,w in r['weights'].items()}, 'sharpe=', r['sharpe'])
"

# Endpoint (after restart)
curl -s -X POST http://localhost:5000/api/research/optimize \
  -H 'Content-Type: application/json' \
  -d '{"symbols":["AAPL","MSFT","GLD","SPY","TLT"],"objective":"max_sharpe"}' | python -m json.tool
```

Click into RESEARCH → Optimizer in the UI, pick "Black-Litterman", click
"+ ADD VIEW", enter `NVDA outperforms SPY by 8% (70% confident)`, then
OPTIMIZE — the blended-returns table will show NVDA pulled up relative to SPY,
and the weights table will reweight accordingly.

## 6. Requirements

No new dependencies. `scipy` and `numpy` are already in `requirements.txt`.
The whole module is ~1.7k SLOC of Python + JS, no native binaries beyond what
scipy already ships.
