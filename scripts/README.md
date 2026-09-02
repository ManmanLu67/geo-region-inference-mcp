# scripts/（已废弃）

本目录 **已废弃**。正常路径请使用 MCP 工具：

- `analyze_regions` — 几何统计 + 在线证据（推荐 `input_path` 读取大 GeoJSON）
- `calculate_geometry` — 仅离线几何统计

## 与 MCP v2.5+ 的差异

| 项目 | MCP 主路径 | 本目录（如 `geo_stats.py`） |
|------|-----------|---------------------------|
| Polygon 质心 | shoelace **面积加权** | **顶点平均** |
| CRS | `geo_input` + pyproj → WGS84 | 旧 heuristic，可能输出 `coordinate_system_warning` |
| properties | `compact_properties` 压缩 | 原样透传 |

**应急使用 `geo_stats.py` 时，质心 lon/lat 可能与 MCP 系统性不同**，勿与 MCP 结果对比或作为 POI 查询中心。

## 何时可碰

仅当 **MCP Server 完全不可用** 且只需粗略形状参考时，可作为最后手段。仍不推荐用于生产推断。

## 其他脚本

- `query_*.py`、`coord_transform.py` — `geo_clients` 薄封装，与 MCP 共用 HTTP 层，**不是** Skill 正常执行路径。
- `validate_output.py` — **DEPRECATED** 薄封装，委托 `validation.py`；Skill 正常路径用 MCP `validate_result`。
