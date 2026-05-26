# INTEGRATE_SYNTH_CATALYST

How to wire `synth_catalyst.py` (Catalyst Timeline) into AUGUR. The
worktree branch is file-isolated — these edits should be applied by the
integrator on `main`.

## 1. `app.py` — two new Flask routes

Add this block next to the other research routes:

```python
# ── Catalyst Timeline (synthesis layer) ───────────────────────────
try:
    import synth_catalyst
except Exception as _sc_err:
    synth_catalyst = None
    log.warning("synth_catalyst unavailable: %s", _sc_err)


@app.route("/api/synth/catalyst", methods=["GET"])
def synth_catalyst_get():
    """Returns the catalyst timeline for the user's portfolio holdings."""
    if synth_catalyst is None:
        return jsonify({"error": "synth_catalyst module not available"}), 500
    days_ahead = _safe_int(request.args.get("days_ahead"), 60)
    return jsonify(synth_catalyst.catalyst_timeline(
        symbols=None, days_ahead=days_ahead,
    ))


@app.route("/api/synth/catalyst", methods=["POST"])
def synth_catalyst_post():
    """Same, but with a caller-supplied symbol list."""
    if synth_catalyst is None:
        return jsonify({"error": "synth_catalyst module not available"}), 500
    body = request.get_json(silent=True) or {}
    syms = body.get("symbols") or []
    if not isinstance(syms, list):
        return jsonify({"error": "`symbols` must be an array"}), 400
    # Sanitize: keep only short, ticker-shaped strings.
    syms = [str(s).strip().upper() for s in syms if str(s).strip()]
    syms = [s for s in syms if _valid_ticker(s)][:25]
    days_ahead = _safe_int(body.get("days_ahead"), 60)
    return jsonify(synth_catalyst.catalyst_timeline(
        symbols=syms, days_ahead=days_ahead,
    ))
```

`_valid_ticker`, `_safe_int` are already defined in `app.py`.

## 2. `templates/index.html` — new top-level view + script

### 2a. Nav button

Add the nav-pill alongside the existing top-level views (DASHBOARD /
RESEARCH / IDEAS / etc — the exact selector depends on your nav markup):

```html
<button class="nav-pill" data-view="catalysts" onclick="switchView('catalysts')">
  CATALYSTS
</button>
```

### 2b. View container

Add the matching `<section>` after the other top-level views:

```html
<section id="view-catalysts" class="view" style="display:none">
  <div id="catalysts-host"></div>
</section>
```

### 2c. Script include

Add the JS file alongside the other `static/js/*.js` includes at the
bottom of `<body>`:

```html
<script src="/static/js/synth_catalyst.js?v=1"></script>
```

Place it after `app.js` so `renderCatalystTimeline` is registered before
`switchView` ever runs.

## 3. `static/js/app.js` — wire view switch

In whichever function shows the top-level views (often `switchView` or
similar), add a case that lazy-renders the catalyst timeline the first
time the user opens it:

```js
} else if (view === 'catalysts') {
  const host = document.getElementById('catalysts-host');
  if (host && host.childElementCount === 0 &&
      typeof renderCatalystTimeline === 'function') {
    // Default: portfolio symbols, 60-day horizon. Users can override
    // inside the panel.
    renderCatalystTimeline(host, { symbols: null, days_ahead: 60 });
  }
}
```

If the host already has rendered content, leave it alone — the panel's
own RUN button refreshes data without re-mounting.

## 4. `setup_app.py` — bundle inclusion

Add `"synth_catalyst"` to `LOCAL_MODULES` (alphabetical ordering puts it
between `smart_money` and `synthetic_insider`):

```python
LOCAL_MODULES = [
    ...,
    "smart_money",
    "synth_catalyst",
    "synthetic_insider",
]
```

No additions to `EXTRA_PACKAGES`: the module's only third-party
dependencies (`yfinance`, `numpy`, `pandas`) are already declared
transitively via `research_eventstudy` / `research_iv_density`.

## 5. `requirements.txt`

No new dependencies.

## 6. (Optional) Cache warmer hook

If `cache_warmer.py` runs warm-up coroutines for other research panels,
consider adding a daily entry for the catalyst timeline so the first
user open is instant. Suggested cadence: once at 06:00 local time,
`symbols=None, days_ahead=60`. The TTL inside `synth_catalyst` is 6h, so
two daily warms cover all market hours.

```python
# Inside cache_warmer.py — add to the rotation
try:
    import synth_catalyst
    synth_catalyst.catalyst_timeline(symbols=None, days_ahead=60)
except Exception as e:
    log.debug("catalyst warmer skipped: %s", e)
```

## 7. Smoke test after integration

```bash
# from repo root, with venv activated
python - <<'PY'
import synth_catalyst as sc
out = sc.catalyst_timeline(["AAPL", "NVDA", "SPY"], days_ahead=120)
print("n_events:", len(out["events"]))
from collections import Counter
print("by_type:", dict(Counter(e["type"] for e in out["events"])))
print("first:", out["events"][0] if out["events"] else None)
PY
```

Expected (within 120-day horizon): at least 12 events — 1-2 FOMC × 3
symbols + 4 OPEX × 3 symbols + per-symbol earnings hits. Per-event
overlays (`implied_move_pct`, `historical_avg_move_pct`) may be `None`
when yfinance is rate-limited; that's expected and the UI handles it.

Then in the running app: open the new **CATALYSTS** view → table
populates and the timeline chart shows colored markers (green ⇒ implied
is cheap vs historical, red ⇒ implied expensive). Filter checkboxes and
column header sorts are interactive.

## Known limitations / TODOs

- **yfinance rate limiting.** The dominant failure mode in the field:
  the option-chain calls and `Ticker.earnings_dates` lookups get
  throttled, and the timeline ships with `null` overlays for affected
  rows. The note text degrades gracefully ("implied move unavailable").
  Pre-warming via `cache_warmer` is the practical fix.

- **Ex-dividend coverage.** Pulled from `yfinance.Ticker().calendar`
  (`Ex-Dividend Date`). This is empty for many non-dividend names and
  for some indices. Acceptable — they simply don't contribute rows.

- **FOMC implied move.** Uses SPY's RND at the nearest post-meeting
  expiry as the implied-move proxy for every symbol-FOMC pair, since
  FOMC is a market-wide event. Per-name implied moves around FOMC
  could be added later by re-querying each symbol's chain at the same
  expiry.

- **Edge score cap.** Capped at ±3.0 to keep one noisy row from
  dominating the chart's color scale. The raw ratio is preserved in
  the note text.

- **Earnings sample depth.** `research_eventstudy` typically yields
  4–16 past earnings instances per symbol (limited by yfinance's
  `earnings_dates` history depth). When `n_events < 5` the historical
  overlay is set to `None`. See `research_eventstudy` for the same
  caveat at the source.

- **Portfolio scoping.** When `symbols=None`, the entire portfolio is
  used and FOMC + OPEX events are duplicated per holding. This is
  intentional (each holding has its own current_signals stack) but can
  produce a long table for large portfolios. The UI's per-symbol
  filter handles this.
