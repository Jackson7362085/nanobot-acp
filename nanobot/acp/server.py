"""JSON-RPC stdio server for ACP."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from typing import Any

from . import schema
from .agent import AcpAgent


class AcpJsonRpcServer:
    """Line-delimited JSON-RPC 2.0 server for ACP."""

    def __init__(self, agent: AcpAgent):
        self.agent = agent
        self._next_request_id = 10_000
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()
        self._line_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    @staticmethod
    def _force_utf8_stdio() -> None:
        """Best-effort stdio reconfigure for stable ACP transport encoding."""
        streams = (
            (sys.stdin, "strict"),
            (sys.stdout, "strict"),
            (sys.stderr, "backslashreplace"),
        )
        for stream, errors in streams:
            try:
                stream.reconfigure(encoding="utf-8", errors=errors)  # type: ignore[attr-defined]
            except Exception:
                # Some wrapped streams may not support reconfigure().
                pass

    async def run_stdio(self) -> None:
        self._force_utf8_stdio()
        loop = asyncio.get_running_loop()

        def _stdin_reader() -> None:
            while True:
                raw = sys.stdin.readline()
                if raw == "":
                    loop.call_soon_threadsafe(self._line_queue.put_nowait, None)
                    return
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    loop.call_soon_threadsafe(
                        asyncio.create_task,
                        self._send(
                            {
                                "jsonrpc": schema.JSONRPC_VERSION,
                                "id": None,
                                "error": {
                                    "code": schema.PARSE_ERROR,
                                    "message": "Parse error",
                                },
                            }
                        ),
                    )
                    continue
                loop.call_soon_threadsafe(self._line_queue.put_nowait, msg)

        threading.Thread(target=_stdin_reader, daemon=True).start()

        while True:
            message = await self._line_queue.get()
            if message is None:
                break
            # Handle requests concurrently so agent->client RPC responses
            # can be processed while a long-running request is in flight.
            if isinstance(message, dict) and "method" in message and "id" in message:
                asyncio.create_task(self._handle_request(message))
            else:
                await self.handle_message(message)

    async def handle_message(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            await self._send(
                {
                    "jsonrpc": schema.JSONRPC_VERSION,
                    "id": None,
                    "error": {
                        "code": schema.INVALID_REQUEST,
                        "message": "Invalid request",
                    },
                }
            )
            return

        # Response to an outstanding agent->client request.
        if "id" in message and ("result" in message or "error" in message):
            req_id = message.get("id")
            if isinstance(req_id, int) and req_id in self._pending_requests:
                fut = self._pending_requests.pop(req_id)
                if not fut.done():
                    fut.set_result(message)
            return

        if "method" in message and "id" in message:
            await self._handle_request(message)
            return

        if "method" in message:
            await self._handle_notification(message)
            return

    async def _handle_request(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params", {})
        try:
            result = await self.dispatch(method, params)
            await self._send(
                {
                    "jsonrpc": schema.JSONRPC_VERSION,
                    "id": msg_id,
                    "result": result,
                }
            )
        except ValueError as e:
            await self._send(
                {
                    "jsonrpc": schema.JSONRPC_VERSION,
                    "id": msg_id,
                    "error": {
                        "code": schema.INVALID_PARAMS,
                        "message": "Invalid params",
                        "data": {"details": str(e)},
                    },
                }
            )
        except NotImplementedError as e:
            await self._send(
                {
                    "jsonrpc": schema.JSONRPC_VERSION,
                    "id": msg_id,
                    "error": {
                        "code": schema.METHOD_NOT_FOUND,
                        "message": "Method not found",
                        "data": {"details": str(e)},
                    },
                }
            )
        except Exception as e:
            await self._send(
                {
                    "jsonrpc": schema.JSONRPC_VERSION,
                    "id": msg_id,
                    "error": {
                        "code": schema.INTERNAL_ERROR,
                        "message": "Internal error",
                        "data": {"details": str(e)},
                    },
                }
            )

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params", {})
        if method == "session/cancel":
            session_id = params.get("sessionId")
            if isinstance(session_id, str) and session_id:
                await self.agent.cancel(session_id)
            return
        # Ignore unknown notifications in first stage.
        _ = params

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == schema.AGENT_METHODS["initialize"]:
            return await self.agent.initialize(params)
        if method == schema.AGENT_METHODS["authenticate"]:
            return await self.agent.authenticate(params)
        if method == schema.AGENT_METHODS["session_new"]:
            return await self.agent.new_session(params)
        if method == schema.AGENT_METHODS["session_load"]:
            return await self.agent.load_session(params)
        if method == schema.AGENT_METHODS["session_list"]:
            return await self.agent.list_sessions(params)
        if method == schema.AGENT_METHODS["session_prompt"]:
            return await self.agent.prompt(params, send_update=self.send_session_update)
        if method == schema.AGENT_METHODS["session_set_mode"]:
            return await self.agent.set_mode(params)
        if method == schema.AGENT_METHODS["session_set_model"]:
            return await self.agent.set_model(params)
        raise NotImplementedError(method)

    async def send_session_update(self, params: dict[str, Any]) -> None:
        await self._send(
            {
                "jsonrpc": schema.JSONRPC_VERSION,
                "method": schema.CLIENT_METHODS["session_update"],
                "params": params,
            }
        )

    async def send_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = fut
        await self._send(
            {
                "jsonrpc": schema.JSONRPC_VERSION,
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        try:
            message = await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
            self._pending_requests.pop(request_id, None)
            raise
        if "error" in message:
            err = message["error"]
            details = ""
            if isinstance(err, dict):
                details = str(err.get("message", "request failed"))
            raise RuntimeError(details or "request failed")
        return message.get("result")

    async def request_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        result = await self.send_request(
            schema.CLIENT_METHODS["session_request_permission"],
            params,
            timeout=120.0,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Invalid permission response")
        return result

    async def read_text_file(self, params: dict[str, Any]) -> dict[str, Any]:
        result = await self.send_request(
            schema.CLIENT_METHODS["fs_read_text_file"],
            params,
            timeout=60.0,
        )
        if not isinstance(result, dict):
            raise RuntimeError("Invalid read_text_file response")
        return result

    async def write_text_file(self, params: dict[str, Any]) -> Any:
        return await self.send_request(
            schema.CLIENT_METHODS["fs_write_text_file"],
            params,
            timeout=60.0,
        )

    async def _send(self, payload: dict[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False)
        async with self._send_lock:
            sys.stdout.write(content + "\n")
            sys.stdout.flush()
