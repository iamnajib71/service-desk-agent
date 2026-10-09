import argparse
import json
import re
from pathlib import Path
from .config import Config
from .engine import Engine
from .model import Ollama
from .store import Store
from .tools import Tools


def main():
    parser = argparse.ArgumentParser(description="Local homelab service desk agent")
    parser.add_argument("--db", default="state/agent.sqlite")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("run")
    start.add_argument("id")
    start.add_argument("ticket")
    start.add_argument("--budget", type=int, default=12)
    start.add_argument("--dry-run", action="store_true")
    resume = sub.add_parser("resume")
    resume.add_argument("id")
    resume.add_argument("--fault", help="before:N / after:N exits 86; transient:N fails once")
    show = sub.add_parser("show")
    show.add_argument("id")
    for verb in ("approve", "deny"):
        command = sub.add_parser(verb)
        command.add_argument("id")
        command.add_argument("step", type=int)
        command.add_argument("--reviewer", required=True)
    args = parser.parse_args()
    config = Config.load()
    store = Store(args.db)
    try:
        if args.command in ("approve", "deny"):
            steps = store.steps(args.id)
            step = next((s for s in steps if s["n"] == args.step and s["state"] == "pending"), None)
            if step is None:
                parser.error("Only an existing pending step can be approved/denied")
            print("Exact action:", json.loads(step["decision"])["action"])
            store.approve(step, args.reviewer, args.command == "approve")
            print("Recorded. Use resume to continue.")
            return
        if args.command == "show":
            print(json.dumps(store.export(args.id), indent=2))
            return
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.id):
            parser.error("ID must be 1..64 letters/numbers/_/-")
        engine = Engine(store, Tools(config), Ollama(config))
        if args.command == "run":
            status = engine.start(args.id, args.ticket, budget=args.budget, dry_run=args.dry_run)
        else:
            status = engine.resume(args.id, fault=args.fault)
        if status not in ("resolved", "dry_run_complete"):
            raise SystemExit(2)
    finally:
        store.close()


if __name__ == "__main__":
    main()

