"""AJTA — execution: order lifecycle, fills, reconciliation, crash recovery.

Implements AJTA-SPEC-1.0 §12 (state machine, idempotency, partial fills),
§13 (reconciliation, broker-as-truth, halt on unexpected live position),
§20.1/§20.2 (crash recovery — reconcile BEFORE proposing, never blind-resubmit).

Paper positions are derived from aj_fills (ADR-001); execute_trade therefore
records aj_orders + aj_fills and NEVER mutates the real `portfolio` table.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

import aj_db
import aj_config
import aj_broker

log = logging.getLogger("augur.aj_execution")

# ── order state machine (§12.1, Appendix E) ──────────────────────────────────
_TERMINAL = {"filled", "canceled", "rejected", "expired"}
_TRANSITIONS = {
    # a 'new' order that never reached a broker may be abandoned
    # (canceled/expired) — this is the safe terminal for a crashed-mid-submit
    # record with no broker_order_id (§20.1), never a blind resubmit.
    "new":              {"submitted", "rejected", "unknown", "canceled", "expired"},
    "submitted":        {"accepted", "partially_filled", "filled", "rejected",
                         "canceled", "expired", "unknown"},
    "accepted":         {"partially_filled", "filled", "canceled", "rejected",
                         "expired", "unknown"},
    "partially_filled": {"partially_filled", "filled", "canceled", "expired",
                         "unknown"},
    # 'unknown' is resolved ONLY by reconciliation against broker truth (§12.1):
    # once the broker's state is known it may move to any real state.
    "unknown":          {"submitted", "accepted", "partially_filled", "filled",
                         "canceled", "rejected", "expired"},
}


def valid_transition(frm: str, to: str) -> bool:
    if frm == to == "partially_filled":
        return True
    if frm in _TERMINAL:
        return False
    if to == "unknown":
        return frm not in _TERMINAL
    return to in _TRANSITIONS.get(frm, set())


def _set_state(order_id: int, to: str, **extra) -> bool:
    row = aj_db.get_row("aj_orders", order_id)
    if not row:
        return False
    frm = row.get("state")
    if frm == to and to != "partially_filled":
        if extra:
            aj_db.update("aj_orders", order_id, **extra)
        return True
    if not valid_transition(frm, to):
        log.warning("illegal order transition %s -> %s (order %s)", frm, to, order_id)
        return False
    cols = dict(extra)
    cols["state"] = to
    if to in _TERMINAL:
        cols["terminal_at"] = aj_db.utc_now_iso()
    aj_db.update("aj_orders", order_id, **cols)
    return True


# ── execute one accepted proposal (§12.2 - row before submit) ─────────────────

def execute_trade(proposal: Dict[str, Any], decision: Dict[str, Any],
                  cycle_id: Optional[str] = None) -> Dict[str, Any]:
    """Submit an order for a proposal that PASSED the risk gate. Writes the
    aj_orders row in 'new' (with the idempotency key) BEFORE the network call,
    so a crash mid-submit leaves a recoverable record (§20.1). Records each
    fill to aj_fills (§12.3). On any submit error the order goes to 'unknown'
    and is left for reconciliation — never assumed filled."""
    if decision.get("decision") != "pass":
        return {"ok": False, "reason": "decision did not pass"}

    symbol = str(proposal.get("symbol") or "").upper()
    side = str(proposal.get("side") or "").lower()
    qty = float(decision.get("qty") or proposal.get("qty") or 0)
    order_type = str(proposal.get("order_type") or "market").lower()
    limit_price = proposal.get("limit_price")
    mode = decision.get("mode") or "paper"
    venue = decision.get("venue") or aj_config.get_config().get("default_broker") or "paper"
    client_order_id = str(uuid.uuid4())          # idempotency key (§12.4)

    # 1) write 'new' BEFORE submit
    order_id = aj_db.insert(
        "aj_orders", proposal_id=proposal.get("id"), client_order_id=client_order_id,
        broker=venue, mode=mode, account_ref=proposal.get("account_ref"),
        symbol=symbol, side=side, qty=qty, order_type=order_type,
        limit_price=limit_price, state="new", filled_qty=0, fees_usd=0,
        created_at=aj_db.utc_now_iso())
    aj_db.audit("order", {"order_id": order_id, "symbol": symbol, "side": side,
                          "qty": qty, "mode": mode, "venue": venue,
                          "client_order_id": client_order_id},
                cycle_id=cycle_id, ref_id=order_id)

    try:
        broker = aj_broker.get_broker(venue)
        broker.connect()
        _set_state(order_id, "submitted", submitted_at=aj_db.utc_now_iso())
        res = broker.submit({
            "symbol": symbol, "side": side, "qty": qty, "order_type": order_type,
            "limit_price": limit_price, "client_order_id": client_order_id,
            "asset_type": _asset_type(symbol)})
        broker.disconnect()
    except aj_broker.BrokerNotEnabled as e:
        _set_state(order_id, "rejected", broker_order_id=None)
        aj_db.audit("order", {"order_id": order_id, "rejected": str(e)},
                    cycle_id=cycle_id, ref_id=order_id)
        return {"ok": False, "order_id": order_id, "state": "rejected", "reason": str(e)}
    except Exception as e:
        # AMBIGUOUS — never assume. Park in 'unknown' for reconciliation (§12.1).
        _set_state(order_id, "unknown")
        aj_db.audit("order", {"order_id": order_id, "unknown": str(e)},
                    cycle_id=cycle_id, ref_id=order_id)
        log.exception("submit failed -> unknown (order %s)", order_id)
        return {"ok": False, "order_id": order_id, "state": "unknown", "reason": str(e)}

    return _apply_broker_result(order_id, res, cycle_id)


def _apply_broker_result(order_id: int, res: Dict[str, Any],
                         cycle_id: Optional[str]) -> Dict[str, Any]:
    boid = res.get("broker_order_id")
    fills = res.get("fills") or []
    # record fills (§12.3) — append + recompute aggregates per fill
    for f in fills:
        _record_fill(order_id, f, cycle_id)
    agg = _recompute_order(order_id)
    state = res.get("state") or "accepted"
    if state not in ("new", "submitted", "accepted", "partially_filled",
                     "filled", "canceled", "rejected", "expired", "unknown"):
        state = "unknown"
    # If broker says filled but we recorded partials, trust fill aggregates.
    if agg["filled_qty"] > 0 and state == "accepted":
        state = "partially_filled"
    _set_state(order_id, state, broker_order_id=boid,
               avg_fill_price=agg["avg_fill_price"], filled_qty=agg["filled_qty"],
               fees_usd=agg["fees_usd"])
    # mark proposal executed if anything filled
    if agg["filled_qty"] > 0 and order_id:
        row = aj_db.get_row("aj_orders", order_id)
        pid = row.get("proposal_id") if row else None
        if pid:
            try:
                aj_db.update("aj_proposals", pid, status="executed")
            except Exception:
                pass
    return {"ok": True, "order_id": order_id, "state": state,
            "filled_qty": agg["filled_qty"], "avg_fill_price": agg["avg_fill_price"],
            "fees_usd": agg["fees_usd"], "broker_order_id": boid}


def _record_fill(order_id: int, fill: Dict[str, Any], cycle_id: Optional[str]) -> int:
    fid = aj_db.insert(
        "aj_fills", order_id=order_id, broker_fill_id=fill.get("broker_fill_id"),
        qty=float(fill.get("qty") or 0), price=float(fill.get("price") or 0),
        fees_usd=float(fill.get("fees_usd") or 0),
        filled_at=fill.get("filled_at") or aj_db.utc_now_iso())
    aj_db.audit("fill", {"order_id": order_id, "qty": fill.get("qty"),
                         "price": fill.get("price"), "fees": fill.get("fees_usd")},
                cycle_id=cycle_id, ref_id=order_id)
    return fid


def _recompute_order(order_id: int) -> Dict[str, Any]:
    rows = aj_db.query("SELECT qty, price, fees_usd FROM aj_fills WHERE order_id=?",
                       (order_id,))
    total_qty = sum(float(r["qty"]) for r in rows)
    fees = sum(float(r["fees_usd"]) for r in rows)
    if total_qty > 0:
        notional = sum(float(r["qty"]) * float(r["price"]) for r in rows)
        avg = notional / total_qty
    else:
        avg = None
    return {"filled_qty": total_qty, "avg_fill_price": avg,
            "fees_usd": aj_db.money(fees)}


def _asset_type(symbol: str) -> str:
    import aj_positions
    return aj_positions.infer_asset_type(symbol)


# ── reconciliation (§13) ──────────────────────────────────────────────────────

def reconcile(broker: Optional[aj_broker.BrokerClient] = None,
              venue: Optional[str] = None, cycle_id: Optional[str] = None) -> Dict[str, Any]:
    """Pull broker truth and compare to local. Broker is source of truth for
    execution facts; local is corrected to broker. Resolves stale 'new' and
    'unknown' orders first (§20.1). Halts on an unexpected live position (§20.3)."""
    if broker is None:
        broker = aj_broker.get_broker(venue)
    resolved = _resolve_open_orders(broker, cycle_id)

    status = "match"
    detail: Dict[str, Any] = {"resolved": resolved, "scopes": {}}
    try:
        local = {p["symbol"].upper(): float(p["qty"]) for p in _local_positions(broker)}
        truth = {p["symbol"].upper(): float(p.get("qty") or 0) for p in broker.positions()}
        divergences = []
        for sym in set(local) | set(truth):
            lq, tq = local.get(sym, 0.0), truth.get(sym, 0.0)
            if abs(lq - tq) > 1e-6:
                divergences.append({"symbol": sym, "local": lq, "broker": tq})
        detail["scopes"]["positions"] = {"local": local, "broker": truth,
                                         "divergences": divergences}
        if divergences:
            status = "divergence"
            # §20.3 — a live divergence implying an unexpected position halts.
            if broker.mode == "live":
                import aj_risk
                aj_risk.halt("reconciliation divergence (live): {}".format(divergences),
                             caps={"divergences": divergences})
    except Exception as e:
        log.exception("reconcile positions failed")
        detail["error"] = str(e)
        status = "divergence"

    rid = aj_db.insert("aj_recon", ts=aj_db.utc_now_iso(),
                       scope="all", status=status,
                       detail_json=json.dumps(detail, default=str))
    aj_db.audit("recon", {"recon_id": rid, "status": status,
                          "resolved": resolved}, cycle_id=cycle_id, ref_id=rid)
    return {"recon_id": rid, "status": status, "detail": detail}


def _local_positions(broker: aj_broker.BrokerClient) -> List[Dict[str, Any]]:
    import aj_positions
    return aj_positions.positions_list("paper" if broker.mode == "paper" else "live")


def _resolve_open_orders(broker: aj_broker.BrokerClient,
                         cycle_id: Optional[str] = None) -> Dict[str, Any]:
    """Resolve non-terminal orders left by a crash (§20.1). Orders still in
    'new' never reached a broker (no broker_order_id) → safe to expire. Orders
    in 'unknown'/'submitted'/'accepted' with a broker_order_id are queried
    against broker truth; unresolved 'unknown' leaves the order blocked.
    NEVER blind-resubmit."""
    out = {"expired_new": 0, "resolved": 0, "still_unknown": 0}
    open_orders = aj_db.query(
        "SELECT * FROM aj_orders WHERE state IN "
        "('new','submitted','accepted','partially_filled','unknown')")
    for o in open_orders:
        oid = o["id"]
        state = o["state"]
        boid = o.get("broker_order_id")
        if state == "new" and not boid:
            # never submitted to a broker → safe to expire (no live exposure)
            _set_state(oid, "expired")
            out["expired_new"] += 1
            aj_db.audit("recon", {"order_id": oid, "action": "expired stale new"},
                        cycle_id=cycle_id, ref_id=oid)
            continue
        if not boid:
            # submitted/unknown without a broker id we can query → keep unknown
            if state != "unknown":
                _set_state(oid, "unknown")
            out["still_unknown"] += 1
            continue
        try:
            truth = broker.get_order(boid)
            tstate = truth.get("state")
            if tstate and tstate != "unknown":
                # adopt broker truth; record any fills it reports
                for f in (truth.get("fills") or []):
                    _record_fill(oid, f, cycle_id)
                agg = _recompute_order(oid)
                _set_state(oid, tstate, avg_fill_price=agg["avg_fill_price"],
                           filled_qty=agg["filled_qty"], fees_usd=agg["fees_usd"])
                out["resolved"] += 1
            else:
                _set_state(oid, "unknown")
                out["still_unknown"] += 1
        except Exception:
            log.exception("could not resolve order %s", oid)
            _set_state(oid, "unknown")
            out["still_unknown"] += 1
    return out


# ── crash recovery entrypoint (§20.1) ─────────────────────────────────────────

def recover_crashed_cycles(current_cycle: Optional[str] = None) -> Dict[str, Any]:
    """Detect cycles left 'running' by a crashed process, mark them crashed,
    and reconcile BEFORE any new proposals are made. Returns a summary."""
    stale = aj_db.find_stale_running_cycles(exclude=current_cycle)
    if not stale:
        return {"crashed": 0, "recon": None}
    for c in stale:
        aj_db.mark_cycle(c["cycle_id"], "crashed")
        aj_db.audit("recon", {"cycle_id": c["cycle_id"], "action": "marked crashed"},
                    cycle_id=current_cycle)
    recon = reconcile(cycle_id=current_cycle)
    for c in stale:
        aj_db.mark_cycle(c["cycle_id"], "recovered")
    return {"crashed": len(stale), "recon": recon}


# ── kill-switch hooks (§11.4) ─────────────────────────────────────────────────

def cancel_all_open(reason: str = "manual") -> int:
    """Best-effort cancel of every non-terminal order. Paper orders are marked
    canceled locally; live venues get a broker cancel. Returns count canceled."""
    n = 0
    open_orders = aj_db.query(
        "SELECT * FROM aj_orders WHERE state IN "
        "('new','submitted','accepted','partially_filled','unknown')")
    for o in open_orders:
        oid = o["id"]
        try:
            if o.get("mode") == "live" and o.get("broker_order_id"):
                broker = aj_broker.get_broker(o.get("broker"))
                broker.cancel(o["broker_order_id"])
            _set_state(oid, "canceled")
            n += 1
            aj_db.audit("order", {"order_id": oid, "canceled": reason}, ref_id=oid)
        except Exception:
            log.exception("cancel failed for order %s", oid)
    return n


def disconnect_all() -> None:
    """Disconnect any live broker connections (best-effort). The non-24/7
    posture holds no persistent connections, so this is usually a no-op."""
    return None
