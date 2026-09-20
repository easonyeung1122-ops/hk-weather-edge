#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hourly_window.py —— 「最佳下注时段」的四轴量化（v0.21.0）

把 watch.log 的日内站点轨迹 + decision_log.csv 的逐时点市场定价 + rec_pnl_log.csv
的台账，按**小时**对齐，回答一个问题：

    十点、十一点、十四点…… 每个小时进场，各自在赌什么？

四条互相独立的轴（都不依赖「我方模型更准」这个不可验证的前提）：

  轴1  剩余行程 R(h) = 最终峰值 − rmax(h)      —— 这一刻「还有多少没兑现」
       统计量：均值 / 中位 / sd / P(R<0.1)（=已见顶）/ P(R≥1.0)（=还能跨档）
  轴2  最终档已达成率 P(floor(rmax(h)) == floor(峰值))
       —— 进场时当天答案是否已经写在墙上
  轴3  市场对「最终结算档」的定价均值           —— 市场什么时候才认账（data/decision_log.csv）
  轴4  该小时的建议 ROI（读 rec_pnl_log.csv）   —— 真金白银（样本极小，只作方向）

用法：
    py -3 scripts/hourly_window.py                  # 全表
    py -3 scripts/hourly_window.py --hour 11        # 只看某小时的逐日明细
    py -3 scripts/hourly_window.py --since 2026-09-11

口径与限制（必须随结论一起转述）：
  * 峰值口径 = watch.log 的「今日已观测最高」最大者（10 分钟采样，会漏 0.1–0.2°C）。
  * n = 天数，不是观测数；日内多个 10 分钟点高度自相关，**不构成独立样本**。
  * 轴3 的市场价是 decision_log 记下的快照价，不是订单簿可成交价。
  * 轴4 的样本按 §5.2 口径 11 折叠过（同腿只记首推），金额只作量级参考。
"""

import argparse
import csv
import os
import re
import statistics as st
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
WATCH = os.path.join(os.path.dirname(HERE), 'watch.log')

HRS = list(range(8, 17))
TEMPLATE = (r'香港时间\s+(\d{2}):(\d{2})\s+天文台站\s+([\d.]+)°C'
            r'\s+今日已观测最高\s+([\d.]+)°C')


# ---------------------------------------------------------------- watch.log
def load_days(path=WATCH):
    """-> {date: {'HH:MM': (站温, rmax)}}"""
    s = open(path, 'rb').read().decode('utf-8', 'replace')
    starts = [(m.start(), m.group(1))
              for m in re.finditer(r'\[silent\] start (\d{4}-\d{2}-\d{2})', s)]
    out = {}
    for k, (pos, dt) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(s)
        r = re.findall(TEMPLATE, s[pos:end])
        if r:
            out[dt] = {a[0] + ':' + a[1]: (float(a[2]), float(a[3])) for a in r}
    return out


def at_hour(day, h, tol=15):
    """取最接近 h:00 的样本（分钟差 ≤ tol）。"""
    best, bd = None, 10 ** 9
    for t, v in day.items():
        hh, mm = int(t[:2]), int(t[3:])
        d = abs((hh * 60 + mm) - h * 60)
        if d <= tol and d < bd:
            best, bd = v, d
    return best


def per_day_table(days):
    rows = []
    for dt in sorted(days):
        day = days[dt]
        final = max(v[1] for v in day.values())
        row = {'date': dt, 'final': final, 'bucket': int(final)}
        for h in HRS:
            v = at_hour(day, h)
            row[h] = None if v is None else {
                't': v[0], 'rmax': v[1], 'R': round(final - v[1], 2),
                'locked': int(v[1]) == int(final),
            }
        # 首次达到最终档的时刻
        lock = None
        for t in sorted(day):
            if int(day[t][1]) == int(final):
                lock = t
                break
        row['lock'] = lock
        rows.append(row)
    return rows


# ------------------------------------------------------- decision_log.csv
def market_by_hour(path=None, days=None):
    """-> {hour: [(市场mid均值, 模型公允均值)]}，**先按「天 × 小时」聚合再跨天平均**
    （§3.3 的方法学要求：一天内几十条快照高度自相关，不能直接当独立样本）"""
    path = path or os.path.join(DATA, 'decision_log.csv')
    if not os.path.exists(path):
        return {}
    days = days or load_days()
    settle = {d: int(max(v[1] for v in days[d].values())) for d in days}
    per_day = defaultdict(lambda: defaultdict(lambda: [[], []]))
    for r in csv.DictReader(open(path, encoding='utf-8')):
        d = r['logged_at'][:10]
        if r['target_date'].strip() != d or d not in settle:
            continue
        try:
            b = int(r['bucket']); mp = float(r['market_p'])
        except (ValueError, KeyError):
            continue
        if b != settle[d]:
            continue
        try:
            fp = float(r['fair_p'])
        except (ValueError, KeyError):
            fp = None
        slot = per_day[int(r['logged_at'][11:13])][d]
        slot[0].append(mp)
        if fp is not None:
            slot[1].append(fp)
    out = {}
    for h, byday in per_day.items():
        m = [sum(v[0]) / len(v[0]) for v in byday.values() if v[0]]
        f = [sum(v[1]) / len(v[1]) for v in byday.values() if v[1]]
        out[h] = (m, f)
    return out


# ------------------------------------------- 轴4：台账 ROI 的静态参照（§3.3）
# 来源：SKILL.md §3.3「实测 P&L 分层」，由 rec_pnl_log.csv 的首推时刻**人工**解析而成
# （note 里的 HH:MM 常指向犯错时刻而非进场时刻，机器解析会错，故不自动读）。
PNL_REF = [
    ('09:00-10:29', 4,  261.40, -150.29, -57.5, '1/4'),
    ('10:30-11:59', 4,  355.00,  149.37,  42.1, '3/4'),
    ('12:00-13:19', 3,  124.20,   98.02,  78.9, '1/3'),
    ('13:20-15:00', 14, 429.72, -429.72, -100.0, '0/14'),
    ('>=15:00',     1,  103.00, -103.00, -100.0, '0/1'),
]


# ------------------------------------------------------------------- print
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2026-09-11')
    ap.add_argument('--hour', type=int)
    ap.add_argument('--watch', default=WATCH)
    a = ap.parse_args()

    days = load_days(a.watch)
    rows = [r for r in per_day_table(days) if r['date'] >= a.since]
    if not rows:
        print('watch.log 里没有 %s 之后的样本' % a.since)
        return

    if a.hour is not None:
        print('=== 逐日明细：%02d:00 时刻 ===' % a.hour)
        print('日            该刻站温  rmax   剩余R  最终峰值 最终档 最终档首达')
        for r in rows:
            v = r.get(a.hour)
            if not v:
                print('%s      -      -      -      %5.1f   %2d    %s'
                      % (r['date'], r['final'], r['bucket'], r['lock'] or '-'))
                continue
            print('%s   %5.1f   %5.1f  %+5.2f    %5.1f   %2d    %s'
                  % (r['date'], v['t'], v['rmax'], v['R'], r['final'],
                     r['bucket'], r['lock'] or '-'))
        return

    # ---- 轴1/轴2：按小时汇总
    print('=== 轴1+轴2｜每个进场小时「还剩多少没兑现」 ===')
    print('（R = 最终峰值 − 该刻 running max；「已锁定」= 该刻 rmax 已落在最终档内）')
    print()
    print('小时   n    R均值   R中位   R的sd  P(R<0.1) P(R≥0.5) P(R≥1.0) 已锁定率')
    mkt = market_by_hour()
    for h in HRS:
        Rs = [r[h]['R'] for r in rows if r.get(h)]
        if not Rs:
            continue
        n = len(Rs)
        f = lambda c: '%6.0f%%' % (100.0 * sum(1 for x in Rs if c(x)) / n)
        lk = [r[h]['locked'] for r in rows if r.get(h)]
        print('%02d时  %2d   %+6.2f  %+6.2f  %6.2f  %s  %s  %s   %5.0f%%'
              % (h, n, sum(Rs) / n, st.median(Rs), st.pstdev(Rs),
                 f(lambda x: x < 0.1), f(lambda x: x >= 0.5),
                 f(lambda x: x >= 1.0),
                 100.0 * sum(lk) / len(lk)))

    # ---- 最终档首达时刻分布
    print()
    print('=== 最终档「首次达成」时刻分布（当天答案什么时候才写在墙上）===')
    hist = defaultdict(int)
    for r in rows:
        if r['lock']:
            hist[r['lock']] += 1
    tot = sum(hist.values())
    for t in sorted(hist):
        print('  %s  %d 天  %s' % (t, hist[t], '█' * hist[t]))
    if tot:
        mins = sorted(int(t[:2]) * 60 + int(t[3:]) for t in hist for _ in range(hist[t]))
        print('  中位数 = %02d:%02d ｜ n=%d 天' % (mins[len(mins) // 2] // 60,
                                                 mins[len(mins) // 2] % 60, tot))

    # ---- 轴3：市场什么时候才认账
    if mkt:
        print()
        print('=== 轴3｜市场 vs 模型 给「最终结算档」的定价（先按天×小时聚合，再跨天平均）===')
        print('小时   天数   市场均值   模型均值   差(模型-市场)')
        for h in HRS:
            v = mkt.get(h)
            if not v:
                continue
            m, f = v
            ms = '%6.3f' % (sum(m) / len(m))
            fs = ('%6.3f' % (sum(f) / len(f))) if f else '   -  '
            ds = ('%+7.3f' % (sum(f) / len(f) - sum(m) / len(m))) if f else '   -   '
            print('%02d时   %3d    %s     %s      %s' % (h, len(m), ms, fs, ds))
    else:
        print()
        print('（decision_log.csv 无同日快照，轴3 跳过）')

    # ---- 轴4：台账 ROI 的静态参照
    print()
    print('=== 轴4｜该时段的建议 ROI（SKILL §3.3，首推时刻口径，n 极小）===')
    print('时段           腿   投入      净盈亏     ROI     胜率')
    for tag, n, cost, net, roi, wr in PNL_REF:
        print('%-13s  %2d  $%7.2f  $%8.2f  %+7.1f%%  %s'
              % (tag, n, cost, net, roi, wr))

    print()
    print('⚠ n = 天数。日内多个 10 分钟点高度自相关，不构成独立样本；')
    print('  轴3 是快照 mid（非可成交价），轴4 是人工口径的静态参照，两者都只作方向参考。')


if __name__ == '__main__':
    main()
