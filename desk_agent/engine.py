import json
import os
import time
from .model import DECISION_SCHEMA, Memory
from .schema import validate, ValidationError
from .store import encoded
from .tools import SPECS, ToolError, UncertainError


class Engine:
    def __init__(self, store, tools, model, *, retries=3, backoff=.5, approval=None, emit=None):
        self.store, self.tools, self.model = store, tools, model
        self.retries, self.backoff = retries, backoff
        self.approval = approval
        self.emit = emit or (lambda message: print(message, flush=True))

    def start(self, run, ticket, *, budget=12, dry_run=False):
        if not 1 <= budget <= 40:
            raise ValueError("Budget must be 1..40")
        if len(ticket) > 4000:
            raise ValueError("Ticket too long")
        memory = []
        try:
            memory = Memory(self.store, self.model).search(ticket)
        except Exception as error:
            self.store.event(run, "memory_unavailable", {"error": str(error)[:300]})
        self.store.create(run, self.tools.config.redact(ticket), "dry-run" if dry_run else "live", budget, memory)
        self.store.event(run, "memory_search", {"citations": memory})
        return self.resume(run)

    def resume(self, run, *, fault=None):
        with self.store.lease(run):
            return self._resume(run, fault=fault)

    def _resume(self, run, *, fault=None):
        record = self.store.get(run)
        if not record:
            raise ValueError("Unknown run")
        if record["status"] in ("resolved", "escalated", "budget_exhausted", "dry_run_complete", "denied"):
            return record["status"]
        self.tools.log = lambda event, data: self.store.event(run, event, data)
        dry_run = record["mode"] == "dry-run"
        while True:
            steps = self.store.steps(run)
            unfinished = [s for s in steps if s["state"] != "done"]
            if unfinished:
                step = unfinished[0]
            else:
                if len(steps) >= record["budget"]:
                    self.store.finish(run, "budget_exhausted", "Step budget exhausted; human review required")
                    return "budget_exhausted"
                history = [{"step": s["n"], "decision": json.loads(s["decision"]),
                            "observation": json.loads(s["result"])} for s in steps]
                decision = None
                for attempt in range(self.retries):
                    try:
                        candidate = self.model.decide(record["ticket"], history, json.loads(record["memory"]))
                        validate(candidate, DECISION_SCHEMA)
                        decision = candidate
                        break
                    except Exception as error:
                        self.store.event(run, "model_retry", {"attempt": attempt + 1,
                             "error": self.tools.config.redact(error)[:500]})
                        time.sleep(self.backoff * 2**attempt)
                if decision is None:
                    self.store.finish(run, "escalated", "Model unavailable or invalid structured output after retries")
                    return "escalated"
                self.store.event(run, "reflection", decision)
                if decision["kind"] != "act":
                    status = "resolved" if decision["kind"] == "resolve" else "escalated"
                    # A model cannot close a ticket without at least one real observation.
                    if status == "resolved" and not any("error" not in json.loads(s["result"]) for s in steps):
                        status = "escalated"
                        decision["summary"] = "No observed evidence; human verification required"
                    if dry_run:
                        status = "dry_run_complete"
                    cited = [h["ticket_id"] for h in json.loads(record["memory"])]
                    summary = self.tools.config.redact(decision["summary"])
                    if cited:
                        summary += " | Retrieved past tickets: " + ", ".join(cited)
                    self.store.finish(run, status, summary)
                    if status == "resolved":
                        try:
                            Memory(self.store, self.model).remember(run, record["ticket"], summary)
                        except Exception as error:
                            self.store.event(run, "memory_write_failed", {"error": self.tools.config.redact(error)[:300]})
                    self.emit(f"{run}: {status}: {summary}")
                    return status
                step = self.store.add_step(run, decision)
            decision = json.loads(step["decision"])
            action = decision["action"]
            spec = SPECS[action["tool"]]
            self.emit(f"{run} step {step['n']}: {action['tool']} {encoded(action['args'])}")
            if spec.risky and not dry_run:
                approved = self.store.approval(step)
                if approved is None and self.approval:
                    reviewer = self.approval(action)
                    if reviewer:
                        self.store.approve(step, reviewer)
                        approved = True
                if approved is None:
                    self.store.finish(run, "awaiting_approval", "Review the exact pending action with show; approve or deny, then resume")
                    self.emit(f"PAUSED: approve {run} step {step['n']} before executing")
                    return "awaiting_approval"
                if not approved:
                    self.store.finish(run, "denied", "Human denied pending action")
                    return "denied"
            # Recover a process death or timeout before considering another execution.
            if step["state"] in ("running", "uncertain") and spec.mutates and not dry_run:
                try:
                    reconciled = self.tools.reconcile(action, step["key"])
                except ToolError as error:
                    self.store.event(run, "reconcile_failed", {"error": self.tools.config.redact(error)})
                    self.store.finish(run, "needs_review", "Unable to reconcile interrupted action")
                    return "needs_review"
                if reconciled is not None:
                    self.store.state(run, step["n"], "done", reconciled)
                    self.store.event(run, "reconciled", {"step": step["n"], "result": reconciled})
                    self.emit("Recovered interrupted action from observed state; no side effect repeated")
                    continue
                if action["tool"] == "ad_reset":
                    self.store.finish(run, "needs_review", "Interrupted password reset has no marker; human reconciliation required")
                    return "needs_review"
            done = False
            for attempt in range(step["attempts"], self.retries):
                self.store.state(run, step["n"], "running", attempt=True)
                self.store.event(run, "act", {"step": step["n"], "attempt": attempt + 1, "key": step["key"], "action": action})
                try:
                    if fault == f"before:{step['n']}":
                        self.store.event(run, "fault_injected", {"when": "before", "step": step["n"]})
                        os._exit(86)
                    if fault == f"transient:{step['n']}" and attempt == 0:
                        raise ToolError("Injected transient failure before tool execution")
                    result = self.tools.execute(action, step["key"], dry_run=dry_run)
                    if fault == f"after:{step['n']}":
                        self.store.event(run, "fault_injected", {"when": "after_effect_before_checkpoint", "step": step["n"]})
                        os._exit(86)
                    self.store.state(run, step["n"], "done", result)
                    self.store.event(run, "observe", {"step": step["n"], "result": result})
                    self.emit("Observed: " + encoded(result)[:600])
                    done = True
                    break
                except UncertainError as error:
                    self.store.state(run, step["n"], "uncertain", {"error": self.tools.config.redact(error)})
                    self.store.finish(run, "needs_review", self.tools.config.redact(error))
                    return "needs_review"
                except ToolError as error:
                    self.store.event(run, "retry", {"step": step["n"], "attempt": attempt + 1,
                                                  "error": self.tools.config.redact(error)})
                    if spec.mutates:
                        try:
                            result = self.tools.reconcile(action, step["key"])
                        except ToolError:
                            result = None
                        if result is not None:
                            self.store.state(run, step["n"], "done", result)
                            done = True
                            break
                    time.sleep(self.backoff * 2**attempt)
            if not done:
                self.store.state(run, step["n"], "done", {"error": "Retries exhausted; choose safe alternate step or escalate"})
                self.store.event(run, "step_failed", {"step": step["n"]})
