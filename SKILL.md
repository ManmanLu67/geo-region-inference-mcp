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

## 架构

Skill 负责推理流程、证据优先级、置信度规则；MCP（`geo-region-inference` Server）负责
几何计算、坐标处理、多数据源并发查询、结果预过滤与校验。架构原理、并发设计图示见 [README.md](README.md)。

正常流程只调用 `analyze_regions` **一次**处理整批地物，不要逐地物或逐数据源单独调工具，
不要执行 `python scripts/*.py`（已废弃，见 [scripts/README.md](scripts/README.md)）。

`calculate_geometry`、`search_project_evidence` **不是**默认链式步骤；仅在调试、单点补查或
`analyze_regions` 无法完成时单独调用。

```text
analyze_regions(整批 GeoJSON/FeatureCollection 或 input_path)
        ↓
prepare_gov_web_search（可选：行政区划已知且无直接证据时）
        ↓
LLM 语义推理（含 evidence_type / source_url）
        ↓
validate_result
```

## 第一步：理解输入

不假设固定字段名——属性表可能几乎为空，也可能有专业代码字段。先识别地物数量和字段结构；
若输入明显只有一个地物且字段一目了然，可直接处理。

- **ArcGIS 用户**：优先导出标准 GeoJSON；若误传 Esri REST JSON（`rings`/`attributes`），MCP **v2.5+** 会尝试自动转换，有损时发 `GEOMETRY_SIMPLIFIED` 告警；无法转换时提示重新导出。
- **大文件/顶点多**：用 `input_path` 指向本地文件，不要把整份 JSON 贴进对话。
- **SHP 输入**：先由宿主/文件工具转换为 GeoJSON，不要让 MCP 自己加载 GDAL/Fiona/
  GeoPandas；若宿主无法转换，如实告知这是输入层限制，不要在 Skill 里重新实现 GIS 解析栈。

## 第二步：批量几何 + 在线证据

调用 `analyze_regions`，传入整个 FeatureCollection 或 `input_path`，不要逐地物调用。

**向用户汇报前必读**：
- `input_alerts` 含 `CRS_ASSUMED` → 提醒用户确认位置（文件未声明坐标系，已假定 WGS84）
- `input_alerts` 含 **`GEOMETRY_INVALID`** → **必须**列出 `invalid_indices`，说明这些地物结果不完整，**不得**当作全量成功
- `input_alerts` 含 **`GEOMETRY_SIMPLIFIED`** → **必须**列出 `feature_indices`，说明 Esri 转换可能使面积/形状偏大
- `online_summary.all_channels_unavailable=true` → 说明在线源不可用（缺 Key/网络），**不是**「此地无项目」
- `online_summary.batch_retry_recommended=true` → **必须**建议拆分重跑（≤5 地物/批）或调低 `AMAP_QPS_LIMIT`；见 [error_codes.md](references/error_codes.md)

**flag 语义**：`search_projects=true`（默认）已用项目关键词检索 POI，`search_poi`
**不会**叠加第二套泛搜；`search_poi=false` 关不掉项目检索；两者都 `false` 时不访问任何
在线源。

该工具一次性完成：几何统计（质心/面积/紧凑度/长宽比）→ 按地块尺寸生成初始半径 →
高德/百度/OSM 并发查询（项目关键词优先；**默认 QPS≈3、批大小 5、批间 2s**）→ 无直接项目证据时扩大半径补查一次（**2.5×**）
→ 结果预过滤压缩。**扩大范围查到的普通 POI 不能直接当作目标地块本身的证据。** 字段定义
见 [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md)。

大批量（如 >10 地物）前可调用 **`check_api_status(probe_mode=burst)`** 诊断 CUQPS；`single` 模式仅验活 Key。

**数据源配置**：高德需 `AMAP_KEY`，百度需 `BAIDU_AK`，OSM 免 Key 但受公共服务限流影响
（`OVERPASS_URL` 可指向备用实例，沙箱拦截时设 `OSM_ENABLED=false` 整体跳过）。见
[references/map_api_setup.md](references/map_api_setup.md)。

**status 处理**（不要盲目重试）：
- `unavailable`（无 Key / 被 `OSM_ENABLED=false` 关闭）→ 不重试
- `error` + `reason_code=INVALID_API_KEY` → 本次任务永久跳过该源
- `error` + `RATE_LIMIT`/`TIMEOUT` → 服务端已退避；若 `batch_retry_recommended` 仍 true，建议分批重跑
- `empty` → 接口成功但附近无结果，属正常情况

多个来源明显矛盾时，不要只选「看起来更像」的一个，将冲突保留进 evidence。

## 第三步：政府公示 Web 检索（可选）

在第二步返回**含行政区划**的证据后，对尚无 MCP 直接 `project_evidence` 的地物，调用
`prepare_gov_web_search(analyze_result)`，MCP 返回**四轮** `search_plan`（街道核心词 →
街道同义词 → 区级+道路交叉 → 纯地名/间接线索）。Agent 用 `web_search`/`web_fetch`
按轮执行，不是 `scripts/` 脚本。详细 SOP 见
[references/gov_web_search_guide.md](references/gov_web_search_guide.md)。

要点：
- **宁可多搜，不可漏搜**：无公示也要判断是否**与本地块**匹配；查不到是常态，但要多轮尝试后再下结论
- **分级推进**：本轮无 `.gov.cn` 命中 → 下一轮；**strong 匹配** → 停止后续轮次
- 产出 `gov_web_notes`（含 `source_url`、`match: strong|weak`），写 `related_projects`
  时对应填 `evidence_type`；政府强证据必填 `source_url`
- `gov_publicity`（strong）可 >0.6；`gov_publicity_weak`（仅同区活动、未对应地块）≤0.3；
  四轮均无 gov 强证据后用 `inferred`（≤0.4）
- 禁止把同一篇公示里所有项目都收进结果；禁止整段摘抄网页原文
- 无 `web_search` 能力时跳过本步，在 evidence 中说明未做政府 Web 检索，**不是** pipeline 失败
- 无 map 源或 `candidate_count=0` 时：从 properties/用户说明提取地址等线索自行
  `web_search`，有支撑时 `data_source` 用 `hybrid`（场景 4），见
  [references/output_schema.md](references/output_schema.md)

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

**规则**：
- 每条 `related_projects` **必须**填 `evidence_type`；校验读该字段，不从 evidence 文本猜
- 没有直接项目线索时 `confidence` **不得超过 0.4**（`inferred` / 弱 gov）
- 即使 `region_type` 置信度很高，也不能因此提高具体项目名称的置信度
- 间接判断须 `supported_by` 指向具体的 `region_type` / `possible_buildings` 候选
- 没有来源支持时禁止编造具体项目名，用「住宅类建设项目，具体名称未知」等类型化描述
- 信息少时宁可只给 1 条可靠候选，不要为凑数量制造弱证据

`region_type`/`possible_buildings` 只是支撑和解释项目判断的中间证据，不追求独立的高精度。
分类词汇见 [references/landuse_taxonomy.md](references/landuse_taxonomy.md)；项目线索见
[references/project_inference_signals.md](references/project_inference_signals.md)。

## 第五步：输出 + 校验

严格按 [references/output_schema.md](references/output_schema.md) 组织结果，展示顺序固定为：
**项目结论 → 项目直接证据 → 区域类型/建筑（推理依据）**。

完成后对每个地物各调一次 `validate_result({"result": <单个对象>})`；失败按返回的 `errors` 修正后重试。不要执行
`python scripts/validate_output.py`（已废弃）。

扬尘源台账等属性线索（如 `BH`/`SGQK`/`XZMC`/`Area`）见 [project_inference_signals.md](references/project_inference_signals.md)。

## 第六步：呈现给用户

每个地物一组，先给出 `related_projects` 主结论，再说明支持它的区域类型/建筑证据——
不要做成三个视觉上完全等价的栏目。间接推断要写明「推断」「具体名称未知」；多来源冲突要
说明冲突，不要只挑一个隐藏分歧。

## 性能与 MCP 宿主

- 不要逐地物启动 Python 进程、不要逐数据源串行调 Tool、不要把 API 原始响应整包塞给 LLM
- MCP Server 为常驻进程；将依赖安装到持久 `.venv` 并在 Host 配置中指向该 Python（见 [MCP_SETUP.md](MCP_SETUP.md)）

## scripts/（已废弃）

**正常 Skill 流程禁用。** 仅当 MCP 完全不可用且只需粗略几何时应急；`geo_stats.py` 质心与 MCP
不同（顶点平均 vs 面积加权），勿与 MCP 结果对比。详见 [scripts/README.md](scripts/README.md)。
