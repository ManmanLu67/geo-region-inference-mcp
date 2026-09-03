# geo-region-inference MCP

## Architecture

The Skill is the semantic workflow layer. The MCP server is the long-lived tool layer.

```text
Agent / LLM
   +-- geo-region-inference Skill (SKILL.md)
   +-- MCP (mcp_server.py)
         +-- geo_input.py / geo_geometry.py
         +-- gov_search.py
         +-- geo_clients.py (httpx, AMap/Baidu/OSM)
```

Normal path: one `analyze_regions` call per batch, not one Python process per feature/source.

MCP evidence fields: [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md).

## Install

Use a persistent Python environment. Do **not** install dependencies inside each task execution.

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
pip install -r requirements-mcp.txt
```

`requirements-mcp.txt` includes Tsinghua PyPI mirror options for CN users. If that mirror times out:

```bash
pip install -r requirements-mcp.txt --index-url https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
```

See [`pip.ini.example`](pip.ini.example) for optional global pip mirror config.

Runtime: **httpx** + **pyproj** (CRS only; no GDAL). No official `mcp` SDK.

## Run

```bash
python mcp_server.py
```

Stdio transport; stays alive until the MCP host closes the connection.

**修改 `geo_geometry.py` / `mcp_server.py` / `geo_input.py` 后必须重启 MCP 宿主**（stdio 常驻进程不热加载）。

## Configure keys and env

Set keys in the MCP host `env` block. Do not put keys into `SKILL.md` or commit them.

Common variables: `AMAP_KEY`, `BAIDU_AK`, `OSM_ENABLED`, `OVERPASS_URL`, `HTTP_TIMEOUT_SECONDS`.

Often tuned: `AMAP_QPS_LIMIT`, `AMAP_BATCH_SIZE`, `GEOMETRY_FAIL_RATIO`, `RATE_LIMIT_BATCH_RATIO`. Hole-debug (stderr only): `GEO_HOLE_DEBUG`, `GEO_HOLE_DEBUG_RATIO`.

**Full env table** (defaults and all keys): [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md#相关环境变量). Alert / rate-limit mapping: [references/error_codes.md](references/error_codes.md).

Key signup steps: [references/map_api_setup.md](references/map_api_setup.md).

## Tool contract

| Tool | One-line | Details |
|------|----------|---------|
| `analyze_regions` | Batch geometry + concurrent online evidence | [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md) |
| `prepare_gov_web_search` | Four-round gov Web search plan (no HTTP) | [references/gov_web_search_guide.md](references/gov_web_search_guide.md) |
| `validate_result` | Validate final JSON (`evidence_type`, caps) | [references/output_schema.md](references/output_schema.md) |
| `calculate_geometry` | Offline geometry + shared scan/fail_fast/`GEOMETRY_INVALID` (no POI) | [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md#calculate_geometry调试) |
| `search_project_evidence` | Single-point project search (debug) | [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md#search_project_evidence调试) |
| `check_api_status` | Probe API keys; burst mode for CUQPS (**AMap only**) | [references/error_codes.md](references/error_codes.md#check_api_status) |

If both `search_projects` and `search_poi` are false, no online APIs are called.

## Performance model

- No repeated `python script.py` per feature or data source.
- Concurrent AMap/Baidu/OSM inside `analyze_regions`; bounded `ThreadPoolExecutor`.
- OSM batched (up to 10 centroids per Overpass HTTP call).
- AMap regeo batched (`batch=true`, ≤20 unique centroids per HTTP) inside `analyze_regions`; Baidu regeo remains single-point.
- Compact evidence returned to the LLM; do not raise worker counts aggressively.

## Backward compatibility

`scripts/` is **deprecated** — see [scripts/README.md](scripts/README.md). Not the Skill execution path.

## Recommended host configuration

Point the host at the persistent project Python interpreter:

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
        "OSM_ENABLED": "true",
        "HTTP_TIMEOUT_SECONDS": "12"
      }
    }
  }
}
```

Prefer this over `pip install` inside each task. The host starts the server once and reuses the connection.
