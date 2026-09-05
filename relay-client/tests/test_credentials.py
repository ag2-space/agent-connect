"""The onboarding token: how it is split, where it may come from, and what a
rotation is allowed to change (C1, C5, C6, C7, I3).

Every case here re-enacts an incident. The `%7C` cases are the Vidhu onboarding
failure of 2026-07-24: a desktop connect flow URL-encodes the separator, a naive
`split("|")` left the whole string as a bare secret with an empty URL, and the
core looked connected while never answering. The URL-change refusal is the
credential-boundary split: honoring a new gateway mid-flight puts the freshly
rotated bearer on the OLD endpoint. The alias-precedence case is the
migration-era env whose stale legacy line sat ABOVE the canonical one and made
recovery hot-swap back to the stale secret.

Run: python3 tests/test_credentials.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import logging
import tempfile
from pathlib import Path

from ag2_relay_client.credentials import (
    CredentialError,
    TokenSource,
    parse_onboarding_token,
    read_token_file,
)

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


class Captured(logging.Handler):
    """Whatever the library said, so a test can read it."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def text(self):
        return "\n".join(r.getMessage() for r in self.records)


# --- C1: the combined-token grammar
check(parse_onboarding_token("https://gw.example/relay|SECRET") ==
      ("https://gw.example/relay", "SECRET"), "literal-pipe form splits")
check(parse_onboarding_token("https://gw.example/relay%7CSECRET") ==
      ("https://gw.example/relay", "SECRET"), "%7C form splits (the desktop connect flow)")
check(parse_onboarding_token("https://gw.example/relay%7cSECRET") ==
      ("https://gw.example/relay", "SECRET"), "lowercase %7c splits too")
check(parse_onboarding_token("http://gw.example/relay|SECRET") ==
      ("http://gw.example/relay", "SECRET"), "http:// is a combined form as well")
check(parse_onboarding_token("HTTPS://gw.example/relay|SECRET") ==
      ("HTTPS://gw.example/relay", "SECRET"), "the scheme test is case-insensitive")
check(parse_onboarding_token("SECRET") == ("", "SECRET"), "a bare secret is opaque")
check(parse_onboarding_token("SEC%7CRET") == ("", "SEC%7CRET"),
      "a bare secret containing %7C is NOT split — no scheme, no separator")
check(parse_onboarding_token("SEC|RET") == ("", "SEC|RET"),
      "a bare secret containing a pipe is NOT split either")
check(parse_onboarding_token("https://gw.example/a%7Cb|SECRET") ==
      ("https://gw.example/a%7Cb", "SECRET"),
      "a literal pipe is preferred over %7C — the URL half keeps its encoding")
check(parse_onboarding_token("https://gw.example/relay|SEC%7CRET") ==
      ("https://gw.example/relay", "SEC%7CRET"),
      "the secret half is returned verbatim, never decoded")
check(parse_onboarding_token("https://gw.example/relay|") ==
      ("https://gw.example/relay", ""), "an empty secret half is reported, not guessed at")
check(parse_onboarding_token("") == ("", ""), "the empty string is not a crash")

# --- C6: alias precedence is by NAME, not by line order
with tempfile.TemporaryDirectory() as tmp:
    both = Path(tmp) / "stale-legacy-above.env"
    both.write_text("AG2_REMOTE_TOKEN=stale-legacy\nREMOTE_TASK_TOKEN=current\n")
    check(read_token_file(both).secret == "current",
          "canonical wins when the legacy line comes first")
    both.write_text("REMOTE_TASK_TOKEN=current\nAG2_REMOTE_TOKEN=stale-legacy\n")
    check(read_token_file(both).secret == "current",
          "canonical wins when the legacy line comes second")

    legacy_only = Path(tmp) / "legacy.env"
    legacy_only.write_text("AG2_REMOTE_TOKEN=only-legacy\n")
    check(read_token_file(legacy_only).secret == "only-legacy",
          "the legacy alias alone is still honored")

    repeated = Path(tmp) / "repeated.env"
    repeated.write_text("REMOTE_TASK_TOKEN=first\nREMOTE_TASK_TOKEN=second\n")
    check(read_token_file(repeated).secret == "second",
          "the last assignment of a repeated key wins, as shell sourcing does")

    shaped = Path(tmp) / "shaped.env"
    shaped.write_text('# a comment\nexport REMOTE_TASK_TOKEN="quoted"\n')
    check(read_token_file(shaped).secret == "quoted",
          "`export ` and surrounding quotes are stripped")

    raw_line = Path(tmp) / "raw.token"
    raw_line.write_text("# written by connect\n\nhttps://gw.example/relay|SECRET\n")
    parsed = read_token_file(raw_line)
    check(parsed.secret == "https://gw.example/relay|SECRET",
          "a bare onboarding string on its own line is the token")

    split_layout = Path(tmp) / "split.env"
    split_layout.write_text("REMOTE_TASK_TOKEN=SECRET\nREMOTE_TASK_URL=https://gw.example/relay\n")
    check(read_token_file(split_layout).url == "https://gw.example/relay",
          "the split layout's URL line is read, not dropped")

    # A raw onboarding line is recognised by NOT being an assignment, not by
    # holding no `=`: a base64-padded secret ends in `=`, and the "=-free" test
    # read this file as empty — a loud error at construction, a silent skip at
    # rotation, where the rotation simply never landed.
    padded = Path(tmp) / "padded.token"
    padded.write_text("# written by connect\n"
                      "https://gw.example/relay|eyJhbGciOiJIUzI1NiJ9.abc==\n")
    check(read_token_file(padded).secret ==
          "https://gw.example/relay|eyJhbGciOiJIUzI1NiJ9.abc==",
          "a raw onboarding line whose secret carries `=` is still the token")

    bare_padded = Path(tmp) / "bare-padded.token"
    bare_padded.write_text("eyJhbGciOiJIUzI1NiJ9.abc==\n")
    check(read_token_file(bare_padded).secret == "eyJhbGciOiJIUzI1NiJ9.abc==",
          "and so is a bare padded secret on its own")

    other_key = Path(tmp) / "other-key.env"
    other_key.write_text("# not ours\nSOME_OTHER_KEY=abc\nexport ANOTHER=xyz\n")
    check(read_token_file(other_key).secret == "",
          "an unrelated KEY=VALUE line is still not mistaken for a token")

    missing = Path(tmp) / "nope.env"
    check(read_token_file(missing).secret == "" and read_token_file(missing).url == "",
          "a missing file is empty, not an exception")

# --- C7 / I3: named sources, no guessed locations, no default URL
with tempfile.TemporaryDirectory() as tmp:
    combined = Path(tmp) / "combined.env"
    combined.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay%7CSECRET\n")

    src = TokenSource(token_file=combined)
    check(src.secret == "SECRET", "the file's combined token is split at construction")
    check(src.base_url == "https://gw.example/relay", "the URL travels inside the token")
    check(str(combined) in src.source, "the source names the file that supplied the token")
    check("SECRET" not in src.source and "SECRET" not in repr(src),
          "the source names the file, never the value")

    inline = TokenSource(token="https://gw.example/relay|SECRET")
    check(inline.secret == "SECRET" and "token" in inline.source.lower(),
          "an inline token is accepted and named as such")

    bare_no_url = False
    try:
        TokenSource(token="SECRET")
    except CredentialError as exc:
        bare_no_url = "url" in str(exc).lower()
    check(bare_no_url, "a bare secret with no URL is an error — there is no compiled-in default")

    nothing = False
    try:
        TokenSource()
    except CredentialError:
        nothing = True
    check(nothing, "no token and no file is an error — nothing is guessed from $HOME")

    check(TokenSource(token="SECRET", base_url="https://gw.example/relay/").base_url ==
          "https://gw.example/relay",
          "a separately supplied URL is accepted, trailing slash normalized")

# --- C7: the source is not only recorded, it is said out loud. The line that
# names which file supplied a token is what diagnoses a wrong-file bind.
with tempfile.TemporaryDirectory() as tmp:
    named = Path(tmp) / "named.env"
    named.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay%7CSECRET\n")
    heard = Captured()
    logging.getLogger("ag2_relay_client").addHandler(heard)
    logging.getLogger("ag2_relay_client").setLevel(logging.DEBUG)
    TokenSource(token_file=named)
    logging.getLogger("ag2_relay_client").removeHandler(heard)
    check(str(named) in heard.text, "construction logs which file supplied the token")
    check("SECRET" not in heard.text, "and never the token itself")

# --- a bearer is never a URL. A token line holding a URL with no separator
# parses to a bare secret that IS the URL; sending it as the bearer is the
# rotation that "kept failing auth" until the encoded separator was understood.
with tempfile.TemporaryDirectory() as tmp:
    url_as_token = False
    try:
        TokenSource(token="https://gw.example/relay", base_url="https://gw.example/relay")
    except CredentialError as exc:
        url_as_token = "url" in str(exc).lower()
    check(url_as_token, "a token that is only a URL is refused, not sent as a bearer")

    split = Path(tmp) / "url-only.env"
    split.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay\n"
                     "REMOTE_TASK_URL=https://gw.example/relay\n")
    refused_at_construction = False
    try:
        TokenSource(token_file=split)
    except CredentialError:
        refused_at_construction = True
    check(refused_at_construction,
          "a split-layout file whose token line is a URL is refused too")

    rotating = Path(tmp) / "rotating.env"
    rotating.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay%7CGOOD\n")
    src = TokenSource(token_file=rotating)
    rotating.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay\n"
                        "REMOTE_TASK_URL=https://gw.example/relay\n")
    outcome = src.reload()
    check(not outcome.rotated and src.secret == "GOOD",
          "a rotation that would make the bearer a URL is refused")

    # The URL that travels with the credential is the gateway; a separately
    # supplied one is for a bare secret, and disagreement is said out loud
    # rather than silently outranking the credential (which would then make
    # every later rotation look like a gateway change, and be refused forever).
    heard = Captured()
    logging.getLogger("ag2_relay_client").addHandler(heard)
    both = TokenSource(token="https://gw.example/relay|SECRET",
                       base_url="https://other.example/relay")
    logging.getLogger("ag2_relay_client").removeHandler(heard)
    check(both.base_url == "https://gw.example/relay",
          "the gateway inside the credential wins over a supplied one")
    check("other.example" in heard.text, "and the disagreement is logged")

    home_file = Path(tmp) / "home.env"
    home_file.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay%7CSECRET\n")
    import os as _os
    # Hermetic on every platform: `~` is whatever expanduser consults, and that
    # is NOT the same variable everywhere — POSIX reads HOME, Windows reads
    # USERPROFILE and ignores HOME entirely (since Python 3.8). Setting HOME
    # alone made this test resolve the real profile directory on Windows.
    home_var = "USERPROFILE" if _os.name == "nt" else "HOME"
    saved = _os.environ.get(home_var)
    _os.environ[home_var] = tmp
    try:
        tilde = TokenSource(token_file="~/home.env")
        check(tilde.secret == "SECRET", "a token file written with ~ is found")
    finally:
        if saved is None:
            _os.environ.pop(home_var, None)
        else:
            _os.environ[home_var] = saved

# --- C5: a rotation NEVER moves the gateway
with tempfile.TemporaryDirectory() as tmp:
    token_file = Path(tmp) / "token.env"
    token_file.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay%7CFIRST\n")
    src = TokenSource(token_file=token_file)

    captured = Captured()
    logging.getLogger("ag2_relay_client").addHandler(captured)
    logging.getLogger("ag2_relay_client").setLevel(logging.DEBUG)

    outcome = src.reload()
    check(not outcome.rotated and src.secret == "FIRST",
          "an unchanged token file is not a rotation")

    token_file.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay%7CSECOND\n")
    outcome = src.reload()
    check(outcome.rotated and src.secret == "SECOND",
          "a new secret for the same gateway is picked up live")
    check(src.base_url == "https://gw.example/relay", "the URL is unchanged by a rotation")
    check("SECOND" not in outcome.reason, "the rotation reason never carries the secret")

    captured.records.clear()
    token_file.write_text("REMOTE_TASK_TOKEN=https://other.example/relay%7CTHIRD\n")
    outcome = src.reload()
    check(not outcome.rotated, "a token file naming a DIFFERENT gateway is refused")
    check(src.secret == "SECOND", "the refused rotation leaves the running bearer alone")
    check("other.example" in captured.text and "gw.example" in captured.text,
          "the refusal is logged, naming both gateways")
    check("THIRD" not in captured.text, "the refusal log never carries the secret")

    # The split layout must hit the SAME guard: a re-onboard that rewrites only
    # the URL line would otherwise send the new bearer to the OLD gateway.
    split_file = Path(tmp) / "split.env"
    split_file.write_text("REMOTE_TASK_TOKEN=FIRST\nREMOTE_TASK_URL=https://gw.example/relay\n")
    split_src = TokenSource(token_file=split_file)
    split_file.write_text("REMOTE_TASK_TOKEN=FOURTH\nREMOTE_TASK_URL=https://other.example/relay\n")
    outcome = split_src.reload()
    check(not outcome.rotated and split_src.secret == "FIRST",
          "the split layout's URL line is guarded exactly like the combined form")

    split_file.write_text("REMOTE_TASK_TOKEN=FIFTH\nREMOTE_TASK_URL=https://gw.example/relay/\n")
    outcome = split_src.reload()
    check(outcome.rotated and split_src.secret == "FIFTH",
          "a trailing slash is not a gateway change")

    # Scheme and host are case-insensitive per RFC 3986, so a difference there
    # is NOT a different gateway. The raw string compare refused one anyway —
    # and because both sides print through `redact_url`, which lowercases them,
    # the log line named the same URL twice. A legitimate rotation could then
    # never land, for the life of the process, with auth recovery holding.
    case_file = Path(tmp) / "case.env"
    case_file.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay%7CBEFORE\n")
    case_src = TokenSource(token_file=case_file)
    case_file.write_text("REMOTE_TASK_TOKEN=https://GW.Example/relay%7CROTATED\n")
    outcome = case_src.reload()
    check(outcome.rotated and case_src.secret == "ROTATED",
          "a capitalised host is the same gateway — the rotation lands")
    case_file.write_text("REMOTE_TASK_TOKEN=HTTPS://gw.example/relay%7CAGAIN\n")
    check(case_src.reload().rotated, "and so is a capitalised scheme")
    check(case_src.base_url == "https://gw.example/relay",
          "the running base URL is still the one this client started on")

    # The path, however, is case-SENSITIVE — a different path is a different
    # gateway and stays refused.
    case_file.write_text("REMOTE_TASK_TOKEN=https://gw.example/Relay%7CNOPE\n")
    outcome = case_src.reload()
    check(not outcome.rotated and case_src.secret == "AGAIN",
          "a path whose case changed IS a different gateway, and is refused")

    # A file may name a gateway in BOTH layouts. Consulting whichever is found
    # first let a re-onboard through: the combined token still naming the old
    # gateway, the URL line naming the new one, and the freshly rotated bearer
    # then going to the old endpoint — C5's scar verbatim.
    both_layouts = Path(tmp) / "both-layouts.env"
    both_layouts.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay%7CSTART\n")
    both_src = TokenSource(token_file=both_layouts)
    both_layouts.write_text(
        "REMOTE_TASK_TOKEN=https://gw.example/relay%7CROTATED\n"
        "REMOTE_TASK_URL=https://other.example/relay\n")
    outcome = both_src.reload()
    check(not outcome.rotated and both_src.secret == "START",
          "a URL line naming a new gateway is refused even when the token names the old one")
    both_layouts.write_text(
        "REMOTE_TASK_TOKEN=https://other.example/relay%7CROTATED\n"
        "REMOTE_TASK_URL=https://gw.example/relay\n")
    outcome = both_src.reload()
    check(not outcome.rotated and both_src.secret == "START",
          "and so is the mirror image — every URL the file names is checked")

    # When redaction makes the two URLs print identically — it drops userinfo,
    # query and fragment, which are the likeliest things to have changed — the
    # message has to say WHAT differs, without printing any of it.
    queried = Path(tmp) / "queried.env"
    queried.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay?token=abc%7CSTART\n")
    queried_src = TokenSource(token_file=queried)
    check(queried_src.base_url == "https://gw.example/relay?token=abc",
          "a gateway provisioned with a query is carried as provisioned")
    queried.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay?token=xyz%7CROTATED\n")
    captured.records.clear()
    outcome = queried_src.reload()
    check(not outcome.rotated and queried_src.secret == "START",
          "a changed query is a different gateway, and is refused")
    check("query" in outcome.reason,
          "the refusal names the component that differs, since the redacted URLs match")
    check("abc" not in captured.text and "xyz" not in captured.text
          and "START" not in captured.text and "ROTATED" not in captured.text,
          "and it prints neither query, nor either secret")

    token_file.unlink()
    outcome = src.reload()
    check(not outcome.rotated and src.secret == "SECOND",
          "an unreadable token file is no rotation, and never an exception")

    no_durable = TokenSource(token="https://gw.example/relay|SECRET").reload()
    check(not no_durable.rotated, "with no durable source there is nothing to reload")

    logging.getLogger("ag2_relay_client").removeHandler(captured)

# --- C7: after a rotation, the source names the layer now supplying the bearer
with tempfile.TemporaryDirectory() as tmp:
    # Constructed from an inline token but carrying a durable file: the first
    # rotation swaps in the FILE's secret, and a source line still naming the
    # construction argument is exactly the wrong-file misdiagnosis C7 prevents.
    mixed = Path(tmp) / "mixed.env"
    mixed.write_text("REMOTE_TASK_TOKEN=https://gw.example/relay%7CFROM-FILE\n")
    mixed_src = TokenSource(token="https://gw.example/relay|FROM-ARG", token_file=mixed)
    check("construction" in mixed_src.source,
          "before any rotation the source names the argument that supplied the token")
    outcome = mixed_src.reload()
    check(outcome.rotated and mixed_src.secret == "FROM-FILE",
          "the file's secret rotates in over the constructed one")
    check(str(mixed) in mixed_src.source,
          "and the source now names the file the bearer actually comes from")
    check("FROM-FILE" not in mixed_src.source and "FROM-FILE" not in repr(mixed_src),
          "still the source, never the value")

    # A raw onboarding line whose secret is base64-padded: the rotation has to
    # land, where the "=-free line" test skipped the file in silence and left
    # the client 401ing on a revoked bearer.
    raw_padded = Path(tmp) / "raw-padded.token"
    raw_padded.write_text("https://gw.example/relay|FIRST\n")
    raw_src = TokenSource(token_file=raw_padded)
    raw_padded.write_text("https://gw.example/relay|eyJhbGciOiJIUzI1NiJ9.next==\n")
    outcome = raw_src.reload()
    check(outcome.rotated and raw_src.secret == "eyJhbGciOiJIUzI1NiJ9.next==",
          "a raw-line rotation to a padded secret lands rather than being skipped")

print("\n" + ("PASS — credentials green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
