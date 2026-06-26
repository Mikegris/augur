"""
jarvis_policy — the user's Investment Policy Statement, enforced by Jarvis.

The user declares standing portfolio rules in plain terms ("max 25% per
name", "keep crypto under 20%") and Jarvis holds them to it: check_portfolio()
flags live violations with a severity band, and check_proposal() warns when a
proposed action would itself break a rule (today: the single-buy dollar cap).

Rules persist as a JSON list under the settings key "jarvis_policy_rules".
Each rule is {"kind": <one of KINDS>, "value": <number>} and there is at most
one rule per kind — setting a kind replaces it.

Design rules, mirrored from the rest of the Jarvis stack:
  * lazy imports — database (and jarvis, for the live pulse) are imported
    inside functions so importing this module never touches the DB and a
    broken sibling module can't take the policy engine down with it.
  * fail-open everywhere — readers return empty/None on any failure;
    check_proposal() runs inline in the proposal path and must never raise.
  * remove_rule() contract — the DELETE route 404s only on `is False`, so
    "nothing to remove" (unknown kind OR known kind not currently set)
    returns the literal bool False; an actual removal returns the ok-dict.

Python 3.9 compatible.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional, Union

log = logging.getLogger("augur.jarvis_policy")

_SETTINGS_KEY = "jarvis_policy_rules"

# Public whitelist — jarvis_tools introspects this constant to tighten its
# own argument validation, so keep the name KINDS and the order stable
# (describe_rules() also renders in this order).
KINDS = (
    "max_weight_pct",        # no single holding above N% of the book
    "max_crypto_pct",        # summed crypto weight below N%
    "max_positions",         # at most N positions held
    "max_single_buy_usd",    # never buy more than $N in one go (proposal-time)
)

# Inclusive value ranges per kind. Percent caps and the position count share
# 1-100; the dollar cap allows anything from $1 to $10M.
_RANGES = {
    "max_weight_pct": (1, 100),
    "max_crypto_pct": (1, 100),
    "max_positions": (1, 100),
    "max_single_buy_usd": (1, 10_000_000),
}

# Severity bands: this far over the line (or closer) reads as "warn";
# anything further is a "breach". Positions are counted, not percent.
_WARN_BAND_PCT = 2.0
_WARN_BAND_POSITIONS = 1


# ─── persistence ─────────────────────────────────────────────────────────────

def _load_rules() -> List[Dict[str, Any]]:
    """Rules from settings, sanitized. Junk (missing key, bad JSON, wrong
    shapes, unknown kinds, non-numeric values) is silently dropped rather
    than propagated — a corrupted setting must not break every caller."""
    import database as db
    raw = (db.get_settings() or {}).get(_SETTINGS_KEY)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    seen = set()
    for r in data:
        if not isinstance(r, dict):
            continue
        kind, value = r.get("kind"), r.get("value")
        # bool is an int subclass — reject it explicitly so a stray `true`
        # in hand-edited JSON doesn't become a cap of 1.
        if (kind in KINDS and kind not in seen
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))):
            out.append({"kind": kind, "value": value})
            seen.add(kind)
    return out


def _save_rules(rules: List[Dict[str, Any]]) -> None:
    import database as db
    db.set_setting(_SETTINGS_KEY, json.dumps(rules))


# ─── CRUD ────────────────────────────────────────────────────────────────────

def get_rules() -> List[Dict[str, Any]]:
    """Current rules — always a list, even when the DB is unreachable."""
    try:
        return _load_rules()
    except Exception:
        log.exception("get_rules failed")
        return []


def _coerce_value(kind: str, value: Any) -> Union[int, float, None]:
    """Numeric coercion + per-kind range check. None means invalid."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    if kind == "max_positions":
        # A position count is a whole number; round rather than reject so
        # parse_rule float output ("15.0") lands cleanly.
        v = int(round(v))
    lo, hi = _RANGES[kind]
    if not (lo <= v <= hi):
        return None
    # Store ints as ints so the JSON round-trips without trailing ".0".
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return v


def set_rule(kind: str, value: Any) -> Dict[str, Any]:
    """Set or replace the rule for `kind`. Returns {"ok": True, "rules": [...]}
    or {"error": str} — never raises."""
    try:
        kind = str(kind or "").strip()
        if kind not in KINDS:
            return {"error": "unknown rule kind: {} (valid: {})".format(
                kind or "(empty)", ", ".join(KINDS))}
        v = _coerce_value(kind, value)
        if v is None:
            lo, hi = _RANGES[kind]
            return {"error": "value for {} must be a number between {} and {}".format(
                kind, lo, hi)}
        rules = [r for r in _load_rules() if r["kind"] != kind]
        rules.append({"kind": kind, "value": v})
        # Keep declaration order canonical (KINDS order) so describe_rules()
        # and the settings JSON stay stable across re-sets.
        rules.sort(key=lambda r: KINDS.index(r["kind"]))
        _save_rules(rules)
        return {"ok": True, "rules": rules}
    except Exception as e:
        log.exception("set_rule failed")
        return {"error": str(e)}


def remove_rule(kind: str) -> Union[Dict[str, Any], bool]:
    """Remove the rule for `kind`. Returns {"ok": True, "rules": [...]} when
    something was actually removed; the literal False when there was nothing
    to remove (unknown kind or kind not currently set) — the DELETE route
    maps `is False` to 404."""
    try:
        kind = str(kind or "").strip()
        if kind not in KINDS:
            return False
        rules = _load_rules()
        kept = [r for r in rules if r["kind"] != kind]
        if len(kept) == len(rules):
            return False
        _save_rules(kept)
        return {"ok": True, "rules": kept}
    except Exception:
        log.exception("remove_rule failed")
        return False


# ─── natural-language parsing ────────────────────────────────────────────────

# One number token: optional $, digits (with optional thousands commas and
# decimals), then an optional k / % / "percent" suffix.
_NUM_RE = re.compile(
    r"(\$)?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(k\b|%|percent\b|pct\b)?",
    re.IGNORECASE)

_CRYPTO_WORDS = ("crypto", "coins", "coin ", "bitcoin", "btc")
_BUY_WORDS = ("buy", "purchase", "per trade", "per order", "single trade",
              "single order", "at once", "one trade", "a trade")
_WEIGHT_WORDS = ("per name", "per position", "per holding", "per stock",
                 "per ticker", "single position", "single name",
                 "single holding", "single stock", "any position",
                 "any name", "any holding", "one position", "one name",
                 "each position", "each name", "each holding",
                 "a position", "concentration")
_COUNT_WORDS = ("positions", "names", "holdings", "tickers", "stocks")


def _numbers(text: str) -> List[Dict[str, Any]]:
    """All number tokens in the text, each annotated with how it was spelled:
    pct (%-ish suffix) and usd ($ prefix or k suffix)."""
    out = []
    for m in _NUM_RE.finditer(text):
        dollar, digits, suffix = m.group(1), m.group(2), (m.group(3) or "")
        try:
            v = float(digits.replace(",", ""))
        except ValueError:
            continue
        suffix = suffix.lower()
        if suffix == "k":
            v *= 1000
        out.append({"value": v,
                    "pct": suffix in ("%", "percent", "pct"),
                    "usd": bool(dollar) or suffix == "k"})
    return out


def _pick(nums: List[Dict[str, Any]], pct: Optional[bool] = None,
          usd: Optional[bool] = None, lo: float = 1,
          hi: float = 10_000_000) -> Optional[float]:
    """First number matching the spelled-as filters and range. Explicitly
    spelled tokens win: a %-marked number is preferred for pct picks even
    when a plain number appears earlier ("top 5 rule: max 25% ...")."""
    # Pass 1 — numbers whose spelling matches exactly (e.g. pct=True finds
    # only %-marked tokens). Pass 2 — relax to "not contradicted" (a plain
    # number can serve as a percent, but a $-marked one cannot).
    for strict in (True, False):
        for n in nums:
            if pct is not None:
                if strict and n["pct"] != pct:
                    continue
                if not strict and pct and n["usd"]:
                    continue  # "$5,000" is never a percent
                if not strict and not pct and n["pct"]:
                    continue  # "25%" is never a count/dollar amount
            if usd is not None and strict and n["usd"] != usd:
                continue
            if not (lo <= n["value"] <= hi):
                continue
            return n["value"]
    return None


def parse_rule(text: Any) -> Optional[Dict[str, Any]]:
    """Parse one natural-language policy phrase into {"kind", "value"}.
    Returns None when there is no confident parse — the POST route 400s on
    None, so guessing wrong is worse than declining."""
    try:
        if not isinstance(text, str) or not text.strip():
            return None
        t = " " + text.lower().strip() + " "   # pad so \b-ish substring checks work at edges
        nums = _numbers(t)
        if not nums:
            return None

        # Order matters: each branch claims its keyword territory before the
        # broader ones below get a look ("no single position over 30%" must
        # not fall through to the position-count branch).
        if any(w in t for w in _CRYPTO_WORDS):
            v = _pick(nums, pct=True, lo=1, hi=100)
            return {"kind": "max_crypto_pct", "value": _norm(v)} if v is not None else None

        if any(w in t for w in _BUY_WORDS):
            v = _pick(nums, pct=False, lo=1, hi=10_000_000)
            return {"kind": "max_single_buy_usd", "value": _norm(v)} if v is not None else None

        if any(w in t for w in _WEIGHT_WORDS):
            v = _pick(nums, pct=True, lo=1, hi=100)
            return {"kind": "max_weight_pct", "value": _norm(v)} if v is not None else None

        # Bare "position" + a percent — the very example the set_policy_rule
        # tool advertises ("max position 10%"). Guard on a %-spelled number so
        # this doesn't poach "10 positions" (count, no pct) from the branch
        # below; the singular word avoids matching the plural "positions".
        if re.search(r"\bposition\b", t) and _pick(nums, pct=True, lo=1, hi=100) is not None:
            v = _pick(nums, pct=True, lo=1, hi=100)
            return {"kind": "max_weight_pct", "value": _norm(v)} if v is not None else None

        if any(w in t for w in _COUNT_WORDS):
            v = _pick(nums, pct=False, usd=False, lo=1, hi=100)
            if v is not None and float(v).is_integer():
                return {"kind": "max_positions", "value": int(v)}
            return None

        return None
    except Exception:
        log.exception("parse_rule failed")
        return None


def _norm(v: Optional[float]) -> Union[int, float, None]:
    """25.0 → 25 so parsed rules JSON-render without a trailing .0."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


# ─── enforcement ─────────────────────────────────────────────────────────────

def _severity_pct(over: float) -> str:
    return "warn" if over <= _WARN_BAND_PCT else "breach"


def check_portfolio(pulse: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Check the live portfolio against every standing rule.

    Returns {"rules": <count>, "violations": [{"kind","detail","severity"}]}.
    (Also mirrors the count under "n" — jarvis_tools reads that key.)
    `pulse` is the jarvis._portfolio_pulse() shape; fetched lazily when not
    supplied. max_single_buy_usd is proposal-time only and never appears here.
    """
    rules = get_rules()
    out: Dict[str, Any] = {"rules": len(rules), "n": len(rules),
                           "violations": []}
    if not rules:
        return out
    try:
        if pulse is None:
            import jarvis
            pulse = jarvis._portfolio_pulse()
    except Exception:
        log.exception("check_portfolio could not fetch portfolio pulse")
        pulse = None
    if not isinstance(pulse, dict):
        return out

    holdings = [h for h in (pulse.get("holdings") or []) if isinstance(h, dict)]
    by_kind = {r["kind"]: float(r["value"]) for r in rules}
    violations: List[Dict[str, Any]] = []

    # Each rule check is individually guarded — one malformed holding row
    # must not suppress the other rules' findings.
    limit = by_kind.get("max_weight_pct")
    if limit is not None:
        try:
            for h in holdings:
                w = h.get("weight_pct")
                if isinstance(w, (int, float)) and not isinstance(w, bool) and w > limit:
                    violations.append({
                        "kind": "max_weight_pct",
                        "detail": "{} is {:.1f}% of the book — cap is {:g}%".format(
                            h.get("symbol") or "?", w, limit),
                        "severity": _severity_pct(w - limit)})
        except Exception:
            log.exception("max_weight_pct check failed")

    limit = by_kind.get("max_crypto_pct")
    if limit is not None:
        try:
            # Mirror the max_weight_pct coercion: a bool weight_pct (int
            # subclass) or a non-numeric string must neither add 1 nor raise
            # inside sum() and silently void the whole crypto check.
            csum = sum(w for h in holdings if h.get("asset_type") == "crypto"
                       for w in [h.get("weight_pct")]
                       if isinstance(w, (int, float)) and not isinstance(w, bool))
            if csum > limit:
                violations.append({
                    "kind": "max_crypto_pct",
                    "detail": "crypto is {:.1f}% of the book — cap is {:g}%".format(
                        csum, limit),
                    "severity": _severity_pct(csum - limit)})
        except Exception:
            log.exception("max_crypto_pct check failed")

    limit = by_kind.get("max_positions")
    if limit is not None:
        try:
            n = pulse.get("num_positions")
            if not isinstance(n, int) or isinstance(n, bool):
                n = len(holdings)
            if n > limit:
                over = n - limit
                violations.append({
                    "kind": "max_positions",
                    "detail": "{} positions held — cap is {:g}".format(n, limit),
                    "severity": "warn" if over <= _WARN_BAND_POSITIONS else "breach"})
        except Exception:
            log.exception("max_positions check failed")

    out["violations"] = violations
    return out


# Proposal args fields that carry a dollar amount, in trust order.
_AMOUNT_FIELDS = ("amount", "value", "usd", "market_value")


def check_proposal(tool: Any, args: Any) -> Optional[str]:
    """Warning string when a proposed action would breach max_single_buy_usd,
    else None. Runs inline in the proposal path — never raises. Only fires
    when the args carry an amount-ish numeric field; tools without one (and
    every other rule kind) pass silently."""
    try:
        cap = next((float(r["value"]) for r in get_rules()
                    if r.get("kind") == "max_single_buy_usd"), None)
        if cap is None or not isinstance(args, dict):
            return None
        for field in _AMOUNT_FIELDS:
            v = args.get(field)
            if v is None or isinstance(v, bool):
                continue
            try:
                amt = float(str(v).replace(",", "").replace("$", "").strip())
            except (TypeError, ValueError):
                continue
            if not math.isfinite(amt):
                continue
            # First amount-ish field decides — checking later fields too
            # would double-count (e.g. amount + market_value on one trade).
            if amt > cap:
                return ("policy: {} of ${:,.0f} exceeds your max single buy "
                        "of ${:,.0f}".format(str(tool or "this action"), amt, cap))
            return None
        return None
    except Exception:
        log.exception("check_proposal failed")
        return None


# ─── description ─────────────────────────────────────────────────────────────

_DESCRIBE = {
    "max_weight_pct": lambda v: "max {:g}% per name".format(v),
    "max_crypto_pct": lambda v: "crypto under {:g}%".format(v),
    "max_positions": lambda v: "max {:g} positions".format(v),
    "max_single_buy_usd": lambda v: "max ${:,.0f} per buy".format(v),
}


def describe_rules() -> str:
    """One human sentence: 'Policy: max 25% per name; crypto under 20%.'"""
    try:
        rules = get_rules()
        if not rules:
            return "No policy set."
        parts = [_DESCRIBE[r["kind"]](float(r["value"])) for r in rules]
        return "Policy: " + "; ".join(parts) + "."
    except Exception:
        log.exception("describe_rules failed")
        return "No policy set."
