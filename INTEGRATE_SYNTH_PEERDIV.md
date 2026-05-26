# Integrating Peer Divergence Detector (`synth_peerdiv`)

Three new files ship with this enhancement:

- `synth_peerdiv.py` — peer discovery + multi-source signal fusion + robust
  z-score divergence ranking
- `static/js/synth_peerdiv.js` — `window.renderPeerDiv(container, symbol)`
- (this file)

No new Python or JS dependencies are required.

## What it does

For any ticker:

1. **Discover peers** — top-N peers via `contagion_graph.build_graph(symbol)`
   (edges of type `peer` ranked by weight, foreign tickers like `005930.KS`
   dropped). When contagion yields fewer than N usable peers, the module
   backfills from a small curated industry → peers map (semis, software,
   banks, healthcare, etc.) keyed off `fetcher.get_fundamentals(s)["industry"]`.
2. **Fetch 10 metrics in parallel** for the symbol + each peer:
   PE, ML 20d up-prob, smart-money score, FF5 annual alpha, YTD return,
   Form-4 60d net flow, Congressional 60d net trades, narrative velocity,
   front-month IV, market beta.
3. **Score divergence** per metric:
   `z = (sym − peer_median) / max(peer_mad, 1e-9)` — MAD is robust to a
   single peer outlier blowing up a stdev-based score. Peers missing a
   given metric are simply dropped from that metric's median+MAD, not
   from the whole row.
4. **Rank** all metrics by `|z|` desc and surface the top 5 as the
   "where this stock breaks from peers" callout.

Empirical NVDA run (2026-05):

```
factor_alpha_annual    z=+4.30   alpha 22% vs peer median -14%
factor_mkt_beta        z=+2.54   beta 1.87 vs peer median 1.63
options_iv_30d         z=-1.93   IV 57% vs peer median 110%
insider_form4_net_60d  z=+1.00   net buying vs peers' net selling
congress_net_60d       z=-7.00   one net sell vs peers' flat books
```

## 1. `app.py`

Add the module guard and the route alongside the existing `/api/synth/...`
or `/api/research/...` endpoints:

```python
# ── Peer Divergence ─────────────────────────────────────────────
try:
    import synth_peerdiv
except Exception as _pd_err:
    synth_peerdiv = None
    log.warning("synth_peerdiv unavailable: %s", _pd_err)


@app.route("/api/synth/peerdiv/<symbol>")
def synth_peerdiv_route(symbol):
    if not synth_peerdiv:
        return jsonify({"error": "synth_peerdiv module not available"}), 500
    if not _valid_ticker(symbol):
        return jsonify({"error": "Invalid symbol"}), 400
    n = _safe_int(request.args.get("n"), 5)
    n = max(1, min(n, 12))
    return jsonify(synth_peerdiv.peer_divergence(symbol.upper(), n))
```

The endpoint returns the full envelope described in `synth_peerdiv.peer_divergence`
docstring — symbol, peers, n_peers, metrics, metric_descriptions,
top_divergences, as_of, and (when applicable) errors.

## 2. `templates/index.html`

In the per-symbol Research view's tab list (same row as `FACTORS`,
`EVENT STUDY`, etc.), add a new tab:

```html
<button class="research-tab" id="rtab-peerdiv"
        onclick="switchResearchTab('peerdiv','{{ symbol }}')">PEER DIVERGENCE</button>
```

Then add a panel slot somewhere inside the research tab content area:

```html
<div id="peerdiv-host" class="card"></div>
```

If the per-tab markup is generated entirely by JS (current build's
`loadResearchFor` does this), just append the same `<div>` host element
alongside the other tab containers — `renderPeerDiv` accepts any DOM node.

Load the script after `app.js`:

```html
<script src="{{ url_for('static', filename='js/synth_peerdiv.js') }}"></script>
```

## 3. `static/js/app.js`

Hook into the existing `switchResearchTab` dispatch without editing the
rest of the file. Add a branch in the existing `if/else` ladder (current
last branch is `eventstudy` at ~line 3785):

```js
} else if (tab === 'peerdiv') {
  const pdEl = document.getElementById('peerdiv-host');
  if (pdEl && window.renderPeerDiv && pdEl.dataset.loaded !== '1') {
    pdEl.dataset.loaded = '1';
    try { window.renderPeerDiv(pdEl, symbol, { n: 5 }); }
    catch(e) { pdEl.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
  }
}
```

(Reset `pdEl.dataset.loaded = ''` in your existing symbol-change handler so
switching to a new ticker re-renders the panel.)

## 4. `setup_app.py`

Add `"synth_peerdiv"` to `LOCAL_MODULES` so py2app bundles it into the
Mac build:

```python
LOCAL_MODULES = [
    "app",
    ...
    "smart_money",
    "synthetic_insider",
    "synth_peerdiv",            # ← new
]
```

## 5. `requirements.txt`

No changes — module uses only Python stdlib + numpy (already in stack).

## 6. Verification

```python
import synth_peerdiv
r = synth_peerdiv.peer_divergence("NVDA", 5)
print(r["peers"])
for d in r["top_divergences"]:
    print(d["metric"], d["z_score"], d["interpretation"])
```

Expect NVDA's high factor alpha (z>+2), elevated market beta, and an
asymmetric 60d insider/Congress flow signature to bubble to the top.

## 7. Performance & caching

- **Cold-call cost**: ~120–400s for the first NVDA-shaped run. The bulk
  is `smart_money.compute_score` (15s × 6 tickers, but parallelised),
  `sec_edgar.get_form4_transactions` (5–10s × 6, also parallelised),
  and `research_factors.factor_exposure` (1–10s × 6).
- **Warm-call cost**: <10ms via `cache_store.coalesce(("peerdiv", sym, n), 900, …)`.
  All sub-fetches sit on their own cache_store entries (fundamentals 24h,
  factors 24h, form4 implicit via sec_edgar, congress 24h, options 10min,
  etc.) so even a "cache-cold peerdiv but caches-warm metrics" run is
  fast (~30s).
- **Congress trades pre-warm**: `congress.get_recent_trades` is called
  ONCE per `peer_divergence` invocation for all symbols at once — without
  this each of the 6 per-ticker metric workers would individually
  download ~80 PTR PDFs in parallel, which kills the SEC connection.

## 8. Bugs found in upstream modules (workarounds documented; not fixed)

- **`research_factors._stock_log_returns` is brittle under yfinance
  rate-limiting.** A bursty back-to-back call set (which this module
  triggers — 6 simultaneous `factor_exposure` requests) can cause every
  one to return `{"error": "no price history"}` even when
  `fetcher.get_chart_data` itself works for the same ticker seconds
  earlier. Root cause: `_stock_log_returns` uses `yf.Ticker(...).history`
  directly instead of the fetcher's resilient Yahoo-direct-chart
  fallback. Workaround in `synth_peerdiv`: tolerate `alpha_annual_pct`
  being `None` and skip that peer from the metric's median+z (already
  the default behaviour). Future fix in `research_factors`: route
  through `fetcher.get_chart_data` so it picks up the rate-limit
  fallback.
- **SEC EDGAR's `/files/company_tickers.json` 429s aggressively in CI**
  (or under any burst of fresh CIK lookups). `sec_edgar` already retries
  but eventually gives up. When this happens for our test peers,
  `_m_insider_form4_net_60d` returns 0.0 instead of a real signed flow.
  This is benign (the peer is dropped from the divergence calc when
  every value is exactly 0) but should be revisited if EDGAR auth gets
  loosened. Not a workaround we can fix from outside `sec_edgar`.
- **`contagion_graph.build_graph` peer list is non-deterministic.**
  Different runs return different peer sets (`['META','AMD','INTC','AVGO','QCOM']`
  vs `['AVGO','AMD','INTC','MRVL','TSM']` for NVDA) depending on which
  SEC 10-K fetches succeed during graph construction. This means the
  cache key `("peerdiv", "NVDA", 5)` may not actually hit on a repeat
  call if the peer list shifts — the divergence is computed against
  whatever peers came back this time. Acceptable for v1 but warrants a
  fixed "preferred peers" cache key strategy in v2 (e.g. compute peers
  separately, cache them for 24h, then key divergence off the cached
  peer set).
- **`congress.get_trades_for_ticker(ticker, days=180)` defaults to
  `max_pdfs=200`** which is overkill for a 60-day net-flow calc.
  Our pre-warm uses `max_pdfs=80` directly and partitions by ticker.

## 9. TODOs / future work

- Make the curated `_INDUSTRY_PEERS` map data-driven — pull it from
  `database.py`'s holdings table, or join with `wikidata_meta` to extract
  same-GICS-sub-industry peers programmatically.
- Add a "metric weight" option so users can up-weight (say) factor alpha
  and down-weight narrative velocity in the top-divergences ranking.
- Surface a per-metric sparkline of historical z-scores (rolling 90d)
  so a +1.8 z today can be contextualised as "highest since last
  earnings" vs "typical".
- The JS renderer currently shows raw numbers — adding `_fmt` overrides
  per metric (e.g. `pe_ratio` → no decimals over 100, `congress_net_60d`
  → plain integer) would tighten the table presentation.
