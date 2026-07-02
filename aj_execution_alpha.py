"""
aj_execution_alpha.py — execution-alpha helpers for the AJ trading agent.

PURPOSE
    Buy/sell at better prices and times, and skip trades whose edge won't
    survive transaction costs. These are *pure* helper functions that the
    operator / strategy / exit-rules call later; they hold no state and make
    no I/O of their own beyond reading the canned market modules
    (fetcher / earnings / gex_engine).

DESIGN CONTRACT (read before touching)
    - Pure helpers. NEVER raise — every public function is wrapped so that any
      internal error returns the SAFE default documented on the function.
    - Every behaviour is gated behind a cfg flag that defaults OFF, so wiring
      this module into the operator changes nothing until a flag is enabled.
    - "Safe default" = the permissive / pre-existing behaviour:
        * entry_price      -> market order (we behaved this way before)
        * cost_gate        -> ok=True   (never block on uncertainty; the risk
                                          gate is the hard authority)
        * event_blackout   -> not blocked
        * gex_entry_lean   -> adjust=0.0 (no nudge)
      EXITS are the exception: when their flag is off they simply DO NOT FIRE
      (time_stop -> exit=False, profit_ladder -> []). Failing open on an exit
      means "don't force a sell", which is also the safe direction.

    No new dependencies. Imports of sibling modules are done lazily inside the
    functions so that importing this module never drags in yfinance et al. and
    a missing/broken sibling fails open rather than blowing up at import time.
"""

from __future__ import annotations

from datetime import datetime, timezone


# ── small internal helpers ────────────────────────────────────────────────

def _f(v, default=None):
    """Best-effort float coercion that never raises."""
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _cfg_get(cfg, key, default):
    """cfg may be a dict-like or None; .get that never raises."""
    try:
        if cfg is None:
            return default
        val = cfg.get(key, default)
        return default if val is None else val
    except Exception:
        return default


def _quote_mid(quote):
    """
    Derive a usable mid price from a quote dict.

    fetcher.get_quote() returns a `price` (last) and may or may not carry
    bid/ask. Prefer the bid/ask mid when both are present and sane; otherwise
    fall back to last price. Returns (mid, bid, ask) where bid/ask may be None.
    """
    if not isinstance(quote, dict):
        return (None, None, None)
    bid = _f(quote.get("bid"))
    ask = _f(quote.get("ask"))
    last = _f(quote.get("price")) or _f(quote.get("last")) or _f(quote.get("mark"))
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        return ((bid + ask) / 2.0, bid, ask)
    return (last, bid, ask)


def _spread_pct(quote, cfg):
    """
    Round-the-clock spread as a percent of mid. Uses bid/ask if present,
    else cfg.get("assumed_spread_bps", 10). Returns a percent (e.g. 0.10).
    """
    mid, bid, ask = _quote_mid(quote)
    if mid and bid is not None and ask is not None and bid > 0 and ask >= bid:
        return (ask - bid) / mid * 100.0
    assumed_bps = _f(_cfg_get(cfg, "assumed_spread_bps", 10), 10.0)
    return assumed_bps / 100.0  # bps -> percent


# ── 1. entry_price ─────────────────────────────────────────────────────────

def entry_price(symbol, side, quote, cfg):
    """
    Choose an order type / limit price for a NEW entry.

    Signature: entry_price(symbol, side, quote, cfg) -> dict
        {"order_type": "market"|"limit", "limit_price": float|None, "reason": str}

    Behaviour:
        - cfg["limit_entry"] OFF (default) -> market order.
        - ON: post a limit slightly *through the mid toward a small discount*
          so we don't pay the full spread / chase the tape.
            BUY  -> limit = mid * (1 - offset_bps/10_000)  (pay a touch less)
            SELL -> limit = mid * (1 + offset_bps/10_000)  (ask a touch more)
          offset from cfg["limit_entry_offset_bps"] (default 10 bps).

    Safe default (flag off, bad quote, or any error): market order.
    """
    try:
        if not _cfg_get(cfg, "limit_entry", False):
            return {"order_type": "market", "limit_price": None,
                    "reason": "limit_entry off -> market"}

        mid, _bid, _ask = _quote_mid(quote)
        if not mid or mid <= 0:
            return {"order_type": "market", "limit_price": None,
                    "reason": "no usable mid -> market"}

        offset_bps = _f(_cfg_get(cfg, "limit_entry_offset_bps", 10), 10.0)
        frac = offset_bps / 10000.0
        s = str(side or "").strip().lower()
        if s in ("buy", "long", "b"):
            limit = round(mid * (1.0 - frac), 4)
            return {"order_type": "limit", "limit_price": limit,
                    "reason": "buy limit {}bps below mid {}".format(offset_bps, round(mid, 4))}
        if s in ("sell", "short", "s"):
            limit = round(mid * (1.0 + frac), 4)
            return {"order_type": "limit", "limit_price": limit,
                    "reason": "sell limit {}bps above mid {}".format(offset_bps, round(mid, 4))}
        # Unknown side -> don't guess direction; fail open to market.
        return {"order_type": "market", "limit_price": None,
                "reason": "unknown side '{}' -> market".format(side)}
    except Exception as e:  # pragma: no cover - defensive
        return {"order_type": "market", "limit_price": None,
                "reason": "error -> market: {}".format(e)}


# ── 2. cost_gate ────────────────────────────────────────────────────────────

def cost_gate(edge_pts, symbol, quote, cfg):
    """
    Decide whether an expected edge survives round-trip transaction cost.

    Signature: cost_gate(edge_pts, symbol, quote, cfg) -> dict
        {"ok": bool, "reason": str, "cost_pct": float}

    `edge_pts` is the expected edge expressed as a PERCENT (e.g. 2.5 == 2.5%).

    Cost model (all in percent). A round trip = ENTER + EXIT, and on each leg we
    pay HALF the quoted spread (cross from mid to the touch) plus one fee:
        spread cost = (spread%/2) per leg × 2 legs   == FULL spread%
        fee cost    = fee%        per leg × 2 legs   == 2 × fee%
        round_trip  = FULL spread% + 2 × fee%        (half-spread per leg, twice)
        required    = round_trip × cost_edge_multiple (safety multiple)
    So `round_trip` is EXACTLY one full quoted spread plus two fees — consistent
    with the half-spread-per-leg assumption above (½·2 = 1 full spread).
    Block (ok=False) when edge_pts < required.

    Gating:
        - cfg["cost_gate"] OFF (default) -> always ok=True.
    Config:
        assumed_spread_bps (10), fee_bps (0), cost_edge_multiple (1.5).

    Safe default (flag off or any error): ok=True (don't block on uncertainty
    — the risk gate is the hard authority).
    """
    try:
        if not _cfg_get(cfg, "cost_gate", False):
            return {"ok": True, "reason": "cost_gate off", "cost_pct": 0.0}

        spread_pct = _spread_pct(quote, cfg)          # FULL quoted spread, percent
        fee_bps = _f(_cfg_get(cfg, "fee_bps", 0), 0.0)
        fee_pct = fee_bps / 100.0                      # per leg, percent

        # Round trip = half the quoted spread per leg, crossed twice (enter+exit)
        # == one FULL spread, plus one fee per leg (2 fees). Written explicitly:
        #   round_trip = (spread_pct/2)*2 + fee_pct*2 = spread_pct + 2*fee_pct
        half_spread = spread_pct / 2.0
        round_trip = 2.0 * half_spread + 2.0 * fee_pct   # == spread_pct + 2*fee_pct
        multiple = _f(_cfg_get(cfg, "cost_edge_multiple", 1.5), 1.5)
        required = round_trip * multiple

        edge = _f(edge_pts, None)
        if edge is None:
            # Uncoercible edge -> fail open; the risk gate is the hard authority.
            return {"ok": True, "reason": "edge unmeasurable -> ok", "cost_pct": 0.0}
        cost_pct = round(required, 4)
        if edge < required:
            return {"ok": False,
                    "reason": "edge {:.3f}% < cost {:.3f}% (rt {:.3f}% x {})".format(
                        edge, required, round_trip, multiple),
                    "cost_pct": cost_pct}
        return {"ok": True,
                "reason": "edge {:.3f}% >= cost {:.3f}%".format(edge, required),
                "cost_pct": cost_pct}
    except Exception as e:  # pragma: no cover - defensive
        return {"ok": True, "reason": "error -> ok: {}".format(e), "cost_pct": 0.0}


# ── 3. time_stop ─────────────────────────────────────────────────────────────

def _position_opened_at(position, opened_at):
    """Resolve the entry timestamp (datetime, tz-aware UTC) from position/arg."""
    raw = opened_at
    if raw is None and isinstance(position, dict):
        for k in ("opened_at", "entry_time", "entry_ts", "opened", "open_time"):
            if position.get(k):
                raw = position.get(k)
                break
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def time_stop(position, cfg, now=None, mark=None, opened_at=None):
    """
    Time-based exit: a thesis that hasn't played out should free the capital.

    Signature: time_stop(position, cfg, now=None, mark=None, opened_at=None) -> dict
        {"exit": bool, "reason": str}

    Fires (exit=True) when ALL hold:
        - cfg["time_stop_days"] > 0
        - the position has been held strictly LONGER than that many days
        - the position is NOT up at least cfg["time_stop_min_gain_pct"] (%)

    Inputs:
        entry time   <- position["opened_at"|...] or opened_at arg
        current mark <- mark arg, else position["mark"|"last"|"price"]
        avg_cost     <- position["avg_cost"]
        now          <- defaults to datetime.now(timezone.utc)

    OFF (time_stop_days == 0) or any error/missing data -> exit=False
    (don't force a sell on uncertainty).
    """
    try:
        days_cfg = _f(_cfg_get(cfg, "time_stop_days", 0), 0.0)
        if not days_cfg or days_cfg <= 0:
            return {"exit": False, "reason": "time_stop off"}

        opened = _position_opened_at(position, opened_at)
        if opened is None:
            return {"exit": False, "reason": "no entry time -> hold"}

        # aj_db.utc_now honors the aj_replay sim clock (a frozen 'now' during
        # historical replays); falls back to wall time outside a replay.
        import aj_db
        now_dt = now or aj_db.utc_now()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        held_days = (now_dt - opened).total_seconds() / 86400.0
        if held_days <= days_cfg:
            return {"exit": False,
                    "reason": "held {:.2f}d <= {}d window".format(held_days, days_cfg)}

        # Within the held window — now check whether the thesis has paid off.
        avg_cost = _f(position.get("avg_cost")) if isinstance(position, dict) else None
        cur = _f(mark)
        if cur is None and isinstance(position, dict):
            cur = (_f(position.get("mark")) or _f(position.get("last"))
                   or _f(position.get("price")))
        if avg_cost is None or avg_cost <= 0 or cur is None:
            # Held past the window but we can't measure gain -> the thesis has
            # had its time; treat as not-up-enough and exit.
            return {"exit": True,
                    "reason": "held {:.2f}d > {}d, gain unmeasurable -> exit".format(
                        held_days, days_cfg)}

        gain_pct = (cur - avg_cost) / avg_cost * 100.0
        min_gain = _f(_cfg_get(cfg, "time_stop_min_gain_pct", 0), 0.0)
        if gain_pct < min_gain:
            return {"exit": True,
                    "reason": "held {:.2f}d > {}d and up {:.2f}% < {}% min -> exit".format(
                        held_days, days_cfg, gain_pct, min_gain)}
        return {"exit": False,
                "reason": "held {:.2f}d but up {:.2f}% >= {}% -> hold".format(
                    held_days, gain_pct, min_gain)}
    except Exception as e:  # pragma: no cover - defensive
        return {"exit": False, "reason": "error -> hold: {}".format(e)}


# ── 4. profit_ladder ─────────────────────────────────────────────────────────

_DEFAULT_RUNGS = [
    {"gain_pct": 15, "trim_frac": 0.33},
    {"gain_pct": 30, "trim_frac": 0.5},
]


def profit_ladder(position, cfg, mark=None):
    """
    Scale-out plan: trim into strength at configured gain rungs.

    Signature: profit_ladder(position, cfg, mark=None) -> list[dict]
        each rung: {"gain_pct": float, "trim_frac": float, "triggered": bool}

    cfg["profit_ladder"] OFF (default) -> [].
    Rungs come from cfg["profit_ladder_rungs"] or a sane default
    ([{15,0.33},{30,0.5}]). A rung is `triggered` when the position's current
    gain% >= that rung's gain_pct.

    Current gain is from (mark or position mark) vs position["avg_cost"]; if it
    can't be computed the rungs are returned untriggered (advisory only).

    Any error -> [].
    """
    try:
        if not _cfg_get(cfg, "profit_ladder", False):
            return []
        if not isinstance(position, dict):
            return []

        raw_rungs = _cfg_get(cfg, "profit_ladder_rungs", _DEFAULT_RUNGS)
        rungs = []
        for r in (raw_rungs or []):
            try:
                gp = _f(r.get("gain_pct"))
                tf = _f(r.get("trim_frac"))
                if gp is None or tf is None:
                    continue
                rungs.append({"gain_pct": gp, "trim_frac": tf})
            except Exception:
                continue
        if not rungs:
            return []

        # Determine current gain%.
        gain_pct = None
        avg_cost = _f(position.get("avg_cost")) if isinstance(position, dict) else None
        cur = _f(mark)
        if cur is None and isinstance(position, dict):
            cur = (_f(position.get("mark")) or _f(position.get("last"))
                   or _f(position.get("price")))
        if avg_cost and avg_cost > 0 and cur is not None:
            gain_pct = (cur - avg_cost) / avg_cost * 100.0

        out = []
        for r in sorted(rungs, key=lambda x: x["gain_pct"]):
            triggered = (gain_pct is not None and gain_pct >= r["gain_pct"])
            out.append({"gain_pct": r["gain_pct"],
                        "trim_frac": r["trim_frac"],
                        "triggered": bool(triggered)})
        return out
    except Exception:  # pragma: no cover - defensive
        return []


# ── 5. event_blackout ────────────────────────────────────────────────────────

def _next_earnings_date(symbol):
    """
    Best-effort next-earnings date (datetime.date) for `symbol`, or None.

    Uses earnings.get_earnings_calendar([symbol]) which returns rows carrying an
    ISO `earnings_date`. Fail-open to None on any error / unknown date.
    """
    try:
        import earnings  # lazy: avoids dragging yfinance in at import time
        rows = earnings.get_earnings_calendar([symbol])
        if not rows:
            return None
        # Calendar is sorted ascending; first future row is the next event.
        import datetime as _dt
        for row in rows:
            ed = str(row.get("earnings_date") or "")[:10]
            try:
                return _dt.date.fromisoformat(ed)
            except (TypeError, ValueError):
                continue
        return None
    except Exception:
        return None


def event_blackout(symbol, cfg, now=None):
    """
    Block NEW entries in the run-up to a known earnings event.

    Signature: event_blackout(symbol, cfg, now=None) -> dict
        {"blocked": bool, "reason": str}

    cfg["event_blackout_days"] OFF (== 0, default) -> not blocked.
    ON: block when the next earnings date is within that many days from `now`
    (0 <= days_until <= window). Unknown earnings date -> not blocked (fail
    open). Past dates are ignored.

    Any error -> not blocked.
    """
    try:
        window = _f(_cfg_get(cfg, "event_blackout_days", 0), 0.0)
        if not window or window <= 0:
            return {"blocked": False, "reason": "event_blackout off"}

        ed = _next_earnings_date(symbol)
        if ed is None:
            return {"blocked": False, "reason": "no known earnings date -> allow"}

        import datetime as _dt
        today = (now.date() if isinstance(now, datetime)
                 else (now or _dt.date.today()))
        if isinstance(today, datetime):
            today = today.date()
        days_until = (ed - today).days
        if days_until < 0:
            return {"blocked": False, "reason": "earnings {} in the past -> allow".format(ed)}
        if days_until <= window:
            return {"blocked": True,
                    "reason": "earnings {} in {}d <= {}d blackout".format(
                        ed.isoformat(), days_until, int(window))}
        return {"blocked": False,
                "reason": "earnings {} in {}d > {}d window".format(
                    ed.isoformat(), days_until, int(window))}
    except Exception as e:  # pragma: no cover - defensive
        return {"blocked": False, "reason": "error -> allow: {}".format(e)}


# ── 6. gex_entry_lean ────────────────────────────────────────────────────────

def gex_entry_lean(symbol, side, cfg):
    """
    Advisory timing nudge from dealer gamma positioning.

    Signature: gex_entry_lean(symbol, side, cfg) -> dict
        {"adjust": float, "reason": str}

    `adjust` is a small dimensionless lean in [-step, +step] (step from
    cfg["gex_timing_step"], default 0.5): positive = favourable to enter now,
    negative = be cautious / wait.

    Heuristic (long-only framing for `side` == buy):
        - spot ABOVE gamma-flip & net_gex > 0 (positive-gamma "pinned" zone)
          -> mild positive lean (mean-reverting, low-vol regime to accumulate).
        - spot BELOW flip / net_gex < 0 (negative-gamma, dealers amplify moves)
          -> mild negative lean (caution entering the chop).
      For a SELL the sign is mirrored.

    cfg["gex_timing"] OFF (default) -> adjust=0.0.
    Unknown/error GEX -> adjust=0.0 (fail open: no nudge).
    """
    try:
        if not _cfg_get(cfg, "gex_timing", False):
            return {"adjust": 0.0, "reason": "gex_timing off"}

        import gex_engine  # lazy
        g = gex_engine.compute_gex(symbol)
        if not isinstance(g, dict) or "error" in g:
            return {"adjust": 0.0, "reason": "gex unavailable -> no nudge"}

        spot = _f(g.get("spot_price"))
        flip = _f(g.get("gamma_flip_price"))
        net_gex = _f(g.get("net_gex"))
        if spot is None or flip is None or net_gex is None:
            return {"adjust": 0.0, "reason": "gex incomplete -> no nudge"}

        step = _f(_cfg_get(cfg, "gex_timing_step", 0.5), 0.5)

        # Base lean from a long perspective.
        if spot >= flip and net_gex > 0:
            base = +step
            why = "spot {} >= flip {} & +gamma (pinned) -> favourable".format(
                round(spot, 2), round(flip, 2))
        elif spot < flip or net_gex < 0:
            base = -step
            why = "spot {} vs flip {} / net_gex {} negative-gamma -> cautious".format(
                round(spot, 2), round(flip, 2), round(net_gex, 2))
        else:
            base = 0.0
            why = "neutral gamma -> no nudge"

        s = str(side or "").strip().lower()
        if s in ("sell", "short", "s"):
            base = -base  # mirror for the short side
            why = "sell side: " + why
        return {"adjust": round(base, 4), "reason": why}
    except Exception as e:  # pragma: no cover - defensive
        return {"adjust": 0.0, "reason": "error -> no nudge: {}".format(e)}
