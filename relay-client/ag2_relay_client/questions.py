"""Owner questions: a private multiple-choice question to this agent's owner.

AG2 Space's room questions (`room.question.ask` / `room.question.inspect`) are
core Actions. They are room-visible, so a question is asked only in the owner's
DM with this agent, and only after its membership is checked to be the agent and
the owner alone. The Actions are reached the way sparrow reaches them:

1. `GET <relay>/v1/mcp/discovery` names `mint_url` and `mcp_url`.
2. `POST <mint_url>` with the relay bearer exchanges it for a short-lived MCP
   access token acting as this agent. The relay bearer goes only to the relay's
   own origin, at the mint path.
3. One JSON-RPC `tools/call` on `mcp_url` per Action, with the access token.

Everything here fails closed: any error is logged without a credential and the
public method returns `None`. Sync, like `RoomOps`.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from typing import Callable, NamedTuple, Optional, Sequence, Tuple

from .transport import AuthRejected, RelayHTTP, RelayHTTPError, same_origin

log = logging.getLogger(__name__)

DISCOVERY_PATH = "/v1/mcp/discovery"
MINT_PATH = "/api/v1/mcp/agent-access-tokens"
AGENTS_PATH = "/v1/agents"
ROOM_PATH = "/v1/room"
MCP_PROTOCOL_VERSION = "2025-06-18"
AUTH_FAILED = "AUTHENTICATION_FAILED"
#: The mint allows 30 per 60 s per agent; a 429 stops minting this long.
MINT_BACKOFF_S = 60.0
TOKEN_TTL_MAX_S = 3600.0
OWNER_TTL_S = 300.0
OWNER_RETRY_S = 30.0
PROMPT_MAX = 2000
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class Owner(NamedTuple):
    mxid: str
    #: The owner's DM with this agent, or "" when none is verified private.
    dm_room: str


class Answer(NamedTuple):
    #: pending, answered, expired, cancelled, or held (answered but withheld).
    status: str
    #: The chosen option id; "" unless answered with an option.
    option: str
    revision: str


class _Fail(Exception):
    """Internal: a step that cannot go on. Never leaves this module."""


def _safe_endpoint(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _Fail("discovery named no endpoint")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme == "https" and parts.hostname:
        return value
    if parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK:
        return value
    raise _Fail("an endpoint is neither https nor loopback http")


class OwnerQuestions:
    """Ask this agent's owner a private question, and read the answer."""

    __slots__ = ("_http", "timeout", "_clock", "_lock", "_endpoints", "_token",
                 "_token_until", "_mint_blocked_until", "_agent", "_owner",
                 "_owner_until")
    _SEALED = frozenset({"_http"})

    def __init__(self, http: RelayHTTP, timeout: float = 15.0,
                 clock: Callable[[], float] = time.monotonic):
        self._http = http
        self.timeout = float(timeout)
        self._clock = clock
        self._lock = threading.Lock()
        self._endpoints: Optional[Tuple[str, str]] = None
        self._token = ""
        self._token_until = 0.0
        self._mint_blocked_until = 0.0
        self._agent = ""
        self._owner: Optional[Owner] = None
        self._owner_until = 0.0

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return "<OwnerQuestions>"

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._SEALED and hasattr(self, name):
            raise AttributeError(f"{name} is fixed at construction")
        object.__setattr__(self, name, value)

    # -- public ---------------------------------------------------------------

    def owner(self) -> Optional[Owner]:
        """This agent's owner and verified-private DM, cached; None if unknown."""
        try:
            return self._owner_cached()
        except Exception as exc:  # noqa: BLE001 — fail closed
            log.warning("owner questions: owner lookup failed (%s)", _describe(exc))
            return None

    def ask(self, prompt: str, options: Sequence[Tuple[str, str]], *,
            expires_in_s: int, operation_id: str) -> Optional[str]:
        """Ask the owner in their private DM; the question id, or None."""
        try:
            owner = self._owner_cached()
            if owner is None or not owner.dm_room:
                return None
            answer = self._action("room.action.execute", owner.dm_room, "room.question.ask", {
                "prompt": (prompt or "")[:PROMPT_MAX],
                "options": [{"id": oid, "label": label} for oid, label in options],
                "target_members": [owner.mxid],
                "answerable_by": "targets",
                "allow_free_text": False,
                "response_mode": "first",
                "expires_in_seconds": max(60, min(int(expires_in_s), 604800)),
            }, operation_id=operation_id)
            question_id = answer.get("question_id")
            return question_id if isinstance(question_id, str) and question_id else None
        except Exception as exc:  # noqa: BLE001
            log.warning("owner questions: ask failed (%s)", _describe(exc))
            return None

    def inspect(self, question_id: str) -> Optional[Answer]:
        """The question's state, or None when it cannot be read."""
        try:
            owner = self._owner_cached()
            if owner is None or not owner.dm_room:
                return None
            answer = self._action("room.action.read", owner.dm_room, "room.question.inspect",
                                  {"question_id": question_id})
            question = answer.get("question")
            if not isinstance(question, dict):
                return None
            status = str(question.get("status") or "")
            withheld = question.get("answer_withheld")
            if isinstance(withheld, dict) and withheld.get("held") is True:
                status = "held"
            option = ""
            accepted = question.get("accepted_answer")
            if status == "answered" and isinstance(accepted, dict) \
                    and accepted.get("kind") == "choice":
                option = str(accepted.get("option_id") or "")
            return Answer(status, option, str(question.get("revision") or ""))
        except Exception as exc:  # noqa: BLE001
            log.warning("owner questions: inspect failed (%s)", _describe(exc))
            return None

    # -- owner ------------------------------------------------------------------

    def _owner_cached(self) -> Optional[Owner]:
        now = self._clock()
        if now < self._owner_until:
            return self._owner
        try:
            found = self._lookup_owner()
        except Exception:
            self._owner, self._owner_until = None, now + OWNER_RETRY_S
            raise
        self._owner = found
        self._owner_until = now + (OWNER_TTL_S if found else OWNER_RETRY_S)
        return found

    def _lookup_owner(self) -> Optional[Owner]:
        agent = self._agent_id()
        listing = self._http.get(AGENTS_PATH, timeout=self.timeout)
        rows = listing.get("agents") if isinstance(listing, dict) else None
        row = next((r for r in rows or [] if isinstance(r, dict) and r.get("id") == agent), None)
        owner = str((row or {}).get("owner") or "")
        if not owner.startswith("@"):
            return None
        dm = str((row or {}).get("owner_dm_room") or "")
        return Owner(owner, dm if dm and self._private(dm, agent, owner) else "")

    def _private(self, room: str, agent: str, owner: str) -> bool:
        answer = self._http.post(ROOM_PATH, {"op": "members", "room_id": room},
                                 timeout=self.timeout)
        rows = answer.get("members") if isinstance(answer, dict) else None
        if not isinstance(rows, list) or (isinstance(answer, dict) and answer.get("error")):
            return False
        members = {r.get("user_id") for r in rows if isinstance(r, dict)}
        return bool(members) and members <= {agent, owner}

    def _agent_id(self) -> str:
        if not self._agent:
            whoami = self._tool("ag2.whoami", {})
            actor = whoami.get("actor") if isinstance(whoami, dict) else None
            agent = str((actor or {}).get("id") or "")
            if not agent.startswith("@"):
                raise _Fail("whoami named no agent")
            self._agent = agent
        return self._agent

    # -- MCP ----------------------------------------------------------------------

    def _action(self, tool: str, room: str, action: str, arguments: dict,
                operation_id: str = "") -> dict:
        call = {"room_id": room, "action": action, "arguments": arguments}
        if operation_id:
            call["operation_id"] = operation_id
        envelope = self._tool(tool, call)
        result = envelope.get("result")
        return result if isinstance(result, dict) else envelope

    def _tool(self, name: str, arguments: dict) -> dict:
        _mint, mcp_url = self._discover()
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}
        for attempt in (0, 1):
            token = self._access_token(refresh=attempt > 0)
            try:
                rpc = self._http.request_url(
                    "POST", mcp_url, token, payload=body, timeout=self.timeout,
                    headers={"MCP-Protocol-Version": MCP_PROTOCOL_VERSION})
            except AuthRejected:
                if attempt == 0:
                    continue
                raise _Fail("the MCP endpoint refused the access token")
            result = rpc.get("result") if isinstance(rpc, dict) else None
            if not isinstance(result, dict):
                raise _Fail("the MCP endpoint did not answer the call")
            envelope = _envelope(result)
            if result.get("isError"):
                if envelope.get("code") == AUTH_FAILED and attempt == 0:
                    continue
                raise _Fail(f"{name} answered an error ({envelope.get('code') or 'no code'})")
            return envelope
        raise _Fail("the MCP endpoint refused the access token")

    def _discover(self) -> Tuple[str, str]:
        if self._endpoints is None:
            answer = self._http.get(DISCOVERY_PATH, timeout=self.timeout)
            if not isinstance(answer, dict):
                raise _Fail("discovery answered nothing usable")
            mint = _safe_endpoint(answer.get("mint_url"))
            if not same_origin(mint, self._http.base_url) \
                    or urllib.parse.urlsplit(mint).path != MINT_PATH:
                raise _Fail("mint_url is not the relay's own mint; the relay token is not sent")
            self._endpoints = (mint, _safe_endpoint(answer.get("mcp_url")))
        return self._endpoints

    def _access_token(self, refresh: bool = False) -> str:
        with self._lock:
            now = self._clock()
            if not refresh and self._token and now < self._token_until:
                return self._token
            if now < self._mint_blocked_until:
                raise _Fail("the mint is rate limited")
            self._token, self._token_until = "", 0.0
            mint, _mcp = self._discover()
            try:
                answer = self._http.request_url(
                    "POST", mint, self._http.credentials.secret, timeout=self.timeout)
            except RelayHTTPError as exc:
                if exc.status == 429:
                    self._mint_blocked_until = now + MINT_BACKOFF_S
                raise _Fail(f"the mint answered {exc.status}") from None
            token = answer.get("access_token") if isinstance(answer, dict) else None
            if not isinstance(token, str) or not token:
                raise _Fail("the mint returned no access token")
            try:
                ttl = float(answer.get("expires_in") or 900)
            except (TypeError, ValueError):
                ttl = 900.0
            ttl = min(ttl, TOKEN_TTL_MAX_S) if ttl == ttl else 900.0
            self._token, self._token_until = token, now + max(30.0, ttl - 60.0)
            return token


def _envelope(result: dict) -> dict:
    """A tool result's JSON: structuredContent, else the last JSON-object content text."""
    import json

    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    for item in reversed(result.get("content") or []):
        try:
            value = json.loads(item.get("text") or "")
        except (ValueError, AttributeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _describe(exc: BaseException) -> str:
    """An error for a log line: its kind and status, never its body or a token."""
    if isinstance(exc, _Fail):
        return str(exc)
    status = getattr(exc, "status", None)
    return f"{type(exc).__name__}" + (f" {status}" if status else "")
