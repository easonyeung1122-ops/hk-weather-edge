#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复盘：把 data/decision_log.csv 的逐时点「模型公允 P vs 市场 P」按结算档算 Brier。

为什么需要它：`--score` 打的是 forecast_log.jsonl（one-touch 阶梯口径），
回答不了「模型和市场谁更准、在一天的哪个时段更准」。
decision_log.csv 恰好逐时点成对记录了 fair_p 与 market_p，是唯一能做这件事的数据。

用法
----
  python scripts/review_brier.py                      # 结算档自动从 maxt_HKO.csv 取
  python scripts/review_brier.py --truth 2026-09-11=32
  python scripts/review_brier.py --json               # 机器可读

口径提醒（很重要）
------------------
Brier 的分母**只取 decision_log 实际记录的 5–8 个档位**，不是全部 11–12 档。
所以绝对值偏高、且不同日之间不可直接比大小。
**同一个时点内「模型 vs 市场」的差值**才有意义——两者用同一套档位、同一套真值。
"""
import argparse
import collections
import csv
import datetime as dt
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
LOG = os.path.join(DATA, 'decision_log.csv')
MAXT = os.path.join(DATA, 'maxt_HKO.csv')
ARCHIVE = os.path.join(DATA, 'obs_1min_archive.json')
TODAY_LOG = os.path.join(DATA, 'obs_1min_log.json')

# 上午 / 午后 分界（与 SKILL.md「出仓窗口 09:00–11:30」一致）
MORNING_CUTOFF_HOUR = 12


def load_settled(maxt_path=MAXT):
    """从结算源本身（maxt_HKO.csv，Absolute Daily Max）取逐日结算档 = floor(值)。"""
    out = {}
    if not os.path.exists(maxt_path):
        return out
    for row in csv.reader(open(maxt_path, encoding='utf-8-sig')):
        if len(row) < 4:
            continue
        try:
            y, m, d, v = int(row[0]), int(row[1]), int(row[2]), float(row[3])
        except ValueError:
            continue
        out[f'{y:04d}-{m:02d}-{d:02d}'] = int(v)   # 档位 N = [N.0, N+1.0)
    return out


def load_rows(path=LOG):
    if not os.path.exists(path):
        return []
    raw = open(path, encoding='utf-8-sig').read().splitlines()
    rows = []
    for line in raw[1:]:
        f = line.split(',')
        if len(f) < 6:
            continue
        try:
            rows.append((f[0].strip(), f[1].strip(), int(f[2]),
                         float(f[3]), float(f[4])))
        except ValueError:
            continue
    return rows


def parse_truth(s):
    """DATE=BUCKET 逗号分隔 -> dict"""
    out = {}
    if not s:
        return out
    for part in s.split(','):
        k, _, v = part.partition('=')
        out[k.strip()] = int(v)
    return out


def brier(d, truth):
    """d: {bucket: (fair_p, market_p)}；返回 (模型Brier, 市场Brier, n, 模型众数, 市场众数)"""
    bm = bk = 0.0
    n = 0
    for b, (mp, mkp) in d.items():
        y = 1.0 if b == truth else 0.0
        bm += (mp - y) ** 2
        bk += (mkp - y) ** 2
        n += 1
    if n == 0:
        return None
    mmode = max(d.items(), key=lambda x: x[1][0])[0]
    kmode = max(d.items(), key=lambda x: x[1][1])[0]
    return bm / n, bk / n, n, mmode, kmode


def hour_of(tstr):
    """decision_log 的 logged_at 是 'YYYY-MM-DD HH:MM'。"""
    try:
        return int(tstr.split()[1].split(':')[0])
    except Exception:
        return None


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _load_station_series():
    """→ [(label, [(HH:MM, temp), ...]), ...]，来源：当日 obs_1min_log + obs_1min_archive。"""
    out = []
    if os.path.exists(TODAY_LOG):
        j = json.load(open(TODAY_LOG, encoding='utf-8'))
        out.append((j.get('date', '?'),
                    [(p['t'][11:16], p['hko']) for p in j.get('points', [])]))
    if os.path.exists(ARCHIVE):
        a = json.load(open(ARCHIVE, encoding='utf-8'))
        for day, pts in sorted(a.items()):
            out.append((day, [(p['t'][11:16], p['hko']) for p in pts]))
    return out


def cmd_flicker(from_hhmm='12:30'):
    """σ_flicker：站点午后 10 分钟级抖动。

    为什么需要它：分位表 `remaining_rise_cdf.json` 是 ERA5 **网格**口径，
    网格（9–31 km 空间平均）把单点的局地抖动抹平了。于是「已观测最高距下一整数档
    只剩 0.1–0.3 °C」这种情形，网格口径看起来是稀有事件（≈9%），
    而真实站点在 10 分钟内就能抖过去（≈99%）。
    2026-09-11 / 09-12 两天的实盘：市场按抖动定价（97% / 99.85%），我们按网格定价（25% / 9%），
    市场两次都对。
    """
    series = _load_station_series()
    if not series:
        print('[warn] 没有站点序列（obs_1min_log.json / obs_1min_archive.json 都缺）')
        return 1
    sds = []
    print('=' * 78)
    print('σ_flicker —— 站点午后（%s 后）10 分钟相邻差分' % from_hhmm)
    print('（只取间隔恰为 10 分钟的对，避免混入 20/30 分钟的跨度）\n')
    for label, pts in series:
        seq = [(t, v) for t, v in pts if t >= from_hhmm]
        diffs = []
        for (t1, v1), (t2, v2) in zip(seq, seq[1:]):
            h1, m1 = map(int, t1.split(':'))
            h2, m2 = map(int, t2.split(':'))
            if (h2 * 60 + m2) - (h1 * 60 + m1) == 10:
                diffs.append(v2 - v1)
        if len(diffs) < 2:
            print(f'{label}  样本不足 (n={len(diffs)})，跳过')
            continue
        mu = sum(diffs) / len(diffs)
        sd = (sum((d - mu) ** 2 for d in diffs) / len(diffs)) ** 0.5
        sds.append(sd)
        print(f'{label}  n={len(diffs)}  均值 {mu:+.3f}  σ = {sd:.3f} °C  '
              f'最大单步 {max(abs(d) for d in diffs):.1f}')
        print(f'   {[f"{v:.1f}" for _, v in seq]}')
    if not sds:
        return 1
    sig = sum(sds) / len(sds)
    print(f'\n→ 合并 σ_flicker = {sig:.3f} °C（{len(sds)} 天；注意 σ 随天气型变化，晴热静风日最大）')

    print('\n' + '=' * 78)
    print(f'距下一整数档还需 d °C 时，「剩余窗口内至少越线一次」的概率（N(0, σ={sig:.3f})）')
    print(f'{"d":>5} | {"单步":>7} | {"剩 6 步":>8} | {"剩 10 步":>8} | {"剩 15 步":>8} | 判读')
    print('-' * 78)
    for d in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0):
        p1 = 1 - _phi(d / sig)
        row = [1 - (1 - p1) ** n for n in (6, 10, 15)]
        note = ('★ 抖动主导：禁止反向下注' if d <= 0.3 else
                '过渡区：按 0.5 折' if d < 0.6 else '分位表可用（抖动项 <30%）')
        print(f'{d:>5.1f} | {p1:>7.1%} | {row[0]:>8.1%} | {row[1]:>8.1%} | {row[2]:>8.1%} | {note}')
    print('\n对照（分位表 = ERA5 网格口径，不含抖动）：h=13 需再升 +0.1 → 约 9%；+0.4 → 约 0-1%。')
    print('→ 网格口径在 d ≤ 0.3 时低估 1–2 个数量级。')
    return 0


def main():
    ap = argparse.ArgumentParser(description='decision_log 的 Brier 时间分层复盘')
    ap.add_argument('--truth', default='',
                    help='覆盖结算档，格式 "2026-09-11=32,2026-09-12=32"（默认读 maxt_HKO.csv，可含本日实测下限）')
    ap.add_argument('--flicker', action='store_true',
                    help='不算 Brier，改算站点午后 10 分钟抖动 σ_flicker 与越线概率表')
    ap.add_argument('--from', dest='from_hhmm', default='12:30',
                    help='--flicker 的午后起点（默认 12:30）')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    a = ap.parse_args()

    if a.flicker:
        return cmd_flicker(a.from_hhmm)

    settled = load_settled()
    settled.update(parse_truth(a.truth))
    rows = load_rows()

    # 按 (target_date, 时点) 归组；注意 logged_at 归属「作出该预测的时刻」
    g = collections.defaultdict(dict)
    for t, date, bucket, fair, mkt in rows:
        g[(date, t)][bucket] = (fair, mkt)

    result = {}
    for date in sorted({k[0] for k in g}):
        if date not in settled:
            continue
        truth = settled[date]
        keys = sorted([k for k in g if k[0] == date], key=lambda k: k[1])
        per = []
        for k in keys:
            r = brier(g[k], truth)
            if not r:
                continue
            bm, bk, n, mm, km = r
            per.append(dict(t=k[1], hour=hour_of(k[1]), n=n,
                            brier_model=round(bm, 6), brier_market=round(bk, 6),
                            mode_model=mm, mode_market=km,
                            winner=('model' if bm < bk - 1e-9 else
                                    'market' if bk < bm - 1e-9 else 'tie')))
        # 分时段汇总（只看当天作出的预测，排除提前一天存档的）
        same_day = [p for p in per if p['t'].startswith(date)]
        summer = [p for p in same_day if p['hour'] is not None
                  and p['hour'] < MORNING_CUTOFF_HOUR]
        pmer = [p for p in same_day if p['hour'] is not None
                and p['hour'] >= MORNING_CUTOFF_HOUR]

        def agg(lst):
            if not lst:
                return None
            return dict(n=len(lst),
                        brier_model=round(sum(x['brier_model'] for x in lst) / len(lst), 6),
                        brier_market=round(sum(x['brier_market'] for x in lst) / len(lst), 6))

        result[date] = dict(truth=truth, points=per,
                            morning=agg(summer), afternoon=agg(pmer))

    source = dict(decision_log=LOG, decision_log_rows=len(rows), maxt=MAXT)

    if a.json:
        print(json.dumps(dict(source=source, days=result),
                         ensure_ascii=False, indent=2))
        return

    # ⚠ 必须打印数据源路径：本技能有两份副本（.workbuddy 是活的、.codebuddy 是 git 源），
    # 各自的 scripts/data/ 是独立的本地状态。跑错副本会读到几天前的旧日志，
    # 复盘结论会整个错掉（2026-09-12 实际踩过：.codebuddy 的 decision_log 停在 09-08）。
    print(f'数据源 decision_log: {LOG}  ({len(rows)} 行)')
    print(f'数据源 maxt_HKO    : {MAXT}')
    if not os.path.exists(LOG):
        print('[warn] decision_log.csv 不存在 —— 该副本从未跑过 --date/--watch？')
        return

    for date, R in result.items():
        print()
        print('=' * 88)
        print(f'{date}   结算档 = {R["truth"]}°C   （记录时点 {len(R["points"])} 个）')
        print(f'{"时刻":<18}{"N":>3}  {"模型Brier":>10}{"市场Brier":>11}  {"胜方":>7}'
              f'{"模型众数":>9}{"市场众数":>9}')
        for p in R['points']:
            print(f'{p["t"]:<18}{p["n"]:>3}  {p["brier_model"]:>10.4f}'
                  f'{p["brier_market"]:>11.4f}  {p["winner"]:>7}'
                  f'{p["mode_model"]:>7}°C{p["mode_market"]:>7}°C')
        for label, key in (('上午 (<12时)', 'morning'), ('午后 (>=12时)', 'afternoon')):
            A = R.get(key)
            if not A:
                continue
            bm, bk = A['brier_model'], A['brier_market']
            if abs(bm - bk) < 1e-9:
                tail = '平'
            elif bm < bk:
                tail = f'模型领先 {(bk - bm) / bk:.1%}'
            else:
                tail = f'市场领先 {(bm - bk) / bm:.1%}'
            print(f'  {label:<12} n={A["n"]}  模型 {bm:.4f}  vs  市场 {bk:.4f}  → {tail}')
    print()
    print('口径：Brier 分母只含 decision_log 记录的 5–8 个档位，绝对值偏高；'
          '只有「模型 vs 市场」的差值可比。')


if __name__ == '__main__':
    sys.exit(main())
