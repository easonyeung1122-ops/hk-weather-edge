#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reach_next.py —— 「今天还能不能升到下一档」的**双口径**估计（v0.16.24）

## 为什么需要这个脚本

2026-09-15 14:30 实测踩到的一个**极易上当的口径陷阱**：

HKO 自己的九月日最高分布是**双峰**的（2015–2024, n=300）：

| 阈值 | P(日最高 ≥ 阈值) |
|---|---:|
| 31.0 | 67.3% |
| 31.4 | 59.3% |
| 32.0 | 48.0% |

于是「结果条件」给出

    P(≥32.0 | 已≥31.4) = 48.0 / 59.3 = **80.9%**

看起来是极强的看多信号 —— 但它是**错的**。因为 `{已≥31.4}` 这个集合里，
包含了大量「**晚**到 31.4 的日子」（15、16 时才到），那些日子这时候已经没有时间了。
把它们的结局算进来，会系统性抬高概率。

正确的条件变量是**时刻 + 当时的 running max**：`P(final ≥ next | rm(h) = level)`。
HKO 没有逐时站史（只有日最高），所以这个口径只能借 VHHH 机场 ASOS 的逐时存档。

## 双口径校准（r 因子）

同一个 VHHH 九月样本（n=330）同时算两种口径：

    r = P(final≥next | rm14 = L, gap≤0.5)  ÷  P(final≥next | final ≥ L)

r 衡量「结果条件相对时间条件抬高了多少倍」，是**站点无关**的形状因子
（只取决于当日成峰时刻的离散度）。把它乘到 HKO 的原生结果条件上，
就得到 HKO 的时间条件估计：

    P_HKO(final ≥ next) ≈  P_HKO(final≥next | final≥level) × r

两条独立路线（VHHH 时间条件直测、HKO 结果条件 × r）在 2026-09-15 收敛到
43–46%，而同期网格模型只给 24%、市场给 21% —— 这个分歧是本工具目前
最大的未解问题（见 SKILL.md）。

## 用法

    py -3 scripts/reach_next.py --level 31.4 --next 32.0 --hour 14
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import statistics as st
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
MAXT_HKO = os.path.join(DATA, 'maxt_HKO.csv')
VHHH = os.path.join(DATA, '_vhhh_asos.csv')


def qv(a, q):
    a = sorted(a)
    return a[min(len(a) - 1, int(q * (len(a) - 1)))]


# ---------------------------------------------------------------- HKO 日最高
def load_maxt_hko():
    """maxt_HKO.csv → [(year, month, max)]。"""
    rows = []
    with io.open(MAXT_HKO, encoding='utf-8-sig') as f:
        for r in csv.reader(f):
            if len(r) < 4:
                continue
            try:
                y, m, v = int(r[0]), int(r[1]), float(r[3])
            except ValueError:
                continue
            rows.append((y, m, v))
    return rows


def hko_outcome_cond(level, nxt, month='09'):
    """HKO 原生的**结果条件** P(final≥next | final≥level)，分窗口。"""
    rows = load_maxt_hko()
    out = []
    for lo, hi in ((2015, 2024), (2005, 2024), (1995, 2024)):
        s = [v for y, m, v in rows if m == int(month) and lo <= y <= hi]
        n = len(s)
        if n == 0:
            continue
        p_lv = sum(1 for v in s if v >= level) / n
        p_nx = sum(1 for v in s if v >= nxt) / n
        out.append({'window': f'{lo}-{hi}', 'n': n, 'p_level': p_lv,
                    'p_next': p_nx,
                    'cond': (p_nx / p_lv) if p_lv > 0 else None})
    return out


# ---------------------------------------------------------------- VHHH 逐时
def load_vhhh():
    """_vhhh_asos.csv → {date: {minutes: temp}}（整数分辨率，见 SKILL.md）。"""
    if not os.path.exists(VHHH):
        return {}
    days = defaultdict(dict)
    with io.open(VHHH, encoding='utf-8') as f:
        rd = csv.reader(f)
        next(rd)
        for r in rd:
            if len(r) < 3 or r[2] in ('M', ''):
                continue
            try:
                v = float(r[2])
            except ValueError:
                continue
            days[r[1][:10]][int(r[1][11:13]) * 60 + int(r[1][14:16])] = v
    return days


def _at(o, m):
    k = max((k for k in o if k <= m), default=None)
    return o[k] if k is not None else None


def vhhh_cond(days, level_i, next_i, hour, month='09', gap_max=0.5):
    """VHHH 的**时间条件**与**结果条件**，及两者的比值 r。"""
    cut = hour * 60
    time_rows, all_final = [], []
    for d, o in days.items():
        if d[5:7] != month or len(o) < 30:
            continue
        cur = _at(o, cut)
        if cur is None:
            continue
        rm = max(v for k, v in o.items() if k <= cut)
        M = max(o.values())
        all_final.append(M)
        if rm == level_i and (rm - cur) <= gap_max:
            time_rows.append(M)
    p_time = (sum(1 for v in time_rows if v >= next_i) / len(time_rows)
              if time_rows else None)
    base = [v for v in all_final if v >= level_i]
    p_out = (sum(1 for v in base if v >= next_i) / len(base)) if base else None
    r = (p_time / p_out) if (p_time and p_out) else None
    return {'n_time': len(time_rows), 'p_time': p_time,
            'n_out': len(base), 'p_out': p_out, 'r': r}


def main():
    ap = argparse.ArgumentParser(description='「能否升到下一档」的双口径估计')
    ap.add_argument('--level', type=float, required=True,
                    help='今日 running max（如 31.4）')
    ap.add_argument('--next', type=float, required=True,
                    help='目标档位下边界（如 32.0）')
    ap.add_argument('--hour', type=int, default=14)
    ap.add_argument('--month', default='09')
    ap.add_argument('--gap-max', type=float, default=0.5,
                    help='纳入时间条件组的最大 gap（rm − cur）')
    ap.add_argument('--station-discount', type=float, default=0.90,
                    help='跨站点折扣：HKO 的 R 比 VHHH 小（k 2.4 vs 2.64）')
    a = ap.parse_args()

    need = a.next - a.level
    lv_i = int(round(a.level))
    nx_i = int(round(a.next))

    print('=' * 78)
    print(f'「能否升到 {a.next:.1f}°C」双口径估计  |  rm={a.level:.1f}  '
          f'还需 +{need:.2f}°C  @ {a.hour}:00  ({a.month} 月)')
    print('=' * 78)

    # ---- 路线 1：HKO 原生结果条件 ----
    print('\n【路线 1】HKO 原生 → **结果条件** P(final≥%.1f | final≥%.1f)'
          % (a.next, a.level))
    print('  ⚠ 这是**高估**：集合里含大量「晚到」的日子，它们已无时间。必须乘 r。')
    hko = hko_outcome_cond(a.level, a.next, a.month)
    for h in hko:
        print(f"    {h['window']}  n={h['n']:3d}  P(≥{a.level:.1f})={100*h['p_level']:5.1f}%  "
              f"P(≥{a.next:.1f})={100*h['p_next']:5.1f}%  → 条件 {100*h['cond']:5.1f}%")

    # ---- 路线 2：VHHH 时间条件 + r ----
    days = load_vhhh()
    if not days:
        print('\n⚠ 缺 scripts/data/_vhhh_asos.csv（跑 station_r_probe.py --refresh 生成）')
        return
    v = vhhh_cond(days, lv_i, nx_i, a.hour, a.month, a.gap_max)
    print(f'\n【路线 2】VHHH 机场站（逐时，n={len(days)} 天）')
    if v['p_time'] is None:
        print('    时间条件组样本为空（整数分辨率下 gap 阈值可能切不出样本）')
    else:
        print(f"    时间条件 P(final≥{nx_i} | rm{a.hour}= {lv_i}, gap≤{a.gap_max}): "
              f"n={v['n_time']}  →  {100*v['p_time']:.1f}%")
    print(f"    结果条件 P(final≥{nx_i} | final≥ {lv_i}): "
          f"n={v['n_out']}  →  {100*v['p_out']:.1f}%")
    if v['r']:
        print(f"    → 口径因子 r = {v['r']:.2f}  （结果条件相对时间条件**高估**了 "
              f"{1/v['r']:.2f} 倍）")

    # ---- 合成 ----
    print('\n【合成】P(final ≥ %.1f):' % a.next)
    ests = {}
    if v['p_time'] is not None:
        ests[f'① VHHH 时间条件直测（跨站折扣 {a.station_discount:.2f}）'] = \
            v['p_time'] * a.station_discount
    if v['r'] and hko:
        h15 = next((h for h in hko if h['window'] == '2015-2024'), hko[0])
        ests['② HKO 结果条件 × r（近 10 年）'] = h15['cond'] * v['r']
        ests['②b HKO 结果条件 × r（1995-2024）'] = hko[-1]['cond'] * v['r']
    for k, val in ests.items():
        print(f'    {k:44s}  {100*val:5.1f}%')
    if ests:
        vals = [x for x in ests.values() if x is not None]
        lo, hi = min(vals), max(vals)
        print(f'\n    **站点口径区间 [{100*lo:.0f}%, {100*hi:.0f}%]，'
              f'中值 {100*st.mean(vals):.0f}%**')
    print('\n  ⓘ 网格模型（l_integrate）给的值通常显著更低：k 是在**无条件组**上'
          '\n     标定的，用在「回落条件组」上偏小。两者分歧未解，需 HKO 逐时站史才能直测。')
    print('  ⓘ VHHH ASOS 是整数分辨率 → <1°C 的阈值不可分辨，'
          'gap 阈值切出的往往是同一子集。')


if __name__ == '__main__':
    main()
