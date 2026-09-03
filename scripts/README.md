# scripts/（已废弃）

本目录 **已废弃**。正常路径请使用 MCP 工具：

- `analyze_regions` — 几何统计 + 在线证据（推荐 `input_path` 读取大 GeoJSON）
- `calculate_geometry` — 仅离线几何统计

## 与 MCP 主路径的差异

| 项目 | MCP 主路径 | 本目录（如 `geo_stats.py`） |
|------|-----------|---------------------------|
| Polygon 质心 / 面积 | shoelace **面积加权** + 局部原点；孔洞净面积 | **同算法**（复用 `geo_geometry`） |
| CRS | `geo_input` + pyproj → WGS84 | 旧 heuristic，可能输出 `coordinate_system_warning` |
| properties | `compact_properties` 压缩 | 原样透传 |

MCP 已修复小地块浮点质心 bug，且 `geo_stats.py` 环级计算与 MCP 共用。仍不推荐用于生产推断。

## 何时可碰

仅当 **MCP Server 完全不可用** 且只需粗略形状参考时，可作为最后手段。

## 其他脚本

- `query_*.py`、`coord_transform.py` — `geo_clients` 薄封装，与 MCP 共用 HTTP 层，**不是** Skill 正常执行路径。
- `validate_output.py` — **DEPRECATED** 薄封装，委托 `validation.py`；Skill 正常路径用 MCP `validate_result`。
