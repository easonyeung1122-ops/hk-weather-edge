#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cross_caliber.py — 跨口径横断 + 稳健性自动否决（v0.25.7 新增）

立规背景：2026-10-21 在 10:30-12:00 窗口内现场手写 LOOCV / 正规方程 / 分层统计，
共 35+ 次工具调用、两次因缓存键名写错整段返工，11:00 官方同步点已握全部决定性输入，
却拖到 11:16 才出结论。用户催：「为什么一个多小时都出不了一个结果」。
本脚本把该流程固化为**一条命令**，使窗口内不再需要任何现场标定代码。

用法（窗口内标准动作，11:00 官方同步点必须已跑完）：
    python cross_caliber.py --date 2026-10-21 --r11 30.5 --market "32:0.44,0.48;33:0.48,0.50;34:0.025,0.038"

    --date        目标日 YYYY-MM-DD
    --r11         官方结算站 running max @11:00（分区 CSV 与 text_readings 交叉核对）
    --market      各档 "档:bid,ask;档:bid,ask"（YES 价；缺省从 market_prices 现拉）
    --gpeak       7M 网格日高（缺省从 data/_ghourly92.json 读当日）
    --g1050       7M 网格 @10:50 插值（缺省同上）
    --topk        参与稳健性检验的 LOOCV RMSE 最优方法数（缺省 3）

输出：七方法 LOOCV 表 → 三条经验锚 → 合成 mu/sigma → EV 表 → 稳健性否决 → 最终决定。
硬规则 55：本脚本只允许跑一次；若失败两次即降级为当日不用横断，不得原地换参重跑。
"""
import argparse
import csv
import json
import math
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
ARC_OBS = os.path.join(DATA, "obs_1min_archive.json")
STRATA = os.path.join(DATA, "strata.csv")
GRID92 = os.path.join(DATA, "_ghourly92.json")
OFF_OVERRIDE = {"2026-10-20": 31.8}  # provisional 官方 1min Max Since Midnight


def load_recs():
    """历史样本：r11 / 网格日高 / 网格@10:50 / 官方终值。"""
    with open(ARC_OBS, encoding="utf-8") as f:
        arc = json.load(f)
    with open(STRATA, encoding="utf-8") as f:
        rows = {r["date"]: r for r in csv.DictReader(f)}
    off = {}
    for k, v in rows.items():
        if v.get("official_max"):
            try:
                off[k] = float(v["official_max"])
            except ValueError:
                pass
    off.update(OFF_OVERRIDE)
    grid = {}
    if os.path.exists(GRID92):
        with open(GRID92, encoding="utf-8") as f:
            grid = json.load(f)
    recs = []
    for k in sorted(arc):
        o = off.get(k)
        if o is None or k not in grid:
            continue
        pts = [p for p in arc[k] if p.get("t", "")[:10] == k and p.get("hko") is not None]
        if not pts:
            continue
        upto = [p["hko"] for p in pts if p["t"][11:16] <= "11:00"]
        if not upto:
            continue
        recs.append(dict(
            d=k, off=o, r11=max(upto),
            gmax=grid[k].get("gpeak"), g1050=grid[k].get("g1050"),
        ))
    return [r for r in recs if r["gmax"] is not None and r["g1050"] is not None]


def _solve(A, B):
    """高斯消元解对称正定正规方程。"""
    k = len(B)
    M = [A[i][:] + [B[i]] for i in range(k)]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            return None
        M[c], M[piv] = M[piv], M[c]
        pv = M[c][c]
        for r in range(k):
            if r != c and M[r][c]:
                fac = M[r][c] / pv
                for cc in range(c, k + 1):
                    M[r][cc] -= fac * M[c][cc]
    return [M[i][k] / M[i][i] for i in range(k)]


def _fit(X, Y):
    """全样本最小二乘 + LOOCV RMSE。"""
    n, k = len(X), len(X[0])
    def coef(idx):
        Xi = [X[j] for j in idx]
        Yi = [Y[j] for j in idx]
        A = [[sum(Xi[j][a] * Xi[j][b] for j in range(len(idx))) for b in range(k)] for a in range(k)]
        B = [sum(Xi[j][a] * Yi[j] for j in range(len(idx))) for a in range(k)]
        return _solve(A, B)
    co = coef(list(range(n)))
    if co is None:
        return float("inf"), None
    errs = []
    for i in range(n):
        hold = [j for j in range(n) if j != i]
        ci = coef(hold)
        if ci is None:
            return float("inf"), co
        errs.append(Y[i] - sum(ci[a] * X[i][a] for a in range(k)))
    return math.sqrt(sum(e * e for e in errs) / n), co


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bucket_p(mu, sigma, lo):
    """P(峰值 ∈ [lo, lo+1))，lo 为整数档下界。"""
    return norm_cdf((lo + 1 - mu) / sigma) - norm_cdf((lo - mu) / sigma)


def build_methods():
    """七方法族：站点系 / 网格系 / 混合系。S7 为已知失控项（RMSE>1.5 自动剔除）。"""
    return {
        "S1 站点r11":        (lambda r: [1.0, r["r11"]],                       lambda t: [1.0, t["r11"]]),
        "S2 网格日高":       (lambda r: [1.0, r["gmax"]],                      lambda t: [1.0, t["gmax"]]),
        "S3 r11+网格":       (lambda r: [1.0, r["r11"], r["gmax"]],            lambda t: [1.0, t["r11"], t["gmax"]]),
        "S4 r11+网格余量":   (lambda r: [1.0, r["r11"], r["gmax"] - r["r11"]],  lambda t: [1.0, t["r11"], t["gmax"] - t["r11"]]),
        "S5 r11+gap":        (lambda r: [1.0, r["r11"], r["r11"] - r["g1050"]], lambda t: [1.0, t["r11"], t["r11"] - t["g1050"]]),
        "S6 r11+gap+余量":   (lambda r: [1.0, r["r11"], r["r11"] - r["g1050"], r["gmax"] - r["r11"]],
                              lambda t: [1.0, t["r11"], t["r11"] - t["g1050"], t["gmax"] - t["r11"]]),
        "S7 gap+余量":       (lambda r: [1.0, r["r11"] - r["g1050"], r["gmax"] - r["r11"]],
                              lambda t: [1.0, t["r11"] - t["g1050"], t["gmax"] - t["r11"]]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--r11", type=float, required=True,
                    help="结算站 running max @11:00（口径锚）。必须喂 11:00 的值，"
                         "不得喂当前最新 rmax —— 样本侧 r11 全部是 11:00 值，换口径即漂移")
    ap.add_argument("--market", default="")
    ap.add_argument("--gpeak", type=float, default=None)
    ap.add_argument("--g1050", type=float, default=None)
    ap.add_argument("--sigma", type=float, default=0.65)
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--ev-threshold", type=float, default=0.08)
    ap.add_argument("--min-depth", type=float, default=100.0)
    a = ap.parse_args()

    recs = load_recs()
    if len(recs) < 20:
        print("样本量不足 n=%d（<20），横断不可用，按规则 41 降级为当日不用横断" % len(recs))
        return 2
    grid_today = {}
    if os.path.exists(GRID92):
        with open(GRID92, encoding="utf-8") as f:
            grid_today = json.load(f).get(a.date, {})
    gpeak = a.gpeak if a.gpeak is not None else grid_today.get("gpeak")
    g1050 = a.g1050 if a.g1050 is not None else grid_today.get("g1050")
    if gpeak is None or g1050 is None:
        print("缺 7M 网格输入（--gpeak/--g1050 或 %s 的 %s 键）" % (os.path.basename(GRID92), a.date))
        return 2
    T = dict(r11=a.r11, gmax=gpeak, g1050=g1050)
    # 口径校验：--r11 必须是 11:00 的 rmax。喂当前最新 rmax = 跨口径引用（2026-10-21 11:45 实踩）。
    _log = os.path.join(DATA, "obs_1min_log.json")
    if os.path.exists(_log):
        try:
            with open(_log, encoding="utf-8") as f:
                _pts = [p for p in json.load(f).get("points", [])
                        if p.get("hko") is not None and p.get("t", "")[:10] == a.date]
            if _pts:
                _upto11 = [p["hko"] for p in _pts if p["t"][11:16] <= "11:00"]
                _r11 = max(_upto11) if _upto11 else None
                _rmax = max(p["hko"] for p in _pts)
                if _r11 is not None and abs(a.r11 - _r11) > 0.05:
                    print("🚨 口径漂移：--r11=%.2f 但 %s 的 rmax@11:00 = %.2f（当前最新 rmax=%.2f）。"
                          "\n   LOOCV 样本的 r11 全部是 11:00 值，喂最新 rmax 会让今日输入与样本异源。"
                          "\n   应改用 --r11 %.2f；当前最新 rmax 只作增量信息报告，不得喂入模型。"
                          % (a.r11, a.date, _r11, _rmax, _r11))
                elif _rmax is not None and _rmax > a.r11 + 0.05:
                    print("信息：r11@11:00 = %.2f（已采用）｜ 当前最新 rmax = %.2f（较 11:00 +%.1f °C），"
                          "该增量只作参考，不回灌模型"
                          % (a.r11, _rmax, _rmax - a.r11))
        except Exception as e:
            print("（口径校验跳过: %s）" % e)
    # 当日网格必须显式传最新 run：previous-runs 缓存是数小时前的 run，
    # 与当前 forecast run 可差 0.3°C+。差异必须显式披露，不得静默混用（规则 49 同源精神）。
    if a.gpeak is None and grid_today.get("gpeak") is not None:
        print("⚠ 未显式传 --gpeak，复用 previous-runs 缓存 gpeak=%.2f；若当日 forecast run 已更新，"
              "应以 --gpeak 显式覆盖（规则 53）" % grid_today["gpeak"])
    elif grid_today.get("gpeak") is not None and abs(a.gpeak - grid_today["gpeak"]) > 0.05:
        print("⚠ --gpeak=%.2f 覆盖缓存 %.2f（差 %+.2f，源于 run 时刻不同，已采用 --gpeak）"
              % (a.gpeak, grid_today["gpeak"], a.gpeak - grid_today["gpeak"]))
    print("样本 n=%d ｜ 今日输入: r11=%.2f gpeak=%.2f g1050=%.2f gap=%+.2f 余量=%.2f"
          % (len(recs), T["r11"], T["gmax"], T["g1050"], T["r11"] - T["g1050"], T["gmax"] - T["r11"]))

    # ---- 1) 七方法 LOOCV ----
    Y = [r["off"] for r in recs]
    methods = build_methods()
    table = []
    for name, (fr, ft) in methods.items():
        X = [fr(r) for r in recs]
        rmse, co = _fit(X, Y)
        if co is None:
            continue
        pred = sum(co[i] * ft(T)[i] for i in range(len(co)))
        table.append(dict(name=name, rmse=rmse, pred=pred))
    table_ok = [t for t in table if t["rmse"] <= 1.5]
    dropped = [t["name"] for t in table if t["rmse"] > 1.5]
    print("\n[1] 七方法 LOOCV 横断")
    for t in sorted(table_ok, key=lambda z: z["rmse"]):
        print("    %-18s RMSE %.3f   预测 %6.2f" % (t["name"], t["rmse"], t["pred"]))
    if dropped:
        print("    (剔除 RMSE>1.5: %s)" % ", ".join(dropped))
    if len(table_ok) < 3:
        print("→ 可用方法 <3，横断不可用（规则 42），当日按单口径出决定并标注分歧未收敛")
        return 3
    preds = [t["pred"] for t in table_ok]
    lo, hi = min(preds), max(preds)
    med = st.median(preds)
    print("    跨度 %.2f – %.2f = %.2f °C ｜ 中位 %.2f%s"
          % (lo, hi, hi - lo, med, "  ⚠ 跨度>1.5°C" if hi - lo > 1.5 else ""))
    site = [t["pred"] for t in table_ok if t["name"] in ("S1 站点r11", "S5 r11+gap")]
    gridm = [t["pred"] for t in table_ok if t["name"] in ("S2 网格日高", "S3 r11+网格", "S4 r11+网格余量", "S6 r11+gap+余量")]
    if site and gridm:
        print("    分裂轴: 站点系 %.2f vs 网格系 %.2f (差 %+.2f)"
              % (st.mean(site), st.mean(gridm), st.mean(gridm) - st.mean(site)))

    # ---- 2) 三条经验锚（不依赖 mu 绝对值）----
    print("\n[2] 经验锚")
    a_r11 = sorted(r["off"] for r in recs if r["r11"] >= 30.0)
    anchors = []
    if len(a_r11) >= 5:
        p33 = sum(1 for f in a_r11 if f >= 33) / len(a_r11)
        p32 = sum(1 for f in a_r11 if f >= 32) / len(a_r11)
        print("    锚A r11>=30.0 层 n=%d: finals %s" % (len(a_r11), a_r11))
        print("       中位 %.2f ｜ P(>=32)=%.0f%%  P(>=33)=%.0f%%"
              % (st.median(a_r11), p32 * 100, p33 * 100))
        anchors.append(("A", st.median(a_r11)))
    gapneg = [r["off"] - r["r11"] for r in recs if r["r11"] - r["g1050"] < 0]
    if len(gapneg) >= 3:
        print("    锚B 站-网缺口<0 层 n=%d: R=final-r11 %+.2f±%.2f → 今日 %.2f"
              % (len(gapneg), st.mean(gapneg), st.pstdev(gapneg), T["r11"] + st.mean(gapneg)))
        anchors.append(("B", T["r11"] + st.mean(gapneg)))
    surplus = T["gmax"] - T["r11"]
    layers = [(0.0, 0.8), (0.8, 1.5), (1.5, 2.2)]
    r_hi = None
    for llo, lhi in layers:
        sub = [r["off"] - r["r11"] for r in recs if llo <= r["gmax"] - r["r11"] < lhi]
        if sub:
            r_hi = st.mean(sub)
    if r_hi is not None:
        note = "（余量 %.2f 超样本上限，取最高层）" % surplus if surplus >= 2.2 else ""
        print("    锚C 余量 %.2f → R=%+.2f → 今日 %.2f %s"
              % (surplus, r_hi, T["r11"] + r_hi, note))
        anchors.append(("C", T["r11"] + r_hi))
    if anchors:
        print("    三锚合成中央: %.2f – %.2f"
              % (min(v for _, v in anchors), max(v for _, v in anchors)))

    # ---- 3) 盘口解析 ----
    # --market 段格式 "档:bid,ask[,[+]可见深度份]"；缺深度 = None（未知，不拦但警告）
    mk = {}
    if a.market:
        for seg in a.market.replace(" ", "").split(";"):
            if not seg:
                continue
            parts = seg.split(":")
            b = int(parts[0])
            q = parts[1].split(",")
            bid, ask = float(q[0]), float(q[1])
            depth = float(q[2]) if len(q) > 2 else None
            mk[b] = dict(bid=bid, ask=ask, depth=depth, bids=[], asks=[])
    else:
        try:
            import urllib.request
            import re
            UA = {"User-Agent": "Mozilla/5.0"}
            months = {"01": "january", "02": "february", "03": "march", "04": "april", "05": "may",
                      "06": "june", "07": "july", "08": "august", "09": "september", "10": "october",
                      "11": "november", "12": "december"}
            slug = "highest-temperature-in-hong-kong-on-%s-%d-%d" % (
                months[a.date[5:7]], int(a.date[8:10]), int(a.date[:4]))
            ev = json.load(urllib.request.urlopen(urllib.request.Request(
                "https://gamma-api.polymarket.com/events?slug=" + slug, headers=UA), timeout=25))[0]
            tids, meta = [], {}
            for m in ev["markets"]:
                t = (m.get("groupItemTitle") or "").strip()
                # 只认纯整数档（"33°C"）；"28°C or below" 这类区间标题必须跳过，
                # 否则 int() 解析崩掉（2026-10-21 实踩）。
                mt = re.match(r"^(\d+)\s*°?C?$", t)
                if not mt:
                    continue
                bkt = int(mt.group(1))
                toks = json.loads(m["clobTokenIds"])
                meta[toks[0]] = (bkt, "YES")
                meta[toks[1]] = (bkt, "NO")
                tids += list(toks)
            payload = json.dumps([{"token_id": t} for t in tids]).encode()
            req = urllib.request.Request("https://clob.polymarket.com/books", data=payload, headers=UA)
            books = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore"))
            for b in books:
                key = str(b.get("asset_id"))
                if key not in meta:
                    continue
                bkt, side = meta[key]
                bids = sorted([(float(x["price"]), float(x["size"])) for x in (b.get("bids") or [])], key=lambda z: -z[0])
                asks = sorted([(float(x["price"]), float(x["size"])) for x in (b.get("asks") or [])], key=lambda z: z[0])
                e = mk.setdefault(bkt, {"bid": None, "ask": None, "depth": 0.0, "bids": [], "asks": []})
                # 深度 = 该档 YES 与 NO 双侧首三档挂单量合计（跨侧同事件互补）
                top3 = sum(s for _, s in (bids[:3] + asks[:3]))
                e["depth"] = round(e["depth"] + top3, 1)
                if side == "YES":
                    e["bid"] = bids[0][0] if bids else None
                    e["ask"] = asks[0][0] if asks else None
                    e["bids"], e["asks"] = bids, asks
        except Exception as e:
            print("盘口现拉失败: %s（用 --market 手工喂）" % e)
            return 4
    if not mk:
        print("无盘口输入，仅输出 mu 横断，不出 EV")
        return 0

    # ---- 4) EV 表 + 稳健性否决 + 深度闸门 ----
    top = sorted(table_ok, key=lambda z: z["rmse"])[:a.topk]
    mu_set = sorted(set([round(t["pred"], 2) for t in top]
                        + [round(v, 2) for _, v in anchors]
                        + ([round(st.mean(site), 2)] if site else [])))
    print("\n[3] EV 与稳健性（sigma=%.2f，EV 门槛 %.0f%%，深度门槛 %.0f 份）"
          % (a.sigma, a.ev_threshold * 100, a.min_depth))
    print("    稳健性 mu 集: %s" % ", ".join("%.2f" % m for m in mu_set))
    print("    %-4s %-13s %-7s %-10s %-9s %s" % ("档", "bid/ask", "深度", "fair@中央", "YES", "NO"))
    survivors = []
    for b in sorted(mk):
        e = mk[b]
        bid, ask, depth = e["bid"], e["ask"], e.get("depth")
        if ask is None:
            continue
        fair = bucket_p(med, a.sigma, b)
        evs_yes = [bucket_p(m, a.sigma, b) - ask for m in mu_set]
        evs_no = [((1 - bucket_p(m, a.sigma, b)) - (1 - bid)) for m in mu_set] if bid is not None else None

        def tag(evs):
            if evs is None:
                return "n/a"
            if min(evs) > a.ev_threshold:
                return "PASS"
            return "FAIL" if max(evs) < 0 else "UNSTABLE"
        tag_y, tag_n = tag(evs_yes), tag(evs_no)
        print("    %-4d %-13s %-7s %-10.3f %-9s %s"
              % (b, "%.3f/%.3f" % (bid if bid else 0, ask),
                 "未知" if depth is None else "%.0f" % depth, fair, tag_y, tag_n))
        if depth is None:
            if "PASS" in (tag_y, tag_n):
                print("         ⚠ 深度未知：--market 未给深度，过闸门前必须用 market_prices.py --depth 补采样（规则 6 / 12d）")
            continue
        if depth < a.min_depth:
            if "PASS" in (tag_y, tag_n):
                print("         深度 %.0f < %.0f 份 → 该档市场口径作废（规则 6 / 12d）" % (depth, a.min_depth))
            continue
        for tg, evs in (("YES", evs_yes), ("NO", evs_no)):
            if tg == "PASS" and evs is not None:
                survivors.append((b, tg, ask if tg == "YES" else (1 - bid), min(evs)))
    print("\n[4] 决定")
    if not survivors:
        print("    → NO LEG：无任何腿同时在全部 mu 下 EV > %.0f%% 且通过深度闸门" % (a.ev_threshold * 100))
        print("      （方向不稳健 → 规则 2；深度不足 → 规则 6 / 12d）")
        return 0
    survivors.sort(key=lambda z: -z[3])
    for b, tg, px, ev in survivors:
        print("    → 候选：买 %s %d @%.3f，最坏 mu 下 EV %+.1fpp" % (tg, b, px, ev * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
