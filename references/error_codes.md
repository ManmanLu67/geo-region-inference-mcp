# 错误码与告警参考

MCP 返回的结构化错误分两类：**`sources[].reason_code`**（在线 API）与 **`input_alerts[].code`**（输入/几何）。

## `sources[].reason_code`

| code | 含义 | Agent 处理 |
|------|------|------------|
| `NO_API_KEY` | 未配置 Key | 不重试；提示配置 env |
| `INVALID_API_KEY` | Key 无效 | 本次任务永久跳过该源 |
| `RATE_LIMIT` | 限流（含 CUQPS） | 服务端已退避重试；看 `online_summary.rate_limit` |
| `TIMEOUT` | 超时 | 可换源；勿空转重试 |
| `HTTP_ERROR` | 传输层/HTTP 异常（连接失败、非业务 JSON 等） | 看 `reason`；勿空转重试 |
| `UPSTREAM_ERROR` | 上游业务错误 | 看 `reason` 摘要 |
| `INVALID_RESPONSE` | 响应结构异常 | 勿重试同一请求 |
| `DISABLED` | OSM 被 `OSM_ENABLED=false` 关闭 | 预期行为；`status=unavailable` |

## `input_alerts[].code`

| code | severity | 含义 | Agent 处理 |
|------|----------|------|------------|
| `CRS_ASSUMED` | warning | 未声明 CRS，假定 WGS84 | **必须**提醒用户确认位置 |
| `GEOMETRY_INVALID` | warning/error | 部分或全部地物几何无效 | **必须**列出 `invalid_indices`；若有 `invalid_count` / `invalid_reasons` 一并说明；不得当作全量成功 |
| `GEOMETRY_SIMPLIFIED` | warning | Esri 有损转换（环角色无法判定已拆部件，或 paths→线） | **必须**列出 `feature_indices`；若有 `simplify_reasons` 按 reason 展开；说明面积/形状可能不准确 |

`invalid_reasons` / `simplify_reasons` 的键为**字符串化的地物 index**（JSON 对象键，如 `"0"`），不是整数。

## `online_summary` 限流字段

| 字段 | 说明 |
|------|------|
| `rate_limit.{source}.feature_count` | 该源遭遇限流的地物数 |
| `rate_limit.{source}.feature_ratio` | 限流地物占比 |
| `rate_limit.{source}.retry_after_hint_ms` | 该源客户端估算退避等待 |
| `batch_retry_recommended` | 当任一路源 `feature_ratio >= RATE_LIMIT_BATCH_RATIO` 为 true |
| `batch_retry_reason` | 触发推荐时的说明（如 `amap rate_limit ratio 0.12 >= threshold 0.1`）；未触发则为 `null` |

## 环境变量 → 告警 / 行为映射

完整 env 表与默认值见 [mcp_evidence_schema.md](mcp_evidence_schema.md#相关环境变量)。下表仅说明**哪些变量影响哪些输出**：

| 变量 | 影响的输出 / 行为 |
|------|-------------------|
| `RATE_LIMIT_BATCH_RATIO` | `online_summary.batch_retry_recommended` / `batch_retry_reason` |
| `GEOMETRY_FAIL_RATIO` | 无效几何占比 ≥ 阈值时 `analyze_regions` 与 `calculate_geometry` 均抛 `ValueError` |
| `GEO_HOLE_DEBUG` / `GEO_HOLE_DEBUG_RATIO` | stderr 孔洞 debug 日志（不进 MCP 返回体） |
| `OSM_ENABLED=false` | OSM `status=unavailable`，`reason_code=DISABLED` |

### `GEOMETRY_INVALID.invalid_reasons`

| reason | 含义 |
|--------|------|
| `residual_esri_keys` | normalize 后 geometry 仍含 `rings`/`paths` 等 Esri 键（含与 coordinates 并存的混合畸形） |
| `geometry_stat_failed` | 质心/面积等几何统计失败（空坐标、无效 Polygon 等） |

### `GEOMETRY_SIMPLIFIED.simplify_reasons`

| reason | 含义 |
|--------|------|
| `esri_ring_roles_unresolved` | Esri `rings` 缠绕/bbox 无法判定外环与孔洞，已拆为独立部件（面积可能偏大） |
| `paths_converted` | Esri `paths` 转为 LineString / MultiLineString |

合法 Esri 环（外环 CW + 孔洞 CCW + bbox 包含）**保留孔洞且不发**本告警。

## `check_api_status`

- **`probe_mode=single`（默认）**：验活高德与百度 Key，**不能**预测批量 `analyze_regions` 并发限流。
- **`probe_mode=burst`**：递增并发探测 **仅高德** CUQPS；首次限流即停；`result["amap"]` 下返回 `estimated_concurrent_limit` / `suggested_amap_qps_limit`（客户端估算，非官方配额）。百度在 burst 模式下仍只做 single 验活。
