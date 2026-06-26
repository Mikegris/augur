# Merge Plan: TradingAgents → AJTA (AUGUR-Jarvis Trading Agent)

**Goal:** Fold the best capabilities of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
(Apache-2.0) — and selected ideas from comparable credible repos — into AJTA, **without weakening AJTA's
fail-closed, paper-first, auditable safety model.** This plan does not simplify the merge: it specifies every
TradingAgents subsystem, where it lands in AJTA, the data wiring, the safety contract, new modules, DB schema,
config, tests, cost controls, and a phased roadmap.

> Status note: this plan was produced alongside the v3.3.0 release (450-fix defect audit shipped; DMG re-signed).
> It is a design document — no code here changes runtime behavior until the phases below are implemented.

---

## 0. Reference systems (verified)

**AJTA today** (`aj_*.py` + `/api/aj/*` + Jarvis): triggered, non-24/7 cycle
`scan → forecast → judge → size → 11-step risk gate → execute → reconcile → analytics → autonomy`.
Strengths: deterministic **fail-closed 11-gate risk system** (`aj_risk.evaluate`), **paper-first** book
(`aj_positions.paper_book`, ADR-001), **immutable hash-chained audit** (`aj_db`), **crash recovery / reconcile-before-propose**
(`aj_execution.reconcile`), **100x sizing/exit/entry-alpha** layer (`aj_alpha`, `aj_rules`, `aj_strategy`),
**autonomy/escalation** (`aj_autonomy`), **privacy-gated local/cloud model routing** (`aj_routing`),
**Langfuse tracing** (`aj_langfuse`), **encrypted leased secrets** (`aj_secrets`), **analytics** (`aj_analytics`).
Gaps (from the capability audit): **no multi-agent debate**, **no fundamental/news/sentiment analyst pipeline**,
**single forecast source** (`forecast_ensemble`), **no qualitative multi-perspective risk reasoning**,
**no alpha-aware reflection memory loop**.

**TradingAgents** (Apache-2.0, ~89k★, active): LangGraph firm-simulation —
`Analysts (fundamentals/news/sentiment/technical) → Bull⇄Bear researcher debate → Research Manager (stance) →
Trader → Aggressive⇄Conservative⇄Neutral risk debate → Portfolio Manager (final arbiter)`.
Key patterns: **two-tier LLM split** (deep-think vs quick-think), **typed Pydantic schemas as the inter-agent protocol**
(`agents/schemas.py`), **hard round-count debate termination** (`graph/conditional_logic.py`),
**alpha-aware reflection** writing terse benchmark-relative lessons (`graph/reflection.py`),
**append-only markdown memory log** (current branch) vs ChromaDB vector memory (paper).
Caveats: **expensive** (~11 LLM + 20+ tool calls / ticker-day), **narrow/favorable eval** (3 mo, 5 mega-cap tech;
implausible SR 5–8), **decision-only** (no enforced sizing/slippage/portfolio constraints), **single-ticker**.

**Comparable repos (for targeted lifts):**
| Repo | License | Lift |
|---|---|---|
| virattt/ai-hedge-fund | MIT | **Investor-persona agents** (Buffett/Burry/Wood…) as composable signal generators + PM/risk aggregation |
| AI4Finance/FinGPT | MIT | Cheap **financial sentiment** scoring via HF models (news/social factor) |
| microsoft/qlib | MIT | **Backtest/risk-analysis discipline**, point-in-time data hygiene, benchmarked model zoo |
| AI4Finance/FinRL | MIT | **train→backtest→paper-trade gating** discipline; Alpaca paper patterns |
| AI4Finance/FinRobot | Apache-2.0 | Equity-research **report generation** templates |
| OpenBB | **AGPLv3 ⚠️** | Inspiration only (unified data abstraction) — **do NOT link**; reimplement the pattern |

---

## 1. Non-negotiable invariants (the merge must preserve all of these)

1. **The 11-step risk gate (`aj_risk.evaluate`) remains the sole authority over whether an order executes.**
   The multi-agent layer is **advisory**: it can *propose, strengthen, or veto/reduce* a signal, but it can **never
   create an order the gate would block, nor bypass any gate.**
2. **Fail-closed.** With the new layer off (default) or on error, AJTA behaves exactly as today (rule-based decision).
   The council is opt-in, behind a config flag **and** a one-time `VERIFY-COUNCIL` gate (mirrors VERIFY-ALPACA/MCP).
3. **Paper-first, live-never-auto.** No change to human-in-the-loop for live orders.
4. **Auditability.** Every analyst report, debate turn, manager stance, and final recommendation is persisted and
   **hash-chained into the existing audit log** (`aj_db` append-only). Reproducible given temperature=0 + frozen data snapshot.
5. **Privacy routing intact.** All council LLM calls go through `aj_routing`; holdings/PII-bearing prompts honor the
   PRIVATE→local-only rule. No raw portfolio egress to cloud unless the existing routing policy already allows it.
6. **No heavy/bundle-hostile deps.** **Do not add LangGraph or ChromaDB.** The desktop build (py2app 0.9 0.27/Python 3.9)
   is dependency-sensitive (see `docs`/memory on flask-compress/backports). The debate graph is reimplemented in compact
   pure-Python; memory is an append-only log (aligns with our audit philosophy), not a vector DB.
7. **Cost-bounded.** Per-cycle hard caps on LLM calls/tokens/spend; council runs only for a bounded top-K candidate set;
   results cached per `(symbol, ET-date, data-hash)`. Cost tracked in `aj_metrics` + Langfuse.
8. **Test parity.** Every new module ships with standalone-runner tests (no network, mocked LLM) matching
   `test_aj_*.py` conventions; the existing aj-suite must stay green.

---

## 2. Strategic insight that shapes the whole merge

TradingAgents' **data plumbing is weaker than AJTA's**. AJTA already owns rich, cached, rate-limited data engines:
`sec_edgar` (filings, 13F, insider Form 4), `congress` (PTR trades), `earnings`, `narrative_engine`,
`alt_data_engine`/`alt_signals`, `smart_money`, `gex_engine`, `liquidity_monitor`, `reflexivity_detector`,
`fred_data`, `finviz_data`, `hn_sentiment`, `contagion_graph`, plus `fetcher` (prices/indicators) and
`forecast_ensemble`/`ml_forecast`.

**Therefore we borrow TradingAgents' reasoning architecture, not its `dataflows/`.** Each AJTA analyst agent is a thin
adapter that turns our existing engines into an analyst's evidence bundle. This is the difference between a shallow
"port the repo" and a real capability uplift: we get firm-style multi-agent reasoning **on top of a data layer that is
already broader than the source repo's.**

---

## 3. Capability gap → merge mapping

| AJTA gap | Source pattern | Lands in AJTA as | Fed by (existing AJTA data) |
|---|---|---|---|
| No fundamental analyst | TA `fundamentals_analyst` | `aj_analysts.fundamentals()` | `sec_edgar`, `earnings`, `fetcher` fundamentals, `smart_money` |
| No news analyst | TA `news_analyst` | `aj_analysts.news()` | `narrative_engine`, `fetcher` news, `fred_data` (macro) |
| No sentiment analyst | TA `sentiment/social_analyst` + FinGPT | `aj_analysts.sentiment()` | `hn_sentiment`, `alt_signals`, `finviz_data`, (opt) FinGPT HF |
| Thin technical reasoning | TA `market_analyst` | `aj_analysts.technical()` | `fetcher` indicators, `forecast_ensemble`, `gex_engine`, `liquidity_monitor` |
| Single reasoning path | TA Bull⇄Bear debate | `aj_debate.research_debate()` | analyst reports + memory |
| No stance synthesis | TA Research Manager | `aj_debate.research_manager()` → `ResearchPlan` | debate transcript |
| No qualitative risk reasoning | TA 3-way risk debate | `aj_debate.risk_debate()` (advisory **pre-gate**) | trader proposal + portfolio state |
| No final arbiter w/ lessons | TA Portfolio Manager | `aj_council.arbiter()` → `CouncilDecision` | risk debate + reflection memory |
| No alpha-aware memory | TA `reflection.py` | `aj_memory` (append-only log) + `research_tracker` hook | realized vs benchmark returns |
| Single model tier | TA deep/quick split | `aj_routing` tiers (`deep`/`quick`) | — |
| Investor personas (optional) | ai-hedge-fund | `aj_personas.py` (opt-in analysts) | same data adapters |

---

## 4. Target architecture — the "Analyst Council" advisory layer

### 4.1 Where it slots into `aj_operator.run_once`

The council is inserted **between forecast and the existing `_judge`/risk gate**, as an advisory enrichment for a
**bounded candidate set** (post-ensemble pre-filter, top-K by edge):

```
scan → forecast ──► [NEW] Analyst Council (opt-in, top-K only) ──► judge ──► size ──► RISK GATE ──► execute → reconcile → analytics → autonomy
                          │                                          ▲
                          └── CouncilDecision (rating, conviction,   │
                              thesis, proposed stop/target, dissent) ┘  (advisory input only)
```

**Safety contract (the heart of the merge):**
- The council emits a `CouncilDecision`. It enters `_judge` as an **additional signal**, combined under a configurable
  policy:
  - `advisory` (default): council may **veto** an ensemble buy or **reduce** conviction; it may **not** create a buy.
  - `confirm` : an entry requires **both** ensemble signal **and** council agreement (council as a red-team gate).
  - `coequal` : council rating is blended with ensemble edge into conviction (still subject to the full risk gate).
- Whatever `_judge` emits still flows through the **unchanged** `aj_risk.evaluate` 11-gate. The council can only make
  the system **more conservative or equally conservative**, never less — except in `coequal` mode where it can raise
  conviction, which only affects **sizing within existing caps**, never gate bypass.
- The TA **risk debate** maps to an advisory step feeding the **conviction/size** input to the gate; the quantitative
  gates (daily-loss halt, notional cap, trades/day, IPS, max-positions, VIX, slippage, momentum/RS/corr/pyramid blocks)
  remain authoritative and run after.

### 4.2 The council pipeline (compact pure-Python orchestrator, no LangGraph)

`aj_council.run(symbol, ctx) -> CouncilDecision`:
1. **Analyst fan-out** (parallel, bounded): `fundamentals, news, sentiment, technical` → typed `AnalystReport`s.
   Each uses `aj_routing` **quick-think** model for tool/data summarization, **deep-think** for the actual analysis.
2. **Research debate**: `Bull` and `Bear` each consume all reports + retrieved memory; alternate for
   `max_research_rounds` (hard-terminate by turn count). → debate transcript.
3. **Research Manager**: picks a definitive stance → `ResearchPlan{recommendation(5-tier), rationale, strategic_actions}`.
4. **Trader**: `ResearchPlan` → `TraderProposal{action(3-tier), reasoning, entry, stop_loss, position_hint}`.
5. **Risk debate**: `Aggressive/Conservative/Neutral` debate the proposal for `max_risk_rounds` (hard-terminate). → transcript.
6. **Arbiter (Portfolio Manager analog)**: synthesizes risk debate + plan + proposal + **reflection memory** →
   `CouncilDecision{rating(5-tier), conviction(0..1), thesis, price_target, time_horizon, stop_hint, dissent_notes}`.
7. **Persist + audit + trace**: write all artifacts to new tables, hash-chain into audit, emit Langfuse span, record cost.

All inter-agent messages are **typed Pydantic schemas** (pydantic already vendored via `openai`), each with a
`render()` to markdown for logging — directly adopting TA's "structured communication" answer to NL information loss.

---

## 5. Component-by-component merge specification

### 5.1 Analysts (`aj_analysts.py`)
- One function per analyst returning an `AnalystReport` schema (`band`, `score 0..10`, `confidence`, `key_points[]`,
  `evidence_refs[]`, `narrative`). Each is a **thin adapter** over existing engines (see §3 table). No new data deps.
- **Fundamentals**: pull `sec_edgar.xbrl_key_metrics`, `earnings.get_earnings_dossier`, `fetcher` fundamentals,
  `smart_money`/insider; deep-think model scores valuation/quality/insider posture.
- **News**: `narrative_engine.analyze_narrative` (phase), `fetcher` headlines, `fred_data` macro; model summarizes
  catalysts and macro regime.
- **Sentiment**: `hn_sentiment`, `alt_signals` (reddit/stocktwits), `finviz_data`; optional **FinGPT** HF sentiment as
  a numeric prior (MIT; opt-in, offline-capable). Emits `SentimentReport` (6-tier band like TA).
- **Technical**: `fetcher.compute_indicators`, `forecast_ensemble.ensemble_forecast`, `gex_engine`,
  `liquidity_monitor`, `reflexivity_detector`; model interprets indicator confluence + ensemble edge.

### 5.2 Debate engine (`aj_debate.py`)
- `research_debate(reports, memory, rounds)` — Bull/Bear alternation, hard turn-count termination (TA
  `conditional_logic` semantics), each turn instructed to rebut the opponent's last point. Returns transcript + per-side
  histories.
- `research_manager(transcript, ctx)` → `ResearchPlan`. Forced to pick a side (no lazy Hold), TA-style.
- `risk_debate(proposal, portfolio, rounds)` — Aggressive/Conservative/Neutral rotation, hard-terminate. **Advisory**.
- All debates obey a **per-cycle call budget**; degrade gracefully (fewer rounds) under budget pressure; on any error,
  return `None` → council yields no signal → cycle proceeds rule-based (fail-closed).

### 5.3 Schemas (`aj_schemas.py`)
- Pydantic models adapted from TA `schemas.py`: `AnalystReport`, `SentimentReport`, `ResearchPlan`, `TraderProposal`,
  `RiskStance`, `CouncilDecision`, plus enums `Rating` (5-tier), `Action` (3-tier), `SentimentBand` (6-tier).
- Each has `render()` (markdown) and `to_audit()` (canonical JSON for the hash chain). Reuse TA's deliberate
  **granularity-narrowing** (5-tier manager → 3-tier trader action) as a forcing function.

### 5.4 Memory / reflection (`aj_memory.py` + hook in `research_tracker`/`aj_autonomy`)
- **Append-only markdown log** (TA current-branch design — NOT ChromaDB) at the AUGUR data dir, atomic temp-replace
  writes, `<!-- ENTRY_END -->` delimiters. Retrieval = metadata filter (up to 5 same-symbol + 3 cross-symbol lessons,
  reverse-chronological). This aligns with AJTA's audit/append-only ethos and adds zero heavy deps.
- **Reflection**: when `research_tracker.score_due_forecasts` resolves a scored decision, compute **alpha vs benchmark**
  (SPY) and write a terse 2–4 sentence lesson keyed to `(symbol, situation-hash)` — TA's `reflect_on_final_decision`
  pattern. Lessons are injected into future council prompts (Bull/Bear/Trader/Arbiter), cheaply improving calibration.
- Optional future upgrade path: semantic recall via a local embedding model behind a flag — explicitly deferred to keep
  the bundle lean.

### 5.5 Model routing (`aj_routing` extension)
- Add a **two-tier abstraction**: `aj_routing.complete(..., tier="deep"|"quick")`. Map tiers to configured models per
  provider (local Ollama / cloud), honoring the existing PRIVATE→local rule and quality floor. Deep-think for analysis &
  arbiter; quick-think for summarization/tool digestion. This is TA's single biggest cost lever and fits our router.

### 5.6 Persistence & audit (`aj_db` migration)
- New append-only tables (forward-only migration, idempotent DDL like existing `aj_*`):
  `aj_council_runs`, `aj_analyst_reports`, `aj_debate_turns`, `aj_council_decisions`, `aj_reflections`.
- Every row's canonical JSON folds into the **existing audit hash chain**; `verify_audit_chain` extended to cover them.

### 5.7 Config (`aj_config`)
- New keys (all default OFF / conservative): `council_enabled`, `council_policy` (`advisory|confirm|coequal`),
  `council_topk`, `max_research_rounds`, `max_risk_rounds`, `council_models` (deep/quick per provider),
  `council_max_calls_per_cycle`, `council_max_spend_usd_per_cycle`, `council_cache_ttl`, `personas_enabled`,
  `fingpt_sentiment_enabled`. Add to the friendly Config tab + presets (conservative/moderate/aggressive get
  progressively more rounds/top-K).

### 5.8 Routes & UI (`app.py` `/api/aj/*`, `static/js/app.js`, `templates/index.html`)
- `GET /api/aj/council/<symbol>` — run/inspect a council decision (read; respects gates).
- `GET /api/aj/council/last` — last cycle's council artifacts (reports, debate transcript, decision) for the UI.
- New "AGENT COUNCIL" panel: per-symbol analyst cards, bull/bear transcript, risk debate, final decision + dissent,
  cost/latency. Mirrors existing AJ panels.
- Jarvis: a "why did the agent (not) trade X" intent that surfaces the council thesis + dissent.

### 5.9 VERIFY gate (`aj_secrets`/`aj_config` gate pattern)
- `VERIFY-COUNCIL` one-time operator acknowledgement (cost + non-determinism disclosure) before the council can run,
  mirroring `VERIFY-ALPACA`/`VERIFY-MCP-READ`.

### 5.10 Observability (`aj_metrics`, `aj_langfuse`)
- Per-cycle council metrics: calls, tokens, spend, latency, rounds used, veto/confirm counts, agreement-with-ensemble
  rate, downstream gate outcome. Langfuse span per council run with nested analyst/debate children.

---

## 6. Targeted lifts from the other repos (sequenced after the core)

- **ai-hedge-fund (MIT) — investor personas:** `aj_personas.py` adds optional persona analysts (value/quality/contrarian/
  momentum archetypes) that plug into the **same analyst interface**. Attribution in NOTICE. Opt-in via `personas_enabled`.
- **FinGPT (MIT) — sentiment:** optional numeric sentiment prior for the sentiment analyst via HF models; offline-capable;
  behind `fingpt_sentiment_enabled` (keeps default bundle lean).
- **qlib / FinRL (MIT) — backtest discipline:** extend AJTA's `backtest-lite` into a **council backtest harness** that
  replays the council on historical dates (point-in-time data hygiene from qlib's discipline) to measure alpha vs SPY
  before any live use — operationalizing TA's reflection metric as a gate ("council must beat benchmark in backtest
  before `coequal` mode is allowed").
- **FinRobot (Apache-2.0) — report gen:** optional richer end-of-day equity-research report templates for the reflection/
  briefing output.
- **OpenBB (AGPLv3):** **inspiration only** for a unified data-vendor abstraction; do not link. Our `aj_dataflows`
  adapter interface (§5.1) already provides the swap-vendor-without-touching-agents benefit, MIT/Apache-clean.

---

## 7. Cost, latency, determinism, safety controls

- **Bounded fan-out:** council runs only for `council_topk` post-ensemble candidates; analysts run in parallel under the
  existing `safe_executor`. Hard per-cycle caps on calls/tokens/spend; on cap hit → degrade rounds → skip council
  (fail-closed to rule-based).
- **Caching:** memoize `(symbol, ET-date, data-snapshot-hash)` → `CouncilDecision` to avoid repaying within a day/cycle.
- **Determinism:** temperature=0 (or provider reasoning-effort), frozen per-cycle data snapshot, recorded model ids;
  decisions reproducible and audit-verifiable.
- **Privacy:** council prompts routed via `aj_routing`; PRIVATE content (holdings, account) stays local-only per existing
  policy; cloud calls carry only the minimum public market context.
- **Kill switch / health:** `aj_autonomy.health_autohalt` extended with council signals (e.g., runaway cost, repeated
  schema-parse failures) → can disable the council without touching core trading.

---

## 8. Phased roadmap (each phase shippable, fail-closed, tested, VERIFY-gated)

**Phase 0 — Scaffolding & schemas (no behavior change).**
`aj_schemas.py`, `aj_routing` deep/quick tiers, `aj_db` migration for council tables, config keys (all OFF),
`VERIFY-COUNCIL` gate, Langfuse/metrics plumbing. Tests: schema round-trip, migration idempotency, routing tier selection.

**Phase 1 — Analyst council (read-only, advisory-off).**
`aj_analysts.py` (4 analysts as adapters over existing engines), `aj_council.run` producing a `CouncilDecision` that is
**logged only** (does not affect `_judge`). New `/api/aj/council/<symbol>` + UI panel. Tests: each analyst with mocked
engines+LLM; council assembles a decision deterministically; cost caps honored.

**Phase 2 — Research debate + manager.**
`aj_debate.research_debate` + `research_manager`, memory log (`aj_memory`) wired read-side into prompts. Still
log-only. Tests: hard-termination by round count; manager forced-stance; transcript persisted + audited.

**Phase 3 — Wire council into `_judge` as advisory (default `advisory`).**
Council can now **veto/reduce** (never create). Behind `council_enabled` + `VERIFY-COUNCIL`. Risk gate unchanged and
still authoritative. Tests: council veto blocks an ensemble buy; council never enables a gate-blocked order;
fail-closed when council errors/over-budget.

**Phase 4 — Risk debate + arbiter + reflection loop.**
`risk_debate`, `aj_council.arbiter`, reflection on resolved forecasts writing alpha-aware lessons via `research_tracker`
hook. Adds `confirm` policy. Tests: reflection writes benchmark-relative lesson; lessons retrieved into next prompt;
arbiter dissent recorded.

**Phase 5 — Backtest gate + `coequal` policy.**
Council backtest harness (qlib/FinRL discipline); `coequal` only unlockable after a backtest shows council alpha ≥ 0 vs
SPY over the configured window. Tests: backtest replay deterministic on cached data; `coequal` gated on backtest result.

**Phase 6 — Optional enrichments.**
ai-hedge-fund personas (`aj_personas`), FinGPT sentiment prior, FinRobot report templates — each opt-in, attributed.

Each phase: standalone-runner tests (no network, mocked LLM), aj-suite stays green, all-module import + `node --check`,
and a friendly Config-tab surface for the new flags.

---

## 9. Testing & verification strategy

- Mirror `test_aj_*.py`: standalone `__main__` runners, fresh temp DB, **no network, LLM mocked** (inject a fake
  `aj_routing.complete`). New suites: `test_aj_schemas.py`, `test_aj_analysts.py`, `test_aj_council.py`,
  `test_aj_debate.py`, `test_aj_memory.py`, `test_aj_council_gate.py` (proves the advisory contract: never enables a
  gate-blocked order), `test_aj_council_cost.py` (budget caps), and an audit-chain extension test.
- Regression gate: the existing deterministic suite (aj 175, test_defects 299, e2e, phase4-7) must stay green; add
  council suites to the sweep. Network-flaky suites unaffected.
- Determinism test: same inputs + mocked model → identical `CouncilDecision` + identical audit hash.

---

## 10. Licensing & attribution

- TradingAgents is **Apache-2.0** → merge-compatible. Any adapted code (schemas, debate control-flow, reflection prompt
  shape) must: preserve a copy of the Apache-2.0 license, add a `NOTICE` crediting Tauric Research, and **state changes**
  in headers of derived files (e.g., `# Adapted from TauricResearch/TradingAgents (Apache-2.0); see NOTICE`).
- ai-hedge-fund, FinGPT, FinRL, qlib are **MIT**; FinRobot **Apache-2.0** → all attribution-only. **OpenBB is AGPLv3 →
  do not vendor/link**; only reimplement the abstraction idea.
- Add `docs/THIRD_PARTY_NOTICES.md` enumerating each borrowed pattern, source repo, license, and what was changed.

---

## 11. What we deliberately do NOT take (and why)

- **LangGraph** — heavy dep, py2app-bundle risk; reimplement the small deterministic graph in pure Python.
- **ChromaDB vector memory** — heavy + known bug (#113); use the append-only markdown log (auditable, lean).
- **TradingAgents `dataflows/`** — our data engines are broader; adapt ours instead.
- **TA performance numbers / eval framing** — narrow, favorable window; do not treat as evidence of edge. We re-validate
  via our own backtest gate before any `coequal`/live influence.
- **Any path that lets the model bypass the risk gate or auto-execute live** — violates AJTA invariants.

---

## 12. Open decisions for the user (gate the build)

1. **Default integration policy** once enabled: `advisory` (recommended, safest) vs `confirm` vs `coequal`?
2. **Model spend posture:** local-Ollama-only (cheapest, private, lower quality) vs cloud deep-think (cost) vs hybrid?
   Sets the default `council_models` + per-cycle spend cap.
3. **Scope of first delivery:** Phases 0–3 (council advisory, the high-value core) as the initial milestone, or push
   through Phase 4 (reflection memory) in the first build?
4. **Personas & FinGPT (Phase 6):** in-scope now or deferred?
5. **Universe/cost:** target `council_topk` and `max_research_rounds` for the default preset (drives cost per cycle).

---

### Appendix A — New/changed files at a glance
New: `aj_schemas.py`, `aj_analysts.py`, `aj_debate.py`, `aj_council.py`, `aj_memory.py`, `aj_personas.py` (P6),
`docs/THIRD_PARTY_NOTICES.md`, `NOTICE`, tests `test_aj_{schemas,analysts,council,debate,memory,council_gate,council_cost}.py`.
Changed: `aj_routing.py` (tiers), `aj_config.py` (+keys+presets), `aj_db.py` (+tables+audit), `aj_operator.py` (council
hook in `run_once`), `aj_risk.py` (consume council conviction — **gate logic unchanged**), `aj_autonomy.py`
(health signals + reflection hook), `research_tracker.py` (reflection on resolve), `aj_metrics.py`/`aj_langfuse.py`
(observability), `app.py` (+routes), `static/js/app.js` + `templates/index.html` (council panel), `requirements*.txt`
(only if FinGPT opt-in adds an extra, kept out of the default bundle).
