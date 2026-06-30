"""假突破跌回 · 做空侧 参数优化(用户 2026-07-01)。

只做空(做多镜像已证伪)。一次性生成"最宽松"候选事件并记录特征, 再纯Python扫阈值:
  特征: vol_ratio(放量倍数), liq_tests(流动性测试次数), reclaim_used(几根内跌回), pierce_pct(插针深度%)
  对每个事件在 rr∈{1.5,2,3} 各结算一次 → 扫(vol_mult,min_tests,reclaim_max,rr)只是选子集求净期望。
时间切分 train(前70%)/holdout(后30%) 做样本外验证, 防过拟合。

用法: .venv/Scripts/python scripts/falsebreak_opt.py --days 365
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import bt_registry as R

FEE = 0.00045
RRS = (1.5, 2.0, 3.0)
LOOKBACK, VOL_MA, GEN_VMULT, GEN_RECLAIM, GEN_TESTS, TTOL, SLBUF = 50, 20, 1.5, 8, 1, 0.15, 0.1


def gen_events(kl):
    """做空候选: 高>前高+放量(>=GEN_VMULT) 且 GEN_RECLAIM内收盘跌回。记录特征+各rr净R。"""
    n = len(kl)
    h = [float(k["high"]) for k in kl]; l = [float(k["low"]) for k in kl]
    c = [float(k["close"]) for k in kl]; v = [float(k["volume"]) for k in kl]
    ot = [int(k["open_time"]) for k in kl]
    out = []
    i = LOOKBACK + VOL_MA
    while i < n - 1:
        level = max(h[i - LOOKBACK:i])
        avgv = sum(v[i - VOL_MA:i]) / VOL_MA
        if avgv <= 0 or not (h[i] > level and v[i] >= GEN_VMULT * avgv):
            i += 1; continue
        tests = sum(1 for x in range(i - LOOKBACK, i) if abs(h[x] - level) <= TTOL * level)
        r = None; grab = h[i]
        for j in range(i, min(n, i + GEN_RECLAIM + 1)):
            grab = max(grab, h[j])
            if c[j] < level:
                r = j; break
        if r is None:
            i += 1; continue
        entry = c[r]; sl = grab * (1 + SLBUF / 100.0); risk = sl - entry
        if risk <= 0:
            i += 1; continue
        fee_r = 2 * FEE * entry / risk
        # 各rr结算
        nets = {}
        for rr in RRS:
            tp = entry - rr * risk; res = None
            for j in range(r + 1, min(n, r + 200)):
                lo, hi = float(kl[j]["low"]), float(kl[j]["high"])
                if hi >= sl:
                    res = -1.0 - fee_r; break
                if lo <= tp:
                    res = rr - fee_r; break
            nets[rr] = res
        out.append({"t": ot[r] // 1000, "vol_ratio": round(v[i] / avgv, 2), "tests": tests,
                    "reclaim": r - i, "pierce_pct": round((grab / level - 1) * 100, 3), "nets": nets})
        i = r + 1
    return out


def stats(ev, rr):
    cl = [e["nets"][rr] for e in ev if e["nets"].get(rr) is not None]
    if not cl:
        return (0, None, None)
    wins = sum(1 for x in cl if x > 0)
    return (len(cl), round(wins / len(cl) * 100, 1), round(sum(cl) / len(cl), 4))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--min-holdout", type=int, default=80)
    a = ap.parse_args()
    C = R.cache_loader(a.days)
    k5 = C("5m")
    print(f"加载 {len(k5)} 币 5m({a.days}天). 生成候选事件…", flush=True)
    ev = []
    for sym, kl in k5.items():
        if len(kl) < 200:
            continue
        ev.extend(gen_events(kl))
    if not ev:
        print("无事件"); return
    ev.sort(key=lambda e: e["t"])
    cut = ev[int(len(ev) * 0.70)]["t"]
    tr = [e for e in ev if e["t"] < cut]; ho = [e for e in ev if e["t"] >= cut]
    print(f"候选事件 {len(ev)} (train {len(tr)} / holdout {len(ho)}), 切分点 ts={cut}\n", flush=True)

    # 基线(策略当前默认: vmult2.5 tests2 reclaim3 rr2)
    def sel(ev, vm, mt, rmax):
        return [e for e in ev if e["vol_ratio"] >= vm and e["tests"] >= mt and e["reclaim"] <= rmax]
    print("=== 基线(当前默认 vmult2.5/tests2/reclaim3/rr2) ===")
    for tag, d in (("train", tr), ("holdout", ho)):
        n, wr, ex = stats(sel(d, 2.5, 2, 3), 2.0)
        print(f"  {tag}: n={n} 胜率{wr}% 净期望{ex}R")

    # 扫参
    grid = []
    for vm in (2.0, 2.5, 3.0, 4.0, 5.0):
        for mt in (1, 2, 3):
            for rmax in (1, 2, 3):
                for rr in RRS:
                    s_tr = sel(tr, vm, mt, rmax); s_ho = sel(ho, vm, mt, rmax)
                    ntr, wtr, etr = stats(s_tr, rr); nho, who, eho = stats(s_ho, rr)
                    if etr is None or eho is None or ntr < 150 or nho < a.min_holdout:
                        continue
                    grid.append((vm, mt, rmax, rr, ntr, wtr, etr, nho, who, eho))
    # 要求 train+holdout 都为正, 按 holdout 净期望排序
    good = [g for g in grid if g[6] > 0 and g[9] > 0]
    good.sort(key=lambda g: -g[9])
    print(f"\n=== 双段(train&holdout)净期望均为正的配置: {len(good)}个 ===")
    print(f"{'vmult':>5}{'tests':>6}{'recl':>5}{'rr':>5} | {'train_n':>8}{'tr胜率':>7}{'tr净R':>8} | {'ho_n':>6}{'ho胜率':>7}{'ho净R':>8}")
    for g in good[:20]:
        vm, mt, rmax, rr, ntr, wtr, etr, nho, who, eho = g
        print(f"{vm:>5}{mt:>6}{rmax:>5}{rr:>5} | {ntr:>8}{wtr:>6}%{etr:>+8} | {nho:>6}{who:>6}%{eho:>+8}")
    if not good:
        print("  无 — 没有任何配置在两段都为正(说明边际不稳健/被费吃光)")
        # 退一步: 只看holdout最优(即便train不一定正)
        ho_only = sorted([g for g in grid if g[9] > 0], key=lambda g: -g[9])[:10]
        print("\n  holdout净期望为正的前10(train不限):")
        for g in ho_only:
            vm, mt, rmax, rr, ntr, wtr, etr, nho, who, eho = g
            print(f"{vm:>5}{mt:>6}{rmax:>5}{rr:>5} | tr净{etr:+} (n{ntr}) | ho净{eho:+} (n{nho}, 胜{who}%)")


if __name__ == "__main__":
    main()
