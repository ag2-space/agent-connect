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

## Settings

**This table is the authoritative list of every setting agent-connect reads.**
Not the module docstrings, not the installer's `--help` — here.
`test_acp_settings.py` fails if a setting exists in the package and not in this
table.

| Setting | What it does | Default |
| --- | --- | --- |
| `AGENT_CONNECT_TOKEN` | your agent identity's relay token, from the Agent Portal | *(required)* |
| `AGENT_CONNECT_ADAPTER` | which adapter runs the task: `codex`, `ollama`, `omnigent`, `cline`, `kilo`, `acp` | *(required)* |
| `AGENT_CONNECT_REPO` | the working directory the agent operates in | cwd (installer: `~/agents`) |
| `AGENT_CONNECT_WORKSPACE` | workspace dir holding `tasks/` + `results/` | `~/.agent-connect/workspace` |
| `AGENT_CONNECT_POLL` | seconds between scans for new tasks | `1.0` |

ACP adapter (see the section above):

| Setting | What it does | Default |
| --- | --- | --- |
| `AGENT_CONNECT_ACP_AGENT` | preset naming the ACP agent to run: `claude`, `gemini` | *(unset)* |
| `AGENT_CONNECT_ACP_COMMAND` | the ACP agent's command line, split as a shell would. **Overrides any preset** | *(unset)* |
| `AGENT_CONNECT_ACP_MODE` | session mode id to request. Left alone by default: every bridge's own default already routes permission requests to the worker, and mode ids are the agent's to name | *(unset)* |
| `AGENT_CONNECT_ACP_SKIP_AUTH_CHECK` | `1` to start even when the agent advertises authentication methods. The escape hatch for an agent whose `authMethods` mean something else | *(unset)* |
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
`REMOTE_TASK_URL`; the installer wires those into `launch.sh` for you.

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

## Status

Early scaffold (2026-07-05). Worker + Codex adapter first; portal onboarding and
the packaged one-liner follow. See `notes` in the Sutando workspace for the full
plan.
