"""策略工具箱 —— Claude 生成的策略代码只写「怎么找入场点」，结算交给这里。

为什么要有这个:
    如果让大模型每次都自己写一遍"走到未来第几根K、先碰止损还是先碰止盈、手续费怎么折算R"，
    它每次都可能写错一点点 —— 而这类错误【不会报错】，只会让回测数字悄悄失真。
    所以把这部分固定下来: 生成的代码只负责发出入场信号，结算由 settle_all() 统一做。

生成的策略必须长这样(scan 的签名固定):

    from strat_kit import settle_all, ema, atr, body, is_bull, is_bear

    BASE = dict(min_drop_pct=3.0, vol_x=2.0, ...)      # 可调参数集中在这

    def scan(C, P=None):
        P = dict(BASE, **(P or {}))
        k5 = C("5m")                                    # {symbol: [bar, ...]}
        rows = []
        for sym, k in k5.items():
            for i in range(50, len(k) - 1):
                # ... 判定条件 ...
                if 命中:
                    rows.append({
                        "symbol": sym, "tf": "5m", "direction": "long",
                        "created_at": int(k[i]["open_time"]) // 1000,   # 触发K的开盘时刻
                        "i": i,                                          # 触发K在 k 里的下标
                        "entry": float(k[i]["close"]),                   # 入场=触发K收盘(无未来函数!)
                        "sl": 止损价, "tp": 止盈价,
                    })
        return settle_all(rows, k5, max_hold=288)       # 结算 → 补上 result/pnl_r/net_r

铁律(违反了回测就是假的):
    - 入场只能用【触发K收盘价】。用当根的 high/low 或下一根的价格 = 未来函数。
    - 判定条件只能引用 k[i] 及之前的K线。碰 k[i+1] 就是作弊。
"""
FEE_PCT_SIDE = 0.045            # 每边手续费%, 与 bt_registry.score / 灵感回测保持一致


def settle_all(rows, kmap, max_hold=288):
    """逐条结算: 从入场那根之后开始走, 先碰止损还是先碰止盈。

    max_hold: 最多持有几根K(默认288根5m=24小时), 超时按当时收盘价平掉 —— 不留永远 open 的单,
              否则统计会被一堆"还没结束"的单子污染。
    保守假设: 同一根K里既碰到止损又碰到止盈时, 算【止损】(不给自己送分)。
    """
    out = []
    for s in rows:
        k = kmap.get(s["symbol"]) or []
        i = s.get("i")
        if i is None or i + 1 >= len(k):
            continue
        entry, sl, tp = float(s["entry"]), float(s["sl"]), float(s["tp"])
        risk = abs(entry - sl)
        if risk <= 0:
            continue                                     # 止损=入场, 无意义
        long = s.get("direction", "long") == "long"
        s["result"], s["pnl_r"] = "open", None

        end = min(len(k), i + 1 + max_hold)
        for j in range(i + 1, end):
            hi, lo = float(k[j]["high"]), float(k[j]["low"])
            hit_sl = (lo <= sl) if long else (hi >= sl)
            hit_tp = (hi >= tp) if long else (lo <= tp)
            if hit_sl:                                   # 保守: 止损优先
                s["result"], s["pnl_r"] = "sl", -1.0
                break
            if hit_tp:
                s["result"] = "tp"
                s["pnl_r"] = round((tp - entry) / risk if long else (entry - tp) / risk, 3)
                break
        if s["result"] == "open" and end - 1 > i:
            px = float(k[end - 1]["close"])
            s["result"] = "timeout"
            s["pnl_r"] = round((px - entry) / risk if long else (entry - px) / risk, 3)

        if s["pnl_r"] is not None:
            cost = 2 * (FEE_PCT_SIDE / 100.0) * entry / risk      # 往返手续费(折算成R)
            s["net_r"] = round(s["pnl_r"] - cost, 3)
        s.pop("i", None)
        out.append(s)
    out.sort(key=lambda x: x["created_at"])
    return out


# ---------------- 常用指标(纯历史, 不含未来) ----------------

def ema(k, n, i, key="close"):
    """k[i] 及之前 n 根的 EMA。"""
    lo = max(0, i - n * 3)
    v = float(k[lo][key])
    a = 2.0 / (n + 1)
    for x in range(lo + 1, i + 1):
        v = float(k[x][key]) * a + v * (1 - a)
    return v


def sma(k, n, i, key="close"):
    lo = max(0, i - n + 1)
    xs = [float(b[key]) for b in k[lo:i + 1]]
    return sum(xs) / len(xs) if xs else 0.0


def atr(k, n, i):
    lo = max(1, i - n + 1)
    trs = []
    for x in range(lo, i + 1):
        h, l, pc = float(k[x]["high"]), float(k[x]["low"]), float(k[x - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0.0


def vol_x(k, i, n=20):
    """k[i] 的成交量 是 前n根均量的几倍。"""
    lo = max(0, i - n)
    xs = [float(b["volume"]) for b in k[lo:i]]
    m = sum(xs) / len(xs) if xs else 0.0
    return (float(k[i]["volume"]) / m) if m > 0 else 0.0


def body(b):
    return abs(float(b["close"]) - float(b["open"]))


def rng(b):
    return float(b["high"]) - float(b["low"])


def is_bull(b):
    return float(b["close"]) >= float(b["open"])


def is_bear(b):
    return float(b["close"]) < float(b["open"])


def hvn(k, lo, hi, bins=24):
    """密集成交区(High Volume Node): k[lo:hi] 这段里成交量最集中的那个价格。

    做法: 把这段的价格范围切成 bins 个价格桶, 每根K的成交量摊到它覆盖的桶里,
    返回成交量最大的那个桶的中点价。

    注意: 这跟「前高」完全是两回事 —— 前高是一个极值点, 密集成交区是【筹码堆积的地方】,
    也就是真正会形成支撑/阻力的位置。用户说的"阻力位"是后者。
    """
    seg = k[max(0, lo):hi]
    if len(seg) < 3:
        return None
    top = max(float(b["high"]) for b in seg)
    bot = min(float(b["low"]) for b in seg)
    if top <= bot:
        return None
    w = (top - bot) / bins
    buckets = [0.0] * bins
    for b in seg:
        h, l, v = float(b["high"]), float(b["low"]), float(b["volume"])
        a = max(0, min(bins - 1, int((l - bot) / w)))
        z = max(0, min(bins - 1, int((h - bot) / w)))
        n = z - a + 1
        for x in range(a, z + 1):
            buckets[x] += v / n          # 成交量平摊到这根K覆盖的价格桶
    j = buckets.index(max(buckets))
    return bot + (j + 0.5) * w


def reclaim(k, i, ia):
    """k[i] 的收盘价 是否把 k[ia] 这根下跌K线【收回去】了(收回它的实体上沿=它的开盘价)。"""
    return float(k[i]["close"]) >= float(k[ia]["open"])


def engulf_bull(k, i):
    """k[i] 阳线实体吞没 k[i-1] 阴线实体。"""
    if i < 1:
        return False
    a, b_ = k[i - 1], k[i]
    return (is_bear(a) and is_bull(b_)
            and float(b_["close"]) >= float(a["open"])
            and float(b_["open"]) <= float(a["close"]))
