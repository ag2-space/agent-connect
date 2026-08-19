"""The status file, read the way an outside observer reads it.

Nothing here imports the Worker to ask it how it feels. A real `python3 -m
agent_connect` is started, the file at the documented path is read from outside
the process, and the assertions are on what it says — which is the whole claim:
a Worker's state is its own to report, and an observer needs no transport
knowledge and no cooperation from anything but the file.

Including the case that matters most. A Worker killed with `SIGKILL` writes
nothing on the way out, by definition, and the only thing left to notice it with
is the freshness the file already promised. So one of these runs really is
killed, and its abandoned file really is put through the staleness rule.

Run: python3 tests/test_worker_status.py   (no dependencies — the `ollama`
adapter is a shim and nothing here contacts it)
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent_connect.status import (
    DEFAULT_HEARTBEAT,
    ERROR,
    SERVING,
    STALE_AFTER,
    STARTING,
    STOPPED,
    StatusFile,
    heartbeat_seconds,
    is_stale,
    status_path,
)

ROOT = _bootstrap.ROOT

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


tmp = Path(tempfile.mkdtemp())
_runs = 0


class Worker:
    """A real Worker process, and the status file it left behind."""

    def __init__(self, adapter="ollama", heartbeat="0.2", status=None, **env):
        global _runs
        _runs += 1
        self.dir = tmp / f"run-{_runs}"
        (self.dir / "ws").mkdir(parents=True)
        self.workspace_status = self.dir / "ws" / "status.json"
        self.status = Path(status) if status else self.workspace_status
        self.log = self.dir / "out.log"
        child = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.dir),
            "PYTHONUNBUFFERED": "1",
            "AGENT_CONNECT_WORKSPACE": str(self.dir / "ws"),
            "AGENT_CONNECT_REPO": str(self.dir),
            "AGENT_CONNECT_POLL": "0.05",
            "AGENT_CONNECT_STATUS_HEARTBEAT": heartbeat,
        }
        if adapter:
            child["AGENT_CONNECT_ADAPTER"] = adapter
        if status:
            child["AGENT_CONNECT_STATUS_FILE"] = str(status)
        child.update(env)
        self._sink = open(self.log, "w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "agent_connect"],
            cwd=str(ROOT), env=child, stdout=self._sink, stderr=subprocess.STDOUT,
        )

    def doc(self):
        try:
            return json.loads(self.status.read_text())
        except (OSError, ValueError):
            return {}

    def wait_for(self, state, timeout=20.0):
        """The document, once it says `state` — or whatever it says at the end."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            doc = self.doc()
            if doc.get("state") == state:
                return doc
            if self.proc.poll() is not None and doc.get("state") not in (None, STARTING):
                return doc
            time.sleep(0.05)
        return self.doc()

    def stop(self, sig=signal.SIGTERM):
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        self.proc.wait(timeout=20)
        self._sink.close()
        return self.doc()


print("\n-- the file appears on startup, and says what is running --")

worker = Worker()
doc = worker.wait_for(SERVING)
check(worker.status.exists(), "the status file is at <workspace>/status.json")
check(doc.get("state") == SERVING, "and reaches 'serving' without being asked anything")
check(doc.get("adapter") == "ollama", "naming the adapter that is running")
check(doc.get("pid") == worker.proc.pid, "and the process that is running it")
check(doc.get("repo") == str(worker.dir), "and the directory it works in")
check(doc.get("version") == 1 and doc.get("last_error") is None,
      "a document version, and no error to report")
check(isinstance(doc.get("started_at"), float) and isinstance(doc.get("updated_at"), float),
      "two clocks: when it started, and when it last said so")


print("\n-- and keeps saying so, on its own, while it serves --")

first = doc["updated_at"]
time.sleep(0.8)
later = worker.doc()
check(later["updated_at"] > first,
      "the file is refreshed while the Worker sits there with nothing to do — "
      "'still alive' is not something a busy Worker gets to prove by accident")
check(later["heartbeat_seconds"] == 0.2,
      "and it states its own refresh interval, so an observer needs nothing out "
      "of band to know what fresh means")
check(not is_stale(later), "a Worker that is serving is not stale")


print("\n-- stopping on purpose is said out loud --")

doc = worker.stop(signal.SIGTERM)
check(doc.get("state") == STOPPED,
      "SIGTERM — how a service manager stops a Worker — leaves 'stopped', so an "
      "observer does not have to wait out the staleness window for news it "
      "could have been told")
check(doc.get("detail") == "terminated", "with which kind of stop it was")


print("\n-- a Worker that was killed is detectable from the file alone --")

worker = Worker()
doc = worker.wait_for(SERVING)
worker.stop(signal.SIGKILL)
abandoned = worker.doc()
check(abandoned.get("state") == SERVING,
      "a killed Worker leaves the file exactly as it was: it never got to write "
      "anything, which is what makes this the case staleness exists for")
fresh_at = abandoned["updated_at"] + abandoned["heartbeat_seconds"]
dead_at = abandoned["updated_at"] + STALE_AFTER * abandoned["heartbeat_seconds"] + 0.01
check(not is_stale(abandoned, now=fresh_at),
      "one missed heartbeat is a busy machine, and not yet a verdict")
check(is_stale(abandoned, now=dead_at),
      "three is nobody home — and the file said so itself, with no help from a "
      "process table, a task queue or the relay")
check(is_stale({}) and is_stale({"state": SERVING}),
      "a document with no clock in it is not a Worker reporting badly; it is not "
      "a status document, and unknown is never alive")


print("\n-- a Worker that cannot start says why, at the same path --")

worker = Worker(adapter="nosuchadapter")
doc = worker.wait_for(ERROR)
check(doc.get("state") == ERROR,
      "an adapter that does not exist is an error state, not a missing file")
check("nosuchadapter" in (doc.get("detail") or ""),
      "carrying the sentence the operator has to act on")
check((doc.get("last_error") or {}).get("message", "").find("nosuchadapter") >= 0
      and isinstance((doc.get("last_error") or {}).get("at"), float),
      "and keeping it as `last_error`, with when it happened")
worker.stop()

worker = Worker(adapter=None)
doc = worker.wait_for(ERROR)
check(doc.get("state") == ERROR and "AGENT_CONNECT_ADAPTER" in (doc.get("detail") or ""),
      "a Worker started with nothing configured leaves the same kind of file")
worker.stop()


print("\n-- the path and the interval are settings, with documented defaults --")

named = tmp / "somewhere" / "else.json"
worker = Worker(status=named)
doc = worker.wait_for(SERVING)
check(named.exists() and doc.get("state") == SERVING,
      "AGENT_CONNECT_STATUS_FILE moves the file, and creates the directory for it")
check(not worker.workspace_status.exists(),
      "and only there — nothing is left behind in the workspace")
worker.stop()

check(status_path({"AGENT_CONNECT_WORKSPACE": "/ws"}) == Path("/ws/status.json"),
      "the default path is <workspace>/status.json")
check(heartbeat_seconds({}) == DEFAULT_HEARTBEAT
      and heartbeat_seconds({"AGENT_CONNECT_STATUS_HEARTBEAT": "nonsense"}) == DEFAULT_HEARTBEAT
      and heartbeat_seconds({"AGENT_CONNECT_STATUS_HEARTBEAT": "-1"}) == DEFAULT_HEARTBEAT,
      "and an unreadable or impossible interval falls back to the documented one")


print("\n-- writing it can fail, and a Task never pays for that --")

clock = [1000.0]
blocked = tmp / "a-file"
blocked.write_text("not a directory\n")
status = StatusFile(blocked / "status.json", 1.0, lambda: clock[0])
status.starting()
status.serving()
check(True, "a status file that cannot be written raises nothing at all — an "
            "observer falls back on staleness, and no Task pays for it")

path = tmp / "beat" / "status.json"
status = StatusFile(path, heartbeat=10.0, clock=lambda: clock[0])
status.serving(detail="one")
written = json.loads(path.read_text())["updated_at"]
clock[0] += 1.0
status.beat(tasks_running=3)
check(json.loads(path.read_text())["updated_at"] == written,
      "a beat inside the heartbeat window does not rewrite the file — the serve "
      "loop spins far more often than anything needs to read it")
clock[0] += 20.0
status.beat(tasks_running=3)
after = json.loads(path.read_text())
check(after["updated_at"] == clock[0] and after["tasks_running"] == 3,
      "and a beat past it writes, carrying whatever changed in the meantime")
check(after["detail"] == "one" and after["state"] == SERVING,
      "without restating — or losing — the facts that did not change")

print("\n" + ("PASS — the status file green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
