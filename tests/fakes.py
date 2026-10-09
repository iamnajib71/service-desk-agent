import json
from pathlib import Path
from desk_agent.config import Config
from desk_agent.schema import validate
from desk_agent.tools import action_schema, SPECS, ToolError


def act(tool, **args):
    return {"kind": "act", "plan": "Inspect, act, verify", "reflection": "Use observed state",
            "action": {"tool": tool, "args": args}}


class FakeModel:
    def __init__(self, config, decisions=None):
        self.config = config
        self.decisions = list(decisions or [])

    def decide(self, ticket, history, memory):
        if self.decisions:
            return self.decisions.pop(0)
        return {"kind": "resolve", "plan": "Verified", "reflection": "Complete", "summary": "Verified fake fix"}

    def embed(self, text):
        return [1.0, 0.0, 0.0]


class FakeTools:
    def __init__(self, root, failures=0):
        self.config = Config(Path(root), Path(root))
        self.file = Path(root) / "effect.json"
        self.log = lambda *a: None
        self.failures = failures
        self.calls = 0

    def reconcile(self, action, key):
        if SPECS[action["tool"]].mutates and self.file.exists():
            return {"verified": True}
        return None

    def execute(self, action, key, *, dry_run=False):
        validate(action, action_schema())
        self.calls += 1
        if dry_run:
            return {"dry_run": True}
        if self.failures:
            self.failures -= 1
            raise ToolError("temporary failure")
        if SPECS[action["tool"]].mutates:
            current = json.loads(self.file.read_text()) if self.file.exists() else {"count": 0}
            current["count"] += 1
            self.file.write_text(json.dumps(current))
        return {"verified": True}

