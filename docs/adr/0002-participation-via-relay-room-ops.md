# Room participation goes through relay room-ops, not our own Matrix client

To make the agent a real participant — visibly working, able to speak — the obvious path
looked like writing a Matrix client: `GET /_matrix/client/v3/sync` is an *outbound* long-poll,
so NAT was never the obstacle the appservice argument made it out to be, and s.space rooms
are unencrypted, which removes the single largest cost. We are not doing it. The relay already
exposes everything that path would buy: `POST /v1/room` performs `message`, `edit`, `react`,
`redact`, `upload` and more *as* the Agent Identity, and `GET /v1/events` is a per-agent log of
rooms the agent subscribed to. The Worker already holds the relay bearer token, so this needs
no new credential and no change on the server.

The decisive argument is not cost, it is ownership: the backend deliberately keeps workers out
of Matrix — *"workers never hold Matrix tokens; the relay is the only Matrix speaker"*. A
`/sync` loop of our own would require the Worker to hold a Matrix access token, breaking a
boundary that was drawn on purpose. Prior notes in this project claimed the relay could not do
these things; that was wrong, and it is why the Matrix-client track was ever considered.

## Consequences

**Replies follow the ladder the protocol prescribes**, not one of our own design: the broker
reacts 🫡 on ack (workers must not, it doubles the eyes), the Worker posts the fleet-wide
placeholder "⏳ On it...", edits it as tool calls progress, edits it into the answer, and closes
the lease with `[REPLIED]` so the deliverer does not post a second copy. Edits are throttled and
driven by tool calls rather than text chunks — an edit per chunk would be a storm of `m.replace`
for content the final edit overwrites anyway. Model thoughts never reach the room.

**Only the outbound half is adopted now.** Reading `GET /v1/events` — becoming a participant
that sees the room rather than one that only answers when addressed — is left for later,
because it brings cursors, retention, at-least-once delivery and, most of all, a turn-taking
policy: an agent that responds to every message is a token catastrophe, and no protocol decides
that for us.

## Amendment (0.4.0): the broker delivers the answer; no placeholder

The placeholder ladder above is retired. Two facts about the backend made it wrong:

- core-api's activity ledger reads a result body that starts with `[REPLIED]` or `[no-send]`,
  with no `status`, as **no action** (`suppressed`), never as replied. Every agent-connect
  reply was recorded as suppressed, and the status icons said so.
- s.space already shows the request's queued / working / replied status from that ledger: the
  relay client's ack drives "working" and the result drives the rest. The placeholder was a
  second, worse copy of it, and it made a turn that needs no reply impossible to finish
  silently, because Room Ops cannot delete the placeholder.

So the Worker posts and edits nothing of the answer. A Turn ends in one result
(`POST /v1/results`, with the lease's `lease_id` and `execution_generation`):

- `status: "completed"` with the answer as the body. The broker posts it once, as a reply to
  the request, and the ledger records responded, then delivered.
- `status: "no_action_needed"` when the agent has nothing to say: no text, or exactly
  `NO_REPLY`. Nothing is posted and nobody is mentioned.
- `status: "failed"` when the Turn produced nothing and did not end normally. The broker posts
  its one failure notice. An Adapter that explained the failure (not configured, not logged
  in) is answered with that explanation instead.

The broker's reply mentions the requester; that is how an answer reaches an agent that asked.
Agent exchanges stay bounded because acknowledgements finish as `no_action_needed`, and
core-api's agent-chain backstop can stop a chain.

Still agent-connect's own messages, through Room Ops: notices that are facts about the run (a
session reset, a reply withheld for the owner's review), owner questions, and a released reply.
A notice never mentions the requester. Live progress edits, the "queued" notice and their
settings (`AGENT_CONNECT_LIVE_PROGRESS`, `AGENT_CONNECT_PROGRESS_THROTTLE`,
`AGENT_CONNECT_EDIT_CEILING`) are gone.
