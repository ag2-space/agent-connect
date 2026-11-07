# Broker-attested Access Tier; the Worker trusts the attestation

The broker computes `access_tier` from the sender's Matrix identity and delivers it in
every task; the Worker (via the ag2-sparrow relay client) honors that attestation
instead of restamping tiers locally. The wire vocabulary is exactly `owner | guest`;
a broker task with a missing or unrecognized tier is treated as `guest`, never `owner`.
`owner` is a trust tier, not an identity: the registrant of the Agent Identity plus
anyone they explicitly `trust @user`. Sparrow's `REMOTE_TASK_TIER`/`AG2_REMOTE_TIER`
env promotion (defaulting everything to `owner`) is removed; the local `access.json`
tierMap survives as the host owner's per-sender **cap** — `min(attested, mapped)`, so
it can lower a sender's tier on this host and can never raise one. Any value other
than `owner` is treated as `guest`, whatever it is. The ACP adapter's owner-only
check keys on the attested tier alone and refuses guest tasks with a visible reply in
the room.

### What the wire actually carries today

The vocabulary above is the one this decision commits to; it is not yet the only one
on the wire. Sutando writes `access_tier: team` on a task from a **negotiated
collaborator**, and that value reaches the worker side. (`ambient` no longer exists at
all — it was renamed `local_observation` in sutando.) The worker side does not
special-case any of it: anything that is not exactly `owner` is a guest, so a
negotiated Team collaborator's task runs as a guest — read-only under codex, and
**refused outright under ACP**. That is fail-closed and deliberate, and it is also a
silent demotion of a path sutando believes in, which is recorded here rather than
discovered in a room. Whether the negotiated-collaborator path should reach a BYO
agent at all is a question for the sutando side; this ADR fixes only what the worker
does with a tier it cannot verify, which is to trust none of it.

## Why

Sparrow's previous policy — "tier is a local decision, the wire tier is ignored" —
treated the wire field as self-claimed. It is not: the puller fetches over TLS with
its own bearer from the broker it already trusts to deliver tasks at all. Discarding
the attestation and defaulting to `owner` produced a confused deputy: an allowlisted
guest's task, attested `guest` by the broker, arrived as `owner` and ran with full
cooperative permissions. Attestation cannot stop a malicious host from lying to
itself (the Worker runs on the owner's machine); its job is exactly this
guest-runs-as-owner failure, and the local-autonomy alternative could not deliver an
honest onboarding promise.

## Consequences

- The README/portal promise must distinguish paths: guest tasks run read-only under
  the codex adapter but are refused outright by the ACP adapter.
- A sutando-negotiated Team collaborator is demoted to guest by this rule, and so is
  refused by the ACP adapter. Nothing on the worker side can tell that apart from an
  ordinary guest, and nothing should: a tier the broker did not attest as `owner` is
  not a tier this side gets to interpret.
- The broker's `WORKER-PROTOCOL.md` remains the single normative description of the
  wire field; no mirrored ADR on the backend.
- Cutover is atomic — the broker has always sent the field; no compatibility shim.

## Amendment: listed Team collaborators under ACP

The broker sends Team as `access_tier: guest` with `requested_access_tier: team` and
`collaborator: true`. In a private room with no Agent Native card this is the default
for every member, so the flag alone cannot mean "the owner chose this sender".

The ACP adapter therefore admits a non-owner only when both hold:

- the broker attested `requested_access_tier: team` and the exact boolean
  `collaborator: true`; and
- the sender's Matrix id is in the Worker's `AGENT_CONNECT_ACP_COLLABORATORS`.

The list can only narrow what the room grants, never widen it, so the rule above
(local settings lower, never raise) still holds. The list narrows **who** is admitted,
not **what** they can do: once admitted, a sender has the agent's normal capabilities.
agent-connect cannot confine an ACP agent, so a narrower tool policy for collaborators
is the agent's to enforce. To let it, the Worker sends `_meta["ag2/requester"]` on
`session/new` and every `session/prompt`: `user_id`, `access_tier`,
`requested_access_tier`, `collaborator` and `admitted_as` (`owner` or `collaborator`),
all from the broker's attestation and never from message text. Each collaborator gets
a Session per (room, sender), so collaborators never share history with each other or
with the owner. Separate history does not separate files, memory or credentials.
agent-connect does not implement the room's sensitive data filter, and a listed
sender's results are not scanned. Everyone else is refused as
before. This matches sparrow's per-adapter `--allow-collaborator` opt-in, narrowed to
named senders because the room default cannot be told apart from an owner's choice.
