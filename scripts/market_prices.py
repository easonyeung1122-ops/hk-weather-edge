#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Polymarket CLOB 订单簿取「可成交」价格。

不要用最后成交价：Gamma 的 outcomePrices 只是一条历史成交记录，可能很旧，
也可能发生在没有真实挂单的价位上（$0.0005 的尾部档位常挂着上万美元"流动性"
却一个 bid 都没有）。用它算 EV 会系统性高估 edge。

真正能成交的价格在订单簿里：
  买 YES → 吃掉 YES token 的最低卖价（best ask）
  卖 YES → 命中 YES token 的最高买价（best bid）
  买 NO  → 吃掉 NO token 的最低卖价（NO token 有独立订单簿，未必等于 1 - YES bid）
  卖 NO  → 命中 NO token 的最高买价

用法：
  py -3 scripts/market_prices.py 2026-09-07              # 按香港日期
  py -3 scripts/market_prices.py 2026-09-08 --depth 3    # 显示前 3 档深度
  py -3 scripts/market_prices.py --slug highest-temperature-in-hong-kong-on-september-7-2026
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com/events?slug=%s"
CLOB_BOOK = "https://clob.polymarket.com/book?token_id=%s"

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def top_of_book(token_id, depth):
    """返回 (bids, asks)，各为 [(price, size)]，已按对你有利的方向排序。"""
    try:
        b = get(CLOB_BOOK % urllib.parse.quote(str(token_id)))
    except Exception as e:
        print("[warn] 订单簿读取失败 %s: %s" % (token_id, e), file=sys.stderr)
        return [], []
    bids = sorted(((float(x["price"]), float(x.get("size", 0)))
                   for x in (b.get("bids") or [])), key=lambda x: -x[0])
    asks = sorted(((float(x["price"]), float(x.get("size", 0)))
                   for x in (b.get("asks") or [])), key=lambda x: x[0])
    return bids[:depth], asks[:depth]


def fmt(levels):
    """(price, top_size, total_size)"""
    if not levels:
        return None, 0.0, 0.0
    return levels[0][0], levels[0][1], sum(s for _, s in levels)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", help="香港日期 YYYY-MM-DD")
    ap.add_argument("--slug", help="直接给 event slug")
    ap.add_argument("--depth", type=int, default=1, help="显示前 N 档深度（默认 1）")
    a = ap.parse_args()

    if a.slug:
        slug = a.slug
    elif a.date:
        y, m, d = a.date.split("-")
        slug = "highest-temperature-in-hong-kong-on-%s-%d-%s" % (MONTHS[int(m) - 1], int(d), y)
    else:
        raise SystemExit("[err] 需要给日期或 --slug")

    ev = get(GAMMA % urllib.parse.quote(slug))
    if not ev:
        raise SystemExit("[err] 找不到事件 %s" % slug)
    e = ev[0]
    print("事件: %s" % e.get("title"))
    print("活跃: %s   收盘: %s   24h量: %s" % (e.get("active"), e.get("endDate"), e.get("volume24hr")))
    print()
    print("%-16s %-22s %-22s %s" % ("档位", "YES bid(卖得)/ask(买付)", "NO bid(卖得)/ask(买付)",
                                    "可成交量 买YES/卖YES/买NO"))
    print("-" * 104)

    mids = []
    for m in e.get("markets", []):
        label = m.get("groupItemTitle") or "?"
        try:
            tokens = json.loads(m.get("clobTokenIds") or "[]")
        except Exception:
            tokens = []
        if len(tokens) < 2:
            continue
        yb, ya = top_of_book(tokens[0], a.depth)
        nb, na = top_of_book(tokens[1], a.depth)

        y_bid, y_bid_sz, y_bid_tot = fmt(yb)
        y_ask, y_ask_sz, y_ask_tot = fmt(ya)
        n_bid, n_bid_sz, n_bid_tot = fmt(nb)
        n_ask, n_ask_sz, n_ask_tot = fmt(na)

        if y_bid is not None and y_ask is not None:
            mid = (y_bid + y_ask) / 2
            mids.append((int(label.replace("°C", "").strip()) if label[:-2].isdigit() else None, mid, label))
        elif y_ask is not None:
            mids.append((None, y_ask, label))

        def cell(bid, ask, tot):
            if bid is None and ask is None:
                return "-"
            b = "%.3f" % bid if bid is not None else "  -  "
            s = "%.3f" % ask if ask is not None else "  -  "
            return "%s / %s" % (b, s)

        print("%-16s %-22s %-22s %s / %s / %s" % (
            label, cell(y_bid, y_ask, y_bid_tot), cell(n_bid, n_ask, n_bid_tot),
            "%.0f" % y_ask_tot if y_ask_tot else "-",
            "%.0f" % y_bid_tot if y_bid_tot else "-",
            "%.0f" % n_ask_tot if n_ask_tot else "-"))

    usable = [(b, p) for b, p, _ in mids if b is not None]
    if usable:
        print()
        print("可直接喂给 hk_edge.py 的 --market（用 YES 订单簿中间价，非最后成交价）：")
        print('  --market "' + ",".join("%d:%.3f" % (b, p) for b, p in sorted(usable)) + '"')


if __name__ == "__main__":
    main()
