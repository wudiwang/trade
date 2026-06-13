"""策略V4：放量破前低 → 底分型(倒三角)收回破位K顶部 → 买入。

形态(做多，做空镜像)：
  破位     放量长阴破前低(创近N根新低)
  一买     破位后第一个底分型(中间K最低)，且收盘收回到破位K顶部(开盘价) → 买
  主力K    一买之后单根"量更大+涨幅更大"的K(可选标注，有就标没有不影响)
  二买     之后再出一个底分型，且是更高的低点 → 买(底分型一成形即提醒)
入场=破位K顶部(开盘价)；止损=底分型最低点下方；止盈=下跌前的顶/密集成交区。
"""


def vol_avg(klines: list, i: int, n: int) -> float:
    s = max(0, i - n)
    w = [float(klines[j]["volume"]) for j in range(s, i)]
    return sum(w) / len(w) if w else 0.0


def detect_breakdown(klines: list, i: int, *, vol_mult: float = 4.0,
                     newlow_lookback: int = 20, body_min: float = 0.5,
                     avg_period: int = 20):
    """放量破前低(做多布局)/放量破前高(做空布局)。返回 ('long'/'short'/None, detail)。"""
    if i < max(newlow_lookback, avg_period) + 1:
        return None, {}
    k = klines[i]
    o, h, l, c, v = (float(k["open"]), float(k["high"]), float(k["low"]),
                     float(k["close"]), float(k["volume"]))
    rng = h - l
    if rng <= 0 or v <= 0:
        return None, {}
    avg = vol_avg(klines, i, avg_period)
    if avg <= 0 or v < vol_mult * avg:
        return None, {}
    if abs(c - o) / rng < body_min:
        return None, {}
    detail = {"vol_ratio": round(v / avg, 2)}
    if c < o:  # 阴线 → 做多布局
        prior_low = min(float(klines[j]["low"]) for j in range(i - newlow_lookback, i))
        if l <= prior_low:
            return "long", detail
    else:      # 阳线 → 做空布局
        prior_high = max(float(klines[j]["high"]) for j in range(i - newlow_lookback, i))
        if h >= prior_high:
            return "short", detail
    return None, {}


def is_bottom_fractal(klines: list, i: int) -> bool:
    """底分型(倒三角)：中间K(i-1)的高低点都低于左右两侧。i 为确认K。"""
    if i < 2:
        return False
    a, b, c = klines[i - 2], klines[i - 1], klines[i]
    return (float(b["low"]) < float(a["low"]) and float(b["low"]) < float(c["low"])
            and float(b["high"]) < float(a["high"]) and float(b["high"]) < float(c["high"]))


def is_top_fractal(klines: list, i: int) -> bool:
    if i < 2:
        return False
    a, b, c = klines[i - 2], klines[i - 1], klines[i]
    return (float(b["high"]) > float(a["high"]) and float(b["high"]) > float(c["high"])
            and float(b["low"]) > float(a["low"]) and float(b["low"]) > float(c["low"]))


def is_main_k(klines: list, i: int, ref_vol: float, atr_val: float,
              range_atr_min: float = 1.2) -> bool:
    """主力K(可选标注)：量超 ref_vol 且 振幅 >= range_atr_min×ATR 的单根。"""
    k = klines[i]
    v = float(k["volume"])
    rng = float(k["high"]) - float(k["low"])
    if v <= ref_vol:
        return False
    if atr_val > 0 and rng < range_atr_min * atr_val:
        return False
    return True


def liquidity_pool(klines: list, i: int, direction: str,
                   lookback: int = 50, tol_pct: float = 0.3, min_touches: int = 2):
    """检测破位K(i)是否扫掉一个"等低点/等高点"流动性池(多个低点/高点聚在同一位)。
    返回 (是否扫池, 池价位, 触及次数)。触及越多池越厚，扫破+收回质量越高。"""
    start = max(0, i - lookback)
    if start >= i:
        return False, None, 0
    if direction == "long":
        lows = [float(klines[j]["low"]) for j in range(start, i)]
        pool = min(lows)
        touches = sum(1 for lo in lows if lo <= pool * (1 + tol_pct / 100))
        swept = float(klines[i]["low"]) < pool
        return (swept and touches >= min_touches), pool, touches
    highs = [float(klines[j]["high"]) for j in range(start, i)]
    pool = max(highs)
    touches = sum(1 for hi in highs if hi >= pool * (1 - tol_pct / 100))
    swept = float(klines[i]["high"]) > pool
    return (swept and touches >= min_touches), pool, touches


def prior_peak(klines: list, i: int, lookback: int, direction: str):
    """下跌前的顶(做多)/底(做空)：最近 lookback 根的极值。"""
    seg = klines[max(0, i - lookback): i + 1]
    if not seg:
        return None
    if direction == "long":
        return max(float(k["high"]) for k in seg)
    return min(float(k["low"]) for k in seg)
