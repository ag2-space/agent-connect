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

Under construction. What ships today is the foundation the wire loop is built
on: credentials, name resolution, the request core, backoff, and the state-dir
layout — plus the optional events channel, which needs nothing above it. The
Task queue and the outbound surface follow.

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
