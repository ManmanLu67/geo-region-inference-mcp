# geo-region-inference MCP

## Architecture

The Skill is now the semantic workflow layer. The MCP server is the long-lived tool layer.

```text
Agent / LLM
   |
   +-- geo-region-inference Skill
   |     - evidence priorities
   |     - project-first semantic inference
   |     - confidence calibration
   |     - output schema
   |
   +-- geo-region-inference MCP
         - batch geometry calculation
         - concurrent AMap/Baidu/OSM queries
         - project-oriented keyword search
         - one automatic radius expansion
         - API-result compaction
         - result validation
```

The normal path is one MCP call to `analyze_regions`, not one Python process per feature and per data source.

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

The bundled server uses the Python standard library only. No `mcp` SDK install is required.

## Run

```bash
python mcp_server.py
```

The server uses stdio and stays alive until the MCP host closes the connection.

## Configure keys

Set `AMAP_KEY` and/or `BAIDU_AK` in the MCP host configuration. OSM/Overpass works without a key. Do not put keys into `SKILL.md` or commit them to the repository.

## Tool contract

### `analyze_regions`

Preferred tool for normal use. Input is a GeoJSON `FeatureCollection`, `Feature`, or bare geometry. It computes geometry for all features, performs project-oriented online searches concurrently, expands the radius once only if no direct project evidence is found, and returns compact evidence.

### `calculate_geometry`

Offline deterministic geometry calculation only.

### `search_project_evidence`

One-point targeted project evidence search. AMap, Baidu, and OSM run concurrently.

### `validate_result`

Validates the final semantic result against the Skill's confidence/evidence rules.

## Performance model

The refactor deliberately removes these costs from the Agent loop:

- repeated `python script.py` process launches;
- serial AMap -> Baidu -> OSM fallback for independent lookups;
- repeated project-keyword searches as separate Agent decisions;
- sending large raw API payloads into the LLM context;
- querying each feature strictly one at a time.

The server uses a bounded `ThreadPoolExecutor` for I/O-bound network calls. API rate limits still apply; do not raise worker counts aggressively.

## Backward compatibility

The original `scripts/` remain in the bundle as legacy/manual fallbacks. They are no longer the main Skill execution path.

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
        "BAIDU_AK": "..."
      }
    }
  }
}
```

This is preferable to `pip install ...` inside a task or dynamically creating an environment. The MCP host starts the server process when the connection is established and keeps it alive for subsequent Tool Calls.
