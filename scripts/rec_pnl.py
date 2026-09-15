#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rec_pnl.py —— 「每日交易建议」的结算盈亏台账。v0.17.0

回答一个问题：**每天给出去的交易建议，整体是赚还是亏？**
（记录的是建议口径，不是成交口径；成交价与成交量未知。）

数据源：scripts/data/rec_pnl_log.csv（一条腿一行，以 # 开头的行为注释）
口径：每条腿取「首推价 + 建议仓位」，同腿后续加仓/改价不重复计入。

v0.17.0 起口径由代码强制，不再依赖人工折叠 CSV：
  - `outcome=void`（作废 / 撤回 / 未发出）**一律不入账**，也不再被当成「未结算」；
    v0.16.29 及更早版本把 void 当 open，导致该日永远无法收口（9/15 实际踩到）。
  - 同一 (结算日, 腿, 方向, 档位) 只取**首次**出现的那一行入账；后续加仓 / 改价 /
    被取代的版本标为「↳ 加仓」仅在明细表里可见（`--all-legs` 可关闭折叠看全量流水）。
  - `--settle` 支持 `--status provisional`，不必再跑完 `--write` 后手工把
    `settle_status` 改回 provisional（老坑，见 CSV 文件头注释）。

用法：
  py -3 scripts/rec_pnl.py                            # 逐日汇总 + 总计 + 累计曲线
  py -3 scripts/rec_pnl.py --json                     # 机器可读
  py -3 scripts/rec_pnl.py --open                     # 只列未结算的腿
  py -3 scripts/rec_pnl.py --all-legs                 # 不折叠同腿加仓（看全量流水）
  py -3 scripts/rec_pnl.py --settle 2026-09-12=32     # 试算回填（不落盘）
  py -3 scripts/rec_pnl.py --settle 2026-09-12=32 --write   # 落盘
  py -3 scripts/rec_pnl.py --settle 2026-09-15=31 --status provisional --write
  py -3 scripts/rec_pnl.py --auto --write             # 用 maxt_HKO.csv（结算源）回填
  py -3 scripts/rec_pnl.py --markdown                 # 重新生成仓库根目录 PNL.md
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
LOG = os.path.join(DATA, 'rec_pnl_log.csv')
MAXT = os.path.join(DATA, 'maxt_HKO.csv')
ROOT = os.path.dirname(HERE)          # 仓库根
MD = os.path.join(ROOT, 'PNL.md')

FIELDS = ['settle_date', 'leg', 'side', 'bucket', 'entry_price', 'cost_usd',
          'settle_bucket', 'settle_status', 'outcome', 'note']

CCY = '$'
VOID = 'void'          # outcome=void：明示作废 / 撤回 / 未发出的建议，不入账


# ---------------------------------------------------------------- IO
def _header_lines():
    """原样保留文件头的注释行，回写时不丢。"""
    out = []
    with open(LOG, encoding='utf-8-sig') as f:
        for ln in f:
            if ln.lstrip().startswith('#') or not ln.strip():
                out.append(ln.rstrip('\n').rstrip('\r'))
            else:
                break
    return out


def load_rows():
    rows = []
    with open(LOG, encoding='utf-8-sig') as f:
        lines = [ln for ln in f
                 if ln.strip() and not ln.lstrip().startswith('#')]
    for r in csv.DictReader(io.StringIO(''.join(lines))):
        if not r.get('settle_date'):
            continue
        rows.append(r)
    return rows


def save_rows(rows, header):
    with open(LOG, 'w', encoding='utf-8', newline='') as f:
        for ln in header:
            f.write(ln + '\n')
        # lineterminator 显式给 '\n'：DictWriter 默认是 '\r\n'，会把整个 CSV 的换行符
        # 从 LF 翻成 CRLF，在 git 里变成整文件重写（2026-09-15 实测）。
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator='\n')
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in FIELDS})


# ---------------------------------------------------------------- 口径
def _oc(r):
    return (r.get('outcome') or '').strip().lower()


def is_void(r):
    """outcome=void 的行：官方口径下的「作废 / 撤回 / 未发出」，永不计入任何经济量。"""
    return _oc(r) == VOID


def leg_key(r):
    """同一条腿的键 = 同一结算日 + 同腿名 + 同方向 + 同档位。"""
    return (r['settle_date'], (r.get('leg') or '').strip(),
            (r.get('side') or '').strip().upper(), (r.get('bucket') or '').strip())


def classify(rows, all_legs=False):
    """给每行打标签：'first'（入账）/ 'addon'（同腿加仓，不入账）/ 'void'（作废，不入账）。

    按 CSV 行序（= 追加顺序 = 时间序）取每腿首次出现的行。
    """
    seen = set()
    tag = {}
    for i, r in enumerate(rows):
        if is_void(r):
            tag[i] = 'void'
            continue
        k = leg_key(r)
        if not all_legs and k in seen:
            tag[i] = 'addon'
        else:
            seen.add(k)
            tag[i] = 'first'
    return tag


def skip_counts(rows, tag):
    """每个结算日被折叠掉的行数（加仓 / 作废），用于输出透明化。"""
    out = {}
    for i, r in enumerate(rows):
        t = tag[i]
        if t == 'first':
            continue
        a = out.setdefault(r['settle_date'], {'addon': 0, 'void': 0})
        a[t] += 1
    return out


# ---------------------------------------------------------------- 结算
def bucket_of_day():
    """从结算源 maxt_HKO.csv 取 {date: bucket}。该源滞后约 10 天。"""
    out = {}
    if not os.path.exists(MAXT):
        return out
    with open(MAXT, encoding='utf-8-sig') as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            try:
                d = dt.date.fromisoformat(row[0].strip())
                v = float(row[1])
            except (ValueError, IndexError):
                continue
            out[d.isoformat()] = int(v // 1)
    return out


def settle(rows, mapping, status='confirmed', only_missing=True):
    """mapping: {date: bucket}。回填 settle_bucket 并按档位重算 outcome。

    `outcome=void` 的行**永不回填** —— 作废 / 撤回的建议本来就不该有结算结果，
    早先 --settle --write 会把它们一并写成 confirmed 并计入盈亏（重复计数）。
    """
    changed = []
    for r in rows:
        if is_void(r):
            continue
        d = r['settle_date']
        if d not in mapping:
            continue
        if only_missing and str(r.get('settle_bucket', '')).strip():
            continue
        b = int(mapping[d])
        old = (r.get('settle_bucket', ''), r.get('outcome', ''))
        old_st = (r.get('settle_status') or '').strip()
        r['settle_bucket'] = str(b)
        r['settle_status'] = status
        r['outcome'] = 'win' if leg_wins(r, b) else 'loss'
        if old != (r['settle_bucket'], r['outcome']) or old_st != status:
            changed.append({'date': r['settle_date'], 'leg': r['leg'], 'bucket': b,
                            'old_outcome': old[1] or 'open', 'new_outcome': r['outcome'],
                            'old_status': old_st or '—', 'new_status': status,
                            'value_changed': old != (r['settle_bucket'], r['outcome'])})
    return changed


def leg_wins(row, bucket):
    """YES N 赢 ⟺ 结算 = N；NO N 赢 ⟺ 结算 ≠ N。"""
    side = (row.get('side') or '').strip().upper()
    n = int(float(row['bucket']))
    if side == 'YES':
        return bucket == n
    if side == 'NO':
        return bucket != n
    raise ValueError(f'未知 side={row.get("side")!r}（应为 YES / NO）')


# ---------------------------------------------------------------- 计算
def leg_econ(r):
    """返回 (price, cost, shares, payout, pnl)；未结算返回 None。"""
    cost = float(r['cost_usd'])
    oc = (r.get('outcome') or '').strip().lower()
    px = (r.get('entry_price') or '').strip()
    px = float(px) if px else None
    shares = (cost / px) if px else None
    if oc not in ('win', 'loss'):
        return px, cost, shares, None, None
    payout = shares if (oc == 'win' and shares is not None) else 0.0
    if oc == 'win' and shares is None:
        return px, cost, None, None, None
    return px, cost, shares, payout, payout - cost


def rollup(rows, tag=None, skips=None):
    """按「首推口径」汇总：只有 tag=='first' 的行进成本 / 回收 / 胜负。

    胜-负按 `outcome` 字段计（而不是按盈亏正负）—— 卖出腿（cost_usd<0）赢的时候
    净额是小的负数，按正负计会把它误记成亏损腿。
    """
    tag = tag if tag is not None else classify(rows)
    by_day = {}
    for i, r in enumerate(rows):
        if tag[i] != 'first':
            continue
        by_day.setdefault(r['settle_date'], []).append(r)
    days = []
    for d in sorted(by_day):
        rs = by_day[d]
        cost = payout = pnl = 0.0
        w = l = op = 0
        complete = True
        for r in rs:
            _px, c, _sh, po, p = leg_econ(r)
            cost += c
            if po is None:
                op += 1
                complete = False
                continue
            payout += po
            pnl += p
            if _oc(r) == 'win':
                w += 1
            else:
                l += 1
        sb = next((r['settle_bucket'] for r in rs if r.get('settle_bucket')), '')
        st = next((r.get('settle_status') or '' for r in rs if r.get('settle_status')), '')
        sk = (skips or {}).get(d, {})
        days.append({
            'date': d, 'settle_bucket': sb, 'settle_status': st,
            'legs': len(rs), 'wins': w, 'losses': l, 'open': op,
            'addon': sk.get('addon', 0), 'void': sk.get('void', 0),
            'cost': cost, 'payout': payout, 'pnl': pnl,
            'roi': (pnl / cost) if cost else 0.0,
            'verdict': '—' if not complete else ('盈' if pnl > 1e-9 else '亏' if pnl < -1e-9 else '平'),
        })
    return days


def by_side(rows, tag=None):
    tag = tag if tag is not None else classify(rows)
    acc = {}
    for i, r in enumerate(rows):
        if tag[i] != 'first':
            continue
        _px, c, _sh, po, p = leg_econ(r)
        if po is None:
            continue
        a = acc.setdefault(r['side'].upper(), {'legs': 0, 'wins': 0, 'cost': 0.0, 'pnl': 0.0})
        a['legs'] += 1
        a['wins'] += 1 if _oc(r) == 'win' else 0
        a['cost'] += c
        a['pnl'] += p
    return acc


# ---------------------------------------------------------------- 输出
def pct(x):
    return f'{x * 100:+.1f}%'


def money(x):
    return f'{CCY}{x:,.2f}' if x >= 0 else f'-{CCY}{abs(x):,.2f}'


def print_report(rows, days, tag=None):
    hdr = _header_lines()
    tag = tag if tag is not None else classify(rows)
    n_addon = sum(1 for t in tag.values() if t == 'addon')
    n_void = sum(1 for t in tag.values() if t == 'void')
    print(f'数据源：{LOG}')
    print(f'        {len(rows)} 条记录 / {len(days)} 个结算日'
          f'（注释 {len(hdr)} 行；以 # 开头的行为口径说明）')
    print(f'        口径：同腿只记首推 → 已折叠同腿加仓 {n_addon} 条、'
          f'作废/撤回 {n_void} 条（不加 --all-legs 时不入账）')
    print()

    print(f'{"结算日":<12}{"结算档":>7}{"腿":>4}{"胜-负":>7}{"投入":>11}'
          f'{"回收":>11}{"净盈亏":>11}{"ROI":>9}   当日')
    print('-' * 84)
    for d in days:
        mark = '*' if d['settle_status'] == 'provisional' else ''
        sb = f'{d["settle_bucket"]}°C{mark}' if d['settle_bucket'] else '—'
        wl = f'{d["wins"]}-{d["losses"]}'
        if d['open']:
            wl += f'+{d["open"]}开'
        _p = money(d['pnl']) if d['open'] == 0 else '—'
        _r = pct(d['roi']) if d['open'] == 0 else '—'
        print(f'{d["date"]:<12}{sb:>7}{d["legs"]:>4}{wl:>7}{money(d["cost"]):>11}'
              f'{money(d["payout"]):>11}{_p:>11}{_r:>9}   {d["verdict"]}')
    print('-' * 84)

    done = [d for d in days if d['open'] == 0]
    tc = sum(d['cost'] for d in done)
    tp = sum(d['payout'] for d in done)
    tpnl = sum(d['pnl'] for d in done)
    tw = sum(d['wins'] for d in done)
    tl = sum(d['losses'] for d in done)
    prof = sum(1 for d in done if d['pnl'] > 1e-9)
    loss = sum(1 for d in done if d['pnl'] < -1e-9)
    print(f'{"合计":<12}{"":>7}{tw + tl:>4}{f"{tw}-{tl}":>7}{money(tc):>11}'
          f'{money(tp):>11}{money(tpnl):>11}{pct(tpnl / tc if tc else 0):>9}'
          f'   {prof}盈利/{loss}亏损')
    print()

    print(f'逐腿胜率 {tw}/{tw + tl} = {tw / (tw + tl) * 100:.0f}%'
          f'　│　单腿均成本 {money(tc / max(tw + tl, 1))}'
          f'　│　单腿均盈亏 {money(tpnl / max(tw + tl, 1))}')
    print()

    print('按方向拆分')
    for s, a in sorted(by_side(rows, tag).items()):
        print(f'  {s:<4} {a["legs"]:>2} 腿  {a["wins"]}-{a["legs"] - a["wins"]}  '
              f'投入 {money(a["cost"]):>10}  净盈亏 {money(a["pnl"]):>10}  '
              f'ROI {pct(a["pnl"] / a["cost"] if a["cost"] else 0)}')
    print()

    print('累计（按结算日）')
    cum = 0.0
    for d in done:
        cum += d['pnl']
        bar = '#' * min(int(abs(cum) / 10), 40)
        print(f'  {d["date"]}  {money(cum):>10}  {"+" if cum >= 0 else "-"}{bar}')
    print()

    if any(d['settle_status'] == 'provisional' for d in days):
        print('注 * 结算档为暂定（按当日结算站已观测最高 + 市场价判定），官方 Daily Extract 隔日出。')


def print_open(rows, tag=None):
    tag = tag if tag is not None else classify(rows)
    op = [r for i, r in enumerate(rows)
          if tag[i] == 'first' and _oc(r) not in ('win', 'loss')]
    if not op:
        print('没有未结算的腿。')
        return
    print(f'未结算 {len(op)} 条腿：')
    for r in op:
        print(f'  {r["settle_date"]}  {r["leg"]:<6} {r["side"]:<4} {r["bucket"]:>3}°C'
              f'  首推价 {r.get("entry_price") or "-":<7} 建议投入 {money(float(r["cost_usd"]))}')


def write_markdown(rows, days, tag=None, all_legs=False):
    tag = tag if tag is not None else classify(rows, all_legs=all_legs)
    done = [d for d in days if d['open'] == 0]
    tc = sum(d['cost'] for d in done)
    tp = sum(d['payout'] for d in done)
    tpnl = sum(d['pnl'] for d in done)
    tw = sum(d['wins'] for d in done)
    tl = sum(d['losses'] for d in done)
    prof = sum(1 for d in done if d['pnl'] > 1e-9)
    loss = sum(1 for d in done if d['pnl'] < -1e-9)
    n_addon = sum(d.get('addon', 0) for d in days)
    n_void = sum(d.get('void', 0) for d in days)
    L = []
    A = L.append
    A('# 每日交易建议 · 盈亏台账\n')
    A('> 记录**建议口径**的结算盈亏（不是成交口径）。每条腿取「首推价 + 建议仓位」，')
    A('> 同腿后续加仓 / 改价不重复计入。档位 `N°C = [N.0, N+1.0)`。')
    A('> 本文件由 `py -3 scripts/rec_pnl.py --markdown` 生成，数据源 `scripts/data/rec_pnl_log.csv`。\n')
    A(f'**累计战绩：{prof} 盈 / {loss} 亏 天　净盈亏 {money(tpnl)}　'
      f'投入 {money(tc)}　ROI {pct(tpnl / tc if tc else 0)}　逐腿 {tw}-{tl}**\n')
    A(f'> **口径由 v0.17.0 起由脚本强制**：同一（结算日 + 腿 + 方向 + 档位）只取首次入账；')
    A(f'> 同腿加仓 / 改价 **{n_addon} 条**、作废 / 撤回 / 未发出 **{n_void} 条** 一律不计入，')
    A(f'> 但仍逐条列在下面的明细里（标 `↳ 加仓` / `⬜ 作废`），`--all-legs` 可看全量流水。\n')
    A('## 逐日\n')
    A('| 结算日 | 结算档 | 腿 | 胜-负 | 投入 | 回收 | 净盈亏 | ROI | 当日 |')
    A('|---|---|---:|---:|---:|---:|---:|---:|:--:|')
    for d in days:
        mark = ' *' if d['settle_status'] == 'provisional' else ''
        sb = f'{d["settle_bucket"]}°C{mark}' if d['settle_bucket'] else '—'
        wl = f'{d["wins"]}-{d["losses"]}' + (f'+{d["open"]}开' if d['open'] else '')
        mv = '—' if d['open'] else money(d['pnl'])
        mr = '—' if d['open'] else pct(d['roi'])
        A(f'| {d["date"]} | {sb} | {d["legs"]} | {wl} | {money(d["cost"])} | '
          f'{money(d["payout"])} | {mv} | {mr} | {d["verdict"]} |')
    A(f'| **合计** | | **{tw + tl}** | **{tw}-{tl}** | **{money(tc)}** | **{money(tp)}** | '
      f'**{money(tpnl)}** | **{pct(tpnl / tc if tc else 0)}** | '
      f'**{prof} 盈 / {loss} 亏** |')
    A('')
    A('`*` = 结算档暂定（按当日结算站已观测最高 + 市场价判定；官方 Daily Extract 隔日出）。\n')
    A('## 逐腿明细\n')
    A('| 结算日 | 腿 | 方向 | 档位 | 首推价 | 建议投入 | 结算档 | 结果 | 盈亏 | 备注 |')
    A('|---|---|:--:|---:|---:|---:|---:|:--:|---:|---|')
    for t, r in sorted(((tag[i], r) for i, r in enumerate(rows)),
                       key=lambda x: (x[1]['settle_date'], x[1]['leg'])):
        _px, c, _sh, _po, p = leg_econ(r)
        oc = (r.get('outcome') or '').strip().lower()
        if t == 'void':
            res, pv = '⬜ 作废', '—'
        elif t == 'addon':
            res, pv = '↳ 加仓', '—'
        elif oc == 'win':
            res, pv = '✅ 赢', money(p)
        elif oc == 'loss':
            res, pv = '❌ 输', money(p)
        else:
            res, pv = '⏳ 未结算', '—'
        px = r.get('entry_price') or '—'
        sb = f'{r["settle_bucket"]}°C' if r.get('settle_bucket') else '—'
        A(f'| {r["settle_date"]} | {r["leg"]} | {r["side"]} | {r["bucket"]}°C | {px} | '
          f'{money(c)} | {sb} | {res} | {pv} | {r.get("note") or ""} |')
    A('')
    A('## 说明\n')
    for ln in _header_lines():
        if ln.startswith('# 未纳入'):
            A('- ' + ln.lstrip('# ').strip())
    A(f'- 表中「投入」为**建议**仓位；`↳ 加仓` / `⬜ 作废` 的行**不计入**逐日与合计'
      f'（口径见 `SKILL.md`《每日建议台账》）；`--all-legs` 可关闭折叠。')
    A(f'- **负投入行 = 建议卖出**（回收现金）：其盈亏按空头口径算（卖出价 vs 结算价 1.0），')
    A(f'  所以会出现「主表记 ✅ 赢、金额却是小负数」的组合。')
    A(f'- 数据源与口径见 `SKILL.md` 的《每日建议台账》一节；逐时点的「模型 vs 市场」Brier 见 '
      f'`py -3 scripts/review_brier.py`。')
    A(f'- 更新：`py -3 scripts/rec_pnl.py --settle YYYY-MM-DD=N --write && '
      f'py -3 scripts/rec_pnl.py --markdown`')
    A('- 金额为建议仓位；实际成交量受订单簿深度限制，通常小于建议额，故本表是**收益上限口径**。')
    A('- 本文不构成投资建议。')
    A('')
    with open(MD, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    return MD


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description='每日交易建议的结算盈亏台账')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    ap.add_argument('--open', action='store_true', help='只列未结算的腿')
    ap.add_argument('--settle', default='',
                    help='手工回填结算档，格式 "2026-09-12=32,2026-09-13=31"（近期日唯一可靠途径）')
    ap.add_argument('--status', default='confirmed', choices=['confirmed', 'provisional'],
                    help='回填时写入的 settle_status（默认 confirmed；官方源未出时用 provisional）')
    ap.add_argument('--auto', action='store_true',
                    help='用 maxt_HKO.csv（结算源，滞后约 10 天）回填缺失的结算档')
    ap.add_argument('--write', action='store_true', help='把回填结果写回 CSV（默认只试算）')
    ap.add_argument('--markdown', action='store_true', help='重新生成仓库根目录 PNL.md')
    ap.add_argument('--all-legs', action='store_true',
                    help='不折叠同腿加仓（看全量建议流水；默认按「同腿只记首推」口径）')
    a = ap.parse_args()

    if not os.path.exists(LOG):
        sys.exit(f'找不到台账文件：{LOG}')
    rows = load_rows()
    header = _header_lines()

    mapping = {}
    if a.auto:
        mapping.update(bucket_of_day())
    for part in [x for x in a.settle.split(',') if x.strip()]:
        k, _, v = part.partition('=')
        try:
            mapping[k.strip()] = int(float(v))
        except ValueError:
            sys.exit(f'--settle 格式错误：{part!r}，应为 DATE=BUCKET')

    if mapping:
        ch = settle(rows, mapping, status=a.status, only_missing=not a.settle)
        nv = sum(1 for c in ch if c['value_changed'])
        print(f'回填 {len(ch)} 条记录（档位/胜负变更 {nv} 条，其余仅改 settle_status）'
              + ('（已写回 CSV）' if a.write else '（试算，未落盘）'))
        for c in ch:
            arrow = f"{c['old_outcome']} → {c['new_outcome']}" if c['value_changed'] \
                else '胜负不变'
            st = f"，status {c['old_status']} → {c['new_status']}" \
                if c['old_status'] != c['new_status'] else ''
            print(f"  {c['date']} {c['leg']:<6} {arrow}（结算 {c['bucket']}°C{st}）")
        print()
        if a.write:
            save_rows(rows, header)

    tag = classify(rows, all_legs=a.all_legs)

    if a.open:
        print_open(rows, tag)
        return

    days = rollup(rows, tag, skip_counts(rows, tag))

    if a.json:
        print(json.dumps({'days': days, 'side': by_side(rows, tag)}, ensure_ascii=False, indent=2))
        return

    print_report(rows, days, tag)

    if a.markdown:
        p = write_markdown(rows, days, tag, all_legs=a.all_legs)
        print(f'已写出 {p}')


if __name__ == '__main__':
    main()
