"""`ag2-relay-client` — the AG2 Space relay wire, as a library.

One bearer's whole conversation with the broker lives here: poll, lease, ack,
results, heartbeat, media in both directions, Room Ops. A consumer gets Tasks
out of an in-memory queue and hands answers back through `complete`/`reject`;
everything that touches the wire — including resolving media markers to local
files — is on this side of that seam.

Two properties are contractual rather than incidental, because the second
consumer of this package is sutando, which will take a *version* of this wire
and not a copy of it:

- **stdlib-only.** No dependency tree comes with a transport.
- **Python >= 3.9, sync/threaded, no asyncio.** An asyncio consumer wraps the
  calls in an executor; a sync one calls them directly.
"""
from __future__ import annotations

#: The single source of the distribution version (`pyproject.toml` reads it).
__version__ = "0.1.1"

# The seam, reachable in one import: a consumer needs the client, the credential
# it is constructed from, the type of the thing that comes out of the queue, and
# the type of what that thing carries. Nothing else here is part of it.
from .client import RelayClient  # noqa: E402
from .credentials import TokenSource  # noqa: E402
from .envelope import Task  # noqa: E402
from .media import Attachment  # noqa: E402

__all__ = ["RelayClient", "TokenSource", "Task", "Attachment", "__version__"]
