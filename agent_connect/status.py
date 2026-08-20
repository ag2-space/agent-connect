"""The status file: a Worker's state is its own to report.

Anything watching a Worker — the desktop supervisor's badge first, an operator's
`cat` second — needs to know three things: what it is doing, what went wrong
last, and whether it is still alive. None of those are answerable from outside.
A process table says a PID exists, not that it is serving; a task queue says
work arrived, not that anything is reading it; and asking the relay would make
every observer a client of the transport, which is exactly the coupling that
would have to be rebuilt the next time the transport changes.

So each Worker writes a small JSON file at a documented path and keeps it
current. The file is the contract; the transport underneath it is not.

## The file

`<workspace>/status.json`, or wherever `AGENT_CONNECT_STATUS_FILE` says.

    {
      "version": 1,
      "state": "serving",              // starting | serving | stopped | error
      "detail": "adapter=acp repo=/Users/me/agents",
      "pid": 4711,
      "adapter": "acp",
      "agent": "acp: Claude Code 2.1.0",
      "repo": "/Users/me/agents",
      "workspace": "/Users/me/.agent-connect/workspace",
      "instance": "scratch",           // AGENT_CONNECT_INSTANCE, or ""
      "tasks_running": 0,
      "oldest_task_seconds": 0.0,      // age of the longest-running Turn
      "started_at": 1755600000.0,      // unix seconds, this process's start
      "updated_at": 1755600123.0,      // unix seconds, this write
      "uptime_seconds": 123.0,         // monotonic, immune to clock steps
      "heartbeat_seconds": 15.0,
      "last_error": {"at": 1755600100.0, "message": "..."} | null,
      "relay": {                       // the transport, or null before it starts
        "state": "connected",          // connected | reconnecting | auth-wait
                                       // | fatal | stopped
        "connected": true,
        "gateway": "https://chat.ag2.space/relay",   // redacted by the library
        "last_ok_ts": 1755600120.0,    // unix seconds of the last healthy poll
        "backoff_s": 0.0,
        "error": null,
        "inflight": 0,                 // Tasks accepted and not yet answered
        "pending_results": 0,          // answers written and not yet POSTed
        "updated_ts": 1755600122.0     // the LIBRARY's clock, not this file's
      }
    }

## The file is layered, and the layers are two different facts

`state`, `adapter`, `agent` and `repo` are the Worker's own account of itself:
what is configured, and whether the Local Agent behind the Agent Identity
answered its preflight. `relay` is the transport's, copied out of the Relay
Client's status snapshot through the change hook it offers — the library also
writes its own connection-only file under its state dir, so observability
survives a consumer that never reads the hook, and this block is the richer
composition the spec asks a consumer to make. It is **not** an impersonation of
anybody else's status schema.

**The relay block never beats.** `updated_at` is refreshed by the Worker's own
heartbeat task and by nothing else, so it goes on meaning exactly one thing: the
Worker's event loop is turning. The status hook runs on the library's poll
thread, and a poll thread that is alive proves nothing about an event loop that
is wedged — letting it refresh `updated_at` would be the third way found to
defeat staleness, after `AGENT_CONNECT_POLL` and the missing beat before
`serving`. So the hook rewrites the document with the new connection facts and
leaves both clocks exactly where the last beat left them. The block carries the
library's own `updated_ts` instead, so a reader who wants to know whether the
*transport's* view is current has it without borrowing this file's clock.

**One file per instance.** The path hangs off the workspace, and a workspace
belongs to exactly one Worker (`README.md` § Running more than one agent on one
machine), so N instances write N status files without being told to. `instance`
carries `AGENT_CONNECT_INSTANCE` — the name whoever started this Worker gave it
— so a supervisor can match its own N rows to the N files it is reading without
having to recognise a workspace path.

**Freshness is stated, not assumed.** `updated_at` is refreshed every
`heartbeat_seconds / 2` while the Worker is running, and the file carries that
interval so an observer needs no out-of-band knowledge of it: a Worker is
**stale** when `now - updated_at > 3 x heartbeat_seconds`. That is `is_stale()`,
and the slack is deliberate — one missed write is a busy machine, six is a
Worker that is not there. A `kill -9`, a panic, a lid closed on a laptop: none
of them get to write "stopped", and all of them are visible as staleness. A
Worker that stopped on purpose says so, so the ordinary case does not have to be
waited out.

The heartbeat is its own task and owes nothing to any other setting. It was
briefly driven by the Task-scanning loop, which made `AGENT_CONNECT_POLL` able
to defeat staleness — a poll longer than three heartbeats left a *healthy*
Worker permanently stale. Freshness must not be something another setting can
switch off, so nothing else paces it. It also starts before preflight, because
an ACP bridge that takes forty-five seconds to answer `initialize` is a Worker
that is starting, not one that is dead.

**What it proves, and what it does not.** A fresh file means the process is
alive and its event loop is turning. It does **not** mean any Turn is making
progress: Turns run as separate tasks, so a Worker whose every Turn is wedged on
an unresponsive Local Agent keeps beating `serving` quite happily. That is why
`tasks_running` and `oldest_task_seconds` are in the document — an observer that
wants to know whether *work* is moving reads those, and a single Turn that has
been running for two hours is visible as exactly that. Detecting a wedged Local
Agent properly belongs to the Turn deadline (`AGENT_CONNECT_TURN_TIMEOUT`),
which cancels it; this file reports, it does not adjudicate.

**The clock is the wall clock, and that has a cost.** `updated_at` has to be
comparable across processes, so it is `time.time()`. If the system clock steps
*backwards* — an NTP correction after a long sleep — a dead Worker's last write
can sit in the future and read as fresh until real time catches up.
`uptime_seconds` is monotonic and is written beside it, so a reader that cares
can notice a document whose uptime stopped advancing. No attempt is made to
paper over the step: a status file that lied about which clock it used would be
worse than one that says plainly which one it is.

**Never fatal.** A status file that cannot be written is an observer that has to
fall back on staleness — annoying, and not worth failing a person's request
over. Every I/O error here is reported once, on stderr, and swallowed. This
module is the same shape as `agent_connect.sessions` for that reason.

**Written whole, or not at all.** Every write goes to a temporary file beside
the target and is renamed over it, so a reader never catches half a document.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Optional

STATUS_ENV = "AGENT_CONNECT_STATUS_FILE"
HEARTBEAT_ENV = "AGENT_CONNECT_STATUS_HEARTBEAT"
INSTANCE_ENV = "AGENT_CONNECT_INSTANCE"

#: The file, under the workspace the Worker already has.
STATUS_NAME = "status.json"

#: How often `updated_at` is refreshed while serving. Fifteen seconds: often
#: enough that a badge stops lying within half a minute, rare enough that an
#: idle Worker is not writing to disk all day.
DEFAULT_HEARTBEAT = 15.0

#: Heartbeat intervals before an observer should call a Worker dead. Writes go
#: out twice per interval, so this is six missed writes: one is a busy machine,
#: six is nobody home.
STALE_AFTER = 3

#: The states, and the whole of them — `_write` accepts nothing else, so the
#: comment cannot quietly stop being true.
STARTING = "starting"
SERVING = "serving"
STOPPED = "stopped"
ERROR = "error"
STATES = frozenset({STARTING, SERVING, STOPPED, ERROR})

#: Bumped only for a change a reader could not survive. Readers should ignore
#: fields they do not know rather than refuse the document.
VERSION = 1

#: What the Worker copies out of the Relay Client's status snapshot, and the
#: whole of it. A projection rather than the snapshot itself: this file is a
#: service contract with an outside reader, and a block that silently gained
#: whatever the library added next would be a contract nobody wrote down. A
#: field the library grows and this file should carry is a line added here.
RELAY_FIELDS = ("state", "connected", "gateway", "last_ok_ts", "backoff_s",
                "error", "inflight", "pending_results", "updated_ts")


def status_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """The status file: named outright, or under the workspace."""
    from .sessions import workspace_dir  # local: sessions owns "the workspace"

    env = os.environ if env is None else env
    explicit = (env.get(STATUS_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return workspace_dir(env) / STATUS_NAME


def instance_name(env: Optional[Mapping[str, str]] = None) -> str:
    """What whoever started this Worker calls it, or "".

    Read here and nowhere else: it exists so that an outside supervisor can map
    N status files onto its own N rows without having to recognise a workspace
    path it computed itself.
    """
    env = os.environ if env is None else env
    return (env.get(INSTANCE_ENV) or "").strip()


def heartbeat_seconds(env: Optional[Mapping[str, str]] = None) -> float:
    """How often the file promises to be refreshed."""
    env = os.environ if env is None else env
    try:
        value = float((env.get(HEARTBEAT_ENV) or "").strip())
    except ValueError:
        return DEFAULT_HEARTBEAT
    return value if value > 0 else DEFAULT_HEARTBEAT


def read(path: Path) -> dict:
    """One status document, or `{}` — a file that is not there says nothing."""
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def is_stale(doc: Mapping, now: Optional[float] = None) -> bool:
    """Whether this document describes a Worker nobody has heard from.

    The rule an outside observer applies, written here so that every observer
    applies the same one. A document with no `updated_at` is not a Worker
    reporting badly — it is not a status document, and unknown is not alive.
    """
    updated = doc.get("updated_at")
    if not isinstance(updated, (int, float)):
        return True
    beat = doc.get("heartbeat_seconds") or DEFAULT_HEARTBEAT
    now = time.time() if now is None else now
    return (now - updated) > STALE_AFTER * beat


class StatusFile:
    """The Worker's own account of itself, kept at a path.

    Construct it once at startup and call the transition methods; `beat` is the
    one that may be called as often as the caller likes, because it is throttled
    to the heartbeat and does nothing in between.
    """

    def __init__(
        self,
        path: Path,
        heartbeat: float = DEFAULT_HEARTBEAT,
        clock=time.time,
        monotonic=time.monotonic,
    ):
        self.path = Path(path)
        self.heartbeat = heartbeat if heartbeat > 0 else DEFAULT_HEARTBEAT
        self._clock = clock
        #: Beside the wall clock, and for the reason in the module docstring: a
        #: clock that steps backwards can make a corpse look fresh, and an
        #: uptime that stopped advancing cannot.
        self._monotonic = monotonic
        self._started = clock()
        self._booted = monotonic()
        self._last_write = 0.0
        self._complained = False
        #: The document is written from two threads now: the Worker's event
        #: loop, and the Relay Client's poll thread through the status hook.
        #: The temporary file is named after the pid, so two writers without a
        #: lock would race each other for the same name and one would rename a
        #: half-written document over the real one — the exact failure the
        #: write-beside-and-rename dance exists to prevent.
        self._lock = threading.RLock()
        #: Everything the file says, kept so a transition need not restate the
        #: facts that have not changed.
        self._doc: dict = {
            "version": VERSION,
            "state": STARTING,
            "detail": "",
            "pid": os.getpid(),
            "started_at": self._started,
            "uptime_seconds": 0.0,
            "heartbeat_seconds": self.heartbeat,
            "instance": "",
            "tasks_running": 0,
            "oldest_task_seconds": 0.0,
            "last_error": None,
            #: Null until the transport is constructed. "No relay block" and "a
            #: relay block saying reconnecting" are different facts, and a
            #: reader that could not tell them apart would report a Worker with
            #: no transport at all as one that is merely offline.
            "relay": None,
        }

    # -- transitions --------------------------------------------------------

    def starting(self, **facts) -> None:
        """Before anything can go wrong — so a Worker that dies in preflight
        still left a file saying it tried."""
        self._write(STARTING, **facts)

    def serving(self, **facts) -> None:
        """Serving Tasks. The state a healthy Worker sits in."""
        self._write(SERVING, **facts)

    def stopped(self, detail: str = "", **facts) -> None:
        """Stopped on purpose. An observer need not wait out the staleness
        window to find out what it already could have been told."""
        self._write(STOPPED, detail=detail, **facts)

    def error(self, message: str, **facts) -> None:
        """Stopped because of this. The message is the operator's to act on, so
        it is kept verbatim and also recorded as `last_error`."""
        with self._lock:
            self._doc["last_error"] = {"at": self._clock(), "message": str(message)}
            self._write(ERROR, detail=str(message), **facts)

    def relay(self, snapshot: Optional[Mapping] = None) -> None:
        """The transport's connection state, as the library just reported it.

        Written for `RelayClient.on_status`, and called from the library's poll
        thread — so it says nothing about the Worker's own state and does not
        touch either clock (see the module docstring: a live poll thread is not
        a live event loop, and `updated_at` must go on meaning only the second).
        Everything else in the document is left exactly as the last transition
        left it.

        A hook that raised would be logged and forgotten by the library, which
        is a status file that quietly stops updating; `_write` swallows its own
        I/O errors already, and there is nothing else here that can raise.
        """
        block = ({name: snapshot.get(name) for name in RELAY_FIELDS}
                 if snapshot is not None else None)
        with self._lock:
            self._write(self._doc.get("state", STARTING), beat=False, relay=block)

    def beat(self, **facts) -> None:
        """Say the same thing again, so that saying nothing means something.

        Anything in `facts` that changed is written with it. Unthrottled: the
        one caller is the heartbeat task, which paces itself, and a `beat` that
        silently declined to write would be a freshness promise this file could
        not keep.
        """
        self._write(self._doc.get("state", SERVING), **facts)

    async def beating(self, facts=None) -> None:
        """Beat for ever, every half interval. Cancel it to stop.

        Its own task, owing nothing to any other setting — see the module
        docstring: freshness that another setting can switch off is not
        freshness. Started before preflight and cancelled on the way out, so
        the gap between `starting` and `serving` is not a gap in the file.

        `facts` is an optional callable, asked afresh on every beat, for the
        things that are only true right now — how many Turns are running, and
        how long the oldest has been.
        """
        while True:
            await asyncio.sleep(max(self.heartbeat / 2.0, 0.01))
            self.beat(**(facts() if facts is not None else {}))

    # -- internals ----------------------------------------------------------

    def _write(self, state: str, *, beat: bool = True, **facts) -> None:
        # A state outside the four is a programming error, not an operator's
        # problem, and it must not reach a reader that switches on the value.
        if state not in STATES:
            raise ValueError(f"not a status state: {state!r}")
        with self._lock:
            self._doc.update(facts)
            self._doc["state"] = state
            if beat:
                # The two clocks advance together, and only for a writer that
                # is the Worker itself. `beat=False` is the status hook, which
                # runs on the transport's thread and has nothing to say about
                # whether this process's event loop is still turning.
                self._doc["updated_at"] = self._clock()
                self._doc["uptime_seconds"] = self._monotonic() - self._booted
                self._last_write = self._doc["updated_at"]
            document = json.dumps(self._doc, indent=2, sort_keys=True) + "\n"
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(document)
                os.replace(tmp, self.path)
            except OSError as exc:  # noqa: BLE001 — an observer's problem, not a Task's
                if not self._complained:
                    self._complained = True
                    print(f"agent-connect: cannot write the status file {self.path}: {exc}",
                          file=sys.stderr, flush=True)
                try:
                    tmp.unlink()
                except OSError:
                    pass


def from_env(env: Optional[Mapping[str, str]] = None) -> StatusFile:
    """The status file this Worker owns, at the documented path."""
    env = os.environ if env is None else env
    status = StatusFile(status_path(env), heartbeat_seconds(env))
    status._doc["instance"] = instance_name(env)
    return status
