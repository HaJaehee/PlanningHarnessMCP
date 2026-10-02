"""Minimal MCP (JSON-RPC 2.0) protocol layer - standard library only.

Implemented directly rather than via the `mcp` SDK because the deployment target is a
locked-down corporate PC where `pip install` may not be available. The surface MCP
clients actually use is small: initialize, tools/list, tools/call, ping.

Note on errors: a failed tool call is returned as a NORMAL result whose payload has
`ok: false` plus a corrective `next_action`. A JSON-RPC-level error would surface to the
model as a raw client exception string, which reliably makes a weak model abandon the
protocol and answer from memory.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

log = logging.getLogger("planning-mcp.protocol")

PROTOCOL_VERSION = "2024-11-05"

INSTRUCTIONS = (
    "Planning harness. Start each new user request with plan_and_think, get the user's "
    "approval with request_user_approval, then track execution with update_task_progress. "
    "Follow the next_action field in every response; ANSWER_USER means write the answer."
)

METHOD_NOT_FOUND = -32601
INVALID_REQUEST = -32600
PARSE_ERROR = -32700


class McpProtocol:
    def __init__(
        self,
        handlers: Any,
        tools: list[dict[str, Any]],
        name: str,
        version: str,
        notifier: Any = None,
    ):
        self.handlers = handlers
        self.tools = tools
        self.name = name
        self.version = version
        self.tool_names = {t["name"] for t in tools}
        # Set by the transport once it owns the output stream. Lets a blocking handler
        # emit progress heartbeats that keep the client's request timer alive.
        self.notifier = notifier
        # Tool calls currently executing, so `notifications/cancelled` can reach the one
        # it names. Keyed by str(id): a client is free to send the id as a number and
        # echo it back as a string, and a missed match means the handler goes on waiting
        # for a client that stopped listening minutes ago.
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_lock = threading.Lock()

    # ---- JSON-RPC plumbing ---------------------------------------------
    @staticmethod
    def _result(msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def handle_message(self, msg: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
        if isinstance(msg, list):  # JSON-RPC batch
            out = [r for r in (self.handle_message(m) for m in msg) if r is not None]
            return out or None
        if not isinstance(msg, dict):
            return self._error(None, INVALID_REQUEST, "Request must be a JSON object.")

        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        try:
            result = self._route(method, params, msg_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("Protocol error handling %s", method)
            if is_notification:
                return None
            return self._error(msg_id, INVALID_REQUEST, f"{type(exc).__name__}: {exc}")

        if result is _UNKNOWN_METHOD:
            if is_notification:
                return None  # unknown notifications are ignored by design
            return self._error(msg_id, METHOD_NOT_FOUND, f"Unknown method: {method}")

        if is_notification:
            return None
        return self._result(msg_id, result)

    # ---- cancellation ----------------------------------------------------
    def _cancel(self, request_id: Any) -> None:
        """A client has given up on a call it made. Stop working on it.

        This used to be swallowed with the other notifications. The cost was invisible
        and large: when a client abandons a blocking approval at its own request
        timeout, the server went on holding the wait - up to the full approval timeout -
        for a reply nobody would ever read, and learned nothing from the event. The
        handler now unblocks promptly and records how long the client actually allowed,
        which is the only reliable way to find out what a given client's limit is.
        """
        if request_id is None:
            return
        with self._inflight_lock:
            event = self._inflight.get(str(request_id))
        if event is None:
            return  # already finished, or never ours
        log.warning("Client cancelled request %s", request_id)
        event.set()

    # ---- method routing -------------------------------------------------
    def _route(self, method: str | None, params: dict[str, Any], msg_id: Any = None) -> Any:
        if method == "initialize":
            # Which host connected. Field loops cannot be reproduced from here, so the
            # audit log has to be able to say which agent produced each line.
            note_client = getattr(self.handlers, "note_client", None)
            if callable(note_client):
                try:
                    note_client(params.get("clientInfo"))
                except Exception:  # noqa: BLE001 - telemetry must never fail a handshake
                    log.debug("Could not record clientInfo")
            return {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
                # Hosts show this to the model as a system instruction. "Call
                # plan_and_think before answering anything" used to be here - the same
                # rule that sent a thinking model back into planning when the server
                # asked it to answer (D25).
                "instructions": INSTRUCTIONS,
            }
        if method == "notifications/cancelled":
            self._cancel(params.get("requestId"))
            return {}
        if method in ("notifications/initialized", "initialized"):
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            return self._call_tool(params, msg_id)
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}
        if method == "logging/setLevel":
            return {}
        return _UNKNOWN_METHOD

    def _call_tool(self, params: dict[str, Any], msg_id: Any = None) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments")
        if isinstance(arguments, str):
            # Some clients forward the raw string the model emitted.
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}

        if name not in self.tool_names:
            payload = {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "plan_status": "NONE",
                "next_action": "CALL_GET_CURRENT_PLAN",
                "next_action_hint": (
                    f"There is no tool named '{name}'. The available tools are: "
                    + ", ".join(sorted(self.tool_names))
                    + ". Call get_current_plan with plan_id='current' to resync."
                ),
            }
        else:
            # MCP only permits progress notifications for a request that supplied a
            # token. Since 1.14.0 the wait no longer depends on them - it is chunked to
            # fit inside the client's limit either way - so the token only decides
            # whether the heartbeat is sent at all.
            meta = params.get("_meta") or {}
            cancel_event = threading.Event()
            key = None if msg_id is None else str(msg_id)
            if key is not None:
                with self._inflight_lock:
                    self._inflight[key] = cancel_event
            try:
                payload = self.handlers.dispatch(
                    name,
                    arguments,
                    progress_token=meta.get("progressToken"),
                    notifier=self.notifier,
                    cancel_event=cancel_event,
                )
            finally:
                if key is not None:
                    with self._inflight_lock:
                        self._inflight.pop(key, None)

        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return {"content": [{"type": "text", "text": text}], "isError": False}


class _Unknown:
    pass


_UNKNOWN_METHOD = _Unknown()
