"""Connection state, said out loud after every poll outcome (D2-D5).

The 2026-07-25 wedge — a client stuck for 21 hours on an unbounded DNS call —
was invisible not because nothing was wrong but because nothing was *written*:
the UI showed "reconnecting" forever with nothing to diagnose from, and the
supervisor was reduced to guessing connectivity from the presence of a terminal
window. So the rule is that every turn of the loop leaves a trace, healthy or
failed, somewhere a supervisor can read without guessing.

Status is layered, and this module is the bottom layer: a **connection-only**
file the library writes itself, so observability survives a consumer that never
reads the hook. It is not an impersonation of anybody else's status schema —
consumers compose richer files under their own name from `snapshot()` and the
change hook.

Three properties are non-negotiable, and each is a scar:

- **`last_ok_ts` survives the reconnecting writes** — "last connected N seconds
  ago" is the number an operator actually needs, and it is exactly the one a
  naive rewrite drops.
- **The URL is redacted before it is persisted (D3)** — a state dir can be
  vault-synced, and a gateway configured with `user:pass@` or `?token=` would
  land there in plaintext.
- **Nothing here may raise (D4).** A status write, a hook, a log line: all of
  them run inside the poll iteration, and anything that can raise inside the
  poll iteration is a delivery blocker. A malformed side-channel must degrade
  to absent, never to a stalled loop.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .state import redact_url, write_private_atomic

log = logging.getLogger(__name__)

#: Connected and polling.
CONNECTED = "connected"
#: Not connected; the loop is backing off and will retry by itself.
RECONNECTING = "reconnecting"
#: The bearer was rejected and a durable token source exists — the loop is
#: holding, re-reading, and will resume live when the rotation lands (C2).
AUTH_WAIT = "auth-wait"
#: The bearer was rejected and there is nothing to re-read. The loop has
#: stopped; only a reconfiguration changes this.
FATAL = "fatal"
#: Another poller holds this bearer's singleton guard (J1). Not polling — two
#: pollers on one bearer double-deliver every task — but still asking, so a
#: holder that dies is taken over from without anybody intervening.
STANDBY = "standby"
#: This client held the guard and another poller took it. Polling has stopped
#: for good; the consumer decides whether anything starts again.
DISPLACED = "displaced"
#: `stop()` was called.
STOPPED = "stopped"


class StatusReporter:
    """The connection's state: a snapshot, a file, and a hook.

    Thread-safe by a plain lock — `update` is called from the poll thread and
    `snapshot` from whatever thread the consumer happens to be on.
    """

    def __init__(
        self,
        path,
        gateway: str = "",
        instance: str = "default",
        hook: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._hook = hook
        self._state: Dict[str, Any] = {
            "instance": instance,
            # Redacted once, at construction: the URL never changes for the
            # life of a client (a gateway move is a restart, not a rotation),
            # and redacting once means no write can forget to.
            "gateway": redact_url(gateway) if gateway else "",
            "connected": False,
            "state": RECONNECTING,
            "error": None,
            "backoff_s": 0.0,
            "inflight": 0,
            "pending_results": 0,
            "last_ok_ts": _previous_last_ok(self.path),
            "updated_ts": 0.0,
        }

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<StatusReporter {self._state.get('state')} {self.path}>"

    def on_change(self, hook: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """Register the consumer's change hook. One hook: a consumer that wants
        to fan out owns the fanning."""
        with self._lock:
            self._hook = hook

    def snapshot(self) -> Dict[str, Any]:
        """A copy of the current state — safe to read from any thread, and safe
        to keep, because it is not a view."""
        with self._lock:
            return dict(self._state)

    def update(self, state: str, **fields: Any) -> Dict[str, Any]:
        """Record one poll outcome, write it, and tell the consumer.

        Never raises. Everything below the first line is best-effort by
        contract: this runs inside the poll iteration.
        """
        with self._lock:
            self._state["state"] = state
            self._state["connected"] = state == CONNECTED
            self._state["updated_ts"] = time.time()
            if state == CONNECTED:
                self._state["last_ok_ts"] = self._state["updated_ts"]
                self._state["error"] = None
                self._state["backoff_s"] = 0.0
            for key, value in fields.items():
                self._state[key] = value
            snapshot = dict(self._state)

        self._write(snapshot)
        self._notify(snapshot)
        return snapshot

    def _write(self, snapshot: Dict[str, Any]) -> None:
        try:
            write_private_atomic(
                self.path,
                json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            )
        except Exception:  # noqa: BLE001 — D4: a status write is never a blocker
            log.debug("status write to %s failed", self.path, exc_info=True)

    def _notify(self, snapshot: Dict[str, Any]) -> None:
        hook = self._hook
        if hook is None:
            return
        try:
            hook(snapshot)
        except Exception:  # noqa: BLE001 — the consumer's hook is a side channel
            log.warning("status hook raised; the poll loop is unaffected",
                        exc_info=True)


def _previous_last_ok(path: Path) -> float:
    """The `last_ok_ts` the previous run left, or 0.

    A restart that reset this to zero would report "never connected" for a
    client that was connected a second ago, which reads as a much worse
    incident than the one that actually happened.
    """
    try:
        previous = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — no file, bad file: both mean "unknown"
        return 0.0
    value = previous.get("last_ok_ts") if isinstance(previous, dict) else None
    return float(value) if isinstance(value, (int, float)) else 0.0
