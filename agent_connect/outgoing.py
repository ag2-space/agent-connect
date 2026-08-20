"""A file the Local Agent produced, on its way out of this machine.

The sibling of `agent_connect.attachments`, and deliberately not part of it. That
module answers *"is this safe to open?"* about a file someone else chose and the
Relay Client already downloaded. This one answers a different question about a
path the **agent** named: *"may this leave the machine?"*

## What used to be here, and why it is not

Files used to go out by being **staged**: the Worker copied one into an outgoing
result directory (`AGENT_CONNECT_RESULT_DIR`) and named it with a `[file: …]`
marker in the result body, and `ag2-sparrow` — a *separate process* — resolved
the marker, checked the path against its own allowlist and uploaded. Decider and
actor were two programs, which is a real property, and workspace `docs/adr/0001`
records its loss honestly.

Two things ended that airlock. The seam it crossed is gone: there is no second
process to stage anything *for*. And the protocol it relied on was never there —
the broker's `parse_result` knows `[no-send]`, `[REPLIED]`, `[deduped:]` and
`[channel:]`, and **not** `[file:]`, so a staged file was never uploaded by
anyone and the marker reached the room as literal text naming an absolute local
path, after the Ladder had already promised the person a file.

## What is here instead

The route is now the Relay Client's `complete`, which reads the marker grammar
in the one place it is written down and uploads from an **allowlisted path** —
resolved, opened one component at a time under `O_NOFOLLOW`, judged on the
descriptor rather than on the string. None of that is in this repository, and
the module that holds it (`ag2_relay_client.egress`) opens with the paragraph
whoever changes it has to read first.

What is left here is the one thing that is genuinely this Worker's: **which
directories those roots are.** It is a small function on purpose. The allowlist
is the whole of the egress policy, it is fixed when the client is built, and a
reviewer asking "what may this program upload?" should have exactly one list to
read and one place to read it from.

**The permitted area is the working directory this Worker's Turns run in** —
`AGENT_CONNECT_REPO`, where the Local Agent works, what the Permission Policy
already guards writes against, and the only place a file *a Turn produced* can
be. An operator with a second one says so in `AGENT_CONNECT_EGRESS_ROOTS`;
nothing else on the machine is sendable.

What this does **not** prevent, stated so nobody reads more into it: an agent
that can be talked into copying `~/.ssh/id_rsa` into its own working directory
can then send the copy. The permitted area is a boundary on paths, not a proof
about contents, and closing that would take a Sandbox that refuses the first
copy — `CONTEXT.md`'s distinction between confinement and a cooperative policy,
again.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

from ag2_relay_client import markers

#: Extra directories a file may be sent from, `os.pathsep`-separated. For a
#: Worker whose agent writes somewhere other than where it works — a build
#: output tree, a shared reports directory. Every entry is a root of its own and
#: is judged the same way: a path is inside one of them or it does not go.
EGRESS_ROOTS_ENV = "AGENT_CONNECT_EGRESS_ROOTS"

#: What the Local Agent is told about sending a file, in the preamble. The words
#: live beside the code that *chooses the roots*, so a change to the rule cannot
#: leave the instruction describing the old one. The judging itself is the
#: library's, and the sentence is deliberately about the working directory
#: rather than about an allowlist: an agent can act on the first.
INSTRUCTION = (
    "To put a file in the room, write [file: <path>] on a line of its own and "
    "agent-connect attaches it to this reply. The path must be inside your "
    "working directory; a file anywhere else is not sent."
)


def egress_roots(
    repo: Optional[object] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[str, ...]:
    """Every directory this Worker may send a file from, and no others.

    The working directory its Turns run in, plus whatever the operator added.
    Order is not significant — a path is judged against all of them — but the
    working directory is first because it is the one that explains the rule.

    Nothing is created and nothing is resolved here: a root that does not exist
    is dropped by the library's allowlist at construction, which is where every
    other judgement about a path is made too. Returning a tuple is the point —
    what the client is built with is a list nobody can add to afterwards.
    """
    env = os.environ if env is None else env
    roots: List[str] = []
    for raw in [repo] + (env.get(EGRESS_ROOTS_ENV) or "").split(os.pathsep):
        named = str(raw or "").strip()
        if not named:
            continue
        expanded = str(Path(named).expanduser())
        if expanded not in roots:
            roots.append(expanded)
    return tuple(roots)


def named_files(text: Optional[str]) -> Tuple[str, ...]:
    """Every file this answer names, as the agent wrote them.

    Asked by the Ladder, which has one decision to make about it: an answer
    carrying a file cannot be marked `[REPLIED]`, because a skip marker is
    terminal in the grammar and a body nobody parses past is a body whose
    attachment never leaves. See `agent_connect.reporter.finish`.

    **It is the library's parser answering, not a copy of its expression.** This
    package used to carry its own, "the same expression so that what this module
    recognises and what the Relay Client recognises cannot drift apart" — and
    they drifted anyway, because the thing on the other side was never reading
    `[file:]` at all. Asking is the only version of that promise that holds.

    The paths come back **unjudged**, and this module deliberately has no way to
    judge one: whether a file may leave is decided at the sink, on the open
    descriptor, inside the library. A consumer that pre-approved a path would be
    a second allowlist somewhere no reviewer looks for one.
    """
    return markers.parse(text).attachments
