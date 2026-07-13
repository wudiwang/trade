"""赢家画像(用户 2026-07-12): macrofvg 盈利信号 vs 亏损信号, 逐特征找差异 + 折半验证。

防自欺: 单纯"挑出赢家找共同点"必然过拟合。这里每个特征都做两件事:
  ① 全样本: 赢/亏两组的均值差 + t值
  ② 折半验证: 前半月 vs 后半月, 该特征的"高分组"净R是否都为正(不都为正=不可信)

用法: .venv/Scripts/python scripts/macrofvg_profile.py --dir long
"""
import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import bt_registry as R


def feats(s):
    """从信号里抽出可能有解释力的特征(全部是入场时点就已知的, 无未来函数)。"""
    ex = s.get("extra") or {}
    st, fv, wy = ex.get("structure") or {}, ex.get("fvg") or {}, ex.get("wyckoff") or {}
    long = s["direction"] == "long"
    p1 = st.get("L1") if long else st.get("H1")          # 一买/一卖极值
    leg = st.get("H1") if long else st.get("L1")          # 一笔的另一端
    p2 = st.get("L2") if long else st.get("H2")           # 二买/二卖极值
    if not (p1 and leg and p2) or not fv:
        return None
    leg_abs = abs(leg - p1)
    if leg_abs <= 0:
        return None
    gap = fv["hi"] - fv["lo"]
    entry, sl = s["entry"], s["sl"]
    f = {
        "爆量倍数": wy.get("vol_ratio") or s.get("vol_ratio") or 0,
        "一笔涨幅%": leg_abs / p1 * 100,
        "回调深度%": abs(leg - p2) / leg_abs * 100,        # 0=没回调, 100=回到起点
        "二买高于一买%": abs(p2 - p1) / p1 * 100,
        "FVG宽度%": gap / ((fv["hi"] + fv["lo"]) / 2) * 100,
        "吃进FVG%": (fv["hi"] - p2) / gap * 100 if long else (p2 - fv["lo"]) / gap * 100,  # 0=只碰上沿,100=踩到下沿
        "FVG在笔中位置%": (((fv["hi"] + fv["lo"]) / 2) - p1) / leg_abs * 100 * (1 if long else -1),
        "一买到二买K数": (st.get("L2_idx", 0) - st.get("L1_idx", 0)) if long else (st.get("H2_idx", 0) - st.get("H1_idx", 0)),
        "入场脱离%": (entry - p2) / leg_abs * 100 if long else (p2 - entry) / leg_abs * 100,
        "止损距离%": abs(entry - sl) / entry * 100,
        "小时(UTC)": time.gmtime(s["created_at"]).tm_hour,
    }
    return f


def tstat(a, b):
    if len(a) < 5 or len(b) < 5:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return (ma - mb) / se if se > 0 else 0.0


def netr(rows):
    c = [s for s in rows if s["result"] in ("tp", "sl")]
    return (sum(s["net_r"] for s in c) / len(c), len(c)) if c else (0.0, 0)


def tercile_report(rows, key, half_cut):
    """按特征三等分, 看每一档的净R; 再看最优档在前/后半月是否都为正。"""
    vals = sorted(s["_f"][key] for s in rows)
    q1, q2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    buckets = {"低": [], "中": [], "高": []}
    for s in rows:
        v = s["_f"][key]
        buckets["低" if v <= q1 else ("高" if v > q2 else "中")].append(s)
    out = {}
    for name, rs in buckets.items():
        m, n = netr(rs)
        h1 = netr([s for s in rs if s["created_at"] < half_cut])
        h2 = netr([s for s in rs if s["created_at"] >= half_cut])
        out[name] = dict(m=m, n=n, h1=h1[0], h1n=h1[1], h2=h2[0], h2n=h2[1])
    return q1, q2, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--strat", default="macrofvg")
    ap.add_argument("--dir", default="long", choices=["long", "short", "both"])
    a = ap.parse_args()

    rows = json.load(open(os.path.join(R.CACHE, f"sig_{a.strat}_{a.days}d.json")))
    rows = [s for s in rows if s.get("result") in ("tp", "sl")]
    if a.dir != "both":
        rows = [s for s in rows if s["direction"] == a.dir]
    for s in rows:
        s["_f"] = feats(s)
        if "net_r" not in s and s.get("pnl_r") is not None:      # bt_scan 写的JSON没net_r, 现算
            risk = abs(s["entry"] - s["sl"]) or 1e-9
            s["net_r"] = s["pnl_r"] - 2 * 0.00045 * s["entry"] / risk
    rows = [s for s in rows if s["_f"]]
    win = [s for s in rows if s["result"] == "tp"]
    lose = [s for s in rows if s["result"] == "sl"]
    ts = sorted(s["created_at"] for s in rows)
    half_cut = ts[len(ts) // 2]
    print(f"[{a.strat} · {a.dir}] 已结 {len(rows)} 单: 盈 {len(win)} / 亏 {len(lose)} "
          f"(胜率 {len(win)/len(rows)*100:.1f}%)  折半分界 {time.strftime('%m-%d', time.localtime(half_cut))}\n")

    keys = list(rows[0]["_f"])
    print("① 赢家 vs 输家 · 各特征均值差")
    print(f"  {'特征':<16}{'赢家':>9}{'输家':>9}{'t值':>8}   显著?")
    scored = []
    for k in keys:
        wv = [s["_f"][k] for s in win]
        lv = [s["_f"][k] for s in lose]
        t = tstat(wv, lv)
        scored.append((abs(t), k, t))
        mark = "★" if abs(t) > 2.5 else ("·" if abs(t) > 1.8 else "")
        print(f"  {k:<16}{sum(wv)/len(wv):>9.2f}{sum(lv)/len(lv):>9.2f}{t:>8.2f}   {mark}")
    print("  (11个特征全是噪音时, 也会有约1个|t|>1.8 —— 所以下面折半验证才是关键)\n")

    print("② 折半验证 · 每个特征按三等分, 看各档扣费后净R(前半月 / 后半月)")
    for _, k, t in sorted(scored, reverse=True)[:5]:
        q1, q2, b = tercile_report(rows, k, half_cut)
        print(f"  【{k}】 分界 {q1:.2f} / {q2:.2f}   (t={t:+.2f})")
        for name in ("低", "中", "高"):
            d = b[name]
            ok = "✓两半都正" if (d["h1"] > 0 and d["h2"] > 0) else ("✗前后不一致" if d["m"] > 0 else "")
            print(f"    {name}档 n={d['n']:>3}  净R={d['m']:+.3f}   前半{d['h1']:+.3f}(n={d['h1n']}) "
                  f"后半{d['h2']:+.3f}(n={d['h2n']})  {ok}")
        print()

    print("③ 币种维度 · 出现≥6次的币, 净R排序(只看极端两头)")
    bysym = {}
    for s in rows:
        bysym.setdefault(s["symbol"], []).append(s)
    tab = [(netr(v)[0], k, len(v)) for k, v in bysym.items() if len(v) >= 6]
    tab.sort(reverse=True)
    for m, k, n in tab[:5]:
        print(f"    {k:<16} n={n:>3}  净R={m:+.3f}")
    print("    ...")
    for m, k, n in tab[-5:]:
        print(f"    {k:<16} n={n:>3}  净R={m:+.3f}")


if __name__ == "__main__":
    main()
