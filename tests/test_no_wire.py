"""The seam, as a fence: `agent_connect` does not speak the wire.

The Relay Client is a library this repository owns and this process runs
(workspace `docs/adr/0001`), and the whole value of that is that there is **one**
speaker. The failure it replaced is not hypothetical and not old: `roomops.py`
independently reimplemented the combined-token parse and the CloudFlare
User-Agent workaround, hardcoded `https://chat.ag2.space/relay`, and let a stale
`REMOTE_TASK_URL` outrank the gateway that travels inside the token — so one
process spoke to two gateways, the Ladder at one and every Task at the other,
with a log line about it. The combined-token parse existed four times in the
workspace; this repository's copy was the unpoliced one, because the file seam
left it nothing to reuse.

Removing those is a commit. Keeping them removed is this file. It reads the
package as a syntax tree and refuses:

* **any HTTP client** — `urllib`, `http.client`, `socket`, `ssl`, `requests`;
* **any credential handling** — a bearer header, a `|`-split of a token, the
  `%7C` its URL-encoded separator is written as;
* **any gateway** — a URL literal in code, which is I3;
* **any second copy of the result-marker grammar**, whose every copy has
  drifted, most recently into a `[file:]` that reached rooms as literal text.

**The Adapters are a different wire, and the fence knows it.** An Adapter drives
a *Local Agent*, and one of them (Ollama) offers an HTTP API on this machine.
That is the sense of "transport" `CONTEXT.md` says is still the right word —
another protocol's layer, like ACP's stdio — and it is not the relay. So a socket
under `adapters/` is allowed, by name, one file at a time: a new Adapter that
opens one has to be added here, deliberately. Everything else on the list — a
bearer, a gateway, the token grammar — is refused there as loudly as anywhere.

**Docstrings are exempt and comments are invisible to `ast`.** That is
deliberate: a module that had `urllib` taken out of it should be able to say so,
and a fence that punished the explanation would be quietly deleting the reason.
What is fenced is what runs.

Run: python3 tests/test_no_wire.py   (no dependencies — pure syntax)
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import ast

PACKAGE = _bootstrap.ROOT / "agent_connect"

#: Modules that are, or open, a socket. `email`/`json`/`base64` are not here:
#: parsing is not speaking.
WIRE_MODULES = {
    "urllib", "urllib.request", "urllib.error", "urllib.parse",
    "http", "http.client", "http.server", "socket", "ssl", "requests",
    "httpx", "aiohttp",
}

#: The Adapters that may open a socket, and what for. A local model server is
#: not the relay; a new name here is a decision somebody made on purpose.
LOCAL_AGENT_SOCKETS = {"ollama.py": "the Ollama server on this machine"}

#: Substrings that have no business in a string this package evaluates —
#: anywhere, Adapters included.
FORBIDDEN_TEXT = {
    "ag2.space": "the relay's own host, compiled in (I3)",
    "ag2space": "the relay's own host, compiled in (I3)",
    "/v1/": "a relay endpoint path, spoken outside the library",
    "Bearer ": "a bearer header assembled outside the library",
    "Authorization": "an auth header assembled outside the library",
    "%7C": "the onboarding token's URL-encoded separator (C1)",
    "sutando-gateway-client": "the CloudFlare User-Agent workaround, copied",
}

#: And one more that only holds outside `adapters/`: nothing else in this
#: package has any business naming a host at all.
NO_URLS_OUTSIDE_ADAPTERS = "://"

#: What a hand-rolled token parse looks like at the call site.
SPLITTERS = {"split", "rsplit", "partition", "rpartition"}

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def sources():
    for path in sorted(PACKAGE.rglob("*.py")):
        yield path, ast.parse(path.read_text())


def docstring_ids(tree):
    """Every constant that is a docstring, so prose can describe what went."""
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            found.add(id(body[0].value))
    return found


def code_strings(tree):
    exempt = docstring_ids(tree)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in exempt):
            yield node, node.value


def imported(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


# --- no HTTP client, anywhere ----------------------------------------------

offenders = []
excused = set()
for path, tree in sources():
    for name in imported(tree):
        head = name.split(".")[0]
        if not (name in WIRE_MODULES or head in WIRE_MODULES):
            continue
        if path.parent.name == "adapters" and path.name in LOCAL_AGENT_SOCKETS:
            excused.add(path.name)
            continue
        offenders.append(f"{path.parent.name}/{path.name} imports {name}")
check(not offenders,
      "zero direct HTTP calls: nothing in agent_connect opens a socket, except "
      f"the named Adapters that drive a Local Agent ({offenders or 'none'})")
check(excused <= set(LOCAL_AGENT_SOCKETS),
      f"and the exemption list is exactly the Adapters that use it ({sorted(excused)})")


# --- no credential handling, and no gateway --------------------------------

offenders = []
for path, tree in sources():
    for node, text in code_strings(tree):
        for needle, why in FORBIDDEN_TEXT.items():
            if needle in text:
                offenders.append(f"{path.name}:{node.lineno} {why}")
check(not offenders, f"no bearer, no relay host, no relay path, no token "
                     f"grammar in code ({offenders or 'none'})")

offenders = []
for path, tree in sources():
    if path.parent.name == "adapters":
        continue
    for node, text in code_strings(tree):
        if NO_URLS_OUTSIDE_ADAPTERS in text:
            offenders.append(f"{path.name}:{node.lineno} {text[:40]!r}")
check(not offenders,
      "and outside the Adapters there is no host written down at all: the "
      "gateway travels inside the credential, and the library is the only "
      f"thing that reads it (I3) ({offenders or 'none'})")

offenders = []
for path, tree in sources():
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in SPLITTERS or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                and "|" in first.value:
            offenders.append(f"{path.name}:{node.lineno} splits on '|'")
check(not offenders,
      "zero token parses: nobody here splits a credential on its separator — "
      f"the parse is `ag2_relay_client.credentials` ({offenders or 'none'})")


# --- one marker parser, and it is not here ---------------------------------

offenders = []
for path, tree in sources():
    for node, text in code_strings(tree):
        if "file" in text and "attach" in text and "send" in text and "[" in text:
            offenders.append(f"{path.name}:{node.lineno}")
check(not offenders,
      "no second copy of the result-marker grammar: this package asks "
      f"`ag2_relay_client.markers` ({offenders or 'none'})")


# --- the airlock's setting is gone from the code ---------------------------

offenders = []
for path, tree in sources():
    for node, text in code_strings(tree):
        if "AGENT_CONNECT_RESULT_DIR" in text:
            offenders.append(f"{path.name}:{node.lineno}")
check(not offenders,
      "no code path reads AGENT_CONNECT_RESULT_DIR — the staging airlock is "
      f"retired with the seam it crossed ({offenders or 'none'})")


# --- and the fence is not vacuous ------------------------------------------
# Every assertion above passes trivially for a package that talks to nothing.
# These are what say the wire is still spoken, just not here.

users = set()
for path, tree in sources():
    for name in imported(tree):
        if name.split(".")[0] == "ag2_relay_client":
            users.add(path.name)
check({"relay.py", "outgoing.py"} <= users,
      f"the library is what is used instead, and by name ({sorted(users)})")

roomops = ast.parse((PACKAGE / "roomops.py").read_text())
posts = {n.attr for n in ast.walk(roomops) if isinstance(n, ast.Attribute)}
check("message" in posts and "edit" in posts,
      "the Ladder still posts and edits — through the client's Room Ops, which "
      "is the whole of what this file is fencing it down to")

print("\n" + ("PASS — the wire is the library's alone" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
