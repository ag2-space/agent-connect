# Dialling a remote ACP Agent over WebSocket

The ACP Adapter has driven one shape of agent since `docs/adr/0001`: a command this
Worker spawns, spoken to over stdio, one process per Turn. This adds a second: an ACP
Agent already running behind a URL, dialled instead of started. Both remain; nothing about
the stdio path changes.

## What is actually new, and what only looks new

**The transport is not the work.** The `agent-client-protocol` SDK already ships a
WebSocket client (`acp.ws.client.create_websocket_stream`) returning the same `Transport`
its stdio helper returns, and `connect_to_agent` has always accepted one in place of a pair
of pipes. Present at 0.12.0 — verified by unpacking the wheel, not by reading metadata — so
the floor already in `pyproject` permits it and needs no bump. Dialling is two lines.

The work is everything the stdio assumption had quietly earned: a Client welded to a child
process, a working directory the Worker believed it owned, and a diagnostic that was a
stderr tail.

## Decisions

**The remote door is WebSocket, not Streamable HTTP.** The remote-transport RFD requires
HTTP/2 for its Streamable HTTP profile, and states in RFC-2119 language that clients MUST
support WebSocket while servers MAY be WebSocket-only. WebSocket therefore excludes no
conforming door and halves the surface. This matches the listener we verified against,
which is WebSocket-only for the same reason.

**A URL and a command are refused together, not resolved.** Every other setting pair here
has a precedence — an explicit command beats a preset, because both name the same kind of
thing. These do not: they name two different agents, with different credentials, a
different filesystem and a different conversation. A Worker that picked one would answer
rooms as an agent nobody chose, with nothing in the room to say which. So it stops and
names both.

**There is no fallback from a dial to a spawn.** A door that is down is a door that is
down. Starting a local agent to cover for it would be the same substitution as above,
performed at exactly the moment nobody is watching, and it would make a broken listener
invisible until somebody noticed the answers had changed character.

**Loopback unless the operator opts out, and that is the safety decision in this ADR.**
`WorkingDirectoryPolicy` answers `session/request_permission` by resolving the requested
paths and comparing them against the Session's working directory — on *this* machine's
filesystem. When the Agent runs elsewhere, those resolutions are about the wrong
filesystem: different symlinks, different mounts, a `..` that climbs somewhere else. The
Policy would still answer, and its answers would be guesses wearing the shape of a
guarantee. `docs/adr/0001` already concedes that this confinement is cooperative rather
than enforced; a remote endpoint moves further in that direction, so it must be asked for
by name (`AGENT_CONNECT_ACP_ALLOW_REMOTE=1`) rather than arrived at by typing a hostname.
ACP stays owner-tier-only either way.

**The working directory is the remote's, but one is still sent.** `session/new` takes a
directory and the protocol offers no way to say "you choose", so a dialled Session is opened
with *something*. On loopback — the default, and the only case not opted into — this
Worker's own directory is a real path on the same filesystem and is the right value.
Across hosts it is not, and nothing this Worker can compute would be, so
`AGENT_CONNECT_ACP_REMOTE_CWD` exists for the operator who has already opted into dialling
elsewhere. What an Agent does with the value is its own business; the listener we verified
against accepts any string, including a path that exists nowhere.

The Session map records the directory that was *sent*, and the comparison that retires a
Session is between that and the directory that would be sent now. Both values are this
Worker's own choice, so the comparison stays meaningful over a socket: a Worker whose own
directory moved does not disturb a dialled Session whose remote directory did not, while
changing that remote directory does retire it — a Session opened under one directory must
not go on serving Turns that believe they are in another. A refused `session/load` remains
the other way a Session ends, and is still announced in the room.

**The Permission Policy is built from the same directory the Session was opened in.** They
differ exactly when a dialled Agent was given one of its own, and judging the Worker's
instead would be wrong in both directions at once: allowing paths under the Worker's
directory that are outside the Session's, and refusing paths under the Session's, which are
the only ones the Agent can legitimately touch.

**The bearer is the Agent's, not the relay's.** It rides the WebSocket handshake as an
`Authorization` header, where a door that authenticates can refuse it before any ACP frame
exists, and it is read per dial rather than captured so a rotation needs no restart. This
is the first credential in this package that is not the relay's, so `test_no_wire.py` gains
its first named exception — by file, with a reason, in the same shape as the socket
allowance that lets the Ollama Adapter talk to a local model server. The fence's real
subject, the relay bearer, is excused nowhere.

**Concurrent dialers are the door's business, not ours.** Refusing a second client would
break the legitimate case of an editor and AG2 Space connected at once. Worth stating
because it is easy to get backwards: the interesting concurrency is not two Workers, it is
*one*. Tasks are processed concurrently across rooms and serialised only within a Session,
so a single Worker with two busy rooms already issues concurrent prompts. Over stdio each
Turn had its own process and that concurrency was isolated by the operating system. Dialled,
it is not.

## Costs, stated plainly

**A cancellation this Worker cannot enforce.** A Turn past its deadline is ended through
`session/cancel`, and only if that is ignored is the last resort used. For a spawned Agent
that resort is real: the child process is this Worker's and ending it ends the work. A
dialled Agent's is not — dropping the socket ends our side of the conversation and nothing
else, and the Agent may still be working. The room is told that, in those words, rather
than the spawned story. There is no remedy here that does not require something from the
far side (cancel-on-disconnect, a lease, a termination endpoint), and inventing one that
only looked like a guarantee is the failure this ADR is trying not to repeat elsewhere.

**A dependency, though a smaller one than it first appears.** The SDK's WebSocket client
lives behind its own `http` extra (`httpx[http2]`, `websockets`). So this is an extra on a
dependency already pinned, exposed as `ag2-agent-connect[websocket]`, and every install
that does not dial pays nothing. It is still a tree where there was none, in a package that
has argued about every dependency it has.

**Remote ACP is an RFD, not stable v1.** Stable ACP standardises stdio only. We are
building against a proposal, on the newest part of a pre-1.0 SDK, behind a range
(`>=0.12,<1.0`) that permits every future minor. The listener we verified against pins the
SDK exactly, having already been broken once by a patch release inside a compatible-looking
range. If this transport starts moving, that pin is the lever, and narrowing the range is
the response — not vendoring the client.

**This is the first agent-specific path in an agent-agnostic package.** The README's claim
is that the framework is agent-agnostic and that the relay client, onboarding and access
tiers are shared. The two benefits below accrue today to exactly one agent: AG2 Assistant,
which is the only ACP Agent we know of hosting a WebSocket listener. That is defensible
because it is our own product and the transport is a published protocol proposal rather
than a private arrangement — any ACP Agent that opens a WebSocket door gets it for free.
But it is a change in what this package is, and pretending otherwise would make the next
such request harder to refuse.

## What justifies it

Two claims, both demonstrated rather than argued, and deliberately not four — a parity
claim and a session-continuity claim were both offered and both withdrawn once tested.

**Boot cost.** Over stdio the Worker spawns a process per room message. For an agent whose
entrypoint builds a full runtime — AG2 Assistant boots a Gateway and its MCP servers,
`npx @playwright/mcp` among them — that is a complete cold start per message, torn down
after one Turn.

**Live UI.** An agent that hosts its listener inside the process serving its own web UI can
stream a Turn into an already-open page. Demonstrated: a turn arrived on an open browser tab
with no reload, because the listener's storage mirrors into the *running* Gateway's event
stream. Over stdio the identical seam points at a Gateway inside a subprocess that exits
with the Turn, so the page stays frozen until somebody reloads it.

Session continuity is **not** among these. It is now equally good on both transports, and
the bug that made it look otherwise was durable-history handling on the agent's side, since
fixed.
