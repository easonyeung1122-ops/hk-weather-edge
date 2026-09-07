# -*- coding: utf-8 -*-
"""生成「某时刻 → 当日峰值」的气候升温幅度表（ERA5 网格），用于日内实况外推。"""
import json, urllib.request, statistics as st, sys

def g(u):
    r = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
    return json.load(urllib.request.urlopen(r, timeout=120))

tab = {}
for m in range(1, 13):
    rises = {h: [] for h in range(24)}
    for yr in range(2020, 2026):
        u = (f"https://archive-api.open-meteo.com/v1/archive?latitude=22.302&longitude=114.173"
             f"&start_date={yr}-{m:02d}-01&end_date={yr}-{m:02d}-12&hourly=temperature_2m"
             f"&timezone=Asia%2FShanghai")
        try:
            h = g(u)['hourly']
        except Exception as e:
            print(m, yr, 'skip', e, file=sys.stderr); continue
        days = {}
        for t, v in zip(h['time'], h['temperature_2m']):
            if v is None: continue
            days.setdefault(t[:10], {})[int(t[11:13])] = v
        for d, hv in days.items():
            if len(hv) < 20: continue
            mx = max(hv.values())
            for hh, v in hv.items():
                rises[hh].append(mx - v)
    tab[m] = {h: {'mean': round(st.mean(rises[h]), 3),
                  'sd': round(st.pstdev(rises[h]), 3),
                  'n': len(rises[h])} for h in range(24) if rises[h]}
    print(f"  {m}月 OK (n≈{max((v['n'] for v in tab[m].values()), default=0)})")

json.dump(tab, open('data/diurnal_climatology.json', 'w'))
print("\n已写入 data/diurnal_climatology.json")
