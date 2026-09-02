# 错误码与告警参考

MCP 返回的结构化错误分两类：**`sources[].reason_code`**（在线 API）与 **`input_alerts[].code`**（输入/几何）。

## `sources[].reason_code`

| code | 含义 | Agent 处理 |
|------|------|------------|
| `NO_API_KEY` | 未配置 Key | 不重试；提示配置 env |
| `INVALID_API_KEY` | Key 无效 | 本次任务永久跳过该源 |
| `RATE_LIMIT` | 限流（含 CUQPS） | 服务端已退避重试；看 `online_summary.rate_limit` |
| `TIMEOUT` | 超时 | 可换源；勿空转重试 |
| `UPSTREAM_ERROR` | 上游业务错误 | 看 `reason` 摘要 |
| `INVALID_RESPONSE` | 响应结构异常 | 勿重试同一请求 |
| `DISABLED` | OSM 被 `OSM_ENABLED=false` 关闭 | 预期行为 |

## `input_alerts[].code`

| code | severity | 含义 | Agent 处理 |
|------|----------|------|------------|
| `CRS_ASSUMED` | warning | 未声明 CRS，假定 WGS84 | **必须**提醒用户确认位置 |
| `GEOMETRY_INVALID` | warning/error | 部分或全部地物几何无效 | **必须**列出 `invalid_indices`；若有 `invalid_reasons` 一并说明；不得当作全量成功 |
| `GEOMETRY_SIMPLIFIED` | warning | Esri 转换丢洞/简化 | **必须**列出 `feature_indices`；说明面积可能偏大 |

## `online_summary` 限流字段

| 字段 | 说明 |
|------|------|
| `rate_limit.{source}.feature_count` | 该源遭遇限流的地物数 |
| `rate_limit.{source}.feature_ratio` | 限流地物占比 |
| `batch_retry_recommended` | 当任一路源 `feature_ratio >= RATE_LIMIT_BATCH_RATIO`（默认 0.10）为 true |
| `retry_after_hint_ms` | 客户端估算退避等待 |

## 环境变量（限流 / 几何）

| 变量 | 默认 | 说明 |
|------|------|------|
| `AMAP_QPS_LIMIT` | 3 | 令牌桶速率（对齐实测 ≈2.5 QPS） |
| `AMAP_BATCH_SIZE` | 5 | 批大小 |
| `AMAP_BATCH_DELAY_MS` | 2000 | 批间间隔 |
| `AMAP_RETRY_MAX` | 3 | 退避重试次数 |
| `GEOMETRY_FAIL_RATIO` | 0.5 | 无效几何占比 ≥ 此值则 hard fail |
| `RATE_LIMIT_BATCH_RATIO` | 0.10 | 触发 `batch_retry_recommended` |

### `GEOMETRY_INVALID.invalid_reasons`

| reason | 含义 |
|--------|------|
| `residual_esri_keys` | normalize 后 geometry 仍含 `rings`/`paths` 等 Esri 键（含与 coordinates 并存的混合畸形） |
| `geometry_stat_failed` | 质心/面积等几何统计失败（空坐标、无效 Polygon 等） |

## `check_api_status`

- **`probe_mode=single`（默认）**：仅验活 Key，**不能**预测批量 `analyze_regions` 并发限流。
- **`probe_mode=burst`**：递增并发探测 CUQPS；首次限流即停；返回 `estimated_concurrent_limit` / `suggested_amap_qps_limit`（客户端估算，非官方配额）。
