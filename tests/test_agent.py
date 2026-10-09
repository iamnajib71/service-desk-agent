import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from desk_agent.config import Config
from desk_agent.engine import Engine
from desk_agent.model import Memory, Ollama
from desk_agent.schema import ValidationError, validate
from desk_agent.store import Store
from desk_agent.tools import SPECS, Tools, action_schema
from tests.fakes import act, FakeModel, FakeTools


class AgentTests(unittest.TestCase):
    def setUp(self):
        scratch = Path("state/test-tmp")
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch.resolve())
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "agent.sqlite")
        self.tools = FakeTools(self.root)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def engine(self, decisions=None, **kwargs):
        return Engine(self.store, self.tools, FakeModel(self.tools.config, decisions), backoff=0, emit=lambda _: None, **kwargs)

    def test_allowlist_and_types(self):
        for action in [act("ad_lookup", user="Administrator"), act("ad_lookup", user="bob; rm -rf /"),
                       act("ad_group", user="bob", group="Domain Admins", present=True),
                       act("ad_group", user="bob", group="GG-Finance", present="true"),
                       act("wg_revoke", name="real-device"), act("ad_reset", user="alice"),
                       act("cups_cancel", job="-a"), act("exec", cmd="whoami")]:
            with self.assertRaises(ValidationError):
                validate(action["action"], action_schema())
        validate(act("ad_lookup", user="bob")["action"], action_schema())

    def test_unknown_fields_rejected(self):
        a = act("ad_lookup", user="bob")["action"]
        a["args"]["command"] = "anything"
        with self.assertRaises(ValidationError):
            validate(a, action_schema())

    def test_dry_run_every_tool_never_calls_adapters(self):
        examples = {
            "ad_lookup": {"user": "bob"}, "ad_groups": {"user": "bob"}, "ad_unlock": {"user": "bob"},
            "ad_reset": {"user": "sdtest_one"}, "ad_group": {"user": "bob", "group": "GG-Staff", "present": True},
            "smb_group": {"user": "bob", "group": "finance", "present": True}, "smb_groups": {"user": "bob"},
            "share_check": {"user": "bob", "share": "Public"}, "cups_list": {}, "cups_jobs": {"queue": "Office-PDF"},
            "cups_resume": {"queue": "Office-PDF"}, "cups_cancel": {"job": "Office-PDF-1"}, "wg_list": {},
            "wg_create": {"name": "agent-test"}, "wg_revoke": {"name": "agent-test"}, "kuma_status": {}}
        tools = Tools(self.tools.config)
        with patch.object(tools, "docker", side_effect=AssertionError), patch.object(tools, "wg", side_effect=AssertionError):
            for name in SPECS:
                self.assertTrue(tools.execute({"tool": name, "args": examples[name]}, "key", dry_run=True)["dry_run"])

    def test_approval_pauses_then_resumes(self):
        engine = self.engine([act("ad_group", user="bob", group="GG-Finance", present=True)])
        self.assertEqual(engine.start("approval", "bob Finance approved"), "awaiting_approval")
        self.assertEqual(self.tools.calls, 0)
        step = self.store.steps("approval")[0]
        self.store.approve(step, "Human")
        self.assertEqual(engine.resume("approval"), "resolved")
        self.assertEqual(self.tools.calls, 1)
        self.assertTrue(any(r["event"] == "approval" for r in self.store.export("approval")["audit"]))

    def test_denial_has_no_effect(self):
        engine = self.engine([act("ad_group", user="bob", group="GG-Finance", present=True)])
        engine.start("denied", "bob Finance")
        self.store.approve(self.store.steps("denied")[0], "Human", False)
        self.assertEqual(engine.resume("denied"), "denied")
        self.assertEqual(self.tools.calls, 0)

    def test_approval_bound_to_action(self):
        self.store.create("binding", "ticket", "live", 5, [])
        step = self.store.add_step("binding", act("ad_group", user="bob", group="GG-Finance", present=True))
        self.store.approve(step, "Human")
        step["decision"] = json.dumps(act("ad_group", user="bob", group="GG-Finance", present=False))
        self.assertIsNone(self.store.approval(step))

    def test_backoff_retries_recorded(self):
        self.tools.failures = 2
        engine = self.engine([act("ad_lookup", user="bob")])
        self.assertEqual(engine.start("retry", "Look up bob"), "resolved")
        self.assertEqual(self.store.steps("retry")[0]["attempts"], 3)

    def test_model_invalid_output_escalates(self):
        engine = self.engine([{"kind": "exec"}] * 3)
        self.assertEqual(engine.start("bad", "ticket"), "escalated")
        self.assertEqual(self.tools.calls, 0)

    def test_budget_bounds_repeated_steps(self):
        engine = self.engine([act("ad_lookup", user="bob")] * 8)
        self.assertEqual(engine.start("budget", "ticket", budget=2), "budget_exhausted")
        self.assertEqual(self.tools.calls, 2)

    def test_no_unobserved_resolution(self):
        self.assertEqual(self.engine().start("empty", "ticket"), "escalated")

    def test_all_failed_steps_cannot_resolve(self):
        self.tools.failures = 3
        engine = self.engine([act("ad_lookup", user="bob")])
        self.assertEqual(engine.start("failed", "Look up bob"), "escalated")
        self.assertEqual(self.store.steps("failed")[0]["attempts"], 3)

    def test_exhausted_step_can_reflect_and_choose_alternate(self):
        self.tools.failures = 3
        engine = self.engine([act("ad_lookup", user="bob"), act("kuma_status")])
        self.assertEqual(engine.start("alternate", "Check bob and office status"), "resolved")
        steps = self.store.steps("alternate")
        self.assertIn("error", json.loads(steps[0]["result"]))
        self.assertTrue(json.loads(steps[1]["result"])["verified"])
        self.assertEqual(len(steps), 2)

    def test_resume_completed_run_no_effect(self):
        engine = self.engine([act("ad_unlock", user="bob")])
        engine.start("complete", "unlock bob")
        calls = self.tools.calls
        self.assertEqual(engine.resume("complete"), "resolved")
        self.assertEqual(self.tools.calls, calls)

    def test_real_process_death_after_effect_no_repeat(self):
        self.store.create("crash", "bob Finance approved", "live", 5, [])
        step = self.store.add_step("crash", act("ad_group", user="bob", group="GG-Finance", present=True))
        self.store.approve(step, "Human")
        proc = subprocess.run([sys.executable, "-m", "tests.crash_worker", str(self.root), "after:1"], capture_output=True)
        self.assertEqual(proc.returncode, 86, proc.stderr.decode())
        self.assertEqual(self.store.steps("crash")[0]["state"], "running")
        proc = subprocess.run([sys.executable, "-m", "tests.crash_worker", str(self.root), "none"], capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(json.loads((self.root / "effect.json").read_text())["count"], 1)
        self.assertEqual(self.store.get("crash")["status"], "resolved")

    def test_interrupted_reset_needs_review(self):
        self.store.create("reset", "ticket", "live", 5, [])
        step = self.store.add_step("reset", act("ad_reset", user="sdtest_one"))
        self.store.approve(step, "Human")
        self.store.state("reset", 1, "running", attempt=True)
        self.assertEqual(self.engine().resume("reset"), "needs_review")
        self.assertEqual(self.tools.calls, 0)

    def test_memory_returns_real_ticket_citation(self):
        model = FakeModel(self.tools.config)
        memory = Memory(self.store, model)
        memory.remember("past-1", "paused PDF", "enabled PDF and verified")
        hit = memory.search("PDF queue paused")[0]
        self.assertEqual(hit["ticket_id"], "past-1")
        self.assertEqual(hit["score"], 1)

    def test_cloud_and_remote_models_rejected(self):
        for config in [Config(self.root, self.root, model="test:cloud"),
                       Config(self.root, self.root, ollama_url="https://remote.example")]:
            with self.assertRaises(ValueError):
                Ollama(config)

    def test_secrets_redacted(self):
        (self.root / ".env").write_text("WG_ADMIN_PASSWORD=private123\nRESET_abc=Aa1!secret\n")
        self.assertEqual(self.tools.config.redact("private123 Aa1!secret"), "[REDACTED] [REDACTED]")

    def test_execution_timeout_is_bounded(self):
        import subprocess
        tools = Tools(self.tools.config)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 30)) as command:
            from desk_agent.tools import ToolError
            with self.assertRaises(ToolError):
                tools.docker("directory", ["samba-tool", "user", "show", "bob"])
            self.assertEqual(command.call_args.kwargs["timeout"], 30)

    def test_model_uses_exact_tool_argument_schema(self):
        model = Ollama(self.tools.config)
        replies = [
            {"message": {"content": json.dumps({"kind": "act", "plan": "Add bob to Finance",
                "reflection": "Approved request", "tool": "smb_group", "summary": ""})}},
            {"message": {"content": json.dumps({"user": "bob", "group": "finance", "present": True})}}]
        with patch.object(model, "request", side_effect=replies) as request:
            decision = model.decide("Manager approved add bob to Samba Finance share group", [], [])
            args_schema = request.call_args_list[1].args[1]["format"]
            self.assertEqual(args_schema["properties"]["user"]["enum"], ["bob"])
            self.assertEqual(args_schema["properties"]["present"]["enum"], [True])
            self.assertTrue(decision["action"]["args"]["present"])

    def test_model_cannot_infer_user_from_memory(self):
        model = Ollama(self.tools.config)
        reply = {"message": {"content": json.dumps({"kind": "escalate", "plan": "Ask for user",
                "reflection": "Missing identity", "tool": "none", "summary": "Username required"})}}
        with patch.object(model, "request", return_value=reply) as request:
            decision = model.decide("Give me Finance access without a username", [], [{"ticket": "bob Finance"}])
            self.assertEqual(request.call_args.args[1]["format"]["properties"]["tool"]["enum"], ["none"])
            self.assertEqual(decision["kind"], "escalate")

    def test_closure_rejects_still_disabled_requested_queue(self):
        history = [{"decision": act("cups_list"), "observation": {"output":
            "printer Office-PDF is idle. enabled\nprinter Office-Laser disabled since yesterday"}}]
        self.assertIn("Office-Laser", Ollama.closure_error("Resume Office-PDF and Office-Laser", history))

    def test_closure_requires_share_check_after_membership_change(self):
        history = [{"decision": act("smb_group", user="bob", group="finance", present=True),
                    "observation": {"verified": True}}]
        self.assertIn("share_check", Ollama.closure_error("Grant bob Finance access", history))
        history.append({"decision": act("share_check", user="bob", share="Finance"), "observation": {"accessible": True}})
        self.assertIsNone(Ollama.closure_error("Grant bob Finance access", history))

    def test_cancellation_does_not_require_resuming_a_paused_queue(self):
        history = [{"decision": act("cups_list"), "observation": {"output": "printer Office-PDF disabled"}}]
        self.assertIsNone(Ollama.closure_error("Cancel confirmed Office-PDF-1. Do not resume or purge other jobs.", history))

    def test_ad_only_scope_excludes_standalone_samba(self):
        model = Ollama(self.tools.config)
        reply = {"message": {"content": json.dumps({"kind": "escalate", "plan": "Review",
                "reflection": "Need review", "tool": "none", "summary": "Review requested"})}}
        with patch.object(model, "request", return_value=reply) as request:
            model.decide("bob needs GG-Finance AD membership. This ticket is AD only.", [], [])
            names = request.call_args.args[1]["format"]["properties"]["tool"]["enum"]
            self.assertNotIn("smb_group", names)
            self.assertNotIn("share_check", names)

    def test_successful_read_is_suppressed_until_change(self):
        model = Ollama(self.tools.config)
        history = [{"step": 1, "decision": act("kuma_status"), "observation": {"all_up": True}}]
        reply = {"message": {"content": json.dumps({"kind": "resolve", "plan": "Complete",
                "reflection": "Observed UP", "tool": "none", "summary": "All monitors UP"})}}
        with patch.object(model, "request", return_value=reply) as request:
            model.decide("Read Uptime Kuma status", history, [])
            schema = request.call_args.args[1]["format"]
            self.assertEqual(schema["properties"]["tool"]["enum"], ["none"])
            self.assertEqual(schema["properties"]["kind"]["enum"], ["resolve", "escalate"])


if __name__ == "__main__":
    unittest.main()
