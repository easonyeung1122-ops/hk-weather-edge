#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""market_shape.py — 盘口形状的**规范化**正态拟合与 RMS 守卫。

为什么单独做一个脚本：2026-09-21 10:40 轮用**错误的 RMS 算法**得出「市场形状不可用
（RMS 74%）」，据此弃用市场隐含 σ、改用站点 σ，把一笔入场 edge 从 +2.7% 吹成 +11.6%
（详见 SKILL.md §2.2-硬规则 24）。本脚本把正确算法固化下来，避免再手算：

  1. mid 先**归一化**到 sum=1；
  2. 拟合与 RMS **用同一组桶**（默认只取流动性桶，见 --buckets）；
  3. 输出必带**桶集**与**归一化前 sum**，便于复核；
  4. RMS > 3% 才允许打印「形状不可用」的警告。

用法：
    py -3 scripts/market_shape.py --market "31:0.12,32:0.49,33:0.355,34:0.025"
    py -3 scripts/market_shape.py --date 2026-09-21          # 现拉订单簿
    py -3 scripts/market_shape.py --date 2026-09-21 --json    # 机器可读

输出：拟合 μ / σ / RMS、逐桶对照、每档 YES 与 NO 的**市场自评公允价**。
"""
import argparse
import json
import math
import os
import sys

RC_USABLE = 3.0          # RMS 阈值（%）：≤ 视为形状可用，见硬规则 24


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def parse_market(s):
    """'31:0.12,32:0.49' → {31: 0.12, 32: 0.49}（键允许 '<=29' / '35+' 形式）"""
    out = {}
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        k, v = part.split(":")
        k = k.strip()
        out[k] = float(v)
    return out


def bucket_bounds(key):
    """档位键 → (下界, 上界)。'N°C' 表示 [N, N+1)。"""
    k = key.strip().replace("°C", "").replace("C", "").strip()
    if k.startswith("<="):
        return (-99.0, float(k[2:]) + 1.0)
    if k.endswith("+"):
        return (float(k[:-1]), 99.0)
    n = float(k)
    return (n, n + 1.0)


def bucket_sort_key(key):
    return bucket_bounds(key)[0]


def fit(mid, buckets, mu_lo=20.0, mu_hi=45.0, sd_lo=0.20, sd_hi=4.0):
    """在给定桶集上做正态 LSQ。返回 (rms, mu, sd, {key: p_fit})，RMS 为**归一化后**的绝对差。"""
    total = sum(mid[k] for k in buckets)
    norm = {k: mid[k] / total for k in buckets}
    best = None
    mu = mu_lo
    while mu <= mu_hi:
        sd = sd_lo
        while sd <= sd_hi:
            p = {}
            for k in buckets:
                lo, hi = bucket_bounds(k)
                p[k] = phi((hi - mu) / sd) - phi((lo - mu) / sd)
            rms = math.sqrt(sum((p[k] - norm[k]) ** 2 for k in buckets) / len(buckets))
            if best is None or rms < best[0]:
                best = (rms, mu, sd, p)
            sd += 0.01
        mu += 0.01
    return best + (norm,)


def fetch_book(date_iso):
    """复用 market_prices 的取数逻辑，返回 {档位键: YES mid}。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import market_prices as mp          # noqa: E402
    import urllib.parse                 # noqa: E402

    y, m, d = date_iso.split("-")
    slug = "highest-temperature-in-hong-kong-on-%s-%d-%s" % (mp.MONTHS[int(m) - 1], int(d), y)
    ev = mp.get(mp.GAMMA % urllib.parse.quote(slug))
    if not ev:
        raise SystemExit("[err] 找不到事件 %s" % slug)
    e = ev[0]
    pairs = []
    for mk in e.get("markets", []):
        label = mk.get("groupItemTitle") or "?"
        try:
            toks = json.loads(mk.get("clobTokenIds") or "[]")
        except Exception:
            toks = []
        if len(toks) >= 2:
            pairs.append((label, toks[0], toks[1]))
    ids = [t for _, y_, n in pairs for t in (y_, n)]
    batch = mp.books_batch(ids) if ids else {}
    out = {}
    for label, ytok, _ in pairs:
        key = label.replace(" or below", "").replace(" or higher", "+").replace("°C or below", "")
        key = key.replace("°C or higher", "+").replace("°C", "")
        lv = mp._levels(batch.get(str(ytok), []), 1)
        if not lv:
            continue
        bid = lv[0][0]
        out[key] = bid      # 用 bid 做保守的 YES 定价（mid 需 ask，见 market_prices）
    return out, e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", help='例如 "31:0.12,32:0.49,33:0.355,34:0.025"')
    ap.add_argument("--date", help="香港日期 YYYY-MM-DD（现拉订单簿，取 YES bid 作保守价）")
    ap.add_argument("--buckets", help="参与拟合的桶，逗号分隔；默认取 mid≥0.005 且相邻的连续段")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    src = "命令行 --market"
    if a.market:
        mid = parse_market(a.market)
    elif a.date:
        mid, _e = fetch_book(a.date)
        src = "订单簿 %s（YES bid）" % a.date
    else:
        raise SystemExit("[err] 需要 --market 或 --date")

    # 桶集：显式给定，或自动取「概率质量 99% 的连续段」，绝不混用不同支撑集
    if a.buckets:
        buckets = [b.strip() for b in a.buckets.split(",")]
    else:
        keys = sorted(mid, key=bucket_sort_key)
        thr = 0.005         # 薄桶（<0.5%）不进拟合集：它们没有信息，只会抬高 RMS
        idx = [i for i, k in enumerate(keys) if mid[k] >= thr]
        lo, hi = (min(idx), max(idx)) if idx else (0, len(keys) - 1)
        buckets = keys[lo:hi + 1]

    total_raw = sum(mid.values())
    rms, mu, sd, fitp, norm = fit(mid, buckets)

    if a.json:
        print(json.dumps({"source": src, "buckets": buckets, "sum_raw": round(total_raw, 4),
                          "mu": round(mu, 2), "sd": round(sd, 2),
                          "rms_pct": round(rms * 100, 2), "usable": rms * 100 <= RC_USABLE,
                          "fair": {k: round(fitp[k], 4) for k in buckets}}, ensure_ascii=False))
        return

    print("源: %s" % src)
    print("桶集（拟合与 RMS 同一支撑集，共 %d 桶）: %s" % (len(buckets), ", ".join(buckets)))
    print("归一化前 sum = %.4f  %s" % (total_raw,
          "⚠ 偏离 1 较多，必须归一化后再比较" if abs(total_raw - 1) > 0.02 else "(≈1)"))
    print()
    print("拟合：μ = %.2f °C   σ = %.2f °C   RMS = %.2f%%   → %s"
          % (mu, sd, rms * 100, "形状可用" if rms * 100 <= RC_USABLE else "⚠ 形状不可用"))
    print()
    print("%-8s %9s %9s %9s %10s %10s" % ("桶", "mid(归一)", "fit", "差 pp", "NO 公允", "YES 公允"))
    for k in buckets:
        print("%-8s %9.4f %9.4f %+9.2f %10.4f %10.4f"
              % (k, norm[k], fitp[k], (fitp[k] - norm[k]) * 100, 1 - fitp[k], fitp[k]))
    if rms * 100 > RC_USABLE:
        print()
        print("⚠ RMS %.2f%% > %.1f%%：先按硬规则 24 用**相邻另一批盘口交叉验证**，"
              "确认两批都拟不上再判「不可用」——2026-09-21 的 74% 就是算法错误。" % (rms * 100, RC_USABLE))


if __name__ == "__main__":
    main()
