# 政府公示 Web 检索指南

在 `analyze_regions` 拿到行政区划与道路线索之后、最终语义推理之前，用 **Agent 自带的 `web_search` / `web_fetch`** 检索政府网站公示信息。本步骤**不进 MCP 爬虫**，也**不要**写成 `scripts/` 固定脚本。

前置工具：MCP **`prepare_gov_web_search`**（仅生成四轮搜索计划，无网络）。

流程总览见 [SKILL.md](../SKILL.md) 第三步；`evidence_type` / `source_url` 见 [output_schema.md](output_schema.md#evidence_type-与置信度上限)。

## 1. 触发与跳过

```text
analyze_regions
       ↓
prepare_gov_web_search(analyze_result)
       ↓
candidate_count = 0 → 跳过（正常，非失败）
candidate_count > 0 → 对每个 candidate 按轮检索
```

**empty_only**：MCP 已有 `project_evidence` 的地物不会进入 `candidates`。

**前置条件**：至少能构建 **区/县级** 行政区（`admin.district_label`）。无区县级则跳过 MCP 计划（见下节 **无 map 源时的 Agent 检索**）。

### 无 map 源时的 Agent 检索

下列情况 `prepare_gov_web_search` 可能返回 `candidate_count = 0`（常见原因：`skipped_summary.no_admin`、全部 map 源 `unavailable`/`empty`）——**不等于**禁止 Web 检索：

1. 读 `analyze_regions` 的 `online_summary`；若 `all_channels_unavailable`，说明无 map 在线证据。
2. 从**输入数据**提取检索线索（优先级从高到低）：
   - 属性中的**地址、地块编号、项目/备案/规划编号、行政区字段**；
   - 用户文件名、对话中给出的地名/道路/项目名；
   - MCP 几何/bbox 仅作弱辅助（勿单独当地名）。
3. Agent **自行构造** `web_search` query（不必等 MCP 四轮模板）；仍优先 `.gov.cn`，匹配规则与分轮 SOP 相同。
4. 写最终结果：`data_source: hybrid`（**场景 4**：无 map 源 + 输入线索 Web 检索）；`related_projects` 用对应 `evidence_type`（政府强证据仍须 `source_url`）。

若输入中也无任何可检索文字线索，才在 `offline` 下仅用几何/属性做 `inferred`（≤0.4）。

## 2. 分轮穷尽机制

对每个 candidate，**严格按 `search_plan.rounds` 顺序**执行，不要用 flat 列表一次性搜完。

四轮粒度逐级放宽：`street_core`、`street_synonyms` 为街道级（无街道则降级区县），
`district_road_cross` 升到区/县级并与道路名交叉，`place_indirect` 退到纯地名兜底
（可在 JSON 里用 `enabled` 关闭）。每轮的 `name`、`admin_level`、`queries` 由
`prepare_gov_web_search` 直接返回，照用即可；模板可在
[gov_search_templates.json](gov_search_templates.json) 配置扩展。

### 轮间推进规则

1. 执行 Round N 的**全部** `queries`（累计 search 不超过 `limits.max_search_per_feature`，默认 24）。
2. 检查本轮搜索结果是否出现 **`.gov.cn`** 域名。
3. **本轮无任何 gov 域名** → 进入 Round N+1（不要在本轮内重复已试过的 query）。
4. **本轮有 gov 链接** → `web_fetch`（每地物最多 `max_fetch_per_feature`，默认 4）→ 做地址/道路匹配：
   - **strong**：公示道路/地名与 `match_roads` 或地块地址可对应 → 记录证据；若 `stop_on_confirmed_match` 为 true → **停止后续轮次**。
   - **weak**：仅确认同区有建设活动、无法对应本地块 → 记录 weak；可继续下一轮找更强证据。
5. **四轮均完成且从未出现 gov 域名**（或仅有 weak 且无 strong）→ 本指南检索阶段结束；进入 **SKILL 第四步**语义推理，用 map/几何做 `inferred`，**不得虚构 gov 证据**。

## 3. Agent 操作要点

- **优先**点击 `.gov.cn` 结果；非 gov 站点仅作线索，不得当作政府公示直接证据。
- **不要**把同一篇公示里列出的所有项目都收进 `related_projects`；必须做**地址关键词匹配**筛选。
- **版权**：禁止整段摘抄公示原文；项目正式名称可短引一次；其余转述；每个 `source_url` 仅引一次。
- evidence 写**摘要**；URL 写入结构化字段 `source_url`（见 [output_schema.md](output_schema.md#evidence_type-与置信度上限)）。

### gov 检索中间产物（供 SKILL 第四～五步）

```jsonc
{
  "feature_index": 0,
  "gov_web_notes": [
    {
      "source_url": "https://xx.gov.cn/...",
      "publicity_date": "2024-06",
      "match": "strong",
      "project_label_hint": "XX路以东地块住宅项目",
      "summary": "转述公示要点……"
    }
  ]
}
```

写入 `related_projects` 时据此填写 `evidence_type`、`source_url`、`publicity_date`（类型与置信度上限见 [output_schema.md](output_schema.md#evidence_type-与置信度上限)）。Round 3 区级公示默认倾向 **weak**，除非道路交叉匹配明确。

## 4. 宿主能力

需要 Agent 具备 **`web_search`** 与 **`web_fetch`**（如 Cursor 内置或 agent-reach）。不可用则跳过 SKILL 第三步，在 evidence 中说明未做政府 Web 检索。

## 5. 限制

- 分轮不能覆盖所有政府措辞，但比固定 4+2 模板更接近「穷尽合理表达」。
- 搜索引擎对区县级子站收录不全，仍可能漏检。
- 批量任务：`empty_only` 缩小候选数；禁止对非 candidate 地物检索。
- `evidence_type` 与 `source_url` 校验**不能保证** Agent 真访问过 URL，仅保证输出结构合规。
