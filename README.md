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
machine connects *out*, long-polls `GET /v1/tasks` for *your* agent (identified by
your token), drops each task into `tasks/`, and posts `results/` back. Appservices
(which require the homeserver to reach *you*) don't scale to laptops behind NAT;
the outbound relay does.

So agent-connect = **the existing relay client** (transport, unchanged) **+ this
worker** (runs your agent on each task). The only new code is the worker + a small
per-agent adapter.

```
room "!codex fix the flaky test"
      │  (relay routes to your Codex agent's token)
      ▼
relay client  ──►  tasks/task-<id>.txt
                          │
                     agent-connect worker  ──►  codex exec --sandbox <tier> --cd <repo> "<task>"
                          │
                   results/task-<id>.txt  ──►  relay client  ──►  posted back to the room
```

## Access tiers (safety)

The relay stamps who sent the task (`access_tier`). The worker maps it to a
sandbox so a stranger in a shared room can't make your agent edit your files:

- **owner** → `workspace-write` (edit files, run builds)
- **everyone else** → `read-only` (read/analyse/answer only)

This is **operating-system confinement**: the sandbox is enforced by the agent CLI
and the OS, so an agent that ignores it is still stopped. The `acp` adapter is the
exception — see below.

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
  codex.** Same tier, weaker confinement: no OS sandbox, and network access is
  always on (`codex exec --sandbox workspace-write` gives neither).
- **Non-owner tasks are refused outright** and never reach ACP, with a message
  saying why. Shipping a read-only *tier* on top of a cooperative *policy* would
  be offering a limit that only looks like one.

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

**The route matters more than the feature.** A file goes out by being placed in
the **outgoing result directory** — the same `results/` the worker already writes
into, and the only directory the relay client's send allowlist trusts. The worker
posts no media itself, and that is deliberate: the allowlist is what stops an
agent from being talked into attaching a private key or somebody's tax return to
a chat message, and a worker that uploaded files directly would turn any message
into an exfiltration trigger. Restricting the ACP adapter to the owner narrows
who can try; it does not close it, because the owner in a room is not necessarily
the person sitting at the machine.

**A file outside the working directory is not sent, and the room is told so** —
by name and with the reason, in the same reply. A file that silently fails to
arrive is indistinguishable from an agent that ignored the request. The same line
appears for a file that has gone missing, one that is not a regular file, and one
over `AGENT_CONNECT_OUTGOING_MAX_BYTES`.

What this does not prevent: an agent talked into *copying* a private file into
its working directory can then send the copy. The permitted area is a boundary on
paths, not a proof about contents — closing that needs a sandbox that refuses the
first copy.

## The status file: what the worker says about itself

**Path:** `<workspace>/status.json` — by default
`~/.agent-connect/workspace/status.json`, or wherever `AGENT_CONNECT_STATUS_FILE`
says. This is part of the service contract: anything watching a worker (the
desktop app's badge first, `cat` second) reads that file and needs to know
nothing about the transport underneath.

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
  "tasks_running": 0,
  "started_at": 1755600000.0,
  "updated_at": 1755600123.0,
  "heartbeat_seconds": 15.0,
  "last_error": null
}
```

**Four states, and that is all of them:** `starting` (the file exists before
anything can go wrong, so a worker that dies in preflight still leaves the
reason), `serving`, `stopped` (it was asked to stop, and said so), and `error`
— which carries the sentence the operator has to act on, in `detail` and in
`last_error`.

**Is it alive?** Compare the clocks: a worker is **stale** when
`now - updated_at > 3 x heartbeat_seconds`. `updated_at` is refreshed at least
every `heartbeat_seconds` while the worker is serving, and the file states that
interval itself so a reader needs nothing out of band. Three intervals of slack
is deliberate — one missed write is a busy machine, three is nobody home. A
`kill -9`, a panic, a closed laptop lid: none of them get to write "stopped",
and all of them show up as staleness. A worker that stops on purpose says so,
so the ordinary case is not something anyone has to wait out.

**Fields you do not recognise are not errors.** `version` goes up only for a
change a reader could not survive; ignore what you do not know.

Writes are atomic (write beside, rename over), so a reader never catches half a
document. A status file that cannot be written is complained about once, on
stderr, and never fails a task — an observer falls back on staleness, which is
what it has for the `kill -9` case anyway.

## Settings

**This table is the authoritative list of every setting agent-connect reads.**
Not the module docstrings, not the installer's `--help` — here.
`tests/test_acp_settings.py` fails if a setting exists in the package and not in this
table.

| Setting | What it does | Default |
| --- | --- | --- |
| `AGENT_CONNECT_TOKEN` | your agent identity's relay token, from the Agent Portal | *(required)* |
| `AGENT_CONNECT_ADAPTER` | which adapter runs the task: `codex`, `ollama`, `omnigent`, `cline`, `kilo`, `acp` | *(required)* |
| `AGENT_CONNECT_REPO` | the working directory the agent operates in | cwd (installer: `~/agents`) |
| `AGENT_CONNECT_WORKSPACE` | workspace dir holding `tasks/` + `results/` | `~/.agent-connect/workspace` |
| `AGENT_CONNECT_STATUS_FILE` | the status file this worker owns (see the section above) | `<workspace>/status.json` |
| `AGENT_CONNECT_STATUS_HEARTBEAT` | seconds between refreshes of the status file's `updated_at`, and the staleness window an observer reads out of it | `15.0` |
| `AGENT_CONNECT_POLL` | seconds between scans for new tasks | `1.0` |
| `AGENT_CONNECT_ATTACHMENT_MAX_BYTES` | how much of one attached file is read into a prompt. An attachment over this is reported in the room, never shrunk to fit. `0` means no limit | `10485760` (10 MB) |
| `AGENT_CONNECT_OUTGOING_MAX_BYTES` | how large a file the agent produced may be and still be sent to the room. The relay refuses more than this anyway; refusing it here means a sentence in the room instead of a log line. `0` means no limit | `26214400` (25 MB) |

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
| `AGENT_CONNECT_ACP_COMMAND` | the ACP agent's command line, split as a shell would. **Overrides any preset** | *(unset)* |
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

The relay client (`ag2-sparrow`) reads its own `AGENT_CONNECT_TASK_DIR`,
`AGENT_CONNECT_RESULT_DIR`, `AGENT_CONNECT_STATE_DIR`, `REMOTE_TASK_TOKEN` and
`REMOTE_TASK_URL`; the installer wires those into `launch.sh` for you. The
worker reads those last two as well, and only for the ladder: they say which
relay to ask for a room op and with which token. Without a token the worker
posts nothing and the answer travels as the task result, as it always did.

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

The framework is agent-agnostic; the transport + onboarding + access-tiers are shared.

## Quick start (MVP)

1. In the AG2 Space Agent Portal, **create a Codex agent** → copy your token.
2. On your machine:
   ```bash
   export AGENT_CONNECT_TOKEN=<token from the portal>
   export AGENT_CONNECT_ADAPTER=codex
   export AGENT_CONNECT_REPO=/path/to/the/repo/codex/should/work/in
   ./run-agent.sh
   ```
3. In an allowed room: `!codex summarize this repo` → your Codex replies.

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

Early scaffold (2026-07-05). Worker + Codex adapter first; portal onboarding and
the packaged one-liner follow. See `notes` in the Sutando workspace for the full
plan.
