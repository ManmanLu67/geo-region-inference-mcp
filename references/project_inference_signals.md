# 项目推理线索参考

本文件服务于 `related_projects` 推理。核心原则是：**直接项目线索优先于间接类型联想**。
`region_type` 与 `possible_buildings` 是项目判断的辅助证据，不应反过来替代项目证据。

## 1. 属性字段中的项目线索

不要求预先知道具体编码规则。只要字段语义或命名明显可能与工程、规划、备案、许可相关，就应作为线索记录，并在最终 evidence 中保留原字段和值。

常见字段名信号：

| 字段示例 | 可能含义 | 推理时的处理 |
|---|---|---|
| `project_no` / `project_id` / `project_code` | 项目编号 | 直接项目线索；若无法查明全称，仍记录编号 |
| `plan_id` / `plan_no` / `planning_id` | 规划编号 | 直接规划项目线索 |
| `permit_no` / `permit_id` | 许可编号 | 直接工程/建设许可线索 |
| `construction_no` / `construction_id` | 建设工程编号 | 高价值项目线索 |
| `land_parcel` / `parcel_no` | 地块编号 | 可用于关联规划或出让项目，但通常不能单独证明具体项目名称 |
| `fj_code` / 类似工程编码 | 行业/工程编码 | 不认识编码规则也要记录，作为待核实项目编号线索 |
| `project_name` / `name` / `title` | 名称 | 若值包含项目、工程、建设、开发等语义，优先作为项目名称证据 |
| `status` / `phase` | 建设状态/阶段 | 与项目名称组合时价值显著提升，例如“在建”+项目名称 |

**禁止规则**：仅凭字段名“看起来像项目编号”就虚构编号对应的具体项目名称；只能说“存在项目编号线索，尚未核实全称”。

## 2. POI / 地图名称中的直接信号

优先寻找以下模式：

- 名称包含“项目”“建设项目”“建设工程”“开发项目”“工程”“工地”；
- 名称包含“在建”“建设中”“施工中”等状态词；
- POI 名称本身是明确项目名称，而不是泛化类别（如“住宅小区”“商场”）；
- 地址、业务区域、反向地理编码结果出现项目名称、开发主体或项目编号。

对中国城市地图数据，`businessAreas` 等业务区域信息可提供行政/商圈背景，但除非其中明确出现项目语义，否则不要将普通商圈名直接当作项目名。

## 3. OSM / 开放地图建设状态信号

以下线索可作为直接或较强的建设状态证据：

- `construction=*`；
- POI / feature `name` 中带“在建”等明确建设状态；
- building / landuse 与项目名称同时存在的对象。

注意：`building=construction` 或 `construction=*` 说明处于建设状态，但**不等于具体项目名称已知**；只有名称、编号或其他直接证据同时出现时，才可提高具体项目名称的置信度。

## 4. 区域类型和建筑作为间接证据

当没有直接项目名称时，可以使用：

`region_type` → `possible_buildings` → 类型化项目描述

例如：

- `region_type=住宅小区` + `possible_buildings=住宅楼` → “住宅类建设项目，具体名称未知”；
- `region_type=教育用地` + `possible_buildings=教学楼/宿舍` → “教育设施建设项目，具体名称未知”；
- `region_type=工业园区/厂房` + `possible_buildings=厂房/仓库` → “工业园区建设项目，具体名称未知”。

这类结论属于**间接推断**，即使区域类型和建筑判断置信度很高，`related_projects.confidence` 仍不应超过 0.4，除非补充了直接项目线索。

## 5. 查询范围与项目证据补查

基础半径首先按地块尺寸计算。若第一轮没有项目名称/编号/建设状态线索，可将半径放大到原值的 2–3 倍，并专门搜索：

- 临近主干道或路口的项目 POI；
- 地块边缘外的“项目/工地/建设”类 POI；
- 反向地理编码的名称、地址和业务区域线索。

扩大范围只用于补充证据，不能把“附近存在某项目”自动等同于“该项目就是该地块对应项目”。必须说明空间关系与证据强弱。

## 6. 证据优先级

建议按以下顺序理解证据强弱：

1. 明确项目名称 + 项目编号/备案/规划信息；
2. 明确项目名称 + “在建/建设中/工程”等状态信息；
3. 属性字段直接给出项目名称或项目编号；
4. POI 名称直接包含项目语义，但缺少编号/公示核验；
5. OSM 建设状态 + 邻近命名对象；
6. `region_type` + `possible_buildings` 推导出的类型化项目描述；
7. 仅凭几何形状联想项目类型。

只有前 3–4 类直接项目线索通常足以支持具体项目名称的较高置信度。后续类别应明确标注为间接推断。

## 7. 政府公示信息

Skill 默认地图源为高德、百度、OSM/Overpass；**政府公示 Web 检索**在 `analyze_regions` 之后由 Agent 执行（MCP `prepare_gov_web_search` 生成四轮搜索计划，Agent 用 `web_search`/`web_fetch` 检索 `.gov.cn`）。

流程见 [gov_web_search_guide.md](gov_web_search_guide.md)。**未完成第三步检索前**，不要写 `evidence_type: gov_publicity` 或假装已核验政府公示。

政府 Web 强证据应优先提取：项目名称、建设单位、项目/备案/规划编号、地块编号、地址、公示日期；写入 `related_projects` 时用 `evidence_type: gov_publicity`（strong 匹配，须 `source_url`）或 `gov_publicity_weak`（仅同区活动，≤0.3）。

政府 Web 证据与 map 源共同支撑结论时，顶层 `data_source` 仍描述 map 参与情况；gov 侧通过 `evidence_type` 体现（见 output_schema hybrid 第 3 种场景）。

**无 map 源时**：若属性/用户说明中有地址、编号、项目名等可检索线索，Agent 仍应做 Web 检索（`prepare_gov_web_search` 无 admin 时可自行组 query）；项目结论由 Web 证据支撑时，`data_source` 用 **`hybrid`（第 4 种：无 map + 输入线索 Web）**，勿误标为已有 map 核验。
