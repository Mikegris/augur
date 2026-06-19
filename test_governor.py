"""Offline tests for the shared OpenAI request governor in ai_summarizer.

No network. We drive ai_summarizer.governed_call (and _chat_completion through a
fake client) and assert:
  (a) under a burst of N threads, observed concurrency never exceeds the cap;
  (b) a 429/RateLimitError once → a backoff retry happens, then success;
  (c) a normal call passes straight through.

Run: ./venv/bin/python test_governor.py
"""
import os
import sys
import time
import threading

# Force a small, deterministic governor config BEFORE importing the module
# (tunables are read once at import time).
os.environ["AUGUR_OPENAI_MAX_CONCURRENCY"] = "3"
os.environ["AUGUR_OPENAI_RPM"] = "100000"          # effectively disable pacing for speed
os.environ["AUGUR_OPENAI_BACKOFF_BASE"] = "0.01"   # tiny backoff so the test is fast
os.environ["AUGUR_OPENAI_BACKOFF_CAP"] = "0.05"
os.environ["AUGUR_OPENAI_MAX_RETRIES"] = "4"
os.environ["AUGUR_OPENAI_ACQUIRE_DEADLINE"] = "10"

import ai_summarizer as ai

_failures = []


def check(cond, msg):
    if cond:
        print("PASS:", msg)
    else:
        print("FAIL:", msg)
        _failures.append(msg)


# ── Fakes ────────────────────────────────────────────────────────────────────
class FakeRateLimitError(Exception):
    """Class name matches what the governor sniffs for (_is_rate_limit)."""
    status_code = 429

    def __init__(self, msg="rate limited", retry_after=None):
        super().__init__(msg)
        if retry_after is not None:
            self.response = type("R", (), {"headers": {"retry-after": str(retry_after)}})()


# Rename so __class__.__name__ == "RateLimitError" (what the governor checks).
FakeRateLimitError.__name__ = "RateLimitError"
FakeRateLimitError.__qualname__ = "RateLimitError"


class ConcurrencyTracker:
    """A fake .create that records peak concurrency while it 'runs'."""
    def __init__(self, hold=0.05):
        self.hold = hold
        self.lock = threading.Lock()
        self.cur = 0
        self.peak = 0
        self.calls = 0

    def create(self, **kwargs):
        with self.lock:
            self.cur += 1
            self.calls += 1
            self.peak = max(self.peak, self.cur)
        try:
            time.sleep(self.hold)
            return {"ok": True}
        finally:
            with self.lock:
                self.cur -= 1


# ── (a) Concurrency cap under a burst ────────────────────────────────────────
def test_concurrency_cap():
    tracker = ConcurrencyTracker(hold=0.05)
    N = 24
    threads = [threading.Thread(target=lambda: ai.governed_call(tracker.create))
               for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    check(not any(t.is_alive() for t in threads),
          "concurrency burst: all worker threads finished (no wedge)")
    check(tracker.calls == N,
          "concurrency burst: every call executed (got %d/%d)" % (tracker.calls, N))
    check(tracker.peak <= ai._GOV_MAX_CONCURRENCY,
          "concurrency burst: peak %d <= cap %d" % (tracker.peak, ai._GOV_MAX_CONCURRENCY))
    check(tracker.peak >= 2,
          "concurrency burst: peak %d shows real parallelism (>=2)" % tracker.peak)


# ── (b) 429 once → backoff retry → success ───────────────────────────────────
def test_backoff_retry():
    state = {"n": 0, "sleeps": []}
    real_sleep = time.sleep

    def fake_sleep(s):
        state["sleeps"].append(s)
        real_sleep(0)  # don't actually wait

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise FakeRateLimitError("slow down", retry_after=0.02)
        return {"ok": True, "attempts": state["n"]}

    ai.time.sleep = fake_sleep
    try:
        result = ai.governed_call(flaky)
    finally:
        ai.time.sleep = real_sleep

    check(result == {"ok": True, "attempts": 2},
          "backoff: succeeded on the 2nd attempt after one 429")
    check(state["n"] == 2, "backoff: function was retried exactly once (calls=%d)" % state["n"])
    # The pacer may also sleep a tiny amount; the backoff sleep is the
    # meaningful one (>= the retry_after we supplied, 0.02s).
    backoff_sleeps = [s for s in state["sleeps"] if s >= 0.02]
    check(len(backoff_sleeps) == 1,
          "backoff: exactly one cooperative-backoff sleep happened (backoffs=%s, all=%s)"
          % (backoff_sleeps, state["sleeps"]))


def test_backoff_gives_up():
    """Persistent 429 → retries exhaust → the error propagates (no infinite loop)."""
    state = {"n": 0}
    real_sleep = time.sleep
    ai.time.sleep = lambda s: real_sleep(0)
    try:
        def always_429():
            state["n"] += 1
            raise FakeRateLimitError("always")
        raised = False
        try:
            ai.governed_call(always_429)
        except Exception as e:
            raised = e.__class__.__name__ == "RateLimitError"
    finally:
        ai.time.sleep = real_sleep
    check(raised, "backoff: persistent 429 eventually propagates (no infinite loop)")
    check(state["n"] == ai._GOV_MAX_RETRIES + 1,
          "backoff: tried initial + MAX_RETRIES (calls=%d, expected %d)"
          % (state["n"], ai._GOV_MAX_RETRIES + 1))


# ── (c) Normal call passes through ───────────────────────────────────────────
def test_passthrough():
    sentinel = {"value": 42}
    result = ai.governed_call(lambda: sentinel)
    check(result is sentinel, "passthrough: normal call returns the result verbatim")


# ── (d) _chat_completion uses the governor (fake client) ─────────────────────
def test_chat_completion_through_governor():
    class FakeResp:
        pass

    class FakeClient:
        def __init__(self):
            self.created = 0
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            self.created += 1
            return FakeResp()

    client = FakeClient()
    out = ai._chat_completion(client, "gpt-5.5", [{"role": "user", "content": "hi"}],
                              max_tokens=10, json_mode=False)
    check(isinstance(out, FakeResp) and client.created == 1,
          "_chat_completion: routes the real create() through the governor once")


# ── (e) Fail-open: governor pacer error doesn't drop the call ────────────────
def test_fail_open_on_pacer_error():
    orig = ai._gov_pace
    ai._gov_pace = lambda dl: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        result = ai.governed_call(lambda: "still-ran")
    finally:
        ai._gov_pace = orig
    check(result == "still-ran", "fail-open: pacer error is swallowed, call still runs")


if __name__ == "__main__":
    test_concurrency_cap()
    test_backoff_retry()
    test_backoff_gives_up()
    test_passthrough()
    test_chat_completion_through_governor()
    test_fail_open_on_pacer_error()

    print()
    if _failures:
        print("RESULT: %d FAILED" % len(_failures))
        for f in _failures:
            print("   -", f)
        sys.exit(1)
    print("RESULT: ALL PASSED")
    sys.exit(0)
