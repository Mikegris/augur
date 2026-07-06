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
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import aj_db
import database as db

log = logging.getLogger("augur.aj_features")

# ── canonical feature contract ────────────────────────────────────────────────
# THE ML CONTRACT. Order is fixed; never reorder/insert in the middle — only
# append (and bump the model). Each entry: (name, neutral_impute_value). The
# numeric features come from the decision-time scan snapshot; `side` is encoded
# as long(buy/cover-close=1.0) vs short(0.0); regime is one-hot expanded below.
#
# NOTE `holding_days` is deliberately NOT a model feature: it is only knowable
# at trade EXIT, but the meta-label model scores P(profit|setup) at ENTRY time
# (aj_operator never supplies it at inference, so it was silently imputed —
# label leakage in training + train/serve skew in production). It stays in the
# DB row / features_json for analysis only. A previously-persisted model that
# still lists it in its stored feature_names keeps scoring self-consistently
# (aj_metalabel aligns the vector to the MODEL's stored names, imputing the
# stored mean); the next retrain picks up this leak-free contract.
_NUMERIC_FEATURES: List[Tuple[str, float]] = [
    ("edge", 0.0),
    ("conviction", 0.5),
    ("prob_up", 0.5),
    ("side_long", 1.0),
    # ── v2 contract: point-in-time technicals at ENTRY (appended per the
    # append-only rule; a model trained on the v1 names keeps scoring
    # self-consistently until its next retrain adopts these).
    ("rsi14", 50.0),           # 14d RSI of the symbol's closes as of entry
    ("vol14_pct", 0.0),        # mean |daily change| over 14d, % of price
                               # (close-to-close ATR proxy; no OHLC needed)
    ("sma20_dist_pct", 0.0),   # % distance of close from its 20d SMA
    ("rs_spy20_pct", 0.0),     # 20d return minus SPY's 20d return (rel strength)
    # ── v3 contract: market context + regime-conditional edge. The deployed
    # model is deliberately LINEAR (JSON-safe, no pickle), so regime gating is
    # expressed as interaction features — edge×regime gives the linear model a
    # separate edge coefficient PER regime, which is exactly a regime-gated
    # entry filter once fit. Computed by finalize_features() at BOTH label
    # time (point-in-time VIX) and inference (current VIX) — no skew.
    ("vix", 20.0),             # VIX close at entry (neutral ~long-run median)
    ("edge_x_bull", 0.0),      # edge when regime==bull else 0
    ("edge_x_bear", 0.0),      # edge when regime==bear else 0
    ("edge_x_chop", 0.0),      # edge when regime==chop else 0
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
    """Decision-time snapshot for `symbol` relative to opened_at.

    `aj_cycle_stats.ts` is stamped at cycle END, so the cycle that DECIDED this
    trade writes its snapshot shortly AFTER the fill — a plain "latest ts <=
    opened_at" match selects the PREVIOUS cycle's edge/prob. Preference order:

      1. the EARLIEST snapshot within a short window after the open (<= 30 min:
         the same cycle's end-stamp);
      2. the latest snapshot at/just-before the open (previous cycle — still
         entry-time information, no look-ahead);
      3. for first-ever trades with no prior cycle: the earliest post-open
         snapshot, but only within a DAY of the open — anything later is
         post-decision (future) data, not a near-miss of the entry scan.

    {} when the symbol was never scanned / nothing usable."""
    lst = idx.get((symbol or "").upper())
    if not lst:
        return {}
    if opened_at is None:
        return dict(lst[0][1])
    same_cycle_max = opened_at + timedelta(minutes=30)
    prior = None
    for ts, feats in lst:
        if ts <= opened_at:
            prior = feats                  # latest at/just-before the open
        elif ts <= same_cycle_max:
            return dict(feats)             # same cycle's end-stamp — best match
        else:
            break                          # sorted ascending: nothing closer follows
    if prior is not None:
        return dict(prior)
    # No snapshot at-or-before the open at all (first trades): accept the
    # earliest post-open snapshot only if it's within a day of the open.
    first_ts, first_feats = lst[0]
    if first_ts <= opened_at + timedelta(days=1):
        return dict(first_feats)
    return {}


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
        # Consume the symbol's FIFO round-trip for EVERY canonical close — even
        # one that already carries entry fields. Skipping the pop for an
        # already-enriched record left its round-trip in the deque, so the NEXT
        # entry-less record for the symbol popped the EARLIER trade's entry
        # (off-by-one misalignment when canonical records are mixed). Canonical
        # (non-None) fields always win — including realized P&L, net of fees.
        rt = by_sym[sym].popleft() if by_sym.get(sym) else None
        if rt is None:
            out.append(t)
            continue
        merged = dict(rt)
        merged.update({k: v for k, v in t.items() if v is not None})
        out.append(merged)
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


# ── context features (v3 contract) ───────────────────────────────────────────

def finalize_features(feats: Dict[str, Any], regime: Optional[str] = None,
                      vix: Optional[float] = None) -> Dict[str, Any]:
    """Complete a feature dict with the v3 context features — ONE shared
    implementation for training (point-in-time regime/VIX) and inference
    (current regime/VIX), so there is no train/serve skew. Mutates + returns
    `feats`. Missing inputs leave features absent (imputed to neutral by the
    vectorizer/model). Never raises."""
    try:
        if vix is not None:
            try:
                fv = float(vix)
                if fv == fv and fv > 0:
                    feats["vix"] = round(fv, 2)
            except (TypeError, ValueError):
                pass
        reg = regime
        if reg is None:
            reg = next((lvl for lvl in _REGIME_LEVELS
                        if _f(feats.get("regime_" + lvl)) == 1.0), None)
        if reg is None:
            r = feats.get("regime")
            reg = str(r).lower() if r else None
        edge = _f(feats.get("edge"))
        if edge is not None and reg in _REGIME_LEVELS:
            for lvl in _REGIME_LEVELS:
                feats["edge_x_" + lvl] = edge if reg == lvl else 0.0
    except Exception:
        log.debug("finalize_features failed", exc_info=True)
    return feats


# ── technical features (v2 contract) ─────────────────────────────────────────

def tech_features_from_closes(closes: List[float],
                              spy_closes: Optional[List[float]] = None) -> Dict[str, float]:
    """Pure v2 technicals from a close series ENDING at the decision date —
    the single implementation shared by training (historical closes as of
    entry) and inference (live closes), so there is no train/serve skew.
    Returns only the features it can honestly compute; callers/imputation
    handle the rest. Never raises."""
    out: Dict[str, float] = {}
    try:
        px = [float(c) for c in (closes or []) if c is not None and float(c) > 0]
        if len(px) >= 15:
            deltas = [px[i] - px[i - 1] for i in range(len(px) - 14, len(px))]
            gains = sum(d for d in deltas if d > 0)
            losses = sum(-d for d in deltas if d < 0)
            if gains + losses > 0:
                out["rsi14"] = round(100.0 * gains / (gains + losses), 2)
            if px[-1] > 0:
                out["vol14_pct"] = round(
                    (sum(abs(d) for d in deltas) / 14.0) / px[-1] * 100.0, 3)
        if len(px) >= 20 and px[-1] > 0:
            sma20 = sum(px[-20:]) / 20.0
            if sma20 > 0:
                out["sma20_dist_pct"] = round((px[-1] / sma20 - 1.0) * 100.0, 3)
        if len(px) >= 21:
            r20 = (px[-1] / px[-21] - 1.0) * 100.0
            spy = [float(c) for c in (spy_closes or []) if c is not None and float(c) > 0]
            if len(spy) >= 21:
                out["rs_spy20_pct"] = round(
                    r20 - (spy[-1] / spy[-21] - 1.0) * 100.0, 3)
    except Exception:
        log.debug("tech_features_from_closes failed", exc_info=True)
    return out


_LIVE_TECH_MEMO: Dict[str, Any] = {}


def live_tech_features(symbol: str) -> Dict[str, float]:
    """v2 technicals from CURRENT history — the inference-time counterpart of
    the training-side point-in-time computation (same pure function). Memoized
    10 min per symbol so the operator loop doesn't refetch. Fail-open to {}."""
    import time as _time
    sym = (symbol or "").upper()
    if not sym or sym.startswith("OPT:"):
        return {}
    now = _time.time()
    hit = _LIVE_TECH_MEMO.get(sym)
    if hit and now - hit[0] < 600:
        return hit[1]
    feats: Dict[str, float] = {}
    try:
        import fetcher
        bars = fetcher.get_chart_data(sym, "6mo", "1d") or []
        closes = [b.get("close") for b in bars if isinstance(b, dict)]
        spy_bars = None
        spy_hit = _LIVE_TECH_MEMO.get("__SPY__")
        if spy_hit and now - spy_hit[0] < 600:
            spy_bars = spy_hit[1]
        else:
            spy_bars = [b.get("close") for b in
                        (fetcher.get_chart_data("SPY", "6mo", "1d") or [])
                        if isinstance(b, dict)]
            _LIVE_TECH_MEMO["__SPY__"] = (now, spy_bars)
        feats = tech_features_from_closes(closes, spy_bars)
    except Exception:
        log.debug("live_tech_features failed for %s", sym, exc_info=True)
    _LIVE_TECH_MEMO[sym] = (now, feats)
    return feats


# ── persisted decision features (#13 — v10 schema) ───────────────────────────

def _persisted_entry_features(symbol: str, close_side: str,
                              opened_at: Any) -> Optional[Dict[str, Any]]:
    """The features_json persisted on the aj_proposals row that OPENED this
    round-trip (written at decision time by the operator). Exact entry state —
    preferred over the cycle-snapshot reconstruction, which is stamped at
    cycle END and can be a cycle stale. The entry proposal is the latest one
    for the symbol on the ENTRY side within [open-30min, open+5min] (the fill
    lands seconds after the proposal row). None when unavailable."""
    try:
        if not opened_at:
            return None
        odt = aj_db.parse_iso(str(opened_at))
        if odt is None:
            return None
        from datetime import timedelta
        entry_side = "sell" if str(close_side or "").lower() == "buy" else "buy"
        rows = aj_db.query(
            "SELECT features_json FROM aj_proposals WHERE symbol=? AND side=? "
            "AND features_json IS NOT NULL AND created_at >= ? AND created_at <= ? "
            "ORDER BY created_at DESC LIMIT 1",
            ((symbol or "").upper(), entry_side,
             (odt - timedelta(minutes=30)).isoformat(),
             (odt + timedelta(minutes=5)).isoformat()))
        if not rows:
            return None
        d = json.loads(rows[0].get("features_json") or "{}")
        return d if isinstance(d, dict) and d else None
    except Exception:
        log.debug("persisted entry features lookup failed", exc_info=True)
        return None


# ── point-in-time helpers (ET dates + historical regime) ──────────────────────

def _et_date_str(ts: Any) -> Optional[str]:
    """ET (America/New_York) calendar date 'YYYY-MM-DD' for a UTC ISO timestamp.
    Market-data maps (benchmark closes, SPY history) are keyed by ET dates, so a
    raw UTC [:10] slice mislabels an evening fill (>= 8pm ET) as the NEXT day.
    Fail-open to the raw date slice when tz conversion is unavailable."""
    if not ts:
        return None
    dt = aj_db.parse_iso(str(ts))
    if dt is None:
        s = str(ts)
        return s[:10] if len(s) >= 10 else None
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return str(ts)[:10]


def _spy_history() -> List[Tuple[str, float]]:
    """Sorted [(ET date, close), ...] for SPY over 2y — the source for the
    POINT-IN-TIME regime reconstruction (fetched ONCE per build_labels call).
    Module-level seam so tests can stub it. Fail-open to []."""
    try:
        import aj_benchmark
        closes = aj_benchmark._index_closes("SPY", "2y") or {}
        return sorted(closes.items())
    except Exception:
        log.debug("SPY history unavailable for regime reconstruction", exc_info=True)
        return []


def _regime_asof(spy_hist: List[Tuple[str, float]], opened_at: Any) -> Optional[str]:
    """Market regime AS OF a trade's open, from the SPY closes ending at that
    date (same 50d/200d-SMA thresholds as aj_alpha.detect_regime — reused via
    aj_alpha.regime_from_closes). Stamping TODAY's detect_regime() on historical
    trades was look-ahead mislabeling: the initial 365-day backfill wrote one
    identical regime on every row. None (-> NULL regime, one-hots 0.0) when the
    open time or enough history is unavailable — NEVER today's regime."""
    if not spy_hist:
        return None
    d = _et_date_str(opened_at)
    if not d:
        return None
    closes = [c for dt, c in spy_hist if dt <= d]
    # detect_regime needs >= 60 closes for a meaningful verdict; with less we
    # genuinely don't know -> None rather than a fake "chop".
    if len(closes) < 60:
        return None
    try:
        import aj_alpha
        return aj_alpha.regime_from_closes(closes)
    except Exception:
        log.debug("regime_asof failed", exc_info=True)
        return None


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
        # SPY history fetched ONCE; each trade gets the regime AS OF its own
        # open (point-in-time), never today's detect_regime() (look-ahead).
        spy_hist = _spy_history()
        # ^VIX history for the point-in-time market-fear context feature (v3).
        vix_hist: List[Tuple[str, float]] = []
        try:
            import aj_benchmark
            vix_hist = sorted((aj_benchmark._index_closes(
                "^VIX", "2y" if lookback_days > 200 else "1y") or {}).items())
        except Exception:
            log.debug("VIX history unavailable for labeling", exc_info=True)

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
            """% market return between two dates (close on/before each).
            bench_closes is keyed by ET dates (aj_benchmark._index_closes), so
            the UTC ISO timestamps must be converted to ET before slicing."""
            if not bench_closes or not start or not end:
                return None
            s = _et_date_str(start)
            e = _et_date_str(end)
            if not s or not e:
                return None

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
        # Per-symbol close history for the point-in-time v2 technicals —
        # fetched lazily ONCE per symbol (trades repeat symbols heavily).
        sym_hist: Dict[str, List[Tuple[str, float]]] = {}

        def _hist(s: str) -> List[Tuple[str, float]]:
            if s not in sym_hist:
                try:
                    import aj_benchmark
                    sym_hist[s] = sorted(
                        (aj_benchmark._index_closes(s, "2y") or {}).items())
                except Exception:
                    sym_hist[s] = []
            return sym_hist[s]

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
                regime = _regime_asof(spy_hist, opened_at)
                feats = _assemble_features(t, snap, hd, regime)
                # v2 technicals AS OF the entry date — same pure function the
                # live inference path uses, so there is no train/serve skew.
                d_open = _et_date_str(opened_at)
                if d_open and not sym.startswith("OPT:"):
                    closes_asof = [c for dt2, c in _hist(sym) if dt2 <= d_open]
                    spy_asof = [c for dt2, c in spy_hist if dt2 <= d_open]
                    feats.update(tech_features_from_closes(closes_asof, spy_asof))
                # Exact decision-time features persisted on the ENTRY proposal
                # (v10, #13) beat any reconstruction — overlay them last.
                pers = _persisted_entry_features(sym, t.get("side"), opened_at)
                if pers:
                    reg_p = next((lvl for lvl in _REGIME_LEVELS
                                  if _f(pers.get("regime_" + lvl)) == 1.0), None)
                    if reg_p:
                        regime = reg_p
                        feats["regime"] = reg_p
                    feats.update({k: v for k, v in pers.items()
                                  if not str(k).startswith("regime_")})
                # v3 context: point-in-time VIX + edge×regime interactions —
                # the same finalize the live inference path applies.
                pit_vix = None
                if d_open and vix_hist:
                    prior = [v for dt2, v in vix_hist if dt2 <= d_open]
                    pit_vix = prior[-1] if prior else None
                finalize_features(feats, regime=regime, vix=pit_vix)
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


def vectorize_rows(rows: List[Dict[str, Any]], target: str = "profit"
                   ) -> Tuple[List[List[float]], List[int], List[str],
                              List[Tuple[Optional[str], Optional[str]]]]:
    """Vectorize aj_trade_labels-shaped row dicts into aligned
    (X, y, feature_names, windows). The ONE assembly used by the live
    training_set AND cross-DB pooled training (aj_replay pool-train), so a
    model trained on pooled replay labels scores live features identically.

    Rows must be time-ordered (closed_at ASC). `windows` carries each KEPT
    row's (opened_at, closed_at) for the purged walk-forward split. Rows are
    finalized (v3 interactions derived from stored edge+regime) so labels
    written before the v3 contract still vectorize consistently."""
    X: List[List[float]] = []
    y: List[int] = []
    windows: List[Tuple[Optional[str], Optional[str]]] = []
    use_alpha = str(target or "profit").lower() == "alpha"
    for r in rows or []:
        try:
            feats = json.loads(r.get("features_json") or "{}")
            if not isinstance(feats, dict):
                feats = {}
        except Exception:
            feats = {}
        # one-hot regime: prefer the features_json value, fall back to column.
        reg = feats.get("regime")
        if reg is None:
            reg = r.get("regime")
        reg = (str(reg).lower() if reg is not None else None)
        finalize_features(feats, regime=reg)
        vec: List[float] = []
        for name, neutral in _NUMERIC_FEATURES:
            v = _f(feats.get(name))
            vec.append(neutral if v is None else v)
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
        windows.append((r.get("opened_at"), r.get("closed_at")))
    return X, y, list(FEATURE_NAMES), windows


def training_set(target: str = "profit") -> Tuple[List[List[float]], List[int], List[str]]:
    """Read all aj_trade_labels and produce aligned (X, y, feature_names).

    X: equal-length float vectors in FEATURE_NAMES order (missing features
    imputed). y: the 0/1 target — `profit` uses the `label` column (net P&L>0);
    `alpha` uses `beat_benchmark` (trade beat the market over its holding
    window), dropping rows whose benchmark label is unknown. Fail-open to
    ([], [], FEATURE_NAMES)."""
    try:
        rows = aj_db.query(
            "SELECT features_json, label, beat_benchmark, regime, opened_at, "
            "closed_at FROM aj_trade_labels ORDER BY closed_at ASC, id ASC")
    except Exception:
        log.debug("training_set query failed", exc_info=True)
        return [], [], list(FEATURE_NAMES)
    X, y, names, _ = vectorize_rows(rows, target)
    return X, y, names


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
