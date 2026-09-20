"""r14_cond_table.py —— ERA5 口径的「条件剩余升温」表（可复现，无需第三方库）

用途
----
回答「rmax(h) 已经到某个水平时，今天还能不能再升 g 度」。
直接从 Open-Meteo archive（ERA5 / ERA5-Land，2015 起）拉逐时序列，按 **rmax@h 的分位**切分，
输出 `P(最终日最高 − rmax@h ≥ g)`。**不需要任何 API key。**

为什么要有这个脚本
------------------
`_vhhh_asos.csv`（Iowa Mesonet 机场站）是**整数分辨率**，无法回答「还差 1.0°C 能不能跨过去」
（实测 P(R≥0.5) = P(R≥1.0) = 27.0%，同一个子集）。ERA5 archive 是 0.1°C 浮点，
可以按任意 g 切分；代价是网格抹平尖峰（尾概率偏薄，读作**下界**）。

输出读法
--------
- 每一行是一个分位阈值 `thr`（= rmax@h 的第 q 分位）下的条件概率。
- 我方需要的是「与今日 rmax(h) 同分位」的那一行 —— 先用当日 rmax(h) 反查它落在历史的哪个分位。
- ERA5 网格尾概率偏薄（9 月 h=13 的 P(R≥0.5) 网格 7.0% vs 机场站 32.8%，4.7 倍），
  所以本表数值应作**下界**用；站点口径可乘经验尾比（≈2–5 倍），但**不得**拿它去乘均值比 k。

用法
----
    py -3 scripts/r14_cond_table.py --hour 14
    py -3 scripts/r14_cond_table.py --hour 14 --lat 22.42 --lon 114.20 --model era5_land
    py -3 scripts/r14_cond_table.py --hour 14 --g 0.4 0.6 1.0
"""
import argparse
import json
import statistics as st
import urllib.request

H = {'User-Agent': 'Mozilla/5.0'}
ARCHIVE = ('https://archive-api.open-meteo.com/v1/archive?latitude=%.3f&longitude=%.3f'
           '&start_date=%s&end_date=%s&hourly=temperature_2m&timezone=Asia%%2FShanghai')


def fetch(lat, lon, start, end, model=None, timeout=90):
    u = ARCHIVE % (lat, lon, start, end)
    if model:
        u += '&models=' + model
    req = urllib.request.Request(u, headers=H)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def daily_rows(payload, hour):
    """按日拆出 (date, rmax@hour, 当日最高, R, 日较差)"""
    times = payload['hourly']['time']
    vals = payload['hourly']['temperature_2m']
    days = {}
    for ti, vi in zip(times, vals):
        if vi is None:
            continue
        days.setdefault(ti[:10], {})[int(ti[11:13])] = vi
    rows = []
    for day, vs in days.items():
        if len(vs) < 24:
            continue
        rm = max(x for hh, x in vs.items() if hh <= hour)
        tmax = max(vs.values())
        tmin = min(vs.values())
        rows.append((day, rm, tmax, round(tmax - rm, 3), round(tmax - tmin, 3)))
    return sorted(rows)


def main():
    ap = argparse.ArgumentParser(description='ERA5 条件剩余升温表（按 rmax@hour 分位切分）')
    ap.add_argument('--hour', type=int, default=14, help='分界时刻 HKT（默认 14）')
    ap.add_argument('--lat', type=float, default=22.25)
    ap.add_argument('--lon', type=float, default=114.17)
    ap.add_argument('--model', default='era5_seamless',
                    help='era5_seamless / era5_land / 空字符串')
    ap.add_argument('--start', default='2015-09-01')
    ap.add_argument('--end', default='2025-09-30')
    ap.add_argument('--g', type=float, nargs='+', default=[0.4, 0.6, 1.0, 1.5])
    ap.add_argument('--quantiles', type=float, nargs='+',
                    default=[0.50, 0.75, 0.80, 0.85, 0.90, 0.95, 0.98, 0.99])
    a = ap.parse_args()

    payload = fetch(a.lat, a.lon, a.start, a.end, a.model or None)
    rows = daily_rows(payload, a.hour)
    rm = sorted(r[1] for r in rows)
    print('=' * 78)
    print('ERA5 条件剩余升温表 | lat=%.3f lon=%.3f model=%s | h=%d | %s~%s'
          % (a.lat, a.lon, a.model or 'default', a.hour, a.start, a.end))
    print('=' * 78)
    print('n(days)=%d  mean rmax@%d=%.2f  mean daily max=%.2f  mean daily range=%.2f'
          % (len(rows), a.hour, st.mean(r[1] for r in rows),
             st.mean(r[2] for r in rows), st.mean(r[4] for r in rows)))
    print()
    head = '   q     thr   n  |' + ''.join('  P(R>=%.1f)' % g for g in a.g)
    print(head)
    print('  ' + '-' * (len(head) + 4))
    for q in a.quantiles:
        thr = rm[min(int(q * len(rm)), len(rm) - 1)]
        sel = [r for r in rows if r[1] >= thr]
        line = '  %.2f  %5.1f  %3d  |' % (q, thr, len(sel))
        for g in a.g:
            c = sum(1 for r in sel if r[3] >= g)
            line += '     %5.1f%%' % (100 * c / len(sel))
        print(line)
    print()
    print('⚠ 读法：ERA5 网格抹平日内尖峰（9 月 h=13 的 P(R>=0.5)：网格 7.0% vs 机场站 32.8%，4.7 倍）')
    print('   -> 上表数值是**下界**；乘「经验尾比 2~5 倍」，**不要**乘均值比 k。')
    print('⚠ 想要站点自身口径，只能等 obs_1min_archive.json 攒够天数，或用 watch.log 的 R 阶梯。')


if __name__ == '__main__':
    main()
