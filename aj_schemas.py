"""AJTA — Analyst Council typed schemas (merge plan §5.3).

Structured inter-agent protocol for the multi-agent "Analyst Council" advisory
layer. Adapted in spirit from TauricResearch/TradingAgents (Apache-2.0)
`agents/schemas.py` — see NOTICE/THIRD_PARTY_NOTICES.md. Reimplemented from
scratch as plain dataclasses (NOT pydantic) to match this codebase's
manual-validation house style and to add zero heavy dependency surface to the
py2app desktop bundle (a merge invariant).

Every schema is:
  * tolerant of messy LLM output — `from_llm()` extracts/repairs JSON and clamps
    every field to a safe range (never raises on bad model output);
  * deterministic — identical inputs render identically;
  * auditable — `to_audit()` returns a canonical, hash-stable dict, and
    `render()` returns markdown for logs/UI.

These are DATA ONLY. They never trade, size, or bypass a gate. The CouncilDecision
is advisory input to aj_operator._judge; the fail-closed risk gate
(aj_risk.evaluate) remains the sole execution authority.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


# ── enums ────────────────────────────────────────────────────────────────────

class Rating(str, Enum):
    """5-tier conviction rating (manager/decision level)."""
    BUY = "BUY"
    OVERWEIGHT = "OVERWEIGHT"
    HOLD = "HOLD"
    UNDERWEIGHT = "UNDERWEIGHT"
    SELL = "SELL"


class Action(str, Enum):
    """3-tier executable action (trader level). Deliberately narrower than
    Rating — a forcing function borrowed from TradingAgents."""
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


class SentimentBand(str, Enum):
    """6-tier sentiment band."""
    VERY_BEARISH = "VERY_BEARISH"
    BEARISH = "BEARISH"
    SLIGHTLY_BEARISH = "SLIGHTLY_BEARISH"
    SLIGHTLY_BULLISH = "SLIGHTLY_BULLISH"
    BULLISH = "BULLISH"
    VERY_BULLISH = "VERY_BULLISH"


# Rating → signed score in [-2, +2]; the conviction/sizing math reads this.
_RATING_SCORE = {
    Rating.BUY: 2.0, Rating.OVERWEIGHT: 1.0, Rating.HOLD: 0.0,
    Rating.UNDERWEIGHT: -1.0, Rating.SELL: -2.0,
}
_RATING_ACTION = {
    Rating.BUY: Action.BUY, Rating.OVERWEIGHT: Action.BUY, Rating.HOLD: Action.HOLD,
    Rating.UNDERWEIGHT: Action.SELL, Rating.SELL: Action.SELL,
}
_BAND_SCORE = {
    SentimentBand.VERY_BEARISH: 0.0, SentimentBand.BEARISH: 2.0,
    SentimentBand.SLIGHTLY_BEARISH: 4.0, SentimentBand.SLIGHTLY_BULLISH: 6.0,
    SentimentBand.BULLISH: 8.0, SentimentBand.VERY_BULLISH: 10.0,
}


# ── tolerant coercion helpers ────────────────────────────────────────────────

def clamp(x: Any, lo: float, hi: float, default: float = 0.0) -> float:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def parse_rating(v: Any, default: Rating = Rating.HOLD) -> Rating:
    if isinstance(v, Rating):
        return v
    s = str(v or "").strip().upper().replace(" ", "_").replace("-", "_")
    for r in Rating:
        if r.value == s:
            return r
    # tolerant aliases
    aliases = {
        "STRONG_BUY": Rating.BUY, "ADD": Rating.OVERWEIGHT, "ACCUMULATE": Rating.OVERWEIGHT,
        "NEUTRAL": Rating.HOLD, "WAIT": Rating.HOLD, "REDUCE": Rating.UNDERWEIGHT,
        "TRIM": Rating.UNDERWEIGHT, "STRONG_SELL": Rating.SELL, "EXIT": Rating.SELL,
        "LONG": Rating.BUY, "SHORT": Rating.SELL,
    }
    return aliases.get(s, default)


def parse_action(v: Any, default: Action = Action.HOLD) -> Action:
    if isinstance(v, Action):
        return v
    s = str(v or "").strip().upper()
    for a in Action:
        if a.value == s:
            return a
    if s in ("LONG", "STRONG_BUY", "ACCUMULATE", "ADD", "OVERWEIGHT"):
        return Action.BUY
    if s in ("SHORT", "STRONG_SELL", "EXIT", "TRIM", "REDUCE", "UNDERWEIGHT"):
        return Action.SELL
    return default


def parse_band(v: Any, default: SentimentBand = SentimentBand.SLIGHTLY_BULLISH) -> SentimentBand:
    if isinstance(v, SentimentBand):
        return v
    s = str(v or "").strip().upper().replace(" ", "_").replace("-", "_")
    for b in SentimentBand:
        if b.value == s:
            return b
    return default


def _str_list(v: Any, cap: int = 12) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        items = [p.strip(" -*\t") for p in v.splitlines() if p.strip()]
    elif isinstance(v, (list, tuple)):
        items = [str(x).strip() for x in v]
    else:
        items = [str(v).strip()]
    return [x for x in items if x][:cap]


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: Any) -> Dict[str, Any]:
    """Best-effort: pull the first JSON object out of an LLM response that may
    be wrapped in prose or ```json fences. Returns {} on failure (never raises)."""
    if isinstance(text, dict):
        return text
    if not text:
        return {}
    s = str(text)
    # strip code fences
    s = re.sub(r"```(?:json)?", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    m = _JSON_OBJ_RE.search(s)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


# ── schemas ──────────────────────────────────────────────────────────────────

@dataclass
class AnalystReport:
    """One analyst's evidence bundle. score is 0..10 (10 = most bullish)."""
    analyst: str
    band: str                       # SentimentBand value (or free band label)
    score: float = 5.0              # 0..10
    confidence: float = 0.5         # 0..1
    key_points: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)
    narrative: str = ""

    def __post_init__(self):
        self.score = clamp(self.score, 0.0, 10.0, 5.0)
        self.confidence = clamp(self.confidence, 0.0, 1.0, 0.5)
        self.key_points = _str_list(self.key_points)
        self.evidence_refs = _str_list(self.evidence_refs, cap=20)
        self.analyst = str(self.analyst or "analyst")
        self.band = str(self.band or "")
        self.narrative = str(self.narrative or "")

    @classmethod
    def from_llm(cls, analyst: str, text: Any, *, band_is_sentiment: bool = False,
                 evidence_refs: Optional[List[str]] = None) -> "AnalystReport":
        d = extract_json(text)
        band_raw = d.get("band") or d.get("sentiment") or d.get("rating") or ""
        band = parse_band(band_raw).value if band_is_sentiment else str(band_raw)
        score = d.get("score")
        if score is None and band_is_sentiment:
            score = _BAND_SCORE.get(parse_band(band_raw), 5.0)
        return cls(
            analyst=analyst, band=band, score=score if score is not None else 5.0,
            confidence=d.get("confidence", 0.5),
            key_points=d.get("key_points") or d.get("points") or [],
            evidence_refs=evidence_refs or d.get("evidence") or [],
            narrative=d.get("narrative") or d.get("summary") or (
                str(text)[:1200] if not d else ""),
        )

    def render(self) -> str:
        pts = "\n".join("- " + p for p in self.key_points) or "- (no points)"
        return ("**{a} analyst** — band={b} score={s:.1f}/10 conf={c:.0%}\n{p}\n{n}"
                .format(a=self.analyst, b=self.band, s=self.score, c=self.confidence,
                        p=pts, n=self.narrative[:600]))

    def to_audit(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchPlan:
    """Research Manager's definitive stance after the bull/bear debate."""
    recommendation: Rating = Rating.HOLD
    rationale: str = ""
    strategic_actions: List[str] = field(default_factory=list)

    def __post_init__(self):
        self.recommendation = parse_rating(self.recommendation)
        self.rationale = str(self.rationale or "")
        self.strategic_actions = _str_list(self.strategic_actions)

    @classmethod
    def from_llm(cls, text: Any) -> "ResearchPlan":
        d = extract_json(text)
        return cls(
            recommendation=parse_rating(d.get("recommendation") or d.get("rating")),
            rationale=d.get("rationale") or d.get("thesis") or (str(text)[:1200] if not d else ""),
            strategic_actions=d.get("strategic_actions") or d.get("actions") or [],
        )

    def render(self) -> str:
        acts = "\n".join("- " + a for a in self.strategic_actions)
        return "**Research stance:** {r}\n{rat}\n{a}".format(
            r=self.recommendation.value, rat=self.rationale[:800], a=acts)

    def to_audit(self) -> Dict[str, Any]:
        d = asdict(self)
        d["recommendation"] = self.recommendation.value
        return d


@dataclass
class TraderProposal:
    """Trader's 3-tier executable proposal derived from the research plan."""
    action: Action = Action.HOLD
    reasoning: str = ""
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    position_hint: Optional[str] = None

    def __post_init__(self):
        self.action = parse_action(self.action)
        self.reasoning = str(self.reasoning or "")
        self.entry_price = _opt_pos_float(self.entry_price)
        self.stop_loss = _opt_pos_float(self.stop_loss)
        if self.position_hint is not None:
            self.position_hint = str(self.position_hint)

    @classmethod
    def from_llm(cls, text: Any) -> "TraderProposal":
        d = extract_json(text)
        return cls(
            action=parse_action(d.get("action") or d.get("decision")),
            reasoning=d.get("reasoning") or d.get("rationale") or (str(text)[:1000] if not d else ""),
            entry_price=d.get("entry_price") or d.get("entry"),
            stop_loss=d.get("stop_loss") or d.get("stop"),
            position_hint=d.get("position_sizing") or d.get("position_hint") or d.get("size"),
        )

    def render(self) -> str:
        return "**Trader:** {a} (entry={e}, stop={s})\n{r}".format(
            a=self.action.value, e=self.entry_price, s=self.stop_loss,
            r=self.reasoning[:600])

    def to_audit(self) -> Dict[str, Any]:
        d = asdict(self)
        d["action"] = self.action.value
        return d


@dataclass
class RiskStance:
    """One risk debater's turn (aggressive | conservative | neutral)."""
    perspective: str
    assessment: str = ""
    recommended_action: Action = Action.HOLD
    round: int = 0

    def __post_init__(self):
        self.perspective = str(self.perspective or "neutral").lower()
        self.assessment = str(self.assessment or "")
        self.recommended_action = parse_action(self.recommended_action)
        self.round = int(self.round or 0)

    @classmethod
    def from_llm(cls, perspective: str, round_: int, text: Any) -> "RiskStance":
        d = extract_json(text)
        return cls(
            perspective=perspective, round=round_,
            assessment=d.get("assessment") or (str(text)[:900] if not d else ""),
            recommended_action=parse_action(d.get("recommended_action") or d.get("action")),
        )

    def render(self) -> str:
        return "**{p} (r{n})** → {a}: {x}".format(
            p=self.perspective, n=self.round, a=self.recommended_action.value,
            x=self.assessment[:500])

    def to_audit(self) -> Dict[str, Any]:
        d = asdict(self)
        d["recommended_action"] = self.recommended_action.value
        return d


@dataclass
class CouncilDecision:
    """Final advisory output of the council for one symbol. Advisory ONLY — the
    risk gate decides execution. `conviction` is 0..1 and only ever influences
    SIZING within existing caps (never gate bypass)."""
    symbol: str
    rating: Rating = Rating.HOLD
    action: Action = Action.HOLD
    conviction: float = 0.0
    thesis: str = ""
    price_target: Optional[float] = None
    time_horizon: Optional[str] = None
    stop_hint: Optional[float] = None
    dissent: str = ""
    # provenance / cost (filled by aj_council)
    cost_usd: float = 0.0
    latency_ms: int = 0
    n_calls: int = 0
    status: str = "ok"              # ok | degraded | error | skipped

    def __post_init__(self):
        self.symbol = str(self.symbol or "").upper()
        self.rating = parse_rating(self.rating)
        self.action = parse_action(self.action)
        self.conviction = clamp(self.conviction, 0.0, 1.0, 0.0)
        self.thesis = str(self.thesis or "")
        self.price_target = _opt_pos_float(self.price_target)
        self.stop_hint = _opt_pos_float(self.stop_hint)
        if self.time_horizon is not None:
            self.time_horizon = str(self.time_horizon)
        self.dissent = str(self.dissent or "")
        self.cost_usd = clamp(self.cost_usd, 0.0, 1e9, 0.0)
        self.n_calls = int(max(0, self.n_calls or 0))

    @classmethod
    def from_plan(cls, symbol: str, plan: "ResearchPlan",
                  proposal: Optional["TraderProposal"] = None,
                  confidence: float = 0.5, **prov) -> "CouncilDecision":
        """Build a decision from the research plan (+ optional trader proposal),
        deriving action and conviction deterministically."""
        rating = plan.recommendation
        action = proposal.action if proposal else _RATING_ACTION.get(rating, Action.HOLD)
        conviction = rating_conviction(rating, confidence)
        return cls(symbol=symbol, rating=rating, action=action, conviction=conviction,
                   thesis=plan.rationale,
                   stop_hint=(proposal.stop_loss if proposal else None),
                   price_target=(proposal.entry_price if proposal else None),
                   **prov)

    def signed_score(self) -> float:
        """Signed conviction in [-1, +1]: positive = bullish, for sizing math."""
        base = _RATING_SCORE.get(self.rating, 0.0) / 2.0
        return max(-1.0, min(1.0, base)) * self.conviction

    def render(self) -> str:
        return ("**COUNCIL {sym}** → {r}/{a} conviction={c:.0%} "
                "(target={t}, horizon={h}, stop={s})\n{th}\n_dissent:_ {d}").format(
            sym=self.symbol, r=self.rating.value, a=self.action.value,
            c=self.conviction, t=self.price_target, h=self.time_horizon,
            s=self.stop_hint, th=self.thesis[:800], d=self.dissent[:400] or "none")

    def to_audit(self) -> Dict[str, Any]:
        d = asdict(self)
        d["rating"] = self.rating.value
        d["action"] = self.action.value
        return d


def _opt_pos_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f <= 0:
        return None
    return f


def rating_conviction(rating: Rating, confidence: float) -> float:
    """Map (rating magnitude × confidence) → conviction in [0, 1]. HOLD → low."""
    mag = abs(_RATING_SCORE.get(rating, 0.0)) / 2.0   # 0, .5, 1
    return clamp(mag * clamp(confidence, 0.0, 1.0, 0.5), 0.0, 1.0, 0.0)
