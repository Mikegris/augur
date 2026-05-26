# Integrating the Cross-Asset Macro Translator (`synth_macrotranslate`)

Three new files ship with this enhancement:

- `synth_macrotranslate.py` — the translator engine. Reads from
  `fred_data.SERIES_CATALOG` for release calendars,
  `research_eventstudy.CALENDAR["fed_fomc"]` for FOMC dates,
  `fetcher.get_chart_data` for SPY + sector ETF prices, and
  `research_factors._load_factor_data` / `portfolio_factor_exposure` for
  factor exposures. No new pip dependencies, no new endpoints.
- `static/js/synth_macrotranslate.js` — the **TRANSLATE TO PORTFOLIO**
  panel. Plain divs (no charting libs required); slots into AUGUR's
  existing macro view.
- `INTEGRATE_SYNTH_MACROTRANSLATE.md` (this file).

## 1. `app.py`

Add the module guard and two routes alongside the other research endpoints
(near the `/api/research/factors/*` cluster around line ~2450):

```python
# ── Cross-Asset Macro Translator ──────────────────────────────────
try:
    import synth_macrotranslate
except Exception as _mt_err:
    synth_macrotranslate = None
    log.warning("synth_macrotranslate unavailable: %s", _mt_err)


@app.route("/api/synth/macrotranslate/<release_id>")
def synth_macrotranslate_get(release_id):
    if not synth_macrotranslate:
        return jsonify({"error": "synth_macrotranslate module not available"}), 500
    surprise = request.args.get("surprise")
    try:
        surprise_pct = float(surprise) if surprise not in (None, "") else None
    except (TypeError, ValueError):
        surprise_pct = None
    return jsonify(synth_macrotranslate.macro_translate(release_id, surprise_pct=surprise_pct))


@app.route("/api/synth/macrotranslate/<release_id>/portfolio", methods=["POST"])
def synth_macrotranslate_portfolio(release_id):
    if not synth_macrotranslate:
        return jsonify({"error": "synth_macrotranslate module not available"}), 500
    body = request.get_json(silent=True) or {}
    holdings = body.get("holdings") or []
    surprise_pct = body.get("surprise_pct")
    if not holdings:
        # Fall back to the current portfolio so a no-body POST works.
        try:
            holdings = [
                {"symbol": h["symbol"], "market_value": h.get("market_value", 0)}
                for h in database.get_holdings_with_values()
            ]
        except Exception:
            holdings = []
    try:
        surprise_pct = float(surprise_pct) if surprise_pct not in (None, "") else None
    except (TypeError, ValueError):
        surprise_pct = None
    return jsonify(synth_macrotranslate.macro_translate(
        release_id, surprise_pct=surprise_pct, portfolio_holdings=holdings))


@app.route("/api/synth/macrotranslate/releases")
def synth_macrotranslate_catalog():
    if not synth_macrotranslate:
        return jsonify({"error": "synth_macrotranslate module not available"}), 500
    return jsonify({"releases": synth_macrotranslate.supported_releases()})
```

The `/portfolio` route accepts `{"holdings": [...], "surprise_pct": x}`
and a missing body falls back to the user's current book (same convention
as `/api/research/factors/portfolio`).

## 2. `templates/index.html`

Inside the **Macro** view, add a panel anchor. Anywhere alongside the
existing FRED / Treasury / VIX cards:

```html
<section id="macro-translate" class="card">
  <div id="macro-translate-host"></div>
</section>
```

Load the script after `app.js`:

```html
<script src="{{ url_for('static', filename='js/synth_macrotranslate.js') }}"></script>
```

## 3. `static/js/app.js`

Hook into the existing macro-view entry point (the function that runs
when the Macro tab activates). At the bottom of that function:

```js
const el = document.getElementById("macro-translate-host");
if (el && window.renderMacroTranslate) {
  // Pass the current portfolio so the projection block fills in
  // immediately. Drop it if you want the GET-only view.
  window.renderMacroTranslate(el, "CPIAUCSL", State.holdings || []);
}
```

If you'd rather wire it without a portfolio (e.g. for visitors with no
positions yet) just call:

```js
window.renderMacroTranslate(el, "CPIAUCSL");
```

The UI's release dropdown lets the user switch between CPI / NFP / FOMC /
others on the fly — no second JS hook needed.

## 4. `setup_app.py`

Add `"synth_macrotranslate"` to the `LOCAL_MODULES` list so py2app bundles
it into the Mac build, next to the other `research_*` / `synthetic_*`
entries:

```python
LOCAL_MODULES = [
    "app",
    "ai_summarizer",
    # ...
    "research_tracker",
    "synth_macrotranslate",      # ← new
    "synthetic_insider",
    # ...
]
```

## 5. `requirements.txt`

No changes — `numpy` is already in the stack and everything else lives
behind modules that already shipped.

## 6. Verification

Quick smoke test:

```python
import synth_macrotranslate as m
r = m.macro_translate("CPIAUCSL")
print(r["release"]["name"], "->", len(r["historical_episodes"]), "episodes")
print("avg SPY 5d:", r["average_response"]["sp500_5d_pct"], "%")

# With a portfolio
r2 = m.macro_translate("CPIAUCSL",
                       portfolio_holdings=[{"symbol":"AAPL","market_value":10000}])
print("expected 5d:", r2["portfolio_projection"]["expected_5d_pct"], "%")
```

Tested 2026-05 — CPI returns 24 episodes, average response includes
all six FF5+Mom factors and 11 SPDR sector ETFs.

## 7. Notes / TODOs

- **Historical surprises are not modelled.** The spec asks for surprise
  bucketing; we cache by signed bucket (`neg2/neg1/pos1/pos2`) so two
  consecutive calls with similar surprise share a cache entry, but we
  don't filter historical episodes by surprise sign because we have no
  consensus-history feed. If you wire one (e.g. Briefing.com or BLS
  XML feeds) just drop a filter into `_historical_release_dates`
  before the trim.
- **FRED observation-date vs release-date.** FRED records observations
  on the start of the period being measured (CPI April → 2026-04-01)
  but the print itself hits markets ~14 days later. We add a fixed lag
  by frequency (`_RELEASE_LAG_DAYS`); if you need bps-accurate alignment,
  swap this for a real BLS schedule.
- **Rate-limit defensive.** SPY + 11 sector ETFs are pulled in sequence
  via the AUGUR fetcher's normal cache pipeline. On a cold cache the
  first call after `cache_store.clear()` may hit yahoo throttling — the
  module degrades gracefully (empty episodes list with an error
  envelope) rather than crashing.
- **Portfolio projection scale.** IQR / best / worst are SPY-distribution
  brackets scaled by the portfolio's market beta. It's a rough bracket,
  not a Monte Carlo. If you ever want a tighter band, feed
  `research_montecarlo` the average-response distribution as its
  per-step drift and you'll get a real confidence cone.
- The portfolio projection re-runs per-holding factor regressions on
  every call. Those are cached for 24h via `cache_store`, so the cost
  only hits the first call per day per ticker.
