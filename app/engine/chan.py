"""缠论核心：K线包含关系处理 + 顶/底分型识别。

输入为原始K线（dict 或 sqlite Row：open_time/open/high/low/close/volume），
输出分型时会把合并K映射回原始K，便于做量能检测。
"""
from dataclasses import dataclass, field


@dataclass
class MergedK:
    high: float
    low: float
    open_time: int          # 取首根原始K的 open_time
    src_idx: list[int] = field(default_factory=list)  # 覆盖的原始K下标


@dataclass
class Fractal:
    kind: str               # 'bottom' / 'top'
    mid_merged_idx: int     # 分型中间合并K的下标
    extreme_price: float    # 底分型=最低价 / 顶分型=最高价
    extreme_src_idx: int    # 极值所在的原始K下标（量能检测用）
    confirm_src_idx: int    # 分型确认K（右侧合并K的最后一根原始K）下标
    open_time: int          # 中间合并K的 open_time


def merge_klines(klines: list) -> list[MergedK]:
    """包含关系处理。
    上升处理（前高<后高方向）：高取高、低取高；下降：高取低、低取低。
    方向由进入包含前最后两根不包含的合并K决定，默认向上。
    """
    merged: list[MergedK] = []
    direction = 1  # 1 向上, -1 向下
    for i, k in enumerate(klines):
        h, l = float(k["high"]), float(k["low"])
        if not merged:
            merged.append(MergedK(h, l, int(k["open_time"]), [i]))
            continue
        last = merged[-1]
        contains = (last.high >= h and last.low <= l) or (h >= last.high and l <= last.low)
        if contains:
            if direction == 1:
                last.high = max(last.high, h)
                last.low = max(last.low, l)
            else:
                last.high = min(last.high, h)
                last.low = min(last.low, l)
            last.src_idx.append(i)
        else:
            if h > last.high and l > last.low:
                direction = 1
            elif h < last.high and l < last.low:
                direction = -1
            merged.append(MergedK(h, l, int(k["open_time"]), [i]))
    return merged


def find_fractals(klines: list, merged: list[MergedK] | None = None) -> list[Fractal]:
    """在合并K序列上找分型。
    底分型：中间合并K的低点与高点都低于左右两侧；顶分型对称。
    extreme_src_idx 取中间合并K覆盖的原始K中极值那根。
    """
    if merged is None:
        merged = merge_klines(klines)
    out: list[Fractal] = []
    for i in range(1, len(merged) - 1):
        a, b, c = merged[i - 1], merged[i], merged[i + 1]
        if b.low < a.low and b.low < c.low and b.high < a.high and b.high < c.high:
            kind = "bottom"
        elif b.high > a.high and b.high > c.high and b.low > a.low and b.low > c.low:
            kind = "top"
        else:
            continue
        if kind == "bottom":
            ext_idx = min(b.src_idx, key=lambda j: float(klines[j]["low"]))
            price = float(klines[ext_idx]["low"])
        else:
            ext_idx = max(b.src_idx, key=lambda j: float(klines[j]["high"]))
            price = float(klines[ext_idx]["high"])
        out.append(Fractal(
            kind=kind,
            mid_merged_idx=i,
            extreme_price=price,
            extreme_src_idx=ext_idx,
            confirm_src_idx=c.src_idx[-1],
            open_time=b.open_time,
        ))
    return out


def volume_ratio(klines: list, idx: int, ma_period: int = 20) -> float:
    """idx 那根K的成交量 / 它之前 ma_period 根的均量。数据不足返回 0。"""
    if idx < ma_period:
        return 0.0
    window = [float(klines[j]["volume"]) for j in range(idx - ma_period, idx)]
    avg = sum(window) / ma_period
    if avg <= 0:
        return 0.0
    return float(klines[idx]["volume"]) / avg


def prior_support(klines: list, fractals: list[Fractal], cur: Fractal,
                  lookback: int) -> float | None:
    """跌破收回的'前低'：当前底分型之前 lookback 根内最近一个底分型的低点；
    没有就用窗口内（分型前）的最低低点。顶分型对称取前高。"""
    start = max(0, cur.extreme_src_idx - lookback)
    if cur.kind == "bottom":
        prev = [f for f in fractals if f.kind == "bottom"
                and f.extreme_src_idx < cur.extreme_src_idx - 2
                and f.extreme_src_idx >= start]
        if prev:
            return prev[-1].extreme_price
        lows = [float(klines[j]["low"]) for j in range(start, max(start + 1, cur.extreme_src_idx - 2))]
        return min(lows) if lows else None
    else:
        prev = [f for f in fractals if f.kind == "top"
                and f.extreme_src_idx < cur.extreme_src_idx - 2
                and f.extreme_src_idx >= start]
        if prev:
            return prev[-1].extreme_price
        highs = [float(klines[j]["high"]) for j in range(start, max(start + 1, cur.extreme_src_idx - 2))]
        return max(highs) if highs else None


def is_break_reclaim(klines: list, cur: Fractal, support: float) -> bool:
    """底分型：极值K跌破前低，确认K收盘收回前低之上。顶分型对称（冲高回落）。"""
    confirm_close = float(klines[cur.confirm_src_idx]["close"])
    if cur.kind == "bottom":
        return cur.extreme_price < support and confirm_close > support
    return cur.extreme_price > support and confirm_close < support


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def aggregate(klines: list, group: int) -> list[dict]:
    """把小级别K线按 group 根聚合成大级别（如 15m×4 → 1h）。尾部不足一组的丢弃。"""
    out = []
    n = (len(klines) // group) * group
    for i in range(0, n, group):
        chunk = klines[i:i + group]
        out.append({
            "open_time": int(chunk[0]["open_time"]),
            "open": float(chunk[0]["open"]),
            "high": max(float(c["high"]) for c in chunk),
            "low": min(float(c["low"]) for c in chunk),
            "close": float(chunk[-1]["close"]),
            "volume": sum(float(c["volume"]) for c in chunk),
        })
    return out


def trend_direction(klines_15m: list, ema_period: int = 50) -> int:
    """1h EMA50 趋势：1=向上, -1=向下, 0=数据不足。用 15m 聚合出 1h。"""
    h1 = aggregate(klines_15m, 4)
    if len(h1) < ema_period + 2:
        return 0
    closes = [k["close"] for k in h1]
    e = ema(closes, ema_period)
    if closes[-1] > e[-1] and e[-1] >= e[-2]:
        return 1
    if closes[-1] < e[-1] and e[-1] <= e[-2]:
        return -1
    return 0
