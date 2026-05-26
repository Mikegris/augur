# INTEGRATE_BACKTEST

How to wire `research_backtest.py` + `research_backtest.js` into AUGUR. The
worktree branch deliberately did NOT touch `app.py`, `static/js/app.js`,
`templates/index.html`, or `setup_app.py`; the integrator applies the diffs
below.

## 1. `app.py` — new Flask route

Add the route alongside the other `/api/research/...` endpoints (near the
existing `research_xbrl` / `research_wikidata` blocks):

```python
# ── Backtest harness ──────────────────────────────────────────────
try:
    import research_backtest
except Exception as _bt_err:
    research_backtest = None
    log.warning("research_backtest unavailable: %s", _bt_err)


@app.route("/api/research/backtest")
def research_backtest_run():
    """Walk-forward backtest of a named signal over [start, end].

    Query params:
      symbol            (required) ticker
      signal            (required) one of momentum | mean_reversion | ml_forecast
      start             (optional) YYYY-MM-DD inclusive lower bound
      end               (optional) YYYY-MM-DD inclusive upper bound
      horizon_override  (optional) int — force this hold length on every signal
    """
    if research_backtest is None:
        return jsonify({"error": "research_backtest module not available"}), 500
    symbol = (request.args.get("symbol") or "").upper()
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400

    signal_name = (request.args.get("signal") or "momentum").strip()
    signal_fn = research_backtest.get_adapter(signal_name)
    if signal_fn is None:
        return jsonify({
            "error": f"Unknown signal '{signal_name}'. Available: "
                     + ", ".join(research_backtest.ADAPTERS.keys()),
        }), 400

    start = request.args.get("start") or None
    end   = request.args.get("end")   or None
    horizon_override = _safe_int(request.args.get("horizon_override"), 0) or None

    try:
        result = research_backtest.run_backtest(
            signal_fn,
            symbol,
            start=start,
            end=end,
            horizon_override=horizon_override,
        )
        return jsonify(result)
    except Exception as e:
        log.exception("backtest failure")
        return jsonify({"error": str(e)}), 500
```

Both `_valid_ticker` and `_safe_int` already exist in `app.py` and are used by
neighboring routes — no new top-of-file imports needed.

## 2. `templates/index.html` — script include + view container

### 2a. Script tag

In `<body>`, immediately after the existing `<script src="/static/js/app.js?v=9">`
line, add:

```html
<script src="/static/js/research_backtest.js?v=1"></script>
```

This registers `window.renderBacktest` before any view loader fires.

### 2b. New view container

Inside `<main id="main">`, alongside the other `<div id="view-*"` blocks
(e.g. next to `view-analytics`), add:

```html
<!-- BACKTEST -->
<div id="view-backtest" class="view">
  <div class="loading"><div class="spinner"></div></div>
</div>
```

The JS module owns the panel HTML — no further markup is required.

## 3. `static/js/app.js` — sub-nav item + view loader

### 3a. Register the sub-nav entry under RESEARCH

Inside the `NAV_GROUPS.research.items` array (around line 350, next to the
existing `analytics` entry), add a `backtest` item:

```javascript
research: {
  label: 'RESEARCH',
  items: [
    { view: 'research',  label: 'Research' },
    { view: 'analytics', label: 'Analytics' },
    { view: 'backtest',  label: 'Backtest'  },   // ← new
    { view: 'intel',     label: 'Intel' },
    { view: 'earnings',  label: 'Earnings' },
    { view: 'screener',  label: 'Screener' },
    { view: 'narrative', label: 'Narrative' },
    { view: 'alt-data',  label: 'Alt Data' },
  ],
},
```

### 3b. Add `loadBacktest()` function

Drop this in next to `loadAnalyticsView()` (around line 3927):

```javascript
async function loadBacktest() {
  const view = document.getElementById('view-backtest');
  if (!view) return;

  // Default to the active research symbol if there is one, else AAPL so the
  // panel always has something to render the first time the user clicks the
  // tab. Users can change the ticker via the input below.
  const defaultSym = (State && State.researchSymbol) ? State.researchSymbol : 'AAPL';

  view.innerHTML = `
    <div class="panel mb-8">
      <div class="panel-header">
        <span class="panel-title">WALK-FORWARD BACKTEST</span>
        <div class="flex gap-8 align-center" style="font-size:11px">
          <label>Ticker:
            <input type="text" id="bt-view-symbol" value="${defaultSym}"
              style="background:transparent;color:var(--green);border:1px solid var(--border);padding:2px 6px;font-family:monospace;width:80px;text-transform:uppercase">
          </label>
          <button class="btn btn-ghost btn-sm" onclick="reloadBacktestPanel()">↻ LOAD</button>
        </div>
      </div>
      <div class="panel-body" style="font-size:11px;color:var(--text-dim);padding:6px 12px;border-bottom:1px solid var(--border)">
        Replays a signal-producing function over historical bars and reports hit-rate, Sharpe, drawdown, and the equity curve.
        Momentum and mean-reversion adapters are leak-free; ml_forecast is best-effort (see module docstring).
      </div>
    </div>
    <div id="backtest-host"></div>
  `;

  // Initial render with the default ticker + momentum signal.
  reloadBacktestPanel();
}

function reloadBacktestPanel() {
  const symInput = document.getElementById('bt-view-symbol');
  const symbol = (symInput && symInput.value ? symInput.value : 'AAPL').toUpperCase().trim();
  const host = document.getElementById('backtest-host');
  if (!host) return;
  if (window.renderBacktest) {
    // Default to a 2-year window ending today — easy on the cache warmer and
    // still long enough for stable hit-rate / Sharpe numbers.
    const today = new Date();
    const start = new Date(today.getFullYear() - 2, today.getMonth(), today.getDate())
                    .toISOString().slice(0, 10);
    const end   = today.toISOString().slice(0, 10);
    window.renderBacktest(host, symbol, 'momentum', { start: start, end: end });
  } else {
    host.innerHTML = '<div style="color:var(--red);padding:12px">research_backtest.js not loaded</div>';
  }
}
```

### 3c. Wire the loader

Two small additions:

1. In the `navigate()` switch (around line 439, alongside
   `case 'analytics':`), add:

   ```javascript
   case 'backtest':     loadBacktest(); break;
   ```

2. In the `VIEW_LOADERS` map (around line 3522), add:

   ```javascript
   backtest:     () => loadBacktest(),
   ```

   This opts the view into the periodic auto-refresh — feel free to omit if
   you'd rather the backtest only run on user action (results are cached for
   6h server-side anyway, so the auto-refresh is cheap).

## 4. `setup_app.py` — bundle the module

Add `"research_backtest"` to the `LOCAL_MODULES` list (around line 21,
alphabetical by convention — between `reflexivity_detector` and `sec_edgar`):

```python
LOCAL_MODULES = [
    ...
    "reflexivity_detector",
    "research_backtest",        # ← add this
    "sec_edgar",
    ...
]
```

`EXTRA_PACKAGES` needs no changes — the module only depends on `numpy`,
`pandas`, the existing `fetcher` / `cache_store` / `ml_forecast` modules, and
the standard library.

## 5. Smoke test (without the UI)

```bash
python3 -c "
import research_backtest as bt
r = bt.run_backtest(bt.adapter_momentum, 'AAPL', '2023-01-01', '2025-01-01')
print('n_signals:', r['n_signals'],
      'hit_rate:', r['hit_rate'],
      'total_return_pct:', r['total_return_pct'],
      'sharpe:', r['sharpe'])
"
```

Expect ~100+ signals, a hit-rate around 0.5–0.7, a finite Sharpe and a
non-trivial equity curve. First call takes a few seconds (the fetcher pulls
5y of bars and the harness scans every day); subsequent calls within 6h
return from `cache_store` instantly.

## 6. Endpoint smoke test (once route is wired)

```bash
curl -s 'http://127.0.0.1:5000/api/research/backtest?symbol=AAPL&signal=momentum&start=2023-01-01&end=2025-01-01' \
  | jq '{n_signals,hit_rate,total_return_pct,sharpe,max_drawdown_pct}'
```

```bash
curl -s 'http://127.0.0.1:5000/api/research/backtest?symbol=AAPL&signal=mean_reversion' \
  | jq '{n_signals,hit_rate,total_return_pct,sharpe}'
```

## Notes & caveats

- The momentum and mean-reversion adapters operate solely on the windowed
  slice the harness hands them, so they are leak-free walk-forward tests.
- The `ml_forecast` adapter is best-effort: `ml_forecast.ml_forecast(symbol)`
  pulls its own 2y of history via the fetcher, so the harness calls it once
  per backtest and re-uses the result across every bar. The resulting "hit
  rate" is informative but is NOT a leak-free out-of-sample evaluation. A
  future version of `ml_forecast` would need an `as_of=` argument so the
  adapter could replay the model at each bar with point-in-time inputs.
- Results are cached for 6h via `cache_store.coalesce` keyed by
  `(symbol, signal_name, start, end, horizon_override, params_hash)`.
- Returned `equity_curve` is one nav value per bar in the *windowed* slice;
  `signals` is per-firing log capped at 200 entries (the 100 most recent +
  the 100 most extreme by realised return — the UI's best/worst panels
  always have enough material).
- The harness assumes "decision at close, fill at close" — i.e. when a
  signal fires using bars `[0, t)` the entry price is `close[t-1]`. This
  matches the most common textbook convention; intraday execution slippage
  is out of scope for the MVP.
