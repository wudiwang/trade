"""策略V2核心：主力标志K线识别（巨量+大振幅+饱满实体+极值位置）。

五重过滤删噪音：
1. 量 >= vol_mult x 均量 且 为近 vol_max_lookback 根最大（鹤立鸡群）
2. 振幅 >= range_atr_min x ATR（排除巨量小振幅的对倒/出货）
3. 实体占比 >= body_min（排除十字星）
4. 收盘位置：做多收在K线上部 close_pos_min 以上（排除冲高被砸回的诱多）
5. 位置：低点贴近/跌破阶段极值（排除半山腰放量）——在 at_extreme 单独判
"""


def is_marker_candle(klines: list, i: int, *, vol_ma_period: int = 20,
                     vol_mult: float = 3.0, vol_max_lookback: int = 30,
                     body_min: float = 0.5, range_atr_min: float = 1.5,
                     close_pos_min: float = 0.65, atr_val: float = 0.0):
    """返回 (是否标志K, 方向'long'/'short'/None, 明细dict)。"""
    if i < max(vol_ma_period, vol_max_lookback):
        return False, None, {}
    k = klines[i]
    o, h, l, c, v = (float(k["open"]), float(k["high"]), float(k["low"]),
                     float(k["close"]), float(k["volume"]))
    rng = h - l
    if rng <= 0 or v <= 0:
        return False, None, {}

    window = [float(klines[j]["volume"]) for j in range(i - vol_ma_period, i)]
    vol_ma = sum(window) / vol_ma_period
    if vol_ma <= 0:
        return False, None, {}
    vol_ratio = v / vol_ma
    recent_max = max(float(klines[j]["volume"]) for j in range(i - vol_max_lookback, i))

    detail = {"vol_ratio": round(vol_ratio, 2), "is_recent_max": v > recent_max,
              "body_pct": round(abs(c - o) / rng, 2),
              "range_atr": round(rng / atr_val, 2) if atr_val > 0 else None,
              "close_pos": round((c - l) / rng, 2)}

    if vol_ratio < vol_mult or v <= recent_max:
        return False, None, detail
    if atr_val > 0 and rng < range_atr_min * atr_val:
        return False, None, detail
    if abs(c - o) / rng < body_min:
        return False, None, detail

    close_pos = (c - l) / rng
    if c > o and close_pos >= close_pos_min:
        return True, "long", detail
    if c < o and close_pos <= 1 - close_pos_min:
        return True, "short", detail
    return False, None, detail


def at_extreme(klines: list, i: int, direction: str,
               lookback: int = 50, tol_pct: float = 1.0) -> bool:
    """位置过滤：做多要求本K低点贴近/跌破前 lookback 根最低点（容差tol_pct%）；
    做空对称看前高。半山腰的标志K一律不要。"""
    start = max(0, i - lookback)
    if start >= i:
        return False
    if direction == "long":
        prior_low = min(float(klines[j]["low"]) for j in range(start, i))
        return float(klines[i]["low"]) <= prior_low * (1 + tol_pct / 100)
    prior_high = max(float(klines[j]["high"]) for j in range(start, i))
    return float(klines[i]["high"]) >= prior_high * (1 - tol_pct / 100)
