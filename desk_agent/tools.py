"""Typed allow-listed adapters to the exact homelab Compose services and v14 API."""
from dataclasses import dataclass
import http.cookiejar
import json
import re
import secrets
import subprocess
import urllib.error
import urllib.request
from .schema import USER, TEMP_USER, PEER, obj, choice, validate


@dataclass(frozen=True)
class Spec:
    schema: dict
    mutates: bool = False
    risky: bool = False


BOOL = {"type": "boolean", "description": "DESIRED future membership: true means ADD, false means REMOVE. Not the observed current membership."}
QUEUE = choice("Office-PDF", "Office-Laser")
GROUP = choice("GG-Staff", "GG-Finance")
SPECS = {
    "ad_lookup": Spec(obj(user=USER)),
    "ad_groups": Spec(obj(user=USER)),
    "ad_unlock": Spec(obj(user=USER), True),
    "ad_reset": Spec(obj(user=TEMP_USER), True, True),
    "ad_group": Spec(obj(user=USER, group=GROUP, present=BOOL), True, True),
    "smb_group": Spec(obj(user=choice("bob"), group=choice("finance"), present=BOOL), True, True),
    "smb_groups": Spec(obj(user=choice("alice", "bob"))),
    "share_check": Spec(obj(user=choice("alice", "bob"), share=choice("Public", "Finance", "Scans"))),
    "cups_list": Spec(obj()),
    "cups_jobs": Spec(obj(queue=QUEUE)),
    "cups_resume": Spec(obj(queue=QUEUE), True),
    "cups_cancel": Spec(obj(job={"type": "string", "pattern": r"Office-(?:PDF|Laser)-[1-9][0-9]{0,7}"}), True, True),
    "wg_list": Spec(obj()),
    "wg_create": Spec(obj(name=PEER), True, True),
    "wg_revoke": Spec(obj(name=PEER), True, True),
    "kuma_status": Spec(obj()),
}


def action_schema():
    return {"oneOf": [obj(tool=choice(name), args=spec.schema) for name, spec in SPECS.items()]}


class ToolError(RuntimeError):
    pass


class UncertainError(ToolError):
    """An ambiguous side effect requires a human reconciliation."""


class Tools:
    def __init__(self, config, log=None):
        self.config = config
        self.log = log or (lambda event, data: None)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def docker(self, service, args, *, input=None, allow_fail=False):
        if service not in {"directory", "fileserver", "printserver"}:
            raise ValueError("Service outside allow-list")
        command = ["docker", "compose", "exec", "-T", service, *args]
        try:
            proc = subprocess.run(command, cwd=self.config.homelab, input=input,
                                  capture_output=True, text=True, timeout=self.config.timeout)
        except subprocess.TimeoutExpired as error:
            self.log("command_timeout", {"service": service, "command": self.config.redact(command)})
            raise ToolError("Docker command timed out; reconcile before retry") from error
        output = self.config.redact((proc.stdout + proc.stderr).strip())
        self.log("command", {"service": service, "command": self.config.redact(command),
                             "returncode": proc.returncode, "output": output})
        if proc.returncode and not allow_fail:
            raise ToolError(output or "Docker command failed")
        return proc.returncode, output

    def wg(self, path="wireguard/client", method="GET", data=None):
        def request(endpoint, verb, body):
            req = urllib.request.Request("http://127.0.0.1:51821/api/" + endpoint,
                      data=json.dumps(body).encode() if body is not None else None,
                      method=verb, headers={"Content-Type": "application/json"})
            with self.opener.open(req, timeout=self.config.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        try:
            request("session", "POST", {"password": self.config.secrets()["WG_ADMIN_PASSWORD"]})
            result = request(path, method, data)
        except (urllib.error.URLError, TimeoutError) as error:
            raise ToolError("WireGuard API unavailable: " + self.config.redact(error)) from error
        # Never retain configurations, public/private keys, session cookies or preshared keys.
        if isinstance(result, list):
            result = [{k: peer[k] for k in ("id", "name", "address", "enabled") if k in peer}
                      for peer in result]
        self.log("http", {"service": "wg-easy", "path": path, "method": method,
                          "data": data, "result": result})
        return result

    def _lookup(self, user):
        code, raw = self.docker("directory", ["samba-tool", "user", "show", user], allow_fail=True)
        if code:
            if "Unable to find user" in raw or "not found" in raw:
                return {"exists": False, "user": user}
            raise ToolError(raw)
        attrs = dict(re.findall(r"^(\w+): (.*)$", raw, re.M))
        return {"exists": True, "user": user, "lockoutTime": attrs.get("lockoutTime", "0"),
                "pwdLastSet": attrs.get("pwdLastSet", ""),
                "disabled": bool(int(attrs.get("userAccountControl", "0")) & 2)}

    def _ad_members(self, group):
        return self.docker("directory", ["samba-tool", "group", "listmembers", group])[1].splitlines()

    def _smb_groups(self, user):
        raw = self.docker("fileserver", ["id", "-Gn", user])[1]
        return raw.split()

    def _reset_marker(self, key):
        return self.docker("directory", ["sh", "-c",
             'test -f "/var/lib/samba/service-desk-agent/$1.done"', "sh", key], allow_fail=True)[0] == 0

    def reconcile(self, action, key):
        """Returns observed desired state or None. Never writes during reconciliation."""
        validate(action, action_schema())
        name, a = action["tool"], action["args"]
        if name == "ad_unlock":
            value = self._lookup(a["user"])
            if value["exists"] and value["lockoutTime"] == "0":
                return value
        elif name == "ad_reset":
            if self._reset_marker(key):
                value = self._lookup(a["user"])
                if value["pwdLastSet"] == "0":
                    return value | {"reset_marker": True}
                raise UncertainError("Reset marker exists but user has since changed password; review required")
        elif name == "ad_group":
            if (a["user"] in self._ad_members(a["group"])) == a["present"]:
                return a | {"verified": True}
        elif name == "smb_group":
            if (a["group"] in self._smb_groups(a["user"])) == a["present"]:
                return a | {"verified": True}
        elif name in ("wg_create", "wg_revoke"):
            peers = [p for p in self.wg() if p["name"] == a["name"]]
            if len(peers) > 1:
                raise UncertainError("Duplicate peer names; human must identify the device")
            if (name == "wg_create" and peers) or (name == "wg_revoke" and not peers):
                return {"name": a["name"], "exists": bool(peers)}
        elif name == "cups_resume":
            raw = self.docker("printserver", ["lpstat", "-p", a["queue"], "-a", a["queue"]])[1]
            if "disabled" not in raw and "not accepting" not in raw:
                return {"queue": a["queue"], "enabled": True, "accepting": True, "output": raw}
        elif name == "cups_cancel":
            queue = a["job"].rsplit("-", 1)[0]
            raw = self.docker("printserver", ["lpstat", "-W", "not-completed", "-o", queue])[1]
            if not any(line.split()[0] == a["job"] for line in raw.splitlines() if line.split()):
                return {"job": a["job"], "pending": False}
        return None

    def execute(self, action, key, *, dry_run=False):
        validate(action, action_schema())
        name, a = action["tool"], action["args"]
        if dry_run:
            return {"dry_run": True, "tool": name, "args": a, "mutates": SPECS[name].mutates}
        if SPECS[name].mutates:
            reconciled = self.reconcile(action, key)
            if reconciled is not None:
                return reconciled | {"already_satisfied": True}
        if name == "ad_lookup":
            return self._lookup(a["user"])
        if name == "ad_groups":
            return {"user": a["user"], "groups": [g for g in ("GG-Staff", "GG-Finance")
                                                     if a["user"] in self._ad_members(g)]}
        if name == "ad_unlock":
            self.docker("directory", ["samba-tool", "user", "unlock", a["user"]])
        elif name == "ad_reset":
            env_path = self.config.root / ".env"
            secret_key = "RESET_" + key
            private = self.config.secrets()
            if secret_key not in private:
                with env_path.open("a", encoding="utf-8") as file:
                    file.write("\n" + secret_key + "=Aa1!" + secrets.token_hex(20) + "\n")
            password = self.config.secrets()[secret_key]
            # Secret goes through stdin, never process arguments or model context.
            script = ('set -eu; IFS= read -r password; '
                      'mkdir -p /var/lib/samba/service-desk-agent; '
                      'samba-tool user setpassword "$1" --newpassword="$password" --must-change-at-next-login; '
                      'touch "/var/lib/samba/service-desk-agent/$2.done"')
            try:
                self.docker("directory", ["sh", "-c", script, "sh", a["user"], key], input=password + "\n")
            except ToolError as error:
                raise UncertainError("Password reset outcome uncertain; inspect remote marker before any retry") from error
        elif name == "ad_group":
            self.docker("directory", ["samba-tool", "group", "addmembers" if a["present"] else "removemembers",
                                       a["group"], a["user"]])
        elif name == "smb_groups":
            return {"user": a["user"], "groups": self._smb_groups(a["user"])}
        elif name == "smb_group":
            self.docker("fileserver", ["addgroup" if a["present"] else "delgroup", a["user"], a["group"]])
            self.docker("fileserver", ["net", "cache", "flush"])
        elif name == "share_check":
            # Uses the homelab container's secret environment exactly like /lab-check.sh.
            script = 'export PASSWD="$SMB_' + a["user"].upper() + '_PASSWORD"; exec smbclient "$1" -U "$2" -m SMB3 -c ls'
            code, raw = self.docker("fileserver", ["sh", "-c", script, "sh", "//fileserver/" + a["share"], a["user"]], allow_fail=True)
            if code and "NT_STATUS_ACCESS_DENIED" not in raw:
                raise ToolError(raw)
            return {"user": a["user"], "share": a["share"], "accessible": code == 0, "output": raw}
        elif name == "cups_list":
            return {"output": self.docker("printserver", ["lpstat", "-p", "-a"])[1]}
        elif name == "cups_jobs":
            return {"queue": a["queue"], "output": self.docker("printserver", ["lpstat", "-W", "not-completed", "-o", a["queue"]])[1]}
        elif name == "cups_resume":
            self.docker("printserver", ["cupsenable", a["queue"]])
            self.docker("printserver", ["cupsaccept", a["queue"]])
        elif name == "cups_cancel":
            self.docker("printserver", ["cancel", a["job"]])
        elif name == "wg_list":
            return {"peers": self.wg()}
        elif name == "wg_create":
            self.wg(method="POST", data={"name": a["name"]})
        elif name == "wg_revoke":
            peer = next(p for p in self.wg() if p["name"] == a["name"])
            self.wg("wireguard/client/" + peer["id"], "DELETE")
        elif name == "kuma_status":
            try:
                with urllib.request.urlopen("http://127.0.0.1:13001/api/status-page/heartbeat/office", timeout=self.config.timeout) as response:
                    data = json.load(response)
                monitors = {k: v[-1] for k, v in data["heartbeatList"].items() if v}
                result = {"monitors": monitors, "all_up": bool(monitors) and all(v["status"] == 1 for v in monitors.values())}
                self.log("http", {"service": "kuma", "result": result})
                return result
            except (urllib.error.URLError, TimeoutError) as error:
                raise ToolError("Kuma unavailable") from error
        if SPECS[name].mutates:
            verified = self.reconcile(action, key)
            if verified is None:
                raise ToolError("Postcondition not satisfied")
            return verified
        raise ValueError("Unknown tool")
