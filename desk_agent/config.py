from dataclasses import dataclass
from pathlib import Path
import os
import re


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {k: v.strip().strip("\"'").replace("$$", "$")
            for k, v in re.findall(r"^(\w+)=(.*)$", path.read_text(encoding="utf-8-sig"), re.M)}


@dataclass
class Config:
    root: Path
    homelab: Path
    model: str = "llama3.1:8b"
    embed_model: str = "nomic-embed-text:latest"
    ollama_url: str = "http://127.0.0.1:11434"
    timeout: int = 30

    @classmethod
    def load(cls, root=None):
        root = Path(root or os.getcwd()).resolve()
        env = read_env(root / ".env")
        return cls(root, Path(env.get("HOMELAB_ROOT", str(root.parent / "it-support-homelab"))),
                   env.get("OLLAMA_MODEL", "llama3.1:8b"),
                   env.get("EMBED_MODEL", "nomic-embed-text:latest"),
                   env.get("OLLAMA_URL", "http://127.0.0.1:11434"))

    def secrets(self):
        return read_env(self.homelab / ".env") | read_env(self.root / ".env")

    def redact(self, text):
        text = str(text)
        for key, value in self.secrets().items():
            if value and re.search(r"PASSWORD|HASH|TOKEN|KEY|RESET_", key):
                text = text.replace(value, "[REDACTED]")
        return re.sub(r"(?im)^((?:PrivateKey|PresharedKey)\s*=\s*).+$", r"\1[REDACTED]", text)

