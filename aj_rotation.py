"""AJTA — capital-rotation (opportunity-cost) engine.

The agent only ever exits on a mechanical trigger (stop / take-profit / trailing
/ time-stop / profit-ladder / signal flip). It has no way to say "I'm fully
invested, but this new name is clearly better than my weakest holding, so rotate
the capital." On a capped book (e.g. 12/12 positions, $0 free cash) that means a
strong fresh signal is simply dropped — the observed failure mode where the
agent sits idle for a whole session while holding names down 8-9%.

`rotation_plan` is the pure, deterministic decision: given each HELD name's
forward edge and each fresh CANDIDATE's forward edge, it returns the set of
swaps (sell weak holding -> free capital for a better candidate) that clear a
cost/hysteresis threshold. The operator executes the sells; the normal buy loop
then funds the incoming names with the freed cash.

Design guarantees (why this can't churn the book):
  * ONLY fires when capital-constrained (the operator passes that in) — when
    there's cash/a free slot, the agent just buys; it never sells to buy.
  * ONLY displaces holdings whose forward edge is BELOW a floor — a position
    still showing conviction is never rotated out.
  * The candidate must beat the holding by a hard margin (min_edge_gain), so a
    marginal improvement never triggers a taxable round-trip.
  * A just-opened name (age < min_hold_days) is untouchable — no in-and-out.
  * Tax-aware: a holding nearing the 1-year long-term mark demands EXTRA edge to
    displace, so the agent doesn't rotate away a soon-cheaper tax lot for a
    small edge gain.
  * Capped per cycle.

Pure function, no I/O, deterministic, Python 3.9 compatible.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _f(cfg: Dict[str, Any], key: str, default: float) -> float:
    try:
        v = cfg.get(key)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return default


def _i(cfg: Dict[str, Any], key: str, default: int) -> int:
    try:
        v = cfg.get(key)
        return int(default if v is None else v)
    except (TypeError, ValueError):
        return default


def rotation_plan(held: List[Dict[str, Any]],
                  candidates: List[Dict[str, Any]],
                  cfg: Dict[str, Any],
                  capital_constrained: bool) -> List[Dict[str, Any]]:
    """Decide which holdings to rotate out for better candidates.

    held:       [{"symbol", "edge", "age_days", "days_to_long_term"}]  (edge in
                pct-points = (prob_up-0.5)*100; None edge => never eligible)
    candidates: [{"symbol", "edge"}]  — NOT currently held, bullish
    capital_constrained: operator's verdict that a new buy can't simply be funded
                (book at the position cap OR available cash below one order).

    Returns [{"sell","buy","sell_edge","buy_edge","edge_gain","near_long_term",
    "reason"}], newest-best first, capped at rotation_max_per_cycle. Empty when
    disabled, unconstrained, or nothing clears the bar."""
    if not cfg.get("rotation_enabled"):
        return []
    if not capital_constrained:
        return []

    floor = _f(cfg, "rotation_hold_edge_floor_pct_pts", 2.0)
    min_gain = _f(cfg, "rotation_min_edge_gain_pct_pts", 4.0)
    min_hold = _i(cfg, "rotation_min_hold_days", 3)
    max_n = max(0, _i(cfg, "rotation_max_per_cycle", 2))
    tax_bias = _f(cfg, "rotation_tax_bias_pct_pts", 3.0)
    tax_window = _i(cfg, "rotation_tax_bias_window_days", 45)
    # A candidate must itself be one the agent would BUY — align to the entry
    # edge floor so rotation never swaps into a barely-positive name.
    cand_floor = _f(cfg, "min_edge_pct_pts", 3.0)
    if max_n <= 0:
        return []

    def _num(v):
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    # Weakest eligible holdings first: forward edge below the floor, past the
    # minimum hold, and a real edge reading (unknown edge => keep, don't guess).
    eligible = []
    for h in held or []:
        e = _num(h.get("edge"))
        if e is None or e >= floor:
            continue
        age = _num(h.get("age_days"))
        if age is not None and age < min_hold:
            continue
        eligible.append({"symbol": h.get("symbol"), "edge": e,
                         "days_to_long_term": _num(h.get("days_to_long_term"))})
    eligible.sort(key=lambda h: h["edge"])            # weakest edge first

    # Best candidates first.
    cands = []
    for c in candidates or []:
        e = _num(c.get("edge"))
        if e is None or e < cand_floor:
            continue
        cands.append({"symbol": c.get("symbol"), "edge": e})
    cands.sort(key=lambda c: -c["edge"])

    swaps: List[Dict[str, Any]] = []
    used = set()
    held_syms = {h["symbol"] for h in eligible}
    for h in eligible:
        if len(swaps) >= max_n:
            break
        for c in cands:
            if c["symbol"] in used or c["symbol"] in held_syms:
                continue
            required = min_gain
            dtl = h["days_to_long_term"]
            near_lt = dtl is not None and 0 <= dtl <= tax_window
            if near_lt:
                required += tax_bias          # protect the soon-cheaper tax lot
            gain = c["edge"] - h["edge"]
            if gain >= required:
                swaps.append({
                    "sell": h["symbol"], "buy": c["symbol"],
                    "sell_edge": round(h["edge"], 3),
                    "buy_edge": round(c["edge"], 3),
                    "edge_gain": round(gain, 3),
                    "near_long_term": near_lt,
                    "reason": "rotation: {} edge {:+.1f} -> {} edge {:+.1f} "
                              "(+{:.1f}pts{})".format(
                                  h["symbol"], h["edge"], c["symbol"], c["edge"],
                                  gain, ", tax-guarded" if near_lt else ""),
                })
                used.add(c["symbol"])
                break
    return swaps
