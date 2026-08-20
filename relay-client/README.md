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
journal, ack, results, heartbeat, auth recovery, status — behind the seam
below. Media in both directions, Room Ops, the marker grammar and the
singleton-per-bearer guard follow.

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

## License

MIT.
