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
      "tasks_running": 0,
      "started_at": 1755600000.0,      // unix seconds, this process's start
      "updated_at": 1755600123.0,      // unix seconds, this write
      "heartbeat_seconds": 15.0,
      "last_error": {"at": 1755600100.0, "message": "..."} | null
    }

**Freshness is stated, not assumed.** `updated_at` is refreshed at least every
`heartbeat_seconds` while the Worker is serving, and the file carries that
interval so an observer needs no out-of-band knowledge of it: a Worker is
**stale** when `now - updated_at > 3 x heartbeat_seconds`. That is
`is_stale()`, and three intervals is deliberate slack — one missed write is a
busy machine, three is a Worker that is not there. A `kill -9`, a panic, a lid
closed on a laptop: none of them get to write "stopped", and all of them are
visible as staleness. A Worker that stopped on purpose says so, so the ordinary
case does not have to be waited out.

**Never fatal.** A status file that cannot be written is an observer that has to
fall back on staleness — annoying, and not worth failing a person's request
over. Every I/O error here is reported once, on stderr, and swallowed. This
module is the same shape as `agent_connect.sessions` for that reason.

**Written whole, or not at all.** Every write goes to a temporary file beside
the target and is renamed over it, so a reader never catches half a document.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Mapping, Optional

STATUS_ENV = "AGENT_CONNECT_STATUS_FILE"
HEARTBEAT_ENV = "AGENT_CONNECT_STATUS_HEARTBEAT"

#: The file, under the workspace the Worker already has.
STATUS_NAME = "status.json"

#: How often `updated_at` is refreshed while serving. Fifteen seconds: often
#: enough that a badge stops lying within half a minute, rare enough that an
#: idle Worker is not writing to disk all day.
DEFAULT_HEARTBEAT = 15.0

#: Missed heartbeats before an observer should call a Worker dead. One is a busy
#: machine; three is nobody home.
STALE_AFTER = 3

#: The states, and the whole of them.
STARTING = "starting"
SERVING = "serving"
STOPPED = "stopped"
ERROR = "error"

#: Bumped only for a change a reader could not survive. Readers should ignore
#: fields they do not know rather than refuse the document.
VERSION = 1


def status_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """The status file: named outright, or under the workspace."""
    from .sessions import workspace_dir  # local: sessions owns "the workspace"

    env = os.environ if env is None else env
    explicit = (env.get(STATUS_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return workspace_dir(env) / STATUS_NAME


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
    ):
        self.path = Path(path)
        self.heartbeat = heartbeat if heartbeat > 0 else DEFAULT_HEARTBEAT
        self._clock = clock
        self._started = clock()
        self._last_write = 0.0
        self._complained = False
        #: Everything the file says, kept so a transition need not restate the
        #: facts that have not changed.
        self._doc: dict = {
            "version": VERSION,
            "state": STARTING,
            "detail": "",
            "pid": os.getpid(),
            "started_at": self._started,
            "heartbeat_seconds": self.heartbeat,
            "last_error": None,
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

    def failed(self, message: str, **facts) -> None:
        """Stopped because of this. The message is the operator's to act on, so
        it is kept verbatim and also recorded as `last_error`."""
        self._doc["last_error"] = {"at": self._clock(), "message": str(message)}
        self._write(ERROR, detail=str(message), **facts)

    def beat(self, **facts) -> None:
        """Refresh `updated_at`, at most once per heartbeat.

        Called from the serve loop, which spins far more often than this file
        needs rewriting. Anything in `facts` that changed is written with it.
        """
        if self._clock() - self._last_write < self.heartbeat:
            self._doc.update(facts)
            return
        self._write(self._doc.get("state", SERVING), **facts)

    # -- internals ----------------------------------------------------------

    def _write(self, state: str, **facts) -> None:
        self._doc.update(facts)
        self._doc["state"] = state
        self._doc["updated_at"] = self._clock()
        self._last_write = self._doc["updated_at"]
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._doc, indent=2, sort_keys=True) + "\n")
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
    return StatusFile(status_path(env), heartbeat_seconds(env))
