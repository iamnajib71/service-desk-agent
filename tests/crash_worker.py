import sys
from pathlib import Path
from desk_agent.engine import Engine
from desk_agent.store import Store
from tests.fakes import FakeModel, FakeTools

root = Path(sys.argv[1])
tools = FakeTools(root)
store = Store(root / "agent.sqlite")
Engine(store, tools, FakeModel(tools.config), backoff=0).resume("crash", fault=sys.argv[2])

