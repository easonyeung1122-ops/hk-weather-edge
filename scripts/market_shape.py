#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""market_shape.py — 盘口形状的**规范化**正态拟合与 RMS 守卫。

Why this script exists: on 2026-09-21 10:40 a round used a **wrong RMS algorithm** and concluded
"market shape unusable (RMS 74%)", discarded the market-implied sigma for a site sigma, and inflated
an entry edge from +2.7% to +11.6% (see SKILL.md section 2.2 hard rule 24). The wrong figure came from a
**relative-error metric** (residual divided by bucket mid): the thinnest bucket, 34C (only 1.8% of mass),
alone contributed **99.4%** of the sum of squares and pushed 1.46% up to 64.7%. This script fixes the
canonical algorithm so it is never hand-computed again:

  1. mid is **normalised** to sum=1 first;
  2. fit and RMS use the **same bucket set** (default: liquid buckets only, see --buckets);
  3. output always carries the **bucket set**, the **pre-normalisation sum**, and
     **per-bucket contribution to the residual sum of squares**;
  4. a bucket contributing > 50% is named explicitly;
  5. only RMS > 3% prints the "shape unusable" warning.

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
    """复用 market_prices 的取数逻辑，返回 {档位键: YES mid}。

    v0.22.1 修复两处缺陷（2026-09-21 实测 --date 直接崩溃 / 支撑集被静默砍掉）：

    1. `mp._levels()` 返回 **(bids, asks) 二元组**，旧代码当成单个列表用，
       `if not lv` 对空档位永不触发 → `lv[0][0]` 抛 IndexError。
       本脚本 v0.22.0 上线后 **--date 路径从未真正跑通过**。
    2. 旧代码只取 **YES bid**：当天 10 档里有 6 档完全没有 YES 买单（显示 `-`），
       这些档被 `continue` **静默丢弃** → 支撑集被削 → 违反硬规则 24 自己的前提。
       做**形状**拟合要的是两侧中间价，与**执行**取价（须用可成交的 ask/bid）是两回事。

    定价优先级：YES (bid+ask)/2 → YES 单边 → 1 − NO (bid+ask)/2 → 1 − NO 单边。
    """
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

    def side(token):
        """→ (best_bid, best_ask)；任一侧缺失则为 None。"""
        bids, asks = mp._levels(batch.get(str(token), {}) or {}, 1)
        bb = bids[0][0] if bids else None
        ba = asks[0][0] if asks else None
        return bb, ba

    out, dropped = {}, []
    for label, ytok, ntok in pairs:
        key = (label.replace("°C or below", "").replace(" or below", "")
               .replace("°C or higher", "+").replace(" or higher", "+")
               .replace("°C", "").strip())
        yb, ya = side(ytok)
        nb, na = side(ntok)
        px = None
        if yb is not None and ya is not None:
            px = (yb + ya) / 2.0
        elif yb is not None:
            px = yb
        elif ya is not None:
            px = ya
        elif nb is not None and na is not None:
            px = 1.0 - (nb + na) / 2.0
        elif nb is not None:
            px = 1.0 - nb
        elif na is not None:
            px = 1.0 - na
        if px is None:
            dropped.append(key)
            continue
        out[key] = px
    if dropped:
        print("[warn] 两侧订单簿全空、已跳过: %s" % ", ".join(dropped), file=sys.stderr)
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
        src = "订单簿 %s（YES/NO 两侧中间价）" % a.date
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
    print("%-8s %9s %9s %9s %10s %10s %8s" % ("桶", "mid(归一)", "fit", "差 pp", "NO 公允", "YES 公允", "贡献%"))
    ss = sum((fitp[k] - norm[k]) ** 2 for k in buckets) or 1e-12
    for k in buckets:
        print("%-8s %9.4f %9.4f %+9.2f %10.4f %10.4f %7.1f%%"
              % (k, norm[k], fitp[k], (fitp[k] - norm[k]) * 100, 1 - fitp[k], fitp[k],
                 (fitp[k] - norm[k]) ** 2 / ss * 100))
    # 单桶主导守卫：绝对残差口径下也会被某个薄桶带偏，必须点名
    top = max(buckets, key=lambda k: (fitp[k] - norm[k]) ** 2)
    share = (fitp[top] - norm[top]) ** 2 / ss * 100
    if share > 50:
        print()
        print("⚠ 单桶主导：`%s` 档占总残差平方和 **%.0f%%**（mid %.4f → fit %.4f）。"
              % (top, share, norm[top], fitp[top]))
        print("  该桶只占 %.1f%% 质量，RMS 由它独撑 —— 报 RMS 前须说明，"
              "并检查是否该把它移出拟合桶集（--buckets）。" % (norm[top] * 100))
    if rms * 100 > RC_USABLE:
        print()
        print("⚠ RMS %.2f%% > %.1f%%：先按硬规则 24 用**相邻另一批盘口交叉验证**，"
              "确认两批都拟不上再判「不可用」——2026-09-21 的 74%% 就是算法错误。"
              % (rms * 100, RC_USABLE))


if __name__ == "__main__":
    main()
