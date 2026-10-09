import contextlib
import datetime
import hashlib
import json
import sqlite3
from pathlib import Path


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, ticket TEXT, mode TEXT, status TEXT, budget INT,
          memory TEXT, final TEXT DEFAULT '', started TEXT);
        CREATE TABLE IF NOT EXISTS steps (
          run TEXT, n INT, key TEXT UNIQUE, decision TEXT, state TEXT,
          attempts INT DEFAULT 0, result TEXT, PRIMARY KEY(run,n));
        CREATE TABLE IF NOT EXISTS approvals (
          key TEXT PRIMARY KEY, action_hash TEXT, reviewer TEXT, approved INT, at TEXT);
        CREATE TABLE IF NOT EXISTS audit (
          seq INTEGER PRIMARY KEY, run TEXT, event TEXT, data TEXT, at TEXT);
        CREATE TABLE IF NOT EXISTS tickets (
          id TEXT PRIMARY KEY, text TEXT, resolution TEXT, embedding TEXT, model TEXT);
        """)

    def close(self):
        self.db.close()

    def event(self, run, event, data):
        with self.db:
            self.db.execute("INSERT INTO audit(run,event,data,at) VALUES(?,?,?,?)",
                            (run, event, encoded(data), now()))

    def create(self, run, ticket, mode, budget, memory):
        if self.get(run):
            raise ValueError("Run ID exists; use resume")
        with self.db:
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
                            (run, ticket, mode, "active", budget, encoded(memory), "", now()))
        self.event(run, "ticket", {"ticket": ticket, "mode": mode, "budget": budget})

    def get(self, run):
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
        return dict(row) if row else None

    def steps(self, run):
        return [dict(r) for r in self.db.execute("SELECT * FROM steps WHERE run=? ORDER BY n", (run,))]

    def add_step(self, run, decision):
        n = len(self.steps(run)) + 1
        key = digest({"run": run, "n": n, "decision": decision})
        with self.db:
            self.db.execute("INSERT INTO steps(run,n,key,decision,state) VALUES(?,?,?,?,?)",
                            (run, n, key, encoded(decision), "pending"))
        self.event(run, "plan", {"step": n, "key": key, "decision": decision})
        return self.steps(run)[-1]

    def state(self, run, n, state, result=None, attempt=False):
        with self.db:
            self.db.execute("UPDATE steps SET state=?, result=?, attempts=attempts+? WHERE run=? AND n=?",
                            (state, encoded(result) if result is not None else None, int(attempt), run, n))

    def finish(self, run, status, final):
        with self.db:
            self.db.execute("UPDATE runs SET status=?,final=? WHERE id=?", (status, final, run))
        self.event(run, status, {"summary": final})

    def approve(self, step, reviewer, approved=True):
        action = json.loads(step["decision"])["action"]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)",
                            (step["key"], digest(action), reviewer, int(approved), now()))
        self.event(step["run"], "approval", {"key": step["key"], "action": action,
                                            "reviewer": reviewer, "approved": approved})

    def approval(self, step):
        row = self.db.execute("SELECT * FROM approvals WHERE key=?", (step["key"],)).fetchone()
        if row and row["action_hash"] == digest(json.loads(step["decision"])["action"]):
            return bool(row["approved"])
        return None

    @contextlib.contextmanager
    def lease(self, run):
        """OS file lock: released on process death; independent runs may proceed."""
        path = Path(self.db.execute("PRAGMA database_list").fetchone()[2]).parent / (digest(run) + ".lock")
        with path.open("a+b") as handle:
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            try:
                if __import__("os").name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise RuntimeError("This run is already executing") from error
            try:
                yield
            finally:
                if __import__("os").name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def export(self, run):
        return {"run": self.get(run), "steps": self.steps(run),
                "audit": [dict(r) for r in self.db.execute("SELECT * FROM audit WHERE run=? ORDER BY seq", (run,))]}
