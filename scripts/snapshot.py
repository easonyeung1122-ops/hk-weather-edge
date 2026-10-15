#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""snapshot.py —— **一次调用**拿到出结论所需的全部实况/ 盘口 / 分层证据。

为什么要有这个脚本（v0.25.0，2026-10-15）
---------------------------------------------
2026-10-15 一次「更新预测」消耗了 25 次工具调用、约 12 分钟，其中约 20 次是
**同一批数据的反复取数与格式探索**：

| 重复动作 | 次数 | 单��耗时 |
|---|---|---|
| 官方 `text_readings_e.htm` Max Since Midnight | 4 | 0.6s |
| 分区 CSV 站温 + 全港最热 | 5 | 0.8s |
| CLOB 订单簿（单书 + 批量交叉验证） | 6 | 1.4s |
| 遍历 `obs_1min_archive` 算分层 | 4 | 1.1s |
| 引擎 `--watch`（拉 NWP 网格 + 集合 + fnd） | 3 | 25s |
| 结构探索（json 键名 / 列序 / token 顺序） | ~8 | — |

本脚本把这些合成**一次调用**（并行，实测 2~4s），并把口径纪律直接写进代码：
- **官方 rmax 与站温分列打印**，不混用一个数（硬规则：官方值定档，CSV 只看趋势）；
- **token 顺序当场标定**：用「已锁定档」（YES bid > 0.95）验证 token[0]=YES，
  标定失败直接报错退出 —— 2026-10-15曾因猜错token 顺序把 NO30 读成 YES30；
- **阶梯 VWAP 按可成交量出**，不拿顶档报价当执行价（硬规则 28）；
- **分层证据只读 `strata.csv`**（由 `build_strata.py` 预落），不再现场遍历22 天；
- **峰已过 / 回撤 / 雨量 / 警告** 全部显式打印，避免漏项（10-14 漏收复速度的教训）。

它**不做**的事：不写 `decision_log.csv`（那是 `hk_edge.py` 的职责，纪律 33 窗口内禁静默）、
不代替 `--watch` 归档、不给公允概率（引擎口径仍走 `hk_edge.py`）。
本脚本是**证据层聚合**，输出给人看；公允概率与 EV 仍按 SKILL.md 流程在引擎侧算。

用法
----
    py -3 scripts/snapshot.py                    # 今天，单次采样
    py -3 scripts/snapshot.py --samples 3        # 盘口连采 3 次（间隔 20s，硬规则 28 要求）
    py -3 scripts/snapshot.py --date 2026-10-15 --shares 130   # 带退出腿的 ladder VWAP
    py -3 scripts/snapshot.py --json             # 机器可读，便于二次加工

依赖：仅标准库。
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
TZ = dt.timezone(dt.timedelta(hours=8))
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

TEXT_READINGS = 'https://www.hko.gov.hk/textonly/v2/forecast/text_readings_e.htm'
OBS_CSV = ('https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/'
           'latest_1min_temperature.csv')
RHRREAD = ('https://data.weather.gov.hk/weatherAPI/opendata/weather.php'
           '?dataType=rhrread&lang=en')
WARNSUM = ('https://data.weather.gov.hk/weatherAPI/opendata/weather.php'
           '?dataType=warnsum&lang=en')
GAMMA = 'https://gamma-api.polymarket.com/events?slug=%s'
CLOB_BOOK = 'https://clob.polymarket.com/book?token_id=%s'
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]


def _get(url, timeout=30, retries=2):
    """带重试的 GET。

    ⚠ **CLOB 对突发连发返回 HTTP 400**（10-15 实测：同一 URL 单独请求成功，
    在连续快速拉多个 book 时 400）。这不是 token 或编码问题 —— 是限流。
    → 指数退避重试；仍失败则抛出，由调用方决定是否降级。
    """
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=timeout).read().decode(
                'utf-8', 'ignore')
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(0.8 * (i + 1))
    raise last


def _jget(url, timeout=30):
    return json.loads(_get(url, timeout))


# ------------------------------------------------------------------ 官方实况
def official_max():
    """官方 `text_readings_e.htm` 的 HK Observatory 行：Max/Min Since Midnight。

    **这是唯一合法定档源**（SKILL §1.2）。静默失败返回 None，绝不打断主流程。
    镜像 `wxinfo/ts/` 版式不同、站名写全称 → 正则必须匹配简写 `HK Observatory`。
    """
    try:
        txt = _get(TEXT_READINGS, 20)
    except Exception:
        return None
    try:
        flat = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', txt))
        m = re.search(r'HK Observatory\s+([\d.]+)\s+(?:[\d.]+|N/A)\s+'
                      r'([\d.]+)\s*/\s*([\d.]+)', flat)
        if not m:
            return None
        tm = re.search(r'Latest readings recorded at (\d{1,2}:\d{2})', flat)
        return {'hko': float(m.group(1)), 'max': float(m.group(2)),
                'min': float(m.group(3)), 'time': tm.group(1) if tm else None}
    except Exception:
        return None


def station_csv():
    """分区 CSV（0.1°C / 10 分钟）：结算站读数 + 全港各站。静默失败返回 None。"""
    try:
        raw = _get(OBS_CSV, 20)
    except Exception:
        return None
    rows, stamp = {}, None
    for ln in raw.strip().splitlines()[1:]:
        p = [c.strip() for c in ln.split(',')]
        if len(p) < 3:
            continue
        stamp = stamp or p[0]
        try:
            rows[p[1]] = float(p[2])
        except ValueError:
            continue
    if not rows or not stamp or len(stamp) < 12:
        return None
    hko = next((rows[n] for n in ('HK Observatory', 'Hong Kong Observatory')
                 if n in rows), None)
    return {'ts': stamp, 'hko': hko, 'all': rows}


def rhrread():
    try:
        d = _jget(RHRREAD, 25)
    except Exception:
        return None
    rain = [(x['place'], x.get('max')) for x in (d.get('rainfall', {}).get('data') or [])
            if x.get('place') == 'Hong Kong Observatory' or x.get('place') == 'Tsim Sha Tsui']
    return {'updateTime': d.get('updateTime'),
            'icon': d.get('icon'), 'uvindex': d.get('uvindex'),
            'warningMessage': d.get('warningMessage'),
            'rain': rain}


def warnings():
    """生效中的警告（纪律 17：每轮必须显式打印，不能靠记得去查）。"""
    try:
        d = _jget(WARNSUM, 20)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    out = []
    for code, v in d.items():
        if isinstance(v, dict):
            out.append({'code': code, 'name': v.get('name', code),
                        'issue': v.get('issueTime'), 'update': v.get('updateTime')})
    out.sort(key=lambda x: x['code'])
    return out


# ------------------------------------------------------------------ 今日轨迹
def today_track(date_iso):
    """今日 0.1°C 轨迹：`obs_1min_log.json`（10 分钟轮询，唯一合法来源）。"""
    p = os.path.join(DATA, 'obs_1min_log.json')
    if not os.path.exists(p):
        return []
    try:
        d = json.load(open(p, encoding='utf-8'))
    except Exception:
        return []
    if d.get('date') != date_iso:
        return []
    pts = []
    for pt in d.get('points', []):
        t = pt.get('t')
        v = pt.get('hko')
        if isinstance(t, str) and len(t) >= 16 and v is not None:
            if t[:10] != date_iso:          # 读端同日渐层（PENDING §K 第 5 条）
                continue
            pts.append((t[11:16], float(v)))
    pts.sort()
    return pts


# ------------------------------------------------------------------ 分层证据
def strata():
    p = os.path.join(DATA, 'strata.csv')
    if not os.path.exists(p):
        return []
    with open(p, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _f(row, col):
    try:
        return float(row.get(col) or '')
    except (ValueError, TypeError):
        return None


def stratum_evidence(rows, rmax, hour=1340, band=0.5, pullback=None):
    """给定今日 rmax 与时点，返回同类日的官方终值分布（**官方口径**）。

    `hour` 是 `strata.csv` 的列键后缀（默认 1340 = 13:40，午后「是否已过峰」的分水岭；
    1100 = 11:00，执行窗口重心）。band 是 rmax 匹配带宽（默认 ±0.5°C）。
    回撤条件化另给 —— Δ 与回撤是两个独立轴，必须同时看。
    """
    key = 'rmax_%s' % hour
    sub = [r for r in rows if _f(r, 'official_max') is not None
           and _f(r, key) is not None
           and abs(_f(r, key) - rmax) <= band]
    if pullback is not None:
        sub2 = [r for r in sub
                if (_f(r, 'pullback_%s' % hour) or 0.0) >= pullback]
        if sub2:
            sub = sub2
    peaks = sorted(_f(r, 'official_max') for r in sub)
    ds = [_f(r, 'official_max') - _f(r, key) for r in sub]
    return {'n': len(sub), 'key': key, 'band': band,
            'dates': [r['date'] for r in sub],
            'peaks': peaks, 'delta_mean': (sum(ds) / len(ds)) if ds else None,
            'p_ge_030': (sum(1 for x in ds if x >= 0.3) / len(ds)) if ds else None}


# ------------------------------------------------------------------ 盘口
def event_slug(date_iso):
    y, m, d = date_iso.split('-')
    return 'highest-temperature-in-hong-kong-on-%s-%d-%s' % (
        MONTHS[int(m) - 1], int(d), y)


def _book(tid):
    b = _jget(CLOB_BOOK + urllib.parse.quote(str(tid)), 25)
    return _levels(b)


def _levels(b):
    bids = sorted(((float(x['price']), float(x.get('size', 0)))
                   for x in (b.get('bids') or [])), key=lambda x: -x[0])
    asks = sorted(((float(x['price']), float(x.get('size', 0)))
                   for x in (b.get('asks') or [])), key=lambda x: x[0])
    return bids, asks


def books_batch(tids, timeout=30):
    """CLOB 批量接口：一次 POST 拿全部订单簿。

    2026-10-15 实测：**逐个单书连发会被 CLOB 返回 HTTP 400**（同一 URL 单独请求成功，
    先访问 Gamma 再连发必失败 → 是连接/速率状态耦合，不是 token 或编码问题）。
    批量接口一次拿全22 个，是官方推荐路径，也是 `market_prices.py` 一直在用的路径。
    失败返回 {}，调用方回退到逐个（并显式告警，因为回退路径本身可能被限流）。
    """
    payload = [{'token_id': str(t)} for t in tids]
    req = urllib.request.Request(
        'https://clob.polymarket.com/books',
        data=json.dumps(payload).encode(),
        headers={'User-Agent': UA['User-Agent'], 'Content-Type': 'application/json'})
    raw = json.loads(urllib.request.urlopen(req, timeout=timeout).read()
                     .decode('utf-8', 'ignore'))
    out = {}
    for i, b in enumerate(raw or []):
        key = str(b.get('asset_id') or (tids[i] if i < len(tids) else ''))
        out[key] = _levels(b)
    return out


def books(date_iso):
    """取全档订单簿并**当场标定token 顺序**。

    2026-10-15 实测教训：曾假设 token[0]=NO，把NO30 读成 YES30，结论反向。
    现在用「已锁定档」自标定 —— 官方 rmax 已达29.7 时，29 档的 YES 必然≈1
    （结算 = floor(29.7) = 29 → 29 档 YES 赢）。找到这样的档就能定序。
    标定失败返回 `calibrated=False`，调用方必须拒绝使用价格。
    """
    try:
        ev = _jget(GAMMA % event_slug(date_iso), 25)
    except Exception as e:
        return {'error': '事件拉取失败: %s' % e}
    if not ev:
        return {'error': '找不到事件 %s' % event_slug(date_iso)}
    e = ev[0]
    out = {'title': e.get('title'), 'volume24h': e.get('volume24hr'),
           'closed': e.get('closed'), 'buckets': [], 'calibrated': False}
    for mk in e.get('markets', []):
        t = (mk.get('groupItemTitle') or '').strip()
        m = re.match(r'^(\d+)', t)
        if not m:
            continue
        try:
            toks = json.loads(mk['clobTokenIds'])
        except Exception:
            continue
        if len(toks) < 2:
            continue
        out['buckets'].append({'level': int(m.group(1)), 'label': t, 'tokens': toks})
    out['buckets'].sort(key=lambda x: x['level'])
    return out


def ladder(levels, shares):
    """按可成交量算 VWAP。返回 (VWAP, 累计份数, 触及的档位数)。"""
    cum, cost = 0.0, 0.0
    for p, s in levels:
        take = min(s, shares - cum)
        if take <= 0:
            break
        cost += take * p
        cum += take
    return (cost / cum if cum else None), cum, len(levels)


def sample_books(ev, owner, shares=0, samples=1, gap=20):
    """逐档采样；`shares>0` 时给出卖出（命中 bid）的 ladder VWAP。"""
    runs = []
    for i in range(max(1, samples)):
        rec = {'at': dt.datetime.now(TZ).strftime('%H:%M:%S'), 'rows': []}
        try:
            books, _ = fetch_all_books(ev)
        except Exception as e:
            rec['error'] = '批量订单簿失败: %s' % e
            runs.append(rec)
            break
        for b in ev.get('buckets', []):
            y = books.get(b['tokens'][0])
            n = books.get(b['tokens'][1])
            if not y or not n:
                continue
            by, ay = y
            bn, an = n
            row = {'level': b['level'],
                   'yes_bid': (round(by[0][0], 3), round(by[0][1])) if by else None,
                   'yes_ask': (round(ay[0][0], 3), round(ay[0][1])) if ay else None,
                   'no_bid': (round(bn[0][0], 3), round(bn[0][1])) if bn else None,
                   'no_ask': (round(an[0][0], 3), round(an[0][1])) if an else None,
                   'yes_bid_depth': round(sum(s for _, s in by[:5]), 0),
                   'no_bid_depth': round(sum(s for _, s in bn[:5]), 0)}
            if shares:
                row['no_exit_vwap'] = (lambda t: round(t[0], 4) if t[0] else None)(
                    ladder(bn, shares))
                row['yes_exit_vwap'] = (lambda t: round(t[0], 4) if t[0] else None)(
                    ladder(by, shares))
            rec['rows'].append(row)
        runs.append(rec)
        if i < samples - 1:
            time.sleep(gap)
    return runs


def fetch_all_books(ev):
    """一次批量拿回全档订单簿 → {token_id: (bids, asks)}。"""
    tids, owner = [], {}
    for b in ev.get('buckets', []):
        for i, tid in enumerate(b['tokens']):
            tids.append(tid)
            owner[tid] = (b['level'], i)
    return books_batch(tids), owner


def _tok_px(entry):
    bids, asks = entry
    if bids:
        return bids[0][0]
    return asks[0][0] if asks else None


def calibrate(books, owner, official_rmax):
    """判定 token[0] 是 YES 还是 NO。返回 (True, 'YES', 锚点名) 等。

    2026-10-15 实测教训：曾假设 token[0]=NO，把 NO30 读成 YES30，结论完全反向。
    本函数**不假设、只标定**，两种锚点任一成立即返回：

    锚点 A「已锁定档」：`floor(官方 rmax)` 那一档的 token[0] 报价≈1。
      仅当该档已确定锁死时可用 —— 10-15 实测官方 rmax 29.7 距 30.0 界只差 0.3，
      29 档 YES bid 只有 0.90（未锁），此锚点**失效**，故必须有锚点 B。

    锚点 B「已出局档」：明显低于 rmax 的档（如 rmax 29.7 时的 28 档，YES 早输），
      token[0] 报价必然≈0。10-15 实测：28 档 token[0] bid 为空、ask 0.001。

    两锚点都失效时返回False → 调用方必须拒绝使用价格（宁可不报，不可报错方向）。
    """
    if not owner:
        return False, None, None
    probe = []
    if official_rmax is not None:
        settled = int(math.floor(official_rmax))
        probe.append(('已锁定档 %d' % settled, settled, 0.95))
        lv = sorted(l for l, _ in owner.values() if l <= settled - 1)
        if lv:
            probe.append(('已出局档 %d' % lv[-1], lv[-1], 0.05))
    for lab, level, thr in probe:
        for tid, (lvl, i) in owner.items():
            if lvl != level or i != 0:
                continue
            ent = books.get(tid)
            if not ent:
                continue
            px = _tok_px(ent)
            if px is None:
                continue
            if (thr > 0.5 and px > thr) or (thr < 0.5 and px < thr):
                return True, 'YES', lab
    return False, None, None


# ------------------------------------------------------------------ 报告
def _fmt_track(pts, limit=14):
    if not pts:
        return '（今日 obs_1min_log 无点：可能尚未开轮询，或已跨天归档）'
    show = pts[-limit:]
    pre = '… ' if len(pts) > limit else ''
    return pre + ' → '.join('%s %.1f' % (t, v) for t, v in show)


def _fmt_pullback(pts):
    """峰已过判定要同时看三列（10-15 记忆 12d）：峰值 / 站温 / 趋势。"""
    if len(pts) < 3:
        return None
    peak_t, peak_v = max(pts, key=lambda x: x[1])
    last_t, last_v = pts[-1]
    trend = last_v - pts[-3][1]
    return {'peak': (peak_t, peak_v), 'last': (last_t, last_v),
            'pullback': round(peak_v - last_v, 2), 'trend3': round(trend, 2),
            'recovered': last_v >= peak_v - 1e-9}


def render(res, shares=0):
    L = []
    now = res['now']
    L.append('=' * 74)
    L.append('香港气温市场快照  采样 %s  目标日 %s' % (now, res['date']))
    L.append('=' * 74)

    # ---- 实况 ----
    L.append('\n【1】实况（官方定档源 vs 分区 CSV 趋势源，分列不混）')
    om = res.get('official_max')
    if om:
        L.append('  官方 Max Since Midnight : %.1f°C  (记录 %s)   最低 %.1f'
                 % (om['max'], om.get('time') or '?', om['min']))
    else:
        L.append('  ⚠ 官方 Max Since Midnight 取不到（页面结构变化或网络异常）'
                 '→ 不得改用 CSV 值定档')
    csvd = res.get('station_csv')
    if csvd and csvd.get('hko') is not None:
        lag = ''
        if csvd['ts'] and now:
            try:
                m = (dt.datetime.strptime(now, '%H:%M:%S')
                     - dt.datetime.strptime(csvd['ts'][8:12] + ':' + csvd['ts'][12:14],
                                            '%H:%M'))
                lag = '（CSV 时间戳 %s，滞后约 %d 分钟）' % (csvd['ts'][8:12] + ':'
                                                           + csvd['ts'][12:14],
                                                           int(m.total_seconds() // 60))
            except Exception:
                lag = '（CSV 时间戳 %s）' % csvd['ts']
        L.append('  分区 CSV 站温(0.1°C)   : %.1f°C  %s' % (csvd['hko'], lag))
        others = {k: v for k, v in csvd['all'].items()
                  if k not in ('HK Observatory', 'Hong Kong Observatory')}
        hot = sorted(others.items(), key=lambda x: -x[1])[:5]
        L.append('  全港最热 5站           : ' +
                 ', '.join('%s %.1f' % (k, v) for k, v in hot))
        if csvd['hko'] is not None and hot:
            L.append('  城乡温差(最热站−天文台): %+.1f°C' % (hot[0][1] - csvd['hko']))
    else:
        L.append('  ⚠ 分区 CSV 取不到 → 站温趋势不可判（不得用 rhrread 整数位替代）')

    tr = res.get('track')
    L.append('\n【2】今日轨迹（0.1°C，唯一合法来源 obs_1min_log）')
    L.append('  %s' % _fmt_track(tr))
    pb = _fmt_pullback(tr or [])
    if pb:
        verdict = []
        verdict.append('峰 %.1f@%s' % (pb['peak'][1], pb['peak'][0]))
        verdict.append('现 %.1f@%s' % (pb['last'][1], pb['last'][0]))
        verdict.append('回撤 %.1f' % pb['pullback'])
        verdict.append('近 20min 趋势 %+.1f' % pb['trend3'])
        if pb['recovered']:
            verdict.append('已收复至前高')
        L.append('  峰况判定（三列并看，硬规则：回撤≠已过峰）: ' + '，'.join(verdict))
        if pb['recovered'] and pb['trend3'] > 0:
            L.append('  ⚠ 已收复且趋势向上 → 「已过峰」不成立，禁止据此判锁档'
                     '（10-14 11:00 回撤 0.8 → 12:40 创新高跨档）')

    # ---- 雨量 / 警告 ----
    L.append('\n【3】降雨与警告（独立于 NWP 的一类信息）')
    rr = res.get('rhrread')
    if rr:
        L.append('  rhrread %s  icon=%s  uv=%s'
                 % (rr.get('updateTime') or '?', rr.get('icon'), rr.get('uvindex')))
        rain = rr.get('rain') or []
        L.append('  天文台午夜起雨量: ' + ('; '.join('%s %s' % (p, v) for p, v in rain)
                                          if rain else '无记录'))
        if str(rr.get('icon')) in ('50', '51', '52', '53', '54', '60', '63'):
            L.append('  ⚠ 有雨迹象 → 升温受限（10-14 17分钟 1mm→35mm 的教训：'
                     '单次读数只证明「至今没下」）')
    ws = res.get('warnings')
    L.append('  生效警告: ' + (', '.join('%s(%s)' % (w['code'], w['name']) for w in ws)
                              if ws else '无'))

    # ---- 分层证据 ----
    L.append('\n【4】同类日分层证据（官方口径，读 strata.csv 预落表）')
    ev1 = res.get('strata_evidence')
    if ev1:
        for lab, e in (('±0.5°C 带宽', ev1),):
            if not e['n']:
                L.append('  rmax@%s %s: 无同类日（n=0）→ 不得据此给经验频率'
                         % (res['hour'], lab))
                continue
            L.append('  rmax@%s %s: n=%d  官方终值 %s'
                     % (res['hour'], lab, e['n'],
                        ' '.join('%.1f' % p for p in e['peaks'])))
            L.append('       Δ(终值−r13) mean %+.2f   P(≥+0.3)=%.0f%%'
                     % (e['delta_mean'], 100 * e['p_ge_030']))
            L.append('       同类日: %s' % ' '.join(d[5:] for d in e['dates']))
    else:
        L.append('  ⚠ 无分层表（先跑 build_strata.py）；不得改用记忆里的数字')

    # ---- 盘口 ----
    L.append('\n【5】盘口（CLOB 真实挂单，顶档 size 才是可成交量）')
    bk = res.get('books') or {}
    if bk.get('error'):
        L.append('  ⚠ %s' % bk['error'])
    else:
        L.append('  事件: %s  24h量 %.0f  closed=%s'
                 % (bk.get('title'), bk.get('volume24h') or 0, bk.get('closed')))
        cal = res.get('calibration')
        L.append('  token 顺序标定: %s' % (
            'token[0]=YES（%s 锚点自标定通过）' % cal[2] if cal and cal[0]
            else '⚠ 标定失败 → 价格不可用（宁可不报，不可报错方向）'))
        for run in (res.get('book_runs') or []):
            L.append('  --- 采样 %s ---' % run['at'])
            L.append('  %-5s %-16s %-16s %-16s %8s %8s' %
                     ('档', 'YES bid/ask', 'NO bid/ask', 'bid深度(5档)',
                      'bidVWAP', 'NO卖VWAP'))
            for r in run['rows']:
                yb = '%.3f x%.0f' % r['yes_bid'] if r['yes_bid'] else '—'
                ya = '%.3f x%.0f' % r['yes_ask'] if r['yes_ask'] else '—'
                nb = '%.3f x%.0f' % r['no_bid'] if r['no_bid'] else '—'
                na = '%.3f x%.0f' % r['no_ask'] if r['no_ask'] else '—'
                L.append('  %-5d %-16s %-16s %-16s %8s %8s' %
                         (r['level'], '%s / %s' % (yb, ya), '%s / %s' % (nb, na),
                          '%.0f/%.0f' % (r['yes_bid_depth'], r['no_bid_depth']),
                          ('%.4f' % r['yes_exit_vwap']) if r.get('yes_exit_vwap') else '—',
                          ('%.4f' % r['no_exit_vwap']) if r.get('no_exit_vwap') else '—'))
    L.append('\n注：顶档 size 常只有 5~30 份 → 执行价必须用 ladder VWAP，'
             '不得用顶档报价（硬规则 28）。')
    L.append('=' * 74)
    return '\n'.join(L)


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser(description='一次拿到实况/轨迹/分层/盘口')
    ap.add_argument('--date', help='目标日 YYYY-MM-DD（默认今天）')
    ap.add_argument('--samples', type=int, default=1, help='盘口采样次数（≥3 满足硬规则 28）')
    ap.add_argument('--gap', type=int, default=20, help='采样间隔秒')
    ap.add_argument('--shares', type=int, default=0, help='给出该份数的卖出 ladder VWAP')
    ap.add_argument('--hour', default='1340',
                    help='分层表时点键后缀（默认 1340=13:40；1100=11:00）')
    ap.add_argument('--band', type=float, default=0.5, help='rmax 匹配带宽°C')
    ap.add_argument('--pullback', type=float, default=None,
                    help='只取回撤≥该值的同类日（条件化第二轴）')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    a = ap.parse_args()

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    now = dt.datetime.now(TZ)
    date_iso = a.date or now.date().isoformat()

    with ThreadPoolExecutor(max_workers=5) as ex:
        f_off = ex.submit(official_max)
        f_csv = ex.submit(station_csv)
        f_rhr = ex.submit(rhrread)
        f_wrn = ex.submit(warnings)
        f_bks = ex.submit(books, date_iso)
        om, sc, rr, wn, ev = (f_off.result(), f_csv.result(), f_rhr.result(),
                              f_wrn.result(), f_bks.result())

    rmax = om['max'] if om else None
    track = today_track(date_iso)
    if rmax is None and track:
        rmax = max(v for _, v in track)      # 仅用于标定兜底，不用于定档

    cal, owner = (False, None, None), {}
    if ev.get('buckets'):
        try:
            bks, owner = fetch_all_books(ev)
            cal = calibrate(bks, owner, rmax)
        except Exception as e:
            cal = (False, None, None)
            print('[warn] 批量订单簿失败，盘口不可用: %s' % e, file=sys.stderr)
    res = {'now': now.strftime('%H:%M:%S'), 'date': date_iso,
           'official_max': om, 'station_csv': sc, 'rhrread': rr,
           'warnings': wn, 'track': track, 'books': ev, 'calibration': cal,
           'official_rmax': rmax, 'hour': a.hour}
    rows = strata()
    if rows and rmax:
        res['strata_evidence'] = stratum_evidence(rows, rmax, a.hour, a.band,
                                                  a.pullback)
    if ev.get('buckets') and cal and cal[0]:
        res['book_runs'] = sample_books(ev, owner, a.shares, a.samples, a.gap)

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    else:
        print(render(res, a.shares))


if __name__ == '__main__':
    main()