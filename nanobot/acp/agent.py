"""ACP agent adapter on top of nanobot AgentLoop."""

from __future__ import annotations

import json
import difflib
import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from nanobot import __version__
from . import schema
from nanobot.agent.loop import AgentLoop
from nanobot.session.manager import SessionManager

SessionUpdateSender = Callable[[dict[str, Any]], Awaitable[None]]


class AcpClientBridge:
    """Subset of client-side ACP methods agent can call."""

    async def request_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def read_text_file(self, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def write_text_file(self, params: dict[str, Any]) -> Any:
        raise NotImplementedError


@dataclass
class SessionCatalogItem:
    """Persistent ACP session metadata."""

    session_id: str
    key: str
    cwd: str
    mode_id: str
    model_id: str
    created_at: str
    updated_at: str


@dataclass
class SessionCatalog:
    """Simple JSON-backed catalog mapping ACP session ids to nanobot keys."""

    path: Path
    items: dict[str, SessionCatalogItem] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "SessionCatalog":
        if not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items: dict[str, SessionCatalogItem] = {}
            for raw in data.get("items", []):
                item = SessionCatalogItem(
                    session_id=raw["session_id"],
                    key=raw["key"],
                    cwd=raw["cwd"],
                    mode_id=raw.get("mode_id", "default"),
                    model_id=raw.get("model_id", ""),
                    created_at=raw["created_at"],
                    updated_at=raw["updated_at"],
                )
                items[item.session_id] = item
            return cls(path=path, items=items)
        except Exception:
            return cls(path=path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "items": [
                {
                    "session_id": item.session_id,
                    "key": item.key,
                    "cwd": item.cwd,
                    "mode_id": item.mode_id,
                    "model_id": item.model_id,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
                for item in self.items.values()
            ]
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class AcpAgent:
    """Implements ACP methods using nanobot's AgentLoop."""

    def __init__(
        self,
        agent_loop: AgentLoop,
        sessions: SessionManager,
        *,
        model: str,
        workspace: Path,
        catalog_path: Path,
    ):
        self.agent_loop = agent_loop
        self.sessions = sessions
        self.model = model
        self.workspace = workspace
        self.catalog = SessionCatalog.load(catalog_path)
        self.client_capabilities: dict[str, Any] | None = None
        self.client_bridge: AcpClientBridge | None = None
        self._sticky_permissions: dict[str, dict[str, str]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._active_prompt_tasks: dict[str, asyncio.Task[Any]] = {}

    def bind_client_bridge(self, bridge: AcpClientBridge) -> None:
        self.client_bridge = bridge

    async def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        self.client_capabilities = params.get("clientCapabilities", {})
        return {
            "protocolVersion": schema.PROTOCOL_VERSION,
            "agentInfo": {
                "name": "nanobot",
                "title": "nanobot",
                "version": __version__,
            },
            "authMethods": [],
            "modes": {
                "currentModeId": "default",
                "availableModes": [
                    {"id": "plan", "name": "Plan", "description": "Plan only mode"},
                    {"id": "default", "name": "Default", "description": "Prompt for approvals"},
                    {"id": "auto-edit", "name": "Auto Edit", "description": "Auto-approve edits"},
                    {"id": "yolo", "name": "YOLO", "description": "Auto-approve all tools"},
                ],
            },
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": False,
                },
            },
        }

    async def authenticate(self, _params: dict[str, Any]) -> None:
        # First stage: no auth handshake required by nanobot.
        return None

    async def cancel(self, session_id: str) -> None:
        event = self._cancel_events.get(session_id)
        if event:
            event.set()
        task = self._active_prompt_tasks.get(session_id)
        if task:
            task.cancel()

    async def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = str(params["cwd"])
        session_id = str(uuid4())
        key = f"acp:{session_id}"
        now = datetime.now().isoformat()

        session = self.sessions.get_or_create(key)
        self.sessions.save(session)

        self.catalog.items[session_id] = SessionCatalogItem(
            session_id=session_id,
            key=key,
            cwd=cwd,
            mode_id="default",
            model_id=self.model,
            created_at=now,
            updated_at=now,
        )
        self.catalog.save()

        return {
            "sessionId": session_id,
            "models": {
                "currentModelId": self.model,
                "availableModels": [
                    {
                        "modelId": self.model,
                        "name": self.model,
                        "description": "nanobot configured default model",
                        "_meta": None,
                    }
                ],
                "_meta": None,
            },
        }

    async def set_mode(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params["sessionId"])
        mode_id = str(params["modeId"])
        if mode_id not in {"plan", "default", "auto-edit", "yolo"}:
            raise ValueError(f"Unsupported mode: {mode_id}")
        item = self.catalog.items.get(session_id)
        if item is None:
            raise ValueError(f"Session not found for id: {session_id}")
        item.mode_id = mode_id
        item.updated_at = datetime.now().isoformat()
        self.catalog.save()
        return {"modeId": mode_id}

    async def set_model(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params["sessionId"])
        model_id = str(params["modelId"])
        if not model_id:
            raise ValueError("modelId cannot be empty")
        item = self.catalog.items.get(session_id)
        if item is None:
            raise ValueError(f"Session not found for id: {session_id}")
        item.model_id = model_id
        item.updated_at = datetime.now().isoformat()
        self.catalog.save()
        return {"modelId": model_id}

    async def load_session(self, params: dict[str, Any]) -> None:
        session_id = str(params["sessionId"])
        item = self.catalog.items.get(session_id)
        if item is None:
            raise ValueError(f"Session not found for id: {session_id}")
        cwd = str(params["cwd"])
        if item.cwd != cwd:
            raise ValueError(f"Session cwd mismatch for id: {session_id}")
        self.sessions.get_or_create(item.key)
        return None

    async def list_sessions(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = str(params["cwd"])
        cursor = int(params.get("cursor", 0) or 0)
        size = int(params.get("size", 20) or 20)

        rows = [item for item in self.catalog.items.values() if item.cwd == cwd]
        rows.sort(key=lambda x: x.updated_at, reverse=True)

        page = rows[cursor : cursor + size]
        result_items = []
        for item in page:
            session = self.sessions.get_or_create(item.key)
            prompt = ""
            for msg in session.messages:
                if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                    prompt = msg["content"][:200]
                    break

            # filePath uses nanobot's persisted session file path.
            file_path = str(self.sessions._get_session_path(item.key))  # noqa: SLF001
            result_items.append(
                {
                    "sessionId": item.session_id,
                    "cwd": item.cwd,
                    "startTime": item.created_at,
                    "mtime": int(session.updated_at.timestamp() * 1000),
                    "prompt": prompt,
                    "gitBranch": "",
                    "filePath": file_path,
                    "messageCount": len(session.messages),
                }
            )

        next_cursor = cursor + size if (cursor + size) < len(rows) else None
        return {
            "items": result_items,
            "hasMore": next_cursor is not None,
            "nextCursor": next_cursor,
        }

    async def prompt(
        self,
        params: dict[str, Any],
        *,
        send_update: SessionUpdateSender,
    ) -> dict[str, Any]:
        session_id = str(params["sessionId"])
        item = self.catalog.items.get(session_id)
        if item is None:
            raise ValueError(f"Session not found for id: {session_id}")

        prompt_text = self._prompt_to_text(params.get("prompt", []))
        if not prompt_text:
            raise ValueError("Prompt content is empty")

        sticky_for_session = self._sticky_permissions.setdefault(session_id, {})
        cancel_event = self._cancel_events.setdefault(session_id, asyncio.Event())
        cancel_event.clear()
        self._active_prompt_tasks[session_id] = asyncio.current_task()

        async def on_tool_approval(
            tool_name: str,
            tool_args: dict[str, Any],
        ) -> tuple[bool, str | None]:
            mode_id = item.mode_id
            if mode_id == "yolo":
                return True, None
            sticky = sticky_for_session.get(tool_name)
            if sticky == "allow":
                return True, None
            if sticky == "reject":
                return False, "Rejected by previous always-deny choice"
            if self.client_bridge is None:
                # No ACP client bridge available: preserve momentum in first-stage hosts.
                return True, None

            option_map = {
                "allow_once": "allow_once",
                "allow_always": "allow_always",
                "reject_once": "reject_once",
                "reject_always": "reject_always",
            }
            tool_call_id = str(uuid4())
            params = {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": tool_call_id,
                    "title": f"{tool_name}({json.dumps(tool_args, ensure_ascii=False)[:160]})",
                    "kind": self._tool_kind(tool_name),
                    "status": "pending",
                    "rawInput": tool_args,
                },
                "options": [
                    {"optionId": option_map["allow_once"], "name": "Allow once", "kind": "allow_once"},
                    {"optionId": option_map["allow_always"], "name": "Always allow", "kind": "allow_always"},
                    {"optionId": option_map["reject_once"], "name": "Reject once", "kind": "reject_once"},
                    {"optionId": option_map["reject_always"], "name": "Always reject", "kind": "reject_always"},
                ],
            }
            try:
                result = await self.client_bridge.request_permission(params)
            except Exception as e:
                return False, f"Permission request failed: {e}"

            outcome = result.get("outcome")
            if not isinstance(outcome, dict):
                return False, "Invalid permission outcome"
            if outcome.get("outcome") == "cancelled":
                return False, "Cancelled by ACP client"
            if outcome.get("outcome") != "selected":
                return False, "Unsupported permission outcome"
            option_id = str(outcome.get("optionId", ""))
            if option_id == option_map["allow_once"]:
                return True, None
            if option_id == option_map["allow_always"]:
                sticky_for_session[tool_name] = "allow"
                return True, None
            if option_id == option_map["reject_once"]:
                return False, "Rejected by ACP client"
            if option_id == option_map["reject_always"]:
                sticky_for_session[tool_name] = "reject"
                return False, "Rejected by ACP client"
            return False, "Unknown permission option"

        async def on_progress(content: str, *, tool_hint: bool = False) -> None:
            update = {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": content},
                },
            }
            if tool_hint:
                update["update"]["_meta"] = {"toolName": "tool_hint"}
            await send_update(update)

        async def on_thought(content: str) -> None:
            if not content.strip():
                return
            await send_update(
                {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": content},
                    },
                }
            )

        async def on_tool_event(event: dict[str, Any]) -> None:
            tool_call_id = str(event.get("tool_call_id", ""))
            tool_name = str(event.get("name", "tool"))
            args = event.get("arguments", {})
            status = str(event.get("status", "in_progress"))
            kind = self._tool_kind(tool_name)
            title = f"{tool_name}({json.dumps(args, ensure_ascii=False)[:160]})"
            if event.get("phase") == "start":
                await send_update(
                    {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": tool_call_id,
                            "title": title,
                            "kind": kind,
                            "status": status,
                            "rawInput": args,
                        },
                    }
                )
                return
            result = str(event.get("result", ""))
            content = []
            if result:
                content.append(
                    {
                        "type": "content",
                        "content": {"type": "text", "text": result[:2000]},
                    }
                )
            await send_update(
                {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": tool_call_id,
                        "title": title,
                        "kind": kind,
                        "status": status,
                        "rawInput": args,
                        "rawOutput": result,
                        "content": content or None,
                    },
                }
            )

        async def on_tool_execute(
            tool_name: str,
            tool_args: dict[str, Any],
        ) -> tuple[bool, str]:
            if self.client_bridge is None:
                return False, ""
            fs_cap = (self.client_capabilities or {}).get("fs") or {}
            can_read = bool(fs_cap.get("readTextFile"))
            can_write = bool(fs_cap.get("writeTextFile"))

            try:
                if tool_name == "read_file" and can_read:
                    path = str(tool_args.get("path", ""))
                    if not path:
                        return True, "Error: Missing required parameter: path"
                    result = await self.client_bridge.read_text_file(
                        {
                            "sessionId": session_id,
                            "path": path,
                            "line": None,
                            "limit": None,
                        }
                    )
                    content = result.get("content", "")
                    return True, str(content)

                if tool_name == "write_file" and can_write:
                    path = str(tool_args.get("path", ""))
                    content = str(tool_args.get("content", ""))
                    if not path:
                        return True, "Error: Missing required parameter: path"
                    await self.client_bridge.write_text_file(
                        {
                            "sessionId": session_id,
                            "path": path,
                            "content": content,
                        }
                    )
                    return True, f"Successfully wrote {len(content)} bytes to {path}"

                if tool_name == "edit_file" and can_read and can_write:
                    path = str(tool_args.get("path", ""))
                    old_text = str(tool_args.get("old_text", ""))
                    new_text = str(tool_args.get("new_text", ""))
                    if not path:
                        return True, "Error: Missing required parameter: path"
                    if old_text == "":
                        return True, "Error: Missing required parameter: old_text"
                    read = await self.client_bridge.read_text_file(
                        {
                            "sessionId": session_id,
                            "path": path,
                            "line": None,
                            "limit": None,
                        }
                    )
                    content = str(read.get("content", ""))
                    if old_text not in content:
                        # Keep behavior close to native edit_file tool.
                        lines = content.splitlines(keepends=True)
                        old_lines = old_text.splitlines(keepends=True)
                        window = len(old_lines)
                        best_ratio, best_start = 0.0, 0
                        for i in range(max(1, len(lines) - window + 1)):
                            ratio = difflib.SequenceMatcher(
                                None,
                                old_lines,
                                lines[i : i + window],
                            ).ratio()
                            if ratio > best_ratio:
                                best_ratio, best_start = ratio, i
                        if best_ratio > 0.5:
                            diff = "\n".join(
                                difflib.unified_diff(
                                    old_lines,
                                    lines[best_start : best_start + window],
                                    fromfile="old_text (provided)",
                                    tofile=f"{path} (actual, line {best_start + 1})",
                                    lineterm="",
                                )
                            )
                            return True, (
                                f"Error: old_text not found in {path}.\n"
                                f"Best match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
                            )
                        return True, f"Error: old_text not found in {path}. No similar text found. Verify the file content."
                    count = content.count(old_text)
                    if count > 1:
                        return True, f"Warning: old_text appears {count} times. Please provide more context to make it unique."
                    updated = content.replace(old_text, new_text, 1)
                    await self.client_bridge.write_text_file(
                        {
                            "sessionId": session_id,
                            "path": path,
                            "content": updated,
                        }
                    )
                    return True, f"Successfully edited {path}"
            except Exception as e:
                return True, f"Error: ACP fs operation failed: {e}"

            return False, ""

        try:
            final_content = await self.agent_loop.process_direct(
                prompt_text,
                session_key=item.key,
                channel="acp",
                chat_id=session_id,
                on_progress=on_progress,
                on_tool_approval=on_tool_approval,
                on_tool_event=on_tool_event,
                on_thought=on_thought,
                should_cancel=cancel_event.is_set,
                on_tool_execute=on_tool_execute,
            )
        except asyncio.CancelledError:
            return {"stopReason": "cancelled"}
        finally:
            self._active_prompt_tasks.pop(session_id, None)

        # Ensure at least one final chunk is sent for clients that only render chunks.
        if final_content:
            await send_update(
                {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": final_content},
                    },
                }
            )

        item.updated_at = datetime.now().isoformat()
        self.catalog.save()
        return {"stopReason": "end_turn"}

    @staticmethod
    def _tool_kind(tool_name: str) -> str:
        if tool_name in {"read_file", "list_dir"}:
            return "read"
        if tool_name in {"write_file", "edit_file"}:
            return "edit"
        if tool_name in {"web_search"}:
            return "search"
        if tool_name in {"web_fetch"}:
            return "fetch"
        if tool_name in {"exec"}:
            return "execute"
        return "other"

    @staticmethod
    def _prompt_to_text(blocks: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
            elif btype == "resource_link":
                uri = block.get("uri")
                if isinstance(uri, str) and uri.strip():
                    parts.append(uri)
        return "\n".join(parts).strip()
