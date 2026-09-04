---
name: geo-region-inference
description: >-
  Infers related construction projects from geographic parcels/regions, using
  region type and possible buildings as supporting evidence, from SHP-derived GeoJSON/JSON
  (geometry + coordinates) whose attribute table lacks descriptive text, ranking conclusions
  by confidence with traceable evidence. Use this skill when a user has shapefile, GeoJSON,
  or similar geo data with sparse or missing descriptive attributes, and asks what project,
  region, or buildings a parcel may correspond to.
---

# 地理区域语义推断

从 SHP/GeoJSON 的几何和属性数据中，**优先寻找相关建设项目（`related_projects`）**。
`region_type`、`possible_buildings` 是项目判断的中间证据，不是并列结论。

## 全局硬约束

以下规则贯穿全程，任何一步都成立。几何计算、坐标处理、多源查询与校验一律交给 MCP，
Skill 只做推理流程、证据分级与置信度判断。

**工具边界**
- `calculate_geometry`、`search_project_evidence` **不是**默认链式步骤；仅在调试、单点补查或
  `analyze_regions` 无法完成时单独调用。
- `calculate_geometry` 与 `analyze_regions` 共用同一套几何校验（残余 Esri 扫描、fail-fast、
  `GEOMETRY_INVALID`），单独调它复现同样的报错属预期，不是新问题。
- Skill 正常路径**不要**执行 `python scripts/*.py`。

**结论与置信度**
- 每条 `related_projects` **必须**填 `evidence_type`；校验读该字段，不从 evidence 文本猜。
- 没有直接项目线索时 `confidence` **不得超过 0.4**，且须用 `supported_by` 指向具体的
  `region_type` / `possible_buildings` 候选。
- 即使 `region_type` 置信度很高，也不能因此提高具体项目名称的置信度。
- 没有来源支持时**禁止编造**具体项目名：写「住宅类建设项目，具体名称未知」这类类型化描述，
  并明确标注为「推断」。
- 信息少时宁可只给 1 条可靠候选，不要为凑数量制造弱证据。
- 多个来源明显矛盾时，不要只选「看起来更像」的一个，把冲突保留进 evidence 并在汇报时说明。

## 第一步：理解输入

不假设固定字段名——属性表可能几乎为空，也可能有专业代码字段。先识别地物数量和字段结构；
若输入明显只有一个地物且字段一目了然，可直接处理。

- **ArcGIS 用户**：优先导出标准 GeoJSON；若误传 Esri REST JSON（`rings`/`attributes`），MCP 会尝试自动转换（合法多环保留孔洞），无法转换时提示重新导出。
- **大文件/顶点多**：用 `input_path` 指向本地文件，不要把整份 JSON 贴进对话。
- **SHP 输入**：须先由宿主/文件工具转成 GeoJSON；宿主无法转换时如实告知这是输入层限制，
  不要在 Skill 里重新实现 GIS 解析栈。
- **投影坐标无 CRS**：`|x|>180` 或 `|y|>90` 且未声明坐标系时 MCP **直接报错**，不要当 WGS84 硬算。

## 第二步：批量几何 + 在线证据

调用 `analyze_regions`，传入整个 FeatureCollection 或 `input_path`，**一次**处理整批——
不要逐地物、也不要逐数据源单独调工具。

**`input_alerts` / `online_summary` 处置**（这些结论必须带进第五步的汇报）：
- `CRS_ASSUMED` → 提醒用户确认位置（文件未声明坐标系，已假定 WGS84）
- **`GEOMETRY_INVALID`** → **必须**列出 `invalid_indices`（及 `invalid_reasons` 若存在），说明这些地物结果不完整，**不得**当作全量成功；`invalid_reasons` 含 `residual_esri_keys` 时提示 ArcGIS 导出标准 GeoJSON
- **`GEOMETRY_SIMPLIFIED`** → **必须**列出 `feature_indices`；有 `simplify_reasons` 时按 reason 说明（`esri_ring_roles_unresolved` = 环角色无法判定已拆部件；`paths_converted` = 线要素转换），面积/形状可能不准确
- 抛出「invalid geometry … GEOMETRY_FAIL_RATIO」类 `ValueError`（无效占比默认 ≥0.5）→ **整批失败**，不要当部分成功；拆出有效地物后重跑
- `all_channels_unavailable=true` → 在线源不可用（缺 Key/网络），**不是**「此地无项目」，结论不得写成无项目
- `batch_retry_recommended=true` → **必须**建议拆分更小批次重跑，可参考 `batch_retry_reason`
  中的 `feature_ratio` 判断拆分幅度；见 [error_codes.md](references/error_codes.md)

**status 处理**（不要盲目重试）：
- `unavailable`（无 Key 或该源被关闭）→ 不重试
- `error` + `reason_code=INVALID_API_KEY` → 本次任务永久跳过该源
- `error` + `RATE_LIMIT`/`TIMEOUT` → 服务端已退避，不必立即重试；是否分批看上面的 `batch_retry_recommended`
- `empty` → 接口成功但附近无结果，属正常情况

**flag 语义**：`search_projects=true`（默认）已用项目关键词检索 POI；`search_poi` 不叠加
第二套泛搜，也关不掉项目检索；两者都 `false` 时不访问任何在线源。

无直接项目证据时该工具会自动扩圈补查一次；**扩大范围查到的普通 POI 不能直接当作目标地块
本身的证据**（定级见第四步）。返回字段定义见 [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md)。

大批量（如 >10 地物）前可调用 **`check_api_status(probe_mode=burst)`** 诊断并发限流——**仅探测高德**，
百度在该模式下仍只做验活，不要据此声称已诊断全部数据源；`single` 模式仅验活 Key。

## 第三步：政府公示 Web 检索（可选）

在第二步返回**含行政区划**的证据后，对尚无 MCP 直接 `project_evidence` 的地物，调用
`prepare_gov_web_search(analyze_result)`，MCP 返回**四轮** `search_plan`（街道核心词 →
街道同义词 → 区级+道路交叉 → 纯地名/间接线索）。Agent 用 `web_search`/`web_fetch`
按轮执行。详细 SOP 见 [references/gov_web_search_guide.md](references/gov_web_search_guide.md)。

要点：
- **宁可多搜，不可漏搜**：无公示也要判断是否**与本地块**匹配；查不到是常态，但要多轮尝试后再下结论
- **分级推进**：本轮无 `.gov.cn` 命中 → 下一轮；**strong 匹配** → 停止后续轮次
- 产出 `gov_web_notes`（含 `source_url`、`match: strong|weak`），供第四步定级
- 禁止把同一篇公示里所有项目都收进结果；禁止整段摘抄网页原文
- 无 `web_search` 能力时跳过本步，在 evidence 中说明未做政府 Web 检索，**不是** pipeline 失败
- 无 map 源时从 properties/用户说明提取线索自行 `web_search`；`data_source` 场景 4 见 [output_schema.md](references/output_schema.md)

## 第四步：语义推理 —— 目标是 `related_projects`

证据优先级（从高到低，命中即用，不需要绕道下一级）：

| 优先级 | 证据 | `evidence_type` | 置信度上限 |
|---|---|---|---|
| 1 | POI 名称 / 属性字段 / 政府公示直接给出项目名 | `poi_name` / `attribute_field` / `gov_publicity` | 可 >0.6（`gov_publicity` 须 `source_url`） |
| 2 | 明确的项目/规划/备案编号 | `project_number` | 可 >0.6 |
| 3 | 名称 +「在建/建设中/工地」等状态组合 | `poi_name`（含状态修饰） | 视完整度而定 |
| 4 | `construction=*` 等直接建设标签 | `attribute_field` | 中等 |
| 5 | 地址/开发主体/业务区域等辅助线索 | `inferred` | ≤0.4，需 `supported_by` |
| 6 | 仅同区域有建设活动，未对应本地块 | `gov_publicity_weak` | ≤0.3 |
| 7 | 纯靠 `region_type`/`possible_buildings` 反推 | `inferred` | ≤0.4，需 `supported_by` |

第二步扩圈补查到的普通 POI 不是本地块的直接证据，**不得**按第 1 级定级。

`region_type`/`possible_buildings` 不追求独立的高精度。分类词汇见
[references/landuse_taxonomy.md](references/landuse_taxonomy.md)；项目线索与扬尘源台账类
属性字段（`BH`/`SGQK`/`XZMC`/`Area`）见 [references/project_inference_signals.md](references/project_inference_signals.md)。

## 第五步：输出、校验与呈现

严格按 [references/output_schema.md](references/output_schema.md) 组织结果。每个地物一组，
展示顺序固定为**项目结论 → 项目直接证据 → 区域类型/建筑（推理依据）**——不要做成三个视觉上
完全等价的栏目。

完成后对每个地物各调一次 `validate_result({"result": <单个对象>})`；失败按返回的 `errors` 修正后重试。

汇报时**必须**带上第二步 `input_alerts` / `online_summary` 的处置结论（无效几何、简化告警、
在线源不可用、建议拆分重跑），不得只报成功部分。

**混合批次（同一 FeatureCollection、空间上可分成多簇）**：逐地物给 `related_projects`，
不要把整批收成一个项目名。不同施工片区即使邻近也不合并。无直接 POI/公示证据的地物按全局
约束写「具体项目名称未知」，不要借用邻块项目名。
