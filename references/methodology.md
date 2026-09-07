# 方法论与实证数据

## 一、为什么「点预报 ≠ 概率」

- 官方预报**四舍五入**到整数，市场把它当众数档定价。
- 结算是**向下取整** `floor()`，区间 `[N.0, N+1.0)`。

两套取整规则系统性错配，实测偏移分布明显左偏（n=87）：

| 实际档 − 官方预报档 | 频率 |
|---|---:|
| −2 及以下 | 10.3% |
| **−1** | **43.7%** |
| 0（市场定价档） | 31.0% |
| +1 | 14.9% |

推论：一个"看起来完全正确"的官方预报，命中结算档的概率只有 31.0%（理论值 39.6%）。
拿到任何点估计后，都必须过一遍 `bucket_probs()` 转成分布，绝不能直接用点值下单。

## 二、站点认知差

「天文台站 − 各站」日最高温差（HKO CLMMAXT，2016–2026 同日配对）：

| 对照站 | 全年 | 夏季(6–8月) | 冬季(12–2月) |
|---|---:|---:|---:|
| 石岗 Shek Kong | −1.15 °C | −0.97 °C | −1.44 °C |
| 打鼓岭 Ta Kwu Ling | −0.89 °C | −0.89 °C | −1.07 °C |
| 长洲 Cheung Chau | +0.10 °C | +0.61 °C | −0.53 °C |
| 京士柏 King's Park | +0.21 °C | +0.41 °C | −0.21 °C |

要点：

- 新闻写「打鼓岭 35 °C」时，结算站大概率只有 33–34 °C。看到媒体报道就线性外推，是最常见的亏钱方式。
- 冬季差值比夏季更大——冬天站点差异更值钱。
- **京士柏紧邻天文台山，是最好的实时代理站**，`--watch` 里可以优先参考它。

## 三、模型回测（7 个全球模式，n=87）

观测 = 天文台站 `CLMMAXT`；预报 = Open-Meteo previous-runs，同格点 22.302 / 114.173。

| 方法 | 偏差 | MAE | RMSE | 残差 sd |
|---|---:|---:|---:|---:|
| 单模型最好（UKMO） | +0.30 | 0.84 | 1.01 | 0.97 |
| ECMWF IFS 0.25° | +1.06 | 1.16 | 1.41 | 0.94 |
| **7 模型融合 + 偏差修正** | **+0.00** | **0.62** | **0.79** | **0.80** |
| 仅气候季节均值（对照） | −0.69 | 1.82 | 2.30 | 2.21（r=0.04） |

要点：

- 融合 + 偏差修正把 RMSE 从 1.01–1.41 压到 **0.79 °C**，这是可用与不可用的分界线。
- ECMWF IFS 单独用偏热 +1.06 °C——不要单独信它。
- 纯气候季节性几乎无预测力（r=0.04），只在极长提前期兜底。

## 四、dressed ensemble 公式

在 `hk_edge.py` 的 `bucket_probs()` 中实现：

```
members  = ECMWF 50 成员集合预报
ens_mean = mean(members)
mu       = det_mean(7模型确定性均值) + calib['bias']      # 或 --mu 手动覆盖
draws    = [mu + (m - ens_mean) for m in members]         # 点估计 + 集合距平
sd       = calib['resid_sd']                              # 残差噪声

P(bucket b) = mean_m[ Φ((b+1 - x_m)/sd) − Φ((b - x_m)/sd) ]
```

最后对所有档归一化。分布宽度随提前期自动变化：提前 6 天很散，当天下午很尖。
`--mu` 是与官方预报做主观加权的入口——模型与 HKO 分歧时用得上。

## 五、日内外推（nowcast）

`--watch` 的路径：

```
站点偏差 cur_bias = 此刻天文台实测 − 此刻网格值
站点加成 k        = calib['bias'] − cur_bias
今日峰值          = 此刻实测 + 气候升温幅度 d['mean'] + k

到档位门槛 b 的概率 = 1 − Φ( (b − 此刻实测 − d['mean'] − k) / d['sd'] )
```

- 气候表 `diurnal_climatology.json` 来自 ERA5 2020–2025，按月 × 逐时统计「该时刻温度 → 当日峰值」的升温幅度的均值与 sd。
- 越接近午后，剩余升温幅度越小，估计越准。
- **阴雨 / 雷暴日显著偏低**：若午后雨已到，外推值就是上限而非中枢。

## 六、数据源（全部免费、无需 API key）

| 用途 | 端点 |
|---|---|
| 结算源历史（回测/校准） | `HKO opendata.php?dataType=CLMMAXT&station=HKO` |
| 实时各站温度（含天文台站） | `HKO weather.php?dataType=rhrread`（每小时） |
| 官方锚（市场看的就是它） | `HKO weather.php?dataType=fnd` → `forecastMaxtemp` |
| 多模型确定性融合 | `api.open-meteo.com/v1/forecast`，models = ecmwf_ifs025, gfs, icon, gem, metno, jma, ukmo |
| 概率分布 | `ensemble-api.open-meteo.com/v1/ensemble`（ECMWF 50 成员） |
| 回测用历史预报 | `previous-runs-api.open-meteo.com`，past_days=92（接口上限） |
| 日内气候曲线 | `archive-api.open-meteo.com/v1/archive`（ERA5） |

## 七、脚本的依赖差异（排障先看这里）

| 脚本 | 依赖 | 备注 |
|---|---|---|
| `hk_edge.py` | 仅标准库（`requests` 可选） | 自动下载 HKO 历史、自动校准 |
| `diurnal.py` | 仅标准库 | 跑一次数分钟，产出已有缓存，通常不必重跑 |
| `fetch_stations.py` | 仅标准库 | 补 `analyze.py` 需要的多站 CSV |
| `backtest.py` | pandas + numpy | 无网/无依赖时会失败 |
| `analyze.py` | pandas + numpy + 多站 CSV | 先跑 `fetch_stations.py` |
| `hitrate.py` | pandas + numpy | 依赖 `backtest.py` 产出的 `backtest_join.csv` |

Windows 环境：`python3` 通常不存在，一律用 `py -3`。

## 八、cache / 留痕文件

`scripts/data/` 下会被 `.gitignore` 掉的：训练集 `maxt_*.csv`、本地缓存 `intraday_log.json`、`decision_log.csv`、`backtest_join.csv`、`model_calib.json`、`summary.json`、`hitrate.json`、`sens_*.json`。
**唯一需要保留进版本的是 `diurnal_climatology.json`**（重算需数分钟）。
