"""Newline-delimited JSON-RPC for the aviation library search tool.

Same small protocol as the archive server: initialize, tools/list, tools/call, ping.
No MCP SDK dependency.
"""
from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any

from .search import library_search

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "library_search",
        "description": (
            "Search the local aviation and meteorology library and return up to k passages. "
            "Each passage includes its title, source, page or section when the library sent one, licence, and text."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The aviation or meteorology question."},
                "k": {"type": "integer", "minimum": 1, "description": "Maximum passages to return. Default 5."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
]


class MCPServer:
    def __init__(self):
        self.initialized = False
        self.version = PROTOCOL_VERSIONS[0]

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        if not isinstance(request, Mapping) or request.get("jsonrpc") != "2.0":
            return _error(request.get("id") if isinstance(request, Mapping) else None, -32600, "Invalid Request")
        request_id = request.get("id")
        if "id" in request and type(request_id) not in (str, int):
            return _error(None, -32600, "Invalid Request id")
        method = request.get("method")
        if not isinstance(method, str):
            return _error(request_id, -32600, "Invalid Request")
        if "id" not in request:
            return None
        try:
            params = request.get("params", {})
            if not isinstance(params, Mapping):
                raise ValueError("params must be an object")
            if method == "ping":
                result: Any = {}
            elif method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                self.initialized = True
                self.version = version
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "isobar-library", "version": "0.1.0"},
                    "instructions": "Read-only aviation and meteorology library search.",
                }
            elif method == "tools/list":
                if not self.initialized:
                    raise ValueError("initialize must be called first")
                result = {"tools": TOOLS}
            elif method == "tools/call":
                if not self.initialized:
                    raise ValueError("initialize must be called first")
                name = params.get("name")
                args = params.get("arguments", {})
                if name != "library_search" or not isinstance(args, Mapping):
                    raise ValueError("unknown tool: " + str(name))
                extra = set(args) - {"query", "k"}
                if extra:
                    raise ValueError("unknown argument(s): " + ", ".join(sorted(extra)))
                if "query" not in args:
                    raise ValueError("query is required")
                result = _tool_result(library_search(**dict(args)), self.version)
            else:
                return _error(request_id, -32601, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (TypeError, ValueError) as exc:
            return _error(request_id, -32602, str(exc))
        except Exception as exc:
            return _error(request_id, -32000, str(exc))

    def run_stdio(self, stdin: Any = sys.stdin, stdout: Any = sys.stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                response = self.handle(request)
            except (ValueError, TypeError):
                response = _error(None, -32700, "Parse error")
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


def _tool_result(value: dict, version: str) -> dict[str, Any]:
    result = {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "isError": bool(value.get("reason")) and not value.get("passages"),
    }
    if version == "2025-06-18":
        result["structuredContent"] = value
    return result


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
