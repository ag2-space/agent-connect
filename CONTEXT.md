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

**Managed Local Agent**:
A [[Local Agent]] whose lifecycle the AG2 Space desktop app's supervisor owns, rather
than one the operator starts themselves. A subtype, not a rival: it is the same Local
Agent behind the same [[Agent Identity]], driven by the same [[Worker]] and
[[Adapter]] — what is managed is starting it, stopping it, restarting it after a
crash, and removing it. It lives only as long as the app does; "always reachable"
remains the install-script-plus-launchd path, and neither half-delivers the other's
promise.
_Avoid_: hosted agent, embedded agent, desktop agent, supervised worker

> **Not the "Local Agent Console".** That tab in the desktop names the bundled core —
> a different thing entirely from a Local Agent here. The two words collided before
> either was written down; *Managed Local Agent* is the one that means a Local Agent.

**Adapter**:
The unit that knows how to drive one kind of Local Agent. One adapter per agent
family; the Worker holds exactly one at a time.
_Avoid_: driver, plugin, connector, integration

**Relay Client**:
What speaks to the broker over HTTP on behalf of one Agent Identity: pulls Tasks,
acknowledges them, returns results, sends heartbeats, and performs [[Room Op]]s. It is
the Worker's own — a library this repository owns and this process runs, not a separate
process. Contrast [[Room Op]], which names the *action*; this names the *speaker*.
_Avoid_: bridge, gateway, sparrow, transport

> **On "transport".** It is on the avoid list as a name for *this* — "the transport"
> meaning the Relay Client — because it names a layer where the thing is a component,
> and it reads as a foreign process, which this stopped being. The word is still the
> right one for a layer that really is one: another protocol's (ACP's stdio transport,
> base64 as a transport encoding), and the seam between this package and the library,
> which the tickets that built it are named after.

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
How much the sender of a Task is trusted — a trust level attested by the broker, not
an identity. Exactly two values cross the wire: `owner` — full trust, held by the
registrant of the Agent Identity and anyone the registrant explicitly trusts; `guest` —
anyone else the owner has allowed to address the agent. A Task whose tier is missing or
unrecognized is treated as `guest`, never `owner`.
_Avoid_: role, permission level, scope

**Sandbox**:
The confinement a Task runs under, derived from its Access Tier — never from anything
the sender can write. Confinement means the operating system refuses; an agent that
declines to ask is still stopped. Contrast [[Permission Policy]].
_Avoid_: mode, policy, isolation

**Concierge**:
The AG2 Space bot that grants and revokes an [[Access Tier]] — `allow`, `trust`,
`untrust`, `remove`. It is a *recipient*, and that is the whole of why it is in this
glossary: those words are only commands in a direct message to it. Sent in a room, or
in a DM to the agent, they reach the Local Agent as an ordinary prompt and are answered
as a question. `@sutando-concierge` on the deployment's homeserver; the Worker never
speaks to it, but the ACP refusal has to name it, so the name is product vocabulary
rather than an implementation detail.
_Avoid_: the bot, admin, allowlist bot

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
