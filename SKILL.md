---
name: hk-weather-edge
description: Polymarket「香港最高气温」日度市场的 edge 计算与实况外推工具。把香港天文台(HKO)开放数据 + Open-Meteo 多模式 NWP 集合转为校准后的摄氏整数档(bucket)公允概率，并与市场价对比输出 EV 与 1/4 Kelly 仓位建议；`--watch` 模式基于结算站实测 + 日内气候曲线做当日峰值 nowcast。当用户提到「香港气温市场」「最高气温预测」「HKO / 天文台结算」「bucket 概率」「temperature bucket edge」「今天香港会到几度」「 polymarket weather Hong Kong」，或需要判断某个整数档是否值得买 YES / NO 时使用。
---

# 香港气温市场 Edge 工具

## 目的

把公开气象数据转换成**可与市场价对比的整数档概率**，用于 Polymarket「Highest temperature in Hong Kong」日度市场。
产出三样东西：每个整数档的公允概率、相对市场价的 EV 与动作（买 YES / 买 NO / 观望）、可选的 1/4 Kelly 下注额。

## 核心认知（决定这个工具有没有用）

Edge **不是**来自比天文台更会预报天气。它来自三个结构性错配：

1. **结算只认一个传感器**——天文台总部站（尖沙咀）。机场、打鼓岭、手机 App 的数都不作数。
2. **市场把点预报当概率**——但结算是 0.1 °C 精度的一维分箱 `floor()`，两者系统性错配（实测偏左：`-1` 档占 43.7%，命中市场定价档仅 31.0%）。
3. **时区窗口**——香港白天是美东深夜，挂单薄，错误定价停留更久；实况每小时更新而官方 CLMMAXT 定稿滞后。

**每次出手前必须记住**：即使完美预知当日期望最高温，命中那个整数档的概率也只有约 31–40%。永远不要用单档全仓去"猜"。

## 结算口径（下单前必读）

| 项目 | 内容 |
|---|---|
| 结算源 | HKO Daily Extract → `Absolute Daily Max (deg. C)`（`https://www.weather.gov.hk/en/cis/climat.htm`） |
| 站点 | **天文台总部站（尖沙咀）**，非机场 / 非打鼓岭 / 非 App |
| 精度 | **0.1 °C**，规则明文以该精度结算 |
| 档位定义 | 档位 `N°C` = 区间 `[N.0, N+1.0)`，即 `floor(实测值)`。31.5 → 31 档；29.6 → 29 档 |
| 日界 | 香港当地日 HKT 00:00–24:00 |
| 修订 | 初始发布后的修订不予考虑，以首次公布为准 |
| 开市 | 约提前 4 天（美东 00:56 ≈ 香港 13:56） |

⚠️ **规则会变。** Polymarket 换过巴黎的结算站点、把深圳切到 NOAA。每次下单前重读该市场的 Rules。
⚠️ **与美国城市盘不同**：纽约/芝加哥用 NWS CLI / METAR（华氏整数），香港是**摄氏 0.1 位**，勿照搬中文社区那套"华氏整数不换算"的经验。

## 环境准备

脚本位于 `scripts/`，全部**仅依赖标准库**（`requests` 可选，有则网络更稳）。

Windows 下用 `py -3`（`python3` 通常不存在）。以下示例均以 skill 根为当前目录。

```bash
py -3 scripts/hk_edge.py            # 首次运行会下载 HKO 历史数据(约1.6MB)并自动校准
```

首次运行后 `scripts/data/` 会生成 `maxt_HKO.csv`（结算源历史）与 `model_calib.json`（模型偏差/残差 sd）。

## 工作流

### A. 每日扫描（找 edge）

```bash
# 1) 未来 7 天各档公允概率
py -3 scripts/hk_edge.py

# 2) 指定日期 + 喂入市场价 + 本金，输出 EV 与 1/4 Kelly 下注额
py -3 scripts/hk_edge.py --date 2026-09-09 --bankroll 500 \
  --market "29:0.12,30:0.30,31:0.42,32:0.11,33:0.03"

# 3) 模型与官方预报分歧时，按自己的判断手动加权点估计
py -3 scripts/hk_edge.py --date 2026-09-07 --mu 31.82 --market "31:0.37,32:0.51"

# 4) 同时产出 HTML 报告到 scripts/edge_report.html
py -3 scripts/hk_edge.py --html
```

输出里每档给出：公允 P、市场价、动作、EV、可选的下注额、分布条。
底部固定提示一句"众数档仅 xx%——别把点预报当概率"，这是给持有仓位时的心理锚。

传入 `--market` 时会自动把 `日期/档位/公允P/市场价/edge` 追加写到 `scripts/data/decision_log.csv`——**结算后回来填实际档位做复盘**，这是长期唯一别人拿不走的 edge。

### B. 日内盯盘（`--watch`）

```bash
py -3 scripts/hk_edge.py --watch
```

建议香港时间 08:00–20:00 每 10 分钟跑一次（cron：`*/10 0-12 * * *`，时区 UTC）。
它不只是记温度，而是用「此刻实测 + 该时刻的气候升温幅度 + 站点加成」外推今日峰值，并给出到达各整数门槛的概率。

- 站点加成 = `日最高偏差 − 此刻偏差`，实时计算，越接近午后峰值越准。
- 气候表由 `diurnal.py` 生成（ERA5 2020–2025，按月 × 逐时），已缓存于 `scripts/data/diurnal_climatology.json`，开箱即用。
- **阴雨/雷暴日会显著低于外推值**——把外推值视为上限，午后下雨则以上即为上限。
- 16:00 后晴天情形下日最高温基本锁定。

### C. 校准与复盘

```bash
py -3 scripts/hk_edge.py --recalibrate   # 强制重算模型偏差(默认缓存<3天自动复用)
py -3 scripts/backtest.py                # 多模型预报 vs 实测 → bias/MAE/RMSE/r + model_calib.json
py -3 scripts/hitrate.py                 # 命中率与取整口径错配分析(需先跑 backtest.py)
py -3 scripts/analyze.py                 # 站点偏差、月度气候、日际波动(需额外站 CSV，见下)
```

`calib` 超过 3 天会自动重算；`n < 20` 时回落到保守默认 `bias=1.17, resid_sd=0.80` 并打印提示。

## 数据 / 脚本清单

| 文件 | 作用 |
|---|---|
| `scripts/hk_edge.py` | 主程序：拉数据 → 校准 → 公允概率 / edge / Kelly / `--watch` |
| `scripts/diurnal.py` | 生成「某时刻 → 当日峰值」气候升温表（ERA5，跑一次数分钟） |
| `scripts/backtest.py` | 多模型回测，产出 `model_calib.json`（**需要 pandas + numpy**） |
| `scripts/analyze.py` | 站点偏差 / 月度气候 / 日际波动（需要 pandas + numpy，且需要多站 CSV） |
| `scripts/hitrate.py` | 命中率与 `floor` 取整错配分析（需要 pandas + numpy） |
| `scripts/data/diurnal_climatology.json` | 日内气候缓存，已随包提供 |
| `assets/docs_edge_map.html` | 完整方法论报告（含实证图表），需要时可直接在浏览器打开给用户看 |

### `analyze.py` 的额外依赖（务必先看）

`analyze.py` 读 `scripts/data/maxt_{HKO,KP,TKL,SEK,CCH}.csv`，但只有 HKO 会由主程序自动下载。其余四站需手动拉取：

```bash
py -3 scripts/fetch_stations.py     # 下载 KP(京士柏)/TKL(打鼓岭)/SEK(石岗)/CCH(长洲)
```

或直接 curl `https://data.weather.gov.hk/weatherAPI/opendata/opendata.php?dataType=CLMMAXT&station=KP&lang=en`。

## 交易纪律（对用户输出建议时必须带上）

- **|EV| > 8% 才动手**——0.79 °C 的 RMSE 意味着概率本身有 ±5% 量级误差。脚本内部 `MIN_EDGE=0.08` 已硬编码。
- **挂限价单做 maker**，别吃单，taker 费用会吃掉薄盘的 edge。
- **跨 2–3 个相邻档 ladder**，收割分布形状而不是猜单点。
- **Kelly**：买 YES `f* = (p−m)/(1−m)`，买 NO `f* = (m−p)/m`；实操一律 1/4 Kelly。
- **别碰 $0.05 以下的档**——EV 看着几百个百分点，但没有挂单，是流动性幻觉。
- **记录每笔「你的概率 vs 市场价 vs 结果」**，攒够 30 笔回头校准自己的命中率。

## 已知局限（输出结论时如实说明）

- 回测窗口只有 92 天（Open-Meteo previous-runs 上限）且偏夏季，冬季表现需自行积累数据。
- 站点偏差是季节平均，单日可能偏离。
- 日内外推的「站点加成」基于单时点实测对比，有噪声。
- 台风 / 暴雨等非线性事件下官方预报会跳变，模型与外推同时失效。
- 事前概率随提前期自动变宽：提前 6 天很散，当天下午很尖。别用同一套仓位标准对待不同提前期。

**两条独立路径交叉验证**：自上而下（NWP 集合）与自下而上（`--watch` 实况外推）同时给峰值估计。两者一致时信心最高；分歧说明模型没吃进最新实况——此时应下调仓位而不是挑一边下注。

## 合规与免责

方法论研究，**不构成投资建议**。香港《赌博条例》(Cap. 148) 下预测市场处于灰色地带：未被明确许可，也未见针对此类平台的执法，SFC 未表态。向用户输出结论时应保留此风险提示，必要时建议其咨询香港律师。**不要建议用 VPN 绕过地区限制**（违反 Polymarket ToS，可能冻结资金）。

数据来源：Weather data © Hong Kong Observatory；Model data via Open-Meteo（ECMWF / NOAA / DWD / Météo-France / JMA / UKMO / CMC），全部免费、无需 API key。

## 参考资源

- 实证数据（站点偏差表、模型回测表、偏移分布、dressed ensemble 公式推导）见 `references/methodology.md`。
- 向用户讲解方法论或需要图表时，直接提供 `assets/docs_edge_map.html`。
