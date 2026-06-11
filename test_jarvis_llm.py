"""
Tests for the LLM layer: ai_summarizer model constants/param mapping and the
optional jarvis polish (briefing `voice` + ask() llm fallback).

Designed to run OFFLINE and deterministically — no OpenAI key is required
(the env key is stripped), network seams are monkeypatched, and a throwaway
DB is used so the real wealth.db is untouched.

Run:  ./venv/bin/python test_jarvis_llm.py
Exit: 0 if all pass, 1 otherwise.
"""
import importlib
import os
import sys
import tempfile

# Isolate DB before importing app/tracker so we never touch the real wealth.db.
_TMPDB = tempfile.NamedTemporaryFile(prefix="augur_test_", suffix=".db", delete=False)
_TMPDB.close()
os.environ["AUGUR_DB_PATH"] = _TMPDB.name
os.environ["DISABLE_WARMER"] = "1"

# Force keyless: the rule-based paths must be exercised regardless of the
# developer's shell env.
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("AUGUR_OPENAI_MODEL_HEAVY", None)
os.environ.pop("AUGUR_OPENAI_MODEL_LIGHT", None)

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  ✓ " + name)
    else:
        _failed += 1
        print("  ✗ " + name + ("  — " + detail if detail else ""))


# ── Fakes for the OpenAI client seam ─────────────────────────────────────────

class _FakeResp:
    def __init__(self, content='{"ok": true}', model="fake-model"):
        message = type("Msg", (), {"content": content})()
        choice = type("Choice", (), {"message": message})()
        self.choices = [choice]
        self.model = model


class _FakeClient:
    """Captures kwargs of every chat.completions.create call; optionally
    raises a model-not-found-shaped error for selected model names."""

    def __init__(self, fail_models=()):
        self.calls = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if kwargs.get("model") in fail_models:
                    raise RuntimeError(
                        "The model `{}` does not exist or you do not have "
                        "access to it.".format(kwargs.get("model")))
                return _FakeResp(model=kwargs.get("model"))

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


# ── 1. Model constants + env overrides (ai_summarizer) ───────────────────────
def test_model_constants():
    import ai_summarizer
    check("MODEL_HEAVY defaults to gpt-5.5",
          ai_summarizer.MODEL_HEAVY == "gpt-5.5",
          "got {!r}".format(ai_summarizer.MODEL_HEAVY))
    check("MODEL_LIGHT defaults to gpt-5.4-mini",
          ai_summarizer.MODEL_LIGHT == "gpt-5.4-mini",
          "got {!r}".format(ai_summarizer.MODEL_LIGHT))
    check("MODEL_FALLBACK is gpt-4o",
          ai_summarizer.MODEL_FALLBACK == "gpt-4o",
          "got {!r}".format(ai_summarizer.MODEL_FALLBACK))
    # Legacy explicit strings (still sent by app.py/cli.py) map to the tiers.
    check("legacy 'gpt-4o' resolves to MODEL_HEAVY",
          ai_summarizer._resolve_heavy_model("gpt-4o") == ai_summarizer.MODEL_HEAVY)
    check("legacy 'gpt-4o-mini' resolves to MODEL_LIGHT",
          ai_summarizer._resolve_light_model("gpt-4o-mini") == ai_summarizer.MODEL_LIGHT)
    check("None resolves to MODEL_HEAVY",
          ai_summarizer._resolve_heavy_model(None) == ai_summarizer.MODEL_HEAVY)
    check("explicit non-legacy model is respected",
          ai_summarizer._resolve_heavy_model("gpt-5.4") == "gpt-5.4")


def test_env_override():
    import ai_summarizer
    os.environ["AUGUR_OPENAI_MODEL_HEAVY"] = "my-heavy-x"
    os.environ["AUGUR_OPENAI_MODEL_LIGHT"] = "my-light-y"
    try:
        importlib.reload(ai_summarizer)
        check("AUGUR_OPENAI_MODEL_HEAVY override honored",
              ai_summarizer.MODEL_HEAVY == "my-heavy-x",
              "got {!r}".format(ai_summarizer.MODEL_HEAVY))
        check("AUGUR_OPENAI_MODEL_LIGHT override honored",
              ai_summarizer.MODEL_LIGHT == "my-light-y",
              "got {!r}".format(ai_summarizer.MODEL_LIGHT))
    finally:
        os.environ.pop("AUGUR_OPENAI_MODEL_HEAVY", None)
        os.environ.pop("AUGUR_OPENAI_MODEL_LIGHT", None)
        importlib.reload(ai_summarizer)
    check("defaults restored after reload",
          ai_summarizer.MODEL_HEAVY == "gpt-5.5"
          and ai_summarizer.MODEL_LIGHT == "gpt-5.4-mini")


# ── 2. _chat_completion: param mapping per model family + fallback ──────────
def test_chat_completion_param_mapping():
    import ai_summarizer

    # gpt-5.x family: max_completion_tokens, no temperature/max_tokens.
    c = _FakeClient()
    ai_summarizer._chat_completion(
        c, "gpt-5.5", [{"role": "user", "content": "hi"}],
        max_tokens=123, temperature=0.2)
    kw = c.calls[0]
    check("gpt-5.5 uses max_completion_tokens",
          kw.get("max_completion_tokens") == 123, str(kw))
    check("gpt-5.5 omits temperature and max_tokens",
          "temperature" not in kw and "max_tokens" not in kw, str(kw))
    check("json mode on by default",
          kw.get("response_format") == {"type": "json_object"}, str(kw))

    # gpt-4o era: classic parameter names.
    c = _FakeClient()
    ai_summarizer._chat_completion(
        c, "gpt-4o", [{"role": "user", "content": "hi"}],
        max_tokens=77, temperature=0.3)
    kw = c.calls[0]
    check("gpt-4o keeps max_tokens + temperature",
          kw.get("max_tokens") == 77 and kw.get("temperature") == 0.3, str(kw))

    # json_mode=False drops response_format (used by jarvis plain-text polish).
    c = _FakeClient()
    ai_summarizer._chat_completion(
        c, "gpt-5.4-mini", [{"role": "user", "content": "hi"}], json_mode=False)
    check("json_mode=False omits response_format",
          "response_format" not in c.calls[0], str(c.calls[0]))


def test_model_not_found_fallback():
    import ai_summarizer

    c = _FakeClient(fail_models=("gpt-5.5",))
    resp = ai_summarizer._chat_completion(
        c, "gpt-5.5", [{"role": "user", "content": "hi"}], max_tokens=50)
    check("model-not-found retries once with gpt-4o",
          len(c.calls) == 2 and c.calls[1]["model"] == "gpt-4o",
          str([k.get("model") for k in c.calls]))
    check("fallback retry uses classic max_tokens",
          c.calls[1].get("max_tokens") == 50, str(c.calls[1]))
    check("fallback response returned", resp.model == "gpt-4o")

    # Non-model errors must NOT trigger the fallback retry.
    class _Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("rate limit exceeded")
    try:
        ai_summarizer._chat_completion(_Boom, "gpt-5.5", [])
        check("non-model errors propagate", False, "no exception raised")
    except RuntimeError as e:
        check("non-model errors propagate", "rate limit" in str(e))


# ── 3. Keyless behavior is unchanged (no key, no voice, ask → help) ─────────
def _patch_offline(jarvis_mod):
    """Kill the network seams used by the briefing/snapshot paths."""
    import database
    database.init_db()  # empty schema on the throwaway DB
    import fetcher
    fetcher.get_market_indices = lambda: []
    fetcher.get_quotes_batch = lambda syms: {}
    fetcher.get_quote = lambda sym: {}


def test_keyless_ask_is_help():
    import ai_summarizer
    import jarvis
    _patch_offline(jarvis)
    check("no OpenAI key in test env", ai_summarizer.get_openai_key() == "")
    check("_llm_available is False keyless", jarvis._llm_available() is False)
    r = jarvis.ask("florble the wumpus quibbles")
    check("keyless ask(gibberish) returns intent help",
          r.get("intent") == "help", str(r))
    check("help answer text present", "portfolio" in (r.get("answer") or ""))


def test_keyless_briefing_has_no_voice():
    import jarvis
    _patch_offline(jarvis)
    b = jarvis.get_briefing(force_refresh=True)
    check("briefing builds offline", isinstance(b, dict) and "headline" in b, str(type(b)))
    check("keyless briefing has no voice (or None)",
          ("voice" not in b) or (b.get("voice") is None), str(b.get("voice")))
    check("headline is a string", isinstance(b.get("headline"), str))


# ── 4. Fake-LLM path: voice appears, ask gibberish → intent llm ─────────────
def test_fake_llm_path():
    import jarvis
    _patch_offline(jarvis)

    orig_avail = jarvis._llm_available
    orig_complete = jarvis._llm_complete
    jarvis._llm_available = lambda: True
    jarvis._llm_complete = (
        lambda messages, max_tokens=200, temperature=0.4: "Quite the quiet tape, sir.")
    try:
        b = jarvis.get_briefing(force_refresh=True)
        check("briefing gains voice with LLM available",
              b.get("voice") == "Quite the quiet tape, sir.", str(b.get("voice")))
        check("headline untouched by polish",
              isinstance(b.get("headline"), str) and b["headline"] != b.get("voice"))

        r = jarvis.ask("florble the wumpus quibbles")
        check("ask(gibberish) returns intent llm when LLM available",
              r.get("intent") == "llm", str(r))
        check("llm answer surfaced",
              r.get("answer") == "Quite the quiet tape, sir.", str(r.get("answer")))
    finally:
        jarvis._llm_available = orig_avail
        jarvis._llm_complete = orig_complete

    # And a failing stub must fail open: briefing intact, ask falls to help.
    jarvis._llm_available = lambda: True

    def _boom(messages, max_tokens=200, temperature=0.4):
        raise RuntimeError("simulated outage")
    jarvis._llm_complete = _boom
    try:
        b = jarvis.get_briefing(force_refresh=True)
        check("LLM failure leaves briefing without voice",
              ("voice" not in b) or (b.get("voice") is None), str(b.get("voice")))
        r = jarvis.ask("florble the wumpus quibbles")
        check("LLM failure in ask falls back to help",
              r.get("intent") == "help", str(r))
    finally:
        jarvis._llm_available = orig_avail
        jarvis._llm_complete = orig_complete


def main():
    tests = [
        ("model constants", test_model_constants),
        ("env override", test_env_override),
        ("chat completion param mapping", test_chat_completion_param_mapping),
        ("model-not-found fallback", test_model_not_found_fallback),
        ("keyless ask → help", test_keyless_ask_is_help),
        ("keyless briefing → no voice", test_keyless_briefing_has_no_voice),
        ("fake-LLM path", test_fake_llm_path),
    ]
    for name, fn in tests:
        print("\n" + name)
        try:
            fn()
        except Exception as e:
            check(name + " (no exception)", False, "{}: {}".format(type(e).__name__, e))
    print("\n{} passed, {} failed".format(_passed, _failed))
    try:
        os.unlink(_TMPDB.name)
    except OSError:
        pass
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
