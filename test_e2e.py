"""
End-to-end route regression for AUGUR.

Boots the real Flask app in a subprocess against a COPY of wealth.db (so the
sweep can exercise read AND curated write paths without touching real data),
then:

  1. GET-sweeps every registered route (path params substituted), failing on
     5xx / timeout / connection error / unparseable JSON for /api/* routes.
  2. POST-sweeps a curated list of representative write/ask endpoints with
     valid and deliberately-invalid payloads, asserting expected statuses.

A 200 whose JSON body contains an "error" key is recorded as a WARN, not a
FAIL — keyless/upstream-down engines legitimately answer with error
envelopes; the e2e contract is "the server never breaks", not "every
upstream is up".

Run:  ./venv/bin/python test_e2e.py
Exit: 0 when zero FAILs, 1 otherwise.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import urllib.request
import urllib.error

PORT = int(os.environ.get("E2E_PORT", "5099"))
BASE = "http://127.0.0.1:{}".format(PORT)
TIMEOUT = 60  # generous: some engines do real work on a cold cache

# Heavy analytical sweeps legitimately take >60s on a stone-cold cache (they
# fan out per-symbol option chains / scan ~100 names). They're coalesce-cached
# in production, so a real user pays this once; the e2e contract is "the server
# returns a valid response", not "it's fast". Give these a wider ceiling.
HEAVY_ROUTE_TIMEOUT = 150
HEAVY_ROUTES = ("/api/synth/catalyst", "/api/synth/cluster-scan",
                "/api/synth/peerdiv", "/api/synth/divmap", "/api/cluster",
                "/api/synthetic-insider", "/api/montecarlo")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
results = []  # (status, route, note)


def record(status, route, note=""):
    results.append((status, route, note))
    mark = {"PASS": "  ✓", "WARN": "  ~", "FAIL": "  ✗"}[status]
    print("%s %-52s %s" % (mark, route, note))


def clone_db(src="wealth.db"):
    """Consistent snapshot via the sqlite backup API (WAL-safe)."""
    fd, dst = tempfile.mkstemp(prefix="augur_e2e_", suffix=".db")
    os.close(fd)
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    s.close()
    d.close()
    return dst


def boot(dbpath):
    env = dict(os.environ)
    env["AUGUR_DB_PATH"] = dbpath
    env["DISABLE_WARMER"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import app; app.app.run(host='127.0.0.1', port={}, debug=False)".format(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/api/version", timeout=3) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("server did not come up on :{}".format(PORT))


def req(method, path, payload=None, timeout=TIMEOUT):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# Path-parameter substitutions for the GET sweep.
SUBS = {"symbol": "AAPL", "sym": "AAPL", "ticker": "AAPL", "view": "portfolio",
        "provider": "openai", "coin_id": "bitcoin", "series": "CPIAUCSL",
        "fund": "berkshire", "cik": "0001067983", "name": "berkshire",
        "fund_name": "Berkshire%20Hathaway", "series_id": "CPIAUCSL",
        "alert_id": "1", "account_id": "1", "memory_id": "1", "id": "1",
        "conversation_id": "1", "horizon": "20"}

# Routes where a non-200 is the CORRECT answer for our substituted params,
# or that we deliberately skip (streaming/long-poll handled separately).
EXPECT = {
    "/api/jarvis/activity/stream": "skip",   # SSE long-poll — covered manually
    "/api/jarvis/ask/stream": "skip",        # POST-only stream — covered below
}
OK_STATUSES = {200}
# id-parameterized rows may legitimately be absent in the snapshot.
ID_OK = {200, 404}


def get_sweep():
    import importlib
    os.environ["DISABLE_WARMER"] = "1"
    app_mod = importlib.import_module("app")
    rules = []
    for rule in app_mod.app.url_map.iter_rules():
        if "GET" not in rule.methods:
            continue
        if rule.rule.startswith("/static") or rule.rule == "/":
            continue
        rules.append(rule)

    # The SPA shell itself.
    status, body = req("GET", "/")
    record(PASS if status == 200 and b"<html" in body[:200].lower() else FAIL,
           "GET /", "status={}".format(status))

    for rule in sorted(rules, key=lambda r: r.rule):
        path = rule.rule
        if EXPECT.get(path) == "skip":
            record(PASS, "GET " + path, "(skipped: streaming)")
            continue
        has_id_param = False
        try:
            for arg in rule.arguments:
                sub = SUBS.get(arg)
                if sub is None:
                    sub = "1"
                if arg.endswith("_id") or arg == "id":
                    has_id_param = True
                path = path.replace("<{}>".format(arg), sub) \
                           .replace("<int:{}>".format(arg), sub) \
                           .replace("<path:{}>".format(arg), sub) \
                           .replace("<string:{}>".format(arg), sub)
        except Exception as e:
            record(WARN, "GET " + rule.rule, "param substitution failed: {}".format(e))
            continue
        t0 = time.time()
        rt = HEAVY_ROUTE_TIMEOUT if any(path.startswith(h) for h in HEAVY_ROUTES) else TIMEOUT
        try:
            status, body = req("GET", path, timeout=rt)
        except Exception as e:
            record(FAIL, "GET " + path, "transport: {}".format(e))
            continue
        ms = int((time.time() - t0) * 1000)
        ok = ID_OK if has_id_param else OK_STATUSES
        if status >= 500:
            record(FAIL, "GET " + path, "HTTP {} ({}ms): {}".format(
                status, ms, body[:120]))
            continue
        if status not in ok and status != 400:
            # 400 without required query params is acceptable API behavior.
            record(FAIL, "GET " + path, "HTTP {} ({}ms)".format(status, ms))
            continue
        note = "{} {}ms".format(status, ms)
        if path.startswith("/api") and status == 200:
            try:
                parsed = json.loads(body)
            except Exception:
                if b"text/" not in body[:0]:  # non-JSON 200 on /api
                    # export route returns markdown — allow by content sniff
                    if b"# JARVIS" in body[:40] or path.endswith("/export"):
                        record(PASS, "GET " + path, note + " (markdown)")
                        continue
                record(FAIL, "GET " + path, "200 but unparseable JSON")
                continue
            if isinstance(parsed, dict) and parsed.get("error"):
                record(WARN, "GET " + path, "error envelope: {}".format(
                    str(parsed.get("error"))[:80]))
                continue
        record(PASS, "GET " + path, note)


def post_sweep():
    cases = [
        # (path, payload, acceptable statuses, note)
        ("/api/jarvis/ask", {"query": "how are markets"}, {200}, "rule intent"),
        ("/api/jarvis/ask", {"query": ""}, {400}, "empty query rejected"),
        ("/api/jarvis/ask", {"query": "x" * 501}, {400}, "oversize rejected"),
        ("/api/jarvis/ask/stream", {"query": "is the market open"}, {200}, "stream"),
        ("/api/jarvis/act", {"tool": "nope", "args": {}}, {400}, "unknown tool rejected"),
        ("/api/jarvis/act", {"tool": "get_quote", "args": {}}, {400}, "read tool rejected"),
        ("/api/jarvis/conversation/new", {}, {200}, "new thread"),
        ("/api/watchlist/add", {"symbol": "E2ETEST", "name": "E2E"}, {200, 201},
         "watchlist add"),
        ("/api/alerts", {"symbol": "E2ETEST", "alert_type": "above", "price": 1.0},
         {200, 201}, "alert add"),
        ("/api/jarvis/tts", {"text": "test"}, {200, 404, 400, 503}, "tts (keyless ok)"),
    ]
    for path, payload, ok, note in cases:
        try:
            status, body = req("POST", path, payload)
        except Exception as e:
            record(FAIL, "POST " + path, "transport: {}".format(e))
            continue
        if path.endswith("/stream") and status == 200:
            good = b'"type": "answer"' in body or b'"type":"answer"' in body
            record(PASS if good else FAIL, "POST " + path,
                   note + (" (terminal frame ok)" if good else " missing answer frame"))
            continue
        record(PASS if status in ok else FAIL, "POST " + path,
               "{} → {} ({})".format(note, status, "" if status in ok else body[:100]))


def main():
    dbpath = clone_db()
    proc = None
    try:
        proc = boot(dbpath)
        print("── GET sweep")
        get_sweep()
        print("── POST sweep")
        post_sweep()
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        for suffix in ("", "-shm", "-wal"):
            try:
                os.unlink(dbpath + suffix)
            except OSError:
                pass

    fails = [r for r in results if r[0] == FAIL]
    warns = [r for r in results if r[0] == WARN]
    print("\n" + "=" * 60)
    print("  E2E route regression: {} passed, {} warned, {} FAILED".format(
        sum(1 for r in results if r[0] == PASS), len(warns), len(fails)))
    print("=" * 60)
    for _, route, note in fails:
        print("  FAIL {} — {}".format(route, note))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
