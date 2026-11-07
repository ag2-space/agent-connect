# What an agent delivers after its Turn reaches the room exactly once

Status: proposed. Depends on the broker change under "Dependencies".

ADR 0005 lets a dialled agent send text between Turns with `_ag2/notify`. It does not cover
four cases:

- **Spawned agents.** A spawned agent gets no Listener Link, so a stdio agent that finishes
  work after its Turn ends has no client to tell.
- **Files.** `_ag2/notify` carries text only. A file leaves the machine only through a Turn's
  result, and a `[file: …]` marker in a notification reaches the room as literal text.
- **Retries.** `_ag2/notify` is fire-and-forget. A post that fails is lost, and one retried
  after a lost response is posted twice. The relay cannot tell a retry from a new post.
- **Standard ACP files in a Turn.** A file returned as an ACP `resource_link` block is
  dropped, because `_extract_text` reads `text` content only.

Long-running tools (image generation, builds, scheduled tasks) all hit these cases. This ADR
is agent-neutral: everything below is an AG2 extension or plain ACP, and an agent that
implements none of it loses nothing.

## Decision

**1. An agent that can deliver later gets a listener, however it is reached.** An agent says
so in its `initialize` response: `_meta["ag2/deliver"] = true`. For a dialled agent this is the
existing Listener Link. For a spawned agent the Worker spawns one more instance of the same
command for the life of the Worker. That instance initializes with `ag2/receive`, opens no
Session, sends no prompt, and is restarted with backoff when it exits. Turns keep spawning
their own process (ADR 0004). An agent that does not advertise `ag2/deliver` gets no extra
process. The queue that survives a listener restart is the agent's: it holds a delivery until
the Worker acknowledges it (step 3), not until it is written to a socket.

**2. Deliveries are bound to a room by the Worker, not by the agent.** `session/new` already
sends `_meta["ag2/destination"]`. It gains:

- `binding`: an opaque random id the Worker creates per (room, Access Tier) and keeps in its
  own store, independent of Session rotation. It expires 30 days after the room's last Turn,
  and each Turn renews it, keeping the same id and `export_dir`.
- `export_dir`: `<workspace>/export/<binding>/`, a directory the Worker creates and deletes
  when the binding expires. The agent writes files it will deliver there. The Worker adds
  `<workspace>/export/` to the egress allowlist itself.

The agent stores both on its own record of the work. A delivery names its `binding`, and the
Worker looks the room up from its own store. An unknown or expired binding is refused, and a
room named by the agent is never used. Attachments are accepted only from that binding's
`export_dir`, and are uploaded through the egress allowlist (`agent_connect.outgoing`,
`RoomOps.upload`) like every other file. A path from another binding is refused even when it
is inside the allowlist, so the Worker enforces that one room cannot send another room's file.
A binding does not expire, and its folder is not deleted, while a delivery for it is in flight.

A binding names a destination; it is not a credential. `_ag2/deliver` is accepted only on the
Worker's own connection to the agent (a process it spawned, or a link it dialled). Which
client inside the agent may deliver is the agent's concern.

**3. Delivery is a request, and its response is the acknowledgment.** `_ag2/deliver` is a
JSON-RPC request, not a notification:

```json
{"binding": "…", "id": "<stable per delivery>",
 "text": "optional caption or message",
 "attachments": [{"path": "<export_dir>/x.png", "name": "x.png", "mimeType": "image/png"}]}
```

Each part is posted with `Idempotency-Key = sha256(binding, id, part)`, so a retry after a
lost response gets back the original event instead of posting again. A resend under the same
`id` must carry identical parts in the same order. Parts that already landed replay, and only
the missing ones post. The Worker answers with one of three results. Each carries
`expires_at`, the time after which a resend is `expired`, so the agent can stop early:

- **Delivered:** `{"delivered": [{"part": 0, "event_id": "…"}]}`. Sent only after the relay
  accepted every part.
- **Retry:** a JSON-RPC error with `data.outcome = "retry"`. Sent when the relay answered 409
  with `state: running`, or 503. Also sent when the relay released the key after a definite
  failure that a retry may cure, such as a failed upload or a transient 4xx on the room post;
  a file that is already uploaded is posted on the retry without uploading again. No response
  at all counts as Retry too. The agent sends it again with the **same `id`**.
- **Final:** a JSON-RPC error with `data = {"outcome": …, "part": n}`. The agent drops the
  delivery and does not resend it, and the Worker logs it. `outcome` is one of:
  - `unknown`: 409 `state: unknown`. The relay cannot say whether the part posted, and keeps
    the key closed rather than risk a second post. The rare cost is a missing file, never a
    duplicate.
  - `conflict`: 409 `state: conflict`. A resend changed a part's content under the same
    `id`. This is an agent bug.
  - `rejected`: a definite failure that a retry cannot cure, such as 413 (over the relay's
    25 MiB cap) or a room the Worker may not post to.
  - `expired`: the delivery is older than 23 hours. The relay keeps a key for 24 hours, so a
    retry after that could post twice. The Worker's ledger records when it first saw each
    `id`, refuses resends past the limit, and survives restarts.

The ledger is `<workspace>/deliveries.json`, written like the Session map. An entry is kept
for 30 days after first seen, or until its binding expires if that is sooner. An agent that
resends an `id` more than 30 days later, having never received a result, can post twice. A
correct agent stops at `expired`.

The broker's idempotency key is what guarantees one post. The Worker's ledger only answers
known deliveries without calling the relay, and enforces `expired`.
When two listeners compete for the same delivery, the agent decides which one owns it. The
same key makes a second attempt harmless anyway.

**4. `_ag2/notify` is unchanged.** AG2 Assistant (its ADR 0049) relies on it. It stays
text-only and best-effort. New agents should use `_ag2/deliver`.

**5. Standard ACP files in a Turn are sent.** An `agent_message_chunk` whose content is a
`resource_link` with a `file://` URI is treated as an outgoing file of that Turn, under the
same egress allowlist as a `[file: …]` marker. Inline `image`/`resource` bytes are not
accepted: `RoomOps.upload` takes only paths, by design.

## Dependencies

- **Broker:** an optional `Idempotency-Key` header (24-hour lifetime) on
  `POST /v1/rooms/{room}/media` and a new
  `POST /v1/rooms/{room}/messages` with the same semantics. These follow the core-api Actions
  pattern:
  - the key is reserved before the upload;
  - a completed key replays the stored `{event_id, mxc}`;
  - a different payload under the same key is refused;
  - a key still in progress, or whose outcome is unknown, is refused rather than posted twice;
  - the key is released only on a definite failure;
  - with a key given and the store unavailable, the request fails closed;
  - a 409 says which case applies in `state`: `running`, `unknown` or `conflict`.

  A request without the header, and the existing `/v1/room` route, behave as today.
  Every relay client benefits. Until this lands, `_ag2/deliver` is not advertised. Proposal:
  "Broker: retry-safe room posts (Idempotency-Key)".
- **Relay client:** `upload` and `message` take an optional idempotency key. `message` moves
  from `/v1/room` `op=message` to `POST /v1/rooms/{room}/messages`. Released as relay client
  0.2.0, then agent-connect 0.3.0 with that minimum.
- **Agents** that want it: advertise `ag2/deliver`, write deliverable files to `export_dir`,
  keep a durable queue, and treat only a success response as delivered.

## Considered options

- **Call core-api Actions, which already de-duplicate.** Rejected: core-api refuses relay
  bearers by design, so every install would need a second, MCP-grade credential. The relay
  token stays the agent's only credential.
- **On an unclear outcome, read the room's recent messages and match before retrying.**
  Rejected: it is heuristic, and two identical deliveries are indistinguishable from one.
- **Keep each Turn's process alive between Turns.** Rejected: it reverses ADR 0004's per-Turn
  lifetime for every agent, to serve the ones that deliver later.
- **Extend `_ag2/notify` with attachments.** Rejected: a notification has no response, so it
  cannot carry the acknowledgment the agent's queue needs.
- **Accept any allowlisted path in a delivery.** Rejected: the allowlist answers "may this
  leave the machine", not "does this belong to this room".

## Consequences

- One extra idle process per Worker for a spawned agent that advertises `ag2/deliver`, and
  none otherwise.
- `export_dir` is a local path, so `_ag2/deliver` attachments work only when the agent and the
  Worker share a filesystem. A dialled remote agent can still deliver text.
- A delivery is complete only when the relay accepted it. A room the Worker cannot post to
  answers with an error, and the agent retries; it is not logged and dropped as with
  `_ag2/notify`.
- Tests against a fake agent:
  - deliver with no follow-up: one caption and one file;
  - lost response, then retry with the same `id`: one post;
  - replay after a listener restart: one post;
  - unknown binding: refused;
  - attachment from another binding: refused;
  - path outside `export_dir`: refused;
  - upload accepted but acknowledgment lost: one post;
  - resend with a changed part under the same `id`: `conflict`, no post;
  - partial delivery, then resend: only the missing parts post;
  - resend past 23 hours, including across a Worker restart: `expired`, no post;
  - simultaneous deliveries to two rooms: each lands in its own room only;
  - binding expiry during an in-flight delivery: the delivery completes, then the folder goes;
  - renewal: same binding id and `export_dir`.
