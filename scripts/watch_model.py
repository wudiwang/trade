"""观察(Watch)剧本状态模型(Phase1 Task10, 纯函数, 不接实盘)。

一个 watch = 为某币记的一个剧本:到达触发价=可行动提醒;跌破/涨破失效价=作废;过期=超时。
状态机:
  watching → triggered   (价格触及触发价 = 可行动)
  watching → invalidated (收盘越过失效价 = 形态破)
  watching → expired     (超过有效期未触发)
  triggered / invalidated / expired / done = 不再自动流转(triggered 等人工处置→done)

供 Watch Agent 与本地剧本面板复用。无外部依赖、无副作用。
"""

WATCHING = "watching"
TRIGGERED = "triggered"
INVALIDATED = "invalidated"
EXPIRED = "expired"
DONE = "done"

TERMINAL = {INVALIDATED, EXPIRED, DONE}


def is_actionable(state: str) -> bool:
    """是否到了可行动(发提醒)的状态。"""
    return state == TRIGGERED


def is_terminal(state: str) -> bool:
    """是否已是终态(不再监控)。"""
    return state in TERMINAL


def make_watch(symbol, direction, trigger_price, invalidate_price, expiry_ts, state=WATCHING):
    if direction not in ("long", "short"):
        raise ValueError("direction must be long/short")
    return {"symbol": symbol, "direction": direction,
            "trigger_price": float(trigger_price), "invalidate_price": float(invalidate_price),
            "expiry_ts": int(expiry_ts), "state": state}


def _bar_ts(bar) -> int:
    t = int(bar.get("open_time", bar.get("t", 0)))
    return t // 1000 if t > 1_000_000_000_000 else t   # 容忍 ms / s


def next_state(watch: dict, bar: dict) -> str:
    """给定当前 watch + 一根K, 返回新状态。纯函数(不修改入参)。
    优先级: 已是终态/已触发 → 不变;超期 → expired;失效(收盘越线) → invalidated;触及触发价 → triggered。"""
    st = watch["state"]
    if st in TERMINAL or st == TRIGGERED:
        return st
    if _bar_ts(bar) > watch["expiry_ts"]:
        return EXPIRED
    hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
    inv, trg = watch["invalidate_price"], watch["trigger_price"]
    if watch["direction"] == "long":
        if cl < inv:                      # 收盘跌破失效价(失效优先于触发)
            return INVALIDATED
        if lo <= trg <= hi:               # 价格触及触发价
            return TRIGGERED
    else:
        if cl > inv:
            return INVALIDATED
        if lo <= trg <= hi:
            return TRIGGERED
    return WATCHING


def apply_bar(watch: dict, bar: dict) -> dict:
    """返回推进一根K后的新 watch(浅拷贝, 不改入参)。"""
    return {**watch, "state": next_state(watch, bar)}
