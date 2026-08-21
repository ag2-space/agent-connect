# ACP as the local execution seam

The Worker drove Local Agents through one-shot subprocesses — `run(task, sandbox, cwd) -> str`
— which meant no memory between messages, no sign of life for up to ten minutes, and no way
to interrupt. We are replacing that seam with the Agent Client Protocol (Zed's ACP: JSON-RPC
over stdio), where the Worker is the ACP Client and holds a long-lived connection to the Local
Agent. Sessions give continuity, `session/update` gives progress, `session/cancel` gives
interruption.

## Consequences worth stating up front

**The adapter contract becomes async and event-shaped.** `run() -> str` becomes a stream of
our own event vocabulary, not raw ACP — otherwise every non-ACP agent (`ollama` talks HTTP to a
local model and will never speak ACP) would have to fake a protocol it does not implement, and
the eventual room-side consumer would have to learn ACP to read a progress update. The five
existing adapters keep their synchronous `run()` behind a thread shim rather than being
rewritten; `codex` is the only adapter verified in production and breaking it for tidiness is a
bad trade.

**Two dependency policies were abandoned deliberately.** `dependencies = []` was a selling
point; it ends here, because the maintained ACP SDK for Python carries pydantic, and Claude
Code speaks ACP only through an npm bridge (`@agentclientprotocol/claude-agent-acp`) — a Python
project now needs Node. Both were accepted knowingly, with the bridge version pinned: that
package renamed itself and moved from 0.16 to 0.65 within six months, so an unpinned `npx -y`
would eventually pull an incompatible major into a working install.

**ACP is restricted to owner-tier, and this is a safety decision, not an oversight.** The
old confinement was real: `codex --sandbox read-only` is enforced by the operating system.
ACP has no sandbox concept at all, and the Claude Agent SDK performs its own file I/O, so the
only lever left is answering `session/request_permission` by policy — which binds only an agent
that chooses to ask. Rather than ship a cooperative imitation of a guarantee we used to have,
non-owner Tasks refuse ACP outright and continue to be served by the sandboxed adapters. Even
for the owner, ACP is *more* powerful than codex ever was (no OS confinement, network always
on), so the policy allows writes under the session `cwd` and rejects outside it — reconstructing
by convention roughly what the OS used to enforce. Lifting the non-owner restriction requires
real confinement around the bridge process (`sandbox-exec`, a separate uid, a container); until
that exists, the README must say the guarantee is cooperative rather than imply protection that
is not there.
