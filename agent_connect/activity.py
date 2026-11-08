"""The owner's Activity row labels: a short title and a one-line outcome.

The agent writes them as two trailer lines at the very end of its reply, which
are removed before the reply is posted. A host that can attach them to the ACP
prompt response as `_meta["ag2/activity"]` takes precedence. Labels are
optional: a reply without them is posted unchanged and the row shows the
request text, as before.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TITLE_TAG = "[activity:title]"
OUTCOME_TAG = "[activity:outcome]"
META_KEY = "ag2/activity"

INSTRUCTION = (
    "End your reply with two lines for your owner's Activity page, which "
    "agent-connect removes before posting: "
    f"`{TITLE_TAG} <what the task is, imperative, at most 60 characters>` and "
    f"`{OUTCOME_TAG} <what came of it, past tense, one sentence, at most 120 "
    "characters>`. Write them outside any code block. Add them after NO_REPLY too.\n"
)

_FENCE = ("```", "~~~")


@dataclass(frozen=True)
class Labels:
    title: str = ""
    outcome: str = ""

    def __bool__(self) -> bool:
        return bool(self.title or self.outcome)


def split(text: str) -> "tuple[str, Labels]":
    """The reply without its trailer, and the labels the trailer carried.

    Only the final lines count: one title and one outcome, in either order,
    adjacent, after any trailing blank lines, and not inside an open code block.
    Anything else leaves the text untouched and returns no labels.
    """
    if not text:
        return text or "", Labels()
    lines = text.rstrip().split("\n")
    found: dict = {}
    cut = len(lines)
    while cut > 0 and len(found) < 2:
        line = lines[cut - 1].strip()
        tag = next((t for t in (TITLE_TAG, OUTCOME_TAG) if line.startswith(t)), None)
        if tag is None or tag in found:
            break
        found[tag] = line[len(tag):].strip()
        cut -= 1
    if not found:
        return text, Labels()
    head = lines[:cut]
    if sum(1 for ln in head if ln.strip().startswith(_FENCE)) % 2:
        return text, Labels()
    return "\n".join(head).rstrip(), Labels(found.get(TITLE_TAG, ""), found.get(OUTCOME_TAG, ""))


def from_meta(meta: Any) -> Labels:
    """Labels from an ACP `_meta` mapping, when the host supplied them."""
    if not isinstance(meta, dict):
        return Labels()
    got = meta.get(META_KEY)
    if not isinstance(got, dict):
        return Labels()
    title, outcome = got.get("title"), got.get("outcome")
    return Labels(title if isinstance(title, str) else "",
                  outcome if isinstance(outcome, str) else "")


def pick(meta: Labels, trailer: Labels) -> Labels:
    """Host metadata wins field by field; the trailer fills what it left out."""
    return Labels(meta.title or trailer.title, meta.outcome or trailer.outcome)


def guarded(ctx, labels: Labels, verdict) -> Labels:
    """Drop any label the reply guard would hold for this requester."""
    def ok(value: str) -> str:
        return value if value and not verdict(ctx, value).hold else ""
    return Labels(ok(labels.title), ok(labels.outcome))


__all__ = ["INSTRUCTION", "Labels", "split", "from_meta", "pick", "guarded",
           "TITLE_TAG", "OUTCOME_TAG", "META_KEY"]
