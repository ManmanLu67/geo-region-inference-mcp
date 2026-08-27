# 输出 JSON Schema

> 本文档描述 LLM **最终**语义输出。MCP `analyze_regions` 返回的中间证据见 [mcp_evidence_schema.md](mcp_evidence_schema.md)。

每个输入地物（feature）对应一个结果对象，多个地物就是一个结果对象数组。
最终语义结果由 Skill 生成，MCP 的 `validate_result` 负责运行时校验。

```jsonc
{
  "feature_index": 0,
  "data_source": "hybrid",

  "region_type": [
    {
      "label": "住宅小区",
      "confidence": 0.72,
      "evidence": "landuse_code=R2；MCP 返回 OSM 周边有 apartments 建筑"
    }
  ],

  "possible_buildings": [
    {
      "label": "多层住宅楼",
      "confidence": 0.70,
      "evidence": "MCP 返回 building=apartments"
    }
  ],

  "related_projects": [
    {
      "label": "XX花园二期建设项目",
      "confidence": 0.80,
      "evidence_type": "poi_name",
      "evidence": "MCP 查询到 POI name=XX花园二期建设项目",
      "source_url": null
    },
    {
      "label": "XX路以东地块住宅项目",
      "confidence": 0.75,
      "evidence_type": "gov_publicity",
      "evidence": "区自然资源局规划公示（转述）：该地块拟建住宅……",
      "source_url": "https://xx.gov.cn/...",
      "publicity_date": "2024-06-15"
    },
    {
      "label": "住宅类建设项目，具体名称未知",
      "confidence": 0.40,
      "evidence_type": "inferred",
      "evidence": "四轮政府 Web 检索无强匹配；根据区域和建筑证据推断",
      "supported_by": "region_type[0] 住宅小区 + possible_buildings[0] 多层住宅楼"
    }
  ]
}
```

## 硬性规则

1. 每个候选列表至少 1 条，不能为空。
2. `confidence` 必须位于 0–1，并按降序排列（允许相等）。
3. `evidence` 必须具体、可追溯，说明属性字段、MCP 数据源、POI 名称、OSM 标签、政府公示或几何特征。
4. 禁止无来源地虚构具体项目名称。
5. `data_source` 要如实反映真正起作用的**地图/API 数据源**（见下）；政府 Web 证据通过 `related_projects[].evidence_type` 体现。
6. 不要把 MCP 的原始 API 响应或政府网页全文复制进最终结果；只保留支持结论所需的证据摘要。

## `data_source` 与 `hybrid`

`data_source` 描述 **amap / baidu / osm / offline / hybrid** 中地图与 MCP 在线 API 的参与情况。`hybrid` 表示以下**任一**（在 evidence 中说明具体哪种）：

1. **多地图源**：两个及以上 map 源（amap/baidu/osm）均 `status=ok` 并参与结论；
2. **地图 + 离线补充**：部分 map 源失败或为空，用几何/属性弱证据补足；
3. **地图 + 政府 Web**：map 证据与政府公示检索共同支撑 `related_projects`（政府侧用 `evidence_type: gov_publicity` 等标注，不改变顶层 `data_source` 枚举）。

单源成功仍用 `amap` / `baidu` / `osm`；全无在线 map 源用 `offline`。

## `related_projects` 的特殊要求

`related_projects` 是本 schema 的**主结论**，`region_type` 与 `possible_buildings` 是其推理依据。

### 必填字段（每条候选）

| 字段 | 说明 |
|------|------|
| `label` | 项目名称或类型化描述 |
| `confidence` | 0–1 |
| `evidence` | 可追溯摘要（转述，非全文摘抄） |
| `evidence_type` | 显式证据类型（见下表） |

### 可选字段

| 字段 | 说明 |
|------|------|
| `source_url` | `gov_publicity` **必填**；`gov_publicity_weak` 建议填 |
| `publicity_date` | 政府公示日期，建议填写 |
| `supported_by` | `evidence_type=inferred` **必填** |

### `evidence_type` 与置信度上限

| evidence_type | 含义 | confidence 上限 |
|---------------|------|-----------------|
| `gov_publicity` | 政府公示 + 地址/道路可对应本地块 | 可 >0.6；须 `source_url` |
| `gov_publicity_weak` | 仅确认同区域有建设活动，未能对应本地块 | **≤0.3** |
| `poi_name` | 地图 POI 直接项目名 | 可 >0.6 |
| `attribute_field` | 属性表项目名/编号/许可 | 可 >0.6 |
| `project_number` | 明确项目/规划/备案编号 | 可 >0.6 |
| `inferred` | 由 region_type + possible_buildings 间接推断 | **≤0.4**；须 `supported_by` |

校验脚本读 `evidence_type` 判断上限，**不**从 evidence 自由文本猜测是否「有直接证据」。

### 其他规则

1. 直接证据（上表前五种之一）可省略 `supported_by`。
2. 没有直接项目线索时，不得把区域类型包装成真实项目名称。
3. 只有 `evidence_type` 为直接类型且证据充分时，`confidence` 才允许 >0.6。
