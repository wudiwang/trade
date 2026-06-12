"""策略V3（弹簧策略）：量价四要素——BTC大盘 / 价格位置 / 成交量 / 标志性K线。

做多链路（做空镜像）：
  阶段1 量的稳态: 触发前15根无放量（覆盖平台/上升旗/下降旗，不识别价格形态）
  阶段2 触发巨量K: 放量长阴破位创新低 → 成为坐标K，开始观察
  阶段3 收回打分: 3根内收盘回到坐标K中点=50分(观察), 吞没实体顶=100分(一买)
  阶段4 坐标升级: 出现量更大的K → 坐标转移（长上影标注）
  阶段5 二买/弹簧: 缩量回测不实质跌破坐标K低点→二买；假破1~2根收回→💎二次弹簧(最强)
"""


def vol_avg(klines: list, i: int, n: int) -> float:
    s = max(0, i - n)
    w = [float(klines[j]["volume"]) for j in range(s, i)]
    return sum(w) / len(w) if w else 0.0


def is_quiet(klines: list, i: int, bars: int = 15, mult: float = 1.5) -> bool:
    """触发K之前的稳态：前 bars 根里没有一根量超过 mult x 这段均量。"""
    if i < bars + 1:
        return False
    avg = vol_avg(klines, i, bars)
    if avg <= 0:
        return False
    return all(float(klines[j]["volume"]) < mult * avg for j in range(i - bars, i))


def detect_trigger(klines: list, i: int, *, atr_val: float,
                   vol_mult: float = 3.0, vol_max_lookback: int = 30,
                   body_min: float = 0.5, range_atr_min: float = 1.5,
                   newlow_lookback: int = 50,
                   quiet_bars: int = 15, quiet_mult: float = 1.5):
    """识别触发巨量K。返回 (方向'long'/'short'/None, 明细)。
    long = 放量长阴破位新低（做多布局的起点）；short 镜像。"""
    if i < max(vol_max_lookback, newlow_lookback, quiet_bars) + 1:
        return None, {}
    k = klines[i]
    o, h, l, c, v = (float(k["open"]), float(k["high"]), float(k["low"]),
                     float(k["close"]), float(k["volume"]))
    rng = h - l
    if rng <= 0 or v <= 0:
        return None, {}
    avg = vol_avg(klines, i, quiet_bars)
    if avg <= 0 or v < vol_mult * avg:
        return None, {}
    if v <= max(float(klines[j]["volume"]) for j in range(i - vol_max_lookback, i)):
        return None, {}
    if abs(c - o) / rng < body_min:
        return None, {}
    if atr_val > 0 and rng < range_atr_min * atr_val:
        return None, {}
    if not is_quiet(klines, i, quiet_bars, quiet_mult):
        return None, {}

    detail = {"vol_ratio": round(v / avg, 2), "range_atr": round(rng / atr_val, 2) if atr_val else None,
              "body_pct": round(abs(c - o) / rng, 2)}
    if c < o:  # 长阴
        prior_low = min(float(klines[j]["low"]) for j in range(i - newlow_lookback, i))
        if l <= prior_low:
            return "long", detail
    else:      # 长阳
        prior_high = max(float(klines[j]["high"]) for j in range(i - newlow_lookback, i))
        if h >= prior_high:
            return "short", detail
    return None, {}


def recovery_score(direction: str, trig: dict, close: float) -> float:
    """收回打分：中点=50，吞没实体顶(阴线开盘价)=100，线性，封顶100。
    trig: {high, low, open}。低于中点返回0。"""
    h, l, o = trig["high"], trig["low"], trig["open"]
    m = (h + l) / 2
    if direction == "long":
        if close < m or o <= m:
            return 0.0
        return min(100.0, 50 + 50 * (close - m) / (o - m))
    else:
        if close > m or o >= m:
            return 0.0
        return min(100.0, 50 + 50 * (m - close) / (m - o))


def upper_wick_pct(k: dict) -> float:
    h, l = float(k["high"]), float(k["low"])
    rng = h - l
    if rng <= 0:
        return 0.0
    return (h - max(float(k["open"]), float(k["close"]))) / rng


def lower_wick_pct(k: dict) -> float:
    h, l = float(k["high"]), float(k["low"])
    rng = h - l
    if rng <= 0:
        return 0.0
    return (min(float(k["open"]), float(k["close"])) - l) / rng


def is_bottom_fractal_3(klines: list, i: int) -> bool:
    """简易底分型：i-1 为三根中最低（用于收回阶段的形态加分标记）。"""
    if i < 2:
        return False
    a, b, c = klines[i - 2], klines[i - 1], klines[i]
    return (float(b["low"]) < float(a["low"]) and float(b["low"]) < float(c["low"])
            and float(b["high"]) < float(a["high"]) and float(b["high"]) < float(c["high"]))


def is_top_fractal_3(klines: list, i: int) -> bool:
    if i < 2:
        return False
    a, b, c = klines[i - 2], klines[i - 1], klines[i]
    return (float(b["high"]) > float(a["high"]) and float(b["high"]) > float(c["high"])
            and float(b["low"]) > float(a["low"]) and float(b["low"]) > float(c["low"]))
