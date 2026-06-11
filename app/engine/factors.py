"""因子库：每个因子返回 (得分, 说明)。新因子=加一个函数+在 score_signal 里登记。"""
from .chan import Fractal


def rsi(closes: list[float], period: int = 14) -> list[float]:
    """Wilder RSI，返回与 closes 等长的序列（前 period 个为 50 中性值）。"""
    n = len(closes)
    if n <= period:
        return [50.0] * n
    out = [50.0] * n
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def atr(klines: list, period: int = 14) -> float:
    """Wilder ATR，返回最新值。数据不足返回 0。"""
    n = len(klines)
    if n <= period:
        return 0.0
    trs = []
    for i in range(1, n):
        h, l = float(klines[i]["high"]), float(klines[i]["low"])
        pc = float(klines[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def f_rsi_extreme(direction: str, rsi_val: float, oversold: float, overbought: float):
    if direction == "long" and rsi_val < oversold:
        return 1, f"RSI超卖({rsi_val:.0f})"
    if direction == "short" and rsi_val > overbought:
        return 1, f"RSI超买({rsi_val:.0f})"
    return 0, None


def f_rsi_divergence(direction: str, fractals: list[Fractal], cur: Fractal,
                     rsi_seq: list[float], lookback: int = 60):
    """底背离：当前分型价更低但RSI更高；顶背离对称。+2"""
    same = [f for f in fractals if f.kind == cur.kind
            and f.extreme_src_idx < cur.extreme_src_idx
            and cur.extreme_src_idx - f.extreme_src_idx <= lookback]
    if not same:
        return 0, None
    prev = same[-1]
    if prev.extreme_src_idx >= len(rsi_seq) or cur.extreme_src_idx >= len(rsi_seq):
        return 0, None
    r_prev, r_cur = rsi_seq[prev.extreme_src_idx], rsi_seq[cur.extreme_src_idx]
    if direction == "long" and cur.extreme_price < prev.extreme_price and r_cur > r_prev + 1:
        return 2, f"RSI底背离({r_prev:.0f}→{r_cur:.0f})"
    if direction == "short" and cur.extreme_price > prev.extreme_price and r_cur < r_prev - 1:
        return 2, f"RSI顶背离({r_prev:.0f}→{r_cur:.0f})"
    return 0, None


def f_funding(direction: str, funding_rate: float | None, extreme: float):
    """资金费率极值：负费率极值利多（空头拥挤），正费率极值利空。+1"""
    if funding_rate is None:
        return 0, None
    if direction == "long" and funding_rate <= -extreme:
        return 1, f"费率{funding_rate*100:.3f}%空头拥挤"
    if direction == "short" and funding_rate >= extreme:
        return 1, f"费率{funding_rate*100:.3f}%多头拥挤"
    return 0, None


def f_taker_ratio(direction: str, confirm_bar: dict, min_ratio: float):
    """确认K主动买盘占比。taker_buy=0 视为无数据(老K线)不计分。+1"""
    vol = float(confirm_bar.get("volume") or 0)
    tb = float(confirm_bar.get("taker_buy") or 0)
    if vol <= 0 or tb <= 0:
        return 0, None
    ratio = tb / vol
    if direction == "long" and ratio >= min_ratio:
        return 1, f"主动买盘{ratio*100:.0f}%"
    if direction == "short" and ratio <= 1 - min_ratio:
        return 1, f"主动卖盘{(1-ratio)*100:.0f}%"
    return 0, None


def f_mtf_resonance(direction: str, tf: str, trend_15m: int):
    """5m信号与15m趋势共振（15m信号本身已被1h过滤，不重复计分）。+1"""
    if tf != "5m" or trend_15m == 0:
        return 0, None
    if (direction == "long" and trend_15m == 1) or (direction == "short" and trend_15m == -1):
        return 1, "15m趋势共振"
    return 0, None


def f_wick_rejection(direction: str, extreme_bar: dict, min_ratio: float):
    """分型极值K拒绝影线：底分型长下影=买盘承接，顶分型长上影=卖压。+1"""
    h, l = float(extreme_bar["high"]), float(extreme_bar["low"])
    o, c = float(extreme_bar["open"]), float(extreme_bar["close"])
    rng = h - l
    if rng <= 0:
        return 0, None
    if direction == "long":
        ratio = (min(o, c) - l) / rng
        if ratio >= min_ratio:
            return 1, f"下影线拒绝{ratio*100:.0f}%"
    else:
        ratio = (h - max(o, c)) / rng
        if ratio >= min_ratio:
            return 1, f"上影线拒绝{ratio*100:.0f}%"
    return 0, None


def f_btc_resonance(direction: str, symbol: str, btc_trend: int):
    """BTC大盘方向共振：山寨短线高度跟随BTC，顺大盘加分。BTC自身不计。+1"""
    if symbol.startswith("BTC") or btc_trend == 0:
        return 0, None
    if (direction == "long" and btc_trend == 1) or (direction == "short" and btc_trend == -1):
        return 1, "BTC大盘共振"
    return 0, None


def sl_atr_sane(entry: float, sl: float, atr_val: float,
                lo: float, hi: float) -> tuple[bool, str]:
    """止损距离须在 [lo*ATR, hi*ATR]。ATR无数据则放行。"""
    if atr_val <= 0:
        return True, ""
    d = abs(entry - sl)
    if d < lo * atr_val:
        return False, f"止损距离{d:.6g}<{lo}xATR(噪音区)"
    if d > hi * atr_val:
        return False, f"止损距离{d:.6g}>{hi}xATR(过远)"
    return True, ""


def score_signal(cfg, *, direction: str, symbol: str, tf: str, klines: list,
                 fractals: list[Fractal], cur: Fractal, confirm_bar: dict,
                 funding_rate: float | None, trend_15m: int, btc_trend: int
                 ) -> tuple[int, list[str], dict]:
    """运行全部因子 → (总分, 命中理由列表, 明细dict)。
    新因子：上面加函数，这里登记一行。config里 factors.disabled 列表可停用单个因子。"""
    g = lambda k, d: cfg.get(f"factors.{k}", d)
    closes = [float(k["close"]) for k in klines]
    rsi_seq = rsi(closes, g("rsi_period", 14))

    registry = {
        "rsi_extreme": lambda: f_rsi_extreme(
            direction, rsi_seq[cur.extreme_src_idx],
            g("rsi_extreme.oversold", 30), g("rsi_extreme.overbought", 70)),
        "rsi_divergence": lambda: f_rsi_divergence(direction, fractals, cur, rsi_seq),
        "funding": lambda: f_funding(direction, funding_rate, g("funding.extreme", 0.0005)),
        "taker_ratio": lambda: f_taker_ratio(direction, confirm_bar, g("taker_ratio.min_ratio", 0.58)),
        "mtf_resonance": lambda: f_mtf_resonance(direction, tf, trend_15m),
        "wick_rejection": lambda: f_wick_rejection(
            direction, klines[cur.extreme_src_idx], g("wick_rejection.min_ratio", 0.5)),
        "btc_resonance": lambda: f_btc_resonance(direction, symbol, btc_trend),
    }

    score = 0
    hits: list[str] = []
    detail: dict = {}
    for name, fn in registry.items():
        if not g(f"{name}.enabled", True):
            continue
        pts, note = fn()
        score += pts
        detail[name] = {"score": pts, "note": note}
        if pts and note:
            hits.append(note)
    return score, hits, detail
