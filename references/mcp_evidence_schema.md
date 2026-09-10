# MCP 中间证据 Schema



本文档描述 MCP `analyze_regions`（及 `search_project_evidence` 的部分字段）返回的**结构化证据**，供 Agent 在语义推理前阅读。



LLM **最终**输出格式见 [output_schema.md](output_schema.md)。不要把 MCP 原始 `sources[]` 复制进最终结果。



## 顶层（`analyze_regions`）



```jsonc

{

  "server": "geo-region-inference",

  "server_version": "2.8.0",  // 示例值；实际以 geo_core/version.py 为准

  "feature_count": 2,

  "effective_max_workers": 4,

  "input_meta": {

    "source_path": "D:/项目/地块.geojson",

    "byte_size": 12345,

    "format_hint": "geojson",

    "esri_converted": false,

    "esri_simplified_indices": [],

    "esri_simplified_reasons": {},

    "crs": { "source_epsg": 4509, "target_epsg": 4326, "reprojected": true, "crs_assumed": false },

    "z_stripped": false

  },

  "input_alerts": [

    { "code": "CRS_ASSUMED", "severity": "warning", "user_message": "..." },

    {

      "code": "GEOMETRY_INVALID",

      "severity": "warning",

      "invalid_indices": [3],

      "invalid_count": 1,

      "invalid_reasons": { "3": "geometry_stat_failed" },

      "user_message": "..."

    }

  ],

  "online_summary": {

    "channels": { "amap": { "status": "unavailable", "reason_code": "NO_API_KEY", "reason": "..." }, "...": "..." },

    "rate_limit": {

      "amap": { "feature_count": 0, "feature_ratio": 0.0, "retry_after_hint_ms": 0 },

      "baidu": { "feature_count": 0, "feature_ratio": 0.0, "retry_after_hint_ms": 0 },

      "osm": { "feature_count": 0, "feature_ratio": 0.0, "retry_after_hint_ms": 0 }

    },

    "all_channels_unavailable": true,

    "batch_retry_recommended": false,

    "batch_retry_reason": null,

    "warnings": ["高德: AMAP_KEY not configured", "..."],

    "user_message": "所有在线数据源均不可用..."

  },

  "features": [ /* 见下 */ ]

}

```



- **`input_alerts`**：Agent **必须**向用户转述（与 `online_summary.warnings` 同级）。常见 code：

  - `CRS_ASSUMED`：未声明 CRS，已假定 WGS84

  - `GEOMETRY_INVALID`：部分/全部地物几何无效；**必须**列出 `invalid_indices`；可选 `invalid_count`、`invalid_reasons`（`residual_esri_keys` / `unsupported_esri_curves` / `degenerate_ring` / `geometry_stat_failed`）

  - `GEOMETRY_SIMPLIFIED`：Esri 有损转换；**必须**列出 `feature_indices`；可选 `simplify_reasons`（`esri_ring_roles_unresolved` = 环穿插无法嵌套 / `paths_converted`）
  - `ESRI_PATHS_DROPPED_MIXED_GEOMETRY`：rings+paths 混合，已丢 paths
  - `GEOMETRY_SELF_INTERSECTING`：自相交，`area_m2` 为空
  - `GEOMETRY_AUTO_CLOSED`：未闭合环已自动补闭合

  - 完整表见 [error_codes.md](error_codes.md)

- **`input_meta.esri_simplified_indices` / `esri_simplified_reasons`**：Esri 转换时产生；`simplify_reasons` 会同步进 `GEOMETRY_SIMPLIFIED` 告警。

- **`online_summary`**：仅当 `search_projects` 或 `search_poi` 为 true 时出现。`all_channels_unavailable=true` 表示三通道均无 `ok`/`empty`，**不是**「无项目」。`rate_limit` 见 [error_codes.md](error_codes.md#online_summary-限流字段)。

- 单次请求最多 **80** 个 feature（`MAX_FEATURES`），超限返回 error。



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

    { "label": "XX花园二期建设项目", "source": "amap", "page_url": "https://uri.amap.com/poidetail?poiid=…", "evidence": { /* POI 摘要，含 id 与 page_url */ } }

  ],

  "sources": [ /* 见下，通常 amap + baidu + osm 各一条 */ ]

}

```



几何统计失败时可能出现 `"error": "no coordinates found"` 等字符串，且无 `centroid`/`area_m2` 等字段；此类地物通常也在 `GEOMETRY_INVALID.invalid_indices` 中。`project_evidence` 最多 **30** 条。



### 几何说明



- Polygon / MultiPolygon 的 `area_m2` 为 **净面积**（外环减孔洞）；`perimeter_m` 为 **仅外环** 周长；`compactness` = `4π·净面积/外环周长²`。净面积为 0 时 `compactness` 为 `null`。

- `centroid` 为 **shoelace 面积加权质心**（孔洞带符号累加），米制后平移到局部原点再计算，不是顶点平均或 bbox 中心。

- 等距圆柱近似（`111320 * cos(lat)`）相对源投影面积的系统性偏差约 **0.12%**，属精度上界，非缺陷。

- 输入坐标在 MCP 内统一为 **WGS84 (EPSG:4326)**；带 CRS 的 GeoJSON（如 EPSG:4509）会经 pyproj 自动重投影。**无 CRS 且坐标像投影（|x|>180 或 |y|>90）会直接报错**。



### `data_source`（MCP feature 级）



> **层级说明**：本字段为 MCP **feature 级**统计，仅反映 **map 源**（amap/baidu/osm）参与情况；`hybrid` = 两个及以上 map 源 `status=ok`；`offline` = 无 map 在线成功。

>

> LLM **最终**输出的顶层 `data_source` 语义更宽（含 gov/Web、场景 4 等），**写最终结果时以 [output_schema.md](output_schema.md) 为准**；可能与 MCP feature 级值不同。



只统计 `status=ok` 的 map 源：



| 值 | 含义 |

|----|------|

| `amap` / `baidu` / `osm` | 仅一个 map 源成功 |

| `hybrid` | 两个及以上 map 源成功 |

| `offline` | 无 map 源成功，或 `search_projects` 与 `search_poi` 均为 false |



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

| `unavailable` | 未配置 Key（高德/百度），或 OSM 被 `OSM_ENABLED=false` 关闭 | 勿当失败重试 |



### `reason_code`



完整枚举与 Agent 处理见 [error_codes.md](error_codes.md#sourcesreason_code)。`error` 时 `reason` 为供应商原始信息摘要；`empty` 时通常无 `reason_code`。



### 扩圈字段



- `radius_m`：初始查询半径（由 bbox 尺寸计算）。

- `expanded_radius_m`：若该地物触发过扩圈补查，为扩圈后半径；否则 `null`。**表示地物级扩圈半径，不代表该 source 一定被重新查询**——仅「非 `unavailable` 且该源尚无项目证据」的源会发起扩圈 HTTP；`merge_source_records` 仍可能给未扩圈源写入同一 `expanded_radius_m`。

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

| `expand_radius_if_needed` | `true` | 无直接项目证据时按源扩圈一次（约 2.5×，上限 5000m）；跳过 `unavailable` 源 |

| `max_workers` | `4` | 高德/百度线程池上限 1–4；返回 `effective_max_workers` |

| `output_path` | — | 可选。把**完整**结果写入 `.json`（父目录须存在）；工具返回摘要（`output_written: true`，无 `sources.places/roads/items`）。`GEO_INPUT_STRICT` 时须在 `GEO_INPUT_ROOT` 下 |



两旗均为 `false` 时不访问高德/百度/OSM，仅返回几何统计。



## 相关环境变量



| 变量 | 说明 |

|------|------|

| `AMAP_KEY` | 高德 Web 服务 Key |

| `BAIDU_AK` | 百度服务端 AK |

| `BAIDU_QPS_LIMIT` | 百度令牌桶速率（默认 3） |

| `BAIDU_RETRY_MAX` | 百度退避重试次数（默认 3） |

| `BAIDU_RETRY_BASE_MS` | 百度退避基数 ms（默认 500） |

| `BURST_PROBE_TIMEOUT_MS` | `check_api_status(burst)` 总时限（默认 3000） |

| `BURST_PROBE_MAX_CONCURRENCY` | burst 最大并发（默认 8，硬顶 8） |

| `GEO_HOLE_DEBUG` | 净/外环面积低于阈值时向 stderr 打印孔洞日志；默认开启，`0`/`false` 关闭 |

| `GEO_HOLE_DEBUG_RATIO` | 触发孔洞 debug 的净/外环比阈值（默认 0.95） |

| `OVERPASS_URL` | Overpass 端点，默认公共 API |

| `OSM_ENABLED` | 是否查询 Overpass，默认 `true`；设 `false` 可跳过所有 OSM 请求（如沙箱 egress 拦截 Overpass 主机时，避免 ~20s 超时等待），OSM 返回 `unavailable` + `reason_code=DISABLED` |

| `HTTP_TIMEOUT_SECONDS` | HTTP 超时（默认 12） |

| `GEO_INPUT_MAX_BYTES` | 文件输入大小上限（默认 **67108864**，约 64 MiB） |

| `GEO_INPUT_STRICT` | 路径沙箱，**默认关闭**。设为 `true`/`1`/`yes`/`on` 时 `input_path`/`output_path` 必须落在 `GEO_INPUT_ROOT` 下。本地单用户通常不必开；多用户/远程部署建议开。 |

| `GEO_INPUT_ROOT` | 严格模式根目录（默认用户主目录）；仅 `GEO_INPUT_STRICT` 开启时生效 |

| `AMAP_QPS_LIMIT` | 高德令牌桶速率（默认 **3**，对齐实测 ≈2.5 QPS） |

| `AMAP_BATCH_SIZE` | 批大小（默认 **5**） |

| `AMAP_BATCH_DELAY_MS` | 批间间隔 ms（默认 **2000**） |

| `AMAP_RETRY_MAX` | 高德退避重试次数（默认 3） |

| `AMAP_RETRY_BASE_MS` | 高德退避基数 ms（默认 500） |

| `GEOMETRY_FAIL_RATIO` | 无效几何占比 ≥ 此值 hard fail（默认 **0.5**）；`analyze_regions` 与 `calculate_geometry` 共用 |

| `RATE_LIMIT_BATCH_RATIO` | 限流占比 ≥ 此值时 `batch_retry_recommended`（默认 **0.10**） |

| `PROJECT_KEYWORDS` | 高德/百度 around 检索关键词（默认 `在建\|项目\|工地\|建设`，`|` 分隔）；同时用于 `project_signal()` 命中判定，后者额外含 `工程` / `construction` / `development` / `project`（**不**写入 around 查询串） |



错误码与告警见 [error_codes.md](error_codes.md)。



**依赖**：MCP Server 需 `pip install -e .`（`pyproject.toml`：httpx + pyproj + mcp-types / pydantic）。



## 后续工具



### `prepare_gov_web_search`



在 `analyze_regions` 之后调用。输入参数：



| 参数 | 必填 | 说明 |

|------|------|------|

| `analyze_result` | 与 `analyze_result_path` 二选一 | `analyze_regions` 的**完整**返回体（须含 `sources[].places`） |

| `analyze_result_path` | 与 `analyze_result` 二选一 | `output_path` 写出的完整 JSON 路径 |

传入摘要（`output_written: true`，或 `ok`/`empty` 源缺少 `places` 键）会 **error**，提示改用 `analyze_result_path`。不要把摘要当成「无行政区」。



返回（节选）：



| 字段 | 说明 |

|------|------|

| `candidate_count` | 需政府 Web 检索的地物数（empty_only：已有 `project_evidence` 的跳过） |

| `skipped_summary` | `has_project_evidence` / `no_admin` 跳过计数 |

| `candidates[]` | 每项含 `index`、`admin`、`match_roads`、`search_plan.rounds[]` |



**无网络**；Agent 按 [gov_web_search_guide.md](gov_web_search_guide.md) 分轮执行 `web_search`/`web_fetch`。



## `calculate_geometry`（调试）



**不是** `analyze_regions` 的在线等价物。几何路径与主工具共用 `geometry_pipeline`（`geometry_stats` → `scan_residual_esri_geometry` → `validate_geometry_fail_fast` → `GEOMETRY_INVALID`），**不**查 POI、不算 `radius_from_stats`。



| 输入 | 说明 |

|------|------|

| `geojson` / `input_path` | 与 `analyze_regions` 相同，二选一 |



返回：`feature_count`、`input_meta`（不含 `input_alerts` 键的副本另见顶层）、`input_alerts`、`features[]`（纯几何统计，无 `sources`/`project_evidence`）。



`analyze_regions` 的半径公式 `radius_from_stats` **不会**在本工具输出中出现（本工具不算半径、不查 POI）。



## `search_project_evidence`（调试）



单点（WGS84 lat/lon）项目关键词检索，非批量。返回字段名与 `analyze_regions` 略有不同：`expanded_search_used`（analyze 为 `expanded_radius_used`）。



| 输入 | 默认 | 说明 |

|------|------|------|

| `lat` / `lon` | 必填 | WGS84 |

| `radius_m` | 300 | 初始半径 |

| `expand_if_empty` | true | 无直接证据时扩圈一次；**跳过 `unavailable` 源** |



返回：`center`、`initial_radius_m`、`expanded_search_used`、`expanded_radius_found_project`、`project_evidence`（最多 **30** 条）、`sources[]`。



## `check_api_status`



见 [error_codes.md](error_codes.md#check_api_status)。`probe_mode=burst` **仅探测高德 CUQPS**，百度只做 single 验活。



## 实现注记



### MCP 协议版本



- 协议类型用 **mcp-types**；传输仍是 `mcp_server.py` 手写 stdio 循环。

- `server/discover` 的 `supportedVersions` 等于 `list(mcp_types.version.SUPPORTED_PROTOCOL_VERSIONS)`（handshake ∪ modern，含官方表中的旧修订）。

- `initialize` 与 `HANDSHAKE_PROTOCOL_VERSIONS` 求交，空则 `LATEST_HANDSHAKE_VERSION`（`2025-11-25`）。客户端请求 `2026-07-28` **不 echo**。



### `search_poi` 与 `search_projects=false`



- `keywords=None` 时：**百度**使用 `BAIDU_DEFAULT_QUERY` 泛搜；**高德**不传 `keywords` 参数（周边全类 POI）。

- 两源「泛搜」行为**不对称**；文档与测试仅保证不传项目关键词，不保证两源等价。


