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
        _print(aj_config.set_config(updates))
    else:
        _print(aj_config.get_config())


def cmd_secret(argv):
    """Store a broker credential in the secrets broker (encrypted at rest).
    Kept off the HTTP path on purpose. Usage: secret --set scope=value [...]"""
    import aj_db, aj_secrets
    aj_db.aj_init()
    i, stored = 0, []
    while i < len(argv):
        if argv[i] == "--set" and i + 1 < len(argv) and "=" in argv[i + 1]:
            k, v = argv[i + 1].split("=", 1)
            ok = aj_secrets.store(k.strip(), v.strip())
            stored.append({k.strip(): "stored" if ok else "failed"})
            i += 2
        else:
            i += 1
    _print({"secrets": stored} if stored else
           {"usage": "aj secret --set alpaca_key_id=PK... --set alpaca_secret_key=SK..."})


def cmd_verify_pass(argv):
    """Mark a VERIFY gate passed AFTER its contract test succeeds.
    Usage: verify-pass <gate>   (e.g. alpaca)"""
    import aj_db, database as dbase
    aj_db.aj_init()
    gate = argv[0] if argv else ""
    if not gate:
        _print({"usage": "aj verify-pass <gate>  (alpaca|ccxt|robinhood|opencode|mcp_read)"})
        return
    dbase.set_setting("aj_verify_" + gate, "pass")
    _print({"gate": gate, "status": "pass"})


def cmd_verify(argv):
    import aj_db, database as dbase
    aj_db.aj_init()
    gates = ("mcp_read", "opencode", "robinhood", "alpaca", "ccxt")
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
        fn(argv[1:])
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
