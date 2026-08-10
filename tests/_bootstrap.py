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

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
