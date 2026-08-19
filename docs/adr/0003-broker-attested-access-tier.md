# Broker-attested Access Tier; the Worker trusts the attestation

The broker computes `access_tier` from the sender's Matrix identity and delivers it in
every task; the Worker (via the ag2-sparrow relay client) honors that attestation
instead of restamping tiers locally. The wire vocabulary is exactly `owner | guest`;
a broker task with a missing or unrecognized tier is treated as `guest`, never `owner`.
`owner` is a trust tier, not an identity: the registrant of the Agent Identity plus
anyone they explicitly `trust @user`. Sparrow's `REMOTE_TASK_TIER`/`AG2_REMOTE_TIER`
env promotion (defaulting everything to `owner`) is removed; the local `access.json`
tierMap survives as the host owner's explicit per-sender override. Local-only values
(`team`, `ambient`) never cross the wire and fail closed as non-owner. The ACP
adapter's owner-only check keys on the attested tier alone and refuses guest tasks
with a visible reply in the room.

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
- The broker's `WORKER-PROTOCOL.md` remains the single normative description of the
  wire field; no mirrored ADR on the backend.
- Cutover is atomic — the broker has always sent the field; no compatibility shim.
