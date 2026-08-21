"""The status file, read the way an outside observer reads it.

Nothing here imports the Worker to ask it how it feels. A real `python3 -m
agent_connect` is started, the file at the documented path is read from outside
the process, and the assertions are on what it says — which is the whole claim:
a Worker's state is its own to report, and an observer needs no relay
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

import asyncio
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _taskqueue import CHILD_TOKEN, child_env
from agent_connect.status import (
    DEFAULT_HEARTBEAT,
    ERROR,
    RELAY_FIELDS,
    SERVING,
    STALE_AFTER,
    STARTING,
    STATES,
    STOPPED,
    StatusFile,
    heartbeat_seconds,
    instance_name,
    is_stale,
    status_path,
)
from agent_connect.worker import _preflight

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

    def __init__(self, adapter="ollama", heartbeat="0.2", status=None,
                 script=None, **env):
        global _runs
        _runs += 1
        self.dir = tmp / f"run-{_runs}"
        (self.dir / "ws").mkdir(parents=True)
        self.workspace_status = self.dir / "ws" / "status.json"
        self.status = Path(status) if status else self.workspace_status
        self.log = self.dir / "out.log"
        child = child_env(**{
            "HOME": str(self.dir),
            "AGENT_CONNECT_WORKSPACE": str(self.dir / "ws"),
            "AGENT_CONNECT_REPO": str(self.dir),
            "AGENT_CONNECT_POLL": "0.05",
            "AGENT_CONNECT_STATUS_HEARTBEAT": heartbeat,
        })
        if adapter:
            child["AGENT_CONNECT_ADAPTER"] = adapter
        if status:
            child["AGENT_CONNECT_STATUS_FILE"] = str(status)
        child.update(env)
        self._sink = open(self.log, "w")
        argv = ([sys.executable, "-c", script] if script
                else [sys.executable, "-m", "agent_connect"])
        self.proc = subprocess.Popen(
            argv, cwd=str(ROOT), env=child,
            stdout=self._sink, stderr=subprocess.STDOUT,
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


print("\n-- and it carries the Relay Client's state beside its own --")

# Layered status (transport-seam spec): the Worker's own facts — which Adapter,
# which Local Agent answered preflight — and the connection state read off the
# Relay Client's hook, in one file under one name. This child is pointed at a
# port nothing is listening on, which is the interesting case: an unreachable
# broker is a fact about the Relay Client, and a Worker that is otherwise perfectly
# healthy must not report it as its own death.
relay = worker.wait_for(SERVING).get("relay") or {}
check(set(relay) == set(RELAY_FIELDS),
      "the relay block is exactly the documented projection of the library's "
      "snapshot, not whatever the library happened to be carrying")
check(doc.get("agent") is not None and doc.get("adapter") == "ollama",
      "beside the Local Agent's own health, which is this Worker's to report")
check(relay.get("connected") is False and relay.get("state") == "reconnecting",
      "a broker that cannot be reached reads as reconnecting, not as an error")
check(doc.get("state") == SERVING,
      "— and the Worker itself is still serving, because it is")
check("127.0.0.1:9" in (relay.get("gateway") or ""),
      "the gateway is named, so an operator can see where it is pointed")
check("test-secret" not in json.dumps(doc),
      "and the credential is not, anywhere in the document: the URL is redacted "
      "by the library before it is ever persisted")


def relay_of(w):
    return (w.doc().get("relay") or {})


# The Relay Client's clock and the Worker's are separate on purpose. The hook runs
# on the library's poll thread, and a live poll thread says nothing about a
# wedged event loop — so it must not be able to refresh `updated_at`, which is
# the third way that promise could have been defeated.
before = worker.doc()
deadline = time.monotonic() + 5.0
while time.monotonic() < deadline and relay_of(worker).get("updated_ts") == \
        (before.get("relay") or {}).get("updated_ts"):
    time.sleep(0.05)
check(relay_of(worker).get("updated_ts") != (before.get("relay") or {}).get("updated_ts"),
      "the relay block is refreshed as the connection's state changes")
check("last_ok_ts" in relay_of(worker),
      "carrying 'last connected when', which is the number an operator wants "
      "and the one a naive rewrite drops")

frozen = StatusFile(tmp / "layered" / "status.json", heartbeat=10.0,
                    clock=lambda: 1000.0)
frozen.serving(detail="serving")
stamped = json.loads((tmp / "layered" / "status.json").read_text())["updated_at"]
frozen.relay({name: "changed" for name in RELAY_FIELDS})
after = json.loads((tmp / "layered" / "status.json").read_text())
check(after["relay"]["state"] == "changed" and after["updated_at"] == stamped,
      "a status hook writes the connection through and does NOT beat: freshness "
      "is the Worker's event loop turning, and the library's thread is not it")
check(after["state"] == SERVING,
      "and it says nothing about the Worker's own state, which is not its news")
frozen.relay(None)
check(json.loads((tmp / "layered" / "status.json").read_text())["relay"] is None,
      "no Relay Client at all is null rather than absent — 'no relay' and 'a relay "
      "that is offline' are different facts about a Worker")


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
said = io.StringIO()
raised = ""
try:
    with contextlib.redirect_stderr(said):
        status.starting()
        status.serving()
        status.beat(tasks_running=1)
        status.relay({name: "x" for name in RELAY_FIELDS})
        status.error("something else went wrong too")
except BaseException as exc:  # noqa: BLE001 — the whole claim is that this is unreachable
    raised = f"{type(exc).__name__}: {exc}"
check(not raised,
      f"a status file that cannot be written raises nothing at all — an "
      f"observer falls back on staleness, and no Task pays for it (got {raised!r})")
check(not (blocked / "status.json").exists() and blocked.read_text() == "not a directory\n",
      "nothing is written, and whatever was in the way is left exactly as it was")
check(said.getvalue().count("cannot write the status file") == 1,
      "and it is complained about once rather than once per write — a Worker "
      "whose workspace went away must not fill the log with the same line")

path = tmp / "beat" / "status.json"
status = StatusFile(path, heartbeat=10.0, clock=lambda: clock[0])
status.serving(detail="one")
clock[0] += 1.0
status.beat(tasks_running=3)
after = json.loads(path.read_text())
check(after["updated_at"] == clock[0] and after["tasks_running"] == 3,
      "a beat always writes — its whole job is that saying nothing means "
      "something, and a beat that declined to write would break that promise")
check(after["detail"] == "one" and after["state"] == SERVING,
      "carrying what changed, without restating — or losing — what did not")

try:
    status._write("busy")
    refused = False
except ValueError:
    refused = True
check(refused,
      "a state outside the four is refused: a reader switches on this value, "
      "and 'the states, and the whole of them' has to stay true")
check(STATES == {STARTING, SERVING, STOPPED, ERROR}, "and there are four of them")


print("\n-- freshness is nobody else's setting to defeat --")

# The heartbeat was briefly beaten by the Task-scanning loop, which made a slow
# AGENT_CONNECT_POLL able to make a healthy Worker look dead.
worker = Worker(heartbeat="0.2", **{"AGENT_CONNECT_POLL": "30"})
doc = worker.wait_for(SERVING)
first = doc["updated_at"]
time.sleep(1.0)
later = worker.doc()
check(later["updated_at"] > first,
      "a Worker whose Task scan runs every 30s with a 0.2s heartbeat is still "
      "fresh — no other setting paces this one")
check(not is_stale(later), "and therefore not stale, which is the point")
check(later["uptime_seconds"] > doc["uptime_seconds"],
      "and a monotonic uptime advances beside the wall clock, so a clock that "
      "steps backwards is something a reader can notice")
check("tasks_running" in later and "oldest_task_seconds" in later,
      "the beat carries what only the serve loop knows: how much work is in "
      "flight, and how long the oldest piece of it has been running")
worker.stop()

# The gap between `starting` and `serving` is a real wait — an ACP bridge can
# take most of a minute to answer `initialize` — and it must not read as death.
beats = tmp / "preflight" / "status.json"
status = StatusFile(beats, heartbeat=0.2)
status.starting()
began = json.loads(beats.read_text())["updated_at"]


async def slow_check():
    await asyncio.sleep(0.8)
    return None


asyncio.run(_preflight(lambda: slow_check(), status))
check(json.loads(beats.read_text())["updated_at"] > began,
      "the file keeps beating while the Adapter's preflight runs: a Worker that "
      "is starting slowly is not a Worker that has died")
check(json.loads(beats.read_text())["state"] == STARTING,
      "and it is still `starting` — beating says nothing new about the state")


print("\n-- N workers, N files, and something to tell them apart --")

worker = Worker(**{"AGENT_CONNECT_INSTANCE": "scratch"})
doc = worker.wait_for(SERVING)
check(doc.get("instance") == "scratch",
      "AGENT_CONNECT_INSTANCE is carried into the file, so a supervisor can "
      "match N status files to its own N rows without recognising a path")
check((worker.dir / "ws" / "relay" / "scratch").is_dir(),
      "and it is the same name the relay client's state is kept under")
worker.stop()
check(instance_name({}) == "" and instance_name({"AGENT_CONNECT_INSTANCE": " a "}) == "a",
      "and it is just a name — unset is empty, not an error")

# The document says what the Worker is actually called. An unnamed one used to
# write `""` here while its state sat in `<workspace>/relay/default/`, which a
# supervisor holding `""` cannot map onto anything on disk.
worker = Worker()
doc = worker.wait_for(SERVING)
check(doc.get("instance") == "default",
      "a Worker nobody named says `default` rather than an empty string")
check((worker.dir / "ws" / "relay" / doc.get("instance", "")).is_dir(),
      "which is, again, exactly the directory its relay state lives in")
worker.stop()

# A name outside the grammar is refused rather than mangled — two instances
# quietly sharing one sanitised name would share one journal. It is reported
# *before* the missing credential: a setting that is wrong outranks one that is
# absent, and hearing about the token first cost the operator a second run to
# find out about the name.
worker = Worker(**{"AGENT_CONNECT_INSTANCE": "two words/here",
                   "AGENT_CONNECT_TOKEN": ""})
doc = worker.wait_for(ERROR)
check(doc.get("state") == ERROR and "AGENT_CONNECT_INSTANCE" in (doc.get("detail") or ""),
      "a Worker with a mistyped instance name and no token at all is told "
      "about the name, which is the one that is wrong")
worker.stop()


print("\n-- an ending that is a number is not an explanation --")

BOOM = """
import sys
from agent_connect import adapters, worker


class Boom:
    async def preflight(self):
        sys.exit(3)

    async def turn(self, ctx):        # speaks the event contract, so it is
        yield None                    # selected unwrapped and its preflight runs


adapters.ADAPTERS["boom"] = Boom()
worker.main()
"""
worker = Worker(adapter="boom", script=BOOM)
doc = worker.wait_for(ERROR)
check(doc.get("state") == ERROR and doc.get("detail") == "exited with status 3",
      "`sys.exit(3)` is recorded as an exit status in words — writing `3` into "
      "the field an operator reads for the reason explains nothing")
worker.stop()

worker = Worker(**{"AGENT_CONNECT_POLL": "not-a-number"})
doc = worker.wait_for(ERROR)
check(doc.get("state") == ERROR and "ValueError" in (doc.get("detail") or ""),
      "and a failure nobody anticipated is recorded with its type and message, "
      "rather than leaving the file saying `starting` for ever")
worker.stop()

print("\n" + ("PASS — the status file green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
