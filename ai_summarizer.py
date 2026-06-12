"""
AI Filing Summarizer — uses OpenAI (MODEL_HEAVY for deep analyses,
MODEL_LIGHT for short summaries; see the model-selection block below).
Falls back to rule-based extraction if no API key configured.

Caching & daily cap:
  - Functions with *immutable* inputs (filing accession, ticker+date) are
    wrapped in cache_store.coalesce so we never pay twice for the same
    summary. Filing summaries get 7d, portfolio analyses 1d.
  - A SQLite-persisted daily counter (ai_call_log) caps total OpenAI calls
    per local day. Default 200; override via AUGUR_AI_DAILY_CAP. Past the
    cap we return a structured error envelope rather than spending more.
"""
import os
import re
import json
import logging

try:
    import cache_store
except Exception:  # pragma: no cover
    cache_store = None

log = logging.getLogger(__name__)

# ── Model selection (centralized) ────────────────────────────────────────────
# Verified against the OpenAI model docs (developers.openai.com/api/docs/models,
# June 2026): gpt-5.5 (snapshot gpt-5.5-2026-04-23) is the current flagship for
# professional/analytical work; gpt-5.4-mini (snapshot gpt-5.4-mini-2026-03-17,
# $0.75/$4.50 per 1M tokens) is the strongest cheap/fast variant — there is no
# gpt-5.5-mini. Both support Chat Completions and JSON-mode structured outputs.
# Override either tier via env, no code changes needed:
#   AUGUR_OPENAI_MODEL_HEAVY — deep analyses (portfolio, earnings briefs)
#   AUGUR_OPENAI_MODEL_LIGHT — short summaries (filings, insiders, theses)
_DEFAULT_MODEL_HEAVY = "gpt-5.5"
_DEFAULT_MODEL_LIGHT = "gpt-5.4-mini"
MODEL_HEAVY = (os.environ.get("AUGUR_OPENAI_MODEL_HEAVY") or _DEFAULT_MODEL_HEAVY).strip() or _DEFAULT_MODEL_HEAVY
MODEL_LIGHT = (os.environ.get("AUGUR_OPENAI_MODEL_LIGHT") or _DEFAULT_MODEL_LIGHT).strip() or _DEFAULT_MODEL_LIGHT
_DEFAULT_MODEL_TTS = "gpt-4o-mini-tts"
MODEL_TTS = (os.environ.get("AUGUR_OPENAI_MODEL_TTS") or _DEFAULT_MODEL_TTS).strip() or _DEFAULT_MODEL_TTS

# Last-resort retry target when the configured model isn't available on the
# user's account (e.g. an account without gpt-5.x access yet).
MODEL_FALLBACK = "gpt-4o"

# Existing API routes / CLI still pass the historic defaults explicitly
# (app.py sends "gpt-4o", cli.py defaults to "gpt-4o"). Treat those as "use
# the configured tier" so the upgrade applies app-wide without touching the
# callers; any other explicit model string is respected as-is.
_LEGACY_HEAVY = {None, "", "default", "gpt-4o"}
_LEGACY_LIGHT = {None, "", "default", "gpt-4o-mini"}


def _resolve_heavy_model(model):
    return MODEL_HEAVY if model in _LEGACY_HEAVY else model


def _resolve_light_model(model):
    return MODEL_LIGHT if model in _LEGACY_LIGHT else model


def _uses_reasoning_params(model):
    """gpt-5.x / o-series models reject `temperature` and `max_tokens` on Chat
    Completions — they take `max_completion_tokens` and only the default
    temperature. gpt-4o-era models keep the classic parameter names."""
    m = (model or "").lower()
    return m.startswith(("gpt-5", "o1", "o3", "o4"))


def _is_model_not_found(exc):
    """True when the API says the requested model doesn't exist / isn't
    accessible on this account — the only failure we silently retry."""
    if exc.__class__.__name__ == "NotFoundError":
        return True
    if getattr(exc, "code", None) == "model_not_found":
        return True
    msg = str(exc).lower()
    return "model" in msg and (
        "not found" in msg or "does not exist" in msg or "do not have access" in msg
    )


def _chat_completion(client, model, messages, max_tokens=None, temperature=None,
                     json_mode=True, tools=None, tool_choice=None):
    """Single seam for every OpenAI chat call in the app.

    - Maps parameters per model family (gpt-5.x/o-series get
      `max_completion_tokens` and no `temperature`; older models get
      `max_tokens` + `temperature`).
    - If the chosen model isn't available on the user's account, retries once
      with MODEL_FALLBACK so an aggressive default can never break a feature.
    """
    def _call(m):
        kwargs = {"model": m, "messages": messages}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        if _uses_reasoning_params(m):
            if max_tokens is not None:
                kwargs["max_completion_tokens"] = max_tokens
        else:
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            if temperature is not None:
                kwargs["temperature"] = temperature
        return client.chat.completions.create(**kwargs)

    try:
        return _call(model)
    except Exception as e:
        if model != MODEL_FALLBACK and _is_model_not_found(e):
            log.warning("model %r unavailable (%s); retrying with %r",
                        model, e, MODEL_FALLBACK)
            return _call(MODEL_FALLBACK)
        raise


# Cache TTLs (seconds)
_FILING_SUMMARY_TTL    = 7 * 24 * 3600    # accession is immutable
_INSIDER_PATTERN_TTL   = 6 * 3600         # txn list shifts within a day
_PORTFOLIO_TTL         = 24 * 3600        # holdings change but slowly
_EARNINGS_BRIEF_TTL    = 6 * 3600         # dossier evolves intraday (price, IV)
_IDEA_THESIS_TTL       = 12 * 3600        # picks change but inputs are stable


def _daily_cap() -> int:
    """Env override > console setting (ai_daily_cap) > default 200."""
    env = os.environ.get("AUGUR_AI_DAILY_CAP")
    if env:
        try:
            return int(env)
        except (TypeError, ValueError):
            pass
    try:
        import database as db
        saved = db.get_settings().get("ai_daily_cap")
        if saved:
            return max(1, int(saved))
    except Exception:
        pass
    return 200


def _cap_exceeded() -> bool:
    """True if today's persisted counter has reached the daily cap."""
    try:
        import database as db
        return db.get_ai_call_count() >= _daily_cap()
    except Exception:
        return False


def _record_ai_call() -> None:
    """Bump the daily counter — best-effort, never raises."""
    try:
        import database as db
        db.increment_ai_call_count()
    except Exception as e:
        log.debug("ai_call_log increment failed: %s", e)


def _cap_error_envelope(context: str) -> dict:
    return {
        "error": "Daily AI request cap reached",
        "context": context,
        "ai_powered": False,
    }


# In-process audio cache: the briefing voice line is spoken repeatedly and
# costs real money per synthesis. Tiny LRU keyed on (model, voice, text).
_TTS_CACHE_MAX = 24
_tts_cache = {}  # key -> bytes (insertion-ordered; py3.7+ dicts)


def tts_speech(text, voice="onyx"):
    """Synthesize speech via the user's OpenAI key. Returns MP3 bytes or
    None (keyless, capped, or any failure — callers fall back to browser
    speech). Counts against the daily cap like every other AI call."""
    text = (text or "").strip()
    if not text:
        return None
    text = text[:600]  # hard bound — TTS is priced per character
    key = get_openai_key()
    if not key or _cap_exceeded():
        return None
    ck = (MODEL_TTS, voice, text)
    hit = _tts_cache.get(ck)
    if hit is not None:
        return hit
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=20)
        resp = client.audio.speech.create(model=MODEL_TTS, voice=voice, input=text)
        audio = resp.read() if hasattr(resp, "read") else getattr(resp, "content", None)
        if not audio:
            return None
        _record_ai_call()
        _tts_cache[ck] = audio
        while len(_tts_cache) > _TTS_CACHE_MAX:
            _tts_cache.pop(next(iter(_tts_cache)))
        return audio
    except Exception as e:
        log.debug("tts_speech failed: %s", e)
        return None


def get_openai_key():
    """Resolve via the central provider registry (env > DB)."""
    try:
        import api_keys
        return api_keys.get_api_key("openai")
    except Exception:
        # registry unavailable (partial install) — legacy direct path
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            try:
                import database as db
                key = db.get_settings().get("openai_api_key", "")
            except Exception:
                pass
        return key.strip()


def summarize_filing(filing_text, form_type, ticker, description=""):
    """
    Returns structured analysis dict:
    {
        signal: "BULLISH" | "BEARISH" | "NEUTRAL" | "MATERIAL",
        summary: str (1-2 sentence plain English),
        key_points: list[str] (3-5 bullet points),
        event_type: str,
        confidence: "HIGH" | "MEDIUM" | "LOW",
        ai_powered: bool,
    }
    """
    key = get_openai_key()
    if key:
        return _openai_summarize(filing_text, form_type, ticker, description, key)
    else:
        return _rule_based_summarize(filing_text, form_type, ticker, description)


def _openai_summarize(text, form_type, ticker, description, api_key):
    # Filing inputs are immutable (a filing accession's text never changes),
    # so cache the AI summary keyed on a content hash. Fall back to rule-based
    # if the daily cap is reached.
    import hashlib
    text_hash = hashlib.sha256((text or "")[:10000].encode("utf-8", "ignore")).hexdigest()[:16]
    cache_key = ("ai_summarize_filing", form_type, ticker, text_hash)

    def _call():
        if _cap_exceeded():
            return _cap_error_envelope("summarize_filing")
        try:
            from openai import OpenAI
            # Bound request time — the SDK's default is 10 min, which is too
            # long for any user-facing call. 30s is enough for any of our
            # short JSON-mode prompts; failures fall through to the rule-based
            # path so the UI never hangs.
            client = OpenAI(api_key=api_key, timeout=30.0)

            system_prompt = """You are a senior equity analyst. Analyze SEC filings and return ONLY valid JSON.
Always return this exact structure:
{
  "signal": "BULLISH" or "BEARISH" or "NEUTRAL" or "MATERIAL",
  "summary": "1-2 sentence plain English summary for a busy investor",
  "key_points": ["point 1", "point 2", "point 3"],
  "event_type": "e.g. Earnings Beat, Executive Departure, Acquisition, Guidance Cut, etc.",
  "confidence": "HIGH" or "MEDIUM" or "LOW"
}
signal meanings: BULLISH=positive for stock, BEARISH=negative, NEUTRAL=informational, MATERIAL=significant event requiring attention."""

            user_prompt = f"""Analyze this {form_type} filing for {ticker}.
Description: {description}

Filing text (excerpt):
{text[:10000]}

Return JSON only."""

            resp = _chat_completion(
                client, MODEL_LIGHT,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=500,
                temperature=0.1,
            )
            _record_ai_call()
            result = json.loads(resp.choices[0].message.content)
            result["ai_powered"] = True
            return result
        except Exception:
            return _rule_based_summarize(text, form_type, ticker, description)

    if cache_store is None:
        return _call()
    return cache_store.coalesce(cache_key, _FILING_SUMMARY_TTL, _call)


def _rule_based_summarize(text, form_type, ticker, description):
    """Structured extraction without AI."""
    text_lower = text.lower()

    # Signal detection
    bullish_terms = ["beat", "exceeded", "raised guidance", "record revenue", "strong growth",
                     "above consensus", "increased dividend", "share buyback", "upgrade"]
    bearish_terms = ["miss", "below expectations", "lowered guidance", "restructuring", "layoffs",
                     "loss", "declining", "downgrade", "investigation", "lawsuit", "restatement",
                     "going concern"]
    material_terms = ["merger", "acquisition", "ceo", "cfo", "executive", "sec investigation",
                      "bankruptcy", "delisted"]

    bull_score = sum(1 for t in bullish_terms if t in text_lower)
    bear_score = sum(1 for t in bearish_terms if t in text_lower)
    material_score = sum(1 for t in material_terms if t in text_lower)

    if material_score >= 2:
        signal = "MATERIAL"
    elif bull_score > bear_score:
        signal = "BULLISH"
    elif bear_score > bull_score:
        signal = "BEARISH"
    else:
        signal = "NEUTRAL"

    # Event type from form
    event_map = {
        "8-K": "Material Event",
        "10-K": "Annual Report",
        "10-Q": "Quarterly Report",
        "S-1": "IPO Filing",
        "13F-HR": "Institutional Holdings",
    }
    event_type = event_map.get(form_type, form_type)
    if description:
        event_type = description[:60]

    # Extract numbers
    numbers = re.findall(r'\$[\d,\.]+\s*(?:billion|million|B|M)?', text[:3000])[:3]

    key_points = [f"{form_type} filing for {ticker}"]
    if numbers:
        key_points.append(f"Key figures: {', '.join(numbers[:2])}")
    if description:
        key_points.append(description[:100])

    return {
        "signal": signal,
        "summary": f"{ticker} filed {form_type}. {description or 'See filing for details.'}",
        "key_points": key_points,
        "event_type": event_type,
        "confidence": "LOW",
        "ai_powered": False,
    }


def analyze_portfolio(holdings, summary, model=None):
    """
    Deep AI analysis of entire portfolio. Uses MODEL_HEAVY by default for
    higher quality (legacy "gpt-4o" requests are mapped to MODEL_HEAVY too).
    Returns structured dict with overall assessment, risks, opportunities, and per-position insights.
    """
    model = _resolve_heavy_model(model)
    key = get_openai_key()
    if not key:
        return _rule_based_portfolio_analysis(holdings, summary)

    # Cache key reflects positions + total state so the same portfolio
    # snapshot doesn't pay twice in a day. Sorted to be order-independent.
    import hashlib
    sig_parts = sorted(
        f"{p.get('symbol','')}:{p.get('shares','')}:{round(p.get('market_value') or 0)}"
        for p in holdings
    )
    sig = hashlib.sha256(
        ("|".join(sig_parts) + f"|{round(summary.get('total_value') or 0)}|{model}").encode("utf-8")
    ).hexdigest()[:16]
    cache_key = ("ai_analyze_portfolio", sig)

    def _do_call():
        if _cap_exceeded():
            return _cap_error_envelope("analyze_portfolio")
        return _analyze_portfolio_uncached(holdings, summary, model, key)

    if cache_store is not None:
        return cache_store.coalesce(cache_key, _PORTFOLIO_TTL, _do_call)
    return _do_call()


def _analyze_portfolio_uncached(holdings, summary, model, key):
    try:
        from openai import OpenAI
        # 60s for the larger heavy-model portfolio call; still bounded, just
        # roomier than the small JSON-mode prompts.
        client = OpenAI(api_key=key, timeout=60.0)

        # Build compact portfolio representation
        positions_text = "\n".join([
            f"- {p['symbol']} ({p['name']}, {p['asset_type']}): "
            f"{p['shares']} shares @ avg ${p['avg_cost']:.2f}, "
            f"current ${p['current_price'] or 'N/A'}, "
            f"market value ${p['market_value']:,.0f} ({p['weight_pct']:.1f}% of portfolio), "
            f"P&L ${p['unrealized_pnl']:+,.0f} ({p['unrealized_pct']:+.1f}%)"
            for p in holdings
        ])

        system_prompt = """You are a senior portfolio manager and equity analyst at a top-tier hedge fund.
Analyze the provided portfolio with the depth and rigor of a professional investment advisor.
Be specific, direct, and actionable. Avoid generic advice. Reference actual position sizes, concentrations, and P&L figures.
Return ONLY valid JSON matching the exact structure specified."""

        user_prompt = f"""Analyze this investment portfolio and return a structured JSON analysis.

PORTFOLIO SUMMARY:
- Total Value: ${summary['total_value']:,.2f}
- Total P&L: ${summary['total_pnl']:+,.2f} ({summary['total_pnl_pct']:+.2f}%)
- Cost Basis: ${summary['total_cost']:,.2f}
- Positions: {summary['num_positions']}

POSITIONS:
{positions_text}

Return this exact JSON structure:
{{
  "overall_signal": "BULLISH" | "BEARISH" | "NEUTRAL" | "MIXED",
  "overall_score": <integer 1-10, portfolio health score>,
  "executive_summary": "<2-3 sentence high-level assessment of portfolio health, performance, and positioning>",
  "strengths": ["<specific strength 1>", "<specific strength 2>", "<specific strength 3>"],
  "risks": ["<specific risk 1>", "<specific risk 2>", "<specific risk 3>"],
  "opportunities": ["<actionable opportunity 1>", "<actionable opportunity 2>", "<actionable opportunity 3>"],
  "diversification": {{
    "score": <integer 1-10>,
    "assessment": "<1-2 sentences on diversification quality>",
    "concentration_warnings": ["<any over-concentrated positions>"]
  }},
  "action_items": [
    {{"priority": "HIGH" | "MEDIUM" | "LOW", "action": "<specific actionable step>", "rationale": "<why>"}}
  ],
  "position_insights": [
    {{"symbol": "<ticker>", "assessment": "<HOLD|ADD|TRIM|EXIT>", "note": "<1 sentence specific insight>"}}
  ],
  "market_context": "<1-2 sentences on how current macro/market conditions affect this portfolio>"
}}"""

        resp = _chat_completion(
            client, model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2000,
            temperature=0.2,
        )
        _record_ai_call()
        result = json.loads(resp.choices[0].message.content)
        result["ai_powered"] = True
        result["model_used"] = getattr(resp, "model", None) or model
        return result
    except Exception as e:
        fallback = _rule_based_portfolio_analysis(holdings, summary)
        fallback["error_detail"] = str(e)
        return fallback


def _rule_based_portfolio_analysis(holdings, summary):
    """Basic rule-based portfolio analysis when no OpenAI key is configured."""
    if not holdings:
        return {"error": "No holdings to analyze", "ai_powered": False}

    total_value = summary.get("total_value", 0)
    total_pnl = summary.get("total_pnl", 0)
    total_pnl_pct = summary.get("total_pnl_pct", 0)

    # Concentration check
    concentration_warnings = []
    for p in holdings:
        if p.get("weight_pct", 0) > 25:
            concentration_warnings.append(f"{p['symbol']} is {p['weight_pct']:.1f}% of portfolio (high concentration)")

    # Asset type diversity
    types = set(p["asset_type"] for p in holdings)

    # Winners and losers
    gainers = sorted([p for p in holdings if (p.get("unrealized_pct") or 0) > 0],
                     key=lambda x: x.get("unrealized_pct", 0), reverse=True)
    losers  = sorted([p for p in holdings if (p.get("unrealized_pct") or 0) < 0],
                     key=lambda x: x.get("unrealized_pct", 0))

    if total_pnl_pct > 5:
        signal = "BULLISH"
    elif total_pnl_pct < -5:
        signal = "BEARISH"
    else:
        signal = "NEUTRAL"

    strengths = []
    if gainers:
        strengths.append(f"Top performer: {gainers[0]['symbol']} up {gainers[0]['unrealized_pct']:+.1f}%")
    if len(types) > 2:
        strengths.append(f"Multi-asset allocation across {len(types)} asset types")
    if total_pnl > 0:
        strengths.append(f"Portfolio is profitable at +${total_pnl:,.0f} total unrealized gain")

    risks = []
    risks.extend(concentration_warnings[:2])
    if losers:
        risks.append(f"Largest loser: {losers[0]['symbol']} down {losers[0]['unrealized_pct']:.1f}%")
    if len(holdings) < 5:
        risks.append("Under-diversified: fewer than 5 positions")

    position_insights = []
    for p in holdings:
        pct = p.get("unrealized_pct", 0) or 0
        if pct > 20:
            assessment, note = "TRIM", f"Up {pct:.1f}% — consider taking partial profits"
        elif pct < -15:
            assessment, note = "REVIEW", f"Down {pct:.1f}% — evaluate stop-loss or thesis"
        else:
            assessment, note = "HOLD", f"Position within normal range at {pct:+.1f}%"
        position_insights.append({"symbol": p["symbol"], "assessment": assessment, "note": note})

    return {
        "overall_signal": signal,
        "overall_score": min(10, max(1, 5 + int(total_pnl_pct / 5))),
        "executive_summary": (
            f"Portfolio of {len(holdings)} positions valued at ${total_value:,.0f} "
            f"with {total_pnl_pct:+.1f}% total return. "
            f"Configure an OpenAI API key in Settings for deep AI-powered analysis."
        ),
        "strengths": strengths or ["Add an OpenAI API key for detailed strength analysis"],
        "risks": risks or ["Add an OpenAI API key for detailed risk analysis"],
        "opportunities": ["Configure OpenAI API key in Settings for AI-powered opportunity identification"],
        "diversification": {
            "score": min(10, len(holdings)),
            "assessment": f"{len(holdings)} positions across {len(types)} asset type(s).",
            "concentration_warnings": concentration_warnings,
        },
        "action_items": [{"priority": "HIGH", "action": "Add OpenAI API key in Settings",
                           "rationale": "Unlock AI-powered portfolio analysis with GPT-4o"}],
        "position_insights": position_insights,
        "market_context": "Add an OpenAI API key in Settings to get macro context analysis.",
        "ai_powered": False,
        "model_used": None,
    }


def generate_earnings_brief(dossier, model=None):
    """
    Generate a pre-earnings analyst brief from a full dossier dict.
    Uses MODEL_HEAVY for depth (legacy "gpt-4o" requests are mapped to
    MODEL_HEAVY too). Falls back to rule-based if no key.
    """
    model = _resolve_heavy_model(model)
    key = get_openai_key()
    if not key:
        return _rule_based_earnings_brief(dossier)

    symbol = dossier.get("symbol", "")
    name = dossier.get("name", symbol)

    # Cache on (symbol, earnings_date, model) — dossier price/IV evolve
    # intraday but a 6h TTL gives a useful budget shield while still picking
    # up meaningful refreshes the same day.
    cache_key = (
        "ai_earnings_brief",
        symbol,
        str(dossier.get("earnings_date", "")),
        model,
    )

    def _do_call():
        if _cap_exceeded():
            return _cap_error_envelope("generate_earnings_brief")
        return _earnings_brief_uncached(dossier, model, key)

    if cache_store is not None:
        return cache_store.coalesce(cache_key, _EARNINGS_BRIEF_TTL, _do_call)
    return _do_call()


def _earnings_brief_uncached(dossier, model, key):
    symbol = dossier.get("symbol", "")
    name = dossier.get("name", symbol)

    # Build context string for the prompt
    history_lines = "\n".join([
        f"  {r['date']}: estimate ${r['estimate']}, actual ${r['actual']}, surprise {(r.get('surprise_pct') or 0):+.1f}%"
        for r in dossier.get("history", [])[:8]
        if r.get("estimate") and r.get("actual")
    ]) or "  No history available"

    moves_lines = "\n".join([
        f"  {m['date']}: {m['move_pct']:+.2f}%"
        for m in dossier.get("post_earnings_moves", [])[:6]
    ]) or "  No move history available"

    insider = dossier.get("insider_activity", {})
    insider_line = (
        f"{insider.get('buys',0)} buys (${insider.get('buy_value',0):,.0f}) vs "
        f"{insider.get('sells',0)} sells (${insider.get('sell_value',0):,.0f}) in last 60 days — "
        f"signal: {insider.get('signal','NEUTRAL')}"
    )

    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=60.0)

        system_prompt = """You are a senior equity analyst at a top-tier hedge fund specializing in earnings events.
Your pre-earnings briefs are known for being specific, data-driven, and actionable.
You help investors decide how to position BEFORE earnings: stay long, trim, hedge, or avoid.
Return ONLY valid JSON."""

        # Pre-format revenue estimate — f-string conditional inside a format
        # spec is not valid Python. The previous expression
        # `${dossier.get('revenue_estimate', 'N/A'):,.0f if ...}` raised
        # `TypeError: unsupported format string passed to NoneType.__format__`
        # whenever revenue_estimate was missing, killing the entire brief.
        _rev = dossier.get('revenue_estimate')
        _rev_str = f"${_rev:,.0f}" if isinstance(_rev, (int, float)) else "N/A"

        user_prompt = f"""Generate a pre-earnings brief for {symbol} ({name}).

EARNINGS DATE: {dossier.get('earnings_date', 'Unknown')} ({dossier.get('days_until', '?')} days away)
EPS CONSENSUS: ${dossier.get('eps_estimate', 'N/A')} (range: ${dossier.get('eps_low')} - ${dossier.get('eps_high')})
REVENUE ESTIMATE: {_rev_str}
CURRENT PRICE: ${dossier.get('current_price', 'N/A')}

BEAT/MISS HISTORY ({dossier.get('beat_rate', '?')}% beat rate over last quarters):
{history_lines}

POST-EARNINGS PRICE MOVES (last 6 quarters):
{moves_lines}
Average absolute move: {dossier.get('avg_abs_move_pct', '?')}%

OPTIONS IMPLIED MOVE: {dossier.get('implied_move_pct', 'N/A')}% (vs historical avg {dossier.get('avg_abs_move_pct', '?')}%)
IV vs Historical: {'+' if (dossier.get('iv_vs_historical') or 0) > 0 else ''}{dossier.get('iv_vs_historical', 'N/A')}% ({'options overpricing' if (dossier.get('iv_vs_historical') or 0) > 0 else 'options underpricing'} the expected move)

INSIDER ACTIVITY (last 60 days): {insider_line}

Return this exact JSON:
{{
  "setup_signal": "BULLISH" | "BEARISH" | "NEUTRAL" | "AVOID",
  "conviction": "HIGH" | "MEDIUM" | "LOW",
  "headline": "<10-word max headline summarizing the setup>",
  "brief": "<3-4 sentence analyst note covering: earnings track record, what could drive a beat or miss, IV analysis implication, key risk>",
  "key_metrics_to_watch": ["<metric 1>", "<metric 2>", "<metric 3>"],
  "positioning_advice": "<1-2 sentence specific recommendation: what to do with position size heading into earnings>",
  "bear_case": "<1 sentence — what could go wrong>",
  "bull_case": "<1 sentence — what drives upside surprise>",
  "options_take": "<1 sentence on whether options are cheap/expensive relative to historical moves and what that implies>"
}}"""

        resp = _chat_completion(
            client, model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=800,
            temperature=0.2,
        )
        _record_ai_call()
        result = json.loads(resp.choices[0].message.content)
        result["ai_powered"] = True
        result["model_used"] = getattr(resp, "model", None) or model
        return result
    except Exception as e:
        fallback = _rule_based_earnings_brief(dossier)
        fallback["error_detail"] = str(e)
        return fallback


def _rule_based_earnings_brief(dossier):
    """Rule-based earnings brief when no OpenAI key is configured."""
    beat_rate = dossier.get("beat_rate")
    avg_surprise = dossier.get("avg_surprise_pct")
    implied = dossier.get("implied_move_pct")
    historical = dossier.get("avg_abs_move_pct")
    iv_vs_hist = dossier.get("iv_vs_historical")
    insider = dossier.get("insider_activity", {})

    # Signal based on beat rate + insider
    insider_signal = insider.get("signal", "NEUTRAL")
    if beat_rate and beat_rate >= 75 and insider_signal != "BEARISH":
        signal = "BULLISH"
    elif beat_rate and beat_rate <= 40:
        signal = "BEARISH"
    elif insider_signal == "BULLISH":
        signal = "BULLISH"
    else:
        signal = "NEUTRAL"

    options_take = "Options data unavailable."
    if implied and historical:
        if iv_vs_hist and iv_vs_hist > 2:
            options_take = f"Options are pricing a {implied:.1f}% move vs {historical:.1f}% historical avg — overpriced by {iv_vs_hist:.1f}pp. Selling premium may be advantageous."
        elif iv_vs_hist and iv_vs_hist < -2:
            options_take = f"Options imply only {implied:.1f}% vs {historical:.1f}% historical avg — underpriced. Buying a straddle is relatively cheap."
        else:
            options_take = f"Options implied move ({implied:.1f}%) is in line with the historical average ({historical:.1f}%)."

    return {
        "setup_signal": signal,
        "conviction": "LOW",
        "headline": f"{beat_rate or '?'}% historical beat rate — {signal.lower()} setup",
        "brief": (
            f"{dossier.get('symbol')} reports in {dossier.get('days_until','?')} days. "
            f"Historical beat rate: {beat_rate or '?'}% with avg surprise of {avg_surprise or '?'}%. "
            f"Configure an OpenAI API key in Settings for a full analyst brief."
        ),
        "key_metrics_to_watch": ["EPS vs consensus", "Revenue guidance", "Margin trends"],
        "positioning_advice": "Configure OpenAI API key in Settings for AI-powered positioning advice.",
        "bear_case": "Miss on guidance or margin compression.",
        "bull_case": "Beat on EPS and raised forward guidance.",
        "options_take": options_take,
        "ai_powered": False,
        "model_used": None,
    }


def generate_idea_thesis(idea, model=None):
    """
    Generate an investment thesis for a randomly-picked idea.
    `idea` is the lightweight context dict assembled by idea_generator.
    Uses MODEL_LIGHT by default (legacy "gpt-4o-mini" requests are mapped to
    MODEL_LIGHT too). Falls back to rule-based thesis if no OpenAI key.
    """
    model = _resolve_light_model(model)
    key = get_openai_key()
    if not key:
        return _rule_based_idea_thesis(idea)

    symbol = idea.get("symbol", "")
    name = idea.get("name", symbol)

    # Cache on the inputs that materially change the prompt — same idea
    # surfaced twice in a 12h window should reuse the thesis.
    cache_key = (
        "ai_idea_thesis",
        symbol,
        str(idea.get("signal", "")),
        round(float(idea.get("composite") or 0), 1),
        str(idea.get("strategy", "")),
        model,
    )

    if _cap_exceeded():
        # Don't fall through into the inner builder when we've already spent
        # the day's budget — return the cap envelope so the UI can surface it.
        if cache_store is not None:
            cached = cache_store.cache_get(cache_key)
            if cached is not None:
                return cached
        return _cap_error_envelope("generate_idea_thesis")

    def _do_call():
        if _cap_exceeded():
            return _cap_error_envelope("generate_idea_thesis")
        return _generate_idea_thesis_uncached(idea, model, key)

    if cache_store is not None:
        return cache_store.coalesce(cache_key, _IDEA_THESIS_TTL, _do_call)
    return _do_call()


def _generate_idea_thesis_uncached(idea, model, key):
    symbol = idea.get("symbol", "")
    name = idea.get("name", symbol)

    sub_scores = idea.get("sub_scores") or {}
    sub_score_lines = "\n".join(
        f"  - {k.replace('_', ' ').title()}: {v}/100"
        for k, v in sub_scores.items()
    ) or "  (no sub-scores)"

    early = idea.get("early_signals") or []
    early_lines = "\n".join(f"  - {s}" for s in early[:5]) or "  (none)"

    headlines = idea.get("headlines") or []
    headline_lines = "\n".join(f"  - {h}" for h in headlines[:3]) or "  (no recent news)"

    metrics = idea.get("key_metrics") or {}
    metrics_str = ", ".join(
        f"{k}={v}" for k, v in metrics.items() if v is not None
    ) or "n/a"

    forecast_pct = idea.get("forecast_pct")
    forecast_str = f"{forecast_pct:+.2f}% (30d)" if isinstance(forecast_pct, (int, float)) else "n/a"

    # ── New factor blocks (may be partial / NA for crypto or sparse coverage) ──
    soc = idea.get("social") or {}
    social_line = (
        f"StockTwits: {soc.get('stocktwits_bull') or 0} bull / {soc.get('stocktwits_bear') or 0} bear "
        f"({(soc.get('bull_ratio') * 100):.0f}% bull)" if soc.get("bull_ratio") is not None
        else "StockTwits: no data"
    )
    social_line += f" · Reddit mentions: {soc.get('reddit_mentions') or 0}"
    social_line += f" · Sentiment: {soc.get('sentiment_label')} (composite {soc.get('social_score')})"

    ins = idea.get("insider") or {}
    insider_line = (
        f"{ins.get('buys', 0)} buys (${(ins.get('buy_value') or 0):,.0f}) vs "
        f"{ins.get('sells', 0)} sells (${(ins.get('sell_value') or 0):,.0f}) over 60d → {ins.get('signal', 'n/a')}"
        if ins.get("available") else "No recent Form 4 activity"
    )

    cong = idea.get("congress") or {}
    congress_line = (
        f"{cong.get('total_trades', 0)} trades by {cong.get('members_count', 0)} members "
        f"({cong.get('buys', 0)} buys / {cong.get('sells', 0)} sells) → {cong.get('signal', 'n/a')}"
        if cong.get("available") else "No recent congressional trades"
    )

    opt = idea.get("options_flow") or {}
    options_line = (
        f"{opt.get('unusual_count', 0)} unusual contracts · {opt.get('call_pct', 0)}% call volume → {opt.get('bias', 'n/a')}"
        if opt.get("available") else "No unusual options flow"
    )

    ev = idea.get("events") or {}
    events_parts = []
    if ev.get("next_earnings_date"):
        days = ev.get("days_to_earnings")
        events_parts.append(f"Earnings {ev.get('next_earnings_date')}{f' ({days}d away)' if days is not None else ''}")
    if ev.get("next_ex_dividend_date"):
        events_parts.append(f"Ex-div {ev.get('next_ex_dividend_date')} (yield {ev.get('dividend_yield_pct') or 0:.2f}%)")
    events_line = " · ".join(events_parts) if events_parts else "No upcoming events"

    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=45.0)

        system_prompt = """You are a senior portfolio manager generating a concise, actionable
investment thesis for a *randomly surfaced* idea. Be specific. Reference the data
including social sentiment, insider activity, congressional trades, and options flow
when they meaningfully support or contradict the thesis. Avoid hedge-everything
boilerplate. Return ONLY valid JSON."""

        user_prompt = f"""Generate an investment thesis for {symbol} ({name}).

ASSET CLASS: {idea.get('asset_class')}
SECTOR: {idea.get('sector') or 'n/a'}
PRICE: {idea.get('price')}
SCANNER SIGNAL: {idea.get('signal')} (composite {idea.get('composite')}/100, lens: {idea.get('strategy')})

SUB-SCORES:
{sub_score_lines}

EARLY SIGNALS DETECTED:
{early_lines}

ML FORECAST:
  - Composite ML signal: {idea.get('ml_signal')}
  - 30-day price target change: {forecast_str}
  - Current regime: {idea.get('regime')}

NARRATIVE ENGINE:
  - Dominant narrative: {idea.get('dominant_narrative')}
  - Phase: {idea.get('narrative_phase')} ({idea.get('velocity_label')})
  - Contrarian signal: {idea.get('contrarian_signal')}

SOCIAL SENTIMENT: {social_line}
INSIDER ACTIVITY (60D): {insider_line}
CONGRESSIONAL TRADES (180D): {congress_line}
UNUSUAL OPTIONS FLOW: {options_line}
UPCOMING EVENTS: {events_line}

KEY METRICS: {metrics_str}

RECENT HEADLINES:
{headline_lines}

Return this exact JSON:
{{
  "headline": "<8-12 word punchy headline summarizing the idea>",
  "thesis": "<3-4 sentence specific thesis citing the data above>",
  "bull_case": "<1 sentence describing the upside trigger>",
  "bear_case": "<1 sentence describing the primary downside risk>",
  "conviction": "HIGH" | "MEDIUM" | "LOW",
  "time_horizon": "<concise — e.g. '2-4 weeks', '1-3 months', '6-12 months'>",
  "key_catalyst": "<the single most important upcoming catalyst or trigger to watch>",
  "suggested_action": "<one of: STARTER POSITION | FULL POSITION | WATCH | AVOID> — and a 1-line why"
}}"""

        resp = _chat_completion(
            client, model,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=600,
            temperature=0.4,
        )
        _record_ai_call()
        result = json.loads(resp.choices[0].message.content)
        result["ai_powered"] = True
        result["model_used"] = getattr(resp, "model", None) or model
        return result
    except Exception as e:
        fallback = _rule_based_idea_thesis(idea)
        fallback["error_detail"] = str(e)
        return fallback


def _rule_based_idea_thesis(idea):
    """Rule-based fallback thesis when no OpenAI key is configured."""
    symbol = idea.get("symbol", "")
    name = idea.get("name", symbol)
    signal = idea.get("signal") or "HOLD"
    composite = idea.get("composite") or 0
    strategy = (idea.get("strategy") or "growth").upper()
    ml_signal = idea.get("ml_signal") or "NEUTRAL"
    forecast_pct = idea.get("forecast_pct")
    regime = idea.get("regime") or "n/a"
    narrative_phase = idea.get("narrative_phase") or "n/a"
    velocity = idea.get("velocity_label") or "n/a"
    contrarian = idea.get("contrarian_signal")
    early = idea.get("early_signals") or []

    conviction_map = {
        "STRONG BUY": "HIGH",
        "BUY": "MEDIUM",
        "HOLD": "MEDIUM",
        "CAUTION": "LOW",
        "AVOID": "LOW",
    }
    conviction = conviction_map.get(signal, "MEDIUM")

    action_map = {
        "STRONG BUY": "FULL POSITION",
        "BUY": "STARTER POSITION",
        "HOLD": "WATCH",
        "CAUTION": "WATCH",
        "AVOID": "AVOID",
    }
    action = action_map.get(signal, "WATCH")

    forecast_str = (
        f"{forecast_pct:+.1f}% over 30 days"
        if isinstance(forecast_pct, (int, float))
        else "no forecast available"
    )

    headline = f"{symbol}: {signal.title()} setup ({strategy.lower()} lens, score {composite:.0f})"

    early_phrase = (
        f" Early signals: {', '.join(early[:2])}." if early else ""
    )

    # Pull factor signals into the rule-based thesis when available
    soc = idea.get("social") or {}
    ins = idea.get("insider") or {}
    cong = idea.get("congress") or {}
    opt = idea.get("options_flow") or {}

    factor_phrases = []
    if soc.get("sentiment_label") and soc.get("sentiment_label") != "NO DATA":
        factor_phrases.append(f"social sentiment {soc['sentiment_label'].lower()}")
    if ins.get("available") and ins.get("signal") not in (None, "NO ACTIVITY", "MIXED"):
        factor_phrases.append(f"insider activity {ins['signal'].lower()}")
    if cong.get("available") and cong.get("signal") not in (None, "NO ACTIVITY", "MIXED"):
        factor_phrases.append(f"congress {cong['signal'].lower()}")
    if opt.get("available") and opt.get("bias") not in (None, "NO DATA", "MIXED"):
        factor_phrases.append(f"options flow {opt['bias'].lower()}")
    factor_phrase = (" Cross-checks: " + ", ".join(factor_phrases) + ".") if factor_phrases else ""

    thesis = (
        f"{name} ({symbol}) scores {composite:.0f}/100 on the {strategy.lower()} lens with a {signal} signal. "
        f"ML model projects {forecast_str} ({ml_signal}) in a {regime} regime. "
        f"Narrative engine reads as {narrative_phase}/{velocity}"
        f"{' with contrarian flag active' if contrarian else ''}.{early_phrase}{factor_phrase}"
    )

    bull = (
        f"Continuation of {regime.lower()} regime + {narrative_phase.lower()} narrative could drive a {ml_signal.lower()} re-rating."
        if ml_signal != "NEUTRAL"
        else f"A regime shift or catalyst surprise could unlock material upside given the {composite:.0f} composite score."
    )
    bear = (
        f"Score deterioration, narrative reversal, or a regime flip away from {regime.lower()} would invalidate the setup."
    )

    horizon_map = {
        "GROWTH": "1-3 months",
        "MOMENTUM": "2-6 weeks",
        "VALUE": "6-12 months",
        "INCOME": "6-18 months",
    }

    return {
        "headline": headline,
        "thesis": thesis,
        "bull_case": bull,
        "bear_case": bear,
        "conviction": conviction,
        "time_horizon": horizon_map.get(strategy, "1-3 months"),
        "key_catalyst": "Earnings, sector rotation, or narrative shift",
        "suggested_action": f"{action} — based on composite score {composite:.0f} and {signal} scanner signal",
        "ai_powered": False,
        "model_used": None,
    }


def analyze_insider_pattern(transactions, ticker):
    """Analyze insider transaction pattern for a ticker."""
    if not transactions:
        return {"signal": "NEUTRAL", "summary": "No recent insider transactions.", "ai_powered": False}

    key = get_openai_key()
    buys = [t for t in transactions if t.get("transaction_type") == "BUY"]
    sells = [t for t in transactions if t.get("transaction_type") == "SELL"]
    total_buy_value = sum(t.get("value", 0) or 0 for t in buys)
    total_sell_value = sum(t.get("value", 0) or 0 for t in sells)

    if not key:
        # Rule-based
        if total_buy_value > total_sell_value * 2 and len(buys) >= 2:
            signal = "BULLISH"
            summary = f"{len(buys)} insider(s) bought ${total_buy_value:,.0f} total. Strong insider conviction."
        elif total_sell_value > total_buy_value * 3:
            signal = "BEARISH"
            summary = f"Insiders sold ${total_sell_value:,.0f} vs ${total_buy_value:,.0f} in buys. Net selling pressure."
        else:
            signal = "NEUTRAL"
            summary = f"{len(buys)} buy(s), {len(sells)} sell(s) in recent transactions."
        return {
            "signal": signal,
            "summary": summary,
            "buy_value": total_buy_value,
            "sell_value": total_sell_value,
            "ai_powered": False,
        }

    txn_summary = json.dumps(transactions[:10], default=str)
    # Cache key reflects the actual prompt input — same txn list shouldn't
    # be re-analysed twice in a 6h window.
    import hashlib
    sig = hashlib.sha256(txn_summary.encode("utf-8", "ignore")).hexdigest()[:16]
    cache_key = ("ai_insider_pattern", ticker, sig)

    def _do_call():
        if _cap_exceeded():
            env = _cap_error_envelope("analyze_insider_pattern")
            env.update({"signal": "NEUTRAL", "summary": env["error"],
                        "buy_value": total_buy_value, "sell_value": total_sell_value})
            return env
        try:
            from openai import OpenAI
            client = OpenAI(api_key=key, timeout=30.0)
            resp = _chat_completion(
                client, MODEL_LIGHT,
                [{
                    "role": "user",
                    "content": f"""Analyze insider transactions for {ticker}. Return JSON only:
{{"signal": "BULLISH/BEARISH/NEUTRAL", "summary": "2 sentence analysis", "key_insight": "most important observation"}}

Transactions: {txn_summary}""",
                }],
                max_tokens=200,
                temperature=0.1,
            )
            _record_ai_call()
            result = json.loads(resp.choices[0].message.content)
            result["ai_powered"] = True
            result["buy_value"] = total_buy_value
            result["sell_value"] = total_sell_value
            return result
        except Exception:
            return {"signal": "NEUTRAL", "summary": "Analysis unavailable.", "ai_powered": False}

    if cache_store is not None:
        return cache_store.coalesce(cache_key, _INSIDER_PATTERN_TTL, _do_call)
    return _do_call()
