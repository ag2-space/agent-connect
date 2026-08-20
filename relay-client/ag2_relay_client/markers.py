"""The one parser for the result-body marker protocol (H2).

A result body is not only prose. It can carry instructions to the delivery
side — *don't post this*, *post it in that room instead*, *put this file in the
room with it* — and those instructions are a grammar. This module is the only
place that grammar is written down.

**One parser, because every copy of it drifted.** Each bridge used to hand-roll
its own recognition, and they diverged in exactly the way copies do: telegram
never recognized `[deduped:]`, slack never recognized `[channel:]`, and two
private file-marker expressions matched only `/…` and `~/…` paths. A marker one
consumer stripped therefore reached the user as **literal marker text** through
another. So: consumers apply the actions they support and strip the rest, and
nobody writes a second regex.

The precedence rules are part of the grammar, not of any consumer:

- **Skip is terminal.** `[no-send]` / `[REPLIED]` / `[deduped: <id>]` at the
  body's start end the parse; no redirect and no attachment is extracted from a
  body nobody will see. What a skip does *not* end is the lease — see H1 and
  `outbound.py`: the marker body is still POSTed, because a skipped
  `POST /v1/results` leaves the lease to expire and the task to be re-served.
- **`[dm-only]` is detected ANYWHERE**, so no ordering of markers can defeat the
  privacy guard, and it suppresses a `[channel:]` redirect: a body carrying
  private data must not be redirectable into a shared room.
- **Stripping is narrower than detection.** Only a *standalone* `[dm-only]` —
  alone on its line — is removed. Removing every occurrence also removed the
  literal from prose that merely *discussed* the marker, silently editing
  owner-facing text. Over-detecting fails safe; silently rewriting a body does
  not.
- **A marker inside markdown code is being shown, not issued.** Fenced blocks,
  indented blocks and inline code spans are masked before attachment markers are
  collected, so an answer explaining `[file: /etc/passwd]` does not try to send
  it. On this transport that is not a formatting nicety: the attach marker is
  the entrance to egress. The mask covers *stripping* as well as detection: an
  answer whose code block demonstrates `[dm-only]` gets that block back with the
  line still in it, because emptying somebody's example is the same silent
  rewrite the standalone-only rule above exists to prevent.

The masking has one invariant, and it is the one worth checking a change
against: **a marker is either issued and stripped, or masked and left visible —
never neither.** "Neither" is how a marker reaches the user as literal text,
which is the scar at the top of this file. That is why the code-span and
indented-block rules below are written to be *narrow*: a mask that over-reaches
does not merely decline to act, it hides a marker in a body it then delivers
verbatim.

Not implemented here, deliberately: sutando's `**[core: N]**` reply header peel.
That header is a pool-core prose convention, not something that exists on this
wire, and a library that speaks the wire should not know about it.
"""
from __future__ import annotations

import re
from typing import List, NamedTuple, Optional, Tuple

#: The three skip reasons, as the broker's deliverer spells them.
SKIP_NO_SEND = "no-send"
SKIP_REPLIED = "REPLIED"
SKIP_DEDUPED = "deduped"

#: Skip markers, anchored at the body's start (leading whitespace allowed).
#:
#: `[REPLIED]` is case-sensitive and the other two are not, which is inherited
#: rather than chosen: it is how the deliverer reads them, and a parser that is
#: more permissive than the deliverer would strip a marker the broker then
#: fails to honour — the exact "literal marker text reaches the user" failure
#: this module exists to prevent, in its most confusing form.
_SKIP_PATTERNS = (
    (re.compile(r"^\s*\[no-send\]\s*", re.IGNORECASE), SKIP_NO_SEND),
    (re.compile(r"^\s*\[REPLIED\]\s*"), SKIP_REPLIED),
    (re.compile(r"^\s*\[deduped:\s*([^\]]+)\]\s*", re.IGNORECASE), SKIP_DEDUPED),
)

#: What a room id may look like — the one definition in this library. `roomops`
#: imports it: the id it puts in a URL path segment and the id read out of a
#: `[channel:]` redirect are the same thing, and two spellings of one grammar is
#: the drift this module exists to have stopped.
ROOM_ID_RE = re.compile(r"^[!#][^\s/\x00-\x1f\x7f]{1,254}$")

#: The redirect marker, on the first non-empty line. `.match()` anchors at the
#: string start on its own, so no MULTILINE flag is wanted here.
#:
#: The value is caught loosely and judged afterwards, on purpose. Tightening the
#: capture instead would leave `[channel: <two lines>]` unrecognised, and an
#: unrecognised marker is delivered as literal text — the failure this module is
#: named after. Recognised-and-stripped-without-acting is the fail-safe corner:
#: nothing is redirected, and nothing leaks.
_REDIRECT_RE = re.compile(r"^\s*\[channel:\s*([^\]]+)\]\s*\n?")

#: Detected anywhere — that is what makes the guard order-independent.
_DMONLY_RE = re.compile(r"\[dm-only\]", re.IGNORECASE)

#: Stripped only when standalone. See the module docstring: detection and
#: stripping are deliberately different scopes.
_DMONLY_STRIP_RE = re.compile(
    r"^[ \t]*\[dm-only\][ \t]*\r?\n?", re.IGNORECASE | re.MULTILINE
)

#: The three spellings of "put this file in the room". Anywhere in the body, in
#: document order.
_ATTACH_RE = re.compile(r"\[(?:file|send|attach):\s*([^\]]+)\]")

#: An **unterminated** marker: an opening tag with no closing `]` before the end
#: of the body. `_ATTACH_RE` cannot match one, so without this the tail — an
#: absolute local path, usually — falls through both the detection and the strip
#: and is delivered to the room verbatim. That is the "neither" case the
#: invariant at the top of this file forbids, and it is the third place this
#: exact hole has been found: `media.py` (`2d9635c`) and `envelope.py` had it
#: too, both fixed the same way. A body is truncated for ordinary reasons — a
#: token limit, a crashed generation — so this is not an attack shape, it is
#: Tuesday. Anchored to `\Z`: a tag that *is* closed later belongs to the
#: regexes above and must not be eaten here.
_UNTERMINATED_RE = re.compile(
    r"\[(?:file|send|attach|channel|deduped):[^\]]*\Z"
)

_FENCE_RE = re.compile(r"^\s{0,3}(?:```|~~~)")

#: A run of N backticks closed by the same run. Matching the *span* rather than
#: the characters beside a marker is what catches one in the middle of a span.
#:
#: A span may cross a line, and may **not** cross a blank line — that is
#: CommonMark, and here it is load-bearing rather than pedantic. Without the
#: guard one stray backtick anywhere in a reply pairs with the next one however
#: far away, and every marker between them is masked: not issued, and therefore
#: not stripped, and therefore delivered to the user as literal `[file: …]`
#: text. Over-masking is not the safe direction. It is the leak.
_SPAN_RE = re.compile(
    r"(?<!`)(`+)(?!`)(?:(?!\1)(?!\n[ \t]*(?:\n|\Z)).)+?\1(?!`)", re.DOTALL
)

#: What a redirect looks like when it is put back on the wire (H3).
CHANNEL_FORM = "[channel: {room}]"


class Action(NamedTuple):
    """One thing the delivery side should do with this body."""

    #: `"skip"` | `"dm-only"` | `"redirect"` | `"attach"`.
    kind: str
    #: The skip reason, the target room, or the path exactly as written.
    value: str
    #: For a `deduped` skip, the holder id it named.
    extra: str = ""


class ParseResult(NamedTuple):
    """The body with every known marker taken out, and what they asked for."""

    body: str
    actions: Tuple[Action, ...] = ()

    def first(self, kind: str) -> Optional[Action]:
        for action in self.actions:
            if action.kind == kind:
                return action
        return None

    @property
    def skip(self) -> str:
        """The skip reason, or `""`. Terminal: nothing else was parsed."""
        found = self.first("skip")
        return found.value if found else ""

    @property
    def skip_id(self) -> str:
        """The holder id a `[deduped: <id>]` named, or `""`."""
        found = self.first("skip")
        return found.extra if found else ""

    @property
    def dm_only(self) -> bool:
        return self.first("dm-only") is not None

    @property
    def redirect(self) -> str:
        """The room a `[channel:]` named, or `""` when there was none — and
        also `""` when `[dm-only]` suppressed one."""
        found = self.first("redirect")
        return found.value if found else ""

    @property
    def attachments(self) -> Tuple[str, ...]:
        """Every path the body named, in document order, exactly as written.

        As written, and unjudged: deciding whether a path may leave the machine
        is `egress.EgressAllowlist`'s job and happens at the upload sink. A
        parser that pre-approved paths would put the sanitizer somewhere no
        reviewer looks for it.
        """
        return tuple(a.value for a in self.actions if a.kind == "attach")


def parse(text: Optional[str]) -> ParseResult:
    """Read one result body. Never raises; an unparseable body is just prose."""
    if not text:
        return ParseResult(body="", actions=())

    actions: List[Action] = []
    body = text

    # 1. SKIP — terminal. A body nobody will see has no redirect to honour and
    # no file to send, so the parse stops here.
    for pattern, reason in _SKIP_PATTERNS:
        found = pattern.match(body)
        if found:
            extra = found.group(1).strip() if reason == SKIP_DEDUPED else ""
            return ParseResult(body="", actions=(Action("skip", reason, extra),))

    # 2. DM-ONLY — before redirect, because it suppresses one.
    #
    # Detection is unmasked: a `[dm-only]` shown inside a code fence still turns
    # the privacy guard on, because over-detecting costs a redirect and
    # under-detecting costs the privacy. Stripping is masked, because a code
    # block is being shown and emptying it is a silent rewrite of the owner's
    # own text — the same asymmetry as the standalone-only rule, one level down.
    dm_only = bool(_DMONLY_RE.search(body))
    if dm_only:
        actions.append(Action("dm-only", ""))
        outside_code = _mask(body)
        body = _DMONLY_STRIP_RE.sub(
            lambda m: m.group(0) if outside_code(m.start()) else "", body
        )

    # 3. REDIRECT — first non-empty line. Under dm-only the marker is still
    # *stripped* (so it cannot leak into the room as literal text) but no
    # redirect action is emitted: stripping without acting is the fail-safe
    # direction, and acting on it is the leak. A value that is not a room id
    # lands in the same corner, for the same reason: it cannot be delivered to,
    # and it must not be repeated at the user.
    found = _REDIRECT_RE.match(body)
    if found:
        named = found.group(1).strip()
        if not dm_only and ROOM_ID_RE.match(named):
            actions.append(Action("redirect", named))
        body = body[found.end():]

    # 4. ATTACH — anywhere, document order, code regions masked.
    in_code = _mask(body)

    for found in _ATTACH_RE.finditer(body):
        if not in_code(found.start()):
            actions.append(Action("attach", found.group(1).strip()))

    body = _ATTACH_RE.sub(
        lambda m: m.group(0) if in_code(m.start()) else "", body
    )

    # 5. UNTERMINATED — an opening tag the body ran out before closing. It named
    # nothing this client can act on, so there is no action to add; what matters
    # is that it does not reach the room. Masked bodies are left alone: inside a
    # fence it is being shown, not issued.
    unterminated = _UNTERMINATED_RE.search(body)
    if unterminated is not None and not in_code(unterminated.start()):
        body = body[: unterminated.start()]

    return ParseResult(body=body.strip(), actions=tuple(actions))


def restitch(body: str, redirect: str) -> str:
    """Put a `[channel:]` redirect back on the first line of a POSTed body (H3).

    The client cannot perform the room move itself and does not try: on this
    transport the broker's deliverer honours the marker, so the parser strips it
    for the consumer's benefit and this puts it back for the wire's. Losing it
    between those two steps delivers a private answer to the room that asked
    rather than the room the agent named.
    """
    room = (redirect or "").strip()
    if not room:
        return body
    if not ROOM_ID_RE.match(room):
        # A "first line" with a newline in it is not a first line, and the
        # deliverer would read the remainder as the body's opening. `parse`
        # never yields one; a consumer that assembled a redirect by hand can.
        return body
    return CHANNEL_FORM.format(room=room) + "\n" + (body or "")


def is_skip(text: Optional[str]) -> bool:
    """Would this body complete the lease without a user-visible post? (H1)"""
    return bool(parse(text).skip)


def _mask(text: str):
    """A `position -> bool` predicate: is this offset inside markdown code?

    Built once per body and closed over the body it was built from, because the
    body is rewritten between steps and an offset into the old one means
    nothing. Everything that masks — detection and stripping alike — asks this.
    """
    lines_in_code, spans = _code_regions(text)

    def inside(position: int) -> bool:
        if text.count("\n", 0, position) in lines_in_code:
            return True
        return any(start <= position < end for start, end in spans)

    return inside


def _code_regions(text: str):
    """`(line indices inside code blocks, inline-span character ranges)`.

    An unclosed fence swallows the rest of the body on purpose: the alternative
    is treating shown-but-unterminated example text as a live directive, and on
    this transport a live `[file:]` directive is an upload.

    An indented line is only a code block when it does not interrupt a
    paragraph — CommonMark's rule, and the one that keeps a wrapped list item
    out of here. `"- item\n    [file: X]"` is a continuation line of the list
    item, indented for readability, and reading it as code masks a live marker
    into a body that then delivers it as literal text. The blank line before an
    indented block is what makes it a block.
    """
    lines = text.split("\n")
    marked = set()
    fenced = False
    in_paragraph = False
    for index, line in enumerate(lines):
        if _FENCE_RE.match(line):
            fenced = not fenced
            marked.add(index)
            in_paragraph = False
            continue
        if fenced:
            marked.add(index)
            continue
        if not line.strip():
            in_paragraph = False
            continue
        if line.startswith(("    ", "\t")) and not in_paragraph:
            marked.add(index)
            continue
        in_paragraph = True
    spans = [(m.start(), m.end()) for m in _SPAN_RE.finditer(text)]
    return marked, spans
