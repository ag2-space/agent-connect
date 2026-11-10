"""Agents the worker starts do not inherit its relay credentials.

Each child here dumps its environment and exits; what it wrote is what crossed.
Covers the ACP spawn and the one-shot CLI adapters (via kilo, with a stub).

Run: python3 tests/test_acp_child_env.py
"""
import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import json
import os
import sys
import tempfile

from agent_connect.acp import AcpClient
import importlib
import stat

from agent_connect.childenv import WORKER_SECRET_NAMES

WORKER_SECRET_ENV = WORKER_SECRET_NAMES + (
    "AGENT_CONNECT_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN",
    "AG2_REMOTE_TOKEN_WORK",  # a per-lane name, caught by prefix
)

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


DUMP = "import json, os, sys; json.dump(dict(os.environ), open(sys.argv[1], 'w'))"


async def spawn_and_dump(out, extra=None):
    try:
        async with AcpClient.spawn([sys.executable, "-c", DUMP, out], env=extra):
            for _ in range(200):
                if os.path.exists(out) and os.path.getsize(out):
                    break
                await asyncio.sleep(0.02)
    except Exception:
        pass  # the child exits without speaking ACP; only its dump matters
    with open(out) as fh:
        return json.load(fh)


with tempfile.TemporaryDirectory() as tmp:
    for name in WORKER_SECRET_ENV:
        os.environ[name] = "secret-" + name
    os.environ["AGENT_CONNECT_UNRELATED"] = "kept"

    seen = asyncio.run(spawn_and_dump(os.path.join(tmp, "a.json")))
    leaked = [n for n in WORKER_SECRET_ENV if n in seen]
    check(not leaked, f"no worker secret reaches the ACP child (leaked: {leaked})")
    check(seen.get("AGENT_CONNECT_UNRELATED") == "kept" and "PATH" in seen,
          "the rest of the environment is inherited")

    seen = asyncio.run(spawn_and_dump(os.path.join(tmp, "b.json"),
                                      {"EXTRA_FOR_CHILD": "1"}))
    check(seen.get("EXTRA_FOR_CHILD") == "1", "an explicit env still reaches the child")
    check("AGENT_CONNECT_TOKEN" not in seen,
          "and passing one does not bring the inherited secrets back")

    # The one-shot CLI adapters (codex, cline, kilo, omnigent) share the rule.
    stub = os.path.join(tmp, "kilo-stub")
    dump = os.path.join(tmp, "kilo.json")
    with open(stub, "w") as fh:
        fh.write(f"#!{sys.executable}\nimport json, os\n"
                 f"json.dump(dict(os.environ), open({dump!r}, 'w'))\n")
    os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
    os.environ["AGENT_CONNECT_KILO_BIN"] = stub
    kilo = importlib.import_module("agent_connect.adapters.kilo")
    kilo.run("hi", "read-only", tmp, timeout=30)
    with open(dump) as fh:
        seen = json.load(fh)
    leaked = [n for n in WORKER_SECRET_ENV if n in seen]
    check(not leaked, f"no worker secret reaches a CLI adapter's child (leaked: {leaked})")

    check(os.environ.get("AGENT_CONNECT_TOKEN") == "secret-AGENT_CONNECT_TOKEN",
          "the worker's own environment is left as it was")

print("\n" + ("PASS — ACP child env green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
