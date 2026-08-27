# 区域类型分类参考表

用于约束 `region_type` 的候选标签词汇，避免生造类别名称。推理时优先从下表选择
label；如果证据明确指向表外的细分类型（如"数据中心""变电站"），可以在下表大类
下使用更具体的子标签，但不要脱离大类体系。

每一行给出：大类标签 | 典型面积范围 | 形状/紧凑度信号 | 对应 OSM landuse/building 标签 |
常见建筑类型 | 常见建设项目命名模式。

| 大类标签 | 典型面积范围 | 形状信号（来自 MCP 几何统计 / `calculate_geometry`） | OSM 标签线索 | 常见建筑 | 常见项目命名模式 |
|---|---|---|---|---|---|
| 住宅小区 | 5,000–100,000 ㎡ | compactness 0.5–0.9，较规整 | landuse=residential, building=apartments/house | 住宅楼、配套幼儿园、地下车库 | "XX花园""XX苑""XX小区""XX府" |
| 商业综合体/写字楼 | 3,000–50,000 ㎡ | compactness 0.4–0.8 | landuse=commercial/retail, building=commercial/office | 商场、写字楼、酒店 | "XX广场""XX中心""XX大厦" |
| 工业园区/厂房 | 10,000–500,000 ㎡ | compactness 常较低（矩形厂房群），aspect_ratio 偏大 | landuse=industrial, building=industrial/warehouse | 厂房、仓库、办公楼 | "XX产业园""XX工业园""XX科技园" |
| 物流仓储 | 5,000–200,000 ㎡ | 大块矩形，aspect_ratio 明显偏大 | landuse=industrial + building=warehouse, shop=logistics | 仓库、装卸平台 | "XX物流园""XX仓储中心" |
| 教育用地 | 8,000–150,000 ㎡ | compactness 中等，常有操场（大块空地） | amenity=school/university, landuse=education | 教学楼、宿舍、操场 | "XX学校""XX大学XX校区" |
| 医疗用地 | 3,000–80,000 ㎡ | compactness 中高 | amenity=hospital/clinic, landuse=healthcare | 门诊楼、住院部、停车楼 | "XX医院""XX卫生院" |
| 公园绿地 | 不定，常较大 | compactness 差异大，常不规则 | landuse=recreation_ground/forest, leisure=park | 亭台、步道、广场 | "XX公园""XX绿地""XX广场" |
| 交通枢纽/场站 | 5,000–300,000 ㎡ | 形状常受线性走廊约束，aspect_ratio 大 | landuse=railway, amenity=bus_station, aeroway=* | 站房、停车场、天桥 | "XX站""XX枢纽""XX停车场" |
| 道路/管线走廊 | 面积计算不适用（LineString/狭长Polygon） | aspect_ratio 很大（>5），compactness 很低 | highway=*, landuse=* (狭长) | 无独立建筑，可能有沿线设施 | "XX路""XX大道""XX管线工程" |
| 水域/水利设施 | 不定 | 常不规则，边界自然弯曲 | natural=water, landuse=reservoir | 泵站、堤坝 | "XX水库""XX泵站""XX河道整治工程" |
| 农业用地 | 通常较大，形状规则（条田） | compactness 中高，常有规律网格 | landuse=farmland/orchard | 农舍、大棚 | "XX农业园""XX基地" |
| 公共服务设施 | 1,000–50,000 ㎡ | 视具体类型而定 | amenity=townhall/community_centre, government=* | 政务中心、社区服务站 | "XX政务服务中心""XX街道办" |
| 未利用地/待建用地 | 不定 | 边界规则但周边无对应 OSM 建筑/POI | landuse=brownfield/greenfield，或无标签 | 无 | "XX地块出让公告""XX项目（在建）" |

## 判断优先级

1. 如果在线数据源返回了 `landuse` 标签（OSM source，来自 `analyze_regions` 或 `query_overpass.py`；高德/百度只有 POI 点数据），优先按该标签映射到上表大类。
2. 没有 landuse 标签但有高德/百度的 POI 数据时，用 POI 类型分布（住宅类 POI 密集 → 住宅小区；商业类 POI 密集 → 商业区）做次优先证据。
3. 若完全没有在线数据，用 `properties` 中任何看起来像用地代码/规划编号的字段（即使不认识具体编码规则，也可以结合数字/字母前缀做弱证据），再结合 MCP 返回的 `geometry` 字段（面积、紧凑度、长宽比；或离线时用 `calculate_geometry`）做形状匹配。
4. 面积和形状信号只是弱证据，不要单独作为高置信度（>0.6）的依据；只有在 landuse 标签直接命中、POI 类型高度一致、或属性字段有明确文字描述时，才给高置信度。
5. 多个大类得分接近时，全部列出，不要强行只给一个"正确答案"。
