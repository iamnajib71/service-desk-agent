"""Fail publication if known private credentials or private-key patterns are tracked."""
from pathlib import Path
import re
import subprocess
from desk_agent.config import Config

config = Config.load()
paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
bad = []
secrets = [v for k,v in config.secrets().items() if v and re.search(r"PASSWORD|HASH|TOKEN|KEY|RESET_", k)]
for name in filter(None, paths):
    if Path(name).name in (".env", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md"):
        bad.append(name)
    raw = Path(name).read_bytes()
    if any(value.encode() in raw for value in secrets):
        bad.append(name + ": known credential")
    if re.search(rb"(?m)^(PrivateKey|PresharedKey)\s*=\s*[A-Za-z0-9+/]{43}=", raw):
        bad.append(name + ": WireGuard key")
if bad:
    raise SystemExit("Secret scan failed: " + ", ".join(bad))
print(f"Secret scan PASS: {len(list(filter(None, paths)))} tracked files")
