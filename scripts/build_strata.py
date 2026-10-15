#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_strata.py —— 把 `obs_1min_archive.json` 的逐日轨迹压成一张分层表 `data/strata.csv`。

为什么要有这个脚本（v0.25.0，2026-10-15）
-----------------------------------------
2026-10-15 一轮预测消耗了 68 次工具调用，其中约 12 次花在**同一件事**上：
每次都重新遍历 `obs_1min_archive.json` 的 22 天 × 每天 25~40 个点，
现场算 `rmax@h`、`pullback@h`、`Δ = 官方终值 − rmax@h`，只为回答一个问题：

    「今天 rmax@13:40 = 29.7，回撤 0.3，同类日最后落到几档？」

这张表**每天只变一行**，却在每轮决策里被读 3~5 次 → 应该预先算好落盘。
本脚本把「原始轨迹」压成「可直接查的行」，把 O(22天×40点) 的现场遍历
变成一次 `csv.DictReader` 过滤。数值口径与现场算**逐位一致**（同一函数 `day_row()`）。

它同时是 PENDING §K 读端污染闸门的落点
--------------------------------------
`obs_1min_archive.json` 已确认被混入外来点（2026-05-29 的 33.4 污染 10-13 段，
并经跨天轮转固化进 `intraday_archive.json`）。修法第 1、2 条（采集端丢弃 + 读端
同日渐层）在 `hk_edge.py` 里改；**第 5 条「读端按 `t[:10] == 键名` 过滤 + 拒写 <3 点键」
在本脚本实现** —— 因为所有分层判据都经过这张表，读端干净就等于判据干净。

分层表读法
----------
`strata.csv` 是**长表**（一行一日），不是预聚合的分组均值 —— 这样任何新的分层
（换 rmax@h、换回撤阈值、换 Δ 窗口）都只要改查询，不要重建。

三个常用切面（`--summary` 直接打印）：

| 切面 | 判据 | 已知结论（截至 10-15） |
|---|---|---|
| `rmax_1100` 分层 | 11:00 的 running max 落在哪个带 | `[28.5,29.5]` 5 天终值 30.8~31.2 → 0/5 落 29 档 |
| `rmax_1340` 分层 | 13:40 的 running max + Δ | 「终值 − r13」mean +0.36 sd 0.38，P(≥+0.3)=4/14≈29% |
| `pullback_1340` | 13:40 是否回撤 ≥0.5 | 回撤层 n=4 → P(≥+0.3)=1/4；非回撤层显著更高 |

⚠ **`official_max` 缺失 = 官方 Daily Extract 还没出那天**，不是 0。
Daily Extract 的滞后节奏**不固定**（10-12 那次一次推进 3 天，10-14 只推进 1 天），
所以缺失是常态。查询时必须 `float(row['official_max'])` 前判空，
**不得用 0 或 rmax 顶替** —— 那会把「还没出」误读成「终值等于当时峰值」。

用法
----
    py -3 scripts/build_strata.py                # 重建（含官方终值补齐），打印摘要
    py -3 scripts/build_strata.py --summary      # 只打印三层切面（不写文件）
    py -3 scripts/build_strata.py --offline      # 跳过 Daily Extract 网络请求
    py -3 scripts/build_strata.py --date 2026-10-15   # 只重算某天

依赖：仅标准库。
"""
import argparse
import csv
import datetime as dt
import json
import os
import statistics as stats
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
ARCHIVE = os.path.join(DATA, 'obs_1min_archive.json')
STRATA_CSV = os.path.join(DATA, 'strata.csv')
DECACHE = os.path.join(DATA, 'daily_extract_cache.json')

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
# 名为 .xml 实返回 JSON；列序 [2]=日最高 [4]=日最低 [8]=雨量
DAILY_EXTRACT = 'https://www.weather.gov.hk/cis/dailyExtract/dailyExtract_%s.xml'
MONTHS = ('january', 'february', 'march', 'april', 'may', 'june', 'july',
          'august', 'september', 'october', 'november', 'december')

# 分层检查点（香港本地时刻）。11:00 = 记忆里的执行窗口重心；
# 13:40 = 午后「是否已过峰」的分水岭；15:00 = 收尾。
CHECKPOINTS = ('10:00', '11:00', '11:30', '12:00', '13:00',
               '13:40', '14:00', '14:30', '15:00')

COLS = (['date', 'npts', 'rmax_all', 'official_max', 'rain_final']
        + ['rmax_%s' % c.replace(':', '') for c in CHECKPOINTS]
        + ['inst_1100', 'inst_1340', 'inst_1400',
           'pullback_1100', 'pullback_1340', 'pullback_1400'])


def _rng(lo, hi):
    """把半开带 [lo, hi) 写成可读标签；None = 该侧无界。"""
    return '[%s,%s)' % ('-inf' if lo is None else '%.1f' % lo,
                        '+inf' if hi is None else '%.1f' % hi)


# ------------------------------------------------------------------ 官方终值
def load_de_cache():
    if os.path.exists(DECACHE):
        try:
            d = json.load(open(DECACHE, encoding='utf-8'))
            if isinstance(d, dict) and 'months' in d:
                return d
        except Exception:
            pass
    return {'months': {}}


def save_de_cache(c):
    json.dump(c, open(DECACHE, 'w', encoding='utf-8'), indent=1, sort_keys=True)


def fetch_month(ym):
    """返回 {'YYYY-MM-DD': {'max':..,'min':..,'rain':..}}；失败返回 {}（静默）。

    ⚠ **雨量列不是数字，是 `Trace`** —— 微量雨的日子 HKO 写字符串 `Trace` 而不是 0。
    v0.25.0 实测踩过：`float(r[8])` 对 Trace 抛 ValueError，若把三列放在同一个 try 里，
    **整行被丢 → 当日的 official max 一起丢**。10-08 / 10-12 / 10-14 三天全是 Trace，
    于是「刚出官方终值的最近三天」恰好全部消失，Δ 分层丢掉的正是最同类的日子。
    → 三列必须**各自独立解析**，雨量非数字时存 None，不得影响 max/min。
    """
    y, m = int(ym[:4]), int(ym[5:7])
    url = DAILY_EXTRACT % '%04d%02d' % (y, m)
    try:
        req = urllib.request.Request(url, headers=UA)
        raw = urllib.request.urlopen(req, timeout=30).read().decode('utf-8', 'ignore')
        d = json.loads(raw)
    except Exception as e:
        print('[warn] Daily Extract %s 拉取失败: %s' % (ym, e), file=sys.stderr)
        return {}
    try:
        rows = d['stn']['data'][0]['dayData']
    except Exception:
        return {}

    def num(v):
        """HKO 的 Trace / 空 / 破折号都表示「微量或无」，不是数字。"""
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    out = {}
    for r in rows:
        try:
            day = '%04d-%02d-%02d' % (y, m, int(r[0]))   # r[0]='Mean/Total'/'Normal' 会抛，跳过
        except (ValueError, IndexError, TypeError):
            continue
        mx, mn, rn = num(r[2] if len(r) > 2 else None), \
            num(r[4] if len(r) > 4 else None), num(r[8] if len(r) > 8 else None)
        if mx is None:
            continue
        out[day] = {'max': mx, 'min': mn, 'rain': rn}
    return out


def official_map(dates, offline=False, max_age_h=2):
    """dates → {date: {'max','min','rain'}}；缺的键**不出现**（不得填 0）。

    ⚠ **TTL 必须短（默认 2 小时），不能用「一天一次」的直觉**。官方 Daily Extract 的
    推进节奏**不固定**：10-12 那次一次推进 3 天（→10-12），10-14 那次只推进 1 天；
    10-15 15:00 建的缓存到 10-15 收盘时官方已出到 10-14，而缓存里 10-12/10-14 仍为空。
    长 TTL 会让「官方还没出」被误读成「这天没数据」，Δ 分层直接丢掉最近两天 ——
    **恰好丢掉的正是最同类的日子**。

    缺失日的打印必须显式列出（`[缺失]` 行），否则「官方未出」与「脚本漏抓」无法区分。
    """
    cache = load_de_cache()
    months = sorted({d[:7] for d in dates})
    now = dt.datetime.now()
    changed = False
    for ym in months:
        ent = cache['months'].get(ym)
        stale = True
        if ent:
            try:
                age = (now - dt.datetime.fromisoformat(ent['fetched'])).total_seconds() / 3600.0
                stale = age > max_age_h
            except Exception:
                stale = True
        if stale and not offline:
            got = fetch_month(ym)
            if got:
                cache['months'][ym] = {'fetched': now.isoformat(timespec='seconds'),
                                       'days': got}
                changed = True
    if changed:
        save_de_cache(cache)
    out = {}
    for ym in months:
        ent = cache['months'].get(ym) or {}
        for day, v in (ent.get('days') or {}).items():
            if day in dates:
                out[day] = v
    return out


# ------------------------------------------------------------------ 读端同日渐层
def clean_points(key, pts):
    """只保留 `t[:10] == key` 的点（PENDING §K 第 5 条：读端按日期过滤）。

    这是对外唯一的读入口：archive 里任何外来点（05-29 污染形态）在进入分层表前
    就被丢弃，running max 不再被读成 33.4。
    """
    out = []
    for p in (pts or []):
        if isinstance(p, dict):
            t, v = p.get('t'), p.get('hko')
        else:
            t, v = (p + (None, None))[:2] if len(p) >= 2 else (None, None)
        if not isinstance(t, str) or len(t) < 16 or v is None:
            continue
        if t[:10] != key:                      # ← 污染点在此被丢弃
            continue
        try:
            out.append((t[11:16], float(v)))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def day_row(date_key, pts, off):
    """把一天的原始点压成分层表的一行。数值口径 = 现场算的同一口径。"""
    ser = clean_points(date_key, pts)
    if len(ser) < 3:                          # 拒写 <3 点的键（§K 第 3 条）
        return None
    row = {c: '' for c in COLS}
    row['date'] = date_key
    row['npts'] = len(ser)
    row['rmax_all'] = '%.1f' % max(v for _, v in ser)
    for cp in CHECKPOINTS:
        upto = [v for t, v in ser if t <= cp]
        row['rmax_%s' % cp.replace(':', '')] = '%.1f' % max(upto) if upto else ''
    for cp in ('11:00', '13:40', '14:00'):
        upto = [v for t, v in ser if t <= cp]
        key = 'inst_%s' % cp.replace(':', '')
        rk = 'rmax_%s' % cp.replace(':', '')
        if upto:
            row[key] = '%.1f' % upto[-1]
            if row[rk] != '':
                row['pullback_%s' % cp.replace(':', '')] = \
                    '%.1f' % (float(row[rk]) - upto[-1])
    o = off.get(date_key)
    if o:
        row['official_max'] = '%.1f' % o['max']
        if o.get('rain') is not None:
            row['rain_final'] = '%.1f' % o['rain']
        else:
            row['rain_final'] = 'Trace'      # 微量雨：显式标记，不当 0 也不当缺失
    return row


# ------------------------------------------------------------------ 汇总 / 切面
def _f(row, col):
    v = row.get(col) or ''
    try:
        return float(v)
    except ValueError:
        return None


def _delta(row):
    """Δ = 官方终值 − 当日 rmax_all（官方口径，跨源相减只用于趋势，不用于定档）。"""
    o, r = _f(row, 'official_max'), _f(row, 'rmax_all')
    return None if (o is None or r is None) else o - r


def load_rows():
    if not os.path.exists(STRATA_CSV):
        return []
    with open(STRATA_CSV, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _band(rows, col, lo, hi):
    out = []
    for r in rows:
        v = _f(r, col)
        if v is None:
            continue
        if (lo is None or v >= lo) and (hi is None or v < hi):
            out.append(r)
    return out


def print_summary(rows):
    ok = [r for r in rows if _f(r, 'official_max') is not None]
    print('分层表 %d 天，其中官方终值已出 %d 天' % (len(rows), len(ok)))
    miss = [r['date'] for r in rows if _f(r, 'official_max') is None]
    if miss:
        # 显式列出，否则「官方未出」与「脚本漏抓」无法区分（v0.25.0 实测踩过：
        # 缓存 TTL 过长导致 10-12/10-14 被当成缺失，Δ 分层丢掉的恰是最同类的两天）
        print('  [缺失] 官方 Daily Extract 未出：%s' % ' '.join(miss))
    if not ok:
        return

    print('\n[1] rmax@11:00 分层 → 官方终值')
    bands = [(None, 28.0), (28.0, 28.5), (28.5, 29.0), (29.0, 29.5), (29.5, None)]
    for lo, hi in bands:
        sub = [r for r in ok if (lambda v: v is not None and
                                  (lo is None or v >= lo) and (hi is None or v < hi))(
                                      _f(r, 'rmax_1100'))]
        if not sub:
            continue
        ds = [d for d in (_delta(r) for r in sub) if d is not None]
        peaks = sorted(_f(r, 'official_max') for r in sub)
        print('  r11 %-14s n=%2d  Δ mean %+.2f  终值 %s'
              % (_rng(lo, hi), len(sub), stats.mean(ds) if ds else float('nan'),
                 ' '.join('%.1f' % p for p in peaks)))

    print('\n[2] rmax@13:40 分层 → 官方终值 − r13')
    ok2 = [r for r in ok if _f(r, 'rmax_1340') is not None]
    ds = [(_f(r, 'official_max') - _f(r, 'rmax_1340')) for r in ok2]
    if ds:
        print('  全样本 n=%d  Δ mean %+.2f sd %.2f  P(≥+0.3)=%d/%d≈%.0f%%'
              % (len(ds), stats.mean(ds), stats.pstdev(ds),
                 sum(1 for x in ds if x >= 0.3), len(ds),
                 100.0 * sum(1 for x in ds if x >= 0.3) / len(ds)))
    for lo, hi in [(28.5, 29.0), (29.0, 29.5), (29.5, 30.0), (30.0, None)]:
        sub = [r for r in ok2 if (lambda v: v is not None and
                                  (lo is None or v >= lo) and (hi is None or v < hi))(
                                      _f(r, 'rmax_1340'))]
        if not sub:
            continue
        d = [_f(r, 'official_max') - _f(r, 'rmax_1340') for r in sub]
        print('  r13:40 %-14s n=%2d  Δ mean %+.2f  P(≥+0.3)=%d/%d  最近同类日 %s'
              % (_rng(lo, hi), len(sub), stats.mean(d),
                 sum(1 for x in d if x >= 0.3), len(sub),
                 ', '.join(sorted(r['date'][5:] for r in sub)[-4:])))

    print('\n[3] 13:40 回撤条件化（pullback = r13:40 − 瞬时站温）')
    for lo, hi, lab in [(0.0, 0.05, '≈0（无回撤）'), (0.05, 0.5, '0.05~0.5'),
                        (0.5, None, '≥0.5（有回撤）')]:
        sub = [r for r in ok2 if (lambda v: v is not None and
                                  (lo is None or v >= lo) and (hi is None or v < hi))(
                                      _f(r, 'pullback_1340'))]
        if not sub:
            continue
        d = [_f(r, 'official_max') - _f(r, 'rmax_1340') for r in sub]
        print('  pullback %-12s n=%2d  Δ mean %+.2f  P(≥+0.3)=%d/%d'
              % (lab, len(sub), stats.mean(d), sum(1 for x in d if x >= 0.3), len(sub)))


def print_pollution(rows_note, archive):
    """读端闸门自检：报告被丢弃的外来点，让污染可见而不是静默修掉。"""
    if rows_note:
        print('\n[闸门] %s' % rows_note)


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser(description='重建/查询分层表 strata.csv')
    ap.add_argument('--summary', action='store_true', help='只打印三层切面')
    ap.add_argument('--offline', action='store_true', help='不拉 Daily Extract')
    ap.add_argument('--ttl-h', type=float, default=2.0,
                    help='官方 Daily Extract 缓存小时数（默认 2；官方推进节奏不固定，'
                         '不要设成「一天一次」——会把刚出的终值当成缺失）')
    ap.add_argument('--date', help='只重算该日 YYYY-MM-DD（其余行沿用盘上已有）')
    a = ap.parse_args()

    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    if not os.path.exists(ARCHIVE):
        raise SystemExit('[err] 缺 %s（需先跑 hk_edge.py --watch 累积）' % ARCHIVE)
    arch = json.load(open(ARCHIVE, encoding='utf-8'))
    keys = sorted(k for k in arch if isinstance(arch[k], list))
    a.date and keys.__setitem__(slice(None), [k for k in keys
                                              if not a.date or k == a.date])

    off = official_map(keys, offline=a.offline, max_age_h=a.ttl_h)
    note, new_rows = [], {}
    for k in keys:
        raw = arch[k]
        before = len(raw)
        row = day_row(k, raw, off)
        dropped = before - len(clean_points(k, raw))
        if dropped:
            note.append('%s 丢弃外来点 %d 个' % (k, dropped))
        if row is None:
            note.append('%s 拒写（有效点 %d < 3）' % (k, before - dropped))
            continue
        new_rows[k] = row

    if a.summary and not a.date:
        rows = [new_rows[k] for k in sorted(new_rows)]
        print_summary(rows)
        print_pollution('；'.join(note), arch)
        return

    # 合并：未重算的日期沿用盘上已有行（保留历史 official_max）
    old = {r['date']: r for r in load_rows()}
    for k in new_rows:
        if new_rows[k]['official_max'] == '' and k in old \
                and old[k].get('official_max'):
            new_rows[k]['official_max'] = old[k]['official_max']
        if new_rows[k]['rain_final'] == '' and k in old \
                and old[k].get('rain_final'):
            new_rows[k]['rain_final'] = old[k]['rain_final']
    old.update(new_rows)
    out = [old[k] for k in sorted(old)]
    with open(STRATA_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in out:
            w.writerow({c: r.get(c, '') for c in COLS})
    print('已写 %s：%d 行（新增/更新 %d 天）' % (STRATA_CSV, len(out), len(new_rows)))
    if note:
        print('[闸门] %s' % '；'.join(note))
    print_summary(out)


if __name__ == '__main__':
    main()