# geo-region-inference MCP

## Architecture

The Skill is the semantic workflow layer. The MCP server is the long-lived tool layer.

```text
Agent / LLM
   +-- geo-region-inference Skill (SKILL.md)
   +-- MCP adapter (mcp_server.py)
         +-- geo_core (GIS / API / evidence / validation)
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
pip install -e .
```

Declared in `pyproject.toml`: **httpx** + **pyproj** (CRS only; no GDAL) + **mcp-types** (pulls **pydantic**). Protocol types come from `mcp-types`; the stdio transport is still the handwritten loop in `mcp_server.py`. There is **no** `starlette` / `uvicorn` / official `mcp` HTTP stack.

CN mirrors: [`pip.ini.example`](pip.ini.example) (Tsinghua). If that times out:

```bash
pip install -e . --index-url https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
```

### Dependency footprint (measured)

Clean venv, `pip install -e .`, Windows CPython 3.12 amd64, 2026-09-09 (Aliyun index; Tsinghua timed out). Bytes via walking `Lib/site-packages` files. Total includes the venv’s `pip`. There is **no** `starlette` / `uvicorn`.

| Scope | Bytes | MB (1024²) |
|-------|------:|----------:|
| `site-packages` total | 52,579,908 | 50.2 |
| `httpx` + `httpcore` / `h11` / `idna` / `certifi` / `anyio` | 3,411,741 | 3.3 |
| `pyproj` (+ `pyproj.libs`) | 27,527,465 | 26.3 |
| `pydantic` + `pydantic_core` / `annotated_types` / `typing_inspection` | 9,325,317 | 8.9 |
| `mcp_types` | 625,311 | 0.6 |

`pyproj` dominates. `pydantic` is the cost of `mcp-types`. Use this table if someone later proposes the full `mcp` package (starlette stack).

## Run

```bash
python mcp_server.py
```

Stdio transport; stays alive until the MCP host closes the connection.

**Cancellation:** the adapter is a **synchronous** `sys.stdin` loop. A long-running `analyze_regions` call cannot be cancelled mid-flight when the host disconnects; the process finishes the current `handle_tool` before `close_http()` in `finally`.

**Handshake:** `initialize` negotiates against `mcp_types.version.HANDSHAKE_PROTOCOL_VERSIONS` (latest `2025-11-25`). A client that requests `2026-07-28` is **not** echoed; the server still answers `2025-11-25`. `server/discover` lists `list(SUPPORTED_PROTOCOL_VERSIONS)` (handshake ∪ modern, including older 2024/2025 revisions from the registry — a wider table than the previous two-element tuple).

### Versioning

**Major** = host-visible **capability break**: tool removed/renamed, required params changed, result keys removed/renamed so old hosts crash, or handshake **stops accepting** a version the host already uses.

**Minor** = same contract, correctness or algorithm fix: official-registry negotiate (no echo of a version we do not speak), `discover` **adding** older revisions, geometry algorithm changes, new **optional** `input_alerts` codes.

**Patch** = docs/tests only; output unchanged.

2.7.0 handshake/discover was a correctness fix (additive `discover`, refuse to echo 2026), not a capability break — no 3.0.0 retrofit. **2.8.0** is geometry-only (G1–G4). Later geometry-only work stays 2.9.x, not a major bump.

`analyze_regions` / `calculate_geometry` may mutate the caller’s GeoJSON dict in place (G4 auto-close). There is no immutability contract.

### `ring_self_intersects` cost (O(n²), segment bbox reject)

Measured 2026-09-10, Windows 11, CPython 3.13.14, Intel Family 6 Model 183: convex n=1200 ring, no self-intersection, **48.7 ms** (threshold 500 ms). Segment bbox reject; no sweep line.

**修改 `mcp_server.py` / `geo_core/**` 后必须重启 MCP 宿主**（stdio 常驻进程不热加载）。重启后，若证据筛选规则（如 `project_evidence`）有变动，已产出但未定稿的结论须重新跑 `analyze_regions`（及后续 gov 检索/校验）再核验，**禁止直接沿用旧结果**。

## Configure keys and env

Set keys in the MCP host `env` block. Do not put keys into `SKILL.md` or commit them.

Common variables: `AMAP_KEY`, `BAIDU_AK`, `OSM_ENABLED`, `OVERPASS_URL`, `HTTP_TIMEOUT_SECONDS`.

Often tuned: `AMAP_QPS_LIMIT`, `AMAP_BATCH_SIZE`, `GEOMETRY_FAIL_RATIO`, `RATE_LIMIT_BATCH_RATIO`. Hole-debug (stderr only): `GEO_HOLE_DEBUG`, `GEO_HOLE_DEBUG_RATIO`.

| 变量 | 默认 | 说明 |
|------|------|------|
| `GEO_INPUT_STRICT` | 关（不设或非 `1`/`true`/`yes`/`on`） | `input_path`/`output_path` 路径白名单。本地单用户通常不必开；多用户/远程部署建议开。 |
| `GEO_INPUT_ROOT` | 用户主目录 | 仅 `GEO_INPUT_STRICT=true` 时生效：路径必须落在此目录下。 |
| `GEO_INPUT_MAX_BYTES` | `67108864`（64 MiB） | 输入文件大小上限。 |
| `PROJECT_KEYWORDS` | `在建\|项目\|工地\|建设` | 高德/百度 around 检索词（`\|` 分隔），整体替换。 |

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
- Concurrent AMap/Baidu/OSM inside `analyze_regions`; `max_workers` capped at 4 (`effective_max_workers`).
- Optional `output_path` writes the full JSON; the tool return is a summary. Use `prepare_gov_web_search(analyze_result_path=...)` afterwards.
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
