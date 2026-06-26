# AUGUR — Quantitative / Math-Correctness Audit

**Date:** 2026-06-26 · **Question answered:** *"Anywhere calculations, forecasting, or mathematical operations occur — is any of it fake/mock, or can it be enhanced?"*
**Method:** 5 parallel quant-reviewer subagents read the actual formulas (not docstrings) across all 30 math-bearing modules. READ-ONLY — nothing changed.

## Verdict legend
- **[FAKE]** — a number presented as a real probability/estimate but actually a hardcoded constant or arbitrary map of a score.
- **[WRONG]** — incorrect formula, wrong estimator, leakage, or unit/scale error.
- **[NAIVE→ENHANCE]** — real & roughly correct, but a stronger standard method exists.
- **[SOUND]** — correct; leave alone.

## Bottom line
The **infrastructure math is genuinely quant-grade** — purged out-of-sample evaluation, Brier accountability, block bootstrap, BL posterior, leak-free walk-forward, beta-adjusted stress test, FIFO with short-cover, and a fail-closed risk gate are all correct and, in several places, better than typical. **No upstream data is fabricated** — every calculation traces to real fetched prices/factors/chains.
The weakness is concentrated in **leaf "probabilities" and composite "scores"**: many are hand-set constants or arbitrary linear/tanh maps of a heuristic, then fed downstream *as if* calibrated. The single highest-value enhancement is wiring these leaf maps to the **already-existing accountability/tracker ledger** (logistic/isotonic calibration) instead of magic constants.

---

## A. FAKE — numbers fabricated or relabeled as real probabilities/estimates

| Location | What it fakes | Fix |
|---|---|---|
| `alt_data_engine.py:507` | `estimated_surprise_pct = (nowcast−50)*0.15` and `beat_probability` — an earnings "surprise %" invented from the heuristic score itself (a score of 80 always "predicts" +4.5%). **Most misleading quantity in the app.** | Fit `surprise ~ f(signals)` against realized surprises, or drop it. |
| `ml_forecast.py:514-526` | `mr_probability` is a hardcoded step-function of half-life (`<5→0.85, <15→0.70…`) presented as `prob_up` and fused into the ensemble. | Calibrate P(reversion) from realized reversion frequency by z-score bucket. |
| `ml_forecast.py:650` + `forecast_ensemble.py:136` | Trend-%→probability via arbitrary linear map (`±20% → ±0.4`). | Logistic fit of forecast-% → realized P(up). |
| `forecast_ensemble.py:73-81` | `_NARRATIVE_PHASE_PROB` — phase→prob hardcoded constants (EMERGENCE→0.62…). Low weight (0.08) limits harm. | Calibrate phase→P(up) from the accountability ledger. |
| `research_multihorizon.py:122,263,322` | Every non-RF horizon maps indicators→prob via hand-tuned `tanh`/linear gains (`*15, *3, *800, *0.25`). Disclosed as "deliberately simple." | Logistic calibration per horizon on realized N-day outcomes. |
| `synth_whatif.py:468-473` | ML-score→return map `(score−0.5)*2*20` — arbitrary ±20% ceiling, and pushes a 20-day classifier prob and a 0–1 composite through the *same* map. | Calibrate per score-bucket from tracker data; separate the two score types. |
| `synth_sectorflow.py:631` | `SECTOR_FACTOR_TILT` — self-admitted "heuristic, not regressed" invented sector betas. | Rolling regression of sector ETFs on FF factors (data already loaded). |
| `synth_groundhyp.py` (feature scalars) | Hardcoded per-feature scalars (`GEX ±0.3`, `alpha/15`, `pct/5`) silently weight a cosine geometry. | z-standardize features across the candidate pool. |
| `liquidity_monitor.py:557` | `percentile_rank` ranks the true 6-indicator composite against a **2-indicator (VIX+volume) proxy history** — apples-to-oranges. | Persist and rank against real composite history. |

---

## B. WRONG — incorrect formula / estimator / unit / scale

| Location | Defect | Fix |
|---|---|---|
| `research_factors.py:348-352` | OLS **homoskedastic** standard errors → alpha & factor **t-stats overstated** (daily residuals are autocorrelated/heteroskedastic). Point estimates fine; inference wrong. | Newey-West (HAC) covariance, lag ~5-10. |
| `research_eventstudy.py:503-519` | Abnormal return = `symbol_mean − benchmark_mean` (implicitly β=1, α=0), **not** a market-model AR; **no CAR significance test**; **overlapping events** treated as iid (inflates apparent significance). | Estimate (α,β) per event over a pre-event window; aggregate CAR + cross-sectional t-stat; de-overlap. |
| `ml_forecast.py:489-512` | OU half-life regressed against a **trailing rolling mean** (spread is mechanically mean-reverting by construction); **no stationarity (ADF) check**. | Test stationarity; regress vs a fixed/Kalman mean. |
| `ml_forecast.py:337` | Trend confidence band = in-sample residual std × √h — **ignores slope uncertainty**, so the band is too narrow far out. | Proper OLS prediction-interval formula (captures slope variance). |
| `aj_alpha.py:236-260` (`_kelly_factor`) | Kelly formula is correct & capped, but the **cold-start probability halves an already-calibrated edge**: `p = 0.5 + edge/200` where `edge` is already `prob_up_cal − 0.5` in points. | Use `p = 0.5 + edge_pts/100`; derive payoff `b` from realized avg-win/avg-loss. |
| `aj_rules.py:248-265` + `aj_alpha.py:446-470` | Per-symbol / sector **weight caps computed against total invested notional, not account equity** → first position always ≈100% of "book"; blind to cash/leverage. | Weight against equity (cash + positions). |
| `alt_data_engine.py:237-242` | Uses **ATR × √(calendar DTE)** as "historical avg move" — ATR is a daily range, not a return σ, and DTE should be trading days. Double unit error. | Return-std × √(trading days). |
| `synth_groundhyp.py:499-528` | Analog distance = **cosine on signed magnitude-bearing vectors over only shared dims with no shared-count floor** → two rows sharing one feature can score a perfect analog. **Highest-leverage stat bug** (every base-rate inherits bad analogs). | Standardized Euclidean (or Mahalanobis); require `shared≥3` + a max-distance ceiling. |
| `synth_cluster.py` | "Independent sources agreeing" premise **violated**: `smart_money` already internally folds insider/options/ml/sec, so the breadth bonus **double-counts** correlated sources. | Drop smart_money from the cluster set or down-weight overlapping sources. |
| `synth_bayessmart.py:378-380` | The Beta-Binomial update (L113) is **real Bayes**, but the final `bayes_score` is a **track-record-weighted average**, not a posterior — the "Bayesian Composite" label oversells. | Rename, or combine calibrated component likelihoods via log-odds for a true posterior P(up). |
| `synth_consensus.py:287` (also sectorflow, groundhyp) | **GEX short-gamma coded as directionally bearish (−0.3)** — short gamma is volatility-amplifying and **direction-symmetric**, injecting a persistent bearish bias. | Make GEX magnitude/confidence-only, not signed. |
| `synth_macrotranslate.py:491-502` | Point estimate sums **all FF factors** but the uncertainty band scales **SPY's IQR by market beta only**; release-date proxy (fixed 14/30-day lag) mis-dates real CPI/BEA releases. | Bracket with the projected quantity's own cross-episode IQR; use real release calendars. |
| `synth_divmap.py` | Single global `0.8` divergence threshold applied across pairs whose signals saturate at **different scales** (insider $5M vs smart_money 50pts). | z-score each signal vs its own history, or per-pair thresholds. |
| `synth_sectorflow.py:295,577` | Narrative term silently drops: tests `"BULL" in phase` but phase words (ACCELERATION/DEVELOPING) never contain "BULL". | Explicit phase→direction map. |
| `synth_whatif.py:197-203` | Sharpe mixes an annual RF rate with MC terminal-NAV stats over 365 days and uses terminal-NAV stdev (drift-contaminated) as the vol denominator. | Annualize return & vol consistently; vol from per-period return stdev. |
| `synth_catalyst.py:580-596` | Differences an option-implied **1-σ** move against a historical **mean-absolute** move (`E|X|≈0.7979σ`) → systematic ~−0.2 edge even on fair chains. | Convert σ→E|move| (×0.7979) before differencing. |
| `liquidity_monitor.py:94-96` + `synthetic_insider.py` (all channels) | **Neutral-50 placeholder for a dead feed enters the weighted composite** → biases a calm read toward ELEVATED (and deflates a high-conviction read). | Renormalize weights over *available* indicators; surface a coverage/confidence field. |

> **Resolved (not a bug):** `aj_positions.py` FIFO P&L was flagged for a missing ×100 options multiplier — verified false. `aj_options._row_premium_contract` returns `per_share × mult` (line 107), so premiums are stored **per-contract** and `mark()` matches; `(price − lot)·qty` is correct with no separate multiplier.

---

## C. NAIVE→ENHANCE — real & correct, but a stronger method exists

**Forecasting / ML**
- RF probabilities are **uncalibrated** (`ml_forecast.py:270`) — add `CalibratedClassifierCV` (isotonic/Platt) on the purged holdout; replace the single 60-row holdout with purged walk-forward CV.
- `forecast_accountability.py:315` — adaptive weights tilt on **hit-rate**, but the leaderboard ranks on **Brier skill**; tilt on Brier skill for consistency.
- Static inner-composite weights (`ml_forecast.py:664` 0.50/0.30/0.20) — could be performance-weighted (the ensemble layer already does this well).
- Horizon mismatch (`forecast_ensemble.py`) — RF(20d)/trend(30d)/MR signals fused under any requested horizon; horizon-scale or restrict.

**Portfolio / stats research**
- `research_optimizer.py:129` — **sample mean returns used directly** as expected returns (estimation-error trap) → shrink (James-Stein) or drive off risk only. **+** `:130` no covariance shrinkage → add **Ledoit-Wolf** (highest-value here). **+** BL final weights solved unconstrained then clipped (`:520`) → route μ̄ back through constrained SLSQP. **+** no transaction-cost/turnover penalty.
- `research_montecarlo.py:408` — "block bootstrap" is actually **iid (block_size=1)** → kills volatility clustering, understates clustered drawdowns. Use a real stationary block bootstrap (~5-21d). **+** MVN uses noisy daily sample mean as drift (`:423`) → demean/shrink. **+** PSD ridge `1e-12` cosmetic (`:437`) → eigenvalue repair.
- `research_iv_density.py` — enforces **monotonicity but not convexity** → floored negative densities bias mass; and splines call *prices* directly → fit an **SVI/SABR IV surface** for arbitrage-free densities by construction.
- `research_probforecast.py` — block bootstrap is correct but resamples raw returns (assumes next-horizon mean = trailing-year mean; no forward vol clustering) → EWMA/GARCH-filtered bootstrap; auto block-length (Politis-White).
- `research_backtest.py` — Sharpe has **no transaction costs**; `adapter_ml_forecast` reuses one current forecast across all bars (documented leakage — exclude from any "validated" hit-rate).

**Options / market-structure**
- **Straddle implied move** uses **raw straddle ÷ spot** and `lastPrice` (stale) in `earnings.py:373`, `smart_money.py:307`, `alt_data_engine.py:225` → apply the ~0.85 factor (or ATM IV×√(T/365)) and use bid/ask **mid**.
- `smart_money.py:321` — compares implied move (≈1.25σ) ÷ HV (1σ); divide the straddle by ~1.25 first.
- `gex_engine.py:64,87` — **no dividend yield `q`**; **hardcoded r=0.05** → add `q` from `.info`, pull `^IRX`. Dealer **call-long/put-short sign (`:354`)** is the industry-standard *naive proxy*, not measured flow — label it as such. (GEX core scaling, BS gamma, max-pain, gamma-flip interpolation, dealer-hedge sign are all **[SOUND]**.)

**Trading-agent**
- **No correlation-aware portfolio risk in *sizing*** — correlation is only a binary entry veto; two correlated names can each get full conviction size. Scale aggregate target by a portfolio vol/correlation budget.
- **Stops are fixed-% by default**, ATR stop is opt-in; and **sizing never keys off stop distance** → size so each trade risks a fixed fraction of equity (`equity × r% / ATR-stop`).
- `aj_alpha.py` RSI/ATR are **simple-MA, not Wilder's** (docstrings say Wilder); vol is close-to-close → Garman-Klass/Yang-Zhang from OHLC already fetched. Multiplicative stacking of 5 sizing factors is fragile → combine in log-space.
- Small-sample feedback: chronic-loser veto at n=3, adaptive thresholds at 5 trades → use Wilson lower-bound / larger windows.

**Indicators / synth**
- `historical_analog.py` — RSI is simple-MA (docstring says "Wilder"); **similarity is discrete band-matching, not a distance** → z-scored/Mahalanobis k-NN for graded, plentiful matches. `_MIN_MATCHES=5` is low-power → report CIs.
- `narrative_engine.py:186` — velocity compares **overlapping** 7d/14d windows with arbitrary `*2` → use non-overlapping windows.
- `reflexivity_detector.py:64` — trend slope in raw price-units/day (not comparable across price levels) → normalize by mean price / regress log-price.
- `contagion_graph.py:493` — takes **max |corr| over 6 lags** (data-snooping bias) → penalize the lag search (Bonferroni / require best-lag to beat lag-0 by a margin).
- `synth_peerdiv.py:704` — MAD **not scaled by 1.4826**, so MAD-branch and stdev-fallback z-scores are on different scales yet ranked together → `denom = 1.4826·mad`; require ≥3 peers.
- `synth_consensus.py:653` — thin single-source consensus gets full dynamic range → attenuate by coverage.

---

## D. Done genuinely WELL (verified correct — do not touch)
- **Purged/embargoed out-of-sample RF evaluation** (`ml_forecast.py:180,246`) — scaler fit train-only; correctly avoids the label-window leakage that made the old version read 95-100%. The single most impressive piece.
- **Brier score + reliability curve + skill-vs-base-rate** (`forecast_accountability.py:140`) — textbook-correct, consistent populations.
- **Disagreement-shrinkage calibration** + adaptive performance weighting (`forecast_ensemble.py:356,327`).
- **Block bootstrap** (`research_probforecast.py:276`) — correct, vectorized, no wrap-around, log-return summation, genuine empirical probabilities.
- **Black-Litterman posterior** (`research_optimizer.py:462`) — textbook He-Litterman; date-aligned covariance avoids trailing-index corruption.
- **Simple-vs-log return unit consistency** in the factor regression (`research_factors.py:319`) — a subtle bias most implementations get wrong.
- **Leak-free walk-forward backtest core** (`research_backtest.py:531`); deterministic horizon-close scoring (`research_hypothesis.py:632`); point-in-time price lookup + double-score race prevention (`research_tracker.py`).
- **BL risk-neutral density** discounting + arbitrage-monotonicity enforcement (`research_iv_density.py`).
- **fetcher.py indicators**: genuine **Wilder RSI**, MACD signal-EMA offset alignment, population-stdev Bollinger, correct TR, **full-sample Sortino**, geometric (CAGR) annualization, and a **beta-adjusted** stress test with display-name sector overrides.
- **GEX core** (S²·0.01 scaling, BS gamma, max-pain, gamma-flip interpolation, dealer-hedge sign negation) and **aj_options** per-contract ×100 handling, mid pricing, liquidity gate.
- **Trading agent**: fail-closed gate (master switch re-read fresh, daily-loss HALT, model-out-of-loop kill switch, NaN/inf guards), **FIFO lot matching incl. short-cover + pro-rated closing fees**, day-P&L session baseline, IPS new-breach-by-identity check, **conviction/edge-scaled sizing (confirmed NOT equal-notional)**, vol-target & drawdown-throttle factors, genuinely out-of-sample backtest-lite hit-rate.
- **Beta-Binomial conjugate update** (`synth_bayessmart.py:113`); correct guarded z-scores in `synth_sectorflow`/`synth_cluster`; robust MAD→stdev fix in `synth_peerdiv`; opposite-sign opposition check in `synth_divmap`; HHI concentration in `synth_whatif`; OPEX third-Friday math in `synth_catalyst`.

---

## Suggested priority (if/when addressed — not done here)
1. **`alt_data_engine` fake surprise%/beat-probability** (B + A) — most misleading; either calibrate or remove.
2. **Neutral-50 composite bias** (`liquidity_monitor`, `synthetic_insider`) — renormalize over available indicators; mechanical, high-impact, low-risk.
3. **Calibrate the leaf "probabilities"** to the existing accountability/tracker ledger (mr_probability, narrative phase, trend-%, multihorizon, whatif) — removes most [FAKE] tags at once.
4. **`research_factors` HAC SEs** + **`research_eventstudy` market-model AR & CAR significance** — fixes statistical inference.
5. **`research_optimizer` Ledoit-Wolf shrinkage + return shrinkage** — stabilizes portfolio weights (biggest robustness win).
6. **`aj_alpha` Kelly probability** + **weight caps on equity not notional** — trading-agent correctness.
7. **`synth_groundhyp` distance metric** + **`synth_cluster` independence** — fixes the two worst synth stats.
8. Polish: Wilder RSI/ATR in aj_alpha & historical_analog, straddle 0.85 factor + mid pricing, GEX dividend/rate, real block bootstrap, IV convexity/SVI.
