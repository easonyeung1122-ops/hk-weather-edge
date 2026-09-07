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

依赖：pip3 install requests（没有也能跑，会回落到 urllib）
"""
import argparse, json, math, os, sys, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor

VERSION = "0.5.0"      # 语义化版本，见 CHANGELOG.md；每次推送 GitHub 前必须递增

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
OM_FC     = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
             "&daily=temperature_2m_max&forecast_days=10&timezone=Asia%2FShanghai&models={m}")
OM_ENS    = ("https://ensemble-api.open-meteo.com/v1/ensemble?latitude={lat}&longitude={lon}"
             "&daily=temperature_2m_max&forecast_days=10&timezone=Asia%2FShanghai&models=ecmwf_ifs025")
OM_PREV   = ("https://previous-runs-api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
             "&daily=temperature_2m_max&past_days=92&forecast_days=1&timezone=Asia%2FShanghai&models={m}")
OM_HR     = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
             "&hourly=temperature_2m&forecast_days=1&past_hours=3"
             "&timezone=Asia%2FShanghai&models=best_match")


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


def fetch_forecasts(use_cache=True, ttl=CACHE_TTL):
    """多模型确定性融合 + ECMWF 集合 + HKO 官方预报（三个接口并行拉取）"""
    _cache_load()          # 先把缓存读进内存，避免线程里重复读盘
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_det = ex.submit(_get, OM_FC.format(lat=OBS_LAT, lon=OBS_LON, m=MODELS),
                          60, use_cache, ttl)
        f_ens = ex.submit(_get, OM_ENS.format(lat=OBS_LAT, lon=OBS_LON), 60, use_cache, ttl)
        f_hko = ex.submit(_get, HKO_FND, 60, use_cache, ttl)
        det = f_det.result()['daily']
        ens = f_ens.result()['daily']
        try:
            hko = f_hko.result()['weatherForecast']
        except Exception:
            hko = []
    return det, ens, hko


def load_observed_max():
    """从 --watch 的日内日志里读「今日已实测到的最高温度」。

    日最高温具有单调性：已经观测到的值就是当日峰值的下限。
    日内复盘时必须以此为条件截断分布，否则会给出物理上不可能的低档概率。
    """
    p = os.path.join(DATA, 'intraday_log.json')
    if not os.path.exists(p):
        return None
    try:
        log = json.load(open(p))
        if log.get('date') != dt.datetime.now(TZ).date().isoformat():
            return None
        vals = [pt.get('hko') for pt in log.get('points', []) if pt.get('hko') is not None]
        return max(vals) if vals else None
    except Exception:
        return None


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

    members = [ens[k][i] for k in ens if k.startswith('temperature_2m_max_member')
               and ens[k][i] is not None]
    if not members:
        members = [det_mean]
    ens_mean = sum(members) / len(members)
    mu = det_mean + calib['bias'] if mu_override is None else mu_override
    spread = math.sqrt(sum((x - ens_mean) ** 2 for x in members) / len(members))
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
def watch():
    """实时追踪：记录当日天文台站已观测到的最高温度（供 cron 每 10 分钟调用）"""
    # 观测与网格实况互不依赖 → 并行拉取（两者都属实况，永不缓存）
    ex = ThreadPoolExecutor(max_workers=2)
    f_rhr = ex.submit(_get, HKO_RHR, 30)
    f_grid = ex.submit(_get, OM_HR.format(lat=OBS_LAT, lon=OBS_LON), 30)
    try:
        r = f_rhr.result()
    except Exception as e:
        print(f"[err] 无法读取天文台实时数据: {e}")
        ex.shutdown(wait=False)
        return
    temp = {t['place']: t['value'] for t in r['temperature']['data']}
    rt = r['temperature']['recordTime']
    hko_t = temp.get('Hong Kong Observatory')
    today = dt.datetime.now(TZ).date().isoformat()
    log_p = os.path.join(DATA, 'intraday_log.json')
    log = json.load(open(log_p)) if os.path.exists(log_p) else {}
    if log.get('date') != today:
        log = {'date': today, 'points': []}
    log['points'].append({'t': rt, 'hko': hko_t, 'all': temp})
    json.dump(log, open(log_p, 'w'))

    pts = [p for p in log['points'] if p['hko'] is not None]
    mx = max(p['hko'] for p in pts) if pts else None
    others = {k: v for k, v in temp.items() if k != 'Hong Kong Observatory'}
    hot = sorted(others.items(), key=lambda x: -x[1])[:5]
    print(f"\n香港时间 {rt[11:16]}  天文台站 {hko_t}°C   今日已观测最高 {mx}°C  (样本 {len(pts)})")
    print(f"  全港最热: " + ", ".join(f"{k} {v}°C" for k, v in hot))
    print(f"  相对天文台: " + ", ".join(f"{k} {v - hko_t:+.0f}" for k, v in hot if hko_t))
    if hko_t is not None and mx is not None:
        print(f"\n  → 结算档位若现在定格: {int(math.floor(mx))}°C  "
              f"(实测 {mx}°C 落在 [{int(math.floor(mx))}.0, {int(math.floor(mx)) + 1}.0))")
    now = dt.datetime.now(TZ)
    # ---- 日内外推：当前实测 + 气候升温幅度 + 站点加成 ----
    try:
        calib = json.load(open(os.path.join(DATA, 'model_calib.json')))
        diur = json.load(open(os.path.join(DATA, 'diurnal_climatology.json')))
        hh = now.hour
        if str(now.month) in diur and str(hh) in diur[str(now.month)]:
            d = diur[str(now.month)][str(hh)]
            oh = f_grid.result()['hourly']      # 上面已并行发起，这里取结果
            grid_now = None
            for t, v in zip(oh['time'], oh['temperature_2m']):
                if t.startswith(today) and int(t[11:13]) == hh:
                    grid_now = v
            if grid_now is not None and hko_t is not None:
                cur_bias = hko_t - grid_now                 # 此刻 实测−网格
                k = calib['bias'] - cur_bias                # 站点升温加成
                # —— 已过峰值守卫 ——
                # 气候表 d['mean'] = 平均而言"还能升多少"，它是无条件统计：既包含
                # 仍在升温的日子，也包含"已冲到峰值又回落"的日子。一旦实测已经比
                # 今日已观测最高低了一截(回落 g)，且处于典型峰值时段(14 时)之后，
                # 当日峰值大概率已定格——无条件期望必须大幅收窄，只留少量
                # "晚些反弹创出新高"的可能。这正是"外推低于已实测最高"这族问题
                # 的另一面：前者发生在早上(网格还没到过那么高)，后者在下午(已经到过)。
                g = float(mx - hko_t) if mx is not None else 0.0
                post = g >= 0.3 and hh >= 14
                if post:
                    s_hour = min(1.0, max(0.0, (hh - 14) / 2.0))   # 14时→0, 16时→1
                    s_gap = min(1.0, max(0.0, (g - 0.3) / 1.2))    # 回落0.3→0, 1.5→1
                    s = min(s_hour, s_gap)                         # 两者都到位才算过峰
                    gross = d['mean'] + k
                    net = max(0.0, gross - g)          # 已回落的幅度不再支撑新高
                    rise_m = net * (1 - 0.75 * s)      # 过峰越强，剩余升温期望越收窄
                    rise_sd = max(0.1, d['sd'] * (1 - 0.4 * s))
                else:
                    rise_m = d['mean'] + k
                    rise_sd = d['sd']
                peak_raw = hko_t + rise_m
                if mx is not None and peak_raw < mx:
                    if not post:
                        print(f"\n  ⚠ 外推 {peak_raw:.2f}°C 低于今日已观测最高 {mx}°C，"
                              f"已按已观测值取下限（日内最高具有单调性）")
                    peak = float(mx)
                else:
                    peak = peak_raw
                print(f"\n  📈 日内外推（气候基准 n={d['n']}）")
                print(f"     此刻 实测{hko_t}°C / 网格{grid_now}°C → 站点偏差{cur_bias:+.2f}")
                print(f"     {hh}时后气候平均还能升 {d['mean']:.2f}°C(±{d['sd']:.2f})，"
                      f"站点加成 {k:+.2f}")
                if post:
                    print(f"     ⚠ 已过峰值信号：实测已从今日最高回落 {g:.1f}°C"
                          f"（{hh} 时 ≥ 14 时典型峰值窗口）"
                          f" → 剩余升温期望 {d['mean'] + k:.2f} → {rise_m:.2f}°C，"
                          f"离散 {rise_sd:.2f}°C")
                print(f"     ⇒ 今日峰值估计 {peak:.2f}°C  "
                      f"区间[{peak - rise_sd:.1f}, {peak + rise_sd:.1f}]  "
                      f"→ 众数档 {int(math.floor(peak))}°C")
                for b in range(int(math.floor(peak)) - 1, int(math.floor(peak)) + 3):
                    need = b - hko_t
                    if mx is not None and b <= mx:
                        prob = 1.0   # 今日已实测到该温度，达成概率为 100%
                    elif rise_sd > 0:
                        z = (need - rise_m) / rise_sd
                        prob = 1 - norm_cdf(z)
                    else:
                        prob = 1.0 if need <= rise_m else 0.0
                    print(f"        到 {b}.0°C 还需升 {need:+.1f}°C  "
                          f"→ 概率约 {prob:.0%}")
                print("     ⚠️ 阴雨/雷暴日会显著低于此估计；若午后雨已到，以上即为上限")
    except Exception as e:
        print(f"  [日内外推跳过: {e}]")

    if now.hour >= 16:
        print("  ⏰ 已过 16:00，晴天情形下日最高温基本锁定，剩余风险主要来自夜间暖平流")
    elif now.hour < 9:
        print("  🌅 早晨时段，日最高温通常在 14:00-16:00 出现，目前离锁定还很远")
    ex.shutdown(wait=False)


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', help='目标日期 YYYY-MM-DD（香港日期）')
    ap.add_argument('--market', help='市场价，格式 "31:0.42,32:0.35"')
    ap.add_argument('--bankroll', type=float, help='本金(USDC)，给出 1/4 Kelly 建议下注额')
    ap.add_argument('--mu', type=float, help='手动覆盖点估计(°C)：把日内实况/官方预报按你的判断加权')
    ap.add_argument('--observed-max', type=float,
                    help='当日已实测到的最高温(°C)：分布截断重命名到该下限（默认自动读 --watch 日志）')
    ap.add_argument('--sigma', type=float,
                    help='剩余总不确定性 sd(°C)，日内用：给出后不再叠加集合距平，直接用 N(mu, sigma)')
    ap.add_argument('--watch', action='store_true', help='当日实时追踪')
    ap.add_argument('--recalibrate', action='store_true')
    ap.add_argument('--ttl', type=int, default=CACHE_TTL,
                    help=f'模式/预报类接口的缓存秒数（默认 {CACHE_TTL}s=15分钟；'
                         f'实况与价格永不缓存）')
    ap.add_argument('--no-cache', action='store_true', help='强制实时拉取，绕过缓存')
    ap.add_argument('--html', action='store_true', help='额外输出 HTML 报告')
    ap.add_argument('--version', action='version', version=f'hk-weather-edge {VERSION}')
    a = ap.parse_args()

    if a.watch:
        watch()
        return

    calib = recalibrate(force=a.recalibrate, use_cache=not a.no_cache, ttl=a.ttl)
    det, ens, hko = fetch_forecasts(use_cache=not a.no_cache, ttl=a.ttl)
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
    if floor_max is not None:
        print(f"已观测下限: {today} 实测最高 {floor_max}°C（来源 {floor_src}）"
              f" → 当日分布截断重命名")
    print("=" * 78)

    targets = [a.date] if a.date else [d for d in det['time'] if d >= today][:7]
    market = {}
    if a.market:
        for kv in a.market.split(','):
            k, v = kv.split(':')
            market[int(k)] = float(v)

    # 有 edge 就必须给出 1/4 Kelly 仓位；未指定本金时按 1000 USDC 估算并明确标注
    bankroll = a.bankroll if a.bankroll else 1000.0
    if not a.bankroll:
        print(f"本金未指定 → 默认按 {bankroll:.0f} USDC 估算 1/4 Kelly 下注额（用 --bankroll 覆盖）")

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
        print(f"\n■ {t} (提前 {lead} 天)  点估计 {r['mu']:.2f}°C{tag}  "
              f"| 集合离散度 ±{r['spread']:.2f}°C  残差sd {r['sd']:.2f}"
              f"  | HKO官方预报 {hko_map.get(t, '?')}°C{ftag}")
        hdr = f"  {'档位':>6} {'公允P':>8} {'市场价':>8} {'动作':>14} {'EV':>7}"
        hdr += f" {'1/4Kelly下注':>12}"   # 始终给出：有 edge 就必须给出 1/4 Kelly 仓位
        print(hdr + "   分布条")
        for b, p in sorted(r['probs'].items()):
            bar = '█' * int(round(p * 50))
            mk = market.get(b)
            if mk is None:
                line = f"  {b:>4}°C {p:>7.1%} {'-':>8} {'-':>14} {'-':>7}"
                line += f" {'-':>12}"
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
            stake = max(0.0, kf) * bankroll * 0.25
            line += f" {'$' + format(stake, '.0f') + f' ({max(0.0, kf) * 25:.1f}%)':>16}" \
                if stake >= 1 else f" {'-':>16}"
            print(line + f"   {bar}")
        top = max(r['probs'].items(), key=lambda x: x[1])
        print(f"  → 众数档 {top[0]}°C 仅 {top[1]:.1%}: 即使完美预测期望值,"
              f"命中整数档的概率也不到一半 —— 别把点预报当概率")

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
