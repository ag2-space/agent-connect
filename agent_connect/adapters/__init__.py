"""Per-agent adapters.

An Adapter takes a `TurnContext`, emits a stream of `agent_connect.events`
values and finishes with `Done`. The five original Adapters are still written
the old way — `run(task, sandbox, cwd) -> str` — and `get()` hands them back
wrapped in `ShimAdapter`, which runs that call off-thread. They were not
rewritten and are not scheduled to be: migrating them is later work and
explicitly not urgent.

An Adapter that speaks the contract natively (the ACP one) registers the object
itself; anything exposing `turn` is passed through untouched. Those live in
`NATIVE` and are built on selection rather than imported here, because the ACP
one carries the `agent-client-protocol` dependency (`docs/adr/0001`) and a
Worker driving codex should not have to have it installed.
"""
import importlib

from . import codex  # noqa: F401
from . import ollama  # noqa: F401
from . import omnigent  # noqa: F401
from . import cline  # noqa: F401
from . import kilo  # noqa: F401
from .shim import ShimAdapter  # noqa: F401

ADAPTERS = {
    "codex": codex,
    "ollama": ollama,
    "omnigent": omnigent,
    "cline": cline,
    "kilo": kilo,
}

#: Name → "module:attribute" of an Adapter class that speaks the event contract
#: natively. Built once, on first selection.
NATIVE = {
    "acp": "agent_connect.adapters.acp:AcpAdapter",
}

_native_instances: dict = {}


def names():
    """Every selectable Adapter name."""
    return sorted(set(ADAPTERS) | set(NATIVE))


def get(name):
    """The named Adapter, wearing the event-shaped contract."""
    if name in NATIVE and name not in ADAPTERS:
        if name not in _native_instances:
            module, _, attr = NATIVE[name].partition(":")
            _native_instances[name] = getattr(importlib.import_module(module), attr)()
        return _native_instances[name]
    a = ADAPTERS.get(name)
    if a is None:
        raise KeyError(f"unknown adapter {name!r}; have: {', '.join(names())}")
    return a if hasattr(a, "turn") else ShimAdapter(name, a)
