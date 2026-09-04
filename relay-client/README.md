# ag2-relay-client

The AG2 Space relay wire, as a library: one bearer's whole conversation with the
broker — poll, lease, ack, results, heartbeat, media in both directions, Room
Ops — behind one seam. A consumer takes Tasks out of an in-memory queue and
hands answers back through `complete` / `reject`; everything that touches the
wire, including resolving media markers to local files, stays on this side.

It exists because that conversation had been implemented twice, in two
processes, from two repos — and the operational lessons (a User-Agent the edge
accepts, a DNS call that cannot hang forever, a token parse that survives a
URL-encoded separator) had to be learned twice too. They live here once now.

## Promises

- **stdlib-only.** No dependency arrives with a transport, ever.
- **Python >= 3.9**, sync and threaded, **no asyncio**. An asyncio consumer
  wraps the calls in an executor; a sync one calls them directly.
- **No compiled-in gateway.** The base URL is discovered at provisioning time —
  it travels inside the onboarding token or alongside it.

## Status

Under construction. What ships today is the whole task path — poll, lease,
journal, ack, results, heartbeat, auth recovery, status — media in both
directions, with inbound markers resolved to local files before delivery — the
whole outbound half — Room Ops, the egress allowlist and the result-marker
grammar — the singleton-per-bearer guard, and the optional events channel. All
of it behind the seam below; nothing from the ticket list is outstanding here.

## The seam

```python
from ag2_relay_client import RelayClient, TokenSource

client = RelayClient(TokenSource(token_file="~/.ag2/relay.env"),
                     state_dir="~/.ag2/state", instance="prod",
                     egress_roots=[workspace])   # the only files it may send
client.start()

task = client.next_task(timeout=30)      # or read client.tasks yourself
if task:
    client.complete(task.id, my_agent.handle(task.body))
```

Two things wide, and that is the whole consumer surface: Tasks come out of an
in-memory queue, answers go back through `complete` — or `reject(id, reason)`,
which dead-letters a permanently malformed task instead of letting it re-serve
five times.

`complete` is not a POST with extra steps. It reads the result-marker grammar
(below), uploads any file the body names **from an allowlisted path**, appends a
sentence about any it refused, re-stitches a `[channel:]` redirect for the
broker's deliverer, and only then journals and POSTs. So a consumer hands over
the text its agent wrote and never touches a marker, a path this library has not
judged, or the wire. `base_dir=` is what a relative path in a marker is read
against; it widens nothing.

### What a Task carries

`id`, `body` and `room_id` are what a simple consumer needs. Around them the
envelope carries what the broker attests about the sender — `user_id`,
`access_tier`, `requested_access_tier`, `collaborator`,
`sensitive_data_filter` — and what it enriches the message with: `source`,
`session_scope`, `interaction_type`, `priority`, `timestamp`, `room_name`,
`sender_name`, `room_members`, `room_member_count`, `reply_to_event`,
`reply_to_me`, `reply_to_sender`, `addressed_to`, `source_message_id`,
`thread_root`, `source_room_id`, `platform_card`, `attempt`. Plus
`attachments`, which the media stage fills.

All of it crosses **as data**. This library maps none of it to a decision: the
tier is an attestation and not a permission, the interaction type has no
whitelist here, and an unknown field on the wire is ignored rather than fatal —
the envelope is additive-only and carries no version. `""` means the broker did
not send it, so absence survives the trip; `room_member_count` and
`platform_card` say the same with `None`, because `0` and `{}` are real values.
A platform card arrives whole or not at all: all five of `card_url`,
`card_sha256`, `sig`, `key_id`, `alg`, never partially, and never verified here.
`room_member_count` is accepted as the plain decimal string the broker writes
it as, and as nothing looser. `thread_root` and `source_room_id` are ingress
only: the broker inherits a result's route from the task id, and a consumer
that echoed them back could name a thread it was not asked in.

The queue is a **handoff, not durability**. What survives a restart is the
journal under the state dir, and the promise attached to it is worth stating
plainly:

- A task the client has already answered is **re-completed upstream, never
  handed to the consumer twice** — a reconnect replays the broker's unacked
  pool, and the replays that prompted this were 500 tasks each.
- An answer is **retained until the broker takes it**. A failed POST, a killed
  process: the answer is on disk and goes out on a later pass, under the same
  id, which the broker dedups.
- A blank answer is refused. "Nothing to say" is `[no-send]`, which completes
  the lease and posts nothing; a blank body means "not ready", and delivering it
  would archive the task and strand the real answer.

Everything the loop knows about itself is readable while it runs:
`client.snapshot()` for a thread-safe copy, `client.on_status(hook)` for a
callback on every change, and a connection-only `connection-status.json` under
the state dir so a supervisor can read it whether or not the consumer bothered
to. A hook that raises is the hook's problem: nothing beside the wire is allowed
to block delivery.

## What the loop guarantees, and why it is shaped like this

Polling cadence is a correctness property. The broker extends a lease only while
the worker keeps polling; a worker that stops has its in-flight tasks re-served.
So nothing in the loop blocks unboundedly — the long poll is `wait=25` with a
socket timeout of `wait+10`, every side call is bounded, the Task queue is
unbounded so a slow consumer cannot stall the poll thread, and status writes,
hooks and logs all fail silently by contract. Neither of the two unbounded
*lists* the loop meets can stretch a turn either: the owed-result queue and a
poll answer (the broker drains its whole queue into one) each get a wall-clock
slice of the turn, and what does not fit waits for the next one. Health is asked
of `client.healthz()`; `GET /v1/tasks` is never a probe, because it leases tasks.

The ordering inside one accepted task is journal, then delivery, then ack. The
ack does not gate the handoff — but it is **not** informational, whatever
`WORKER-PROTOCOL.md` says. Against this broker, liveness alone extends an
un-acked lease twice and then requeues it with `attempt` bumped, and a re-serve
starts un-acked again, so the ack is what buys a long Turn its lease. A re-served
id the consumer still holds is therefore re-acked — never re-delivered — and a
pause on acking is written into the status file as the delivery outage it is.
Its 404 is content-sniffed, because `not leased to you` is one task's expired
lease and a bare 404 is a broker without the route, and treating the first as the
second once blinded a whole host's delivery state.

A result that the broker will not take costs its own id a backoff and costs the
answers behind it nothing: a `break` there put the poisoned answer permanently at
the head of an oldest-first queue, with everything behind it durable, owed and
never sent.

A rejected bearer is a wait, not a death: the durable token source is re-read at
once (a rotation may already have happened), and otherwise the loop holds at a
slow cadence, saying so every pass, until one lands — then resumes live, no
restart. A token file that starts naming a *different* gateway is a
reconfiguration and is refused.

## Receiving a file

The broker sends **no attachments field**. Media rides inside the task text as a
marker — `[ag2space-media: <url> mime= name= size= kind=]` — and a client that
waits for a header waits for ever. The library resolves it before the Task is
delivered, so a consumer reads paths:

```python
client = RelayClient(creds, state_dir="~/.ag2/state",
                     media_dir="~/.ag2/media")       # optional; state dir by default

task = client.next_task(timeout=30)
for got in task.attachments:                          # 0..N, already resolved
    if got.ok:
        my_agent.read(got.path, got.mime)             # a local file, on disk
    else:
        say(f"I couldn't read {got.name or 'the attachment'}: {got.reason}")
```

**The consumer never sees a marker or a URL** — not on a success, not on a
failure, not when the URL was nonsense. What crosses the seam is a path or a
reason.

- **The mime comes from the fetch's `Content-Type`.** The marker's is a hint,
  and so are `name` and `kind`: nothing in the marker is escaped, so a filename
  with a space truncates `name=` at the space and one with a `]` truncates the
  whole marker. `name` is never used to name the file on disk.
- **A failed fetch never blocks the Task.** One budgeted retry, then the Task is
  delivered with that attachment marked failed and the reason carried. It is not
  held — the gateway's media route answers `502` for *every* cause, membership
  refusals included, so waiting for a good answer can wait for ever — and it is
  not auto-rejected, because an agent that can say "I couldn't read that" is
  more use than a dead-lettered task.
- **The fetch runs off the poll thread.** Cadence is correctness, and a 25 MiB
  download on the poll thread is a stalled loop. A task with nothing to fetch is
  never queued behind one that has; a task with media is delivered when its
  bytes are.
- **The bearer goes only to the gateway's own URLs**: exact parsed origin, at or
  under the base path with a real `/` boundary — `relay.example.evil` and
  `/relay-evil/` are both a no. No redirect is followed, so a gateway-written URL
  cannot bounce this bearer to a third-party host; `cap + 1` bytes are read
  against a 25 MiB ceiling, so a lying `Content-Length` cannot OOM anything; and
  a URL that will not parse is a failed attachment, never an exception out of
  task intake.

Fetched files are **deleted when the task is answered**, and anything an earlier
run left behind is swept when the next one starts — those Tasks were sitting in
an in-memory queue, so no live task can claim their bytes. A consumer whose own
archives reference the paths opts out at construction with
`media_retention_s=<seconds>`, which keeps the files and sweeps only what has
aged out.

**The media directory is not auto-allowlisted for egress.** A consumer that
wants to send back what arrived adds it as an explicit root, so all egress
policy stays in one place a reviewer can read: the `egress_roots` list on the
`RelayClient` constructor, and nothing else. There is no environment variable
below this line, no default root, and no way to add one after construction.

## One poller per bearer

A bearer's queue tolerates exactly one concurrent poller. The broker does not
detect a second one and does not reject it — two clients simply split the lease
stream and **deliver every task twice**. The guarantee therefore cannot come
from the wire, and it is not left to the consumer either: every client arbitrates
for the bearer through a lock file in its own state dir, and nobody has to
remember to switch it on.

```python
client.snapshot()["singleton"]   # "held" | "lost" | "degraded" | "off"
```

Four properties, and each one is an incident:

- **Atomic acquire.** Two clients starting in the same millisecond produce one
  winner, because the whole decision — read, judge, write — happens under an
  exclusive lock on the guard file.
- **Liveness is heartbeat freshness, never pid-alive.** The holder re-stamps the
  record wherever its poll loop demonstrably got to — at every phase boundary of
  a turn, and through the wait between turns — so the freshness window bounds the
  longest single call the loop can be *inside*, not the sum of the calls it
  makes. A holder that stops turning loses the guard after 150 s, however alive
  its pid may be; a holder that is merely slow never does. The ghost that prompted this
  was alive-but-stale for *days*, and pid recycling makes "is that pid running?"
  a question about somebody else's process. There is no `os.kill` in the guard,
  and a test asserts there never will be.
- **A definitive loser stops immediately.** A client that never held the guard
  stands by and keeps asking — so a holder that dies without releasing is taken
  over from with nobody in the loop — while a client whose claim is *taken* stops
  polling on that same turn and stays stopped, saying `displaced` in its status.
  Coming back is the consumer's decision, never the loop's: `start()` re-enters
  the arbitration as a standby, so a client that was displaced comes back behind
  whoever took the bearer rather than alongside them.
- **It fails open.** An I/O error, a filesystem without locking, a bug in the
  guard: all of them mean *poll anyway*, loudly logged and visible as
  `"degraded"` in the status. A lock that fails closed silences delivery
  entirely, and the worst case on the other side is the doubled message the
  guard was there to prevent in the first place.

A clean `stop()` releases the guard, so a restart takes it back at once rather
than waiting out a freshness window — but only when its join actually joined. A
`stop()` whose join times out leaves the guard held and says so: releasing a
bearer this client may still be inside a poll for is the dual poller the
ordering exists to prevent. `RelayClient(..., singleton=False)` turns
the whole thing off for a consumer that has its own singleton mechanism; it logs
a warning, because nothing else here prevents a second poller.

## Credentials

The onboarding string is the combined `<url>|<secret>` form — the gateway
travels inside the credential. The separator may arrive URL-encoded (`%7C`),
which is what a desktop connect flow writes:

```python
from ag2_relay_client.credentials import TokenSource

creds = TokenSource(token_file="~/.ag2/relay.env")  # or token="https://gw/relay|SECRET"
creds.base_url          # "https://gw/relay"
creds.source            # names the file, never the value
creds.reload()          # picks up a rotated secret; refuses a changed gateway
```

A rotation changes the secret and nothing else. A token source that starts
naming a *different* gateway is a reconfiguration, not a rotation: the swap is
refused and logged, and the client keeps running where it is. Restart to move
gateways.

Nothing is guessed. There is no bare-home fallback path, because that is the one
lookup that can silently bind the wrong identity after a reinstall or an account
switch.

## Requests

```python
from ag2_relay_client.transport import RelayHTTP

http = RelayHTTP(creds)
http.get("/v1/tasks", params={"wait": 25}, timeout=35)
http.post("/v1/results", {"id": "task-1", "body": "done"})
```

Every request carries an explicit `User-Agent` (the edge rejects urllib's
default), reads the bearer live so a rotation needs no restart, refuses
redirects while credentialed, and opens its socket through a resolver whose DNS
is wall-clock-bounded, single-flight, and IPv4-first — per client, with nothing
patched process-wide. `401`/`403` raise `AuthRejected`, which best-effort
callers must not swallow; other unusable answers raise `RelayHTTPError`,
carrying the status and the body for the call sites that have to read it.

## State

```python
from ag2_relay_client.state import StateLayout

layout = StateLayout("~/.ag2/state", instance="prod")
layout.ensure()         # 0700; holds the journal, the status file, the lock
```

Per-instance by construction: one host may run clients against several gateways,
and broker task ids are unique only *within* a gateway.

## Room Ops

Speaking in a room *as* the agent identity — post, edit, react, upload:

```python
rooms = client.room_ops                         # built from `egress_roots`
event = rooms.message(room_id, "⏳ On it...")   # keep the id; the ladder needs it
rooms.edit(room_id, event, answer)              # ...then complete with [REPLIED]
```

`client.room_ops` is read-only and holds no reachable bearer: four ops, and the
same allowlist `complete` uploads through — not a second door for a file.

**Nothing here raises into your loop.** A room that cannot be spoken to is a
room whose answer arrives the plain way, through `POST /v1/results`; losing an
answer to a decoration is never acceptable. A failure answers `None` / `False`
and marks room ops unavailable for a **time-gated** cooldown (~300 s) — long
enough that a broken broker is not retried per task, short enough that it heals
itself after a broker deploy without a restart. A `401`/`403` still reaches the
`on_auth_rejected` hook, because a revoked bearer is not a cosmetic failure.

An `op:edit` body over 4000 characters is refused *locally*, without spending
the cooldown: the reply goes through `/v1/results`, whose render path chunks it.
And this client never reacts to a message it was served — the broker places the
intake reaction, and a second one is the room seeing double.

## Sending a file

Egress is **paths only**, and the roots are fixed when the client is built:

```python
client = RelayClient(creds, state_dir=..., egress_roots=[workspace],
                     egress_max_bytes=24 * 1024 * 1024)
```

A client built with no roots sends no files, and says so in the answer rather
than in a log — the fail-closed reading of "nobody configured this", and the
only safe one.

**There is no bytes-upload API, public or private.** One exists nowhere in this
package on purpose: a surface that took bytes would make the allowlist
decorative, since a caller could read anything and hand the contents over. The
only door is `EgressAllowlist.open(path)`, which returns a descriptor it has
already judged — resolved, inside a root by a real path-separator boundary,
opened one component at a time with `O_NOFOLLOW` so a symlink swapped in after
the check cannot be followed, and `fstat`'d for regular-file-ness, size, and a
single hard link. There is no method that widens an allowlist at runtime, and
the object refuses attribute writes after construction.

The check now runs in the same process that holds the bearer, where it used to
run in a separate one. `egress.py`'s docstring records that regression honestly;
read it before changing anything there, and read `tests/test_egress.py` as the
threat model it mitigates.

## Result markers

One parser, because every copy of it drifted — and a marker one consumer
stripped reached users through another as literal text:

`client.complete` runs it; there is nothing to wire up and no second parser to
write:

```python
prepared = client.complete(task.id, agent_output, base_dir=where_it_ran)
prepared.uploaded          # ("chart.png",)
prepared.refused           # ("[attachment not sent: … (…)]",)
```

`prepare` reads the grammar (skip is terminal; `[dm-only]` is detected anywhere
and suppresses a `[channel:]` redirect; stripping is narrower than detection so
prose *discussing* a marker is not rewritten; a marker inside markdown code is
being shown, not issued), uploads whatever the body named, and hands back the
body to POST — with `[channel:]` re-stitched for the broker's deliverer and any
refused file explained in-band as `[attachment not sent: …]`.

Two properties are worth stating out loud:

- `[no-send]` / `[REPLIED]` / `[deduped: <id>]` **still POST**. They complete
  the lease with no user-visible message; skipping the POST as well leaves the
  lease to expire and the task to be re-served for ever.
- A **retried result POST uploads nothing more**. The body is re-derived from
  the same markers and the `(task, path)` ledger answers for the second pass;
  the ledger is retired by the POST that finally succeeds, because only success
  may retire one (F5).

## Events (optional, off unless asked for)

A Server-Sent Events channel on `/v1/events/stream`, for a consumer that wants
workspace events durably. Nothing in this library starts it and no module here
imports it — the only way one exists is this call:

```python
from ag2_relay_client.events import events

channel = events(http, sink)   # starts its own thread
channel.health()               # {"status": "connected", "last_cursor": 41, ...}
channel.stop()
```

`sink` is the consumer's durable store, duck-typed on two methods:
`durable_cursor()` returns the cursor of the last event that is durable, and
`commit(event)` **returning** is what makes one durable. Resume uses the durable
cursor, so a crash between "received" and "durable" replays the event rather
than losing it — the sink must be idempotent.

The channel is **isolated by construction**: its own thread, connection, backoff
and cursor, and a loop that swallows everything. A dead network, a revoked
bearer, a garbled frame, a sink that raises — none of them can reach task
delivery, which is the whole reason the module is shaped this way. Beyond that:
a 120 s read timeout, because a black-holed TCP path never says anything;
unusable frames skipped and not resumed from, because handing one to a sink that
raises means replaying it forever; `404` fatal and `401`/`403` retryable only
while a durable token source could still deliver a rotation. The bearer is read
through the same credential the poll loop rotates, so a rotation reaches the
stream on its next connect without a restart.

## License

MIT.
