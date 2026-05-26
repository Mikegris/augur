# Integrating the Cross-Source Consensus module

Three new files ship with this enhancement:

- `synth_consensus.py` — score engine that folds every per-symbol signal
  AUGUR collects into one 0..100 bullish/bearish number
- `static/js/synth_consensus.js` — `renderConsensus(el, symbol)` UI
- `INTEGRATE_SYNTH_CONSENSUS.md` — this file

No new Python dependencies.  All upstream modules are imported behind
`try/except`, so the module degrades cleanly if any of them are missing.

## 1. `app.py`

Add a module guard and one route alongside the existing
`/api/synth/...` (or research) endpoints:

```python
# ── Cross-source consensus score ─────────────────────────────────
try:
    import synth_consensus
except Exception as _sc_err:
    synth_consensus = None
    log.warning("synth_consensus unavailable: %s", _sc_err)


@app.route("/api/synth/consensus/<symbol>")
def synth_consensus_symbol(symbol):
    if not synth_consensus:
        return jsonify({"error": "synth_consensus module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    return jsonify(synth_consensus.consensus_score(symbol.upper()))
```

Caching is handled inside `consensus_score` via `cache_store.coalesce`
with a 10-minute TTL, so the route itself is a thin pass-through.

## 2. `templates/index.html`

In the per-symbol **Research** view, add a new sub-tab "CONSENSUS" and a
panel slot.  The new sub-tab sits alongside Backtest / Factors / Event
Study / etc.:

```html
<button class="research-tab" data-tab="consensus">CONSENSUS</button>
…
<section id="research-consensus" class="research-pane" style="display:none;">
  <div id="consensus-host" class="card"></div>
</section>
```

Load the script after `app.js`:

```html
<script src="{{ url_for('static', filename='js/synth_consensus.js') }}"></script>
```

## 3. `static/js/app.js`

Hook into the existing research view loader.  In `loadResearchFor(symbol)`,
add:

```js
const el = document.getElementById('consensus-host');
if (el && window.renderConsensus) {
  window.renderConsensus(el, symbol);
}
```

If the research view uses a tab activation pattern, only call
`renderConsensus` when the user actually clicks the "CONSENSUS" tab — the
underlying call hits up to 10 upstream modules and (on a cold cache) can
take ~20s.  The response is cached for 10 minutes server-side, so
subsequent visits during the same session are instant.

## 4. `setup_app.py`

Add `"synth_consensus"` to the `LOCAL_MODULES` list so py2app bundles it
into the Mac build:

```python
LOCAL_MODULES = [
    "app",
    "ai_summarizer",
    # ...
    "smart_money",
    "synth_consensus",          # ← new
    "synthetic_insider",
]
```

## 5. `requirements.txt`

No changes.

## 6. Verification

```python
import synth_consensus as sc
print(sc.consensus_score("AAPL"))
print(sc.consensus_score("NVDA"))
print(sc.consensus_score("TLT"))
```

Sample output observed during a 2026-05-26 dev run:

```
AAPL  score=50.2  label=NEUTRAL    n_contributors=6
  contributors: insider_form4_net, smart_money, alt_signals_avg,
                narrative_phase, ml_forecast_20d, gex_regime
  missing: ['congress_net_60d', 'factor_alpha', 'reflexivity', 'price_momentum']
NVDA  score=73.3  label=BULLISH    n_contributors=4
TLT   score=42.1  label=NEUTRAL    n_contributors=4
```

`missing` is non-empty whenever an upstream module errors, rate-limits, or
returns the wrong shape — the score still computes from the remaining
contributors.

## 7. Bugs / Quirks in upstream modules

These are real issues in existing modules that the consensus engine works
around — none of them were fixed inside their owning files per the
file-isolation rules.  Recommend a follow-up pass to address them in
their source modules:

1. **`sec_edgar.get_form4_transactions`** is **very** sensitive to
   SEC EDGAR rate-limiting (HTTP 429 from `www.sec.gov`).  During the
   smoke test, the NVDA call took ~70s and parsed 0 of 20 Form 4 filings
   because every XML fetch tripped the 429 cap.  Workaround: we gracefully
   degrade — if no insider transactions are returned, the contributor is
   marked missing.  Suggested fix in `sec_edgar`: longer backoff, smaller
   parallel pool, or per-symbol short-term caching of the parsed XMLs.

2. **`narrative_engine.analyze_narrative`** returns `narrative_phase`
   strings that include `"BREAKOUT"` per the spec example, but the actual
   `_detect_phase` only ever emits `ACCELERATION / EMERGENCE / REVERSAL /
   CONSENSUS / EXHAUSTION / DEVELOPING`.  Workaround: the consensus engine
   maps both spellings (`BREAKOUT` and `ACCELERATION`) to the same
   positive bucket, so callers that read the spec example don't get
   surprised.  Suggested fix: add an alias or rename one of them.

3. **`gex_engine.get_gex_summary`** returns `gamma_regime` strings of the
   form `"LONG GAMMA" / "SHORT GAMMA" / "NEUTRAL"`, but the spec example
   used `"POSITIVE" / "NEGATIVE"`.  Workaround: the consensus engine
   matches both vocabularies (`"LONG"` and `"POSITIVE"` both → +0.3,
   `"SHORT"` and `"NEGATIVE"` both → -0.3).

4. **`congress.get_recent_trades`** is an extremely slow PDF-parse path
   (downloads House PTR PDFs and runs pdfplumber on each).  For an
   unknown ticker with no recent trades it can still spend tens of
   seconds enumerating PDFs.  Workaround: we call `get_trades_for_ticker`
   which capping at `days=60` per the spec, and our consensus call's
   10-minute cache absorbs the cost.  Suggested fix: a fast "does this
   ticker appear in any recent filing at all?" pre-check before paying
   for full PDF parses.

5. **`research_tracker.get_track_record`** returns `hit_rate=None` and
   `n_directional=0` for every signal in a fresh install (no scoring has
   run yet).  Workaround: we fall back to the static weight in that case
   — this is by design but worth knowing.  Once
   `research_tracker.score_due_forecasts()` has been kicked a few times
   in production, the dynamic weight kicks in automatically.

6. **`fetcher.get_quote`** uses a 30s in-process cache.  If you call
   `consensus_score()` repeatedly for the same symbol, you may see the
   price-momentum contributor "snap" between values until the upstream
   yfinance refresh.  Not a bug — just expected behaviour.

## 8. TODOs

- Add `synth_consensus` to whatever the cache-warmer's "warm me on boot"
  list is so the first per-symbol research view doesn't pay the full
  cold-cache cost.
- Add a small spark-line of historical consensus scores once the tracker
  has been logging consensus calls for a few weeks.  The data layer is
  ready (`research_tracker.log_forecast(name="consensus", …)`) — just
  need a follow-up to actually log them from inside `_compute`.
- Consider promoting `price_momentum` out of the contributor list and
  using it as an independent overlay — it's much cheaper than the rest
  and double-counts what `ml_forecast` already partially captures.
