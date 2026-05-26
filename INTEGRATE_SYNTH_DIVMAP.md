# Integrating the Divergence Map (synth_divmap)

Three new files ship with this enhancement:

- `synth_divmap.py` — divergence scan engine
- `static/js/synth_divmap.js` — UI renderer
- `INTEGRATE_SYNTH_DIVMAP.md` — this file

No new Python dependencies are required. The module re-uses existing
sources (`sec_edgar`, `congress`, `smart_money`, `ml_forecast`,
`research_factors`, `narrative_engine`, `fetcher`,
`reflexivity_detector`) and the shared `cache_store`.

## What it does

Scans a universe of symbols looking for cases where two normally-aligned
signals point in *opposite* directions on the same stock. A heavy bearish
insider Form-4 flow on a name with a 78/100 Smart Money composite, for
example, is the kind of mispricing the rest of the app currently
surfaces only in pieces. This view ranks all such contradictions in one
sortable list.

Ships with 5 signal pairs:

1. `insider_form4_vs_smart_money`
2. `ml_forecast_vs_factor_alpha`
3. `narrative_vs_options_pcr`
4. `congress_vs_smart_money`
5. `reflexivity_vs_narrative_velocity`

Magnitude threshold = 0.8 on a [-1, +1] axis (≥80% opposition before
counting as divergence). Sort by magnitude descending, return top_n.

## 1. `app.py`

Add the module guard and two routes alongside the existing
`/api/synth/...` and `/api/research/...` endpoints:

```python
# ── Divergence Map ───────────────────────────────────────────────
try:
    import synth_divmap
except Exception as _dm_err:
    synth_divmap = None
    log.warning("synth_divmap unavailable: %s", _dm_err)


@app.route("/api/synth/divergence-map", methods=["GET", "POST"])
def synth_divergence_map():
    if not synth_divmap:
        return jsonify({"error": "synth_divmap module not available"}), 500

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        universe = body.get("universe")
        top_n = _safe_int(body.get("top_n"), 20)
    else:
        universe = request.args.get("universe") or "sp500_top100"
        top_n = _safe_int(request.args.get("top_n"), 20)

    return jsonify(synth_divmap.divergence_map(universe=universe, top_n=top_n))
```

The endpoint accepts a custom universe via POST body as a JSON list of
symbols, or a named universe via the `universe` querystring (currently
just `sp500_top100`, the default).

## 2. `templates/index.html`

Add a new top-level view tab. Adjacent to the existing Signals / Research
nav entries:

```html
<button class="nav-tab" data-view="divergences">DIVERGENCES</button>
```

And a host panel — place after the Research panels are good:

```html
<section id="view-divergences" class="view hidden">
  <div id="divergence-map-host" class="card" style="padding:12px;"></div>
</section>
```

Load the script after `app.js`:

```html
<script src="{{ url_for('static', filename='js/synth_divmap.js') }}"></script>
```

## 3. `static/js/app.js`

Hook the new view into the existing nav switch. In `showView(name)`
(or wherever the view-tab handlers live), add:

```js
if (name === "divergences") {
  const el = document.getElementById("divergence-map-host");
  if (el && window.renderDivergenceMap) {
    // Re-render whenever the view becomes visible. Cached on the server
    // for 1h so this isn't expensive.
    window.renderDivergenceMap(el, { universe: "sp500_top100", top_n: 20 });
  }
}
```

If you prefer a sub-tab under Signals or Research rather than a top-level
view, the same `renderDivergenceMap` call works against any host element.

## 4. `setup_app.py`

Add `"synth_divmap"` to the `LOCAL_MODULES` list so py2app bundles it
into the Mac build:

```python
LOCAL_MODULES = [
    "app",
    "ai_summarizer",
    # ...
    "synthetic_insider",
    "synth_divmap",                  # ← new
]
```

## 5. `cache_warmer.py` (optional)

A 6h warmer keeps the default scan fresh so the first user click on
DIVERGENCES is instant. Tunables added at module scope:

```python
DIVMAP_INTERVAL = 6 * 3600   # 6h cadence for divergence map scan
```

Then in the warmer's main loop, alongside the other long-period tasks:

```python
try:
    import synth_divmap
except Exception:
    synth_divmap = None

# In the cycle dispatcher:
if synth_divmap is not None and _due("divmap", DIVMAP_INTERVAL):
    _safe("divmap", synth_divmap.warm_default_scan)
```

The scan walks ~100 symbols × 5 pairs with `ThreadPoolExecutor(max_workers=6)`.
On a healthy network this completes in roughly 60-90 seconds; on a heavily
rate-limited connection (cold cache, SEC bulk-lookup blocked) it can take
3-4 minutes, but the warmer thread is a daemon so this never blocks the UI.

## 6. `requirements.txt`

No changes. Everything the divmap touches is already on the path.

## 7. Verification

```python
import synth_divmap as d
out = d.divergence_map(
    universe=["AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOGL", "SPY"],
    top_n=20,
)
print(out["n_symbols_scanned"], "symbols,", out["total_found"], "divergences")
print(out["divergences"][:3])
```

When the upstream APIs are responsive, the small universe above will
typically produce 1-3 divergences (most commonly insider-vs-smart-money
or narrative-vs-PCR on the more news-driven mega-caps). If you get zero
on a first run, check the warning logs for SEC EDGAR 429s — the
`get_form4_transactions` calls are the heaviest source and rate-limit
first.

## 8. Notes / TODOs

- The narrative direction signal maps news *topic* buckets to a
  bull/bear axis (GROWTH/PROFITABILITY/TURNAROUND/M_AND_A → bullish,
  REGULATORY/SCANDAL → bearish). MACRO is treated as neutral so a
  bunch of "interest rate" articles doesn't accidentally flag every
  mega-cap as divergent. If a future enhancement adds an actual
  sentiment score per article, swap that in.
- The PCR signal uses only the *front-month* option chain (whatever
  `fetcher.get_option_chain` returns by default). For a more durable
  read, sum across the next 3 expirations — but that triples the
  yfinance call count, which trips Yahoo's rate limits on a full
  universe scan.
- Magnitude saturation: a few pairs (insider $, congress $) use
  hand-tuned saturation constants ($5M and $250k respectively). These
  are reasonable for mega-caps but might over-saturate on small-caps.
  If you ship a small-cap universe variant, parameterize the saturation
  per universe.
- The scan currently checks "opposite signs" before counting as a
  divergence — so a strong bull on signal A and a weak bull on signal B
  is skipped even if the *magnitude* exceeds the threshold. This is by
  design (you want actual opposition) but it means a couple of edge
  cases (e.g. signal_a ≈ +1.0, signal_b ≈ +0.1) won't surface; they're
  alignment differences in degree, not contradictions in direction.
- The same-sign-skip has a small-magnitude bypass (< 0.05) so a
  signal sitting essentially at zero on one side still surfaces if the
  other side is strongly directional.
- Per-symbol futures are capped at 45s; if the SEC's bulk CIK lookup
  is slow that limit may bite. Raise `_PER_SYMBOL_TIMEOUT_S` if you
  see "future failed" debug lines for the form4 pair specifically.
