"""独立策略存档:「BTC 放量跌破两日低 → PUMP 首个底分型(+停顿)做多」联动规则 btcbreak_pump。

状态: 未审核 - 仅提醒与模拟, 无实盘通道。
纯模块: 不联网、不读写库、不 import 引擎/下单代码; 只复用 chan.py / chan_bi.py 的
包含合并(merge_klines)、分型(find_fractals)、停顿K(stall_after) —— 与已锁定的
「一买 = 底分型 + 停顿」是同一套定义。

规则(合约 CONTRACT B):
  1. 触发(BTCUSDT 5m, 收盘判定): 收盘价 < 前 48h 窗口(不含本K)所有K的最低 low,
     且 成交量 ≥ vol_mult × 前 vol_ma 根(不含本K)均量。触发后进入「武装」状态,
     有效期到 触发K收盘时刻 + ttl_hours; 武装期内再次触发 = retrigger, 有效期从后一次重新起算。
  2. 入场(PUMPUSDT 5m): 破位之后形成的【第一个】底分型。K线先做包含合并, 底分型 = 三根合并K
     L/M/R 且 M 最低; M 内最低的那根原始K的 open_time 必须 ≥ 首次触发K的 open_time。
     require_stall=True(默认): 分型确认后 stall_max_gap 根内第一根「收盘 > 右侧合并K最高价」的K = 信号K;
     gap 内没有 → 放弃该分型, 只要仍在武装期就继续看下一个底分型。
     require_stall=False: 信号K = 确认K。
  3. 入场价 = 信号K收盘; SL = 分型低点 × (1 - sl_buffer_pct/100); TP = 入场 + rr × (入场 - SL)。
  4. 一轮(episode)只出一次入场; 入场或到期后回到 idle, 需要新的触发。
  5. 严格因果: t 时刻的判定只用 t 及之前已收盘的K; 后面有没有K, 已产生的事件完全一样。

==================== 合约之外不得不做的定义选择(逐条) ====================
 A. 时间口径。事件里的 time / *_time 都是K线 open_time(ms)。armed_at / expires_at / expired_at
    是「时刻」(ms): armed_at = 首次触发K的收盘时刻, expires_at = 最近一次触发K收盘时刻 + ttl,
    expired_at 恒等于到期那一刻的 expires_at。一根K在 open_time + tf 才收盘、才参与判定。
    信号K的收盘时刻 ≤ expires_at 即有效(含端点); 默认参数下 = 触发K之后的第 1~48 根。
    ttl_hours 向下取整到整根K(默认 4h = 48 根, 本就是整数), 所以 expires_at 总落在K线收盘时刻上。
 B. 两日低窗口 = open_time ∈ [t - 48h, t) 的已收K(按时间, 不按根数); 严格小于才算跌破(相等不算)。
    并列最低时 ref_low_time 取最近的一根。
 C. 窗口没铺满不触发: 喂入的第一根 BTC K 必须不晚于 t - 48h, 且窗口内根数 ≥ 98%
    (MIN_WINDOW_COVERAGE, 默认 576 根里至少 565 根)。历史不够时宁可不报, 不拿「不到两天的低点」冒充。
 D. 均量 = 紧挨触发K之前、已接受的 vol_ma 根的简单平均(按到达序列, 不含触发K)。不足 vol_ma 根
    或均量为 0 不触发。量 = 币本位成交量("volume"), 与 chan.volume_ratio 同口径。门槛含等号(≥)。
 E. retrigger 只延长有效期, 不移动「低点须在破位K之后」的锚点 —— 锚点始终是本轮【首次】触发K。
 F. 同一根K(同 open_time)上「BTC 再次破位」和「PUMP 入场信号」同时出现: 两根K同一时刻收盘、
    到达先后任意, 所以两者都按「这根K之前的状态」判定 —— 入场成立; 这次破位记为本轮的 retrigger
    (进 episode.retriggers); entry.last_trigger_time 只取信号K【之前】的最后一次触发; 随后回到 idle,
    这次破位不会另开新一轮。只有这样, 结果才与两根K谁先到无关。
 G. 分型计算范围 = PUMP 自「首次触发K之前 PRE_BARS(60)根」到当前K。一轮之内起点固定, 所以包含合并的
    结果在这一轮里前后一致, 且严格等于在同一段K上跑 chan.merge_klines / find_fractals。
    每根K收盘只问一句:「有没有信号恰好在这根K完成」, 不回看、不改写已发出的事件。
 H. 「分型低点」= 中间合并K里最低的那根原始K(chan.Fractal.extreme_src_idx; 并列取最早)。
 I. 「确认」的因果化。合约写「R 的最后一根原始K收盘时确认」, 但在那一刻无法知道它是不是最后一根
    (下一根可能仍被 R 包含)。因此:
      - require_stall=True: 完全照 chan_bi.stall_after —— R 被后面一根不包含的K终结, 自 R 末根之后的
        stall_max_gap 根内, 第一根 收盘 > R(合并后)最高价 的K = 信号K。候选K按定义都不属于 R,
        判定时 R 已定型, 没有未来函数。fx_confirm_time = R 的最后一根原始K。
      - require_stall=False: 信号K = R 的第一根原始K(三根合并K的形态首次成立的那一根)。此后 R 再吞并
        新K只会让 R 更高, 不会让这个底分型失效。此时 fx_confirm_time = fx_right_time = signal_time。
 J. 同一根K上有多个分型同时满足 → 取最早的那个分型(「第一个」)。
 K. 停顿之前分型低点已被跌破: 默认【不剔除】(stall_after 本身不看这个), 用 entry.fx_low_intact 标出来
    供审图; require_low_intact=True(合约外新增参数, 默认 False)可直接剔除这类分型。
 L. SL / TP / 均量 保留 10 位有效数字; risk_pct = (入场 - SL) / 入场 × 100, 保留 4 位小数。
    不做交易所 tick 取整(本规则不下单)。
 M. 到期事件什么时候发出: 两个品种都已收到「收盘时刻 ≥ expires_at」的K后立即发出; 若某品种缺K,
    则在任一品种下一根K到来时补发; replay 在数据末尾用 expire_until 收尾。事件内容只由数据决定。
 N. 重复/乱序/异常K: 每个品种只接受 open_time 严格递增的K, 其余忽略(以第一次到达的为准)。
    未收盘("closed"=0)、字段缺失/非数、不在 tf 网格上的K 一律忽略, 不抛异常。
 O. 到达顺序。保证的是: 按 open_time 不减的顺序喂入时(实盘 WS 和 replay 都是这样), 同一 open_time
    的两根K先后任意, 事件集合完全相同; 唯一的差别是 F 条那种同K的 retrigger / entry 在 on_bar 返回
    里的先后, replay 会按(判定时刻, retrigger 在前)排成规范顺序。若某品种比另一品种落后超过一根K,
    迟到的K仍按当前状态安全处理, 但不保证与理想时序一致。
 P. seed 需要的历史: BTC 至少 lookback + 约 ttl 的长度(默认 > 52h ≈ 630 根 5m)才能在启动后立刻有效;
    引擎内存缓存启动时约 500 根、上限 550 根(≈45.8h), 不够, 要从 sqlite klines 表(1500 根)取。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass

from .chan import find_fractals, merge_klines
from .chan_bi import stall_after

STATUS = "未审核 - 仅提醒与模拟,无实盘通道"

MIN_WINDOW_COVERAGE = 0.98   # 选择 C: 两日窗口内K线根数的最低覆盖率
PRE_BARS = 60                # 选择 G: 分型计算范围在首次触发K之前多带的根数
TARGET_KEEP_BARS = 720       # 目标品种原始K保留根数(60h), 超长的一轮才会用满

_TF_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
_EVENT_RANK = {"expired": 0, "armed": 1, "retrigger": 1, "entry": 2}


def tf_to_ms(tf: str) -> int:
    """'5m' → 300000。只支持 m/h/d。"""
    try:
        n = int(tf[:-1])
        unit = _TF_UNIT_MS[tf[-1]]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError(f"unsupported tf: {tf!r}") from None
    if n <= 0:
        raise ValueError(f"unsupported tf: {tf!r}")
    return n * unit


@dataclass
class BtcBreakParams:
    name: str = "btcbreak_pump"
    trigger_symbol: str = "BTCUSDT"
    target_symbol: str = "PUMPUSDT"
    tf: str = "5m"
    lookback_hours: float = 48.0      # 「两日低」
    vol_ma: int = 20
    vol_mult: float = 2.0
    ttl_hours: float = 4.0            # 最近一次触发后的武装时长
    require_stall: bool = True
    stall_max_gap: int = 3
    sl_buffer_pct: float = 0.3
    rr: float = 2.0
    require_low_intact: bool = False  # 合约外新增(选择 K): True = 停顿前分型低点被跌破的分型直接剔除


def _sig(x: float, digits: int = 10) -> float:
    return float(f"{x:.{digits}g}")


def _norm_bar(bar):
    """把一根K规整成 (open_time, o, h, l, c, v); 未收盘/畸形返回 None。
    支持 dict / sqlite3.Row(按键取) 和 tuple/list(前 6 位 = open_time,o,h,l,c,volume;
    长度恰为 9 时第 9 位是引擎的 closed 标记)。"""
    try:
        if isinstance(bar, (tuple, list)):
            if len(bar) == 9 and not bar[8]:
                return None
            t, o, h, l, c, v = bar[0], bar[1], bar[2], bar[3], bar[4], bar[5]
        else:
            if "closed" in bar.keys() and not bar["closed"]:
                return None
            t, o, h, l, c, v = (bar["open_time"], bar["open"], bar["high"],
                                bar["low"], bar["close"], bar["volume"])
        t = int(t)
        o, h, l, c, v = float(o), float(h), float(l), float(c), float(v)
    except Exception:
        return None
    if t < 0 or not all(map(math.isfinite, (o, h, l, c, v))):
        return None
    if l <= 0 or h < l or v < 0:
        return None
    return t, o, h, l, c, v


def event_instant(ev: dict, tf_ms: int) -> int:
    """事件的判定时刻(ms): armed/retrigger = 触发K收盘; entry = 信号K收盘; expired = expired_at。"""
    kind = ev["type"]
    if kind in ("armed", "retrigger"):
        return int(ev["trigger"]["time"]) + tf_ms
    if kind == "entry":
        return int(ev["entry"]["signal_time"]) + tf_ms
    return int(ev["expired_at"])


def sort_events(events: list, tf_ms: int) -> list:
    """规范顺序: 按判定时刻; 同一时刻 retrigger 在 entry 之前(选择 F / O)。稳定排序。"""
    return sorted(events, key=lambda e: (event_instant(e, tf_ms), _EVENT_RANK.get(e["type"], 9)))


class BtcBreakRule:
    """流式状态机。on_bar 喂两个品种的【已收盘】K, 返回事件列表; 不做任何 IO。"""

    def __init__(self, params: BtcBreakParams | None = None):
        p = params or BtcBreakParams()
        tf = tf_to_ms(p.tf)
        lookback = int(round(float(p.lookback_hours) * 3_600_000))
        ttl = int(round(float(p.ttl_hours) * 3_600_000)) // tf * tf   # 选择 A: 向下取整到整根K
        if lookback < tf:
            raise ValueError("lookback_hours must cover at least one bar")
        if ttl < tf:
            raise ValueError("ttl_hours must cover at least one bar")
        if int(p.vol_ma) < 1 or not float(p.vol_mult) > 0:
            raise ValueError("vol_ma must be >= 1 and vol_mult > 0")
        if int(p.stall_max_gap) < 1:
            raise ValueError("stall_max_gap must be >= 1")
        if float(p.sl_buffer_pct) < 0 or float(p.sl_buffer_pct) >= 100 or not float(p.rr) > 0:
            raise ValueError("sl_buffer_pct must be in [0, 100) and rr > 0")
        self.params = p
        self._tf = tf
        self._lookback = lookback
        self._ttl = ttl
        self._need = max(1, math.ceil(MIN_WINDOW_COVERAGE * (lookback // tf)))
        # ---- 触发品种 ----
        self._tw: deque = deque()                  # 单调队列 (open_time, low), low 严格递增 → 队首 = 窗口最低
        self._tt: deque = deque()                  # 窗口内K的 open_time(覆盖率)
        self._tv: deque = deque(maxlen=int(p.vol_ma))   # 最近 vol_ma 根的量
        self._t_first: int | None = None
        self._t_last: int | None = None
        # ---- 目标品种 ----
        self._g: deque = deque(maxlen=TARGET_KEEP_BARS)  # 原始K(dict 行)
        self._g_last: int | None = None
        # ---- 轮次 ----
        self._ep: dict | None = None               # 当前武装中的一轮
        self._prev: dict | None = None             # 最近结束的一轮(选择 F 的同K retrigger 要用)

    # ------------------------------------------------------------------ 对外
    def on_bar(self, symbol: str, bar) -> list[dict]:
        """喂一根已收盘K。返回事件列表(可能为空)。重复/乱序/畸形K忽略。"""
        p = self.params
        is_trigger = symbol == p.trigger_symbol
        is_target = symbol == p.target_symbol
        if not (is_trigger or is_target):
            return []
        n = _norm_bar(bar)
        if n is None or n[0] % self._tf:
            return []
        events: list[dict] = []
        if is_trigger and (self._t_last is None or n[0] > self._t_last):
            events.extend(self._on_trigger_bar(*n))
        if is_target and (self._g_last is None or n[0] > self._g_last):
            events.extend(self._on_target_bar(*n))
        return events

    def seed(self, trigger_bars: list, target_bars: list) -> None:
        """用历史预热, 不返回事件; 结束后的状态与「把这些K按时间顺序实时喂一遍」完全相同。"""
        for sym, n in _merged_feed(self.params, trigger_bars, target_bars):
            self.on_bar(sym, n)

    def expire_until(self, now_ms: int) -> list[dict]:
        """调用方声明「收盘时刻 ≤ now_ms 的K都已喂完」。若武装期已过则发出 expired。
        replay 在数据末尾调用; 实盘不必调用(on_bar 自己会发)。"""
        events: list[dict] = []
        ep = self._ep
        if ep is not None and int(now_ms) >= ep["expires_at"]:
            self._expire(ep, events)
        return events

    def state(self) -> dict:
        """当前状态快照(只读, 可 JSON 序列化)。ref_low = 下一根触发品种K需要收破的价位。"""
        p = self.params
        ep = self._ep
        ref_low = ref_low_time = None
        warm = False
        window_bars = 0
        if self._t_last is not None:
            lo = self._t_last + self._tf - self._lookback
            for tm, lw in self._tw:
                if tm >= lo:
                    ref_low, ref_low_time = lw, tm
                    break
            skip = 0
            for tm in self._tt:
                if tm >= lo:
                    break
                skip += 1
            window_bars = len(self._tt) - skip
            warm = (self._t_first is not None and self._t_first <= lo
                    and window_bars >= self._need and len(self._tv) == int(p.vol_ma))
        vma = _sig(sum(self._tv) / len(self._tv)) if len(self._tv) == int(p.vol_ma) else None
        prev = self._prev
        return {
            "rule": p.name,
            "phase": "armed" if ep is not None else "idle",
            "episode_id": ep["id"] if ep else None,
            "armed_at": ep["id"] + self._tf if ep else None,
            "expires_at": ep["expires_at"] if ep else None,
            "trigger": dict(ep["trigger"]) if ep else None,
            "last_trigger_time": ep["times"][-1] if ep else None,
            "retriggers": list(ep["times"][1:]) if ep else [],
            "ref_low": ref_low,
            "ref_low_time": ref_low_time,
            "vol_ma": vma,
            "vol_threshold": _sig(vma * float(p.vol_mult)) if vma is not None else None,
            "warm": warm,
            "window_bars": window_bars,
            "window_need": self._need,
            "last_bar": {"trigger": self._t_last, "target": self._g_last},
            "last_episode": None if prev is None else {
                "episode_id": prev["id"],
                "outcome": prev["outcome"],
                "end_time": prev["end_time"],
                "expires_at": prev["expires_at"],
                "retriggers": list(prev["times"][1:]),
                "trigger": dict(prev["trigger"]),
                "entry": dict(prev["entry"]) if prev["entry"] else None,
            },
        }

    # ------------------------------------------------------------------ 事件
    def _event(self, kind: str, ep: dict, **extra) -> dict:
        ev = {"type": kind, "rule": self.params.name, "episode_id": ep["id"]}
        ev.update(extra)
        return ev

    def _end(self, ep: dict, outcome: str, end_time: int) -> None:
        ep["outcome"] = outcome
        ep["end_time"] = end_time
        self._prev = ep
        self._ep = None

    def _expire(self, ep: dict, events: list) -> None:
        events.append(self._event("expired", ep, trigger=dict(ep["trigger"]),
                                  last_trigger_time=ep["times"][-1],
                                  expired_at=ep["expires_at"]))
        self._end(ep, "expired", ep["expires_at"])

    def _lazy_expire(self, instant: int, events: list) -> None:
        """当前K的收盘时刻已晚于有效期 → 这一轮在这根K之前就到期了。"""
        ep = self._ep
        if ep is not None and instant > ep["expires_at"]:
            self._expire(ep, events)

    def _early_expire(self, events: list) -> None:
        """两个品种都已收到「收盘时刻 ≥ expires_at」的K(下一根必然更晚)→ 立即到期。"""
        ep = self._ep
        if ep is None or self._t_last is None or self._g_last is None:
            return
        x = ep["expires_at"]
        if self._t_last + 2 * self._tf > x and self._g_last + 2 * self._tf > x:
            self._expire(ep, events)

    # ------------------------------------------------------------------ 触发品种
    def _on_trigger_bar(self, t, o, h, l, c, v) -> list[dict]:
        p = self.params
        instant = t + self._tf
        events: list[dict] = []

        # 1) 只用本K【之前】的窗口判定
        lo = t - self._lookback
        tt, tw = self._tt, self._tw
        while tt and tt[0] < lo:
            tt.popleft()
        while tw and tw[0][0] < lo:
            tw.popleft()
        trig = None
        if (self._t_first is not None and self._t_first <= lo and len(tt) >= self._need
                and tw and len(self._tv) == int(p.vol_ma)):
            ref_time, ref_low = tw[0]
            vma = sum(self._tv) / int(p.vol_ma)
            if vma > 0 and c < ref_low and v >= float(p.vol_mult) * vma:
                trig = {"symbol": p.trigger_symbol, "time": t, "close": c,
                        "ref_low": ref_low, "ref_low_time": ref_time,
                        "volume": v, "vol_ma": _sig(vma), "vol_ratio": round(v / vma, 4)}

        # 2) 本K进入窗口(供后面的K使用)
        while tw and tw[-1][1] >= l:
            tw.pop()
        tw.append((t, l))
        tt.append(t)
        self._tv.append(v)
        if self._t_first is None:
            self._t_first = t
        self._t_last = t

        # 3) 轮次
        self._lazy_expire(instant, events)
        if trig is not None:
            new_expiry = instant + self._ttl
            ep = self._ep
            prev = self._prev
            if ep is not None:                                   # 武装期内再次破位
                ep["times"].append(t)
                ep["expires_at"] = new_expiry
                events.append(self._event("retrigger", ep, trigger=dict(trig),
                                          expires_at=new_expiry))
            elif (prev is not None and prev["outcome"] == "entry"
                  and prev["end_time"] == t and prev["times"][-1] < t):
                # 选择 F: 同一根K上入场信号先到 → 这次破位仍记为那一轮的 retrigger
                prev["times"].append(t)
                prev["expires_at"] = new_expiry
                events.append(self._event("retrigger", prev, trigger=dict(trig),
                                          expires_at=new_expiry))
            else:                                                # 新的一轮
                ep = {"id": t, "trigger": trig, "times": [t], "expires_at": new_expiry,
                      "outcome": None, "end_time": None, "entry": None}
                self._ep = ep
                events.append(self._event("armed", ep, trigger=dict(trig),
                                          expires_at=new_expiry))
        self._early_expire(events)
        return events

    # ------------------------------------------------------------------ 目标品种
    def _on_target_bar(self, t, o, h, l, c, v) -> list[dict]:
        instant = t + self._tf
        events: list[dict] = []
        self._g.append({"open_time": t, "open": o, "high": h, "low": l, "close": c, "volume": v})
        self._g_last = t
        self._lazy_expire(instant, events)
        ep = self._ep
        if ep is not None:
            entry = self._find_signal(ep)
            if entry is not None:
                before = [x for x in ep["times"] if x < t]       # 选择 F: 只看信号K之前的触发
                ep["entry"] = entry
                events.append(self._event("entry", ep, trigger=dict(ep["trigger"]),
                                          last_trigger_time=before[-1] if before else ep["id"],
                                          entry=dict(entry)))
                self._end(ep, "entry", t)
        self._early_expire(events)
        return events

    def _find_signal(self, ep: dict):
        """是否有入场信号【恰好在最新这根目标K】完成。有 → 返回 entry dict, 否则 None。"""
        p = self.params
        t0 = ep["id"]                                            # 首次触发K的 open_time(锚点)
        anchor = t0 - PRE_BARS * self._tf
        win = []
        for b in reversed(self._g):
            if b["open_time"] < anchor:
                break
            win.append(b)
        if len(win) < 3:
            return None
        win.reverse()
        last = len(win) - 1
        merged = merge_klines(win)
        if len(merged) < 3:
            return None
        for fx in find_fractals(win, merged):
            if fx.kind != "bottom":
                continue
            low_idx = fx.extreme_src_idx
            if win[low_idx]["open_time"] < t0:                   # 低点必须出在破位K当根或之后
                continue
            rk = fx.mid_merged_idx + 1
            right = merged[rk]
            if p.require_stall:
                if stall_after(win, merged, fx, int(p.stall_max_gap)) != last:
                    continue
                confirm_idx, stall_time = fx.confirm_src_idx, win[last]["open_time"]
            else:
                # 形态在这根K首次成立: R 是最后一根合并K, 且只含当前这一根原始K
                if rk != len(merged) - 1 or right.src_idx != [last]:
                    continue
                confirm_idx, stall_time = last, None
            fx_low = float(fx.extreme_price)
            intact = all(b["low"] >= fx_low for b in win[low_idx + 1:])
            if p.require_low_intact and not intact:
                continue
            entry = float(win[last]["close"])
            sl = fx_low * (1.0 - float(p.sl_buffer_pct) / 100.0)
            risk = entry - sl
            if not risk > 0:
                continue
            return {
                "symbol": p.target_symbol, "tf": p.tf,
                "fx_low": fx_low,
                "fx_low_time": win[low_idx]["open_time"],
                "fx_left_time": merged[rk - 2].open_time,
                "fx_mid_time": merged[rk - 1].open_time,
                "fx_right_time": right.open_time,
                "fx_right_high": float(right.high),
                "fx_confirm_time": win[confirm_idx]["open_time"],
                "stall_time": stall_time,
                "signal_time": win[last]["open_time"],
                "entry": entry,
                "sl": _sig(sl),
                "tp": _sig(entry + float(p.rr) * risk),
                "rr": float(p.rr),
                "risk_pct": round(risk / entry * 100.0, 4),
                "fx_low_intact": intact,
            }
        return None


# ---------------------------------------------------------------------- 回放
def _merged_feed(p: BtcBreakParams, trigger_bars, target_bars) -> list:
    """两个品种的K合并成 [(symbol, 规整后的K)], 按 open_time 升序; 同一时刻触发品种在前。"""
    rows = []
    for order, sym, bars in ((0, p.trigger_symbol, trigger_bars), (1, p.target_symbol, target_bars)):
        for b in bars or ():
            n = _norm_bar(b)
            if n is not None:
                rows.append((n[0], order, sym, n))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(sym, n) for _, _, sym, n in rows]


def episodes_from_events(events: list) -> tuple[list, dict | None]:
    """事件 → (已结束的轮次列表, 仍在武装中的那一轮或 None)。只按 episode_id 归并, 与事件先后无关。"""
    eps: dict = {}
    order: list = []
    for ev in events:
        eid = ev.get("episode_id")
        kind = ev.get("type")
        if kind == "armed":
            eps[eid] = {"episode_id": eid, "trigger": ev["trigger"], "retriggers": [],
                        "expires_at": ev["expires_at"], "outcome": None,
                        "entry": None, "expired_at": None}
            order.append(eid)
    for ev in events:
        ep = eps.get(ev.get("episode_id"))
        if ep is None:
            continue
        kind = ev["type"]
        if kind == "retrigger":
            ep["retriggers"].append(ev["trigger"]["time"])
            ep["expires_at"] = max(ep["expires_at"], ev["expires_at"])
        elif kind == "entry":
            ep["outcome"] = "entry"
            ep["entry"] = ev["entry"]
        elif kind == "expired":
            ep["outcome"] = "expired"
            ep["expired_at"] = ev["expired_at"]
    done, open_ep = [], None
    for eid in order:
        ep = eps[eid]
        ep["retriggers"].sort()
        if ep["outcome"] is None:
            open_ep = ep
        else:
            done.append(ep)
    return done, open_ep


def replay(trigger_klines: list, target_klines: list,
           params: BtcBreakParams | None = None) -> dict:
    """整段历史回放(只找形态, 不结算、不算任何盈亏统计)。
    返回 {"params", "events"(规范顺序), "episodes"(已结束), "open_episode"(数据末尾仍武装的一轮或 None)}。"""
    rule = BtcBreakRule(params)
    p = rule.params
    tf = tf_to_ms(p.tf)
    events: list[dict] = []
    end = None
    for sym, n in _merged_feed(p, trigger_klines, target_klines):
        events.extend(rule.on_bar(sym, n))
        end = n[0]
    if end is not None:
        events.extend(rule.expire_until(end + tf))
    events = sort_events(events, tf)
    episodes, open_ep = episodes_from_events(events)
    return {"params": asdict(p), "events": events, "episodes": episodes, "open_episode": open_ep}
