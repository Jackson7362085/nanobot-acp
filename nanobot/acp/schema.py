"""ACP method names and basic protocol constants."""

PROTOCOL_VERSION = 1
JSONRPC_VERSION = "2.0"

AGENT_METHODS = {
    "initialize": "initialize",
    "authenticate": "authenticate",
    "session_new": "session/new",
    "session_load": "session/load",
    "session_list": "session/list",
    "session_prompt": "session/prompt",
    "session_set_mode": "session/set_mode",
    "session_set_model": "session/set_model",
}

CLIENT_METHODS = {
    "session_update": "session/update",
    "session_request_permission": "session/request_permission",
    "fs_read_text_file": "fs/read_text_file",
    "fs_write_text_file": "fs/write_text_file",
    "authenticate_update": "authenticate/update",
}

# JSON-RPC 2.0 standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
