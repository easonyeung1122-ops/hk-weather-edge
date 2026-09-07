# hk-weather-edge

CodeBuddy / Claude 技能包：Polymarket「香港最高气温」日度市场的 **edge 计算与实况外推**。

把香港天文台（HKO）开放数据 + Open-Meteo 多模式 NWP 集合，转成校准后的**摄氏整数档（bucket）公允概率**，
与市场价对比后输出 EV 与 1/4 Kelly 仓位建议；`--watch` 模式基于**结算站实测 + 日内气候曲线**外推当日峰值。

> 方法论研究，**不构成投资建议**。香港《赌博条例》(Cap. 148) 下预测市场属灰色地带，请自行评估合规风险。
> 不要用 VPN 绕过地区限制（违反 Polymarket ToS，可能冻结资金）。

## 安装

```bash
git clone https://github.com/easonyeung1122-ops/hk-weather-edge.git ~/.codebuddy/skills/hk-weather-edge
```

放到 `~/.codebuddy/skills/`（用户级，全工作区可用）或 `.codebuddy/skills/`（项目级）。

## 快速开始

脚本仅依赖标准库（`requests` 可选）。Windows 用 `py -3`：

```bash
py -3 scripts/hk_edge.py                                # 未来 7 天各档公允概率
py -3 scripts/hk_edge.py --date 2026-09-09 --bankroll 500 \
  --market "29:0.12,30:0.30,31:0.42,32:0.11,33:0.03"    # 算 edge + 1/4 Kelly
py -3 scripts/hk_edge.py --watch                        # 当日实况追踪（建议每 10 分钟）
```

日内（已能看到当天实测温度后）必须用三件套，否则分布不会以「已观测到的最高温」为条件：

```bash
py -3 scripts/hk_edge.py --date 2026-09-07 --observed-max 30.0 --mu 30.15 --sigma 0.5 \
  --market "30:0.71,31:0.235"
```

`--observed-max` 截断并重命名到已实测最高温（省略时自动读 `--watch` 的日志）；
`--sigma` 给出剩余总不确定性（日内远小于提前一天）；`--mu` 覆盖点估计——
午后一旦发生雷暴，模式的日最高值不会实时下修，必须手动改。

首次运行会下载天文台历史数据（约 1.6MB）并自动做一次模型偏差校准。

## 结算口径（下单前必读）

| 项目 | 内容 |
|---|---|
| 结算源 | HKO Daily Extract → `Absolute Daily Max (deg. C)` |
| 站点 | **天文台总部站（尖沙咀）**，非机场 / 非打鼓岭 / 非手机 App |
| 精度 | **0.1 °C** |
| 档位 | `N°C` = `[N.0, N+1.0)`，即 `floor(实测值)` |

⚠️ Polymarket 规则会变（换过巴黎的结算站点、把深圳切到 NOAA），每次下单前重读 Rules。
⚠️ 与美国城市盘不同：香港是**摄氏 0.1 位**，勿照搬华氏整数那套经验。

## 取价：用订单簿，不要用最后成交价

Gamma 的 `outcomePrices`（最后成交价）和 `bestBid/bestAsk` 都是**延迟指标**，只有 CLOB 订单簿实时。
实测同一时刻（临近结算时）30 档：最后成交 0.82、Gamma 0.79/0.85、**订单簿 0.962/0.988** —— 差 15 分以上，
用最后成交价算出的 EV 完全是假的。

```bash
py -3 scripts/market_prices.py 2026-09-07            # 按香港日期取盘口
py -3 scripts/market_prices.py 2026-09-08 --depth 3  # 多看几档深度
```

算 EV 用可执行价：买 YES 用 YES 的 **ask**，买 NO 用 **NO token 自己的 ask**（NO 有独立订单簿）。
算完 Kelly 后**必须核对可成交量**——临近结算常出现「EV +150% 但只能买 165 份」的情况。

## 三条 edge 来源

1. **结算只认一个传感器**——市场看的是"香港天气"这个模糊概念
2. **市场把点预报当概率**——但结算是 0.1 °C 精度的一维分箱，两者系统性错配
   （实测偏移明显左偏：`-1` 档 43.7%，命中市场定价档仅 31.0%）
3. **时区窗口**——香港白天是美东深夜，挂单薄、错误定价停留更久

## 目录结构

```
SKILL.md                              技能主指令
references/methodology.md             实证数据 / dressed ensemble 公式 / 数据源 / 排障
assets/docs_edge_map.html             完整方法论报告（浏览器可直接打开）
scripts/hk_edge.py                    主程序
scripts/market_prices.py              从 CLOB 订单簿取真实可成交价（取价必用）
scripts/diurnal.py                    生成日内气候升温表（ERA5）
scripts/fetch_stations.py             下载多站历史 CSV（供 analyze.py）
scripts/backtest.py / analyze.py / hitrate.py   校准与分析（需 pandas + numpy）
scripts/data/diurnal_climatology.json 日内气候缓存，开箱即用
```

## 数据源（全部免费、无需 API key）

Weather data © Hong Kong Observatory；Model data via Open-Meteo
（ECMWF / NOAA / DWD / Météo-France / JMA / UKMO / CMC）。

MIT License —— 详见 `LICENSE`。
