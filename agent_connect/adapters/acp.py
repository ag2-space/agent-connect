"""The ACP Adapter: one Adapter for any Local Agent that speaks ACP.

The Worker is the ACP Client and the Local Agent — usually through the
`@agentclientprotocol/claude-agent-acp` bridge — is the ACP Agent. This module
is the translation layer between the two vocabularies: a `TurnContext` in, a
stream of `agent_connect.events` out, ACP in the middle.

**Owner-tier only, and that is the design.** Every other Adapter is confined by
the operating system: the Sandbox derived from the Access Tier is passed to a
CLI that enforces it, and an agent that declines to co-operate is still stopped.
ACP has no such thing. The Local Agent performs its own file access and the only
lever left is answering its `session/request_permission` calls by policy — which
binds an agent that asks and does nothing whatsoever to one that does not.
Rather than ship a cooperative imitation of a guarantee that used to be real,
a guest's Task is refused at the top of `turn` and never reaches ACP. It is
refused **out loud**: the refusal is the Turn's answer, so it climbs the Ladder
like any other and the room reads a sentence rather than watching a placeholder
that never resolves. Silence is indistinguishable from breakage, and a guest who
cannot tell the two apart reports the wrong bug.

That refusal is in exactly one place, deliberately: lifting it later (once there
is real confinement around the bridge process) is a single change, and there is
no second path into this Adapter that could forget it. It reads `ctx.access_tier`
and nothing else — the tier the broker attested, settled by the Worker into
`owner` or `guest` before the Turn was built (`docs/adr/0003`) and not something
the sender can write: a duplicated `access_tier` header already fails closed.

**Naming an agent is enough; a command is always allowed to win.** The ordinary
operator sets `AGENT_CONNECT_ACP_AGENT=claude` and a preset in this module says
what to run. An operator with an ACP Agent nobody here anticipated sets
`AGENT_CONNECT_ACP_COMMAND` instead, and it overrides every preset — a preset
table must never be the reason a working ACP Agent cannot be used.

Every setting this Adapter reads is documented once, in `README.md` §
Settings. That section is the authoritative list for the whole Worker;
`test_acp_settings.py` fails if a setting exists in code and not there.

`AGENT_CONNECT_ACP_MODE` is optional because the mode ids are the ACP Agent's to
name. What matters is that the Session runs in the mode that *routes* permission
requests to the Worker rather than one that suppresses them — which is what
every bridge's default mode does, so the default is to leave it alone. The
variable exists for an agent whose default is something else.

**Startup check, and nothing more.** `preflight()` is what the Worker runs
before it serves its first Task: it resolves the command, notices a missing
bridge and turns it into install advice, and asks the ACP Agent whether it is
logged in. It opens no Session and sends no prompt. Discovering at the first
message in a room that the Local Agent wants a login is the failure this
prevents — the person who asked gets an answer, not an authentication notice.

**One Session per (room, Access Tier), remembered across Turns and across
restarts.** A follow-up in a room continues the conversation, because the
Session identifier the Local Agent gave us is kept in the Session map
(`agent_connect.sessions`) and resumed with `session/load` on the next Turn. The
key is the pair, never the room alone: a Session carries a permission mode, and
a lower-trust request must inherit neither that mode nor the context of someone
else's work in the same room.

Two properties of that arrangement are deliberate and easy to undo by accident:

*Resumption is silent.* `session/load` makes the Local Agent replay the entire
prior conversation as ordinary `session/update` notifications — the same
callback live progress arrives on. Consumed naively that dumps an old
transcript into a live room. The Turn's update callback is therefore
**suppressed for the duration of the load**: replayed updates are dropped before
they become events, so nothing downstream can publish them and nothing can
mistake them for this Turn's answer.

*A conversation that cannot be continued costs context, not the request.* An
agent that refuses to resume, or never advertised resumption, or a Session that
retired on its Turn budget or idle timeout — each opens a fresh Session and
tells the room so, as a `Notice`. Silent amnesia is the failure being prevented.

**A Turn has a deadline, and it is enforced through the protocol.** After
`AGENT_CONNECT_TURN_TIMEOUT` seconds the Turn is ended with `session/cancel`,
which makes the ACP Agent stop and hand back everything it had produced —
`prompt()` returns normally, with a `cancelled` stop reason and the chunks so
far. That partial answer goes to the room *with the interruption stated*: work
that nearly finished is not thrown away, and an answer cut off mid-sentence
without a word about it reads as a complete one. Killing the process instead
would do neither, and under a Local Agent shared between rooms it would end
every other room's conversation; ending the child is the last resort after a
cancellation the agent ignored, and it takes only this Turn's own process with
it. A permission request outstanding when a Turn is cancelled is answered
`cancelled`, as the protocol requires — see `on_permission`.

**A Turn that produced nothing produces no reply.** A refusal, a deadline that
came before any output, a bridge that died: each ends the Turn with an empty
answer, and the `TurnReporter` turns that into a structured rejection so the
broker posts the failure notice. This Adapter's part is only to end honestly —
the reason and a sentence saying what happened.

**An attached file is content of the prompt, or it is said out loud.** A
screenshot dropped into a room is base64'd into an `image` content block beside
the text — not mentioned by filename and not summarised, so the Local Agent can
actually be asked what is wrong with it. What it can be sent is bounded by what
it advertised at `initialize` (`promptCapabilities`), and an attachment it did
not advertise, or one that could not be read, is **named in the room** as one it
cannot read, so the person knows to paste the content instead of waiting for an
answer about a file that never arrived. Nothing is converted, resized or
transcoded on the way: base64 is the protocol's own transport encoding for
bytes, exactly reversible, and the bytes inside it are the ones on disk. See
`agent_connect.attachments` for how a sender-adjacent path is opened.

**No interactive terminal, on any path.** ACP lets a Client offer terminal
provisioning so the Agent can ask for a shell. The Worker does not implement it
and does not advertise it (`CLIENT_CAPABILITIES` in `acp/core.py` is where that
decision is stated, and `test_acp_no_terminal.py` asserts it holds on the wire).
A remotely-triggered process does not get a terminal on the operator's machine.
"""
from __future__ import annotations

import asyncio
import base64
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Sequence, Tuple

from .. import attachments as att
from .. import outgoing
from ..acp.core import (
    AcpAgentGone,
    AcpAuthRequired,
    AcpClient,
    AcpCommandMissing,
    AcpDialFailed,
    AcpError,
    SessionResumeRefused,
    TurnResult,
    Update,
    new_cookie_store,
)
from ..acp.policy import WorkingDirectoryPolicy
from ..events import (
    CANCELLED,
    COMPLETED,
    FAILED,
    REFUSED,
    TIMEOUT,
    TOKEN_LIMIT,
    Done,
    MessageChunk,
    Notice,
    PermissionAsked,
    Plan,
    Thinking,
    ToolFinished,
    ToolStarted,
    TurnContext,
    TurnEvent,
)
from ..sessions import SessionSettings, SessionStore, store_from_env

#: The one Access Tier this Adapter serves. See the module docstring: this is
#: the single point the whole restriction lives at. Spelled here rather than
#: imported from `agent_connect.worker`, which imports the Adapters in turn —
#: the two spellings are pinned together by `test_acp_guest_refusal.py`.
OWNER = "owner"

COMMAND_ENV = "AGENT_CONNECT_ACP_COMMAND"
MODE_ENV = "AGENT_CONNECT_ACP_MODE"
AGENT_ENV = "AGENT_CONNECT_ACP_AGENT"
#: An ACP Agent already running behind a URL, dialled instead of spawned.
URL_ENV = "AGENT_CONNECT_ACP_URL"
#: The bearer that URL's door checks at the WebSocket upgrade.
TOKEN_ENV = "AGENT_CONNECT_ACP_TOKEN"
#: Opt out of the loopback-only rule, deliberately and per install.
ALLOW_REMOTE_ENV = "AGENT_CONNECT_ACP_ALLOW_REMOTE"
#: The directory to open Sessions in on the *dialled* Agent's machine.
REMOTE_CWD_ENV = "AGENT_CONNECT_ACP_REMOTE_CWD"
SKIP_AUTH_ENV = "AGENT_CONNECT_ACP_SKIP_AUTH_CHECK"
TIMEOUT_ENV = "AGENT_CONNECT_TURN_TIMEOUT"

#: How long one Turn may run before it is cancelled through the protocol. Ten
#: minutes: long enough for the kind of work a person waits for in a chat room,
#: short enough that a wedged Local Agent does not hold a room's Session all
#: afternoon. `0` disables the deadline, for an operator who would rather wait
#: than lose the work.
DEFAULT_TIMEOUT = 600.0

#: How long the ACP Agent is given to end its Turn *after* `session/cancel`.
#: Not a setting: it is not a policy, it is the allowance for one round trip
#: through an agent that is already misbehaving. When it runs out the Turn's own
#: child process is reaped — see `_deadline`.
CANCEL_GRACE = 15.0

#: The bridge that makes Claude Code an ACP Agent, pinned.
#:
#: Pinned rather than floated because this package renamed itself (it was
#: `claude-code-acp`) and moved through many major versions inside six months.
#: An unpinned fetch eventually pulls an incompatible release into an install
#: that was working yesterday, and the operator has changed nothing.
#:
#: **This is the pinned version of record.** `install.sh` installs this exact
#: spec and `install.test.sh` asserts the two agree, so raising it is a diff a
#: reviewer sees rather than something that happens on its own.
BRIDGE_PACKAGE = "@agentclientprotocol/claude-agent-acp"
BRIDGE_VERSION = "0.64.2"
BRIDGE_SPEC = f"{BRIDGE_PACKAGE}@{BRIDGE_VERSION}"


@dataclass(frozen=True)
class Preset:
    """What running one well-known ACP Agent takes, as far as we know it.

    `binary` is what an installed copy is called on PATH; `fallback` is the
    command to run when it is not installed — pinned, never `@latest`.
    `verified` says whether a real Turn was actually observed against it, which
    is the difference between a preset an operator can trust and a preset that
    is our best reading of someone's documentation.
    """

    binary: str
    fallback: Tuple[str, ...]
    install: str
    login: str
    verified: bool
    args: Tuple[str, ...] = ()
    note: str = ""

    def command(self, env: Optional[dict] = None) -> List[str]:
        """The command to run: the installed copy, else the pinned fetch."""
        path = shutil.which(self.binary, path=(env or os.environ).get("PATH"))
        return [path, *self.args] if path else list(self.fallback)


#: Presets live here, in code, deliberately: a table an operator has to edit is
#: not a preset. Anything absent is served by `AGENT_CONNECT_ACP_COMMAND`, which
#: overrides this table entirely — see the module docstring.
PRESETS: Dict[str, Preset] = {
    "claude": Preset(
        binary="claude-agent-acp",
        fallback=("npx", "-y", BRIDGE_SPEC),
        install=f"npm install -g {BRIDGE_SPEC}",
        login="claude  (then /login), or `claude setup-token`",
        verified=True,
        note=f"Claude Code through the {BRIDGE_PACKAGE} bridge, pinned to "
             f"{BRIDGE_VERSION} — the version a full Turn was run against.",
    ),
    "gemini": Preset(
        binary="gemini",
        fallback=("npx", "-y", "@google/gemini-cli", "--experimental-acp"),
        install="npm install -g @google/gemini-cli",
        login="gemini  (then /auth)",
        verified=False,
        args=("--experimental-acp",),
        note="Gemini CLI speaks ACP itself, behind --experimental-acp. Read "
             "from its documentation, NOT observed here: if it has moved, set "
             f"{COMMAND_ENV} and the preset is out of your way.",
    ),
}

#: Who `trust` is a command *to*. It is a message to the concierge and nothing
#: else: `relay_allowlist.parse_command` on the AG2 Space side is only ever
#: reached from a direct message to that user, so the same words typed in a room
#: — or in a DM to the agent — arrive here as an ordinary prompt and are answered
#: as a question. Named here rather than left to the reader because ticket 14
#: watched an owner spend four Turns discovering it, and nothing said a word.
#:
#: The local half only. It is the backend's `CONCIERGE_USER` default and is not
#: overridden on prd; there is deliberately no setting for it, because an
#: operator who has to configure the name of the bot they are being sent to has
#: already lost the thread this sentence exists to hand them.
CONCIERGE_LOCALPART = "sutando-concierge"


def concierge_for(ctx: TurnContext) -> str:
    """The concierge's MXID, on the homeserver of whoever is being refused.

    A localpart is not a recipient. The whole defect this addresses is an
    instruction that cannot be carried out, and "message the concierge" is still
    one if the reader cannot paste it into a client's start-a-DM box — so the
    domain is filled in rather than described.

    The Worker never learns its own MXID, but it does not need to: everyone in
    this conversation is on one homeserver, so the sender's own MXID carries the
    domain, and the room identifier carries it again if the sender's is missing.
    A Task with neither degrades to the bare localpart, which is worth less but
    is not a guess.
    """
    for identifier in (ctx.user_id, ctx.room):
        _, sep, domain = (identifier or "").partition(":")
        if sep and domain:
            return f"@{CONCIERGE_LOCALPART}:{domain}"
    return f"@{CONCIERGE_LOCALPART}"

#: What a guest is told, in the room, instead of being ignored. It is the Turn's
#: answer — the Ladder edits the placeholder into it — because a refusal nobody
#: can see is indistinguishable from a Worker that is simply broken. It says
#: what happened, why this Adapter in particular, and what would change it.
#:
#: Since 2026-08-20 it is also the *default* thing the platform says to anyone
#: who is not the owner: the Agent Portal's Connect flow now opens on Claude
#: Code, so the ACP path is what a new agent is unless its owner chooses
#: otherwise. That is why the last two paragraphs carry more than they used to.
#: The pointer has to be followable — an instruction with no recipient is not one
#: — and it has to be honest about what it asks for: `trust` is owner tier on
#: every agent the owner owns, not a visitor's pass to this conversation.
REFUSAL = (
    "I only answer my owner over this connection.\n\n"
    "This agent is driven through the Agent Client Protocol, which — unlike the "
    "other ways agent-connect runs a local agent — gives the operating system no "
    "say in what the agent may touch. The only limit available is the agent "
    "asking permission and being told no, and an agent that does not ask is not "
    "stopped by it. Rather than offer a limit that only looks like one, "
    "agent-connect does not run this connection for anyone but the person who "
    "registered the agent.\n\n"
    "Nothing is wrong with your message. My owner can `trust` you, and then I "
    "will answer it — but that grants owner tier, which is their own level of "
    "access on every agent they own, not a guest pass to this room.\n\n"
    "Where it has to be said: in a direct message to the concierge, "
    "`{concierge}`. Sent anywhere else, `trust @user` is not a command at all — "
    "in this room, or in a direct message to me, I read it as an ordinary "
    "question and answer it as one."
)


def refusal(ctx: TurnContext) -> str:
    """`REFUSAL`, addressed to a recipient the reader can act on."""
    return REFUSAL.format(concierge=concierge_for(ctx))

#: What the room is told when its conversation starts over. One sentence, in
#: the room's own terms — a person needs to know that the agent no longer
#: remembers, and why, at the moment it becomes true.
RESET = "🧠 agent-connect: starting a fresh conversation — {why}. I no longer have the earlier context in this room."

#: What the room is told about a Turn that ran past its deadline. Said plainly,
#: and said *whether or not* there is anything to show: an answer cut off
#: mid-sentence with no note reads as a finished answer.
INTERRUPTED = (
    "⏱ agent-connect: this turn ran past its {seconds:.0f}-second deadline and was "
    "cancelled. {what}"
)
INTERRUPTED_PARTIAL = "What the agent had produced by then is above; it is not the whole answer."
INTERRUPTED_EMPTY = "It had produced nothing by then."

#: And when the agent would not stop being asked nicely. The process reaped is
#: this Turn's own — every Session under this Adapter has its own — but it is
#: still a failure of cancellation and is worth saying in those words.
UNSTOPPABLE = (
    " The agent did not answer the cancellation within {grace:.0f}s, so its "
    "process was ended."
)

#: A dead Local Agent, and what happens to the conversation it was holding.
#: The Session identifier survives in the Session map, so the next message in
#: the room resumes the conversation rather than starting over — which is the
#: difference between one crash and one lost conversation.
RESUMABLE = (
    "The conversation itself is kept: the next message in this room will try to "
    "resume it."
)

#: What the room is told about files that did not reach the Local Agent. Said
#: as the ticket says it: the person is told they were not read, told which, and
#: told what to do instead — pasting the content is the thing that works, and
#: silence is what leaves them waiting for an answer about a file nobody saw.
UNREAD = (
    "📎 agent-connect: I can't read that kind of attachment. Your message reached "
    "the agent; {what} did not:\n{lines}\n"
    "Paste the content into the room instead and I can work with that."
)
UNREAD_ONE = "one attachment"
UNREAD_MANY = "{count} of its attachments"
UNREAD_LINE = "• {label} ({mime}) — {why}"

#: Why an attachment of that kind cannot be sent at all. `promptCapabilities` is
#: the ACP Agent's own statement of what it takes; sending it something it never
#: advertised is how a Turn fails for a reason nobody can see.
NOT_ADVERTISED = "this agent did not say it can take {word} attachments"

#: Which kinds it *did* say it takes, appended to the lines above so the answer
#: to "why not?" is followed by "what would work".
ACCEPTS = "\nThis agent accepts: {kinds}."
ACCEPTS_NOTHING = "\nThis agent accepts no attachments at all — only text."

#: How the prompt's text block introduces the blocks that follow it. Framing,
#: like the rest of the preamble, and the *only* thing attachment handling is
#: allowed to add: what the person typed is repeated verbatim after it.
ATTACHED = "Attached to this message, and included below as content: {names}.\n\n"

#: How the prompt's text block accounts for a file that never reached this
#: machine. The Relay Client fetches what someone attached before the Task is
#: delivered, and one it could not fetch arrives with a reason instead of bytes
#: — delivered rather than dead-lettered, because an agent that can say "you
#: attached something and I could not read it" is more use than a question
#: nobody ever answered. So the agent is told in band, in the framing, and the
#: room is told separately; neither notice touches what the person typed.
UNAVAILABLE = (
    "Attached to this message but not readable, so it is not included below: "
    "{names}. Say so if it matters to the answer.\n\n"
)

#: Why the previous conversation ended, in the four ways it can.
WHY_REFUSED = "the agent could not restore our previous one ({detail})"
WHY_RETIRED = "the previous one was retired because {reason}"
WHY_MOVED = "the working directory changed, and a session's directory is fixed when it opens"

#: ACP's stop reasons, mapped onto ours. Anything unrecognised is `FAILED` on
#: purpose: a stop reason added upstream must not read as a completed answer.
STOP_REASONS = {
    "end_turn": COMPLETED,
    "cancelled": CANCELLED,
    "refusal": REFUSED,
    "max_tokens": TOKEN_LIMIT,
    "max_turn_requests": FAILED,
}

#: ACP tool kinds, mapped onto the coarse `ToolStarted.action` classification.
TOOL_ACTIONS = {
    "read": "read", "edit": "edit", "delete": "edit", "move": "edit",
    "execute": "execute", "search": "search", "fetch": "other",
    "think": "other", "other": "other",
}


#: Hosts the Permission Policy's reasoning survives. See `resolve_url`.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

#: The only schemes dialled. The remote-transport RFD requires HTTP/2 for its
#: Streamable HTTP profile and says a server may be WebSocket-only, so WebSocket
#: is the profile that excludes no conforming door.
URL_SCHEMES = ("ws://", "wss://")


def url_from_env(env: Optional[dict] = None) -> str:
    """The ACP Agent's URL as the operator wrote it, validated."""
    raw = (env if env is not None else os.environ).get(URL_ENV, "").strip()
    return resolve_url(raw, env) if raw else ""


def token_from_env(env: Optional[dict] = None) -> str:
    """The bearer for that door, read at dial time so a rotation needs no restart."""
    return (env if env is not None else os.environ).get(TOKEN_ENV, "").strip()


def resolve_url(raw: str, env: Optional[dict] = None) -> str:
    """`raw`, if it is a URL this Worker will dial. Otherwise `AcpError`.

    **Loopback unless the operator says otherwise, and that is a safety rule
    rather than a convenience.** The Permission Policy answers
    `session/request_permission` by resolving the requested paths and comparing
    them against the Session's working directory — on *this* machine's
    filesystem. When the ACP Agent runs somewhere else, those resolutions are
    about the wrong filesystem: different symlinks, different mounts, a `..`
    that climbs somewhere else. The Policy would still answer, and its answers
    would be guesses wearing the shape of a guarantee. `docs/adr/0001` already
    concedes the confinement is cooperative; a remote endpoint moves further in
    that direction, so it has to be asked for by name.
    """
    env = os.environ if env is None else env
    if not raw.startswith(URL_SCHEMES):
        raise AcpError(
            f"{URL_ENV}={raw!r} is not a WebSocket URL. It must start with "
            f"{' or '.join(URL_SCHEMES)} — ACP's remote transport is WebSocket here."
        )
    host = _host_of(raw)
    if host not in LOOPBACK_HOSTS and not _truthy(env.get(ALLOW_REMOTE_ENV)):
        raise AcpError(
            f"{URL_ENV}={raw!r} names {host or 'another host'}, and the "
            f"Permission Policy that guards file operations resolves paths on "
            f"*this* machine — against a different host it is guesswork. Keep "
            f"the ACP Agent on loopback, or say you accept that:\n"
            f"    export {ALLOW_REMOTE_ENV}=1"
        )
    return raw


def _host_of(url: str) -> str:
    """The host in `url`, without scheme, credentials, port or path."""
    rest = url.split("://", 1)[1] if "://" in url else url
    rest = rest.split("/", 1)[0].split("@")[-1]
    if rest.startswith("["):
        return rest.split("]", 1)[0] + "]"
    return rest.rsplit(":", 1)[0] if ":" in rest else rest


def command_from_env(env: Optional[dict] = None) -> List[str]:
    """The ACP Agent's command line, as the operator wrote it.

    Split the way a shell would, so `npx @scope/pkg --flag` works as typed,
    without inviting a shell to interpret anything else in it.
    """
    raw = (env if env is not None else os.environ).get(COMMAND_ENV, "").strip()
    if not raw:
        raise AcpError(_unconfigured())
    return shlex.split(raw)


def _unconfigured() -> str:
    """What to say to someone who has selected the ACP Adapter and stopped."""
    return (
        f"the ACP Adapter needs to know which agent to run. Either name one:\n"
        f"    export {AGENT_ENV}={'|'.join(sorted(PRESETS))}\n"
        f"or give the command yourself, which overrides any preset:\n"
        f'    export {COMMAND_ENV}="npx -y {BRIDGE_SPEC}"\n'
        f"or dial one that is already running:\n"
        f'    export {URL_ENV}="ws://127.0.0.1:8802/acp"'
    )


@dataclass(frozen=True)
class Endpoint:
    """Where this Worker's ACP Agent is: a URL to dial, or a command to run."""

    url: str = ""
    token: str = ""
    command: Tuple[str, ...] = ()

    @property
    def dialled(self) -> bool:
        return bool(self.url)

    def describe(self) -> str:
        return self.url if self.dialled else " ".join(self.command)


def resolve_endpoint(env: Optional[dict] = None) -> Endpoint:
    """The one ACP Agent this Worker drives, from the settings as written.

    **A URL and a command are mutually exclusive, and that is a rejection rather
    than a precedence.** Silently preferring one would mean an operator who set
    both gets answers from an agent they did not pick — different credentials, a
    different filesystem, a different conversation — with nothing in the room to
    say which one spoke. A precedence is right for a *preset* versus a command,
    where both name the same kind of thing; it is wrong for two different agents.

    There is deliberately no fallback from a URL to spawning. A door that is
    down is a door that is down: starting a second agent locally to cover for it
    would answer the room as something other than what the operator configured,
    and would do it exactly when nobody is watching.
    """
    env = os.environ if env is None else env
    url = (env.get(URL_ENV) or "").strip()
    command = (env.get(COMMAND_ENV) or "").strip()
    if url and command:
        raise AcpError(
            f"{URL_ENV} and {COMMAND_ENV} are both set, and they name two "
            f"different ACP Agents:\n"
            f"    {URL_ENV}={url}\n"
            f"    {COMMAND_ENV}={command}\n"
            f"Unset one. A Worker that guessed would answer the room as an "
            f"agent nobody chose."
        )
    if url:
        return Endpoint(url=resolve_url(url, env), token=token_from_env(env))
    return Endpoint(command=tuple(resolve_command(env)))


def preset_for(name: str) -> Preset:
    """The named preset, or a sentence naming the ones that exist."""
    preset = PRESETS.get(name.strip().lower())
    if preset is None:
        raise AcpError(
            f"{AGENT_ENV}={name!r} is not a preset agent-connect knows. "
            f"Known: {', '.join(sorted(PRESETS))}.\n"
            f"Any other ACP Agent runs through {COMMAND_ENV}, e.g.\n"
            f'    export {COMMAND_ENV}="my-agent --acp"'
        )
    return preset


def resolve_command(env: Optional[dict] = None) -> List[str]:
    """The ACP Agent command, from an explicit command or from a preset.

    Precedence is the point of this function: an explicit command wins over a
    named preset unconditionally, even when the preset exists and looks better
    informed. A preset table that could block a working ACP Agent would be
    worse than no presets at all.
    """
    env = os.environ if env is None else env
    if (env.get(COMMAND_ENV) or "").strip():
        return command_from_env(env)
    name = (env.get(AGENT_ENV) or "").strip()
    if name:
        return preset_for(name).command(env)
    raise AcpError(_unconfigured())


def install_advice(command: Sequence[str], env: Optional[dict] = None) -> str:
    """What an operator whose bridge is missing has to install, by name.

    A `FileNotFoundError` traceback names a path; this names a package and the
    line that installs it. Which package is knowable when the command came from
    a preset, and guessable-but-not-claimed when it did not.
    """
    env = os.environ if env is None else env
    missing = command[0] if command else "(no command)"
    name = (env.get(AGENT_ENV) or "").strip().lower()
    preset = PRESETS.get(name) if not (env.get(COMMAND_ENV) or "").strip() else None
    lines = [f"the ACP Agent's command is not installed: {missing!r}."]
    if preset is not None:
        lines.append(f"Install it with:\n    {preset.install}")
    if missing in ("npx", "npm", "node"):
        lines.append(
            "That command comes from Node.js, which is not on this machine's "
            "PATH — install Node.js 18+ first (https://nodejs.org)."
        )
    elif preset is None:
        lines.append(
            f"It came from {COMMAND_ENV}, so agent-connect cannot say what "
            f"installs it. Check the command runs in your own shell, or name a "
            f"preset instead: {AGENT_ENV}={'|'.join(sorted(PRESETS))}."
        )
    return "\n".join(lines)


def login_advice(agent, env: Optional[dict] = None) -> str:
    """What an operator whose Local Agent is not logged in has to run.

    Built from the preset when there is one, and from what the ACP Agent itself
    advertised when there is not — `authMethods` carries a human name and often
    a description, which is the agent's own words for its login.
    """
    env = os.environ if env is None else env
    name = (env.get(AGENT_ENV) or "").strip().lower()
    preset = PRESETS.get(name)
    lines = [
        "the Local Agent is not logged in — it offered agent-connect a way to "
        "authenticate, which means it has no credentials of its own yet."
    ]
    if preset is not None:
        lines.append(f"Log in with:\n    {preset.login}")
    offered = [
        (m.get("name") or m.get("id") or "").strip()
        for m in (agent.auth_methods or [])
    ]
    offered = [o for o in offered if o]
    if offered:
        lines.append("The agent offered: " + ", ".join(offered) + ".")
    lines.append(
        "agent-connect will not log in for you: it never opens an interactive "
        "terminal on your machine on behalf of a room. Log in yourself in your "
        "own shell, then start the Worker again.\n"
        f"If this check is wrong for your agent, set {SKIP_AUTH_ENV}=1."
    )
    return "\n".join(lines)


def auth_failed_advice(agent, env: Optional[dict] = None) -> str:
    """What to say when the ACP Agent refuses a Turn as unauthenticated.

    Not `login_advice`: that one opens by saying the Agent advertised a login
    method, and here it advertised none — `preflight` passed and the refusal
    only came at `session/prompt`. `preflight` cannot catch it without sending
    a prompt, which would spend tokens on every Worker start.
    """
    env = os.environ if env is None else env
    name = (env.get(AGENT_ENV) or "").strip().lower()
    preset = PRESETS.get(name) if not (env.get(COMMAND_ENV) or "").strip() else None
    lines = [
        "the Local Agent refused this turn because it is not authenticated. It "
        "advertised no login method when the Worker started, so the startup "
        "check had nothing to catch — it only said so when the first real work "
        "arrived."
    ]
    if preset is not None:
        lines.append(f"Log in with:\n    {preset.login}")
    else:
        lines.append(
            "Log in to the agent yourself, in your own shell, then start the "
            "Worker again."
        )
    lines.append(
        "agent-connect will not log in for you: it never opens an interactive "
        "terminal on your machine on behalf of a room."
    )
    return "\n".join(lines)


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def preamble(ctx: TurnContext, attached: Sequence[str] = (),
             unavailable: Sequence[str] = ()) -> str:
    """What the Local Agent is told about the situation it is answering in.

    Written here rather than inherited from the shim's sandbox preamble, which
    describes an operating-system Sandbox this Adapter does not have. Saying
    "this run's sandbox is workspace-write" over ACP would be stating a
    confinement that is not there.

    `attached` names the files that follow as content blocks, so the agent can
    tell which block is which when someone says "the second screenshot".
    `unavailable` names the ones the Relay Client could not fetch at all, so the
    agent knows a file was meant to be there and can say it is not, rather than
    answering a question about a screenshot as if none had been sent. Both are
    part of the framing and never part of the message: what the person typed is
    appended after this, unchanged, whatever they attached to it.

    The outgoing rule is stated here too, in `agent_connect.outgoing`'s own
    words: an agent that is never told how to hand a file to the room pastes it
    into a code block instead, which is the thing this framing exists to avoid.
    """
    who = ctx.sender_name or "the owner"
    where = f" in {ctx.room_name}" if ctx.room_name else ""
    framing = (
        f"[agent-connect] {who} is asking you this{where}, through a chat room. "
        "Answer in chat: prose, no more than a few short paragraphs unless asked "
        "for more. You are working in the directory this session was opened in; "
        f"file operations outside it will be refused when you ask for them.\n"
        f"{outgoing.INSTRUCTION}\n\n"
    )
    if attached:
        framing += ATTACHED.format(names=", ".join(attached))
    if unavailable:
        framing += UNAVAILABLE.format(names=", ".join(unavailable))
    return framing


def accepted_kinds(agent) -> List[str]:
    """The attachment kinds this ACP Agent advertised, in a person's words."""
    if agent is None:
        return []
    kinds = []
    if agent.accepts_prompt_content("image"):
        kinds.append("images")
    if agent.accepts_prompt_content("audio"):
        kinds.append("audio")
    if agent.accepts_prompt_content("embeddedContext"):
        kinds.append("other files")
    return kinds


def _capability(modality: str) -> str:
    """Which `promptCapabilities` flag carrying this modality depends on.

    ACP's baseline is text and a resource *link*; images, audio and embedded
    resources are each optional and each advertised separately. A video, and
    anything that is not image or audio, rides as an embedded resource — the
    only block that carries arbitrary bytes — so it needs `embeddedContext`.
    Sending a link instead would be handing over a filename, which is the thing
    this Adapter exists to stop doing.
    """
    return {att.IMAGE: "image", att.AUDIO: "audio"}.get(modality, "embeddedContext")


def _content_block(modality: str, mime: str, opened: att.Opened) -> dict:
    """One attachment as an ACP content block, byte for byte.

    Base64 is the protocol's transport encoding for bytes and nothing else: it
    is exactly reversible, and what goes into it is what `os.read` returned. No
    decoding to text, no re-encoding, no thumbnail — a resized screenshot is a
    different screenshot.
    """
    payload = base64.b64encode(opened.data).decode("ascii")
    if modality in (att.IMAGE, att.AUDIO):
        return {"type": modality, "mimeType": mime, "data": payload}
    return {
        "type": "resource",
        "resource": {"uri": Path(opened.path).as_uri(), "mimeType": mime,
                     "blob": payload},
    }


def prompt_blocks(
    ctx: TurnContext, agent, limit: int
) -> Tuple[List[dict], List[str]]:
    """The prompt as content blocks, and the attachments that could not be one.

    Returns `(blocks, problems)`. The first block is always the text — the
    framing followed by exactly what the person typed — so a Turn whose every
    attachment failed still asks the question. `problems` are room-facing lines,
    one per attachment that did not make it, which the caller turns into a
    single `Notice`: several files failing is one fact about the run, not
    several messages about it.

    A file the Relay Client never fetched is answered before anything else is
    asked about it. Its reason is the library's, which is honest about what
    happened; running it past `promptCapabilities` first would report an agent's
    advertisement as the cause of an absence that had nothing to do with the
    agent — and would do it under a media type read off a marker hint. It is
    also the one failure the agent itself is told about, because it is the one
    where a file was meant to be in this conversation and is not.
    """
    passed: List[str] = []
    missing: List[str] = []
    blocks: List[dict] = []
    problems: List[str] = []
    for attachment in ctx.attachments:
        name = att.label(attachment)
        mime = att.mime_of(attachment)
        if not attachment.ok:
            missing.append(name)
            problems.append(
                UNREAD_LINE.format(label=name, mime=mime, why=attachment.reason))
            continue
        modality = att.modality(attachment)
        capability = _capability(modality)
        if agent is None or not agent.accepts_prompt_content(capability):
            problems.append(UNREAD_LINE.format(
                label=name, mime=mime,
                why=NOT_ADVERTISED.format(word=att.MODALITY_WORDS[modality])))
            continue
        opened = att.read(attachment, limit)
        if not opened.ok:
            problems.append(
                UNREAD_LINE.format(label=name, mime=mime, why=opened.problem))
            continue
        blocks.append(_content_block(modality, mime, opened))
        passed.append(name)
    # The text block is built last and put first: it names what follows it, and
    # what follows it is only known once every attachment has been tried.
    text = preamble(ctx, passed, missing) + ctx.prompt
    return [{"type": "text", "text": text}] + blocks, problems


def unread_notice(problems: Sequence[str], agent) -> str:
    """The one message a room gets about attachments that did not arrive."""
    if not problems:
        return ""
    what = (UNREAD_ONE if len(problems) == 1
            else UNREAD_MANY.format(count=len(problems)))
    kinds = accepted_kinds(agent)
    tail = ACCEPTS.format(kinds=", ".join(kinds)) if kinds else ACCEPTS_NOTHING
    return UNREAD.format(what=what, lines="\n".join(problems)) + tail


def events_for(update: Update) -> List[TurnEvent]:
    """One ACP `session/update` notification, in our vocabulary.

    Returns a list because most updates map onto one event and some onto none:
    an update this Adapter has no word for is dropped rather than guessed at.
    """
    raw = update.raw or {}
    if update.kind == "agent_message_chunk":
        return [MessageChunk(text=update.text)] if update.text else []
    if update.kind == "agent_thought_chunk":
        return [Thinking(text=update.text)] if update.text else []
    if update.kind == "plan":
        entries = [
            {"title": e.get("content") or e.get("title") or "", "status": e.get("status") or ""}
            for e in (raw.get("entries") or [])
            if isinstance(e, dict)
        ]
        return [Plan(entries=entries)]
    if update.kind in ("tool_call", "tool_call_update"):
        return [_tool_event(raw)]
    return []


def _tool_event(raw: dict) -> TurnEvent:
    """A tool update as a start or an end, by the status it carries.

    ACP reports one tool call's whole life through the same notification kind,
    so the status is the only thing that says which of our two events it is.
    """
    tool_id = raw.get("toolCallId") or ""
    title = raw.get("title") or ""
    status = raw.get("status") or ""
    if status in ("completed", "failed"):
        return ToolFinished(
            tool_id=tool_id, title=title,
            status=COMPLETED if status == "completed" else FAILED,
            detail={"raw_status": status},
        )
    return ToolStarted(
        tool_id=tool_id, title=title,
        action=TOOL_ACTIONS.get(raw.get("kind") or "", "other"),
        detail={"raw_status": status},
    )


class AcpAdapter:
    """Drives one Turn over ACP, for the owner, under a Permission Policy.

    Registered as an object rather than a module because it already speaks the
    event-shaped contract: the Adapter registry passes anything exposing `turn`
    through unwrapped.
    """

    name = "acp"

    def __init__(
        self,
        command: Optional[Sequence[str]] = None,
        mode: Optional[str] = None,
        store: Optional[SessionStore] = None,
        session_settings: Optional[SessionSettings] = None,
        timeout: Optional[float] = None,
        url: Optional[str] = None,
        token: Optional[str] = None,
        remote_cwd: Optional[str] = None,
    ):
        # Injectable so a test does not have to set process environment; `None`
        # means "read the environment when the Turn runs", which is what the
        # Worker gets.
        self._command = list(command) if command else None
        self._url = url or None
        self._token = token or ""
        self._remote_cwd = remote_cwd
        # One store for this Adapter's whole life, because affinity cookies are
        # about the *next* dial: a fresh one per dial loses the sticky session a
        # load balancer handed out.
        self._cookies = None
        self._mode = mode
        self._timeout = timeout
        # The Session map is per-Adapter, and the Adapter is per-Worker: the
        # whole point is that two Turns in one room find the same Session.
        self._store = store
        self._session_settings = session_settings
        #: What `preflight` learned about the ACP Agent, if it has run.
        self.agent_description = None

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<AcpAdapter {self._url or self._command or 'from ' + COMMAND_ENV}>"

    def command(self) -> List[str]:
        return list(self._command) if self._command else resolve_command()

    def remote_cwd(self) -> str:
        """The directory a dialled Agent should open its Sessions in.

        `session/new` requires a working directory, so one is always sent — the
        protocol has no way to say "you choose". On loopback, which is the
        default, this Worker's own directory is a real path on the same
        filesystem and is the right thing to send. Across hosts it is not, and
        there is nothing this Worker can compute that would be: hence a setting,
        for the case the operator has already opted into with
        `AGENT_CONNECT_ACP_ALLOW_REMOTE`.

        Empty means "send this Worker's own", which is what a loopback install
        wants and never has to configure. Note that what an Agent *does* with
        the value is its own business — the one we verified against accepts any
        string, including a path that exists nowhere.
        """
        if self._remote_cwd is not None:
            return self._remote_cwd
        return os.environ.get(REMOTE_CWD_ENV, "").strip()

    def endpoint(self) -> Endpoint:
        """Where this Adapter's ACP Agent is — injected, or from the settings."""
        if self._url:
            return Endpoint(url=resolve_url(self._url), token=self._token)
        if self._command:
            return Endpoint(command=tuple(self._command))
        return resolve_endpoint()

    def _connect(self, endpoint: Endpoint, *, cwd: str, **kwargs):
        """A live connection to `endpoint`, dialled or spawned.

        The two are never alternatives at runtime: `resolve_endpoint` has
        already settled which one this Worker has, and a dial that fails stays
        failed rather than falling back to starting something locally.
        """
        if endpoint.dialled:
            if self._cookies is None:
                self._cookies = new_cookie_store()
            return AcpClient.dial(
                endpoint.url,
                token=endpoint.token,
                cookie_store=self._cookies,
                **kwargs,
            )
        return AcpClient.spawn(list(endpoint.command), cwd=cwd, **kwargs)

    def mode(self) -> str:
        return self._mode if self._mode is not None else os.environ.get(MODE_ENV, "").strip()

    def timeout(self) -> float:
        """Seconds one Turn may run before it is cancelled. `0` is no deadline.

        A value typed wrong is the default rather than a Worker that will not
        start: the operator finds out about a bad setting from a Turn that runs
        for the usual ten minutes, not from a machine that never serves.
        """
        if self._timeout is not None:
            return max(0.0, float(self._timeout))
        raw = (os.environ.get(TIMEOUT_ENV) or "").strip()
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_TIMEOUT
        return value if value >= 0 else DEFAULT_TIMEOUT

    def session_settings(self) -> SessionSettings:
        if self._session_settings is None:
            self._session_settings = SessionSettings.from_env()
        return self._session_settings

    def store(self) -> SessionStore:
        """The Session map, built on first use rather than at import.

        Late, because an Adapter that is constructed and then never asked to run
        a Turn — the registry builds one to inspect it — has no business
        touching the operator's state directory.
        """
        if self._store is None:
            self._store = store_from_env()
        return self._store

    async def preflight(self) -> Optional[str]:
        """Is this Worker able to serve? `None` if yes, else what to fix.

        Run once at startup, before any Task exists. It resolves the command,
        starts the ACP Agent, and asks it who it is — `initialize` and nothing
        else. **No Session is opened and no prompt is sent**, so this costs no
        tokens and cannot do work; and no terminal is offered, here or anywhere.

        The authentication signal is `authMethods` on the initialize response:
        an ACP Agent that is already authenticated has no method to offer, and
        one that offers methods is telling the Client it needs to log in.

        **How far that is verified, exactly.** An authenticated
        `@agentclientprotocol/claude-agent-acp` 0.64.2 answers with an empty
        list — observed, twice. An ACP Agent that *does* advertise methods stops
        the Worker with the login command — observed against the fake ACP Agent.
        What could not be observed is a genuinely logged-out Claude bridge:
        started with `$HOME` redirected at an empty directory it still answered
        `authMethods: []`, so it is finding credentials somewhere else (a
        keychain, most likely) and this check would not fire for it. Treat the
        check as a real catch for ACP Agents that advertise auth methods and as
        no catch at all for ones that do not — and note that the honest reading
        is "the agent said it needs authenticating", which is what the message
        says. `AGENT_CONNECT_ACP_SKIP_AUTH_CHECK=1` exists for the other
        direction: a startup check that is wrong must be escapable without
        editing the package.
        """
        try:
            endpoint = self.endpoint()
        except AcpError as exc:
            return str(exc)
        if not endpoint.dialled and shutil.which(endpoint.command[0]) is None:
            return install_advice(list(endpoint.command))
        try:
            async with self._connect(endpoint, cwd=os.getcwd()) as client:
                agent = await client.initialize()
        except AcpDialFailed as exc:
            # A door that is merely not up yet is a real state, and it is not
            # the same failure as a bridge that is not installed. Said plainly
            # so the operator fixes the listener rather than the install.
            return f"the ACP Agent at {endpoint.url} could not be reached: {exc}"
        except AcpError as exc:
            if isinstance(exc, AcpCommandMissing):
                return install_advice(list(endpoint.command))
            return f"the ACP Agent would not start: {exc}"
        except Exception as exc:  # noqa: BLE001 — a startup check reports, never raises
            return f"the ACP Agent would not start: {exc}"
        self.agent_description = agent
        if agent.auth_methods and not _truthy(os.environ.get(SKIP_AUTH_ENV)):
            return login_advice(agent)
        return None

    def agent_description_or_none(self):
        """What `preflight` learned about the Agent, if it ran at all."""
        return getattr(self, "agent_description", None)

    def describe(self) -> str:
        """One line about what preflight found, for the Worker's startup log."""
        agent = getattr(self, "agent_description", None)
        if agent is None:
            return "acp: no ACP Agent contacted yet"
        return f"acp: {agent.name or 'an unnamed ACP Agent'} {agent.version}".strip()

    async def turn(self, ctx: TurnContext) -> AsyncIterator[TurnEvent]:
        """One Turn: refuse, or run it over ACP and report what happened."""
        # --- the whole non-owner restriction, in one place -----------------
        if ctx.access_tier != OWNER:
            yield Done(reason=REFUSED, text=refusal(ctx))
            return

        try:
            endpoint = self.endpoint()
        except AcpError as exc:
            yield Done(reason=FAILED, text="", note=f"agent-connect: {exc}")
            return

        cwd = ctx.cwd or os.getcwd()
        policy = WorkingDirectoryPolicy(cwd)
        # What the Agent is told to open its Session in. The same directory when
        # it runs here — which is every loopback install — and the operator's
        # own value when they have opted into dialling another host, where this
        # Worker's paths mean nothing.
        session_cwd = cwd
        if endpoint.dialled:
            session_cwd = self.remote_cwd() or cwd
        queue: asyncio.Queue = asyncio.Queue()
        settings = self.session_settings()
        store = self.store() if settings.memory else None
        key = ctx.session_key

        # Per-Turn, not per-Adapter: two rooms run their Turns concurrently and
        # one of them resuming must not silence the other's live progress.
        muted = _Suppression()
        live = _LiveTurn()

        def on_update(update: Update) -> None:
            # Replayed history arrives here exactly like live progress — the
            # core cannot tell them apart and says so. Dropping it before it
            # becomes an event is what keeps an old transcript out of the room
            # *and* out of this Turn's answer.
            if muted.on:
                return
            for event in events_for(update):
                queue.put_nowait(event)

        def on_permission(request):
            """The Permission Policy decides, and the room gets to hear about it.

            The event is emitted after the decision and carries it, because a
            rejected request is the interesting one: without it a blocked agent
            is indistinguishable from a lazy one.
            """
            if live.cancelled:
                # The protocol requires an outstanding permission request to be
                # answered `cancelled` once the Turn has been cancelled — an
                # unanswered one leaves the Local Agent waiting on a Client that
                # has stopped listening. `None` is how the core spells that
                # outcome. Deliberately no `PermissionAsked`: this is not the
                # Policy refusing anything, and reporting it as a refusal would
                # put "agent-connect refused 1 operation" in a reply whose real
                # story is that the turn ran out of time.
                return None
            decision = policy.decide(request)
            queue.put_nowait(
                PermissionAsked(
                    title=decision.title,
                    allowed=decision.allowed,
                    reason=decision.reason,
                    detail={"paths": list(decision.paths)},
                )
            )
            return decision.option_id

        # Which Session this Turn belongs to, decided before the ACP Agent is
        # even started: a retired or misplaced one is dropped here, and the room
        # hears about it whether or not the agent turns out to be reachable.
        record = store.get(key) if store is not None else None
        if record is not None:
            why = _why_unusable(
                record, cwd, settings, owns_cwd=not endpoint.dialled
            )
            if why:
                store.forget(key)
                record = None
                queue.put_nowait(Notice(text=RESET.format(why=why)))

        async def run():
            async with self._connect(
                endpoint, cwd=cwd, on_update=on_update, permission_handler=on_permission
            ) as client:
                agent = await client.initialize()
                live.client = client
                # Muted from before the Session is touched until the prompt is
                # sent. Not merely "during `load_session`": the boundary that
                # actually holds is the prompt, because *nothing* arriving
                # before it is progress on this Turn — it is history, by
                # definition — and drawing the line there leaves no window in
                # which a late replay notification could be mistaken for live
                # work. Cleared in a `finally`, so a Session that refused to
                # resume cannot leave this Turn's own progress silenced.
                muted.on = True
                try:
                    session_id, turns = "", 0
                    if record is not None:
                        try:
                            await client.load_session(
                                record.session_id, cwd=session_cwd
                            )
                            session_id, turns = record.session_id, record.turns
                        except SessionResumeRefused as exc:
                            queue.put_nowait(
                                Notice(text=RESET.format(
                                    why=WHY_REFUSED.format(detail=exc)))
                            )
                    if not session_id:
                        session_id = await client.new_session(cwd=session_cwd)
                        turns = 0
                    mode = self.mode()
                    if mode:
                        await client.set_session_mode(session_id, mode)
                    if store is not None:
                        # Remembered *before* the prompt, not after: a Turn that
                        # crashes or is cancelled still happened, and its history
                        # is in the Local Agent's Session whatever this Worker
                        # does next. Counting it now is also what makes the Turn
                        # budget bound the tokens rather than the successes.
                        store.remember(key, session_id, session_cwd, turns + 1)
                finally:
                    muted.on = False
                # The deadline may have passed while the Session was being
                # opened or resumed. Prompting anyway would start work that is
                # already over its time; the Turn ends here with what it has,
                # which is nothing.
                if not live.begin(session_id):
                    return TurnResult(stop_reason="cancelled", text="")
                # Read here, not at the top of the Turn: what may be sent is
                # what *this* agent advertised, and that is not known until it
                # has answered `initialize`. An attachment that cannot be sent
                # is said out loud before the prompt goes, so the room learns it
                # while the agent is still working rather than afterwards.
                blocks, problems = prompt_blocks(ctx, agent, att.max_bytes())
                if problems:
                    queue.put_nowait(Notice(text=unread_notice(problems, agent)))
                return await client.prompt(session_id, blocks)

        seconds = self.timeout()
        work = asyncio.ensure_future(run())
        watchdog = (
            asyncio.ensure_future(_deadline(seconds, live, work)) if seconds else None
        )
        chunks: List[str] = []
        try:
            async for event in _drain(queue, work):
                if isinstance(event, MessageChunk):
                    chunks.append(event.text)
                yield event
            result = work.result()
        except asyncio.CancelledError:
            if not live.reaped:
                raise
            # Our own last resort, not the caller's cancellation: the Turn was
            # cancelled through the protocol, the agent ignored it, and its
            # process was ended. Whatever it had said still stands.
            yield Done(reason=TIMEOUT, text="".join(chunks),
                       note=_interrupted(seconds, chunks, unstoppable=True))
            return
        except AcpAgentGone as exc:
            # The Local Agent died. The Worker does not: this is one Turn's
            # failure, and the Session identifier stays in the map so the next
            # message in this room resumes the conversation instead of losing
            # it.
            yield Done(reason=FAILED, text="".join(chunks),
                       note=_gone_note(exc, store, key))
            return
        except AcpAuthRequired:
            yield Done(reason=FAILED, text="".join(chunks),
                       note=f"agent-connect: {auth_failed_advice(self.agent_description_or_none())}")
            return
        except AcpError as exc:
            # A missing bridge mid-Turn gets the same install advice the startup
            # check gives, rather than a bare "command not found".
            note = (install_advice(list(endpoint.command))
                    if isinstance(exc, AcpCommandMissing) else str(exc))
            yield Done(reason=FAILED, text="".join(chunks),
                       note=f"agent-connect: {note}")
            return
        except Exception as exc:  # noqa: BLE001 — one Turn's failure is its own
            yield Done(reason=FAILED, text="".join(chunks),
                       note=f"agent-connect: the ACP Turn failed: {exc}")
            return
        finally:
            if watchdog is not None and not watchdog.done():
                watchdog.cancel()
            if not work.done():
                work.cancel()

        if live.cancelled and result.stop_reason == "cancelled":
            # It stopped because we said so, and we said so because it ran out
            # of time — a `cancelled` stop reason here means the deadline, not
            # a person changing their mind, and the room is told which. An agent
            # that finished anyway in the moment between the two is reported as
            # having finished: what it said is a real answer.
            yield Done(reason=TIMEOUT, text="".join(chunks),
                       note=_interrupted(seconds, chunks))
            return

        yield Done(
            reason=STOP_REASONS.get(result.stop_reason, FAILED),
            text="".join(chunks),
            note=_note(result.stop_reason),
        )


class _LiveTurn:
    """The running Turn, as much of it as cancellation needs to reach.

    Cancellation has to happen from outside the coroutine doing the work — a
    watchdog that fires while `prompt()` is still awaited — and it needs two
    things the coroutine owns: the connection and the Session identifier. This
    is that handover, and it is per Turn, never per Adapter: cancelling one
    room's Turn must not touch another's.
    """

    __slots__ = ("client", "session_id", "cancelled", "reaped")

    def __init__(self) -> None:
        self.client = None
        self.session_id = ""
        #: True once this Turn has been cancelled. Read by the permission
        #: handler as well as by the ending: a request arriving after this must
        #: be answered `cancelled`, which the protocol requires.
        self.cancelled = False
        #: True only if cancelling through the protocol did not work and the
        #: child process had to be ended. Distinguishes our own last resort from
        #: the caller cancelling us, which must propagate untouched.
        self.reaped = False

    def begin(self, session_id: str) -> bool:
        """The Session is open and the prompt is about to go out — unless the
        deadline beat it here."""
        self.session_id = session_id
        return not self.cancelled

    async def interrupt(self) -> None:
        """End this Turn **through the protocol**, not by killing anything.

        `session/cancel` makes the ACP Agent stop and the outstanding `prompt`
        return normally, with a `cancelled` stop reason and everything it
        produced up to that point. Killing the process would do neither, and
        under a Local Agent shared by several rooms it would end every other
        room's conversation as well — which is the whole reason this method
        exists rather than a `terminate()`.

        A Turn cancelled before its Session was open has nothing to cancel: the
        flag alone stops the prompt from ever being sent.
        """
        self.cancelled = True
        client, session_id = self.client, self.session_id
        if client is None or not session_id or not getattr(client, "alive", False):
            return
        try:
            await client.cancel(session_id)
        except AcpError:
            # It was already gone, or it will not answer. Either way the Turn is
            # over and the ending is reported from what we have.
            pass


async def _deadline(seconds: float, live: _LiveTurn, work: asyncio.Future) -> None:
    """Cancel the Turn when it runs too long, and reap it only if it will not go.

    Two steps, in this order and never the other way round: `session/cancel`
    first, so a well-behaved ACP Agent ends its Turn itself and hands back what
    it had; and only if that is ignored for `CANCEL_GRACE` seconds, cancel the
    coroutine — which unwinds `AcpClient.spawn` and ends **this Turn's own**
    child process. Every Turn under this Adapter has its own, so no other
    room's Session is touched even by the last resort.
    """
    await asyncio.sleep(seconds)
    if work.done():
        return
    await live.interrupt()
    await asyncio.sleep(CANCEL_GRACE)
    if not work.done():
        live.reaped = True
        work.cancel()


def _interrupted(seconds: float, chunks, unstoppable: bool = False) -> str:
    """What the room is told about a Turn that hit its deadline."""
    note = INTERRUPTED.format(
        seconds=seconds,
        what=INTERRUPTED_PARTIAL if "".join(chunks).strip() else INTERRUPTED_EMPTY,
    )
    return note + (UNSTOPPABLE.format(grace=CANCEL_GRACE) if unstoppable else "")


def _gone_note(exc: Exception, store, key) -> str:
    """What the room is told when the Local Agent's process died under it.

    The sentence says what happened and, when there is a conversation left to
    continue, that it is not lost. The Session record is still in the map — it
    is written before the prompt — so the next Task in this room resumes it.
    """
    lines = [f"agent-connect: {exc}"]
    if store is not None and store.get(key) is not None:
        lines.append(RESUMABLE)
    return " ".join(lines)


class _Suppression:
    """Whether updates are being dropped rather than turned into events.

    A one-field object rather than a `nonlocal` because the flag is read from a
    callback the core owns and written from the coroutine driving the load; a
    mutable cell makes both sides obviously the same flag.
    """

    __slots__ = ("on",)

    def __init__(self) -> None:
        self.on = False


def _why_unusable(
    record, cwd: str, settings: SessionSettings, *, owns_cwd: bool = True
) -> str:
    """Why this remembered Session cannot serve this Task — or `""`.

    Two reasons, and neither involves asking the Local Agent. A Session's
    working directory is fixed when it opens, so a Task from a differently
    configured Worker needs a new one however much context the old one holds.
    And a Session past its Turn budget or its idle timeout is retired here,
    which is the point of the budget: the boundary is the Worker's to enforce
    and to announce, not something to discover when the context is already
    enormous.

    **`owns_cwd` is false when the Agent was dialled rather than spawned**, and
    the working-directory reason goes with it. A dialled Agent's directory
    belongs to *its* host: the value in the map is the one that Agent reported,
    it means nothing on this filesystem, and comparing it against this Worker's
    own would retire a live Session every Turn and lose the room its history for
    a mismatch that was never a mismatch. Whether the Session is still good is
    then the remote's to answer, and it answers by refusing `session/load` —
    which is already handled, and already says so in the room.
    """
    if owns_cwd and not record.matches(cwd):
        return WHY_MOVED
    reason = settings.retirement(record)
    return WHY_RETIRED.format(reason=reason) if reason else ""


async def _drain(queue: asyncio.Queue, work: asyncio.Future) -> AsyncIterator[TurnEvent]:
    """Events as they arrive, until the Turn ends — then the ones still queued.

    The callbacks the core takes are pushed to rather than pulled from, so the
    queue is what turns them back into a stream. Draining after `work` finishes
    matters: the last message chunk of a Turn is often still in flight when the
    `prompt` call returns, and dropping it would truncate the answer.
    """
    while True:
        item = asyncio.ensure_future(queue.get())
        done, _ = await asyncio.wait({item, work}, return_when=asyncio.FIRST_COMPLETED)
        if item in done:
            yield item.result()
            continue
        item.cancel()
        break
    while not queue.empty():
        yield queue.get_nowait()
    work.result()  # re-raises whatever the Turn failed with


def _note(stop_reason: str) -> str:
    """The operator- and room-facing footnote for a Turn that was not plain.

    It says nothing about rejected permission requests. It used to, and then the
    `TurnReporter` began reading `PermissionAsked` straight off the stream for
    its summary — so this stopped, rather than telling the person twice. The
    events still carry every rejection; this is only about who says it.
    """
    lines: List[str] = []
    mapped = STOP_REASONS.get(stop_reason)
    if mapped is None:
        lines.append(
            "(the ACP Agent stopped for a reason agent-connect does not know: "
            f"{stop_reason or 'none given'!r})"
        )
    elif mapped != COMPLETED:
        lines.append(f"(the Turn stopped early: {stop_reason})")
    return "\n".join(lines)
