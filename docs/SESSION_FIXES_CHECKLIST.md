# Session Fix Checklist — "fix every new and open item, skip none"

> **STATUS: COMPLETE (2026-06-27).** All P1–P6 items implemented across 44 source files via 6 parallel agents; P7 integration green: 100/100 modules import, all 3 JS files `node --check` pass, full deterministic regression = 0 failures (25 suites, exit 0). Two test assertions updated to the now-correct behavior (audit-justified): `test_aj_100x` ATR-on-by-default, `test_aj_council_phase6` fingpt lexicon contract. The ◇ items below were consciously left unchanged with rationale (safety invariants / disclosed design) — not skipped.


Source audits: `docs/MOCK_AND_DEADCODE_INVENTORY.md` + `docs/QUANT_MATH_AUDIT.md`.
Status: ☐ todo · ☑ done · ◇ intentional-no-change (rationale given).

## Guardrails (apply to every fix)
- Never break a public function signature or a return-dict key the UI/API/tests rely on (add fields, don't rename/remove).
- Preserve the fail-closed risk gate, paper-first/live-never-auto, and hash-chained audit invariants.
- No new heavy dependencies (must keep py2app desktop bundle buildable). Pure-numpy/scipy/sklearn only (already bundled).
- All new computation must fail OPEN to the prior neutral/None behavior on error — never raise into a request path.

## P1 — Forecast / ML
- ☐ A2 `ml_forecast.py:514` mr_probability → empirical reversion frequency by z-bucket (not hardcoded steps)
- ☐ A3 `ml_forecast.py:650` + `forecast_ensemble.py:136` trend-%→prob → normal-CDF mapping `Φ(exp_ret/(vol·√h))`
- ☐ A4 `forecast_ensemble.py:73` narrative-phase prob → calibrate from accountability ledger if data, else documented prior
- ☐ A5 `research_multihorizon.py:122,263,322` tanh gains → vol-normalized normal-CDF probability
- ☐ B3 `ml_forecast.py:489` OU half-life → add ADF stationarity guard; flag low-confidence when non-stationary
- ☐ B4 `ml_forecast.py:337` trend CI → proper OLS prediction interval (slope uncertainty)
- ☐ C `ml_forecast.py:270` RF probs uncalibrated → `CalibratedClassifierCV` (isotonic) on purged holdout
- ☐ C `forecast_accountability.py:315` adaptive weights → tilt on Brier skill, not hit-rate

## P2 — Stats / portfolio research
- ☐ B1 `research_factors.py:348` → Newey-West (HAC) standard errors
- ☐ B2 `research_eventstudy.py:503` → market-model AR (est-window α,β) + cross-sectional CAR t-stat
- ☐ C `research_optimizer.py:130` → Ledoit-Wolf covariance shrinkage + expected-return shrinkage; route BL through constrained SLSQP; optional turnover penalty
- ☐ C `research_montecarlo.py:408` → real stationary block bootstrap (block≈10); demean MVN drift; eigenvalue PSD repair
- ☐ C `research_iv_density.py` → enforce convexity (isotonic on 2nd differences), not just monotonicity
- ☐ C `research_backtest.py` → per-turnover transaction cost in Sharpe; exclude ml-adapter from "validated" hit-rate

## P3 — Options / microstructure
- ☐ A1 `alt_data_engine.py:507` fake `estimated_surprise_pct`/`beat_probability` → drop the fabricated %, relabel as ordinal "signal strength" (no model = no number)
- ☐ B7 `alt_data_engine.py:237` ATR×√(cal dte) → return-std × √(trading days)
- ☐ B17 `liquidity_monitor.py:94` + `synthetic_insider.py` neutral-50 bias → renormalize weights over available indicators + emit coverage field
- ☐ A9 `liquidity_monitor.py:557` percentile vs proxy history → persist real composite history, rank against it
- ☐ C straddle implied move (raw/spot, lastPrice) → ×0.85 factor + bid/ask mid: `earnings.py:373`, `smart_money.py:307`, `alt_data_engine.py:225`
- ☐ C `smart_money.py:321` implied(≈1.25σ)÷HV(1σ) → divide straddle by ~1.25 first
- ☐ C `gex_engine.py:64,87` add dividend yield q + pull ^IRX risk-free; label dealer call-long/put-short as naive proxy
- ☐ LOW `smart_money.py:805` relabel "AI-analyzed SEC filings" → "8-K item-code analysis"
- ☐ LOW `synthetic_insider.py:6-8` soften unverifiable backtest docstring claim

## P4 — Trading-agent math
- ☐ B5 `aj_alpha.py:252` Kelly cold-start → `p = 0.5 + edge_pts/100`; derive b from realized win/loss
- ☐ B6 `aj_rules.py:248` + `aj_alpha.py:446` weight caps → against account equity (cash+positions), not invested notional
- ☐ C `aj_strategy.py:49` correlation-aware sizing → scale aggregate target by portfolio correlation/vol budget
- ☐ C `aj_rules.py:146` make ATR stop the default exit; `aj_strategy.py` size off ATR stop distance (risk = equity·r%/stop)
- ☐ C `aj_alpha.py:138,194` RSI/ATR → Wilder's smoothing (or fix docstrings); vol close-to-close → Garman-Klass/Yang-Zhang
- ☐ C `aj_alpha.py:294,662` small-sample vetoes → Wilson lower-bound / larger windows; log-space factor stacking

## P5 — Indicators + synth
- ☐ B11 GEX directional-bearish → magnitude/confidence-only: `synth_consensus.py:287`, `synth_sectorflow.py`, `synth_groundhyp.py`
- ☐ B8 `synth_groundhyp.py:499` cosine → standardized Euclidean + require shared≥3 + max-distance ceiling; A8 z-standardize features
- ☐ B9 `synth_cluster.py` independence → drop/down-weight smart_money (double-counts insider/options/ml/sec); use cross-sectional percentiles for 13F
- ☐ B10 `synth_bayessmart.py:378` rename to "track-record-weighted composite" + add true log-odds posterior P(up)
- ☐ B13 `synth_divmap.py` global 0.8 threshold → z-score each signal vs own history (per-pair scale)
- ☐ B14 `synth_sectorflow.py:295` narrative term silently drops → explicit phase→direction map; A7 SECTOR_FACTOR_TILT → regressed sector betas
- ☐ B12 `synth_macrotranslate.py:491` multi-factor estimate / single-factor band → bracket with projected-quantity IQR; real release calendar lag
- ☐ B16 `synth_catalyst.py:580` σ vs E|move| → ×0.7979 before differencing; shrink edge by n_events
- ☐ C `synth_peerdiv.py:704` MAD×1.4826 + require ≥3 peers
- ☐ C `synth_consensus.py:653` attenuate thin consensus by coverage
- ☐ C `historical_analog.py:43` RSI Wilder/doc + Mahalanobis/z-scored k-NN similarity + CIs (raise MIN_MATCHES)
- ☐ C `narrative_engine.py:186` velocity → non-overlapping windows
- ☐ C `reflexivity_detector.py:64` slope → normalize by mean price (or log-price)
- ☐ C `contagion_graph.py:493` max-|corr|-over-6-lags snooping → require best-lag to beat lag-0 by margin

## P6 — Dead-code + mock features
- ☐ HIGH `aj_personas.py:96` fingpt_sentiment → implement a REAL lightweight finance-lexicon scorer as default (no heavy deps); keep local-helper hook
- ☐ MED `aj_routing.py:78` quality_floor length-proxy → rename to `_meets_min_length` + document it's a completeness heuristic
- ☐ MED `data_sources.py:14` stale CBOE put/call docstring → correct it (feature never existed; keep NotImplementedError redirect)
- ☐ MED `sec_filings_v2.py` inert unless unpinned edgartools → add to `requirements.txt` (runtime, NOT build) + keep is_available guard
- ☐ LOW `cli.py:2187` web-terminal run_command omits ask/briefing/health → add to dispatch + allowlist (match main())
- ☐ LOW `cache_warmer.py:78` unused BENCHMARK_INTERVAL → remove
- ☐ LOW `static/js/app.js:6569` empty `_buildMLDetailPanel` + dead var → remove
- ☐ LOW `static/js/app.js:7929` orphan comment banner → remove
- ☐ LOW `static/js/research_iv_density.js:207` disabled leftover dataset → remove
- ☐ LOW `static/js/synth_bayessmart.js:57` duplicate green branch → remove
- ☐ LOW `synth_bayessmart.py:174` seed footgun → guard `seed_synthetic_history` behind explicit `AUGUR_ALLOW_SEED=1` + refuse default wealth.db

## Intentional — NO CHANGE (rationale; not skipped)
- ◇ `aj_broker.py` CCXT/Robinhood inert stubs — paper-first/live-never-auto safety invariant; raising > faking. Only improve label.
- ◇ `aj_opencode.py` sandbox stub — VERIFY-OPENCODE-gated; wiring a binary is out of scope + a safety gate.
- ◇ `aj_execution.disconnect_all` no-op — correct for non-persistent-connection posture.
- ◇ `aj_mcp_read.serve_stdio` — optional transport, raises clearly if dep absent.
- ◇ `database.py` `_RETIRED_SETTING_KEYS`/migration scaffolding — intentional forward-stamp framework.
- ◇ `aj_memory.py` markdown recall — disclosed deliberate design (vector store rejected on purpose).
- ◇ `ai_summarizer` `_rule_based_*`, `opportunity_scanner` crypto neutral-dims, `aj_alpaca` no-crypto, `hn_sentiment` keyword polarity, `app.js` loading-stage UX, `jarvis_claude` empty trace fields — honest disclosed fallbacks / cosmetic UX, not defects.
- ◇ `research_backtest adapter_ml_forecast` leakage — already documented; P2 only excludes it from "validated" hit-rate (no behavior break).
- ◇ `fetcher.py` `_SCENARIOS`, FF/FOMC calendars, `SP500_TOP100` snapshots — legitimate reference constants.
