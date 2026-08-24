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
      "evidence": "MCP 查询到 POI name=XX花园二期建设项目；名称是直接项目线索"
    },
    {
      "label": "住宅类建设项目，具体名称未知",
      "confidence": 0.40,
      "evidence": "未发现直接项目名称，只能根据区域和建筑证据推断",
      "supported_by": "region_type[0] 住宅小区 + possible_buildings[0] 多层住宅楼"
    }
  ]
}
```

## 硬性规则

1. 每个候选列表至少 1 条，不能为空。
2. `confidence` 必须位于 0–1，并按降序排列（允许相等）。
3. `evidence` 必须具体、可追溯，说明属性字段、MCP 数据源、POI 名称、OSM 标签或几何特征。
4. 禁止无来源地虚构具体项目名称。
5. `data_source` 要如实反映真正起作用的数据：`amap`、`baidu`、`osm`、`offline` 或 `hybrid`。
6. 不要把 MCP 的原始 API 响应复制进最终结果；只保留支持结论所需的证据摘要。

## `related_projects` 的特殊要求

`related_projects` 是本 schema 的**主结论**，`region_type` 与 `possible_buildings` 是其推理依据。

1. 如果证据直接来自项目名称、项目编号、规划/备案字段、政府公示信息或明确 POI `name`，应优先写明直接来源；此时 `supported_by` 可以省略。
2. 如果项目判断是间接推理，必须填写 `supported_by`，明确引用 `region_type` 和/或 `possible_buildings` 的候选。
3. 没有直接项目名称/编号/公示线索时，不得把区域类型直接包装成真实项目名称，应使用类型化描述。
4. 仅凭区域类型或建筑判断，`related_projects.confidence` 不得超过 0.4。
5. 只有存在直接项目名称、备案/规划编号、政府公示信息，或明确名称 + 建设状态组合证据时，`related_projects.confidence` 才允许超过 0.6。
