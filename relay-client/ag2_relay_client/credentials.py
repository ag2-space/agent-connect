"""The onboarding token: splitting it, naming where it came from, and refusing
the one change a rotation may not make.

The combined onboarding form is `<url>|<secret>` — the gateway travels *inside*
the credential, which is why nothing service-specific is compiled into this
package (I3). The separator arrives as a literal `|`, or as `%7C`/`%7c` when the
desktop connect flow URL-encodes it; a parse that knows only the first form
leaves the whole string as a bare secret with an empty URL, and the client fails
at startup while the product it belongs to looks connected (2026-07-24).

Rotation is a change of *secret*, never of gateway (C5). A durable token source
that suddenly names a different gateway is a reconfiguration — honoring it live
would leave long-lived callers on the old base URL while the freshly rotated
bearer went to the new one, or worse, sent the new bearer to the old endpoint.
So the swap is refused, loudly, and the client keeps running where it is; a
restart is how a gateway moves.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
from pathlib import Path
from typing import NamedTuple, Optional, Tuple, Union

from .state import redact_url

log = logging.getLogger(__name__)

#: The URL-encoded separator, as the desktop connect flow writes it.
_ENCODED_SEPARATOR_RE = re.compile(r"%7[Cc]")

#: Token-file key names, canonical first. Precedence is by NAME, not by line
#: order: a migration-era file with a stale legacy line above the canonical one
#: made auth recovery hot-swap back to the stale secret (C6).
_TOKEN_KEYS = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")
_URL_KEYS = ("REMOTE_TASK_URL", "AG2_REMOTE_URL")

#: A dotenv assignment: an environment-variable name, then a single `=`. What
#: tells a `KEY=VALUE` line from a raw onboarding string, which a plain "contains
#: an `=`" test cannot — a base64-padded secret ends in `=`, and a token line
#: holding one was skipped in silence, so a valid rotation never landed.
#:
#: `=(?!=)` is why base64 padding is not read as an assignment even when the
#: secret before it happens to spell a legal name: no dotenv writer emits `K==v`,
#: and by the time this pattern is consulted every key this module knows has
#: already been looked for, so the only lines left are a token or somebody
#: else's.
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(?!=)")

#: The component order a gateway URL is compared in. Names, because the refusal
#: below has to be able to *say* which of them differs without printing it.
_URL_PARTS = ("scheme", "userinfo", "host", "port", "path", "query", "fragment")

PathLike = Union[str, "os.PathLike[str]"]


class CredentialError(Exception):
    """The client cannot be constructed from what it was given."""


def _looks_like_url(value: str) -> bool:
    """A bearer is never a URL.

    A token line holding a URL and no separator parses to a bare secret that
    *is* that URL — and sending it as the bearer is how a valid rotation kept
    failing auth until the encoded separator was understood.
    """
    return value.lower().startswith(("http://", "https://"))


def _components(url: str):
    """A gateway URL taken apart, spelling preserved.

    Deliberately *not* `redact_url`: this is what the comparison and the refusal
    reason are built from, so nothing may be dropped — a `?token=` that changed
    is a different gateway, and only a full decomposition can see it. Nothing
    here is ever printed; only the component NAMES are.

    The host is read out of the authority by hand rather than through
    `parts.hostname`, which lowercases: a case difference has to stay visible to
    the describer even though it is not a gateway change.
    """
    try:
        parts = urllib.parse.urlsplit((url or "").strip().rstrip("/"))
    except Exception:  # noqa: BLE001 — an unsplittable URL still has to compare
        return None
    userinfo, at, authority = parts.netloc.rpartition("@")
    if authority.startswith("["):  # an IPv6 literal keeps its brackets
        close = authority.find("]") + 1
        host, port = authority[:close], authority[close:].lstrip(":")
    else:
        host, _, port = authority.partition(":")
    return {
        "scheme": parts.scheme,
        "userinfo": userinfo if at else "",
        "host": host,
        "port": port,
        "path": parts.path.rstrip("/"),
        "query": parts.query,
        "fragment": parts.fragment,
    }


def _canonical_gateway(url: str) -> str:
    """A comparison key two spellings of the same gateway share.

    Scheme and host are case-insensitive per RFC 3986, so a difference there is
    not a different gateway — and a token file rewritten with a capitalised host
    was refused as a gateway move for the life of the process, which means the
    rotation never landed and auth recovery held forever. Everything else is
    compared as written: the path is case-SENSITIVE, and userinfo, port, query
    and fragment each change where or as whom this client talks.

    Not a URL — a key. It is never shown to anyone.
    """
    parts = _components(url)
    if parts is None:
        return (url or "").strip().rstrip("/")
    parts["scheme"] = parts["scheme"].lower()
    parts["host"] = parts["host"].lower()
    return "\x00".join(parts[name] for name in _URL_PARTS)


def _difference(named: str, running: str) -> str:
    """Which components of two gateway URLs differ, named and never printed.

    `redact_url` lowercases scheme and host and drops userinfo, query and
    fragment — the five carriers most likely to be what changed — so a refusal
    that prints both redacted URLs can print the same string twice and call them
    different. This is the part of the message that still says something, while
    keeping the no-secret-leak property absolute.
    """
    left, right = _components(named), _components(running)
    if left is None or right is None:
        return "differing in spelling"
    differing = []
    for name in _URL_PARTS:
        one, other = left[name], right[name]
        if one == other:
            continue
        differing.append(name + " case" if one.lower() == other.lower() else name)
    if not differing:
        return "differing in spelling"
    return "differing in " + " and ".join(differing)


def _name_file(path) -> str:
    """How a token file is named wherever this module mentions one, so the C7
    source line reads the same at construction and after a rotation."""
    return f"the token file {path}"


class TokenFile(NamedTuple):
    """What a durable token source held: the URL line of a split-layout file
    (bare secret plus a separate URL line), and the onboarding string.

    The pair is ordered `(url, secret)` to match `parse_onboarding_token`: one
    concept, one order, so a positional call site cannot silently swap them.
    """

    url: str
    secret: str


class Rotation(NamedTuple):
    """The outcome of re-reading the durable token source.

    `reason` is written to be logged: it names sources and gateways, never
    secrets.
    """

    rotated: bool
    reason: str


def parse_onboarding_token(raw: str) -> Tuple[str, str]:
    """Split an onboarding string into `(url_from_token, secret)`.

    The split is a split and nothing else — neither half is decoded or otherwise
    mutated, so a bearer that itself contains `%7C` or `|` survives intact. Only
    the combined form, which begins with an `http(s)://` scheme, carries a
    separator to split on; a bare secret is opaque and comes back untouched even
    when it contains one.

    A literal `|` is preferred over `%7C` when both appear: a raw pipe cannot
    legally occur inside a URL, so where one exists it *is* the separator — and
    preferring it keeps a URL half that carries an encoded `%7C` intact.
    """
    raw = raw or ""
    if not raw.lower().startswith(("http://", "https://")):
        return "", raw  # bare secret — opaque, never touched
    pipe = raw.find("|")
    if pipe != -1:
        return raw[:pipe], raw[pipe + 1:]
    encoded = _ENCODED_SEPARATOR_RE.search(raw)
    if encoded is None:
        # A scheme but no separator: not a combined form. The caller's
        # missing-URL guard is what speaks about it.
        return "", raw
    return raw[:encoded.start()], raw[encoded.end():]


def _value_for(text: str, keys: Tuple[str, ...]) -> str:
    """The value of the first of `keys` assigned anywhere in `text`.

    Every line is read before precedence is applied, so the canonical name wins
    regardless of where in the file it sits; within one name, the last
    assignment wins, matching shell sourcing.
    """
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        for key in keys:
            if line.startswith(key + "="):
                found[key] = line[len(key) + 1:].strip().strip("'\"")
    for key in keys:
        if found.get(key):
            return found[key]
    return ""


def read_token_file(path: PathLike) -> TokenFile:
    """Read a durable token source. Missing, unreadable and empty are all the
    same answer — `TokenFile("", "")` — because to every caller they mean the
    same thing: no rotation to apply.

    Two shapes are accepted: a dotenv-style file carrying a `REMOTE_TASK_TOKEN=`
    line (legacy `AG2_REMOTE_TOKEN=` honored, optional `export `, surrounding
    quotes stripped), or the raw onboarding string alone on the first
    non-comment line that is not an assignment.

    "Not an assignment" and "holds no `=`" are not the same test, which is what
    the second shape used to check: a base64-padded secret ends in `=`, so a file
    holding the bare onboarding string read as empty — a loud error at
    construction, and a silent skip at rotation, where it meant the rotation
    never landed and the client kept 401ing on a revoked bearer. A line is an
    assignment when the text before its first `=` is a valid environment-variable
    name; `https://gw/relay|eyJ…abc==` is not one, and `SOME_OTHER_KEY=abc` is,
    so it stays ignored as it always was.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return TokenFile("", "")
    secret = _value_for(text, _TOKEN_KEYS)
    if not secret:
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if _ASSIGNMENT_RE.match(line):
                continue  # some other key's line; this file names no token
            secret = line
            break
    return TokenFile(_value_for(text, _URL_KEYS), secret)


class TokenSource:
    """One bearer and the gateway it belongs to, plus the durable file it can be
    rotated from.

    Nothing is guessed (C7). The token arrives as a value or as an explicitly
    named file; a bare-home fallback is the one lookup that could silently bind
    the wrong identity after a reinstall or an account switch, so it does not
    exist here. Which layer supplied the credential *is* recorded — that line is
    load-bearing when diagnosing a wrong-file bind — and the value never is.

    There is no default gateway (I3): the base URL is discovered at provisioning
    time, travelling inside the onboarding token or alongside it. A client that
    cannot say where it is pointed does not start.
    """

    def __init__(
        self,
        token: str = "",
        token_file: Optional[PathLike] = None,
        base_url: str = "",
    ):
        # `~` expanded: a path the caller wrote, not a location this library
        # went looking in.
        self.token_file = Path(token_file).expanduser() if token_file is not None else None

        held = TokenFile("", "")
        raw = (token or "").strip()
        if raw:
            self.source = "the token passed at construction"
        elif self.token_file is not None:
            # Read once. Two reads can straddle a rotation and pair the URL of
            # one version of the file with the secret of another.
            held = read_token_file(self.token_file)
            raw = held.secret
            self.source = _name_file(self.token_file)
        else:
            raise CredentialError(
                "no credential: pass a token, or the path of a token file — "
                "this client never guesses a location"
            )

        url_from_token, secret = parse_onboarding_token(raw)
        if not secret:
            raise CredentialError(f"{self.source} holds no token")
        if _looks_like_url(secret):
            raise CredentialError(
                f"{self.source} holds a URL where a token should be — a bearer "
                "is never a URL. A combined credential is <url>|<secret>, with "
                "the separator written literally or as %7C"
            )

        # The gateway that travels WITH the credential wins. A separately
        # supplied `base_url` is for a bare secret: letting it outrank the
        # credential would make every later rotation look like a gateway change
        # and be refused for the life of the process.
        resolved = (url_from_token or held.url or base_url).strip().rstrip("/")
        if not resolved:
            raise CredentialError(
                "no gateway URL: it travels inside the onboarding token "
                "(<url>|<secret>) or alongside it — there is no default"
            )
        supplied = (base_url or "").strip().rstrip("/")
        if supplied and supplied != resolved:
            log.warning(
                "%s names gateway %s; the supplied %s is ignored — the URL that "
                "travels with the credential is the one this client uses",
                self.source, redact_url(resolved), redact_url(supplied),
            )

        self._secret = secret
        #: The gateway this client is pointed at, for its whole life.
        self.base_url = resolved
        # C7: which layer supplied the credential, said out loud once. After an
        # incident this line is what tells a wrong-file bind from a bad token.
        log.info("credential for %s from %s", redact_url(self.base_url), self.source)

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        # Redacted, because a repr lands in tracebacks and log lines, and a
        # gateway may be provisioned with `user:pass@` or a `?token=` (D3).
        return f"<TokenSource {redact_url(self.base_url)} from {self.source}>"

    @property
    def secret(self) -> str:
        """The current bearer. Read through the property on every request, so a
        rotation applied here reaches every caller without a restart."""
        return self._secret

    def reload(self) -> Rotation:
        """Re-read the durable token source and swap in a rotated secret.

        Returns whether the swap happened, with a reason fit for a log line.
        A rotation is only ever a change of secret: if the file names a
        different gateway — in the combined `url|secret` layout, in the split
        token-plus-URL-line layout, or in both at once — the swap is refused and
        logged, and this client keeps polling the gateway it started on.
        """
        if self.token_file is None:
            return Rotation(False, "no durable token source is configured")

        held = read_token_file(self.token_file)
        if not held.secret:
            return Rotation(False, f"{self.token_file} is unreadable or holds no token")

        url_from_token, secret = parse_onboarding_token(held.secret)
        running = _canonical_gateway(self.base_url)
        # EVERY URL the file names, not the first one found. A re-onboard that
        # rewrites one layout and leaves the other is the shape C5 is written
        # against: consulting only the combined token lets a file whose URL line
        # names the NEW gateway rotate silently, and the freshly rotated bearer
        # then goes to the old endpoint.
        for named in (url_from_token, held.url):
            named = (named or "").strip().rstrip("/")
            if not named or _canonical_gateway(named) == running:
                continue
            shown, shown_running = redact_url(named), redact_url(self.base_url)
            # Redaction can make the two sides print identically — it drops
            # exactly the components most likely to be what changed. Where it
            # does, the message names the differing components instead, so it
            # still says something and still carries nothing secret.
            where = (
                f"({shown}) than the running one ({shown_running})"
                if shown != shown_running else
                f"than the running one ({shown_running}), "
                f"{_difference(named, self.base_url)}"
            )
            reason = (
                f"{self.token_file} names a different gateway {where} — a "
                "gateway change is a reconfiguration, not a rotation; restart "
                "to move gateways"
            )
            log.warning("refusing token rotation: %s", reason)
            return Rotation(False, reason)

        if _looks_like_url(secret):
            reason = (
                f"{self.token_file} now holds a URL where a token should be; "
                "the running bearer is kept"
            )
            log.warning("refusing token rotation: %s", reason)
            return Rotation(False, reason)

        if not secret or secret == self._secret:
            return Rotation(False, f"{self.token_file} holds the same token")

        self._secret = secret
        # C7 names the layer actually supplying the bearer, and after this swap
        # that is the file — not whatever was passed at construction. A source
        # line that keeps naming a layer the credential no longer comes from is
        # precisely the wrong-file misdiagnosis C7 exists to prevent.
        self.source = _name_file(self.token_file)
        return Rotation(True, f"rotated to the token now in {self.token_file}")
