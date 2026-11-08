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

from ag2_relay_client.client import OUTCOME_MAX, TITLE_MAX, clean_label

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

    The trailer is the run of final lines that are each a title line, an
    outcome line or blank, outside any open code block. It is removed whole or
    not at all. A repeated tag is consumed; its last line wins.
    """
    if not text:
        return text or "", Labels()
    lines = text.split("\n")
    found: dict = {}
    cut = len(lines)
    while cut > 0:
        line = lines[cut - 1].strip()
        if not line:
            cut -= 1
            continue
        tag = next((t for t in (TITLE_TAG, OUTCOME_TAG) if line.startswith(t)), None)
        if tag is None:
            break
        found.setdefault(tag, line[len(tag):].lstrip(" :").strip())
        cut -= 1
    if not found or _in_fence(lines[:cut]):
        return text, Labels()
    return "\n".join(lines[:cut]).rstrip(), Labels(found.get(TITLE_TAG, ""), found.get(OUTCOME_TAG, ""))


def _in_fence(lines) -> bool:
    """Whether a code block opened in `lines` is still open at their end."""
    open_fence = ""
    for line in lines:
        s = line.strip()
        if open_fence:
            if s.startswith(open_fence):
                open_fence = ""
        elif s.startswith(_FENCE):
            open_fence = s[:3]
    return bool(open_fence)


def from_meta(meta: Any) -> Labels:
    """Labels from an ACP `_meta` mapping, when the host supplied them."""
    if not isinstance(meta, dict):
        return Labels()
    got = meta.get(META_KEY)
    if not isinstance(got, dict):
        return Labels()
    title, outcome = got.get("title"), got.get("outcome")
    return Labels(title.strip() if isinstance(title, str) else "",
                  outcome.strip() if isinstance(outcome, str) else "")


def pick(meta: Labels, trailer: Labels) -> Labels:
    """Host metadata wins field by field; the trailer fills what it left out."""
    return Labels(meta.title or trailer.title, meta.outcome or trailer.outcome)


def guarded(ctx, labels: Labels, verdict) -> Labels:
    """The labels as they will be sent, minus any the reply guard would hold.

    Cleaned first, so the guard checks the exact string that leaves: a control
    character cannot split a secret past the scan and then be removed.
    """
    def ok(value: str, limit: int) -> str:
        value = clean_label(value, limit)
        return value if value and not verdict(ctx, value).hold else ""
    return Labels(ok(labels.title, TITLE_MAX), ok(labels.outcome, OUTCOME_MAX))


__all__ = ["INSTRUCTION", "Labels", "split", "from_meta", "pick", "guarded",
           "TITLE_TAG", "OUTCOME_TAG", "META_KEY"]
