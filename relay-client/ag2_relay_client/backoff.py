"""How long to wait after a poll that did not work (D1).

Exponential from 1 s to a 60 s ceiling, cleared by one healthy round-trip. The
loop that owns this object catches broadly and backs off on *anything* — an
unexpected exception must cost a delay, never the bearer's only poller.

The 60 s ceiling equals the broker's default lease visibility timeout. That
coupling is recorded rather than engineered around: a client sitting at max
backoff with work in flight can lose its leases and see the tasks re-served, and
that redelivery is absorbed by the dedup journal, which is why the journal is
load-bearing and not an optimization. Lowering the cap would not help — an
unreachable broker cannot have its leases extended at any cap.

This object only says *how long*; the waiting itself belongs to the loop, which
has a stop event to wait on.
"""
from __future__ import annotations


class Backoff:
    """The current retry delay, in seconds."""

    def __init__(self, start: float = 1.0, cap: float = 60.0):
        self.start = float(start)
        self.cap = float(cap)
        self._seconds = 0.0

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<Backoff {self._seconds}s of {self.cap}s>"

    @property
    def seconds(self) -> float:
        """The delay in force — `0.0` when the last round-trip was healthy.

        Read by the status writer: a supervisor seeing a non-zero backoff knows
        the client is retrying rather than idle.
        """
        return self._seconds

    def after_error(self) -> float:
        """Advance the climb and return how long to wait now."""
        self._seconds = self.start if not self._seconds else min(self._seconds * 2, self.cap)
        return self._seconds

    def after_success(self) -> None:
        """One healthy round-trip is the whole condition — not a run of them."""
        self._seconds = 0.0
