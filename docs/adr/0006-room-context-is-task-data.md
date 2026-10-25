# Room context is bounded task data

Room context is supplied by the authoritative relay alongside a Task. The Worker
does not read room history itself: membership, Agent Native context visibility,
acting Agent Identity, and history availability belong to the service that can
verify them. Room text cannot grant an Access Tier or alter a Sandbox.

The context packet crosses the parser and Adapter boundary separately from the
current prompt. It has an independent byte budget; an oversized or invalid
packet cannot consume the current message's existing text budget. Absence is
compatible with old brokers. Unsupported or mismatched context is unavailable,
not a reason to invent room facts or broaden a read.

The room card's optional `category` is a string of at most 256 Unicode code
points, accepted without trimming or coercion alongside `name` and `topic`.
Other unknown fields remain invalid. This is a pre-activation contract update:
the producer must omit category for consumers without the updated support, as
the existing v1 capability does not distinguish earlier strict validators.

Each capable Worker's authenticated poll declares the known scalar
`capabilities=room-context-v1` through the existing task-poll field. A heartbeat
alone can be stale after a consumer change or come from a different process
sharing the credential. Generic Relay Client users remain unadvertised until
explicit opt-in; format support is separate from producer authorization. The
heartbeat array is not serialized into a guessed multiple-value query format.

Each Turn carries its own packet. A "sent once" cache cannot establish that the
current Local Agent Session received it: the Session may be new, retired,
compacted, or restored after a Worker restart. A changed membership or policy
boundary must also stop reuse of a Session containing older context.

The Relay Client journal continues to store IDs, rooms, and outbound pending
results only. It does not persist inbound context or task bodies. After a
restart, context arrives again with broker redelivery and is parsed again.

ACP receives a separately marked context block, so consumers that support
profile memory can exclude room material from profile extraction. Text-only
Adapters receive bounded, explicitly framed room data before the unchanged
current prompt. This framing expresses provenance; it is not an authorization
mechanism or a guarantee against model instruction-following mistakes.

Framing instructs the agent to use the briefing first and avoid an extra context
read when it is sufficient. Available MCP tools remain appropriate for a
specific missing or fresh permitted fact or a requested action, using the same
acting identity and originating room under current policy and normal permission
checks. This does not permit automatic bulk replacement of missing context,
reads of denied sections, or identity/credential switching to bypass a denial.
If the needed information is unavailable or denied, the agent should state the
limitation and ask. This guidance neither installs tools nor enforces their use;
the backend must authorize every read and action. Tests verify what reaches the
adapter, not the reasoning behavior of an arbitrary connected model.

This consumer support does not enable automatic context delivery. Backend
policy enforcement and real invite/join-to-response verification are required
before rollout. Shipping also requires publishing the corresponding Relay
Client distribution before installing a Worker that depends on it; a source
checkout test does not verify that a published package contains these changes.
