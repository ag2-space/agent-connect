"""Runtime enforcement for Adapters that emit the native Turn contract.

A native Adapter speaks the event-shaped boundary directly instead of going
through ``ShimAdapter``. That is an implementation distinction, not permission
to widen the boundary: it still emits only ``TurnEvent`` values, and exactly one
``Done`` is the last value in every Turn.

This module enforces those two properties at the registry seam. A malformed
stream becomes a failed ``Done`` rather than being ignored or reading as a
successful Turn. Events already emitted remain available, so a partial answer is
not thrown away merely because its Adapter broke the ending contract.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from ..events import FAILED, Done, TurnContext, TurnEvent


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

        The first ``Done`` is terminal. A value outside ``TurnEvent`` or a
        stream that ends without ``Done`` is converted into a failed terminal
        event. In either case, an underlying async generator is closed so code
        after the broken boundary cannot continue running in the background.
        """
        stream = self._adapter.turn(ctx)
        try:
            async for event in stream:
                if not isinstance(event, TurnEvent):
                    yield Done(
                        reason=FAILED,
                        note=(
                            f"⚠️ agent-connect: the {self.name} Adapter emitted "
                            f"{type(event).__name__}, which is outside the "
                            "TurnEvent contract."
                        ),
                    )
                    return
                yield event
                if isinstance(event, Done):
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
