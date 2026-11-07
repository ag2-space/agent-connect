"""What a collaborator's Turn may post without the owner (`docs/adr/0003`).

A collaborator's prompt can steer the agent, and the agent holds the owner's
files and credentials, so a collaborator's reply is checked before it reaches
the room. A reply is held for the owner when it carries:

- a `[channel:]` redirect, always: it moves the reply somewhere the sender chose;
- with the room's sensitive data filter on (the default): a `[file:]`, which
  cannot be scanned, or text matching a known secret format. A scan that fails
  holds the reply too.

A held reply is posted only if the owner releases it from an approval card. No
card configured, a timeout or "Discard" means it is never posted. The detector
matches known secret formats only; it is a backstop, not containment.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional, Tuple

from ag2_relay_client import markers

from . import hitl_seam
from .events import TurnContext

OWNER = "owner"

#: Known secret formats, from Sutando's `chat_secret_filter` fallback list.
PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("AWS access key", re.compile(r"AKIA[A-Z0-9]{16}")),
    ("GitHub token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{36,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Matrix access token", re.compile(r"syt_[A-Za-z0-9_-]{20,}")),
    ("JSON Web Token", re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    ("Slack token", re.compile(r"xox[abps]-[A-Za-z0-9-]+")),
    ("OpenAI key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_-]{32,}")),
    ("Stripe key", re.compile(r"[sr]k_(?:live|test)_[A-Za-z0-9]{24,}")),
    ("Discord bot token", re.compile(
        r"[MNO][A-Za-z\d_-]{23,}\.[A-Za-z\d_-]{6,}\.[A-Za-z\d_-]{27,}")),
    ("Telegram bot token", re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

RELEASE = "release"
DISCARD = "discard"
#: Longest display name shown beside the attested Matrix id on a card.
DISPLAY_NAME_MAX = 40


def secret_types(text: str) -> Tuple[str, ...]:
    """The known secret formats `text` contains, in `PATTERNS` order."""
    return tuple(name for name, pattern in PATTERNS if pattern.search(text or ""))


def guarded(ctx: TurnContext) -> bool:
    return ctx.access_tier != OWNER and ctx.collaborator


@dataclass(frozen=True)
class Verdict:
    hold: bool
    why: Tuple[str, ...] = ()


def verdict(ctx: TurnContext, body: str, scan=secret_types) -> Verdict:
    """Whether a collaborator's reply must wait for the owner, and why."""
    if not guarded(ctx):
        return Verdict(False)
    why = []
    kinds = {action.kind for action in markers.parse(body or "").actions}
    if "redirect" in kinds:
        why.append("a redirect to another room")
    if ctx.sensitive_data_filter:
        if "attach" in kinds:
            why.append("a file, which cannot be scanned")
        try:
            why += [f"a possible {name}" for name in scan(body)]
        except Exception:  # noqa: BLE001 — an unscannable reply is held
            why.append("a scan that failed")
    return Verdict(bool(why), tuple(why))


def _display_name(raw: str) -> str:
    """A sender-set display name, safe to show: no control or format characters
    (bidi overrides included), one line, capped."""
    kept = "".join(" " if unicodedata.category(c)[0] == "C" else c for c in raw)
    name = " ".join(kept.split())
    return name if len(name) <= DISPLAY_NAME_MAX else name[:DISPLAY_NAME_MAX - 1] + "…"


def asked_by(ctx: TurnContext) -> str:
    """Who a non-owner Turn is for, led by the attested Matrix id; `""` for the owner."""
    if ctx.access_tier == OWNER:
        return ""
    name = _display_name(ctx.sender_name)
    return f'{ctx.user_id} ("{name}")' if name and name != ctx.user_id else ctx.user_id


async def ask_owner(ctx: TurnContext, why: Tuple[str, ...],
                    timeout: Optional[float] = None,
                    poll: float = hitl_seam.DEFAULT_POLL_S) -> bool:
    """Ask the owner on a card whether to post a held reply. False unless released."""
    workspace = hitl_seam.configured()
    if workspace is None:
        return False
    request = SimpleNamespace(
        tool_call={
            "toolCallId": f"release-{ctx.task_id or uuid.uuid4().hex}",
            "title": "post a collaborator's reply containing " + ", ".join(why),
            "kind": "release",
        },
        options=[
            {"optionId": RELEASE, "name": "Post it", "kind": "allow_once"},
            {"optionId": DISCARD, "name": "Discard", "kind": "reject_once"},
        ],
    )
    chosen = await hitl_seam.escalate(
        request,
        session="|".join(ctx.session_key),
        workspace=workspace,
        timeout=hitl_seam.timeout_s() if timeout is None else timeout,
        poll=poll,
        asked_by=asked_by(ctx),
    )
    return chosen == RELEASE
