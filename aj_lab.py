"""AJTA — the Research Scientist (aj_lab).

The autonomous improvement loop: generate hypotheses about the trading
agent's configuration, test them as K-fold walk-forward twin replays in the
Replay Lab (isolated processes, real pipeline, point-in-time data), and
PROMOTE only what beats the live baseline under a multiple-testing-deflated
bar — with full provenance and an automatic demotion watchdog.

This module is the productionized version of the manual research sessions
that produced (and twice correctly REJECTED) the meta-label filter, and that
promoted the 0.60/8 thresholds from the 8-year grid.

Constitution (the operator's rails — the lab can NEVER touch these):
  * Only keys in _TUNABLE_BOUNDS may be changed, only within their bounds.
  * Master switches, cash, notional/daily-loss caps, live gating, and the
    allowlist are structurally out of reach (not in the whitelist).
  * Every promotion is audited with the experiment id + evidence, records an
    expectation, and auto-reverts (demotion) if live performance since the
    promotion falls clearly below even the experiment's baseline.
  * When evidence is ambiguous the verdict is NOT promoted — same fail-closed
    posture as the risk gate.

Multiple-testing honesty: the promotion margin RISES with the number of
experiments ever run — margin(n) = 0.08 * sqrt(ln(1+n)/ln 2) Sharpe points of
mean fold improvement (a deliberately simple deflation heuristic, documented
rather than hidden: the more you search, the luckier your best result looks).

Python 3.9 compatible. Fail-open: nothing here can break a trading cycle.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import subprocess
import sys
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import aj_db
import aj_config

log = logging.getLogger("augur.aj_lab")

# ── the constitution: what the lab may tune, and how far ─────────────────────
_TUNABLE_BOUNDS: Dict[str, Tuple[float, float]] = {
    "buy_prob_threshold":   (0.50, 0.75),
    "sell_prob_threshold":  (0.30, 0.50),
    "min_edge_pct_pts":     (0.0, 10.0),
    "stop_loss_pct":        (3.0, 15.0),
    "take_profit_pct":      (5.0, 40.0),
    "trailing_stop_pct":    (0.0, 10.0),
    "time_stop_days":       (0, 30),
    "exit_cooldown_min":    (0, 240),
    "cross_sectional_top_n": (3, 25),
}
# Neighborhood step per key for the templated hypothesis generator.
_TUNABLE_STEPS: Dict[str, float] = {
    "buy_prob_threshold": 0.05, "sell_prob_threshold": 0.05,
    "min_edge_pct_pts": 2.0, "stop_loss_pct": 3.0, "take_profit_pct": 5.0,
    "trailing_stop_pct": 4.0, "time_stop_days": 10, "exit_cooldown_min": 60,
    "cross_sectional_top_n": 5,
}
_INT_TUNABLES = {"time_stop_days", "exit_cooldown_min", "cross_sectional_top_n"}

_DEFAULT_UNIVERSE = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,JPM,XOM,UNH"
_MIN_FOLD_TRADES = 8          # a fold with fewer closed trades can't be judged


def _margin(n_prior: int) -> float:
    """Promotion bar in mean-fold-Sharpe-delta, deflated for search volume:
    0.08 at zero prior experiments, rising strictly with every experiment ever
    judged (n=1 -> ~0.10, n=7 -> ~0.14, n=31 -> ~0.18). The more you search,
    the luckier your best result looks — so the bar must climb."""
    return 0.08 * math.sqrt(math.log(2 + max(0, n_prior)) / math.log(2.0))


def _now() -> str:
    return aj_db.utc_now_iso()


# ── hypothesis generation ─────────────────────────────────────────────────────

def propose(cfg: Optional[Dict[str, Any]] = None, max_new: int = 6) -> Dict[str, Any]:
    """Refill the experiment queue with templated parameter-neighborhood
    hypotheses around the LIVE config (dedup'd against anything queued,
    running, or already tested at the same value), plus optional LLM-proposed
    ones when the council is available. Never raises."""
    try:
        cfg = cfg or aj_config.get_config()
        seen = set()
        try:
            for r in aj_db.query(
                    "SELECT params_json, status, verdict FROM aj_lab_experiments"):
                seen.add(r.get("params_json"))
        except Exception:
            pass
        queued = 0
        cands: List[Dict[str, Any]] = []
        for key, (lo, hi) in _TUNABLE_BOUNDS.items():
            cur = cfg.get(key)
            try:
                cur_f = float(cur)
            except (TypeError, ValueError):
                continue
            step = _TUNABLE_STEPS.get(key, 0)
            for direction in (+1, -1):
                val = cur_f + direction * step
                val = max(lo, min(hi, val))
                if key in _INT_TUNABLES:
                    val = int(round(val))
                else:
                    val = round(val, 4)
                if abs(val - cur_f) < 1e-9:
                    continue
                cands.append({
                    "kind": "param",
                    "hypothesis": "{} {} -> {}".format(key, cur, val),
                    "rationale": "neighborhood probe around the live value "
                                 "({} step {:+g} within bounds [{:g},{:g}])".format(
                                     key, direction * step, lo, hi),
                    "params": {"set": {key: val}},
                })
        cands.extend(_propose_llm(cfg))
        for c in cands:
            if queued >= max_new:
                break
            pj = json.dumps(c["params"], sort_keys=True)
            if pj in seen:
                continue
            seen.add(pj)
            aj_db.insert("aj_lab_experiments", created_at=_now(),
                         status="queued", kind=c["kind"],
                         hypothesis=c["hypothesis"][:200],
                         rationale=c["rationale"][:400], params_json=pj)
            queued += 1
        return {"queued": queued}
    except Exception as e:
        log.debug("lab.propose failed", exc_info=True)
        return {"queued": 0, "error": str(e)[:120]}


def _propose_llm(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Council-as-research-director: one QUICK call summarizing recent
    experiment verdicts + trade stats into 1-2 structured hypotheses.
    Strictly validated against the constitution; fail-open to []."""
    try:
        if not cfg.get("council_enabled"):
            return []
        import aj_routing
        import aj_analytics
        from aj_schemas import extract_json
        recent = aj_db.query(
            "SELECT hypothesis, verdict FROM aj_lab_experiments "
            "WHERE status='done' ORDER BY id DESC LIMIT 8")
        stats = {}
        try:
            stats = aj_analytics.trade_stats() or {}
        except Exception:
            pass
        prompt = (
            "You direct a trading-agent research program. Tunable keys and "
            "bounds: {b}. Live values: {v}. Recent experiment verdicts: {r}. "
            "Live trade stats: {s}. Propose up to 2 parameter hypotheses as "
            "STRICT JSON: [{{\"key\":\"...\",\"value\":<num>,"
            "\"hypothesis\":\"<=25 words\"}}]").format(
                b=json.dumps(_TUNABLE_BOUNDS),
                v=json.dumps({k: cfg.get(k) for k in _TUNABLE_BOUNDS}),
                r=json.dumps([dict(r) for r in recent], default=str),
                s=json.dumps({k: stats.get(k) for k in
                              ("trades", "win_rate", "profit_factor")}))
        r = aj_routing.complete(prompt, role="lab.proposer",
                                sensitivity="public", max_tokens=300)
        if not (r and r.get("ok") and r.get("text")):
            return []
        d = extract_json(r["text"])
        items = d if isinstance(d, list) else d.get("hypotheses") or []
        out = []
        for it in items[:2]:
            key = str((it or {}).get("key") or "")
            if key not in _TUNABLE_BOUNDS:
                continue
            lo, hi = _TUNABLE_BOUNDS[key]
            try:
                val = float(it.get("value"))
            except (TypeError, ValueError):
                continue
            val = max(lo, min(hi, val))
            if key in _INT_TUNABLES:
                val = int(round(val))
            out.append({"kind": "llm",
                        "hypothesis": str(it.get("hypothesis") or
                                          "{} -> {}".format(key, val)),
                        "rationale": "council research director proposal "
                                     "(validated against the constitution)",
                        "params": {"set": {key: val}}})
        return out
    except Exception:
        log.debug("lab llm proposer skipped", exc_info=True)
        return []


# ── fold execution (replay twins) ─────────────────────────────────────────────

def _replay_dirs() -> Tuple[str, str]:
    import database as _db
    root = os.path.dirname(os.path.realpath(_db.DB_PATH))
    return (os.path.join(root, "data", "replays"),
            os.path.join(root, "data", "replay_cache"))


def _fold_windows(folds: int, fold_days: int) -> List[Tuple[str, str]]:
    """K sequential calendar windows ending ~3 days ago (cache freshness),
    oldest first."""
    end = aj_db.utc_now() - timedelta(days=3)
    out = []
    for i in range(folds - 1, -1, -1):
        w_end = end - timedelta(days=i * fold_days)
        w_start = w_end - timedelta(days=fold_days - 1)
        out.append((w_start.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d")))
    return out


def _run_replay_fold(sets: List[str], start: str, end: str, universe: str,
                     run_id: str, cash: float = 100000.0) -> Optional[Dict[str, Any]]:
    """One isolated replay; returns the metrics dict (result.json) or None.
    Module-level seam so tests stub it without subprocesses."""
    try:
        import aj_replay
        out_dir, cache_dir = _replay_dirs()
        lab_dir = os.path.join(out_dir, "lab")
        os.makedirs(lab_dir, exist_ok=True)
        prefix, env = aj_replay.spawn_cmd_env(run_id, lab_dir)
        cmd = prefix + ["run", "--run-id", run_id, "--out-dir", lab_dir,
                        "--cache-dir", cache_dir, "--symbols", universe,
                        "--start", start, "--end", end, "--cash", str(cash),
                        "--offline"]
        for s in sets:
            cmd += ["--set", s]
        p = subprocess.run(cmd, env=env, capture_output=True, text=True,
                           timeout=3600)
        if p.returncode != 0:
            log.warning("lab fold %s failed: %s", run_id, (p.stderr or "")[-400:])
            return None
        with open(os.path.join(lab_dir, run_id, "result.json")) as f:
            return json.load(f)
    except Exception:
        log.debug("lab fold %s errored", run_id, exc_info=True)
        return None


def _baseline_sets(cfg: Dict[str, Any]) -> List[str]:
    """The live values of every tunable, pinned explicitly so the baseline is
    reproducible and cacheable regardless of future live-config changes."""
    return ["{}={}".format(k, cfg.get(k)) for k in sorted(_TUNABLE_BOUNDS)
            if cfg.get(k) is not None]


def _fold_metrics(res: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not res:
        return None
    trades = (res.get("trades") or {}).get("trades")
    if trades is None or trades < _MIN_FOLD_TRADES or res.get("sharpe") is None:
        return None
    return {"sharpe": res.get("sharpe"),
            "return_pct": res.get("total_return_pct"),
            "alpha_pct": res.get("alpha_pct"),
            "max_drawdown_pct": res.get("max_drawdown_pct"),
            "trades": trades}


def _baseline_fold(sets: List[str], start: str, end: str, universe: str,
                   cash: float) -> Optional[Dict[str, Any]]:
    """Baseline folds are shared across experiments — cache them on disk keyed
    by (config, window, universe, cash) so each nightly experiment costs K
    candidate replays, not 2K."""
    key = hashlib.sha1(json.dumps([sorted(sets), start, end, universe, cash])
                       .encode()).hexdigest()[:16]
    out_dir, _ = _replay_dirs()
    cdir = os.path.join(out_dir, "lab", "baselines")
    os.makedirs(cdir, exist_ok=True)
    path = os.path.join(cdir, key + ".json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    res = _run_replay_fold(sets, start, end, universe, "base_" + key, cash)
    m = _fold_metrics(res)
    if m is not None:
        with open(path, "w") as f:
            json.dump(m, f)
    return m


# ── the experiment runner ─────────────────────────────────────────────────────

def run_next(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Pop the oldest queued experiment and judge it: K walk-forward folds,
    candidate-vs-baseline twins, deflated promotion bar. Applies the promotion
    (bounded, audited, expectation-recorded) when it wins. Never raises."""
    try:
        cfg = cfg or aj_config.get_config()
        rows = aj_db.query("SELECT * FROM aj_lab_experiments WHERE "
                           "status='queued' ORDER BY id ASC LIMIT 1")
        if not rows:
            return {"ran": False, "reason": "queue empty"}
        exp = rows[0]
        # CAS-claim so two runners can't take the same experiment.
        if not aj_db.update_if("aj_lab_experiments", exp["id"], "status",
                               "queued", status="running", started_at=_now()):
            return {"ran": False, "reason": "claim lost"}
        try:
            try:
                params = json.loads(exp.get("params_json") or "{}")
            except Exception:
                params = {}
            changes = (params.get("set") or {})
            verdict, evidence = _judge(cfg, changes)
            aj_db.update("aj_lab_experiments", exp["id"], status="done",
                         finished_at=_now(), verdict=verdict,
                         evidence_json=json.dumps(evidence, default=str))
        except Exception:
            # Release the claim: an exception between the CAS-claim and the
            # 'done' update would otherwise orphan the row as a phantom
            # 'running' forever (run_next only pops 'queued'; the dedup
            # 'seen' set would also block re-proposing that value). A crash
            # (process death) is handled by the reaper in run_due.
            try:
                aj_db.update_if("aj_lab_experiments", exp["id"], "status",
                                "running", status="queued")
            except Exception:
                pass
            raise
        aj_db.audit("lab", {"experiment_id": exp["id"],
                            "hypothesis": exp.get("hypothesis"),
                            "verdict": verdict,
                            "mean_delta": evidence.get("mean_delta"),
                            "margin": evidence.get("margin")}, actor="system")
        if verdict == "promoted":
            _apply_promotion(exp["id"], cfg, changes, evidence)
        return {"ran": True, "experiment_id": exp["id"],
                "hypothesis": exp.get("hypothesis"), "verdict": verdict,
                "evidence": evidence}
    except Exception as e:
        log.exception("lab.run_next failed")
        return {"ran": False, "reason": "error: {}".format(str(e)[:120])}


def _judge(cfg: Dict[str, Any], changes: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """K-fold twin evaluation of `changes` against the live baseline."""
    # constitution re-check before spending compute
    for k, v in changes.items():
        if k not in _TUNABLE_BOUNDS:
            return "rejected", {"reason": "key {} outside the constitution".format(k)}
        lo, hi = _TUNABLE_BOUNDS[k]
        try:
            if not (lo - 1e-9 <= float(v) <= hi + 1e-9):
                return "rejected", {"reason": "{}={} outside bounds".format(k, v)}
        except (TypeError, ValueError):
            return "rejected", {"reason": "non-numeric value for {}".format(k)}

    folds = max(2, int(cfg.get("lab_folds", 3) or 3))
    fold_days = max(60, int(cfg.get("lab_fold_days", 180) or 180))
    allow = [s for s in (cfg.get("symbol_allowlist") or []) if s]
    universe = ",".join(allow[:15]) if allow else _DEFAULT_UNIVERSE
    cash = float(cfg.get("paper_cash") or 100000) or 100000.0

    base_sets = _baseline_sets(cfg)
    cand_map = dict((s.split("=")[0], s) for s in base_sets)
    for k, v in changes.items():
        cand_map[k] = "{}={}".format(k, v)
    cand_sets = [cand_map[k] for k in sorted(cand_map)]

    n_prior = 0
    try:
        n_prior = int(aj_db.query("SELECT COUNT(*) AS n FROM aj_lab_experiments "
                                  "WHERE status='done'")[0]["n"])
    except Exception:
        pass
    margin = _margin(n_prior)

    fold_rows = []
    wins = 0
    deltas = []
    for i, (start, end) in enumerate(_fold_windows(folds, fold_days)):
        base = _baseline_fold(base_sets, start, end, universe, cash)
        cand = _fold_metrics(_run_replay_fold(
            cand_sets, start, end, universe,
            "exp{}_f{}".format(hashlib.sha1(json.dumps(
                [cand_sets, start, end]).encode()).hexdigest()[:10], i), cash))
        row = {"fold": i, "start": start, "end": end,
               "baseline": base, "candidate": cand}
        if base is None or cand is None:
            row["verdict"] = "inconclusive"
            fold_rows.append(row)
            continue
        delta = float(cand["sharpe"]) - float(base["sharpe"])
        row["delta_sharpe"] = round(delta, 3)
        row["verdict"] = "win" if delta > 0 else "loss"
        wins += 1 if delta > 0 else 0
        deltas.append(delta)
        fold_rows.append(row)

    evidence: Dict[str, Any] = {
        "folds": fold_rows, "wins": wins, "judged": len(deltas),
        "mean_delta": round(sum(deltas) / len(deltas), 3) if deltas else None,
        "margin": round(margin, 3), "n_prior_experiments": n_prior,
        "universe": universe, "fold_days": fold_days,
    }
    if len(deltas) < folds:      # any unjudgeable fold => no promotion
        return "inconclusive", evidence
    if wins >= folds - 1 and evidence["mean_delta"] is not None \
            and evidence["mean_delta"] >= margin:
        return "promoted", evidence
    return "rejected", evidence


def _apply_promotion(exp_id: int, cfg: Dict[str, Any],
                     changes: Dict[str, Any], evidence: Dict[str, Any]) -> None:
    """Bounded, audited config change + expectation record for the watchdog."""
    base_sharpes = [f["baseline"]["sharpe"] for f in evidence.get("folds", [])
                    if f.get("baseline")]
    expectation = {
        "mean_delta": evidence.get("mean_delta"),
        "baseline_sharpe_mean": (round(sum(base_sharpes) / len(base_sharpes), 3)
                                 if base_sharpes else None),
    }
    for k, v in changes.items():
        old = cfg.get(k)
        aj_config.set_config({k: v})
        # one ACTIVE promotion per key: older ones are superseded, and the
        # watchdog only polices the newest.
        try:
            for r in aj_db.query("SELECT id FROM aj_lab_promotions WHERE "
                                 "key=? AND status='active'", (k,)):
                aj_db.update("aj_lab_promotions", r["id"], status="superseded")
        except Exception:
            pass
        aj_db.insert("aj_lab_promotions", experiment_id=exp_id,
                     promoted_at=_now(), key=k, old_value=str(old),
                     new_value=str(v),
                     expectation_json=json.dumps(expectation))
        aj_db.audit("lab", {"kind": "promotion", "experiment_id": exp_id,
                            "key": k, "old": old, "new": v,
                            "expectation": expectation}, actor="system")
        log.info("lab PROMOTED %s: %s -> %s (exp #%s)", k, old, v, exp_id)


# ── the demotion watchdog ─────────────────────────────────────────────────────

def check_promotions(cfg: Optional[Dict[str, Any]] = None,
                     min_days: int = 15) -> Dict[str, Any]:
    """Auto-revert any active promotion whose LIVE performance since promotion
    falls clearly below even the experiment's baseline expectation.
    Conservative v1 rule: >= min_days of live equity history since the
    promotion AND live Sharpe < baseline_sharpe_mean - 0.5. Never raises."""
    out = {"checked": 0, "demoted": []}
    try:
        cfg = cfg or aj_config.get_config()
        actives = aj_db.query(
            "SELECT * FROM aj_lab_promotions WHERE status='active'")
        for p in actives:
            out["checked"] += 1
            try:
                exp = json.loads(p.get("expectation_json") or "{}")
            except Exception:
                exp = {}
            base_sharpe = exp.get("baseline_sharpe_mean")
            if base_sharpe is None:
                continue
            # aj_equity.equity_usd is CUMULATIVE P&L written once per CYCLE
            # (N rows/day under auto-run), NOT an account-equity daily series.
            # Judge on one point per DISTINCT trading date (latest row per
            # date, mirroring aj_analytics.equity_curve) so min_days means
            # days, not cycles — and convert P&L to equity with a capital
            # base (same recipe as aj_alpha.current_drawdown_pct), since
            # %-returns over raw cumulative P&L are meaningless (they made
            # live_sharpe essentially random, and negative P&L rows were
            # silently dropped by the >0 filter, hiding bad promotions).
            rows = aj_db.query(
                "SELECT date, equity_usd FROM aj_equity WHERE ts >= ? AND id IN "
                "(SELECT MAX(id) FROM aj_equity GROUP BY date) ORDER BY date ASC",
                (p["promoted_at"],))
            try:
                base_cap = float(cfg.get("compound_base_equity_usd") or 0)
            except (TypeError, ValueError):
                base_cap = 0.0
            if base_cap <= 0:
                try:
                    base_cap = float(cfg.get("paper_cash") or 0)
                except (TypeError, ValueError):
                    base_cap = 0.0
            if base_cap <= 0:
                continue    # no capital base -> % returns undefined; fail open
            vals = [base_cap + float(r["equity_usd"]) for r in rows
                    if r.get("equity_usd") is not None]
            if len(vals) < min_days:    # < min_days DISTINCT live dates
                continue
            rets = [vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals))
                    if vals[i - 1] > 0]
            if len(rets) < min_days - 1:
                continue
            m = sum(rets) / len(rets)
            sd = (sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
            live_sharpe = (m / sd * (252 ** 0.5)) if sd > 0 else 0.0
            if live_sharpe < float(base_sharpe) - 0.5:
                aj_config.set_config({p["key"]: p["old_value"]})
                aj_db.update("aj_lab_promotions", p["id"], status="demoted",
                             demoted_at=_now(),
                             demote_reason="live sharpe {:.2f} < baseline {:.2f} - 0.5".format(
                                 live_sharpe, float(base_sharpe)))
                aj_db.audit("lab", {"kind": "demotion", "key": p["key"],
                                    "reverted_to": p["old_value"],
                                    "live_sharpe": round(live_sharpe, 2),
                                    "baseline_sharpe": base_sharpe},
                            actor="system")
                log.warning("lab DEMOTED %s back to %s (live sharpe %.2f)",
                            p["key"], p["old_value"], live_sharpe)
                out["demoted"].append(p["key"])
    except Exception:
        log.debug("check_promotions failed", exc_info=True)
    return out


# ── nightly entry point + status ──────────────────────────────────────────────

def run_due(cfg: Optional[Dict[str, Any]] = None, budget: int = 1) -> Dict[str, Any]:
    """The scheduler's nightly call: police active promotions, refill the
    queue if empty, then run up to `budget` experiments. Never raises."""
    try:
        cfg = cfg or aj_config.get_config()
        if not cfg.get("lab_enabled"):
            return {"ran": False, "reason": "lab disabled"}
        # Reaper: a process killed mid-experiment leaves its row 'running'
        # forever (the in-process requeue in run_next can't fire). Requeue
        # anything claimed >12h ago — a legitimate run is bounded well under
        # that (folds subprocess-timeout at 1h each). CAS so we never race a
        # runner that finishes while we look.
        try:
            cutoff = (aj_db.utc_now() - timedelta(hours=12)).replace(
                microsecond=0).isoformat()
            for r in aj_db.query("SELECT id, started_at FROM aj_lab_experiments "
                                 "WHERE status='running'"):
                if (r.get("started_at") or "") < cutoff:
                    if aj_db.update_if("aj_lab_experiments", r["id"], "status",
                                       "running", status="queued"):
                        log.warning("lab reaped stale running experiment #%s "
                                    "(claimed %s) back to queued",
                                    r["id"], r.get("started_at"))
        except Exception:
            log.debug("lab reaper failed", exc_info=True)
        watch = check_promotions(cfg)
        q = aj_db.query("SELECT COUNT(*) AS n FROM aj_lab_experiments "
                        "WHERE status='queued'")
        if int(q[0]["n"]) == 0:
            propose(cfg)
        results = []
        for _ in range(max(1, int(budget))):
            r = run_next(cfg)
            results.append(r)
            if not r.get("ran"):
                break
        return {"ran": any(r.get("ran") for r in results),
                "watchdog": watch, "experiments": results}
    except Exception as e:
        log.exception("lab.run_due failed")
        return {"ran": False, "reason": "error: {}".format(str(e)[:120])}


def status(limit: int = 20) -> Dict[str, Any]:
    """Queue/ledger snapshot for the CLI, API, and Replay Lab panel."""
    try:
        queued = aj_db.query("SELECT id, created_at, kind, hypothesis FROM "
                             "aj_lab_experiments WHERE status IN "
                             "('queued','running') ORDER BY id ASC LIMIT ?",
                             (limit,))
        done = aj_db.query("SELECT id, finished_at, kind, hypothesis, verdict, "
                           "evidence_json FROM aj_lab_experiments WHERE "
                           "status='done' ORDER BY id DESC LIMIT ?", (limit,))
        for d in done:
            try:
                ev = json.loads(d.pop("evidence_json") or "{}")
                d["mean_delta"] = ev.get("mean_delta")
                d["wins"] = ev.get("wins")
                d["margin"] = ev.get("margin")
            except Exception:
                d.pop("evidence_json", None)
        promos = aj_db.query("SELECT * FROM aj_lab_promotions "
                             "ORDER BY id DESC LIMIT ?", (limit,))
        n_done = aj_db.query("SELECT COUNT(*) AS n FROM aj_lab_experiments "
                             "WHERE status='done'")[0]["n"]
        return {"enabled": bool(aj_config.get_config().get("lab_enabled")),
                "queue": queued, "recent": done, "promotions": promos,
                "experiments_total": int(n_done),
                "current_margin": round(_margin(int(n_done)), 3)}
    except Exception as e:
        return {"error": str(e)[:120]}
