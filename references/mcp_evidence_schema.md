# MCP 中间证据 Schema

本文档描述 MCP `analyze_regions`（及 `search_project_evidence` 的部分字段）返回的**结构化证据**，供 Agent 在语义推理前阅读。

LLM **最终**输出格式见 [output_schema.md](output_schema.md)。不要把 MCP 原始 `sources[]` 复制进最终结果。

## 顶层（`analyze_regions`）

```jsonc
{
  "server": "geo-region-inference",
  "server_version": "2.3.0",
  "feature_count": 2,
  "input_meta": {
    "source_path": "D:/项目/地块.geojson",
    "crs": { "source_epsg": 4509, "target_epsg": 4326, "reprojected": true, "crs_assumed": false },
    "z_stripped": false
  },
  "input_alerts": [
    { "code": "CRS_ASSUMED", "severity": "warning", "user_message": "..." }
  ],
  "online_summary": {
    "channels": { "amap": { "status": "unavailable", "reason_code": "NO_API_KEY", "reason": "..." }, "...": "..." },
    "all_channels_unavailable": true,
    "warnings": ["高德: AMAP_KEY not configured", "..."],
    "user_message": "所有在线数据源均不可用..."
  },
  "features": [ /* 见下 */ ]
}
```

- **`input_alerts`**：Agent **必须**向用户转述（与 `online_summary.warnings` 同级）。`CRS_ASSUMED` 表示文件未声明坐标系、已假定 WGS84。
- **`online_summary`**：仅当 `search_projects` 或 `search_poi` 为 true 时出现。`all_channels_unavailable=true` 表示三通道均无 `ok`/`empty`，**不是**「无项目」。
- 单次请求最多 **80** 个 feature（`MAX_FEATURES`），超限返回 error。

### 破坏性变更（v2.3）

- 已移除 feature 级 `coordinate_system_warning`；CRS 信息见 `input_meta.crs`。
- 输入经 `normalize_geo_input` 重投影为 WGS84 后再做几何统计；无 CRS 的投影坐标会直接报错。

## 每个 feature

几何统计与在线证据合并为同一对象：

```jsonc
{
  "index": 0,
  "geometry_type": "Polygon",
  "vertex_count": 12,
  "centroid": { "lon": 116.39, "lat": 39.91 },
  "bbox": { "min_lon": 116.38, "min_lat": 39.90, "max_lon": 116.40, "max_lat": 39.92 },
  "area_m2": 12500.0,
  "perimeter_m": 450.2,
  "compactness": 0.78,
  "bbox_width_m": 120.5,
  "bbox_height_m": 103.8,
  "aspect_ratio": 1.16,
  "properties": { /* 压缩后的属性子集 */ },
  "property_keys": ["field_a", "field_b"],

  "radius_m": 180.0,
  "expanded_radius_used": false,
  "expanded_radius_found_project": false,
  "data_source": "hybrid",
  "project_evidence": [
    { "label": "XX花园二期建设项目", "source": "amap", "evidence": { /* POI 摘要 */ } }
  ],
  "sources": [ /* 见下，通常 amap + baidu + osm 各一条 */ ]
}
```

### 几何说明

- 多环 Polygon 的 `centroid` 为 **shoelace 面积加权质心**，不是简单顶点平均或 bbox 中心。
- 输入坐标在 MCP 内统一为 **WGS84 (EPSG:4326)**；带 CRS 的 GeoJSON（如 EPSG:4509）会经 pyproj 自动重投影。

### `data_source`

只统计 `status=ok` 的源：

| 值 | 含义 |
|----|------|
| `amap` / `baidu` / `osm` | 仅一个源成功 |
| `hybrid` | 两个及以上源成功 |
| `offline` | 无在线源成功，或 `search_projects` 与 `search_poi` 均为 false |

## `sources[]` 每条记录

```jsonc
{
  "source": "amap",
  "status": "ok",
  "reason_code": null,
  "reason": null,
  "count": 8,
  "radius_m": 180.0,
  "expanded_radius_m": null,
  "project_signal_count": 2,
  "landuse": [],
  "buildings": { "by_type": {}, "count": 0 },
  "amenities": [],
  "roads": [],
  "places": [],
  "items": [ /* POI 摘要，最多 12 条 */ ],
  "project_evidence": [ /* 该源上的直接项目线索，最多 10 条 */ ]
}
```

### `status` 四态

| status | 含义 | Agent 处理 |
|--------|------|------------|
| `ok` | 请求成功且有可用结果 | 正常读字段 |
| `empty` | 请求成功但附近无结果 | 非失败，勿重试 |
| `error` | 请求失败（Key 无效、限流、超时等） | 看 `reason_code` |
| `unavailable` | 未配置 Key（高德/百度） | 勿当失败重试 |

### `reason_code` 枚举

| reason_code | 典型场景 |
|-------------|----------|
| `NO_API_KEY` | 未设置 `AMAP_KEY` / `BAIDU_AK` |
| `INVALID_API_KEY` | Key 无效；本任务永久跳过该源 |
| `RATE_LIMIT` | 限流；可换源，勿空转重试 |
| `TIMEOUT` | 超时 |
| `UPSTREAM_ERROR` | 上游 HTTP/业务错误 |
| `INVALID_RESPONSE` | 响应非 JSON 或结构异常 |
| `DISABLED` | 被 `OSM_ENABLED=false` 关闭，未发起任何 Overpass 请求 |

`error` 时 `reason` 为供应商原始信息摘要；`empty` 时通常无 `reason_code`。

### 扩圈字段

- `radius_m`：初始查询半径（由 bbox 尺寸计算）。
- `expanded_radius_m`：若做过扩圈补查，为扩圈后半径；否则 `null`。
- feature 级 `expanded_radius_used` / `expanded_radius_found_project` 表示是否扩圈及扩圈是否找到直接项目证据。
- 每个 source **始终一条记录**；扩圈合并进同一条，不会出现两个 `amap`。

### 按源差异

| source | 特有内容 |
|--------|----------|
| `amap` / `baidu` | `items`（POI）、`places`（含条件 regeo）；无面状 landuse |
| `osm` | `landuse`、`buildings`、`amenities`、`roads`；不做 regeo |

OSM 字段解读见 [overpass_query_guide.md](overpass_query_guide.md)。

## `analyze_regions` 参数（Tool 输入）

| 参数 | 默认 | 说明 |
|------|------|------|
| `geojson` | — | FeatureCollection / Feature / 裸 geometry（与 `input_path` 二选一） |
| `input_path` | — | 本地 `.json`/`.geojson` 路径，**大文件推荐**（与 `geojson` 二选一） |
| `search_projects` | `true` | 项目关键词 POI 检索 |
| `search_poi` | `true` | 仅当 `search_projects=false` 时有泛搜意义；为 true 时**不会**加倍请求 |
| `expand_radius_if_needed` | `true` | 无直接项目证据时扩圈一次（约 2.5×，上限 5000m） |
| `max_workers` | `8` | 并发上限 8；高德/百度线程池另限 4 |

两旗均为 `false` 时不访问高德/百度/OSM，仅返回几何统计。

## 相关环境变量

| 变量 | 说明 |
|------|------|
| `AMAP_KEY` | 高德 Web 服务 Key |
| `BAIDU_AK` | 百度服务端 AK |
| `OVERPASS_URL` | Overpass 端点，默认公共 API |
| `OSM_ENABLED` | 是否查询 Overpass，默认 `true`；设 `false` 可跳过所有 OSM 请求（如沙箱 egress 拦截 Overpass 主机时，避免 ~20s 超时等待），OSM 返回 `unavailable` + `reason_code=DISABLED` |
| `HTTP_TIMEOUT_SECONDS` | HTTP 超时（默认 12） |
| `GEO_INPUT_MAX_BYTES` | 文件输入大小上限（默认 67108864） |
| `GEO_INPUT_STRICT` | 设为 `true` 时启用路径沙箱（须在 `GEO_INPUT_ROOT` 下） |
| `GEO_INPUT_ROOT` | 严格模式根目录（默认用户主目录） |

配置细节见 [map_api_setup.md](map_api_setup.md)。

**依赖**：MCP Server 需 `pip install -r requirements-mcp.txt`（含 `pyproj` 用于 CRS 重投影）。

## 实现注记（当前行为，待进一步确认）

### MCP 协议版本

- `server/discover` 的 `supportedVersions` 包含 `2026-07-28` 与 `2025-11-25`。
- `initialize` 当前**固定协商** `2025-11-25`（见 `mcp_server.negotiate_initialize` 与 `tests/test_offline.py`）。
- 与 `discover` 所列新版不一致的原因**尚未在仓库 commit 中明确记录**；**暂不建议改动**，除非确认 Host 兼容性。

### `search_poi` 与 `search_projects=false`

- `keywords=None` 时：**百度**使用 `BAIDU_DEFAULT_QUERY` 泛搜；**高德**不传 `keywords` 参数（周边全类 POI）。
- 两源「泛搜」行为**不对称**；是否为产品设计**待确认**。文档与测试仅保证不传项目关键词，不保证两源等价。
