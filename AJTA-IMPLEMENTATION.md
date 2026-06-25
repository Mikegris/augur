# AJTA Implementation Notes (AJTA-SPEC-1.0 → v3.0.0)

What was built, how it maps to the spec, the VERIFY gates that remain open, and
how to operate it. **Paper-by-default, fail-closed, human-in-the-loop for any
live order.** Not investment, tax, or legal advice.

## Modules

| Module | Spec sections | Role |
|---|---|---|
| `aj_db.py` | §5–8, §19.2, §22.4 | schema + forward-only `aj_migrate()`, `money()`, market sessions, single-instance lock, cycle bookkeeping, append-only **hash-chained** audit, WAL-safe backup |
| `aj_config.py` | §11.1, App. D | trade-control config in `settings`, **all defaults fail-closed** |
| `aj_positions.py` | §4 (ADR-001) | FIFO **paper book** derived from `aj_fills` (real portfolio never touched) |
| `aj_risk.py` | §11 | ordered fail-closed gate, day-P&L (FIFO + marks), kill switch, halt/re-arm |
| `aj_broker.py` | §12.5, §17 | `BrokerClient` ABC, `PaperBroker` (slippage+fees), VERIFY-gated live stubs |
| `aj_execution.py` | §12, §13, §20.1 | order state machine (incl `unknown`), fills, reconciliation, crash recovery |
| `aj_routing.py` | §10 | ModelRouter: private→local-only (no egress), telemetry, bounded escalation |
| `aj_operator.py` | §19 | `run_once()` cycle: scan→forecast→judge→red-team→size→propose→gate→execute→reconcile→score |
| `aj_metrics.py` | §21.2 | observability metrics + alert thresholds |
| `aj_mcp_read.py` | §15 | **frozen** read-tool contract (VERIFY-MCP-READ) |
| `aj_secrets.py` | §22.3 | secrets broker — Fernet-encrypted, lease-based, never plaintext / never in model context |
| `aj_alpaca.py` | §26.5, VERIFY-ALPACA | real Alpaca REST adapter (paper + live), keys leased from `aj_secrets`, fail-closed |
| `aj_eval.py` | §23.5, §8, §23.3 | routing eval/leaderboard + demotion advice, `aj_routing` retention prune, backtest caveat |
| `aj_cli.py` | §19, §11.4 | `run / status / kill / rearm / recon / config / verify / secret / verify-pass` |
| app.js `trading` view | §11.4, §19, §21 | **AJ dashboard UI**: status, day/cum P&L, KILL button, RUN trigger, config editor, proposals/orders, alerts |
| app.py `/api/aj/*` | — | read routes + local control plane (kill/rearm/run/config/approve) |
| `aj_opencode.py` | §16, VERIFY-OPENCODE | sandboxed candidate-signal/backtest runner — research only, never execution, fail-closed + forbidden-path guard |
| `aj_voice.py` | §18 | spoken-command gateway — reads run, high-risk needs approval (never auto-executes) |
| `aj_langfuse.py` | §21.1 | trace exporter — fail-open no-op unless LANGFUSE_* configured |
| `deploy/` | §22.1 | docker-compose topology + Dockerfile (local-only profile) |

### Open-universe mode (off-allowlist trading)
`allow_any_symbol` (config, default **false**) lets the agent choose ANY quotable, valid ticker instead of only the allowlist. It bypasses ONLY the allowlist rail (§11.3 step 3) — every other cap (per-order notional, trades/day, daily-loss HALT, IPS, quotability) still binds, and it's paper-first / human-approved for live. The operator then scans a bounded universe (allowlist ∪ watchlist ∪ equity holdings ∪ idea pool, capped at `scan_universe_max`). Toggle in the Trading tab.

## ADR-001 — paper book vs real portfolio
The spec models a Position as the AUGUR `portfolio` row. AUGUR is a **live
personal portfolio tracker**, so applying *paper* fills to `portfolio` would
corrupt real holdings. The agent instead keeps a self-contained FIFO **paper
book** from `aj_fills`; day-P&L basis (§11.2) comes from it. A future live venue
MAY reflect into the real portfolio behind its own switch. This is the only
deliberate deviation from the spec's literal wording, made on safety grounds.

## Acceptance phases (§26) status
1. **Local read-only** — ✅ MCP read tools, ModelRouter, status/forecast; no execution unless switched on.
2. **Paper execution** — ✅ `PaperBroker` + `execute_trade` + risk gate; slippage/fees modeled; reconcile clean; kill/halt proven (unit + contract tests).
3. **Autonomous (triggered) paper** — ✅ `run_once` under a scheduler with single-instance lock + crash recovery; accountability scoring wired; injected-crash test passes with **no double-submit**.
4. **Honest evaluation** — gate defined (§23.4 below); requires a real paper period before any live.
5. **(Optional) live** — adapters are fail-closed stubs; NOT enabled. Requires §27 VERIFY + the three switches + per-order human approval.

## §27 — open VERIFY gates (MUST close before the guarded feature is enabled)
Tracked in `settings` as `aj_verify_<gate>` (value `pass` to clear). `aj_cli.py verify` shows status.
- **VERIFY-MCP-READ** — ✅ self-verifiable offline; `aj_mcp_read.contract_ok()` is asserted by `test_aj_contract`. The frozen list is `aj_mcp_read.FROZEN_TOOLS`.
- **VERIFY-ALPACA / VERIFY-CCXT** — OPEN. Confirm SDK/license, order surface, auth; contract-test against the broker's paper sandbox before `live_trading_enabled`.
- **VERIFY-ROBINHOOD** — OPEN. Eligibility/fees, MCP order surface, token lifetime → secrets broker, reconciliation vs activity feed, disconnect-as-kill.
- **VERIFY-OPENCODE** — OPEN. Non-interactive write + output schema on the pinned version.
- **Licenses** — see `BLEND_LEDGER.md`; core profile clean, overlays recorded.

## §23.4 — paper-validation gate (precondition to ANY live)
Live trading MUST NOT be enabled until: a meaningful paper period has run; the
accountability loop shows **positive risk-adjusted realized (paper)** results
after modeled costs; reconciliation has been clean; and kill switch + halt +
disconnect have all been exercised. Even then live starts at minimal funded
size with **human approval per order**. The shipped config cannot reach live:
`trading_enabled`, `live_trading_enabled`, `robinhood_enabled` all default
false, `symbol_allowlist` empty, every cap 0.

## Operating it
```
# show status / config
python aj_cli.py status
python aj_cli.py config

# enable PAPER trading (still fail-closed until caps are set)
python aj_cli.py config --set trading_enabled=true \
    --set symbol_allowlist=NVDA,AAPL \
    --set max_order_notional_usd=1000 \
    --set max_trades_per_day=5 \
    --set max_daily_loss_usd=500

# one triggered cycle (paper). Schedule via cron/launchd N×/day in market hours.
python aj_cli.py run --mode paper

# emergency stop (model NOT in the loop)
python aj_cli.py kill "stop now"
python aj_cli.py rearm          # after reviewing a halt
```
Web control plane mirrors these at `POST /api/aj/{run,kill,rearm,recon,config}`
and `GET /api/aj/{status,proposals,orders,audit}`.

## Tests
- `test_aj_unit.py` (§23.1) — money, sessions, audit chain + tamper, lock,
  migrations, gate ordering + fail-closed, day-P&L FIFO/fees/marks, kill/halt/
  rearm, state machine + idempotency.
- `test_aj_contract.py` (§23.2) — VERIFY-MCP-READ frozen contract, PaperBroker
  slippage/fees/limit-resting, gated brokers fail-closed, BrokerClient
  conformance vs mock, reconcile, **crash recovery no-double-submit**, operator
  paper cycle, **live-never-auto**, routing privacy (no egress) + telemetry,
  kill switch.
