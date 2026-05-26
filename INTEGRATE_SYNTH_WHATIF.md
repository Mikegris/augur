# INTEGRATE_SYNTH_WHATIF.md — Portfolio What-If synthesis engine

A "what happens if I add NVDA at $20K to my portfolio?" engine. Runs the
candidate trade through five lenses (factor exposure, stress tests, Monte
Carlo, optimizer, ML forecast) and returns a single synthesised diff dict.

## Files shipped

- `synth_whatif.py` — engine (`whatif(current_holdings, candidate) -> dict`)
- `static/js/synth_whatif.js` — `renderWhatIf(containerEl, opts?)` panel
- `INTEGRATE_SYNTH_WHATIF.md` — this file

## 1. Wire up the Flask route

Add this near the other `/api/research/*` and `/api/synth/*` routes in
`app.py`. Place it after the `research_optimizer` route registration.

```python
# ─── Synth: What-If engine ──────────────────────────────────────
try:
    import synth_whatif as _whatif_mod
except Exception as _wif_err:
    _whatif_mod = None
    log.warning("synth_whatif unavailable: %s", _wif_err)


@app.route("/api/synth/whatif", methods=["POST"])
def synth_whatif_route():
    if not _whatif_mod:
        return jsonify({"error": "synth_whatif module not available"}), 500
    body = request.get_json(silent=True) or {}
    current = body.get("current_holdings") or []
    candidate = body.get("candidate") or {}
    if not isinstance(current, list) or not isinstance(candidate, dict):
        return jsonify({"error": "expected JSON {current_holdings:[...], candidate:{...}}"}), 400
    try:
        return jsonify(_whatif_mod.whatif(current, candidate))
    except Exception as e:
        log.exception("synth_whatif failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/synth/whatif", methods=["GET"])
def synth_whatif_get():
    """Convenience GET that pulls current_holdings from the DB and accepts
    candidate parameters via query string (?symbol=...&market_value=...&action=...).
    Useful from a curl/browser-bar smoke test or for the warmer."""
    if not _whatif_mod:
        return jsonify({"error": "synth_whatif module not available"}), 500
    sym = (request.args.get("symbol") or "").strip()
    try:
        mv = float(request.args.get("market_value") or 0.0)
    except (TypeError, ValueError):
        mv = 0.0
    action = (request.args.get("action") or "add").lower()
    if not sym or mv < 0:
        return jsonify({"error": "need ?symbol=...&market_value=...&action=add|remove|resize_to"}), 400

    # Pull current holdings from the existing /api/portfolio code path so we
    # inherit live pricing and account filtering.
    acct = request.args.get("account_id")
    holdings_raw = db.get_portfolio(account_id=int(acct) if acct and acct.isdigit() else None)
    enriched = []
    for h in holdings_raw or []:
        # Use shares × last close if available; otherwise fall back to cost basis.
        mv_h = (h.get("market_value") or
                (h.get("shares", 0) or 0) * (h.get("avg_cost", 0) or 0))
        if mv_h <= 0:
            continue
        enriched.append({
            "symbol": h["symbol"],
            "market_value": float(mv_h),
            "asset_type": h.get("asset_type", "stock"),
            "shares": h.get("shares"),
            "avg_cost": h.get("avg_cost"),
        })
    return jsonify(_whatif_mod.whatif(enriched, {
        "symbol": sym, "market_value": mv, "action": action,
    }))
```

The POST route is the canonical entry point used by the JS panel. The GET
variant is a developer/warmer convenience that auto-pulls holdings from
SQLite — handy for `curl 'localhost:5050/api/synth/whatif?symbol=NVDA&market_value=20000'`.

## 2. Bundle the module for desktop builds

Add `"synth_whatif"` to `LOCAL_MODULES` in `setup_app.py`, in alphabetical
order near `synthetic_insider`:

```python
LOCAL_MODULES = [
    # … existing entries …
    "synthetic_insider",
    "synth_whatif",
]
```

## 3. Add a "WHAT-IF" sub-tab to the Portfolio or Analytics view

### 3a. Template (`templates/index.html`)

Add a `<div>` container under the Portfolio (or Analytics) tab panel and
load the script. Recommended placement: as a sub-tab within the existing
Portfolio view so the user can pivot between Holdings → Risk → What-If
without leaving the page.

```html
<!-- inside the Portfolio panel's tab nav -->
<button class="tab-btn" data-tab="portfolio-whatif">WHAT-IF</button>

<!-- inside the Portfolio panel body -->
<div id="portfolio-whatif" class="tab-content" style="display:none;">
  <div id="whatif-panel"></div>
</div>

<!-- near the bottom of <body>, alongside the other research_*.js tags -->
<script src="{{ url_for('static', filename='js/synth_whatif.js') }}"></script>
```

### 3b. JS wiring (`static/js/app.js`)

When the WHAT-IF tab becomes active, call `renderWhatIf` on the container:

```javascript
function showWhatIfTab() {
  const el = document.getElementById("whatif-panel");
  if (!el) return;
  // Pass current holdings from the cached portfolio data if you have it;
  // otherwise renderWhatIf will auto-fetch /api/portfolio.
  window.renderWhatIf(el, {
    holdings: window._lastPortfolioHoldings || null,
  });
}
```

Hook into the existing tab-switching dispatcher however other tabs do it
(e.g. an entry in the `tabHandlers` object or a `data-tab` click listener).

## 4. Warm the cache (optional)

If you have a `cache_warmer.py` background task, add a periodic call that
seeds a few canonical what-ifs so the first user click hits cache. The
engine caches each `(holdings_hash, candidate_hash)` pair for 15 minutes.

```python
# in cache_warmer's periodic loop, every ~10 minutes:
try:
    import synth_whatif, database as db
    portfolio = db.get_portfolio() or []
    if portfolio:
        h = [{"symbol": p["symbol"],
              "market_value": (p.get("shares", 0) or 0) * (p.get("avg_cost", 0) or 0)}
             for p in portfolio if (p.get("shares") or 0) > 0]
        for sym in ("NVDA", "TLT", "GLD"):  # common what-if candidates
            synth_whatif.whatif(h, {"symbol": sym, "market_value": 20000, "action": "add"})
except Exception:
    log.debug("whatif warmer skipped")
```

## API contract (response shape)

```json
{
  "candidate": {"symbol": "TLT", "market_value": 20000.0, "action": "add"},
  "current":   { "nav": 100000.0, "factor_exposure": {...}, "expected_return_pct": 11.4,
                 "expected_vol_pct": 16.2, "scenario_losses": {"2008": -28.4, ...},
                 "concentration_hhi": 0.5, "top_5_weights": [...] },
  "proposed":  { ...same shape, with the trade applied... },
  "delta": {
    "expected_return_pct": 0.7,
    "expected_vol_pct": 0.8,
    "sharpe_ratio_delta": -0.02,
    "concentration_hhi": -0.125,
    "factor_exposure_shifts": [{"factor": "Mkt-RF", "current": 1.05, "proposed": 1.02, "shift": -0.03}, ...],
    "scenario_loss_shifts":   {"2008": 1.2, "covid": 0.4, "dot_com": 0.0, "rate_hike": -0.5},
    "ml_forecast_weighted_return_delta_pct": 0.45,
    "nav_delta": 20000.0
  },
  "optimizer_says": {
    "candidate_symbol": "TLT",
    "current_optimal_size_pct": 0.04,
    "your_proposed_size_pct":   0.17,
    "verdict": "OVERSIZED",
    "optimizer_sharpe": 0.62, ...
  },
  "monte_carlo_delta": {
    "p05_shift": -2400.0,
    "median_shift": 1200.0,
    "p95_shift": 5800.0,
    "prob_loss_delta_pct": 1.2
  },
  "errors": null,
  "elapsed_ms": 4811,
  "as_of": "2026-05-26T12:34:56Z"
}
```

### Sub-section failure modes (graceful degradation)

Each lens has its own failure modes — Monte Carlo can return an all-zero
terminal distribution when no holding has price history; the factor
regression needs ≥60 overlapping days of FF data; the optimizer can fail
to converge. The engine wraps each in try/except and on failure sets the
corresponding field to `null` with an explanation under `errors.<section>`.
The rest of the bundle still computes — the user always gets *something*.

## Testing

Smoke test from the CLI:

```bash
python -c "
import synth_whatif, json
out = synth_whatif.whatif(
    [{'symbol': 'AAPL', 'market_value': 50000},
     {'symbol': 'SPY',  'market_value': 50000}],
    {'symbol': 'TLT', 'market_value': 20000, 'action': 'add'}
)
print(json.dumps(out, indent=2, default=str)[:2000])
"
```

Or via HTTP once the route is wired:

```bash
curl -sX POST localhost:5050/api/synth/whatif -H 'Content-Type: application/json' \
  -d '{"current_holdings":[{"symbol":"AAPL","market_value":50000},{"symbol":"SPY","market_value":50000}],
       "candidate":{"symbol":"TLT","market_value":20000,"action":"add"}}' | jq .
```

You should see all top-level keys present (`current`, `proposed`, `delta`,
`optimizer_says`, `monte_carlo_delta`). Any individual `*_pct` or sub-dict
may be `null` if its underlying data source was unreachable — that's by
design, and the corresponding diagnostic shows up in the `errors` block.

## Version bump

This is the 11th research/synth module. After integration, bump the version
in `app.py` (`__version__` near the top) and update the changelog with a
single line:

> Add Portfolio What-If synthesis engine — multi-lens trade impact across
> factor exposure, stress tests, Monte Carlo, optimizer, and ML forecast.
