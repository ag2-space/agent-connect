# What a dialled agent says unasked reaches the room it names

A Turn is a dial, a `session/new` or `session/load`, one prompt, one answer, and a hang-up
(`docs/adr/0004`). Nothing the agent says outside that window has anywhere to go: between
Turns no socket is open, and inside one the Client listened for `session/update` only. An AG2
Assistant that runs a scheduled task hours after a room asked for it therefore finished in
silence — the outcome existed in its web UI and nowhere else.

Two additions, both small, and the design question is where the room is known.

**The Session is told where its messages belong.** `session/new` now carries
`_meta["ag2/destination"] = {chat_id, room_name, access_tier}`: the Turn's room, and the Tier
beside it. The room is the one durable address — a Session id dies with its dial, the room
outlives every reconnect — and the agent keeps it on the Session it opened, so a Task created
in that Session reports to the room, not to a socket that is gone. A roomless Task declares
nothing. The key is namespaced the way the protocol's extensibility section asks; the agent
that honours it is AG2 Assistant (its ADR 0049), and an agent that ignores `_meta` loses
nothing.

**Extension notifications are received, and a standing link is held to receive them.** The
Client implements `ext_notification`; `_ag2/notify` with `{destination, text}` is posted to
`destination` through Room Ops, the Worker's only way of speaking in a room (`docs/adr/0002`),
and every other method is ignored. A dialled agent additionally gets the Listener Link: one
dial held open by the Worker for the life of the process, re-dialled after a pause when it
dies, that opens no Session and sends no prompt. It exists so the agent has a client to
deliver to between Turns; the agent queues until then, so a Worker that was down receives
what it missed on its next dial. A spawned agent gets no link — it only exists during a Turn,
and a Turn's own dial listens.

## Considered options

- **Treat the Turn's `session/update` stream as the delivery** — rejected: a run that ends
  after the Turn has no Turn to update, and a reconnected Client has a different Session.
- **Poll the agent for finished tasks** — rejected: it makes the Worker know the agent's task
  model; the notification keeps the agent the authority on what is worth saying.
- **Let the agent post to the room itself** — rejected: the relay is the only Matrix speaker
  and the bearer is the Worker's; the agent names the room, the Worker speaks.

## Consequences

- A second standing connection per Worker to a dialled agent. The door accepts concurrent
  dialers by design (`docs/adr/0004`); the link is idle but for the notifications it carries.
- Delivery is the agent's promise, not this Worker's: the Worker posts what it is handed and
  logs a room it cannot speak to. A failed post is not retried here, because the agent's
  queue is drained the moment the notification is accepted on the wire.
- Posting is unconditional on Tier: the agent is only ever reached at owner tier (ADR 0001),
  so everything it says unasked is the owner's. The `access_tier` in the destination is
  context for the agent, never an instruction to this Worker.
