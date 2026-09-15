#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点口径「剩余升温」探针 —— 把 ERA5 网格的 R 表标定到真实观测站。

为什么需要（v0.16.21，2026-09-15）
--------------------------------
`data/remaining_rise_cdf.json` 的 R = 日最高 − running_max(h) 全部来自
**ERA5 25km 网格**。网格把单点的日内尖峰平滑掉，于是「13 时之后还能升多少」
被压得比真实站点小得多。此前只有两个粗略说法（「尾部薄 4.7 倍」「站点口径
约为其 2–5 倍」），没有可复现的量化，导致 2026-09-15 12:20 站点已冲到 31.4°C、
越过网格全天峰值 29.5°C 时，still 拿网格 R 表算出「32 档 ~20%」这种明显偏低的数。

本脚本用**真实观测站**逐时温度直接算 R，并和同一时段、同一站点的 ERA5 网格对算，
给出两个口径的比值。

数据源
------
Iowa Environmental Mesonet ASOS 存档（VHHH 香港国际机场，2015 年起，30 分钟），
缓存在 `data/_vhhh_asos.csv`（约 5.6MB），不重复下载。

用法
----
    py -3 scripts/station_r_probe.py                    # 全部诊断
    py -3 scripts/station_r_probe.py --rm 31.4 --cur 30.2 --hour 13
                                                        # 直接给今日条件
    py -3 scripts/station_r_probe.py --refresh          # 强制重下 ASOS

输出
----
1. 站点口径 R(h) 的分位与「需要再升 x°C」的概率
2. 同站点 ERA5 网格的同一统计 → **放大比 k(h)**（日较差比 vs R 比）
3. 条件化：给定 (rm − cur) 的回落幅度，R 分布怎么变
   （v0.16.21 实测：13 时回落 ≥0.5°C 的日子，P(R≥1°C) 从 51.5% 掉到 25.8%）
"""
import argparse
import csv
import datetime as dt
import json
import os
import statistics as st
import sys
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hk_edge as H  # noqa: E402

DATA = H.DATA
VHHH_CACHE = os.path.join(DATA, '_vhhh_asos.csv')
ERA5_HKO = os.path.join(DATA, '_era5_hourly_cache.json')       # ERA5 @ HKO 点
ERA5_VHHH = os.path.join(DATA, '_era5_vhhh.json')              # ERA5 @ VHHH 点（同点位对照）
VHHH_LAT, VHHH_LON = 22.309, 113.915
ARCH = ("https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}"
        "&start_date=2015-01-01&end_date={end}&hourly=temperature_2m"
        "&timezone=Asia%2FShanghai")
ASOS = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=VHHH"
        "&data=tmpc&year1={y1}&month1=1&day1=1&year2={y2}&month2=12&day2=31"
        "&tz=Asia%2FHong_Kong&format=onlycomma&latlon=no&missing=M&trace=T"
        "&direct=no&report_type=3&report_type=4")


def fetch_asos(refresh=False):
    if refresh or not os.path.exists(VHHH_CACHE):
        y2 = dt.datetime.now().year
        url = ASOS.format(y1=2015, y2=y2)
        raw = urllib.request.urlopen(
            urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}),
            timeout=120).read()
        open(VHHH_CACHE, 'wb').write(raw)
    return VHHH_CACHE


def load_station(path):
    """→ {date: {minute_of_day: temp}}（VHHH，METAR 整数度为主）。"""
    days = defaultdict(dict)
    with open(path, encoding='utf-8') as f:
        rd = csv.reader(f)
        next(rd)
        for r in rd:
            if len(r) < 3 or r[2] in ('M', ''):
                continue
            try:
                v = float(r[2])
            except Exception:
                continue
            t = r[1]
            days[t[:10]][int(t[11:13]) * 60 + int(t[14:16])] = v
    return days


def load_era5(path, is_cache=True):
    """ERA5 → {date: {hour: temp}}。is_cache=True 读 hk_edge 的扁平缓存，否则读 API 原始 json。"""
    if is_cache:
        c = json.load(open(path, encoding='utf-8'))
        time, temp = c['time'], c['temperature_2m']
    else:
        c = json.load(open(path, encoding='utf-8'))['hourly']
        time, temp = c['time'], c['temperature_2m']
    d = defaultdict(dict)
    for t, v in zip(time, temp):
        if v is None:
            continue
        d[t[:10]][int(t[11:13])] = float(v)
    return d


def station_r(days, month, cut_min):
    """站点 R 统计：返回 (R 列表, 日较差列表, rm, cur, 日期)。"""
    rec = []
    for d, o in days.items():
        if d[5:7] != month or len(o) < 6 * 4:
            continue
        up = {k: v for k, v in o.items() if k <= cut_min}
        if len(up) < 20:
            continue
        cur = at(o, cut_min)
        if cur is None:
            continue
        rec.append({'d': d, 'rm': max(up.values()), 'cur': cur,
                    'R': max(o.values()) - max(up.values()),
                    'rng': max(o.values()) - min(o.values())})
    return rec


def grid_r(days, month, hour):
    rec = []
    for d, hh in days.items():
        if d[5:7] != month or len(hh) < 20 or hour not in hh:
            continue
        rec.append({'d': d, 'rm': max(v for k, v in hh.items() if k <= hour),
                    'cur': hh[hour],
                    'R': max(hh.values()) - max(v for k, v in hh.items() if k <= hour),
                    'rng': max(hh.values()) - min(hh.values())})
    return rec


def qv(a, q):
    a = sorted(a)
    return a[min(len(a) - 1, int(q * (len(a) - 1)))]


def at(series, minute):
    k = max((k for k in series if k <= minute), default=None)
    return series[k] if k is not None else None


def probe_station(rec, cond_gap=None, cond_rm=None):
    rows = rec
    if cond_gap is not None:
        rows = [r for r in rows if (r['rm'] - r['cur']) >= cond_gap]
    if cond_rm is not None:
        rows = [r for r in rows if abs(r['rm'] - cond_rm) <= 1.0]
    if len(rows) < 6:
        return None
    return {
        'n': len(rows),
        'R': sorted(r['R'] for r in rows),
        'rm_p50': st.median(r['rm'] for r in rows),
        'cur_p50': st.median(r['cur'] for r in rows),
    }


def report(tag, s, needs):
    if not s:
        print(f'{tag}: 样本过少')
        return
    print(f'{tag}  n={s["n"]}  rm中位 {s["rm_p50"]:.1f}  cur中位 {s["cur_p50"]:.1f}')
    print('   R 分位: ' + '  '.join(
        f'p{int(q*100)}={qv(s["R"], q):.1f}' for q in (0.25, 0.5, 0.75, 0.9, 0.95)))
    line = '   '.join(f'P(R≥{x:.1f})={100*sum(1 for v in s["R"] if v >= x)/s["n"]:.1f}%'
                      for x in needs)
    print('   ' + line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--month', default='09', help='月份，两位（默认 09）')
    ap.add_argument('--hour', type=int, default=13)
    ap.add_argument('--rm', type=float, help='今日 running max（结算站）')
    ap.add_argument('--cur', type=float, help='今日当前值（结算站）')
    ap.add_argument('--refresh', action='store_true', help='强制重下 ASOS')
    a = ap.parse_args()

    cut = a.hour * 60
    s_days = load_station(fetch_asos(a.refresh))
    s_rec = station_r(s_days, a.month, cut)
    print('=' * 78)
    print(f'站点口径 R 探针  |  {a.month} 月 截至 {a.hour}:00  |  ASOS 实测 vs ERA5')
    print('=' * 78)
    report('【站点 VHHH 无条件】', probe_station(s_rec), (0.5, 0.6, 1.0, 1.6))

    # ---- 🚨 同点位放大比：必须在**同一个坐标**上算，否则混进站点差 ----
    if not os.path.exists(ERA5_VHHH):
        end = (dt.date.today() - dt.timedelta(days=10)).isoformat()
        raw = urllib.request.urlopen(
            urllib.request.Request(ARCH.format(lat=VHHH_LAT, lon=VHHH_LON, end=end),
                                   headers={'User-Agent': 'Mozilla/5.0'}),
            timeout=90).read()
        open(ERA5_VHHH, 'wb').write(raw)
    g_vhhh = grid_r(load_era5(ERA5_VHHH, is_cache=False), a.month, a.hour)
    g_hko = grid_r(load_era5(ERA5_HKO, is_cache=True), a.month, a.hour)

    print('\n【放大比 k = R_站 / R_网】—— 🚨 必须同点位，否则 k 会被站点差污染')
    for tag, g, s in (('同点位 VHHH', g_vhhh, s_rec),):
        if not g or not s:
            continue
        kr = st.mean([r['rng'] for r in s]) / st.mean([r['rng'] for r in g])
        kR = st.mean([r['R'] for r in s]) / st.mean([r['R'] for r in g])
        print(f'  {tag}: n_站={len(s)} n_网={len(g)}')
        print(f'    日较差  站 {st.mean([r["rng"] for r in s]):.2f} vs 网 '
              f'{st.mean([r["rng"] for r in g]):.2f} → 比 {kr:.2f}')
        print(f'    R 均值  站 {st.mean([r["R"] for r in s]):.2f} vs 网 '
              f'{st.mean([r["R"] for r in g]):.2f} → **k = {kR:.2f}**')
    if g_hko:
        print(f'  ERA5@HKO 点（仅参考）: n={len(g_hko)}  R 均值 '
              f'{st.mean([r["R"] for r in g_hko]):.2f}  日较差 '
              f'{st.mean([r["rng"] for r in g_hko]):.2f}')
        print('    ⚠ 拿「ERA5@HKO vs VHHH 站」直接相除会得到 k≈4.7 —— **这是错的**，'
              '\n      它把「HKO 网格点午后特别平」混进了「站点比网格尖」。'
              '\n      正确做法：k 取同点位值，再用日较差比把 k 外推到 HKO。')

    # ---- 回落条件 ----
    print('\n【条件化：13 时相对当日 running max 的回落（站点）】')
    for g in (0.0, 0.5, 1.0):
        report(f'  回落 ≥{g:.1f}°C', probe_station(s_rec, cond_gap=g), (0.5, 1.0, 1.6))

    if a.rm is not None and a.cur is not None:
        gap = a.rm - a.cur
        print(f'\n【今日（结算站 HKO）】rm={a.rm:.1f}  cur={a.cur:.1f}  回落 {gap:.1f}°C')
        base = probe_station(s_rec, cond_gap=0.5) if gap >= 0.5 else probe_station(s_rec)
        for b in range(int(a.rm) + 1, int(a.rm) + 3):
            need = b - a.rm
            p = 100 * sum(1 for v in base['R'] if v >= need) / base['n']
            print(f'   结算档 ≥{b}: 需再升 {need:.1f}°C → VHHH 站点口径 {p:.1f}%')
        print('   ⓘ 这是**机场站**的绝对值；HKO 的 R 比 VHHH 小（网格 R 0.13 vs 0.25），'
              '\n     跨站点外推要打折，尾档尤其 —— 详见 SKILL.md「站点口径 R 的放大比」。')


if __name__ == '__main__':
    main()
