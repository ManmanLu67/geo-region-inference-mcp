#!/usr/bin/env python3
"""
geo-region-inference MCP adapter.

Thin stdio JSON-RPC surface: tool registration, argument mapping, core calls,
error wrapping. Business logic lives in geo_core.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from mcp_types import (
    Implementation,
    InitializeResult,
    ListToolsResult,
    ServerCapabilities,
    Tool,
    ToolsCapability,
)
from mcp_types.jsonrpc import (
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    JSONRPCError,
    JSONRPCResponse,
)
from mcp_types.methods import serialize_server_result
from mcp_types.version import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    LATEST_HANDSHAKE_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)

from geo_core import (
    SERVER_VERSION,
    analyze_regions,
    calculate_geometry,
    check_api_status,
    close_http,
    prepare_gov_web_search,
    search_project_evidence,
    validate_result,
)

SERVER_NAME = "geo-region-inference"


def log(msg: str) -> None:
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


def json_result(payload: Any, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload if isinstance(payload, dict) else {"result": payload},
        "isError": is_error,
    }


def ok(value: Any) -> dict[str, Any]:
    return json_result(value, False)


def err(message: str, details: Any | None = None) -> dict[str, Any]:
    payload = {"error": message}
    if details is not None:
        payload["details"] = details
    return json_result(payload, True)


TOOLS = {
    "analyze_regions": {
        "description": "Batch-process a GeoJSON/FeatureCollection from inline geojson or input_path: geometry stats plus concurrent AMap/Baidu/OSM evidence. When search_projects is true, project-keyword search already covers the POI channel; search_poi does not add a second query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "geojson": {"type": "object", "description": "Inline GeoJSON FeatureCollection, Feature, or geometry."},
                "input_path": {
                    "type": "string",
                    "description": "Local .json/.geojson file path (preferred for large datasets). Mutually exclusive with geojson.",
                },
                "search_projects": {
                    "type": "boolean",
                    "default": True,
                    "description": "Project-keyword POI search. When true, search_poi adds no extra request.",
                },
                "search_poi": {
                    "type": "boolean",
                    "default": True,
                    "description": "General nearby search only when search_projects is false. Does not disable project search.",
                },
                "expand_radius_if_needed": {"type": "boolean", "default": True},
                "max_workers": {"type": "integer", "default": 4, "minimum": 1, "maximum": 4},
                "output_path": {
                    "type": "string",
                    "description": "Write the full result JSON to this .json path; the tool return is a summary without sources.items/places/roads.",
                },
            },
        },
    },
    "calculate_geometry": {
        "description": "Compute deterministic geometry statistics for one Feature or a FeatureCollection (shared scan/fail-fast/GEOMETRY_INVALID with analyze_regions; no POI).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "geojson": {"type": "object"},
                "input_path": {"type": "string", "description": "Local .json/.geojson file path. Mutually exclusive with geojson."},
            },
        },
    },
    "search_project_evidence": {
        "description": "Search project-oriented evidence around one WGS84 point. AMap, Baidu, and OSM queries run concurrently; if direct project evidence is absent, the search can automatically expand once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "radius_m": {"type": "number", "default": 300},
                "expand_if_empty": {"type": "boolean", "default": True},
            },
            "required": ["lat", "lon"],
        },
    },
    "validate_result": {
        "description": "Validate one feature inference result against the Skill's output and project-confidence rules.",
        "inputSchema": {"type": "object", "properties": {"result": {"type": "object"}}, "required": ["result"]},
    },
    "prepare_gov_web_search": {
        "description": "After analyze_regions: build a four-round government web search plan for features without direct project_evidence and with district-level admin context. No HTTP; Agent runs web_search/web_fetch. Pass the full analyze_regions body, or analyze_result_path to a file written via output_path. Summaries (output_written) are rejected.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "analyze_result": {
                    "type": "object",
                    "description": "Full analyze_regions response body (features with sources/places/roads). Mutually exclusive with analyze_result_path.",
                },
                "analyze_result_path": {
                    "type": "string",
                    "description": "Path to the full JSON file written by analyze_regions(output_path=...). Mutually exclusive with analyze_result.",
                },
            },
        },
    },
    "check_api_status": {
        "description": (
            "Probe AMap/Baidu API key health. single (default): key validity only — cannot predict concurrent "
            "rate limits for bulk analyze_regions. burst: diagnose CUQPS with incremental concurrent probes; "
            "stops on first rate limit; consumes small quota."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Probe latitude (default Beijing)."},
                "lon": {"type": "number", "description": "Probe longitude (default Beijing)."},
                "probe_mode": {
                    "type": "string",
                    "enum": ["single", "burst"],
                    "default": "single",
                    "description": "single=验活 Key；burst=诊断 CUQPS 并发上限（首次限流即停）。",
                },
            },
        },
    },
}


def handle_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "analyze_regions":
        try:
            return ok(
                analyze_regions(
                    args.get("geojson"),
                    input_path=args.get("input_path"),
                    search_projects=bool(args.get("search_projects", True)),
                    search_poi=bool(args.get("search_poi", True)),
                    expand_radius_if_needed=bool(args.get("expand_radius_if_needed", True)),
                    max_workers=int(args.get("max_workers", 4)),
                    output_path=args.get("output_path") or None,
                )
            )
        except ValueError as e:
            return err(str(e))
    if name == "calculate_geometry":
        try:
            return ok(
                calculate_geometry(
                    geojson=args.get("geojson"),
                    input_path=args.get("input_path"),
                )
            )
        except ValueError as e:
            return err(str(e))
    if name == "search_project_evidence":
        try:
            return ok(
                search_project_evidence(
                    float(args["lat"]),
                    float(args["lon"]),
                    radius_m=float(args.get("radius_m", 300)),
                    expand_if_empty=bool(args.get("expand_if_empty", True)),
                )
            )
        except (ValueError, KeyError) as e:
            return err(str(e))
    if name == "validate_result":
        return ok(validate_result(args["result"]))
    if name == "prepare_gov_web_search":
        try:
            return ok(
                prepare_gov_web_search(
                    args.get("analyze_result"),
                    analyze_result_path=args.get("analyze_result_path"),
                )
            )
        except ValueError as e:
            return err(str(e))
    if name == "check_api_status":
        return ok(
            check_api_status(
                lat=args.get("lat"),
                lon=args.get("lon"),
                probe_mode=str(args.get("probe_mode", "single")),
            )
        )
    return err(f"Unknown tool: {name}")


def capabilities() -> dict[str, Any]:
    return ServerCapabilities(tools=ToolsCapability(list_changed=False)).model_dump(
        by_alias=True, exclude_none=True
    )


def response(req_id: Any, result: Any) -> dict[str, Any]:
    return JSONRPCResponse(jsonrpc="2.0", id=req_id, result=result).model_dump(
        by_alias=True, exclude_none=True
    )


def error_response(req_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    err_body: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err_body["data"] = data
    return JSONRPCError(jsonrpc="2.0", id=req_id, error=err_body).model_dump(
        by_alias=True, exclude_none=False
    )


def negotiate_initialize(requested: str | None) -> str:
    if requested in HANDSHAKE_PROTOCOL_VERSIONS:
        return requested
    return LATEST_HANDSHAKE_VERSION


def handle_rpc(req: dict[str, Any]) -> dict[str, Any] | None:
    if "id" not in req:
        return None
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "server/discover":
        # DiscoverResult in mcp-types has no serverInfo; keep that field by hand.
        result = {
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": capabilities(),
            "instructions": "Use analyze_regions for normal work: it batches geometry + project-oriented online evidence and returns compact evidence for semantic inference.",
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
        return response(req_id, result)
    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        negotiated = negotiate_initialize(str(requested) if requested else None)
        init = InitializeResult(
            protocol_version=negotiated,
            capabilities=ServerCapabilities(tools=ToolsCapability(list_changed=False)),
            server_info=Implementation(name=SERVER_NAME, version=SERVER_VERSION),
            instructions="Use analyze_regions for normal work.",
        )
        result = serialize_server_result(
            "initialize", negotiated, init.model_dump(by_alias=True)
        )
        return response(req_id, result)
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        listed = ListToolsResult(
            tools=[
                Tool(name=n, description=v["description"], input_schema=v["inputSchema"])
                for n, v in TOOLS.items()
            ]
        )
        result = serialize_server_result(
            "tools/list", LATEST_HANDSHAKE_VERSION, listed.model_dump(by_alias=True)
        )
        return response(req_id, result)
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        return response(req_id, handle_tool(name, args))
    return error_response(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")


def main() -> None:
    log(f"started v{SERVER_VERSION}; stdio transport")
    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
            except json.JSONDecodeError as e:
                print(json.dumps(error_response(None, PARSE_ERROR, "Parse error", str(e))), flush=True)
                continue
            try:
                out = handle_rpc(req)
            except Exception as e:
                log(f"error in {req.get('method')}: {e!r}")
                out = error_response(req.get("id"), -32000, "Tool/server error", str(e))
            if out is not None:
                print(json.dumps(out, ensure_ascii=False, separators=(",", ":")), flush=True)
    finally:
        close_http()


if __name__ == "__main__":
    main()
