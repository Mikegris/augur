"""AJTA — autonomous-operation layer (enhancements 21-25).

The machinery that lets the agent run itself: a market-hours scheduler, a
self-diagnostic health monitor that can pull its own kill switch, performance-
triggered risk-preset escalation, an end-of-day reflection journal, and a
pre-market opportunity briefing.

All opt-in and fail-safe: with the 100x flags off these functions are no-ops or
pure reads. Anything that could place trades still routes through the fail-closed
risk gate; the only autonomous *mutation* of control state is health_autohalt's
kill switch (a strictly risk-REDUCING action) and the opt-in preset escalation
(which never touches master switches or the allowlist).
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import date as _date, timedelta
from typing import Any, Dict, List, Optional

import aj_db
import aj_config

log = logging.getLogger("augur.aj_autonomy")

_LAST_RUN_KEY = "__aj_last_auto_run"
_REFLECTION_KEY = "__aj_reflection"        # + _YYYY-MM-DD
_BRIEFING_KEY = "__aj_briefing"
_ESCALATION_KEY = "__aj_preset_level"
_BACKOFF_KEY = "__aj_sched_backoff"        # crash-backoff surfacing (UI/health)


def _et_date() -> str:
    """The ET trading date. Journal keys must roll with the ET session — a
    UTC key means a 19:00-20:00 ET (EST) cycle files today's reflection under
    TOMORROW'S date, permanently suppressing tomorrow's real entry (first-write
    -wins) while today's own key is never written."""
    try:
        from zoneinfo import ZoneInfo
        return aj_db.utc_now().astimezone(
            ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return aj_db.utc_now().strftime("%Y-%m-%d")

# health_check audit-chain cadence: full re-scan every Nth check (catches a
# tampered prefix the quick tail-verify would miss), quick in between.
_FULL_CHAIN_EVERY = 20
_health_chain_counter = 0


# ════════════════════════════════════════════════════════════════════════════
#  21 — CONTINUOUS AUTO-RUN SCHEDULER
# ════════════════════════════════════════════════════════════════════════════

def _last_auto_run():
    raw = aj_db.get_setting_raw(_LAST_RUN_KEY)
    return aj_db.parse_iso(raw) if raw else None


def minutes_since_last_run() -> Optional[float]:
    last = _last_auto_run()
    if not last:
        return None
    return (aj_db.utc_now() - last).total_seconds() / 60.0


def _clock_slot(dt, interval: int) -> int:
    """Which interval bucket a UTC instant falls in on a fixed global grid
    anchored to the epoch. Two runs in the same slot => same boundary already
    served. For intervals that divide 60 (5/10/15/30) the UTC grid lines up with
    ET clock marks because the ET offset is a whole number of hours."""
    return int(dt.timestamp() // 60) // max(1, interval)


def due_to_run(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Whether an automatic cycle is due now: auto-run on, a tradable session,
    and the interval satisfied.

    Two cadence modes (auto_run_align_to_clock, default True):
      • clock-aligned — fire once per wall-clock boundary (:00, :10, :20…), so
        runs land on the minute you expect regardless of cycle duration; no drift.
      • last-run+interval — fire `interval` minutes after the previous run
        COMPLETED (legacy behavior; drifts by the cycle's own runtime)."""
    cfg = cfg or aj_config.get_config()
    if not cfg.get("auto_run_enabled"):
        return {"due": False, "reason": "auto_run disabled"}
    session = aj_db.market_session()
    wl = cfg.get("session_whitelist")
    if wl is None:
        wl = ["regular"]
    if session not in wl:
        return {"due": False, "reason": "session {} not tradable".format(session)}
    interval = max(1, int(cfg.get("auto_run_interval_min") or 30))
    mins = minutes_since_last_run()
    aligned = bool(cfg.get("auto_run_align_to_clock", True))

    if aligned:
        last = _last_auto_run()
        now = aj_db.utc_now()
        cur_slot = _clock_slot(now, interval)
        # Not yet served this boundary? (no prior run, or prior run was an
        # earlier slot) -> due. Same slot already ran -> wait for the next mark.
        if last is not None and cur_slot <= _clock_slot(last, interval):
            return {"due": False,
                    "reason": "waiting for next :{:02d}m clock mark (interval {}m)".format(
                        ((cur_slot + 1) * interval) % 60, interval),
                    "minutes_since": round(mins, 1) if mins is not None else None,
                    "aligned": True}
        return {"due": True, "reason": "due (clock-aligned)", "minutes_since": mins,
                "session": session, "aligned": True}

    if mins is not None and mins < interval:
        return {"due": False, "reason": "only {:.0f}m since last run (interval {}m)".format(mins, interval),
                "minutes_since": round(mins, 1), "aligned": False}
    return {"due": True, "reason": "due", "minutes_since": mins, "session": session, "aligned": False}


def _claim_run_slot(prev_raw: Optional[str], stamp: str) -> bool:
    """Atomically claim the auto-run slot by CAS-advancing _LAST_RUN_KEY from the
    exact value `due_to_run` observed (`prev_raw`) to `stamp`. Returns True iff
    THIS caller won the claim. Closes the due-check->stamp race: two ticks that
    both saw 'due' off the same prev value can't both run — only the one that
    flips the stamp proceeds; the loser is told it's no longer due."""
    import database as _db
    try:
        with _db._write_lock:
            conn = _db.get_conn()
            if prev_raw is None:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
                    (_LAST_RUN_KEY, stamp))
                won = cur.rowcount == 1
            else:
                cur = conn.execute(
                    "UPDATE settings SET value=? WHERE key=? AND value=?",
                    (stamp, _LAST_RUN_KEY, prev_raw))
                won = cur.rowcount == 1
            conn.commit()
        if won:
            try:
                _db._invalidate_settings_cache()
            except Exception:
                pass
        return won
    except Exception:
        log.debug("_claim_run_slot failed", exc_info=True)
        return False


# ── crash backoff (scheduler self-protection) ────────────────────────────────
# A cycle that RAN and crashed (ok=False WITH a cycle_id) keeps its interval
# slot consumed (see run_scheduled), so the next attempt is one interval away —
# but a persistent fault (dead forecast dep, broker API change) would still
# re-crash a full cycle EVERY interval, forever, silently. Backoff makes the
# retry cadence exponential (interval * 2^failures, capped at 6h) and SURFACES
# the condition (settings JSON + log.warning + audit alert) so the UI/health
# systems can show "auto-run is broken" instead of it looking merely idle.
# State is process-local (the daemon owns it); the settings key is surfacing.

def _backoff_remaining() -> Optional[Dict[str, Any]]:
    """The active crash-backoff window, or None when clear/expired."""
    with _sched_state_lock:
        n = int(_sched_state.get("consec_failures") or 0)
        until = _sched_state.get("backoff_until")
    if n <= 0 or not until:
        return None
    dt = aj_db.parse_iso(until)
    if dt is None or aj_db.utc_now() >= dt:
        return None
    return {"consec_failures": n, "next_retry": until}


def _note_cycle_crash(cfg: Dict[str, Any], reason: Any) -> None:
    """Escalate the backoff after a crashed cycle and surface it. One
    escalation == one crashed cycle, so the warning/audit fire once per
    escalation, not once per 30s tick. Never raises (scheduler hot path)."""
    try:
        interval = max(1, int(cfg.get("auto_run_interval_min") or 30))
        with _sched_state_lock:
            n = int(_sched_state.get("consec_failures") or 0) + 1
            delay_min = min(interval * (2 ** n), _BACKOFF_CAP_MIN)
            until = (aj_db.utc_now() + timedelta(minutes=delay_min)).isoformat()
            _sched_state["consec_failures"] = n
            _sched_state["backoff_until"] = until
        state = {"consecutive_failures": n, "next_retry": until,
                 "delay_min": delay_min, "reason": str(reason or "")[:200],
                 "updated": aj_db.utc_now_iso()}
        try:
            aj_db.set_setting_raw(_BACKOFF_KEY, json.dumps(state))
        except Exception:
            log.debug("backoff state persist failed", exc_info=True)
        log.warning("scheduler crash backoff: %d consecutive failed cycle(s); "
                    "suppressing auto-runs for %dm (until %s)", n, delay_min, until)
        try:
            aj_db.audit("alert", {"kind": "scheduler_backoff", **state})
        except Exception:
            log.debug("backoff audit failed", exc_info=True)
    except Exception:
        log.exception("_note_cycle_crash failed")


def _note_cycle_success() -> None:
    """Reset the crash backoff after a successful cycle (and clear the
    surfaced settings state so the UI stops showing a stale alert)."""
    with _sched_state_lock:
        had = int(_sched_state.get("consec_failures") or 0)
        _sched_state["consec_failures"] = 0
        _sched_state["backoff_until"] = None
    if had:
        try:
            aj_db.set_setting_raw(_BACKOFF_KEY, "")
        except Exception:
            log.debug("backoff state clear failed", exc_info=True)
        log.info("scheduler crash backoff cleared after a successful cycle "
                 "(was %d consecutive failures)", had)


def run_scheduled(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run one cycle iff due. Stamps the last-run time on success. This is the
    function an external timer/cron ticks; it never raises."""
    cfg = cfg or aj_config.get_config()
    # Crash backoff FIRST — checked here (the actuator) rather than in
    # due_to_run so the pure interval policy (and scheduler_status's due_now
    # read) stays untouched; the suppression is a property of the daemon.
    bo = _backoff_remaining()
    if bo:
        return {"ran": False,
                "reason": "crash backoff: {} consecutive failure(s), retry after {}".format(
                    bo["consec_failures"], bo["next_retry"]),
                "backoff": bo}
    gate = due_to_run(cfg)
    if not gate.get("due"):
        return {"ran": False, "reason": gate.get("reason")}
    # health gate FIRST — never auto-run into a known-bad state (22), and never
    # consume/stamp the interval slot when we're not going to run anyway.
    if cfg.get("health_autohalt"):
        hc = health_check(cfg)
        if hc.get("halted"):
            return {"ran": False, "reason": "health auto-halt: " + hc.get("summary", "")}
    # CLAIM the slot before running: CAS-advance the last-run stamp from the
    # value due_to_run just observed. The winner runs; a racing tick that saw the
    # same prior value loses the CAS and bows out. This both serializes ticks and
    # stamps the interval clock atomically with the due decision (no due->stamp
    # gap during which a second tick could re-fire the same interval).
    prev_raw = aj_db.get_setting_raw(_LAST_RUN_KEY)
    stamp = aj_db.utc_now_iso()
    if not _claim_run_slot(prev_raw, stamp):
        return {"ran": False, "reason": "another tick claimed this interval"}
    try:
        import aj_operator
        # run_once handles post-cycle autonomy (escalation/reflection/briefing).
        result = aj_operator.run_once("paper")
        if not result.get("ok"):
            # RESTORE the prior stamp ONLY when no cycle actually ran (lock
            # contention — no cycle_id in the result). ok=False ALSO covers a
            # CRASHED cycle that genuinely ran (exits/proposals may have
            # executed before the fault); restoring its stamp would make the
            # 30s scheduler tick re-fire a FULL trading cycle every tick for
            # as long as the fault persists, instead of once per interval — a
            # crashed cycle keeps its interval slot consumed.
            if not result.get("cycle_id"):
                try:
                    if prev_raw is not None:
                        aj_db.set_setting_raw(_LAST_RUN_KEY, prev_raw)
                    else:
                        # First-ever run: there was no prior stamp, so we INSERTed it.
                        # Remove it (don't leave the slot marked done) so the next tick
                        # retries instead of waiting a full interval.
                        aj_db.set_setting_raw(_LAST_RUN_KEY, "")
                except Exception:
                    log.debug("run_scheduled: stamp restore failed", exc_info=True)
            else:
                # A REAL crashed cycle (it ran — has a cycle_id), not lock
                # contention: escalate the exponential backoff so a persistent
                # fault doesn't re-crash a full cycle every single interval.
                _note_cycle_crash(cfg, result.get("reason"))
            return {"ran": False, "reason": result.get("reason") or "cycle did not run",
                    "result": result}
        # success — the claimed stamp already marks this interval done.
        _note_cycle_success()
        return {"ran": True, "cycle": result.get("cycle_id"),
                "proposals": len(result.get("proposals") or []), "result": result}
    except Exception as e:
        log.exception("run_scheduled failed")
        return {"ran": False, "reason": "error: {}".format(str(e)[:120])}


# ── continuous scheduler thread (the timer that ticks run_scheduled) ──────────
#
# aj_autonomy provides the *policy* (due_to_run / run_scheduled); this is the
# *driver* that was previously missing — a single daemon thread that wakes every
# _SCHED_TICK_SECONDS and asks run_scheduled() to run a cycle iff due. The thread
# is intentionally dumb: ALL gating (auto_run_enabled, market session, interval,
# health auto-halt, kill switch) lives in run_scheduled/run_once and is re-read
# from config every tick — so toggling auto-run or changing the interval in the
# UI takes effect within one tick with no restart. Mirrors cache_warmer's
# idempotent-start + daemon-thread pattern.

_SCHED_TICK_SECONDS = 30      # wake cadence; the real interval gate is in cfg
_SCHED_BOOT_DELAY = 20        # let the app/cache warm up before the first tick

_sched_thread: Optional[threading.Thread] = None
_sched_started = False
_sched_lock = threading.Lock()
_sched_state: Dict[str, Any] = {
    "last_tick": None, "last_fire": None, "last_reason": None,
    "ticks": 0, "fires": 0, "errors": 0,
    # crash backoff: consecutive CRASHED cycles (ran + failed, not lock
    # contention) and the ISO timestamp before which auto-runs are suppressed.
    "consec_failures": 0, "backoff_until": None,
}
_sched_state_lock = threading.Lock()

# Exponential-backoff ceiling after consecutive crashed cycles. Without a cap a
# long outage (e.g. a broken forecast dependency overnight) would push the next
# retry out for days; 6h means the agent probes at most 4x/day while broken.
_BACKOFF_CAP_MIN = 360  # 6 hours


def _scheduler_tick() -> Dict[str, Any]:
    """One scheduler wake: run a cycle iff due. NEVER raises. Factored out of the
    loop so it is unit-testable without spawning a thread."""
    try:
        res = run_scheduled()
    except Exception as e:                       # defense in depth — run_scheduled
        log.exception("scheduler tick failed")   # already swallows, but never die
        res = {"ran": False, "reason": "tick error: {}".format(str(e)[:120])}
    now = aj_db.utc_now_iso()
    reason = str(res.get("reason") or "")
    with _sched_state_lock:
        _sched_state["ticks"] += 1
        _sched_state["last_tick"] = now
        _sched_state["last_reason"] = res.get("reason") if not res.get("ran") else "ran"
        if res.get("ran"):
            _sched_state["fires"] += 1
            _sched_state["last_fire"] = now
        elif reason.startswith(("error", "tick error")):
            _sched_state["errors"] += 1
    if res.get("ran"):
        log.info("scheduler fired a cycle: %s (%s proposals)",
                 res.get("cycle"), res.get("proposals"))
    else:
        log.debug("scheduler idle: %s", reason)
    return res


def _scheduler_loop() -> None:
    # Boot delay so the first tick doesn't race app/cache warm-up.
    time.sleep(_SCHED_BOOT_DELAY)
    log.info("aj scheduler loop running (tick=%ss)", _SCHED_TICK_SECONDS)
    while True:
        _scheduler_tick()
        _maybe_nightly_counterfactual()
        _maybe_nightly_lab()
        time.sleep(_SCHED_TICK_SECONDS)


# ── nightly counterfactual (Replay Lab evidence loop) ─────────────────────────
# Once per ET evening, replay the recent past under the LIVE config and its
# neighbors in ISOLATED subprocesses (aj_replay counterfactual). Results land
# in data/replays and surface in the Insights Replay Lab panel next morning —
# config drift becomes a rolling measurement instead of a one-shot grid.
# In-app (not an OS crontab) so it runs wherever the app runs, gated by the
# nightly_counterfactual config toggle, and can never touch the live book:
# every child process binds its own throwaway DB.

_CF_STAMP_KEY = "__aj_last_counterfactual"
_CF_BASKET = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,JPM,XOM,UNH"
_cf_thread: Optional[threading.Thread] = None


def _replays_base_dirs() -> tuple:
    """(out_dir, cache_dir) beside the RESOLVED live DB — reachable from both
    the source checkout and the frozen desktop bundle (whose CWD/__file__ are
    not the repo)."""
    import os
    import database as _db
    root = os.path.dirname(os.path.realpath(_db.DB_PATH))
    return (os.path.join(root, "data", "replays"),
            os.path.join(root, "data", "replay_cache"))


def _maybe_nightly_counterfactual() -> bool:
    """Fire the nightly counterfactual once per ET day after the evening cutoff.
    Never raises; the work runs on a daemon worker thread so a slow grid can
    never stall the trading scheduler's tick."""
    global _cf_thread
    try:
        cfg = aj_config.get_config()
        if not cfg.get("nightly_counterfactual"):
            return False
        if _cf_thread is not None and _cf_thread.is_alive():
            return False
        try:
            from zoneinfo import ZoneInfo
            now_et = aj_db.utc_now().astimezone(ZoneInfo("America/New_York"))
        except Exception:
            now_et = aj_db.utc_now()
        if now_et.hour < 21:      # after the extended session is done
            return False
        today = now_et.strftime("%Y-%m-%d")
        if str(aj_db.get_setting_raw(_CF_STAMP_KEY) or "") == today:
            return False
        aj_db.set_setting_raw(_CF_STAMP_KEY, today)   # claim before the slow work

        allow = [s for s in (cfg.get("symbol_allowlist") or []) if s]
        syms = ",".join(allow[:15]) if allow else _CF_BASKET

        def _work():
            import os
            import subprocess
            import sys as _sys
            try:
                out_dir, cache_dir = _replays_base_dirs()
                os.makedirs(out_dir, exist_ok=True)
                env = dict(os.environ)
                env["PYTHONPATH"] = os.pathsep.join(
                    p if p else os.getcwd() for p in _sys.path)
                log_path = os.path.join(out_dir, "nightly.log")
                with open(log_path, "a") as lf:
                    lf.write("\n-- nightly counterfactual {} --\n".format(today))
                    lf.flush()
                    # 1) refresh the bar cache (the only network step)
                    subprocess.run(
                        [_sys.executable, "-m", "aj_replay", "build-cache",
                         "--symbols", syms + ",SPY,^VIX", "--period", "2y",
                         "--refresh", "--cache-dir", cache_dir],
                        env=env, stdout=lf, stderr=subprocess.STDOUT, timeout=1800)
                    # 2) live-config ± neighbors over the last 30 sessions
                    subprocess.run(
                        [_sys.executable, "-m", "aj_replay", "counterfactual",
                         "--days", "30", "--symbols", syms, "--offline",
                         "--max-cells", "8", "--out-dir", out_dir,
                         "--cache-dir", cache_dir,
                         "--grid-id", "cf_{}".format(today.replace("-", ""))],
                        env=env, stdout=lf, stderr=subprocess.STDOUT, timeout=5400)
                aj_db.audit("alert", {"kind": "nightly_counterfactual",
                                      "date": today, "symbols": syms},
                            actor="system")
                log.info("nightly counterfactual complete (%s)", today)
            except Exception:
                log.exception("nightly counterfactual failed")

        _cf_thread = threading.Thread(target=_work, daemon=True,
                                      name="aj-nightly-counterfactual")
        _cf_thread.start()
        return True
    except Exception:
        log.debug("nightly counterfactual gate failed", exc_info=True)
        return False


# ── nightly research scientist (aj_lab) ───────────────────────────────────────

_LAB_STAMP_KEY = "__aj_last_lab_run"
_lab_thread: Optional[threading.Thread] = None


def _maybe_nightly_lab() -> bool:
    """Run the Research Scientist once per ET evening (after the
    counterfactual window opens): police active promotions, then run ONE
    queued experiment (K-fold twin replays in isolated subprocesses). Gated
    by lab_enabled; a slow experiment runs on a daemon worker so the trading
    scheduler's tick never stalls. Never raises."""
    global _lab_thread
    try:
        cfg = aj_config.get_config()
        if not cfg.get("lab_enabled"):
            return False
        if _lab_thread is not None and _lab_thread.is_alive():
            return False
        try:
            from zoneinfo import ZoneInfo
            now_et = aj_db.utc_now().astimezone(ZoneInfo("America/New_York"))
        except Exception:
            now_et = aj_db.utc_now()
        if now_et.hour < 21:
            return False
        today = now_et.strftime("%Y-%m-%d")
        if str(aj_db.get_setting_raw(_LAB_STAMP_KEY) or "") == today:
            return False
        aj_db.set_setting_raw(_LAB_STAMP_KEY, today)

        def _work():
            try:
                import aj_lab
                res = aj_lab.run_due(budget=1)
                log.info("nightly lab: %s", json.dumps(
                    {k: res.get(k) for k in ("ran", "watchdog")}, default=str))
            except Exception:
                log.exception("nightly lab failed")

        _lab_thread = threading.Thread(target=_work, daemon=True,
                                       name="aj-nightly-lab")
        _lab_thread.start()
        return True
    except Exception:
        log.debug("nightly lab gate failed", exc_info=True)
        return False


def start_scheduler() -> bool:
    """Spawn the auto-run scheduler thread once. Idempotent — safe to call from
    multiple boot paths (app.py, desktop.py). Returns True if it started the
    thread on this call, False if one was already running.

    Starting the thread does NOT mean cycles will run: run_scheduled self-gates
    on auto_run_enabled (default OFF), market session, and interval, so the
    thread is cheap (one market_session() read per tick) until the operator
    enables auto-run in the Config tab."""
    global _sched_thread, _sched_started
    with _sched_lock:
        # Respawn if the thread was started before but has since DIED (an
        # unexpected crash of _scheduler_loop): treating `started` as permanent
        # would leave auto-run silently dead forever. Only refuse when a live
        # thread is actually running.
        if _sched_started and _sched_thread is not None and _sched_thread.is_alive():
            return False
        _sched_thread = threading.Thread(
            target=_scheduler_loop, daemon=True, name="aj-autorun-scheduler")
        _sched_thread.start()
        _sched_started = True
    log.info("aj auto-run scheduler thread launched")
    return True


def scheduler_status(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Live scheduler health for /api/aj/status and the Config tab — lets the
    operator confirm the timer is actually running and see the next-due verdict."""
    cfg = cfg or aj_config.get_config()
    alive = bool(_sched_thread and _sched_thread.is_alive())
    mins = minutes_since_last_run()
    with _sched_state_lock:
        snap = dict(_sched_state)
    interval = int(cfg.get("auto_run_interval_min") or 30)
    aligned = bool(cfg.get("auto_run_align_to_clock", True))
    # human-readable next expected fire
    next_eta = None
    try:
        now = aj_db.utc_now()
        if aligned:
            nxt = (_clock_slot(now, interval) + 1) * interval     # epoch-minutes of next mark
            next_eta = round(nxt - now.timestamp() / 60.0, 1)     # minutes from now to next mark
        elif mins is not None:
            next_eta = round(max(0.0, interval - mins), 1)
    except Exception:
        pass
    return {
        "thread_running": alive,
        "started": _sched_started,
        "auto_run_enabled": bool(cfg.get("auto_run_enabled")),
        "interval_min": interval,
        "cadence": "clock-aligned" if aligned else "last-run+interval",
        "next_fire_in_min": next_eta,
        "tick_seconds": _SCHED_TICK_SECONDS,
        "minutes_since_last_run": (round(mins, 1) if mins is not None else None),
        "due_now": due_to_run(cfg),
        **snap,
    }


# ════════════════════════════════════════════════════════════════════════════
#  22 — SELF-DIAGNOSTIC HEALTH MONITOR (can pull the kill switch)
# ════════════════════════════════════════════════════════════════════════════

def health_check(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Inspect the agent's own metrics for anomalies. When health_autohalt is on
    and a CRITICAL issue is found, engage the kill switch (risk-reducing)."""
    cfg = cfg or aj_config.get_config()
    issues: List[Dict[str, str]] = []
    try:
        import aj_metrics
        orders = aj_metrics.order_stats() or {}
        recon = aj_metrics.recon_stats() or {}
        # fill-rate collapse (only meaningful with a few submissions)
        fr = orders.get("fill_rate")
        if fr is not None and orders.get("total", 0) >= 6 and fr < 0.4:
            issues.append({"level": "critical", "msg": "fill-rate {:.0%} collapsed".format(fr)})
        # unknown-state orders — broker truth lost
        by_state = orders.get("by_state") or {}
        if int(by_state.get("unknown", 0)) > 0:
            issues.append({"level": "critical", "msg": "{} unknown-state orders".format(by_state["unknown"])})
        # reconciliation divergence — paper book vs broker disagree
        if int(recon.get("divergence", 0)) > 0:
            issues.append({"level": "critical", "msg": "{} unresolved reconciliation divergences".format(recon["divergence"])})
        # audit chain broken — tamper / corruption. The hot path uses quick=True
        # (verify only the tail), but that can't catch an edit INSIDE the already-
        # verified prefix. So the autonomous monitor does a FULL re-scan every
        # _FULL_CHAIN_EVERY checks (and on the first check), quick in between —
        # bounding how long out-of-band tampering can go undetected.
        global _health_chain_counter
        _health_chain_counter += 1
        full = (_health_chain_counter % _FULL_CHAIN_EVERY) == 1
        chain = aj_db.verify_audit_chain(quick=not full)
        if not chain.get("ok"):
            issues.append({"level": "critical", "msg": "audit chain broken at {}".format(chain.get("broken_at"))})
    except Exception:
        log.exception("health_check metrics failed")
        issues.append({"level": "warning", "msg": "health metrics unavailable"})

    critical = [i for i in issues if i["level"] == "critical"]
    summary = "; ".join(i["msg"] for i in issues) or "healthy"
    out = {"ok": not critical, "issues": issues, "summary": summary, "halted": False}
    if critical and cfg.get("health_autohalt"):
        try:
            import aj_risk
            if not aj_risk.is_halted():
                aj_risk.kill_switch("health auto-halt: " + summary[:160])
                out["halted"] = True
        except Exception:
            log.exception("health auto-halt kill_switch failed")
    return out


# ════════════════════════════════════════════════════════════════════════════
#  23 — PERFORMANCE-TRIGGERED PRESET ESCALATION
# ════════════════════════════════════════════════════════════════════════════

_PRESET_LADDER = ["conservative", "moderate", "aggressive"]


def _current_level() -> int:
    raw = aj_db.get_setting_raw(_ESCALATION_KEY)
    try:
        return max(0, min(2, int(float(raw)))) if raw is not None else 1
    except (TypeError, ValueError):
        return 1


def _cas_escalation_level(from_level: int, to_level: int) -> bool:
    """Compare-and-set the preset-level key from `from_level` to `to_level`.
    Returns True iff this caller won. Handles the absent-key case (the level
    defaults to 1 in _current_level, so an absent key is treated as level 1 and
    claimed via INSERT)."""
    import database as _db
    try:
        with _db._write_lock:
            conn = _db.get_conn()
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?", (_ESCALATION_KEY,)).fetchone()
            if row is None:
                # absent => effective level 1; only valid to move off 1.
                if from_level != 1:
                    return False
                cur = conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
                    (_ESCALATION_KEY, str(to_level)))
                won = cur.rowcount == 1
            else:
                cur = conn.execute(
                    "UPDATE settings SET value=? WHERE key=? AND value=?",
                    (str(to_level), _ESCALATION_KEY, row["value"]))
                # only count it ours if the stored value really was from_level
                try:
                    stored = max(0, min(2, int(float(row["value"]))))
                except (TypeError, ValueError):
                    stored = 1
                won = cur.rowcount == 1 and stored == from_level
            conn.commit()
        if won:
            try:
                _db._invalidate_settings_cache()
            except Exception:
                pass
        return won
    except Exception:
        log.debug("_cas_escalation_level failed", exc_info=True)
        return False


def maybe_escalate(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Move the risk preset up the ladder on proven performance, down on a
    drawdown. Earns its way up: positive Sharpe-like + win-rate ≥ 55% over
    enough trades escalates; a deep drawdown de-escalates. Never touches master
    switches or the allowlist (apply_preset only changes risk/strategy knobs)."""
    cfg = cfg or aj_config.get_config()
    if not cfg.get("auto_preset_escalation"):
        return {"changed": False, "reason": "disabled"}
    try:
        import aj_analytics, aj_alpha
        stats = aj_analytics.trade_stats() or {}
        sharpe = (aj_analytics.sharpe_like() or {}).get("sharpe")
        dd = aj_alpha.current_drawdown_pct()
        n = int(stats.get("trades") or 0)
        wr = stats.get("win_rate")
        level = _current_level()
        new_level = level
        reason = "hold"

        # de-escalate first (safety): a >15% drawdown steps down
        if dd >= 15.0 and level > 0:
            new_level = level - 1
            reason = "drawdown {:.0f}% — step down".format(dd)
        # escalate: proven edge and not in a drawdown
        elif (n >= 10 and wr is not None and wr >= 0.55
              and sharpe is not None and sharpe > 0.5 and dd < 5.0 and level < 2):
            new_level = level + 1
            reason = "win-rate {:.0%}, sharpe {:.1f} — step up".format(wr, sharpe)

        if new_level == level:
            return {"changed": False, "level": _PRESET_LADDER[level], "reason": reason,
                    "metrics": {"trades": n, "win_rate": wr, "sharpe": sharpe, "drawdown_pct": round(dd, 1)}}
        # CAS the level key from the value we read to the new value: two cycles
        # racing (e.g. manual + scheduled) must not both apply a preset jump off
        # the same `level` — the loser sees a changed key and bows out, so the
        # ladder advances exactly one step. Apply the preset only after we own it.
        if not _cas_escalation_level(level, new_level):
            return {"changed": False, "reason": "escalation level changed concurrently"}
        aj_config.apply_preset(_PRESET_LADDER[new_level])
        aj_db.audit("preset_escalation",
                    {"from": _PRESET_LADDER[level], "to": _PRESET_LADDER[new_level], "reason": reason})
        return {"changed": True, "from": _PRESET_LADDER[level], "to": _PRESET_LADDER[new_level], "reason": reason}
    except Exception as e:
        log.exception("maybe_escalate failed")
        return {"changed": False, "reason": "error: {}".format(str(e)[:100])}


# ════════════════════════════════════════════════════════════════════════════
#  24 — DAILY REFLECTION JOURNAL
# ════════════════════════════════════════════════════════════════════════════

def build_reflection() -> Dict[str, Any]:
    """Compose (without persisting) the day's self-review: P&L, trade quality,
    best/worst names, drawdown, and a one-line takeaway."""
    import aj_analytics, aj_risk, aj_alpha
    stats = aj_analytics.trade_stats() or {}
    attrib = aj_analytics.attribution() or []
    day = aj_risk.compute_day_pnl() or {}
    best = attrib[0] if attrib else None
    worst = attrib[-1] if len(attrib) > 1 and attrib[-1].get("total", 0) < 0 else None
    dd = aj_alpha.current_drawdown_pct()
    wr = stats.get("win_rate")
    # deterministic takeaway (LLM optional below)
    if wr is None:
        takeaway = "No closed trades yet — accumulating positions; watch unrealized risk."
    elif wr >= 0.55 and day.get("day_pnl", 0) >= 0:
        takeaway = "Edge is working — win-rate {:.0%}. Let winners run, keep sizing disciplined.".format(wr)
    elif dd >= 10:
        takeaway = "In a {:.0f}% drawdown — tighten entries, shrink size, protect capital.".format(dd)
    else:
        takeaway = "Mixed day — win-rate {:.0%}. Review weakest names and entry timing.".format(wr or 0)

    refl = {
        "date": _et_date(),
        "day_pnl_usd": day.get("day_pnl"),
        "trades_closed": stats.get("trades"),
        "win_rate": wr,
        "profit_factor": stats.get("profit_factor"),
        "drawdown_pct": round(dd, 1),
        "best_name": ({"symbol": best.get("symbol"), "total": best.get("total")} if best else None),
        "worst_name": ({"symbol": worst.get("symbol"), "total": worst.get("total")} if worst else None),
        "takeaway": takeaway,
    }
    # optional LLM polish — best-effort, never blocks
    try:
        cfg = aj_config.get_config()
        if cfg.get("use_llm_synthesis"):
            import aj_routing
            r = aj_routing.complete(
                "In one sentence, give a trading coach's takeaway. Day P&L ${:.0f}, "
                "win-rate {}, drawdown {:.0f}%.".format(
                    float(day.get("day_pnl") or 0),
                    "{:.0%}".format(wr) if wr is not None else "n/a", dd),
                role="judge", sensitivity="public", quality_floor=1.0)
            if r.get("ok") and isinstance(r.get("text"), str):
                refl["takeaway"] = r["text"].strip()[:300]
    except Exception:
        log.debug("reflection LLM polish skipped", exc_info=True)
    return refl


def write_reflection() -> Dict[str, Any]:
    """Build today's reflection and persist it — FIRST write of the day wins.

    Keyed by date with INSERT OR IGNORE so a manual + scheduled run on the same
    day don't double-write (and double-audit) the journal entry. If today's entry
    already exists, return it unchanged rather than rebuilding/overwriting it."""
    import database as _db
    date = _et_date()
    key = "{}_{}".format(_REFLECTION_KEY, date)
    existing = get_reflection(date)
    if existing is not None:
        # Weekly check still runs on the already-written path: the ">7 days
        # stale" trigger can come due later in the day than the first write.
        _maybe_write_weekly()
        return existing
    refl = build_reflection()
    try:
        won = False
        with _db._write_lock:
            conn = _db.get_conn()
            cur = conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
                (key, json.dumps(refl)))
            conn.commit()
            won = cur.rowcount == 1
        try:
            _db._invalidate_settings_cache()
        except Exception:
            pass
        # Only audit when WE created today's entry (a racing writer that won the
        # INSERT already audited it).
        if won:
            aj_db.audit("reflection", {"date": refl["date"], "takeaway": refl["takeaway"]})
        else:
            prior = get_reflection(date)
            if prior is not None:
                return prior
    except Exception:
        log.exception("write_reflection persist failed")
    # Dual-write into the dated journal (24b). INSERT OR IGNORE preserves the
    # first-write-wins semantics; reads stay on the settings path (back-compat).
    _journal_write("reflection", date, refl)
    _maybe_write_weekly()
    return refl


def get_reflection(date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    date = date or _et_date()
    raw = aj_db.get_setting_raw("{}_{}".format(_REFLECTION_KEY, date))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ── 24b — DATED JOURNAL (aj_journal) + WEEKLY ROLLUP ─────────────────────────
#
# Reflections/briefings historically live in per-day settings blobs
# (__aj_reflection_YYYY-MM-DD / __aj_briefing). Those are single-key reads with
# no history browsing and no cross-day aggregation. The migration-v10 aj_journal
# table (kind, date, ts, payload_json, UNIQUE(kind,date)) gives them a dated,
# queryable home. We DUAL-WRITE at the existing write points and keep all reads
# on the settings path (back-compat) — the journal is additive observability,
# so every function here is fail-open: a journal failure must never break a
# reflection/briefing (let alone a trading cycle).

def _journal_write(kind: str, date: str, payload: Any) -> bool:
    """INSERT OR IGNORE one journal entry — first-write-wins via the
    UNIQUE(kind,date) key, mirroring the settings-blob semantics exactly (a
    manual + scheduled run on the same day can both call this; only the first
    lands). aj_journal is deliberately NOT in aj_db._ALLOWED_TABLES (the
    generic insert() whitelist is for trading tables), so we write it directly
    under the shared write lock, same as the settings CAS helpers above."""
    import database as _db
    try:
        with _db._write_lock:
            conn = _db.get_conn()
            cur = conn.execute(
                "INSERT OR IGNORE INTO aj_journal (kind, date, ts, payload_json) "
                "VALUES (?,?,?,?)",
                (kind, date, aj_db.utc_now_iso(), json.dumps(payload)))
            conn.commit()
            return cur.rowcount == 1
    except Exception:
        log.debug("journal write (%s/%s) failed", kind, date, exc_info=True)
        return False


def journal_entries(kind: Optional[str] = None,
                    limit: int = 30) -> List[Dict[str, Any]]:
    """Recent journal entries, newest first, payload parsed. `kind` filters to
    'reflection' / 'briefing' / 'weekly'; None returns all kinds interleaved."""
    try:
        limit = max(1, min(500, int(limit)))
        if kind:
            rows = aj_db.query(
                "SELECT kind, date, ts, payload_json FROM aj_journal "
                "WHERE kind=? ORDER BY date DESC, id DESC LIMIT ?", (kind, limit))
        else:
            rows = aj_db.query(
                "SELECT kind, date, ts, payload_json FROM aj_journal "
                "ORDER BY date DESC, id DESC LIMIT ?", (limit,))
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(r.get("payload_json") or "null")
            except Exception:
                payload = None    # a corrupt row must not hide the others
            out.append({"kind": r.get("kind"), "date": r.get("date"),
                        "ts": r.get("ts"), "payload": payload})
        return out
    except Exception:
        log.debug("journal_entries failed", exc_info=True)
        return []


def weekly_rollup(end_date: Optional[str] = None) -> Dict[str, Any]:
    """Compose the last 7 ET days of journal REFLECTIONS into one weekly view:
    average win-rate, total day P&L, best/worst day, and the daily takeaways.
    Pure read/compose — persisting is _maybe_write_weekly's job."""
    end = end_date or _et_date()
    try:
        start = (_date.fromisoformat(end) - timedelta(days=6)).isoformat()
    except (TypeError, ValueError):
        start = end   # degenerate date string — roll up just that day
    days: List[str] = []
    wrs: List[float] = []
    takeaways: List[str] = []
    total_pnl = 0.0
    best: Optional[Dict[str, Any]] = None
    worst: Optional[Dict[str, Any]] = None
    try:
        rows = aj_db.query(
            "SELECT date, payload_json FROM aj_journal "
            "WHERE kind='reflection' AND date BETWEEN ? AND ? ORDER BY date",
            (start, end))
    except Exception:
        log.debug("weekly_rollup query failed", exc_info=True)
        rows = []
    for r in rows:
        try:
            p = json.loads(r.get("payload_json") or "{}") or {}
        except Exception:
            continue
        d = r.get("date")
        days.append(d)
        wr = p.get("win_rate")
        if wr is not None:
            try:
                wrs.append(float(wr))
            except (TypeError, ValueError):
                pass
        try:
            pnl = float(p["day_pnl_usd"]) if p.get("day_pnl_usd") is not None else None
        except (TypeError, ValueError, KeyError):
            pnl = None
        if pnl is not None:
            total_pnl += pnl
            if best is None or pnl > best["day_pnl_usd"]:
                best = {"date": d, "day_pnl_usd": pnl}
            if worst is None or pnl < worst["day_pnl_usd"]:
                worst = {"date": d, "day_pnl_usd": pnl}
        tw = p.get("takeaway")
        if tw:
            takeaways.append("{}: {}".format(d, tw))
    return {
        "period": "{}..{}".format(start, end),
        "days": days,
        "avg_win_rate": (round(sum(wrs) / len(wrs), 4) if wrs else None),
        "total_day_pnl": round(total_pnl, 2),
        "best_day": best,
        "worst_day": worst,
        "takeaways": takeaways,
    }


def _maybe_write_weekly() -> bool:
    """Persist a 'weekly' journal rollup when one is due: today is the ET
    Sunday (last day of the week), OR the latest weekly entry is >7 days old
    (covers an agent that never runs on Sundays — markets don't), OR none
    exists yet (bootstraps the first rollup). Cheap (one indexed SELECT) and
    idempotent — UNIQUE(kind,date) makes a same-day re-run a no-op, and the
    >7-day staleness check stops a weekday trigger from re-firing daily."""
    try:
        today = _et_date()
        today_d = _date.fromisoformat(today)
        is_last_day = today_d.weekday() == 6   # Sunday closes the ET week
        latest = aj_db.query(
            "SELECT date FROM aj_journal WHERE kind='weekly' "
            "ORDER BY date DESC LIMIT 1")
        stale = True
        if latest:
            try:
                stale = (today_d - _date.fromisoformat(latest[0]["date"])).days > 7
            except (TypeError, ValueError):
                stale = True   # unparseable stored date — treat as due
        if not (is_last_day or stale):
            return False
        roll = weekly_rollup(today)
        if not roll.get("days"):
            # Nothing to roll up — don't burn today's UNIQUE(kind,date) slot on
            # an empty rollup that would then block the real one all day.
            return False
        return _journal_write("weekly", today, roll)
    except Exception:
        log.debug("weekly rollup skipped", exc_info=True)
        return False


# ════════════════════════════════════════════════════════════════════════════
#  25 — PRE-MARKET OPPORTUNITY BRIEFING
# ════════════════════════════════════════════════════════════════════════════

def build_briefing(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Rank the candidate universe by setup score and surface the top ideas with
    a quick read on each — the pre-market 'what to watch today' list."""
    cfg = cfg or aj_config.get_config()
    try:
        import aj_operator, aj_alpha
        universe = aj_operator._scan_universe() or list(cfg.get("symbol_allowlist") or [])
        k = max(1, int(cfg.get("opportunity_radar_top_k") or 5))

        # NaN-safe ordering, mirroring aj_alpha.rank_universe's _score_key:
        # coerce non-floats and sink NaN to the bottom so a degenerate score
        # can't make the top-K ordering undefined.
        def _score_key(t):
            try:
                fv = float(t[1])
            except (TypeError, ValueError):
                return float("inf")
            return float("inf") if math.isnan(fv) else -fv
        scored = sorted(((s, aj_alpha._radar_score(s)) for s in universe),
                        key=_score_key)[:k]
        ideas = []
        for sym, score in scored:
            closes = aj_alpha._closes(sym, "1y")
            rs = aj_alpha._pct_return(closes, 20)
            r = aj_alpha._rsi(closes, 14)
            ideas.append({"symbol": sym, "score": round(score, 1),
                          "rs_20d_pct": round(rs, 1) if rs is not None else None,
                          "rsi": round(r, 0) if r is not None else None})
        briefing = {
            "date": aj_db.utc_now().strftime("%Y-%m-%d"),
            "session": aj_db.market_session(),
            "regime": aj_alpha.detect_regime(),
            "universe_size": len(universe),
            "ideas": ideas,
        }
        return briefing
    except Exception as e:
        log.exception("build_briefing failed")
        return {"date": aj_db.utc_now().strftime("%Y-%m-%d"), "ideas": [],
                "error": str(e)[:120]}


def write_briefing(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    # First briefing of the day wins: if today's briefing is already stored,
    # return it rather than rebuilding (a manual + scheduled premarket run on the
    # same day must not double-build/overwrite). Keyed on the stored payload's
    # date so get_briefing()'s single-key contract is preserved.
    today = aj_db.utc_now().strftime("%Y-%m-%d")
    existing = get_briefing()
    if existing is not None and existing.get("date") == today:
        return existing
    b = build_briefing(cfg)
    # Never let an errored/empty build claim the day's first-write slot: a
    # transient quote-feed outage at 4:05am would otherwise cache an empty
    # briefing that every later premarket cycle and UI read serves all day.
    # Skipping the persist keeps it self-healing (rebuilt next call).
    if b.get("error") or not b.get("ideas"):
        return b
    try:
        aj_db.set_setting_raw(_BRIEFING_KEY, json.dumps(b))
    except Exception:
        log.exception("write_briefing persist failed")
    # Dual-write into the dated journal (24b), keyed by the ET trading date
    # (the payload's own date stays UTC for back-compat with existing readers).
    # First briefing of the ET day wins via UNIQUE(kind,date); fail-open.
    _journal_write("briefing", _et_date(), b)
    return b


def get_briefing() -> Optional[Dict[str, Any]]:
    raw = aj_db.get_setting_raw(_BRIEFING_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None
