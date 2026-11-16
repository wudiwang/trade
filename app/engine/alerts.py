"""近实时提醒引擎 —— 只产出文字通知, 不下单、不产生交易信号、不碰交易模块。

三类提醒(全部可用 cfg 的 alerts.* 调整, 见 _load_params):
  a) 价位提醒  用户设的"跌破/涨破某个位置"。存 sqlite 表 price_alerts, 越过即响一次, 之后 status=fired。
  b) 急跌提醒  滑动窗口(默认 5 分钟 / 15 分钟)内, 现价较窗口内最高价回落 ≥ 阈值%。
               每个窗口各自冷却; 冷却期内只有两种情况提前再响: 窗口内跌幅比上次响时再加深一个 step,
               或 窗口跌幅仍在阈值之上、现价比上次响时的价格又低了一个 step、且该币已有一个窗口时长
               没响过急跌提醒(反弹后的第二段; 一路直线暴跌时不靠这一档刷屏)。
  c) 破位提醒  现价跌破最近 N 日(默认 2 日)最低价。参照低点在每根 5m 收盘时由已收盘K线重算。
               一段连续创新低只在第一下响"破位"; 之后每比上一条的价格再低 step_pct(默认 3%)响一条"续跌"。

运行位置与约束:
  on_tick 在 websocket 回调里同步执行 —— 无网络、无逐笔写库、均摊 O(1), 任何异常都吞掉并返回 []。
  价位提醒的活动列表常驻内存(写时复制), on_tick 不读库; 只有某个价位真的触发时才写一次库。
  时间一律用行情事件时间(ts_ms); 与本机时钟差超过 MAX_CLOCK_SKEW_S 的脏时间戳改用本机时间。

破位参照低点的粒度(引擎缓存 5m≈41.7h / 15m≈5.2天 / 1h≈20.8天):
  取"能覆盖整个窗口的最小级别", 更小级别补齐最近一段。N=2日→15m, N≤5日→15m, N≤20日→1h。
  窗口起点按该级别整根K对齐(2日窗口最多多看 15 分钟), 终点精确到最近一根已收盘 5m。
  缓存覆盖不到整个窗口时不给参照价、不报破位(避免拿半截窗口冒充 N 日低点)。

兜底: 如果某根 5m 收盘时实时报价已缺席 20 秒以上(tick 通路没接上/断线), on_closed_bar 会把这根
  收盘K按 开→高/低→收 当四笔报价补判一遍, 三类提醒退化成 5 分钟粒度而不是悄悄失效。

配置键(都可省略, 括号里是默认; settings 表热覆盖最多 5 分钟内生效 —— 在收盘K回调里重读):
  alerts.enabled (true)                    总开关; 关掉后 tick_symbols() 为空
  alerts.symbols (["BTCUSDT"])             报急跌/破位的币; 价位提醒不受此限
  alerts.base_tf (timeframes 里最小的)     重算参照低点/兜底用的级别
  alerts.levels.max_active (50)            活动价位提醒上限
  alerts.drop.enabled (true) / alerts.drop.directions (["down"]; 可加 "up")
  alerts.drop.windows_min ([5, 15])        窗口(分钟)
  alerts.drop.pct_5m (1.5) / pct_15m (2.0)             阈值%
  alerts.drop.cooldown_min_5m (30) / cooldown_min_15m (60)
  alerts.drop.step_pct_5m / step_pct_15m (阈值的一半)  冷却内再响所需的步长: 窗口跌幅比上次多这么多,
                                                       或现价比上次提醒价低这么多; 0=冷却内不再响
  alerts.range_break.enabled (true) / alerts.range_break.directions (["down"])
  alerts.range_break.days ([2])            N 日低点, 可多档如 [2, 7]
  alerts.range_break.cooldown_min (240)    距上一次创新低不足这么久算同一次破位
  alerts.range_break.buffer_pct (0)        要跌破参照价再多这么多 % 才算
  alerts.range_break.step_pct (3.0)        同一次破位里, 比上一条破位提醒的价格又低这么多 % → 再响一条"续跌"; 0=关

重启: 急跌/破位的冷却状态在【响的那一刻】写进 sqlite 表 alert_state(每个 币×窗口×方向 一行), 启动时读回,
  所以重启落在刚响过的那根未收盘K里不会把同一次破位/同一段急跌再报一遍。
行情中断(停机/看门狗重连)期间被触及又回去的价位: rescan_levels() 用已收盘K线补查, 引擎在回填之后调用。

默认阈值来自 scripts/alert_calibrate.py 对约 1 年 BTCUSDT 1m 数据的回放(只数触发次数)。
"""
from __future__ import annotations

import collections
import html
import logging
import math
import re
import threading
import time

log = logging.getLogger("alerts")

BKK_OFFSET = 7 * 3600            # 曼谷 UTC+7
MAX_CLOCK_SKEW_S = 60.0          # 事件时间与本机时间差超过它 → 视为脏时间戳
PARAMS_TTL_S = 60.0              # 配置快照最多隔这么久重读一次(在收盘K回调里, 不在 tick 里)
STALE_TICK_S = 20.0              # 一根K收盘时, 实时报价已缺席这么久 → 用这根收盘K兜底
REF_RETRY_S = 30.0               # tick 路上最多隔这么久检查一次"参照低点要不要补算"
MAX_NOTE_LEN = 200
# 币种写法与 web/Telegram 的校验一致(app/web/server.py 的 _SYMBOL_RE): 字母数字下划线, 含中文名合约
SYMBOL_RE = re.compile(r"^\w{2,32}$")
# 「最近已知价格」最多允许多旧(以基准级别的根数计, 再加 60 秒余量): 更旧的不当现价用。
# 正常运行时最近一根已收盘K的收盘时刻最多落后一根; 落后两根以上 = 这个币的行情已经断了。
PRICE_MAX_AGE_BARS = 2
# 手动价位离现价的合理范围(倍数): 超出多半是写错了(如 63 000 被拆成 63 和备注 000)
LEVEL_MIN_RATIO = 0.2
LEVEL_MAX_RATIO = 5.0
STATE_KEEP_S = 7 * 86400.0       # alert_state 表里超过这么久的冷却记录不再恢复

TF_SEC = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
          "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200, "1d": 86400}

DEFAULT_SYMBOLS = ("BTCUSDT",)
DEFAULT_MAX_ACTIVE_LEVELS = 50

# ---- 急跌提醒默认值(分钟窗口 → 阈值% / 冷却分钟) ----
# 由 scripts/alert_calibrate.py 校准(BTCUSDT 1m, 2025-11-17 → 2026-11-16 共 52 周, 只数触发次数):
#   5 分钟 ≥1.5% + 15 分钟 ≥2.0% 两个窗口合计 115 条 = 均值 2.21 条/周
#   (中位 1, P90 5, 最多的一周 24; 52 周里 23 周一条都没有, 6 周 ≥5 条)。
#   口径 = --proxy dense 的端到端回放: 1m K线的 开→高/低→收 四个锚点之间按直线插值成每秒一笔, 与实盘节奏
#   (每币每秒一笔)一致。一分钟只喂四笔的粗口径(--proxy ohlc)是 90 条 = 1.73 条/周, 偏少: 粗的一跳会跳过阈值、
#   两个窗口也更容易并成一条; 只用 1m 收盘价(看不到影线)更少, 只能当下界。实盘的路径比直线更曲折, 条数不会
#   比 dense 口径少。
#   步长取阈值的一半。换币种/想更密更疏: 跑一遍校准脚本看表, 再改 alerts.drop.pct_5m / pct_15m / step_pct_*。
DEFAULT_DROP_WINDOWS = (5, 15)
DEFAULT_DROP = {
    5: {"pct": 1.5, "cooldown_min": 30},
    15: {"pct": 2.0, "cooldown_min": 60},
}
DROP_STEP_RATIO = 0.5                # 没单独配 step 时: 加深步长 = 阈值 × 这个比例
# ---- 破位提醒默认值 ----
DEFAULT_RANGE_DAYS = (2,)
# 同一份数据上"跌破 2 日低点"(冷却 4 小时)共 187 次 = 3.60 次/周(中位 4, 最多的一周 7, 4 周为 0)。
# 冷却 8 小时 → 2.88 次/周, 12 小时 → 2.35 次/周; N=3日 → 3.02, N=7日 → 1.81 次/周。
DEFAULT_RANGE_COOLDOWN_MIN = 240     # 距上一次创新低不足这么久 → 算同一次破位, 不再响
# 同一次破位里(冷却一直被新低顺延), 现价比【上一条破位提醒发出时的价格】又低了这么多 % → 再响一条"续跌"。
# 没有它, 一段几小时不停创新低的阶梯式下跌从头到尾只有第一条。0 = 关闭。
# 同一份数据上: 步长 3% → 全年 27 条 ≈0.5 条/周(52 周里 37 周为 0); 2% → ≈1.0 条/周; 4% → ≈0.2; 5% → ≈0.1。
DEFAULT_RANGE_STEP_PCT = 3.0

_OP_ALIASES = {
    "below": "below", "<": "below", "<=": "below", "down": "below", "under": "below",
    "跌破": "below", "下破": "below", "低于": "below", "跌穿": "below",
    "above": "above", ">": "above", ">=": "above", "up": "above", "over": "above",
    "突破": "above", "升破": "above", "涨破": "above", "高于": "above", "上破": "above",
}


# =====================================================================
# 纯逻辑小件(校准脚本直接复用, 保证"回放 = 引擎")
# =====================================================================
class RollingExtreme:
    """滑动窗口内的最高价(sign=+1)或最低价(sign=-1): 单调队列, 均摊 O(1)。

    每个整秒最多留一个样本 → 队列长度 ≤ 窗口秒数+2, 内存有界。
    update() 返回 (极值出现的时间戳, 极值价格), 当前这笔也算在窗口内。
    """
    __slots__ = ("window_s", "sign", "dq")

    def __init__(self, window_s: float, sign: int = 1):
        self.window_s = float(window_s)
        self.sign = 1 if sign >= 0 else -1
        self.dq: collections.deque = collections.deque()

    def update(self, ts: float, price: float) -> tuple:
        dq = self.dq
        cut = ts - self.window_s
        while dq and dq[0][0] < cut:              # 过期的从左边走
            dq.popleft()
        v = price * self.sign
        while dq and dq[-1][1] <= v:              # 被当前价压过的从右边走
            dq.pop()
        if not dq or int(dq[-1][0]) != int(ts):   # 同一秒里更低的样本不必留(与更高者同时过期)
            dq.append((ts, v))
        t0, v0 = dq[0]
        return t0, v0 * self.sign

    def __len__(self) -> int:
        return len(self.dq)


class DropTrigger:
    """阈值 + 冷却 + 加深升级。

    check(ts, move_pct, price, sign, last_any_ts, quiet_s) → None(不响) / "new"(冷却外的新一次) /
      "deeper"(冷却内, 窗口内的幅度比上次响时又加深了一个 step) /
      "lower"(冷却内, 窗口幅度仍在阈值之上, 且现价比【上次响时的价格】又低了一个 step ——
              反弹之后的第二段下跌: 窗口内幅度未必比上次大, 但价格已经更低)。
              为了不在一路直线下跌时刷屏, 这一档还要求: 距该币【任何窗口】上一条同向提醒(last_any_ts)
              已过去 quiet_s 秒(引擎传窗口时长)。连续暴跌由"deeper"那一档按幅度分级去报。
    每次响都重置冷却计时, 并记下这次的幅度与价格作为下一次升级的基准。step_pct<=0 表示冷却内不升级。
    price 不传(None)时只有前两种, 与旧行为相同。sign=+1 看下跌, -1 看上涨(此时 lower = 比上次更高)。
    """
    __slots__ = ("pct", "cooldown_s", "step_pct", "last_ts", "last_pct", "last_price", "count")

    def __init__(self, pct: float, cooldown_s: float, step_pct: float):
        self.pct = float(pct)
        self.cooldown_s = float(cooldown_s)
        self.step_pct = float(step_pct)
        self.last_ts = None
        self.last_pct = 0.0
        self.last_price = None
        self.count = 0

    def check(self, ts: float, move_pct: float, price=None, sign: int = 1,
              last_any_ts=None, quiet_s: float = 0.0):
        if move_pct < self.pct:
            return None
        if self.last_ts is None or ts - self.last_ts >= self.cooldown_s:
            kind = "new"
        elif self.step_pct <= 0:
            return None
        elif move_pct >= self.last_pct + self.step_pct:
            kind = "deeper"
        elif (price is not None and self.last_price
              and (last_any_ts is None or ts - last_any_ts >= quiet_s)
              and (self.last_price - price) / self.last_price * 100.0 * (1 if sign >= 0 else -1)
              >= self.step_pct):
            kind = "lower"
        else:
            return None
        self.last_ts = ts
        self.last_pct = move_pct
        self.last_price = price
        self.count += 1
        return kind


def window_extreme(get_bars, symbol: str, tfs, days: float, sign: int, now_s: float):
    """最近 days 天已收盘K线的最低价(sign=+1)或最高价(sign=-1)。

    tfs: [(tf, 秒数), ...] 由小到大。逐级别从新往旧扫, 直到某个级别覆盖到窗口起点为止。
    返回 (极值, 极值所在K线的收盘时刻[秒], 覆盖窗口所用的级别); 覆盖不到 → (None, None, 最后试的级别)。
    """
    cutoff_ms = (now_s - days * 86400.0) * 1000.0
    key = "low" if sign >= 0 else "high"
    best = best_t = used = None
    covered = False
    for tf, tf_s in tfs:
        bars = get_bars(symbol, tf)
        if not bars:
            continue
        if not isinstance(bars, (list, tuple)):
            bars = list(bars)
        tf_ms = tf_s * 1000.0
        used = tf
        hit_edge = False
        for b in reversed(bars):
            ot = b["open_time"]
            if ot + tf_ms <= cutoff_ms:           # 这根整根都在窗口之前 → 本级别已覆盖到窗口起点
                hit_edge = True
                break
            v = float(b[key])
            if best is None or (v < best if sign >= 0 else v > best):
                best, best_t = v, (ot + tf_ms) / 1000.0
        if hit_edge or bars[0]["open_time"] <= cutoff_ms:
            covered = True
            break
    if not covered or best is None:
        return None, None, used
    return best, best_t, used


def last_break_from_bars(get_bars, symbol: str, tfs, days: float, sign: int,
                         now_s: float, lookback_s: float):
    """已收盘K线里, 最近 lookback_s 秒内【最后一次创 days 日新低(sign=+1)/新高(sign=-1)】的那根K的收盘时刻。

    只在启动(或新加一档 N 日)时用一次: 把"上一次破位是什么时候"从K线里找回来,
    这样重启在一段连续下跌中间, 不会因为内存状态丢了而把同一次破位再报一遍。没有 → None。
    """
    need_ms = (now_s - days * 86400.0 - lookback_s) * 1000.0
    pick = None
    for tf, tf_s in tfs:                          # 取能覆盖 [窗口+回看] 的最小级别; 都不够就用最大的
        bars = get_bars(symbol, tf)
        if not bars:
            continue
        pick = (bars, tf_s)
        if bars[0]["open_time"] <= need_ms:
            break
    if pick is None:
        return None
    bars, tf_s = pick
    tf_ms = tf_s * 1000.0
    win_ms = days * 86400.0 * 1000.0
    check_from_ms = (now_s - lookback_s) * 1000.0
    key = "low" if sign >= 0 else "high"
    k = 1.0 if sign >= 0 else -1.0
    dq: collections.deque = collections.deque()   # 单调队列: 之前 days 天内的最低(取负后即最高)
    last = None
    for b in bars:
        ot = b["open_time"]
        if ot + tf_ms <= need_ms:
            continue
        v = float(b[key]) * k
        while dq and dq[0][0] < ot - win_ms:
            dq.popleft()
        if dq and ot + tf_ms > check_from_ms and v < dq[0][1]:
            last = (ot + tf_ms) / 1000.0
        while dq and dq[-1][1] >= v:
            dq.pop()
        dq.append((ot, v))
    return last


def fmt_price(p) -> str:
    p = float(p)
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.8f}".rstrip("0").rstrip(".")


def bkk(ts: float, fmt: str = "%m-%d %H:%M") -> str:
    """秒级时间戳 → 曼谷时间字符串, 如 11-16 13:05。"""
    return time.strftime(fmt, time.gmtime(float(ts) + BKK_OFFSET))


def _fmt_pct(x: float) -> str:
    return f"{float(x):g}%"


# =====================================================================
# 内部状态
# =====================================================================
class _Level:
    __slots__ = ("id", "symbol", "below", "price", "note", "created_at", "restored")

    def __init__(self, id, symbol, op, price, note, created_at, restored):
        self.id = int(id)
        self.symbol = symbol
        self.below = (op == "below")
        self.price = float(price)
        self.note = note or ""
        self.created_at = float(created_at or 0)
        self.restored = restored          # True=启动时从库里恢复的(不是本次运行中新建的)


class _Win:
    """某币某窗口某方向的急跌(急涨)检测状态。"""
    __slots__ = ("minutes", "sign", "roll", "trig")

    def __init__(self, minutes, sign, pct, cooldown_s, step_pct):
        self.minutes = minutes
        self.sign = sign                  # +1 看跌幅(追踪窗口最高价), -1 看涨幅
        self.roll = RollingExtreme(minutes * 60.0, sign)
        self.trig = DropTrigger(pct, cooldown_s, step_pct)


class _Range:
    """某币 N 日低点(高点)的破位检测状态。"""
    __slots__ = ("days", "sign", "ref", "ref_ts", "tf", "break_ts", "booted", "count",
                 "alert_price", "alert_ts")

    def __init__(self, days, sign):
        self.days = days
        self.sign = sign                  # +1 跌破 N 日低点, -1 突破 N 日高点
        self.ref = None                   # 参照价(已收盘K线算出)
        self.ref_ts = None                # 参照价那根K线的收盘时刻
        self.tf = None                    # 覆盖窗口所用的级别(粒度)
        self.break_ts = None              # 最近一次"越过参照价"(=创新低/新高)的时刻; 冷却从它起算
        self.booted = False               # break_ts 是否已从K线找回过(启动后一次)
        self.count = 0
        self.alert_price = None           # 上一次破位提醒发出时的价格(同一段行情里"续跌"提醒的基准)
        self.alert_ts = None


class _Sym:
    __slots__ = ("last_price", "last_ts", "real_ts", "real_wall", "ticks",
                 "wins", "ranges", "refs_at", "ref_retry_at")

    def __init__(self):
        self.last_price = None
        self.last_ts = 0.0                # 最近一笔(含兜底合成)的事件时间
        self.real_ts = None               # 最近一笔【真实】报价的事件时间
        self.real_wall = None             # 最近一笔真实报价到达时的本机时间
        self.ticks = 0                    # 真实报价计数
        self.wins = []
        self.ranges = []
        self.refs_at = None               # 参照价上次重算的本机时间
        self.ref_retry_at = 0.0


def _as_float(v, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _as_bool(v, default):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_list(v, default):
    if isinstance(v, (list, tuple, set, frozenset)):
        return list(v)
    if isinstance(v, str):
        return [x for x in re.split(r"[,\s]+", v.strip()) if x]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return [v]
    return list(default)


def _num_list(v, default, lo: float, hi: float, cap: int = 6) -> list:
    """配置里的数字列表: 去掉脏值/越界/重复, 最多 cap 个; 一个合法的都没有 → 用默认。"""
    out = []
    for x in _as_list(v, default):
        f = _as_float(x, None)
        if f is None or not (lo <= f <= hi):
            continue
        f = int(f) if float(f).is_integer() else f
        if f not in out:
            out.append(f)
        if len(out) >= cap:
            break
    return out or list(default)


def _dirs(v, default=("down",)):
    """方向列表(down/up/both); 没有合法值 → 默认只看下跌。要整类关掉请用 *.enabled=false。"""
    out = []
    for x in _as_list(v, default):
        s = str(x).strip().lower()
        if s in ("down", "up") and s not in out:
            out.append(s)
        elif s == "both":
            out = ["down", "up"]
    return tuple(out) or tuple(default)


# =====================================================================
# 引擎
# =====================================================================
class AlertEngine:
    """见模块说明。公开方法全部不抛异常(构造函数除外也尽量不抛: 库不可用时价位提醒停用)。"""

    def __init__(self, cfg, db, get_bars, now=time.time):
        self.cfg = cfg
        self.db = db
        self._get_bars = get_bars
        self._now = now
        self._lock = threading.Lock()                 # 只保护 _levels / _tick_set 的替换与写库
        self._levels: dict[str, tuple] = {}           # symbol -> (活动 _Level, ...) 写时复制
        self._tick_set: frozenset = frozenset()
        self._syms: dict[str, _Sym] = {}
        self._saved: dict = {}                        # alert_state 表的内存副本: key -> (ts, v1, v2)
        self._unsaved: list = []                      # 触发了但写库失败的 (id, fired_at, fired_price)
        self._unsaved_retry_at = 0.0
        self._errors = 0
        self._err_logged_at = 0.0
        self._fired = {"level": 0, "drop": 0, "range": 0}
        self._ticks_total = 0
        self._db_ok = False
        self._p: dict = {}
        self._p_sig = None
        self._p_at = 0.0
        self._started_at = self._safe_now()
        try:
            self._init_db()
            self._db_ok = True
        except Exception:
            log.exception("price_alerts 表初始化失败 —— 价位提醒停用, 其余提醒照常")
        try:
            self._reload_params(force=True)
        except Exception:
            log.exception("提醒配置读取失败, 使用内置默认值")
            self._apply_params(self._default_params())

    # ------------------------------------------------------------ 基础
    def _safe_now(self) -> float:
        try:
            return float(self._now())
        except Exception:
            return time.time()

    def _note_error(self, where: str) -> None:
        """异常计数 + 限速日志(每分钟最多一条堆栈), 自身绝不抛。"""
        try:
            self._errors += 1
            t = time.time()
            if t - self._err_logged_at >= 60:
                self._err_logged_at = t
                log.exception("alerts.%s 异常(累计 %d 次)", where, self._errors)
        except Exception:
            pass

    def _init_db(self) -> None:
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS price_alerts ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " symbol TEXT NOT NULL, op TEXT NOT NULL, price REAL NOT NULL,"
            " note TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'active',"
            " created_at INTEGER NOT NULL, created_price REAL,"
            " fired_at INTEGER, fired_price REAL)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_price_alerts_status ON price_alerts(status)")
        levels: dict[str, list] = {}
        for r in self.db.query(
                "SELECT id, symbol, op, price, note, created_at FROM price_alerts "
                "WHERE status='active' ORDER BY id"):
            if r["op"] not in ("below", "above"):
                continue
            levels.setdefault(r["symbol"], []).append(
                _Level(r["id"], r["symbol"], r["op"], r["price"], r["note"], r["created_at"], True))
        self._levels = {s: tuple(v) for s, v in levels.items()}
        # 急跌/破位提醒的冷却状态: 只在【真的响了】那一刻写一行, 重启后读回 ——
        # 否则重启落在刚响过的那根未收盘K里, 同一次破位/同一段急跌会再报一遍。
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS alert_state ("
            " key TEXT PRIMARY KEY, ts REAL NOT NULL, v1 REAL, v2 REAL)")
        saved = {}
        for r in self.db.query("SELECT key, ts, v1, v2 FROM alert_state"):    # 每个(币, 窗口, 方向)一行, 很小
            saved[r["key"]] = (float(r["ts"]), r["v1"], r["v2"])
        self._saved = saved

    @staticmethod
    def _state_key(kind: str, symbol: str, n, sign: int) -> str:
        return f"{kind}|{symbol}|{float(n):g}|{'d' if sign > 0 else 'u'}"

    def _save_state(self, kind: str, symbol: str, n, sign: int, ts: float, v1, v2=None) -> None:
        """提醒响了之后记一行冷却状态(一次本地写库; 不在逐笔路径上, 只在触发那一刻)。失败只计数。"""
        key = self._state_key(kind, symbol, n, sign)
        self._saved[key] = (float(ts), v1, v2)
        if not self._db_ok:
            return
        try:
            self.db.execute(
                "INSERT OR REPLACE INTO alert_state (key, ts, v1, v2) VALUES (?,?,?,?)",
                (key, float(ts), v1, v2))
        except Exception:
            self._note_error("save_state")

    # ------------------------------------------------------------ 配置
    def _default_params(self) -> dict:
        return {
            "enabled": True, "symbols": tuple(DEFAULT_SYMBOLS), "base_tf": "5m",
            "tfs": (("5m", 300), ("15m", 900), ("1h", 3600)),
            "max_active": DEFAULT_MAX_ACTIVE_LEVELS,
            "drop_enabled": True, "drop_dirs": ("down",),
            "drop_windows": tuple((m, DEFAULT_DROP[m]["pct"], DEFAULT_DROP[m]["cooldown_min"] * 60.0,
                                   round(DEFAULT_DROP[m]["pct"] * DROP_STEP_RATIO, 4))
                                  for m in DEFAULT_DROP_WINDOWS),
            "range_enabled": True, "range_dirs": ("down",), "range_days": tuple(DEFAULT_RANGE_DAYS),
            "range_cooldown_s": DEFAULT_RANGE_COOLDOWN_MIN * 60.0, "range_buffer_pct": 0.0,
            "range_step_pct": DEFAULT_RANGE_STEP_PCT,
        }

    def _load_params(self) -> dict:
        g = self.cfg.get
        p = self._default_params()
        p["enabled"] = _as_bool(g("alerts.enabled", True), True)

        raw_syms = _as_list(g("alerts.symbols", list(DEFAULT_SYMBOLS)), DEFAULT_SYMBOLS)
        syms = []
        for s in raw_syms:
            s = str(s).strip().upper()
            if SYMBOL_RE.match(s) and s not in syms:
                syms.append(s)
        if not syms and raw_syms:                 # 写了但全是脏值 → 默认; 明确写空列表 = 只留价位提醒
            syms = list(DEFAULT_SYMBOLS)
        p["symbols"] = tuple(syms)

        tfs = []
        for tf in _as_list(g("timeframes", ["5m", "15m", "1h"]), ("5m", "15m", "1h")):
            tf = str(tf)
            if tf in TF_SEC and (tf, TF_SEC[tf]) not in tfs:
                tfs.append((tf, TF_SEC[tf]))
        tfs.sort(key=lambda x: x[1])
        if not tfs:
            tfs = [("5m", 300), ("15m", 900), ("1h", 3600)]
        p["tfs"] = tuple(tfs)
        base = str(g("alerts.base_tf", "") or "")
        p["base_tf"] = base if base in TF_SEC and any(t == base for t, _ in tfs) else tfs[0][0]

        p["max_active"] = max(1, int(_as_float(g("alerts.levels.max_active", DEFAULT_MAX_ACTIVE_LEVELS),
                                               DEFAULT_MAX_ACTIVE_LEVELS)))

        # ---- 急跌 ----
        p["drop_enabled"] = _as_bool(g("alerts.drop.enabled", True), True)
        p["drop_dirs"] = _dirs(g("alerts.drop.directions", ["down"]))
        wins = []
        for m in _num_list(g("alerts.drop.windows_min", list(DEFAULT_DROP_WINDOWS)),
                           DEFAULT_DROP_WINDOWS, 0.5, 240):
            base_d = DEFAULT_DROP.get(m)
            if base_d is None:                    # 没校准过的窗口: 以 5 分钟档按 √时间 外推
                k = math.sqrt(m / 5.0)
                base_d = {"pct": round(DEFAULT_DROP[5]["pct"] * k, 2),
                          "cooldown_min": max(10.0, m * 6.0)}
            pct = _as_float(g(f"alerts.drop.pct_{m}m", base_d["pct"]), base_d["pct"])
            cd = _as_float(g(f"alerts.drop.cooldown_min_{m}m", base_d["cooldown_min"]), base_d["cooldown_min"])
            if pct <= 0:                          # 阈值必须为正, 否则每个报价都会响
                pct = base_d["pct"]
            step = _as_float(g(f"alerts.drop.step_pct_{m}m", None), None)
            if step is None:                      # 没配步长 → 跟着阈值走(一半); 配 0 = 冷却内不再响
                step = round(pct * DROP_STEP_RATIO, 4)
            wins.append((m, pct, max(0.0, cd) * 60.0, max(0.0, step)))
        p["drop_windows"] = tuple(wins)

        # ---- 破位 ----
        p["range_enabled"] = _as_bool(g("alerts.range_break.enabled", True), True)
        p["range_dirs"] = _dirs(g("alerts.range_break.directions", ["down"]))
        p["range_days"] = tuple(_num_list(
            g("alerts.range_break.days", list(DEFAULT_RANGE_DAYS)), DEFAULT_RANGE_DAYS, 0.1, 60))
        p["range_cooldown_s"] = max(0.0, _as_float(
            g("alerts.range_break.cooldown_min", DEFAULT_RANGE_COOLDOWN_MIN), DEFAULT_RANGE_COOLDOWN_MIN)) * 60.0
        p["range_buffer_pct"] = max(0.0, _as_float(g("alerts.range_break.buffer_pct", 0.0), 0.0))
        p["range_step_pct"] = max(0.0, _as_float(
            g("alerts.range_break.step_pct", DEFAULT_RANGE_STEP_PCT), DEFAULT_RANGE_STEP_PCT))
        return p

    def _reload_params(self, force: bool = False) -> None:
        self._p_at = self._safe_now()
        p = self._load_params()
        sig = repr(sorted(p.items()))
        if force or sig != self._p_sig:
            self._p_sig = sig
            self._apply_params(p)

    def _apply_params(self, p: dict) -> None:
        self._p = p
        self._watch = frozenset(p["symbols"])
        self._enabled = bool(p["enabled"])
        self._base_tf = p["base_tf"]
        self._base_tf_s = TF_SEC.get(p["base_tf"], 300)
        self._rebuild_tick_set()
        for sym in list(self._syms):
            if sym not in self._tick_set:
                self._syms.pop(sym, None)         # 不再关心的币: 状态清掉, 内存有界
            else:
                self._configure_sym(sym, self._syms[sym])

    def _rebuild_tick_set(self) -> None:
        if not self._enabled:
            self._tick_set = frozenset()
        else:
            self._tick_set = frozenset(self._watch) | frozenset(self._levels)

    def _configure_sym(self, symbol: str, st: _Sym) -> None:
        """按当前配置(重)建该币的窗口/破位状态; 参数没变的窗口沿用旧状态(不丢样本、不丢冷却)。"""
        p = self._p
        wins, ranges = [], []
        if symbol in self._watch:
            if p["drop_enabled"]:
                old = {(w.minutes, w.sign): w for w in st.wins}
                for m, pct, cd_s, step in p["drop_windows"]:
                    for d in p["drop_dirs"]:
                        sign = 1 if d == "down" else -1
                        w = _Win(m, sign, pct, cd_s, step)
                        o = old.get((m, sign))
                        if o is not None:
                            w.roll = o.roll
                            w.trig.last_ts, w.trig.last_pct, w.trig.last_price, w.trig.count = \
                                o.trig.last_ts, o.trig.last_pct, o.trig.last_price, o.trig.count
                        else:                     # 新建(启动后第一次): 把上次运行留下的冷却状态接回来
                            sv = self._restore("drop", symbol, m, sign)
                            if sv is not None:
                                w.trig.last_ts = sv[0]
                                w.trig.last_pct = _as_float(sv[1], 0.0)
                                w.trig.last_price = _as_float(sv[2], None)
                        wins.append(w)
            if p["range_enabled"]:
                oldr = {(r.days, r.sign): r for r in st.ranges}
                for days in p["range_days"]:
                    for d in p["range_dirs"]:
                        sign = 1 if d == "down" else -1
                        r = oldr.get((days, sign))
                        if r is None:
                            r = _Range(days, sign)
                            sv = self._restore("range", symbol, days, sign)
                            if sv is not None:
                                r.break_ts = r.alert_ts = sv[0]
                                r.alert_price = _as_float(sv[1], None)
                        ranges.append(r)
        st.wins, st.ranges = wins, ranges

    def _restore(self, kind: str, symbol: str, n, sign: int):
        """上次运行记下的冷却状态 (ts, v1, v2); 没有/太旧/时间在未来 → None。"""
        sv = self._saved.get(self._state_key(kind, symbol, n, sign))
        if sv is None:
            return None
        age = self._safe_now() - sv[0]
        return sv if -MAX_CLOCK_SKEW_S <= age <= STATE_KEEP_S else None

    def _ensure_sym(self, symbol: str) -> _Sym:
        st = self._syms.get(symbol)
        if st is None:
            st = _Sym()
            self._configure_sym(symbol, st)
            self._syms[symbol] = st
        return st

    # ------------------------------------------------------------ 对外: 订阅集合
    def tick_symbols(self) -> set:
        """此刻需要实时报价的币: alerts.symbols + 有活动价位提醒的币。总开关关着时为空。"""
        try:
            return set(self._tick_set)
        except Exception:
            return set()

    # ------------------------------------------------------------ 对外: 实时报价
    def on_tick(self, symbol: str, price: float, ts_ms: int) -> list:
        try:
            if symbol not in self._tick_set:      # 不关心的币: 一次集合查找就走
                return []
            price = float(price)
            if not (0.0 < price < math.inf):      # NaN / 0 / 负数 / inf 一律丢弃
                return []
            now = self._safe_now()
            try:
                ts = float(ts_ms) / 1000.0
            except (TypeError, ValueError):
                ts = now
            if not (abs(ts - now) <= MAX_CLOCK_SKEW_S):   # 含 NaN
                ts = now
            st = self._syms.get(symbol)
            if st is None:
                st = self._ensure_sym(symbol)
            first = st.ticks == 0
            st.ticks += 1
            self._ticks_total += 1
            st.real_wall = now
            msgs = self._process(symbol, st, price, ts, None, first)
            st.real_ts = st.last_ts
            # 参照低点: 启动后首次 / 长时间没被收盘K刷新(收盘回调没接上)时, 在这里补算(有限速)
            if st.ranges and now >= st.ref_retry_at:
                st.ref_retry_at = now + REF_RETRY_S
                if st.refs_at is None or now - st.refs_at > 2 * self._base_tf_s                         or any(r.ref is None for r in st.ranges):
                    self._refresh_refs(symbol, st, now, None)
            return msgs
        except Exception:
            self._note_error("on_tick")
            return []

    def _process(self, symbol: str, st: _Sym, price: float, ts: float,
                 level_cutoff, first: bool) -> list:
        """一笔报价过三类检测。level_cutoff: 仅兜底合成报价用 —— 晚于它创建的价位不参与判定。"""
        if ts < st.last_ts:
            ts = st.last_ts                        # 乱序报价按最近时间算, 保证窗口时间单调
        st.last_ts = ts
        st.last_price = price
        msgs = []

        # (a) 价位
        lv = self._levels.get(symbol)
        if lv:
            hit = None
            for L in lv:
                if (price <= L.price) if L.below else (price >= L.price):
                    if level_cutoff is not None and L.created_at > level_cutoff:
                        continue
                    if hit is None:
                        hit = []
                    hit.append(L)
            if hit:
                msgs.extend(self._fire_levels(symbol, hit, price, ts, first))

        # (b) 急跌 / 急涨
        if st.wins:
            fired = None
            for w in st.wins:
                ext_ts, ext = w.roll.update(ts, price)
                move = (ext - price) / ext * 100.0 if w.sign > 0 else (price - ext) / ext * 100.0
                trig = w.trig
                if move >= trig.pct:
                    prev, prev_px = trig.last_pct, trig.last_price
                    last_any = None                # 该币同方向任何窗口最近一次响的时刻
                    for o in st.wins:
                        lt = o.trig.last_ts
                        if o.sign == w.sign and lt is not None and (last_any is None or lt > last_any):
                            last_any = lt
                    kind = trig.check(ts, move, price, w.sign, last_any, w.minutes * 60.0)
                    if kind:
                        if fired is None:
                            fired = []
                        fired.append((w, kind, move, ext, ext_ts, prev, prev_px))
                        self._save_state("drop", symbol, w.minutes, w.sign, ts, move, price)
            if fired:
                for sign in (1, -1):
                    grp = [f for f in fired if f[0].sign == sign]
                    if grp:
                        self._fired["drop"] += 1
                        msgs.append(self._drop_msg(symbol, sign, grp, price, ts))

        # (c) 破位
        if st.ranges:
            for r in st.ranges:
                if r.ref is not None:
                    m = self._check_range(symbol, r, price, ts)
                    if m:
                        msgs.append(m)
        return msgs

    def _check_range(self, symbol: str, r: _Range, price: float, ts: float, label: str = "现价"):
        """一个价格过一档 N 日低点(高点)。越过参照价 → 返回要发的消息或 None。"""
        p = self._p
        buf = p["range_buffer_pct"] / 100.0
        ref = r.ref
        if not ((price < ref * (1.0 - buf)) if r.sign > 0 else (price > ref * (1.0 + buf))):
            return None
        # 冷却从"上一次创新低"起算, 且每创一次新低就顺延 → 一段连续下跌里"破位"只响第一下;
        # 横了至少一个冷却期之后再创新低, 才算新的一次破位。
        # 同一段里如果价格比上一条提醒发出时又低了 step% → 响一条"续跌"(否则阶梯式下跌全程只有一条)。
        last = r.break_ts
        r.break_ts = ts
        more = None
        if last is None or ts - last >= p["range_cooldown_s"]:
            pass
        else:
            step, base = p["range_step_pct"], r.alert_price
            if not (step > 0 and base):
                return None
            more = ((base - price) if r.sign > 0 else (price - base)) / base * 100.0
            if more < step:
                return None
        r.count += 1
        self._fired["range"] += 1
        prev_px = r.alert_price
        r.alert_price, r.alert_ts = price, ts
        self._save_state("range", symbol, r.days, r.sign, ts, price)
        return self._range_msg(symbol, r, price, ref, ts, label, more, prev_px)

    # ------------------------------------------------------------ 对外: 收盘K
    def on_closed_bar(self, symbol: str, tf: str, bar: tuple) -> list:
        try:
            now = self._safe_now()
            if now - self._p_at >= PARAMS_TTL_S:
                self._reload_params()
            if self._unsaved:
                self._flush_unsaved()
            if tf != self._base_tf or symbol not in self._tick_set:
                return []
            st = self._ensure_sym(symbol)
            open_ms = float(bar[0])
            o, h, l, c = float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
            bar_open = open_ms / 1000.0
            bar_end = bar_open + self._base_tf_s
            msgs = []
            # 实时报价缺席(tick 路没接/断线)时, 用这根收盘K兜底: 开→高/低→收 四笔合成报价。
            # 必须在刷新参照低点【之前】做, 否则这根K自己的低点已进参照价, 破位就看不出来了。
            sane = all(0.0 < x < math.inf for x in (o, h, l, c)) and abs(bar_end - now) <= 3 * self._base_tf_s
            if sane and (st.real_ts is None or bar_end - st.real_ts > STALE_TICK_S):
                path = (o, l, h, c) if c >= o else (o, h, l, c)
                step = self._base_tf_s / 3.0
                for i, px in enumerate(path):
                    ts = bar_end if i == 3 else bar_open + i * step
                    msgs.extend(self._process(symbol, st, px, ts, None if i == 3 else bar_open, False))
                if msgs:                           # 让人看得出这条不是实时报价触发的, 且K线内的先后顺序是推定的
                    order = "开→低→高→收" if c >= o else "开→高→低→收"
                    msgs = [f"{m} · 无实时报价, 由{self._base_tf}收盘K按 {order} 估算(K内先后为推定)"
                            for m in msgs]
            elif sane:
                # 报价正常时也拿这根K的最低/最高再核一遍价位: 每秒只取一笔报价, 不足一秒的插针会漏。
                # 只核"这根K开盘前就已存在"的价位(开盘后才建的, 影线可能发生在它之前)。
                lv = self._levels.get(symbol)
                if lv:
                    lo = [L for L in lv if L.below and l <= L.price and L.created_at <= bar_open]
                    hi = [L for L in lv if not L.below and h >= L.price and L.created_at <= bar_open]
                    if lo:
                        msgs.extend(self._fire_levels(symbol, lo, l, bar_end, False, f"{self._base_tf}最低"))
                    if hi:
                        msgs.extend(self._fire_levels(symbol, hi, h, bar_end, False, f"{self._base_tf}最高"))
                # 破位同理: 这根K的最低/最高若越过了【刷新前】的参照价而报价没赶上(收盘前最后一秒被节流、
                # 不足一秒的插针), 必须在下面刷新参照价之前补判 —— 否则这根K的低点并进参照价, 这次破位就永远
                # 看不出来了。报价已经报过的, 这里落在冷却内, 不会重复。
                for r in st.ranges:
                    if r.ref is not None:
                        m = self._check_range(symbol, r, l if r.sign > 0 else h, bar_end,
                                              f"{self._base_tf}{'最低' if r.sign > 0 else '最高'}")
                        if m:
                            msgs.append(m)
            if st.ranges:
                self._refresh_refs(symbol, st, now, bar)
            return msgs
        except Exception:
            self._note_error("on_closed_bar")
            return []

    def _refresh_refs(self, symbol: str, st: _Sym, now: float, bar) -> None:
        """由已收盘K线重算 N 日低点/高点。bar: 刚收盘的那根(缓存可能还没来得及放进去, 一并算上)。"""
        st.refs_at = now
        tfs = self._p["tfs"]
        for r in st.ranges:
            try:
                best, best_t, used = window_extreme(self._get_bars, symbol, tfs, r.days, r.sign, now)
                if best is not None and bar is not None:
                    v = float(bar[3] if r.sign > 0 else bar[2])
                    if 0.0 < v < math.inf and (v < best if r.sign > 0 else v > best):
                        best, best_t = v, float(bar[0]) / 1000.0 + self._base_tf_s
                r.ref, r.ref_ts, r.tf = best, best_t, used
                if best is not None and not r.booted:
                    r.booted = True
                    # 上一次破位的时刻 = max(上次运行记下的提醒时刻, 已收盘K线里最后一次创新低)。
                    # 前者管"重启落在刚报过的那根未收盘K里", 后者管"报过之后又在已收盘K里继续创了新低"。
                    seen = last_break_from_bars(
                        self._get_bars, symbol, tfs, r.days, r.sign, now, self._p["range_cooldown_s"])
                    if seen is not None and (r.break_ts is None or seen > r.break_ts):
                        r.break_ts = seen
            except Exception:
                r.ref = None                       # 算不出来就不报, 不拿旧参照价硬撑
                self._note_error("refresh_refs")

    # ------------------------------------------------------------ 价位: 触发与持久化
    def _fire_levels(self, symbol: str, hit: list, price: float, ts: float, first: bool,
                     label: str = "现价", extra: str = "") -> list:
        msgs = []
        fired_at = int(ts)
        with self._lock:
            cur = self._levels.get(symbol, ())
            ids = {L.id for L in hit}
            really = [L for L in cur if L.id in ids]          # 期间被删掉的不再响
            if not really:
                return msgs
            rest = tuple(L for L in cur if L.id not in ids)
            new = dict(self._levels)
            if rest:
                new[symbol] = rest
            else:
                new.pop(symbol, None)
            self._levels = new
            self._rebuild_tick_set()
        for L in really:
            self._fired["level"] += 1
            if not self._save_fired(L.id, fired_at, price):
                self._unsaved.append((L.id, fired_at, price))
            msgs.append(self._level_msg(L, price, ts, first and L.restored, label, extra))
        return msgs

    def rescan_levels(self, symbol=None) -> list:
        """用已收盘K线补查「行情中断期间(停机/断线)被触及、之后又回去了」的价位。返回要发的消息。

        依据: 一个价位只要还在生效, 就说明在有报价的时间里没被越过(越过即响、响了即移出)。
        所以「整根都在它创建之后」的已收盘K里若有一根的最低/最高越过了它, 只可能发生在没有报价的时候。
        引擎在启动回填之后、看门狗回填之后各调用一次(不在逐笔路径上); 永不抛异常。"""
        out: list = []
        try:
            if not self._enabled:
                return out
            now = self._safe_now()
            for sym, lv in list(self._levels.items()):
                if symbol is not None and sym != symbol:
                    continue
                for L in lv:
                    hit = None
                    for tf, tf_s in self._p["tfs"]:           # 由小到大: 小级别覆盖得到就用小级别的那根
                        for b in self._get_bars(sym, tf) or ():
                            ot = float(b["open_time"]) / 1000.0
                            end = ot + tf_s
                            if ot < L.created_at or end > now + MAX_CLOCK_SKEW_S:
                                continue
                            px = float(b["low"] if L.below else b["high"])
                            if not (0.0 < px < math.inf):
                                continue
                            if (px <= L.price) if L.below else (px >= L.price):
                                hit = (px, end, tf)
                                break
                        if hit:
                            break
                    if hit:
                        px, end, tf = hit
                        out.extend(self._fire_levels(
                            sym, [L], px, end, False, f"{tf}{'最低' if L.below else '最高'}",
                            f"行情中断期间触及, 由已收盘K线补查({bkk(end - TF_SEC.get(tf, 0), '%m-%d %H:%M')} 那根)"))
            return out
        except Exception:
            self._note_error("rescan_levels")
            return out

    def _save_fired(self, alert_id: int, fired_at: int, price: float) -> bool:
        try:
            self.db.execute(
                "UPDATE price_alerts SET status='fired', fired_at=?, fired_price=? "
                "WHERE id=? AND status='active'", (fired_at, price, alert_id))
            return True
        except Exception:
            self._note_error("save_fired")
            return False

    def _flush_unsaved(self) -> None:
        """补写之前没写进库的"已触发"。半分钟最多试一轮, 遇到第一条失败就停(库还没好, 别反复撞)。"""
        now = time.time()
        if now < self._unsaved_retry_at:
            return
        self._unsaved_retry_at = now + 30.0
        while self._unsaved:
            if not self._save_fired(*self._unsaved[0]):
                break
            self._unsaved.pop(0)
        del self._unsaved[:-200]                               # 库一直写不进去也不让它无限涨

    # ------------------------------------------------------------ 文案
    def _level_msg(self, L: _Level, price: float, ts: float, after_restart: bool,
                   label: str = "现价", extra: str = "") -> str:
        sym = html.escape(L.symbol)
        word = "跌破" if L.below else "涨破"         # 与 Telegram 指令 / 网页列表的用词一致
        lines = [f"🎯 <b>价位提醒</b> {sym} {word} {fmt_price(L.price)}",
                 f"{label} {fmt_price(price)} · #{L.id}"]
        if L.note:
            lines[1] += f" {html.escape(L.note)}"
        if after_restart:
            side = "下方" if L.below else "上方"
            lines.append(f"重启后首个报价已在该位{side}")
        if extra:
            lines.append(extra)
        lines.append(bkk(ts))
        return "\n".join(lines)

    def _drop_msg(self, symbol: str, sign: int, grp: list, price: float, ts: float) -> str:
        sym = html.escape(symbol)
        down = sign > 0
        deeper = any(k in ("deeper", "lower") for _, k, *_ in grp)
        title = ("急跌加深" if deeper else "急跌") if down else ("急涨加速" if deeper else "急涨")
        emoji = "🔻" if down else "🔺"
        s = "-" if down else "+"
        moves = " · ".join(f"{w.minutes}分钟 {s}{mv:.2f}%" for w, _, mv, *_ in grp)
        ext_word = "高点" if down else "低点"
        ext_list = [f"{fmt_price(ext)}({bkk(ext_ts, '%H:%M')})" for _, _, _, ext, ext_ts, *_ in grp]
        if len(set(ext_list)) == 1:                # 几个窗口的高点是同一个 → 只写一次
            ext_list = ext_list[:1]
        exts = " / ".join(ext_list)
        ths = " / ".join(_fmt_pct(w.trig.pct) for w, *_ in grp)
        tail = f"阈值 {ths}"
        prevs = [f"{s}{prev:.2f}%" for _, k, _, _, _, prev, _ in grp if k == "deeper"]
        if prevs:
            tail += f" · 上次 {' / '.join(prevs)}"
        # 冷却内的第二段: 窗口内幅度没比上次大, 但价格比上次提醒时又走远了一个步长
        lows = [ppx for _, k, _, _, _, _, ppx in grp if k == "lower" and ppx]
        if lows:
            ppx = lows[0]
            gap = abs(ppx - price) / ppx * 100.0
            tail += f" · 较上次提醒价 {fmt_price(ppx)} 再{'跌' if down else '涨'} {gap:.2f}%"
        return (f"{emoji} <b>{title}</b> {sym} {moves}\n"
                f"现价 {fmt_price(price)} ← 窗口{ext_word} {exts}\n"
                f"{tail} · {bkk(ts)}")

    def _range_msg(self, symbol: str, r: _Range, price: float, ref: float, ts: float,
                   label: str = "现价", more=None, prev_px=None) -> str:
        sym = html.escape(symbol)
        d = f"{r.days:g}"
        made = f"({bkk(r.ref_ts)})" if r.ref_ts else ""      # 参照价那根K线的收盘时刻
        # 注意: Telegram HTML 里不能出现裸的 < > &, 所以这里不用比较符号
        down = r.sign > 0
        if more is not None and prev_px:                     # 同一次破位里又走远了一个步长
            head = f"📉 <b>跌破{d}日低点后续跌</b> {sym}" if down else f"📈 <b>突破{d}日高点后续涨</b> {sym}"
            body = (f"{label} {fmt_price(price)} · 较上次提醒价 {fmt_price(prev_px)} "
                    f"再{'跌' if down else '涨'} {more:.2f}%")
        elif down:
            head = f"📉 <b>跌破{d}日低点</b> {sym}"
            body = f"{label} {fmt_price(price)} · 近{d}日最低 {fmt_price(ref)}{made}"
        else:
            head = f"📈 <b>突破{d}日高点</b> {sym}"
            body = f"{label} {fmt_price(price)} · 近{d}日最高 {fmt_price(ref)}{made}"
        return f"{head}\n{body}\n{bkk(ts)}"

    # ------------------------------------------------------------ 对外: 价位增删查
    def _price_info(self, symbol: str) -> tuple:
        """→ (最近已知价格, 它的时刻[秒])。新鲜的实时报价优先, 否则取各级别最近一根收盘价里最新的; 没有 → (None, None)。"""
        st = self._syms.get(symbol)
        now = self._safe_now()
        if st is not None and st.last_price is not None and st.real_wall is not None \
                and now - st.real_wall <= MAX_CLOCK_SKEW_S:
            return st.last_price, st.real_wall
        best_t, best = None, None
        for tf, tf_s in self._p["tfs"]:
            bars = self._get_bars(symbol, tf)
            if not bars:
                continue
            b = bars[-1]
            t = float(b["open_time"]) / 1000.0 + tf_s
            c = float(b["close"])
            if 0.0 < c < math.inf and (best_t is None or t > best_t):
                best_t, best = t, c
        if st is not None and st.last_price is not None and (best_t is None or st.last_ts >= best_t):
            return st.last_price, st.last_ts
        return best, best_t

    def _price_max_age(self) -> float:
        return PRICE_MAX_AGE_BARS * self._base_tf_s + 60.0

    def last_price(self, symbol: str):
        """最近已知价格: 新鲜的实时报价优先, 否则取最近一根收盘价; 没有、或已经过期(这个币的行情断了,
        比如已被移出监控范围而缓存还留着几小时前的K线) → None。过期的价格不能当现价用。"""
        try:
            px, t = self._price_info(symbol)
            if px is None or t is None or self._safe_now() - t > self._price_max_age():
                return None
            return px
        except Exception:
            self._note_error("last_price")
            return None

    def _resolve_symbol(self, symbol) -> tuple:
        """→ (规范化的symbol, 最近价)。支持省略 USDT 后缀(BTC → BTCUSDT)。"""
        s = str(symbol or "").strip().upper()
        if not SYMBOL_RE.match(s):
            return None, None
        px = self.last_price(s)
        if px is None and not s.endswith("USDT") and SYMBOL_RE.match(s + "USDT"):
            px2 = self.last_price(s + "USDT")
            if px2 is not None:
                return s + "USDT", px2
        return s, px

    def add_level(self, symbol: str, price: float, op=None, note: str = "") -> dict:
        try:
            if not self._db_ok:
                return {"ok": False, "error": "提醒数据表不可用, 无法保存价位提醒"}
            if not self._enabled:
                return {"ok": False, "error": "提醒引擎未启用(alerts.enabled=false)"}
            sym, last = self._resolve_symbol(symbol)
            if sym is None:
                return {"ok": False, "error": "币种格式不对, 例: BTCUSDT"}
            px = _as_float(price, None)
            if px is None or px <= 0:
                return {"ok": False, "error": "价格无效, 需要一个大于 0 的数字"}
            if op is None or (isinstance(op, str) and not op.strip()):
                op_n = None
            else:
                op_n = _OP_ALIASES.get(str(op).strip().lower())
                if op_n is None:
                    return {"ok": False, "error": "方向无效, 只支持 below(跌破) / above(涨破)"}
            if last is None:
                old_px, old_t = self._price_info(sym)
                if old_px is not None and old_t is not None:
                    mins = max(0.0, self._safe_now() - old_t) / 60.0
                    return {"ok": False, "error": f"{sym} 的行情已过期(最近一笔在 {mins:.0f} 分钟前, 该币当前"
                                                  f"不在行情订阅里), 无法设置提醒; 可先把它加入关注列表"}
                return {"ok": False, "error": f"没有 {sym} 的行情(不在监控列表里), 无法设置提醒"}
            ratio = px / last
            if not (LEVEL_MIN_RATIO <= ratio <= LEVEL_MAX_RATIO):
                return {"ok": False, "error": f"提醒价 {fmt_price(px)} 与当前价 {fmt_price(last)} 相差过大"
                                              f"({(ratio - 1.0) * 100.0:+.1f}%), 请检查是否写错"
                                              f"(例如 63 000 中间的空格会被拆开, 请写 63000 或 63k)"}
            if op_n is None:
                if px == last:
                    return {"ok": False, "error": f"提醒价与当前价 {fmt_price(last)} 相同, 请指明方向或换个价位"}
                op_n = "below" if px < last else "above"
            if op_n == "below" and last <= px:
                return {"ok": False, "error": f"当前价 {fmt_price(last)} 已在 {fmt_price(px)} 下方, 「跌破」提醒已无意义"}
            if op_n == "above" and last >= px:
                return {"ok": False, "error": f"当前价 {fmt_price(last)} 已在 {fmt_price(px)} 上方, 「涨破」提醒已无意义"}
            note_s = str(note or "").strip()[:MAX_NOTE_LEN]
            created = int(self._safe_now())
            with self._lock:
                n_active = sum(len(v) for v in self._levels.values())
                cap = self._p["max_active"]
                if n_active >= cap:
                    return {"ok": False, "error": f"生效中的价位提醒已达上限 {cap} 条, 请先删除一些"}
                for L in self._levels.get(sym, ()):
                    if L.below == (op_n == "below") and L.price == px:
                        return {"ok": False, "error": f"已有相同的提醒 #{L.id}"}
            # 写库不拿锁: 行情线程触发价位时要拿同一把锁, 不能让它等磁盘
            cur = self.db.execute(
                "INSERT INTO price_alerts (symbol, op, price, note, status, created_at, created_price) "
                "VALUES (?,?,?,?,'active',?,?)", (sym, op_n, px, note_s, created, last))
            aid = cur.lastrowid
            with self._lock:
                new = dict(self._levels)
                new[sym] = new.get(sym, ()) + (_Level(aid, sym, op_n, px, note_s, created, False),)
                self._levels = new
                self._rebuild_tick_set()
            return {"ok": True, "alert": {
                "id": aid, "symbol": sym, "op": op_n, "price": px, "note": note_s, "status": "active",
                "created_at": created, "fired_at": None, "fired_price": None}}
        except Exception:
            self._note_error("add_level")
            return {"ok": False, "error": "保存提醒失败(内部错误), 请稍后再试"}

    def list_levels(self, include_fired: bool = False) -> list:
        """活动中的价位提醒(新的在前); include_fired=True 时再附上最近 100 条已触发的。已删除的不返回。"""
        try:
            cols = "id, symbol, op, price, note, status, created_at, fired_at, fired_price"
            rows = list(self.db.query(
                f"SELECT {cols} FROM price_alerts WHERE status='active' ORDER BY id DESC"))
            if include_fired:
                rows += list(self.db.query(
                    f"SELECT {cols} FROM price_alerts WHERE status='fired' "
                    "ORDER BY fired_at DESC, id DESC LIMIT 100"))
            return [{k: r[k] for k in ("id", "symbol", "op", "price", "note", "status",
                                       "created_at", "fired_at", "fired_price")} for r in rows]
        except Exception:
            self._note_error("list_levels")
            return []

    def remove_level(self, alert_id: int) -> bool:
        """删除一条价位提醒(活动中的停止监控; 已触发的从列表里隐藏)。不存在/已删 → False。"""
        try:
            aid = int(alert_id)
            cur = self.db.execute(                          # 先落库(不拿锁), 成功了再动内存
                "UPDATE price_alerts SET status='removed' WHERE id=? AND status IN ('active','fired')",
                (aid,))
            changed = cur.rowcount > 0
            with self._lock:
                new, touched = {}, False
                for sym, lv in self._levels.items():
                    keep = tuple(L for L in lv if L.id != aid)
                    touched = touched or len(keep) != len(lv)
                    if keep:
                        new[sym] = keep
                if touched:
                    self._levels = new
                    self._rebuild_tick_set()
            return bool(changed or touched)
        except Exception:
            self._note_error("remove_level")
            return False

    # ------------------------------------------------------------ 对外: 状态
    def status(self) -> dict:
        try:
            p = self._p
            now = self._safe_now()
            syms = dict(self._syms)
            refs, ages, prices = {}, {}, {}
            for sym, st in syms.items():
                ages[sym] = None if st.real_wall is None else round(max(0.0, now - st.real_wall), 1)
                prices[sym] = st.last_price
                rr = {}
                for r in st.ranges:
                    rr[f"{r.days:g}d_{'low' if r.sign > 0 else 'high'}"] = {
                        "price": r.ref, "at": None if r.ref_ts is None else int(r.ref_ts),
                        "at_bkk": None if r.ref_ts is None else bkk(r.ref_ts), "tf": r.tf, "fired": r.count}
                if rr:
                    refs[sym] = rr
            levels = self._levels
            return {
                "enabled": self._enabled,
                "symbols": list(p["symbols"]),
                "tick_symbols": sorted(self._tick_set),
                "base_tf": self._base_tf,
                "thresholds": {
                    "drop": {
                        "enabled": p["drop_enabled"], "directions": list(p["drop_dirs"]),
                        "windows": [{"minutes": m, "pct": pct, "cooldown_min": cd / 60.0, "step_pct": step}
                                    for m, pct, cd, step in p["drop_windows"]]},
                    "range_break": {
                        "enabled": p["range_enabled"], "directions": list(p["range_dirs"]),
                        "days": list(p["range_days"]), "cooldown_min": p["range_cooldown_s"] / 60.0,
                        "buffer_pct": p["range_buffer_pct"], "step_pct": p["range_step_pct"]},
                    "levels": {"max_active": p["max_active"]},
                },
                "ref_lows": refs,
                "last_tick_age": ages,
                "last_price": prices,
                "counts": {
                    "levels_active": sum(len(v) for v in levels.values()),
                    "ticks": self._ticks_total,
                    "fired": dict(self._fired),
                    "errors": self._errors,
                    "unsaved": len(self._unsaved),
                },
            }
        except Exception:
            self._note_error("status")
            return {"enabled": False, "error": "status 读取失败"}
