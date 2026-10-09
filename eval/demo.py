"""Record an actual local-model run, approval pause, process death and resume."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
from desk_agent.config import Config
from desk_agent.engine import Engine
from desk_agent.model import Ollama
from desk_agent.store import Store, now
from desk_agent.tools import Tools
from .live import Fixture, SCOPE


class Recorder:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("w", encoding="utf-8")
        self.start = time.monotonic()
        self.file.write(json.dumps({"version": 2, "width": 110, "height": 28,
            "timestamp": int(time.time()), "title": "Live Finance repair: approval and crash recovery",
            "env": {"TERM": "xterm-256color"}}) + "\n")
        self.lines = []

    def emit(self, message):
        # Readable terminal output; exact full raw observations are retained in audit JSON.
        print(message, flush=True)
        self.lines.append(message)
        self.file.write(json.dumps([round(time.monotonic()-self.start, 4), "o", message + "\r\n"]) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


def main():
    parser = argparse.ArgumentParser(description="Real live recovery demo. " + SCOPE)
    parser.add_argument("--approve-fixtures", action="store_true")
    parser.add_argument("--reviewer", required=True)
    args = parser.parse_args()
    if not args.approve_fixtures:
        parser.error("Explicit fixture approval required")
    config = Config.load()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = "demo-" + stamp
    folder = config.root / "eval/results" / run
    folder.mkdir(parents=True)
    dbpath = config.root / "state" / (run + ".sqlite")
    store = Store(dbpath)
    tools = Tools(config)
    fixture_logs = []
    tools.log = lambda event, data: fixture_logs.append({"at": now(), "event": event, "data": data})
    scenario = {"id": "99", "fixture": "smb_missing", "check": "smb_access", "ticket":
                "Manager-approved fictional bob needs standalone Samba Finance share access. Public works, Finance is denied. Inspect Finance with share_check, add missing finance membership with smb_group present true, then verify bob can list Finance using share_check. AD does not grant this share."}
    fixture = Fixture(tools, scenario, stamp)
    recorder = Recorder(config.root / "docs/demo.cast")
    summary = {}
    try:
        ticket = fixture.setup()
        recorder.emit("SERVICE DESK AGENT | runs locally | real homelab + local Ollama")
        recorder.emit("Ticket: fictional bob cannot open Finance. Manager approval required.")
        recorder.emit("$ python -m desk_agent.cli run demo '<Finance access ticket>'")
        def emit(message):
            # Full smb directory listings are available in the raw audit; keep demo within terminal width.
            if message.startswith("Observed:") and len(message) > 180:
                message = message[:177] + "..."
            recorder.emit(message)
        engine = Engine(store, tools, Ollama(config), emit=emit)
        status = engine.start(run, ticket)
        assert status == "awaiting_approval", f"Expected real approval pause, got {status}"
        pending = [s for s in store.steps(run) if s["state"] == "pending"][0]
        action = json.loads(pending["decision"])["action"]
        reviewer = fixture.approval(action, args.reviewer)
        assert reviewer, "Demo action outside explicitly approved fixture scope"
        recorder.emit("HUMAN REVIEW: " + json.dumps(action))
        store.approve(pending, reviewer)
        recorder.emit("APPROVED by " + args.reviewer + " (bounded evaluation scope)")
        recorder.emit(f"$ python -m desk_agent.cli resume {run} --fault after:{pending['n']}")
        process = subprocess.Popen([sys.executable, "-u", "-m", "desk_agent.cli", "--db", str(dbpath),
                                    "resume", run, "--fault", f"after:{pending['n']}"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=config.root)
        for line in process.stdout:
            recorder.emit(config.redact(line.rstrip()))
        code = process.wait(timeout=180)
        assert code == 86, f"Expected intentional death 86; got {code}"
        recorder.emit("PROCESS KILLED (exit 86) after group change, before SQLite completion.")
        assert "finance" in tools._smb_groups("bob"), "Side effect did not happen"
        recorder.emit("Independent check: bob is in finance; checkpoint still says running.")
        recorder.emit("$ python -m desk_agent.cli resume " + run)
        status = engine.resume(run)
        passed, evidence = fixture.check(status, store, run)
        audit = store.export(run)
        writes = [r for r in audit["audit"] if r["event"] == "command" and "'addgroup', 'bob', 'finance'" in r["data"]]
        assert passed and len(writes) == 1, f"Recovery failed or repeated side effect: {status}, writes={len(writes)}"
        recorder.emit("VERIFIED: smbclient Finance access succeeds; addgroup executed exactly ONCE.")
        recorder.emit("Audit: action, human approval, injected death, reconciliation, verification retained.")
        summary = {"run": run, "status": status, "success": passed, "addgroup_calls": len(writes),
                   "process_exit": code, "evidence": evidence,
                   "seconds": round(time.monotonic()-recorder.start, 2)}
        (folder / "agent.json").write_text(config.redact(json.dumps(audit, indent=2)), encoding="utf-8")
    finally:
        (folder / "agent.json").write_text(config.redact(json.dumps(store.export(run), indent=2)), encoding="utf-8")
        tools.log = lambda event, data: fixture_logs.append({"at": now(), "event": event, "data": data})
        fixture.cleanup()
        recorder.emit("Cleanup: original bob Finance membership restored.")
        recorder.close()
        (folder / "fixture.json").write_text(config.redact(json.dumps(fixture_logs, indent=2)), encoding="utf-8")
        (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        store.close()
    print("Demo recorded:", config.root / "docs/demo.cast")


if __name__ == "__main__":
    main()
