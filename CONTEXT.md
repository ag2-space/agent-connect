# agent-connect

Runs *your* local coding agent, on *your* machine, with *your* credentials, and makes
it answerable from an AG2 Space room. This context covers the local half: how a
message from a room becomes a run of a local agent, and what that agent is allowed
to do.

## Language

### Roles

**Worker**:
The long-running local process this repository ships. It receives work addressed to
one agent identity and turns it into agent runs.
_Avoid_: daemon, bot, bridge, client

**Local Agent**:
The coding CLI on the user's machine that actually does the thinking — Codex, Claude
Code, Cline. It holds its own tool credentials, which AG2 Space never sees.
_Avoid_: model, LLM, backend, tool

**Adapter**:
The unit that knows how to drive one kind of Local Agent. One adapter per agent
family; the Worker holds exactly one at a time.
_Avoid_: driver, plugin, connector, integration

**Agent Identity**:
The addressable participant an AG2 Space room mentions (`!codex …`). It is issued in
the Agent Portal, and is what a token or MXID authenticates. Distinct from the Local
Agent that fulfils it — one identity, many possible Local Agents behind it.
_Avoid_: agent, bot, user

> **Note on the word "agent".** Unqualified, it is ambiguous here — and ACP makes it
> worse by naming two protocol roles. Always qualify: *Local Agent* (the CLI),
> *Agent Identity* (the room participant), *ACP Agent* / *ACP Client* (protocol
> roles, below).

### ACP

**ACP**:
Agent Client Protocol (Zed Industries) — JSON-RPC 2.0 over stdio, in which a *Client* drives an *Agent*.

**ACP Client**:
The protocol role that opens sessions and sends prompts — played by the Worker.
It is the side that holds policy: it decides what the Local Agent may do.
_Avoid_: host, driver

**ACP Agent**:
The protocol role that receives prompts and does the work — played by the Local
Agent, or by a bridge process standing in for one.
_Avoid_: server, backend

### Conversation

**Session**:
One continuing conversation with a Local Agent, holding its own history and its own
permission mode. Keyed by the pair *(room, Access Tier)* — never by room alone, since
a mode belongs to a Session and a Tier must not inherit another Tier's.
_Avoid_: conversation, thread, context, chat

**Turn**:
One exchange inside a Session: a prompt goes in, the Local Agent works, a stop reason
comes back. Only one Turn at a time may be open on a Session.
_Avoid_: request, round, exchange, run

**Steering**:
Injecting a message into a Turn that is already running, so it changes course instead
of queueing behind. An extension, not every Local Agent offers it.
_Avoid_: interrupt, follow-up, barge-in

**Permission Policy**:
The rule the Worker applies when a Local Agent asks to do something. It is the
Worker's, not the Local Agent's — and it is *cooperative*: it binds only an agent that
asks. Confinement that binds regardless is a [[Sandbox]], and the two are not
substitutes.
_Avoid_: permissions, approval, guardrails

### Work

**Task**:
One unit of work addressed to an Agent Identity, originating from a room message.
Carries who asked, where they asked, and at what tier.
_Avoid_: job, request, prompt, message

**Access Tier**:
How much the sender of a Task is trusted. `owner` is the person who registered the
Agent Identity; everyone else is not.
_Avoid_: role, permission level, scope

**Sandbox**:
The confinement a Task runs under, derived from its Access Tier — never from anything
the sender can write. Confinement means the operating system refuses; an agent that
declines to ask is still stopped. Contrast [[Permission Policy]].
_Avoid_: mode, policy, isolation

### Speaking to the room

**Room Op**:
An action the relay performs in a room *as* the Agent Identity — post, edit, react,
upload. The Worker asks for it over the relay; it never speaks Matrix itself.
_Avoid_: matrix call, send, api call

**Ladder**:
The agreed shape of a reply: a placeholder posted when work starts, edited in place as
it progresses, edited once more into the final answer. The Task's result then carries
only a marker, because the answer is already in the room.
_Avoid_: streaming, progress updates, live reply
