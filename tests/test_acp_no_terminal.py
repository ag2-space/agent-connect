"""No code path grants the Local Agent an interactive terminal.

ACP lets a Client offer terminal provisioning — `terminal/create`,
`terminal/output`, and friends — so an Agent can ask for a shell and drive it.
agent-connect does not implement them and does not advertise them. A Task
arrives from a chat room, from a person who may not be the operator, and the
operator is usually not at the keyboard; handing that a terminal is not a trade
worth making, and "we simply never wrote the handler" is not a guarantee anyone
can check. This file is the check.

It asserts three separate things, because any one of them alone could pass while
the property was lost:

1. **On the wire.** The fake ACP Agent records the `initialize` params it was
   sent. The client capabilities in them must offer no terminal.
2. **In the source.** No module in the package names a terminal-provisioning
   method, and `CLIENT_CAPABILITIES` has no `terminal` key.
3. **At the class.** The callbacks object handed to the ACP library implements
   no terminal method, so an Agent that asks anyway is answered "unsupported"
   by the library rather than by something we forgot to write.

Requires the `agent-client-protocol` package (see `docs/adr/0001`).

Run: .venv/bin/python tests/test_acp_no_terminal.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

try:
    from agent_connect.acp.core import CLIENT_CAPABILITIES, AcpClient, _ClientCallbacks
except ImportError as exc:  # pragma: no cover — an environment problem, not a bug
    raise SystemExit(
        f"test_acp_no_terminal.py: {exc}\n"
        "This test has a dependency (see docs/adr/0001). Run it from an\n"
        "environment that has it:\n"
        "    python3 -m venv .venv && .venv/bin/pip install -e .\n"
        "    .venv/bin/python tests/test_acp_no_terminal.py"
    )

HERE = Path(__file__).parent
ROOT = _bootstrap.ROOT
FAKE = str(HERE / "fake_acp_agent.py")

#: The protocol's terminal surface, in both spellings a handler could take.
TERMINAL_METHODS = (
    "terminal/create", "terminal/output", "terminal/release",
    "terminal/wait_for_exit", "terminal/kill",
    "createTerminal", "terminalOutput", "releaseTerminal",
    "create_terminal", "terminal_output", "release_terminal",
)

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


# --- 1) what actually goes out on the wire ---------------------------------


def initialize_params() -> dict:
    """Connect to the fake ACP Agent, initialize, and read what it was sent."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "script.json"
        script.write_text(json.dumps({}))
        report = Path(tmp) / "report.json"
        os.environ["FAKE_ACP_REPORT"] = str(report)

        async def run():
            async with AcpClient.spawn(
                [sys.executable, FAKE, str(script)], cwd=tmp
            ) as client:
                await client.initialize()

        try:
            asyncio.run(asyncio.wait_for(run(), timeout=30))
        finally:
            os.environ.pop("FAKE_ACP_REPORT", None)
        return json.loads(report.read_text())["initialize"] or {}


params = initialize_params()
caps = params.get("clientCapabilities") or {}
check(params.get("protocolVersion") is not None,
      "the fake ACP Agent really was initialized (so the wire check means something)")
check("terminal" not in caps,
      "the initialize the Worker sends advertises no terminal capability")
check(caps.get("terminal") is not True,
      "and certainly not a terminal capability that is on")

# The library omits default-valued fields, so `fs` may be absent rather than
# explicitly false — "not advertised" is the assertable form. See ticket 02.
check(not (caps.get("fs") or {}).get("writeTextFile"),
      "nor a filesystem write capability")

# --- 2) the source says so too ---------------------------------------------

check("terminal" not in CLIENT_CAPABILITIES,
      "CLIENT_CAPABILITIES has no terminal key")

def executable_code(path: Path) -> str:
    """The module with its comments and string literals removed.

    A comment or a docstring saying why we do *not* implement terminals is the
    decision being visible, and must not read as the decision being broken.
    Only executable tokens count. (Same technique as `test_acp_core.py`'s
    vocabulary check, for the same reason.)
    """
    import io
    import tokenize

    kept = []
    for tok in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(tok.string)
    return " ".join(kept)


sources = sorted(ROOT.glob("agent_connect/**/*.py"))
check(len(sources) > 5, f"the source scan found the package ({len(sources)} modules)")
code = {p.name: executable_code(p) for p in sources}
for method in TERMINAL_METHODS:
    # Method names carrying a slash cannot survive tokenisation as one token, so
    # they are checked against the raw source minus comments instead: a string
    # literal "terminal/create" in executable position is exactly what a handler
    # registration would look like.
    haystack = code if "/" not in method else {
        p.name: re.sub(r"(?m)^\s*#.*$", "", p.read_text()) for p in sources
    }
    named_in = sorted(name for name, text in haystack.items() if method in text)
    check(not named_in,
          f"nothing implements {method} ({', '.join(named_in) or 'nowhere'})")

# --- 3) the callbacks object offers nothing terminal-shaped ----------------

# The library's `Client` protocol declares the terminal methods with empty
# bodies, so `dir()` finds them whatever we do. What matters is what *we*
# implement: only the two callbacks a Session actually needs.
ours = sorted(a for a in vars(_ClientCallbacks) if not a.startswith("_"))
check(ours == ["request_permission", "session_update"],
      f"the callbacks object implements only the two Session callbacks ({ours})")
check(not [a for a in vars(_ClientCallbacks) if "terminal" in a.lower()],
      "and nothing terminal-shaped of its own")

# --- 4) an Agent that asks for a terminal anyway does not get one ----------


def ask_for_a_terminal() -> dict:
    """Script the fake Agent to request `terminal/create` mid-Turn."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "script.json"
        script.write_text(json.dumps({"turns": [{"actions": [
            {"type": "request", "method": "terminal/create",
             "params": {"command": "/bin/sh", "args": ["-i"]}},
            {"type": "message", "text": "carried on"},
        ], "stopReason": "end_turn"}]}))
        report = Path(tmp) / "report.json"
        os.environ["FAKE_ACP_REPORT"] = str(report)

        async def run():
            async with AcpClient.spawn(
                [sys.executable, FAKE, str(script)], cwd=tmp
            ) as client:
                await client.initialize()
                session = await client.new_session(cwd=tmp)
                await client.prompt(session, "run a shell for me")

        try:
            asyncio.run(asyncio.wait_for(run(), timeout=30))
        finally:
            os.environ.pop("FAKE_ACP_REPORT", None)
        return json.loads(report.read_text())


asked = ask_for_a_terminal()
requests = asked.get("requests") or []
check(len(requests) == 1 and requests[0]["method"] == "terminal/create",
      "the fake ACP Agent really did ask for a terminal")
if requests:
    answer = requests[0]
    check(answer["errored"] or not (answer["answer"] or {}).get("terminalId"),
          "and it was not given one — no terminal id came back")
    check(not (isinstance(answer["answer"], dict)
               and "terminalId" in json.dumps(answer["answer"])),
          "nothing terminal-shaped is in the answer at all")
check(len(asked.get("prompts") or []) == 1,
      "the Turn ran to its end anyway — refusing a terminal does not kill the "
      "connection, it just declines")

print("\n" + ("PASS — no terminal is ever offered" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
