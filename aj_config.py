"""AJTA — trade-control configuration (AJTA-SPEC-1.0 §11.1, Appendix D).

The risk/trade switches and caps. EVERY default is the safest, blocking value
(fail-closed): trading off, live off, robinhood off, empty allowlist, zero
caps. A config that has never been touched can place NO order, paper or live.

Authoritative store is AUGUR `settings` (string KV), keyed `aj_*`; this module
owns typing/validation. A `risk.yaml` template ships in the repo as docs, but
settings is the source of truth (spec: "risk.yaml, persisted to settings").
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import database as db

log = logging.getLogger("augur.aj_config")

# ── safe defaults (§11.1) ─────────────────────────────────────────────────────
DEFAULTS: Dict[str, Any] = {
    # master + venue switches — ALL false
    "trading_enabled":        False,
    "live_trading_enabled":   False,
    "robinhood_enabled":      False,
    # caps — ALL zero = block (must be set > 0 to trade)
    "symbol_allowlist":       [],     # EMPTY = nothing tradable
    "allow_any_symbol":       False,  # OPT-IN open universe: trade off-allowlist
    "max_order_notional_usd": 0.0,
    "max_trades_per_day":     0,
    "max_daily_loss_usd":     0.0,
    # semantics
    "daily_loss_basis":       "realized_plus_unrealized",  # or "realized"
    "halt_rearm":             "manual",                    # or "session_open"
    "session_whitelist":      ["regular"],
    # execution defaults (paper-first)
    "default_broker":         "paper",
    "auto_approve_paper":     True,    # paper MAY auto-approve; live NEVER does
    # paper fill model (§12.5)
    "paper_slippage_bps":     2.0,
    "paper_spread_fraction":  0.5,
    # Paper partial-fill simulation: liquidity-bounded fills so partial-fill
    # handling and order TTL are exercised in paper exactly as live. Opt-in.
    "paper_partial_fills":    False,
    "paper_fill_liquidity_usd": 25000.0,  # nominal per-order liquidity budget
    # Cash account: the capital base the agent may trade WITH. 0 (default)
    # keeps the legacy unfunded book (no buying-power gate). When set, the
    # agent derives available cash from its fills ledger (base - open cost +
    # realized P&L), sizes entries within it, and the risk gate blocks any
    # buy beyond it (live mode uses the broker's settled cash instead).
    # Stored as aj_paper_cash — the same setting aj_alpha has always read.
    "paper_cash":             0.0,
    # Tax view (aj_tax): flat COMBINED (federal+state) marginal rates the tax
    # estimator applies to realized short/long-term gains. 0 (default) keeps the
    # estimator informational — it reports the ST/LT gain split but invents no
    # liability until the operator sets their own rates. Fractions, e.g. 0.35.
    "tax_short_term_rate":    0.0,
    "tax_long_term_rate":     0.0,
    # Mean-reversion signal adapter (aj_signals.meanrev_signal): buy quality
    # dips as a complementary orthogonal signal to momentum. Off by default
    # pending live-ensemble replay validation; once on, the IC scorecard governs
    # its ensemble weight (cold start neutral, chronic misses decay to silence).
    "meanrev_adapter_enabled": False,
    "fee_bps":                0.0,     # commission-free equities default
    "min_fee_usd":            0.0,
    "crypto_fee_bps":         10.0,    # exchange-typical for crypto venues
    # operator decision tunables (§19 scan→judge→size)
    "forecast_horizon_days":  20,
    "buy_prob_threshold":     0.55,    # prob_up at/above => buy candidate
    "sell_prob_threshold":    0.45,    # prob_up at/below => sell held name
    "min_edge_pct_pts":       3.0,     # |edge| floor; below => no trade
    "order_notional_target_usd": 0.0,  # 0 => half of max_order_notional_usd
    # Per-asset-class notional caps (opt-in, 0 = disabled). An option or crypto
    # position risks a far larger fraction of its notional than an equity — a
    # weekly call can go to zero, and crypto routinely swings 50%+ — so sizing
    # them to the SAME dollar notional as a stock over-risks the book. These cap
    # the per-order notional for that asset class BELOW the global cap. See the
    # v3.25 expectancy post-mortem (one -73.8% crypto lot + oversized weekly
    # calls single-handedly tripped the governor's negative-expectancy breaker).
    "max_crypto_notional_usd": 0.0,    # A: cap any single crypto buy's notional
    "max_option_notional_usd": 0.0,    # B: cap any single option order's premium
    "option_notional_target_usd": 0.0, # B: size options to this budget (0 => reuse the equity target)
    "use_llm_synthesis":      False,   # off => deterministic rule-based thesis
    "scan_universe_max":      25,      # cap symbols/cycle in open-universe mode
    # ── 25 enhancements (all opt-in; 0/false = disabled) ─────────────────────
    # sizing & entry
    "conviction_sizing":      False,   # scale order size by edge strength
    "min_order_notional_usd": 0.0,     # skip dust orders below this
    "entry_order_type":       "market",  # or "limit"
    "entry_limit_offset_bps": 0.0,     # limit placed this far through the quote
    "order_ttl_cycles":       0,       # expire resting limit orders after N cycles
    # exit rules
    "take_profit_pct":        0.0,     # auto-sell a paper long up this %
    "stop_loss_pct":          0.0,     # auto-sell a paper long down this %
    "trailing_stop_pct":      0.0,     # exit on drawdown from peak mark
    "exit_cooldown_min":      0,       # no re-entry of a symbol for N min after exit
    # extra gates
    "max_open_positions":     0,       # cap simultaneous open paper positions
    # Capital-rotation (opportunity-cost) engine (aj_rotation): when the book is
    # capital-constrained (at the position cap OR out of cash), sell the weakest
    # holding to fund a clearly-better fresh candidate instead of sitting idle.
    # Off by default; only fires when constrained, only displaces weak holdings,
    # and demands a hard edge margin so it can't churn the book.
    "rotation_enabled":              False,
    "rotation_min_edge_gain_pct_pts": 4.0,   # candidate edge must beat holding by this
    "rotation_hold_edge_floor_pct_pts": 2.0, # only displace holdings weaker than this
    "rotation_min_hold_days":        3,      # never rotate a name younger than this
    "rotation_max_per_cycle":        2,      # cap swaps per cycle
    "rotation_tax_bias_pct_pts":     3.0,    # extra edge to displace a near-long-term lot
    "rotation_tax_bias_window_days": 45,     # "near long-term" = within N days of 1yr
    "max_symbol_weight_pct":  0.0,     # cap any one name's % of the paper book
    "max_trades_per_symbol_per_day": 0,
    "trade_skip_open_min":    0,       # skip first N min of the regular session
    "trade_skip_close_min":   0,       # skip last N min of the regular session
    "max_slippage_bps":       0.0,     # reject a paper fill exceeding this slippage
    "risk_off_vix":           0.0,     # skip NEW buys when VIX is above this
    "dry_run":                False,   # propose + gate but NEVER execute (preview)
    "notify_fills":           False,   # macOS notification on a fill
    # ── 100x layer (aj_alpha / aj_autonomy) — all opt-in, fail-closed ────────
    # sizing intelligence (1-5)
    "kelly_sizing":           False,   # 1: fractional-Kelly sizing from realized win-rate/edge
    "kelly_fraction":         0.5,     #    fraction of full Kelly (half-Kelly default)
    "volatility_target_pct":  0.0,     # 2: target per-position daily-vol %; 0=off
    "compound_sizing":        False,   # 3: scale order size with the agent's equity
    "compound_base_equity_usd": 10000.0,  #  equity baseline for compounding ratio
    "symbol_performance_weighting": False,  # 4: up/down-size by per-symbol realized record
    "drawdown_throttle_pct":  0.0,     # 5: equity-drawdown % at which size is fully throttled; 0=off
    # Regime-aware ENTRY sizing (data-driven, v3.26 slice report): the paper book
    # was -$1,873 in bull regimes vs +$1,504 in chop — a mean-reversion-natured
    # strategy that fades trends and gets run over in them. Downsize (or skip) new
    # entries in bull. bull_entry_size_factor in [0,1]: 1.0 (default) = off,
    # 0.5 = half-size bull entries, 0.0 = skip them. chop/bear unchanged. This is
    # the UNCONFOUNDED lever — regime is an entry property, unlike holding-period
    # (an exit-machinery artifact: 48% of exits are stops, so a min-hold would
    # just enlarge stop-losses).
    "bull_entry_size_factor": 1.0,
    # entry alpha (6-11)
    "momentum_filter_days":   0,       # 6: only buy if price > N-day SMA; 0=off
    "mean_reversion_rsi_max": 0.0,     # 7: only buy if RSI(14) <= this; 0=off
    "relative_strength_filter": False, # 8: only buy names outperforming SPY over the lookback
    "relative_strength_lookback_days": 20,
    "max_book_correlation":   0.0,     # 9: block a buy whose avg corr to the book exceeds this; 0=off
    "correlation_size_budget": 0.0,    # portfolio corr/vol budget: avg-corr above which a buy's size is throttled DOWN; 0=off
    "correlation_size_floor": 0.25,    #     floor multiplier the correlation throttle can shrink size to
    "earnings_blackout_days": 0,       # 10: skip buys within N days of earnings; 0=off
    "max_sector_weight_pct":  0.0,     # 11: cap any one GICS sector's % of book; 0=off
    # exit intelligence (12-15)
    "max_holding_days":       0,       # 12: force-exit a position older than N days; 0=off
    "profit_ratchet_pct":     0.0,     # 13: once up this %, lock in a floor gain; 0=off
    "profit_ratchet_lock_pct": 0.0,    #     floor gain % the ratchet protects
    "tp_ladder":              False,   # 14: scale out in thirds at take_profit_pct, 1.5x, 2x
    # 15: ATR volatility stop is now the DEFAULT exit (stop distance = mult x
    # ATR(14)). It is risk-REDUCING, so a non-zero default is strictly safer than
    # leaving it off; set to 0 to disable. Fixed-% TP/SL remain available as a
    # fallback / complement when ATR is unavailable.
    "atr_stop_mult":          3.0,
    "atr_period":             14,
    # Risk-based position sizing: size each trade so it risks ~risk_per_trade_pct
    # of account equity to its ATR-based stop (qty ~ equity*r% / atr_stop_dist),
    # bounded by the existing notional cap. OFF by default (safer/existing
    # notional-target sizing); falls back to notional sizing when ATR/equity
    # unavailable.
    "risk_based_sizing":      False,
    "risk_per_trade_pct":     1.0,     # % of equity risked to the stop when risk_based_sizing on
    # adaptive brain (16-20)
    "adaptive_thresholds":    False,   # 16: nudge buy/sell thresholds from recent realized hit-rate
    "regime_adaptive":        False,   # 17: switch threshold/size profile on bull/bear/chop regime
    "pyramiding":             False,   # 18: allow adding to a winning position
    "pyramid_max_adds":       2,       #     max additional adds per position
    "pyramid_min_gain_pct":   3.0,     #     position must be up this % to pyramid
    "signal_scorecard":       False,   # 19: track per-signal realized accuracy (analytics/adaptive)
    "opportunity_radar":      False,   # 20: rank the scan universe, trade only the top-K
    "opportunity_radar_top_k": 5,
    # autonomous operation (21-25)
    "auto_run_enabled":       False,   # 21: run cycles automatically during market hours
    "auto_run_interval_min":  30,      #     minutes between automatic cycles
    "auto_run_align_to_clock": True,   #     fire on wall-clock marks (:00,:10,:20…) vs last-run+interval
    "health_autohalt":        False,   # 22: self-halt on fill-rate collapse / divergence / unknown orders
    "auto_preset_escalation": False,   # 23: auto conservative<->moderate<->aggressive on performance
    "daily_reflection":       False,   # 24: write an end-of-day self-review journal entry
    "premarket_briefing":     False,   # 25: build a pre-market ranked opportunity briefing
    # ── Analyst Council layer (merge plan) — ALL opt-in, fail-closed ─────────
    # Advisory multi-agent reasoning. The council can VETO/REDUCE a signal or
    # (in coequal mode) raise conviction within existing caps; it can NEVER
    # create an order the risk gate would block nor bypass any gate. Requires
    # both council_enabled AND the VERIFY-COUNCIL gate (aj_verify_council=pass).
    "council_enabled":        False,   # master switch for the council layer
    "council_policy":         "advisory",  # advisory | confirm | coequal
    "council_topk":           3,       # run council only for top-K scan candidates
    "max_research_rounds":    1,       # bull/bear debate rounds (hard-terminated)
    "max_risk_rounds":        1,       # aggressive/conservative/neutral rounds
    "council_max_calls_per_cycle": 40, # hard cap on LLM calls/cycle (cost guard)
    "council_cache_ttl_min":  360,     # memoize a decision per (symbol,date,hash)
    "council_deep_max_tokens": 1024,   # token budget for deep-think analysis
    "council_quick_max_tokens": 512,   # token budget for quick-think summarization
    "council_deep_model":     "",      # model for the deep tier ("" = provider default)
    "council_quick_model":    "",      # model for the quick tier ("" = provider default)
    "council_analyst_fundamentals": True,
    "council_analyst_news":         True,
    "council_analyst_sentiment":    True,
    "council_analyst_technical":    True,
    "personas_enabled":       False,   # Phase 6: investor-persona analysts (opt-in)
    "fingpt_sentiment_enabled": False, # Phase 6: FinGPT numeric sentiment prior (opt-in)
    # coequal-policy track-record gate (Phase 5): coequal may BOOST size only
    # after the council's realized alpha track record clears these bars.
    "coequal_min_samples":    20,      # min resolved reflections before unlock
    "coequal_min_alpha":      0.0,     # mean alpha (fraction) required to unlock
    "coequal_max_boost":      0.5,     # max extra size fraction (0.5 => up to 1.5x)
    # How a NEUTRAL council HOLD is treated for a rule-engine BUY (advisory/
    # coequal). >0 => proceed at this size fraction (trim, not block); 0 => veto
    # the BUY (legacy strict behavior). An actively-BEARISH council call
    # (UNDERWEIGHT/SELL) always vetoes regardless.
    "council_hold_size_factor": 0.5,
    # ── universe / market screener (aj_universe) ─────────────────────────────
    # How the cycle picks its candidate universe:
    #   'allowlist'     — only symbol_allowlist (fail-closed, fully controlled)
    #   'open'          — allowlist ∪ watchlist ∪ portfolio ∪ idea-pool (legacy)
    #   'market_screen' — sweep the FULL investable population (SEC equities +
    #                     top crypto), screened to a shortlist each cycle. DEFAULT.
    "universe_mode":          "market_screen",
    "screen_full_equities":   True,    # use the full ~10k SEC list (else curated)
    "include_crypto":         True,    # include crypto in the screened universe
    "crypto_universe_top":    60,      # top-N crypto by market cap to include
    "screen_scan_batch":      400,     # names batch-quoted per cycle (rotating sweep)
    "screen_max":             150,     # shortlist size handed to the forecaster
    "screen_min_price":       1.0,     # drop sub-$1 names
    "screen_min_dollar_volume": 1000000.0,  # min price*volume (liquidity floor)
    "screen_min_market_cap":  0.0,     # 0 = no market-cap floor
    # ── options trading (aj_options) — single-leg LONG calls, paper ───────────
    # When on, a BUY signal on an underlying becomes a long CALL (ATM, ~N DTE),
    # sized by premium, paper-filled at the chain mid. Live options gated like
    # live equities. Default OFF (fail-closed).
    "trade_options":          False,
    "option_target_dte":      35,      # target days-to-expiry for picked contracts
    "option_moneyness":       0.0,     # 0 = ATM; +0.05 = 5% OTM call
    "option_contract_multiplier": 100,
    "option_min_open_interest": 50,    # skip illiquid contracts below this OI
    "option_max_spread_pct":  0.30,    # skip contracts whose bid-ask > this × mid
    "option_fee_per_contract": 0.65,   # paper option commission per contract
    "option_trade_puts":      True,    # also buy long PUTS on bearish signals
    # When options are on, bias the screener toward names likely to have a
    # listed, liquid option chain (higher-priced, non-warrant/unit tickers) so
    # buys can actually route to the options sleeve. Soft rank boost only — it
    # never hard-drops equities, so equity trading is unaffected.
    "option_prefer_optionable": True,
    "option_optionable_min_price": 10.0,  # names below this rarely have liquid options
    # When nothing passes the OI/spread screen, allow falling back to the best
    # UNSCREENED contract? Default OFF (fail-closed toward not trading illiquid
    # contracts whose paper mids are unrealistic).
    "option_allow_illiquid_fallback": False,
    # screener global-best cache: rank across recently-seen names, not just the
    # current rotating slice (closes the "rolling sweep only sees 400/cycle" gap).
    "screen_cache_ttl_min":   45,      # how long a screened quote stays rank-eligible
    # ── effectiveness layer (alpha/selection/execution/allocation) ───────────
    # ALL opt-in, default OFF, fail-open. Never weakens the fail-closed gate.
    # Batch 1 — multi-factor alpha fusion (orthogonal signals into the ensemble)
    "multi_factor_signals":   False,   # fuse smart_money/insider/congress/social into prob_up
    "adapter_scorecard":      False,   # log per-adapter forecasts + decay weak sources
    # Event alpha: LLM-scored news/SEC-filing events as a point-in-time,
    # IC-gated ensemble signal. The model's opinions are stored with source
    # timestamps, decay at their own half-life, and are graded against
    # realized returns — it earns weight by skill, never by fiat.
    "event_alpha_enabled":    False,
    "event_max_llm_per_cycle": 8,      # LLM scoring budget per cycle
    "event_symbols_per_cycle": 10,     # symbols swept for fresh events per cycle
    # Nightly counterfactual: after the evening session, replay the last ~30
    # sessions under the LIVE config and its neighbors (isolated subprocesses);
    # results land in the Replay Lab panel next morning. The rolling "are my
    # current settings still the evidence-backed ones?" loop.
    "nightly_counterfactual": False,
    # Research Scientist (aj_lab): nightly hypothesis -> K-fold walk-forward
    # twin replays -> bounded, audited promotion (or rejection) with an
    # automatic demotion watchdog. Tunes ONLY whitelisted strategy keys within
    # hard bounds; never touches switches, caps, cash, or the allowlist.
    "lab_enabled":            False,
    "lab_folds":              3,       # walk-forward folds per experiment
    "lab_fold_days":          180,     # calendar days per fold
    "regime_conditional_weights": False,  # tilt signal weights by detected regime
    # Batch 4 — signal validation / IC promotion gate (guards batch 1)
    "signal_ic_gate":         True,    # require realized-skill promotion before a new signal counts
    "ic_min_samples":         20,      # min scored history to judge a signal
    "ic_min_brier_skill":     0.0,     # must beat the base rate
    "ic_min_ic":              0.0,     # info-coefficient floor (only enforced when available)
    "wf_min_signals":         20,      # offline walk-forward validation thresholds
    "wf_min_hit_rate":        0.5,
    "wf_min_sharpe":          0.0,
    # Batch 2 — cross-sectional selection (trade the best relative opportunities)
    "cross_sectional_selection": False,
    "cross_sectional_top_n":  5,
    # How many of the momentum-ranked scan names to actually FORECAST each
    # cycle before cross-sectional ranking. Forecasting is seconds-per-name;
    # forecasting the full ~250 screener output collapses under the cycle's
    # parallel time budget, so bound it to the top movers.
    "cross_sectional_scan_max": 40,
    # Batch 3 — execution alpha (better entries/exits + cost discipline)
    "limit_entry":            False,   # post a limit through the mid vs market-on-cycle
    "limit_entry_offset_bps": 10.0,    # how far through the mid to improve the fill
    "cost_gate":              False,   # skip trades whose edge < round-trip cost
    "assumed_spread_bps":     10.0,    # spread assumption when bid/ask absent
    "cost_fee_bps":           0.0,     # per-side fee assumption for the cost gate
    "cost_edge_multiple":     1.5,     # edge must exceed cost by this multiple
    "time_stop_days":         0,       # exit a stagnant thesis after N days (0 = off)
    "time_stop_min_gain_pct": 0.0,     # ...unless it's at least this far in the money
    "profit_ladder":          False,   # scale out at gain rungs
    "event_blackout_days":    0,       # block NEW entries within N days of earnings (0 = off)
    "gex_timing":             False,   # nudge entry timing by dealer gamma
    # Batch 5 — portfolio construction (risk-aware allocation across the book)
    "portfolio_construction": False,
    "alloc_method":           "risk_parity",  # risk_parity | max_sharpe | equal
    "max_position_weight":    0.25,    # per-name cap as a fraction of equity
    "max_sector_weight":      0.40,    # per-sector cap
    "correlation_cap":        True,    # down-weight highly-correlated clusters
    "correlation_cap_threshold": 0.6,
    "correlation_cap_floor":  0.5,
    # ── meta-labeling layer (learn the agent's own edge from realized trades) ──
    # A calibrated P(profitable|setup) model trained on CLOSED trades. ALL opt-in,
    # default OFF, fail-open, and double-gated: it only goes live after it beats a
    # baseline out-of-sample (metalabel_min_auc over metalabel_min_samples). When
    # live it can only FILTER (skip a trade below the prob threshold) or SHRINK/
    # scale size by realized edge — it can NEVER bypass the fail-closed risk gate.
    "metalabel_enabled":      False,   # use P(profit) to filter + size entries
    "metalabel_min_samples":  50,      # min closed-trade labels before a model can promote
    "metalabel_min_auc":      0.55,    # out-of-sample AUC the model must beat to go live
    "metalabel_prob_threshold": 0.5,   # skip a BUY whose P(profit) is below this
    "metalabel_size_by_edge": True,    # scale size by the model's calibrated edge (kelly-lite)
    "metalabel_retrain_min_new": 10,   # retrain after this many new labeled trades
    # What the meta-label model learns to predict:
    #   'profit' — P(the trade is net profitable)          [absolute]
    #   'alpha'  — P(the trade BEATS the benchmark over its holding window)
    # 'alpha' steers entries toward market-relative edge (a +1% trade in a +3%
    # market is a LOSS vs the index). Benchmark = metalabel_benchmark (SPY).
    "metalabel_target":       "profit",
    "metalabel_benchmark":    "SPY",
    # ── portfolio Risk Governor + alpha-decay circuit breaker ─────────────────
    # ONE global exposure multiplier G on NEW entries, from drawdown / regime /
    # realized alpha-decay. Opt-in, fail-open to G=1.0. Can only SHRINK exposure
    # autonomously (or pause via the circuit breaker, G=0); levering up (G>1)
    # requires risk_governor_max>1 AND proven realized edge. Never touches a sell
    # (exits always close the full position) nor the fail-closed risk gate.
    "risk_governor_enabled":  False,
    "risk_governor_max":      1.0,     # >1 permits levering up ONLY on proven edge
    "risk_governor_min":      0.0,     # floor on G when not in a breaker
    "rg_drawdown_derisk_pct": 10.0,    # start shrinking above this portfolio drawdown
    "rg_drawdown_breaker_pct": 20.0,   # circuit breaker: go flat at/above this drawdown
    # C: robust realized-expectancy estimator. 0 (default) = plain mean (legacy).
    # When >0, winsorize BOTH tails of the trailing-return window at this
    # percentile before averaging, so a single catastrophic outlier can't
    # dominate the breaker (a -73.8% lot dragged a +0.3% book to -0.7% and
    # froze all entries). Winsorizing keeps every trade as a data point — a big
    # loss still counts, just capped at the p{winsor} value — unlike a trimmed
    # mean, which discards the fat winners this strategy depends on.
    "rg_expectancy_winsor_pct": 0.0,
    "rg_vix_derisk":          30.0,    # halve exposure when VIX at/above this
    "rg_alpha_decay_min_trades": 20,   # min realized closed trades before the alpha arm engages
    "rg_alpha_decay_floor_pct": 0.1,   # weak-but-positive realized expectancy (%) -> halve
    "rg_lever_unlock_trades": 50,      # realized trades required before G may exceed 1.0
    "rg_lever_min_expectancy_pct": 0.5,  # realized expectancy (%) required to lever up
}

_BOOL_KEYS = {"trading_enabled", "live_trading_enabled", "robinhood_enabled",
              "auto_approve_paper", "use_llm_synthesis", "allow_any_symbol",
              "conviction_sizing", "dry_run", "notify_fills",
              # 100x layer
              "kelly_sizing", "compound_sizing", "symbol_performance_weighting",
              "relative_strength_filter", "tp_ladder", "adaptive_thresholds",
              "regime_adaptive", "pyramiding", "signal_scorecard",
              "opportunity_radar", "risk_based_sizing",
              "auto_run_enabled", "auto_run_align_to_clock", "health_autohalt",
              "auto_preset_escalation", "daily_reflection", "premarket_briefing",
              "nightly_counterfactual", "lab_enabled", "event_alpha_enabled",
              # council layer
              "council_enabled", "council_analyst_fundamentals",
              "council_analyst_news", "council_analyst_sentiment",
              "council_analyst_technical", "personas_enabled",
              "fingpt_sentiment_enabled",
              # universe screener
              "screen_full_equities", "include_crypto",
              # paper fill realism
              "paper_partial_fills",
              # options
              "trade_options", "option_trade_puts", "option_prefer_optionable",
              "option_allow_illiquid_fallback",
              # effectiveness layer
              "multi_factor_signals", "regime_conditional_weights",
              "signal_ic_gate", "cross_sectional_selection",
              "limit_entry", "cost_gate", "profit_ladder", "gex_timing",
              "portfolio_construction", "correlation_cap",
              "metalabel_enabled", "metalabel_size_by_edge",
              "risk_governor_enabled", "adapter_scorecard",
              # capital rotation
              "rotation_enabled",
              # mean-reversion signal adapter
              "meanrev_adapter_enabled"}
_LIST_KEYS = {"symbol_allowlist", "session_whitelist"}
_FLOAT_KEYS = {"max_order_notional_usd", "max_daily_loss_usd",
               "paper_slippage_bps", "paper_spread_fraction", "fee_bps",
               "min_fee_usd", "crypto_fee_bps", "buy_prob_threshold",
               "sell_prob_threshold", "min_edge_pct_pts",
               "order_notional_target_usd",
               "max_crypto_notional_usd", "max_option_notional_usd",
               "option_notional_target_usd",
               "min_order_notional_usd", "entry_limit_offset_bps",
               "take_profit_pct", "stop_loss_pct", "trailing_stop_pct",
               "max_symbol_weight_pct", "max_slippage_bps", "risk_off_vix",
               # 100x layer
               "kelly_fraction", "volatility_target_pct",
               "compound_base_equity_usd", "drawdown_throttle_pct",
               "mean_reversion_rsi_max", "bull_entry_size_factor",
               "max_book_correlation",
               "correlation_size_budget", "correlation_size_floor",
               "max_sector_weight_pct", "profit_ratchet_pct",
               "profit_ratchet_lock_pct", "atr_stop_mult",
               "risk_per_trade_pct",
               "pyramid_min_gain_pct",
               # council coequal gate
               "coequal_min_alpha", "coequal_max_boost", "council_hold_size_factor",
               # universe screener
               "screen_min_price", "screen_min_dollar_volume",
               "screen_min_market_cap", "option_moneyness",
               "option_max_spread_pct", "option_fee_per_contract",
               "option_optionable_min_price",
               # effectiveness layer
               "ic_min_brier_skill", "ic_min_ic", "wf_min_hit_rate",
               "wf_min_sharpe", "limit_entry_offset_bps", "assumed_spread_bps",
               "cost_fee_bps", "cost_edge_multiple", "time_stop_min_gain_pct",
               "max_position_weight", "max_sector_weight",
               "correlation_cap_threshold", "correlation_cap_floor",
               "metalabel_min_auc", "metalabel_prob_threshold",
               "risk_governor_max", "risk_governor_min", "rg_drawdown_derisk_pct",
               "rg_drawdown_breaker_pct", "rg_vix_derisk", "rg_alpha_decay_floor_pct",
               "rg_lever_min_expectancy_pct", "rg_expectancy_winsor_pct",
               # paper fill realism + cash account
               "paper_fill_liquidity_usd", "paper_cash",
               # tax view
               "tax_short_term_rate", "tax_long_term_rate",
               # capital rotation
               "rotation_min_edge_gain_pct_pts", "rotation_hold_edge_floor_pct_pts",
               "rotation_tax_bias_pct_pts"}
_INT_KEYS = {"max_trades_per_day", "forecast_horizon_days", "scan_universe_max",
             "order_ttl_cycles", "exit_cooldown_min", "max_open_positions",
             "max_trades_per_symbol_per_day", "trade_skip_open_min",
             "trade_skip_close_min",
             # capital rotation
             "rotation_min_hold_days", "rotation_max_per_cycle",
             "rotation_tax_bias_window_days",
             # 100x layer
             "momentum_filter_days", "relative_strength_lookback_days",
             "earnings_blackout_days", "max_holding_days", "atr_period",
             "pyramid_max_adds", "opportunity_radar_top_k",
             "auto_run_interval_min",
             # council layer
             "council_topk", "max_research_rounds", "max_risk_rounds",
             "council_max_calls_per_cycle", "council_cache_ttl_min",
             "council_deep_max_tokens", "council_quick_max_tokens",
             "coequal_min_samples",
             # universe screener
             "crypto_universe_top", "screen_scan_batch", "screen_max",
             # effectiveness layer
             "ic_min_samples", "wf_min_signals", "cross_sectional_top_n",
             "cross_sectional_scan_max",
             "time_stop_days", "event_blackout_days",
             "metalabel_min_samples", "metalabel_retrain_min_new",
             "rg_alpha_decay_min_trades", "rg_lever_unlock_trades",
             # research scientist
             "lab_folds", "lab_fold_days",
             # event alpha
             "event_max_llm_per_cycle", "event_symbols_per_cycle",
             # options / screener knobs previously untyped: they loaded back as
             # raw strings and int("35.5")-style coercion at the call sites
             # raised, killing contract picking; negatives also bypassed the
             # reset-to-default guard
             "option_contract_multiplier", "option_min_open_interest",
             "option_target_dte", "screen_cache_ttl_min"}
_STR_KEYS = {"daily_loss_basis", "halt_rearm", "default_broker",
             "entry_order_type", "council_policy",
             "council_deep_model", "council_quick_model",
             "universe_mode", "alloc_method",
             "metalabel_target", "metalabel_benchmark"}

_VALID_COUNCIL_POLICY = ("advisory", "confirm", "coequal")
_VALID_UNIVERSE_MODE = ("allowlist", "open", "market_screen")
_VALID_ALLOC_METHOD = ("risk_parity", "max_sharpe", "equal")

_PREFIX = "aj_"
_VALID_LOSS_BASIS = ("realized_plus_unrealized", "realized")
_VALID_REARM = ("manual", "session_open")
# 'closed' is a market STATE, never a tradable session — it must not be
# whitelist-able or the gate's session check would treat market-closed as OK.
_TRADABLE_SESSIONS = ("premarket", "regular", "afterhours")
_BASE_BROKERS = {"paper", "alpaca", "ccxt", "robinhood"}


def _valid_brokers() -> set:
    """Allowed default_broker values = the known venues PLUS anything actually
    registered in aj_broker (custom/test brokers). Lazy import avoids a circular
    dependency. A typo still falls outside this set and resets to paper."""
    brokers = set(_BASE_BROKERS)
    try:
        import aj_broker
        brokers |= set(aj_broker._BROKERS.keys())
    except Exception:
        pass
    return brokers
# Config keys that are probabilities — clamped to [0,1] so a typo can't make
# the agent buy/sell on every signal.
_PROB_KEYS = ("buy_prob_threshold", "sell_prob_threshold",
              # tax rates are fractions in [0,1] — a >1.0 rate is a typo, clamp it
              "tax_short_term_rate", "tax_long_term_rate")

# ── declarative config schema (#5) ────────────────────────────────────────────
# ONE schema entry per key {type, default, min, max, enum}, built from the
# typed key sets above and VERIFIED COMPLETE at import: every DEFAULTS key must
# land in exactly one type set, so an untyped key (which used to load back as a
# raw string with no clamping — the option_target_dte bug) is structurally
# impossible to add without tripping _UNTYPED_KEYS (asserted empty in tests).
_STATIC_ENUMS: Dict[str, tuple] = {
    "daily_loss_basis": _VALID_LOSS_BASIS,
    "halt_rearm": _VALID_REARM,
    "entry_order_type": ("market", "limit"),
    "council_policy": _VALID_COUNCIL_POLICY,
    "universe_mode": _VALID_UNIVERSE_MODE,
    "alloc_method": _VALID_ALLOC_METHOD,
    "metalabel_target": ("profit", "alpha"),
    # default_broker is validated dynamically against _valid_brokers()
}
_UNTYPED_KEYS: List[str] = []


def _build_schema() -> Dict[str, Dict[str, Any]]:
    schema: Dict[str, Dict[str, Any]] = {}
    for k, dflt in DEFAULTS.items():
        if k in _BOOL_KEYS:
            t = "bool"
        elif k in _LIST_KEYS:
            t = "list"
        elif k in _FLOAT_KEYS:
            t = "float"
        elif k in _INT_KEYS:
            t = "int"
        elif k in _STR_KEYS:
            t = "str"
        else:
            # Never silently accept: record + loudly log (tests assert empty).
            # Treated as str at runtime so a stray key still round-trips.
            _UNTYPED_KEYS.append(k)
            t = "str"
        schema[k] = {
            "type": t,
            "default": dflt,
            # numeric keys share one invariant: never negative (a negative cap
            # resets to the default rather than silently disabling a guard)
            "min": 0 if t in ("float", "int") else None,
            "max": 1.0 if k in _PROB_KEYS else None,
            "enum": _STATIC_ENUMS.get(k),
        }
    if _UNTYPED_KEYS:
        log.error("aj_config: DEFAULTS keys missing from the typed key sets "
                  "(add each to _BOOL/_LIST/_FLOAT/_INT/_STR_KEYS): %s",
                  _UNTYPED_KEYS)
    return schema


_SCHEMA: Dict[str, Dict[str, Any]] = _build_schema()


def config_schema() -> Dict[str, Dict[str, Any]]:
    """Public, copy-safe view of the declarative config schema — the Config-tab
    UI / CLI can render inputs (type, bounds, enum choices) from this instead
    of hardcoding key lists."""
    return {k: dict(v) for k, v in _SCHEMA.items()}


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _coerce_list(v: Any, upper: bool = True) -> List[str]:
    """Normalize a value into a de-duped list of strings. `upper` uppercases
    each entry (correct for ticker symbols); session names must use upper=False
    so the persisted value stays case-consistent with _TRADABLE_SESSIONS."""
    if isinstance(v, list):
        items = v
    else:
        s = str(v or "").strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            items = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            items = [x for x in s.replace(",", " ").split() if x]
    out: List[str] = []
    for it in items:
        t = str(it).strip()
        t = t.upper() if upper else t.lower()
        if t and t not in out:
            out.append(t)
    return out


import re as _re

# A number with optional sign, optional '$', and commas ONLY as proper
# thousands separators (e.g. 1,234,567.89). '1,2' / '12,5' / '1,2,3' do NOT
# match and fall back to the safe default rather than being silently
# re-interpreted as a larger value — critical for cap inputs.
_THOUSANDS_RE = _re.compile(r"^[+-]?\$?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")


def _coerce_float(v: Any, default: float = 0.0) -> float:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else default
    s = str(v).strip()
    try:
        if "," in s:
            # commas allowed only as well-formed thousands separators
            if not _THOUSANDS_RE.match(s):
                return default
            s = s.replace(",", "")
        f = float(s.replace("$", ""))
        return f if f == f and f not in (float("inf"), float("-inf")) else default
    except Exception:
        return default


def get_config() -> Dict[str, Any]:
    """Typed config = DEFAULTS overlaid with any stored `aj_*` settings."""
    raw = {}
    try:
        raw = db.get_settings()
    except Exception:
        log.debug("get_config: settings unavailable, using defaults")
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        sk = _PREFIX + key
        if sk not in raw:
            continue
        val = raw[sk]
        # Dispatch on the declarative schema (single source of truth) — an
        # untyped key can no longer silently take the raw-str branch.
        ktype = (_SCHEMA.get(key) or {}).get("type", "str")
        if ktype == "bool":
            cfg[key] = _coerce_bool(val)
        elif ktype == "list":
            cfg[key] = _coerce_list(val, upper=(key != "session_whitelist"))
        elif ktype == "float":
            cfg[key] = _coerce_float(val, DEFAULTS[key])
        elif ktype == "int":
            # round (not truncate): a fractional '0.6' must not silently collapse
            # to 0 and disable a guard the operator believed they enabled.
            cfg[key] = int(round(_coerce_float(val, DEFAULTS[key])))
        else:
            cfg[key] = str(val)
    # sanitize enums (an out-of-band stored value must not weaken the gate)
    if cfg["daily_loss_basis"] not in _VALID_LOSS_BASIS:
        cfg["daily_loss_basis"] = "realized_plus_unrealized"
    if cfg["halt_rearm"] not in _VALID_REARM:
        cfg["halt_rearm"] = "manual"
    if str(cfg["default_broker"]).lower() not in {b.lower() for b in _valid_brokers()}:
        cfg["default_broker"] = "paper"   # unknown venue => safe internal paper
    if str(cfg.get("entry_order_type")).lower() not in ("market", "limit"):
        cfg["entry_order_type"] = "market"
    if str(cfg.get("council_policy")).lower() not in _VALID_COUNCIL_POLICY:
        cfg["council_policy"] = "advisory"   # unknown policy => safest advisory
    else:
        cfg["council_policy"] = str(cfg["council_policy"]).lower()
    um = str(cfg.get("universe_mode") or "").lower()
    cfg["universe_mode"] = um if um in _VALID_UNIVERSE_MODE else "market_screen"
    am = str(cfg.get("alloc_method") or "").lower()
    cfg["alloc_method"] = am if am in _VALID_ALLOC_METHOD else "risk_parity"
    mt = str(cfg.get("metalabel_target") or "").lower()
    cfg["metalabel_target"] = mt if mt in ("profit", "alpha") else "profit"
    cfg["session_whitelist"] = [s.lower() for s in cfg["session_whitelist"]
                                if s.lower() in _TRADABLE_SESSIONS] or ["regular"]
    # numeric guards: caps/counts can never be negative; probs clamp to [0,1].
    # A negative is an invalid/typo'd value — reset it to the DEFAULT rather than
    # to 0.0. For protective limits where 0 means 'disabled' (e.g. max_slippage_bps,
    # take_profit_pct), forcing 0.0 would silently turn the guard OFF; resetting to
    # the default preserves the intended posture instead of weakening it.
    for k in _FLOAT_KEYS:
        if cfg.get(k, 0) < 0:
            cfg[k] = float(DEFAULTS[k])
    for k in _INT_KEYS:
        if cfg.get(k, 0) < 0:
            cfg[k] = int(DEFAULTS[k])
    if cfg.get("scan_universe_max", 0) < 1:
        cfg["scan_universe_max"] = 1
    for k in _PROB_KEYS:
        v = cfg.get(k, 0.5)
        cfg[k] = min(1.0, max(0.0, v))
    # Gate sanity: buy threshold must sit at/above the sell threshold. An inverted
    # pair (buy < sell) makes almost every prob both a buy AND a sell candidate —
    # reset both to their safe defaults so the gate can't be silently inverted.
    if cfg["sell_prob_threshold"] > cfg["buy_prob_threshold"]:
        cfg["buy_prob_threshold"] = float(DEFAULTS["buy_prob_threshold"])
        cfg["sell_prob_threshold"] = float(DEFAULTS["sell_prob_threshold"])
    return cfg


def _serialize(key: str, value: Any) -> str:
    # Validate/clamp to the SAME rules get_config applies on read, so the
    # persisted setting is never out-of-range or an invalid enum/session. This
    # keeps the round-trip lossless and protects any direct reader of the raw
    # setting (get_config still re-sanitizes defensively on read).
    ktype = (_SCHEMA.get(key) or {}).get("type", "str")
    if ktype == "bool":
        return "true" if _coerce_bool(value) else "false"
    if ktype == "list":
        items = _coerce_list(value, upper=(key != "session_whitelist"))
        if key == "session_whitelist":
            items = [s for s in items if s in _TRADABLE_SESSIONS] or ["regular"]
        return json.dumps(items)
    if ktype == "float":
        f = _coerce_float(value, DEFAULTS[key])
        if f < 0:
            f = float(DEFAULTS[key])
        if key in _PROB_KEYS:
            f = min(1.0, max(0.0, f))
        return str(f)
    if ktype == "int":
        i = int(round(_coerce_float(value, DEFAULTS[key])))
        if i < 0:
            i = int(DEFAULTS[key])
        return str(i)
    if key == "daily_loss_basis" and str(value) not in _VALID_LOSS_BASIS:
        return "realized_plus_unrealized"
    if key == "halt_rearm" and str(value) not in _VALID_REARM:
        return "manual"
    if key == "entry_order_type" and str(value).lower() not in ("market", "limit"):
        return "market"
    if key == "council_policy":
        v = str(value).lower()
        return v if v in _VALID_COUNCIL_POLICY else "advisory"
    if key == "universe_mode":
        v = str(value).lower()
        return v if v in _VALID_UNIVERSE_MODE else "market_screen"
    if key == "default_broker" and \
            str(value).lower() not in {b.lower() for b in _valid_brokers()}:
        return "paper"
    return str(value)


def set_config(partial: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a partial config update. Unknown keys are ignored. Returns the
    full typed config after the update. Validation rejects nonsensical values
    by clamping to the safe default rather than storing them."""
    if not isinstance(partial, dict):
        raise ValueError("config update must be a dict")
    for key, value in partial.items():
        if key not in DEFAULTS:
            log.warning("set_config: ignoring unknown key %r", key)
            continue
        db.set_setting(_PREFIX + key, _serialize(key, value))
    return get_config()


# ── ㉓ config presets ─────────────────────────────────────────────────────────
# Risk + strategy bundles. Deliberately DO NOT touch trading_enabled,
# live_trading_enabled, robinhood_enabled, or symbol_allowlist — the operator
# always controls those explicitly.
PRESETS: Dict[str, Dict[str, Any]] = {
    "conservative": {
        "max_order_notional_usd": 500, "max_trades_per_day": 3,
        "max_daily_loss_usd": 200, "buy_prob_threshold": 0.62,
        "sell_prob_threshold": 0.40, "min_edge_pct_pts": 6.0,
        "max_open_positions": 3, "stop_loss_pct": 5.0, "take_profit_pct": 10.0,
        "trailing_stop_pct": 4.0, "exit_cooldown_min": 60,
        "conviction_sizing": True, "min_order_notional_usd": 50.0,
    },
    "moderate": {
        "max_order_notional_usd": 1000, "max_trades_per_day": 5,
        "max_daily_loss_usd": 500, "buy_prob_threshold": 0.55,
        "sell_prob_threshold": 0.45, "min_edge_pct_pts": 3.0,
        "max_open_positions": 6, "stop_loss_pct": 8.0, "take_profit_pct": 15.0,
        "trailing_stop_pct": 6.0, "exit_cooldown_min": 30,
        "conviction_sizing": True, "min_order_notional_usd": 50.0,
    },
    "aggressive": {
        "max_order_notional_usd": 2500, "max_trades_per_day": 10,
        "max_daily_loss_usd": 1500, "buy_prob_threshold": 0.52,
        "sell_prob_threshold": 0.48, "min_edge_pct_pts": 1.5,
        "max_open_positions": 12, "stop_loss_pct": 12.0, "take_profit_pct": 25.0,
        "trailing_stop_pct": 10.0, "exit_cooldown_min": 0,
        "conviction_sizing": True, "min_order_notional_usd": 0.0,
    },
}


def apply_preset(name: str) -> Optional[Dict[str, Any]]:
    """Apply a risk/strategy preset. Returns the full config, or None if the
    preset name is unknown.

    NOTE: presets are ADDITIVE OVERLAYS — they set only the keys listed in
    PRESETS and leave every other tunable (incl. advanced 100x guards such as
    max_book_correlation, atr_stop_mult, kelly_sizing) at its current value.
    Switching presets therefore does NOT reset those untouched guards; the
    operator manages them explicitly. This is deliberate so a preset switch can
    never silently weaken a guard the operator set by hand."""
    p = PRESETS.get(str(name or "").lower())
    if not p:
        return None
    return set_config(dict(p))


def get(key: str) -> Any:
    return get_config().get(key)


def is_trade_path_enabled() -> bool:
    """True only when the master switch is on. Convenience for callers that
    want to short-circuit before building a proposal."""
    return bool(get_config().get("trading_enabled"))


def is_open_universe(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True when the cycle may trade OFF the allowlist — i.e. universe_mode is
    'open' or 'market_screen', or the legacy allow_any_symbol flag is set. Both
    the scan (which symbols to consider) and the risk gate (which symbols may
    pass the allowlist check) MUST agree on this, so they both call here."""
    c = cfg if cfg is not None else get_config()
    mode = str(c.get("universe_mode") or "").lower()
    return mode in ("open", "market_screen") or bool(c.get("allow_any_symbol"))


# ── Analyst Council gating ────────────────────────────────────────────────────
# The council is doubly gated: the config flag AND a one-time operator
# acknowledgement (VERIFY-COUNCIL) that the layer is costly + non-deterministic.
# This mirrors VERIFY-ALPACA / VERIFY-OPENCODE / VERIFY-MCP-READ.

def council_verify_passed() -> bool:
    """True iff the operator has closed the VERIFY-COUNCIL gate."""
    try:
        return str(db.get_settings().get("aj_verify_council")) == "pass"
    except Exception:
        return False


def council_active(cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True only when the council may run: enabled in config AND VERIFY-COUNCIL
    passed. Any read error => False (fail-closed: council stays off)."""
    try:
        c = cfg if cfg is not None else get_config()
        return bool(c.get("council_enabled")) and council_verify_passed()
    except Exception:
        return False
