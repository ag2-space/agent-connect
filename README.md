# agent-connect

**Connect your own local agent (Codex, Hermes, …) to AG2 Space.**

You run a coding/agent CLI locally (with your own creds and your own repos).
`agent-connect` lets that local agent appear as a **first-class agent in AG2 Space
rooms**: people `!codex …` (or `@`-mention it) in a room, the task is routed to
*your* agent, your machine runs it, and the result is posted back.

## Why a local worker (and not "just a server bot")

The whole point is **your** local agent — your API key, your working copy. A
browser or a server can't run your local CLI or see your repos. So agent-connect
is a tiny thing that runs **on your machine**:

- **Setup happens in the web Agent Portal** (create the agent, get a token + a
  one-line command). No desktop app required.
- **Execution happens locally** via this worker — one command links your machine,
  like a self-hosted CI runner.

## How it reuses AG2 Space infra (no new appservice)

AG2 Space's relay is already a generic **outbound** transport: a client on your
machine connects *out* and long-polls `GET /v1/tasks` for *your* agent
(identified by your token). Appservices (which require the homeserver to reach
*you*) don't scale to laptops behind NAT; the outbound relay does.

agent-connect speaks that relay itself, through **`ag2-relay-client`** — a
stdlib-only library published from this repository (`relay-client/`) that owns
the whole wire: the long poll, the lease, the acknowledgement, results,
heartbeats and room ops. The worker holds it in-process and takes tasks off its
queue. There is no task directory between the two and no second process: the
delivery guarantee was never a file queue's, it is the broker's lease, and a
worker restarting mid-task now re-*completes* the answer it already gave rather
than running the turn again.

```
room "!codex fix the flaky test"
      │  (relay routes to your Codex agent's token)
      ▼
agent-connect ── ag2-relay-client ──►  GET /v1/tasks   (long poll, leased)
                          │
                     the worker  ──►  codex exec --sandbox <tier> --cd <repo> "<task>"
                          │
                ag2-relay-client ──►  POST /v1/results ──►  posted back to the room
```

## Access tiers (safety)

This is the promise, in one sentence, and it is the same sentence the Agent
Portal makes:

> The broker attests every task's sender; the Worker trusts the attestation;
> owner tier → workspace write; guest → read-only sandbox on the codex path and
> a polite refusal on the ACP path; anyone in the room is attested guest
> already, and `allow @user` / `trust @user` — said in a **direct message to
> the concierge**, `@sutando-concierge:<your homeserver>` — set which tier a
> sender is attested at; the host may explicitly override a specific sender
> locally.

Unpacked:

**Two tiers cross the wire, and only two.** The broker knows who sent the
message, works out how much you trust them, and writes `access_tier: owner` or
`access_tier: guest` on the task. The worker acts on what the broker said and
does not re-decide it — that attestation is the only thing standing between an
allowed stranger and your files. A task whose tier is missing, duplicated or
unrecognised is **treated as `guest`, never `owner`**: the absence of an
attestation is not a grant of one (`docs/adr/0003`).

**Owner is a trust level, not a person.** You, who registered the agent, are
owner. `trust @user` makes someone else owner too; `allow @user` names someone
as a guest. Both are yours to give and yours to take back.

**They are a dial, not a door.** Somebody who is in a room with your agent can
address it already: they arrive attested `guest` by default and get whatever
guest gets on your adapter, `allow` or no `allow`. What these two commands
change is *which tier* a sender is attested at — they are not what lets anyone
in, and withholding one keeps nobody out. The way to keep somebody out is to not
have them in the room.

**Both are commands to the concierge, and to nothing else.** Say them in a
**direct message to `@sutando-concierge:<your homeserver>`** — the same bot that
registered the agent for you. Typed anywhere else they are not commands at all:
in your agent's room, or in a direct message to the agent, `trust @user` is
ordinary text arriving at a language model. The platform answers such a message
with a one-line pointer back to the concierge rather than acting on it, and
nothing is granted. The concierge is deliberately not a member of your agent's
rooms, so looking around will not find it — this line is where the recipient is
written down.

**`trust` hands over more than the word suggests.** Both of the following are
standing behaviour rather than rough edges awaiting a fix, and both are worth
knowing *before* you grant it:

- **It applies to every agent you own, not the one you had in mind.** Trust is
  modelled as a relationship between *people*, so a single `trust @user` makes
  that person owner on all of your agents at once — the concierge's reply lists
  which ones. `untrust @user` takes it back everywhere in the same one go.
- **They do not get a session of their own — they continue yours.** A session is
  keyed by room *and* access tier together (see [The agent remembers the
  conversation](#the-agent-remembers-the-conversation-acp)), so a second
  owner-tier sender in the same room lands in the *same* session you left there:
  the same session id, the same working directory, and whatever context your own
  conversation has already accumulated in it. `trust` therefore shares a live
  conversation as well as workspace write. This was weighed and kept
  deliberately — it is part of what owner tier means here.

**What each tier gets depends on the adapter, and the difference is not
cosmetic:**

- **owner** → `workspace-write` (edit files, run builds), on every adapter.
- **guest, on the codex path** → `read-only` (read/analyse/answer only). This is
  **operating-system confinement**: the sandbox is enforced by the agent CLI and
  the OS, so an agent that ignores it is still stopped.
- **guest, on the ACP path** → **a polite refusal in the room**, and no run at
  all. ACP has no sandbox to fall back to — see the next section — so there is
  no read-only tier to offer, and offering one anyway would be a limit that only
  looks like one. The refusal is a visible reply, not silence: it climbs the
  same ladder as an answer, so the guest reads a sentence saying what happened
  and that `trust` would change it.

**You can lower a specific sender on your own machine.** The relay client's
local `access.json` tier map is yours, and it is a **cap, not a re-tier**: the
tier a task runs at is the lower of what the broker attested and what your map
allows, so the map can take trust away from a named sender on this host and can
never hand any out. Nothing a *sender* writes touches it either way.

## The ACP adapter: cooperative confinement, owner only

The `acp` adapter drives any agent that speaks the [Agent Client
Protocol](https://agentclientprotocol.com) (Claude Code via
`@agentclientprotocol/claude-agent-acp`, Cline, PI, …). **Its safety story is
different from every other adapter's, and weaker. Read this before enabling it.**

- **There is no sandbox.** ACP gives the OS no say in what the agent touches. The
  agent does its own file access and asks the worker for permission as it goes.
- **The limit is a *permission policy*, and it is cooperative.** agent-connect
  allows operations under the session's working directory and rejects the rest —
  but that binds only an agent that *chooses to ask*. An agent that just writes
  the file is not stopped, because nothing is there to stop it. It is a
  convention, not a guarantee; it is not a substitute for a sandbox.
- **Owner-tier under ACP is therefore MORE permissive than owner-tier under
  codex.** Same tier, weaker confinement: codex runs owner-tier tasks under
  `codex exec --sandbox workspace-write`, where the OS refuses writes outside
  the workspace whatever the agent tries; ACP has no such boundary, only the
  cooperative policy above. Network is *not* the difference — agent-connect
  turns network access on for owner-tier codex tasks too
  (`sandbox_workspace_write.network_access=true`, see
  `agent_connect/adapters/codex.py`); it is off only for guest/read-only tasks,
  which never reach ACP at all.
- **Guest tasks are refused outright** and never reach ACP — the ACP agent is
  not even started. Shipping a read-only *tier* on top of a cooperative *policy*
  would be offering a limit that only looks like one. The refusal is **said in
  the room**, as the reply to the message that asked: it takes over the same
  `⏳ On it...` placeholder every other task gets, so it is one message rather
  than a placeholder left hanging beside an apology, and it names both what would
  change the answer (`trust @user`) and who has to be told — the concierge, by
  full MXID, because an instruction whose recipient is missing is not one. A
  guest who hears nothing cannot tell a refusal from a broken worker, and reports
  the wrong problem.

Name your agent and the worker knows what to run:

```bash
export AGENT_CONNECT_ADAPTER=acp
export AGENT_CONNECT_ACP_AGENT=claude          # a preset: claude | gemini
export AGENT_CONNECT_REPO=/path/to/the/repo    # the session's working directory
```

Or install it in one line, with the bridge pinned for you:

```bash
curl -fsSL <installer-url>/install.sh | sh -s -- --token <TOKEN> --adapter acp --acp-agent claude
```

**Presets never block you.** They live in code (`agent_connect/adapters/acp.py`)
and cover the agents we know about. For anything else, give the command
yourself — it overrides every preset:

```bash
export AGENT_CONNECT_ACP_COMMAND="my-agent --acp"
```

The installer takes the same thing as a flag, so an agent no preset describes is
still a one-liner:

```bash
curl -fsSL <installer-url>/install.sh | sh -s -- --token <TOKEN> \
     --adapter acp --acp-command "my-agent --acp"
```

`--acp-command` implies `--acp-agent custom`: a supplied command overrides every
preset anyway, so naming a preset would only npm-install a bridge you are not
using and make a login failure name an agent you are not running. Pass
`--acp-agent` as well if you want a different name recorded.

Re-running the installer **without** `--acp-command` keeps whatever command is
already in `config.env`, and keeps the agent name beside it — a plain re-run to
upgrade the worker does not quietly re-point your agent.

Naming a preset is how you switch back. `--adapter acp --acp-agent claude` on a
later run **clears** a stored `AGENT_CONNECT_ACP_COMMAND`, because a command
overrides every preset: keeping it would leave the old agent running while the
installer reported the preset you asked for. The installer says so when it
happens, and the previous file is at `config.env.bak`. An install for a
different adapter never touches these two keys — `--acp-agent` is ignored there,
out loud, and your ACP settings are left as they are.

**Startup checks, not surprises in a room.** Before the worker serves its first
task it starts the ACP agent, runs `initialize`, and stops with a sentence you
can act on if the bridge is not installed (naming the package and the install
line) or if the local agent is not logged in (naming the login command). It
opens no session and sends no prompt, so the check costs no tokens. The worker
**never opens an interactive terminal** on your machine — ACP has a way for an
agent to ask for one and agent-connect does not implement it, so logging in is
something you do yourself, in your own shell.

Refused permission requests are reported back in the reply, so a blocked agent is
distinguishable from a lazy one.

**The bridge is pinned.** `@agentclientprotocol/claude-agent-acp@0.64.2` — the
version a full turn was actually run against. It renamed itself once and moves
through major versions fast, so the installer never fetches `@latest`. The pin
lives in two places that `install.test.sh` forces to agree: `ACP_BRIDGE_SPEC` in
`install.sh` and `BRIDGE_VERSION` in `agent_connect/adapters/acp.py`.

## The ladder: one message, from "on it" to the answer

A request used to be met with silence until the answer arrived — sometimes ten
minutes of it. Now a placeholder appears as soon as the worker picks the task
up, the same message is edited as the agent works ("Read worker.py", "npm test"),
and it is finally edited into the answer. **One message**: the room sees exactly
one reply, and the answer carries a compact summary of what was run — including
the operations the permission policy rejected.

The shape is the relay's, not ours: the broker places the intake reaction, the
worker posts the fleet-wide `⏳ On it...` copy and edits it, and closes the lease
with `[REPLIED]` so nothing posts a second copy. The worker never speaks Matrix —
every post and edit is a room op the relay performs as your agent identity, over
the token you already have (`docs/adr/0002`).

The live editing on top of that two-step shape is agent-connect's own extension.
Edits are driven by **tool activity, not text chunks**, and throttled to at most
one per `AGENT_CONNECT_PROGRESS_THROTTLE` seconds. **The model's internal
reasoning never reaches the room** — the chat carries answers, not
thinking-out-loud. Set `AGENT_CONNECT_LIVE_PROGRESS=0` and you are back to the
plain placeholder-then-answer shape, with nothing else changed. An answer too
long to fit in an edit is not truncated: the placeholder becomes a short pointer
and the answer arrives in full as its own message.

Adapters that are not ACP get the placeholder and the final answer too, with
nothing in between — they have no tool activity to report, and inventing some
would put fiction in a room.

If the worker holds no relay token, or the relay refuses a room op, the answer
travels the way it always did — as the task result the relay client posts. The
ladder degrades; it never eats the answer.

## The agent remembers the conversation (ACP)

Ask for something, then say "now the same for the other module", and it works:
each room continues one conversation with your local agent instead of starting
over every message. The session is keyed by **room *and* access tier together**,
never by room alone — a session carries a permission mode and its own context,
and a lower-trust request must inherit neither.

The session survives you restarting the worker: the identifier is kept in
`<workspace>/sessions.json` and resumed on the next message. Resuming makes the
local agent replay the whole prior exchange, which the worker consumes **in a
suppressed mode that posts nothing** — restarting must not dump yesterday's
transcript into a live room.

**Memory is bounded, and its ending is announced.** A session retires after
`AGENT_CONNECT_SESSION_TURNS` turns or `AGENT_CONNECT_SESSION_IDLE` seconds of
silence, and the room is told, in its own message, that the context was reset
and why. The same message appears when a conversation cannot be resumed — an
agent that refuses, or one that never offered resumption at all: the request is
answered from a fresh session rather than failed. Silent amnesia is the problem
being solved, so nothing here happens quietly. Set
`AGENT_CONNECT_SESSION_MEMORY=0` and every message starts fresh, as it did
before.

Only the ACP adapter has this. The other five run a one-shot CLI per task and
have no session to continue.

## Nothing is lost, and endings tell the truth

**A message that arrives while the agent is busy is queued, and you are told
so.** Only one turn at a time may be open on a session — the protocol's rule,
not a choice — so a second message in the same room waits for the first. It
waits *out loud*: a short "this one is queued and will be answered next" lands
before the placeholder, and the answer follows when the earlier turn is done.
Queueing is per session, so a room that is busy blocks nothing but itself and
every other room keeps running.

**A turn that runs too long is cancelled through the protocol, never by killing
the agent.** After `AGENT_CONNECT_TURN_TIMEOUT` seconds the worker sends
`session/cancel`; the local agent stops and hands back everything it had
produced. That partial answer goes to the room **with the interruption stated
plainly** — long work that nearly finished is not thrown away, and an answer cut
off mid-sentence with no note reads as a complete one. Ending the process is
the last resort, only after a cancellation the agent ignored, and it takes that
one turn's process with it and no other room's conversation.

**A turn that produced nothing produces no reply from the worker.** A refusal,
a deadline that arrived before any output, a bridge that died: each is written
as a `[no-send]` result, which the relay client archives and delivers nowhere.
The failure notice in the room is the broker's to post, and two apologies for
one failure is worse than one. Nothing about it is silent, though: the reason is
in the archived result, and any stop reason other than a finished answer always
appears as its own line rather than as silence.

**The local agent's process dying does not end the worker.** One crash is one
turn's failure. The session identifier is kept, so the next message in that room
resumes the conversation rather than starting over.

## A screenshot you drop in the room is a screenshot the agent sees

Attach an image to a message and ask what is wrong with it, and the local agent
is shown the image — as **content of the prompt**, beside your text, not as a
filename it is told to go and open. The relay client has already downloaded the
file by the time the worker sees the task, so nothing is fetched from the room.
Several attachments on one message are all passed, in the order you sent them,
and your own text goes through untouched.

**Nothing is converted, resized or transcoded.** The bytes the agent receives
are the bytes on disk. `AGENT_CONNECT_ATTACHMENT_MAX_BYTES` bounds how much of
one file is read, and a file over it is *reported*, never shrunk to fit — a
resized screenshot is a different screenshot.

**An attachment the agent cannot take is said out loud.** What it accepts is
what it advertised when the worker connected to it (ACP's `promptCapabilities`);
a kind it did not advertise, a file that has gone missing, a file over the
limit — each is named in the room, with the reason, and with the suggestion to
paste the content instead. The failure a person can act on is better than the
one they find out about by reading an answer that ignored their screenshot.

The other adapters run through a plain command line that takes text and nothing
else. They say exactly that, for the same reason — and they do **not** pass the
file's path to the agent instead: that would tell it to go and read a file from
a directory the relay owns, which is not what someone attaching a picture asked
for.

## A file the agent produced arrives in the room

Ask for a report, a diff or a chart and the file itself lands in the room —
openable, not pasted into a code block. The agent writes the file in its working
directory and names it in its reply (`[file: report.md]` on a line of its own);
the worker takes the marker out of the text, so the room reads prose and receives
the file beside it, as one reply. Several files from one turn all arrive, in the
order the agent named them.

**The route matters more than the feature.** A file goes out through the relay
client's **egress allowlist**: a list of directories, fixed when the worker
builds the client and impossible to widen afterwards, holding the working
directory the agent runs in and anything you added in
`AGENT_CONNECT_EGRESS_ROOTS`. The allowlist is what stops an agent from being
talked into attaching a private key or somebody's tax return to a chat message.
Restricting the ACP adapter to the owner narrows who can try; it does not close
it, because the owner in a room is not necessarily the person sitting at the
machine.

The check runs **inside this process**, on the open file descriptor rather than
on the path: resolved first, then opened one directory component at a time with
`O_NOFOLLOW`, then judged by `fstat` for regular-file-ness, size and a single
hard link. It used to run in a *separate* process, and workspace `docs/adr/0001`
records the loss of that separation honestly — the guarantee stays permission
policy, not confinement, which is what it always was, since the worker has always
held the token.

**A file outside the allowlisted directories is not sent, and the room is told
so** — by name and with the reason, in the same reply. A file that silently fails
to arrive is indistinguishable from an agent that ignored the request. The same
line appears for a file that has gone missing, one that is not a regular file,
one reached through a symlink pointing out of the permitted area, and one over
the relay's upload ceiling.

What this does not prevent: an agent talked into *copying* a private file into
its working directory can then send the copy. The permitted area is a boundary on
paths, not a proof about contents — closing that needs a sandbox that refuses the
first copy.

## The config file: settings written down, not exported

Everything below is an environment variable, which is a fine interface for a
shell and a poor one for a service. So the same keys can be written in a file
instead, and the worker reads it itself:

```bash
install -m 600 /dev/null ~/.agent-connect/config.env
cat > ~/.agent-connect/config.env <<'EOF'
AGENT_CONNECT_TOKEN=<token from the portal>
AGENT_CONNECT_ADAPTER=acp
AGENT_CONNECT_ACP_AGENT=claude
AGENT_CONNECT_REPO=/Users/me/agents
EOF
agent-connect
```

**The keys are the ones in the Settings table below** — the same names, no
translation, nothing to learn twice. `KEY=value`, one per line, `#` comments,
everything after the first `=` taken verbatim (one matching pair of surrounding
quotes is removed, and that is the whole of the syntax). It is **not** a shell
script and is never evaluated as one, so the launcher and the worker read the
same file and agree about what it says.

**Where it looks**, in order: `--config <path>` on the command line, then
`AGENT_CONNECT_CONFIG`, then `~/.agent-connect/config.env` if it exists. The
flag exists so a start needs no environment at all; the variable exists so a
service unit can point at a file without putting anything else on a command
line. A file you *named* and that is not there stops the worker — it holds the
token, and starting without it gives you a worker that runs, pulls nothing and
looks healthy.

**Environment variables win over the file.** A setting already exported is left
exactly as it is, per setting, and the worker says which ones the file offered
and did not get:

```
agent-connect: config /Users/me/.agent-connect/config.env — 4 setting(s) applied,
  1 already set in the environment (AGENT_CONNECT_ADAPTER) and left alone
```

That direction is the only one that makes `AGENT_CONNECT_ADAPTER=codex
agent-connect` mean what it says. A config file is a default that persists, not
an authority that overrules the shell you are standing in.

**It may set settings and nothing else.** Only `AGENT_CONNECT_*`,
`REMOTE_TASK_*` and `OLLAMA_HOST` are applied; any other key is named on stderr
and ignored, because a file that could set `PATH` would be deciding which
`codex` binary runs. A misspelt setting is named for the same reason: it looks
exactly like a setting that did not work. A key written **twice** takes the last
line, as it would in any file of this shape, and the repetition is named too —
that one is worth being sure about when the setting is a token.

**There is exactly one parser, and the launchers ask it.** A launcher has to
know a setting or two of its own — whether there is a token at all, which
workspace's pidfile to clear — so `launch.sh` and `run-agent.sh` run

```bash
_cfg="$(agent-connect --export-config)" || exit 1
eval "$_cfg"
```

which prints the config file as `export` lines — with the environment's own
values already deferred to — and nothing else on stdout. The shell loop that
used to do this instead disagreed with the worker about duplicated keys, CRLF
line endings and whitespace-only variables; the first of those handed the relay
client and the worker *different tokens* out of one file. Two readers of one
file is two answers to "what does this file say".

**It holds your agent's token, so keep it to yourself.** The installer writes it
`0600`. A file others can read is still loaded — refusing would break a working
install over a warning's worth of problem — and complained about at every start,
with the `chmod` that fixes it.

**This is what the installer now does.** `install.sh` writes your token, adapter
and working directory into that file and points the launchd plist (or systemd
unit) at it with `AGENT_CONNECT_CONFIG`, so **the service definition carries no
bearer token** — it used to sit in plaintext in
`~/Library/LaunchAgents/space.ag2.agent-connect.plist`, which is world-readable
by default. Re-running the installer keeps the previous file as
`config.env.bak` and carries across every key it does not manage itself, so a
setting you added by hand survives a re-run of the one-liner.

## Running more than one agent on one machine

**The launch unit is one worker pointed at one config file.** That is the
contract a supervisor builds on, and it is deliberately not a wire: what a
supervisor should depend on is the unit, named by the config file it was given.

```bash
agent-connect --config ~/.agent-connect/instances/scratch/config.env
```

**Today that unit is one process, and a supervisor may rely on it.** The worker
carries its own relay client (workspace ADR 0001), so there is nothing to start
beside it — `install.sh` writes a `launch.sh` that execs the worker and nothing
else. It was a pair once, worker plus a separate `ag2-sparrow` process, and the
pair was not merely one process too many: both halves long-polled the gateway
with the same bearer and excluded each other from nothing, so whichever won a
given long-poll took the task. When ag2-sparrow won, the task went into a
directory the worker no longer reads, and the person who sent the message got
no answer and no error. If you are supervising a worker installed before this
changed, re-run `install.sh` — an old `launch.sh` still starts both.

**An instance is a config file and a workspace, and they come together.** Give
each instance its own config file, and in it its own `AGENT_CONNECT_WORKSPACE`
— everything per-instance hangs off that one setting:

```bash
# ~/.agent-connect/instances/scratch/config.env   (mode 0600)
AGENT_CONNECT_TOKEN=<this instance's own agent token>
AGENT_CONNECT_ADAPTER=acp
AGENT_CONNECT_ACP_AGENT=claude
AGENT_CONNECT_REPO=/Users/me/agents/scratch
AGENT_CONNECT_WORKSPACE=/Users/me/.agent-connect/instances/scratch/workspace
```

**Two instances must never share a workspace.** The relay client's state (its
journal of which tasks have been answered), the session map and the status file
all live in it, and two workers sharing one
journal each believe the other's tasks are already answered. Two instances
sharing an *agent token* is the same bug one layer up: one queue, two pullers.
A new instance means a new Agent Identity.

Each instance also owns a **status file** under its own workspace, which is how
a supervisor watching several of them tells one worker's state from another's.

## The status file: what the worker says about itself

**Path:** `<workspace>/status.json` — by default
`~/.agent-connect/workspace/status.json`, or wherever `AGENT_CONNECT_STATUS_FILE`
says. This is part of the service contract: anything watching a worker (the
desktop app's badge first, `cat` second) reads that file and needs to know
nothing about the relay client underneath.

```json
{
  "version": 1,
  "state": "serving",
  "detail": "adapter=acp repo=/Users/me/agents ws=/Users/me/.agent-connect/workspace",
  "pid": 4711,
  "adapter": "acp",
  "agent": "acp: Claude Code 2.1.0",
  "repo": "/Users/me/agents",
  "workspace": "/Users/me/.agent-connect/workspace",
  "instance": "scratch",
  "tasks_running": 1,
  "oldest_task_seconds": 12.4,
  "started_at": 1755600000.0,
  "updated_at": 1755600123.0,
  "uptime_seconds": 123.0,
  "heartbeat_seconds": 15.0,
  "last_error": null,
  "relay": {
    "state": "connected",
    "connected": true,
    "gateway": "https://chat.ag2.space/relay",
    "last_ok_ts": 1755600120.0,
    "backoff_s": 0.0,
    "recheck_s": 0.0,
    "acks_paused_s": 0.0,
    "singleton": "held",
    "error": null,
    "inflight": 0,
    "pending_results": 0,
    "updated_ts": 1755600122.0
  }
}
```

**The file is layered, and the layers are two different facts.** `state`,
`adapter` and `agent` are the worker's own account of itself — what is
configured, and whether the local agent behind the identity answered its
preflight. `relay` is the connection's, read off the relay client's status hook:
whether the broker is reachable (`connected`, `state` is one of `connected`,
`reconnecting`, `auth-wait`, `fatal`, `standby`, `displaced`, `stopped`), when
it last was (`last_ok_ts`), how long the client is waiting before its next
attempt (`backoff_s`), how many tasks it has accepted and not yet answered
(`inflight`), and how many answers it is still trying to hand back
(`pending_results`). The gateway URL is redacted before it is ever written.
`relay` is `null` before the relay client is constructed — "no relay" and "a
relay that is offline" are different facts about a worker.

Three of those need a sentence of their own, because they are the ones an
observer misreads. **`standby`** is not an outage: another poller holds this
bearer's guard, so this client is deliberately not polling and is asking again
every `recheck_s` — one bearer tolerates exactly one poller, and two would
double-deliver every message. It is also the state a *restarted* worker sits in
for up to two and a half minutes when its predecessor was killed rather than
stopped. **`displaced`** is terminal: this client held the guard and another
poller took it, so it has stopped for good and only a restart changes it.
**`acks_paused_s`** is seconds left of the ack cooldown, and non-zero means
tasks accepted right now are having their leases requeued underneath running
turns — the number that tells "quiet" apart from "everything is being retried".
**`singleton`** is the guard's own verdict — `held`, `lost`, `degraded` (it
could not be read, so the client polls anyway), `idle` or `off` — because
"polling with the guard held" and "polling anyway with a guard nobody could
read" look identical from outside and are not the same situation.

**The relay block does not beat.** `updated_at` is refreshed by the worker's own
heartbeat and by nothing else, so it goes on meaning exactly one thing: this
process's event loop is turning. The relay client's status hook runs on its own
polling thread, and a live polling thread proves nothing about a wedged event
loop — so the hook writes the connection through without touching either clock.
The block carries the client's own `updated_ts`, for a reader who wants to know
whether the *relay client's* view is current.

**The block is a projection, and the list above is all of it.** The worker
copies exactly those fields out of the client's snapshot rather than passing the
snapshot through, because this file is a contract with an outside reader and a
block that silently gained whatever the library added next would be a contract
nobody wrote down. `tests/test_worker_queue.py` fails when the two lists drift
apart.

**Four states, and that is all of them:** `starting` (the file exists before
anything can go wrong, so a worker that dies in preflight still leaves the
reason), `serving`, `stopped` (it was asked to stop, and said so), and `error`
— which carries the sentence the operator has to act on, in `detail` and in
`last_error`.

**Is it alive?** Compare the clocks: a worker is **stale** when
`now - updated_at > 3 x heartbeat_seconds`. `updated_at` is refreshed every
half-interval while the worker is running, and the file states that interval
itself so a reader needs nothing out of band. The slack is deliberate — one
missed write is a busy machine, six is nobody home. A `kill -9`, a panic, a
closed laptop lid: none of them get to write "stopped", and all of them show up
as staleness. A worker that stops on purpose says so, so the ordinary case is
not something anyone has to wait out.

**Nothing else paces the heartbeat.** It is its own task, so no other setting
can switch this promise off — `AGENT_CONNECT_POLL` in particular does not
change how fresh this file is. It also runs during startup, so a worker waiting
on an ACP bridge that takes a minute to answer `initialize` stays visibly
`starting` instead of reading as dead.

**What "serving" proves, and what it does not.** A fresh file means the process
is alive and its loop is turning. It does **not** mean any turn is making
progress: turns run as separate tasks, so a worker whose every turn is wedged on
an unresponsive local agent keeps beating `serving` quite happily. That is what
`tasks_running` and `oldest_task_seconds` are for — one turn that has been
running for two hours is visible as exactly that, and an observer that cares
about *work* rather than *process* reads those two fields. Cancelling a wedged
turn is `AGENT_CONNECT_TURN_TIMEOUT`'s job; this file reports, it does not
adjudicate.

**One clock caveat, stated rather than papered over.** `updated_at` is the wall
clock, because it has to be comparable across processes. If the system clock
steps *backwards* — an NTP correction after a long sleep — a dead worker's last
write can sit in the future and read as fresh until real time catches up.
`uptime_seconds` is monotonic and sits beside it, so a reader that cares can
notice a document whose uptime stopped advancing.

**One file per instance.** The path hangs off the workspace, and a workspace
belongs to exactly one worker (see § Running more than one agent on one
machine), so N instances write N status files with nothing extra to configure.
`instance` carries `AGENT_CONNECT_INSTANCE` — whatever name the supervisor that
started this worker calls it — so N files can be matched to N rows without
having to recognise a path.

**Fields you do not recognise are not errors.** `version` goes up only for a
change a reader could not survive; ignore what you do not know.

Writes are atomic (write beside, rename over), so a reader never catches half a
document. A status file that cannot be written is complained about once, on
stderr, and never fails a task — an observer falls back on staleness, which is
what it has for the `kill -9` case anyway.

**One cost, named.** So that a service manager's `SIGTERM` can write "stopped"
rather than leaving an observer to wait out staleness, the worker turns it into
an ordinary exit. That exit is raised at whatever bytecode the interpreter
happens to be on, so a task result being written at that instant can be left
truncated, where an unhandled `SIGTERM` would have killed the process between
syscalls. The answer itself has already gone to the room up the ladder; what is
at risk is one archived result file.


## Settings

**This table is the authoritative list of every setting agent-connect reads.**
Not the module docstrings, not the installer's `--help` — here.
`tests/test_acp_settings.py` fails if a setting exists in the package and not in this
table.

| Setting | What it does | Default |
| --- | --- | --- |
| `AGENT_CONNECT_CONFIG` | the config file to read (see the section above). The `--config` flag wins over it | `~/.agent-connect/config.env`, if it exists |
| `AGENT_CONNECT_TOKEN` | your agent identity's relay token, from the Agent Portal. It is a combined `<gateway-url>\|<secret>` credential: the gateway travels inside it, and there is no default to fall back on, so a bare secret needs `REMOTE_TASK_URL` beside it. Without a token the worker refuses to start, because it would have no way of being given any work | *(required)* |
| `AGENT_CONNECT_ADAPTER` | which adapter runs the task: `codex`, `ollama`, `omnigent`, `cline`, `kilo`, `acp` | *(required)* |
| `AGENT_CONNECT_REPO` | the working directory the agent operates in. Created for you when it is the default; a path under `~/Documents`, `~/Desktop` or `~/Downloads` is warned about, because macOS privacy protection turns agent file operations there into an unexplained "operation not permitted" | `~/agents` |
| `AGENT_CONNECT_WORKSPACE` | workspace dir holding the relay client's state under `relay/`, the session map and the status file | `~/.agent-connect/workspace` |
| `AGENT_CONNECT_STATUS_FILE` | the status file this worker owns (see the section above) | `<workspace>/status.json` |
| `AGENT_CONNECT_STATUS_HEARTBEAT` | seconds between refreshes of the status file's `updated_at`, and the staleness window an observer reads out of it | `15.0` |
| `AGENT_CONNECT_INSTANCE` | a name for this worker instance: carried into its status file so a supervisor watching several can tell them apart, and used to namespace the relay client's state under `<workspace>/relay/`. Letters, digits, `_` and `-`, at most 32 — a name outside that is refused rather than mangled, because two instances quietly sharing one sanitised name would share one journal | `default` |
| `AGENT_CONNECT_POLL` | seconds one read of the relay client's task queue waits before the worker looks around. It paces nothing else: a task that arrives wakes the read immediately, and the long-poll cadence on the wire belongs to the relay client | `1.0` |
| `AGENT_CONNECT_ATTACHMENT_MAX_BYTES` | how much of one attached file is read into a prompt. An attachment over this is reported in the room, never shrunk to fit. `0` means no limit | `10485760` (10 MB) |
| `AGENT_CONNECT_EGRESS_ROOTS` | extra directories a file the agent produced may be sent to the room *from*, separated by `:`. The working directory (`AGENT_CONNECT_REPO`) is always one; this is for a worker whose agent writes somewhere else as well. Nothing outside these directories is ever uploaded, and the list is fixed when the worker starts | *(none)* |

The ladder (see the section above):

| Setting | What it does | Default |
| --- | --- | --- |
| `AGENT_CONNECT_LIVE_PROGRESS` | `0` turns live progress editing off and leaves the plain placeholder-then-answer ladder. **This is the one setting that retreats to the fleet-wide convention** | `1` |
| `AGENT_CONNECT_PROGRESS_THROTTLE` | seconds between progress edits, however busy the turn is | `3.0` |
| `AGENT_CONNECT_EDIT_CEILING` | characters an answer may have and still be edited into the placeholder; a longer one arrives as its own message | `4000` |

Sessions — the conversation a room continues (ACP adapter only, see the section
above):

| Setting | What it does | Default |
| --- | --- | --- |
| `AGENT_CONNECT_SESSION_TURNS` | turns a session may run before it is retired and the room is told the context was reset. `0` means no budget | `20` |
| `AGENT_CONNECT_SESSION_IDLE` | seconds of silence after which a session is retired, announced the same way. `0` means never | `3600` |
| `AGENT_CONNECT_SESSION_MEMORY` | `0` turns continuing conversations off entirely: every task opens a fresh session and nothing is written to disk | `1` |
| `AGENT_CONNECT_SESSION_STORE` | file the room-to-session map is kept in, so conversations survive a worker restart | `<workspace>/sessions.json` |

ACP adapter (see the section above):

| Setting | What it does | Default |
| --- | --- | --- |
| `AGENT_CONNECT_ACP_AGENT` | preset naming the ACP agent to run: `claude`, `gemini` | *(unset)* |
| `AGENT_CONNECT_ACP_COMMAND` | the ACP agent's command line, split as a shell would. **Overrides any preset**. `install.sh --acp-command` writes it; a re-run without that flag keeps it | *(unset)* |
| `AGENT_CONNECT_ACP_MODE` | session mode id to request. Left alone by default: every bridge's own default already routes permission requests to the worker, and mode ids are the agent's to name | *(unset)* |
| `AGENT_CONNECT_ACP_SKIP_AUTH_CHECK` | `1` to start even when the agent advertises authentication methods. The escape hatch for an agent whose `authMethods` mean something else | *(unset)* |
| `AGENT_CONNECT_TURN_TIMEOUT` | seconds one turn may run before it is cancelled **through the protocol**, keeping whatever it produced. `0` waits forever | `600` |
| `AGENT_CONNECT_ACP_BRIDGE_SPEC` | *(installer only)* override the pinned bridge npm spec | the pin above |

Other adapters:

| Setting | What it does | Default |
| --- | --- | --- |
| `AGENT_CONNECT_OLLAMA_MODEL` | model tag the `ollama` adapter asks for | `qwen2.5:3b` |
| `OLLAMA_HOST` | the Ollama server the `ollama` adapter talks to | `http://localhost:11434` |
| `AGENT_CONNECT_OMNIGENT_HARNESS` | default harness for the `omnigent` adapter (a `[harness]` prefix in the message wins) | `claude` |
| `AGENT_CONNECT_OMNIGENT_MODEL` | model passed to the omnigent harness | *(harness default)* |
| `AGENT_CONNECT_OMNIGENT_BIN` | path to the `omnigent` binary | `omnigent` |
| `AGENT_CONNECT_CLINE_BIN` | path to the `cline` binary | `cline` |
| `AGENT_CONNECT_CLINE_PROVIDER` | provider the `cline` adapter selects | *(cline default)* |
| `AGENT_CONNECT_CLINE_MODEL` | model the `cline` adapter selects | *(cline default)* |
| `AGENT_CONNECT_KILO_BIN` | path to the `kilo` binary | `kilo` |

`REMOTE_TASK_TOKEN` and `REMOTE_TASK_URL` are the names the old two-process
launcher exported for the relay client's own process. Both are still read, and
mean what they always meant: the same credential under its other name, and a
gateway for a bare secret that does not carry one. **`AGENT_CONNECT_TOKEN` wins
where both are set** — the setting this document calls the setting outranks the
one it calls the old name, so a `REMOTE_TASK_TOKEN` forgotten in an old launchd
plist cannot quietly beat a token you have just rotated. And a gateway that
travels *inside* a combined token wins over `REMOTE_TASK_URL`, for the wire and
for the room ops alike: the URL that arrived with a credential is the one that
credential belongs to.

**Retired: `AGENT_CONNECT_TASK_DIR`, `AGENT_CONNECT_RESULT_DIR` and
`AGENT_CONNECT_STATE_DIR`.** They were the directory interface of `ag2-sparrow`,
the separate process that used to carry the wire, and nothing in this package
has read them since the worker took the wire over. Setting them now does
nothing at all — not even to a relay client, because this install starts none.
An old `launch.sh`, or a launcher of your own, that still exports them can drop
the lines.

`AGENT_CONNECT_RESULT_DIR` was the exception: it *was* read here, by the
outgoing-file staging airlock. That protocol is retired, and its replacement is
`AGENT_CONNECT_EGRESS_ROOTS` above. `AGENT_CONNECT_OUTGOING_MAX_BYTES` went with
it — the size ceiling is the relay client's own, so there is one number rather
than two that can disagree.

## Adapters

An adapter is ~20 lines: "given a task string + sandbox + working dir, run the
agent and return its output." Ships with:

**Three integration levers** (pick per agent):
1. **Direct adapter** — a ~30-line wrapper around an agent's own headless CLI. Best when the agent has a clean exec mode.
2. **`omnigent` adapter** — one adapter that drives [omnigent](https://github.com/omnigent-ai/omnigent)'s whole harness catalog (claude, codex, cursor, kimi, qwen, goose, hermes, pi, opencode, …). Unlocks many agents from a single file and isolates that (alpha) dependency. Per-message `[harness]` prefix selects the harness.
3. **`acp` adapter** — one adapter for any agent that speaks the Agent Client Protocol (Cline, Pi, Codex, Claude, OpenClaw via `acpx`). **Owner-tier only, cooperative confinement — see the section above.**

**Shipped adapters:**
- **codex** — `codex exec`. ✅ verified, live.
- **ollama** — local model via the Ollama HTTP API (fully private, no provider auth). ✅ verified, live.
- **omnigent** — drives any omnigent harness. ✅ verified, live.
- **cline** — `cline -y`. ✅ verified (command path + auth handling); go-live needs Cline auth.
- **acp** — any ACP-speaking agent, driven over stdio. ⚠️ owner-tier only, and the confinement is cooperative (see above).
- **kilo** — `kilo run --auto`. ⚠️ scaffold; headless output capture unverified (needs Kilo auth to confirm / finish).

**Roadmap coverage** (owner list Codex/Hermes/OpenClaw/Cline/PI/Kilo): Codex ✅ · Hermes ✅ (omnigent) · PI ✅ (omnigent / ACP) · Cline ✅ (direct / ACP) · Kilo ⚠️ (direct scaffold, or omnigent's opencode harness) · OpenClaw = a personal-assistant *gateway*, not a coding harness → reach its coding via ACP.

**Auth model — two independent layers:** (1) agent identity → AG2 Space (a relay token we issue); (2) the agent's own tool auth (its login / provider API key), which AG2 Space never sees. `ollama` needs no provider auth (the model is local).

The framework is agent-agnostic; the relay client + onboarding + access-tiers are shared.

## Quick start (MVP)

1. In the AG2 Space Agent Portal, **create a Codex agent** → copy your token.
2. On your machine:
   ```bash
   export AGENT_CONNECT_TOKEN=<token from the portal>
   export AGENT_CONNECT_ADAPTER=codex
   export AGENT_CONNECT_REPO=/path/to/the/repo/codex/should/work/in
   agent-connect
   ```
3. In an allowed room: `!codex summarize this repo` → your Codex replies.

**`agent-connect` is the whole thing to run.** The worker carries its own relay
client (workspace ADR 0001), so it long-polls the gateway itself — there is no
second process to start. From a checkout of this repo, `./run-agent.sh` is the
same single process with a pidfile that clears the last one.

## Tests

They live in `tests/` and each one is a plain script — no runner, no test
framework, and every assertion printed as it passes. Run one by naming it:

```bash
python3 tests/test_worker_parse.py          # no dependencies
.venv/bin/python tests/test_acp_core.py     # the ACP tests need the package
bash install.test.sh                        # the installer
```

The split is the point: everything that can run under a bare interpreter still
does, so a broken environment cannot quietly take the whole suite with it. The
ACP tests are the ones that talk to `agent-client-protocol` (see
`docs/adr/0001`); run under bare `python3` they exit with the venv command
rather than a traceback:

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

`tests/fake_acp_agent.py` is not a test — it is the fake ACP Agent the ACP tests
drive: a real child process speaking real JSON-RPC over stdio, scripted per
test. `tests/test_acp_real_bridge.py` is opt-in (`ACP_REAL_BRIDGE=1`) because it
spends real tokens against a real bridge.

## Status

The ACP seam is built (2026-08-10, plan `.scratch/acp-adapter/plan.md`, tickets
01–10 all resolved): async worker serving rooms concurrently, the ACP Adapter
(owner-only, cooperative Permission Policy), presets with a pinned bridge and a
startup auth check, the Ladder (placeholder → live edits → answer), Sessions per
room and Access Tier surviving restart, the full Turn lifecycle (queueing,
cancellation, honest endings), and attachments in both directions. The five
original adapters (codex first among them) still run through the shim. Not yet
verified against a live relay: the room-op wire path and real-bridge session
resume/cancel — both degrade safely. Portal onboarding and the desktop-hosted
worker are the next efforts (`.scratch/byo-agent/` at the workspace root).
