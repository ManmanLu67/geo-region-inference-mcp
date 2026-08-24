# Overpass 查询指南

## 执行路径

**正常任务**：使用 MCP `analyze_regions`，OSM 查询由 `geo_clients.overpass_query_batch` 在服务端批量完成，Agent **不要**逐地物调用脚本或手写 Overpass QL。

**本地调试**：`python scripts/query_overpass.py <lat> <lon> [radius_m]` 为 `geo_clients` 薄封装，HTTP 与解析逻辑在 `geo_clients.py`。

字段定义见 [mcp_evidence_schema.md](mcp_evidence_schema.md) 中 `sources[]`（`source=osm`）一节。

## 半径怎么选

MCP 内置 `radius_from_stats()`（`analyze_regions` / `calculate_geometry` 共用），公式：

```
radius_m = min(max(150, 0.6 * max(bbox_width_m, bbox_height_m)), 2500)
```

若首轮没有直接项目名称/项目编号/「在建」等线索，MCP 最多再扩圈一次（约 **2.5×**，上限 5000m），见 `expand_radius_if_needed`。扩大范围的目标是捕获临近主干道、地块边界外的项目 POI 或反向地理编码线索；扩大查询结果不能自动视为地块本身的直接证据。

- 小地块（单栋楼、几千㎡）：150–300 米足够覆盖周边街区
- 大地块（园区、大型小区，几万㎡以上）：按上面公式放大，避免查询范围只覆盖到地块内部
- 线状地物（道路、管线）：MCP 使用面积加权质心；半径仍按 bbox 尺寸计算

## MCP 批量行为

- 单次 `analyze_regions` 最多 **80** 个 feature（`MAX_FEATURES`），超限返回 error
- Overpass 按质心 **每 10 个合并为一次 HTTP**（`ceil(N/10)` 次；有扩圈则约 ×2）
- Agent **不要**在 MCP 批处理外再 `sleep(1)` 或逐地物重复调用 OSM
- 可通过 `OVERPASS_URL` 指向备用/自建 Overpass 实例

## 结果怎么解读

MCP 返回的 OSM source（或 CLI 脚本 stdout）中，以下字段重要性从高到低：

1. **landuse** — 最直接的证据。如果非空，`region_type` 判断应该以此为主要依据，参照 [landuse_taxonomy.md](landuse_taxonomy.md) 里的映射表。
2. **buildings.by_type** — 建筑类型分布（如 `{"apartments": 8, "yes": 4}`）。`apartments`/`house` 强烈指向住宅；`industrial`/`warehouse` 指向工业/仓储；`commercial`/`office` 指向商业。`yes` 是通用建筑标签，信息量低，不要过度解读。
3. **amenities** — 具体设施点（学校、医院、商铺等），对判断「可能建筑」和「相关项目」很有用，尤其是带 `name` 的条目可以直接作为候选建筑名称的参考。
4. **roads** — 道路名称，主要用于地址描述和辅助判断区域等级（主干道 vs 巷道），不单独作为区域类型的证据。
5. **places** — 如果命中了 `place=suburb/neighbourhood` 等标签且带 `name`，这通常是该区域的官方或通用地名，可以直接用于描述「这是什么区域」。

## 空结果怎么办

如果 OSM source 的上述字段全部为空，或 `status=empty`，说明该区域 OSM 数据覆盖不足（常见于新建区域、农村地区），此时：

- 不要把「OSM 无数据」当作「这里没有建筑/是空地」的证据
- 看 `evidence.data_source`：若所有在线源均不可用/为空，则为 `offline`
- 转为依赖 MCP 返回的 `geometry` 形状特征（或离线时 `scripts/geo_stats.py`）和 `properties` 做弱证据推理
- `region_type`/`possible_buildings`/`related_projects` 里对应给出较低置信度（建议不超过 0.4），并在 evidence 里写明「OSM 无覆盖数据，基于几何形状推测」

## 失败与降级

| 路径 | 行为 |
|------|------|
| **MCP** | `sources[osm].status=error`，看 `reason_code`（`TIMEOUT` / `UPSTREAM_ERROR` / `INVALID_RESPONSE`）。`unavailable` 不适用于 OSM（无 Key 要求）。不要空转重试同一批请求。 |
| **CLI 脚本** | 网络失败时退出码 2；不要重试超过 1 次，直接转离线模式。 |
