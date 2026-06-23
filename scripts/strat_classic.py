"""经典战法批量策略(用户 2026-06-23:市面常见20种经典战法挂上自测平台)。

每个策略 = detect_<name>(kl, P) → 信号列表(纯5m, 事件触发, 多空对称)。
统一口径:入场=触发K收盘;止损=入场∓ atr_mult×ATR;止盈=入场± rr×风险。
参数都在 P 里,供"回测Agent"调参。配 CLASSIC_SCANS/META/DETAIL 挂进注册表。

第一批(6):ma_cross / macd_cross / donchian / boll_break / rsi_revert / boll_revert
"""

# ------------------------- 指标 -------------------------
def _arr(kl, key):
    return [float(k[key]) for k in kl]


def _sma(v, n):
    out = [None] * len(v)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def _ema(v, n):
    out = [None] * len(v)
    k = 2.0 / (n + 1)
    e = None
    for i, x in enumerate(v):
        e = x if e is None else (x - e) * k + e
        out[i] = e
    return out


def _atr(kl, n):
    tr = [0.0] * len(kl)
    for i in range(1, len(kl)):
        h, l = float(kl[i]["high"]), float(kl[i]["low"])
        pc = float(kl[i - 1]["close"])
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    return _sma(tr, n)


def _rsi(closes, n):
    out = [None] * len(closes)
    gain = loss = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g, l = max(ch, 0), max(-ch, 0)
        if i <= n:
            gain += g; loss += l
            if i == n:
                ag, al = gain / n, loss / n
                out[i] = 100 - 100 / (1 + (ag / al if al else 999))
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
            out[i] = 100 - 100 / (1 + (ag / al if al else 999))
    return out


def _boll(closes, n, k):
    mid = _sma(closes, n)
    up, lo = [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        m = mid[i]
        var = sum((closes[j] - m) ** 2 for j in range(i - n + 1, i + 1)) / n
        sd = var ** 0.5
        up[i], lo[i] = m + k * sd, m - k * sd
    return mid, up, lo


# ------------------------- 通用 -------------------------
def _mk(kl, i, direction, P, feat=None):
    entry = float(kl[i]["close"])
    atr = _ATR_CACHE[-1][i] if _ATR_CACHE else None
    return entry, atr


def _sig(kl, i, direction, atr, P, extra=None):
    entry = float(kl[i]["close"])
    if not atr or atr <= 0:
        return None
    risk = atr * P["atr_mult"]
    if direction == "long":
        sl, tp = entry - risk, entry + P["rr"] * risk
    else:
        sl, tp = entry + risk, entry - P["rr"] * risk
    d = {"direction": direction, "entry": entry, "sl": sl, "tp": tp,
         "entry_idx": i, "created_at": int(kl[i]["open_time"]) // 1000}
    if extra:
        d.update(extra)
    return d


_ATR_CACHE = []


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


# ------------------------- 6个经典策略 -------------------------
def detect_ma_cross(kl, P):
    c = _arr(kl, "close"); atr = _atr(kl, P["atr_n"])
    fast, slow = _sma(c, P["fast"]), _sma(c, P["slow"])
    out = []
    for i in range(P["slow"] + 1, len(kl) - 1):
        if None in (fast[i], slow[i], fast[i - 1], slow[i - 1], atr[i]):
            continue
        if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            s = _sig(kl, i, "long", atr[i], P)
            if s: out.append(s)
        elif fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
            s = _sig(kl, i, "short", atr[i], P)
            if s: out.append(s)
    return out


def detect_macd_cross(kl, P):
    c = _arr(kl, "close"); atr = _atr(kl, P["atr_n"])
    macd = [(a - b) if (a is not None and b is not None) else None
            for a, b in zip(_ema(c, P["fast"]), _ema(c, P["slow"]))]
    sig = _ema([m if m is not None else 0.0 for m in macd], P["signal"])
    out = []
    for i in range(P["slow"] + P["signal"], len(kl) - 1):
        if None in (macd[i], macd[i - 1]):
            continue
        if macd[i - 1] <= sig[i - 1] and macd[i] > sig[i]:
            s = _sig(kl, i, "long", atr[i], P)
            if s: out.append(s)
        elif macd[i - 1] >= sig[i - 1] and macd[i] < sig[i]:
            s = _sig(kl, i, "short", atr[i], P)
            if s: out.append(s)
    return out


def detect_donchian(kl, P):
    h = _arr(kl, "high"); l = _arr(kl, "low"); atr = _atr(kl, P["atr_n"])
    n = P["channel"]
    out = []
    for i in range(n + 1, len(kl) - 1):
        if atr[i] is None:
            continue
        hh = max(h[i - n:i]); ll = min(l[i - n:i])
        c = float(kl[i]["close"])
        if c > hh:
            s = _sig(kl, i, "long", atr[i], P)
            if s: out.append(s)
        elif c < ll:
            s = _sig(kl, i, "short", atr[i], P)
            if s: out.append(s)
    return out


def detect_boll_break(kl, P):
    c = _arr(kl, "close"); atr = _atr(kl, P["atr_n"])
    mid, up, lo = _boll(c, P["period"], P["k"])
    out = []
    for i in range(P["period"] + 1, len(kl) - 1):
        if None in (up[i], lo[i], up[i - 1], lo[i - 1], atr[i]):
            continue
        if c[i - 1] <= up[i - 1] and c[i] > up[i]:          # 突破上轨追多
            s = _sig(kl, i, "long", atr[i], P)
            if s: out.append(s)
        elif c[i - 1] >= lo[i - 1] and c[i] < lo[i]:        # 跌破下轨追空
            s = _sig(kl, i, "short", atr[i], P)
            if s: out.append(s)
    return out


def detect_rsi_revert(kl, P):
    c = _arr(kl, "close"); atr = _atr(kl, P["atr_n"]); rsi = _rsi(c, P["period"])
    out = []
    for i in range(P["period"] + 2, len(kl) - 1):
        if None in (rsi[i], rsi[i - 1], atr[i]):
            continue
        if rsi[i - 1] <= P["oversold"] and rsi[i] > P["oversold"]:   # 从超卖区上穿=反弹做多
            s = _sig(kl, i, "long", atr[i], P)
            if s: out.append(s)
        elif rsi[i - 1] >= P["overbought"] and rsi[i] < P["overbought"]:
            s = _sig(kl, i, "short", atr[i], P)
            if s: out.append(s)
    return out


def detect_boll_revert(kl, P):
    c = _arr(kl, "close"); atr = _atr(kl, P["atr_n"])
    mid, up, lo = _boll(c, P["period"], P["k"])
    out = []
    for i in range(P["period"] + 2, len(kl) - 1):
        if None in (up[i], lo[i], atr[i]):
            continue
        if c[i - 1] < lo[i - 1] and c[i] >= lo[i]:          # 触下轨回收=均值回归做多
            s = _sig(kl, i, "long", atr[i], P)
            if s: out.append(s)
        elif c[i - 1] > up[i - 1] and c[i] <= up[i]:
            s = _sig(kl, i, "short", atr[i], P)
            if s: out.append(s)
    return out


# ------------------------- 默认参数 + 注册 -------------------------
DEFAULTS = {
    "ma_cross":   dict(fast=10, slow=30, atr_n=14, atr_mult=1.5, rr=2.0),
    "macd_cross": dict(fast=12, slow=26, signal=9, atr_n=14, atr_mult=1.5, rr=2.0),
    "donchian":   dict(channel=20, atr_n=14, atr_mult=1.5, rr=2.0),
    "boll_break": dict(period=20, k=2.0, atr_n=14, atr_mult=1.5, rr=2.0),
    "rsi_revert": dict(period=14, oversold=30, overbought=70, atr_n=14, atr_mult=1.5, rr=2.0),
    "boll_revert": dict(period=20, k=2.0, atr_n=14, atr_mult=1.5, rr=2.0),
}
DETECTORS = {
    "ma_cross": detect_ma_cross, "macd_cross": detect_macd_cross, "donchian": detect_donchian,
    "boll_break": detect_boll_break, "rsi_revert": detect_rsi_revert, "boll_revert": detect_boll_revert,
}
LABELS = {
    "ma_cross": "双均线交叉", "macd_cross": "MACD交叉", "donchian": "唐奇安突破",
    "boll_break": "布林带突破", "rsi_revert": "RSI反转", "boll_revert": "布林带回归",
}


def _make_scan(name):
    det = DETECTORS[name]

    def scan(C):
        P = DEFAULTS[name]
        out = []
        for sym, k5 in C("5m").items():
            if len(k5) < 120:
                continue
            for s in det(k5, P):
                s["symbol"] = sym
                _settle(s, k5)
                s["strat"] = name
                out.append(s)
        return out
    return scan


CLASSIC_SCANS = {n: _make_scan(n) for n in DETECTORS}
CLASSIC_META = {n: {"label": LABELS[n], "tf": "5m",
                    "logic": [f"经典战法: {LABELS[n]}", "入场=触发K收盘; 止损=ATR×倍数; 止盈=RR×风险; 多空对称",
                              f"默认参数: {DEFAULTS[n]}"]} for n in DETECTORS}
CLASSIC_DETAIL = {n: {"desc": f"经典战法·{LABELS[n]}", "idea": "市面常见经典技术战法, 待回测Agent调参验证",
                      "updated": "2026-06-23", "code": "scripts/strat_classic.py", "doc": ""} for n in DETECTORS}
