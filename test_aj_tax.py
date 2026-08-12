#!/usr/bin/env python3
"""AJTA tax-view tests (aj_tax).

Deterministic, offline: a fresh temp DB, hand-seeded fills with explicit dates
so short-term vs long-term classification, per-year rollup, wash-sale flagging
and the estimated-tax netting are all pinned. No network/LLM/broker.
"""
import os
import sys
import tempfile

os.environ["AUGUR_DB_PATH"] = tempfile.mktemp(suffix="_ajtax.db")

import database as db          # noqa: E402
import aj_db                    # noqa: E402
import aj_config                # noqa: E402
import aj_positions             # noqa: E402
import aj_tax                   # noqa: E402

aj_db.aj_init()

_TABLES = ("aj_orders", "aj_fills", "aj_proposals", "aj_audit")


def _reset():
    conn = db.get_conn()
    with aj_db.audit_maintenance():
        for t in _TABLES:
            conn.execute("DELETE FROM {}".format(t))
    conn.execute("DELETE FROM settings WHERE key LIKE 'aj_%' OR key LIKE '__aj_%'")
    conn.commit()
    try:
        db._invalidate_settings_cache()
    except Exception:
        pass


def _fill(sym, side, qty, price, when, fees=0.0):
    """Seed one order+fill at an explicit ISO datetime `when`."""
    oid = aj_db.insert("aj_orders", proposal_id=1,
                       client_order_id=sym + side + str(qty) + str(price) + when,
                       broker="paper", mode="paper", symbol=sym, side=side, qty=qty,
                       order_type="market", state="filled", created_at=when)
    aj_db.insert("aj_fills", order_id=oid, qty=qty, price=price, fees_usd=fees,
                 filled_at=when)


# ── holding-period classification ──────────────────────────────────────────────

def test_long_vs_short_term_split():
    _reset()
    # LONG: bought 2023-01-10, sold 2024-06-01 (>365d) -> long-term +$100
    _fill("LNG", "buy", 10, 100.0, "2023-01-10T15:00:00+00:00")
    _fill("LNG", "sell", 10, 110.0, "2024-06-01T15:00:00+00:00")
    # SHORT: bought and sold inside a year -> short-term +$50
    _fill("SHT", "buy", 5, 200.0, "2024-02-01T15:00:00+00:00")
    _fill("SHT", "sell", 5, 210.0, "2024-08-01T15:00:00+00:00")
    lots = aj_tax.tax_lots("paper")
    by = {l["symbol"]: l for l in lots}
    assert by["LNG"]["term"] == "long" and by["LNG"]["holding_days"] > 365
    assert by["LNG"]["gain"] == 100.0
    assert by["SHT"]["term"] == "short" and by["SHT"]["holding_days"] == 182
    assert by["SHT"]["gain"] == 50.0
    s = aj_tax.tax_summary("paper")
    assert s["long_term_gain"] == 100.0 and s["short_term_gain"] == 50.0
    assert s["total_realized"] == 150.0


def test_exactly_one_year_is_short_term():
    _reset()
    # held EXACTLY 365 days -> still short-term (long-term is > 1 year)
    _fill("EDG", "buy", 1, 100.0, "2023-06-01T15:00:00+00:00")
    _fill("EDG", "sell", 1, 120.0, "2024-05-31T15:00:00+00:00")   # 365 days
    lot = aj_tax.tax_lots("paper")[0]
    assert lot["holding_days"] == 365 and lot["term"] == "short", lot


# ── parity with the canonical FIFO engine ──────────────────────────────────────

def test_parity_with_positions_realized_total():
    _reset()
    _fill("AAA", "buy", 10, 50.0, "2024-01-02T15:00:00+00:00", fees=1.0)
    _fill("AAA", "sell", 6, 60.0, "2024-03-02T15:00:00+00:00", fees=0.5)
    _fill("BBB", "buy", 4, 120.0, "2024-01-05T15:00:00+00:00")
    _fill("BBB", "sell", 4, 100.0, "2024-09-05T15:00:00+00:00")     # a loss
    tax_total = sum(l["gain"] for l in aj_tax.tax_lots("paper"))
    pos_total = sum(t["realized"] for t in aj_positions.realized_trades("paper"))
    assert abs(tax_total - pos_total) < 1e-6, (tax_total, pos_total)


# ── per-year rollup ────────────────────────────────────────────────────────────

def test_per_year_breakdown():
    _reset()
    _fill("X", "buy", 1, 100.0, "2023-02-01T15:00:00+00:00")
    _fill("X", "sell", 1, 150.0, "2023-10-01T15:00:00+00:00")      # 2023: +50 ST
    _fill("Y", "buy", 1, 100.0, "2024-02-01T15:00:00+00:00")
    _fill("Y", "sell", 1, 80.0, "2024-10-01T15:00:00+00:00")       # 2024: -20 ST
    s = aj_tax.tax_summary("paper")
    yrs = {y["year"]: y for y in s["by_year"]}
    assert yrs[2023]["short_term_gain"] == 50.0
    assert yrs[2024]["short_term_gain"] == -20.0
    # a single-year scope filters correctly
    s23 = aj_tax.tax_summary("paper", year=2023)
    assert s23["total_realized"] == 50.0 and s23["scope_year"] == 2023


# ── estimated tax + netting ────────────────────────────────────────────────────

def test_estimated_tax_with_rates():
    _reset()
    aj_config.set_config({"tax_short_term_rate": 0.35, "tax_long_term_rate": 0.15})
    # +$100 short-term, +$200 long-term
    _fill("S", "buy", 1, 100.0, "2024-01-02T15:00:00+00:00")
    _fill("S", "sell", 1, 200.0, "2024-06-02T15:00:00+00:00")       # +100 ST
    _fill("L", "buy", 1, 100.0, "2022-01-02T15:00:00+00:00")
    _fill("L", "sell", 1, 300.0, "2024-01-02T15:00:00+00:00")       # +200 LT
    s = aj_tax.tax_summary("paper")
    assert s["rates_configured"] is True
    est = s["estimate"]
    assert est["tax_short_term"] == 35.0     # 100 * 0.35
    assert est["tax_long_term"] == 30.0      # 200 * 0.15
    assert est["tax_total"] == 65.0
    assert s["after_tax_realized"] == 300.0 - 65.0


def test_loss_cross_offsets_other_term():
    _reset()
    aj_config.set_config({"tax_short_term_rate": 0.35, "tax_long_term_rate": 0.15})
    # -$150 short-term loss, +$200 long-term gain -> LT reduced to $50, tax 7.50
    _fill("S", "buy", 1, 200.0, "2024-01-02T15:00:00+00:00")
    _fill("S", "sell", 1, 50.0, "2024-06-02T15:00:00+00:00")        # -150 ST
    _fill("L", "buy", 1, 100.0, "2022-01-02T15:00:00+00:00")
    _fill("L", "sell", 1, 300.0, "2024-02-02T15:00:00+00:00")       # +200 LT
    s = aj_tax.tax_summary("paper")
    est = s["estimate"]
    assert est["tax_short_term"] == 0.0
    assert est["tax_long_term"] == 7.5, est          # (200-150)*0.15
    assert est["loss_carryover"] == 0.0


def test_net_loss_yields_zero_tax_and_carryover():
    _reset()
    aj_config.set_config({"tax_short_term_rate": 0.35, "tax_long_term_rate": 0.15})
    _fill("S", "buy", 1, 300.0, "2024-01-02T15:00:00+00:00")
    _fill("S", "sell", 1, 100.0, "2024-06-02T15:00:00+00:00")       # -200 ST
    s = aj_tax.tax_summary("paper")
    assert s["estimate"]["tax_total"] == 0.0
    assert s["estimate"]["loss_carryover"] == -200.0


def test_rates_unset_reports_split_but_no_liability():
    _reset()
    _fill("S", "buy", 1, 100.0, "2024-01-02T15:00:00+00:00")
    _fill("S", "sell", 1, 200.0, "2024-06-02T15:00:00+00:00")
    s = aj_tax.tax_summary("paper")
    assert s["rates_configured"] is False
    assert s["short_term_gain"] == 100.0
    assert s["estimate"]["tax_total"] == 0.0        # invents no liability
    assert "not tax advice" in s["note"]


# ── wash sale heuristic ─────────────────────────────────────────────────────────

def test_wash_sale_flag_on_repurchase_within_30d():
    _reset()
    # sell at a loss, then rebuy the same name 10 days later -> flagged
    _fill("W", "buy", 10, 100.0, "2024-01-02T15:00:00+00:00")
    _fill("W", "sell", 10, 90.0, "2024-03-01T15:00:00+00:00")       # -100 loss
    _fill("W", "buy", 10, 92.0, "2024-03-11T15:00:00+00:00")        # rebuy in 10d
    lots = aj_tax.tax_lots("paper")
    loss_lot = next(l for l in lots if l["gain"] < 0)
    assert loss_lot["wash_sale"] is True
    s = aj_tax.tax_summary("paper")
    assert s["wash_sale_flags"] >= 1


def test_no_wash_flag_on_gain_or_distant_rebuy():
    _reset()
    _fill("G", "buy", 10, 100.0, "2024-01-02T15:00:00+00:00")
    _fill("G", "sell", 10, 120.0, "2024-03-01T15:00:00+00:00")      # a GAIN
    _fill("G", "buy", 10, 118.0, "2024-03-05T15:00:00+00:00")       # rebuy — but gain
    lots = aj_tax.tax_lots("paper")
    gain_lot = next(l for l in lots if l["gain"] > 0)
    assert gain_lot["wash_sale"] is False


# ── CSV export ─────────────────────────────────────────────────────────────────

def test_lots_csv_export():
    _reset()
    _fill("C", "buy", 1, 100.0, "2024-01-02T15:00:00+00:00")
    _fill("C", "sell", 1, 150.0, "2024-06-02T15:00:00+00:00")
    csv = aj_tax.realized_lots_csv("paper")
    lines = csv.strip().split("\n")
    assert lines[0].startswith("symbol,term,open_date,close_date")
    assert any(row.startswith("C,short,2024-01-02,2024-06-02") for row in lines[1:])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print("aj_tax — {} tests".format(len(fns)))
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
