"""The Session map: which conversation a room and Tier are already having.

A [[Session]] is one continuing conversation with a Local Agent, keyed by the
pair *(room, Access Tier)*. This module is the part of it that outlives a single
Turn: a small JSON file mapping that key to the Session identifier the Local
Agent gave us, the working directory it was opened in, how many Turns have run
on it and when it was last used.

**Why a file at all.** The whole point of the Session is that a follow-up
continues the conversation, and an operator restarting the Worker is not a
conversation ending. Everything else about a Session lives in the Local Agent —
the history, the context, the permission mode — and the only thing the Worker
has to hold on to in order to get it back is the identifier. So that is all
this stores, and it is stored where the operator can read it.

**Bounded, and the boundary is announced.** A Session retires after a budget of
Turns or a period of idleness (`SessionSettings`), and the caller is expected to
tell the room. Unlimited memory is not the goal; predictable memory is. Silent
amnesia — the agent forgetting without saying so — is the thing being prevented,
and a store that retired a Session quietly would be exactly that bug moved
somewhere else.

**Never fatal.** A store that cannot be read is an empty store, and a store that
cannot be written is a conversation that will not resume. Both are worse than
working; neither is worth failing a person's request over, so every I/O error
here is reported once and swallowed.

This module imports nothing from the rest of the package: the Adapter that owns
Sessions depends on it, and so does the Worker's idea of where its state lives.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

STORE_ENV = "AGENT_CONNECT_SESSION_STORE"
TURNS_ENV = "AGENT_CONNECT_SESSION_TURNS"
IDLE_ENV = "AGENT_CONNECT_SESSION_IDLE"
MEMORY_ENV = "AGENT_CONNECT_SESSION_MEMORY"
WORKSPACE_ENV = "AGENT_CONNECT_WORKSPACE"

#: Modest, as the spec asks. Twenty Turns is a long conversation in a chat room,
#: and an hour of silence is a different conversation whatever the room thinks.
DEFAULT_TURNS = 20
DEFAULT_IDLE = 3600.0

#: The file the Session map lives in, under the workspace the Worker already has.
STORE_NAME = "sessions.json"

_OFF = ("0", "false", "no", "off")

#: A Session key as it travels: (room, Access Tier). Same pair as
#: `TurnContext.session_key`, which is what the Worker locks on.
Key = Tuple[str, str]


def workspace_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """Where this Worker keeps its state — tasks, results, and now Sessions."""
    env = os.environ if env is None else env
    return Path(
        env.get(WORKSPACE_ENV) or (Path.home() / ".agent-connect" / "workspace")
    ).expanduser()


def store_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """The Session map's file: named outright, or under the workspace."""
    env = os.environ if env is None else env
    explicit = (env.get(STORE_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return workspace_dir(env) / STORE_NAME


@dataclass(frozen=True)
class SessionRecord:
    """One remembered Session.

    `cwd` is carried because the protocol fixes a Session's working directory at
    creation. A Task arriving with a different one cannot be answered inside
    this Session, however much context it holds — see `matches`.
    """

    session_id: str
    cwd: str
    turns: int = 0
    updated_at: float = 0.0

    def matches(self, cwd: str) -> bool:
        return self.cwd == cwd

    def as_json(self, key: Key) -> dict:
        return {
            "room": key[0],
            "access_tier": key[1],
            "session_id": self.session_id,
            "cwd": self.cwd,
            "turns": self.turns,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class SessionSettings:
    """When a Session stops being reused, and whether it ever is.

    `turns` and `idle` are both "0 means no limit", so an operator who wants an
    unbounded conversation can have one without editing the package — and has
    to ask for it, which is the point of the defaults being modest.
    """

    memory: bool = True
    turns: int = DEFAULT_TURNS
    idle: float = DEFAULT_IDLE

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "SessionSettings":
        env = os.environ if env is None else env
        return cls(
            memory=(env.get(MEMORY_ENV, "").strip().lower() or "1") not in _OFF,
            turns=_number(env.get(TURNS_ENV), DEFAULT_TURNS, int),
            idle=_number(env.get(IDLE_ENV), DEFAULT_IDLE, float),
        )

    def retirement(self, record: SessionRecord, now: Optional[float] = None) -> str:
        """Why this Session is over, in words for the room — or `""` if it is not.

        Returned as the reason rather than a boolean because the room is told
        *why* its context was reset. "The agent forgot" is the complaint being
        answered; "this conversation reached its 20-turn budget" is an answer.
        """
        now = time.time() if now is None else now
        if self.turns and record.turns >= self.turns:
            return f"it reached its budget of {self.turns} turns"
        if self.idle and record.updated_at and now - record.updated_at > self.idle:
            return f"it had been idle for {_duration(now - record.updated_at)}"
        return ""


def _number(raw, fallback, cast):
    """A setting a person typed, or the default — never a crash at startup."""
    try:
        value = cast(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return fallback
    return value if value >= 0 else fallback


def _duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    return f"{seconds / 3600:.1f}h"


class SessionStore:
    """The key-to-Session map, on disk.

    Loaded once and kept in memory; written whole after every change, because it
    holds one line per active room and an operator being able to `cat` it is
    worth more than an append-only format would be.
    """

    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self._clock = clock
        self._records: Optional[Dict[Key, SessionRecord]] = None
        #: True once a read or a write failed; the Worker keeps going without.
        self.degraded = False

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<SessionStore {self.path} n={len(self._loaded())}>"

    # -- the map ----------------------------------------------------------

    def get(self, key: Key) -> Optional[SessionRecord]:
        return self._loaded().get(tuple(key))

    def remember(self, key: Key, session_id: str, cwd: str, turns: int) -> SessionRecord:
        """Record that this key is now this Session, one Turn further on."""
        record = SessionRecord(
            session_id=session_id, cwd=cwd, turns=turns, updated_at=self._clock()
        )
        self._loaded()[tuple(key)] = record
        self._save()
        return record

    def forget(self, key: Key) -> None:
        if self._loaded().pop(tuple(key), None) is not None:
            self._save()

    def keys(self):
        return list(self._loaded())

    # -- disk -------------------------------------------------------------

    def _loaded(self) -> Dict[Key, SessionRecord]:
        if self._records is None:
            self._records = self._read()
        return self._records

    def _read(self) -> Dict[Key, SessionRecord]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001 — a bad store is an empty store
            self._complain(f"cannot read {self.path}: {exc}")
            return {}
        records: Dict[Key, SessionRecord] = {}
        for entry in (raw.get("sessions") if isinstance(raw, dict) else None) or []:
            if not isinstance(entry, dict) or not entry.get("session_id"):
                continue
            key = (str(entry.get("room") or ""), str(entry.get("access_tier") or ""))
            records[key] = SessionRecord(
                session_id=str(entry["session_id"]),
                cwd=str(entry.get("cwd") or ""),
                turns=int(entry.get("turns") or 0),
                updated_at=float(entry.get("updated_at") or 0.0),
            )
        return records

    def _save(self) -> None:
        payload = {
            "version": 1,
            "sessions": [r.as_json(k) for k, r in sorted(self._loaded().items())],
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2) + "\n")
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001 — losing memory beats losing the Turn
            self._complain(f"cannot write {self.path}: {exc}")

    def _complain(self, message: str) -> None:
        if not self.degraded:
            print(
                f"agent-connect: the session map is not usable — {message}. "
                "Conversations will not continue across turns.",
                file=sys.stderr, flush=True,
            )
        self.degraded = True


def store_from_env(env: Optional[Mapping[str, str]] = None) -> SessionStore:
    return SessionStore(store_path(env))
