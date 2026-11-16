"""实盘交易与交易系统的对照描述(只描述, 不评判合规)。

依据: CLAUDE.md 第二章(先定方向, 再顺势回调介入)、第九章(核心=多级别趋势跟随;
辅助=失衡反转; 禁入=盘整区预判) 与 research/ideas/008-multi-timeframe-trend-imbalance-playbook。

硬约束:
- 纯函数, 无 I/O。
- 严格因果: 只使用首笔入场成交之前已收盘的 K 线; 入场之后的任何 K 线都不读。
- 不读取任何结果数据(平仓成交、已实现盈亏、持仓时长)。只读方向、首笔入场时间与入场价。
- 趋势/笔/分型/停顿/中枢全部复用 app/engine/chan.py 与 app/engine/chan_bi.py 的既有定义,
  本模块只做 Kline(Decimal) -> 引擎 dict 行(float) 的适配, 不另立口径。
- 数据不足一律给"未知", 不猜。

证据的 state 只表示"有 / 无", 不表示"做得对 / 做得不对":
- 趋势、入场时刻是纯描述 -> NA(报告里显示为说明行, 没有勾叉);
- 触发、成笔、结构转换、位移、盘整区内: PASS = 有, FAIL = 无("盘整区内 = 有"是需要留意的一种);
- 顺逆势: PASS = 顺势, FAIL = 逆势 / 无序;
- 形态归类: PASS = 对上了三种模型之一, FAIL = 禁入场景或系统外, UNKNOWN = 缺数据。

"顺势回调"这个归类只核对了 顺势 + 反向笔完成 + 小级别触发; CLAUDE.md 第二/九章要求的
"回调到关键位(POC / FVG / 中枢边界 / 前高前低)"没有量化, 所以名字里写明"关键位未核对",
它不等于核心模型成立。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from statistics import median

from app.engine.chan import find_fractals, merge_klines
from app.engine.chan_bi import (
    build_bi,
    build_zhongshu,
    stall_after,
    trend_by_zhongshu,
    trend_state,
)

from .models import Kline, TradeEpisode

try:  # Check / CheckState 的归属模块是 app/review/principles.py(另一位作者并行编写)
    from .principles import Check, CheckState
except ImportError:  # principles.py 尚未就位时, 用与共享契约逐字段一致的本地定义兜底
    class CheckState(str, Enum):
        PASS = "pass"
        FAIL = "fail"
        UNKNOWN = "unknown"
        NA = "na"

    @dataclass(frozen=True)
    class Check:
        key: str
        label: str
        tier: str
        state: CheckState
        detail: str = ""
        value: Decimal | str | None = None


INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}

ALIGNMENTS = ("顺势", "逆势", "无序", "未知")
SETUP_PULLBACK = "顺势回调(关键位未核对)"
SETUP_FORBIDDEN = "盘整区预判(禁入)"
SETUPS = (SETUP_PULLBACK, "趋势转换反转", "失衡反转(辅助)", SETUP_FORBIDDEN, "系统外", "未知")
TRIGGER_NONE = "无"
TRIGGER_UNKNOWN = "未知"

_TIER = "reference"
_TREND_CN = {"up": "上涨", "down": "下跌", "range": "无序", "unknown": "未知"}
_KIND_CN = {"bottom": "底分型", "top": "顶分型"}


@dataclass(frozen=True)
class MatchParams:
    # 引擎口径(保持引擎默认值)
    min_merged: int = 5                 # 一笔至少 5 根合并K (build_bi)
    zs_min_bis: int = 3                 # 中枢至少三笔重叠 (build_zhongshu)
    stall_max_gap: int = 3              # 分型确认后几根内出现停顿K (stall_after)
    zs_min_width_pct: float = 0.0       # 趋势判定时过滤过窄中枢 (significant_zhongshu), 0=不过滤
    zs_filter_min_bis: int = 0          # 趋势判定时过滤笔数过少的中枢, 0=不过滤
    # 趋势
    min_trend_bars: int = 120           # 入场前连续已收盘K少于此数 -> 趋势"未知"
    trend_max_bars: int = 500           # 趋势判定最多使用最近多少根
    # 两个中枢给出了方向, 但最近收盘已经走到后枢的反方向一侧(上涨却在后枢下方 / 下跌却在后枢上方):
    # 结构正在被破坏, 记为无序而不是继续叫上涨/下跌。
    range_when_price_left_last_centre: bool = True
    fallback_to_1h_when_4h_range: bool = True   # 4h 无序/未知时由 1h 定方向
    # 触发
    trigger_lookback_bars: int = 6      # 停顿K必须落在入场前最近几根已收盘K内
    min_trigger_bars: int = 12          # 触发级别少于此数 -> 该级别不可用
    structure_max_bars: int = 300       # 触发/结构/盘整判定最多使用最近多少根
    # 15m 结构转换 / 盘整
    min_structure_bars: int = 30
    # 失衡位移
    imbalance_lookback_bars: int = 12   # 入场前最近几根内找位移K
    imbalance_base_bars: int = 20       # 与此前多少根的实体比较(必须比其中每一根都大)
    imbalance_body_mult: Decimal = Decimal("3")
    # 展示
    tz_offset_hours: int = 8            # detail 里的时间所用时区; 默认北京时间(与日报窗口一致)


@dataclass(frozen=True)
class SystemFit:
    episode_id: str
    trend_1h: str
    trend_4h: str
    alignment: str
    trigger: str
    setup: str
    evidence: tuple[Check, ...]


# ----------------------------------------------------------------- 适配与格式化

def to_engine_rows(bars: list[Kline]) -> list[dict]:
    """Kline(Decimal) -> 引擎使用的 dict 行(float)。"""
    return [
        {
            "open_time": int(bar.start_ms),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
        }
        for bar in bars
    ]


def closed_before(bars: list[Kline] | None, interval_ms: int, entry_ms: int,
                  cap: int | None = None) -> tuple[list[Kline], str]:
    """入场前已收盘、且一直连续到入场前一根的那段 K 线。

    返回 (K线, 原因)。原因: "" 可用 / "missing" 未提供 / "none" 入场前无已收盘K /
    "stale" 最近一根已收盘K与入场之间还缺K(数据没覆盖到入场时刻)。
    中间有缺口时只取缺口之后的连续段, 不跨缺口拼接。
    """
    if not bars:
        return [], "missing"
    unique = {bar.start_ms: bar for bar in bars if bar.start_ms + interval_ms <= entry_ms}
    ordered = [unique[key] for key in sorted(unique)]
    if not ordered:
        return [], "none"
    if entry_ms - (ordered[-1].start_ms + interval_ms) >= interval_ms:
        return [], "stale"
    first = len(ordered) - 1
    while first > 0 and ordered[first].start_ms - ordered[first - 1].start_ms == interval_ms:
        first -= 1
    tail = ordered[first:]
    if cap is not None:
        tail = tail[-cap:]
    return tail, ""


def _num(value) -> str:
    if isinstance(value, float):
        value = Decimal(repr(value))
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return format(value.quantize(Decimal("1")), "f")
        return format(value.normalize(), "f")
    return str(value)


def _dec(value: float) -> Decimal:
    return Decimal(repr(value))


def _ts(p: MatchParams, ms: int) -> str:
    zone = timezone(timedelta(hours=p.tz_offset_hours))
    return datetime.fromtimestamp(ms / 1000, tz=zone).strftime("%m-%d %H:%M")


_REASON_CN = {
    "missing": "未提供K线",
    "none": "入场前没有已收盘K线",
    "stale": "K线没有覆盖到入场前一根",
}


def _check(key: str, label: str, state: CheckState, detail: str,
           value: Decimal | str | None = None) -> Check:
    return Check(key=key, label=label, tier=_TIER, state=state, detail=detail, value=value)


def _first_entry(episode: TradeEpisode) -> tuple[int, Decimal] | None:
    """首笔入场成交的 (时间, 价格)。继承仓位或没有入场成交 -> None。只看 action == "entry"。"""
    if episode.inherited_position:
        return None
    entries = [fill.execution for fill in episode.fills if fill.action == "entry"]
    if not entries:
        return None
    first = min(entries, key=lambda row: row.timestamp_ms)
    return first.timestamp_ms, first.price


# ----------------------------------------------------------------- 1. 趋势

def _zs_text(p: MatchParams, zs: dict) -> str:
    return (f"[ZD {_num(zs['ZD'])}, ZG {_num(zs['ZG'])}]"
            f"({_ts(p, zs['start_time'])}~{_ts(p, zs['end_time'])}, {zs['bis']}笔)")


def _trend(tf: str, bars: list[Kline] | None, entry_ms: int, p: MatchParams) -> tuple[str, Check]:
    key, label = f"trend_{tf}", f"{tf} 趋势"
    tail, reason = closed_before(bars, INTERVAL_MS[tf], entry_ms, p.trend_max_bars)
    if reason:
        return "unknown", _check(key, label, CheckState.UNKNOWN,
                                 f"{tf} {_REASON_CN[reason]}", "unknown")
    span = f"{_ts(p, tail[0].start_ms)}~{_ts(p, tail[-1].start_ms)} 共{len(tail)}根已收盘K"
    if len(tail) < p.min_trend_bars:
        return "unknown", _check(
            key, label, CheckState.UNKNOWN,
            f"{span}, 少于{p.min_trend_bars}根, 不足以判定两个中枢的相对位置", "unknown")
    rows = to_engine_rows(tail)
    info = trend_by_zhongshu(rows, p.min_merged, p.zs_min_bis,
                             p.zs_min_width_pct, p.zs_filter_min_bis)
    ma_view = _TREND_CN[trend_state(rows)]
    last_close = f"最近收盘 {_num(tail[-1].close)}({_ts(p, tail[-1].start_ms)})"
    if info["n_zs"] == 0:
        return "unknown", _check(
            key, label, CheckState.UNKNOWN,
            f"{span}内未形成中枢, 无法按中枢判定; {last_close}; 均线口径(仅参考): {ma_view}",
            "unknown")
    last = info["last"]
    pos_cn = {"above": "上方", "inside": "内", "below": "下方"}[info["pos"]]
    if info["n_zs"] == 1:
        detail = (f"{span}内只有1个中枢 {_zs_text(p, last)} -> 无序; "
                  f"{last_close} 位于中枢{pos_cn}; 均线口径(仅参考): {ma_view}")
        return "range", _check(key, label, CheckState.NA, detail, "range")
    prev = info["prev"]
    state = info["state"]
    if state == "up":
        why = f"后枢下沿 {_num(last['ZD'])} > 前枢上沿 {_num(prev['ZG'])} -> 上涨"
    elif state == "down":
        why = f"后枢上沿 {_num(last['ZG'])} < 前枢下沿 {_num(prev['ZD'])} -> 下跌"
    else:
        why = "两个中枢区间有重叠 -> 无序"
    left = ""
    if (state, info["pos"]) in (("up", "below"), ("down", "above")):
        if p.range_when_price_left_last_centre:
            left = (f"; 但价格已走到后枢的反方向一侧, 中枢给出的{_TREND_CN[state]}正在被破坏 "
                    f"-> 记为无序")
            state = "range"
        else:
            left = "(价格已走到后枢的反方向一侧, 趋势判定仍按中枢位置)"
    detail = (f"{span}; 前枢 {_zs_text(p, prev)}, 后枢 {_zs_text(p, last)}; {why}; "
              f"{last_close} 位于后枢{pos_cn}{left}; 均线口径(仅参考): {ma_view}")
    return state, _check(key, label, CheckState.NA, detail, state)


# ----------------------------------------------------------------- 2. 顺逆势

def _alignment(direction: str, t1: str, t4: str, p: MatchParams) -> tuple[str, Check]:
    side_cn = {"long": "做多", "short": "做空"}.get(direction)
    both = f"4h {_TREND_CN[t4]}, 1h {_TREND_CN[t1]}"
    if side_cn is None:
        return "未知", _check("alignment", "顺逆势", CheckState.UNKNOWN,
                              f"方向未知; {both}", "未知")
    directional = ("up", "down")
    decider = None
    if t4 in directional:
        decider = "4h"
    elif t1 in directional and (p.fallback_to_1h_when_4h_range or t4 == "unknown"):
        decider = "1h"
    if decider is None:
        if "range" in (t4, t1):
            tf = "4h" if t4 == "range" else "1h"
            return "无序", _check("alignment", "顺逆势", CheckState.FAIL,
                                  f"{side_cn}; {both}; 由 {tf} 判定为无序", "无序")
        return "未知", _check("alignment", "顺逆势", CheckState.UNKNOWN,
                              f"{side_cn}; {both}; 两个级别都无法判定", "未知")
    trend = t4 if decider == "4h" else t1
    aligned = (direction == "long") == (trend == "up")
    result = "顺势" if aligned else "逆势"
    note = ""
    if decider == "4h" and t1 in directional and t1 != t4:
        note = "; 注意 1h 与 4h 方向相反"
    elif decider == "1h":
        note = f"; 4h {_TREND_CN[t4]}, 改由 1h 定方向"
    return result, _check(
        "alignment", "顺逆势", CheckState.PASS if aligned else CheckState.FAIL,
        f"{side_cn}; {both}; 由 {decider}({_TREND_CN[trend]})判定为{result}{note}", result)


# ----------------------------------------------------------------- 3. 小级别触发

@dataclass(frozen=True)
class _Trigger:
    tf: str
    label: str
    detail: str
    bars_before: int
    stall_close_ms: int


def _broken_after(tail: list[Kline], fx) -> int | None:
    """分型极值之后, 第一根击穿该极值的K下标(底分型=更低的低点, 顶分型=更高的高点)。"""
    ref = tail[fx.extreme_src_idx]
    for idx in range(fx.extreme_src_idx + 1, len(tail)):
        if fx.kind == "bottom" and tail[idx].low < ref.low:
            return idx
        if fx.kind == "top" and tail[idx].high > ref.high:
            return idx
    return None


def _fx_price(tail: list[Kline], fx) -> Decimal:
    bar = tail[fx.extreme_src_idx]
    return bar.low if fx.kind == "bottom" else bar.high


def _find_trigger(tf: str, tail: list[Kline], direction: str,
                  p: MatchParams) -> tuple[_Trigger | None, str]:
    """入场前 trigger_lookback_bars 根已收盘K内, 是否有同向的 分型+停顿(未被击穿)。"""
    rows = to_engine_rows(tail)
    merged = merge_klines(rows)
    want = "bottom" if direction == "long" else "top"
    kind_cn = _KIND_CN[want]
    last = len(tail) - 1
    window = (f"{tf} 入场前{p.trigger_lookback_bars}根已收盘K"
              f"({_ts(p, tail[max(0, last - p.trigger_lookback_bars + 1)].start_ms)}"
              f"~{_ts(p, tail[last].start_ms)})")
    near_miss = ""
    for fx in reversed(find_fractals(rows, merged)):
        if fx.kind != want:
            continue
        stall = stall_after(rows, merged, fx, p.stall_max_gap)
        if stall is None:
            continue
        bars_before = last - stall + 1
        price = _fx_price(tail, fx)
        where = f"{kind_cn} {_num(price)}({_ts(p, tail[fx.extreme_src_idx].start_ms)})"
        if bars_before > p.trigger_lookback_bars:
            if not near_miss:
                near_miss = f"; 最近一次{where}+停顿在入场前第{bars_before}根, 超出范围"
            continue
        broken = _broken_after(tail, fx)
        if broken is not None:
            if not near_miss:
                level = tail[broken].low if want == "bottom" else tail[broken].high
                near_miss = (f"; {where}+停顿出现过, 但已在 {_ts(p, tail[broken].start_ms)} "
                             f"被击穿({_num(level)})")
            continue
        right = merged[fx.mid_merged_idx + 1]
        ref = right.high if want == "bottom" else right.low
        cmp_cn = "> 右K最高" if want == "bottom" else "< 右K最低"
        detail = (f"{tf} {where}; 停顿K {_ts(p, tail[stall].start_ms)} 收盘 "
                  f"{_num(tail[stall].close)} {cmp_cn} {_num(ref)}; "
                  f"停顿K是入场前第{bars_before}根已收盘K")
        return _Trigger(tf, f"{tf} {kind_cn}+停顿, 入场前{bars_before}根", detail,
                        bars_before, tail[stall].start_ms + INTERVAL_MS[tf]), ""
    return None, f"{window}内没有未被击穿的{kind_cn}+停顿{near_miss}"


def _trigger(klines: dict[str, list[Kline]], direction: str, entry_ms: int,
             p: MatchParams) -> tuple[str, Check, _Trigger | None, dict[str, list[Kline]]]:
    """返回 (标签, 证据, 找到的触发, 各可用级别的入场前K线)。5m 优先, 5m 不可用或没有再看 15m。"""
    usable: dict[str, list[Kline]] = {}
    notes: list[str] = []
    for tf in ("5m", "15m"):
        tail, reason = closed_before(klines.get(tf), INTERVAL_MS[tf], entry_ms,
                                     p.structure_max_bars)
        if reason:
            notes.append(f"{tf} {_REASON_CN[reason]}")
        elif len(tail) < p.min_trigger_bars:
            notes.append(f"{tf} 入场前连续已收盘K仅{len(tail)}根, 少于{p.min_trigger_bars}根")
        else:
            usable[tf] = tail
    if direction not in ("long", "short"):
        return TRIGGER_UNKNOWN, _check("trigger", "小级别触发", CheckState.UNKNOWN,
                                       "方向未知", TRIGGER_UNKNOWN), None, usable
    if not usable:
        return TRIGGER_UNKNOWN, _check("trigger", "小级别触发", CheckState.UNKNOWN,
                                       "; ".join(notes), TRIGGER_UNKNOWN), None, usable
    for tf in ("5m", "15m"):
        if tf not in usable:
            continue
        found, miss = _find_trigger(tf, usable[tf], direction, p)
        if found is not None:
            detail = "; ".join(notes + [found.detail])
            return found.label, _check("trigger", "小级别触发", CheckState.PASS, detail,
                                       found.label), found, usable
        notes.append(miss)
    return TRIGGER_NONE, _check("trigger", "小级别触发", CheckState.FAIL, "; ".join(notes),
                                TRIGGER_NONE), None, usable


# ----------------------------------------------------------------- 4. 位置证据

def _pullback(tf: str | None, usable: dict[str, list[Kline]], direction: str,
              p: MatchParams) -> Check:
    """回调/反弹是否成笔: 该级别最近一笔是反向笔, 且其末端分型尚未被击穿。"""
    label = "回调成笔" if direction == "long" else "反弹成笔"
    tf = tf or ("5m" if "5m" in usable else "15m" if "15m" in usable else None)
    if tf is None or direction not in ("long", "short"):
        return _check("pullback", label, CheckState.UNKNOWN, "5m/15m 入场前K线不可用")
    tail = usable[tf]
    _, seq = build_bi(to_engine_rows(tail), p.min_merged)
    want = "bottom" if direction == "long" else "top"
    leg_cn = "下跌笔" if direction == "long" else "上涨笔"
    if len(seq) < 2:
        return _check("pullback", label, CheckState.FAIL,
                      f"{tf} {_ts(p, tail[0].start_ms)}~{_ts(p, tail[-1].start_ms)} 内尚未形成完整一笔")
    start, end = seq[-2], seq[-1]
    legs = (f"{_KIND_CN[start.kind]} {_num(_fx_price(tail, start))}"
            f"({_ts(p, tail[start.extreme_src_idx].start_ms)}) -> "
            f"{_KIND_CN[end.kind]} {_num(_fx_price(tail, end))}"
            f"({_ts(p, tail[end.extreme_src_idx].start_ms)})")
    if end.kind != want:
        return _check("pullback", label, CheckState.FAIL,
                      f"{tf} 最近一笔 {legs} 与入场同向, 入场前的{leg_cn}尚未成笔")
    broken = _broken_after(tail, end)
    if broken is not None:
        return _check("pullback", label, CheckState.FAIL,
                      f"{tf} {leg_cn} {legs} 的末端已在 {_ts(p, tail[broken].start_ms)} 被击穿, "
                      f"{leg_cn}仍在延续")
    return _check("pullback", label, CheckState.PASS,
                  f"{tf} {leg_cn} {legs} 已成笔, 末端到入场前未被击穿",
                  _fx_price(tail, end))


def _structure_shift(klines: dict[str, list[Kline]], direction: str, entry_ms: int,
                     entry_price: Decimal, p: MatchParams) -> tuple[Check, Check]:
    """15m 趋势转换: 做空=高点不创新高 + 收盘跌破前低(做多镜像), 且之后未被收盘收回次高/次低。
    第二个返回值: 入场价是否回抽到位——做空要在破位价(底1)与次高(顶2)之间(做多镜像):
    低于底1 是追着破位入场; 高于顶2 说明入场时(含尚未收盘的那根K)结构已经被破坏。"""
    label, retest_label = "15m 结构转换", "回抽到位"
    tail, reason = closed_before(klines.get("15m"), INTERVAL_MS["15m"], entry_ms,
                                 p.structure_max_bars)
    na = _check("shift_retest", retest_label, CheckState.NA, "没有可用的 15m 结构转换")
    if direction not in ("long", "short"):
        return _check("structure_shift", label, CheckState.UNKNOWN, "方向未知"), na
    if reason:
        return _check("structure_shift", label, CheckState.UNKNOWN,
                      f"15m {_REASON_CN[reason]}"), na
    if len(tail) < p.min_structure_bars:
        return _check("structure_shift", label, CheckState.UNKNOWN,
                      f"15m 入场前连续已收盘K仅{len(tail)}根, 少于{p.min_structure_bars}根"), na
    _, seq = build_bi(to_engine_rows(tail), p.min_merged)
    first_kind = "top" if direction == "short" else "bottom"
    notes: list[str] = []
    for end in (len(seq) - 1, len(seq) - 2, len(seq) - 3):
        if end < 2 or seq[end].kind != first_kind:
            continue
        a, b, d = seq[end - 2], seq[end - 1], seq[end]
        pa, pb, pd = _fx_price(tail, a), _fx_price(tail, b), _fx_price(tail, d)
        if direction == "short":
            shape = (f"顶1 {_num(pa)}({_ts(p, tail[a.extreme_src_idx].start_ms)}), "
                     f"底1 {_num(pb)}({_ts(p, tail[b.extreme_src_idx].start_ms)}), "
                     f"顶2 {_num(pd)}({_ts(p, tail[d.extreme_src_idx].start_ms)})")
            if not pd < pa:
                notes.append(f"{shape}: 顶2 未低于顶1")
                continue
            brk = next((i for i in range(d.extreme_src_idx + 1, len(tail))
                        if tail[i].close < pb), None)
            if brk is None:
                notes.append(f"{shape}: 顶2 之后没有收盘跌破底1")
                continue
            undo = next((i for i in range(brk + 1, len(tail)) if tail[i].close > pd), None)
            if undo is not None:
                notes.append(f"{shape}: 跌破底1后又在 {_ts(p, tail[undo].start_ms)} "
                             f"收盘 {_num(tail[undo].close)} 站回顶2 上方, 结构已被化解")
                continue
            detail = (f"{shape}; 顶2 低于顶1, {_ts(p, tail[brk].start_ms)} 收盘 "
                      f"{_num(tail[brk].close)} 跌破底1, 之后没有收盘站回顶2")
            if entry_price > pd:
                retest = _check("shift_retest", retest_label, CheckState.FAIL,
                                f"入场价 {_num(entry_price)} 已高于顶2 {_num(pd)}, "
                                f"结构在入场时已被破坏", entry_price)
            elif entry_price < pb:
                retest = _check("shift_retest", retest_label, CheckState.FAIL,
                                f"入场价 {_num(entry_price)} 低于破位价(底1) {_num(pb)}, "
                                f"属于追着破位入场", entry_price)
            else:
                retest = _check("shift_retest", retest_label, CheckState.PASS,
                                f"入场价 {_num(entry_price)} 在破位价(底1) {_num(pb)} 与顶2 "
                                f"{_num(pd)} 之间", entry_price)
        else:
            shape = (f"底1 {_num(pa)}({_ts(p, tail[a.extreme_src_idx].start_ms)}), "
                     f"顶1 {_num(pb)}({_ts(p, tail[b.extreme_src_idx].start_ms)}), "
                     f"底2 {_num(pd)}({_ts(p, tail[d.extreme_src_idx].start_ms)})")
            if not pd > pa:
                notes.append(f"{shape}: 底2 未高于底1")
                continue
            brk = next((i for i in range(d.extreme_src_idx + 1, len(tail))
                        if tail[i].close > pb), None)
            if brk is None:
                notes.append(f"{shape}: 底2 之后没有收盘升破顶1")
                continue
            undo = next((i for i in range(brk + 1, len(tail)) if tail[i].close < pd), None)
            if undo is not None:
                notes.append(f"{shape}: 升破顶1后又在 {_ts(p, tail[undo].start_ms)} "
                             f"收盘 {_num(tail[undo].close)} 跌回底2 下方, 结构已被化解")
                continue
            detail = (f"{shape}; 底2 高于底1, {_ts(p, tail[brk].start_ms)} 收盘 "
                      f"{_num(tail[brk].close)} 升破顶1, 之后没有收盘跌回底2")
            if entry_price < pd:
                retest = _check("shift_retest", retest_label, CheckState.FAIL,
                                f"入场价 {_num(entry_price)} 已低于底2 {_num(pd)}, "
                                f"结构在入场时已被破坏", entry_price)
            elif entry_price > pb:
                retest = _check("shift_retest", retest_label, CheckState.FAIL,
                                f"入场价 {_num(entry_price)} 高于破位价(顶1) {_num(pb)}, "
                                f"属于追着突破入场", entry_price)
            else:
                retest = _check("shift_retest", retest_label, CheckState.PASS,
                                f"入场价 {_num(entry_price)} 在底2 {_num(pd)} 与破位价(顶1) "
                                f"{_num(pb)} 之间", entry_price)
        return _check("structure_shift", label, CheckState.PASS, detail), retest
    if not notes:
        notes.append(f"15m {_ts(p, tail[0].start_ms)}~{_ts(p, tail[-1].start_ms)} "
                     f"笔端点仅{len(seq)}个, 没有可比较的 高-低-高 / 低-高-低")
    return _check("structure_shift", label, CheckState.FAIL, "; ".join(notes)), na


def _imbalance(usable: dict[str, list[Kline]], direction: str,
               p: MatchParams) -> tuple[Check, int | None]:
    """失衡位移: 入场前若干根内, 是否有一根K的实体比此前 N 根都大, 且 >= 其中位数的 M 倍。
    PASS 只表示位移方向与入场方向相反(入场是在接这段位移的反向)。
    第二个返回值: 最早一根反向位移K的收盘时刻(用于确认"先位移, 后触发")。"""
    label = "失衡位移"
    tf = "5m" if "5m" in usable else "15m" if "15m" in usable else None
    if tf is None or direction not in ("long", "short"):
        return _check("imbalance", label, CheckState.UNKNOWN, "5m/15m 入场前K线不可用"), None
    tail = usable[tf]
    against_up = direction == "short"      # 做空时, 逆向位移 = 向上的大实体
    # 每个方向各记两样: 达标的最大一根(位移K), 以及不论是否达标的最大一根(用于说明差多少)
    hit: dict[bool, tuple[Decimal, int] | None] = {True: None, False: None}
    top: dict[bool, tuple[Decimal, int] | None] = {True: None, False: None}
    evaluated = 0
    first_against_close: int | None = None
    for idx in range(max(0, len(tail) - p.imbalance_lookback_bars), len(tail)):
        if idx < p.imbalance_base_bars:
            continue
        bodies = [abs(bar.close - bar.open) for bar in tail[idx - p.imbalance_base_bars: idx]]
        base = Decimal(median(bodies))
        if base <= 0:
            continue
        evaluated += 1
        bar = tail[idx]
        body = abs(bar.close - bar.open)
        if body == 0:
            continue
        item = (body / base, idx)
        against = (bar.close > bar.open) == against_up
        if top[against] is None or item > top[against]:
            top[against] = item
        if item[0] >= p.imbalance_body_mult and body > max(bodies):
            if hit[against] is None or item > hit[against]:
                hit[against] = item
            if against and first_against_close is None:
                first_against_close = bar.start_ms + INTERVAL_MS[tf]
    base_cn = f"此前{p.imbalance_base_bars}根"
    if not evaluated:
        return _check("imbalance", label, CheckState.UNKNOWN,
                      f"{tf} 入场前连续已收盘K仅{len(tail)}根, 无法与{base_cn}实体比较"), None

    def text(item) -> str:
        ratio, idx = item
        bar = tail[idx]
        return (f"{_ts(p, bar.start_ms)} 开 {_num(bar.open)} 收 {_num(bar.close)}, "
                f"实体为{base_cn}中位数的 {ratio:.1f} 倍")

    rule = f"实体大于{base_cn}每一根且达到中位数 {_num(p.imbalance_body_mult)} 倍"
    side_against = "向上" if against_up else "向下"
    side_with = "向下" if against_up else "向上"
    with_note = (f"; 另有与入场同向({side_with})的位移K {text(hit[False])}"
                 if hit[False] else "")
    if hit[True]:
        return _check("imbalance", label, CheckState.PASS,
                      f"{tf} 入场前{p.imbalance_lookback_bars}根内有与入场反向({side_against})的"
                      f"位移K({rule}): {text(hit[True])}{with_note}",
                      hit[True][0].quantize(Decimal("0.01"))), first_against_close
    biggest = f"; 反向最大一根: {text(top[True])}" if top[True] else ""
    return _check("imbalance", label, CheckState.FAIL,
                  f"{tf} 入场前{p.imbalance_lookback_bars}根内没有与入场反向({side_against})的"
                  f"位移K({rule}){biggest}{with_note}"), None


def _consolidation(klines: dict[str, list[Kline]], entry_ms: int, entry_price: Decimal,
                   p: MatchParams) -> Check:
    """盘整区内: 15m(缺则 1h)最近一个中枢仍在延伸(没有一笔离开过), 且入场前收盘在 [ZD, ZG] 内。"""
    label = "盘整区内"
    notes: list[str] = []
    for tf in ("15m", "1h"):
        tail, reason = closed_before(klines.get(tf), INTERVAL_MS[tf], entry_ms,
                                     p.structure_max_bars)
        if reason:
            notes.append(f"{tf} {_REASON_CN[reason]}")
            continue
        if len(tail) < p.min_structure_bars:
            notes.append(f"{tf} 入场前连续已收盘K仅{len(tail)}根, 少于{p.min_structure_bars}根")
            continue
        _, seq = build_bi(to_engine_rows(tail), p.min_merged)
        centres = build_zhongshu(seq, p.zs_min_bis)
        span = f"{tf} {_ts(p, tail[0].start_ms)}~{_ts(p, tail[-1].start_ms)}"
        if not centres:
            return _check("consolidation", label, CheckState.FAIL, f"{span} 内没有中枢")
        last = centres[-1]
        zd, zg = _dec(last["ZD"]), _dec(last["ZG"])
        close = tail[-1].close
        ongoing = last["start_fx"] + last["bis"] >= len(seq) - 1
        inside = zd <= close <= zg
        where = (f"{span} 最近中枢 {_zs_text(p, last)}; 入场前收盘 {_num(close)}"
                 f"({_ts(p, tail[-1].start_ms)})")
        if ongoing and inside:
            pct = (entry_price - zd) / (zg - zd) * 100
            return _check("consolidation", label, CheckState.PASS,
                          f"{where} 在中枢内, 且中枢仍在延伸; 入场价 {_num(entry_price)} "
                          f"位于中枢自下而上 {pct:.0f}% 处", f"{pct:.0f}%")
        if not ongoing:
            return _check("consolidation", label, CheckState.FAIL,
                          f"{where}; 已有一笔离开该中枢, 不算仍在盘整")
        side = "上方" if close > zg else "下方"
        return _check("consolidation", label, CheckState.FAIL, f"{where} 在中枢{side}")
    return _check("consolidation", label, CheckState.UNKNOWN, "; ".join(notes))


# ----------------------------------------------------------------- 5. 归类

def _setup(alignment: str, checks: dict[str, Check],
           displacement_first: bool) -> tuple[str, Check]:
    state = {key: item.state for key, item in checks.items()}
    passed = CheckState.PASS
    if alignment == "顺势" and state["trigger"] is passed and state["pullback"] is passed:
        name = SETUP_PULLBACK
        why = ("大级别顺势 + 反向笔已完成 + 小级别触发; 回调有没有到关键位(POC / FVG / 中枢边界 / "
               "前高前低)没有核对, 不等于核心模型(多级别趋势跟随)成立")
    elif state["structure_shift"] is passed and state["shift_retest"] is passed:
        name = "趋势转换反转"
        why = "15m 出现同向结构转换, 入场价在破位价与次高/次低之间"
    elif state["imbalance"] is passed and state["trigger"] is passed and displacement_first:
        name = "失衡反转(辅助)"
        why = "入场前有反向位移K, 且之后出现同向分型+停顿(对应辅助模型: 价格失衡反转)"
    elif (state["consolidation"] is passed and state["trigger"] is CheckState.FAIL
          and state["imbalance"] is CheckState.FAIL):
        name = SETUP_FORBIDDEN
        why = "入场前价格在仍在延伸的中枢内, 没有位移K, 也没有小级别触发(对应禁入场景: 盘整区预判)"
    else:
        unknown = [checks[key].label for key in
                   ("alignment", "trigger", "pullback", "structure_shift", "imbalance",
                    "consolidation") if state[key] is CheckState.UNKNOWN]
        if unknown:
            name = "未知"
            why = "以下证据缺数据, 无法排除任何一种模型: " + "、".join(unknown)
        else:
            name = "系统外"
            missing = []
            if alignment != "顺势":
                missing.append(f"大级别{alignment}")
            if state["pullback"] is not passed:
                missing.append("反向笔未完成")
            if state["trigger"] is not passed:
                missing.append("无小级别触发")
            if state["structure_shift"] is not passed:
                missing.append("无 15m 结构转换")
            elif state["shift_retest"] is not passed:
                missing.append("有 15m 结构转换但入场未回抽到位")
            if state["imbalance"] is not passed:
                missing.append("无反向位移K")
            elif state["trigger"] is passed and not displacement_first:
                missing.append("反向位移K出现在触发之后")
            if state["consolidation"] is not passed:
                missing.append("不在延伸中的中枢内")
            why = "三种模型与禁入场景都不匹配: " + "、".join(missing)
    matched = name not in ("系统外", "未知", SETUP_FORBIDDEN)     # 禁入场景不是"符合"
    setup_state = (CheckState.PASS if matched else
                   CheckState.UNKNOWN if name == "未知" else CheckState.FAIL)
    return name, _check("setup", "形态归类", setup_state, why, name)


def match_episode(episode: TradeEpisode, klines: dict[str, list[Kline]],
                  params: MatchParams | None = None) -> SystemFit:
    """描述一笔真实交易相对交易系统的位置。只描述, 不评判; 不读取任何结果数据。"""
    p = params or MatchParams()
    klines = klines or {}
    entry = _first_entry(episode)
    if entry is None:
        reason = ("继承仓位: 首次建仓不在复盘窗口内, 入场时刻未知"
                  if episode.inherited_position else "没有入场成交, 入场时刻未知")
        check = _check("setup", "形态归类", CheckState.UNKNOWN, reason, "未知")
        return SystemFit(episode.episode_id, "unknown", "unknown", "未知", TRIGGER_UNKNOWN,
                         "未知", (check,))
    entry_ms, entry_price = entry
    direction = episode.direction

    trend_4h, check_4h = _trend("4h", klines.get("4h"), entry_ms, p)
    trend_1h, check_1h = _trend("1h", klines.get("1h"), entry_ms, p)
    alignment, check_alignment = _alignment(direction, trend_1h, trend_4h, p)
    trigger, check_trigger, found, usable = _trigger(klines, direction, entry_ms, p)
    check_imbalance, displacement_close = _imbalance(usable, direction, p)
    check_shift, check_retest = _structure_shift(klines, direction, entry_ms, entry_price, p)
    checks = {
        "trend_4h": check_4h,
        "trend_1h": check_1h,
        "alignment": check_alignment,
        "trigger": check_trigger,
        "pullback": _pullback(found.tf if found else None, usable, direction, p),
        "structure_shift": check_shift,
        "shift_retest": check_retest,
        "imbalance": check_imbalance,
        "consolidation": _consolidation(klines, entry_ms, entry_price, p),
    }
    displacement_first = (found is not None and displacement_close is not None
                          and displacement_close < found.stall_close_ms)
    setup, check_setup = _setup(alignment, checks, displacement_first)
    entry_check = _check(
        "entry", "入场时刻", CheckState.NA,
        f"首笔入场 {_ts(p, entry_ms)}(UTC{p.tz_offset_hours:+d}) 价 {_num(entry_price)}; "
        f"以下证据只使用此前已收盘的K线, 时间同为 UTC{p.tz_offset_hours:+d}",
        entry_price)
    evidence = (entry_check, *checks.values(), check_setup)
    return SystemFit(episode.episode_id, trend_1h, trend_4h, alignment, trigger, setup, evidence)
