"""The Permission Policy: the rule the Worker applies when a Local Agent asks.

The Worker is the ACP Client, so the decision about what the Local Agent may do
never rests with the Local Agent. This module is that decision, and only that:
it takes a `PermissionRequest` and answers it. It knows nothing of Tasks, rooms
or Access Tiers — who is allowed to reach ACP at all is the Adapter's business.

**The rule.** An operation whose targets all resolve under the Session's working
directory is allowed; anything else is rejected. That reconstructs, by
convention, roughly what the operating-system Sandbox used to enforce for the
other Adapters — and only roughly, because it is *cooperative*: it binds an
agent that asks and does nothing at all to one that does not. See `CONTEXT.md`
on [[Permission Policy]] versus [[Sandbox]]; they are not substitutes.

**It fails closed, in three places, deliberately:**

*Paths are resolved before they are judged.* `..`, `~`, a relative path and a
symlink pointing out of the working directory are all ways to name a file
outside it with a string that looks like it is inside. `Path.resolve()` settles
all four, and the comparison happens on what came back.

*An operation whose targets cannot be determined is rejected.* A shell command
is the common case: the Worker cannot tell what `make install` will write, and a
policy that guesses "probably fine" is a policy that says yes to everything it
does not understand. This makes the policy strict — a Local Agent that asks to
run a command without declaring what it touches is refused — and that strictness
is the honest reading of "allows operations under the working directory".

*An allowed operation the agent offered no way to allow is still not allowed.*
Rather than silently pick some other option id, the request is refused with a
reason saying so.

`allow_once` is preferred over `allow_always`: standing permission outlives the
Turn that justified it. Option *ids* are the ACP Agent's to name and change
between releases, so options are chosen by the `kind` the protocol defines.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from .core import PermissionRequest

#: Keys in a tool call's `rawInput` that name something on disk. The protocol
#: does not standardise `rawInput` — it is the Local Agent's own tool schema —
#: so this is a best-effort widening of the `locations` the protocol *does*
#: define. Anything not found here leaves the request unlocatable, which is
#: refused rather than assumed harmless.
PATH_KEYS = frozenset(
    {
        "path", "file_path", "filePath", "abs_path", "absPath", "filename",
        "file", "dir", "directory", "cwd", "notebook_path", "notebookPath",
        "target_file", "old_path", "new_path", "source", "destination",
    }
)

ALLOW_ONCE = "allow_once"
ALLOW_ALWAYS = "allow_always"
REJECT_ONCE = "reject_once"
REJECT_ALWAYS = "reject_always"


@dataclass(frozen=True)
class Decision:
    """What the Policy decided, and enough of why to tell a person.

    `option_id` is what goes back to the ACP Agent — `None` cancels the request,
    which is what is left when the agent offered no option that says no.
    """

    allowed: bool
    reason: str
    title: str
    option_id: Optional[str] = None
    paths: Tuple[str, ...] = ()


def paths_in(tool_call: dict) -> Tuple[str, ...]:
    """Everything on disk this tool call says it will touch, as written.

    Unresolved on purpose: resolution is the deciding step and belongs with the
    decision, but the strings as the agent wrote them are what a person needs to
    read in an explanation.
    """
    found: list = []
    for location in tool_call.get("locations") or []:
        if isinstance(location, dict) and isinstance(location.get("path"), str):
            if location["path"].strip():
                found.append(location["path"])
    raw = tool_call.get("rawInput")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in PATH_KEYS and isinstance(value, str) and value.strip():
                found.append(value)
    seen: list = []
    for path in found:
        if path not in seen:
            seen.append(path)
    return tuple(seen)


class WorkingDirectoryPolicy:
    """Allows what is under one working directory, and rejects the rest.

    Usable directly as the `permission_handler` an `AcpClient` is spawned with;
    `decide()` is separate so a caller can report the decision as well as make
    it — a rejected request is the interesting one, and a room that never hears
    about it cannot tell a blocked agent from a lazy one.
    """

    def __init__(self, cwd: str):
        # The root is resolved once, and symlinks with it: on macOS `/tmp` *is*
        # a symlink, so an unresolved root would reject every path under the
        # directory it was itself given.
        self.root = Path(cwd).expanduser().resolve()

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<WorkingDirectoryPolicy {self.root}>"

    def __call__(self, request: PermissionRequest) -> Optional[str]:
        return self.decide(request).option_id

    def decide(self, request: PermissionRequest) -> Decision:
        tool_call = request.tool_call or {}
        title = tool_call.get("title") or tool_call.get("toolCallId") or "an operation"
        paths = paths_in(tool_call)
        if not paths:
            return self._no(
                request, title, paths,
                "agent-connect cannot tell what this would touch, so it was not allowed",
            )
        outside = [p for p in paths if not self.contains(p)]
        if outside:
            return self._no(
                request, title, paths,
                "outside the working directory: " + ", ".join(outside),
            )
        option_id = request.option_of_kind(ALLOW_ONCE) or request.option_of_kind(ALLOW_ALWAYS)
        if option_id is None:
            return self._no(
                request, title, paths,
                "the ACP Agent offered no option that allows it",
            )
        return Decision(True, "under the working directory", title, option_id, paths)

    def contains(self, raw: str) -> bool:
        """Whether `raw` names something under the working directory.

        Resolved first — `..`, `~`, a relative path and a symlink all name a
        file outside a directory while looking like they name one inside it.
        A path that cannot be resolved at all is not under anything.
        """
        try:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = self.root / candidate
            resolved = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        return resolved == self.root or self.root in resolved.parents

    def _no(self, request: PermissionRequest, title: str, paths, reason: str) -> Decision:
        """Say no through the agent's own vocabulary where it offered one.

        An explicit `reject` option tells the Local Agent it was refused *this*
        thing and may carry on; cancelling is the blunter fallback for an agent
        that offered no way to decline.
        """
        option_id = request.option_of_kind(REJECT_ONCE) or request.option_of_kind(REJECT_ALWAYS)
        return Decision(False, reason, title, option_id, tuple(paths))
