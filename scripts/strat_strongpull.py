"""强势币回踩跟踪(用户 2026-07-29)。

筛选(日线, 由1h聚合):
  ① 近 window_days 天内【翻倍起步】: 期间最高价 / 起涨最低价 >= double_x
  ② 翻倍后【回调很浅】: 从最高点算起的最大回撤 <= max_dd(最佳~10%)
  ③ 回调后【横盘跌不下去】: 最高点之后至少 hold_days 天, 且期间收盘始终守在
     高点×(1-max_dd) 之上; 近 flat_days 天日振幅收窄(<= flat_atr_ratio × 前期均振幅)
  ④ 日线【明显低点结构】: 最高点之后出现底分型, 且该底 >= 起涨低点(不破坏上升结构)
买点(4h, 由1h聚合):
  ⑤ 4h 出现【底分型】(去包含后), 分型确认即为买点; 入场=确认后下一根4h开盘
止损: 该4h底分型的最低点下方 sl_buf%;  止盈: 暂用 RR(待用户定结构位)

注: 仅"选币+找买点", 不含期望值验证 —— 按 CLAUDE.md 流程先出图人工审核。
"""
from dataclasses import dataclass


def aggregate(k1h: list, group: int) -> list:
    """1h → group小时。尾部不足一组丢弃。"""
    out = []
    n = (len(k1h) // group) * group
    for i in range(0, n, group):
        c = k1h[i:i + group]
        out.append({"open_time": int(c[0]["open_time"]), "open": float(c[0]["open"]),
                    "high": max(float(x["high"]) for x in c),
                    "low": min(float(x["low"]) for x in c),
                    "close": float(c[-1]["close"]),
                    "volume": sum(float(x["volume"]) for x in c)})
    return out


DEFAULT = dict(
    window_days=10,        # ① 回看天数
    double_x=1.5,          # ① 涨幅倍数(2026-07-29 用户: 2.0→1.5, 样本够回测)
    max_dd=0.30,           # ② 从最高点回撤上限(用户: 0.20→0.30, 暴涨币日振幅20-40%, 20%太苛刻)
    ideal_dd=0.10,         # ② 最佳回撤(仅用于打分)
    hold_days=2,           # ③ 高点后至少横盘天数
    flat_days=3,           # ③ 近N天算振幅收窄
    flat_ratio=1.0,        # ③ 近N天均振幅 / 拉升期均振幅 上限
    floor_on_close=True,   # 跌不下去按收盘判定(刚翻倍的币日振幅20-40%, 按最低价一根影线就破)
    sl_buf=0.5,            # ⑤ 止损缓冲%
    rr=2.0,
)


@dataclass
class Pick:
    symbol: str
    run_low: float          # 起涨低点
    run_high: float         # 翻倍后最高点
    gain_x: float           # 涨幅倍数
    dd: float               # 从最高点的回撤
    high_day_idx: int
    days_since_high: int
    flat_ratio: float
    daily_bottom: float | None      # 日线底分型价
    score: float


def _fractals(kl: list):
    """返回 (merged, fractals)。复用工程里的缠论去包含+分型。"""
    from app.engine.chan import find_fractals, merge_klines
    m = merge_klines(kl)
    return m, find_fractals(kl, m)


def screen(symbol: str, k1h: list, P: dict = None) -> Pick | None:
    """日线筛选。返回 Pick 或 None。"""
    P = {**DEFAULT, **(P or {})}
    d = aggregate(k1h, 24)
    W = int(P["window_days"])
    if len(d) < W + 2:
        return None
    seg = d[-W:]
    highs = [x["high"] for x in seg]
    lows = [x["low"] for x in seg]
    hi_i = max(range(len(seg)), key=lambda i: highs[i])
    # ① 起涨低点 = 最高点【之前】的最低低点; 必须翻倍
    if hi_i == 0:
        return None
    run_low = min(lows[:hi_i + 1])
    run_high = highs[hi_i]
    if run_low <= 0:
        return None
    gain = run_high / run_low
    if gain < float(P["double_x"]):
        return None
    # ②③ 见顶后【一路守住】不跌破 high*(1-max_dd)，且已守够 hold_days 天。
    # 2026-07-29 修: 原先用"剩余窗口最低点"算累计回撤 —— 回撤随时间累积, 等横盘天数够了
    # 累计回撤必然超限, 与③④⑤在时间上互斥, 导致全市场 0 命中。改为逐日"至今仍守着"判定。
    after = seg[hi_i + 1:]
    if len(after) < int(P["hold_days"]):
        return None
    floor = run_high * (1 - float(P["max_dd"]))
    px = "close" if P.get("floor_on_close", True) else "low"
    if any(x[px] < floor for x in after):      # 中途任何一天跌破即淘汰(默认看收盘, 插针不算)
        return None
    dd = (run_high - min(x["low"] for x in after)) / run_high
    # ③ 振幅收窄: 近 flat_days 天均振幅 / 拉升期(起涨→高点)均振幅
    F = int(P["flat_days"])
    rng_now = sum((x["high"] - x["low"]) / x["close"] for x in seg[-F:]) / F
    rise = seg[:hi_i + 1]
    rng_rise = sum((x["high"] - x["low"]) / x["close"] for x in rise) / max(len(rise), 1)
    fr = rng_now / rng_rise if rng_rise > 0 else 9.9
    if fr > float(P["flat_ratio"]):
        return None
    # ④ 日线低点结构: 高点之后出现底分型, 且不破起涨低点
    dbot = None
    if len(after) >= 3:
        _, fxs = _fractals(d)
        base = len(d) - len(after)
        for f in fxs:
            if f.kind == "bottom" and f.extreme_src_idx >= base and f.extreme_price >= run_low:
                dbot = f.extreme_price
    if dbot is None:
        return None
    # 打分: 回撤越接近 ideal_dd 越好 + 涨幅加成 + 越平越好
    ideal = float(P["ideal_dd"])
    score = round(max(0.0, 1 - abs(dd - ideal) / max(ideal, 1e-9)) * 2 + min(gain, 5) * 0.5 + (1 - fr), 2)
    return Pick(symbol, run_low, run_high, round(gain, 2), round(dd, 4), hi_i,
                len(after), round(fr, 2), dbot, score)


def find_buys(k1h: list, P: dict = None) -> list[dict]:
    """在4h上找底分型买点(全历史枚举, 供回测/出图)。"""
    P = {**DEFAULT, **(P or {})}
    k4 = aggregate(k1h, 4)
    if len(k4) < 20:
        return []
    merged, fxs = _fractals(k4)
    out = []
    for f in fxs:
        if f.kind != "bottom":
            continue
        ci = f.confirm_src_idx           # 分型确认K
        ei = ci + 1                      # 入场K = 确认后下一根
        if ei >= len(k4):
            continue
        entry = float(k4[ei]["open"])
        low = f.extreme_price
        sl = low * (1 - float(P["sl_buf"]) / 100.0)
        risk = entry - sl
        if risk <= 0:
            continue
        out.append({"tf": "4h", "direction": "long", "entry": entry, "sl": sl,
                    "tp": entry + float(P["rr"]) * risk,
                    "fx_low": low, "fx_time": int(k4[f.extreme_src_idx]["open_time"]),
                    "entry_idx": ei, "entry_time": int(k4[ei]["open_time"])})
    return out
