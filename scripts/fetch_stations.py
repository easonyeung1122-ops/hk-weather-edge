# -*- coding: utf-8 -*-
"""下载各站日最高温历史 CSV（CLMMAXT），供 analyze.py 做站点偏差对照。

analyze.py 需要 HKO / KP / TKL / SEK / CCH 五个站的 CSV。
主程序 hk_edge.py 只会自动下载 HKO，其余四站用本脚本补齐。

用法：
  py -3 scripts/fetch_stations.py
  py -3 scripts/fetch_stations.py KP TKL        # 只下载指定站

输出：scripts/data/maxt_<STATION>.csv
"""
import os
import sys
import urllib.request

STATIONS = {
    'HKO': '天文台总部站（尖沙咀）— 结算站',
    'KP':  '京士柏（紧邻天文台山，最佳实时代理站）',
    'TKL': '打鼓岭（新闻最常引用）',
    'SEK': '石岗',
    'CCH': '长洲',
}
BASE = ("https://data.weather.gov.hk/weatherAPI/opendata/opendata.php"
        "?dataType=CLMMAXT&station={s}&lang=en")


def fetch(station, data_dir, timeout=120):
    out = os.path.join(data_dir, f'maxt_{station}.csv')
    url = BASE.format(s=station)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        raw = urllib.request.urlopen(req, timeout=timeout).read()
    except Exception as e:
        print(f"[warn] {station}: 下载失败 {e}")
        return False
    if not raw:
        print(f"[warn] {station}: 空响应")
        return False
    with open(out, 'wb') as f:
        f.write(raw)
    print(f"[ok] {station:4s} {STATIONS.get(station, '')} → {out} ({len(raw) / 1024:.0f} KB)")
    return True


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(here, 'data')
    os.makedirs(data_dir, exist_ok=True)

    want = [s.upper() for s in sys.argv[1:]] or list(STATIONS)
    unknown = [s for s in want if s not in STATIONS]
    if unknown:
        raise SystemExit(f"[err] 未知站点 {unknown}，可用: {', '.join(STATIONS)}")

    ok = sum(fetch(s, data_dir) for s in want)
    print(f"\n完成 {ok}/{len(want)}。之后可运行: py -3 scripts/analyze.py")


if __name__ == '__main__':
    main()
