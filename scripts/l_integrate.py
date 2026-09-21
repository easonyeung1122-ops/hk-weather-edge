#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L 积分器：把「当日水位 L」当随机量积掉，输出档位公允概率 + 组合回收矩阵 + 头寸检查。

为什么需要它
------------
`hk_edge.py --watch` 只用**一个** L（整点对齐中位）给出点位估计与 one-touch 概率。
但 2026-09-15 实测：09h 的 L=+0.23、10h 的 L=−0.95 —— **一小时摆动 1.18°C**，
L 自身的 1σ ≈ 0.6°C，与脚本残差 sd 0.76 同量级。
选一个 L 报出去，等于把一半的预测不确定性藏起来。

本脚本对 L ~ N(L_hat, sd_L) 积分，并对每个 L：
  · 经验 one-touch 口径：`remaining_rise_cdf.json` 上尾（把模式升水 δ 当随机量）
  · 正态截断口径：N(max(mx, g_rest+L), resid_sd) 截断到 [已观测最高, ∞)
两口径各半混合 → 得到「模型公允」。
再把市场 mid 归一化后按 25% 权重收缩（抑制过度自信），得到「收缩公允」。

同时反解**市场隐含 L**（让模型分布最贴近市场归一价的那个 L）——
这是「我们与市场的分歧有多大」的单一量化口径。

用法
----
    py -3 scripts/l_integrate.py --observed-max 28.6 --current 28.6 --hour 10 \
        --market "28:0.076,29:0.290,30:0.375,31:0.255,32:0.055,33:0.015" \
        --hold "29:0.700:172:N,29:0.290:185:Y,30:0.340:17:Y,31:0.740:338:N,32:0.060:282:Y"

所有概率口径说明见 SKILL.md「L 积分」一节。
"""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hk_edge as H  # noqa: E402

DATA = H.DATA
RESID_SD_DEFAULT = 0.76
# ⚠ v0.18.0：0.60 这个数**没有统计支持** —— 它来自 2026-09-15 的 09h/10h **两个**整点样本
# 的摆动（n=2）。按「禁用 n≤5 样本内 sd」的纪律，它只能算占位值，不许当校准结果引用。
# 保留原值是为了不静默改变输出；替换路径已列入 P2：L 的 sd 应改由**当日 devs 序列的
# 稳健离散度**（1.4826×MAD，双边对齐后 n≥10）估计，前提是 obs_1min_archive 攒够。
# 在替换之前，任何「L 很稳」的表述都不成立。
SD_L_DEFAULT = 0.60
MKT_WEIGHT = 0.25            # 市场收缩权重

# ⚠ 口径必须统一（2026-09-15 11:20 发现并修复）：
# `model_calib.json` 的 bias/resid_sd 是拿**7 个 NWP 模式的平均日最高**校准的，
# 而 `hk_edge.OM_HR` 用的是 Open-Meteo 的 `best_match`（加权混合，今天就比 7 模式
# 平均高出 +0.79°C）。把 best_match 的 g_rest 配上 7 模式平均的 bias 做收缩，
# 会**系统性高估峰值 0.79°C** —— 正好横跨 30/31 档分界线。
# 因此默认改用同一批模式的平均值作网格基线。
OM_HR_MULTI = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               "&hourly=temperature_2m&forecast_days=1&past_hours={ph}"
               "&timezone=Asia%2FShanghai&models=" + H.MODELS)
GRID_SRC_DEFAULT = 'multimodel'

# —— v0.16.21：把网格口径的 R 表折算到站点口径 ——
# `remaining_rise_cdf.json` 是 ERA5 25km 网格口径，网格把单点日内尖峰平滑掉，
# 「13 时之后还能升多少」被压得比真实站点小得多。同点位实测（VHHH 站 vs ERA5@VHHH，
# 2015–2025 九月，n≈330）：日较差只差 1.39 倍，而 R13 差 **2.41 倍** —— 网格不是
# 等比压缩，它专吃尖峰。用 `scripts/station_r_probe.py` 复算。
# ⚠ 不要拿「ERA5@HKO 点 vs VHHH 站」相除（会得到 4.7），那混进了站点差。
R_SCALE_DEFAULT = 2.4
# 站点已从当日峰值回落 ≥0.5°C 时，剩余升温概率腰斩（VHHH 9 月：51.5% → 25.8%）
PULLBACK_FACTOR = 0.5
PULLBACK_MIN_GAP = 0.5


def norm_pdf(x, mu, s):
    return math.exp(-0.5 * ((x - mu) / s) ** 2) / (s * math.sqrt(2 * math.pi))


def norm_cdf(x, mu, s):
    return 0.5 * (1 + math.erf((x - mu) / (s * math.sqrt(2))))


def grid_series(hour, date_iso, grid_src):
    """返回 {hour: 网格温度}。grid_src='multimodel' 时用 7 模式平均（与 bias 同口径）。"""
    ph = max(3, hour + 1)
    if grid_src == 'best_match':
        oh = H._get(H.OM_HR.format(lat=H.OBS_LAT, lon=H.OBS_LON, ph=ph), 30)
        hr = oh['hourly']
        cols = ['temperature_2m']
    else:
        oh = H._get(OM_HR_MULTI.format(lat=H.OBS_LAT, lon=H.OBS_LON, ph=ph), 30)
        hr = oh['hourly']
        cols = [k for k in hr if k.startswith('temperature_2m_')
                or k == 'temperature_2m']
    grid = {}
    for i, t in enumerate(hr['time']):
        if not t.startswith(date_iso):
            continue
        vals = [hr[c][i] for c in cols
                if hr.get(c) and i < len(hr[c]) and hr[c][i] is not None]
        if vals:
            grid[int(t[11:13])] = sum(float(v) for v in vals) / len(vals)
    return grid


def load_context(observed_max, current, hour, date_iso=None, grid_src=GRID_SRC_DEFAULT):
    """装配 one-touch 所需的全部输入（与 hk_edge.watch() 同口径）。"""
    import datetime as dt
    date_iso = date_iso or dt.datetime.now(H.TZ).date().isoformat()
    calib = json.load(open(os.path.join(DATA, 'model_calib.json')))
    rcdf = json.load(open(os.path.join(DATA, 'remaining_rise_cdf.json')))
    tbl = (rcdf.get('by_month', {}).get(str(int(date_iso[5:7])), {}).get(str(hour))
           or rcdf.get('by_hour', {}).get(str(hour)))
    grid = grid_series(hour, date_iso, grid_src)
    g_rest = max((v for h, v in grid.items() if h >= hour), default=None)
    return calib, tbl, grid, g_rest


def bucket_probs_at_L(L, ctx):
    """给定单个 L，返回 {档位: 概率}（经验 one-touch 与正态截断各半）。"""
    calib, tbl, g_rest, mx, cur, resid_sd = ctx[:6]
    r_scale = ctx[6] if len(ctx) > 6 else R_SCALE_DEFAULT
    pred = max(mx, g_rest + L)
    rho_hat = (g_rest + L) - mx                    # 站点口径：模型预测峰值 − 已观测最高
    overshoot = mx - (g_rest + L)                  # >0 = 站点已越过网格全天峰值
    if overshoot > 0:
        # 🚨 v0.16.21：网格自己的峰值低于实况 → 它的「午后塌陷」是它自己的，
        #    δ 不再是关于今天的信号。旧实现让 δ 被钳到 −1.00（下限），
        #    等于断言「今天比气候少升 1°C」，把一个口径失效转成了硬惩罚。
        #    此时退回**纯气候**（δ=0），只保留站点/网格放大比。
        delta_g = 0.0
    else:
        # δ 在网格口径上算：先把站点的 ρ̂ 折成网格刻度，再减网格表的均值
        delta_g = max(-1.0, min(1.0, rho_hat / r_scale - tbl.get('mean', 0.0)))
    delta = delta_g * r_scale                      # 对外仍报站点口径的等效 δ

    # —— 经验 one-touch：P(越线 k.0) = P(R ≥ (k − running_max)/k_R − δ) ——
    # 两点口径（v0.16.21 修正）：
    #   ① base 必须是 **running max**，不是当前瞬时值 —— 分位表的 R = M − RM(h)；
    #      用瞬时值会把「已经回落过」当成「还要升那么多」。
    #   ② R 阈值要除以放大比 k_R：表是网格口径，站点口径的尖峰比网格尖 k_R 倍。
    emp, b = {}, int(math.floor(mx))
    # 回落条件：站点已从当日峰值回落 ≥0.5°C 时，剩余升温概率**腰斩**
    # （VHHH 9 月实测 51.5% → 25.8%，n=330/120；v0.16.21）
    pf = PULLBACK_FACTOR if (mx - cur) >= PULLBACK_MIN_GAP else 1.0
    reach = {}
    for k in range(b, b + 8):
        if k <= mx:
            reach[k] = 1.0
        else:
            need_g = (k - mx) / r_scale
            reach[k] = pf * H.cdf_upper_delta(tbl, need_g - delta_g,
                                              H.DELTA_REL_SIGMA * abs(delta_g))
    for k in range(b, b + 7):
        emp[k] = max(0.0, reach[k] - reach[k + 1])

    # —— 正态截断：N(pred, resid_sd) 截断到 [mx, ∞) ——
    # v0.18.0：核由正态改 t(5)（H.t_cdf），与 hk_edge 的主公允表、以及上面的
    # 经验支路（H.cdf_upper_delta 内部已带 σ 下限 + t 核）保持同源。
    # 可靠性图：预测 10–20% 的档实际命中 26.7%，两端都比正态厚。
    nrm, lo, tot = {}, int(math.floor(mx)), 0.0
    for k in range(lo, lo + 9):
        lo_edge = max(k, mx)
        if lo_edge >= k + 1:
            nrm[k] = 0.0
            continue
        p = H.t_cdf(k + 1, pred, resid_sd) - H.t_cdf(lo_edge, pred, resid_sd)
        nrm[k] = p
        tot += p
    if tot > 0:
        nrm = {k: v / tot for k, v in nrm.items()}

    keys = sorted(set(emp) | set(nrm))
    mix = {k: 0.5 * emp.get(k, 0.0) + 0.5 * nrm.get(k, 0.0) for k in keys}
    s = sum(mix.values())
    return ({k: v / s for k, v in mix.items()} if s > 0 else mix), pred, delta


def integrate(L_hat, sd_L, ctx, ns=61, span=2.4):
    """L ~ N(L_hat, sd_L) 积分。"""
    acc, wsum, rows = {}, 0.0, []
    for i in range(ns):
        L = L_hat + (i - (ns - 1) / 2.0) / ((ns - 1) / 2.0) * span
        w = norm_pdf(L, L_hat, sd_L)
        pr, pred, delta = bucket_probs_at_L(L, ctx)
        rows.append((L, w, pred, delta, pr))
        for k, v in pr.items():
            acc[k] = acc.get(k, 0.0) + w * v
        wsum += w
    return {k: v / wsum for k, v in acc.items()}, rows


def solve_market_L(mkt_norm, ctx, lo=-2.5, hi=2.5, step=0.01):
    """反解市场隐含 L：让模型分布（不积分，用单点 L）最贴近市场归一价。"""
    best, best_err = None, 1e9
    n = int((hi - lo) / step) + 1
    for i in range(n):
        L = lo + i * step
        pr, _, _ = bucket_probs_at_L(L, ctx)
        err = 0.0
        for k, pm in mkt_norm.items():
            err += (pr.get(k, 0.0) - pm) ** 2
        if err < best_err:
            best, best_err = L, err
    return best, math.sqrt(best_err)


def mean_from_buckets(probs, floor_value, hi=None):
    """由档位概率反算日最高的期望值 —— 必须用「尾和积分」，不能用 Σ k·P_k。

    Σ k·P_k 隐含「档内质量均布在 k+0.5」，但紧贴现测值的档位质量集中在**下缘**
    （28 档里 90% 的质量在 28.0–28.4），会把期望值系统性拉低 0.4–0.5°C
    （2026-09-15 实测：Σk·P_k = 29.59，尾和积分 = 30.06，而模型的点估计是 30.04
     —— 尾和积分才是对的）。
    这里用 E = floor_value + Σ_k ∫_k^{k+1} P(max≥x) dx，梯形近似。
    """
    if not probs:
        return None
    lo = min(probs)
    reach = {}
    ks = sorted(probs)
    for k in ks:
        reach[k] = sum(probs[j] for j in ks if j >= k)
    # 从实测值到第一个整数线的局部段
    first = min(k for k in ks if k > floor_value) if any(
        k > floor_value for k in ks) else lo
    tot = (first - floor_value) * (1.0 + reach.get(first, 0.0)) / 2.0
    top = (hi or (max(ks) + 6))
    for k in range(first, top):
        r0 = reach.get(k, 0.0)
        r1 = reach.get(k + 1, 0.0)
        tot += (r0 + r1) / 2.0
    return floor_value + tot


def kelly(p, c, frac=0.35):
    if c <= 0 or c >= 1:
        return 0.0
    f = p - (1 - p) * c / (1 - c)       # 二元合约标准 Kelly
    return max(0.0, frac * f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--observed-max', type=float, required=True)
    ap.add_argument('--current', type=float, required=True)
    ap.add_argument('--hour', type=int, required=True, help='观测时刻 HKT 整点')
    ap.add_argument('--l-hat', type=float, default=None, help='L 点估计（默认取 --bias）')
    # v0.18.0：默认值由 -0.36 改为 None。旧默认是 **2026-09-15 当天的水位硬编码**，
    # 等于把某一天的结果写成所有日子的默认输入 —— 典型的「运气变默认值」。
    # 现在缺省回落到 model_calib 的气候先验 bias（+1.008），语义唯一、不含任何单日值。
    ap.add_argument('--bias', type=float, default=None,
                    help='L 点估计（双边对齐中位，v0.18.0 起；缺省 = 气候先验 bias）')
    ap.add_argument('--sd-l', type=float, default=SD_L_DEFAULT)
    ap.add_argument('--resid-sd', type=float, default=RESID_SD_DEFAULT)
    ap.add_argument('--market', default='', help='YES 中间价（用于一致性检验）"28:0.076,29:0.290"')
    ap.add_argument('--book', default='',
                    help='YES 买一/卖一 "29:0.270/0.310,30:0.360/0.390"（下单/清算必须用它）')
    ap.add_argument('--bankroll', type=float, default=1000)   # 常规本金 1000（硬规则 25 / v0.23.0；当日另行指定则显式传入）
    ap.add_argument('--grid', default=GRID_SRC_DEFAULT,
                    choices=('multimodel', 'best_match'),
                    help='网格基线口径；必须与 model_calib.json 的 bias 同源（默认 multimodel）')
    ap.add_argument('--hold', default='')
    ap.add_argument('--date', default=None)
    ap.add_argument('--json-out', default=None)
    ap.add_argument('--r-scale', type=float, default=R_SCALE_DEFAULT,
                    help='站点 R / 网格 R 放大比（v0.16.21，默认 2.4；1.0 = 退回旧口径）')
    a = ap.parse_args()

    calib, tbl, grid, g_rest = load_context(a.observed_max, a.current, a.hour, a.date,
                                            a.grid)
    # L 缺省 = 气候先验 bias（v0.18.0）。用 `is not None` 判空：0.0 是合法水位，
    # 旧写法 `a.bias if a.bias else` 会把 0.0 当成「没给」。
    L_hat = (a.l_hat if a.l_hat is not None
             else (a.bias if a.bias is not None else calib['bias']))
    resid_sd = a.resid_sd if a.resid_sd else calib['resid_sd']
    ctx = (calib, tbl, g_rest, a.observed_max, a.current, resid_sd, a.r_scale)
    obs_h_after = a.hour
    grid_after = max((v for h, v in grid.items() if h >= obs_h_after), default=None)

    print('=' * 78)
    print(f"L 积分器  |  观测 {a.current:.1f}°C @ {a.hour:02d}h  已观测最高 {a.observed_max:.1f}°C")
    print(f"L ~ N({L_hat:+.2f}, {a.sd_l:.2f})   剩余网格最高 g_rest = {g_rest:.2f}°C"
          f"   残差 sd = {resid_sd:.2f}   分位表 n = {tbl['n']}")
    print(f"网格口径 = {a.grid}（bias {calib['bias']:+.3f} 与 resid_sd {calib['resid_sd']:.3f}"
          f" 均按 7 模式平均校准 → 必须同源）")
    print('=' * 78)

    pr_model, rows = integrate(L_hat, a.sd_l, ctx)
    pr_point, pred_point, delta_point = bucket_probs_at_L(L_hat, ctx)
    # 点估计（不积分）
    pred_det = max(a.observed_max, g_rest + L_hat)
    print(f"\n单点口径：峰值 {pred_det:.2f}°C  众数档 {int(math.floor(pred_det))}°C   "
          f"δ = {delta_point:+.2f}")

    mkt = {}
    if a.market:
        for it in a.market.split(','):
            k, v = it.split(':')
            mkt[int(k)] = float(v)
    # YES 买一/卖一；NO 的买价 = 1 − YES 卖一，NO 的卖价 = 1 − YES 买一
    ybid, yask = {}, {}
    if a.book:
        for it in a.book.split(','):
            k, v = it.split(':')
            b, s = v.split('/')
            ybid[int(k)], yask[int(k)] = float(b), float(s)
    if not mkt and ybid:
        mkt = {k: (ybid[k] + yask[k]) / 2 for k in ybid}
    mkt_norm = {}
    if mkt:
        s = sum(mkt.values())
        mkt_norm = {k: v / s for k, v in mkt.items()}

    print(f"\n{'档':>4} {'单点L':>8} {'L积分':>8} {'收缩':>8} {'市场norm':>9} {'差(积分-市场)':>13}")
    shrink = {}
    for k in sorted(set(pr_model) | set(mkt_norm)):
        pm, pp = pr_model.get(k, 0.0), pr_point.get(k, 0.0)
        mk = mkt_norm.get(k, 0.0)
        sk = (1 - MKT_WEIGHT) * pm + MKT_WEIGHT * mk
        shrink[k] = sk
        d = (pm - mk) * 100 if mkt_norm else 0.0
        print(f"{k:>3}° {pp:>8.1%} {pm:>8.1%} {sk:>8.1%} {(f'{mk:.1%}' if mkt_norm else '-'):>9} "
              f"{d:>+13.1f}")

    if mkt_norm:
        Lm, rms = solve_market_L(mkt_norm, ctx)
        print(f"\n市场隐含 L = {Lm:+.2f}°C（拟合 RMS {rms:.1%}）｜ 我方 L = {L_hat:+.2f}°C"
              f" → 分歧 {L_hat - Lm:+.2f}°C")
        mod_mu = mean_from_buckets(pr_model, a.observed_max)
        mkt_mu = mean_from_buckets(mkt_norm, a.observed_max)
        print(f"中心（尾和积分）：模型 {mod_mu:.2f}°C  vs  市场 {mkt_mu:.2f}°C"
              f"  → 差 {mod_mu - mkt_mu:+.2f}°C")
        print(f"  点估计 g_rest + L = {g_rest:.1f} + ({L_hat:+.2f}) = {g_rest + L_hat:.2f}°C"
              f"；市场隐含 L = {Lm:+.2f} → 市场隐含峰值 {g_rest + Lm:.2f}°C")

    # —— 头寸 ——
    holds = []
    for it in [x for x in a.hold.split(',') if x.strip()]:
        ps = [y for y in it.split(':') if y]
        b, ent, sh = int(ps[0]), float(ps[1]), float(ps[2])
        side = (ps[3] if len(ps) > 3 else 'Y').upper()
        holds.append({'b': b, 'ent': ent, 'sh': sh, 'side': side})

    if holds and pr_model:
        print(f"\n■ 头寸检查（Brier 口径：公允 = L积分；收缩列只用于下单决策）")
        print(f"  {'腿':>9} {'份数':>6} {'成本':>7} {'卖价':>7} {'公允':>8} {'差(公允-卖价)':>14} "
              f"{'公允市值':>9} {'成本':>8}")
        mv_tot = cost_tot = fair_tot = 0.0
        for hd in holds:
            b, side = hd['b'], hd['side']
            p = pr_model.get(b, 0.0)
            fair = p if side == 'Y' else 1 - p
            bid = (ybid.get(b) if side == 'Y' else
                   (1 - yask[b]) if (side == 'N' and b in yask) else None)
            mk = mkt.get(b) if mkt else None
            if bid is None and mk is not None:
                bid = mk if side == 'Y' else 1 - mk
            mv = bid * hd['sh'] if bid is not None else 0.0
            ct = hd['ent'] * hd['sh']
            fv = fair * hd['sh']
            mv_tot += mv
            cost_tot += ct
            fair_tot += fv
            d = (fair - bid) if bid is not None else None
            print(f"  {str(b) + '° ' + ('YES' if side == 'Y' else 'NO'):>9} {hd['sh']:>6.0f} "
                  f"{hd['ent']:>7.3f} {(f'{bid:.3f}' if bid is not None else '-'):>7} "
                  f"{fair:>8.1%} {(f'{d:+.1%}' if d is not None else '-'):>14} "
                  f"{fv:>9.1f} {ct:>8.1f}")
        print(f"  {'合计':>9} {'':>6} {'':>7} {'':>7} {'':>8} {'':>14} "
              f"{fair_tot:>9.1f} {cost_tot:>8.1f}")
        print(f"  市值 ${mv_tot:.1f}（{mv_tot/a.bankroll:.1%} 本金）｜ 公允市值 ${fair_tot:.1f}"
              f"｜ 成本 ${cost_tot:.1f}")

        # —— 结算回收矩阵 ——
        print(f"\n■ 组合结算回收矩阵（L 积分公允）")
        levels = sorted(set(list(pr_model) + [int(math.floor(a.observed_max))]))
        print(f"  {'结算档':>7} {'模型P':>8} {'市场P':>8} {'回收':>9} {'vs市值':>10}")
        exp_rec = 0.0
        for k in levels:
            rec = 0.0
            for hd in holds:
                win = (hd['b'] == k) if hd['side'] == 'Y' else (hd['b'] != k)
                if win:
                    rec += hd['sh']
            pk = pr_model.get(k, 0.0)
            exp_rec += pk * rec
            vs = (rec / mv_tot - 1) if mv_tot else 0.0
            print(f"  {k:>5}° {pk:>8.1%} {(f'{mkt_norm.get(k,0):.1%}' if mkt_norm else '-'):>8} "
                  f"{rec:>9.0f} {vs:>+10.1%}")
        print(f"  {'期望':>7} {'':>8} {'':>8} {exp_rec:>9.1f} "
              f"{exp_rec/mv_tot-1:>+10.1%}")

        # —— 可出手腿 ranking ——
        if yask or mkt:
            print(f"\n■ 新开仓候选（限价 = 真实可成交价：买 YES 用 YES ask，买 NO 用 "
                  f"1 − YES bid）")
            print(f"  {'腿':>9} {'限价':>7} {'公允':>8} {'EV':>8} {'35%Kelly$':>10} {'份数':>7}")
            cands = []
            for k in sorted(pr_model):
                for side in ('Y', 'N'):
                    if side == 'Y':
                        price = yask.get(k, mkt.get(k))
                    else:
                        price = (1 - ybid[k]) if k in ybid else (
                            (1 - mkt[k]) if k in mkt else None)
                    if price is None or price <= 0.001 or price >= 0.999:
                        continue
                    p_sh = shrink.get(k, 0.0)
                    pt = p_sh if side == 'Y' else 1 - p_sh
                    ev = (pt - price) / price
                    f = kelly(pt, price)
                    cands.append((ev, k, side, price, pt, f))
            for ev, k, side, price, pt, f in sorted(cands, reverse=True)[:8]:
                tag = 'YES' if side == 'Y' else 'NO '
                print(f"  {str(k) + '° ' + tag:>9} {price:>7.3f} {pt:>8.1%} {ev:>+8.1%} "
                      f"{f*a.bankroll:>10.0f} {f*a.bankroll/price:>7.0f}")

    if a.json_out:
        json.dump({'model': pr_model, 'point': pr_point, 'shrink': shrink,
                   'market_norm': mkt_norm, 'L_hat': L_hat, 'sd_L': a.sd_l,
                   'pred_point': pred_det, 'delta_point': delta_point},
                  open(a.json_out, 'w'), indent=1)
        print(f"\n[写出] {a.json_out}")


if __name__ == '__main__':
    main()
