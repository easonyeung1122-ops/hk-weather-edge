import pandas as pd, numpy as np, json, warnings
warnings.filterwarnings('ignore')

def load(station):
    df = pd.read_csv(f'data/maxt_{station}.csv', skiprows=2, encoding='utf-8-sig',
                     names=['y','m','d','v','c'])
    df['v'] = pd.to_numeric(df['v'], errors='coerce')
    df = df.dropna(subset=['v'])
    df['date'] = pd.to_datetime(dict(year=df.y, month=df.m, day=df.d))
    return df.set_index('date')['v']

hko = load('HKO'); kp = load('KP'); tkl = load('TKL'); sek = load('SEK'); cch = load('CCH')
print(f"HKO: {hko.index.min().date()} ~ {hko.index.max().date()}  n={len(hko)}")
R = hko.loc['2016':'2026']
print(f"近10年样本 n={len(R)}\n")

# 1) 各月分布
print("=== 天文台站(HKO) 日最高温 分月统计 (2016-2026) ===")
g = R.groupby(R.index.month)
tab = pd.DataFrame({'n':g.size(),'mean':g.mean().round(2),'sd':g.std().round(2),
                    'p10':g.quantile(.1).round(1),'p90':g.quantile(.9).round(1)})
tab.index.name='月'
print(tab.to_string())

# 2) HKO vs 其他站的差值（同日）
print("\n=== HKO 减去 其他站 的平均差值（正=天文台更热）2016-2026, 按月 ===")
for name, s in [('京士柏KP',kp),('打鼓岭TKL',tkl),('石岗SEK',sek),('长洲CCH',cch)]:
    s10 = s.loc['2016':'2026']
    joined = pd.concat([R.rename('hko'), s10.rename('o')], axis=1).dropna()
    diff = (joined.hko - joined.o).groupby(joined.index.month).mean().round(2)
    print(f"{name}: " + "  ".join([f"{m}月{diff[m]:+.2f}" for m in range(1,13) if m in diff.index]))

# 3) 日间持续性
print("\n=== 日间持续性: 今日最高温 - 昨日最高温 ===")
d = R.diff().dropna()
print(f"  日际变化 sd = {d.std():.2f} °C,  均值 {d.mean():+.2f}")
print(f"  变化幅度分布: |Δ|<=1: {(d.abs()<=1).mean():.1%}  <=2: {(d.abs()<=2).mean():.1%}  >=3: {(d.abs()>=3).mean():.1%}")

# 4) bucket 边界敏感性：实际值小数部分的分布
print("\n=== 关键: 实际最高温的小数部分分布 (0.1°C精度决定bucket归属) ===")
frac = (R - R.round(0) + 0.5)  # 0..1, 0.5 = .0
print(f"  小数 x.x0: {(R.apply(lambda x: abs(round(x)-x)<0.05)).mean():.1%}")
print(f"  距下边界<=0.2 (即 x.0~x.2): {((R % 1)<=0.2).mean():.1%}")
print(f"  距上边界<=0.2 (即 x.8~x.9): {((R % 1)>=0.8).mean():.1%}")
