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
    """返回 C(tf)->{symbol: klines},读 .btcache 当日缓存,带记忆。"""
    date = time.strftime("%Y%m%d")

    def C(tf):
        key = (tf, days, date)
        if key in _CACHE_MEM:
            return _CACHE_MEM[key]
        tag = f"_{tf}_{days}d_{date}.json"
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
    P = dict(decline_bars=10, vol_ma=20, climax_min=3.0, climax_max=12.0, drop_pct=6.0,
             dryup_window=10, dryup_ratio=0.6, sl_buf_pct=0.3, rr_target=2.0)
    out = []
    for sym, k5 in C("5m").items():
        if len(k5) < 80:
            continue
        for d in ("long", "short"):
            for s in sb.detect_small_to_big(k5, d, P):
                s["symbol"] = sym
                sb._settle(s, k5)
                s["strat"] = "smallbig"
                s["climaxX"] = s.get("climax_ratio")
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


META = {
    "smallbig": {
        "label": "小转大", "tf": "5m",
        "logic": [
            "纯5m量能高潮反转。",
            "① 急跌段:幅度特别大(≥6%)且下跌中量能不断放大",
            "② 巨量高潮:最后一根下跌K 量 = 前20均量的 3~12 倍(明显大但不夸张)",
            "③ 缩量企稳:高潮后量能萎缩、不破高潮低点",
            "④ 5m底分型:企稳区出现底分型 → 买入(做空为镜像)",
            "止损=高潮/分型低点;止盈=RR2。",
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
}

SCANS = {"smallbig": scan_smallbig, "pullback": scan_pullback,
         "deepbase": scan_deepbase, "reversal": scan_reversal}


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
