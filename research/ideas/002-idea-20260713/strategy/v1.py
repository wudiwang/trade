from strat_kit import settle_all, hvn, reclaim, ema, sma, atr, vol_x, body, rng, is_bull, is_bear, engulf_bull

TITLE = "大阴插针力竭·阻力位回踩企稳反转"

BASE = dict(
    zone_lookback=12,           # 阻力位回溯窗口: 放量大阴线(j)之前12根5m(=1小时), 用hvn()在这段里找筹码堆积处
    vol_mult=2.0,                # 大阴线放量倍数 —— 用户拍板"两倍以上"
    body_mult=3.0,               # 大阴线实体 / 阻力区内小K实体均值 的倍数 —— 用户拍板"长3倍左右"
    probe_window=6,              # c4"再往下砸"允许持续几根K(默认6根=30分钟, 待拍板)
    recover_window=2,            # c4破新低后"马上收回"允许几根之内收回(默认2根, 待拍板)
    reclaim_search=8,            # c4确认力竭后, 允许多少根K之内把大阴线(j)整根收回去(默认8根, 待拍板)
    consolidate_window=4,        # c5站回阻力位后, 横盘至少要走几根K才算数(默认4根=20分钟, 待拍板)
    consolidate_range_pct=0.01,  # c5横盘振幅阈值: (最高-最低)/close <= 1%(默认, 待拍板)
    sl_buffer_pct=0.002,         # 止损缓冲: 结构低点再往下0.2%(默认, 待拍板)
    tp_r_cap=1.8,                # 固定R兜底止盈倍数(默认1.8倍止损距离), 与阻力位上沿(zone_top)取更近者
)


def scan(C, P=None):
    P = dict(BASE, **(P or {}))
    k5 = C("5m")
    rows = []
    zl = P["zone_lookback"]

    for sym, k in k5.items():
        n = len(k)
        if n < 60:
            continue

        for j in range(zl + 5, n - 1):
            # ---- c1: 阻力位 = j之前zl根K线的密集成交区(筹码堆积处), 用hvn定位, 不用"前高"近似 ----
            lo, hi = j - zl, j
            zone_px = hvn(k, lo, hi)
            if zone_px is None:
                continue
            zone_top = max(float(b["high"]) for b in k[lo:hi])
            zone_bot = min(float(b["low"]) for b in k[lo:hi])
            if zone_top <= zone_bot:
                continue

            cj = k[j]
            # ---- c2: 放量大阴线, 往下打穿阻力位下沿, 做出要打穿的样子 ----
            if not is_bear(cj):
                continue
            if float(cj["low"]) >= zone_bot:
                continue
            if vol_x(k, j, n=zl) < P["vol_mult"]:
                continue
            avg_body = sum(body(b) for b in k[lo:hi]) / max(1, hi - lo)
            if avg_body <= 0 or body(cj) < P["body_mult"] * avg_body:
                continue

            j1 = j + 1
            if j1 >= n - 1:
                continue
            cj1 = k[j1]
            # ---- c3: 小阳线拉回 ----
            if not is_bull(cj1):
                continue
            if float(cj1["close"]) <= float(cj["close"]):
                continue

            # ---- c4: 再往下砸不破新低(或破了马上收回) = 力竭确认 ----
            low_j = float(cj["low"])
            probe_end = min(n - 1, j1 + P["probe_window"])
            broke_unrecovered = False
            exhaustion_idx = probe_end
            for m in range(j1 + 1, probe_end + 1):
                if float(k[m]["low"]) < low_j:
                    recovered_at = None
                    for r in range(m, min(n - 1, m + P["recover_window"]) + 1):
                        if float(k[r]["close"]) > low_j:
                            recovered_at = r
                            break
                    if recovered_at is None:
                        broke_unrecovered = True
                        break
                    exhaustion_idx = max(exhaustion_idx, recovered_at)
            if broke_unrecovered:
                continue

            # ---- c4关键: 必须把这根大阴线(j)整根收回去(close >= open[j]), 用reclaim()判定 ----
            reclaim_end = min(n - 1, exhaustion_idx + P["reclaim_search"])
            reclaim_idx = None
            for m in range(j1, reclaim_end + 1):
                if reclaim(k, m, j):
                    reclaim_idx = m
                    break
            if reclaim_idx is None:
                continue

            # ---- c5: 站回阻力位后开始横盘, 横盘最后一根K = 入场K ----
            cw = P["consolidate_window"]
            entry_i = None
            for m in range(reclaim_idx, reclaim_end + 1):
                if not reclaim(k, m, j):
                    continue
                seg_end = m + cw - 1
                if seg_end >= n - 1:
                    break
                seg = k[m:seg_end + 1]
                seg_top = max(float(b["high"]) for b in seg)
                seg_bot = min(float(b["low"]) for b in seg)
                if (seg_top - seg_bot) / float(seg[-1]["close"]) <= P["consolidate_range_pct"]:
                    entry_i = seg_end
                    break
            if entry_i is None:
                continue

            i = entry_i
            if i >= n - 1:
                continue

            entry = float(k[i]["close"])
            struct_low = min([low_j] + [float(k[x]["low"]) for x in range(j1, reclaim_idx + 1)])
            sl = struct_low * (1 - P["sl_buffer_pct"])
            risk = entry - sl
            if risk <= 0:
                continue

            tp_r = entry + P["tp_r_cap"] * risk
            tp = min(zone_top, tp_r) if zone_top > entry else tp_r
            if tp <= entry:
                continue

            rows.append({
                "symbol": sym, "tf": "5m", "direction": "long",
                "created_at": int(k[i]["open_time"]) // 1000,
                "i": i,
                "entry": entry, "sl": sl, "tp": tp,
            })

    return settle_all(rows, k5, max_hold=288)