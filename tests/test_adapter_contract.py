"""The native Adapter registry enforces the closed Turn event contract.

``agent_connect.events`` says the vocabulary is closed and that exactly one
``Done`` ends every Turn. Before this guard, the reporter trusted a native
Adapter to honour both claims: a stream with no ``Done`` defaulted to
``completed``, an unknown event was ignored, and values after ``Done`` were
still consumed.

These tests use tiny native Adapters rather than ACP. The contract belongs to
the Adapter boundary, independent of whichever protocol an Adapter speaks.

Run: python3 tests/test_adapter_contract.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 - puts both distributions on sys.path

import asyncio
import sys
import types
from dataclasses import dataclass

from agent_connect import adapters
from agent_connect.adapters.contract import NativeAdapterContract
from agent_connect.events import (
    COMPLETED,
    FAILED,
    Done,
    MessageChunk,
    Notice,
    TurnContext,
    TurnEvent,
)
from agent_connect.reporter import LadderSettings, TurnReporter

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


CTX = TurnContext(prompt="do it", task_id="contract-1", access_tier="owner")


async def events_of(adapter):
    return [event async for event in adapter.turn(CTX)]


class Valid:
    marker = "delegated"

    async def turn(self, ctx):
        yield MessageChunk(text="answer")
        yield Done(reason=COMPLETED, text="answer")


print("\n-- a valid native stream passes through --")
valid = NativeAdapterContract("valid", Valid())
out = asyncio.run(events_of(valid))
check([type(event) for event in out] == [MessageChunk, Done],
      "the documented event sequence passes through unchanged")
check(out[-1].text == "answer" and out[-1].reason == COMPLETED,
      "the Adapter's terminal event is preserved")
check(valid.marker == "delegated",
      "non-turn attributes delegate to the native Adapter")


print("\n-- ending without Done fails, without losing partial output --")


class MissingDone:
    async def turn(self, ctx):
        yield MessageChunk(text="partial answer")


missing = NativeAdapterContract("missing", MissingDone())
out = asyncio.run(events_of(missing))
check(isinstance(out[0], MessageChunk) and out[0].text == "partial answer",
      "chunks emitted before the broken ending are preserved")
check(type(out[-1]) is Done and out[-1].reason == FAILED,
      "the missing terminal event becomes a failed Done")
check("without a terminal Done" in out[-1].note,
      "and the failure names the invariant that was broken")

body = asyncio.run(
    TurnReporter(None, LadderSettings()).run(
        NativeAdapterContract("missing", MissingDone()), CTX
    )
)
check("partial answer" in body,
      "the room-facing result keeps the partial answer")
check("without a terminal Done" in body,
      "and cannot read as a normally completed answer")


print("\n-- the event vocabulary stays closed --")


@dataclass(frozen=True)
class ProtocolSpecificEvent(TurnEvent):
    """What an upstream protocol might accidentally leak across the seam."""

    kind = "protocol_specific"
    payload: str = "private"


class ForeignSubclass:
    async def turn(self, ctx):
        yield MessageChunk(text="safe so far")
        yield ProtocolSpecificEvent()
        yield Done(text="must not escape")


out = asyncio.run(events_of(
    NativeAdapterContract("foreign-subclass", ForeignSubclass())
))
check(all(type(event) in (MessageChunk, Done) for event in out),
      "a new TurnEvent subclass does not widen the vocabulary by accident")
check(type(out[-1]) is Done and out[-1].reason == FAILED,
      "a protocol-specific event fails the Turn")
check("ProtocolSpecificEvent" in out[-1].note
      and "closed TurnEvent contract" in out[-1].note,
      "the failure identifies the foreign event shape")
check(all(getattr(event, "text", "") != "must not escape" for event in out),
      "nothing after the contract violation is consumed")


class ForeignValue:
    async def turn(self, ctx):
        yield {"not": "an event"}


out = asyncio.run(events_of(NativeAdapterContract("foreign-value", ForeignValue())))
check(len(out) == 1 and type(out[0]) is Done and out[0].reason == FAILED,
      "a value outside TurnEvent fails closed too")


print("\n-- Done is terminal, even when the Adapter disagrees --")
closed = {"value": False}


class AfterDone:
    async def turn(self, ctx):
        try:
            yield Done(text="first and final")
            yield Notice(text="too late")
        finally:
            closed["value"] = True


out = asyncio.run(events_of(NativeAdapterContract("terminal", AfterDone())))
check(len(out) == 1 and type(out[0]) is Done
      and out[0].text == "first and final",
      "the first Done is the last event the consumer sees")
check(closed["value"],
      "the malformed underlying stream is closed at the terminal event")


print("\n-- the registry applies the contract to every native Adapter --")
module_name = "_agent_connect_contract_test_adapter"
registered_name = "contract-test"
module = types.ModuleType(module_name)
module.Registered = Valid
sys.modules[module_name] = module
adapters.NATIVE[registered_name] = f"{module_name}:Registered"
try:
    selected = adapters.get(registered_name)
    check(isinstance(selected, NativeAdapterContract),
          "a native registry entry is selected through the contract guard")
    check(selected.marker == "delegated",
          "the selected Adapter keeps its own public surface")
    check(adapters.get(registered_name) is selected,
          "and the guarded native instance is still built only once")
finally:
    adapters._native_instances.pop(registered_name, None)
    adapters.NATIVE.pop(registered_name, None)
    sys.modules.pop(module_name, None)


print("\n" + ("PASS — native Adapter contract enforced" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
