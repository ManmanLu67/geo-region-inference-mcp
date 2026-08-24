# 地图 API 数据源配置说明

本 skill 支持三个数据源，按优先级自动选择，不需要每次手动指定：

| 优先级 | 数据源 | 是否需要 Key | 国内覆盖 | 说明 |
|---|---|---|---|---|
| 1 | 高德(AMap) | 需要 `AMAP_KEY` | 好 | POI 密度高，逆地理编码准确 |
| 2 | 百度(Baidu) | 需要 `BAIDU_AK` | 好 | 作为高德的备用/交叉验证 |
| 3 | OSM(Overpass) | 不需要 | 国内城市区域一般、偏远地区较差 | 免费兜底，唯一能拿到真正 landuse 面状数据的源 |
| — | 离线推理 | 不需要 | — | 以上均不可用或结果为空时的最终兜底 |

## 怎么申请 Key

### 高德地图（AMAP_KEY）

1. 打开 <https://console.amap.com/> 注册开发者账号（个人认证即可，免费）
2. 进入"应用管理" → "创建新应用" → 添加 Key
3. Key 类型选 **"Web服务"**（不是"Web端(JS API)"，也不是"Android/iOS"——选错类型这个脚本调不通）
4. 拿到 Key 后设置环境变量：
   ```bash
   export AMAP_KEY=你的key
   ```
5. 免费额度：个人开发者一般是每日 5,000～30,000 次（不同接口额度不同，以控制台实际显示为准），本 skill 每处理一个地物大约消耗 2 次调用（逆地理编码 + 周边搜索）

### 百度地图（BAIDU_AK）

1. 打开 <https://lbsyun.baidu.com/> 注册开发者账号
2. 进入"控制台" → "创建应用"
3. 应用类型选 **"服务端"**（不是"浏览器端"/"移动端"，选错会报权限错误）
4. 设置环境变量：
   ```bash
   export BAIDU_AK=你的AK
   ```
5. 免费额度同样有每日调用上限，具体以控制台显示为准

### 都不配置

不设置任何环境变量也完全能用，skill 会自动跳到 OSM(Overpass)，OSM 完全免费、无需注册，只是国内部分区域数据稀疏。

## 数据源选择逻辑（skill 内部自动执行，不需要手动干预）

对每个地物：

1. 检测 `AMAP_KEY` 是否存在 → 存在则调用 `query_amap.py`
   - 返回退出码 0 且结果非空 → 采用，`data_source` 标 `amap`，跳过后面的源
   - 返回退出码 3（未配置）→ 直接跳到第 2 步，不算失败
   - 返回退出码 2（网络失败）或结果为空 → 跳到第 2 步
2. 检测 `BAIDU_AK` 是否存在 → 存在则调用 `query_baidu.py`，逻辑同上，成功则 `data_source` 标 `baidu`
3. 调用 `query_overpass.py`（无需 key，总是尝试）→ 成功则 `data_source` 标 `osm`
4. 以上全部失败或结果为空 → 转纯离线推理，`data_source` 标 `offline`

如果高德/百度都配置了且都成功返回，但两者结果指向明显不同的判断（比如高德显示是商业区、百度周边POI显示是住宅区），把两个来源的证据都写进 `evidence` 里交叉说明，不要只选一个隐藏另一个的分歧。

## 坐标系提醒

- 高德用 GCJ-02，百度用 BD-09，OSM/Overpass 用标准 WGS84
- `query_amap.py` 和 `query_baidu.py` 内部已经自动调用 `coord_transform.py` 做转换，
  传入参数时**直接用 ArcGIS 导出的 WGS84 经纬度即可**，不需要手动转换
- 如果要单独测试转换是否正确，可以用：
  ```bash
  python scripts/coord_transform.py wgs84_to_gcj02 <lon> <lat>
  python scripts/coord_transform.py wgs84_to_bd09 <lon> <lat>
  ```
