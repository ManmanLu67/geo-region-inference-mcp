# geo-region-inference

基于 **Skill + MCP** 的地理区域语义推断工具：从 SHP/GeoJSON 地块数据中**优先识别相关建设项目（`related_projects`）**，`region_type` 与 `possible_buildings` 为辅助中间证据。

---

## 1. 核心目标

```text
几何 + 属性 + POI / OSM / 项目线索
                ↓
region_type / possible_buildings   （中间证据）
                ↓
related_projects                   （最终目标）
```

**`related_projects` > `region_type` / `possible_buildings`** — 有直接 POI、编号或公示线索时优先使用，勿先猜区域类型再「联想」项目名。

---

## 2. Skill + MCP 分工

```text
Agent / LLM
    ├── Skill（流程、证据优先级、置信度、输出结构）→ SKILL.md
    └── MCP Server（几何、CRS、并发 API、校验）→ mcp_server.py
```

正常路径一次 `analyze_regions` 处理整批地物，可选 `prepare_gov_web_search` + Agent Web 检索，再语义推理与 `validate_result`。详见 [SKILL.md](SKILL.md)。

并发设计与性能说明见 [MCP_SETUP.md](MCP_SETUP.md#performance-model)。

---

## 3. 项目结构

```text
geo-region-inference/
├── SKILL.md                         # Agent 工作流（执行推断读此）
├── README.md                        # 本文件：概览与文档地图
├── MCP_SETUP.md                     # 安装、Host 配置、stdio 启动
├── mcp_server.py                    # MCP Server
├── geo_input.py                     # GeoJSON 加载、CRS、Esri 误传检测
├── geo_geometry.py                  # 权威几何统计
├── geo_clients.py                   # httpx / 高德 / 百度 / OSM
├── gov_search.py                    # 政府 Web 四轮 query 计划（无 HTTP）
├── validation.py                    # 输出 Schema 校验
├── requirements-mcp.txt             # httpx + pyproj；含 PyPI 镜像选项
├── pip.ini.example                  # 可选全局 pip 镜像示例
├── tests/
│   ├── test_offline.py
│   └── fixtures/                    # 合成测试数据
├── references/                      # 参数 / env / schema 单一事实源
└── scripts/                         # deprecated — scripts/README.md
```

---

## 4. 文档地图（从哪里读什么）

| 主题 | 文件 |
|------|------|
| **怎么推断（主文档）** | [SKILL.md](SKILL.md) |
| **安装 MCP / Host JSON / venv** | [MCP_SETUP.md](MCP_SETUP.md) |
| MCP 工具参数、返回字段、env、`status` | [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md) |
| LLM 最终 JSON、`evidence_type`、`data_source` | [references/output_schema.md](references/output_schema.md) |
| 政府 Web 分轮 SOP | [references/gov_web_search_guide.md](references/gov_web_search_guide.md) |
| 政府 Web query 模板 | [references/gov_search_templates.json](references/gov_search_templates.json) |
| API Key 申请 | [references/map_api_setup.md](references/map_api_setup.md) |
| OSM / Overpass 半径与字段解读 | [references/overpass_query_guide.md](references/overpass_query_guide.md) |
| 属性 / POI 字段线索启发 | [references/project_inference_signals.md](references/project_inference_signals.md) |
| 区域类型词汇 | [references/landuse_taxonomy.md](references/landuse_taxonomy.md) |
| 错误码与告警 | [references/error_codes.md](references/error_codes.md) |
| scripts 废弃说明 | [scripts/README.md](scripts/README.md) |

---

## 5. 快速开始

1. 安装：见 [MCP_SETUP.md](MCP_SETUP.md)（`pip install -r requirements-mcp.txt`，持久 `.venv`）。
2. 配置 Host：指向 `.venv` 的 Python + `mcp_server.py`；`env` 注入 `AMAP_KEY` / `BAIDU_AK` 等（见 [map_api_setup.md](references/map_api_setup.md)）。
3. Agent 按 [SKILL.md](SKILL.md) 执行：`analyze_regions` →（可选 gov Web）→ 推理 → `validate_result`。

**输入**：GeoJSON / `input_path`；SHP 须宿主先转 GeoJSON；Esri REST JSON v2.5+ 可尝试自动转换（见 SKILL 第一步）。

**MCP 工具一览**（细节见 [references/mcp_evidence_schema.md](references/mcp_evidence_schema.md)）：

| 工具 | 用途 |
|------|------|
| `analyze_regions` | 主路径：几何 + 并发在线证据 |
| `prepare_gov_web_search` | 四轮政府 Web 搜索计划（无 HTTP） |
| `validate_result` | 最终 JSON 结构与置信度校验 |
| `check_api_status` | 验活 Key / burst 诊断 CUQPS |
| `calculate_geometry` | 调试：仅离线几何 |
| `search_project_evidence` | 调试：单点项目检索 |

---

## 6. scripts/（已废弃）

正常 Skill **不要**执行 `python scripts/*.py`。仅 MCP 完全不可用且只需粗略几何时应急，见 [scripts/README.md](scripts/README.md)。
