# Room participation goes through relay room-ops, not our own Matrix client

To make the agent a real participant — visibly working, able to speak — the obvious path
looked like writing a Matrix client: `GET /_matrix/client/v3/sync` is an *outbound* long-poll,
so NAT was never the obstacle the appservice argument made it out to be, and AG2 Space rooms
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
