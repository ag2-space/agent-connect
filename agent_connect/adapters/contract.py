"""Runtime enforcement for Adapters that emit the native Turn contract.

A native Adapter speaks the event-shaped boundary directly instead of going
through ``ShimAdapter``. That is an implementation distinction, not permission
to widen the boundary: it still emits only the closed ``TurnEvent`` vocabulary,
and exactly one ``Done`` is the last value in every Turn.

This module enforces those two properties at the registry seam. A malformed
stream becomes a failed ``Done`` rather than being ignored or reading as a
successful Turn. Events already emitted remain available, so a partial answer is
not thrown away merely because its Adapter broke the ending contract.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from ..events import (
    FAILED,
    Done,
    MessageChunk,
    Notice,
    PermissionAsked,
    Plan,
    Thinking,
    ToolFinished,
    ToolStarted,
    TurnContext,
    TurnEvent,
)

#: The Adapter boundary is a closed vocabulary. Checking against ``TurnEvent``
#: alone would let an upstream protocol widen it by subclassing the base class,
#: and the reporter would then silently ignore a value it does not understand.
_EVENT_TYPES = (
    MessageChunk,
    Thinking,
    ToolStarted,
    ToolFinished,
    Plan,
    PermissionAsked,
    Notice,
    Done,
)


class NativeAdapterContract:
    """A native Adapter with its documented event stream enforced.

    Attribute access other than ``turn`` delegates to the underlying Adapter so
    lifecycle hooks such as ``preflight`` keep their existing surface.
    """

    def __init__(self, name: str, adapter: Any):
        self.name = name
        self._adapter = adapter

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<NativeAdapterContract {self.name}>"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    async def turn(self, ctx: TurnContext) -> AsyncIterator[TurnEvent]:
        """Yield one closed, terminal event stream for this Turn.

        The first ``Done`` is terminal. A value outside the closed event
        vocabulary or a stream that ends without ``Done`` is converted into a
        failed terminal event. In either case, an underlying async generator is
        closed so code after the broken boundary cannot continue running in the
        background.
        """
        stream = self._adapter.turn(ctx)
        try:
            async for event in stream:
                if type(event) not in _EVENT_TYPES:
                    yield Done(
                        reason=FAILED,
                        note=(
                            f"⚠️ agent-connect: the {self.name} Adapter emitted "
                            f"{type(event).__name__}, which is outside the "
                            "closed TurnEvent contract."
                        ),
                    )
                    return
                yield event
                if type(event) is Done:
                    return
            yield Done(
                reason=FAILED,
                note=(
                    f"⚠️ agent-connect: the {self.name} Adapter ended without "
                    "a terminal Done event."
                ),
            )
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - cleanup cannot replace Done
                    pass
