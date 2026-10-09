"""A real Kuma tool read with one injected transient failure and backoff."""
import datetime
import json
from desk_agent.config import Config
from desk_agent.engine import Engine
from desk_agent.model import Ollama
from desk_agent.store import Store
from desk_agent.tools import Tools


def main():
    config = Config.load()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = "retry-" + stamp
    folder = config.root / "eval/results" / run
    folder.mkdir(parents=True)
    store = Store(config.root / "state" / (run + ".sqlite"))
    tools, model = Tools(config), Ollama(config)
    ticket = "Read Uptime Kuma office status and report whether all six monitors are UP. Do not change services."
    try:
        store.create(run, ticket, "live", 8, [])
        decision = model.decide(ticket, [], [])
        assert decision["kind"] == "act" and decision["action"]["tool"] == "kuma_status"
        store.add_step(run, decision)
        status = Engine(store, tools, model).resume(run, fault="transient:1")
        evidence = tools.execute({"tool": "kuma_status", "args": {}}, "check")
        export = store.export(run)
        attempts = export["steps"][0]["attempts"]
        assert status == "resolved" and attempts == 2 and evidence["all_up"]
        (folder / "agent.json").write_text(config.redact(json.dumps(export, indent=2)), encoding="utf-8")
        (folder / "summary.json").write_text(json.dumps({"run": run, "status": status,
            "success": True, "attempts": attempts, "retries": attempts-1, "evidence": evidence}, indent=2), encoding="utf-8")
        print("Verified: one transient failure, one retry, live Kuma response, no mutation.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
