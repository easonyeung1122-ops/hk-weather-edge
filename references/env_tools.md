# 环境、接口与工具速查

> 自 SKILL.md §7-C~7-F 外置（2026-09-30 结构整理）。**内容一字未改**。
> 用途：手工取数时的接口字段、Windows 静默运行、性能缓存、脚本清单。

### 7-C. 接口字段速查（手工取数时踩过的坑）

调试或手工复核要直接打接口时，先记住这三条，否则第一次调用一定拿不到数：

1. **Open-Meteo 多模式**：请求 `models=a,b,c` 时，`daily` 里的键会**带上模式后缀** ——
   `temperature_2m_max_ecmwf_ifs025`、`precipitation_sum_gfs_seamless` …，
   **不是** `temperature_2m_max`。顶层也不再有各模式的子字典（那是单模式才有的形状）。
   写成 `d['daily']['temperature_2m_max']` 会 `KeyError`。
2. **HKO 九天预报 `fnd`**：字段名是 **`forecastMaxtemp`**（小写 t）+ 嵌套 `{value, unit}`，
   **不是** `forecastMaxTemp` / `forecastMaxTemperature`；温度没有扁平字段。
   取 `w['forecastMaxtemp']['value']`。`forecastDate` 是 `YYYYMMDD` 字符串，
   **当日的条目可能已从列表移除**，只有未来 9 天。
3. **HKO CLMMAXT**：`rformat=csv` 时首行是 `\ufeff` BOM + 中文标题，含 3 行表头，
   末尾两行是 `*** 沒有數據` / `# 數據不完整` 图例 —— 解析要按列位取
   `year,month,day,value,completeness`，别把图例行当数据。**该源滞后约 10 天**。

**常用复现命令**
```bash
# 七模式 × 逐日（一次请求）
api.open-meteo.com/v1/forecast?latitude=22.302&longitude=114.174&daily=temperature_2m_max&past_days=2&forecast_days=4&timezone=Asia%2FShanghai&models=ecmwf_ifs025,gfs_seamless,icon_seamless,gem_seamless,metno_seamless,jma_seamless,ukmo_seamless

# 逐时曲线（判断网格峰值时刻）
api.open-meteo.com/v1/forecast?latitude=22.302&longitude=114.174&hourly=temperature_2m&past_days=2&forecast_days=1&timezone=Asia%2FShanghai&models=best_match

# HKO 月 XML（气候先验 + 核对结算档）
https://www.weather.gov.hk/cis/dailyExtract/dailyExtract_YYYYMM.xml

# 其他四站日最高（analyze.py 需要）
https://data.weather.gov.hk/weatherAPI/opendata/opendata.php?dataType=CLMMAXT&station=KP&lang=en
```

### 7-D. Windows 静默运行与进程管理

脚本位于 `scripts/`，全部**仅依赖标准库**（`requests` 可选，有则网络更稳）。
Windows 下用 `py -3`（`python3` 通常不存在）。以下示例均以 skill 根为当前目录。

```bash
py -3 scripts/hk_edge.py            # 首次运行会下载 HKO 历史数据(约1.6MB)并自动校准
```
首次运行后 `scripts/data/` 会生成 `maxt_HKO.csv`（结算源历史）与 `model_calib.json`。

**为什么会弹出一个命令框，怎么关掉** `[v0.13.4]`

**现象**：屏幕上冒出黑色命令框。如果是**每 30 分钟自己弹一次**，那不是脚本，是计划任务。

**原因**（是 Windows 的机制，不是脚本 bug）：`python.exe`、`cmd.exe` 都是**控制台子系统**程序，
Windows 规定它们必须挂在某个控制台上。当启动它的宿主自己没有控制台时（桌面版应用、资源管理器
双击、计划任务、部分 GUI 启动器），系统会**新建一个控制台窗口**给它——那个黑框就是它。

**根治办法：换成 `pythonw.exe`。** 它是 **GUI 子系统**程序，Windows 根本不为其创建控制台。

| 场景 | 表现 | 处理 |
|---|---|---|
| **计划任务定时触发** | 任务动作写成 `watch.bat` → 每 30 分钟必弹一次 | 动作改为直接启动 `pythonw.exe scripts\watch_silent.py`。**不要指向 vbs**：`Run(..., 0, False)` 立刻返回，任务秒判完成，`IgnoreNew` 失效 → 每 30 分钟叠一个循环 |
| **双击 `watch.bat`** | `.bat` 只能由 cmd.exe 解析 → 必开窗口 | **改双击 `watch_silent.vbs`**（启动 pythonw），停止用 `stop_watch.vbs` |
| 由宿主/GUI 启动 `python.exe` | 宿主无控制台 → 系统为子进程新建一个 | `hk_edge.py` 启动时自查：该控制台**只挂自己一个进程**且 stdout 不在终端上 → `ShowWindow(hwnd, 0)` 自我隐藏。这是第二道防线，不是主路径 |

自查当前环境属于哪种（0 = 无控制台，不会弹）：
```bash
py -3 -c "import ctypes;print(ctypes.windll.kernel32.GetConsoleWindow())"
```

#### 两个必踩的坑（都已实测复现）

**1. `.bat` 必须写成纯 ASCII + CRLF。** cmd.exe 按系统 ANSI 代码页（中文 Windows = GBK）解析 .bat，
所以 UTF-8 中文注释会解成乱码；更糟的是 **LF-only 换行会让 `rem` 词元被粘到上一行行尾**，
cmd 于是把注释当命令执行，刷出满屏「不是内部或外部命令」。
同一段注释的对照实验：UTF-8+LF 版 = 4 行乱码报错；ASCII+CRLF 版 = `PARSE_OK`、零报错。
**改完用 `od -c` 或字节统计确认 `bareLF == 0`**，编辑器「另存为」默认不保证。

**2. 不要在一个 cmd.exe 正在执行的 .bat 上做就地改写。** cmd 是**分块增量读取**批处理文件的，
改写会让它重新读到新内容并**重复执行命令**——2026-09-10 排查时实测：重写 `watch.bat` 后，
同一个 cmd 又起了一个 `python.exe --loop`。计划任务的动作条目不会有这个问题。

**日志文件的共享陷阱**：不要用 shell 重定向（`>> watch.log`）写日志：
**cmd.exe 的重定向句柄拒绝写共享**，只要那个循环还活着，其它进程 append 同一个文件就一律
`PermissionError [Errno 13]`。Python 的 `open()` 允许共享，所以 `watch_silent.py` 自己开日志文件。

计划任务现状（`Get-ScheduledTask HKWeatherEdge-Watch` 可查）：每天 11:00 起、每 30 分钟一次、
持续 6 小时，动作 = `pythonw.exe "…\scripts\watch_silent.py"`，`MultipleInstances=IgnoreNew`
（因任务全程保持 `Running`，此设置才有效 → 退化为看门狗）。

### 7-E. 性能与缓存 `[v0.11.0 实测重写]`

**先记住这条诊断**：单次运行的耗时**几乎全在 TCP/TLS 握手，跟数据量无关**。
实测同一主机：首次请求 **1.02s**，连接热了之后 **0.20s**；
7 模型 × 10 天（1251 B）与 1 模型 × 2 天（318 B）在热连接上都是 0.20s。
→ **砍模型数、砍 `forecast_days`、砍 payload 几乎不省时间**；
省时间只有两条路：**少握手、别重新握手**。

本机实测：

| 命令 | 耗时 |
|---|---|
| `hk_edge.py --date …`（缓存命中） | **0.39s** |
| `hk_edge.py --date … --no-cache` | 1.65s |
| `hk_edge.py --watch` | 1.56s |
| `market_prices.py … --depth 6` | 1.38s |

加速只动了传输层，**任何一个数字的计算方式都没变**（已逐位比对验证）：

| 手段 | 作用 |
|---|---|
| `requests.Session` 保活 | 同主机复用 TCP/TLS |
| 互不依赖的接口并行 | 墙钟时间 = 最慢那个请求 |
| CLOB 批量接口 `/books` | 22 个订单簿 1 次请求拿全（0.5s vs 逐个并行 1.4s） |
| 模式/预报短 TTL 磁盘缓存 | 15 分钟内重复运行直接命中（模式更新周期 ≥6h） |
| 历史 CSV 按需加载 | 校准有缓存时不解析 4.9 万行 |
| `--watch --loop` | 一个进程内反复采集，复用同一条连接：首次之后平均 0.6~0.8s，比每 10 分钟起新进程快 **2.5~3 倍** |
| `--sigma` 时跳过集合 | 概率直接用 `N(mu, sigma)`，省掉最慢那个请求 |

**严谨性边界（不可越过）**：
- **实况（`rhrread`、网格实况）与订单簿价格永不缓存**——那是结论的输入源头。
- 缓存只作用于模式/官方预报类接口，键 = 完整 URL，参数变化自动失效。
- 命中缓存时输出会明确打印**数据年龄**（`数据时效: 缓存命中，最旧数据取自 X 分钟前`），不隐藏。
- 下单前想拿最新数据：`--no-cache`；想改缓存时长：`--ttl <秒>`（默认 900）。
- 批量订单簿失败会打印 `[warn]` 并**自动回退**到逐个并行拉取。

### 7-F. 数据 / 脚本清单

| 文件 | 作用 |
|---|---|
| `scripts/hk_edge.py` | 主程序：拉数据 → 校准 → 公允概率 / edge / Kelly / `--watch` |
| `scripts/market_prices.py` | **从 CLOB 订单簿取真实可成交价**（取价必用，见 §3.1） |
| `scripts/diurnal.py` | 生成「某时刻 → 当日峰值」气候升温表（ERA5，跑一次数分钟） |
| `scripts/backtest.py` | 多模型回测，产出 `model_calib.json`（**需要 pandas + numpy**） |
| `scripts/analyze.py` | 站点偏差 / 月度气候 / 日际波动（需 pandas + numpy，且需多站 CSV） |
| `scripts/hitrate.py` | 命中率与 `floor` 取整错配分析（需 pandas + numpy） |
| `scripts/build_remaining_rise.py` | **重建剩余升温经验分位表**（one-touch 口径），`--validate` 附新旧 Brier 对照 |
| `scripts/review_brier.py` | **收盘复盘**：`decision_log.csv` 逐时点算「模型 vs 市场」Brier，按上午/午后分层。结算档默认读 `maxt_HKO.csv`，近期日需 `--truth DATE=N`；`--flicker` 改算 σ_flicker 与越线概率表 |
| `scripts/station_r_probe.py` | **站点口径 R 探针**（v0.16.23）：用 Iowa Mesonet 的 VHHH 机场 ASOS 逐时存档直测站点口径 R 分布，并与 ERA5@同坐标对算放大比 k。含 k 随时段表、gap × rm 分档表、今日 base 双重条件化。`--refresh` 重下 ASOS |
| `scripts/hourly_window.py` | **最佳下注时段分析**（v0.21.0）：按小时对齐四条独立证据 —— 轴1 剩余行程 `R(h)`、轴2 最终档已锁定率、轴3 市场对最终档的逐时定价（`decision_log.csv`）、轴4 台账 ROI 分层。`--hour 11` 看逐日明细 |
| `scripts/r14_cond_table.py` | **ERA5 条件剩余升温表**（v0.20.0）：按 `rmax@h` 的分位切分，输出 `P(最终日最高 − rmax@h ≥ g)`。0.1 °C 浮点、无需 API key，是回答「距边界还差 g 度」的可复现口径（**读作下界**）。`--hour 14 --g 0.4 0.6 1.0` |
| `scripts/reach_next.py` | **「能否升到下一档」双口径估计**（v0.16.24）：并列输出【HKO 原生结果条件】与【VHHH 时间条件】+ 口径因子 r，最后合成站点口径区间。`--level 31.4 --next 32.0 --hour 14` |
| `scripts/l_integrate.py` | **L 积分的固化实现**（v0.16.16）：L 网格积分 / 反解市场隐含 L / 组合回收矩阵 / 可成交价 EV 排序 |
| `scripts/l_cond_check.py` | 把 `running_max(h)` 当条件变量查历史最终日最高，并诊断 ERA5 振幅压缩（v0.16.17） |
| `scripts/postrain_prior.py` | 气候先验的**条件化**（当日雨量 / 前日雨量），v0.16.11 |
| `scripts/rec_pnl.py` | **建议盈亏台账**：`--settle DATE=N [--status provisional] --write` 回填结算档，`--markdown` 重生 `PNL.md`，`--all-legs` 看未折叠流水 |
| `scripts/watch_silent.py` | **零窗口轮询入口**：由 `pythonw.exe` 启动，自己开 `watch.log`、带 PID 锁 |
| `scripts/data/rec_pnl_log.csv` | 每日建议腿的台账（**纳入 git**） |
| `scripts/data/remaining_rise_cdf.json` | 剩余升温 R 的经验分位表（按月×时，ERA5 4266 天） |
| `scripts/data/obs_1min_archive.json` | 0.1°C 结算站序列**按日归档**，攒够后可重建站点口径分位表 |
| `scripts/data/forecast_log.jsonl` | 每次 `--watch` 落地的概率向量，`--score` 用它打 Brier |
| `scripts/data/diurnal_climatology.json` | 日内气候缓存（v0.13.0 起 watch 只用它兜底） |
| `scripts/data/http_cache.json` | 模式/预报类接口的短 TTL 缓存（实况与价格不进这里） |
| `scripts/data/model_calib.json` | `bias` / `resid_sd` —— **必须与 `MODELS` 同源**（见 §2.2-硬规则4） |
| `scripts/data/cold_pool_state.json` | 当日冷池最大回撤记忆（只增不减） |
| `PNL.md` | 台账的 GitHub 可见记分牌（由 `rec_pnl.py --markdown` 生成，**勿手改**） |
| `PNL_actual.md` | **实际成交口径**台账（手工维护，**不会被 `--markdown` 覆盖**） |
| `watch_silent.vbs` / `stop_watch.vbs` | 手工双击启动 / 停止静默轮询 |
| `assets/docs_edge_map.html` | 完整方法论报告（含实证图表），需要时可直接打开给用户看 |
| `references/methodology.md` | 实证数据：站点偏差表、模型回测表、偏移分布、dressed ensemble 公式推导 |

