"""Per-agent adapters. An adapter maps (task, sandbox, cwd) -> output text."""
from . import codex  # noqa: F401
from . import ollama  # noqa: F401
from . import omnigent  # noqa: F401
from . import cline  # noqa: F401

ADAPTERS = {"codex": codex, "ollama": ollama, "omnigent": omnigent, "cline": cline}


def get(name):
    a = ADAPTERS.get(name)
    if a is None:
        raise KeyError(f"unknown adapter {name!r}; have: {', '.join(sorted(ADAPTERS))}")
    return a
