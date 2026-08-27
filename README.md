# geo-region-inference

基于 **Skill + MCP** 的地理区域语义推断工具。

它面向这样的任务：用户提供 SHP 转换后的 GeoJSON/JSON 地物数据，几何和属性信息较丰富，但属性表缺少直接的项目描述，希望根据几何、属性、POI、OSM 建设状态等证据，**优先识别相关建设项目（`related_projects`）**，过程使用 `region_type` 与 `possible_buildings` 作为辅助解释。

---

## 1. 核心目标

本项目是围绕下面的最终目标建立证据链：

```text
几何 + 属性 + POI / OSM / 项目线索
                ↓
region_type / possible_buildings
        （中间推理证据）
                ↓
related_projects
        （最终目标）
```

三类结果关系如下：

**`related_projects` > `region_type` / `possible_buildings`**

如果能直接从 POI、属性字段、项目编号、建设状态或公示信息中找到项目线索，应优先使用直接证据，而不是先猜区域类型再“联想”项目名称。

---

## 2. 采用 Skill + MCP

项目将 AI 的“推理方法”和确定性的工具执行分开。

```text
                    Agent / LLM
                         │
             ┌───────────┴───────────┐
             │                       │
             ▼                       ▼
      geo-region-inference       MCP Server
           Skill                    │
             │              ┌───────┼────────┐
             │              ▼       ▼        ▼
             │            GIS     POI/API   校验
             │              │       │        │
             └──────────────┴───────┴────────┘
                            │
                            ▼
                      结构化证据
                            │
                            ▼
                       语义推理结果
```

### Skill 负责

- 任务流程
- 证据优先级
- 项目优先的推理逻辑
- `related_projects` 置信度规则
- `region_type` / `possible_buildings` 的辅助定位
- 输出 Schema
- 最终结果呈现

### MCP 负责

- 批量几何计算
- 坐标与查询范围处理
- 高德 / 百度 / OSM 查询
- 项目关键词检索
- 并发网络请求
- 一次自动扩大查询范围
- API 结果预过滤与压缩
- 最终结果校验

因此不再让 Agent 反复执行：

```text
python script.py
→ 等待
→ 再启动 python
→ 高德
→ 再启动 python
→ 百度
→ 再启动 python
→ OSM
```

而是尽量变成：

```text
整个 GeoJSON
    ↓
analyze_regions
    ↓
批量处理 + 并发查询 + 结果压缩
    ↓
LLM 进行语义推理
    ↓
validate_result
```

---

## 3. 项目结构

```text
geo-region-inference/
│
├── SKILL.md                         # Agent 语义工作流（读这个执行推断）
├── README.md                        # 项目概览与文档地图
├── MCP_SETUP.md                     # MCP 安装与配置
├── mcp_server.py                    # MCP Server（编排 + JSON-RPC）
├── geo_input.py                     # GeoJSON 加载、CRS 重投影、Esri 误传检测
├── geo_geometry.py                  # MCP 权威几何统计
├── geo_clients.py                   # 坐标 / httpx / 高德百度 OSM
├── validation.py                    # 输出 Schema 校验（MCP 与 CLI 共用）
├── mcp_config.example.json
├── requirements-mcp.txt             # httpx + pyproj（CRS，非 GDAL）
├── tests/
│   ├── test_offline.py
│   └── fixtures/                    # 合成测试数据（无真实地块）
│
├── references/                      # 参数/env/schema 单一事实源 → 见 README「文档地图」
│
└── scripts/                         # deprecated — 见 scripts/README.md
```

**文档地图**

| 读什么 | 文件 |
|--------|------|
| 怎么推断 | `SKILL.md` |
| 怎么装 MCP | `MCP_SETUP.md` |
| 工具参数 / env / MCP 返回字段 | `references/mcp_evidence_schema.md` |
| LLM 最终 JSON | `references/output_schema.md` |
| API Key | `references/map_api_setup.md` |

`scripts/` **已废弃**，正常路径只用 MCP；见 [scripts/README.md](scripts/README.md)。

---

## 4. 工作流
```text
SHP
 ↓
输入层转换为 GeoJSON
 ↓
geo-region-inference Skill
 ↓
geo-region-inference MCP
 ↓
结构化证据
 ↓
LLM 项目语义推理
 ↓
validate_result
 ↓
最终结果
```

### Step 1：准备输入

推荐输入：

- GeoJSON `FeatureCollection` / `Feature`（内联或 **`input_path` 指向本地文件**）
- ArcGIS 导出 GeoJSON（推荐 WGS84；投影坐标系如 EPSG:4509 须带 CRS，MCP 自动重投影）
- **不要**传入 Esri REST JSON（`rings`+`attributes`）；须先在 ArcGIS 导出 GeoJSON

大文件示例：

```text
analyze_regions(input_path="D:/项目/地块.geojson")
```

如果原始数据是 SHP，建议在输入层先转换为 GeoJSON/JSON。

### Step 2：调用 `analyze_regions`

正常任务优先调用：

```text
analyze_regions
```

一次输入整个 FeatureCollection，或使用 `input_path`。

**汇报前必读** `input_alerts` 与 `online_summary`（在线全挂 ≠ 无项目；`CRS_ASSUMED` 须提醒用户确认位置）。

该 Tool 会完成：

1. 批量几何计算；
2. 根据地块尺寸确定查询半径；
3. 高德、百度、OSM 并发查询；
4. 优先寻找项目名称、项目编号、建设状态等直接线索；
5. 没有直接项目证据时最多进行一次扩大范围补查；
6. 过滤和压缩 API 原始结果；
7. 返回供 LLM 推理的结构化证据。

### Step 3：LLM 推理

LLM 根据返回证据生成：

```text
related_projects
region_type
possible_buildings
```

但优先级仍然是：

```text
related_projects
    ↑
region_type / possible_buildings
```

### Step 4：调用 `validate_result`

最终结果通过：

```text
validate_result
```

进行规则检查。

尤其检查：

- 项目置信度是否超过证据允许范围；
- 是否存在直接项目证据；
- 间接项目推断是否填写 `supported_by`；
- 输出结构是否符合 Schema。

---

## 5. MCP Tools

### `analyze_regions`

**主工具 / 正常任务首选。**

输入整个 GeoJSON/FeatureCollection 或 `input_path`，完成批量分析和在线证据收集。返回含 `input_meta`、`input_alerts`、`online_summary`（在线查询时）。默认 `search_projects=true` 只打项目关键词，不因 `search_poi=true` 加倍请求。OSM 按最多 10 个质心合并一次 Overpass。

**参数与环境变量**详见 [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md)（单一事实源）。

**限制**：单次最多 **80** 个 feature；多环 Polygon 质心为面积加权（shoelace）。

**返回**：`features[]` 含几何统计 + `sources[]`（`status` / `reason_code` / `expanded_radius_m` 等），供 LLM 推理；不是最终语义 JSON。

适合：

- 多地物分析
- 项目识别
- 区域类型判断
- 建筑类型辅助判断

---

### `calculate_geometry`

仅做确定性的离线几何计算，例如：

- 面积
- 周长
- centroid
- bbox
- bbox 长宽比
- compactness

单次最多 **80** 个 feature。多环 Polygon 使用面积加权质心。

适合调试或补充计算，不是正常任务的首选入口。

---

### `search_project_evidence`

对指定位置执行一次项目导向的定向检索。

适合：

- `analyze_regions` 后补查某个地物；
- 用户明确要求调查某个位置；
- 调试项目线索查询。

---

### `validate_result`

对最终语义结果执行结构和证据规则校验。

---

## 6. 性能设计

本次重构的重点之一是减少 Agent 的无效等待。

### 原来的模式

```text
每个地物
  ↓
启动 Python
  ↓
调用高德
  ↓
调用百度
  ↓
调用 OSM
  ↓
LLM 再决定下一步
```

问题包括：

- Python 进程反复启动；
- 网络查询串行；
- Agent 需要多次决策；
- 原始 API 响应进入上下文；
- 多个地物不能统一批处理。

### 现在的模式

```text
整个 FeatureCollection
          ↓
   一个 analyze_regions
          ↓
┌───────────────────────┐
│ 批量几何计算           │
│ 并发 API 查询          │
│ 项目关键词优先         │
│ 一次范围扩展           │
│ API 结果预过滤         │
└───────────────────────┘
          ↓
      精简证据 JSON
          ↓
          LLM
```

MCP Server 是长驻进程。MCP Host 建立连接后，Python 运行时和 MCP Server 不需要为了每次 Tool Call 重新初始化。

**注意：MCP 不会自动加速外部 API。** 实际网络延迟仍然取决于高德、百度、Overpass 等服务。速度优化主要来自：

- 批量处理；
- 并发 I/O；
- 减少重复查询；
- 结果预过滤；
- 减少 Agent Tool Call；
- 常驻进程。

---

## 7. 依赖与安装

MCP Server 使用 Python 标准库加 **httpx** + **pyproj**（CRS 重投影，见 `requirements-mcp.txt`），无 GDAL/GeoPandas。不需要官方 `mcp` SDK。

推荐创建一个**持久虚拟环境**：

```bash
python -m venv .venv
```

Windows：

```bash
.venv\\Scripts\\activate
```

macOS / Linux：

```bash
source .venv/bin/activate
```

然后：

```bash
pip install -r requirements-mcp.txt
```

装好依赖后做离线测试：

```bash
python -m unittest discover -s tests -v
```

---

## 8. API Key 配置

支持的数据源：

| 数据源 | 用途 | Key |
|---|---|---|
| 高德 | POI / 项目名称 / 地址等 | `AMAP_KEY` |
| 百度 | POI / 项目名称 / 地址等 | `BAIDU_AK` |
| OSM / Overpass | 开放地图与建设标签 | 通常无需 Key（可用 `OSM_ENABLED=false` 关闭） |

推荐通过 MCP Host 的 `env` 注入，不要写入 `SKILL.md`。

示例：

```json
{
  "mcpServers": {
    "geo-region-inference": {
      "command": "C:/path/to/geo-region-inference/.venv/Scripts/python.exe",
      "args": [
        "C:/path/to/geo-region-inference/mcp_server.py"
      ],
      "env": {
        "AMAP_KEY": "YOUR_AMAP_KEY",
        "BAIDU_AK": "YOUR_BAIDU_AK",
        "OVERPASS_URL": "https://overpass-api.de/api/interpreter",
        "OSM_ENABLED": "true",
        "HTTP_TIMEOUT_SECONDS": "12"
      }
    }
  }
}
```

具体配置见：

`MCP_SETUP.md`

---

## 9. 证据与置信度原则

### 项目证据优先级

```text
明确项目名称
    ↓
项目/规划/备案编号
    ↓
名称 + 在建/建设中/工地
    ↓
construction=* 等直接建设标签
    ↓
地址 / 开发主体 / businessAreas 等辅助线索
    ↓
region_type + possible_buildings
    ↓
单纯几何形状
```

### 关键规则

如果没有直接项目名称、编号、公示或明确的项目建设状态证据：

```text
related_projects.confidence <= 0.4
```

即使：

```text
region_type.confidence = 0.9
```

也不能因此把：

```text
某具体住宅项目 = 0.9
```

因为：

> “判断得出这是住宅区”不等于“知道它具体是哪一个住宅项目”。

---

## 10. SHP 的处理边界

SHP 本身包含：

```text
.shp  几何
.shx  索引
.dbf  属性
.prj  投影
```

本项目选择：

```text
SHP
 ↓
输入层转换
 ↓
GeoJSON / JSON
 ↓
MCP
```

而不是：

```text
SHP
 ↓
Skill 自己安装 GDAL / Fiona / GeoPandas
 ↓
Skill 内部解析
```

这样可以避免：

- 重型 GIS 依赖；
- Serverless/轻量 Skill 环境安装失败；
- 每次任务重新加载 GIS 环境；
- Agent 层与 GIS 文件解析强耦合。

---

## 11. scripts/（已废弃）

本目录 **不是** 正常 Skill 执行路径。详见 [scripts/README.md](scripts/README.md)。

`geo_stats.py` 质心算法与 MCP 不同（顶点平均 vs 面积加权），应急使用时勿与 MCP 结果对比。

---

## 12. 推荐部署

适合个人开发 / Cursor / Claude Code / 其他支持 MCP 的 Agent Host：

```text
项目目录
   │
   ├── .venv
   │
   ├── SKILL.md
   ├── mcp_server.py
   └── references/
```

MCP Host 指向固定的 `.venv` Python：

```text
Host
  ↓
固定 Python
  ↓
mcp_server.py
  ↓
长期运行
```

不要采用：

```text
每次任务
 ↓
pip install
 ↓
启动
 ↓
运行
 ↓
删除环境
```

---

## 13. 最简使用原则

```text
1. 准备 GeoJSON / JSON
2. 加载 geo-region-inference Skill
3. 确保 MCP Server 已配置
4. Agent 优先调用 analyze_regions
5. LLM 根据证据生成结果
6. 调用 validate_result
```
