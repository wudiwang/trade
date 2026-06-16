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
    db = P["decline_bars"]
    vol_ma = P["vol_ma"]
    sigs = []
    i = max(vol_ma, db) + 1
    while i < n - 1:
        avg = sum(v[i - vol_ma:i]) / vol_ma
        if avg <= 0:
            i += 1; continue
        ratio = v[i] / avg
        if not (P["climax_min"] <= ratio <= P["climax_max"]):     # ③ 巨量但不夸张
            i += 1; continue
        # 高潮K必须是顺势那根并做极值
        if long:
            if not (c[i] < o[i] and l[i] <= min(l[i - db:i + 1])):
                i += 1; continue
            seg_ext = max(h[i - db:i])
            move = (seg_ext - l[i]) / seg_ext if seg_ext > 0 else 0   # ① 急跌幅度
        else:
            if not (c[i] > o[i] and h[i] >= max(h[i - db:i + 1])):
                i += 1; continue
            seg_ext = min(l[i - db:i])
            move = (h[i] - seg_ext) / seg_ext if seg_ext > 0 else 0   # 急涨幅度
        if move < P["drop_pct"] / 100.0:
            i += 1; continue
        # ② 量能递增:末3根均量 > 段首3根均量
        last3 = sum(v[i - 2:i + 1]) / 3
        first3 = sum(v[i - db:i - db + 3]) / 3
        if first3 <= 0 or last3 <= first3:
            i += 1; continue
        climax_lo = l[i] if long else h[i]
        # ④⑤ 缩量企稳 + 底/顶分型
        e_idx = None; fext = None
        for k in range(i + 1, min(n - 1, i + 1 + P["dryup_window"])):
            if v[k] >= v[i] * P["dryup_ratio"]:                  # 还没缩量
                continue
            if long:
                if l[k] < l[k - 1] and l[k] < l[k + 1] and l[k] >= climax_lo:
                    e_idx = k + 1; fext = l[k]; break
            else:
                if h[k] > h[k - 1] and h[k] > h[k + 1] and h[k] <= climax_lo:
                    e_idx = k + 1; fext = h[k]; break
        if e_idx is None or e_idx >= n:
            i += 1; continue
        entry = c[e_idx]
        buf = P["sl_buf_pct"] / 100.0
        if long:
            sl = min(climax_lo, fext) * (1 - buf); risk = entry - sl
            tp = entry + P["rr_target"] * risk
        else:
            sl = max(climax_lo, fext) * (1 + buf); risk = sl - entry
            tp = entry - P["rr_target"] * risk
        if risk > 0:
            sigs.append({"direction": direction, "entry": entry, "sl": sl, "tp": tp,
                         "anchor": int(k5[i]["open_time"]), "entry_idx": e_idx,
                         "created_at": int(k5[e_idx]["open_time"]) // 1000,
                         "climax_ratio": round(ratio, 2), "move_pct": round(move * 100, 1)})
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
                                rr_target=2.0, decline_bars=10, vol_ma=20,
                                climax_min=3.0, climax_max=12.0, drop_pct=6.0,
                                dryup_window=10, dryup_ratio=0.6, sl_buf_pct=0.3,
                                spans=(7, 14, 30), progress=None):
    """一次拉 days 天 5m,切出 spans 各档统计。now_ms 用于切窗(不传则用最后一根K时间)。"""
    from .backtest import fetch_series
    import time as _t
    t0 = _t.time()
    P = dict(decline_bars=decline_bars, vol_ma=vol_ma, climax_min=climax_min,
             climax_max=climax_max, drop_pct=drop_pct, dryup_window=dryup_window,
             dryup_ratio=dryup_ratio, sl_buf_pct=sl_buf_pct, rr_target=rr_target)
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
