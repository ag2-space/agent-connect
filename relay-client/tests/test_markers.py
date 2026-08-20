"""The marker grammar and its precedence (H2), plus the re-stitch (H3).

The scar is a leak in both directions. Each bridge used to hand-roll marker
recognition, so a marker one of them stripped reached the user through another
as literal `[deduped: task-123]` text; and two private file-marker expressions
matched only `/…` and `~/…` paths, so anything else was delivered verbatim.
One parser is the fix, and precedence is part of the grammar rather than of any
consumer — which is what these checks are about: skip is terminal, `[dm-only]`
is undefeatable by ORDER, and stripping is narrower than detection so prose
discussing a marker is not silently rewritten.

Run: python3 tests/test_markers.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path

from ag2_relay_client import markers

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


# --- skip is terminal (H1's vocabulary, H2's precedence)
for text, reason in (
    ("[no-send]", markers.SKIP_NO_SEND),
    ("[NO-SEND] nothing to say", markers.SKIP_NO_SEND),
    ("   \n[no-send]\nnotes for the log", markers.SKIP_NO_SEND),
    ("[REPLIED]", markers.SKIP_REPLIED),
    ("[REPLIED] the edit stands as the reply", markers.SKIP_REPLIED),
    ("[deduped: task-1755]", markers.SKIP_DEDUPED),
    ("[DEDUPED: task-1755] folded into the holder", markers.SKIP_DEDUPED),
):
    check(markers.parse(text).skip == reason, f"{text.strip()[:28]!r} is a skip")

check(markers.parse("[deduped: task-1755]").skip_id == "task-1755",
      "a deduped skip carries the holder id it named")
check(markers.parse("[replied]").skip == "",
      "[replied] is not [REPLIED] — the deliverer reads it case-sensitively, and "
      "stripping a marker the broker will not honour is how literal text leaks")

terminal = markers.parse("[no-send]\n[channel: !other:ag2.space]\n[file: /etc/passwd]")
check(terminal.skip == markers.SKIP_NO_SEND, "skip wins over everything after it")
check(terminal.redirect == "" and terminal.attachments == (),
      "and nothing else is parsed out of a body nobody will see")
check(markers.is_skip("[REPLIED]") and not markers.is_skip("here you go"),
      "is_skip answers the lease question on its own")

check(markers.parse("the answer\n[no-send]").skip == "",
      "a skip marker further down the body is not a skip — it is anchored")

# --- dm-only: detected ANYWHERE, so marker order cannot defeat it
both_ways = (
    "[dm-only]\n[channel: !shared:ag2.space]\nthe private answer",
    "[channel: !shared:ag2.space]\n[dm-only]\nthe private answer",
    "[channel: !shared:ag2.space]\nthe private answer\n\n[dm-only]\n",
)
for text in both_ways:
    parsed = markers.parse(text)
    check(parsed.dm_only, "[dm-only] is detected wherever it sits")
    check(parsed.redirect == "",
          "and it suppresses the redirect regardless of which came first")
    check("channel:" not in parsed.body and "dm-only" not in parsed.body.lower(),
          "both markers are stripped, so neither reaches the room as text")

# --- stripping is narrower than detection
prose = "the #2170 [dm-only] marker closes the leak vector"
parsed = markers.parse(prose)
check(parsed.dm_only, "an inline mention still counts for detection (fails safe)")
check(parsed.body == prose,
      "but is delivered verbatim — rewriting the owner's own prose is not a "
      "routing outcome and does not fail safe")

standalone = "before\n[dm-only]\nafter"
check(markers.parse(standalone).body == "before\nafter",
      "a standalone [dm-only] is stripped")

# ...and the code mask covers the STRIP, not only the detection. An answer whose
# fence demonstrates the marker had that fence silently emptied — the same
# rewrite of the owner's own text that the standalone-only rule above exists to
# prevent, in the one form the checks above never tried.
demo = "to keep an answer private write:\n\n```\n[dm-only]\n```\n\nand it stays a DM"
parsed = markers.parse(demo)
check(parsed.dm_only,
      "a [dm-only] shown inside a fence still ARMS the guard — over-detecting "
      "costs a redirect, under-detecting costs the privacy")
check(parsed.body == demo,
      "but the fence comes back with its line still in it: " + repr(parsed.body))

# --- the redirect's value is a room id, not "anything without a `]`"
twoline = markers.parse("[channel: !a:b\nsecond line]\nthe answer")
check(twoline.redirect == "",
      "a [channel:] value carrying a newline names no room, so nothing acts on it")
check("[channel:" not in twoline.body,
      "but it is still stripped — recognised-and-not-acted-on is the fail-safe "
      "corner, and unrecognised is the leak this module is named after")
check(markers.parse("[channel: not-a-room]\nthe answer").redirect == "",
      "and a value with no room sigil is not a room either")
check(markers.restitch("the answer", "!a:b\nx") == "the answer",
      "restitch will not build a first line with a newline in it — the deliverer "
      "would read the remainder as the body's opening")
check(markers.restitch("the answer", "!ok:ag2.space")
      == "[channel: !ok:ag2.space]\nthe answer", "a real room id restitches")

# --- redirect (H3): stripped for the consumer, re-stitched for the wire
parsed = markers.parse("[channel: !other:ag2.space]\nthe answer")
check(parsed.redirect == "!other:ag2.space", "the redirect names its room")
check(parsed.body == "the answer", "and is stripped out of the body")
check(markers.restitch(parsed.body, parsed.redirect)
      == "[channel: !other:ag2.space]\nthe answer",
      "restitch puts it back on the first line — the broker performs the move")
check(markers.restitch("the answer", "") == "the answer",
      "restitching nothing changes nothing")

check(markers.parse("the answer\n[channel: !x:y]").redirect == "",
      "a redirect that is not on the first line is not a redirect")

# --- attach markers: three spellings, anywhere, document order
parsed = markers.parse(
    "here it is [file: /a/one.png] and [send: /a/two.txt]\nplus [attach: /a/3.md]"
)
check(parsed.attachments == ("/a/one.png", "/a/two.txt", "/a/3.md"),
      "all three spellings are recognised, in document order")
check("[file:" not in parsed.body and "[send:" not in parsed.body,
      "and stripped from the text the room reads")
check(parsed.body == "here it is  and \nplus", "the prose around them survives")

check(markers.parse("[file: ~/notes/a.md]").attachments == ("~/notes/a.md",),
      "a `~` path is a path — the old private expressions matched only `/…` and "
      "`~/…`, and delivered everything else as literal text")
check(markers.parse("[file: relative/a.md]").attachments == ("relative/a.md",),
      "and so is a relative one; judging it is the allowlist's job, not the parser's")

# --- a marker inside code is being SHOWN, not issued. On this transport that
# is not cosmetic: the attach marker is the entrance to egress.
shown = "to send a file write:\n\n```\n[file: /etc/passwd]\n```\n\ndone"
parsed = markers.parse(shown)
check(parsed.attachments == (), "a marker in a fenced block issues nothing")
check("[file: /etc/passwd]" in parsed.body,
      "and stays visible, which is the whole point of showing it")

inline = "write `[file: /etc/passwd]` on its own line"
parsed = markers.parse(inline)
check(parsed.attachments == (), "a marker in an inline code span issues nothing")
check("[file:" in parsed.body, "and is delivered verbatim")

indented = "example:\n\n    [file: /etc/passwd]\n\ndone"
check(markers.parse(indented).attachments == (),
      "a marker in an indented block issues nothing")

mixed = "here [file: /a/real.png] and shown `[file: /etc/passwd]`"
parsed = markers.parse(mixed)
check(parsed.attachments == ("/a/real.png",),
      "a live marker beside a shown one: only the live one is acted on")
check("/etc/passwd" in parsed.body and "/a/real.png" not in parsed.body,
      "and only the live one is stripped")

# --- the mask must be NARROW. A mask that over-reaches does not decline to act,
# it hides a marker in a body it then delivers verbatim — which is exactly the
# "literal marker text reaches the user" scar, arrived at from the other side.
stray = "a ` b\n\nc [file: /tmp/a.png] d ` e\n[file: /tmp/b.png]"
parsed = markers.parse(stray)
check(parsed.attachments == ("/tmp/a.png", "/tmp/b.png"),
      "one loose backtick does not pair across a blank line — a code span cannot "
      "contain one, and pretending otherwise masks every marker in between: "
      + repr(parsed.attachments))
check("[file:" not in parsed.body,
      "so both are stripped, and neither is delivered as literal marker text")

wrapped = "a `span that\nwraps one line [file: /etc/passwd]` still masks"
check(markers.parse(wrapped).attachments == (),
      "while a span crossing a SINGLE newline still masks — that much is "
      "CommonMark, and the narrowing does not reach it")

listy = "- item\n    [file: /tmp/a.png]"
parsed = markers.parse(listy)
check(parsed.attachments == ("/tmp/a.png",),
      "a four-space continuation of a list item is a continuation, not an "
      "indented code block: indented code cannot interrupt a paragraph")
check("[file:" not in parsed.body, "so its marker is stripped, not left as text")
check(markers.parse("    [file: /etc/passwd]\nafter").attachments == (),
      "while an indented block that opens the body still issues nothing")

# --- nothing at all
check(markers.parse("").body == "" and markers.parse(None).actions == (),
      "an empty body parses to nothing, and does not raise")
check(markers.parse("just an answer").body == "just an answer",
      "prose with no markers is unchanged")
check(markers.parse("[unknown-marker] hello").body == "[unknown-marker] hello",
      "an unknown marker is not a marker — it is text, and stays text")

# --- an UNTERMINATED marker is the "neither" case (review 2026-08-20) -------
# `_ATTACH_RE` needs a closing `]`, so a body that ran out before one — a token
# limit, a crashed generation, a truncated line — fell through both detection
# and stripping and was delivered verbatim. The tail is an absolute local path.
# Third place this exact hole was found: `media.py` and `envelope.py` had it too.
r = markers.parse("Here you go.\n\n[file: /home/nikita/.ssh/id_rsa")
check(r.body == "Here you go.", "an unterminated attach marker is cut, not delivered")
check(".ssh" not in r.body, "so no local path reaches the room")
check(r.actions == (), "it named nothing actionable — there is no action to take")

r = markers.parse("talk [channel: !room")
check(r.body == "talk", "an unterminated channel marker goes the same way")
r = markers.parse("x [deduped: abc")
check(r.body == "x", "and an unterminated deduped marker too")

r = markers.parse("ok [file: /tmp/a.png] done")
check(len(r.actions) == 1 and r.body == "ok  done",
      "a marker that IS closed still parses — \\Z anchoring eats nothing early")

fenced = "see:\n```\n[file: /tmp/x\n```"
check(markers.parse(fenced).body == fenced,
      "inside a fence it is being shown, not issued — left untouched")

r = markers.parse("[file: /tmp/only")
check(r.body == "", "a body that is only an unterminated marker degrades to empty")

print("\n" + ("PASS — markers green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
