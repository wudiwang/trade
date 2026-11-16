"""急跌提醒阈值校准(近实时提醒引擎 app/engine/alerts.py 的配套脚本)。

做什么:
  1. 下载约 1 年 BTCUSDT 合约 1m K线(公开接口, 无需密钥), 缓存在 .btcache/。
  2. 用【引擎同一套类】(RollingExtreme + DropTrigger: 同样的滑动窗口/冷却/加深升级逻辑)
     回放, 打印各阈值下"每周会响几次"(均值 + 分布: 安静周/忙碌周)。
  3. 用完整的 AlertEngine 端到端回放模块默认值, 报告急跌提醒(5m+15m 合计)与
     "跌破 N 日低点"提醒的每周次数。

只统计【触发次数】, 不统计任何触发后的涨跌/胜率/期望 —— 这是提醒频率校准, 不是策略回测。

tick 代理(实盘是 ~1 秒一个报价, 历史只有 1m K线):
  默认 ohlc: 每根 1m K线展开成 4 个报价 —— 开盘, 然后 高/低(阳线先低后高, 阴线先高后低), 收盘。
            窗口高点取得到 1m 最高价, 现价摸得到 1m 最低价。但一分钟只有 4 笔, 比实盘稀得多:
            粗粒度的一跳会"跳过"阈值(幅度记得偏大, 吃掉后面的加深档), 两个窗口也更容易落在同一笔上
            合并成一条 → 次数【偏少】。
  --proxy dense: 同样四个锚点, 但中间按直线插值成每秒一笔(不引入新的极值, 只补上实盘也会看到的中间价)。
            这是最接近实盘节奏(每币每秒一笔)的口径, 【默认阈值的条数以它为准】。慢(一年 ≈ 3150 万笔),
            所以只跑阈值表 + 一次端到端回放; 破位的冷却/N 日两张表用 ohlc 口径看(加 --full 强制全跑)。
  --proxy close: 只用 1m 收盘价(看不到影线 → 次数偏少, 可当下界)。
  --proxy low:   每根只喂 1m 最低价(窗口高点也只由"低点"构成 → 偏少)。

用法:
  python scripts/alert_calibrate.py                 # 用缓存(没有就下载)跑完整校准(ohlc 口径)
  python scripts/alert_calibrate.py --proxy dense   # 每秒一笔的口径(默认阈值的条数以它为准, 约几分钟)
  python scripts/alert_calibrate.py --download-only # 只下载/补齐缓存
  python scripts/alert_calibrate.py --refresh       # 忽略缓存重新下载
  python scripts/alert_calibrate.py --show 5        # 另外打印最近 5 条提醒原文
"""
import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")     # Windows 控制台中文输出
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".btcache")
BASE = "https://fapi.binance.com"
MIN_MS = 60_000
DAY_MS = 86_400_000
WEEK_S = 7 * 86400.0
WARMUP_DAYS = 7          # 跌破N日低点需要历史打底; 这几天只喂数据不计数
REQ_GAP_S = 0.4          # 每次请求间隔(limit=1500 权重10; 2400/分钟上限 → 留一半余量)


# ---------------------------------------------------------------- 下载 + 缓存
def cache_path(symbol: str) -> str:
    return os.path.join(CACHE, f"alertcal_{symbol}_1m.json.gz")


def _load_cache(symbol: str) -> dict:
    p = cache_path(symbol)
    if not os.path.exists(p):
        return {}
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            rows = json.load(f)["rows"]
        return {int(r[0]): r for r in rows}
    except Exception as e:
        print(f"  缓存损坏, 忽略重下: {e}", flush=True)
        return {}


def _save_cache(symbol: str, by_t: dict) -> None:
    os.makedirs(CACHE, exist_ok=True)
    p = cache_path(symbol)
    tmp = p + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump({"symbol": symbol, "interval": "1m", "cols": ["t", "o", "h", "l", "c", "v"],
                   "rows": [by_t[t] for t in sorted(by_t)]}, f, separators=(",", ":"))
    os.replace(tmp, p)


def _get_klines(symbol: str, start_ms: int, limit: int = 1500):
    qs = urllib.parse.urlencode({"symbol": symbol, "interval": "1m",
                                 "limit": limit, "startTime": start_ms})
    req = urllib.request.Request(f"{BASE}/fapi/v1/klines?{qs}",
                                 headers={"User-Agent": "kline-cache/1.0"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                used = int(r.headers.get("X-MBX-USED-WEIGHT-1M") or 0)
                return json.loads(r.read().decode("utf-8")), used
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
                ra = e.headers.get("Retry-After", "?")
                raise SystemExit(f"币安限速/封禁 HTTP {e.code}, Retry-After={ra}s —— 中止, 稍后再跑(已下的在缓存里)")
            print(f"  HTTP {e.code}, 重试 {attempt + 1}/5", flush=True)
        except Exception as e:
            print(f"  网络错误 {type(e).__name__}: {e}, 重试 {attempt + 1}/5", flush=True)
        time.sleep(2 + attempt * 2)
    raise SystemExit("下载失败(重试 5 次)")


def _fetch_range(symbol: str, by_t: dict, start_ms: int, end_ms: int, now_ms: int) -> int:
    """把 [start_ms, end_ms) 的已收盘 1m K线补进 by_t, 返回新增根数。"""
    cur, added, calls = start_ms, 0, 0
    while cur < end_ms:
        batch, used = _get_klines(symbol, cur)
        calls += 1
        if not batch:
            break
        for k in batch:
            t = int(k[0])
            if t >= end_ms or int(k[6]) >= now_ms:      # 未收盘的不要
                continue
            if t not in by_t:
                added += 1
            by_t[t] = [t, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
        nxt = int(batch[-1][0]) + MIN_MS
        if nxt <= cur:
            break
        cur = nxt
        if calls % 40 == 0:
            print(f"  ... {time.strftime('%Y-%m-%d', time.gmtime(cur / 1000))} "
                  f"(+{added} 根, 已用权重 {used}/2400)", flush=True)
            _save_cache(symbol, by_t)                    # 断点续传
        if len(batch) < 1500:
            break
        time.sleep(REQ_GAP_S if used < 1500 else 15)     # 权重偏高就多歇
    return added


def load_1m(symbol: str, days: int, refresh: bool = False) -> list:
    """返回最近 days 天的已收盘 1m K线 [[t,o,h,l,c,v], ...] 升序; 缺的部分自动下载。"""
    now_ms = int(time.time() * 1000)
    end_ms = now_ms // MIN_MS * MIN_MS
    start_ms = end_ms - days * DAY_MS
    by_t = {} if refresh else _load_cache(symbol)
    have = sorted(t for t in by_t if start_ms <= t < end_ms)
    todo = []
    if not have:
        todo.append((start_ms, end_ms))
    else:
        if have[0] - start_ms > 5 * MIN_MS:
            todo.append((start_ms, have[0]))
        prev = have[0]
        for t in have[1:]:                                # 中间的洞(>5分钟)
            if t - prev > 5 * MIN_MS:
                todo.append((prev + MIN_MS, t))
            prev = t
        if end_ms - have[-1] > 24 * 60 * MIN_MS:          # 尾部落后超过 1 天才补(校准不差这几个小时)
            todo.append((have[-1] + MIN_MS, end_ms))
    added = 0
    for a, b in todo:
        print(f"下载 {symbol} 1m: {time.strftime('%Y-%m-%d %H:%M', time.gmtime(a / 1000))} → "
              f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(b / 1000))} UTC", flush=True)
        added += _fetch_range(symbol, by_t, a, b, now_ms)
    if added:
        _save_cache(symbol, by_t)
        print(f"缓存已更新: {cache_path(symbol)} (+{added} 根)", flush=True)
    rows = [by_t[t] for t in sorted(by_t)]
    return rows[-days * 1440:]                            # 以缓存里最新一根为终点, 往前取 days 天


# ---------------------------------------------------------------- 回放
def ticks_of(row, proxy: str):
    """一根 1m K线 → ((秒级时间戳, 价格), ...) 合成报价。"""
    t = row[0] / 1000.0
    o, h, l, c = row[1], row[2], row[3], row[4]
    if proxy == "close":
        return ((t + 59.0, c),)
    if proxy == "low":
        return ((t + 30.0, l),)
    if c >= o:                                   # 阳线: 开 → 低 → 高 → 收
        pts = ((t, o), (t + 20.0, l), (t + 40.0, h), (t + 59.0, c))
    else:                                        # 阴线: 开 → 高 → 低 → 收
        pts = ((t, o), (t + 20.0, h), (t + 40.0, l), (t + 59.0, c))
    if proxy != "dense":
        return pts
    out = []                                     # 每秒一笔: 锚点之间直线插值, 不产生新的极值
    for (ta, pa), (tb, pb) in zip(pts, pts[1:]):
        n = int(tb - ta)
        out.append((ta, pa))
        if pa == pb:
            out.extend((ta + i, pa) for i in range(1, n))
        else:
            d = (pb - pa) / n
            out.extend((ta + i, pa + d * i) for i in range(1, n))
    out.append(pts[-1])
    return out


def week_stats(stamps, t0: float, weeks: int) -> dict:
    """触发时间戳 → 每周次数的分布。"""
    per = [0] * weeks
    for ts in stamps:
        i = int((ts - t0) // WEEK_S)
        if 0 <= i < weeks:
            per[i] += 1
    srt = sorted(per)
    n = len(per)
    return {
        "total": sum(per), "mean": sum(per) / n, "median": srt[n // 2],
        "p90": srt[min(n - 1, int(n * 0.9))], "max": srt[-1],
        "quiet": sum(1 for x in per if x == 0),            # 一次都不响的周
        "busy": sum(1 for x in per if x >= 5),             # 一周响 5 次以上
        "per": per,
    }


def _row(label: str, st: dict, extra: str = "") -> str:
    return (f"  {label:<14} 合计{st['total']:>5}  均值{st['mean']:>6.2f}/周  中位{st['median']:>3}  "
            f"P90{st['p90']:>4}  最多{st['max']:>4}  安静周(0次){st['quiet']:>3}  忙碌周(>=5){st['busy']:>3}{extra}")


def drop_grid(rows, t0: float, proxy: str, grid: dict, cooldown_min: dict, step_ratio: float) -> dict:
    """急跌提醒: 每个窗口一条 RollingExtreme, 每个阈值一个 DropTrigger —— 与引擎 on_tick 里完全同一套类。
    返回 {(窗口分钟, 阈值): {"all": [ts...], "new": n, "deeper": n, "lower": n}}。
    deeper = 冷却内窗口幅度再加深一个步长; lower = 冷却内现价比上次提醒价再低一个步长(第二段)。"""
    from app.engine.alerts import DropTrigger, RollingExtreme
    out = {}
    plan = []
    for m, pcts in grid.items():
        roll = RollingExtreme(m * 60.0, 1)
        trigs = []
        for pct in pcts:
            rec = {"all": [], "new": 0, "deeper": 0, "lower": 0}
            out[(m, pct)] = rec
            trigs.append((DropTrigger(pct, cooldown_min[m] * 60.0, round(pct * step_ratio, 4)), rec))
        plan.append((roll, trigs, min(pcts)))
    for row in rows:
        for ts, px in ticks_of(row, proxy):
            for roll, trigs, lo in plan:
                _, ext = roll.update(ts, px)
                move = (ext - px) / ext * 100.0
                if move < lo:
                    continue
                for trig, rec in trigs:
                    if move >= trig.pct:
                        kind = trig.check(ts, move, px)
                        if kind and ts >= t0:
                            rec["all"].append(ts)
                            rec[kind] += 1
    return out


class _Cfg:
    """最小配置桩: 只实现 get(dotted, default)。"""

    def __init__(self, values: dict):
        self.v = values

    def get(self, dotted, default=None):
        return self.v.get(dotted, default)


def aggregate(rows, tf_s: int) -> list:
    """1m → 更大级别的已收盘K线(引擎缓存同款字典行)。不完整的首尾桶丢弃。"""
    out, cur, n = [], None, 0
    tf_ms = tf_s * 1000
    need = tf_s // 60
    for r in rows:
        b = r[0] // tf_ms * tf_ms
        if cur is None or cur["open_time"] != b:
            if cur is not None and n == need:
                out.append(cur)
            cur = {"open_time": b, "open": r[1], "high": r[2], "low": r[3], "close": r[4],
                   "volume": r[5], "quote_volume": 0.0, "taker_buy": 0.0, "closed": 1}
            n = 1
        else:
            if r[2] > cur["high"]:
                cur["high"] = r[2]
            if r[3] < cur["low"]:
                cur["low"] = r[3]
            cur["close"] = r[4]
            cur["volume"] += r[5]
            n += 1
    if cur is not None and n == need:
        out.append(cur)
    return out


def engine_replay(rows, symbol: str, t0: float, proxy: str, overrides: dict | None = None) -> dict:
    """用完整的 AlertEngine 端到端回放: 逐笔 on_tick + 每根 5m/15m/1h 收盘 on_closed_bar。
    get_bars 模拟引擎缓存(每个级别只给最近 550 根已收盘K线)。
    返回 {"drop": [ts], "range": [ts](新的一次破位), "range_more": [ts](同一次破位里的续跌),
           "msgs": [(ts, 文本)], "status": {...}}。"""
    import tempfile
    from pathlib import Path
    from app.db import DB
    from app.engine.alerts import AlertEngine

    tfs = {"5m": 300, "15m": 900, "1h": 3600}
    agg = {tf: aggregate(rows, s) for tf, s in tfs.items()}
    ends = {tf: [b["open_time"] + s * 1000 for b in agg[tf]] for tf, s in tfs.items()}
    idx = {tf: 0 for tf in tfs}                      # 已收盘根数(指针)
    clock = [rows[0][0] / 1000.0]

    def get_bars(sym, tf):
        if sym != symbol or tf not in agg:
            return []
        i = idx[tf]
        return agg[tf][max(0, i - 550):i]

    values = {"alerts.symbols": [symbol], "timeframes": list(tfs)}
    values.update(overrides or {})
    tmp = tempfile.mkdtemp(prefix="alertcal_")
    db = DB(Path(tmp) / "cal.db")                    # 引擎要一张 price_alerts 表; 用完即删
    eng = AlertEngine(_Cfg(values), db, get_bars, now=lambda: clock[0])
    out = {"drop": [], "range": [], "range_more": [], "other": [], "msgs": []}

    def take(msgs, ts):
        for m in msgs:
            if ts < t0:
                continue
            kind = ("drop" if m.startswith("🔻") else
                    "range_more" if m.startswith("📉") and "后续跌</b>" in m else
                    "range" if m.startswith("📉") else "other")
            out[kind].append(ts)
            out["msgs"].append((ts, m))

    for row in rows:
        for ts, px in ticks_of(row, proxy):
            clock[0] = ts
            msgs = eng.on_tick(symbol, px, int(ts * 1000))
            if msgs:
                take(msgs, ts)
        end_ms = row[0] + MIN_MS
        clock[0] = end_ms / 1000.0
        for tf in tfs:
            e, i = ends[tf], idx[tf]
            if i < len(e) and e[i] <= end_ms:
                idx[tf] = i + 1
                b = agg[tf][i]
                bar = (b["open_time"], b["open"], b["high"], b["low"], b["close"], b["volume"], 0.0, 0.0, 1)
                msgs = eng.on_closed_bar(symbol, tf, bar)
                if msgs:
                    take(msgs, clock[0])
    out["status"] = eng.status()
    try:
        import shutil
        db.conn().close()
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="急跌提醒阈值校准(只数触发次数)")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--weeks", type=int, default=52, help="统计多少周(默认 52)")
    ap.add_argument("--proxy", choices=("ohlc", "dense", "close", "low"), default="ohlc")
    ap.add_argument("--full", action="store_true", help="dense 口径下也跑破位的冷却/N 日两张表(很慢)")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新下载")
    ap.add_argument("--download-only", action="store_true")
    ap.add_argument("--no-engine", action="store_true", help="跳过 AlertEngine 端到端回放(只出阈值表)")
    ap.add_argument("--show", type=int, default=0, help="打印端到端回放里最近 N 条提醒原文")
    a = ap.parse_args()
    days = a.weeks * 7 + WARMUP_DAYS
    rows = load_1m(a.symbol, days, refresh=a.refresh)
    print(f"{a.symbol} 1m: {len(rows)} 根, "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(rows[0][0] / 1000))} → "
          f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(rows[-1][0] / 1000))} UTC")
    if a.download_only:
        return

    sys.path.insert(0, ROOT)
    from app.engine import alerts as A

    # 统计区间: 最后 weeks 周(整周); 之前的只打底不计数
    t_end = (rows[-1][0] + MIN_MS) / 1000.0
    t0 = t_end - a.weeks * WEEK_S
    step_ratio = A.DROP_STEP_RATIO
    cooldown = {m: A.DEFAULT_DROP[m]["cooldown_min"] for m in A.DEFAULT_DROP_WINDOWS}
    grid = {5: [0.6, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5],
            15: [1.0, 1.2, 1.5, 1.8, 2.0, 2.5, 3.0, 3.5]}
    grid = {m: list(grid.get(m, [])) for m in A.DEFAULT_DROP_WINDOWS}
    for m in A.DEFAULT_DROP_WINDOWS:                        # 模块默认值一定在表里
        d = A.DEFAULT_DROP[m]["pct"]
        if d not in grid[m]:
            grid[m] = sorted(grid[m] + [d])

    print(f"\n统计区间: {time.strftime('%Y-%m-%d', time.gmtime(t0))} → "
          f"{time.strftime('%Y-%m-%d', time.gmtime(t_end))} UTC, 共 {a.weeks} 周; tick 代理 = {a.proxy}")
    print("规则与引擎一致: 现价较窗口内最高价回落 >= 阈值 → 响; 冷却 "
          + " / ".join(f"{m}分钟窗口 {cooldown[m]:g} 分钟" for m in cooldown)
          + f"; 冷却内 窗口跌幅再加深 {step_ratio:g}×阈值, 或现价比上次提醒价再低 {step_ratio:g}×阈值 才再响。")

    t_run = time.time()
    res = drop_grid(rows, t0, a.proxy, grid, cooldown, step_ratio)
    for m in grid:
        print(f"\n== 急跌 {m} 分钟窗口: 各阈值每周响几次(含加深再响) ==")
        for pct in grid[m]:
            rec = res[(m, pct)]
            st = week_stats(rec["all"], t0, a.weeks)
            mark = "  ← 默认" if pct == A.DEFAULT_DROP[m]["pct"] else ""
            print(_row(f">={pct:g}%", st, f"  (新{rec['new']} 加深{rec['deeper']} 更低{rec['lower']}){mark}"))

    ms = list(grid)
    if len(ms) >= 2:
        m1, m2 = ms[0], ms[1]
        print("\n== 两个窗口合计(同一笔报价上两个窗口同时响只算一条消息): 均值 次/周 ==")
        print("  " + f"{m1}分＼{m2}分".ljust(9) + "".join(f"{'>=' + format(p, 'g') + '%':>8}" for p in grid[m2]))
        for p1 in grid[m1]:
            cells = []
            for p2 in grid[m2]:
                both = set(res[(m1, p1)]["all"]) | set(res[(m2, p2)]["all"])
                cells.append(f"{len(both) / a.weeks:>8.2f}")
            print("  " + f">={p1:g}%".ljust(9) + "".join(cells))
        d1, d2 = A.DEFAULT_DROP[m1]["pct"], A.DEFAULT_DROP[m2]["pct"]
        both = sorted(set(res[(m1, d1)]["all"]) | set(res[(m2, d2)]["all"]))
        print(f"\n模块默认 {m1}分钟>={d1:g}% + {m2}分钟>={d2:g}% 合计:")
        print(_row("急跌(合计)", week_stats(both, t0, a.weeks)))
    print(f"  (阈值表用时 {time.time() - t_run:.1f}s)")

    if a.no_engine:
        return

    # ---- 端到端: 完整 AlertEngine + 模块默认值 ----
    print("\n== AlertEngine 端到端回放(模块默认值) ==\n"
          "   急跌一行 ≤ 上面的合计: 阈值表里各窗口各自计数; 引擎里「比上次提醒价更低」那一档还要求该币\n"
          "   最近一个窗口时长内没响过任何急跌提醒(直线暴跌时不刷屏)。条数以这里为准。")
    t_run = time.time()
    rep = engine_replay(rows, a.symbol, t0, a.proxy)
    th = rep["status"]["thresholds"]
    print(f"  生效阈值: 急跌 {th['drop']['windows']}")
    print(f"            破位 {th['range_break']}")
    print(_row("急跌(5m+15m)", week_stats(rep["drop"], t0, a.weeks)))
    print(_row("跌破2日低点", week_stats(rep["range"], t0, a.weeks)))
    print(_row("破位后续跌", week_stats(rep["range_more"], t0, a.weeks)))
    print(_row("全部合计", week_stats(rep["drop"] + rep["range"] + rep["range_more"], t0, a.weeks)))
    print(f"  (用时 {time.time() - t_run:.1f}s)")
    if a.show:
        print(f"\n最近 {a.show} 条提醒原文:")
        for ts, m in rep["msgs"][-a.show:]:
            print("  " + m.replace("\n", "\n  ") + "\n")

    if a.proxy == "dense" and not a.full:
        print("\n(dense 口径: 破位的冷却/N 日两张表略过 —— 用默认的 ohlc 口径看, 或加 --full)")
        return

    # ---- 破位后续跌: 步长对条数的影响(急跌关掉以加速) ----
    print("\n== 跌破 2 日低点后续跌: 不同步长下每周多响几条(0 = 关闭) ==")
    for stp in (0, 2.0, 3.0, 4.0, 5.0):
        r = engine_replay(rows, a.symbol, t0, a.proxy, {
            "alerts.drop.enabled": False, "alerts.range_break.step_pct": stp})
        mark = "  ← 默认" if stp == A.DEFAULT_RANGE_STEP_PCT else ""
        print(_row(f"步长 {stp:g}%", week_stats(r["range_more"], t0, a.weeks), mark))

    # ---- 破位提醒: 冷却时长对次数的影响(急跌关掉以加速) ----
    print("\n== 跌破 2 日低点: 不同冷却时长下每周响几次"
          "(参照低点 = 已收盘 15m+5m K线的 2 日最低, 每根 5m 收盘重算) ==")
    for cd in (60, 120, 240, 480, 720):
        r = engine_replay(rows, a.symbol, t0, a.proxy, {
            "alerts.drop.enabled": False, "alerts.range_break.cooldown_min": cd})
        mark = "  ← 默认" if cd == A.DEFAULT_RANGE_COOLDOWN_MIN else ""
        print(_row(f"冷却 {cd / 60:g} 小时", week_stats(r["range"], t0, a.weeks), mark))

    # ---- 破位提醒: 换成 N 日低点的次数(默认冷却; 一次回放同时挂多档 N) ----
    day_grid = (1, 2, 3, 5, 7)
    print(f"\n== 跌破 N 日低点: 不同 N 每周响几次(冷却 {A.DEFAULT_RANGE_COOLDOWN_MIN / 60:g} 小时) ==")
    r = engine_replay(rows, a.symbol, t0, a.proxy, {
        "alerts.drop.enabled": False, "alerts.range_break.days": list(day_grid)})
    for n in day_grid:
        stamps = [ts for ts, m in r["msgs"] if m.startswith(f"📉 <b>跌破{n}日低点</b>")]
        tf = (r["status"]["ref_lows"].get(a.symbol, {}).get(f"{n}d_low") or {}).get("tf")
        mark = "  ← 默认" if n in A.DEFAULT_RANGE_DAYS else ""
        print(_row(f"N={n} 日({tf})", week_stats(stamps, t0, a.weeks), mark))


if __name__ == "__main__":
    main()
