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
`region_type` 和 `possible_buildings` 是项目判断的中间证据，不是与项目并列的最终目标。

## 架构边界：Skill 与 MCP 的职责

本 Skill 不再通过 `python scripts/*.py` 逐个启动脚本，也不负责调度高德 → 百度 → OSM 的
串行 fallback。正常执行路径是调用本 Skill 配套的 **`geo-region-inference` MCP Server**。

- **Skill 负责**：任务流程、证据优先级、项目语义推理、置信度校准、最终输出结构。
- **MCP 负责**：几何计算、坐标处理、在线 API、并发查询、项目关键词检索、半径补查、结果预过滤。
- **LLM 负责**：从 MCP 返回的结构化证据中归纳区域类型/建筑，并以这些中间证据支撑最终项目判断。

正常工作流优先使用：

```text
analyze_regions(整个 GeoJSON/FeatureCollection)
        ↓
prepare_gov_web_search → Agent 分轮 web_search/fetch（可选）
        ↓
LLM 做语义推理（含 evidence_type / source_url）
        ↓
validate_result(最终结果)
```

不要把单个 `calculate_geometry`、`search_project_evidence` 等工具当成默认链式步骤；只有在
调试、补查或 `analyze_regions` 无法完成时才单独调用。

## 第一步：理解输入

检查用户提供的 JSON 结构和属性字段。不要假设固定字段名——属性表可能几乎为空，也可能有
专业代码字段。先识别地物数量和属性字段名；若输入明显只有一个地物且字段一目了然，可以直接处理。

**ArcGIS 用户（推荐流程）**

1. 在 ArcGIS 中将图层 **导出为 GeoJSON**（`.geojson` 或 `.json`）。
2. 坐标系优先选 **WGS 1984 (EPSG:4326)**；若数据为其他，导出时保留坐标系信息即可，MCP 会自动重投影到 WGS84。
3. **不要**直接把 Esri REST JSON（含 `rings`/`attributes` 的 ArcGIS 接口格式）传给 MCP；若误传，MCP 会提示重新导出为 GeoJSON。

**大文件 / 顶点多**

使用文件路径，不要把整份 JSON 贴进对话：

```text
analyze_regions(input_path="D:/项目/地块.geojson")
```

如果用户提供的是 SHP：

- 优先让宿主/文件工具先把 SHP 转换为 GeoJSON/JSON，或提供已转换结果；
- 不要要求 MCP Server 自己加载 GDAL/Fiona/GeoPandas；当前服务器就是为了避免这种重型依赖；
- 如果当前宿主无法把 SHP 内容转换为 MCP 可读取的结构化 JSON，应明确指出这是输入层问题，而不是在
  Skill 中重新实现完整 GIS 文件解析栈。

## 第二步：批量计算几何并获取在线证据

调用 MCP 工具：

```text
analyze_regions
```

传入整个 GeoJSON `FeatureCollection`（或单个 `Feature`），或使用 `input_path` 指向本地文件。不要逐地物调用。

**向用户汇报前必须先读**：

- `input_alerts` — 若含 `CRS_ASSUMED`，提醒用户确认位置是否合理（文件未声明坐标系，已假定 WGS84）。
- `online_summary` — 若 `all_channels_unavailable` 为 true，说明在线源不可用（缺 Key / Overpass 不通），**不是**「此地无项目」。

`search_projects=true`（默认）时已用项目关键词检索 POI，`search_poi` **不会**再打第二套泛搜。`search_poi=false` 关不掉项目检索。仅当 `search_projects=false` 时才做无项目关键词的周边检索。两旗都 `false` 时不访问高德/百度/OSM。

该工具会一次性完成：

1. 质心、bbox、面积、周长、紧凑度、bbox 长宽比等确定性几何计算；
2. 根据地块尺寸自动生成合理的初始查询半径；
3. **项目导向查询优先**：高德、百度、OSM 独立网络查询并发执行；
4. 项目线索优先检查 POI 名称、地址、业务区域、建设状态和 OSM `construction=*` 等；
5. 初次查询没有直接项目证据时，只额外执行一次约 `2–2.5×` 的扩展半径项目补查；
6. 对 API 原始结果进行预过滤和压缩，只把与语义判断有关的字段返回给 LLM。

MCP 返回字段定义见 [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md)（`sources[].status`、`reason_code` 等）。

因此，不要执行旧式 `python scripts/*.py` 链路（**已废弃**，见 [scripts/README.md](scripts/README.md)）。确定性调度已在 MCP Server 内完成。

### MCP 数据源行为

`analyze_regions` 对一个地物的独立网络查询采用并发，而不是串行 fallback。这样可以避免：

```text
高德等待 → 百度等待 → OSM等待
```

变成：

```text
高德 ─┐
百度 ─┼→ 同时返回 → 统一整理
OSM  ─┘
```

**注意**：并发不意味着无限提高请求数。MCP Server 使用有界线程池；不要为了追求速度擅自扩大并发数，
以免触发高德/百度/Overpass 限流。

### 查询范围规则

初始半径由 MCP 根据 bbox 尺寸确定；如果没有直接项目证据，MCP 最多再做一次扩大范围的项目补查。
扩大范围得到的普通 POI 不得直接视作目标地块本身的证据。

### 数据源配置

- 高德：配置 `AMAP_KEY` 才可用；
- 百度：配置 `BAIDU_AK` 才可用；
- OSM/Overpass：无需 API key，但受公共服务限流影响；可通过 `OVERPASS_URL` 指向备用/自建实例；若 Overpass 主机被沙箱 egress 拦截、纯等待超时，可设 `OSM_ENABLED=false` 直接跳过所有 OSM 请求（OSM 返回 `unavailable` + `reason_code=DISABLED`）。

Key 配置参照 [map_api_setup.md](references/map_api_setup.md)。

### 降级与异常

如果某个数据源不可用、超时或返回空结果，不要让 Agent 因此反复重试。MCP 已负责一次查询过程中的
并发、超时和单轮扩展补查；Skill 只需要判断最终证据是否足够。

看每个 source 的 `status`：`ok` / `empty` / `error` / `unavailable`。没配 Key 是 `unavailable`（`reason_code=NO_API_KEY`），不要当失败重试；OSM 被 `OSM_ENABLED=false` 关闭时也是 `unavailable`（`reason_code=DISABLED`），同样不要重试。`error` 且 `reason_code=INVALID_API_KEY` 时本任务永久跳过该源；`RATE_LIMIT` / `TIMEOUT` 可换源，不要空转重试同一 Key。`empty` 表示接口成功但附近没有结果。

如果返回的多个来源明显矛盾，不要只选“看起来更像”的一个；将冲突作为 evidence 的一部分保留。

## 第三步：政府公示 Web 检索（Agent + web 工具）

在第二步 `analyze_regions` 返回**含行政区划**的证据后，对尚无 MCP 直接 `project_evidence` 的地物，调用：

```text
prepare_gov_web_search(analyze_result)
```

MCP 返回 **四轮** `search_plan`（街道核心词 → 街道同义词 → 区级+道路交叉 → 纯地名/间接线索）。Agent 用 **`web_search` / `web_fetch`** 按轮执行，**不是** `scripts/` 脚本。

详细 SOP：[gov_web_search_guide.md](references/gov_web_search_guide.md)。

要点：

- **宁可多搜，不可漏搜**：宁可多轮尝试，无公示项目也要判断是否**与本地块**匹配；查不到是常态。
- **分级推进**：本轮所有 query 均无 `.gov.cn` 命中 → 下一轮；**strong 匹配** → 停止后续轮次。
- 第三步产出 `gov_web_notes`（含 `source_url`、`match: strong|weak`）；第四步写 `related_projects` 时填写 **`evidence_type`**，政府强证据必填 **`source_url`**。
- `gov_publicity`（strong）可 >0.6；`gov_publicity_weak`（仅同区活动、未对应地块）≤0.3；四轮均无 gov 强证据后用 `inferred`（≤0.4）。
- 禁止把同一篇公示里所有项目都收进结果；禁止整段摘抄网页原文。
- 无 `web_search` 能力时跳过本步，在 evidence 中说明未做政府 Web 检索；**不是** pipeline 失败。

## 第四步：语义推理——最终目标是 `related_projects`

**本步骤最重要、最优先得到的信息是相关建设项目。**

`region_type` 和 `possible_buildings` 的作用是帮助解释、筛选和支撑项目判断。不要把三者当成三个
同等重要、平均分配推理时间的列表。

证据链固定为：

```text
几何 + 原始属性 + MCP 在线证据
             ↓
region_type / possible_buildings   ← 中间证据
             ↓
related_projects                   ← 最终目标
```

具体执行顺序：

1. 读取 MCP 返回的几何统计，但只把面积、紧凑度、长宽比视为弱证据；
2. 从属性、landuse、building、POI、道路、建设状态等信息中归纳少量 `region_type` 与 `possible_buildings`；
3. **立即围绕 `related_projects` 做重点判断**：优先查找项目名称、项目编号、备案/规划编号、政府公示、
   “在建/建设中/工地”等直接线索；
4. 如果找到直接项目名称或编号，应优先以此作为主结论，即使区域类型推断并不特别精确；
5. 如果没有直接项目线索，只能给出类型化项目描述，不得把“可能是住宅区”包装成某个真实项目名称。

### 项目证据优先级

从高到低大致按以下顺序理解：

1. 明确项目名称（POI `name`、属性字段、政府公示）；
2. 明确项目/规划/备案编号；
3. “名称 + 在建/建设中/工地”等状态组合；
4. 直接的 `construction=*`、建设用地/工程相关标签；
5. 地址、开发主体、业务区域中的项目线索；
6. `region_type` + `possible_buildings` 的组合推断；
7. 单纯几何形状推断。

项目线索的字段和典型信号参照 [project_inference_signals.md](references/project_inference_signals.md)。

区域分类词汇参照 [landuse_taxonomy.md](references/landuse_taxonomy.md)。

### `related_projects` 置信度规则

- 每条 `related_projects` **必须**填写 `evidence_type`（见 [output_schema.md](references/output_schema.md)）；校验读字段，**不**从 evidence 文本猜 direct/indirect。
- `inferred`：**≤0.4**，须 `supported_by`。
- `gov_publicity_weak`：**≤0.3**（同区有建设活动、未能对应本地块）。
- `gov_publicity` / `poi_name` / `attribute_field` / `project_number`：有直接证据时可 >0.6；`gov_publicity` 须 `source_url`。
- 没有直接项目名称/编号/公示线索时，`related_projects.confidence` **不得超过 0.4**；
- 即使 `region_type` 置信度很高，也不能因此提高具体项目名称的置信度；
- 间接项目判断必须用 `supported_by` 指向具体的 `region_type` / `possible_buildings` 候选；
- 没有来源支持时，禁止编造具体项目名，应使用“住宅类建设项目，具体名称未知”等类型化表达；
- 信息少时宁可只给 1 条可靠候选，不要为了凑数量制造弱证据。

## 第五步：按 Schema 组织结果

严格按照 [output_schema.md](references/output_schema.md) 输出。

最终结果展示顺序固定为：

```text
相关建设项目（主结论）
    ↓
项目直接证据
    ↓
region_type / possible_buildings（推理依据）
```

### MCP 校验

完成最终结果后调用 MCP：

```text
validate_result
```

不要再执行 `python scripts/validate_output.py`（**已废弃**）。若校验失败，根据 `validate_result` 返回的 `errors` 修改后重试。

## 第六步：呈现给用户

每个地物一组，先给出 `related_projects` 主结论，再说明支持它的区域类型和建筑证据。

不要把：

- 区域类型
- 可能建筑
- 相关项目

做成三个视觉上完全等价的栏目。

如果项目名称只是间接推断，要明确写出“推断”“具体名称未知”等措辞；如果有多个来源冲突，说明冲突。

## 性能原则

本 Skill 已针对 Agent 工具调用开销做过重构：

- **不要逐地物启动 Python 进程**；
- **不要逐数据源让 LLM 串行调 Tool**；
- **不要把整个 API 原始响应直接塞给 LLM**；
- **不要重复进行同样的项目关键词查询**；
- **不要每次任务重新安装 MCP/Python 依赖**。

MCP Server 是常驻进程。MCP Host 通常启动一次 Server 子进程，然后在同一连接中复用；Python 运行时和代码不会因每个 Tool Call 重新初始化。

建议把 MCP 环境安装到一个持久虚拟环境，配置中直接指向该环境的 Python。

## scripts/（已废弃）

见 [scripts/README.md](scripts/README.md)。**勿**在 Skill 正常流程中调用；`geo_stats.py` 质心与 MCP 不同。

MCP 配置与启动说明见 [MCP_SETUP.md](MCP_SETUP.md)。
