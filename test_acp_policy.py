"""Tests for the Permission Policy — the rule the Worker applies when a Local
Agent asks to do something.

The Policy is the only lever the ACP Adapter has: ACP brings no
operating-system Sandbox, so what the Local Agent may touch is decided here, by
answering its requests. These tests are about the deciding, in isolation from
Tasks, rooms and Access Tiers — a real `PermissionRequest` in, a `Decision` out.

Every way of naming a file outside the working directory with a string that
looks like it names one inside gets its own check: `..`, `~`, a relative path,
and a symlink that points out. So does the case the Policy is strictest about —
an operation whose targets cannot be determined at all.

Requires the `agent-client-protocol` package (see `docs/adr/0001`).

Run: .venv/bin/python test_acp_policy.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

try:
    from agent_connect.acp import PermissionRequest
    from agent_connect.acp.policy import WorkingDirectoryPolicy, paths_in
except ImportError as exc:  # pragma: no cover — an environment problem, not a bug
    raise SystemExit(
        f"test_acp_policy.py: {exc}\n"
        "This test has a dependency (see docs/adr/0001). Run it from an\n"
        "environment that has it:\n"
        "    python3 -m venv .venv && .venv/bin/pip install -e .\n"
        "    .venv/bin/python test_acp_policy.py"
    )

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


# The options a well-behaved ACP Agent offers. Ids are deliberately *not* the
# obvious words: the Policy must choose by `kind`, which the protocol defines,
# not by an id, which the agent names and changes between releases.
OPTIONS = [
    {"optionId": "opt-7f3", "name": "Yes", "kind": "allow_once"},
    {"optionId": "opt-a11", "name": "Yes, always", "kind": "allow_always"},
    {"optionId": "opt-b22", "name": "No", "kind": "reject_once"},
]


def request(paths=(), *, options=OPTIONS, raw=None, title="write a file"):
    """A permission request naming `paths` through the protocol's `locations`."""
    tool_call = {"toolCallId": "tool-1", "title": title}
    if paths:
        tool_call["locations"] = [{"path": p} for p in paths]
    if raw is not None:
        tool_call["rawInput"] = raw
    return PermissionRequest(
        session_id="s1", tool_call=tool_call, options=list(options)
    )


# The working directory is created on disk and resolved, because the Policy
# resolves too: on macOS the temp dir lives under a symlinked /private/tmp, and
# a test that compared unresolved strings would pass for the wrong reason.
root = Path(tempfile.mkdtemp()).resolve()
(root / "sub").mkdir()
(root / "sub" / "notes.txt").write_text("hello\n")
outside = Path(tempfile.mkdtemp()).resolve()
(outside / "secrets.env").write_text("TOKEN=1\n")

policy = WorkingDirectoryPolicy(str(root))

# --- under the working directory is allowed --------------------------------

d = policy.decide(request([str(root / "sub" / "notes.txt")]))
check(d.allowed, "a file under the working directory is allowed")
check(d.option_id == "opt-7f3",
      "the allowance is expressed as the agent's own allow_once option id")
check(d.title == "write a file", "the decision carries the operation's title")
check(d.paths == (str(root / "sub" / "notes.txt"),),
      "the decision carries the paths as the agent wrote them")

d = policy.decide(request([str(root)]))
check(d.allowed, "the working directory itself is allowed")

d = policy.decide(request([str(root / "does-not-exist-yet.txt")]))
check(d.allowed, "a file that does not exist yet, under the directory, is allowed")

d = policy.decide(request([str(root / "a.txt"), str(root / "b.txt")]))
check(d.allowed, "several paths, all under the directory, are allowed")

# --- outside it is rejected ------------------------------------------------

d = policy.decide(request([str(outside / "secrets.env")]))
check(not d.allowed, "a file outside the working directory is rejected")
check(d.option_id == "opt-b22",
      "the rejection is expressed as the agent's own reject_once option id")
check("outside the working directory" in d.reason,
      "the rejection says why, in words a person can read")
check(str(outside / "secrets.env") in d.reason,
      "the rejection names the path it objected to")

d = policy.decide(request([str(root / "ok.txt"), str(outside / "secrets.env")]))
check(not d.allowed,
      "one path outside is enough to reject an otherwise-allowed operation")

# --- the four ways of naming an outside file as if it were inside ----------

d = policy.decide(request([str(root / ".." / outside.name / "secrets.env")]))
check(not d.allowed, "`..` does not escape the working directory")

d = policy.decide(request([str(root / "sub" / ".." / ".." / "elsewhere.txt")]))
check(not d.allowed, "`..` buried mid-path does not escape either")

d = policy.decide(request(["~/.ssh/id_rsa"]))
check(not d.allowed, "`~` is expanded before it is judged, and rejected")

d = policy.decide(request(["notes.txt"]))
check(d.allowed,
      "a relative path is resolved against the working directory, and allowed")

d = policy.decide(request([os.path.join("..", outside.name, "secrets.env")]))
check(not d.allowed, "a relative path that climbs out is rejected")

os.symlink(str(outside), str(root / "shortcut"))
d = policy.decide(request([str(root / "shortcut" / "secrets.env")]))
check(not d.allowed,
      "a symlink pointing out of the working directory does not smuggle a file in")

os.symlink(str(root / "sub"), str(root / "inward"))
d = policy.decide(request([str(root / "inward" / "notes.txt")]))
check(d.allowed, "a symlink that stays inside is still allowed")

# --- an operation whose targets cannot be determined -----------------------

d = policy.decide(request([]))
check(not d.allowed, "an operation naming nothing on disk is rejected")
check("cannot tell" in d.reason,
      "the rejection says the targets could not be determined")

d = policy.decide(
    PermissionRequest(session_id="s1", tool_call={}, options=list(OPTIONS))
)
check(not d.allowed, "a request with no tool call at all is rejected")

shell = request([], raw={"command": "make install"}, title="run a command")
d = policy.decide(shell)
check(not d.allowed,
      "a shell command that does not declare what it touches is rejected")

d = policy.decide(request([], raw={"path": str(root / "sub" / "notes.txt")}))
check(d.allowed,
      "a path the agent put in rawInput rather than locations still counts")

d = policy.decide(request([], raw={"file_path": str(outside / "secrets.env")}))
check(not d.allowed, "and is judged by the same rule when it points outside")

d = policy.decide(request([""]))
check(not d.allowed, "an empty path names nothing, so it is not determinable")

# --- fail closed on what the agent offered ---------------------------------

d = policy.decide(
    request([str(root / "notes.txt")],
            options=[{"optionId": "opt-b22", "name": "No", "kind": "reject_once"}])
)
check(not d.allowed,
      "an allowable operation the agent offered no way to allow is not allowed")
check("no option that allows" in d.reason,
      "and it says that is why, rather than looking like a policy rejection")

d = policy.decide(request([str(outside / "secrets.env")], options=[]))
check(not d.allowed and d.option_id is None,
      "with no way to decline, the rejection cancels the request instead")

only_always = [{"optionId": "opt-a11", "name": "Always", "kind": "allow_always"}]
d = policy.decide(request([str(root / "notes.txt")], options=only_always))
check(d.allowed and d.option_id == "opt-a11",
      "allow_always is used when it is the only allowance offered")

# --- usable directly as the core's permission_handler ----------------------

check(policy(request([str(root / "notes.txt")])) == "opt-7f3",
      "calling the Policy returns the option id, so it is a permission_handler")
check(policy(request([str(outside / "secrets.env")])) == "opt-b22",
      "and returns the rejecting option id for something outside")

# --- reading paths out of a tool call --------------------------------------

check(paths_in({"locations": [{"path": "/a"}, {"path": "/a"}]}) == ("/a",),
      "a path named twice is reported once")
check(paths_in({"locations": [{"path": "/a"}], "rawInput": {"path": "/b"}})
      == ("/a", "/b"),
      "locations and rawInput are both read")
check(paths_in({"rawInput": {"prompt": "/etc/passwd"}}) == (),
      "a value under a key that does not name a file is not treated as a path")
check(paths_in({"locations": "nonsense", "rawInput": 7}) == (),
      "a malformed tool call yields no paths, which the Policy then rejects")

print("\n" + ("PASS — permission policy green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
