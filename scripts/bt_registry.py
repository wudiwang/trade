"""策略注册表:把已存档的各策略统一接入本地回测/看图器。

每个策略登记:label(中文名) + logic(思路, 看图器侧栏展示) + scan(读缓存跑信号)。
scan 统一返回信号列表,每条带: strat/symbol/direction/created_at/entry/sl/tp/result/pnl_r/anchor(+可选 stage/climaxX/movePct)。
新增策略 → 加一个 META 条目 + 一个 scan 函数即可,看图器自动多出一类。
"""
import glob
import importlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".btcache")
_MODS = {}
_CACHE_MEM = {}


def load_strat(name):
    """按包导入 app.engine.strat_<name>,使其内部相对导入(from .chan_bi)正常解析。"""
    if name in _MODS:
        return _MODS[name]
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    m = importlib.import_module(f"app.engine.strat_{name}")
    _MODS[name] = m
    return m


def cache_loader(days):
    """返回 C(tf)->{symbol: klines},读 .btcache 缓存(覆盖式, 文件名不带日期), 带记忆。"""
    def C(tf):
        key = (tf, days)
        if key in _CACHE_MEM:
            return _CACHE_MEM[key]
        tag = f"_{tf}_{days}d.json"
        out = {}
        for f in glob.glob(os.path.join(CACHE, f"*{tag}")):
            sym = os.path.basename(f)[: -len(tag)]
            try:
                out[sym] = json.load(open(f))
            except Exception:
                pass
        _CACHE_MEM[key] = out
        return out
    return C


# ------------------------- 各策略 scan -------------------------
def scan_smallbig(C):
    sb = load_strat("smallbig")
    P = dict(decline_bars=8, baseline_ma=20, sustain_mult=2.0, elevated_mult=1.5,
             sustain_bars_min=3, drop_pct=6.0, dryup_ratio=0.8, rebound_within=4,
             sl_buf_pct=0.3, rr_target=2.0)
    out = []
    for sym, k5 in C("5m").items():
        if len(k5) < 80:
            continue
        for d in ("long", "short"):
            for s in sb.detect_small_to_big(k5, d, P):
                s["symbol"] = sym
                sb._settle(s, k5)
                s["strat"] = "smallbig"
                s["climaxX"] = s.get("leg_vol_x")     # 恐慌段持续放量倍数
                s["movePct"] = s.get("move_pct")
                out.append(s)
    return out


def scan_pullback(C):
    pb = load_strat("pullback")
    out = []
    for sym, k5 in C("5m").items():
        if len(k5) < 80:
            continue
        for s in pb._walk_symbol(sym, k5, 5, 0.5, 10, 2.0, True, 3):
            s["strat"] = "pullback"
            s["anchor"] = s.get("struct_anchor")
            out.append(s)
    return out


def scan_deepbase(C):
    db = load_strat("deepbase")
    P = dict(w1=48, drop_min=0.30, pos_max=0.5, ma_1h=50, cap_lookback=30, cap_vol_mult=3.0,
             hold_min=10, contract_ratio=0.7, conv_eps=0.02, rr_target=2.0)
    k15, k1h = C("15m"), C("1h")
    out = []
    for sym in k15:
        if len(k15[sym]) < 80 or len(k1h.get(sym, [])) < 60:
            continue
        for s in db._walk(sym, k15[sym], k1h[sym], P):
            s["strat"] = "deepbase"
            out.append(s)
    return out


def scan_reversal(C):
    rv = load_strat("reversal")
    P = dict(w1=48, drop_min=0.30, pos_max=0.5, ma_1h=50, cap_lookback=30, vol_ma=20,
             climax_mult=3.0, reclaim_bars=6, hold_bars=4, hold_tol_pct=3.0,
             platform_window=20, sl_buf_pct=0.3, rr_target=2.0)
    k5, k1h = C("5m"), C("1h")
    out = []
    for sym in k5:
        if len(k5[sym]) < 80 or len(k1h.get(sym, [])) < 60:
            continue
        for s in rv._walk(sym, k5[sym], k1h[sym], ["long", "short"], P):
            s["strat"] = "reversal"
            out.append(s)
    return out


def _macro_params():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from app.config import get_config
    cfg = get_config()
    keys = (
        "enabled", "exclusive", "timeframes", "structure_tf", "trigger_tf", "context_tf",
        "vol_ma", "vol_mult", "lookback", "reclaim_bars", "reclaim_tolerance_pct",
        "reclaim_body_pct", "wyckoff_fractal_window",
        "min_leg_pct", "second_tolerance_pct", "stop_buffer_pct", "cooldown_bars",
        "max_signal_bars_after_second", "max_entry_leg_ratio", "min_effective_bars_between",
        "min_rr", "tp_rr_long", "tp_rr_short", "tp_lookback", "vp_bins",
    )
    params = {k: cfg.get(f"macro_pullback.{k}") for k in keys}
    params = {k: v for k, v in params.items() if v is not None}
    params["account_equity"] = cfg.get("risk.account_equity", 1000)
    params["risk_pct"] = cfg.get("risk.risk_pct", 0.5)
    params["tf"] = "5m"
    return params


def _settle_signal(s: dict, k5: list):
    s["result"], s["pnl_r"], s["bars_held"] = "open", None, None
    entry, sl, tp = float(s["entry"]), float(s["sl"]), float(s["tp"])
    risk = abs(entry - sl)
    if risk <= 0:
        return s
    start_ms = int(s.get("entry_time") or s["created_at"] * 1000)
    start = 0
    while start < len(k5) and int(k5[start]["open_time"]) < start_ms:
        start += 1
    for j in range(start, len(k5)):
        k = k5[j]
        lo, hi = float(k["low"]), float(k["high"])
        if s["direction"] == "long":
            if lo <= sl:
                s["result"], s["pnl_r"] = "sl", -1.0
            elif hi >= tp:
                s["result"], s["pnl_r"] = "tp", round((tp - entry) / risk, 3)
        else:
            if hi >= sl:
                s["result"], s["pnl_r"] = "sl", -1.0
            elif lo <= tp:
                s["result"], s["pnl_r"] = "tp", round((entry - tp) / risk, 3)
        if s["result"] != "open":
            s["bars_held"] = j - start
            break
    return s


def scan_macro_pullback(C):
    from app.engine.chan import find_fractals, merge_klines
    from app.engine.macro_pullback import (
        _body_reclaim_level, _effective_bar_count, _entry_near_second, _f,
        _tp_for, _vol_ratio,
    )
    params = _macro_params()
    lookback = int(params.get("lookback", 20))
    vol_ma = int(params.get("vol_ma", 20))
    vol_mult = float(params.get("vol_mult", 3.0))
    reclaim_bars = int(params.get("reclaim_bars", 4))
    fractal_window = int(params.get("wyckoff_fractal_window", 5))
    body_pct = float(params.get("reclaim_body_pct", 80)) / 100.0
    min_leg = float(params.get("min_leg_pct", 0.8)) / 100.0
    tol = float(params.get("second_tolerance_pct", 0.2)) / 100.0
    min_bars = int(params.get("min_effective_bars_between", 5))
    stop_buf = float(params.get("stop_buffer_pct", 0.0)) / 100.0
    out = []
    for sym, k5 in C("5m").items():
        if len(k5) < max(80, vol_ma + lookback + 10):
            continue
        fractals = find_fractals(k5, merge_klines(k5))
        chan_bottoms = [int(f.extreme_src_idx) for f in fractals if f.kind == "bottom"]
        chan_tops = [int(f.extreme_src_idx) for f in fractals if f.kind == "top"]
        fired = set()
        processed_l1 = set()
        processed_h1 = set()

        def emit(direction, first, second):
            label = "second_buy" if direction == "long" else "second_sell"
            entry_idx = second["entry_idx"]
            entry = _f(k5[entry_idx], "close")
            sl = second["L2"] * (1 - stop_buf) if direction == "long" else second["H2"] * (1 + stop_buf)
            if direction == "long" and sl >= entry:
                return
            if direction == "short" and sl <= entry:
                return
            if not _entry_near_second(direction, k5[:entry_idx + 1], second, entry, sl, params):
                return
            tp, rr = _tp_for(k5[:entry_idx + 1], direction, entry, sl, params)
            anchor = second.get("L2_time") or second.get("H2_time")
            key = (direction, anchor)
            if key in fired:
                return
            fired.add(key)
            row = {
                "strat": "macro_pullback", "symbol": sym, "tf": "5m",
                "direction": direction, "type": label, "stage": label,
                "created_at": int(second["entry_time"]) // 1000,
                "entry_time": int(second["entry_time"]), "entry": round(entry, 8),
                "sl": round(sl, 8), "tp": round(tp, 8), "rr": round(rr, 2),
                "vol_ratio": float(first["vol_ratio"]), "anchor": anchor,
                "climaxX": float(first["vol_ratio"]),
                "extra": {"path": "macro_chan_pullback", "type": label,
                          "wyckoff": first, "structure": second},
            }
            _settle_signal(row, k5)
            out.append(row)

        def long_seconds(first):
            l1_idx = int(first["idx"])
            l1 = _f(k5[l1_idx], "low")
            for l2_idx in chan_bottoms:
                if l2_idx < l1_idx + 3:
                    continue
                if l2_idx > l1_idx + lookback:
                    break
                if _f(k5[l2_idx], "low") < l1 * (1 - tol):
                    continue
                leg_high_idx = max(range(l1_idx + 1, l2_idx + 1), key=lambda x: _f(k5[x], "high"))
                leg_high = _f(k5[leg_high_idx], "high")
                if _effective_bar_count(k5, l1_idx, leg_high_idx) < min_bars:
                    continue
                if _effective_bar_count(k5, leg_high_idx, l2_idx) < min_bars:
                    continue
                if (leg_high - l1) / max(l1, 1e-12) < min_leg:
                    continue
                l2 = _f(k5[l2_idx], "low")
                right_idx, stall_idx, entry_idx = l2_idx + 1, l2_idx + 2, l2_idx + 3
                if entry_idx >= len(k5):
                    continue
                if _f(k5[stall_idx], "close") <= _f(k5[right_idx], "high"):
                    continue
                if any(_f(k, "low") < l1 for k in k5[l1_idx + 1: entry_idx + 1]):
                    continue
                yield {"L1": l1, "H1": leg_high, "L2": l2,
                       "L1_time": int(k5[l1_idx]["open_time"]), "L2_time": int(k5[l2_idx]["open_time"]),
                       "L1_idx": l1_idx, "H1_idx": leg_high_idx, "L2_idx": l2_idx,
                       "stall_idx": stall_idx, "stall_time": int(k5[stall_idx]["open_time"]),
                       "entry_idx": entry_idx, "entry_time": int(k5[entry_idx]["open_time"])}

        def short_seconds(first):
            h1_idx = int(first["idx"])
            h1 = _f(k5[h1_idx], "high")
            for h2_idx in chan_tops:
                if h2_idx < h1_idx + 3:
                    continue
                if h2_idx > h1_idx + lookback:
                    break
                if _f(k5[h2_idx], "high") > h1 * (1 + tol):
                    continue
                leg_low_idx = min(range(h1_idx + 1, h2_idx + 1), key=lambda x: _f(k5[x], "low"))
                leg_low = _f(k5[leg_low_idx], "low")
                if _effective_bar_count(k5, h1_idx, leg_low_idx) < min_bars:
                    continue
                if _effective_bar_count(k5, leg_low_idx, h2_idx) < min_bars:
                    continue
                if (h1 - leg_low) / max(h1, 1e-12) < min_leg:
                    continue
                h2 = _f(k5[h2_idx], "high")
                right_idx, stall_idx, entry_idx = h2_idx + 1, h2_idx + 2, h2_idx + 3
                if entry_idx >= len(k5):
                    continue
                if _f(k5[stall_idx], "close") >= _f(k5[right_idx], "low"):
                    continue
                if any(_f(k, "high") > h1 for k in k5[h1_idx + 1: entry_idx + 1]):
                    continue
                yield {"H1": h1, "L1": leg_low, "H2": h2,
                       "H1_time": int(k5[h1_idx]["open_time"]), "H2_time": int(k5[h2_idx]["open_time"]),
                       "H1_idx": h1_idx, "L1_idx": leg_low_idx, "H2_idx": h2_idx,
                       "stall_idx": stall_idx, "stall_time": int(k5[stall_idx]["open_time"]),
                       "entry_idx": entry_idx, "entry_time": int(k5[entry_idx]["open_time"])}

        for i in range(vol_ma, len(k5) - 4):
            start = max(0, i - lookback)
            if i - start < 3:
                continue
            vr = _vol_ratio(k5, i, vol_ma)
            if vr < vol_mult:
                continue
            prior_low = min(_f(k, "low") for k in k5[start:i])
            if _f(k5[i], "low") < prior_low:
                reclaim_level = _body_reclaim_level(k5[i], "long", body_pct)
                reclaim_end = min(len(k5) - 1, i + reclaim_bars)
                reclaimed_at = next((j for j in range(i + 1, reclaim_end + 1)
                                     if _f(k5[j], "close") >= reclaim_level), None)
                if reclaimed_at is not None:
                    near = [x for x in chan_bottoms if abs(x - i) <= fractal_window and reclaimed_at >= x]
                    if near:
                        l1_idx = min(near, key=lambda x: _f(k5[x], "low"))
                        if l1_idx in processed_l1:
                            continue
                        processed_l1.add(l1_idx)
                        first = {"kind": "spring", "idx": l1_idx, "sweep_idx": i,
                                 "reclaimed_at": reclaimed_at, "level": prior_low,
                                 "reclaim_level": round(reclaim_level, 8), "vol_ratio": round(vr, 2)}
                        for second in long_seconds(first):
                            emit("long", first, second)
            prior_high = max(_f(k, "high") for k in k5[start:i])
            if _f(k5[i], "high") > prior_high:
                reclaim_level = _body_reclaim_level(k5[i], "short", body_pct)
                reclaim_end = min(len(k5) - 1, i + reclaim_bars)
                reclaimed_at = next((j for j in range(i + 1, reclaim_end + 1)
                                     if _f(k5[j], "close") <= reclaim_level), None)
                if reclaimed_at is not None:
                    near = [x for x in chan_tops if abs(x - i) <= fractal_window and reclaimed_at >= x]
                    if near:
                        h1_idx = max(near, key=lambda x: _f(k5[x], "high"))
                        if h1_idx in processed_h1:
                            continue
                        processed_h1.add(h1_idx)
                        first = {"kind": "utad", "idx": h1_idx, "sweep_idx": i,
                                 "reclaimed_at": reclaimed_at, "level": prior_high,
                                 "reclaim_level": round(reclaim_level, 8), "vol_ratio": round(vr, 2)}
                        for second in short_seconds(first):
                            emit("short", first, second)
    return out


META = {
    "smallbig": {
        "label": "小转大", "tf": "5m",
        "logic": [
            "纯5m·持续放量恐慌后的第一根反弹买点。",
            "① 急跌段(默认8根):段均量≥基线2x且段内≥3根明显放量 = 持续放量(非单根spike)",
            "② 急跌幅度≥6%,段末为恐慌低点(锚)",
            "③ 立即缩量:低点后量能马上萎缩(≤恐慌段均量0.8x)",
            "④ 第一根反弹K(收阳, 4根内)= 买/卖点入场(不等底分型)",
            "止损=恐慌低点;止盈=RR2。锚标在恐慌低,箭头标在反弹入场K。",
        ]},
    "pullback": {
        "label": "浅回调二买/二卖", "tf": "5m",
        "logic": [
            "5m三笔浅回调。",
            "下跌笔 → 反弹笔 → 再次下跌但不破新低(更高的低点)",
            "回调幅度 ≤ 50%,且入场底分型前2根放量≥2x",
            "做多:底分型免停顿即买;做空:顶分型需停顿才触发",
            "止损=分型极值;止盈=反弹/回调那一笔的端点。",
        ]},
    "deepbase": {
        "label": "深跌后企稳", "tf": "15m+1h",
        "logic": [
            "多周期底部识别。",
            "1h:跌幅≥30% + 现价处于区间下部 + 趋势向下",
            "15m:恐慌放量低点 + 之后≥10根不创新低 + 波动收缩 + 短均收敛走平",
            "命中→ 做多止损=企稳区间低;做空止损=企稳区间高;止盈RR2。",
        ]},
    "reversal": {
        "label": "反转战法", "tf": "5m+1h",
        "logic": [
            "深跌/深涨背景下的弹簧反转,二段建仓。",
            "1h深跌(多)/深涨(空)→ 爆量标志K插穿前低/前高",
            "收盘收回标志K起跌位 → 横盘企稳 = Entry-1(轻仓)",
            "平台内出现底/顶分型 = Entry-2(加仓);收盘越过针尖即作废。",
            "止损=针尖/分型;止盈=RR2。",
        ]},
    "macro_pullback": {
        "label": "反转战法", "tf": "5m",
        "logic": [
            "本地缓存回测默认多空双向扫描，不访问服务器。",
            "爆量扫低/扫高 K 可以在真正 L1/H1 分型前后 5 根内。",
            "L1/H1 必须是真正的缠论合并 K 底/顶分型，后续不能被新低/新高破坏。",
            "L2/H2 也必须是真正的缠论合并 K 底/顶分型；确认后等停顿 K，真实入场在停顿后的下一根 K。",
        ]},
}

SCANS = {"smallbig": scan_smallbig, "pullback": scan_pullback,
         "deepbase": scan_deepbase, "reversal": scan_reversal,
         "macro_pullback": scan_macro_pullback}


def score(strat, days=30, spans=(7, 14, 30), fee_pct_side=0.045, n_samples=8):
    """单策略成绩单:1周/2周/1月 × 多空,含扣费后净期望 + 代表性样本。供策略研究Agent判断。"""
    sigs, _ = scan_all(days, [strat])
    ref = max((s["created_at"] for s in sigs), default=0)

    def bucket(rows):
        out = {}
        for s in rows:
            d = s["direction"]
            b = out.setdefault(d, {"n": 0, "closed": 0, "wins": 0, "r": 0.0, "rnet": 0.0})
            b["n"] += 1
            if s.get("result") in ("tp", "sl"):
                b["closed"] += 1
                b["r"] += s["pnl_r"]
                risk = abs(s["entry"] - s["sl"]) or 1e-9
                cost = 2 * (fee_pct_side / 100.0) * s["entry"] / risk     # 往返手续费(R)
                b["rnet"] += s["pnl_r"] - cost
                if s["result"] == "tp":
                    b["wins"] += 1
        for b in out.values():
            c = b["closed"] or 1
            b["win_rate"] = round(b["wins"] / c * 100, 1)
            b["exp_r"] = round(b["r"] / c, 3)
            b["exp_net_r"] = round(b["rnet"] / c, 3)
            b["total_r"] = round(b["r"], 1)
        return out

    by = {}
    for sp in spans:
        cut = ref - sp * 86400
        by[f"{sp}d"] = bucket([s for s in sigs if s["created_at"] >= cut])
    closed = [s for s in sigs if s.get("result") in ("tp", "sl")]
    wins = [s for s in closed if s["result"] == "tp"][:n_samples // 2]
    losses = [s for s in closed if s["result"] == "sl"][:n_samples - len(wins)]
    samp = [{"sym": s["symbol"], "dir": s["direction"], "t": s["created_at"],
             "entry": s["entry"], "sl": s["sl"], "tp": s["tp"], "result": s["result"],
             "pnl_r": s["pnl_r"], "climaxX": s.get("climaxX"), "movePct": s.get("movePct")}
            for s in wins + losses]
    return {"strat": strat, "label": META.get(strat, {}).get("label", strat),
            "days": days, "fee_pct_side": fee_pct_side, "n_sig": len(sigs),
            "by_span": by, "samples": samp}


def scan_all(days, strats=None):
    """跑选定策略(默认全部),返回 (signals, stats_by_strat)。"""
    C = cache_loader(days)
    names = strats or list(SCANS)
    sigs = []
    stats = {}
    for n in names:
        try:
            rows = SCANS[n](C)
        except Exception as e:
            rows = []
            stats[n] = {"error": str(e)}
        for s in rows:
            s.setdefault("strat", n)
        sigs.extend(rows)
        closed = [s for s in rows if s.get("result") in ("tp", "sl")]
        wins = sum(1 for s in closed if s["result"] == "tp")
        stats.setdefault(n, {})
        stats[n].update(n_sig=len(rows), n_closed=len(closed),
                        win_rate=round(wins / len(closed) * 100, 1) if closed else 0.0)
    sigs.sort(key=lambda s: s["created_at"])
    for i, s in enumerate(sigs):
        s["id"] = i
    return sigs, stats
