"""站点「雨后次日」条件分布（HKO Daily Extract 月 XML，2015–2026）。

分析窗口：每年 8/25 – 10/5（9 月主体 + 两侧边缘，n=477）。

用途：D-1 或当日给出「前一日雨量 × 当日雨量」条件下的站点日最高档位频率，
作为气候基线的条件化版本，与市场价对比找错配。

用法：py -3 scripts/postrain_prior.py

关键结论（2026-09-14 首测）：
  1. 前日大雨几乎不压制次日峰值 —— 真正的开关是「当日雨量」。
  2. 市场把 33°C 及以上的概率整体搬到 31/32：31 档被高估 +11.1pp、32 档高估 +8.2pp，
     而 30 档被准确定价（+0.2pp）。
  3. 无雨情景下 30 档只有 11.6%，中雨情景跳至 23.3%、大雨情景 17.5% ——
     三情景加权后期望 16.3% ≈ 市场 16.5%。**30 档没有 edge。**
  4. 唯一在所有情景下都正 EV 的腿是 NO31（+11.4% ~ +32.1%）。
详见 reviews/2026-09-15_prediction.md。
"""
import urllib.request, gzip, json, statistics as st, datetime as dt

H = {"User-Agent": "Mozilla/5.0"}
VERBOSE = False
BASE = 'https://www.hko.gov.hk/cis/dailyExtract/dailyExtract_%s.xml'


def num(x):
    try:
        return float(x)
    except Exception:
        return None


def fetch_month(yyyymm):
    for url in (BASE % yyyymm, BASE.replace('www.hko', 'www.weather') % yyyymm):
        try:
            raw = urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=40).read()
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
            d = json.loads(raw.decode('utf-8', 'replace'))
            rows = []
            for blk in d['stn']['data']:
                for r in blk.get('dayData', []):
                    try:
                        day = int(r[0])
                    except Exception:
                        continue
                    tmax = num(r[2])
                    if tmax is None:
                        continue
                    rows.append(dict(
                        date='%s-%s-%02d' % (yyyymm[:4], yyyymm[4:], day),
                        p=num(r[1]), tmax=tmax, tmean=num(r[3]),
                        tmin=num(r[4]), grass=num(r[5]),
                        rh1=num(r[6]), rh2=num(r[7]),
                        rain=(num(r[8]) if num(r[8]) is not None else 0.0),
                    ))
            return rows
        except Exception as e:
            if VERBOSE:
                print('  fetch fail %s: %r' % (yyyymm, e))
            continue
    return []


rows = []
for y in range(2015, 2027):
    for m in (8, 9, 10):
        rows += fetch_month('%d%02d' % (y, m))
print('总行数', len(rows))
by = {}
for r in rows:
    try:
        d = dt.date.fromisoformat(r['date'])
    except Exception:
        continue
    by[d] = r

# 分析窗口：8/25 – 10/5（9 月主体 + 两侧边缘，把 n 放大到 3 倍）
WIN = [(dt.date(y, 8, 25), dt.date(y, 10, 5)) for y in range(2015, 2027)]


def in_win(d):
    return any(a <= d <= b for a, b in WIN)


seq = sorted(by)
recs = []
for d in seq:
    if not in_win(d):
        continue
    prev = by.get(d - dt.timedelta(days=1))
    if prev is None:
        continue
    recs.append(dict(date=d.isoformat(), tmax=by[d]['tmax'], rain=by[d]['rain'],
                     prain=prev['rain'], ptmax=prev['tmax']))
print('可用样本', len(recs))


def dist(sub, label):
    if not sub:
        print('%-42s n=0' % label)
        return
    t = sorted(x['tmax'] for x in sub)
    n = len(t)
    buckets = {}
    for v in t:
        buckets[int(v)] = buckets.get(int(v), 0) + 1
    line = ' '.join('%d:%4.1f%%' % (k, 100.0 * v / n) for k, v in sorted(buckets.items()))
    print('%-42s n=%3d 均值%.2f  中位%.2f  ' % (label, n, st.mean(t), st.median(t)))
    print('    ' + line)


print()
dist(recs, '全样本（8/25–10/5）')
dist([x for x in recs if x['prain'] >= 20], '前一日雨量 >=20mm')
dist([x for x in recs if x['prain'] >= 35], '前一日雨量 >=35mm')
dist([x for x in recs if 20 <= x['prain'] and x['rain'] < 5], '前日>=20mm 且 当日<5mm')
dist([x for x in recs if 20 <= x['prain'] and x['rain'] >= 5], '前日>=20mm 且 当日>=5mm')
dist([x for x in recs if 20 <= x['prain'] and x['rain'] >= 20], '前日>=20mm 且 当日>=20mm')
dist([x for x in recs if x['rain'] < 5], '当日雨量 <5mm')
dist([x for x in recs if 5 <= x['rain'] < 20], '当日雨量 5-20mm')
dist([x for x in recs if x['rain'] >= 20], '当日雨量 >=20mm')

print()
print('--- 明日最匹配：前日大雨 + 当日干 ---')
dist([x for x in recs if x['prain'] >= 20 and x['rain'] < 2], '前日>=20mm 且 当日<2mm')
dist([x for x in recs if x['prain'] >= 35 and x['rain'] < 2], '前日>=35mm 且 当日<2mm')
dist([x for x in recs if x['prain'] >= 35 and x['rain'] < 5], '前日>=35mm 且 当日<5mm')
dist([x for x in recs if x['prain'] >= 20 and 5 <= x['rain'] < 20], '前日>=20mm 且 当日 5-20mm')

print()
print('--- 前一日 >=20mm 的逐例明细（看站点最高当日值）---')
sub = [x for x in recs if x['prain'] >= 20]
for x in sorted(sub, key=lambda z: -z['tmax']):
    print('  %s  当日最高%5.1f  当日雨%6.1f  前日雨%6.1f' % (x['date'], x['tmax'], x['rain'], x['prain']))
