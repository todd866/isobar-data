"""The small newline-delimited JSON-RPC MCP server used by the archive daemon.

This deliberately has no MCP SDK dependency: the archive is a read-only, local
stdio service and only needs the initialize, tools/list, and tools/call methods.
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Iterable, Mapping
from typing import Any

from isobar_data.normalise import json_ready


PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _schema(properties: Mapping[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _number(description: str) -> dict[str, Any]:
    return {"type": "number", "description": description}


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_runs",
        "description": "List retained model runs and their available forecast hours.",
        "inputSchema": _schema({}),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "sample",
        "description": "Bilinear grid sampling; adjacent listed hours only. Stored names include mslp, t2m, t850, u10, v10, tp (accumulation since run). wind10 derives kt and direction from u/v.",
        "inputSchema": _schema(
            {"var": _string("Variable name as stored"), "lat": _number("Latitude in degrees"),
             "lon": _number("Longitude in degrees"), "valid_time_utc": _string("ISO-8601 UTC time"),
             "run": _string("Optional model run identifier"), "family": _string("Optional grid family")},
            ("var", "lat", "lon", "valid_time_utc"),
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "profile",
        "description": "Return pressure-level values at a coordinate and valid UTC time.",
        "inputSchema": _schema(
            {"lat": _number("Latitude in degrees"), "lon": _number("Longitude in degrees"),
             "valid_time_utc": _string("ISO-8601 UTC time"), "run": _string("Optional model run identifier"),
             "family": _string("Optional grid family")},
            ("lat", "lon", "valid_time_utc"),
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "section",
        "description": "Sample a variable along a great-circle route.",
        "inputSchema": _schema(
            {"points": {"type": "array", "minItems": 2, "maxItems": 1000, "items": {"type": "array", "prefixItems": [_number("Latitude"), _number("Longitude")], "minItems": 2, "maxItems": 2}},
             "valid_time_utc": _string("ISO-8601 UTC time"), "var": _string("Variable name as stored"),
             "samples": {"type": "integer", "minimum": 2, "maximum": 2000}, "png": {"type": "boolean"},
             "family": _string("Optional grid family")},
            ("points", "valid_time_utc", "var"),
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "run_change",
        "description": "Compare a valid time across all retained model runs.",
        "inputSchema": _schema(
            {"var": _string("Variable name as stored"), "lat": _number("Latitude in degrees"),
             "lon": _number("Longitude in degrees"), "valid_time_utc": _string("ISO-8601 UTC time"),
             "family": _string("Optional grid family")},
            ("var", "lat", "lon", "valid_time_utc"),
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "forecast_vs_observed",
        "description": "Compare archived model surface values with station observations.",
        "inputSchema": _schema(
            {"station": _string("Station identifier"), "icao": _string("ICAO identifier"),
             "hours_back": {"type": "integer", "minimum": 1, "maximum": 720}},
        ),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "facts",
        "description": "Return archived aerodrome and runway facts for an ICAO.",
        "inputSchema": _schema({"icao": _string("ICAO identifier")}, ("icao",)),
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
]


class MCPServer:
    """Dispatch MCP requests to an ArchiveStore-like object."""

    def __init__(self, store: Any):
        self.store = store
        self.initialized = False
        self.version = PROTOCOL_VERSIONS[0]

    def _call(self, name: str, args: Mapping[str, Any]) -> Any:
        return getattr(self.store, name)(**args)

    @staticmethod
    def _validate_args(name: str, args: Mapping[str, Any]) -> None:
        schema = next(tool['inputSchema'] for tool in TOOLS if tool['name'] == name)
        extra = set(args) - set(schema['properties'])
        if extra:
            raise ValueError('unknown argument(s): ' + ', '.join(sorted(extra)))
        required = schema['required']
        missing = [key for key in required if key not in args]
        if missing:
            raise ValueError("missing required argument(s): " + ", ".join(missing))
        for key in ("lat", "lon"):
            if key in args and (isinstance(args[key], bool) or not isinstance(args[key], (int, float)) or not math.isfinite(args[key])):
                raise ValueError(f"{key} must be a finite number")
        for key in ("var", "valid_time_utc", "run", "family", "station", "icao"):
            if key in args and not isinstance(args[key], str):
                raise ValueError(f"{key} must be a string")
        if "samples" in args and (isinstance(args["samples"], bool) or not isinstance(args["samples"], int) or not 2 <= args["samples"] <= 2000):
            raise ValueError("samples must be an integer from 2 through 2000")
        if "hours_back" in args and (isinstance(args["hours_back"], bool) or not isinstance(args["hours_back"], int) or not 1 <= args["hours_back"] <= 720):
            raise ValueError("hours_back must be an integer from 1 through 720")
        if "png" in args and not isinstance(args["png"], bool):
            raise ValueError("png must be a boolean")
        if name == "forecast_vs_observed" and (("station" in args) == ("icao" in args)):
            raise ValueError("supply exactly one of station or icao")
        if name == "section":
            points = args["points"]
            if not isinstance(points, list) or not 2 <= len(points) <= 1000:
                raise ValueError("points must contain 2 through 1000 [lat, lon] pairs")
            for point in points:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError("each point must be a [lat, lon] pair")
                for coordinate in point:
                    if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)) or not math.isfinite(coordinate):
                        raise ValueError("point coordinates must be finite numbers")

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        """Handle one request; return None for notifications."""
        if not isinstance(request, Mapping):
            return self._error(None, -32600, "Invalid Request")
        if "id" in request and type(request["id"]) not in (str, int):
            return self._error(None, -32600, "Invalid Request id")
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0":
            return self._error(request_id, -32600, "Invalid Request")
        method = request.get("method")
        if not isinstance(method, str):
            return self._error(request_id, -32600, "Invalid Request")
        is_notification = "id" not in request
        if is_notification:
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
                result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "isobar-archive", "version": "0.1.0"},
                          "instructions": "Read-only retained weather archive tools."}
                self.initialized = True
                self.version = version
            elif method == "tools/list":
                if not self.initialized:
                    raise ValueError("initialize must be called first")
                result = {"tools": TOOLS}
            elif method == "tools/call":
                if not self.initialized:
                    raise ValueError("initialize must be called first")
                name = params.get("name")
                args = params.get("arguments", {})
                if not isinstance(name, str) or not isinstance(args, Mapping):
                    raise ValueError("tools/call requires a name and object arguments")
                if name not in {tool["name"] for tool in TOOLS}:
                    raise ValueError(f"unknown tool: {name}")
                self._validate_args(name, args)
                try:
                    value = json_ready(self._call(name, args))
                except (TypeError, ValueError) as exc:
                    return self._error(request_id, -32602, str(exc))
                except Exception as exc:
                    result = {"content": [{"type": "text", "text": json.dumps({"value": None, "reason": str(exc)})}], "isError": True}
                else:
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, allow_nan=False)}]}
                    if self.version == "2025-06-18":
                        result["structuredContent"] = value
            else:
                return self._error(request_id, -32601, f"Method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            return self._error(request_id, -32602, str(exc))
        except Exception as exc:  # backend failures are returned, keeping the daemon alive
            return self._error(request_id, -32000, str(exc))

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def run_stdio(self, stdin: Any = sys.stdin, stdout: Any = sys.stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON number")))
                response = self.handle(request)
            except (ValueError, TypeError, AttributeError, RecursionError):
                response = self._error(None, -32700, "Parse error")
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
                stdout.flush()
