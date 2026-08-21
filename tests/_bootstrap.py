"""Make a test runnable as a plain script from anywhere.

Every test here is a script, run as `python3 tests/test_x.py` and asserting in
plain `check(...)` calls — there is no runner to configure and no third-party
dependency to install. That style costs one thing once the tests live in a
subdirectory: the interpreter puts *this* directory on the import path, not the
repo, so `import agent_connect` would find nothing without an editable install.

Importing this module first fixes that, and hands over `ROOT` for the handful of
tests that read the repo itself — the settings table in `README.md`, the version
in `pyproject.toml`, the package sources a few assertions parse.

    import _bootstrap  # noqa: F401 — repo root on sys.path
    from agent_connect.worker import parse_task

The repo goes *after* the standard library but ahead of site-packages, so an
editable install of the same package cannot shadow the working tree under test.
"""
from __future__ import annotations

import sys
from pathlib import Path

#: The repository root — the directory holding `agent_connect/` and `README.md`.
ROOT = Path(__file__).resolve().parent.parent

#: The Relay Client's distribution root. It is a *separate distribution* built
#: from this same repository — the Worker depends on a published version of it
#: rather than on these files — but the suite has to run against the working
#: tree, or a change here and a change there could disagree for a whole release
#: without a single test noticing. Its own suite lives beside it under
#: `relay-client/tests/` and never imports anything from up here: the dependency
#: points one way, and this line is the only place the two trees meet.
RELAY_CLIENT = ROOT / "relay-client"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(RELAY_CLIENT) not in sys.path:
    sys.path.insert(1, str(RELAY_CLIENT))
