# Third-Party Notices

The AUGUR Analyst Council layer (`aj_schemas.py`, `aj_analysts.py`, `aj_debate.py`,
`aj_council.py`, `aj_memory.py`, `aj_personas.py`) adapts ideas and patterns from
the open-source projects below. No source code was copied verbatim; the
implementations here are independent reimplementations in this codebase's style.
Each upstream project's license is permissive and compatible with this use;
attribution is provided per their terms.

| Project | License | What we adapted (patterns, not code) |
|---|---|---|
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | Apache-2.0 | The firm-simulation architecture: analyst team → bull/bear research debate → research manager → trader → 3-way risk debate → portfolio-manager arbiter; typed inter-agent schemas; hard round-count debate termination; deep/quick LLM tiering; alpha-aware reflection memory. We did NOT vendor its `dataflows/`, LangGraph orchestration, or ChromaDB memory. |
| [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | MIT | The investor-persona pattern — personas as composable analysts feeding a manager/risk aggregation layer (`aj_personas.py`). |
| [AI4Finance-Foundation/FinGPT](https://github.com/AI4Finance-Foundation/FinGPT) | MIT | Optional numeric financial-sentiment prior (`aj_personas.fingpt_sentiment`, lazy/opt-in). |
| [AI4Finance-Foundation/FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | Apache-2.0 | Equity-research report-generation pattern (`aj_personas.council_report`). |
| [microsoft/qlib](https://github.com/microsoft/qlib), [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL) | MIT | The "validate before sizing up" discipline — the coequal track-record gate (`aj_council.coequal_unlocked`). |

OpenBB (AGPLv3) was reviewed for its unified data-vendor abstraction idea but is
NOT used or linked, to avoid copyleft obligations.

TradingAgents is Apache-2.0; per its terms a copy of the Apache-2.0 license text
is available at https://www.apache.org/licenses/LICENSE-2.0 and changes are noted
in the adapted source file headers.
