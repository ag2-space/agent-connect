"""Per-agent adapters.

An Adapter takes a `TurnContext`, emits a stream of `agent_connect.events`
values and finishes with `Done`. The five original Adapters are still written
the old way — `run(task, sandbox, cwd) -> str` — and `get()` hands them back
wrapped in `ShimAdapter`, which runs that call off-thread. They were not
rewritten and are not scheduled to be: migrating them is later work and
explicitly not urgent.

An Adapter that speaks the contract natively (the ACP one) registers the object
itself; anything exposing `turn` is passed through untouched.
"""
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


def get(name):
    """The named Adapter, wearing the event-shaped contract."""
    a = ADAPTERS.get(name)
    if a is None:
        raise KeyError(f"unknown adapter {name!r}; have: {', '.join(sorted(ADAPTERS))}")
    return a if hasattr(a, "turn") else ShimAdapter(name, a)
