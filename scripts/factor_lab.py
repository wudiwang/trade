"""因子实验室(用户 2026-06-23):逐因子优化一个策略。

方法:分析"赢单 vs 亏单"在一组候选特征上的差异 → 找到最能区分的特征+阈值 = 一个因子(过滤器)
→ 在【没参与挑选的 holdout 段】验证它真能提升扣费后期望 → 留住 → 下一轮在残余信号上再找
→ 攒够 N 个因子(默认3)收工。每个因子都可解释(就是一句"只留 X 特征 > 阈值 的信号")。

子命令:
  build   <strat>   生成信号+特征面板, 存 .btcache/feat_<strat>.jsonl(慢, 跑一次)
  iterate <strat>   读特征+已留因子, 找并验证下一个因子, 更新状态(loop每轮调一次)
状态: docs/agents/optimize/<strat>_state.json   审计: docs/agents/optimize/<strat>.md
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import bt_registry as R

if R.ROOT not in sys.path:
    sys.path.insert(0, R.ROOT)   # 让 scan_macro_pullback 里的 from app.engine... 能解析

FEE = 0.00045
HOLDOUT_DAYS = 7
TARGET_FACTORS = 3
MIN_HOLDOUT = 60      # 关键护栏: 加因子后 holdout 子集不得少于此, 否则=过滤到噪音(过拟合)
MIN_TRAIN = 150
OPT_DIR = os.path.join(R.ROOT, "docs", "agents", "optimize")
FEAT_PATH = lambda s: os.path.join(R.CACHE, f"feat_{s}.jsonl")
STATE_PATH = lambda s: os.path.join(OPT_DIR, f"{s}_state.json")

# 候选特征(在入场K计算; 带 _al 的已按交易方向对齐, 越大=越顺势)
FEATURES = ["vol_ratio", "atr_pct", "body_ratio", "mom5_al", "mom20_al",
            "dist_ma20_al", "wick_al", "rsi_room"]


def _sma(v, n, i):
    return sum(v[i - n + 1:i + 1]) / n if i >= n - 1 else None


def _rsi(c, n, i):
    if i < n:
        return 50.0
    g = sum(max(c[j] - c[j - 1], 0) for j in range(i - n + 1, i + 1)) / n
    l = sum(max(c[j - 1] - c[j], 0) for j in range(i - n + 1, i + 1)) / n
    return 100 - 100 / (1 + (g / l if l else 999))


def _atr(kl, n, i):
    if i < n:
        return 0.0
    tr = []
    for j in range(i - n + 1, i + 1):
        h, l = float(kl[j]["high"]), float(kl[j]["low"]); pc = float(kl[j - 1]["close"])
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(tr) / n


def features_at(kl, i, direction):
    """入场K i 的候选特征。dir: long=+1/short=-1 对齐。"""
    d = 1.0 if direction == "long" else -1.0
    o, h, l, c = (float(kl[i]["open"]), float(kl[i]["high"]), float(kl[i]["low"]), float(kl[i]["close"]))
    closes = [float(k["close"]) for k in kl]
    vols = [float(k["volume"]) for k in kl]
    avgv = _sma(vols, 20, i) or 1e-9
    ma20 = _sma(closes, 20, i) or c
    rng = (h - l) or 1e-9
    body = abs(c - o)
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    atr = _atr(kl, 14, i)
    rsi = _rsi(closes, 14, i)
    mom5 = (c - closes[i - 5]) / closes[i - 5] if i >= 5 else 0.0
    mom20 = (c - closes[i - 20]) / closes[i - 20] if i >= 20 else 0.0
    return {
        "vol_ratio": vols[i] / avgv,
        "atr_pct": atr / c if c else 0.0,
        "body_ratio": body / rng,
        "mom5_al": mom5 * d * 100,
        "mom20_al": mom20 * d * 100,
        "dist_ma20_al": (c - ma20) / ma20 * d * 100,
        "wick_al": (lower_wick if direction == "long" else upper_wick) / rng,
        "rsi_room": (100 - rsi) if direction == "long" else rsi,
    }


def cmd_build(strat):
    os.makedirs(OPT_DIR, exist_ok=True)
    C = R.cache_loader(30)
    k5map = C("5m")
    sigs = R.SCANS[strat](C)
    print(f"{strat}: {len(sigs)} 信号, 算特征中…", flush=True)
    rows = []
    cache = {}
    for s in sigs:
        if s.get("result") not in ("tp", "sl"):
            continue
        sym = s["symbol"]; k5 = k5map.get(sym)
        if not k5:
            continue
        if sym not in cache:
            cache[sym] = {int(k["open_time"]) // 1000: idx for idx, k in enumerate(k5)}
        i = cache[sym].get(s["created_at"])
        if i is None or i < 25 or i >= len(k5):
            continue
        f = features_at(k5, i, s["direction"])
        risk = abs(s["entry"] - s["sl"]) or 1e-9
        net_r = s["pnl_r"] - 2 * FEE * s["entry"] / risk
        rows.append({"t": s["created_at"], "sym": sym, "dir": s["direction"],
                     "win": 1 if s["result"] == "tp" else 0, "net_r": net_r, "f": f})
    with open(FEAT_PATH(strat), "w", encoding="utf-8") as fp:
        for r in rows:
            fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"已写 {len(rows)} 条带特征样本 → {os.path.basename(FEAT_PATH(strat))}")


def _apply(rows, factors):
    out = rows
    for fac in factors:
        out = [r for r in out if (r["f"][fac["feat"]] >= fac["thr"] if fac["op"] == ">=" else r["f"][fac["feat"]] <= fac["thr"])]
    return out


def _metrics(rows):
    n = len(rows)
    if not n:
        return {"n": 0, "win": 0.0, "exp": 0.0}
    return {"n": n, "win": round(sum(r["win"] for r in rows) / n * 100, 1),
            "exp": round(sum(r["net_r"] for r in rows) / n, 3)}


def cmd_iterate(strat):
    os.makedirs(OPT_DIR, exist_ok=True)
    rows = [json.loads(x) for x in open(FEAT_PATH(strat), encoding="utf-8")]
    state = json.load(open(STATE_PATH(strat))) if os.path.exists(STATE_PATH(strat)) else {"factors": [], "tried": [], "iter": 0}
    state["iter"] += 1
    if len(state["factors"]) >= TARGET_FACTORS:
        print("DONE: 已达成3个因子, 停止loop。"); state["done"] = True
        json.dump(state, open(STATE_PATH(strat), "w"), ensure_ascii=False, indent=1)
        return state

    ref = max(r["t"] for r in rows)
    cut = ref - HOLDOUT_DAYS * 86400
    kept = state["factors"]
    base = _apply(rows, kept)
    train = [r for r in base if r["t"] < cut]
    hold = [r for r in base if r["t"] >= cut]
    base_hold = _metrics(hold)

    best = None
    for feat in FEATURES:
        if feat in [f["feat"] for f in kept] + state.get("tried_feat", []):
            pass  # 允许同特征不同阈值, 这里仍尝试
        vals = sorted(r["f"][feat] for r in train)
        if len(vals) < 50:
            continue
        for q in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7):
            thr = vals[int(q * len(vals))]
            for op in (">=", "<="):
                fac = {"feat": feat, "op": op, "thr": round(thr, 5)}
                tr = _apply(train, [fac]); ho = _apply(hold, [fac])
                if len(tr) < MIN_TRAIN or len(ho) < MIN_HOLDOUT:   # 样本量护栏: 防过滤到噪音
                    continue
                mtr, mho = _metrics(tr), _metrics(ho)
                # 训练段要提升期望, 且 holdout 段也要提升(样本外验证), 且保留足够样本
                lift_tr = mtr["exp"] - _metrics(train)["exp"]
                lift_ho = mho["exp"] - base_hold["exp"]
                if lift_tr > 0 and lift_ho > 0:
                    score = lift_ho + 0.3 * lift_tr
                    if not best or score > best["score"]:
                        best = {"fac": fac, "score": score, "mtr": mtr, "mho": mho, "lift_ho": round(lift_ho, 3)}

    log = [f"\n## 第{state['iter']}轮 ({time.strftime('%Y-%m-%d %H:%M')})",
           f"基线(已留{len(kept)}因子) holdout: {base_hold}"]
    if best:
        kept.append(best["fac"])
        state["factors"] = kept
        log.append(f"✅ 新增因子: {best['fac']['feat']} {best['fac']['op']} {best['fac']['thr']}")
        log.append(f"   训练段: {best['mtr']}  holdout: {best['mho']}  holdout期望提升 +{best['lift_ho']}R")
        print(f"KEEP 因子#{len(kept)}: {best['fac']['feat']} {best['fac']['op']} {best['fac']['thr']} | holdout {base_hold['exp']}→{best['mho']['exp']}R (n {base_hold['n']}→{best['mho']['n']})")
    else:
        state["tried"].append(state["iter"])
        log.append("⚠ 本轮未找到能在holdout上提升期望的因子(诚实记录, 不硬塞)。")
        print("NO-GAIN: 本轮没找到样本外能提升的因子。")
    json.dump(state, open(STATE_PATH(strat), "w"), ensure_ascii=False, indent=1)
    with open(os.path.join(OPT_DIR, f"{strat}.md"), "a", encoding="utf-8") as fp:
        fp.write("\n".join(log) + "\n")
    state["base_hold"] = base_hold
    return state


# 整夜 campaign: 逐个策略做"3因子优化", 一次调用推进一个未完成的策略。
CAMPAIGN = ["macro_pullback", "smallbig", "reversal", "deepbase", "pullback"]


def cmd_campaign():
    for s in CAMPAIGN:
        st = json.load(open(STATE_PATH(s))) if os.path.exists(STATE_PATH(s)) else None
        if st and st.get("done"):
            continue
        print(f"=== campaign 推进策略: {s} ===", flush=True)
        if not os.path.exists(FEAT_PATH(s)):
            cmd_build(s)
        for _ in range(6):                      # 最多6轮内拿满3因子或停
            r = cmd_iterate(s)
            if r.get("done") or len(r.get("factors", [])) >= TARGET_FACTORS:
                break
        print(f"=== {s} 本轮完成 ===")
        return s
    print("ALL DONE: campaign 全部策略已优化完。")
    return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "iterate", "campaign"])
    ap.add_argument("strat", nargs="?")
    a = ap.parse_args()
    if a.cmd == "build":
        cmd_build(a.strat)
    elif a.cmd == "iterate":
        cmd_iterate(a.strat)
    else:
        cmd_campaign()
