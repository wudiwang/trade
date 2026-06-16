"""独立策略存档:「小转大」(用户 2026-06-16)。

纯 5m 量能高潮反转。形态(做多):
  ① 急跌段:近 decline_bars 根内 (段起最高→高潮最低)/段起 ≥ drop_pct(幅度特别大)
  ② 量能递增:下跌过程量能不断放大(末3根均量 > 段首3根均量)
  ③ 巨量高潮:最后那根下跌K 量 ∈ [climax_min, climax_max] × 前vol_ma均量(明显大但不夸张)
  ④ 缩量企稳:高潮后 dryup_window 根内缩量(量 < 高潮量×dryup_ratio)且不破高潮低点
  ⑤ 5m底分型:企稳区出现底分型 → 买入
  止损 = min(高潮低, 分型低) 下方;止盈 = 按RR。
做空镜像(放量递增急涨→巨量顶→缩量→顶分型卖)。

完全独立,不接实盘、不改引擎,仅回测。一次拉30天,切出近1周/2周/1月三档统计。
"""
import asyncio


def _arr(k):
    return ([float(x["open"]) for x in k], [float(x["high"]) for x in k],
            [float(x["low"]) for x in k], [float(x["close"]) for x in k],
            [float(x["volume"]) for x in k])


def detect_small_to_big(k5: list, direction: str, P: dict) -> list[dict]:
    """事件式扫描整段 5m,返回该方向信号(未结算)。无前视。"""
    n = len(k5)
    o, h, l, c, v = _arr(k5)
    long = direction == "long"
    db = P["decline_bars"]            # 恐慌段(急跌/急涨)长度
    bma = P["baseline_ma"]            # 段前基线均量回看
    sigs = []
    i = bma + db
    while i < n - 1:
        L = i                          # 候选恐慌极值K(段末=最低/最高)
        legv = v[L - db + 1:L + 1]
        base = sum(v[L - db - bma:L - db]) / bma   # 段之前的基线量
        if base <= 0:
            i += 1; continue
        leg_avg = sum(legv) / len(legv)
        # ① 持续放量: 段均量 ≥ sustain_mult×基线, 且段内 ≥ sustain_bars_min 根明显放量(非单根spike)
        if leg_avg < P["sustain_mult"] * base:
            i += 1; continue
        elevated = sum(1 for x in legv if x >= P["elevated_mult"] * base)
        if elevated < P["sustain_bars_min"]:
            i += 1; continue
        # ② 急跌/急涨幅度 + L是段内极值
        if long:
            if l[L] != min(l[L - db + 1:L + 1]):
                i += 1; continue
            seg_ext = max(h[L - db + 1:L + 1])
            move = (seg_ext - l[L]) / seg_ext if seg_ext > 0 else 0
        else:
            if h[L] != max(h[L - db + 1:L + 1]):
                i += 1; continue
            seg_ext = min(l[L - db + 1:L + 1])
            move = (h[L] - seg_ext) / seg_ext if seg_ext > 0 else 0
        if move < P["drop_pct"] / 100.0:
            i += 1; continue
        ext = l[L] if long else h[L]               # 恐慌极值 = 止损基准
        # ③立即缩量 + ④第一根反弹K(rebound_within根内) = 买/卖点
        e_idx = None
        for j in range(L + 1, min(n, L + 1 + P["rebound_within"])):
            if long and l[j] < ext:                # 又破新低=没企稳, 作废
                break
            if (not long) and h[j] > ext:
                break
            dried = v[j] <= leg_avg * P["dryup_ratio"]    # 相对恐慌段均量明显缩量
            rebound = (c[j] > o[j]) if long else (c[j] < o[j])   # 第一根反弹(收阳/收阴)
            if dried and rebound:
                e_idx = j; break
        if e_idx is None:
            i += 1; continue
        entry = c[e_idx]
        buf = P["sl_buf_pct"] / 100.0
        if long:
            sl = ext * (1 - buf); risk = entry - sl
            tp = entry + P["rr_target"] * risk
        else:
            sl = ext * (1 + buf); risk = sl - entry
            tp = entry - P["rr_target"] * risk
        if risk > 0:
            sigs.append({"direction": direction, "entry": entry, "sl": sl, "tp": tp,
                         "anchor": int(k5[L]["open_time"]), "entry_idx": e_idx,
                         "created_at": int(k5[e_idx]["open_time"]) // 1000,
                         "leg_vol_x": round(leg_avg / base, 2), "move_pct": round(move * 100, 1),
                         "rebound_lag": e_idx - L})
        i = e_idx + 1
    return sigs


# ------------------------- 回测 -------------------------
def _settle(s, series):
    s["result"], s["pnl_r"], s["bars_held"] = "open", None, None
    entry, sl, tp = s["entry"], s["sl"], s["tp"]
    risk = abs(entry - sl)
    if risk <= 0:
        return
    long = s["direction"] == "long"
    for j in range(s["entry_idx"] + 1, len(series)):
        lo, hi = float(series[j]["low"]), float(series[j]["high"])
        if long:
            if lo <= sl:
                s["result"], s["pnl_r"] = "sl", -1.0
            elif hi >= tp:
                s["result"], s["pnl_r"] = "tp", (tp - entry) / risk
        else:
            if hi >= sl:
                s["result"], s["pnl_r"] = "sl", -1.0
            elif lo <= tp:
                s["result"], s["pnl_r"] = "tp", (entry - tp) / risk
        if s["result"] != "open":
            s["bars_held"] = j - s["entry_idx"]
            break


def _bucket(rows, keyfn):
    out = {}
    for s in rows:
        b = out.setdefault(keyfn(s), {"signals": 0, "closed": 0, "wins": 0, "total_r": 0.0, "open": 0})
        b["signals"] += 1
        if s["result"] == "open":
            b["open"] += 1
        else:
            b["closed"] += 1
            b["total_r"] += s["pnl_r"]
            if s["result"] == "tp":
                b["wins"] += 1
    for b in out.values():
        b["win_rate"] = round(b["wins"] / b["closed"] * 100, 1) if b["closed"] else 0.0
        b["expectancy_r"] = round(b["total_r"] / b["closed"], 3) if b["closed"] else 0.0
        b["total_r"] = round(b["total_r"], 2)
    return out


def _walk(sym, k5, directions, P):
    out = []
    for d in directions:
        for s in detect_small_to_big(k5, d, P):
            s["symbol"] = sym
            _settle(s, k5)
            out.append(s)
    return out


async def run_smallbig_backtest(rest, symbols, days=30, now_ms=None, directions=("long", "short"),
                                rr_target=2.0, decline_bars=8, baseline_ma=20,
                                sustain_mult=2.0, elevated_mult=1.5, sustain_bars_min=3,
                                drop_pct=6.0, dryup_ratio=0.8, rebound_within=4,
                                sl_buf_pct=0.3, spans=(7, 14, 30), progress=None):
    """一次拉 days 天 5m,切出 spans 各档统计。now_ms 用于切窗(不传则用最后一根K时间)。"""
    from .backtest import fetch_series
    import time as _t
    t0 = _t.time()
    P = dict(decline_bars=decline_bars, baseline_ma=baseline_ma, sustain_mult=sustain_mult,
             elevated_mult=elevated_mult, sustain_bars_min=sustain_bars_min, drop_pct=drop_pct,
             dryup_ratio=dryup_ratio, rebound_within=rebound_within,
             sl_buf_pct=sl_buf_pct, rr_target=rr_target)
    sem = asyncio.Semaphore(5)
    alls = []
    done = [0]
    last_ts = [0]

    async def one(sym):
        async with sem:
            try:
                k5 = await fetch_series(rest, sym, "5m", days)
            except Exception:
                k5 = []
        if len(k5) >= 80:
            last_ts[0] = max(last_ts[0], int(k5[-1]["open_time"]) // 1000)
            alls.extend(await asyncio.to_thread(_walk, sym, k5, list(directions), P))
        done[0] += 1
        if progress and done[0] % 10 == 0:
            progress(done[0], len(symbols), sym)

    await asyncio.gather(*(one(s) for s in symbols))
    ref = (now_ms // 1000) if now_ms else last_ts[0]
    by_span = {}
    for sp in spans:
        cut = ref - sp * 86400
        rows = [s for s in alls if s["created_at"] >= cut]
        by_span[f"{sp}d"] = {"by_direction": _bucket(rows, lambda s: s["direction"]),
                             "total": _bucket(rows, lambda s: "all").get("all", {}),
                             "n": len(rows)}
    return {"days": days, "symbols": len(symbols), "directions": list(directions),
            "params": P, "elapsed_s": round(_t.time() - t0, 1), "n_all": len(alls),
            "by_span": by_span,
            "samples": [{"t": s["created_at"], "sym": s["symbol"], "dir": s["direction"],
                         "climaxX": s["climax_ratio"], "move%": s["move_pct"],
                         "entry": round(s["entry"], 6), "result": s["result"],
                         "pnl_r": s["pnl_r"]} for s in alls[-30:]]}
