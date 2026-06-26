# AUGUR — Mock / Dead-Code / Not-Production-Ready Inventory

**Date:** 2026-06-26 · **Scope:** entire repo (136 Python files ~74k LOC + 24 JS + templates), excluding `venv/`, `node_modules/`, `build/`, `dist/`, test files, and scan-artifact dirs.
**Method:** deterministic grep sweep + 6 parallel read-only subagent audits partitioned by subsystem.
**Nature:** INVENTORY ONLY — nothing was fixed or changed.

## Headline
- **No TODO/FIXME/XXX/HACK markers** anywhere in source (test files excluded).
- **No commented-out code blocks** — every `#`-prefixed code-looking line is an explanatory math/behavior note, not dead code.
- **Only one `raise NotImplementedError`** in the whole tree, and it's an intentional fail-fast redirect.
- **HIGH-severity "advertised feature is fake": 1** (`aj_personas.fingpt_sentiment`), and even it is opt-in + disclosed + fails open (never fabricates a number).
- The dominant pattern that *looks* like mocking — `try/except → return None/[]/score:50` — is, on inspection, **legitimate graceful degradation** (neutral value when a real upstream is down), not fabricated data.

Severity key: **HIGH** = advertised feature returns fake/non-functional data · **MED** = partial/placeholder, or a real path that substitutes a heuristic for the advertised thing · **LOW** = cosmetic dead code / disclosed minor heuristic / transparency note.

---

## CATEGORY 1 — Dead / commented / missing code

| Sev | Location | Finding |
|---|---|---|
| LOW | `data_sources.py:438` | Intentional loud-stub `raise NotImplementedError` gating the legacy-named `cboe_put_call_ratio()` — fail-fast redirect to `cboe_vix_history()`. |
| LOW | `aj_opencode.py:73-76` | `_run_sandboxed()` is an unimplemented `# pragma: no cover` stub (returns `executable: False`). Double-gated behind `VERIFY-OPENCODE`; `available()` is false by default. |
| LOW | `aj_execution.py:482-485` | `disconnect_all()` is a documented `return None` no-op (correct for the non-persistent-connection posture). |
| LOW | `aj_mcp_read.py:215-225` | `serve_stdio()` optional MCP transport; raises clear `RuntimeError` if the optional `mcp` package is absent. Intentional, not missing impl. |
| LOW | `database.py:354` | `_RETIRED_SETTING_KEYS = []` empty → the settings-retirement sweep in `_migrate_settings` is dead scaffolding until populated. |
| LOW | `database.py:377-379` | Settings-migration framework is a no-op that only stamps the version forward (`# For now we just stamp the version forward.`). |
| LOW | `cli.py:2187-2219` | Web-terminal `run_command()` dispatch omits `ask`/`briefing`/`health` (present in `main()`); those Jarvis commands are unreachable from the web terminal. Likely oversight. |
| LOW | `cache_warmer.py:78` | `BENCHMARK_INTERVAL` constant now only surfaced in `status()`; underlying benchmark warm task was deliberately removed. |
| LOW | `static/js/app.js:6569-6572` | `_buildMLDetailPanel()` is an empty stub returning `''`; its result var `mlPanel` is never referenced. Real ML detail loads via `renderMLForecastPanel`. |
| LOW | `static/js/app.js:7929-7935` | Orphan/misplaced comment banner ("NARRATIVE VELOCITY ENGINE" header with no code beneath it). |
| LOW | `static/js/research_iv_density.js:207-221` | Disabled leftover `Spot` chart dataset (`hidden:true`), superseded by the `spotMarker` plugin. |
| LOW | `static/js/synth_bayessmart.js:57-58` | Duplicate green branch in `_scoreColor` (`>=75` and `>=60` return identical color). |

---

## CATEGORY 2 — Mock / not-production-ready / not-truly-functional features

### HIGH — advertised feature does not actually do the real thing
| Location | Finding |
|---|---|
| `aj_personas.py:96-129` `fingpt_sentiment()` | The advertised "FinGPT numeric sentiment prior" **can never return a score**: it looks up project-local helpers `fingpt_local`/`finbert_local`, **neither of which exists in the repo**, so every path returns `None`. Headline gathering + clamp/label logic are real; the actual scoring is an absent integration stub. Mitigated: opt-in (`fingpt_sentiment_enabled`), lazy, fail-open, honestly documented as a "Placeholder scoring hook." Functionally degrades cleanly rather than faking a number — but as a *named feature* it is non-functional. |

### MED — real path substitutes a heuristic / stub for the advertised thing (mostly disclosed, several by-design)
| Location | Finding |
|---|---|
| `aj_opencode.py:59-76` | Self-improvement sandbox runner is unimplemented (fail-closed stub by design; cannot execute or fake a result). |
| `aj_broker.py:241-293` | Live `CCXTBroker` & `RobinhoodBroker` are inert `BrokerNotEnabled`-raising stubs — no real execution path. By design (VERIFY-gated, fail-closed); factory forces `PaperBroker` when live is off. Only Alpaca has a real live adapter. |
| `aj_routing.py:78-84` `_meets_floor()` | Advertised `quality_floor` is enforced with a **string-length proxy** (`len(text) >= 8*floor`), not real quality grading. Honestly commented but a caller trusting it as a quality bar would be misled. |
| `data_sources.py` (docstring l.14 + l.438) | The advertised **"CBOE put/call ratio"** feature **never existed** — the legacy-named function only ever fetched VIX. Docstring is stale/misleading; function now fail-fast redirects. |
| `sec_filings_v2.py` | Entire module is inert unless the optional, **unpinned** `edgartools` package is installed; otherwise every function returns `[]`. Likely non-functional in the shipped build; data is real when the dep is present. |
| `alt_data_engine.py:507` | Earnings "nowcast" is a hardcoded-threshold heuristic presented as a predictive model; `estimated_surprise_pct = (nowcast-50)*0.15` fabricates a "surprise %" from the heuristic score itself, not any earnings model. Inputs are real; the "beat probability"/"surprise" framing is heuristic dressing. |
| `liquidity_monitor.py:381-471` | The 30-point history sparkline does **not** replay the real 6-indicator composite — it fabricates a 2-factor (VIX+volume) approximation (`approx_score = vix_s*0.55 + vol_s*0.45`) and `percentile_rank` derives from that stand-in. Current/live composite is real; the *history series* is an approximation shown as the composite. |
| `contagion_graph.py:204-289, 650-663` | When EDGAR 10-K text parse yields no mentions, the graph falls back to a hardcoded ~17-ticker `KNOWN_SUPPLY_CHAINS` map (labeled `filing_date="curated"`). Primary filing-text parse is genuine; symbols outside the curated set with an empty parse return no edges. |
| `synth_macrotranslate.py:446` | `surprise_pct` per-episode is permanently `None` ("historical surprises not modelled"); the `surprise_pct` API input is accepted and echoed but only buckets the cache key — it never conditions analog selection. Partially inert advertised input. |
| `research_backtest.py:36,278` | `adapter_ml_forecast` computes one forecast and reuses it across every bar → its backtest hit-rate is **not leak-free out-of-sample** (disclosed in-code). The momentum/mean-reversion adapters ARE leak-free walk-forward. |

### LOW — disclosed heuristics, label mismatches, and operational footguns (not fabricated user-facing data)
| Location | Finding |
|---|---|
| `aj_memory.py` | Council "memory" recall is metadata filtering over an append-only markdown log, **not embeddings/vector search**. Disclosed and intentional ("Chosen over a vector store… deliberately"); real working recall, just not semantic. |
| `synth_bayessmart.py:174-178` | `seed_synthetic_history()` fabricates pre-scored rows into `signal_forecasts` — but it's a **CLI/test-only** seed path; the user-facing `bayes_smart_money()` is fully real. **Footgun:** the seed writes to the default `AUGUR_DB_PATH` (`wealth.db`) with no test-DB guard, so running `python synth_bayessmart.py seed` against prod would pollute the live track-record/Bayes weights. |
| `smart_money.py:805` | UI label "AI-analyzed SEC filings" overstates: `_score_sec_sentiment` maps 8-K item codes via a static table (no AI/LLM). Score is real; label is cosmetic mismatch. |
| `synthetic_insider.py:6-8` | Docstring asserts an unverifiable backtest claim ("historically preceded major moves by 2-6 weeks") with no backtest in code. The signal itself composes REAL inputs (options flow, Form 4 clusters, congress, institutional, ml_forecast) — not fabrication. |
| `hn_sentiment.py:87-93` | `_score_text` is keyword bag-of-words polarity, disclosed in-docstring as "a vibe check not a real sentiment model." Mention counts are real. |
| `synth_cluster.py:65,80,244` | Hardcoded `SP500_TOP100` universe snapshot, hand-tuned `SOURCE_WEIGHTS`, and a disclosed yfinance "13F proxy" (`_component_13f_institutional`) rather than true 13F aggregation. All disclosed. |
| `synth_divmap.py:129` | Hardcoded `SP500_TOP100` universe snapshot (disclosed). |
| `synth_sectorflow.py:429,631` | Crude flat reddit baseline (25.0 posts, 0.04 weight) + heuristic `SECTOR_FACTOR_TILT` ("not regressed"). Low weight, disclosed. |
| `research_multihorizon.py:348` | `_bail()` returns neutral `prob_up:0.5, confidence:0.0` placeholder when a horizon's inputs are missing; excluded from consensus. Documented graceful-degradation sentinel. |
| `finviz_data.py:114` | `change_1w` field actually holds current-day change (self-documented mislabel); value is real. |
| `fetcher.py:2244-2301` | `_SCENARIOS` stress-test drawdown tables (2008/2020/2022/2000) are hardcoded modeling constants — transparent, documented. |
| `opportunity_scanner.py:657-663` | Crypto scorer hardcodes inapplicable dimensions (smart_money/fundamentals/insider/congress/options_flow) to neutral 50, dividend to 0 → crypto composite is effectively momentum + ml_forecast only. Design choice (signals don't exist for crypto). |
| `static/js/app.js:6422-6434` (+ `scanOptionsFlow`, `scanPortfolioOptions`, `loadCongressData`) | `_loadingStages(...)` renders a scripted time-driven progress checklist ("Training Random Forest classifier…") on a fixed `setInterval`, decoupled from the single real backend request. **Results are real; only the progress animation is theater.** |
| `aj_alpaca.py:126-129` | Alpaca adapter rejects crypto orders ("does not support crypto yet") rather than faking them — honest capability gap. |
| `ai_summarizer.py` `_rule_based_*` | Rule-based summaries set `"ai_powered": False` and prompt the user to configure a key — honest fallbacks, **not mocks**. |
| `jarvis_claude.py:432-438` | Inert UI-trace display fields (`reasoning.plan=[]`, `note=""`) never populated; substantive fields are real CLI-event data. |

---

## Explicitly verified as REAL (not mocks), despite suggestive names/docstrings
- **`ml_forecast.py`** — "no mock data" docstring **VERIFIED TRUE**: trains real sklearn RF (with purged/embargoed out-of-sample holdout), OLS trend, KMeans regime, OU mean-reversion on the stock's own fetched history.
- **`forecast_ensemble.py`** — real weighted signal fusion + calibration; no fabrication.
- **`jarvis_semmem.py`** — real OpenAI `text-embedding-3-small` + numpy cosine recall (genuine semantic memory).
- **`jarvis_research.py`** — real web_search (OpenAI Responses API) + GDELT + EDGAR + HN Algolia.
- **`jarvis_counterfactual.py`** — real replay of actual SELL rows vs live prices.
- **`jarvis_delivery.py`** — real SMTP/STARTTLS + osascript + file drop.
- **`gex_engine.py`** — real Black-Scholes gamma exposure from live yfinance option chains.
- **`aj_options.py`** — real yfinance option chains + mid/last premium pricing + liquidity filter.
- **`aj_universe.py`** — real full-SEC-universe screener with batch quotes + persistent cache ranking.
- **`smart_money.py` / `synthetic_insider.py`** — synthesize from REAL inputs (EDGAR Form 4, option chains, congress, ml_forecast); "synthetic" = composed, not fabricated.
- **Data sources** sec_edgar, fred_data, congress, cftc_cot, crypto_exchanges, alt_signals, earnings, calendar_v2, reflexivity_detector, historical_analog, wiki_attention, wikidata_meta, narrative_engine — all genuine live fetches.
- All 20 `research_*.js` / `synth_*.js` frontend panels fetch real backend data — no `Math.random()`, no canned arrays.

---

## Suggested priority if/when these are addressed (not done here)
1. **`aj_personas.fingpt_sentiment`** — either wire a real local FinBERT/FinGPT helper, or rename/remove the "FinGPT prior" so the feature name matches reality.
2. **`data_sources` CBOE put/call** — fix the stale module docstring (the feature was never there).
3. **`synth_bayessmart` seed footgun** — add a test-DB guard so `seed` can't write fabricated rows into prod `wealth.db`.
4. **`sec_filings_v2`** — pin `edgartools` in requirements, or remove the dormant module.
5. **`alt_data_engine` nowcast** & **`liquidity_monitor` history sparkline** — relabel the heuristic/approximation framing so the UI doesn't imply a model/true-composite.
6. **`aj_routing` quality_floor** — rename to reflect it's a length heuristic, or implement real grading.
7. Cosmetic dead code (`app.js _buildMLDetailPanel`, orphan banners, dup JS branch, empty `_RETIRED_SETTING_KEYS`).
