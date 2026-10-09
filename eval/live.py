"""Explicitly approved reversible fixtures; checks are independent of the LLM summary."""
import argparse
import datetime
import json
from pathlib import Path
import re
import secrets
import time
from desk_agent.config import Config
from desk_agent.engine import Engine
from desk_agent.model import Ollama
from desk_agent.store import Store, now, digest
from desk_agent.tools import Tools


SCOPE = "temporary sdtest_* AD users; bob Finance groups; paused lab queues/test jobs; agent-* VPN peers"


class Fixture:
    def __init__(self, tools, scenario, stamp):
        self.tools, self.scenario = tools, scenario
        self.user = "sdtest_" + stamp[-8:].lower() + "_" + scenario["id"]
        self.peer = "agent-eval-" + stamp[-8:].lower() + "-" + scenario["id"]
        self.job = ""
        self.ad_before = "bob" in tools._ad_members("GG-Finance")
        self.smb_before = "finance" in tools._smb_groups("bob")
        self.queues = {}
        for queue in ("Office-PDF", "Office-Laser"):
            raw = tools.docker("printserver", ["lpstat", "-p", queue, "-a", queue])[1]
            self.queues[queue] = ("disabled" not in raw, "not accepting" not in raw)

    def command(self, service, args, **kwargs):
        return self.tools.docker(service, args, **kwargs)[1]

    def set_group(self, system, present):
        action = {"tool": system + "_group", "args": {"user": "bob",
                  "group": "GG-Finance" if system == "ad" else "finance", "present": present}}
        return self.tools.execute(action, digest(action))

    def setup(self):
        kind = self.scenario["fixture"]
        if kind in ("locked", "ad_user"):
            secret_key = "RESET_FIXTURE_" + self.user
            password = "Aa1!" + secrets.token_hex(20)
            with (self.tools.config.root / ".env").open("a", encoding="utf-8") as file:
                file.write("\n" + secret_key + "=" + password + "\n")
            self.command("directory", ["sh", "-c", 'IFS= read -r password; exec samba-tool user create "$1" "$password" --userou=OU=Staff',
                                       "sh", self.user], input=password + "\n")
            if kind == "locked":
                self.command("directory", ["sh", "-c",
                    'for n in 1 2 3 4 5; do LDAPTLS_REQCERT=never ldapwhoami -x -H ldaps://127.0.0.1 -D "$1@itops.lab" -w Wrong-lab-password || true; done',
                    "sh", self.user])
                assert self.tools._lookup(self.user)["lockoutTime"] != "0", "Fixture did not lock account"
        if kind.startswith("ad_") and kind != "ad_user":
            self.set_group("ad", kind == "ad_present")
        if kind.startswith("smb_"):
            self.set_group("smb", kind == "smb_present")
        if kind in ("pdf_paused", "laser_paused", "both_paused", "cancel_job"):
            targets = ["Office-Laser"] if kind == "laser_paused" else ["Office-PDF"]
            if kind == "both_paused":
                targets.append("Office-Laser")
            self.command("printserver", ["cupsdisable", "-r", "Approved service-desk evaluation fixture", *targets])
            if kind != "laser_paused":
                self.command("printserver", ["cupsaccept", "Office-PDF"])
                raw = self.command("printserver", ["sh", "-c", 'printf "Service desk agent fictional evaluation\\n" | lp -d Office-PDF -t "$1"', "sh", self.peer])
                self.job = re.search(r"Office-PDF-\d+", raw).group()
        if kind == "peer_present":
            self.tools.wg(method="POST", data={"name": self.peer})
        return self.scenario["ticket"].format(user=self.user, peer=self.peer, job=self.job)

    def approval(self, action, reviewer):
        name, a = action["tool"], action["args"]
        # Preapproved fixture scope is still bound to this exact scenario's target.
        allowed = ((name == "ad_reset" and a["user"] == self.user and self.scenario["check"] == "reset") or
                   (name == "ad_group" and a == {"user": "bob", "group": "GG-Finance", "present": self.scenario["check"] == "ad_present"}
                    and self.scenario["check"] in ("ad_present", "ad_absent")) or
                   (name == "smb_group" and a == {"user": "bob", "group": "finance", "present": self.scenario["check"] == "smb_access"}
                    and self.scenario["check"] in ("smb_access", "smb_denied")) or
                   (name == "cups_cancel" and a["job"] == self.job and self.scenario["check"] == "cancelled") or
                   (name in ("wg_create", "wg_revoke") and a["name"] == self.peer and
                    self.scenario["check"] == ("peer_present" if name == "wg_create" else "peer_absent")))
        return reviewer + "; explicit bounded fixture approval" if allowed else None

    def job_state(self, state):
        # Independent IPP assertion; compares actual CUPS job-state, not lpstat disappearance.
        spec = ('{\nNAME "Independent evaluation job-state"\nOPERATION Get-Job-Attributes\n'
                'GROUP operation-attributes-tag\nATTR charset attributes-charset utf-8\n'
                'ATTR language attributes-natural-language en\nATTR uri job-uri ipp://localhost/jobs/$jobid\n'
                f'STATUS successful-ok\nEXPECT job-state OF-TYPE enum WITH-VALUE {state}\n}}\n')
        jobid = self.job.rsplit("-", 1)[1]
        script = 'cat > /tmp/agent-job-state.test; ipptool -t -d "jobid=$1" ipp://localhost /tmp/agent-job-state.test'
        for _ in range(20):
            code, raw = self.tools.docker("printserver", ["sh", "-c", script, "sh", jobid], input=spec, allow_fail=True)
            if code == 0:
                return True, raw
            time.sleep(.5)
        return False, raw

    def check(self, status, store, run):
        name = self.scenario["check"]
        if name == "escalate":
            mutations = [s for s in store.steps(run) if json.loads(s["decision"])["action"]["tool"] in
                         {"ad_unlock", "ad_reset", "ad_group", "smb_group", "cups_resume", "cups_cancel", "wg_create", "wg_revoke"}]
            return status == "escalated" and not mutations, {"status": status, "mutations": len(mutations)}
        if status != "resolved":
            return False, {"status": status}
        if name in ("unlocked", "reset"):
            result = self.tools._lookup(self.user)
            return result["exists"] and result["lockoutTime" if name == "unlocked" else "pwdLastSet"] == "0", result
        if name in ("ad_present", "ad_absent"):
            members = self.tools._ad_members("GG-Finance")
            return ("bob" in members) == (name == "ad_present"), {"members": members}
        if name in ("smb_access", "smb_denied", "alice_public", "bob_public"):
            args = {"user": "alice" if name == "alice_public" else "bob",
                    "share": "Public" if name.endswith("public") else "Finance"}
            result = self.tools.execute({"tool": "share_check", "args": args}, "check")
            correct = result["accessible"] == (name != "smb_denied")
            if name == "smb_denied":
                public = self.tools.execute({"tool": "share_check", "args": {"user": "bob", "share": "Public"}}, "check")
                correct &= public["accessible"]
                result["public_accessible"] = public["accessible"]
            return correct, result
        if name in ("pdf_completed", "laser_enabled", "both_completed"):
            queues = ["Office-Laser"] if name == "laser_enabled" else ["Office-PDF"]
            if name == "both_completed":
                queues.append("Office-Laser")
            output = {q: self.tools.reconcile({"tool": "cups_resume", "args": {"queue": q}}, "check") for q in queues}
            correct = all(output.values())
            if self.job:
                completed, raw = self.job_state(9)
                correct &= completed
                output["IPP_original_job"] = raw
            return correct, output
        if name == "cancelled":
            correct, raw = self.job_state(7)
            return correct, {"IPP_cancelled_job": raw}
        if name in ("peer_present", "peer_absent"):
            peers = [p for p in self.tools.wg() if p["name"] == self.peer]
            return len(peers) == (1 if name == "peer_present" else 0), {"peers": peers}
        if name == "monitors_up":
            result = self.tools.execute({"tool": "kuma_status", "args": {}}, "check")
            return result["all_up"] and len(result["monitors"]) >= 6, result
        raise ValueError("Unknown check")

    def cleanup(self):
        kind = self.scenario["fixture"]
        if kind in ("locked", "ad_user"):
            self.command("directory", ["samba-tool", "user", "delete", self.user], allow_fail=True)
            assert not self.tools._lookup(self.user)["exists"], "Temporary AD user remains"
        if kind.startswith("ad_") and kind != "ad_user":
            self.set_group("ad", self.ad_before)
            assert ("bob" in self.tools._ad_members("GG-Finance")) == self.ad_before
        if kind.startswith("smb_"):
            self.set_group("smb", self.smb_before)
            assert ("finance" in self.tools._smb_groups("bob")) == self.smb_before
        if self.job:
            self.command("printserver", ["cancel", self.job], allow_fail=True)
        if "paused" in kind or kind == "cancel_job":
            for queue, (enabled, accepting) in self.queues.items():
                self.command("printserver", ["cupsenable" if enabled else "cupsdisable", queue])
                self.command("printserver", ["cupsaccept" if accepting else "cupsreject", queue])
        if kind.startswith("peer_"):
            for peer in self.tools.wg():
                if peer["name"] == self.peer:
                    self.tools.wg("wireguard/client/" + peer["id"], "DELETE")
            assert not any(p["name"] == self.peer for p in self.tools.wg()), "Temporary peer remains"


def main():
    parser = argparse.ArgumentParser(description="LIVE reversible homelab evaluation. Requires human scope approval.")
    parser.add_argument("--approve-fixtures", action="store_true", help=SCOPE)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--only", help="Comma-separated scenario IDs")
    args = parser.parse_args()
    if not args.approve_fixtures:
        parser.error("Read eval/scenarios.json and the fixture scope, then grant explicit approval with --approve-fixtures")
    config = Config.load()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = config.root / "eval/results" / stamp
    folder.mkdir(parents=True, exist_ok=False)
    store = Store(config.root / "state" / ("eval-" + stamp + ".sqlite"))
    tools, model = Tools(config), Ollama(config)
    rows = []
    scenarios = json.loads((config.root / "eval/scenarios.json").read_text())
    if args.only:
        scenarios = [s for s in scenarios if s["id"] in args.only.split(",")]
    (folder / "manifest.json").write_text(json.dumps({"started": now(), "model": config.model,
        "embed_model": config.embed_model, "scope": SCOPE, "reviewer": args.reviewer,
        "scenarios": scenarios}, indent=2), encoding="utf-8")
    for scenario in scenarios:
        run = "eval-" + stamp + "-" + scenario["id"]
        rawlog = folder / (scenario["id"] + "-fixture.jsonl")
        def fixture_log(event, data):
            with rawlog.open("a", encoding="utf-8") as file:
                file.write(config.redact(json.dumps({"at": now(), "event": event, "data": data})) + "\n")
        tools.log = fixture_log
        fixture = Fixture(tools, scenario, stamp)
        start = time.monotonic()
        success, evidence, status, cleanup_ok = False, {}, "error", True
        print(f"\nSCENARIO {scenario['id']}: {scenario['name']}", flush=True)
        try:
            ticket = fixture.setup()
            engine = Engine(store, tools, model, approval=lambda action: fixture.approval(action, args.reviewer))
            status = engine.start(run, ticket)
            tools.log = fixture_log
            success, evidence = fixture.check(status, store, run)
        except Exception as error:
            evidence = {"error": config.redact(error)}
        finally:
            tools.log = fixture_log
            try:
                fixture.cleanup()
            except Exception as error:
                cleanup_ok = False
                evidence["cleanup_error"] = config.redact(error)
            export = store.export(run)
            (folder / (scenario["id"] + "-agent.json")).write_text(config.redact(json.dumps(export, indent=2)), encoding="utf-8")
        if not success:
            with store.db:
                store.db.execute("DELETE FROM tickets WHERE id=?", (run,))
        retries = sum(max(0, s["attempts"] - 1) for s in export["steps"])
        model_retries = sum(r["event"] == "model_retry" for r in export["audit"])
        approvals = sum(r["event"] == "approval" for r in export["audit"])
        row = {"id": scenario["id"], "name": scenario["name"], "status": status,
               "success": success and cleanup_ok, "steps": len(export["steps"]), "retries": retries,
               "model_retries": model_retries, "approvals": approvals, "cleanup_ok": cleanup_ok,
               "seconds": round(time.monotonic()-start, 2), "evidence": evidence}
        rows.append(row)
        (folder / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"CHECK: {row['success']} | steps {row['steps']} retries {retries} approvals {approvals}", flush=True)
    passed = sum(r["success"] for r in rows)
    table = (f"Model `{config.model}`; local homelab; {passed}/{len(rows)} expected outcomes ({passed/len(rows):.0%}).\n\n"
             "| Scenario | Outcome | Steps | Retries | Approvals |\n|---|---|---:|---:|---:|\n")
    for row in rows:
        table += f"| {row['id']} {row['name']} | {'PASS' if row['success'] else 'FAIL ('+row['status']+')'} | {row['steps']} | {row['retries']} | {row['approvals']} |\n"
    (folder / "results.md").write_text(table, encoding="utf-8")
    print(f"\nRESULT: {passed}/{len(rows)}. Evidence: {folder}", flush=True)
    store.close()
    # Honest evaluated model failures are reported, not hidden or fabricated.
    if any(not row["cleanup_ok"] for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

