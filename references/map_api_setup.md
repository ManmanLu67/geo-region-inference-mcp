# 地图 API 数据源配置说明

MCP 的 `analyze_regions` **并发**查询高德、百度、OSM（不是串行 fallback）。最终 `data_source` 见 [output_schema.md](output_schema.md)：单源 map 用 `amap`/`baidu`/`osm`，多源或 map+Web/离线组合用 `hybrid`；**仅**几何且无 Web 检索支撑项目结论时用 `offline`。无 map 源但输入属性线索 + Agent Web 检索支撑结论 → `hybrid`（场景 4）。Skill 正常路径禁用 `scripts/`；人工本地调试见 [scripts/README.md](../scripts/README.md)。

| 数据源 | 是否需要 Key | 国内覆盖 | 说明 |
|---|---|---|---|
| 高德(AMap) | 需要 `AMAP_KEY` | 好 | 周边 POI；around 结果里没有行政区时才补一次逆地理 |
| 百度(Baidu) | 需要 `BAIDU_AK` | 好 | 交叉验证；同样仅在缺行政区时 regeo |
| OSM(Overpass) | 不需要（可用 `OSM_ENABLED=false` 关闭） | 国内城市一般、偏远较差 | 免费 landuse；按质心批量查询，不做 regeo |
| 离线推理 | 不需要 | — | 以上均不可用或为空时的兜底 |

## 怎么申请 Key

### 高德地图（AMAP_KEY）

1. 打开 <https://console.amap.com/> 注册开发者账号（个人认证即可，免费）
2. 进入"应用管理" → "创建新应用" → 添加 Key
3. Key 类型选 **"Web服务"**（不是"Web端(JS API)"，也不是"Android/iOS"——选错类型调不通）
4. 拿到 Key 后设置环境变量：
   ```bash
   export AMAP_KEY=你的key
   ```
5. 免费额度以控制台为准。MCP 每个地物逻辑上：1 次 around，**仅当 around 没有省市区/地址时**再 1 次 regeo；高德 regeo 在 `analyze_regions` 内按坐标去重后 **HTTP 可 `batch=true`（每批 ≤20，计 1 次 QPS）**，百度仍为单点。

### 百度地图（BAIDU_AK）

1. 打开 <https://lbsyun.baidu.com/> 注册开发者账号
2. 进入"控制台" → "创建应用"
3. 应用类型选 **"服务端"**（不是"浏览器端"/"移动端"）
4. 设置环境变量：
   ```bash
   export BAIDU_AK=你的AK
   ```

### 都不配置

不设置环境变量也能用：高德/百度为 `unavailable`，仍可走 OSM 或纯离线推理。

## MCP 查询行为

对每个地物：高德、百度、OSM **并发**请求；无直接项目证据时最多扩圈一次（**2.5×**，上限 5000m）；OSM 按最多 10 个质心合并为一次 Overpass。参数、扩圈与 `sources[]` 字段见 [mcp_evidence_schema.md](mcp_evidence_schema.md)。

来源冲突时把两边都写进 `evidence`，不要只留一个。

## 坐标系提醒

- 高德用 GCJ-02，百度用 BD-09，OSM/Overpass 用标准 WGS84
- 转换在 `geo_clients.py` 内完成；传入 ArcGIS 导出的 WGS84 即可
- **已废弃**：`scripts/coord_transform.py` 仅本地调试（见 [scripts/README.md](../scripts/README.md)）。坐标转换已在 `geo_clients.py` / MCP 内完成。

## MCP 证据 Schema

`analyze_regions` 返回的 `features[]`、`sources[]`、`status`、`reason_code` 等字段见 [mcp_evidence_schema.md](mcp_evidence_schema.md)。LLM 最终输出格式见 [output_schema.md](output_schema.md)。
