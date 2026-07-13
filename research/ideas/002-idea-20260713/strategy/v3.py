from strat_kit import settle_all, hvn, reclaim, ema, sma, atr, vol_x, body, rng, is_bull, is_bear, engulf_bull

TITLE = "大阴插针力竭·阻力位回踩企稳反转"

BASE = dict(
    zone_lookback=12,              # 阻力位回溯窗口: 放量大阴线(j)之前12根5m(=1小时), 用hvn()在这段里找筹码堆积处
    dominance_lookback=12,         # 【本版新增】大阴线必须是这个窗口内实体最大的一根(默认=zone_lookback, 待拍板)
    trend_ema=20,                  # 【本版新增】下跌趋势判定用的EMA周期(待拍板)
    trend_lookback=20,             # 【本版新增】下跌趋势判定回溯窗口: 20根=100分钟(待拍板)
    vol_mult=2.0,                  # 大阴线放量倍数 —— 用户拍板"两倍以上", 本版不动(理由见文字版)
    body_mult=4.0,                 # 大阴线实体/阻力区内小K实体均值 —— 从v2的3.0上调到4.0(收紧, 待拍板)
    probe_window=6,                # c4"二次探底"必须发生在小阳线拉回后几根K以内(沿用v2, 待拍板)
    probe_retest_tolerance=0.0008, # c4二次探底判定容差: 从v2的0.15%收紧到0.08%(待拍板)
    recover_window=2,              # c4探底如果破了新低, 必须在几根K之内收盘收回到low[j]之上(沿用v2, 待拍板)
    reclaim_search=6,              # c4力竭确认后, 允许多少根K之内收回大阴线 —— 从v2的8收紧到6(待拍板)
    consolidate_window=4,          # c5站回阻力位后, 横盘至少要走几根K才算数(沿用v2, 待拍板)
    consolidate_range_pct=0.01,    # c5横盘振幅阈值(沿用v2, 待拍板)
    sl_buffer_pct=0.002,           # 止损缓冲(沿用v2, 待拍板)
    tp_r_cap=1.8,                  # 固定R兜底止盈倍数(沿用v2, 待拍板)
)


def scan(C, P=None):
    P = dict(BASE, **(P or {}))
    k5 = C("5m")
    rows = []
    zl = P["zone_lookback"]
    start = zl + P["trend_lookback"] + 5  # trend precondition需要向前多看trend_lookback根, 提前跳过必然不够的下标

    for sym, k in k5.items():
        n = len(k)
        if n < 60:
            continue

        for j in range(start, n - 1):
            cj = k[j]
            # ---- 便宜条件放最前(铁律5): 先看是不是阴线 ----
            if not is_bear(cj):
                continue

            lo, hi = j - zl, j
            zone_top = max(float(b["high"]) for b in k[lo:hi])
            zone_bot = min(float(b["low"]) for b in k[lo:hi])
            if zone_top <= zone_bot:
                continue
            if float(cj["low"]) >= zone_bot:
                continue

            # ---- c2 放量+实体倍数(沿用v1/v2逻辑, 阈值收紧) ----
            if vol_x(k, j, n=zl) < P["vol_mult"]:
                continue
            avg_body = sum(body(b) for b in k[lo:hi]) / max(1, hi - lo)
            if avg_body <= 0 or body(cj) < P["body_mult"] * avg_body:
                continue

            # ---- c2追加(本版新增, 结构性): 大阴线必须是dominance_lookback窗口里实体最大的一根 ----
            # 对应盲测"这根大K线的下跌是哪根啊/标志性下跌K线是哪一段" —— 如果j只是一堆差不多大小
            # 阴线里普通的一根(比如是一段阴跌里的中间一根), 就不算"标志性", 直接淘汰。
            dom_lo = max(0, j - P["dominance_lookback"])
            if body(cj) < max(body(b) for b in k[dom_lo:j]):
                continue

            # ---- c0(本版新增, 结构性, 必须优先落实): 必须处在下跌趋势里反转, 不是上涨趋势里延续 ----
            # 对应盲测"这个前面都是5分钟的上涨趋势...我是要在下跌趋势里面找反转"。
            # 检查阻力区起点(lo)的EMA相比trend_lookback根之前的EMA是下降的, 确认大背景是下跌。
            trend_i = lo - P["trend_lookback"]
            if trend_i < 0:
                continue
            if ema(k, P["trend_ema"], lo) >= ema(k, P["trend_ema"], trend_i):
                continue

            # ---- c1: 阻力位, 用hvn()确认真的是密集成交区(放在所有便宜条件之后, 铁律5) ----
            zone_px = hvn(k, lo, hi)
            if zone_px is None:
                continue

            j1 = j + 1
            if j1 >= n - 1:
                continue
            cj1 = k[j1]
            # ---- c3 小阳线拉回 ----
            if not is_bull(cj1):
                continue
            if float(cj1["close"]) <= float(cj["close"]):
                continue

            # ---- c4a 二次探底(必须条件, 沿用v2, 容差收紧) ----
            low_j = float(cj["low"])
            probe_end = min(n - 1, j1 + P["probe_window"])
            retest_thresh = low_j * (1 + P["probe_retest_tolerance"])
            retest_idx = None
            retest_broke_low = False
            for m in range(j1 + 1, probe_end + 1):
                if float(k[m]["low"]) <= retest_thresh:
                    retest_idx = m
                    retest_broke_low = float(k[m]["low"]) < low_j
                    break
            if retest_idx is None:
                continue  # 没有二次探底, 直接反包 -> 淘汰

            # ---- c4b 探底后的力竭确认(沿用v2) ----
            if retest_broke_low:
                recovered_at = None
                for r in range(retest_idx, min(n - 1, retest_idx + P["recover_window"]) + 1):
                    if float(k[r]["close"]) > low_j:
                        recovered_at = r
                        break
                if recovered_at is None:
                    continue
                exhaustion_idx = recovered_at
            else:
                exhaustion_idx = retest_idx

            # ---- c4关键 收回大阴线(力竭确认之后, reclaim_search收紧) ----
            reclaim_end = min(n - 1, exhaustion_idx + P["reclaim_search"])
            reclaim_idx = None
            for m in range(exhaustion_idx, reclaim_end + 1):
                if reclaim(k, m, j):
                    reclaim_idx = m
                    break
            if reclaim_idx is None:
                continue

            # ---- c5 站回阻力位+横盘, 末根为入场K ----
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