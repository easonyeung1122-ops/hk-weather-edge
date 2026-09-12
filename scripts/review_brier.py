#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复盘：把 data/decision_log.csv 的逐时点「模型公允 P vs 市场 P」按结算档算 Brier。

为什么需要它：`--score` 打的是 forecast_log.jsonl（one-touch 阶梯口径），
回答不了「模型和市场谁更准、在一天的哪个时段更准」。
decision_log.csv 恰好逐时点成对记录了 fair_p 与 market_p，是唯一能做这件事的数据。

用法
----
  python scripts/review_brier.py                      # 结算档自动从 maxt_HKO.csv 取
  python scripts/review_brier.py --truth 2026-09-11=32
  python scripts/review_brier.py --json               # 机器可读

口径提醒（很重要）
------------------
Brier 的分母**只取 decision_log 实际记录的 5–8 个档位**，不是全部 11–12 档。
所以绝对值偏高、且不同日之间不可直接比大小。
**同一个时点内「模型 vs 市场」的差值**才有意义——两者用同一套档位、同一套真值。
"""
import argparse
import collections
import csv
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
LOG = os.path.join(DATA, 'decision_log.csv')
MAXT = os.path.join(DATA, 'maxt_HKO.csv')

# 上午 / 午后 分界（与 SKILL.md「出仓窗口 09:00–11:30」一致）
MORNING_CUTOFF_HOUR = 12


def load_settled(maxt_path=MAXT):
    """从结算源本身（maxt_HKO.csv，Absolute Daily Max）取逐日结算档 = floor(值)。"""
    out = {}
    if not os.path.exists(maxt_path):
        return out
    for row in csv.reader(open(maxt_path, encoding='utf-8-sig')):
        if len(row) < 4:
            continue
        try:
            y, m, d, v = int(row[0]), int(row[1]), int(row[2]), float(row[3])
        except ValueError:
            continue
        out[f'{y:04d}-{m:02d}-{d:02d}'] = int(v)   # 档位 N = [N.0, N+1.0)
    return out


def load_rows(path=LOG):
    if not os.path.exists(path):
        return []
    raw = open(path, encoding='utf-8-sig').read().splitlines()
    rows = []
    for line in raw[1:]:
        f = line.split(',')
        if len(f) < 6:
            continue
        try:
            rows.append((f[0].strip(), f[1].strip(), int(f[2]),
                         float(f[3]), float(f[4])))
        except ValueError:
            continue
    return rows


def parse_truth(s):
    """DATE=BUCKET 逗号分隔 -> dict"""
    out = {}
    if not s:
        return out
    for part in s.split(','):
        k, _, v = part.partition('=')
        out[k.strip()] = int(v)
    return out


def brier(d, truth):
    """d: {bucket: (fair_p, market_p)}；返回 (模型Brier, 市场Brier, n, 模型众数, 市场众数)"""
    bm = bk = 0.0
    n = 0
    for b, (mp, mkp) in d.items():
        y = 1.0 if b == truth else 0.0
        bm += (mp - y) ** 2
        bk += (mkp - y) ** 2
        n += 1
    if n == 0:
        return None
    mmode = max(d.items(), key=lambda x: x[1][0])[0]
    kmode = max(d.items(), key=lambda x: x[1][1])[0]
    return bm / n, bk / n, n, mmode, kmode


def hour_of(tstr):
    """decision_log 的 logged_at 是 'YYYY-MM-DD HH:MM'。"""
    try:
        return int(tstr.split()[1].split(':')[0])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description='decision_log 的 Brier 时间分层复盘')
    ap.add_argument('--truth', default='',
                    help='覆盖结算档，格式 "2026-09-11=32,2026-09-12=32"（默认读 maxt_HKO.csv，可含本日实测下限）')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    a = ap.parse_args()

    settled = load_settled()
    settled.update(parse_truth(a.truth))
    rows = load_rows()

    # 按 (target_date, 时点) 归组；注意 logged_at 归属「作出该预测的时刻」
    g = collections.defaultdict(dict)
    for t, date, bucket, fair, mkt in rows:
        g[(date, t)][bucket] = (fair, mkt)

    result = {}
    for date in sorted({k[0] for k in g}):
        if date not in settled:
            continue
        truth = settled[date]
        keys = sorted([k for k in g if k[0] == date], key=lambda k: k[1])
        per = []
        for k in keys:
            r = brier(g[k], truth)
            if not r:
                continue
            bm, bk, n, mm, km = r
            per.append(dict(t=k[1], hour=hour_of(k[1]), n=n,
                            brier_model=round(bm, 6), brier_market=round(bk, 6),
                            mode_model=mm, mode_market=km,
                            winner=('model' if bm < bk - 1e-9 else
                                    'market' if bk < bm - 1e-9 else 'tie')))
        # 分时段汇总（只看当天作出的预测，排除提前一天存档的）
        same_day = [p for p in per if p['t'].startswith(date)]
        summer = [p for p in same_day if p['hour'] is not None
                  and p['hour'] < MORNING_CUTOFF_HOUR]
        pmer = [p for p in same_day if p['hour'] is not None
                and p['hour'] >= MORNING_CUTOFF_HOUR]

        def agg(lst):
            if not lst:
                return None
            return dict(n=len(lst),
                        brier_model=round(sum(x['brier_model'] for x in lst) / len(lst), 6),
                        brier_market=round(sum(x['brier_market'] for x in lst) / len(lst), 6))

        result[date] = dict(truth=truth, points=per,
                            morning=agg(summer), afternoon=agg(pmer))

    source = dict(decision_log=LOG, decision_log_rows=len(rows), maxt=MAXT)

    if a.json:
        print(json.dumps(dict(source=source, days=result),
                         ensure_ascii=False, indent=2))
        return

    # ⚠ 必须打印数据源路径：本技能有两份副本（.workbuddy 是活的、.codebuddy 是 git 源），
    # 各自的 scripts/data/ 是独立的本地状态。跑错副本会读到几天前的旧日志，
    # 复盘结论会整个错掉（2026-09-12 实际踩过：.codebuddy 的 decision_log 停在 09-08）。
    print(f'数据源 decision_log: {LOG}  ({len(rows)} 行)')
    print(f'数据源 maxt_HKO    : {MAXT}')
    if not os.path.exists(LOG):
        print('[warn] decision_log.csv 不存在 —— 该副本从未跑过 --date/--watch？')
        return

    for date, R in result.items():
        print()
        print('=' * 88)
        print(f'{date}   结算档 = {R["truth"]}°C   （记录时点 {len(R["points"])} 个）')
        print(f'{"时刻":<18}{"N":>3}  {"模型Brier":>10}{"市场Brier":>11}  {"胜方":>7}'
              f'{"模型众数":>9}{"市场众数":>9}')
        for p in R['points']:
            print(f'{p["t"]:<18}{p["n"]:>3}  {p["brier_model"]:>10.4f}'
                  f'{p["brier_market"]:>11.4f}  {p["winner"]:>7}'
                  f'{p["mode_model"]:>7}°C{p["mode_market"]:>7}°C')
        for label, key in (('上午 (<12时)', 'morning'), ('午后 (>=12时)', 'afternoon')):
            A = R.get(key)
            if not A:
                continue
            bm, bk = A['brier_model'], A['brier_market']
            if abs(bm - bk) < 1e-9:
                tail = '平'
            elif bm < bk:
                tail = f'模型领先 {(bk - bm) / bk:.1%}'
            else:
                tail = f'市场领先 {(bm - bk) / bm:.1%}'
            print(f'  {label:<12} n={A["n"]}  模型 {bm:.4f}  vs  市场 {bk:.4f}  → {tail}')
    print()
    print('口径：Brier 分母只含 decision_log 记录的 5–8 个档位，绝对值偏高；'
          '只有「模型 vs 市场」的差值可比。')


if __name__ == '__main__':
    sys.exit(main())
