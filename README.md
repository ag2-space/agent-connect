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

```bash
export AGENT_CONNECT_ADAPTER=acp
export AGENT_CONNECT_ACP_COMMAND="npx @agentclientprotocol/claude-agent-acp"
export AGENT_CONNECT_REPO=/path/to/the/repo   # the session's working directory
```

Refused permission requests are reported back in the reply, so a blocked agent is
distinguishable from a lazy one.

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
