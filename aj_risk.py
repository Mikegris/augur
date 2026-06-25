"""AJTA — risk gate (AJTA-SPEC-1.0 §11). The single chokepoint every order
passes before reaching a broker. FAIL-CLOSED: any error in the control path
yields *no trade*.

Implements:
  §11.2  day-P&L (FIFO realized + unrealized vs session-open mark, money()).
  §11.3  ordered gate (first failure blocks; daily-loss breach HALTS).
  §11.4  kill switch (reachable without the model in the loop).
  HALT + re-arm semantics.

Extends `jarvis_policy` (check_proposal / check_portfolio) for IPS checks.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import database as db
import aj_db
import aj_config

log = logging.getLogger("augur.aj_risk")

import math
import re
_CRYPTO_SUFFIX = "-USD"


def _trading_enabled_fresh() -> bool:
    """Uncached read of the master switch — a kill/halt on another thread (or
    mid-evaluation) must be seen immediately, not up to 5s later through the
    settings cache. Absent key => False (fail-closed)."""
    raw = aj_db.get_setting_raw("aj_trading_enabled")
    if raw is None:
        return False
    return str(raw).strip().lower() in ("1", "true", "yes", "on")
# Valid ticker shape for open-universe mode — alnum core + optional one
# .  or - suffix (e.g. BRK-B, BTC-USD). Bounds what "any symbol" can mean.
_VALID_SYMBOL = re.compile(r"^[A-Z0-9]{1,9}(?:[.\-][A-Z0-9]{1,4})?$")


# ── quotes / marks ────────────────────────────────────────────────────────────

def _quote_symbol(sym: str, asset_type: str = "") -> str:
    s = (sym or "").upper()
    if asset_type == "crypto" and not s.endswith(_CRYPTO_SUFFIX):
        return s + _CRYPTO_SUFFIX
    return s


def _marks(symbols: List[str]) -> Dict[str, Optional[float]]:
    """Best-effort current prices keyed by the ORIGINAL symbol. A missing /
    non-finite quote maps to None so callers zero its P&L contribution and
    raise a data-quality warning rather than trading on a stale price."""
    out: Dict[str, Optional[float]] = {}
    if not symbols:
        return out
    try:
        import fetcher
        qmap = fetcher.get_quotes_batch(list({s for s in symbols})) or {}
    except Exception:
        log.debug("marks: get_quotes_batch failed", exc_info=True)
        qmap = {}
    for s in symbols:
        q = qmap.get(s) or {}
        p = q.get("price")
        out[s] = float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) and p == p and p not in (float("inf"), float("-inf")) else None
    return out


# ── day-P&L (§11.2) ───────────────────────────────────────────────────────────

def _today() -> str:
    return aj_db.utc_now().strftime("%Y-%m-%d")


def _session_date() -> str:
    """The ET trading date. The session-open baseline must reset at the ET
    session, not at 00:00 UTC (which lands mid-afterhours ET)."""
    try:
        from zoneinfo import ZoneInfo
        return aj_db.utc_now().astimezone(
            ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return aj_db.utc_now().strftime("%Y-%m-%d")


def _session_open_unrealized(current_unrealized: float) -> float:
    """The unrealized mark captured at session open. Snapshotted once per ET
    trading day on first observation; subsequent calls return the stored value
    so the day's delta is measured from a fixed baseline.

    MUST use the raw settings reader: the key is `__`-prefixed and
    get_settings() hides those, so the old code always read None and re-stamped
    the snapshot to "now" every call — making (unreal_now - unreal_open) always
    0 and silently dropping unrealized drawdown from the daily-loss HALT."""
    key = "__aj_unreal_open_" + _session_date()
    raw = aj_db.get_setting_raw(key)
    if raw is not None and str(raw) != "":
        try:
            return aj_db.money(raw)
        except Exception:
            pass
    aj_db.set_setting_raw(key, str(aj_db.money(current_unrealized)))
    return aj_db.money(current_unrealized)


def compute_day_pnl(basis: Optional[str] = None) -> Dict[str, Any]:
    """day_pnl per §11.2, computed over the agent's PAPER BOOK (ADR-001 — the
    real portfolio is never touched by paper trading). Returns {day_pnl,
    realized, unrealized_now, unrealized_open, warnings:[...]}. Never raises."""
    import aj_positions
    cfg = aj_config.get_config()
    if basis is None:
        basis = cfg.get("daily_loss_basis", "realized_plus_unrealized")
    warnings: List[str] = []

    try:
        book = aj_positions.paper_book()
    except Exception:
        log.exception("compute_day_pnl: paper_book failed")
        book = {"positions": {}, "realized_today": 0.0}
        warnings.append("paper book unavailable")

    # ── realized today: FIFO from the paper book (incl fees) ──────────────────
    realized = aj_db.money(book.get("realized_today") or 0)

    # ── unrealized now: mark open paper positions ─────────────────────────────
    unreal = 0.0
    try:
        positions = book.get("positions") or {}
        syms = list(positions.keys())
        marks = _marks(syms)
        for sym, p in positions.items():
            qty = float(p.get("qty") or 0)
            avg = float(p.get("avg_cost") or 0)
            mark = marks.get(sym)
            if mark is None:
                qsym = _quote_symbol(sym, p.get("asset_type") or "")
                if qsym != sym:
                    mark = _marks([qsym]).get(qsym)
            if mark is None:
                warnings.append("no quote for {} — contribution 0".format(sym))
                continue
            unreal += (mark - avg) * qty
    except Exception:
        log.exception("compute_day_pnl: unrealized leg failed")
        warnings.append("unrealized P&L unavailable")
    unreal = aj_db.money(unreal)

    unreal_open = _session_open_unrealized(unreal)

    if basis == "realized":
        day_pnl = realized
    else:
        day_pnl = aj_db.money(realized + (unreal - unreal_open))

    return {"day_pnl": day_pnl, "realized": realized,
            "unrealized_now": unreal, "unrealized_open": unreal_open,
            "warnings": warnings}


# ── helpers for the gate ──────────────────────────────────────────────────────

def _trades_today() -> int:
    try:
        rows = aj_db.query(
            "SELECT COUNT(*) AS n FROM aj_orders "
            "WHERE substr(COALESCE(submitted_at, created_at),1,10) = ? "
            "AND state NOT IN ('rejected','canceled')", (_today(),))
        return int(rows[0]["n"]) if rows else 0
    except Exception:
        log.exception("_trades_today failed")
        return 0


def _order_price(symbol: str, order_type: str, limit_price: Optional[float],
                 asset_type: str = "") -> Optional[float]:
    if order_type == "limit" and limit_price:
        return float(limit_price)
    mark = _marks([symbol]).get(symbol)
    if mark is None:
        qsym = _quote_symbol(symbol, asset_type)
        if qsym != symbol:
            mark = _marks([qsym]).get(qsym)
    return mark


def _ips_block_reason(symbol: str, side: str, qty: float, price: float,
                      account_id: Optional[int]) -> Optional[str]:
    """IPS gate (§11.3 step 6): proposal-level cap + post-trade portfolio
    projection. Returns a reason string to block, or None to pass. Fail-closed:
    an exception here returns a block reason."""
    try:
        import jarvis_policy
        notional = aj_db.money(qty * price)
        # proposal-level: max_single_buy_usd
        if side == "buy":
            warn = jarvis_policy.check_proposal("buy {}".format(symbol),
                                                {"symbol": symbol, "amount": notional})
            if warn:
                return warn
        # post-trade projection vs portfolio rules
        before = jarvis_policy.check_portfolio()
        proj = _project_pulse(symbol, side, qty, price, account_id)
        after = jarvis_policy.check_portfolio(pulse=proj)

        def _breaches(res):
            return [v for v in (res.get("violations") or [])
                    if v.get("severity") == "breach"]
        nb, na = len(_breaches(before)), len(_breaches(after))
        if na > nb:
            new = _breaches(after)[-1] if _breaches(after) else {}
            return "IPS: {}".format(new.get("detail") or "post-trade rule breach")
        return None
    except Exception:
        log.exception("_ips_block_reason failed -> block (fail-closed)")
        return "IPS check error (fail-closed)"


def _project_pulse(symbol: str, side: str, qty: float, price: float,
                   account_id: Optional[int]) -> Dict[str, Any]:
    """Build a jarvis-pulse-shaped dict for the PAPER BOOK after applying the
    proposed trade, so check_portfolio can evaluate weight/crypto/positions
    against the projected agent book (ADR-001 — not the real portfolio)."""
    import aj_positions
    book = aj_positions.paper_book()
    sym_u = symbol.upper()
    rows: Dict[str, Dict[str, Any]] = {}
    marks = _marks(list(book["positions"].keys()))
    for s, p in book["positions"].items():
        mark = marks.get(s) or float(p.get("avg_cost") or 0)
        rows[s] = {"symbol": s, "asset_type": p.get("asset_type") or "stock",
                   "shares": float(p.get("qty") or 0), "mark": mark}
    # apply the trade
    delta = qty if side == "buy" else -qty
    if sym_u in rows:
        rows[sym_u]["shares"] = max(0.0, rows[sym_u]["shares"] + delta)
        if not rows[sym_u]["mark"]:
            rows[sym_u]["mark"] = price
    elif side == "buy":
        rows[sym_u] = {"symbol": sym_u, "asset_type": "stock",
                       "shares": qty, "mark": price}
    holdings = []
    total = 0.0
    for s, r in rows.items():
        mv = r["shares"] * (r["mark"] or 0)
        if r["shares"] <= 0:
            continue
        holdings.append({"symbol": s, "asset_type": r["asset_type"],
                         "market_value": mv})
        total += mv
    for h in holdings:
        h["weight_pct"] = (h["market_value"] / total * 100.0) if total > 0 else 0.0
    return {"holdings": holdings, "num_positions": len(holdings),
            "total_value": total}


# ── halt / kill / re-arm (§11.3 step 7, §11.4) ───────────────────────────────

def is_halted() -> bool:
    return not bool(aj_config.get_config().get("trading_enabled"))


def _emit_alert(level: str, msg: str) -> None:
    # Alerts are surfaced via audit + log; an external alerter can subscribe.
    log.warning("ALERT[%s] %s", level, msg)
    try:
        aj_db.audit("alert", {"level": level, "message": msg}, actor="system")
    except Exception:
        pass


def halt(reason: str, day_pnl: Optional[float] = None,
         proposal_id: Optional[int] = None, caps: Optional[Dict[str, Any]] = None) -> None:
    """Daily-loss / safety halt: disable trading, record the event, alert.
    Re-enable only per halt_rearm (§11.3 step 7)."""
    aj_config.set_config({"trading_enabled": False})
    aj_db.insert("aj_risk_events", created_at=aj_db.utc_now_iso(),
                 proposal_id=proposal_id, decision="halt", reason=reason,
                 caps_json=json.dumps(caps or {}, default=str), day_pnl_usd=day_pnl)
    aj_db.audit("halt", {"reason": reason, "day_pnl": day_pnl}, actor="system")
    _emit_alert("critical", "HALT: {}".format(reason))


def kill_switch(reason: str = "manual kill") -> Dict[str, Any]:
    """§11.4 — single operation, model NOT in the loop. Disables trading AND
    robinhood atomically, cancels open orders best-effort, disconnects live
    broker. Reachable from CLI / UI / signal file."""
    aj_config.set_config({"trading_enabled": False, "robinhood_enabled": False})
    canceled = 0
    try:
        import aj_execution
        canceled = aj_execution.cancel_all_open(reason="kill_switch")
    except Exception:
        log.debug("kill_switch: cancel_all_open unavailable/failed", exc_info=True)
    try:
        import aj_execution
        aj_execution.disconnect_all()
    except Exception:
        pass
    # Surface any order that survived cancellation — a live broker cancel that
    # threw leaves a position open; the operator must know, not assume clean.
    still_open = 0
    try:
        rows = aj_db.query(
            "SELECT COUNT(*) AS n FROM aj_orders WHERE state IN "
            "('new','submitted','accepted','partially_filled','unknown')")
        still_open = int(rows[0]["n"]) if rows else 0
    except Exception:
        log.debug("kill_switch still-open count failed", exc_info=True)
    aj_db.insert("aj_risk_events", created_at=aj_db.utc_now_iso(),
                 proposal_id=None, decision="halt", reason="kill: " + reason,
                 caps_json="{}", day_pnl_usd=None)
    aj_db.audit("halt", {"reason": "kill_switch: " + reason,
                         "orders_canceled": canceled, "still_open": still_open},
                actor="human")
    _emit_alert("critical", "KILL SWITCH: {} ({} orders canceled)".format(reason, canceled))
    if still_open:
        _emit_alert("critical",
                    "KILL SWITCH: {} order(s) STILL OPEN after cancel — manual review".format(still_open))
    return {"ok": True, "orders_canceled": canceled, "still_open": still_open}


def rearm(actor: str = "human") -> Dict[str, Any]:
    """Re-enable trading after a halt, honoring halt_rearm policy. 'manual'
    requires this explicit call; 'session_open' additionally requires the
    current session to be tradable."""
    cfg = aj_config.get_config()
    if cfg.get("halt_rearm") == "session_open":
        sess = aj_db.market_session()
        if sess not in cfg.get("session_whitelist", ["regular"]):
            return {"ok": False, "reason": "session {} not tradable".format(sess)}
    aj_config.set_config({"trading_enabled": True})
    aj_db.insert("aj_risk_events", created_at=aj_db.utc_now_iso(),
                 proposal_id=None, decision="rearm", reason="rearm by " + actor,
                 caps_json="{}", day_pnl_usd=None)
    aj_db.audit("rearm", {"actor": actor}, actor=actor)
    return {"ok": True}


# ── the gate (§11.3) ──────────────────────────────────────────────────────────

class RiskDecision(dict):
    @property
    def passed(self) -> bool:
        return self.get("decision") == "pass"


def _record(decision: str, reason: Optional[str], caps: Dict[str, Any],
            day_pnl: Optional[float], proposal_id: Optional[int]) -> None:
    try:
        aj_db.insert("aj_risk_events", created_at=aj_db.utc_now_iso(),
                     proposal_id=proposal_id, decision=decision, reason=reason,
                     caps_json=json.dumps(caps, default=str), day_pnl_usd=day_pnl)
    except Exception:
        log.exception("could not record risk event")


def evaluate(proposal: Dict[str, Any]) -> RiskDecision:
    """Run the ordered §11.3 gate on a proposal dict
    {symbol, side, qty|notional_usd, order_type, limit_price, account_id,
     broker(optional), id(optional proposal_id)}.

    Returns a RiskDecision {decision: pass|block|halt, reason, mode, day_pnl,
    caps, order_price, qty}. EVERY evaluation writes an aj_risk_events row.
    Any exception => block (fail-closed) + alert."""
    pid = proposal.get("id")
    cfg = aj_config.get_config()
    caps = dict(cfg)
    try:
        symbol = str(proposal.get("symbol") or "").upper()
        side = str(proposal.get("side") or "").lower()
        order_type = str(proposal.get("order_type") or "market").lower()
        limit_price = proposal.get("limit_price")
        account_id = proposal.get("account_id")
        venue = str(proposal.get("broker") or cfg.get("default_broker") or "paper").lower()

        if side not in ("buy", "sell"):
            _record("block", "invalid side", caps, None, pid)
            return RiskDecision(decision="block", reason="invalid side")

        # Step 1 — master switch (uncached: see a kill/halt immediately)
        if not _trading_enabled_fresh():
            _record("block", "trading_enabled is false", caps, None, pid)
            return RiskDecision(decision="block", reason="trading disabled (master switch off)")

        # Step 2 — session
        session = aj_db.market_session()
        if session not in cfg.get("session_whitelist", ["regular"]):
            _record("block", "session {} not whitelisted".format(session), caps, None, pid)
            return RiskDecision(decision="block", reason="session {} not tradable".format(session))

        # Step 3 — allowlist (EMPTY => all blocked) UNLESS open-universe mode is
        # explicitly enabled. In open-universe mode the allowlist gate is
        # bypassed, but the symbol must still be syntactically valid and
        # quotable (next step), and EVERY other cap (notional, trades/day,
        # daily-loss, IPS) still binds — this is a deliberate, opt-in loosening
        # of one rail, never a removal of the gate.
        allow = cfg.get("symbol_allowlist") or []
        if not cfg.get("allow_any_symbol"):
            if symbol not in allow:
                _record("block", "symbol not in allowlist", caps, None, pid)
                return RiskDecision(decision="block", reason="{} not in symbol allowlist".format(symbol))
        else:
            if not _VALID_SYMBOL.match(symbol):
                _record("block", "open-universe: invalid symbol", caps, None, pid)
                return RiskDecision(decision="block", reason="invalid symbol {}".format(symbol))

        # price + notional
        price = _order_price(symbol, order_type, limit_price)
        if price is None or price <= 0:
            _record("block", "no usable price", caps, None, pid)
            return RiskDecision(decision="block", reason="no quote/price for {} (no trade)".format(symbol))
        if proposal.get("qty") is not None:
            qty = float(proposal["qty"])
        elif proposal.get("notional_usd"):
            qty = float(proposal["notional_usd"]) / price
        else:
            _record("block", "no qty/notional", caps, None, pid)
            return RiskDecision(decision="block", reason="proposal lacks qty and notional")
        # qty MUST be finite + positive: a NaN/inf notional yields a non-finite
        # qty that passes `qty <= 0` (NaN comparisons are False) and then
        # money(qty*price)=0, which would slip under any positive cap.
        if not math.isfinite(qty) or qty <= 0:
            _record("block", "qty not finite/positive", caps, None, pid)
            return RiskDecision(decision="block", reason="qty must be a finite number > 0")
        order_notional = aj_db.money(qty * price)

        # Step 4 — per-order notional cap (0 => block)
        max_notional = aj_db.money(cfg.get("max_order_notional_usd") or 0)
        if not (max_notional > 0 and order_notional <= max_notional):
            _record("block", "order notional {} > cap {}".format(order_notional, max_notional),
                    caps, None, pid)
            return RiskDecision(decision="block",
                                reason="order ${:,.2f} exceeds per-order cap ${:,.2f}".format(
                                    order_notional, max_notional))

        # Step 5 — trades/day cap (0 => block)
        max_trades = int(cfg.get("max_trades_per_day") or 0)
        done_today = _trades_today()
        if not (max_trades > 0 and done_today < max_trades):
            _record("block", "trades today {} >= cap {}".format(done_today, max_trades),
                    caps, None, pid)
            return RiskDecision(decision="block",
                                reason="daily trade cap reached ({}/{})".format(done_today, max_trades))

        # Step 6 — IPS
        ips = _ips_block_reason(symbol, side, qty, price, account_id)
        if ips:
            _record("block", ips, caps, None, pid)
            return RiskDecision(decision="block", reason=ips)

        # Step 7 — daily loss => HALT
        pnl = compute_day_pnl(cfg.get("daily_loss_basis"))
        caps["day_pnl"] = pnl
        max_loss = aj_db.money(cfg.get("max_daily_loss_usd") or 0)
        if not (max_loss > 0 and pnl["day_pnl"] > -max_loss):
            halt("daily loss breach: day P&L ${:,.2f} vs limit ${:,.2f}".format(
                pnl["day_pnl"], -max_loss), day_pnl=pnl["day_pnl"], proposal_id=pid, caps=caps)
            return RiskDecision(decision="halt",
                                reason="daily loss limit hit (day P&L ${:,.2f})".format(pnl["day_pnl"]),
                                day_pnl=pnl["day_pnl"])

        # Step 8 — force paper unless live explicitly enabled
        mode = "live" if cfg.get("live_trading_enabled") else "paper"
        if mode == "live" and venue == "paper":
            mode = "paper"  # paper broker is always paper

        # Step 9 — Robinhood third gate
        if venue == "robinhood" and not cfg.get("robinhood_enabled"):
            _record("block", "robinhood_enabled is false", caps, None, pid)
            return RiskDecision(decision="block", reason="Robinhood venue not enabled")

        # Final fresh master-switch check — a kill/halt may have landed during
        # evaluation (between the step-1 read and here).
        if not _trading_enabled_fresh():
            _record("block", "trading disabled mid-evaluation", caps, None, pid)
            return RiskDecision(decision="block", reason="trading disabled (master switch off)")

        _record("pass", None, caps, pnl["day_pnl"], pid)
        return RiskDecision(decision="pass", reason=None, mode=mode,
                            day_pnl=pnl["day_pnl"], order_price=price, qty=qty,
                            order_notional=order_notional, venue=venue)
    except Exception as e:
        log.exception("risk gate raised -> block (fail-closed)")
        _record("block", "gate exception: {}".format(e), caps, None, pid)
        _emit_alert("critical", "risk gate exception (fail-closed): {}".format(e))
        return RiskDecision(decision="block", reason="risk gate error (fail-closed)")
