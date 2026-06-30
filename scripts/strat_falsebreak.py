"""假突破跌回 策略(用户 2026-07-01):5m, 前期高点流动性区放量假突破→快速收回→反手。

【流动性的量化定义】
"流动性"= 止损单/突破单聚集的价格区域。前期高点【上方】聚集着:做空者止损(涨过即触发)
+ 突破追多者买单。主力"假突破"= 冲进这片区域扫掉这些单(grab liquidity), 然后反手砸 → 做空。
量化:
  - 前期高点 H = 近 lookback 根的最高高点(近期阻力位)。
  - 流动性强度(加分): 该高点被【测试过≥min_tests次】(等高/双顶, 近lookback内有几根高点落在 H 的 tol% 内)
    → 测试越多, 堆积的止损越多, 流动性越强, 假突破后跌回越狠。
  - 假突破 = 一根K高点 > H(冲进流动性区), 且【放量】(量 ≥ vol_mult×前vol_ma均量)。
  - 跌回 = reclaim_bars 根内有一根【收盘 < H】(突破失败) → 反手做空。
做多镜像: 前期低点下方流动性, 假跌破→收回→做多。

入场=收回那根K收盘; 止损=假突破插针最高(grab极值)上方; 止盈=RR×风险。

【判决·2026-07-01 已证伪】
一年(top50主流币 5m)+全参数扫(falsebreak_opt.py)+train/holdout样本外:
  - 做空基线 train -0.317R / holdout -0.352R, 双段为正的配置=0个, holdout单段为正=0个。
  - 加趋势过滤(仅收<MA200做空): 仍 -0.179R。做多镜像更差(-0.22~-0.27R)。
30天曾见+0.1~0.2R毛边际, 实为该窗口偏空的运气; 一年横跨牛熊后边际消失, 再叠加~0.2R手续费拖累。
结论: 无可交易边际, 不上线。保留代码与优化器作存档与方法论复用。
"""


def _arr(kl):
    return ([float(k["open"]) for k in kl], [float(k["high"]) for k in kl],
            [float(k["low"]) for k in kl], [float(k["close"]) for k in kl],
            [float(k["volume"]) for k in kl])


def detect_falsebreak(kl, direction, P):
    n = len(kl)
    o, h, l, c, v = _arr(kl)
    long = direction == "long"
    lb = P["lookback"]; vma = P["vol_ma"]; mult = P["vol_mult"]
    rb = P["reclaim_bars"]; tol = P["pierce_tol"] / 100.0
    mintests = P["min_tests"]; ttol = P["test_tol"] / 100.0
    out = []
    i = lb + vma
    while i < n - 1:
        # 前期极值(流动性位)= 近lb根的最高/最低(不含当前)
        if long:
            level = min(l[i - lb:i])
        else:
            level = max(h[i - lb:i])
        avgv = sum(v[i - vma:i]) / vma
        if avgv <= 0:
            i += 1; continue
        # i 是"假突破"候选K: 刺穿流动性位 + 放量
        pierced = (l[i] < level * (1 - tol)) if long else (h[i] > level * (1 + tol))
        if not (pierced and v[i] >= mult * avgv):
            i += 1; continue
        # 流动性强度: level 被测试过几次(近lb内有几根极值落在 level 的 ttol 内)
        if long:
            tests = sum(1 for x in range(i - lb, i) if abs(l[x] - level) <= ttol * level)
        else:
            tests = sum(1 for x in range(i - lb, i) if abs(h[x] - level) <= ttol * level)
        if tests < mintests:
            i += 1; continue
        # 跌回/收回: rb根内有一根收盘 收回到 level 内侧
        r = None; grab = l[i] if long else h[i]
        for j in range(i, min(n, i + rb + 1)):
            grab = min(grab, l[j]) if long else max(grab, h[j])
            if (c[j] > level) if long else (c[j] < level):   # 收盘收回 = 假突破成立
                r = j; break
        if r is None:
            i += 1; continue
        entry = c[r]
        buf = P["sl_buf"] / 100.0
        sl = grab * (1 - buf) if long else grab * (1 + buf)
        risk = (entry - sl) if long else (sl - entry)
        if risk <= 0:
            i += 1; continue
        tp = entry + P["rr"] * risk if long else entry - P["rr"] * risk
        out.append({"direction": direction, "entry": entry, "sl": sl, "tp": tp,
                    "entry_idx": r, "created_at": int(kl[r]["open_time"]) // 1000,
                    "vol_ratio": round(v[i] / avgv, 2), "liq_tests": tests,
                    "anchor": int(kl[i]["open_time"])})
        i = r + 1
    return out


def _settle(s, kl):
    s["result"], s["pnl_r"], s["bars_held"] = "open", None, None
    e, sl, tp = s["entry"], s["sl"], s["tp"]
    risk = abs(e - sl)
    if risk <= 0:
        return
    long = s["direction"] == "long"
    for j in range(s["entry_idx"] + 1, len(kl)):
        lo, hi = float(kl[j]["low"]), float(kl[j]["high"])
        if long:
            if lo <= sl: s["result"], s["pnl_r"] = "sl", -1.0
            elif hi >= tp: s["result"], s["pnl_r"] = "tp", (tp - e) / risk
        else:
            if hi >= sl: s["result"], s["pnl_r"] = "sl", -1.0
            elif lo <= tp: s["result"], s["pnl_r"] = "tp", (e - tp) / risk
        if s["result"] != "open":
            s["bars_held"] = j - s["entry_idx"]
            break


DEFAULT = dict(lookback=50, vol_ma=20, vol_mult=2.5, reclaim_bars=3,
               pierce_tol=0.0, min_tests=2, test_tol=0.15, sl_buf=0.1, rr=2.0)


def scan_falsebreak(C):
    out = []
    for sym, k5 in C("5m").items():
        if len(k5) < 120:
            continue
        for d in ("long", "short"):
            for s in detect_falsebreak(k5, d, DEFAULT):
                s["symbol"] = sym
                _settle(s, k5)
                s["strat"] = "falsebreak"
                s["climaxX"] = s.get("vol_ratio")
                out.append(s)
    return out


META = {"falsebreak": {"label": "假突破跌回", "tf": "5m",
        "logic": ["前期高点上方=流动性区(空头止损+突破买单聚集)",
                  "放量假突破(高>前高+放量) → 扫单 → reclaim_bars内收盘跌回前高内 = 假突破成立",
                  "反手做空; 止损=插针最高上方; 止盈=RR2。做多镜像(假跌破前低收回)",
                  f"默认: {DEFAULT}"]}}
DETAIL = {"falsebreak": {"desc": "假突破跌回(流动性扫单反转)",
          "idea": "主力冲进前高上方扫止损(grab liquidity)后反砸, 跌回即做空",
          "updated": "2026-07-01", "code": "scripts/strat_falsebreak.py", "doc": ""}}
SCANS = {"falsebreak": scan_falsebreak}
