"""AJTA — feature/label store for meta-labeling (schema v8 `aj_trade_labels`).

For every CLOSED round-trip trade the agent made, build a single training row:
the features that were present at ENTRY time (recovered from the per-cycle
`aj_cycle_stats` scan snapshot nearest at/just-before the trade opened) plus a
binary label = was the round-trip net profitable (P&L net of fees > 0).

Three public functions:

  * build_labels(lookback_days)  -> {"built", "total", "base_rate"}  (writes)
  * training_set()               -> (X, y, feature_names)            (read)
  * label_stats()                -> {n, base_rate, by_regime, ...}   (read)

CONTRACT: training_set() returns a CONSISTENT, ordered feature vector. The
meta-label model codes against FEATURE_NAMES below; the order is deterministic
and stable, and a missing feature is imputed to a fixed neutral value so every
row is the same length.

Guardrails: fail-open everywhere (any error -> {"error": ...} / empty, never
raise); writes go through database._write_lock; idempotent (INSERT OR IGNORE on
the UNIQUE (symbol, opened_at, closed_at) key — re-running never duplicates).
No new dependencies; Python-3.9 compatible.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

import aj_db
import database as db

log = logging.getLogger("augur.aj_features")

# ── canonical feature contract ────────────────────────────────────────────────
# THE ML CONTRACT. Order is fixed; never reorder/insert in the middle — only
# append (and bump the model). Each entry: (name, neutral_impute_value). The
# numeric features come from the decision-time scan snapshot; `side` is encoded
# as long(buy/cover-close=1.0) vs short(0.0); regime is one-hot expanded below.
_NUMERIC_FEATURES: List[Tuple[str, float]] = [
    ("edge", 0.0),
    ("conviction", 0.5),
    ("prob_up", 0.5),
    ("holding_days", 0.0),
    ("side_long", 1.0),
]
# Regime is categorical -> stable one-hot columns (neutral 0.0 each). Unknown /
# unseen regimes simply leave all three at 0.0 (still a valid, consistent row).
_REGIME_LEVELS: List[str] = ["bull", "bear", "chop"]

FEATURE_NAMES: List[str] = (
    [n for n, _ in _NUMERIC_FEATURES]
    + ["regime_" + r for r in _REGIME_LEVELS]
)

_CONVICTION_MAP = {"low": 0.0, "med": 0.5, "medium": 0.5, "high": 1.0}


def _conviction_to_num(v: Any) -> Optional[float]:
    """Map low/med/high -> 0/.5/1; pass through anything already numeric."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        s = str(v).strip().lower()
    except Exception:
        return None
    if s in _CONVICTION_MAP:
        return _CONVICTION_MAP[s]
    try:
        return float(s)
    except Exception:
        return None


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except Exception:
        return None
    if x != x or x in (float("inf"), float("-inf")):  # NaN/inf guard
        return None
    return x


# ── scan-snapshot index (decision-time features by symbol) ─────────────────────

def _load_scan_index() -> Dict[str, List[Tuple]]:
    """Build {SYMBOL: [(ts_dt, {edge, conviction, prob_up}), ...]} from every
    aj_cycle_stats row, sorted ascending by timestamp. Used to recover the
    features present when a trade was opened by matching the snapshot whose ts
    is the nearest at/just-before the trade's open time. Fail-open to {}."""
    idx: Dict[str, List[Tuple]] = defaultdict(list)
    try:
        rows = aj_db.query(
            "SELECT scan_json, ts FROM aj_cycle_stats ORDER BY ts ASC")
    except Exception:
        log.debug("scan index query failed", exc_info=True)
        return {}
    for r in rows:
        ts = aj_db.parse_iso(r.get("ts"))
        if ts is None:
            continue
        raw = r.get("scan_json")
        try:
            entries = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            continue
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            sym = (e.get("symbol") or "").upper()
            if not sym:
                continue
            idx[sym].append((ts, {
                "edge": e.get("edge"),
                "conviction": e.get("conviction"),
                "prob_up": e.get("prob_up"),
            }))
    for sym in idx:
        idx[sym].sort(key=lambda t: t[0])
    return idx


def _nearest_prior_snapshot(idx: Dict[str, List[Tuple]], symbol: str,
                            opened_at) -> Dict[str, Any]:
    """Snapshot for `symbol` whose ts is the latest <= opened_at. Falls back to
    the earliest snapshot if none precede the open (better a near miss than
    nothing); {} when the symbol was never scanned."""
    lst = idx.get((symbol or "").upper())
    if not lst:
        return {}
    if opened_at is None:
        return dict(lst[0][1])
    best = None
    for ts, feats in lst:
        if ts <= opened_at:
            best = feats
        else:
            break
    if best is None:
        best = lst[0][1]  # all snapshots are after the open -> nearest one
    return dict(best)


# ── round-trip reconstruction (entry price/time per closed trade) ──────────────

def _round_trips(mode: str = "paper") -> List[Dict[str, Any]]:
    """Closed round-trip trades with ENTRY price/time + realized P&L.

    aj_positions.realized_trades() is the canonical list of closing fills (net
    of fees) but its records carry only the CLOSE side; the meta-label row needs
    the matching entry price + open time. We replay aj_fills FIFO ourselves to
    pair each closing quantity with the lot(s) it consumed and recover the
    weighted entry price + the oldest consumed lot's open timestamp. If a
    record already carries entry fields (e.g. injected in tests), they win.

    Output per trade: {symbol, side, qty, entry_price, exit_price, opened_at,
    closed_at, realized_pnl_usd}. Fail-open to []."""
    try:
        canonical = _canonical_trades(mode)
    except Exception:
        log.debug("canonical trades unavailable", exc_info=True)
        canonical = []

    # If the canonical records already carry entry data, use them directly.
    enriched: List[Dict[str, Any]] = []
    need_fifo = False
    for t in canonical:
        if t.get("entry_price") is not None or t.get("opened_at") is not None:
            enriched.append(t)
        else:
            need_fifo = True
            enriched.append(t)
    if not need_fifo:
        return enriched

    # Reconstruct entry price/time via our own FIFO replay over the fills, keyed
    # by symbol+closing-order to match the canonical list positionally.
    fifo = _fifo_round_trips(mode)
    # Index the FIFO results by symbol in close order so we can fill gaps.
    by_sym: Dict[str, deque] = defaultdict(deque)
    for rt in fifo:
        by_sym[rt["symbol"]].append(rt)
    out: List[Dict[str, Any]] = []
    for t in enriched:
        sym = (t.get("symbol") or "").upper()
        if (t.get("entry_price") is None or t.get("opened_at") is None) and by_sym.get(sym):
            rt = by_sym[sym].popleft()
            merged = dict(rt)
            merged.update({k: v for k, v in t.items() if v is not None})
            # canonical realized P&L (net of fees) is authoritative
            if t.get("realized_pnl_usd") is not None:
                merged["realized_pnl_usd"] = t["realized_pnl_usd"]
            out.append(merged)
        else:
            out.append(t)
    return out


def _canonical_trades(mode: str) -> List[Dict[str, Any]]:
    """Normalize aj_positions.realized_trades() into our field names. Pulls
    entry fields too if a record happens to carry them (tests inject them)."""
    import aj_positions
    rows = aj_positions.realized_trades(mode)
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        pnl = r.get("realized_pnl_usd")
        if pnl is None:
            pnl = r.get("realized")
        out.append({
            "symbol": (r.get("symbol") or "").upper(),
            "side": r.get("side"),
            "qty": r.get("qty"),
            "entry_price": r.get("entry_price", r.get("entry")),
            "exit_price": r.get("exit_price", r.get("price")),
            "opened_at": r.get("opened_at", r.get("entry_at")),
            "closed_at": r.get("closed_at", r.get("filled_at")),
            "realized_pnl_usd": pnl,
        })
    return out


def _fifo_round_trips(mode: str = "paper") -> List[Dict[str, Any]]:
    """Replay aj_fills FIFO; emit one round-trip per closing event with the
    weighted entry price + oldest consumed lot's open time. Mirrors
    aj_positions' FIFO but retains entry metadata. Fail-open to []."""
    lots: Dict[str, deque] = defaultdict(deque)  # SYM -> deque([qty, price, opened_at])
    out: List[Dict[str, Any]] = []
    try:
        rows = aj_db.query(
            "SELECT o.symbol AS symbol, o.side AS side, f.qty AS qty, "
            "f.price AS price, f.fees_usd AS fees, f.filled_at AS filled_at "
            "FROM aj_fills f JOIN aj_orders o ON f.order_id = o.id "
            "WHERE o.mode = ? ORDER BY f.filled_at ASC, f.id ASC", (mode,))
    except Exception:
        return out
    eps = 1e-9
    for r in rows:
        sym = (r.get("symbol") or "").upper()
        side = (r.get("side") or "").lower()
        qty = float(r.get("qty") or 0)
        price = float(r.get("price") or 0)
        when = r.get("filled_at")
        dq = lots[sym]
        closed_qty = 0.0
        entry_cost = 0.0
        oldest_open = None
        if side == "buy":
            remaining = qty
            while remaining > eps and dq and dq[0][0] < 0:
                lot = dq[0]
                take = min(remaining, -lot[0])
                entry_cost += lot[1] * take
                if oldest_open is None:
                    oldest_open = lot[2]
                closed_qty += take
                lot[0] += take
                remaining -= take
                if lot[0] >= -eps:
                    dq.popleft()
            if remaining > eps:
                dq.append([remaining, price, when])
        elif side == "sell":
            remaining = qty
            while remaining > eps and dq and dq[0][0] > 0:
                lot = dq[0]
                take = min(remaining, lot[0])
                entry_cost += lot[1] * take
                if oldest_open is None:
                    oldest_open = lot[2]
                closed_qty += take
                lot[0] -= take
                remaining -= take
                if lot[0] <= eps:
                    dq.popleft()
            if remaining > eps:
                dq.append([-remaining, price, when])
        if closed_qty > eps:
            entry_price = entry_cost / closed_qty if closed_qty else None
            out.append({
                "symbol": sym, "side": side, "qty": closed_qty,
                "entry_price": entry_price, "exit_price": price,
                "opened_at": oldest_open, "closed_at": when,
                "realized_pnl_usd": None,
            })
    return out


# ── feature assembly for one trade ─────────────────────────────────────────────

def _holding_days(opened, closed) -> Optional[float]:
    o = aj_db.parse_iso(opened)
    c = aj_db.parse_iso(closed)
    if o is None or c is None:
        return None
    try:
        return max(0.0, (c - o).total_seconds() / 86400.0)
    except Exception:
        return None


def _return_pct(side: Any, entry: Any, exit_: Any) -> Optional[float]:
    e = _f(entry)
    x = _f(exit_)
    if e is None or x is None or e == 0:
        return None
    raw = (x - e) / abs(e) * 100.0
    # A short round-trip profits when exit < entry, so flip the sign.
    s = (str(side or "").lower())
    if s == "buy":          # a BUY that closed -> it was covering a SHORT
        raw = -raw
    return raw


def _assemble_features(trade: Dict[str, Any], snap: Dict[str, Any],
                       holding_days: Optional[float],
                       regime: Optional[str]) -> Dict[str, Any]:
    """Feature dict stored as features_json. Neutral/empty when a source is
    missing — the row is still labelable."""
    conv = _conviction_to_num(snap.get("conviction"))
    # Entry direction: realized_trades' `side` is the CLOSING side. A closing
    # SELL closed a LONG (side_long); a closing BUY covered a SHORT.
    close_side = str(trade.get("side") or "").lower()
    side_long = 0.0 if close_side == "buy" else 1.0
    feats: Dict[str, Any] = {
        "edge": _f(snap.get("edge")),
        "conviction": conv,
        "prob_up": _f(snap.get("prob_up")),
        "holding_days": holding_days,
        "side_long": side_long,
        "regime": regime,
    }
    return feats


# ── public API ─────────────────────────────────────────────────────────────────

def build_labels(lookback_days: int = 365) -> Dict[str, Any]:
    """Enumerate closed round-trip trades within the lookback, recover their
    decision-time features, label them (net P&L > 0 -> 1), and upsert into
    aj_trade_labels (idempotent on the UNIQUE key). Returns
    {"built": n_new, "total": total_rows, "base_rate": pct_profitable}.
    Fail-open: any error -> {"error": ...}."""
    try:
        try:
            cutoff = aj_db.utc_now() - __import__("datetime").timedelta(
                days=max(0, int(lookback_days)))
        except Exception:
            cutoff = None

        trades = _round_trips("paper")
        scan_idx = _load_scan_index()
        try:
            import aj_alpha
            regime = aj_alpha.detect_regime()
        except Exception:
            regime = None

        # Benchmark closes fetched ONCE, to label each trade's alpha vs the market
        # over its own holding window (opened_at -> closed_at). Fail-open to {}.
        bench_closes: Dict[str, float] = {}
        try:
            import aj_config
            import aj_benchmark
            bench_sym = str(aj_config.get_config().get("metalabel_benchmark") or "SPY").upper()
            bench_closes = aj_benchmark._index_closes(
                bench_sym, "2y" if lookback_days > 200 else "1y") or {}
        except Exception:
            bench_closes = {}
        _bsorted = sorted(bench_closes)

        def _bench_ret(start, end):
            """% market return between two dates (close on/before each)."""
            if not bench_closes or not start or not end:
                return None
            s = str(start)[:10]
            e = str(end)[:10]

            def _on(d):
                if d in bench_closes:
                    return bench_closes[d]
                prev = [x for x in _bsorted if x <= d]
                return bench_closes[prev[-1]] if prev else None
            p0, p1 = _on(s), _on(e)
            if p0 is None or p1 is None or p0 <= 0:
                return None
            return (p1 / p0 - 1.0) * 100.0

        now = aj_db.utc_now_iso()
        built = 0
        with db._write_lock:
            conn = db.get_conn()
            for t in trades:
                sym = (t.get("symbol") or "").upper()
                if not sym:
                    continue
                opened_at = t.get("opened_at")
                closed_at = t.get("closed_at")
                # lookback filter on the CLOSE time (best-effort).
                if cutoff is not None and closed_at is not None:
                    cdt = aj_db.parse_iso(closed_at)
                    if cdt is not None and cdt < cutoff:
                        continue
                pnl = _f(t.get("realized_pnl_usd"))
                if pnl is None:
                    pnl = 0.0
                label = 1 if pnl > 0 else 0
                hd = _holding_days(opened_at, closed_at)
                opened_dt = aj_db.parse_iso(opened_at)
                snap = _nearest_prior_snapshot(scan_idx, sym, opened_dt)
                feats = _assemble_features(t, snap, hd, regime)
                ret_pct = _return_pct(t.get("side"), t.get("entry_price"),
                                      t.get("exit_price"))
                # benchmark-relative label: did the trade beat the market over
                # its holding window? alpha = trade return - market return.
                bret = _bench_ret(opened_at, closed_at)
                alpha = (ret_pct - bret) if (ret_pct is not None and bret is not None) else None
                beat = (1 if alpha > 0 else 0) if alpha is not None else None
                try:
                    fjson = json.dumps(feats, default=str)
                except Exception:
                    fjson = "{}"
                cur = conn.execute(
                    "INSERT OR IGNORE INTO aj_trade_labels "
                    "(symbol, side, opened_at, closed_at, holding_days, regime, "
                    " features_json, realized_return_pct, realized_pnl_usd, "
                    " label, benchmark_return_pct, alpha_pct, beat_benchmark, "
                    " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sym, t.get("side"), opened_at, closed_at, hd, regime,
                     fjson, ret_pct, aj_db.money(pnl), label,
                     bret, alpha, beat, now))
                if cur.rowcount and cur.rowcount > 0:
                    built += 1
            # backfill benchmark fields on any pre-existing rows that lack them
            # (e.g. labeled before this feature, or when the fetch was down).
            try:
                miss = conn.execute(
                    "SELECT id, opened_at, closed_at, realized_return_pct "
                    "FROM aj_trade_labels WHERE beat_benchmark IS NULL").fetchall()
                for m in miss:
                    br = _bench_ret(m["opened_at"], m["closed_at"])
                    rp = _f(m["realized_return_pct"])
                    if br is None or rp is None:
                        continue
                    al = rp - br
                    conn.execute(
                        "UPDATE aj_trade_labels SET benchmark_return_pct=?, "
                        "alpha_pct=?, beat_benchmark=? WHERE id=?",
                        (br, al, 1 if al > 0 else 0, m["id"]))
            except Exception:
                log.debug("benchmark backfill skipped", exc_info=True)
            conn.commit()

        rows = aj_db.query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(label),0) AS wins FROM aj_trade_labels")
        n = int(rows[0]["n"]) if rows else 0
        wins = int(rows[0]["wins"]) if rows else 0
        base_rate = (wins / n) if n else 0.0
        return {"built": built, "total": n, "base_rate": base_rate}
    except Exception as e:
        log.exception("build_labels failed")
        return {"error": str(e)}


def training_set(target: str = "profit") -> Tuple[List[List[float]], List[int], List[str]]:
    """Read all aj_trade_labels and produce aligned (X, y, feature_names).

    X: equal-length float vectors in FEATURE_NAMES order (missing features
    imputed). y: the 0/1 target — `profit` uses the `label` column (net P&L>0);
    `alpha` uses `beat_benchmark` (trade beat the market over its holding
    window), dropping rows whose benchmark label is unknown. Fail-open to
    ([], [], FEATURE_NAMES)."""
    X: List[List[float]] = []
    y: List[int] = []
    use_alpha = str(target or "profit").lower() == "alpha"
    try:
        rows = aj_db.query(
            "SELECT features_json, label, beat_benchmark, regime FROM aj_trade_labels "
            "ORDER BY closed_at ASC, id ASC")
    except Exception:
        log.debug("training_set query failed", exc_info=True)
        return [], [], list(FEATURE_NAMES)

    for r in rows:
        try:
            feats = json.loads(r.get("features_json") or "{}")
            if not isinstance(feats, dict):
                feats = {}
        except Exception:
            feats = {}
        vec: List[float] = []
        for name, neutral in _NUMERIC_FEATURES:
            v = _f(feats.get(name))
            vec.append(neutral if v is None else v)
        # one-hot regime: prefer the features_json value, fall back to column.
        reg = feats.get("regime")
        if reg is None:
            reg = r.get("regime")
        reg = (str(reg).lower() if reg is not None else None)
        for lvl in _REGIME_LEVELS:
            vec.append(1.0 if reg == lvl else 0.0)
        if use_alpha:
            bb = r.get("beat_benchmark")
            if bb is None:          # no benchmark label -> can't use this row for alpha
                continue
            try:
                lab = 1 if int(bb) == 1 else 0
            except Exception:
                continue
        else:
            try:
                lab = 1 if int(r.get("label") or 0) == 1 else 0
            except Exception:
                lab = 0
        X.append(vec)
        y.append(lab)
    return X, y, list(FEATURE_NAMES)


def label_stats() -> Dict[str, Any]:
    """{n, base_rate, by_regime: {regime: {n, base_rate}}, oldest, newest}.
    Fail-open to {"error": ...}."""
    try:
        rows = aj_db.query(
            "SELECT regime, label, beat_benchmark, closed_at FROM aj_trade_labels")
        n = len(rows)
        wins = 0
        n_alpha = 0        # rows with a benchmark label
        alpha_wins = 0     # rows that beat the market
        by_regime: Dict[str, Dict[str, Any]] = {}
        oldest = None
        newest = None
        reg_acc: Dict[str, List[int]] = defaultdict(lambda: [0, 0])  # [n, wins]
        for r in rows:
            lab = 1 if int(r.get("label") or 0) == 1 else 0
            wins += lab
            bb = r.get("beat_benchmark")
            if bb is not None:
                n_alpha += 1
                try:
                    alpha_wins += 1 if int(bb) == 1 else 0
                except Exception:
                    pass
            reg = r.get("regime") or "unknown"
            reg_acc[reg][0] += 1
            reg_acc[reg][1] += lab
            ca = r.get("closed_at")
            if ca:
                if oldest is None or ca < oldest:
                    oldest = ca
                if newest is None or ca > newest:
                    newest = ca
        for reg, (rn, rw) in reg_acc.items():
            by_regime[reg] = {"n": rn, "base_rate": (rw / rn) if rn else 0.0}
        return {
            "n": n,
            "base_rate": (wins / n) if n else 0.0,           # % profitable
            "n_alpha": n_alpha,                               # rows with a market label
            "alpha_base_rate": (alpha_wins / n_alpha) if n_alpha else None,  # % that beat the market
            "by_regime": by_regime,
            "oldest": oldest,
            "newest": newest,
        }
    except Exception as e:
        log.exception("label_stats failed")
        return {"error": str(e)}
