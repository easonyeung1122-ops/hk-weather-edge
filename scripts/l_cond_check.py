#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""条件化检验：给定「11 时 running max」的水平，ERA5 网格日最高的经验分布。

为什么需要
----------
`remaining_rise_cdf.json` 给的是 R = M − running_max(h) 的**无条件**分布
（按月×时）。但 R 与 running_max 的水平本身相关：已经热到 29°C 的日子，
11 时后通常升得更少。今天的 running_max(11) = 29.07 在 9 月属于偏高端，
直接套无条件表会**高估**剩余升温。

同时给出 ERA5 网格与 HKO 站实测日最高的偏差（bias），用于口径换算。

用法
----
    py -3 scripts/l_cond_check.py --rm11 29.07 --cur 28.5
"""
import argparse
import datetime as dt
import gzip
import json
import os
import re
import statistics as st
import sys
import urllib.request

HTTP_H = {"User-Agent": "Mozilla/5.0"}
ARCH = ("https://archive-api.open-meteo.com/v1/archive?latitude=%.3f&longitude=%.3f"
        "&start_date=%s&end_date=%s&hourly=temperature_2m&timezone=Asia%%2FShanghai")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hk_edge as HK  # noqa: E402

HKO_DAILY = 'https://www.hko.gov.hk/cis/dailyExtract/dailyExtract_%s.xml'


def _num(x):
    try:
        return float(x)
    except Exception:
        return None


def hko_month(yyyymm):
    """HKO Daily Extract 月表 → [(date, tmax)]。"""
    for url in (HKO_DAILY % yyyymm, HKO_DAILY.replace('www.hko', 'www.weather') % yyyymm):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=HTTP_H),
                                         timeout=40).read()
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
            d = json.loads(raw.decode('utf-8', 'replace'))
            out = []
            for blk in d['stn']['data']:
                for r in blk.get('dayData', []):
                    try:
                        day = int(r[0])
                    except Exception:
                        continue
                    t = _num(r[2])
                    if t is not None:
                        out.append((f'{yyyymm[:4]}-{yyyymm[4:]}-{day:02d}', t))
            return out
        except Exception:
            continue
    return []


def era5_month(yyyymm):
    y, m = int(yyyymm[:4]), int(yyyymm[4:])
    last = (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)).day
    s, e = f"{yyyymm[:4]}-{yyyymm[4:]}-01", f"{yyyymm[:4]}-{yyyymm[4:]}-{last:02d}"
    raw = urllib.request.urlopen(
        urllib.request.Request(ARCH % (HK.OBS_LAT, HK.OBS_LON, s, e), headers=HTTP_H),
        timeout=60).read()
    hr = json.loads(raw.decode('utf-8'))['hourly']
    days = {}
    for t, v in zip(hr['time'], hr['temperature_2m']):
        if v is None:
            continue
        days.setdefault(t[:10], {})[int(t[11:13])] = float(v)
    return days


def qv(arr, q):
    return arr[min(len(arr) - 1, int(q * (len(arr) - 1)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rm11', type=float, required=True, help='今日 11 时 running max')
    ap.add_argument('--cur', type=float, required=True, help='今日 11 时瞬时值')
    ap.add_argument('--tol', type=float, default=0.75, help='条件窗口半宽（°C）')
    ap.add_argument('--months', default='09')
    a = ap.parse_args()

    pairs, era5_dmax, obs = [], [], []
    for y in range(2015, 2026):
        ym = f'{y}{a.months}'
        try:
            days = era5_month(ym)
        except Exception as e:
            print(f'ERA5 {ym} ERR {e}')
            days = {}
        for day, hh in sorted(days.items()):
            if len(hh) < 20 or 11 not in hh:
                continue
            rm11 = max(hh[k] for k in hh if k <= 11)
            pairs.append((rm11, max(hh.values()) - rm11, max(hh.values())))
            era5_dmax.append(max(hh.values()))
        obs += [t for _, t in hko_month(ym)]

    print(f'ERA5 9 月样本 n = {len(pairs)}｜HKO 站实测 n = {len(obs)}')
    if not pairs or not obs:
        return
    print(f'ERA5 网格日最高：均值 {st.mean(era5_dmax):.2f}  中位 {st.median(era5_dmax):.2f}')
    print(f'HKO 站实测日最高：均值 {st.mean(obs):.2f}  中位 {st.median(obs):.2f}')
    bias = st.mean(obs) - st.mean(era5_dmax)
    print(f'→ 站点 − ERA5 网格 日最高偏差 bias = {bias:+.2f}°C')

    allrm = sorted(rm for rm, _, _ in pairs)
    print('\n9 月 running_max(11) 分位：'
          + '  '.join(f'p{int(q*100)}={qv(allrm, q):.2f}'
                      for q in (0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99, 1.0)))
    print(f'今天 rm11 = {a.rm11:.2f} → 落在 p{100*sum(1 for x in allrm if x < a.rm11)/len(allrm):.0f}')

    lo, hi = a.rm11 - a.tol, a.rm11 + a.tol
    sub = [dm for rm, _, dm in pairs if lo <= rm <= hi]
    print(f'\n【条件】ERA5 running_max(11) ∈ [{lo:.2f}, {hi:.2f}]   n = {len(sub)}')
    if len(sub) < 5:
        print('  样本过少 —— 今天的 11 时水平在 9 月 ERA5 里十分罕见')
        return
    dms = sorted(sub)
    rs = sorted(dm - a.rm11 for dm in dms)
    print(f'  R = 日最高 − rm11：中位 {st.median(rs):.2f}  均值 {st.mean(rs):.2f}  '
          f'p90 {qv(rs,0.9):.2f}  p95 {qv(rs,0.95):.2f}  max {rs[-1]:.2f}')
    print(f'  网格日最高：中位 {st.median(dms):.2f}  p90 {qv(dms,0.9):.2f}  '
          f'p95 {qv(dms,0.95):.2f}  max {dms[-1]:.2f}')
    for t in (30.0, 31.0, 32.0):
        print(f'  ERA5 网格日最高 ≥ {t:.1f}：{sum(1 for v in dms if v >= t)/len(dms):>6.1%}')

    # ⚠ 下面这一步**故意不合并两个口径**（2026-09-15 踩过）：
    # bias = +2.4°C 是「站点日最高 − ERA5 网格日最高」的**气候平均差**，成因是
    # ERA5 把站点的高频峰值平滑掉了（振幅压缩），**不是当天恒定的偏移量**。
    # 拿它去修当天的网格值会得到 32°C+ 的荒谬峰值（今天实测 11 时站点仅 28.8）。
    # 正确用法：R 表只提供「网格内部剩余升温的形状」，绝对水平必须靠
    # 7 模式平均 + model_calib.bias（同源），见 hk_edge / l_integrate 的 L 框架。
    print(f'\n  ⚠ 振幅压缩诊断：ERA5 网格 11 时后剩余升温中位 {st.median(rs):.2f}°C，'
          f'而站点口径（7 模式平均 + bias）隐含 ~2.0°C')
    print(f'     → ERA5 的 R 表**系统性低估**站点剩余升温；'
          f'它在 l_integrate 里只当「形状」用，均值靠 δ 修正对齐。')
    print(f'\n  今天的实际需求（自 {a.cur:.1f}°C 起还需升温）——'
          f'对照 ERA5 网格条件分布（下界，站点真实频率更高）：')
    for tgt in (30.0, 30.6, 31.0, 31.6, 32.0):
        need = tgt - a.cur
        frac = sum(1 for dm in dms if dm - a.rm11 >= need - (a.rm11 - a.cur)) / len(dms)
        print(f'    峰值 {tgt:.1f} → 需 +{need:.1f}°C，ERA5 条件下界频率 {frac:.1%}')


if __name__ == '__main__':
    main()
