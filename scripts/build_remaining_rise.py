#!/usr/bin/env python3
"""构建「剩余升温」经验分布表  data/remaining_rise_cdf.json

背景（one-touch 视角）
--------------------
结算量是**日内路径的最大值**，不是某个时点的温度 → 这是连续监控的向上触碰，
不是数字期权。因此正确的条件量不是「日最高 − 当前读数」，而是

    R_h = M − RM_h         （剩余升温 = 日最高 − 到 h 时为止的已观测最高）

其中 RM_h 是 running max——「已触碰」是吸收态，日最高一旦观测到就不会被抹掉，
所以 R_h ≥ 0，且它才是这个市场（one-touch）真正需要的不确定性来源。

为什么不建模「M − 预测值」的误差：气候日变化曲线在峰值时点之后没有剩余升水，
预测值会退化成 RM_h，误差分布随之变成单侧（恒 ≥ 0），于是对任何低于预测值的
门槛都只能给出 100% —— 2026-09-10 13:20 实测就踩了这个坑（P(32) 报 99%）。

今天模式（NWP）自己的日变化曲线只以**偏离气候升水的部分**进入：
    δ = ρ̂ − ρ̄_h,   ρ̂ = max(grid[t], t≥h) + L_h − RM_h,   ρ̄_h = E[R_h]
即气候升水已隐含在经验分布里，NWP 只贡献它的增量，避免把点估计当确定性。

旧做法（v0.11 及以前）用的是 E[M − T_h]（无条件、且从**当前读数**起算）配正态，
实测斜率 β = −0.007~−0.22（旧公式隐含 −1.0），误差在 13 时前后反号。

用法
----
    py -3 scripts/build_remaining_rise.py             # 重建表
    py -3 scripts/build_remaining_rise.py --validate  # 附带新旧方法 Brier 对照

输出
----
    data/remaining_rise_cdf.json
      by_hour[h]        : 全月合并（n≈4200/时）
      by_month[m][h]    : 分月（n≈350/时，样本少时回退到 by_hour）
      每个单元 = {"n": int, "q": [[p, value], ...]}，p 为下侧分位概率
"""
import argparse
import json
import math
import os
import statistics as st
import sys
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from hk_edge import OBS_LAT, OBS_LON, DATA
except Exception:                                   # 独立运行时的兜底
    OBS_LAT, OBS_LON = 22.302, 114.173
    DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

ARCHIVE = ("https://archive-api.open-meteo.com/v1/archive"
           "?latitude={lat}&longitude={lon}&start_date={s}&end_date={e}"
           "&hourly=temperature_2m&timezone=Asia/Shanghai")
START, END = "2015-01-01", "2026-09-05"
PROBS = [0.01, 0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50,
         0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.93, 0.95, 0.97, 0.99, 1.0]
OUT = os.path.join(DATA, 'remaining_rise_cdf.json')


def fetch_days(cache=None):
    """返回 {date: {hour: temp}}，只保留 24 小时齐全的日子"""
    raw = None
    if cache and os.path.exists(cache):
        raw = json.load(open(cache))
    if raw is None:
        url = ARCHIVE.format(lat=OBS_LAT, lon=OBS_LON, s=START, e=END)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        raw = json.load(urllib.request.urlopen(req, timeout=300))['hourly']
        if cache:
            json.dump(raw, open(cache, 'w'))
    by = defaultdict(dict)
    for t, v in zip(raw['time'], raw['temperature_2m']):
        if v is None:
            continue
        by[t[:10]][int(t[11:13])] = float(v)
    return {d: v for d, v in by.items() if len(v) == 24}


def quantiles(xs, probs=PROBS):
    xs = sorted(xs)
    n = len(xs)
    if n < 20:
        return None
    out = []
    for p in probs:
        i = min(n - 1, max(0, int(math.ceil(p * n)) - 1))
        out.append([round(p, 4), round(xs[i], 2)])
    return out


def build(days, hours=range(0, 24)):
    """返回 (by_hour, by_month) 两个「剩余升温 R = M − RM_h」的分位表"""
    resid_h = defaultdict(list)
    resid_mh = defaultdict(list)
    for d, v in days.items():
        m = int(d[5:7])
        M = max(v.values())
        run = -99.0
        for hh in range(24):
            run = max(run, v[hh])
            if hh not in hours:
                continue
            r = M - run                     # 剩余升温，恒 ≥ 0
            resid_h[hh].append(r)
            resid_mh[(m, hh)].append(r)

    def cell(xs):
        q = quantiles(xs)
        return None if q is None else {"n": len(xs), "mean": round(sum(xs) / len(xs), 3), "q": q}

    return ({h: c for h in resid_h if (c := cell(resid_h[h]))},
            {m: {h: c for h in hours if (c := cell(resid_mh[(m, h)]))}
             for m in range(1, 13)})


def validate(days, calib_tbl):
    """新旧方法 Brier 对照。奇数年建表、偶数年检验（避免同样本乐观）。

    注意：这里检验的是 δ=0 的情形，即**不用**当日 NWP 日变化曲线。
    实盘会再叠加 δ = ρ̂ − ρ̄_h，只会更好（NWP 日变化优于气候平均）。
    """
    diur_path = os.path.join(DATA, 'diurnal_climatology.json')
    diur = json.load(open(diur_path)) if os.path.exists(diur_path) else {}

    print("\n[Brier 对照 · 偶数年检验，δ=0]  P(日最高 ≥ 已观测最高 + Δ)")
    print("  %-6s %-6s %-14s %-14s %9s" % ("h", "Δ", "旧 正态+守卫", "新 经验CDF", "改进"))
    tot_o = tot_n = tot_c = 0.0
    for hh in (11, 12, 13, 14, 15, 16):
        for dK in (0.5, 1.0, 1.5):
            po, pn = [], []
            for d, v in days.items():
                if int(d[:4]) % 2 == 1:
                    continue                      # 奇数年用于建表
                m = int(d[5:7])
                M, run = max(v.values()), max(v[t] for t in range(hh + 1))
                y = 1.0 if M >= run + dK else 0.0
                # 旧：气候表 E[M−T_h] + 正态 + 过峰守卫（v0.12.5 实际逻辑）
                dm = diur.get(str(m), {}).get(str(hh))
                if dm:
                    g = run - v[hh]
                    if g >= 0.3 and hh >= 14:
                        s = min(min(1.0, max(0.0, (hh - 14) / 2.0)),
                                min(1.0, max(0.0, (g - 0.3) / 1.2)))
                        net = max(0.0, dm['mean'] - g)
                        rm_, rs_ = net * (1 - 0.75 * s), max(0.1, dm['sd'] * (1 - 0.4 * s))
                    else:
                        rm_, rs_ = dm['mean'], dm['sd']
                    peak = max(v[hh] + rm_, run)
                    po.append((1 - norm((run + dK - peak) / rs_), y))
                # 新：R = M − RM_h 的经验分布
                tbl = (calib_tbl.get('by_month', {}).get(str(m), {}).get(str(hh))
                       or calib_tbl.get('by_hour', {}).get(str(hh)))
                if tbl:
                    pn.append((_cdf_upper(tbl, dK), y))
            if not po or not pn:
                continue
            bo = sum((a - b) ** 2 for a, b in po) / len(po)
            bn = sum((a - b) ** 2 for a, b in pn) / len(pn)
            tot_o += bo; tot_n += bn; tot_c += 1
            print("  %-6d %-6.1f %-14.4f %-14.4f %8.1f%%"
                  % (hh, dK, bo, bn, 100 * (bn - bo) / bo))
    if tot_c:
        print("  %-13s %-14.4f %-14.4f %8.1f%%"
              % ("平均", tot_o / tot_c, tot_n / tot_c, 100 * (tot_n - tot_o) / tot_o))


def _cdf_upper(tbl, x):
    q = tbl['q']
    if x <= q[0][1]:
        return 1.0 - q[0][0]
    for i in range(1, len(q)):
        if x <= q[i][1]:
            p0, v0 = q[i - 1]
            p1, v1 = q[i]
            f = 0.0 if v1 == v0 else (x - v0) / (v1 - v0)
            return max(0.0, min(1.0, 1.0 - (p0 + f * (p1 - p0))))
    return 0.0


def norm(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--validate', action='store_true')
    ap.add_argument('--cache', default=os.path.join(DATA, '_era5_hourly_cache.json'))
    a = ap.parse_args()

    days = fetch_days(a.cache)
    print("ERA5 完整日数: %d  (%s ~ %s)" % (len(days), min(days), max(days)))
    by_hour, by_month = build(days)

    n_ok = sum(1 for h in by_hour if by_hour[h]['q'])
    print("分位表: by_hour %d/%d 个时点可用" % (n_ok, len(by_hour)))
    for hh in (11, 13, 15, 16, 18):
        c = by_hour.get(hh)
        if c and c['q']:
            qq = dict(c['q'])
            print("   h=%2d n=%d  mean %.2f  P50 %.2f  P90 %.2f  P99 %.2f  最大 %.2f"
                  % (hh, c['n'], c['mean'], qq.get(0.5, 0), qq.get(0.90, 0),
                     qq.get(0.99, 0), c['q'][-1][1]))

    json.dump({"meta": {"source": "ERA5 hourly via Open-Meteo archive",
                        "lat": OBS_LAT, "lon": OBS_LON,
                        "window": [min(days), max(days)], "days": len(days),
                        "def": "R(h) = M - running_max(h)  (remaining rise, >= 0)",
                        "probs": PROBS},
               "by_hour": by_hour, "by_month": by_month},
              open(OUT, 'w'), indent=1)
    print("\n已写出 %s" % OUT)

    if a.validate:
        validate(days, json.load(open(OUT)))


if __name__ == '__main__':
    main()
