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
_KNOWN_STATES = {"new", "submitted", "accepted", "partially_filled",
                 "filled", "canceled", "rejected", "expired", "unknown"}
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
            # compare-and-set so we don't clobber a concurrent terminal write
            aj_db.update_if("aj_orders", order_id, "state", frm, **extra)
        return True
    if not valid_transition(frm, to):
        log.warning("illegal order transition %s -> %s (order %s)", frm, to, order_id)
        return False
    cols = dict(extra)
    cols["state"] = to
    if to in _TERMINAL:
        cols["terminal_at"] = aj_db.utc_now_iso()
    # Conditional on the state we validated against — if another thread already
    # transitioned this order (e.g. reconcile vs cancel), we lose and return
    # False rather than silently overwriting a terminal state.
    ok = aj_db.update_if("aj_orders", order_id, "state", frm, **cols)
    if not ok:
        log.warning("order %s state changed under transition %s->%s (lost race)",
                    order_id, frm, to)
    return ok


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
    # The risk gate computed the authoritative size in decision['qty']; honor it
    # explicitly (even a 0) rather than via truthiness, only falling back to the
    # proposal qty when the gate left it unset.
    dqty = decision.get("qty")
    qty = float(dqty if dqty is not None else (proposal.get("qty") or 0))
    if qty <= 0:
        return {"ok": False, "reason": "non-positive order qty ({})".format(qty)}
    order_type = str(proposal.get("order_type") or "market").lower()
    limit_price = proposal.get("limit_price")
    mode = decision.get("mode") or "paper"
    venue = decision.get("venue") or aj_config.get_config().get("default_broker") or "paper"
    # Idempotency key (§12.4): DETERMINISTIC per proposal so the UNIQUE
    # constraint on aj_orders.client_order_id dedups a double-submit of the same
    # proposal (a double-clicked /approve, or operator+route racing) at the DB
    # level — exactly one order can ever exist for a proposal. Ad-hoc calls with
    # no proposal id fall back to a random uuid.
    pid = proposal.get("id")
    client_order_id = "ajp-{}".format(pid) if pid is not None else str(uuid.uuid4())

    # 1) write 'new' BEFORE submit. A UNIQUE violation means this proposal
    # already produced an order — return that, never a second submit.
    try:
        order_id = aj_db.insert(
            "aj_orders", proposal_id=pid, client_order_id=client_order_id,
            broker=venue, mode=mode, account_ref=proposal.get("account_ref"),
            symbol=symbol, side=side, qty=qty, order_type=order_type,
            limit_price=limit_price, state="new", filled_qty=0, fees_usd=0,
            created_at=aj_db.utc_now_iso())
    except Exception as e:
        existing = aj_db.query(
            "SELECT id, state FROM aj_orders WHERE client_order_id=?", (client_order_id,))
        if existing:
            log.warning("duplicate submit blocked for proposal %s (order %s)",
                        pid, existing[0]["id"])
            return {"ok": False, "order_id": existing[0]["id"],
                    "state": existing[0]["state"],
                    "reason": "duplicate proposal submission (idempotency)"}
        raise
    aj_db.audit("order", {"order_id": order_id, "symbol": symbol, "side": side,
                          "qty": qty, "mode": mode, "venue": venue,
                          "client_order_id": client_order_id},
                cycle_id=cycle_id, ref_id=order_id)

    broker = None
    try:
        broker = aj_broker.get_broker(venue)
        # The risk gate evaluated this proposal under decision['mode']. If the
        # constructed broker's mode differs (live_trading_enabled flipped
        # between gate and submit), routing would place a paper-evaluated order
        # with real money — or book a live-tagged order into the paper book,
        # corrupting mode-scoped reconciliation. Fail closed.
        if getattr(broker, "mode", None) != mode:
            _set_state(order_id, "rejected")
            aj_db.audit("order", {"order_id": order_id,
                                  "rejected": "broker mode {} != decision mode {}".format(
                                      getattr(broker, "mode", None), mode)},
                        cycle_id=cycle_id, ref_id=order_id)
            return {"ok": False, "order_id": order_id, "state": "rejected",
                    "reason": "broker/decision mode mismatch (fail-closed)"}
        broker.connect()
        _set_state(order_id, "submitted", submitted_at=aj_db.utc_now_iso())
        res = broker.submit({
            "symbol": symbol, "side": side, "qty": qty, "order_type": order_type,
            "limit_price": limit_price, "client_order_id": client_order_id,
            "asset_type": _asset_type(symbol)})
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
    finally:
        # Always release the broker session — a leaked live connection on a
        # failed submit holds a socket/auth session open (PaperBroker is a no-op).
        if broker is not None:
            try:
                broker.disconnect()
            except Exception:
                log.exception("broker.disconnect failed after submit (order %s)", order_id)

    return _apply_broker_result(order_id, res, cycle_id)


def _purge_synthetic_agg(order_id: int) -> None:
    """Remove the synthesized '<x>-agg' aggregate fill once REAL per-fill
    detail arrives for the order — keeping both would double-count the same
    shares in the FIFO paper book (filled_qty can even exceed order qty)."""
    try:
        import database as db
        with db._write_lock:
            conn = db.get_conn()
            conn.execute(
                "DELETE FROM aj_fills WHERE order_id=? AND broker_fill_id LIKE '%-agg'",
                (order_id,))
            conn.commit()
    except Exception:
        log.exception("purge of synthetic agg fill failed (order %s)", order_id)


def _apply_broker_result(order_id: int, res: Dict[str, Any],
                         cycle_id: Optional[str]) -> Dict[str, Any]:
    boid = res.get("broker_order_id")
    fills = res.get("fills") or []
    # record fills (§12.3) — append + recompute aggregates per fill. Real
    # per-fill detail supersedes any earlier synthesized aggregate fill.
    if fills:
        _purge_synthetic_agg(order_id)
    for f in fills:
        _record_fill(order_id, f, cycle_id)
    state = res.get("state") or "accepted"
    # Some brokers report a terminal 'filled' state separately from fill detail
    # (no fills array). Synthesize a single fill from the result's aggregate so
    # a genuinely filled order isn't stranded in 'unknown' for lack of detail.
    if (not fills and str(state).lower() in ("filled", "partially_filled")
            and float(res.get("filled_qty") or 0) > 0
            and float(res.get("avg_fill_price") or 0) > 0):
        already = aj_db.query("SELECT id FROM aj_fills WHERE order_id=?", (order_id,))
        if not already:
            # Idempotency for the SYNTHESIZED aggregate fill keys on the ORDER
            # (one synthetic fill per order), NOT on filled_at: stamping a fresh
            # `now` each pass would defeat the natural-key dedup and double-count
            # an aggregate re-reported by a later reconcile. Use a deterministic
            # broker_fill_id ('<order>-agg', or the broker's '<boid>-agg') so a
            # re-report dedups even when the order-level guard is racing.
            _record_fill(order_id, {
                "broker_fill_id": ("{}-agg".format(boid) if boid
                                   else "order{}-agg".format(order_id)),
                "qty": res.get("filled_qty"),
                "price": res.get("avg_fill_price"),
                "fees_usd": res.get("fees_usd"),
            }, cycle_id)
    agg = _recompute_order(order_id)
    if state not in ("new", "submitted", "accepted", "partially_filled",
                     "filled", "canceled", "rejected", "expired", "unknown"):
        state = "unknown"
    try:
        order_qty = float((aj_db.get_row("aj_orders", order_id) or {}).get("qty") or 0)
    except Exception:
        order_qty = 0.0
    # If broker says 'accepted' but we recorded fills, trust the aggregates: a
    # complete fill set (filled_qty >= order_qty) is terminal 'filled'; anything
    # short is genuinely partial. Relative tolerance so the comparison scales
    # across asset classes.
    if agg["filled_qty"] > 0 and state == "accepted":
        state = ("filled" if order_qty > 0
                 and agg["filled_qty"] >= order_qty * (1 - 1e-6)
                 else "partially_filled")
    # If the broker LABELS it filled but our recorded fills are short of the
    # order qty, don't accept the terminal 'filled' — keep it reconcilable so a
    # later poll can pull the missing fills (a terminal order is never revisited
    # by _resolve_open_orders).
    # Relative tolerance so the short-fill test scales across asset classes (a
    # fixed absolute epsilon is meaningful for fractional crypto but negligible
    # for large equity quantities).
    if (state == "filled" and order_qty > 0
            and agg["filled_qty"] < order_qty * (1 - 1e-6)):
        state = "partially_filled" if agg["filled_qty"] > 0 else "unknown"
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
                # Order/fills are already recorded; a failed status flip leaves
                # the proposal looking unexecuted (re-propose risk) — surface it
                # rather than swallowing the inconsistency silently.
                log.exception(
                    "failed to mark proposal %s executed (order %s)", pid, order_id)
    return {"ok": True, "order_id": order_id, "state": state,
            "filled_qty": agg["filled_qty"], "avg_fill_price": agg["avg_fill_price"],
            "fees_usd": agg["fees_usd"], "broker_order_id": boid}


def _record_fill(order_id: int, fill: Dict[str, Any], cycle_id: Optional[str]) -> int:
    # Idempotent on broker_fill_id: a reconciliation poll that re-reports the
    # same fill must not double-count it (live brokers report cumulative fills).
    bfid = fill.get("broker_fill_id")
    qv = float(fill.get("qty") or 0)
    pv = float(fill.get("price") or 0)
    fv = float(fill.get("fees_usd") or 0)
    at = fill.get("filled_at") or aj_db.utc_now_iso()
    # TOCTOU guard: a concurrent reconcile + submit could both pass a bare
    # SELECT-then-INSERT and double-insert the SAME fill, double-counting P&L.
    # Hold the single write lock across the dedup SELECT *and* the INSERT so the
    # check-then-act is atomic (the natural-key dedup is the second line of
    # defense for id-less fills re-reported by reconciliation).
    import database as _db
    with _db._write_lock:
        conn = _db.get_conn()
        if bfid:
            dup = conn.execute(
                "SELECT id FROM aj_fills WHERE order_id=? AND broker_fill_id=?",
                (order_id, bfid)).fetchone()
        else:
            # No broker fill id — dedup on the natural key so an id-less fill
            # re-reported by reconciliation isn't double-counted.
            dup = conn.execute(
                "SELECT id FROM aj_fills WHERE order_id=? AND broker_fill_id IS NULL "
                "AND qty=? AND price=? AND filled_at=?",
                (order_id, qv, pv, at)).fetchone()
        if dup:
            return int(dup["id"])
        cur = conn.execute(
            "INSERT INTO aj_fills (order_id, broker_fill_id, qty, price, fees_usd, filled_at) "
            "VALUES (?,?,?,?,?,?)", (order_id, bfid, qv, pv, fv, at))
        conn.commit()
        fid = int(cur.lastrowid)
    # Audit the SAME coerced values that were persisted to aj_fills so the
    # hash-chained record reconciles with the queryable row (raw fill.get(...)
    # values may be None/str when the stored value coerced to 0.0).
    aj_db.audit("fill", {"order_id": order_id, "qty": qv,
                         "price": pv, "fees": fv, "filled_at": at},
                cycle_id=cycle_id, ref_id=order_id)
    return fid


def _recompute_order(order_id: int) -> Dict[str, Any]:
    rows = aj_db.query("SELECT qty, price, fees_usd FROM aj_fills WHERE order_id=?",
                       (order_id,))
    total_qty = sum(float(r["qty"]) for r in rows)
    fees = sum(float(r["fees_usd"]) for r in rows)
    if total_qty > 0:
        notional = sum(float(r["qty"]) * float(r["price"]) for r in rows)
        # Round the volume-weighted average price to a fixed precision so the
        # division residue (0.1+0.2-style float drift) never propagates into
        # stored P&L. 8 dp preserves fractional/crypto price granularity that
        # money()'s cent rounding would destroy.
        avg = round(notional / total_qty, 8)
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
    # Case-fold so a broker reporting mode 'LIVE' still triggers the live halt;
    # default to treating an uncertain mode as live (fail-closed) for the halt
    # decision below.
    is_live = str(getattr(broker, "mode", "live")).strip().lower() != "paper"
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
            if is_live:
                import aj_risk
                aj_risk.halt("reconciliation divergence (live): {}".format(divergences),
                             caps={"divergences": divergences})
    except Exception as e:
        # A live broker that errors on positions() is the MOST likely signal
        # something is wrong — halt fail-closed rather than leave trading on.
        log.exception("reconcile positions failed")
        detail["error"] = str(e)
        status = "divergence"
        if is_live:
            try:
                import aj_risk
                aj_risk.halt("reconciliation error (live): {}".format(str(e)[:120]),
                             caps={"error": str(e)[:200]})
            except Exception:
                log.exception("halt-on-recon-error failed")

    # ── cash-account reconciliation ───────────────────────────────────────────
    # Paper: available cash is derived from the fills ledger by construction,
    # so this is an INVARIANT check (available == base − open cost + realized,
    # equity == available + book MV) — drift means a bookkeeping bug, not a
    # market event. Live: compare our ledger-derived view against the venue's
    # settled cash; sustained drift means fills/fees exist that we never
    # recorded. Never affects `status`-driven halts on its own (advisory), but
    # is persisted + audited so the operator sees it.
    try:
        import aj_alpha
        bp = aj_alpha.buying_power()
        if bp.get("enabled"):
            cash_scope: Dict[str, Any] = {"source": bp.get("source"),
                                          "available": bp.get("available"),
                                          "base": bp.get("base"),
                                          "equity": bp.get("equity")}
            if bp.get("source") == "paper" and bp.get("available") is not None:
                import aj_positions
                book = aj_positions.paper_book()
                positions = book.get("positions") or {}
                open_cost = sum(float((p or {}).get("cost_basis") or 0)
                                for p in positions.values())
                expect = max(0.0, float(bp.get("base") or 0) - open_cost
                             + float(book.get("realized_total") or 0))
                drift = abs(float(bp["available"]) - expect)
                cash_scope["invariant_ok"] = drift <= 0.01
                if drift > 0.01:
                    aj_db.audit("alert", {"kind": "cash_invariant_drift",
                                          "available": bp["available"],
                                          "expected": expect}, cycle_id=cycle_id)
            elif bp.get("source") == "live" and is_live:
                # Ledger-derived view of what live cash SHOULD be, vs venue truth.
                venue_cash = bp.get("available")
                cash_scope["venue_cash"] = venue_cash
                if venue_cash is None:
                    cash_scope["invariant_ok"] = False
                    aj_db.audit("alert", {"kind": "live_cash_unknown"},
                                cycle_id=cycle_id)
            detail["scopes"]["cash"] = cash_scope
    except Exception:
        log.debug("cash reconciliation skipped", exc_info=True)

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
    # Scope to the mode (paper vs live) of the broker actually being reconciled:
    # broker.get_order(boid) only knows ids minted by its own venue, so a paper
    # broker must not interrogate live order ids (and vice-versa) — that would
    # force every other-venue order to bogus 'unknown'. Mirrors _local_positions,
    # which also scopes by broker.mode.
    bmode = "paper" if getattr(broker, "mode", "live") == "paper" else "live"
    open_orders = aj_db.query(
        "SELECT * FROM aj_orders WHERE mode = ? AND state IN "
        "('new','submitted','accepted','partially_filled','unknown')", (bmode,))
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
            # submitted/unknown without a broker id: try the deterministic
            # client_order_id lookup first — a submit that timed out AFTER the
            # venue accepted it is otherwise invisible forever (it can fill
            # real money with no local record, and the idempotency key blocks
            # any retry of that proposal).
            resolved_by_cid = False
            lookup = getattr(broker, "get_order_by_client_id", None)
            coid = o.get("client_order_id")
            if callable(lookup) and coid:
                try:
                    truth = lookup(coid)
                    tstate = truth.get("state")
                    tboid = truth.get("broker_order_id")
                    if tboid and tstate in _KNOWN_STATES and tstate != "unknown":
                        if truth.get("fills"):
                            _purge_synthetic_agg(oid)
                        for f in (truth.get("fills") or []):
                            _record_fill(oid, f, cycle_id)
                        agg = _recompute_order(oid)
                        if _set_state(oid, tstate, broker_order_id=tboid,
                                      avg_fill_price=agg["avg_fill_price"],
                                      filled_qty=agg["filled_qty"],
                                      fees_usd=agg["fees_usd"]):
                            out["resolved"] += 1
                            resolved_by_cid = True
                            aj_db.audit("recon", {"order_id": oid,
                                                  "action": "resolved via client_order_id",
                                                  "broker_order_id": tboid},
                                        cycle_id=cycle_id, ref_id=oid)
                except Exception:
                    log.debug("client_order_id lookup failed for order %s",
                              oid, exc_info=True)
            if resolved_by_cid:
                continue
            if state != "unknown":
                _set_state(oid, "unknown")
            out["still_unknown"] += 1
            continue
        try:
            truth = broker.get_order(boid)
            tstate = truth.get("state")
            if tstate in _KNOWN_STATES and tstate != "unknown":
                # adopt broker truth; record any fills it reports (real detail
                # supersedes a synthesized '-agg' fill from submit time)
                if truth.get("fills"):
                    _purge_synthetic_agg(oid)
                for f in (truth.get("fills") or []):
                    _record_fill(oid, f, cycle_id)
                agg = _recompute_order(oid)
                # Only count as resolved if the transition was actually applied;
                # an unexpected/illegal transition leaves the order reconcilable.
                if _set_state(oid, tstate, avg_fill_price=agg["avg_fill_price"],
                              filled_qty=agg["filled_qty"], fees_usd=agg["fees_usd"]):
                    out["resolved"] += 1
                else:
                    out["still_unknown"] += 1
            else:
                # blank/unknown or an unrecognized broker state string → keep
                # the order reconcilable rather than adopting a bogus state.
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
    # CLAIM each stale cycle with a compare-and-set (running -> crashed) before
    # reconciling it. Two recovery passes racing the same crashed cycle would
    # otherwise both mark+reconcile it; only the pass that actually flips the
    # state (rowcount 1) owns the recovery — the loser skips it (TOCTOU fix).
    claimed = []
    for c in stale:
        if aj_db.claim_stale_cycle(c["cycle_id"]):
            claimed.append(c)
            aj_db.audit("recon", {"cycle_id": c["cycle_id"], "action": "marked crashed"},
                        cycle_id=current_cycle)
    if not claimed:
        return {"crashed": 0, "recon": None}
    recon = reconcile(cycle_id=current_cycle)
    for c in claimed:
        aj_db.mark_cycle(c["cycle_id"], "recovered")
    return {"crashed": len(claimed), "recon": recon}


# ── kill-switch hooks (§11.4) ─────────────────────────────────────────────────

def cancel_all_open(reason: str = "manual", force_live: bool = False) -> int:
    """Best-effort cancel of every non-terminal order. Paper orders are marked
    canceled locally; live venues get a broker cancel. Returns count canceled.

    `force_live` constructs the LIVE venue adapter directly (bypassing
    get_broker's "live disabled -> PaperBroker" fallback) so the kill switch can
    cancel a live order at the venue even while it is disabling live trading in
    the same operation — otherwise get_broker returns a PaperBroker that knows
    nothing of the live order and the venue order is never canceled."""
    n = 0
    open_orders = aj_db.query(
        "SELECT * FROM aj_orders WHERE state IN "
        "('new','submitted','accepted','partially_filled','unknown')")
    # Reuse one broker per (mode, venue) rather than rebuilding it per order —
    # a fresh live adapter per order would re-auth/re-connect on every loop.
    _brokers: Dict[str, aj_broker.BrokerClient] = {}

    def _broker_for(mode: str, venue: str) -> aj_broker.BrokerClient:
        key = "{}|{}".format(mode, venue)
        b = _brokers.get(key)
        if b is None:
            if mode == "live" and force_live:
                # Build the live adapter directly so a concurrent live-disable
                # can't downgrade it to PaperBroker mid-cancel.
                cls = aj_broker._BROKERS.get((venue or "").lower())
                if venue and venue.lower() == "alpaca":
                    b = aj_broker._alpaca_cls()()
                elif cls is not None:
                    b = cls()
                else:
                    b = aj_broker.get_broker(venue)
            else:
                b = aj_broker.get_broker(venue)
            _brokers[key] = b
        return b

    for o in open_orders:
        oid = o["id"]
        try:
            if o.get("mode") == "live" and not o.get("broker_order_id"):
                # A live order with no broker id (submit timeout) may still be
                # WORKING at the venue under its client_order_id — a local
                # 'canceled' would hide real exposure and report a clean kill.
                # Leave it 'unknown' so reconciliation (client-id lookup) and
                # kill_switch's still-open alert surface it honestly.
                _set_state(oid, "unknown")
                aj_db.audit("order", {"order_id": oid,
                                      "cancel_skipped": "live order without broker id",
                                      "reason": reason}, ref_id=oid)
                continue
            if o.get("mode") == "live" and o.get("broker_order_id"):
                broker = _broker_for("live", o.get("broker"))
                res = broker.cancel(o["broker_order_id"]) or {}
                rstate = str(res.get("state") or "").strip().lower()
                # Only flip local state to canceled when the venue actually
                # reports a terminal cancel. A non-terminal/ambiguous result
                # means the live order may still be open — leave it 'unknown'
                # for reconciliation so kill_switch's still-open count is honest.
                if rstate in ("canceled", "rejected", "expired", "filled"):
                    _set_state(oid, rstate)
                    if rstate == "canceled":
                        n += 1
                    aj_db.audit("order", {"order_id": oid, "cancel_result": rstate,
                                          "reason": reason}, ref_id=oid)
                else:
                    _set_state(oid, "unknown")
                    aj_db.audit("order", {"order_id": oid, "cancel_unconfirmed": rstate,
                                          "reason": reason}, ref_id=oid)
                continue
            # Only count when the CAS transition actually applied — a concurrent
            # writer that already moved this order to a terminal state makes
            # _set_state return False, and counting it would overstate the
            # orders_canceled total kill_switch surfaces. Mirrors the live branch.
            if _set_state(oid, "canceled"):
                n += 1
            aj_db.audit("order", {"order_id": oid, "canceled": reason}, ref_id=oid)
        except Exception:
            log.exception("cancel failed for order %s", oid)
    return n


def disconnect_all() -> None:
    """Disconnect any live broker connections (best-effort). The non-24/7
    posture holds no persistent connections, so this is usually a no-op."""
    return None
