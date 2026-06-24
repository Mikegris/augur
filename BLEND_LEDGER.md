# BLEND_LEDGER — dependency & license audit (AJTA-SPEC-1.0 §24)

Every networked/bundled dependency the AUGUR–Jarvis Trading Agent relies on,
with its license and obligation. **No component may be adopted with an
unresolved license** (§24, normative). AGPL components, if ever introduced,
MUST be assessed for network-copyleft reach before inclusion.

## Core (in the always-on, paper-first system)

| Component | Version (pin) | License | Obligation / status |
|---|---|---|---|
| AUGUR (this repo) | local | **MIT** | permissive; the base. |
| Python stdlib (sqlite3, zoneinfo, hashlib, fcntl, decimal) | 3.9 | PSF | permissive. |
| Flask | per `requirements.txt` | BSD-3 | permissive. |
| scikit-learn / numpy / pandas / scipy | per lock | BSD-3 | permissive. |
| yfinance | per lock | Apache-2.0 | permissive; market reads only. |

The **PaperBroker** and the entire local-only profile (§22.2) introduce **no
new third-party dependency** — they are pure Python over the existing stack.
The local-only profile therefore has **no unresolved licenses**.

## Optional overlays (VERIFY-gated, OFF by default — not yet enabled)

| Component | License | Obligation | Enable gate |
|---|---|---|---|
| Ollama + model weights | per-model | **each model's weights carry their own license** — verify automated/commercial terms per model before use | model routing private path (optional) |
| LiteLLM (model gateway) | verify (commonly MIT) | confirm before bundling a gateway service | model gateway overlay |
| Alpaca SDK | verify | broker ToS + SDK license | VERIFY-ALPACA + `live_trading_enabled` |
| ccxt | MIT (verify) | library use | VERIFY-CCXT + `live_trading_enabled` |
| Robinhood Agentic Trading | **service ToS (not a license)** | ToS governs automated access | VERIFY-ROBINHOOD + `robinhood_enabled` |
| opencode | verify | runs as separate process (no linking) | VERIFY-OPENCODE |
| Langfuse | verify (OSS tier) | self-host terms | observability overlay |
| `mcp` package (read server transport) | verify | only imported when `serve_stdio()` is called | MCP transport |

**Status:** every optional overlay is implemented as a fail-closed, switch-off
stub/adapter. None is active in the shipped configuration, so none introduces a
runtime license obligation today. Before flipping any overlay's switch, record
its confirmed license/ToS here and pass the matching VERIFY gate (§27).
