# geo-region-inference MCP

## Architecture

The Skill is the semantic workflow layer. The MCP server is the long-lived tool layer.

```text
Agent / LLM
   |
   +-- geo-region-inference Skill
   |     - evidence priorities
   |     - project-first semantic inference
   |     - confidence calibration
   |     - output schema
   |
   +-- geo-region-inference MCP (mcp_server.py)
   |     - JSON-RPC / tool orchestration
   |     - batch geometry + analyze_regions
   |
   +-- geo_clients.py
         - sync httpx client (singleton)
         - coordinate transforms
         - AMap / Baidu / OSM queries + batch Overpass
```

The normal path is one MCP call to `analyze_regions`, not one Python process per feature and per data source.

MCP evidence field reference: [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md).

## Install

Use a persistent Python environment. Do **not** install dependencies inside each task execution.

```bash
python -m venv .venv
# Windows
.venv\\Scripts\\activate
# macOS/Linux
source .venv/bin/activate
pip install -r requirements-mcp.txt
```

Runtime dependency is `httpx` (`pip install -r requirements-mcp.txt`). No official `mcp` SDK and no GIS stack. After pulling this repo, reinstall into the persistent `.venv` once.

## Run

```bash
python mcp_server.py
```

The server uses stdio and stays alive until the MCP host closes the connection.

## Configure keys and env

| Variable | Required | Purpose |
|----------|----------|---------|
| `AMAP_KEY` | No | AMap Web service key |
| `BAIDU_AK` | No | Baidu server-side AK |
| `OVERPASS_URL` | No | Overpass endpoint (default public API) |
| `HTTP_TIMEOUT_SECONDS` | No | HTTP timeout in seconds (default 12) |

Set keys in the MCP host `env` block. Do not put keys into `SKILL.md` or commit them to the repository.

## Tool contract

### `analyze_regions`

Preferred tool. Input: GeoJSON `FeatureCollection`, `Feature`, or bare geometry.

- Computes geometry for all features (max **80** features per call).
- Concurrent AMap/Baidu/OSM; OSM batched up to 10 centroids per Overpass HTTP call.
- Expands radius once if no direct project evidence (`expand_radius_if_needed`, default true).
- Returns compact evidence per feature; see [mcp_evidence_schema.md](references/mcp_evidence_schema.md).

**Parameters:**

| Param | Default | Notes |
|-------|---------|-------|
| `search_projects` | true | Project-keyword POI search |
| `search_poi` | true | General nearby search only when `search_projects` is false; does **not** add a second query when both are true |
| `expand_radius_if_needed` | true | One expansion (~2.5×, cap 5000m) |
| `max_workers` | 8 | Capped at 8; AMap/Baidu pool capped at 4 |

If both `search_projects` and `search_poi` are false, no online APIs are called.

### `calculate_geometry`

Offline deterministic geometry only. Same **80** feature limit.

### `search_project_evidence`

One-point targeted project evidence search. AMap, Baidu, and OSM run concurrently.

### `validate_result`

Validates the final semantic result against the Skill's confidence/evidence rules (not MCP evidence).

## Performance model

The refactor deliberately removes these costs from the Agent loop:

- repeated `python script.py` process launches;
- serial AMap -> Baidu -> OSM fallback for independent lookups;
- repeated project-keyword searches as separate Agent decisions;
- sending large raw API payloads into the LLM context;
- querying each feature strictly one at a time for OSM.

The server uses a bounded `ThreadPoolExecutor` for I/O-bound network calls. API rate limits still apply; do not raise worker counts aggressively.

## Backward compatibility

`scripts/` remain as legacy/manual fallbacks. `query_*.py` and `coord_transform.py` are thin wrappers around `geo_clients.py` (same HTTP layer as MCP). They are not the main Skill execution path.

## Recommended host configuration

The MCP host should point directly to the persistent Python interpreter used for this project.
For example:

```json
{
  "mcpServers": {
    "geo-region-inference": {
      "command": "C:/path/to/geo-region-inference/.venv/Scripts/python.exe",
      "args": ["C:/path/to/geo-region-inference/mcp_server.py"],
      "env": {
        "AMAP_KEY": "...",
        "BAIDU_AK": "...",
        "OVERPASS_URL": "https://overpass-api.de/api/interpreter",
        "HTTP_TIMEOUT_SECONDS": "12"
      }
    }
  }
}
```

This is preferable to `pip install ...` inside a task or dynamically creating an environment. The MCP host starts the server process when the connection is established and keeps it alive for subsequent Tool Calls.
