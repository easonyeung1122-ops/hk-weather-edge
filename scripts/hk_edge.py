#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket「香港最高气温」市场 Edge 计算器

结算口径（必须背下来）：
  结算源 = 香港天文台 Daily Extract 的 "Absolute Daily Max (deg. C)"
            https://www.weather.gov.hk/en/cis/climat.htm
  精度   = 摄氏 0.1 度；档位 N°C 表示 [N.0, N+1.0)，即 floor(实测值)
  站点   = 香港天文台总部站（尖沙咀），不是机场、不是打鼓岭、不是你手机上那个数

用法：
  python3 hk_edge.py                      # 列出未来 7 天所有档位的公允概率
  python3 hk_edge.py --date 2026-09-08    # 只看某一天
  python3 hk_edge.py --watch              # 当日实时追踪（每 10 分钟由 cron 调用）
  python3 hk_edge.py --date 2026-09-08 --market "30:0.08,31:0.35,32:0.40,33:0.12"
                                          # 手工输入市场价，算 edge
  python3 hk_edge.py --recalibrate        # 强制重算模型偏差
  python3 hk_edge.py --no-cache           # 强制实时拉取（下单前用它）
  python3 hk_edge.py --ttl 300            # 模式/预报缓存改为 5 分钟（默认 900）
  python3 hk_edge.py --date 2026-09-08 --hold "34:0.065:200,33:0.56:70:N"
                                          # 已用头寸的浮盈 + 该不该止盈

依赖：pip3 install requests（没有也能跑，会回落到 urllib）
"""
import argparse, json, math, os, sys, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor

VERSION = "0.13.5"      # 语义化版本，见 CHANGELOG.md；每次推送 GitHub 前必须递增

# Kelly 缩放：满 Kelly 波动太大、且概率本身有 ±5% 量级误差，实操一律打折。
# 这里用 35% Kelly（原来是 1/4=25%）。
KELLY_FRAC = 0.35

# ---- HTTP 层 ----
# 一次运行要打 3~4 个互不依赖的接口，串行时耗时几乎全是 TLS 握手（实测 2.2s ≈ 3×0.7s）。
# 三层加速，都不改变任何数字：
#   1) Session 复用 TCP/TLS（有 requests 时；没有则退回 urllib，只是没有保活）
#   2) 互不依赖的接口并行拉取 → 墙钟时间 = 最慢那个，而不是总和
#   3) 短 TTL 磁盘缓存：模式更新周期 ≥6h、官方预报一天数次，15 分钟缓存不影响结论
# 严谨性边界：**实况观测(rhrread)、网格实况、订单簿价格永不缓存**——那是结论的来源，
# 不是可以复用的输入。缓存只作用于下面白名单式的调用点（见 cache=True 的传参）。
_SESSION = None   # None=未初始化；False=退回 urllib；否则是 requests.Session

def _http(u, t):
    # 真正发请求时才 import requests：命中缓存的运行完全不需要它，省约 0.3s 启动时间
    global _SESSION
    if _SESSION is None:
        try:
            import requests
            _SESSION = requests.Session()      # keep-alive：同主机复用 TCP/TLS
        except ImportError:
            _SESSION = False
    if _SESSION:
        r = _SESSION.get(u, timeout=t)
        r.raise_for_status()
        return r.json()
    import urllib.request
    req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
    return json.load(urllib.request.urlopen(req, timeout=t))


def _http_raw(u, t=20):
    """取原始文本（CSV 等非 JSON 接口用）。与实况相关，永不缓存。"""
    global _SESSION
    if _SESSION is None:
        try:
            import requests
            _SESSION = requests.Session()
        except ImportError:
            _SESSION = False
    if _SESSION:
        r = _SESSION.get(u, timeout=t)
        r.raise_for_status()
        return r.text
    import urllib.request
    req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
    return urllib.request.urlopen(req, timeout=t).read().decode('utf-8-sig')

CACHE_TTL = 900          # 15 分钟：远小于任何模式的更新周期
_CACHE_PATH = None       # 绑定到 DATA 后赋值
_cache_mem, _fetch_meta = None, {}   # 缓存本体 / 本次运行各 URL 的数据年龄(秒)

def _cache_load():
    global _cache_mem
    if _cache_mem is None:
        try:
            _cache_mem = json.load(open(_CACHE_PATH))
        except Exception:
            _cache_mem = {}
    return _cache_mem

def _cache_save():
    if _cache_mem is not None:
        try:
            json.dump(_cache_mem, open(_CACHE_PATH, 'w'))
        except Exception:
            pass

def _get(u, t=60, cache=False, ttl=CACHE_TTL):
    if cache:
        e = _cache_load().get(u)
        if e and time.time() - e['t'] < ttl:
            _fetch_meta[u] = time.time() - e['t']
            return e['v']
    v = _http(u, t)
    if cache:
        _cache_load()[u] = {'t': time.time(), 'v': v}
    _fetch_meta[u] = 0.0
    return v

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, 'data')
os.makedirs(DATA, exist_ok=True)
_CACHE_PATH = os.path.join(DATA, 'http_cache.json')

# 结算站点坐标（尖沙咀天文台总部，海拔约 32m）
OBS_LAT, OBS_LON = 22.302, 114.173
TZ = dt.timezone(dt.timedelta(hours=8))
MODELS = "ecmwf_ifs025,gfs_seamless,icon_seamless,gem_seamless,metno_seamless,jma_seamless,ukmo_seamless"

HKO_RHR   = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=rhrread&lang=en"
HKO_FND   = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType=fnd&lang=en"
HKO_MAXT  = "https://data.weather.gov.hk/weatherAPI/opendata/opendata.php?dataType=CLMMAXT&station=HKO&lang=en"
# 分区气温 CSV：0.1°C 精度、每 10 分钟更新、39 个站（含结算站）。
# 结算站在这个 CSV 里的名字是 "HK Observatory"（不是 rhrread 里的 "Hong Kong Observatory"）。
HKO_OBS_1MIN = ("https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/"
                "latest_1min_temperature.csv")
OBS_STATION_NAMES = ('HK Observatory', 'Hong Kong Observatory')
OM_FC     = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
             "&daily=temperature_2m_max&forecast_days=10&timezone=Asia%2FShanghai&models={m}")
OM_ENS    = ("https://ensemble-api.open-meteo.com/v1/ensemble?latitude={lat}&longitude={lon}"
             "&daily=temperature_2m_max&forecast_days=10&timezone=Asia%2FShanghai&models=ecmwf_ifs025")
OM_PREV   = ("https://previous-runs-api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
             "&daily=temperature_2m_max&past_days=92&forecast_days=1&timezone=Asia%2FShanghai&models={m}")
OM_HR     = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
             "&hourly=temperature_2m&forecast_days=1&past_hours={ph}"
             "&timezone=Asia%2FShanghai&models=best_match")


# ---------------------------------------------------------------- 下次刷新时刻
# 「算完就忘」是本工具最大的实操漏洞：概率的输入源各自有更新节奏，结论只在下一批
# 新数据落地前有效。所有刷新锚点均来自实测（见 SKILL.md「各数据源更新频率」表）。
MARKET_CLOSE_HKT = 20      # Polymarket 收盘 = 目标日 12:00 UTC = 20:00 HKT
NWP_LAND_HKT     = (2, 8, 14, 20)          # NWP 新一轮落地（00/06/12/18 UTC + ~2h）
FND_UPDATE_HKT   = tuple(range(7, 23, 2))  # HKO 九天预报约每 2 小时更新（:30 前后）
PEAK_WINDOW      = (12, 17)                # 日最高温峰值窗口，此间必须 10 分钟级盯

def next_refresh(now, target, is_today):
    """下一个值得重跑的时刻。now 为 HKT aware datetime，target 为目标日 date。
    返回 (datetime, 理由)；已过收盘返回 (None, None)。"""
    close = dt.datetime.combine(target, dt.time(MARKET_CLOSE_HKT, 0), tzinfo=TZ)
    if now >= close:
        return None, None
    cands = []
    for d in (0, 1):
        day = now.date() + dt.timedelta(days=d)
        for h in NWP_LAND_HKT:
            t = dt.datetime.combine(day, dt.time(h, 0), tzinfo=TZ)
            if now < t <= close:
                cands.append((t, 'NWP 多模式新一轮落地（决定点估计，最重要）'))
        for h in FND_UPDATE_HKT:
            t = dt.datetime.combine(day, dt.time(h, 30), tzinfo=TZ)
            if now < t <= close:
                cands.append((t, 'HKO 九天预报 fnd 更新（官方口径，措辞变化优先于数字）'))
    if is_today:
        if PEAK_WINDOW[0] <= now.hour <= PEAK_WINDOW[1]:
            t = (now.replace(minute=0, second=0, microsecond=0)
                 + dt.timedelta(minutes=(now.minute // 10 + 1) * 10))
            if t <= close:
                cands.append((t, '峰值窗口：0.1°C 实况每 10 分钟（--watch 必须跟上，漏峰会低估已观测下限）'))
        t = now.replace(minute=5, second=0, microsecond=0)
        if t <= now:
            t += dt.timedelta(hours=1)
        if t <= close:
            cands.append((t, 'HKO 整点实况 rhrread（整点后约 5 分）'))
    return min(cands, key=lambda x: x[0]) if cands else (None, None)


def print_next_refresh(now, target, is_today):
    t, why = next_refresh(now, target, is_today)
    print()
    if t is None:
        print(f"■ 下次刷新：—— 目标日 {target} 已于 {MARKET_CLOSE_HKT}:00 HKT 收盘，"
              f"不再有重跑价值（结算后回填 decision_log.csv）")
        return
    mins = int((t - now).total_seconds() // 60)
    hh = f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"
    print(f"■ 下次刷新：{t.strftime('%m-%d %H:%M')} HKT（{hh} 后） —— {why}")
    print(f"  盘口价实时变动，想盯盘随时跑 market_prices.py；"
          f"本结论在下一批新数据落地前有效。")


# ---------------------------------------------------------------- 工具
def norm_cdf(x, mu=0.0, s=1.0):
    return 0.5 * (1 + math.erf((x - mu) / (s * math.sqrt(2))))


def load_obs(force=False):
    """下载/读取天文台历史 Absolute Daily Max（就是结算源本身）"""
    p = os.path.join(DATA, 'maxt_HKO.csv')
    if force or not os.path.exists(p):
        raw = None
        try:
            import urllib.request
            req = urllib.request.Request(HKO_MAXT, headers={'User-Agent': 'Mozilla/5.0'})
            raw = urllib.request.urlopen(req, timeout=120).read()
        except Exception as e:
            print(f"[warn] 无法下载 HKO 历史数据: {e}", file=sys.stderr)
        if raw:
            open(p, 'wb').write(raw)
    if not os.path.exists(p):
        return {}
    import csv, io
    obs = {}
    for row in csv.reader(io.StringIO(open(p, encoding='utf-8-sig').read())):
        if len(row) < 4:
            continue
        try:
            y, m, d = int(row[0]), int(row[1]), int(row[2])
            v = float(row[3])
        except ValueError:
            continue
        obs[dt.date(y, m, d)] = v
    return obs


def recalibrate(obs=None, force=False, use_cache=True, ttl=CACHE_TTL):
    """用过去 92 天的多模型预报 vs 实测，算出网格+模型的系统性偏差。

    obs 允许为 None：校准结果有缓存时（默认 <3 天）根本用不到历史数据，
    不必每次解析 4.9 万行 CSV。
    """
    p = os.path.join(DATA, 'model_calib.json')
    if not force and os.path.exists(p):
        c = json.load(open(p))
        if (dt.datetime.now(TZ).date() - dt.date.fromisoformat(c['window'][1])).days < 3:
            return c
    if obs is None:
        obs = load_obs()
    # 模式回看数据一天才变一次，可缓存；--recalibrate 强制实时
    d = _get(OM_PREV.format(lat=OBS_LAT, lon=OBS_LON, m=MODELS), 120,
             cache=use_cache and not force, ttl=ttl)['daily']
    cols = [k for k in d if k != 'time']
    errs, n = [], 0
    for i, day in enumerate(d['time']):
        dd = dt.date.fromisoformat(day)
        if dd not in obs:
            continue
        vals = [d[c][i] for c in cols if d[c][i] is not None]
        if len(vals) < 4:
            continue
        errs.append(obs[dd] - sum(vals) / len(vals))
        n += 1
    if n < 20:
        return {'bias': 1.17, 'resid_sd': 0.80, 'n': n, 'window': ['?', '?'], 'models': cols}
    bias = sum(errs) / n
    sd = math.sqrt(sum((e - bias) ** 2 for e in errs) / (n - 1))
    c = {'bias': round(bias, 3), 'resid_sd': round(sd, 3), 'n': n,
         'window': [d['time'][0], d['time'][-1]], 'models': cols}
    json.dump(c, open(p, 'w'), indent=1)
    return c


def fetch_forecasts(use_cache=True, ttl=CACHE_TTL, need_ens=True):
    """多模型确定性融合 + ECMWF 集合 + HKO 官方预报（三个接口并行拉取）

    need_ens=False 时跳过集合请求。这**只在 --sigma 给出时**才允许：那时概率
    直接用 N(mu, sigma)，集合成员根本不进入计算（`draws=[mu]`），而集合恰好是
    三个请求里最慢的一个（实测 1.5s，占冷启动大半）。跳过它不改变任何概率数字，
    只是「集合离散度」显示为 n/a。
    """
    _cache_load()          # 先把缓存读进内存，避免线程里重复读盘
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_det = ex.submit(_get, OM_FC.format(lat=OBS_LAT, lon=OBS_LON, m=MODELS),
                          60, use_cache, ttl)
        f_ens = (ex.submit(_get, OM_ENS.format(lat=OBS_LAT, lon=OBS_LON), 60, use_cache, ttl)
                 if need_ens else None)
        f_hko = ex.submit(_get, HKO_FND, 60, use_cache, ttl)
        det = f_det.result()['daily']
        ens = f_ens.result()['daily'] if f_ens is not None else None
        try:
            hko = f_hko.result()['weatherForecast']
        except Exception:
            hko = []
    return det, ens, hko


def _obs1min_path():
    return os.path.join(DATA, 'obs_1min_log.json')


def _load_1min_log():
    """今日的 0.1°C 观测序列（跨天自动作废）。"""
    p = _obs1min_path()
    if not os.path.exists(p):
        return {}
    try:
        d = json.load(open(p))
        today = dt.datetime.now(TZ).date().isoformat()
        return d if d.get('date') == today else {}
    except Exception:
        return {}


def fetch_hko_1min(record=True):
    """读分区气温 CSV（0.1°C / 10 分钟），并把结算站读数追加进 running-max 日志。

    为什么必须有它：`rhrread` 的 temperature **只有整数位且每小时更新**，在结算值
    刚跨过整数边界时会双重失真——① 漏掉整点之间的升温（2026-09-09 13:00 报 31，
    实际 13:40 已 32.2，差 1.2°C）；② 整数舍入撑开 ±0.5°C 未知，足以改变档位归属。
    这是唯一能免费拿到 0.1°C 结算站实况的源。

    注意它只给「最新一笔」，不自带当日最高 —— running max 靠每 10 分钟轮询累积，
    轮询越密，漏掉的峰值越小（10 分钟级 ≈ 0.2~0.3°C，rhrread 级实测可达 1.2°C）。

    返回 {'ts': ISO时刻, 'hko': 温度, 'all': {站名: 温度}}；失败返回 None。
    """
    try:
        txt = _http_raw(HKO_OBS_1MIN, 20)
    except Exception:
        return None
    rows, stamp = {}, None
    for ln in txt.strip().splitlines()[1:]:
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
    hko = next((rows[n] for n in OBS_STATION_NAMES if n in rows), None)
    if hko is None:
        return None
    iso = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[8:10]}:{stamp[10:12]}:00+08:00"
    if record:
        try:
            log = _load_1min_log() or {'date': iso[:10], 'points': []}
            if log.get('date') != iso[:10]:
                _archive_1min(log)          # 跨天：先把昨天归档，再开新一天
                log = {'date': iso[:10], 'points': []}
            if all(pt.get('t') != iso for pt in log['points']):
                log['points'].append({'t': iso, 'hko': hko})
                json.dump(log, open(_obs1min_path(), 'w'))
        except Exception:
            pass
    return {'ts': iso, 'hko': hko, 'all': rows}


def _archive_1min(log):
    """把一整天的 0.1°C 结算站序列归档到 data/obs_1min_archive.json。

    这是 skill 长期变好的**唯一途径**：ERA5 网格的剩余升温尾部比真实站点薄约 4.7 倍
    （9月 h=13 的 P(剩余升温≥0.5)：网格 7.0% vs 机场站 32.8%），只有攒够结算站自己的
    10 分钟级序列，才能把分位表换成站点口径。攒够后跑
    `python scripts/build_remaining_rise.py --from-station` 重建。
    """
    if not log or not log.get('points'):
        return
    p = os.path.join(DATA, 'obs_1min_archive.json')
    arc = {}
    if os.path.exists(p):
        try:
            arc = json.load(open(p))
        except Exception:
            arc = {}
    d = log['date']
    if d in arc and len(arc[d]) >= len(log['points']):
        return
    arc[d] = [{'t': pt.get('t'), 'hko': pt.get('hko')} for pt in log['points']]
    json.dump(arc, open(p, 'w'))


def _log_forecast(day, ts, hour, rm, pred, probs):
    """把每次 watch 的概率向量记进 data/forecast_log.jsonl，供 `--score` 打回。

    没有落地记录就只能靠人工事后复盘，而人工复盘恰恰是这套东西反复出错的原因
    （2026-09-08 到 09-10 连续三轮偏差都是事后才发现的）。
    """
    if not probs:
        return
    p = os.path.join(DATA, 'forecast_log.jsonl')
    rec = {'date': day, 't': ts, 'hour': hour, 'rm': rm, 'pred': pred,
           'probs': {str(k): round(v, 4) for k, v in probs.items()}}
    try:
        # 同一 (date, t) 覆盖而不是追加：同一分钟内手工跑两次 --watch 会写两条一样的，
        # 让 --score 重复计分、把样本量算翻倍。
        lines = []
        if os.path.exists(p):
            for ln in open(p, encoding='utf-8'):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                if o.get('date') == day and o.get('t') == ts:
                    continue
                lines.append(o)
        lines.append(rec)
        with open(p, 'w', encoding='utf-8') as f:
            for o in lines:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_1min_max():
    """今日 0.1°C 源已观测到的最高温（running max）。无数据返回 None。"""
    vals = [pt.get('hko') for pt in _load_1min_log().get('points', [])
            if pt.get('hko') is not None]
    return max(vals) if vals else None


def load_observed_max():
    """从日内日志里读「今日已实测到的最高温度」。

    日最高温具有单调性：已经观测到的值就是当日峰值的下限。
    日内复盘时必须以此为条件截断分布，否则会给出物理上不可能的低档概率。

    优先取 0.1°C 分区气温源的 running max，再与 `--watch` 的整点日志取大者——
    后者是整数位、每小时一次，只作为前者缺失时的兜底。
    """
    p = os.path.join(DATA, 'intraday_log.json')
    mx = None
    if os.path.exists(p):
        try:
            log = json.load(open(p))
            if log.get('date') == dt.datetime.now(TZ).date().isoformat():
                vals = [pt.get('hko') for pt in log.get('points', [])
                        if pt.get('hko') is not None]
                mx = max(vals) if vals else None
        except Exception:
            pass
    m1 = load_1min_max()
    cands = [v for v in (mx, m1) if v is not None]
    return max(cands) if cands else None


def watch_loop(interval_min=10, until='17:00'):
    """常驻轮询：在一个进程里反复 watch()，复用同一条 TLS 连接。

    **为什么这比"每 10 分钟起一个新进程"快 3~5 倍**：实测同一主机首次请求 1.0s、
    连接热了之后 0.20s —— 单次运行的耗时几乎全在 TCP/TLS 握手上，跟数据量无关
    （7 模型 10 天 1.02s vs 1 模型 2 天 0.20s，差的只是握手）。每 10 分钟一个新
    进程就要重新握一次手；常驻进程只在第一次付这笔钱，之后每次约 0.3~0.5s。

    数字完全不变：走的还是同一个 watch()，只是调用它的方式变了。
    """
    import time as _time
    hh, mm = (int(x) for x in until.split(':'))
    print(f"[loop] 每 {interval_min} 分钟采集一次，直到 {until} HKT（Ctrl-C 退出）")
    n = 0
    while True:
        now = dt.datetime.now(TZ)
        end = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now >= end:
            print(f"[loop] 已过 {until}，退出（共采集 {n} 次）")
            break
        n += 1
        print(f"[loop] #{n}  {now:%H:%M:%S}", flush=True)
        try:
            watch()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[loop] 本次采集失败: {type(e).__name__}: {e}", flush=True)
        # 对齐到下一个 interval 边界，避免漂移
        nxt = (dt.datetime.now(TZ) + dt.timedelta(minutes=interval_min)).replace(
            second=0, microsecond=0)
        nxt -= dt.timedelta(minutes=nxt.minute % interval_min)
        delay = (nxt - dt.datetime.now(TZ)).total_seconds()
        if delay <= 0:
            delay = interval_min * 60
        remain = (end - dt.datetime.now(TZ)).total_seconds()
        if remain <= 0:
            continue
        try:
            _time.sleep(min(delay, remain))
        except KeyboardInterrupt:
            print(f"\n[loop] 已停止（共采集 {n} 次）")
            break


def bucket_probs(target, det, ens, calib, mu_override=None, floor=None, sd_override=None):
    """
    目标日每个整数档的公允概率。
    做法 = 偏差修正后的集合预报（dressed ensemble）：
      每个集合成员 m: x_m = det_mean + bias + (member_m - ens_mean)
      再叠加残差噪声 N(0, resid_sd)，对 50 个成员求平均

    floor: 当日已实测到的最高温度（°C）。给出后，分布被截断到 [floor, +∞)
           并重新归一化——已经观测到的最高温只会被刷新、不会被抹掉。
    """
    days = det['time']
    if target not in days:
        raise SystemExit(f"[err] {target} 不在预报窗口 {days[0]} ~ {days[-1]}")
    i = days.index(target)
    cols = [c for c in det if c.startswith('temperature_2m_max_')]
    dv = [det[c][i] for c in cols if det[c][i] is not None]
    if not dv:
        raise SystemExit("[err] 无确定性预报")
    det_mean = sum(dv) / len(dv)

    # ens 为 None = 调用方跳过了集合请求（只在 --sigma 给出时允许）
    members = ([ens[k][i] for k in ens if k.startswith('temperature_2m_max_member')
                and ens[k][i] is not None] if ens else [])
    ens_mean = sum(members) / len(members) if members else det_mean
    # 没有集合成员时离散度不可得 → None，调用方显示 n/a
    spread = (math.sqrt(sum((x - ens_mean) ** 2 for x in members) / len(members))
              if members else None)
    mu = det_mean + calib['bias'] if mu_override is None else mu_override
    # resid_sd 是「提前一天」的不确定性；接近收盘时剩余不确定性小得多，可用 --sigma 收窄
    sd = calib['resid_sd'] if sd_override is None else sd_override
    # dressed ensemble: 偏差修正后的点估计 + 集合距平 + 残差噪声
    # 但 --sigma 表示「剩余总不确定性」（日内场景）：此时集合距平已过时，直接用单个正态
    draws = [mu] if sd_override is not None else [mu + (x - ens_mean) for x in members]

    lo_b, hi_b = int(math.floor(min(draws) - 3 * sd)), int(math.ceil(max(draws) + 3 * sd))
    if floor is not None:
        lo_b = max(lo_b, int(math.floor(floor)))
        hi_b = max(hi_b, lo_b)
    out = {}
    for b in range(lo_b, hi_b + 1):
        # 截断：档位 [b, b+1) 与 [floor, +∞) 求交集，floor 以下的整档概率为 0
        lo_edge = b if floor is None else max(b, floor)
        if lo_edge >= b + 1:
            continue
        p = sum(norm_cdf(b + 1, x, sd) - norm_cdf(lo_edge, x, sd) for x in draws) / len(draws)
        if p > 0.0005:
            out[b] = p
    s = sum(out.values())
    out = {k: v / s for k, v in out.items()}
    return {'mu': mu if floor is None else max(mu, floor), 'spread': spread, 'sd': sd,
            'n_members': len(members), 'det_mean': det_mean, 'floor': floor, 'probs': out}


# ---------------------------------------------------------------- 当日实时追踪
def cdf_upper(tbl, x):
    """经验分布的上尾概率 P(R ≥ x)。

    tbl = {"n": int, "q": [[p, value], ...]}，q 按下侧分位概率升序。
    用于「剩余升温」的历史经验分布——它不是正态，右偏且 0 处有原子
    （13 时后大多数日子当天最高已经出现过了），正态假设会把两端都算错：
    近端高估 41%、远端低估 3.4 倍（ERA5 9月实测）。
    """
    q = tbl.get('q') or []
    if not q:
        return None
    if x <= q[0][1]:
        return 1.0 - q[0][0]
    for i in range(1, len(q)):
        if x <= q[i][1]:
            p0, v0 = q[i - 1]
            p1, v1 = q[i]
            f = 0.0 if v1 == v0 else (x - v0) / (v1 - v0)
            return max(0.0, min(1.0, 1.0 - (p0 + f * (p1 - p0))))
    return 0.0


def watch():
    """实时追踪：记录当日天文台站已观测到的最高温度（供 cron 每 10 分钟调用）"""
    now = dt.datetime.now(TZ)
    # 观测与网格实况互不依赖 → 并行拉取（两者都属实况，永不缓存）
    # past_hours 拉到今天 0 时：没采样到的小时要靠网格回填（见下方"回填未采样"）
    ex = ThreadPoolExecutor(max_workers=3)
    # 0.1°C / 10 分钟的分区气温是首选实况源；rhrread（整点整数）只作兜底
    f_1min = ex.submit(fetch_hko_1min)
    f_rhr = ex.submit(_get, HKO_RHR, 30)
    f_grid = ex.submit(_get, OM_HR.format(lat=OBS_LAT, lon=OBS_LON,
                                         ph=max(3, now.hour + 1)), 30)
    m1 = f_1min.result()
    if m1 and m1.get('hko') is not None:
        rt, hko_t, temp = m1['ts'], m1['hko'], m1['all']
        src_note = "分区气温CSV 0.1°C/10分钟"
    else:
        try:
            r = f_rhr.result()
        except Exception as e:
            print(f"[err] 无法读取天文台实时数据: {e}")
            ex.shutdown(wait=False)
            return
        temp = {t['place']: t['value'] for t in r['temperature']['data']}
        rt = r['temperature']['recordTime']
        hko_t = temp.get('Hong Kong Observatory')
        src_note = "rhrread 整点整数（0.1°C 源不可用，已回落——整数位会漏掉整点间升温）"
    today = dt.datetime.now(TZ).date().isoformat()
    log_p = os.path.join(DATA, 'intraday_log.json')
    log = json.load(open(log_p)) if os.path.exists(log_p) else {}
    if log.get('date') != today:
        log = {'date': today, 'points': []}
    log['points'].append({'t': rt, 'hko': hko_t, 'all': temp})
    json.dump(log, open(log_p, 'w'))

    pts = [p for p in log['points'] if p['hko'] is not None]
    # running max：0.1°C 源与整点日志取大者（前者精度高、采样密）
    _mx_cands = [v for v in (
        max((p['hko'] for p in pts), default=None), load_1min_max()) if v is not None]
    mx = max(_mx_cands) if _mx_cands else None
    sampled_h = {int(p['t'][11:13]) for p in pts}
    for pt in _load_1min_log().get('points', []):
        try:
            sampled_h.add(int(pt['t'][11:13]))
        except (TypeError, ValueError):
            pass
    others = {k: v for k, v in temp.items() if k not in OBS_STATION_NAMES}
    hot = sorted(others.items(), key=lambda x: -x[1])[:5]
    try:
        obs_h = int(rt[11:13])          # 观测时刻——所有小时内推算都以它为基准
    except (TypeError, ValueError):
        obs_h = now.hour
    print(f"\n香港时间 {rt[11:16]}  天文台站 {hko_t}°C   今日已观测最高 {mx}°C  (样本 {len(pts)})")
    print(f"  实况源: {src_note}")
    if hko_t is not None and now.hour > obs_h:
        print(f"  ⚠ 天文台观测滞后 {now.hour - obs_h} 小时（最新记录 {rt[11:16]}），"
              f"以下推算全部以观测时刻为基准")
    print(f"  全港最热: " + ", ".join(f"{k} {v}°C" for k, v in hot))
    print(f"  相对天文台: " + ", ".join(f"{k} {v - hko_t:+.0f}" for k, v in hot if hko_t))
    if hko_t is not None and mx is not None:
        print(f"\n  → 结算档位若现在定格: {int(math.floor(mx))}°C  "
              f"(实测 {mx}°C 落在 [{int(math.floor(mx))}.0, {int(math.floor(mx)) + 1}.0))")
    intraday_mx = mx      # 回填后的估计（下面算出来），用于概率下限与过峰守卫
    # ---- 日内外推：当前实测 + 气候升温幅度 + 站点加成 ----
    try:
        calib = json.load(open(os.path.join(DATA, 'model_calib.json')))
        diur = json.load(open(os.path.join(DATA, 'diurnal_climatology.json')))
        # 剩余升温的历史经验分位表（scripts/build_remaining_rise.py 生成）
        _rr = os.path.join(DATA, 'remaining_rise_cdf.json')
        rcdf = json.load(open(_rr)) if os.path.exists(_rr) else None
        if rcdf is None:
            print("  ⓘ 缺少 remaining_rise_cdf.json，回退到气候表+正态"
                  "（python scripts/build_remaining_rise.py 可重建）")
        hh = obs_h       # 以"观测时刻"为基准，不是"现在"——两者不一致时若用现在，
                         # 会拿上一小时的实测去比对下一小时的网格，凭空造出偏差
        if str(now.month) in diur and str(hh) in diur[str(now.month)]:
            d = diur[str(now.month)][str(hh)]
            oh = f_grid.result()['hourly']      # 上面已并行发起，这里取结果
            grid_now = None
            grid_by_h = {}
            for t, v in zip(oh['time'], oh['temperature_2m']):
                if not t.startswith(today) or v is None:
                    continue
                try:
                    h = int(t[11:13])
                except ValueError:
                    continue
                grid_by_h[h] = float(v)
                if h == hh:
                    grid_now = v
            if grid_now is not None and hko_t is not None:
                cur_bias = hko_t - grid_now                 # 观测时刻的 实测−网格
                # —— 回填未采样的观测 ——
                # 「今日已观测最高」原本取自身轮询记录的最大值：一旦漏点就会系统性
                # 低估当日真实最高（2026-09-08 就漏掉了 12、13 时整点）。而它既是
                # 各档位的概率下限，也是「已过峰值守卫」的触发条件——低估等于让模型
                # 以为"还能再升"。这里用今天的站点偏差把网格逐时值翻译成站点估计，
                # 只回填没有实测样本的小时，有实测的永远以实测为准。
                # 回填偏差**封顶在气候值**：单点偏差今天能在 1 小时内摆 ±1.3°C
                # （09-08：15时 +0.3 → 16时 +1.7），直接拿它去外推没采样的小时，
                # 会凭空宣称"中午已经到过 33.1°C"。宁可低估（有实测兜底），不可高估。
                # 偏差**不再封顶在气候值**。旧版 min(cur_bias, 气候偏差) 在干燥晴热天
                # 制造系统性偏冷：当天实测偏差长期维持在 +2.4 而不回归到 +1.01，
                # 封顶等于硬砍掉 1.4°C（2026-09-10 上午 P32 被市场一路打脸就是这么来的）。
                # 改用「今日已采样时段的中位偏差」抗单点噪声，只留一个宽 sane 带。
                devs = []
                for p in pts:
                    try:
                        ph = int(p['t'][11:13])
                    except (TypeError, ValueError):
                        continue
                    if ph in grid_by_h:
                        devs.append(p['hko'] - grid_by_h[ph])
                devs.sort()
                robust_bias = devs[len(devs) // 2] if len(devs) >= 3 else cur_bias
                fill_bias = min(max(robust_bias, calib['bias'] - 1.0),
                                calib['bias'] + 2.5)
                mx_rec = mx
                if mx is not None:
                    for h in sorted(grid_by_h):
                        if h > hh or h in sampled_h:
                            continue
                        est = round(grid_by_h[h] + fill_bias, 1)
                        if est > mx_rec:
                            mx_rec = est
                if mx is not None and mx_rec > mx + 1e-9:
                    intraday_mx = mx_rec
                    print(f"     ⓘ 观测有漏点，已按偏差{fill_bias:+.2f}回填未采样时段："
                          f"已观测最高 {mx}°C → {mx_rec:.1f}°C（测站在其间可能已达此值）")
                # —— 日内外推：one-touch（时变障碍）+ 经验剩余升温分布 ——
                # 结算量是**日内路径的最大值** → 连续监控的向上触碰，不是数字期权。
                # 预测峰值 = max(已观测最高, 今日网格未来日变化最大值 + 当日水位 L)
                #   L      = 实测 − 网格。今日水位**直接观测，不回归到气候均值**：
                #           旧版 k = 气候偏差 − 当下偏差 会在干燥晴热天制造系统性偏冷
                #           （当天偏差长期维持 +2.4 而不回到 +1.01，等于硬砍 1.4°C）。
                #   g_rest = max(grid[t] for t ≥ 观测时刻)，用**今天模式自己**的日变化
                #           曲线，不是气候平均——晚峰日（2026-09-08）也能给出真实剩余升温。
                # 剩余不确定性取历史经验分位表：不假设正态、不需要手调"过峰守卫"。
                # 午后 g_rest 单调下降，pred 会自然收敛到已观测最高（吸收态）。
                # 今日水位 L：用**已采样时段的中位偏差**，不用单点。
                # 单点会把 10 分钟级的站点抖动当成水位变化（13:20 L=+2.50 → 13:30 站点
                # 掉 0.3°C 就变成 +2.20），直接污染 ρ̂ 与 δ。
                L = robust_bias if len(devs) >= 3 else cur_bias
                g_rest = max((v for h, v in grid_by_h.items() if h >= hh), default=None)
                tbl = None
                if rcdf:
                    tbl = (rcdf.get('by_month', {}).get(str(now.month), {}).get(str(hh))
                           or rcdf.get('by_hour', {}).get(str(hh)))
                # 今天模式自己的日变化曲线，只以「相对气候升水的偏离 δ」进入：
                # 气候升水已隐含在 R 的经验分布里，NWP 只贡献增量，点估计不当确定性用。
                # δ 限幅 ±1.0°C——NWP 日变化本身也有误差，不能全信。
                rho_hat = delta = 0.0
                if g_rest is not None and intraday_mx is not None:
                    rho_hat = (g_rest + L) - intraday_mx
                    if tbl:
                        delta = max(-1.0, min(1.0, rho_hat - tbl.get('mean', 0.0)))
                pred = (max(float(intraday_mx), g_rest + L)
                        if (g_rest is not None and intraday_mx is not None)
                        else (float(intraday_mx) if intraday_mx is not None else None))
                print(f"\n  📈 日内外推（one-touch：剩余升温经验分布）")
                print(f"     实测{hko_t}°C / 网格{grid_now}°C → 今日水位 L = {L:+.2f}"
                      f"（单点 {cur_bias:+.2f}，气候 {calib['bias']:+.2f}，均不回归）")
                if g_rest is not None:
                    print(f"     {hh}时后网格最高 {g_rest:.1f}°C（今日模式日变化）"
                          f" → 站点预测 {g_rest + L:.2f}°C，"
                          f"模式升水 ρ̂ = {rho_hat:+.2f}°C")
                if pred is None:
                    raise RuntimeError("无可用外推输入")
                print(f"     ⇒ 今日峰值估计 {pred:.2f}°C（下限 = 已观测最高 {intraday_mx}°C）"
                      f"  → 众数档 {int(math.floor(pred))}°C")
                if tbl:
                    print(f"     剩余升温分布: 经验分位 n={tbl['n']}，气候升水 "
                          f"ρ̄={tbl.get('mean', 0):.2f}°C → δ={delta:+.2f}°C"
                          f"（正态假设近端高估 41%、远端低估 3.4 倍）")
                    print("     ⓘ 分位表基于 ERA5 网格，尾部比真实站点薄得多："
                          "9月 h=13 的 P(剩余升温≥0.5) 网格 7.0% vs 机场站实测 32.8%（4.7 倍）。"
                          "\n       以下概率是**下界**；站点口径约为其 2–5 倍，尾档尤其如此。")
                lo_b = int(math.floor(intraday_mx)) if intraday_mx is not None \
                    else int(math.floor(pred))
                probs_out = {}
                for b in range(lo_b, lo_b + 4):
                    if intraday_mx is not None and b <= intraday_mx:
                        probs_out[b] = 1.0
                        print(f"        到 {b}.0°C  已实测达成  → 概率约 100%")
                        continue
                    need = b - (intraday_mx if intraday_mx is not None else hko_t)
                    if tbl is not None and intraday_mx is not None:
                        prob = cdf_upper(tbl, need - delta)
                    else:
                        # 回退：气候表 + 正态（旧行为，仅当分位表缺失时）
                        rm_ = d['mean'] + (calib['bias'] - cur_bias)
                        prob = (1 - norm_cdf((need - rm_) / d['sd'])) if d['sd'] > 0 \
                            else (1.0 if need <= rm_ else 0.0)
                    probs_out[b] = prob
                    print(f"        到 {b}.0°C  需再升 {need:+.1f}°C  → 概率约 {prob:.0%}")
                _log_forecast(today, rt, hh, intraday_mx, pred, probs_out)
                print("     ⚠️ 阴雨/雷暴日会显著低于此估计；若午后雨已到，以上即为上限")
    except Exception as e:
        print(f"  [日内外推跳过: {e}]")

    if now.hour >= 16:
        print("  ⏰ 已过 16:00，晴天情形下日最高温基本锁定，剩余风险主要来自夜间暖平流")
    elif now.hour < 9:
        print("  🌅 早晨时段，日最高温通常在 14:00-16:00 出现，目前离锁定还很远")
    print_next_refresh(now, now.date(), True)
    ex.shutdown(wait=False)


# ---------------------------------------------------------------- 主流程
def score():
    """给历史预测打分：Brier + 可靠性分层。

    这是 skill 的"体检报告"。没有它，模型偏差只能靠盘中人工发现——
    2026-09-08（午后概率高估约 100 倍）到 09-10（上午系统性偏冷）连续三轮
    都是事后复盘才抓到的。有了它，每次改完分位表/校准参数都能直接看分数变化。
    """
    p = os.path.join(DATA, 'forecast_log.jsonl')
    if not os.path.exists(p):
        print("没有 data/forecast_log.jsonl —— 先多跑几次 --watch 才会累积记录。")
        return
    obs = load_obs()
    rows = [json.loads(l) for l in open(p, encoding='utf-8') if l.strip()]
    scored = []
    for r in rows:
        try:
            d = dt.date.fromisoformat(r['date'])
        except (TypeError, ValueError):
            continue
        if d not in obs:
            continue
        for b, pr in r.get('probs', {}).items():
            scored.append((r.get('hour', -1), float(b), float(pr),
                           1.0 if obs[d] >= float(b) else 0.0))
    if not scored:
        print(f"日志 {len(rows)} 条，但都还没有对应实测值"
              f"（HKO 官方逐日最高 bulk 有滞后，实测最新到 {max(obs) if obs else '—'}）。")
        return
    br = sum((a - b) ** 2 for _, _, a, b in scored) / len(scored)
    print(f"\n预测打分  n={len(scored)} 条（{len(rows)} 次 watch × 档位）  总 Brier = {br:.4f}")
    print("\n  按时点:")
    by_h = {}
    for h, _b, pr, y in scored:
        by_h.setdefault(h, []).append((pr, y))
    for h in sorted(by_h):
        xs = by_h[h]
        print("    %2d时  n=%4d  Brier=%.4f" % (
            h, len(xs), sum((a - b) ** 2 for a, b in xs) / len(xs)))
    print("\n  可靠性（预测区间 → 实际发生频率）:")
    bins = {}
    for _h, _b, pr, y in scored:
        k = min(4, int(pr * 5))
        s, c = bins.get(k, (0, 0))
        bins[k] = (s + y, c + 1)
    for k in sorted(bins):
        s, c = bins[k]
        print("    预测 %3.0f-%3.0f%%  n=%4d  实际 %5.1f%%"
              % (k * 20, k * 20 + 20, c, 100 * s / c))


def _silence_own_console():
    """Windows：若本进程拿到的是一个**只为它新建**的控制台窗口，就地隐藏。

    为什么会有黑框弹出来：`python.exe` / `cmd.exe` 都是「控制台子系统」程序，
    Windows 规定它们必须挂在某个控制台上。当启动它的宿主自己没有控制台时
    （WorkBuddy 桌面版、资源管理器双击、计划任务、某些 GUI 启动器），
    系统会**新建一个控制台窗口**给子进程——那个一闪而过的黑框就是它。
    `--watch --loop` 会一直挂到 17:00，于是框也一直不关。

    安全条件：只有当这个控制台**只挂着自己一个进程**、且 stdout 不在终端上
    （输出已经有别的去处，比如管道或 `> watch.log`）时才隐藏。
    用户在自己的 cmd / Windows Terminal 里手动跑时，控制台上还挂着 cmd.exe，
    列表长度 > 1，这里什么都不做——绝不会把用户自己的窗口弄没。
    """
    if os.name != 'nt':
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        u = ctypes.windll.user32
        # 必须显式声明签名：HWND 是 64 位指针，按默认 int 传会被截断；
        # 且 ShowWindow 住在 user32，不在 kernel32（写成 kernel32.ShowWindow 会
        # AttributeError，被下面的 except 吞掉 —— 表面无事，实际没隐藏）。
        k.GetConsoleWindow.restype = ctypes.c_void_p
        u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        hwnd = k.GetConsoleWindow()
        if not hwnd:
            return                       # 压根没控制台，无所谓
        try:
            if sys.stdout.isatty():      # 用户在交互终端里看着，别动
                return
        except Exception:
            pass
        buf = (ctypes.c_uint * 32)()
        n = k.GetConsoleProcessList(buf, 32)
        if n <= 1:                       # 只挂着自己 → 是专为本进程新建的
            u.ShowWindow(hwnd, 0)        # SW_HIDE
    except Exception:
        pass


def main():
    _silence_own_console()
    # Windows 下 stdout 一旦被重定向（`> watch.log`）就用 GBK，输出里的 ⚠ / 📈
    # 会直接抛 UnicodeEncodeError，把整个 --watch --loop 的采集打掉
    # （2026-09-10 13:50 实测：loop 报 "本次采集失败: UnicodeEncodeError"，当次全废）。
    # 必须在任何 print 之前修好。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', help='目标日期 YYYY-MM-DD（香港日期）')
    ap.add_argument('--market', help='市场价，格式 "31:0.42,32:0.35"')
    ap.add_argument('--bankroll', type=float,
                    help=f'本金(USDC)，给出 {KELLY_FRAC:.0%} Kelly 建议下注额')
    ap.add_argument('--mu', type=float, help='手动覆盖点估计(°C)：把日内实况/官方预报按你的判断加权')
    ap.add_argument('--observed-max', type=float,
                    help='当日已实测到的最高温(°C)：分布截断重命名到该下限（默认自动读 --watch 日志）')
    ap.add_argument('--sigma', type=float,
                    help='剩余总不确定性 sd(°C)，日内用：给出后不再叠加集合距平，直接用 N(mu, sigma)')
    ap.add_argument('--exit-ev', type=float, default=0.15,
                    help='止盈阈值：某方向"现在买入"的 EV ≤ 这个负值时打 ⚠'
                         '（默认 -0.15，含义是市价已明显高于公允值，持有人该检查止盈）')
    ap.add_argument('--hold', help='已持有头寸 "档位:成本价:份数[:Y|N],..."（N=持有 NO），'
                                   '例 "34:0.065:200,33:0.56:70:N" → 输出浮盈与止盈建议')
    ap.add_argument('--watch', action='store_true', help='当日实时追踪')
    ap.add_argument('--loop', action='store_true',
                    help='配合 --watch：常驻进程内反复采集，复用同一条 TLS 连接'
                         '（单次运行的耗时几乎全是握手，常驻只在第一次付这笔钱）')
    ap.add_argument('--loop-min', type=int, default=10, help='--loop 的间隔分钟数（默认 10）')
    ap.add_argument('--until', default='17:00', help='--loop 的停止时刻 HKT（默认 17:00）')
    ap.add_argument('--score', action='store_true',
                    help='给 data/forecast_log.jsonl 里的历史预测打分（Brier + 可靠性）')
    ap.add_argument('--recalibrate', action='store_true')
    ap.add_argument('--ttl', type=int, default=CACHE_TTL,
                    help=f'模式/预报类接口的缓存秒数（默认 {CACHE_TTL}s=15分钟；'
                         f'实况与价格永不缓存）')
    ap.add_argument('--no-cache', action='store_true', help='强制实时拉取，绕过缓存')
    ap.add_argument('--html', action='store_true', help='额外输出 HTML 报告')
    ap.add_argument('--version', action='version', version=f'hk-weather-edge {VERSION}')
    a = ap.parse_args()

    if a.score:
        score()
        return

    if a.watch:
        if a.loop:
            watch_loop(a.loop_min, a.until)
        else:
            watch()
        return

    calib = recalibrate(force=a.recalibrate, use_cache=not a.no_cache, ttl=a.ttl)
    # --sigma 给出时概率直接用 N(mu, sigma)，集合成员不进入计算 → 跳过最慢的那个请求
    need_ens = a.sigma is None
    det, ens, hko = fetch_forecasts(use_cache=not a.no_cache, ttl=a.ttl, need_ens=need_ens)
    _cache_save()
    hko_map = {f"{x['forecastDate'][:4]}-{x['forecastDate'][4:6]}-{x['forecastDate'][6:]}":
               x['forecastMaxtemp']['value'] for x in hko}

    print("=" * 78)
    print(f"结算源: 香港天文台 Daily Extract / Absolute Daily Max (0.1°C), 档位 N = [N.0, N+1.0)")
    print(f"模型校准: bias {calib['bias']:+.2f}°C, 残差 sd {calib['resid_sd']:.2f}°C "
          f"(回测 n={calib['n']}, {calib['window'][0]}~{calib['window'][1]})")
    age = max(_fetch_meta.values(), default=0.0)
    if a.no_cache:
        print(f"数据时效: 强制实时拉取（--no-cache），{len(_fetch_meta)} 个请求并行")
    elif age > 1:
        print(f"数据时效: 缓存命中，最旧数据取自 {age / 60:.1f} 分钟前（TTL {a.ttl / 60:.0f} 分钟）"
              f" —— 模式更新周期 ≥6h，不影响结论")
    else:
        print(f"数据时效: 实时拉取（{len(_fetch_meta)} 个请求并行）")

    # 当日已实测到的最高温 = 峰值的硬下限，用它截断分布（只对当日生效）
    today = dt.datetime.now(TZ).date().isoformat()
    floor_max, floor_src = a.observed_max, None
    if floor_max is not None:
        floor_src = '--observed-max'
    else:
        floor_max = load_observed_max()
        if floor_max is not None:
            floor_src = 'intraday_log.json（--watch 记录）'
    # 只有当日（--date 未给或 == today）才用已观测最高温截断；查未来日期时截断不适用，
    # 此时若照常打印会让读者误以为分布已被截断（实际 bucket_probs 里 floor=None）
    _floor_applies = floor_max is not None and (a.date is None or a.date == today)
    if _floor_applies:
        print(f"已观测下限: {today} 实测最高 {floor_max}°C（来源 {floor_src}）"
              f" → 当日分布截断重命名")
    elif floor_max is not None:
        print(f"注: {today} 已实测最高 {floor_max}°C 已知，但目标日不是当日 → 不做截断")
    print("=" * 78)

    targets = [a.date] if a.date else [d for d in det['time'] if d >= today][:7]
    market = {}
    if a.market:
        for kv in a.market.split(','):
            k, v = kv.split(':')
            market[int(k)] = float(v)

    # 有 edge 就必须给出 Kelly 仓位；未指定本金时按 1000 USDC 估算并明确标注
    bankroll = a.bankroll if a.bankroll else 1000.0
    if not a.bankroll:
        print(f"本金未指定 → 默认按 {bankroll:.0f} USDC 估算 {KELLY_FRAC:.0%} Kelly "
              f"下注额（用 --bankroll 覆盖）")

    results = {}
    for t in targets:
        r = bucket_probs(t, det, ens, calib, mu_override=a.mu,
                         floor=floor_max if t == today else None, sd_override=a.sigma)
        if a.mu is not None:
            r['manual_mu'] = True
        results[t] = r
        lead = (dt.date.fromisoformat(t) - dt.datetime.now(TZ).date()).days
        tag = " [手动覆盖]" if r.get('manual_mu') else ""
        ftag = f"  | 下限 {r['floor']}°C [已截断]" if r.get('floor') is not None else ""
        # 跳过集合请求时（--sigma）离散度不可得，如实标 n/a，不要假装有数
        sp = f"±{r['spread']:.2f}°C" if r.get('spread') is not None else "n/a(--sigma)"
        print(f"\n■ {t} (提前 {lead} 天)  点估计 {r['mu']:.2f}°C{tag}  "
              f"| 集合离散度 {sp}  残差sd {r['sd']:.2f}"
              f"  | HKO官方预报 {hko_map.get(t, '?')}°C{ftag}")
        hdr = f"  {'档位':>6} {'公允P':>8} {'市场价':>8} {'动作':>14} {'EV':>7}"
        hdr += f" {f'{KELLY_FRAC:.0%}Kelly(金额/份数)':>22}"   # 有 edge 就必须给出仓位
        print(hdr + "   分布条")
        for b, p in sorted(r['probs'].items()):
            bar = '█' * int(round(p * 50))
            mk = market.get(b)
            if mk is None:
                line = f"  {b:>4}°C {p:>7.1%} {'-':>8} {'-':>14} {'-':>7}"
                line += f" {'-':>22}"
                print(line + f"   {bar}")
                continue
            ev_buy = p / mk - 1
            ev_sell = (1 - p) / (1 - mk) - 1
            MIN_EDGE = 0.08   # 低于 8% 不下手：你的概率本身有 ±5% 量级误差
            if ev_buy > MIN_EDGE:
                act, ev, side = "买 YES ▲", ev_buy, 'yes'
            elif ev_sell > MIN_EDGE:
                act, ev, side = "买 NO ▼", ev_sell, 'no'
            else:
                act, ev, side = f"观望 (edge不足)", max(ev_buy, ev_sell), None
            line = f"  {b:>4}°C {p:>7.1%} {mk:>7.1%} {act:>14} {ev:>+6.0%}"
            if side == 'yes':
                kf = (p - mk) / (1 - mk)
            elif side == 'no':
                kf = (mk - p) / mk
            else:
                kf = 0
            stake = max(0.0, kf) * bankroll * KELLY_FRAC
            # 金额之外必须给份数：下单界面要填的是份数，不是美元
            if stake >= 1 and side is not None:
                unit = mk if side == 'yes' else (1 - mk)      # 该方向的单价
                shares = stake / unit if unit > 0 else 0.0
                cell = (f"${stake:.0f} / {shares:.0f}份 "
                        f"({max(0.0, kf) * KELLY_FRAC * 100:.1f}%)")
            else:
                cell = '-'
            line += f" {cell:>22}"
            # 反向 edge = 止盈信号：这个方向的 EV 很负，说明市价已明显高于公允值，
            # 还拿着这个方向头寸的人应该考虑平仓，而不是继续持有到期。
            warns = []
            if ev_buy <= -a.exit_ev:
                warns.append(f"⚠持YES止盈{ev_buy:.0%}")
            if ev_sell <= -a.exit_ev:
                warns.append(f"⚠持NO止盈{ev_sell:.0%}")
            if warns:
                line += "  " + " ".join(warns)
            print(line + f"   {bar}")
        top = max(r['probs'].items(), key=lambda x: x[1])
        print(f"  → 众数档 {top[0]}°C 仅 {top[1]:.1%}: 即使完美预测期望值,"
              f"命中整数档的概率也不到一半 —— 别把点预报当概率")
    if market:
        print("\n  份数 = 建议金额 ÷ 该档单价（买 YES 用市场价 mk，买 NO 用 1−mk）；"
              "下单前按订单簿可执行价重算一次（见 A0）——单价差 1 分钱，份数会差不少。")
        print(f"  ⚠ 止盈提醒：某方向「现在买入」的 EV ≤ {-a.exit_ev:.0%} 时会打 ⚠，"
              f"含义是市价已明显高于公允值 —— 持有该方向头寸者应检查止盈。")

    # ---- 持仓检查：把已用头寸喂进来，直接算浮盈与该不该止盈 ----
    if a.hold:
        t0 = targets[-1]
        r0 = results[t0]
        print(f"\n■ 持仓检查 {t0}（止盈阈值：现在买入 EV ≤ {-a.exit_ev:.0%}）")
        print(f"  {'头寸':>10} {'份数':>7} {'成本':>7} {'现价':>7} {'浮盈':>8} {'公允':>8} "
              f"{'现在买入EV':>10}   建议")
        for item in a.hold.split(','):
            ps = [x for x in item.split(':') if x]
            try:
                b, ent, sh = int(ps[0]), float(ps[1]), float(ps[2])
                side = (ps[3] if len(ps) > 3 else 'Y').upper()
            except Exception:
                print(f"  [跳过，格式应为 档位:成本:份数[:Y|N] -> {item}]")
                continue
            p = r0['probs'].get(b)
            if p is None:
                print(f"  {b}°C 不在公允分布内，跳过")
                continue
            fair = p if side != 'N' else 1 - p
            mk = market.get(b)
            if mk is None:
                print(f"  {b}°C {'YES' if side != 'N' else 'NO '} {sh:.0f}份 @{ent:.3f}"
                      f"   公允 {fair:.1%}（未给该档市场价，算不了浮盈）")
                continue
            cur = mk if side != 'N' else 1 - mk
            pnl = (cur - ent) / ent if ent > 0 else 0.0
            ev_now = fair / cur - 1 if cur > 0 else 0.0
            if ev_now <= -a.exit_ev:
                adv = f"⚠ 止盈（现价高出公允 {-ev_now:.0%}）"
            elif ev_now >= 0.08:
                adv = f"仍低估 {ev_now:.0%} → 持有/可加仓"
            else:
                adv = "持有观察（edge 不足）"
            name = f"{b}°C {'YES' if side != 'N' else 'NO '}"
            print(f"  {name:>10} {sh:>7.0f} {ent:>7.3f} {cur:>7.3f} {pnl:>+8.0%} "
                  f"{fair:>8.1%} {ev_now:>+10.0%}   {adv}")

    # 决策留痕：攒够 30 笔就能回头校准自己的命中率（长期真正的 edge 来源）
    if market:
        import csv
        lp = os.path.join(DATA, 'decision_log.csv')
        new = not os.path.exists(lp)
        with open(lp, 'a', newline='') as f:
            w = csv.writer(f)
            if new:
                w.writerow(['logged_at', 'target_date', 'bucket', 'fair_p', 'market_p', 'edge'])
            for t in targets:
                for b, p in sorted(results[t]['probs'].items()):
                    if b in market:
                        w.writerow([dt.datetime.now(TZ).strftime('%Y-%m-%d %H:%M'), t, b,
                                    round(p, 4), market[b], round(p - market[b], 4)])
        print(f"\n[已记录] {len(market)} 档写入 data/decision_log.csv —— 结算后回来填实际档位做复盘")

    if a.html:
        write_html(results, calib, hko_map, market)

    # 结论末尾固定提醒下次刷新：结论只在下一批新数据落地前有效
    _tgt = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(TZ).date()
    print_next_refresh(dt.datetime.now(TZ), _tgt, (a.date is None or a.date == today))


def write_html(results, calib, hko_map, market):
    rows = []
    for t, r in results.items():
        for b, p in sorted(r['probs'].items()):
            mk = market.get(b)
            rows.append((t, b, p, mk))
    trs = []
    for t, b, p, mk in rows:
        if mk is not None:
            ev = p / mk - 1
            sev = (1 - p) / (1 - mk) - 1
            cls = 'good' if ev > 0.08 else ('bad' if sev > 0.08 else '')
            act = f"买 YES {ev:+.0%}" if ev > 0.08 else (f"买 NO {sev:+.0%}" if sev > 0.08 else "—")
        else:
            cls, act = '', '—'
        trs.append(f"<tr class='{cls}'><td>{t}</td><td>{b}°C</td><td class='num'>{p:.1%}</td>"
                   f"<td class='num'>{f'{mk:.0%}' if mk is not None else '—'}</td><td>{act}</td>"
                   f"<td><div class='bar' style='width:{p*220:.0f}px'></div></td></tr>")
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>香港气温市场 Edge 报告</title><style>
body{{font-family:-apple-system,"PingFang SC",sans-serif;background:#0e1117;color:#e6e6e6;padding:28px;max-width:960px;margin:auto}}
h1{{font-size:22px}} .meta{{color:#8b949e;font-size:13px;line-height:1.7}}
table{{border-collapse:collapse;width:100%;margin-top:18px;font-size:14px}}
th,td{{padding:8px 10px;border-bottom:1px solid #222;text-align:left}}
th{{color:#8b949e;font-weight:500}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
tr.good{{background:rgba(63,185,80,.10)}} tr.bad{{background:rgba(248,81,73,.10)}}
.bar{{height:10px;background:linear-gradient(90deg,#58a6ff,#3fb950);border-radius:2px}}
code{{background:#161b22;padding:2px 6px;border-radius:4px;color:#79c0ff}}
</style></head><body>
<h1>Polymarket 香港最高气温 — 公允概率与 Edge</h1>
<div class="meta">
结算源：香港天文台 <code>Daily Extract / Absolute Daily Max</code>（0.1 °C，档位 N = [N.0, N+1.0)）<br>
模型校准：bias {calib['bias']:+.2f} °C，残差 sd {calib['resid_sd']:.2f} °C（回测 n={calib['n']}，{calib['window'][0]} ~ {calib['window'][1]}）<br>
生成时间：{dt.datetime.now(TZ).strftime('%Y-%m-%d %H:%M')} HKT
</div>
<table><tr><th>日期</th><th>档位</th><th class="num">公允概率</th><th class="num">市场价</th><th>动作</th><th>分布</th></tr>
{''.join(trs)}</table>
</body></html>"""
    p = os.path.join(HERE, 'edge_report.html')
    open(p, 'w', encoding='utf-8').write(html)
    print(f"\nHTML 报告已生成: {p}")


if __name__ == '__main__':
    main()
