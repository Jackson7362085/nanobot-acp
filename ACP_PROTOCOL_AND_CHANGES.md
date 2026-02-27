# ACP Protocol Understanding and Nanobot Changes

## 1. ACP protocol understanding (as implemented here)

This project integrates the **Agent Client Protocol (ACP)** shape used by `qwen-code`:

- Transport: **line-delimited JSON-RPC 2.0 over stdio**
- Direction:
  - Client -> Agent requests (e.g. `initialize`, `session/new`, `session/prompt`)
  - Agent -> Client notifications (e.g. `session/update`)
- Message format:
  - Request: `{"jsonrpc":"2.0","id":N,"method":"...","params":{...}}`
  - Response: `{"jsonrpc":"2.0","id":N,"result":...}` or `error`
  - Notification: `{"jsonrpc":"2.0","method":"...","params":{...}}`

Implemented first-stage methods:

- `initialize`
- `authenticate` (no-op for now)
- `session/new`
- `session/load`
- `session/list`
- `session/prompt`
- Notification out: `session/update`

Implemented second-stage methods/capabilities:

- `session/set_mode`
- `session/set_model`
- Agent -> client permission RPC: `session/request_permission`
- Server support for bidirectional client RPC:
  - `session/request_permission`
  - `fs/read_text_file`
  - `fs/write_text_file`

Implemented third-stage methods/capabilities:

- `session/cancel` now actively cancels in-flight prompt tasks
- richer `session/update` stream:
  - `agent_message_chunk`
  - `agent_thought_chunk`
  - `tool_call`
  - `tool_call_update`
- ACP FS bridge for tool execution:
  - `read_file` can use client `fs/read_text_file`
  - `write_file` can use client `fs/write_text_file`
  - `edit_file` can be emulated via client read + write

Current remaining non-goals:

- Full `authenticate/update` flow
- Full parity for advanced update families like `plan` and mode/model delta notifications
- Full IDE-specific auth UX parity with qwen-code


## 2. What was changed in this repo

### New files

- `nanobot/acp/__init__.py`
- `nanobot/acp/schema.py`
- `nanobot/acp/agent.py`
- `nanobot/acp/server.py`
- `tests/test_acp_server.py`

### Modified file

- `nanobot/cli/commands.py`

### Behavior added

1. New CLI command: `nanobot acp`
   - Starts ACP server over stdio
   - Reuses existing `Config`, `Provider`, `AgentLoop`, and `SessionManager`

2. ACP-to-nanobot mapping:
   - ACP `sessionId` is mapped to internal session key `acp:{sessionId}`
   - Session mapping is persisted to:
     - `~/.nanobot/acp/sessions.json`

3. Prompt flow:
   - `session/prompt` calls `AgentLoop.process_direct(...)`
   - Progress/final text is emitted as `session/update` with `agent_message_chunk`
   - Request response returns `{"stopReason":"end_turn"}`

4. Reliability fix:
   - ACP stdio read path now reads stdin on the same thread
   - This avoids occasional stalls seen with cross-thread `TextIO` buffering

5. Tool permission gating:
   - `AgentLoop` now supports optional per-tool approval callback
   - ACP prompt path can ask client permission before tool execution
   - Supports `allow_once` / `allow_always` / `reject_once` / `reject_always`

6. Cancellation support:
   - `session/cancel` now maps to active prompt task cancellation per ACP session


## 3. Validation performed

1. Unit tests:
   - `tests/test_acp_server.py` passed

2. Real ACP smoke:
   - `initialize` works
   - `session/new` works
   - `session/list` and `session/load` work

3. Real model prompt check (DeepSeek config):
   - ACP `session/prompt` returned `stopReason: end_turn`
   - Received `session/update` content from model (`OK.`)


## 4. How to run

Standard nanobot:

- `nanobot agent`
- `nanobot gateway`

ACP mode:

- `nanobot acp`
- with logs: `nanobot acp --logs`
