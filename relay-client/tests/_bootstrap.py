"""Make a test runnable as a plain script from anywhere.

Same discipline as the agent-connect suite next door: every test here is a
script, run as `python3 tests/test_x.py`, asserting in plain `check(...)` calls.
Importing this module first puts the *distribution* root (the directory holding
`ag2_relay_client/`) on the path, ahead of site-packages, so an installed copy
of the same package cannot shadow the working tree under test.

    import _bootstrap  # noqa: F401 — distribution root on sys.path
    from ag2_relay_client.state import StateLayout
"""
from __future__ import annotations

import sys
from pathlib import Path

#: The distribution root — the directory holding `ag2_relay_client/`.
ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
