#!/usr/bin/env python3
"""AJTA — operator CLI (AJTA-SPEC-1.0 §19 triggers, §11.4 kill switch).

Usage:
    python aj_cli.py run [--mode paper|live]   # one triggered operator cycle
    python aj_cli.py status                     # JSON status snapshot
    python aj_cli.py kill [reason]              # KILL SWITCH (model out of loop)
    python aj_cli.py rearm                       # re-enable trading after a halt
    python aj_cli.py recon                       # reconcile against broker truth
    python aj_cli.py config [--set k=v ...]      # show / set risk config
    python aj_cli.py verify                       # show VERIFY-gate status
    python aj_cli.py explain [--cycle CYCLE_ID]  # decision funnel for a cycle
    python aj_cli.py secret --rotate-key          # rotate the secrets master key
    python aj_cli.py replay <run|grid|counterfactual|report|list|...> ...
                                                  # historical replay engine

The kill switch is intentionally a plain, dependency-light path — it must be
reachable without the model or the web app in the loop.
"""
import json
import sys


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def cmd_run(argv):
    import aj_operator
    mode = "paper"
    if "--mode" in argv:
        i = argv.index("--mode")
        if i + 1 < len(argv):
            mode = argv[i + 1]
    if mode not in ("paper", "live"):
        print(json.dumps({"ok": False,
                          "error": "unknown mode {!r}; expected paper|live".format(mode)}),
              file=sys.stderr)
        # Non-zero so a cron/monitor sees the misconfiguration instead of an
        # eternally-"successful" job that never runs a cycle.
        return 2
    if mode == "live":
        # live cycles still NEVER auto-execute; the operator only proposes +
        # gates, leaving approval to a human. Make the posture explicit.
        print("NOTE: live mode proposes + gates only; no order auto-executes.",
              file=sys.stderr)
    _print(aj_operator.run_once(mode))


def cmd_status(argv):
    import aj_db, aj_metrics
    aj_db.aj_init()
    _print(aj_metrics.status())


def cmd_replay(argv):
    """Run the replay engine in a FRESH subprocess: `run` must bind an
    isolated DB before any aj import, and this CLI process has already bound
    the live one — delegation gives isolation by construction."""
    import os, subprocess
    env = dict(os.environ)
    env.pop("AUGUR_DB_PATH", None)   # let aj_replay pick the per-run DB
    p = subprocess.run([sys.executable,
                        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "aj_replay.py")] + list(argv), env=env)
    return p.returncode


def cmd_kill(argv):
    import aj_db, aj_risk
    aj_db.aj_init()
    reason = " ".join(a for a in argv if not a.startswith("-")) or "manual kill (cli)"
    _print(aj_risk.kill_switch(reason))


def cmd_rearm(argv):
    import aj_db, aj_risk
    aj_db.aj_init()
    _print(aj_risk.rearm(actor="cli"))


def cmd_recon(argv):
    import aj_db, aj_execution, aj_config
    aj_db.aj_init()
    _print(aj_execution.reconcile(venue=aj_config.get_config().get("default_broker")))


def cmd_config(argv):
    import aj_db, aj_config
    aj_db.aj_init()
    updates = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--set" and i + 1 < len(argv):
            kv = argv[i + 1]
            if "=" in kv:
                k, v = kv.split("=", 1)
                updates[k.strip()] = v.strip()
            i += 2
        else:
            i += 1
    if updates:
        # set_config silently ignores unknown keys; surface those so a typo'd
        # cap (e.g. 'max_order_notinal_usd=...') doesn't look like it applied.
        ignored = [k for k in updates if k not in aj_config.DEFAULTS]
        result = aj_config.set_config(updates)
        if ignored:
            print("WARNING: ignored unknown config keys (not applied): {}".format(
                ", ".join(ignored)), file=sys.stderr)
            _print({"config": result, "ignored_keys": ignored})
        else:
            _print(result)
    else:
        _print(aj_config.get_config())


def _read_secret_value(scope, raw):
    """Resolve a secret value WITHOUT leaving plaintext on argv where possible.
      scope=@-            -> read from stdin (one line, newline stripped)
      scope=@/path/file  -> read from the named file
      scope=@env:NAME    -> read from environment variable NAME
      scope= (empty)     -> getpass prompt (interactive, not echoed)
      scope=value        -> legacy inline value (discouraged: visible in `ps`)
    """
    import os
    if raw == "@-":
        return sys.stdin.readline().rstrip("\n")
    if raw.startswith("@env:"):
        return os.environ.get(raw[5:], "")
    if raw.startswith("@"):
        with open(os.path.expanduser(raw[1:]), "r") as f:
            return f.read().rstrip("\n")
    if raw == "":
        import getpass
        return getpass.getpass("value for {}: ".format(scope))
    return raw


def cmd_secret(argv):
    """Store a broker credential in the secrets broker (encrypted at rest).
    Kept off the HTTP path on purpose. Prefer a source that doesn't expose the
    plaintext on argv (visible in shell history / `ps`):
        secret --set alpaca_key_id=@-           # read value from stdin
        secret --set alpaca_key_id=@/path/key   # read value from a file
        secret --set alpaca_key_id=@env:KID      # read value from $KID
        secret --set alpaca_key_id=             # getpass prompt (no echo)
    The legacy `--set scope=value` inline form still works but is discouraged.

    `secret --rotate-key` rotates the MASTER key: every stored blob is
    re-encrypted under a freshly generated Fernet key (atomic-ish — aborts
    untouched unless every blob decrypts first). Prints counts only, never
    key material."""
    import aj_db, aj_secrets
    aj_db.aj_init()
    if "--rotate-key" in argv:
        r = aj_secrets.rotate_master_key()
        # Counts + locations only — the key itself must NEVER hit stdout (it
        # would land in shell history / CI logs). The operator reads the key
        # file directly if an external AUGUR_SECRETS_KEY source needs updating.
        out = {"ok": bool(r.get("ok")), "rotated": r.get("rotated", 0)}
        if r.get("key_file"):
            out["key_file"] = r["key_file"]
        if r.get("env_key_active"):
            out["warning"] = ("AUGUR_SECRETS_KEY is set: update the external "
                              "env source to the new key (see key_file) or the "
                              "next restart cannot decrypt the rotated secrets")
        if r.get("error"):
            out["error"] = r["error"]
        _print(out)
        return 0 if out["ok"] else 1
    i, stored = 0, []
    while i < len(argv):
        if argv[i] == "--set" and i + 1 < len(argv) and "=" in argv[i + 1]:
            k, raw = argv[i + 1].split("=", 1)
            scope = k.strip()
            v = _read_secret_value(scope, raw.strip())
            # An empty resolved value (typo'd @env:NAME, empty file/stdin)
            # would silently ENCRYPT AND OVERWRITE a working credential — the
            # breakage only surfacing later as opaque broker auth failures.
            # Refuse it; the getpass path already re-prompts interactively.
            if not v:
                stored.append({scope: "refused: resolved value is empty "
                                      "(check @env:/@file source)"})
                i += 2
                continue
            ok = aj_secrets.store(scope, v)
            stored.append({scope: "stored" if ok else "failed"})
            i += 2
        else:
            i += 1
    _print({"secrets": stored} if stored else
           {"usage": "aj secret --set alpaca_key_id=@- --set alpaca_secret_key=@-"})


# Gates that flip a LIVE broker on; refusing to set these to 'pass' without an
# explicit operator acknowledgement that the contract test was actually run.
_BROKER_GATES = ("alpaca", "ccxt", "robinhood")
# 'council' gates the advisory Analyst Council (cost + non-determinism ack); it
# is not a live-money/broker gate, so it needs no contract test or --force.
_KNOWN_GATES = _BROKER_GATES + ("opencode", "mcp_read", "council")
_FORCE_FLAG = "--force-i-ran-the-contract-test"


def cmd_verify_pass(argv):
    """Mark a VERIFY gate passed AFTER its contract test succeeds.
    Usage: verify-pass <gate> [--force-i-ran-the-contract-test]
      <gate> must be one of: alpaca|ccxt|robinhood|opencode|mcp_read|council
      * mcp_read is self-verified here (its contract_ok() must pass);
      * broker gates require the explicit --force acknowledgement because the
        CLI cannot run the live broker contract test for you — fail-closed."""
    import aj_db, database as dbase
    aj_db.aj_init()
    gate = next((a for a in argv if not a.startswith("-")), "")
    forced = _FORCE_FLAG in argv
    if not gate:
        _print({"usage": "aj verify-pass <gate>  (alpaca|ccxt|robinhood|opencode|mcp_read|council)"})
        return
    if gate not in _KNOWN_GATES:
        _print({"error": "unknown gate", "gate": gate, "known": list(_KNOWN_GATES)})
        return
    if gate == "mcp_read":
        try:
            import aj_mcp_read
            if not aj_mcp_read.contract_ok():
                _print({"error": "mcp_read contract test did not pass; gate NOT set",
                        "gate": gate})
                return
        except Exception as e:
            _print({"error": "mcp_read contract test errored; gate NOT set",
                    "gate": gate, "detail": str(e)})
            return
    elif gate in _BROKER_GATES and not forced:
        _print({"error": "broker gate requires explicit acknowledgement that the "
                         "contract test was run", "gate": gate,
                "hint": "re-run with {}".format(_FORCE_FLAG)})
        return
    dbase.set_setting("aj_verify_" + gate, "pass")
    aj_db.audit("verify_pass", {"gate": gate, "forced": forced}, actor="cli")
    _print({"gate": gate, "status": "pass"})


def cmd_analytics(argv):
    import aj_db, aj_analytics
    aj_db.aj_init()
    _print(aj_analytics.summary())


def cmd_journal(argv):
    """Print the trade journal as CSV to stdout."""
    import aj_db, aj_analytics
    aj_db.aj_init()
    sys.stdout.write(aj_analytics.journal_csv())


def cmd_preset(argv):
    """Apply a risk/strategy preset. Usage: preset conservative|moderate|aggressive"""
    import aj_db, aj_config
    aj_db.aj_init()
    name = argv[0] if argv else ""
    r = aj_config.apply_preset(name)
    if r is None:
        _print({"error": "unknown preset", "presets": list(aj_config.PRESETS.keys())})
    else:
        _print({"preset": name, "applied": True})


def cmd_explain(argv):
    """Human-readable decision funnel for one operator cycle (the latest by
    default, or `--cycle CYCLE_ID`). Reads the aj_cycle_stats observability
    snapshot only — plain text, no network, never touches the trading path."""
    import aj_db
    aj_db.aj_init()
    cyc = None
    if "--cycle" in argv:
        i = argv.index("--cycle")
        if i + 1 < len(argv):
            cyc = argv[i + 1]
    if cyc:
        rows = aj_db.query("SELECT * FROM aj_cycle_stats WHERE cycle_id=?", (cyc,))
    else:
        rows = aj_db.query("SELECT * FROM aj_cycle_stats ORDER BY ts DESC LIMIT 1")
    if not rows:
        print("no cycle stats found" +
              (" for cycle {!r}".format(cyc) if cyc else " — run a cycle first"))
        return 1
    r = rows[0]

    def _i(k):
        try:
            return int(r.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    print("cycle   : {}".format(r.get("cycle_id")))
    print("ts      : {}   session: {}   mode: {}".format(
        r.get("ts"), r.get("session") or "?", r.get("mode") or "?"))
    print("funnel  : scanned {} -> with_signal {} -> executed {}   (exits {})".format(
        _i("scanned"), _i("with_signal"), _i("executed"), _i("exits")))

    # Per-symbol dispositions, grouped by result so the operator reads "what
    # blocked everything this cycle" at a glance rather than scanning JSON.
    try:
        scan = json.loads(r.get("scan_json") or "[]") or []
    except (TypeError, ValueError):
        scan = []
    if not scan:
        print("(no per-symbol scan rows recorded for this cycle)")
        return 0
    groups = {}
    for row in scan:
        groups.setdefault(str(row.get("result") or "?"), []).append(row)

    def _num(v, fmt):
        try:
            return fmt.format(float(v))
        except (TypeError, ValueError):
            return "-"
    # 'executed' first (the outcome), then the block reasons by frequency.
    order = sorted(groups, key=lambda k: (k != "executed", -len(groups[k]), k))
    for res in order:
        rows_g = groups[res]
        print("\n{} ({}):".format(res, len(rows_g)))
        for row in rows_g:
            print("  {:<10} edge={:>7}  prob_up={:>5}".format(
                str(row.get("symbol") or "?"),
                _num(row.get("edge"), "{:+.2f}"),
                _num(row.get("prob_up"), "{:.2f}")))
    return 0


def cmd_verify(argv):
    import aj_db, database as dbase
    aj_db.aj_init()
    gates = ("mcp_read", "opencode", "robinhood", "alpaca", "ccxt", "council")
    raw = dbase.get_settings()
    out = {}
    for g in gates:
        out[g] = raw.get("aj_verify_" + g, "not-passed")
    # mcp_read can be self-verified offline
    try:
        import aj_mcp_read
        out["mcp_read_contract_ok"] = aj_mcp_read.contract_ok()
    except Exception as e:
        out["mcp_read_contract_ok"] = "error: {}".format(e)
    _print(out)


_CMDS = {
    "run": cmd_run, "status": cmd_status, "kill": cmd_kill, "rearm": cmd_rearm,
    "recon": cmd_recon, "config": cmd_config, "verify": cmd_verify,
    "secret": cmd_secret, "verify-pass": cmd_verify_pass,
    "analytics": cmd_analytics, "journal": cmd_journal, "preset": cmd_preset,
    "explain": cmd_explain, "replay": cmd_replay,
}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    fn = _CMDS.get(cmd)
    if not fn:
        print("unknown command: {}\n{}".format(cmd, __doc__), file=sys.stderr)
        return 2
    try:
        # Propagate a command's explicit exit code (e.g. cmd_run's invalid-mode
        # 2) — swallowing it made every misconfigured invocation exit 0.
        rc = fn(argv[1:])
        return int(rc) if isinstance(rc, int) else 0
    except Exception as e:
        # Emit the full traceback to stderr (an operator debugging a kill/recon
        # failure needs it), but keep the secret-handling commands from echoing
        # any argv-derived value into the one-line JSON error.
        import logging, traceback
        logging.getLogger("augur.aj_cli").exception("aj_cli command %r failed", cmd)
        if cmd in ("secret", "verify-pass"):
            err = "{} command failed (see stderr traceback)".format(cmd)
        else:
            err = str(e)
        traceback.print_exc()
        print(json.dumps({"ok": False, "error": err}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
