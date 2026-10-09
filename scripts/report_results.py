"""Insert real metrics into README; never invent or fill missing results."""
import argparse
import json
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results")
    parser.add_argument("--demo", required=True)
    parser.add_argument("--retry", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = Path(args.results)
    rows = json.loads((folder / "summary.json").read_text())
    assert len(rows) == 20, "Full 20-case evidence required"
    demo = json.loads((Path(args.demo) / "summary.json").read_text())
    retry = json.loads((Path(args.retry) / "summary.json").read_text())
    assert demo["success"] and demo["addgroup_calls"] == 1 and retry["success"] and retry["retries"] == 1
    passed = sum(r["success"] for r in rows)
    repairs = [r for r in rows if r["id"] not in ("18", "19", "20")]
    repairs_passed = sum(r["success"] for r in repairs)
    def rel(path):
        return Path(path).resolve().relative_to(root).as_posix()
    section = (f"Recorded **10 October 2026 (Australia/Sydney)** using `llama3.1:8b` and `nomic-embed-text:latest` against the running homelab. "
               f"**{passed}/20 expected outcomes ({passed/20:.0%})**, including the three safe-escalation cases; "
               f"**{repairs_passed}/17 repair/read outcomes ({repairs_passed/17:.1%})**. "
               f"Total: **{sum(r['steps'] for r in rows)} tool steps**, **{sum(r['retries'] for r in rows)} tool retries**, "
               f"**{sum(r['model_retries'] for r in rows)} model validation retries**, **{sum(r['approvals'] for r in rows)} audited action approvals**. "
               "Those live repairs used the explicitly approved fictional fixture scope, not unattended production approval.\n\n")
    section += (folder / "results.md").read_text().split("\n\n",1)[1]
    section += (f"\n[Full live manifest, independent checks and raw per-scenario logs]({rel(folder)}). "
                "Earlier failures and pilots remain alongside it.\n\n"
                f"**Failure recovery:** [real process-death evidence]({rel(args.demo)}) records exit **{demo['process_exit']}**, "
                f"a recovered Finance repair and exactly **{demo['addgroup_calls']}** `addgroup` write. "
                f"[Live transient-retry evidence]({rel(args.retry)}) records **{retry['attempts']} attempts / {retry['retries']} retry** and a real Kuma response. "
                "The unit suite also kills a subprocess after a durable fake effect and proves the effect counter stays at one.\n")
    path = root / "README.md"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"<!-- evaluation:start -->.*?<!-- evaluation:end -->", "<!-- evaluation:start -->\n" + section + "<!-- evaluation:end -->", text, flags=re.S)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()

