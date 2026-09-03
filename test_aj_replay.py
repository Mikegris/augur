#!/usr/bin/env python3
"""Replay-engine tests (aj_replay / aj_replay_data).

Fully offline: synthetic deterministic bars, subprocess replays against
temp DBs. Slower than the other aj suites (it runs real replays) but
network-free and deterministic.
"""
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import date, timedelta, datetime, timezone

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajreplay_live.db")

import aj_db                     # noqa: E402  (this process's "live" DB)
import aj_replay_data as rd      # noqa: E402

aj_db.aj_init()

REPO = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
WORK = tempfile.mkdtemp(prefix="ajreplay_")
CACHE = os.path.join(WORK, "cache")
OUT = os.path.join(WORK, "replays")


# ── synthetic dataset (deterministic LCG walk) ────────────────────────────────

def _dates(start, end):
    d, e, out = date.fromisoformat(start), date.fromisoformat(end), []
    while d <= e:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


DS = _dates("2023-01-02", "2023-12-29")


def _series(seed, base, drift, vol):
    x, px, out = seed, base, []
    for i, d in enumerate(DS):
        x = (1103515245 * x + 12345) % (2 ** 31)
        eps = (x / (2 ** 31) - 0.5) * 2.0
        px = max(1.0, px * (1.0 + drift + vol * eps
                            + 0.02 * vol * math.sin(i / 17.0)))
        out.append({"date": d, "open": round(px * 0.999, 4),
                    "high": round(px * 1.01, 4), "low": round(px * 0.99, 4),
                    "close": round(px, 4), "volume": 1000000 + (x % 500000)})
    return out


def _build_cache():
    os.makedirs(CACHE, exist_ok=True)
    syms = {"SPY": _series(7, 380.0, 0.0004, 0.008),
            "^VIX": [dict(b, close=20.0) for b in _series(11, 15.0, 0.0, 0.0)],
            "AAA": _series(13, 50.0, 0.0009, 0.02),
            "BBB": _series(29, 120.0, -0.0003, 0.015)}
    for sym, bars in syms.items():
        safe = sym.replace("/", "_").replace(":", "_")
        with open(os.path.join(CACHE, safe + ".json"), "w") as f:
            json.dump({"symbol": sym, "period": "synthetic", "bars": bars}, f)


_build_cache()


def _run_replay(run_id, start, end, extra=()):
    env = dict(os.environ)
    env["AUGUR_DB_PATH"] = os.path.join(OUT, run_id, "replay.db")
    os.makedirs(os.path.join(OUT, run_id), exist_ok=True)
    cmd = [PY, os.path.join(REPO, "aj_replay.py"), "run",
           "--symbols", "AAA,BBB", "--start", start, "--end", end,
           "--cash", "100000", "--offline", "--cache-dir", CACHE,
           "--out-dir", OUT, "--run-id", run_id] + list(extra)
    p = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       cwd=REPO, timeout=600)
    assert p.returncode == 0, p.stderr[-1500:]
    with open(os.path.join(OUT, run_id, "result.json")) as f:
        return json.load(f)


# ── PIT store: as-of semantics + no look-ahead ────────────────────────────────

def test_pit_asof_no_lookahead():
    store = rd.PITStore(CACHE)
    store.load(["AAA", "SPY"])
    ds = store.trading_dates("2023-03-01", "2023-03-31", anchor="SPY")
    assert len(ds) >= 20
    mid = ds[10]
    store.set_asof(mid)
    q = store.quote("AAA")
    bars = store.chart("AAA", "6mo", "1d")
    assert q is not None and q["price"] > 0
    # the quote is EXACTLY that day's close, and no chart bar postdates asof
    assert bars[-1]["date"] == mid and abs(q["price"] - bars[-1]["close"]) < 1e-9
    assert all(b["date"] <= mid for b in bars)
    # tomorrow's close differs -> the store can't be serving the future
    store.set_asof(ds[11])
    q2 = store.quote("AAA")
    assert q2["price"] != q["price"]


def test_pit_staleness_returns_none():
    store = rd.PITStore(CACHE)
    store.inject("DEAD", [{"date": "2023-01-05", "close": 10.0, "volume": 1}])
    store.set_asof("2023-01-06")
    assert store.quote("DEAD")["price"] == 10.0
    store.set_asof("2023-02-01")     # > MAX_QUOTE_STALENESS_DAYS later
    assert store.quote("DEAD") is None   # delisted names go quiet, not stale


def test_patch_blocks_network_and_restores():
    import fetcher
    import requests
    orig_chart = fetcher.get_chart_data
    store = rd.PITStore(CACHE)
    store.load(["AAA"])
    store.set_asof("2023-06-01")
    rd.patch(store, block_network=True)
    try:
        try:
            requests.get("https://example.com", timeout=1)
            assert False, "network was not blocked"
        except rd.ReplayNetworkBlocked:
            pass
        assert fetcher.get_quote("AAA")["price"] > 0
    finally:
        rd.unpatch()
    assert fetcher.get_chart_data is orig_chart
    # after unpatch a plain requests call object exists again (not our stub)
    assert requests.get is not rd._blocked


def test_sim_clock_drives_sessions():
    aj_db.set_sim_clock(datetime(2023, 3, 15, 14, 30, tzinfo=timezone.utc))
    try:
        assert aj_db.utc_now().year == 2023
        assert aj_db.market_session() == "regular"   # 10:30 ET weekday
    finally:
        aj_db.set_sim_clock(None)
    assert aj_db.utc_now().year >= 2026


# ── the replay itself ─────────────────────────────────────────────────────────

def test_replay_end_to_end_and_isolated():
    before = aj_db.query("SELECT COUNT(*) AS n FROM aj_orders")[0]["n"]
    r = _run_replay("t_e2e", "2023-02-01", "2023-03-31")
    assert r["days"] >= 35 and r["kind"] == "replay"
    assert r.get("total_return_pct") is not None
    assert r.get("benchmark_return_pct") is not None
    assert (r.get("gate") or {}).get("pass", 0) > 0, r.get("gate")
    assert isinstance(r.get("daily"), list) and r["daily"][0]["equity"] == 100000.0
    assert any("survivorship" in c for c in r.get("caveats") or [])
    # equity moves once trades happen (the pipeline actually traded)
    eqs = {d["equity"] for d in r["daily"] if d["equity"] is not None}
    assert len(eqs) > 1
    # ISOLATION: this process's ("live") DB gained no orders from the replay
    after = aj_db.query("SELECT COUNT(*) AS n FROM aj_orders")[0]["n"]
    assert after == before


def test_replay_deterministic():
    a = _run_replay("t_det1", "2023-02-01", "2023-03-15")
    b = _run_replay("t_det2", "2023-02-01", "2023-03-15")
    for k in ("total_return_pct", "sharpe", "max_drawdown_pct",
              "alpha_pct", "days"):
        assert a.get(k) == b.get(k), (k, a.get(k), b.get(k))
    assert [d["equity"] for d in a["daily"]] == [d["equity"] for d in b["daily"]]


def test_replay_config_overrides_change_behavior():
    strict = _run_replay("t_strict", "2023-02-01", "2023-03-31",
                         ["--set", "buy_prob_threshold=0.99"])
    # an impossible entry bar means no buys -> flat equity all period
    eqs = {d["equity"] for d in strict["daily"] if d["equity"] is not None}
    assert eqs == {100000.0}, sorted(eqs)[:3]


def test_grid_ranks_and_reports_holdout():
    env = dict(os.environ)
    env.pop("AUGUR_DB_PATH", None)
    cmd = [PY, os.path.join(REPO, "aj_replay.py"), "grid",
           "--symbols", "AAA,BBB", "--start", "2023-02-01",
           "--end", "2023-03-31", "--offline", "--cache-dir", CACHE,
           "--out-dir", OUT, "--grid-id", "t_grid",
           "--param", "buy_prob_threshold=0.55,0.99", "--max-cells", "4"]
    p = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       cwd=REPO, timeout=1200)
    assert p.returncode == 0, p.stderr[-1500:]
    with open(os.path.join(OUT, "t_grid", "grid.json")) as f:
        g = json.load(f)
    assert len(g["cells"]) == 2 and g["winner"] is not None
    for cell in g["cells"]:
        assert "train_sharpe" in cell and "test_return_pct" in cell
    # the 0.99 cell never trades: flat equity -> no sharpe; ranking put a
    # traded config first (or at least produced a deterministic order)
    params = [c["params"]["buy_prob_threshold"] for c in g["cells"]]
    assert set(params) == {"0.55", "0.99"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_replay — {} tests".format(len(fns)))
    failed = 0
    for fn in fns:
        try:
            fn()
            print("  [OK] {}".format(fn.__name__))
        except AssertionError as e:
            failed += 1
            print("  [XX] {}: {}".format(fn.__name__, e))
        except Exception as e:
            failed += 1
            print("  [XX] {}: unexpected {}: {}".format(
                fn.__name__, type(e).__name__, e))
    print("PASS" if failed == 0 else "{} FAILED".format(failed))
    sys.exit(1 if failed else 0)


def test_meanrev_signal_module():
    """aj_meanrev primitives: momentum vs mean-reversion diverge as designed,
    and the blend switches on regime. Pure functions, no I/O."""
    import aj_meanrev
    # a steadily RISING series: momentum bullish, mean-reversion NOT (not oversold)
    up = [100.0 * (1.02 ** i) for i in range(80)]
    pm = aj_meanrev.momentum_prob(up)
    assert pm is not None and pm > 0.6, pm
    # a series that rose then DIPPED sharply while 60d trend still positive:
    # mean-reversion should read it as a buyable dip
    dip = [100.0 * (1.015 ** i) for i in range(70)] + [x for x in
           (100.0 * (1.015 ** 69) * (1 - 0.03 * j) for j in range(1, 8))]
    pmr = aj_meanrev.meanrev_prob(dip)
    assert pmr is not None
    # blend switches: chop -> meanrev value, trend -> momentum value
    assert aj_meanrev.blended_prob(up, "chop") == aj_meanrev.meanrev_prob(up)
    assert aj_meanrev.blended_prob(up, "bull") == aj_meanrev.momentum_prob(up)
    # insufficient history -> None, and prob_to_ensemble handles None
    assert aj_meanrev.momentum_prob([100.0] * 10) is None
    assert aj_meanrev.prob_to_ensemble(None, "x")["ensemble"] is None
    ens = aj_meanrev.prob_to_ensemble(0.7, "pit_test")["ensemble"]
    assert ens["prob_up"] == 0.7 and ens["conviction"] in ("low", "medium", "high")
