# What an agent delivers after its Turn posts at most once per part, and is retry-safe

Status: proposed. Depends on the broker, relay-client and agent-connect #40 changes under
"Dependencies". This PR is the decision only; none of it is implemented yet.

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

The guarantee is: each part of a delivery is posted **at most once** while the broker
remembers its key (24 hours), and a lost response can be retried without a second post. It
is not "exactly once": a delivery can end without posting (`unknown`, `expired`).

## Decision

**1. Both sides declare it, and only a listener carries it.** The Worker declares
`_meta["ag2/deliver"] = true` on its `initialize` request, beside `ag2/receive`. The agent
declares the same in its `initialize` response. An agent sends `_ag2/deliver` only to a
client that declared it. This is required, not courtesy: today's Workers subclass
`acp.Client` without `ext_method` (`agent_connect/acp/core.py` `_ClientCallbacks`), so an
unknown request comes back as a JSON-RPC **success** with `result: null`, which an agent
would read as delivered. The same hole ADR 0005 closed for `_ag2/notify` with `ag2/receive`.

`_ag2/deliver` is accepted only on a standing listener, never on a Turn's connection, which
closes or is killed at Turn end. For a dialled agent the listener is the existing Listener
Link. For a spawned agent that declared `ag2/deliver`, the Worker spawns one more instance
of the same command for the life of the Worker. It initializes with `ag2/receive` and
`ag2/deliver`, opens no Session, sends no prompt, and is restarted with backoff when it
exits. Turns keep spawning their own process (ADR 0004). An agent that does not declare
`ag2/deliver` gets no extra process. A delivery the Worker has accepted runs to completion
even if the listener's connection closes meanwhile; the agent learns the result on its
retry, from the ledger.

The queue that survives a listener restart is the agent's: it holds a delivery until the
Worker acknowledges it (step 3), not until it is written to a socket.

**2. Deliveries are bound to a room by the Worker, not by the agent.** `session/new` already
sends `_meta["ag2/destination"]`. It gains:

- `binding`: an opaque id the Worker creates per Session key (room and owner, or room and
  collaborator; ADR 0003's amendment, #40). It is `<worker id>.<random>`, so a client that
  did not issue it can tell at once. Two collaborators in one room never share a binding or
  an `export_dir`. Renewal keeps the same id and `export_dir`.
- `export_dir`: `<workspace>/export/<binding>/`. The agent writes files it will deliver there.

Lifetimes:

- An owner binding expires 30 days after the room's last owner Turn; each owner Turn renews
  it.
- A collaborator binding records the sender and expires **24 hours** after that sender's last
  attested Turn; each of their Turns renews it. The Worker cannot learn outside a Turn that a
  room stopped granting Team or that the sender left, so 24 hours is the honest upper bound
  on that window. A Turn in which the broker no longer attests the sender as Team
  collaborator drops their bindings at once.
- `AGENT_CONNECT_ACP_COLLABORATORS` is read when the Worker starts. Removing a sender takes
  effect on restart; at delivery the Worker re-checks the list it holds and answers
  `rejected` for a sender no longer on it.
- Revocation and expiry stop **new** deliveries. A delivery already accepted completes; the
  binding's folder is deleted after it finishes.

The binding store is `<workspace>/bindings.json`, written under the ledger's rules below
(fsync, fail closed, single writer). A lost store makes every binding unknown, which is
answered `not_mine` (step 3), so the agent can go back to its other client or a new Turn
rather than drop the delivery.

**Files.** The Worker creates `<workspace>/export/` (mode 0700) at start, before the egress
client is built, and each binding's folder (0700) when the binding is created. The folder
stays until the binding expires; delivered files are kept there until then, because a retry
may need them. Deletion does not follow symlinks the agent placed there. The ACP Permission
Policy allows writes to the Session's own `export_dir` and no other binding's.

`<workspace>/export/` is **not** an egress root for Turns: a `[file: …]` marker or
`resource_link` in a Turn cannot name a path under it. A delivery's attachments are judged
by the relay client against an allowlist rooted at that binding's `export_dir` alone, on the
opened file descriptor, not by the Worker comparing path strings first (`outgoing.py`: a
consumer must not pre-approve a path). So the Worker refuses cross-binding paths. It cannot
stop the agent, which writes every folder as the same user, from copying a file from one
folder into another; that is the agent's discipline, as it is for any file it can read.

A binding names a destination; it is not a credential. Which client inside the agent may
deliver is the agent's concern.

**3. Delivery is a request, and its response is the acknowledgment.**

```json
{"binding": "…", "id": "<stable per delivery>",
 "text": "optional caption or message",
 "attachments": [{"path": "<export_dir>/x.png", "name": "x.png", "mimeType": "image/png"}]}
```

Parts are numbered in order: each attachment is a part, in list order, starting at 0, and the
text, if any, is the last part. Each part is its own room event. Files go first so that a
caption never arrives promising a file that did not; if a later part ends in a Final outcome
after earlier parts posted, the Worker posts a one-line notice in the room saying a part of
the delivery could not be sent.

Each part is posted with
`Idempotency-Key = sha256(canonical JSON of [binding, id, part])` (UTF-8, no whitespace), so
`id="x1", part=0` and `id="x", part=10` cannot collide. The broker scopes keys per agent
identity. A resend under the same `id` must carry identical parts in the same order. Parts
that already landed replay, and only the missing ones post. Parts are posted one after
another; there is no ordering across deliveries.

The Worker answers with one of three results. Each carries `expires_at`, the time after
which a resend is `expired`, so the agent can stop early:

- **Delivered:** `{"delivered": [{"part": 0, "event_id": "…"}, …]}`. Sent only after the
  relay accepted every part. The agent treats it as delivered only if `delivered` lists every
  part it sent.
- **Retry:** a JSON-RPC error with `data.outcome = "retry"`. Sent for:
  - a 409 with `state: running`, or a 503;
  - a definite failure the broker released the key for (a failed upload, a transient 4xx on
    the room post); a file already uploaded is posted on the retry without uploading again;
  - the relay client refusing without sending (its cooldown);
  - a ledger or binding store the Worker cannot read or write.

  No response at all counts as Retry too. The agent sends it again with the **same `id`**.
- **Final:** a JSON-RPC error with `data = {"outcome": …, "part": n}`. The agent stops
  resending this `id`, and the Worker logs it. `outcome` is one of:
  - `not_mine`: the binding is unknown to this Worker or names another Worker. The agent
    should send it to the client that issued it, or wait for a new Turn's binding; it is not
    dropped as a failure.
  - `unknown`: 409 `state: unknown`. The relay cannot say whether the part posted, and keeps
    the key closed rather than risk a second post. The rare cost is a missing file, never a
    duplicate.
  - `conflict`: 409 `state: conflict`. A resend changed a part's content under the same
    `id`. This is an agent bug.
  - `rejected`: a definite failure that a retry cannot cure: 413 (over the relay's 25 MiB
    cap), a room the Worker may not post to, a sender no longer admitted, or a path outside
    the binding's `export_dir`.
  - `expired`: the delivery is older than 23 hours. The relay keeps a key for 24 hours, so a
    retry after that could post twice.

**The ledger.** `<workspace>/deliveries.json` records, per `id`, when the Worker first saw it
and each part's result. It is what enforces `expired` after the key's lifetime, so it fails
closed:

- The first-seen entry is written and fsynced (temp file, fsync, rename, fsync of the
  directory) **before** the first relay call for that `id`. A crash after posting cannot
  restart the 23-hour clock.
- If the ledger cannot be read or written, deliveries are answered Retry; an unreadable
  ledger is never treated as empty.
- One Worker process owns a workspace's ledger, enforced by a lock file. A second Worker on
  the same workspace refuses to start the listener.
- Entries are kept 30 days after first seen, or until their binding expires if sooner. Each
  write drops expired entries. Past 10,000 entries the oldest finished ones go first; an
  entry still in progress is never dropped, and if those alone exceed the cap, new
  deliveries are answered Retry.
- If the file is deleted, the Worker loses first-seen times: a resend of an old `id` more than
  24 hours after its first attempt could post twice. A correct agent stops at `expired`
  before then, and the lock and fsync make accidental loss unlikely, but this is the limit.

The broker's idempotency key is what guarantees one post. The ledger only answers known
deliveries without calling the relay, and enforces `expired`. When two listeners compete for
the same delivery, the agent decides which one owns it; the same key makes a second attempt
harmless anyway.

**4. `_ag2/notify` is unchanged.** AG2 Assistant (its ADR 0049) relies on it. It stays
text-only and best-effort. New agents should use `_ag2/deliver`.

**5. Standard ACP files in a Turn are sent, for local agents.** An `agent_message_chunk`
whose content is a `resource_link` with a `file://` URI is treated as an outgoing file of
that Turn, under the same egress allowlist as a `[file: …]` marker. Only for a spawned agent
or one dialled on loopback: a remote agent's `file://` names a path on another machine
(ADR 0004). Inline `image`/`resource` bytes are not accepted: `RoomOps.upload` takes only
paths, by design.

## Dependencies

- **agent-connect #40:** ADR 0003's amendment, `AGENT_CONNECT_ACP_COLLABORATORS`, and Sessions
  per (room, sender). With it, ADR 0005's "posting is unconditional on Tier: the agent is
  only ever reached at owner tier" no longer holds; this ADR's per-sender bindings and
  re-check replace that assumption for `_ag2/deliver`, and ADR 0005 should be amended to
  point here.
- **Broker:** an optional `Idempotency-Key` header (24-hour lifetime, scoped per agent
  identity) on `POST /v1/rooms/{room}/media` and a new `POST /v1/rooms/{room}/messages` with
  the same semantics. These follow the core-api Actions pattern:
  - the key is reserved before the upload;
  - a completed key replays the stored `{event_id, mxc}`;
  - a different payload under the same key is refused;
  - a key still in progress, or whose outcome is unknown, is refused rather than posted twice;
  - the key is released only on a definite failure;
  - with a key given and the store unavailable, the request fails closed;
  - a 409 says which case applies in `state`: `running`, `unknown` or `conflict`;
  - **every keyed response echoes that the key was honoured** (an `Idempotency-Status`
    header: `created` or `replayed`).

  A request without the header, and the existing `/v1/room` route, behave as today. Today's
  broker ignores the header, so a retry would post twice with two 200s. The Worker therefore
  does not declare `ag2/deliver` unless the broker it talks to echoes the key: it checks at
  start, and a keyed response without the echo stops the delivery path (answered Retry) and
  clears the declaration on the next listener restart. Proposal: "Broker: retry-safe room
  posts (Idempotency-Key)".
- **Relay client:** `upload` and `message` take an optional idempotency key, and `message`
  moves from `/v1/room` `op=message` to `POST /v1/rooms/{room}/messages`. Today `RoomOps._call`
  maps every failure to `None` and any failure switches room ops off for 300 seconds in every
  room (`roomops.py` `COOLDOWN_S`, `_trip`), and `UploadResult` has no `event_id`. So it also
  needs:
  - the HTTP status, the 409 `state`, the echo and `event_id` returned to the caller
    (`UploadResult` gains `event_id`);
  - 409 and 413 kept out of the cooldown, so a `running` retry does not stop placeholders in
    other rooms;
  - a refusal during the cooldown (nothing sent) told apart from a failure, so the Worker can
    answer Retry;
  - uploads judged against an allowlist rooted at one binding's `export_dir`.

  Released as relay client 0.2.0, then agent-connect 0.3.0 with that minimum.
- **Agents** that want it: declare `ag2/deliver`, send it only to a client that declared it,
  write deliverable files to `export_dir`, keep a durable queue, and treat only a result
  covering every part as delivered.

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
- **Add `export/` to the Turn egress roots.** Rejected: any Turn, including a collaborator's,
  could then attach another binding's files.

## Consequences

- One extra idle process per Worker for a spawned agent that declares `ag2/deliver`, and
  none otherwise.
- Deliveries are not scanned by the room's sensitive data filter unless agent-connect's
  collaborator result scan (#40) is applied to them too; until it is, collaborator bindings
  are not issued while the room's filter is on.
- A `resource_link` in a Turn is no wider than a `[file: …]` marker. For an admitted
  collaborator, a prompt can ask for any file inside the egress allowlist roots, so choose
  those roots with collaborators in mind.
- `export_dir` is a local path, so `_ag2/deliver` attachments work only when the agent and the
  Worker share a filesystem. A dialled remote agent can still deliver text.
- A delivery is complete only when the relay accepted every part. Transient failures are
  Retry; a room the Worker may not post to is a Final `rejected`, logged by the Worker and
  dropped by the agent. Unlike `_ag2/notify`, the agent always learns which.
- Planned tests, against a fake agent and a fake broker (none exist yet; this PR is docs):
  - a Worker that does not declare `ag2/deliver` is never sent one;
  - deliver with no follow-up: one file, then one caption;
  - lost response, then retry with the same `id`: one post per part;
  - replay after a listener restart: one post;
  - listener connection closed mid-delivery: the delivery completes, and the retry gets
    Delivered;
  - unknown or foreign binding: `not_mine`;
  - a collaborator removed from the list (after restart), then delivering: `rejected`;
  - a collaborator binding past 24 hours since their last Turn: `not_mine`;
  - two collaborators in one room: separate bindings and folders;
  - attachment from another binding, or outside `export_dir`: `rejected`, nothing uploaded;
  - a Turn's `[file: …]` naming a path under `export/`: refused;
  - upload accepted but acknowledgment lost: one post;
  - resend with a changed part under the same `id`: `conflict`, no post;
  - partial delivery, then resend: only the missing parts post;
  - a later part Final after earlier parts posted: one notice in the room;
  - resend past 23 hours, including across a Worker restart: `expired`, no post;
  - ledger unreadable or unwritable: Retry, no post;
  - a second Worker on the same workspace: refuses the listener;
  - broker without the key echo: `ag2/deliver` not declared, delivery answered Retry;
  - relay 409 `running`: Retry, and room ops in other rooms keep working;
  - keys for `id="x1", part=0` and `id="x", part=10` differ;
  - simultaneous deliveries to two rooms: each lands in its own room only;
  - binding expiry during an in-flight delivery: the delivery completes, then the folder goes,
    without following a symlink inside it;
  - renewal: same binding id and `export_dir`.
