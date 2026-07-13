#!/usr/bin/env python3
"""AJTA — the replay engine ("time machine").

Replays the REAL operator decision loop — scan -> forecast -> judge -> size ->
fail-closed risk gate -> paper fill -> exits -> labels — one historical
trading day at a time, against point-in-time data, in a fully ISOLATED
database. The same code paths that trade live run in the past, so every
threshold, exit rule, sizing knob, and ML model can be chosen by evidence
instead of defaults.

Usage (each `run` executes in its own process with its own DB):
    python aj_replay.py run --start 2023-01-02 --end 2024-12-31 \\
        --symbols AAPL,MSFT,NVDA,AMZN,GOOGL --cash 100000 \\
        [--set stop_loss_pct=8 --set buy_prob_threshold=0.6 ...]
    python aj_replay.py grid --start ... --end ... --symbols ... \\
        --param buy_prob_threshold=0.55,0.60,0.65 --param stop_loss_pct=5,8
    python aj_replay.py counterfactual --days 30 --symbols ...   # live-config ± neighbors
    python aj_replay.py report [--run RUN_ID]                     # print a stored result
    python aj_replay.py list                                       # index of runs
    python aj_replay.py build-cache --symbols ... --period 10y     # prefetch bars (network)
    python aj_replay.py export-model --run RUN_ID                  # replay-trained meta-label -> live DB

Design invariants:
  * ISOLATION — `run` sets AUGUR_DB_PATH to a per-run DB BEFORE importing any
    aj module; the live wealth.db is never touched by a replay.
  * NO LOOK-AHEAD — aj_replay_data patches all market-data reads to bars
    <= the simulated day and blocks raw network (a live API call from a
    signal adapter would inject present-day information into the past).
  * REAL PIPELINE — aj_operator.run_once runs verbatim: the gate, sizing,
    exits, cash account, labeling and meta-label training are the production
    code. The forecast SOURCE is pluggable: the default 'pit' forecaster is a
    fast deterministic technical ensemble (same output shape); '--forecaster
    ensemble' runs the real forecast_ensemble offline (slower; its ML
    engines may not be bit-deterministic).
  * HONEST CAVEATS — results carry a survivorship_bias caveat: the replay
    universe is names you can fetch history for TODAY; names that delisted
    along the way are underrepresented. Treat absolute returns with care;
    RELATIVE config comparisons (grid/counterfactual) share the bias evenly.

Python 3.9 compatible. Heavy imports happen inside functions on purpose: the
parent process (grid/counterfactual orchestration) must not bind the live DB.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

DEFAULT_OUT_DIR = os.path.join("data", "replays")
_SESSION_HOUR_ET = (10, 30)     # each replayed cycle runs at 10:30 ET

# Config preset every replay starts from (overridable via --set). Council and
# every network-needing overlay are off; the deterministic core is on.
_REPLAY_PRESET: Dict[str, Any] = {
    "trading_enabled": True, "auto_approve_paper": True,
    "default_broker": "paper", "universe_mode": "allowlist",
    "session_whitelist": ["regular"], "halt_rearm": "manual",
    "council_enabled": False, "daily_reflection": False,
    "premarket_briefing": False, "auto_run_enabled": False,
    "multi_factor_signals": False, "adapter_scorecard": False,
    "trade_options": False, "health_autohalt": False,
    "metalabel_enabled": False,     # generation run; training happens after
    "max_trades_per_day": 20, "max_open_positions": 10,
    "buy_prob_threshold": 0.55, "sell_prob_threshold": 0.45,
    "min_edge_pct_pts": 3.0,
    "take_profit_pct": 15.0, "stop_loss_pct": 8.0, "trailing_stop_pct": 0.0,
    "exit_cooldown_min": 0, "trade_skip_open_min": 0, "trade_skip_close_min": 0,
}


# ── deterministic point-in-time forecaster (default) ──────────────────────────

def _pit_forecast_factory(store):
    """Build a forecaster with the SAME output shape as forecast_ensemble:
    {"symbol", "ensemble": {"prob_up", "edge_pct_pts", "conviction"}, ...}.
    A plain momentum/RSI/SMA blend on as-of closes — deliberately simple,
    fully deterministic, zero network. It is the replay's SIGNAL SOURCE; the
    decision pipeline consuming it is the production code."""
    import math

    def pit_forecast(symbol: str, horizon_days: int = 20) -> Dict[str, Any]:
        bars = store.chart(symbol, "1y", "1d")
        closes = [b.get("close") for b in bars if b.get("close")]
        if len(closes) < 40:
            return {"symbol": symbol, "ensemble": None, "signals": [],
                    "n_signals": 0, "error": "insufficient history"}
        px = [float(c) for c in closes]
        r5 = px[-1] / px[-6] - 1.0
        r20 = px[-1] / px[-21] - 1.0
        deltas = [px[i] - px[i - 1] for i in range(len(px) - 14, len(px))]
        gains = sum(d for d in deltas if d > 0)
        losses = sum(-d for d in deltas if d < 0)
        rsi = 100.0 * gains / (gains + losses) if (gains + losses) > 0 else 50.0
        sma20 = sum(px[-20:]) / 20.0
        sma_dist = (px[-1] / sma20 - 1.0) if sma20 > 0 else 0.0
        # daily vol for scale-invariance (a 2% move means more in a quiet name)
        rets = [px[i] / px[i - 1] - 1.0 for i in range(max(1, len(px) - 60), len(px))]
        mu = sum(rets) / len(rets)
        vol = (sum((r - mu) ** 2 for r in rets) / max(1, len(rets) - 1)) ** 0.5 or 0.01
        # blended momentum z-score -> logistic prob_up
        z = (0.5 * (r20 / (vol * (20 ** 0.5))) + 0.3 * (r5 / (vol * (5 ** 0.5)))
             + 0.2 * ((rsi - 50.0) / 20.0) + 0.15 * (sma_dist / (vol * (20 ** 0.5))))
        prob_up = 1.0 / (1.0 + math.exp(-1.2 * z))
        prob_up = min(0.95, max(0.05, prob_up))
        edge = (prob_up - 0.5) * 100.0
        conviction = ("high" if abs(edge) >= 12 else
                      "medium" if abs(edge) >= 6 else "low")
        return {"symbol": symbol,
                "ensemble": {"prob_up": round(prob_up, 4),
                             "edge_pct_pts": round(edge, 3),
                             "conviction": conviction},
                "signals": [{"name": "pit_momentum", "prob_up": prob_up}],
                "n_signals": 1}

    return pit_forecast


# ── the replay run (executes with an ISOLATED DB) ─────────────────────────────

def _sim_instant(date_str: str):
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        return datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=_SESSION_HOUR_ET[0], minute=_SESSION_HOUR_ET[1],
            tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
    except Exception:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=15, minute=30, tzinfo=timezone.utc)


def run_replay(args) -> Dict[str, Any]:
    """The actual simulation. MUST run in a process whose AUGUR_DB_PATH was
    set to the run DB before any aj import (main() guarantees this)."""
    import aj_replay_data as rd

    run_dir = os.path.dirname(os.environ["AUGUR_DB_PATH"])
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    bench = args.benchmark.upper()
    need = list(dict.fromkeys(symbols + [bench, "^VIX"]))

    # 1) dataset: build any missing cache (network) unless --offline, then load
    store = rd.PITStore(args.cache_dir)
    if not args.offline:
        rd.build_cache(need, period=args.cache_period, cache_dir=args.cache_dir)
    loaded = store.load(need)
    missing = [s for s in symbols if s not in loaded]
    if bench not in loaded:
        raise SystemExit("benchmark {} has no cached history — run "
                         "build-cache first or drop --offline".format(bench))
    if missing:
        print("WARN: no history for {} — excluded".format(",".join(missing)),
              file=sys.stderr)
        symbols = [s for s in symbols if s in loaded]
    if not symbols:
        raise SystemExit("no replayable symbols")

    # 2) patch the data layer BEFORE any aj import triggers a fetch
    rd.patch(store, block_network=True)

    import aj_db
    import aj_config
    import aj_risk
    import aj_operator
    import aj_alpha
    aj_db.aj_init()

    # 3) replay config: preset + cash + universe + user overrides
    cash = float(args.cash)
    cfg_over: Dict[str, Any] = dict(_REPLAY_PRESET)
    cfg_over.update({
        "symbol_allowlist": symbols,
        "scan_universe_max": max(len(symbols), 25),
        "paper_cash": cash,
        "compound_base_equity_usd": cash,
        "max_order_notional_usd": round(cash * 0.10, 2),
        "order_notional_target_usd": round(cash * 0.05, 2),
        "max_daily_loss_usd": round(cash * 0.03, 2),
        "min_order_notional_usd": 50.0,
    })
    for kv in (args.set or []):
        k, _, v = kv.partition("=")
        cfg_over[k.strip()] = v.strip()
    aj_config.set_config(cfg_over)
    cfg = aj_config.get_config()

    # 3b) optional FROZEN model import: install a pooled/pre-trained meta-label
    # model into this run's isolated DB before the day loop. Combined with
    # `--set metalabel_enabled=true --set metalabel_retrain_min_new=999999`
    # this replays the pipeline FILTERED by a model that never retrains —
    # the clean holdout test of the filter itself.
    imported_model = None
    if getattr(args, "import_model", None):
        with open(args.import_model) as f:
            imported_model = json.load(f)
        aj_db.set_setting_raw("__aj_metalabel_model", json.dumps(imported_model))
        # CRITICAL: stamp the last-train counter too. maybe_retrain treats a
        # missing counter as "never trained" and trains IMMEDIATELY on cycle 1
        # — on a fresh replay DB that fit sees zero labels and overwrites the
        # imported model with a non-promoted stub, silently disabling the
        # filter for the whole run (how the first validation run went dark).
        aj_db.set_setting_raw("__aj_metalabel_last_train_n",
                              str(int(imported_model.get("n_train") or 0)))
        print("imported meta-label model: oos_auc={} promoted={}".format(
            imported_model.get("oos_auc"), imported_model.get("promoted")),
            file=sys.stderr)

    # 4) forecaster
    if args.forecaster == "pit":
        import forecast_ensemble
        forecast_ensemble.ensemble_forecast = _pit_forecast_factory(store)
    # 'ensemble' keeps the real forecast_ensemble (data layer already
    # patched + its result cache disabled by rd.patch)

    # 5) day loop — the REAL operator cycle at each historical session
    dates = store.trading_dates(args.start, args.end, anchor=bench)
    if not dates:
        raise SystemExit("no trading dates in range (check cache period)")
    daily: List[Dict[str, Any]] = []
    halts = 0
    t_total = len(dates)
    for i, d in enumerate(dates):
        store.set_asof(d)
        aj_db.set_sim_clock(_sim_instant(d))
        if aj_db.market_session() != "regular":
            continue
        # a daily-loss halt is a real event worth counting — but the replay
        # plays the operator who re-arms next morning, else one bad day ends
        # a 5-year simulation.
        if aj_risk.is_halted():
            halts += 1
            aj_risk.rearm(actor="replay")
        summary = aj_operator.run_once("paper")
        bp = aj_alpha.buying_power(cfg)
        equity = bp.get("equity")
        daily.append({
            "date": d,
            "equity": round(float(equity), 2) if equity is not None else None,
            "available": bp.get("available"),
            "executed": sum(1 for p in (summary.get("proposals") or [])
                            if p.get("result") == "executed"),
            "exits": len(summary.get("exits") or []),
            "ok": bool(summary.get("ok")),
        })
        if args.progress and (i % 25 == 0 or i == t_total - 1):
            print("  [{}/{}] {} equity={}".format(
                i + 1, t_total, d, daily[-1]["equity"]), file=sys.stderr)

    # 6) label every closed trade + train the meta-label on the replay's
    # history (thousands of samples instead of dozens). SPY/bench history for
    # labeling must cover the WHOLE replay, not the live 2y window.
    store.set_asof(args.end)
    aj_db.set_sim_clock(_sim_instant(dates[-1]))
    import aj_features
    import aj_benchmark
    import aj_metalabel
    full_hist = {b["date"]: float(b["close"])
                 for b in store.chart(bench, "max", "1d")}
    # Patch PER SYMBOL: build_labels calls _index_closes for the benchmark,
    # for ^VIX, and for each traded symbol. An earlier version returned the
    # benchmark's closes for EVERY symbol, which poisoned the labels — the
    # point-in-time "VIX" became an SPY price (~450 vs ~20, so finalize
    # overwrote the real persisted vix on every row) and per-symbol
    # technicals were computed off SPY's series. Models pool-trained on
    # those labels carried a ~20x-off vix mean/std into live inference.
    # store.chart is as-of-aware (set_asof above) and returns [] for symbols
    # without cached history, which _index_closes callers treat as missing.
    aj_benchmark._index_closes = lambda sym, period: {
        b["date"]: float(b["close"]) for b in store.chart(sym, "max", "1d")}
    aj_features._spy_history = lambda _h=sorted(full_hist.items()): list(_h)
    labels = aj_features.build_labels(lookback_days=10 ** 5)
    if imported_model is not None:
        # A frozen-model validation run: do NOT retrain at the end (it would
        # overwrite the artifact of record). Report the model that actually
        # filtered the run, plus how often it fired.
        ml = {"imported": True, "oos_auc": imported_model.get("oos_auc"),
              "n_train": imported_model.get("n_train"),
              "promoted": imported_model.get("promoted")}
        try:
            import database as _dbm
            skips = 0
            for row in _dbm.get_conn().execute(
                    "SELECT result_json FROM aj_cycle_stats"):
                try:
                    skips += int((json.loads(row["result_json"] or "{}")
                                  ).get("metalabel_skip") or 0)
                except Exception:
                    pass
            ml["skips"] = skips
        except Exception:
            pass
    else:
        ml_cfg = dict(cfg)
        ml_cfg["metalabel_enabled"] = True
        try:
            ml = aj_metalabel.train(ml_cfg)
        except Exception as e:
            ml = {"error": str(e)[:200]}
    aj_db.set_sim_clock(None)

    # 7) metrics
    result = _metrics(daily, full_hist, cash, args, symbols, halts,
                      labels, ml)
    result["run_id"] = args.run_id
    result["db_path"] = os.environ["AUGUR_DB_PATH"]
    _write_artifact(run_dir, result)
    print(_render(result))
    return result


def _metrics(daily, bench_closes, cash, args, symbols, halts,
             labels, ml) -> Dict[str, Any]:
    import aj_analytics
    import aj_db
    eq = [(d["date"], d["equity"]) for d in daily if d["equity"] is not None]
    out: Dict[str, Any] = {
        "kind": "replay", "start": args.start, "end": args.end,
        "symbols": symbols, "cash": cash, "forecaster": args.forecaster,
        "config_overrides": args.set or [], "days": len(daily),
        "halts": halts, "daily": daily,
        "labels": labels, "metalabel": ml,
        "caveats": ["survivorship_bias: universe is names fetchable today; "
                    "delisted names are underrepresented — prefer RELATIVE "
                    "config comparisons over absolute returns"],
    }
    try:
        out["trades"] = aj_analytics.trade_stats() or {}
    except Exception:
        out["trades"] = {}
    try:
        rows = aj_db.query("SELECT decision, COUNT(*) AS n FROM aj_risk_events "
                           "GROUP BY decision")
        out["gate"] = {r["decision"]: int(r["n"]) for r in rows}
    except Exception:
        out["gate"] = {}
    if len(eq) >= 2:
        vals = [v for _, v in eq]
        rets = [vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals))
                if vals[i - 1] and vals[i - 1] > 0]
        # Guard vals[0] exactly like the CAGR line below — a 0.0 day-1 equity
        # (e.g. account_equity's fail-open 0.0 on a transient ledger error)
        # must not ZeroDivisionError away a fully completed simulation at the
        # very last (metrics) step.
        out["total_return_pct"] = round((vals[-1] / vals[0] - 1.0) * 100.0, 2) \
            if vals[0] > 0 else None
        years = max(1e-9, len(vals) / 252.0)
        out["cagr_pct"] = round(((vals[-1] / vals[0]) ** (1.0 / years) - 1.0) * 100.0, 2) \
            if vals[0] > 0 and vals[-1] > 0 else None
        if len(rets) >= 10:
            m = sum(rets) / len(rets)
            sd = (sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
            out["sharpe"] = round(m / sd * (252 ** 0.5), 2) if sd > 0 else None
        peak, mdd = vals[0], 0.0
        for v in vals:
            peak = max(peak, v)
            if peak > 0:
                mdd = max(mdd, (peak - v) / peak)
        out["max_drawdown_pct"] = round(mdd * 100.0, 2)
        # benchmark over the same dates (buy-and-hold the index) — the daily
        # series also ships in the artifact so the UI can chart agent vs index
        bd = [bench_closes.get(d) for d, _ in eq]
        out["benchmark_daily"] = [{"date": d, "close": c}
                                  for (d, _), c in zip(eq, bd) if c]
        pairs = [(a, b) for (_, a), b in zip(eq, bd) if a and b]
        if len(pairs) >= 2:
            b0, b1 = pairs[0][1], pairs[-1][1]
            out["benchmark_return_pct"] = round((b1 / b0 - 1.0) * 100.0, 2)
            out["alpha_pct"] = round(out["total_return_pct"]
                                     - out["benchmark_return_pct"], 2) \
                if out["total_return_pct"] is not None else None
            ex = []
            for i in range(1, len(pairs)):
                a0, bb0 = pairs[i - 1]
                a1, bb1 = pairs[i]
                if a0 > 0 and bb0 > 0:
                    ex.append((a1 / a0 - 1.0) - (bb1 / bb0 - 1.0))
            if len(ex) >= 10:
                m = sum(ex) / len(ex)
                sd = (sum((x - m) ** 2 for x in ex) / (len(ex) - 1)) ** 0.5
                if sd > 0:
                    out["information_ratio"] = round(m / sd * (252 ** 0.5), 2)
                    out["tracking_error_pct"] = round(sd * (252 ** 0.5) * 100.0, 2)
    return out


def _write_artifact(run_dir: str, result: Dict[str, Any]) -> None:
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "result.json"), "w") as f:
        json.dump(result, f, indent=1, default=str)


def _render(r: Dict[str, Any]) -> str:
    t = r.get("trades") or {}
    lines = [
        "═══ replay {} ═══".format(r.get("run_id", "?")),
        "period {} → {}  ({} sessions, {} symbols, forecaster={})".format(
            r.get("start"), r.get("end"), r.get("days"),
            len(r.get("symbols") or []), r.get("forecaster")),
        "equity ${:,.0f} start → return {}%  (CAGR {}%)  benchmark {}%  alpha {}%".format(
            r.get("cash") or 0, r.get("total_return_pct"), r.get("cagr_pct"),
            r.get("benchmark_return_pct"), r.get("alpha_pct")),
        "sharpe {}  IR {}  max drawdown {}%  halts {}".format(
            r.get("sharpe"), r.get("information_ratio"),
            r.get("max_drawdown_pct"), r.get("halts")),
        "trades {}  win-rate {}  profit factor {}".format(
            t.get("trades"), t.get("win_rate"), t.get("profit_factor")),
        "labels built {}  metalabel oos_auc {}".format(
            (r.get("labels") or {}).get("built"),
            (r.get("metalabel") or {}).get("oos_auc")),
        "gate decisions {}".format(r.get("gate")),
        "caveat: {}".format((r.get("caveats") or [""])[0]),
    ]
    return "\n".join(lines)


# ── orchestration modes (parent process — never binds the live DB for runs) ──

def spawn_cmd_env(run_id: str, out_dir: str) -> tuple:
    """(argv-prefix, env) for launching a replay `run` in a FRESH process.
    Uses `-m aj_replay` with the parent's sys.path (NOT a file path): inside
    the frozen desktop bundle this module lives in python39.zip, so there is
    no aj_replay.py on disk to exec — module invocation works in both the
    source checkout and the py2app bundle."""
    run_dir = os.path.join(out_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    env = dict(os.environ)
    env["AUGUR_DB_PATH"] = os.path.join(run_dir, "replay.db")
    # sys.path's cwd entry is the EMPTY string — dropping it would strip the
    # repo from the child's path; materialize it. In the frozen bundle this
    # module's dirname is inside python39.zip (not a real dir) and sys.path
    # already carries the zip, so only real directories are added.
    paths = [p if p else os.getcwd() for p in sys.path]
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(here) and here not in paths:
        paths.insert(0, here)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
    return [sys.executable, "-m", "aj_replay"], env


def _spawn_run(base_args: List[str], run_id: str, out_dir: str,
               extra_sets: List[str]) -> Optional[Dict[str, Any]]:
    run_dir = os.path.join(out_dir, run_id)
    prefix, env = spawn_cmd_env(run_id, out_dir)
    cmd = prefix + ["run", "--run-id", run_id, "--out-dir", out_dir] + base_args
    for s in extra_sets:
        cmd += ["--set", s]
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if p.returncode != 0:
        print("run {} FAILED:\n{}".format(run_id, (p.stderr or "")[-2000:]),
              file=sys.stderr)
        return None
    try:
        with open(os.path.join(run_dir, "result.json")) as f:
            return json.load(f)
    except Exception:
        return None


def _split_metrics(result: Dict[str, Any], train_frac: float) -> Dict[str, Any]:
    """Train/holdout split of a run's daily equity series: configs are RANKED
    on the train window and judged on the untouched holdout — the discipline
    that stops the grid from just memorizing the whole period."""
    eq = [(d["date"], d["equity"]) for d in (result.get("daily") or [])
          if d.get("equity") is not None]
    if len(eq) < 20:
        return {"train_sharpe": None, "test_sharpe": None, "test_return_pct": None}
    cut = int(len(eq) * train_frac)

    def _sharpe(vals):
        rets = [vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals))
                if vals[i - 1] > 0]
        if len(rets) < 10:
            return None
        m = sum(rets) / len(rets)
        sd = (sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
        return round(m / sd * (252 ** 0.5), 2) if sd > 0 else None

    train = [v for _, v in eq[:cut]]
    test = [v for _, v in eq[cut:]]
    return {
        "train_sharpe": _sharpe(train),
        "test_sharpe": _sharpe(test),
        "test_return_pct": (round((test[-1] / test[0] - 1.0) * 100.0, 2)
                            if len(test) >= 2 and test[0] > 0 else None),
        "split_date": eq[cut][0] if cut < len(eq) else None,
    }


def run_grid(args) -> Dict[str, Any]:
    """Cartesian grid over --param key=v1,v2,... . Each cell replays in its
    own process/DB over the SAME period + dataset; ranking is by train-window
    sharpe, and the winner is reported with its HOLDOUT performance."""
    params: Dict[str, List[str]] = {}
    for p in (args.param or []):
        k, _, vs = p.partition("=")
        params[k.strip()] = [v.strip() for v in vs.split(",") if v.strip()]
    if not params:
        raise SystemExit("grid needs at least one --param key=v1,v2")
    combos = list(itertools.product(*params.values()))
    if len(combos) > args.max_cells:
        raise SystemExit("grid has {} cells (> --max-cells {})".format(
            len(combos), args.max_cells))
    base = ["--start", args.start, "--end", args.end,
            "--symbols", args.symbols, "--cash", str(args.cash),
            "--forecaster", args.forecaster, "--benchmark", args.benchmark,
            "--cache-dir", args.cache_dir, "--cache-period", args.cache_period]
    for s in (args.set or []):
        base += ["--set", s]
    keys = list(params.keys())
    rows = []
    stamp = args.grid_id
    for i, combo in enumerate(combos):
        sets = ["{}={}".format(k, v) for k, v in zip(keys, combo)]
        rid = "{}_c{:03d}".format(stamp, i)
        print("grid cell {}/{}: {}".format(i + 1, len(combos), sets),
              file=sys.stderr)
        res = _spawn_run(base, rid, args.out_dir, sets)
        if res is None:
            continue
        row = {"run_id": rid, "params": dict(zip(keys, combo)),
               "total_return_pct": res.get("total_return_pct"),
               "sharpe": res.get("sharpe"), "alpha_pct": res.get("alpha_pct"),
               "max_drawdown_pct": res.get("max_drawdown_pct"),
               "trades": (res.get("trades") or {}).get("trades")}
        row.update(_split_metrics(res, args.train_frac))
        rows.append(row)
    rows.sort(key=lambda r: (r.get("train_sharpe") is None,
                             -(r.get("train_sharpe") or -999)))
    summary = {"kind": "grid", "grid_id": stamp, "params": params,
               "start": args.start, "end": args.end, "symbols": args.symbols,
               "train_frac": args.train_frac, "cells": rows,
               "winner": rows[0] if rows else None}
    gd = os.path.join(args.out_dir, stamp)
    os.makedirs(gd, exist_ok=True)
    with open(os.path.join(gd, "grid.json"), "w") as f:
        json.dump(summary, f, indent=1, default=str)
    print(json.dumps({"winner": summary["winner"],
                      "cells_ranked": [{k: r.get(k) for k in
                                        ("run_id", "params", "train_sharpe",
                                         "test_sharpe", "test_return_pct",
                                         "alpha_pct")} for r in rows]},
                     indent=1, default=str))
    return summary


def run_counterfactual(args) -> Dict[str, Any]:
    """Replay the recent past under the LIVE config and simple neighbors —
    the nightly 'what would tighter stops have done this month' loop.
    Reads the live config (read-only) to seed the grid."""
    from datetime import datetime, timedelta, timezone
    import aj_config          # read-only peek at the live DB
    cfg = aj_config.get_config()
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc)
             - timedelta(days=int(args.days * 1.6) + 5)).strftime("%Y-%m-%d")
    live_sets = ["{}={}".format(k, cfg.get(k)) for k in
                 ("buy_prob_threshold", "sell_prob_threshold",
                  "min_edge_pct_pts", "stop_loss_pct", "take_profit_pct")]
    if not args.param:
        sl = float(cfg.get("stop_loss_pct") or 8) or 8.0
        bt = float(cfg.get("buy_prob_threshold") or 0.55)
        args.param = [
            "stop_loss_pct={:g},{:g},{:g}".format(max(1, sl * 0.5), sl, sl * 1.5),
            "buy_prob_threshold={:g},{:g}".format(bt, min(0.9, bt + 0.05)),
        ]
    args.start, args.end = start, end
    args.set = (args.set or []) + [s for s in live_sets
                                   if s.split("=")[0] not in
                                   "".join(args.param)]
    args.grid_id = args.grid_id or "cf_{}".format(end.replace("-", ""))
    return run_grid(args)


def pool_train(args) -> Dict[str, Any]:
    """Train ONE meta-label model on the UNION of label rows from many replay
    DBs (step 1+2 of the win-rate program: replay-scale training data instead
    of the live book's dozens of trades). Read-only on every replay DB; the
    result is written to a JSON file — nothing touches the live model store
    until an explicit export/enable."""
    import sqlite3
    rows: List[Dict[str, Any]] = []
    used = []
    for rid in [r.strip() for r in args.runs.split(",") if r.strip()]:
        db_path = os.path.join(args.out_dir, rid, "replay.db")
        if not os.path.exists(db_path):
            print("WARN: no replay DB for {} — skipped".format(rid),
                  file=sys.stderr)
            continue
        conn = sqlite3.connect("file:{}?mode=ro".format(db_path), uri=True)
        conn.row_factory = sqlite3.Row
        q = ("SELECT features_json, label, beat_benchmark, regime, opened_at, "
             "closed_at FROM aj_trade_labels")
        params: tuple = ()
        if args.cutoff:
            q += " WHERE closed_at < ?"
            params = (args.cutoff,)
        got = [dict(r) for r in conn.execute(q, params).fetchall()]
        conn.close()
        rows.extend(got)
        used.append({"run": rid, "labels": len(got)})
    # ONE time-ordering across the whole pool — the purged walk-forward split
    # inside the fit assumes closed_at ascending.
    rows.sort(key=lambda r: (r.get("closed_at") or "", r.get("opened_at") or ""))
    import aj_metalabel
    cfg = {"metalabel_min_samples": args.min_samples,
           "metalabel_min_auc": args.min_auc}
    model = aj_metalabel.train_from_rows(rows, cfg, target=args.target)
    summary = {"kind": "pool_train", "runs": used, "n_pooled": len(rows),
               "cutoff": args.cutoff, "target": args.target,
               "oos_auc": model.get("oos_auc"), "promoted": model.get("promoted"),
               "C": model.get("C"), "n_purged": model.get("n_purged"),
               "base_rate": model.get("base_rate"),
               "reason": model.get("reason") or model.get("error")}
    if model.get("error"):
        print(json.dumps(summary, indent=1, default=str))
        raise SystemExit(1)
    with open(args.out, "w") as f:
        json.dump(model, f, indent=1, default=str)
    summary["model_file"] = args.out
    print(json.dumps(summary, indent=1, default=str))
    return summary


def export_model(args) -> None:
    """Copy a replay-trained meta-label model into the LIVE DB (with
    provenance) so live inference benefits from replay-scale training."""
    import sqlite3
    run_dir = os.path.join(args.out_dir, args.run)
    db_path = os.path.join(run_dir, "replay.db")
    if not os.path.exists(db_path):
        raise SystemExit("no replay DB at {}".format(db_path))
    conn = sqlite3.connect("file:{}?mode=ro".format(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT value FROM settings WHERE key=?",
                       ("__aj_metalabel_model",)).fetchone()
    conn.close()
    if not row or not row["value"]:
        raise SystemExit("run {} has no trained meta-label model".format(args.run))
    model = json.loads(row["value"])
    model["provenance"] = {"source": "aj_replay", "run_id": args.run}
    import aj_db
    aj_db.aj_init()
    aj_db.set_setting_raw("__aj_metalabel_model", json.dumps(model))
    aj_db.audit("config", {"kind": "metalabel_model_import",
                           "run_id": args.run,
                           "oos_auc": model.get("oos_auc"),
                           "n_train": model.get("n_train")}, actor="human")
    print(json.dumps({"imported": True, "run_id": args.run,
                      "oos_auc": model.get("oos_auc"),
                      "feature_names": model.get("feature_names")},
                     indent=1, default=str))


def cmd_report(args) -> None:
    out_dir = args.out_dir
    run = args.run
    if not run:
        # Guard like cmd_list: on a fresh checkout / desktop install the
        # replays dir doesn't exist yet — surface the friendly SystemExit,
        # not a raw FileNotFoundError traceback from os.listdir.
        runs = sorted(d for d in os.listdir(out_dir)
                      if os.path.exists(os.path.join(out_dir, d, "result.json"))) \
            if os.path.isdir(out_dir) else []
        if not runs:
            raise SystemExit("no replay runs in {}".format(out_dir))
        run = runs[-1]
    with open(os.path.join(out_dir, run, "result.json")) as f:
        print(_render(json.load(f)))


def cmd_list(args) -> None:
    out_dir = args.out_dir
    rows = []
    if os.path.isdir(out_dir):
        for d in sorted(os.listdir(out_dir)):
            p = os.path.join(out_dir, d, "result.json")
            g = os.path.join(out_dir, d, "grid.json")
            if os.path.exists(p):
                try:
                    with open(p) as f:
                        r = json.load(f)
                    rows.append({"run": d, "kind": "replay",
                                 "period": "{}→{}".format(r.get("start"), r.get("end")),
                                 "return_pct": r.get("total_return_pct"),
                                 "alpha_pct": r.get("alpha_pct"),
                                 "sharpe": r.get("sharpe")})
                except Exception:
                    pass
            elif os.path.exists(g):
                rows.append({"run": d, "kind": "grid"})
    print(json.dumps(rows, indent=1, default=str))


# ── CLI ────────────────────────────────────────────────────────────────────────

def _add_common(sp):
    sp.add_argument("--symbols", required=True,
                    help="comma-separated replay universe")
    sp.add_argument("--start")
    sp.add_argument("--end")
    sp.add_argument("--cash", type=float, default=100000.0)
    sp.add_argument("--forecaster", choices=["pit", "ensemble"], default="pit")
    sp.add_argument("--benchmark", default="SPY")
    sp.add_argument("--cache-dir", default=None)
    sp.add_argument("--cache-period", default="10y")
    sp.add_argument("--out-dir", default=None)
    sp.add_argument("--set", action="append",
                    help="config override key=value (repeatable)")
    sp.add_argument("--offline", action="store_true",
                    help="require the disk cache; never touch the network")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="aj_replay", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("run", help="one replay in THIS process (isolated DB)")
    _add_common(rp)
    rp.add_argument("--run-id", default=None)
    rp.add_argument("--progress", action="store_true", default=True)
    rp.add_argument("--import-model", default=None,
                    help="JSON model file to install (frozen) before the day loop")

    gp = sub.add_parser("grid", help="config grid (spawns one process per cell)")
    _add_common(gp)
    gp.add_argument("--param", action="append",
                    help="grid axis key=v1,v2,... (repeatable)")
    gp.add_argument("--train-frac", type=float, default=0.7)
    gp.add_argument("--max-cells", type=int, default=24)
    gp.add_argument("--grid-id", default=None)

    cp = sub.add_parser("counterfactual",
                        help="recent-past grid seeded from the LIVE config")
    _add_common(cp)
    cp.add_argument("--days", type=int, default=30)
    cp.add_argument("--param", action="append")
    cp.add_argument("--train-frac", type=float, default=0.7)
    cp.add_argument("--max-cells", type=int, default=24)
    cp.add_argument("--grid-id", default=None)

    bp = sub.add_parser("build-cache", help="prefetch bar history (network)")
    bp.add_argument("--symbols", required=True)
    bp.add_argument("--period", default="10y")
    bp.add_argument("--cache-dir", default=None)
    bp.add_argument("--refresh", action="store_true")

    ep = sub.add_parser("export-model",
                        help="replay-trained meta-label model -> live DB")
    ep.add_argument("--run", required=True)
    ep.add_argument("--out-dir", default=None)

    pt = sub.add_parser("pool-train",
                        help="train ONE model on labels pooled from many runs")
    pt.add_argument("--runs", required=True, help="comma-separated run ids")
    pt.add_argument("--out", required=True, help="output model JSON file")
    pt.add_argument("--cutoff", default=None,
                    help="only labels closed BEFORE this ISO date (holdout hygiene)")
    pt.add_argument("--target", choices=["profit", "alpha"], default="profit")
    pt.add_argument("--min-samples", type=int, default=200)
    pt.add_argument("--min-auc", type=float, default=0.55)
    pt.add_argument("--out-dir", default=None)

    sub.add_parser("list", help="index of stored runs").add_argument(
        "--out-dir", default=None)
    rp2 = sub.add_parser("report", help="print a stored run's summary")
    rp2.add_argument("--run", default=None)
    rp2.add_argument("--out-dir", default=None)

    args = ap.parse_args(argv)
    # A non-positive bankroll makes every equity metric degenerate (and used
    # to surface as a ZeroDivisionError at the metrics step AFTER the full
    # day loop) — reject it up front, for run/grid/counterfactual alike.
    if getattr(args, "cash", None) is not None and args.cash <= 0:
        raise SystemExit("--cash must be > 0")
    if getattr(args, "out_dir", None) is None:
        args.out_dir = DEFAULT_OUT_DIR
    if getattr(args, "cache_dir", None) is None:
        args.cache_dir = os.environ.get("AJ_REPLAY_CACHE",
                                        os.path.join("data", "replay_cache"))

    if args.cmd == "run":
        from datetime import datetime, timezone
        if not args.run_id:
            args.run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        if not (args.start and args.end):
            raise SystemExit("run needs --start and --end")
        # ISOLATION: bind this process to the run DB BEFORE any aj import.
        # (When spawned by grid/counterfactual the env is already set.)
        if "AUGUR_DB_PATH" not in os.environ or \
                os.path.basename(os.environ["AUGUR_DB_PATH"]) != "replay.db":
            run_dir = os.path.join(args.out_dir, args.run_id)
            os.makedirs(run_dir, exist_ok=True)
            os.environ["AUGUR_DB_PATH"] = os.path.join(run_dir, "replay.db")
        if "database" in sys.modules:
            raise SystemExit("aj_replay run must start in a fresh process "
                             "(the live DB is already bound)")
        run_replay(args)
        return 0
    if args.cmd == "grid":
        from datetime import datetime, timezone
        if not (args.start and args.end):
            raise SystemExit("grid needs --start and --end")
        if not args.grid_id:
            args.grid_id = "grid_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        run_grid(args)
        return 0
    if args.cmd == "counterfactual":
        run_counterfactual(args)
        return 0
    if args.cmd == "build-cache":
        import aj_replay_data as rd
        n = rd.build_cache([s.strip() for s in args.symbols.split(",")],
                           period=args.period, cache_dir=args.cache_dir,
                           refresh=args.refresh)
        print(json.dumps(n, indent=1))
        return 0
    if args.cmd == "export-model":
        export_model(args)
        return 0
    if args.cmd == "pool-train":
        pool_train(args)
        return 0
    if args.cmd == "report":
        cmd_report(args)
        return 0
    if args.cmd == "list":
        cmd_list(args)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
