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
journal, ack, results, heartbeat, auth recovery, status — the whole outbound
half — Room Ops, the egress allowlist and the result-marker grammar — and the
optional events channel, all behind the seam below. Media ingress (marker
resolution on the way in) and the singleton-per-bearer guard follow.

## The seam

```python
from ag2_relay_client import RelayClient, TokenSource

client = RelayClient(TokenSource(token_file="~/.ag2/relay.env"),
                     state_dir="~/.ag2/state", instance="prod")
client.start()

task = client.next_task(timeout=30)      # or read client.tasks yourself
if task:
    client.complete(task.id, my_agent.handle(task.body))
```

Two things wide, and that is the whole consumer surface for the inbound half:
Tasks come out of an in-memory queue, answers go back through `complete` — or
`reject(id, reason)`, which dead-letters a permanently malformed task instead of
letting it re-serve five times.

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
hooks and logs all fail silently by contract. Health is asked of
`client.healthz()`; `GET /v1/tasks` is never a probe, because it leases tasks.

The ordering inside one accepted task is journal, then ack, then delivery. The
ack is informational and gates nothing — its 404 is content-sniffed, because
`not leased to you` is one task's expired lease and a bare 404 is a broker
without the route, and treating the first as the second once blinded a whole
host's delivery state.

A rejected bearer is a wait, not a death: the durable token source is re-read at
once (a rotation may already have happened), and otherwise the loop holds at a
slow cadence, saying so every pass, until one lands — then resumes live, no
restart. A token file that starts naming a *different* gateway is a
reconfiguration and is refused.

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
from ag2_relay_client.roomops import RoomOps

rooms = RoomOps(http, allowlist=allowlist)
event = rooms.message(room_id, "⏳ On it...")   # keep the id; the ladder needs it
rooms.edit(room_id, event, answer)              # ...then complete with [REPLIED]
```

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
from ag2_relay_client.egress import EgressAllowlist

allowlist = EgressAllowlist([workspace / "results"], max_bytes=24 * 1024 * 1024)
```

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

```python
from ag2_relay_client.outbound import Outbound

prepared = Outbound(rooms).prepare(task_id, room_id, agent_output)
http.post("/v1/results", {"id": task_id, "body": prepared.body})
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
- Calling `prepare` again for the same task — which is what a retried result
  POST does — re-derives the same body and **uploads nothing more**. Call
  `forget(task_id)` when the POST finally succeeds; only success retires an id.

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
