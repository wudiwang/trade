"""缠论笔策略：笔(≥5根合并K) + 底/顶分型 + 停顿K确认 → 一买/二买(及做空镜像)。

定义(用户 2026-06-13)：
- 笔：K线去包含后，顶分型↔底分型交替连接，一笔至少5根合并K。
- 一买：之前下跌成一笔 → 末端底分型 → 停顿K(分型后一根K收盘>底分型右K最高价) = 买。
- 二买：一买后 上涨成一笔 + 下跌成一笔 → 第二笔末端底分型(更高低点) → 停顿K = 买。
做空镜像：上涨成笔 → 顶分型 → 停顿K(收盘<顶分型右K最低价)。
"""
from .chan import merge_klines, find_fractals


def build_bi(klines: list, min_merged: int = 5):
    """返回 (merged, seq)。seq=交替的分型列表，相邻两个构成一笔(间隔≥min_merged-1根合并K)。"""
    merged = merge_klines(klines)
    fxs = find_fractals(klines, merged)
    seq = []
    for fx in fxs:
        if not seq:
            seq.append(fx)
            continue
        last = seq[-1]
        if fx.kind == last.kind:
            # 同类型分型，保留更极端的那个
            better = ((fx.kind == "bottom" and fx.extreme_price < last.extreme_price) or
                      (fx.kind == "top" and fx.extreme_price > last.extreme_price))
            if better:
                seq[-1] = fx
        elif fx.mid_merged_idx - last.mid_merged_idx >= min_merged - 1:
            seq.append(fx)
        # 间隔不足 → 不成笔，忽略该分型
    return merged, seq


def _merged_oc(klines: list, mk):
    """合并K的开/收：首根原始K开盘、末根原始K收盘。"""
    return float(klines[mk.src_idx[0]]["open"]), float(klines[mk.src_idx[-1]]["close"])


GRADE_CN = {"strongest": "最强", "standard": "标准", "weak": "最弱",
            "continuation": "中继", "unknown": "?"}
_GOOD_GRADES = ("strongest", "standard")


def fractal_grade(klines: list, merged: list, fx) -> str:
    """分型强弱分级(参考用户图2)。L=左K M=中K(极值) R=右K。
    底分型：
      最强 strongest   右K最高点 > 左K最高点(明显向右上斜)
      标准 standard    右K收盘 ≥ 左K实体中点 且 收盘 > 中K最高点(真实收回)
      中继 continuation 右K收盘 < 左K实体中点(没回到第一根实体一半)
      最弱 weak        其余(右K收回无力)
    顶分型镜像。返回 strongest/standard/weak/continuation/unknown。"""
    mid = fx.mid_merged_idx
    if mid < 1 or mid + 1 >= len(merged):
        return "unknown"
    L, M, R = merged[mid - 1], merged[mid], merged[mid + 1]
    Lo, Lc = _merged_oc(klines, L)
    Lbody_mid = (Lo + Lc) / 2.0
    _, Rc = _merged_oc(klines, R)
    if fx.kind == "bottom":
        if R.high > L.high:
            return "strongest"
        if Rc >= Lbody_mid and Rc > M.high:
            return "standard"
        if Rc < Lbody_mid:
            return "continuation"
        return "weak"
    else:
        if R.low < L.low:
            return "strongest"
        if Rc <= Lbody_mid and Rc < M.low:
            return "standard"
        if Rc > Lbody_mid:
            return "continuation"
        return "weak"


def front_vol_ratio(klines: list, merged: list, fx, ma: int = 10) -> float:
    """底分型前2根(左K、中K)中,合并K量/前ma根均量 的最大值(放量倍数)。
    合并K的量 = 其覆盖原始K量之和；均量 = 该合并K起点前 ma 根原始K均量。"""
    mid = fx.mid_merged_idx
    if mid < 1:
        return 0.0
    best = 0.0
    for mk in (merged[mid - 1], merged[mid]):
        start = mk.src_idx[0]
        if start < ma:
            continue
        avg = sum(float(klines[x]["volume"]) for x in range(start - ma, start)) / ma
        vol = sum(float(klines[x]["volume"]) for x in mk.src_idx)
        if avg > 0:
            best = max(best, vol / avg)
    return best


def vol_spike_before(klines: list, merged: list, fx, ma: int = 10, mult: float = 2.0) -> bool:
    """量能规则：底分型前2根任意一根放量 ≥ mult×前ma根均量。"""
    return front_vol_ratio(klines, merged, fx, ma) >= mult


def strong_reversal(klines: list, merged: list, fx, body_ratio: float = 0.6) -> bool:
    """15m 增量条件(强反转形态)。底分型(左L 中M 右R)三条同时满足：
      ① 右K实体 ≥ 自身振幅 × body_ratio(大实体反转K)
      ② 右K收盘 > 左K最高价(完全吞掉第一根下跌K)
      ③ 中K(最低那根)有下影线(低点被买盘拒绝)
    顶分型镜像(右K实体大 / 右K收盘<左K最低 / 中K有上影线)。"""
    mid = fx.mid_merged_idx
    if mid < 1 or mid + 1 >= len(merged):
        return False
    L, M, R = merged[mid - 1], merged[mid], merged[mid + 1]
    Mo, Mc = _merged_oc(klines, M)
    Ro, Rc = _merged_oc(klines, R)
    rng = R.high - R.low
    if rng <= 0:
        return False
    body_ok = abs(Rc - Ro) >= body_ratio * rng                 # ① 右K大实体
    if fx.kind == "bottom":
        engulf = Rc > L.high                                   # ② 右K收盘 > 左K最高
        wick = (min(Mo, Mc) - M.low) > 0                       # ③ 中K有下影线
    else:
        engulf = Rc < L.low                                    # ② 右K收盘 < 左K最低
        wick = (M.high - max(Mo, Mc)) > 0                      # ③ 中K有上影线
    return body_ok and engulf and wick


def quality_ok(klines: list, merged: list, fx, vol_ma: int = 10, vol_mult: float = 2.0):
    """返回 (是否通过, 强度grade, 放量倍数)。通过 = 最强/标准 且 前2根放量达标。"""
    grade = fractal_grade(klines, merged, fx)
    vr = round(front_vol_ratio(klines, merged, fx, vol_ma), 2)
    if grade not in _GOOD_GRADES:
        return False, grade, vr
    if vr < vol_mult:
        return False, grade, vr
    return True, grade, vr


# ======================= 背驰(力度衰竭) =======================

def macd_hist(closes: list, fast: int = 12, slow: int = 26, signal: int = 9) -> list:
    """MACD 柱(DIF-DEA)。返回与 closes 等长的列表。"""
    from .chan import ema
    ef, es = ema(closes, fast), ema(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, signal)
    return [d - s for d, s in zip(dif, dea)]


def _seg_area(hist: list, i: int, j: int, sign: int) -> float:
    """[i,j] 区间内同号柱体面积绝对值之和。sign<0 取绿柱(负), sign>0 取红柱(正)。"""
    lo, hi = (i, j) if i <= j else (j, i)
    s = 0.0
    for x in range(max(0, lo), min(len(hist), hi + 1)):
        v = hist[x]
        if sign < 0 and v < 0:
            s += -v
        elif sign > 0 and v > 0:
            s += v
    return s


def divergence(klines: list, seq: list, direction: str,
               macd_cfg=(12, 26, 9)):
    """底背驰(long)/顶背驰(short)。比较"进入段a"与"离开段b"两段同向笔：
      形态背驰：创新低(高)但 b 段价格幅度 < a 段;
      MACD背驰：创新低(高)但 b 段柱体面积 < a 段。
    两者任一成立即背驰。返回 (bool, tag)。seq 需≥4个交替分型(a段+反弹/中枢+b段)。"""
    if len(seq) < 4:
        return False, ""
    closes = [float(k["close"]) for k in klines]
    hist = macd_hist(closes, *macd_cfg)
    if direction == "long":
        b_bot, b_top, a_bot, a_top = seq[-1], seq[-2], seq[-3], seq[-4]
        if not (b_bot.kind == "bottom" and b_top.kind == "top"
                and a_bot.kind == "bottom" and a_top.kind == "top"):
            return False, ""
        if b_bot.extreme_price >= a_bot.extreme_price:      # 没创新低 → 不是底背驰场景
            return False, ""
        a_amp = a_top.extreme_price - a_bot.extreme_price
        b_amp = b_top.extreme_price - b_bot.extreme_price
        shape = 0 < b_amp < a_amp
        a_area = _seg_area(hist, a_top.extreme_src_idx, a_bot.extreme_src_idx, -1)
        b_area = _seg_area(hist, b_top.extreme_src_idx, b_bot.extreme_src_idx, -1)
        macd = a_area > 0 and 0 <= b_area < a_area
    else:
        b_top, b_bot, a_top, a_bot = seq[-1], seq[-2], seq[-3], seq[-4]
        if not (b_top.kind == "top" and b_bot.kind == "bottom"
                and a_top.kind == "top" and a_bot.kind == "bottom"):
            return False, ""
        if b_top.extreme_price <= a_top.extreme_price:      # 没创新高 → 不是顶背驰场景
            return False, ""
        a_amp = a_top.extreme_price - a_bot.extreme_price
        b_amp = b_top.extreme_price - b_bot.extreme_price
        shape = 0 < b_amp < a_amp
        a_area = _seg_area(hist, a_bot.extreme_src_idx, a_top.extreme_src_idx, 1)
        b_area = _seg_area(hist, b_bot.extreme_src_idx, b_top.extreme_src_idx, 1)
        macd = a_area > 0 and 0 <= b_area < a_area
    if shape or macd:
        tag = "+".join([t for t, ok in (("形态", shape), ("MACD", macd)) if ok])
        return True, tag
    return False, ""


def _vol_ratio_at(klines: list, idx: int, ma: int = 20) -> float:
    """第 idx 根量 / 其前 ma 根均量。"""
    if idx < ma:
        return 1.0
    avg = sum(float(klines[j]["volume"]) for j in range(idx - ma, idx)) / ma
    v = float(klines[idx]["volume"])
    return v / avg if avg > 0 else 1.0


def _wick_ok(k, direction: str, wick_min: float) -> bool:
    """扫破K要有反向影线(拒绝)。多: 下影≥振幅×wick_min; 空: 上影。"""
    o, c, h, l = float(k["open"]), float(k["close"]), float(k["high"]), float(k["low"])
    rng = h - l
    if rng <= 0:
        return False
    if direction == "long":
        return (min(o, c) - l) >= wick_min * rng
    return (h - max(o, c)) >= wick_min * rng


def wyckoff_spring(klines: list, lookback: int = 20, reclaim_bars: int = 4,
                   pierce_tol_pct: float = 0.0, vol_ma: int = 20,
                   climax_mult: float = 2.0, dryup_ratio: float = 1.0,
                   min_bounce_pct: float = 1.5, wick_min: float = 0.4):
    """威科夫弹簧(多)/上冲回落UTAD(空)。在最新K上判定。
    多: 近 reclaim_bars 根有一根"爆量阴线"扫破前低, 价格再收回到该爆量K的【启动位置=开盘价】以上;
       且前低形成后价格曾反弹离开它≥min_bounce_pct%(回探支撑,非下跌途中)。空头镜像(爆量阳线扫前高)。
    返回 (dir, 扫破极值=止损参考, 爆量K启动位置=入场, 扫破idx, grade, vol_ratio) 或 None。"""
    n = len(klines)
    if n < lookback + reclaim_bars + 2:
        return None
    i = n - 1
    c_now = float(klines[i]["close"])
    b0 = max(0, i - reclaim_bars - lookback + 1)
    base = klines[b0: i - reclaim_bars + 1]
    if len(base) < max(5, lookback // 2):
        return None
    lows = [float(k["low"]) for k in base]
    highs = [float(k["high"]) for k in base]
    j0 = i - reclaim_bars + 1
    recent = klines[j0: i + 1]
    rl = [float(k["low"]) for k in recent]
    rh = [float(k["high"]) for k in recent]
    # 多头弹簧: 爆量阴线扫破前低 → 收回到该爆量K启动位置(开盘)以上
    prior_low = min(lows)
    lo_pos = lows.index(prior_low)
    spring_low = min(rl)
    sidx = j0 + rl.index(spring_low)
    so, sc = float(klines[sidx]["open"]), float(klines[sidx]["close"])
    bounced_up = highs[lo_pos + 1:] and max(highs[lo_pos + 1:]) >= prior_low * (1 + min_bounce_pct / 100.0)
    if (spring_low < prior_low * (1 - pierce_tol_pct / 100.0)
            and sc < so and c_now > so and bounced_up):       # 爆量阴线 + 收回到其开盘上方
        vr = _vol_ratio_at(klines, sidx, vol_ma)
        grade = "缩量弹簧" if vr < dryup_ratio else ("放量弹簧" if vr >= climax_mult else "中性弹簧")
        return "long", spring_low, so, sidx, grade, round(vr, 2)
    # 空头 UTAD: 爆量阳线扫破前高 → 回落到该爆量K启动位置(开盘)以下
    prior_high = max(highs)
    hi_pos = highs.index(prior_high)
    spring_high = max(rh)
    hidx = j0 + rh.index(spring_high)
    ho, hc = float(klines[hidx]["open"]), float(klines[hidx]["close"])
    dropped = lows[hi_pos + 1:] and min(lows[hi_pos + 1:]) <= prior_high * (1 - min_bounce_pct / 100.0)
    if (spring_high > prior_high * (1 + pierce_tol_pct / 100.0)
            and hc > ho and c_now < ho and dropped):          # 爆量阳线 + 回落到其开盘下方
        vr = _vol_ratio_at(klines, hidx, vol_ma)
        grade = "缩量上冲" if vr < dryup_ratio else ("放量上冲" if vr >= climax_mult else "中性上冲")
        return "short", spring_high, ho, hidx, grade, round(vr, 2)
    return None


def trend_reversal(klines: list, min_merged: int = 5):
    """趋势反转(结构破位 MSB)。在最新K判定:
    看跌: 顶2 < 顶1(更低的高点) 且 当前收盘 < 底1(收盘跌破前低);
    看涨镜像: 底2 > 底1(更高的低点) 且 当前收盘 > 顶1(收盘升破前高)。
    返回 (direction, ref_extreme, ref_time) 或 None。
    direction: short=看跌反转 / long=看涨反转; ref=失败判定参照(顶1/底1)。"""
    _, seq = build_bi(klines, min_merged)
    if len(seq) < 3:
        return None
    c = float(klines[-1]["close"])
    a, b, d = seq[-3], seq[-2], seq[-1]
    if a.kind == "top" and b.kind == "bottom" and d.kind == "top":
        if d.extreme_price < a.extreme_price and c < b.extreme_price:   # 更低高点 + 收盘破前低
            return "short", a.extreme_price, a.open_time
    if a.kind == "bottom" and b.kind == "top" and d.kind == "bottom":
        if d.extreme_price > a.extreme_price and c > b.extreme_price:   # 更高低点 + 收盘破前高
            return "long", a.extreme_price, a.open_time
    return None


def head_shoulders_top(klines: list, min_merged: int = 5, shoulder_tol_pct: float = 8.0):
    """头肩顶: 笔序列末端 左肩-谷-头-谷-右肩(三顶,头最高,两肩相近且低于头),
    颈线=两谷较低者,当前收盘跌破颈线 → 触发。返回 (neckline, head_price, head_time) 或 None。"""
    _, seq = build_bi(klines, min_merged)
    if len(seq) < 5:
        return None
    ls, t1, h, t2, rs = seq[-5:]
    if not (ls.kind == "top" and t1.kind == "bottom" and h.kind == "top"
            and t2.kind == "bottom" and rs.kind == "top"):
        return None
    if not (h.extreme_price > ls.extreme_price and h.extreme_price > rs.extreme_price):
        return None
    if abs(ls.extreme_price - rs.extreme_price) / h.extreme_price > shoulder_tol_pct / 100.0:
        return None                                  # 两肩高度需相近
    neckline = min(t1.extreme_price, t2.extreme_price)
    if float(klines[-1]["close"]) < neckline:        # 收盘跌破颈线
        return neckline, h.extreme_price, h.open_time
    return None


def trend_state(klines: list, ma_period: int = 20, lookback: int = 10, slope_pct: float = 0.3) -> str:
    """趋势判定 'up'/'down'/'range'：MA(ma_period)近lookback根斜率 > slope_pct% 且收盘在MA上 → up;
    镜像 → down; 否则 range(震荡)。用于顺势过滤:上升趋势禁做空、下降趋势禁做多。"""
    n = len(klines)
    if n < ma_period + lookback + 1:
        return "range"
    closes = [float(k["close"]) for k in klines]
    ma_now = sum(closes[n - ma_period:]) / ma_period
    ma_prev = sum(closes[n - ma_period - lookback: n - lookback]) / ma_period
    if ma_prev <= 0:
        return "range"
    slope = (ma_now - ma_prev) / ma_prev * 100.0
    cur = closes[-1]
    if cur > ma_now and slope > slope_pct:
        return "up"
    if cur < ma_now and slope < -slope_pct:
        return "down"
    return "range"


def lifecycle_state(klines: list, fx_price: float, fx_time_ms: int,
                    direction: str, min_merged: int = 5) -> str:
    """信号生命周期判定，返回 'fail' / 'ok' / 'try'。
    fail = 分型后价格打穿分型极值(底分型最低/顶分型最高)= 一买/一卖失败;
    ok   = 分型之后走完一笔(出现反向分型，间隔≥min_merged-1合并K) = 底/顶成立;
    否则 try(仍在尝试，未定)。先判失败(破极值优先)。"""
    for k in klines:
        if int(k["open_time"]) <= fx_time_ms:
            continue
        if direction == "long" and float(k["low"]) < fx_price:
            return "fail"
        if direction == "short" and float(k["high"]) > fx_price:
            return "fail"
    _, seq = build_bi(klines, min_merged)
    want = "top" if direction == "long" else "bottom"
    for f in seq:
        if f.open_time > fx_time_ms and f.kind == want:
            return "ok"
    return "try"


def stall_idx(klines: list, merged: list, fx, max_gap: int = 3):
    """停顿K：底分型→某根K收盘>右K最高价(顶分型→收盘<右K最低价)，且必须是最后一根K。
    返回停顿K原始下标或 None。"""
    rk = fx.mid_merged_idx + 1
    if rk >= len(merged):
        return None
    last = len(klines) - 1
    right_last = fx.confirm_src_idx
    if last <= right_last or last - right_last > max_gap:
        return None
    c = float(klines[last]["close"])
    if fx.kind == "bottom" and c > merged[rk].high:
        return last
    if fx.kind == "top" and c < merged[rk].low:
        return last
    return None


def structure_fractal(klines: list, min_merged: int = 5,
                      vol_ma: int = 10, vol_mult: float = 2.0):
    """笔末端最新分型(下跌成笔→底分型 / 上涨成笔→顶分型)，**不要求本级别停顿**，
    但要求 最强/标准 强度 + 前2根放量达标。供多级别联立用：高级别给结构，低级别给停顿。
    返回 (末端分型, grade) 或 None。"""
    merged, seq = build_bi(klines, min_merged)
    if len(seq) < 2 or seq[-1].kind == seq[-2].kind:
        return None
    fx = seq[-1]
    ok, grade, vr = quality_ok(klines, merged, fx, vol_ma, vol_mult)
    if not ok:
        return None
    return fx, grade, vr


def detect(klines: list, min_merged: int = 5, max_gap: int = 3,
           vol_ma: int = 10, vol_mult: float = 2.0, apply_quality: bool = True):
    """返回 (direction, sig_type, fx, stall, grade, vol_ratio, seq) 或 None。
    direction: long(底分型) / short(顶分型)；sig_type: buy1/buy2；
    vol_ratio=底分型前2根放量倍数；seq=笔分型序列(供背驰判定)。
    apply_quality=True 时强制 最强/标准 强度 + 前2根放量；False 仅用于多级别触发停顿(不卡分型质量)。"""
    if len(klines) < min_merged * 3:
        return None
    merged, seq = build_bi(klines, min_merged)
    if len(seq) < 2:
        return None
    last_fx = seq[-1]
    prev_fx = seq[-2]

    s = stall_idx(klines, merged, last_fx, max_gap)
    if s is None:
        return None

    grade = "unknown"
    vratio = 0.0
    if apply_quality:
        ok, grade, vratio = quality_ok(klines, merged, last_fx, vol_ma, vol_mult)
        if not ok:
            return None

    if last_fx.kind == "bottom":
        # 末端底分型，前一笔必须是下跌笔(顶→底)
        if prev_fx.kind != "top":
            return None
        direction = "long"
        bottoms = [f for f in seq if f.kind == "bottom"]
        # 二买：上一个底存在 且 本底更高(更高的低点)
        sig_type = "buy2" if (len(bottoms) >= 2 and last_fx.extreme_price > bottoms[-2].extreme_price) else "buy1"
    else:
        if prev_fx.kind != "bottom":
            return None
        direction = "short"
        tops = [f for f in seq if f.kind == "top"]
        # 二卖：上一个顶存在 且 本顶更低(更低的高点)；类型名沿用 buy1/buy2，方向分多空
        sig_type = "buy2" if (len(tops) >= 2 and last_fx.extreme_price < tops[-2].extreme_price) else "buy1"
    return direction, sig_type, last_fx, s, grade, vratio, seq


# ======================= 底座 v1(用户 2026-07-27 确认定义) =======================
# 三条锁定定义：
#   ① 中枢=标准(连续三笔重叠, ZG/ZD 由前三笔锁定, 相交则延伸, 离开即结束)
#   ② 一买=任意下跌笔末端底分型 + 停顿K(无背驰、无中枢门槛)
#   ③ 二买=严格: 一买后 上涨成笔→回调成笔, 且 B2>B1(不破一买低点) + 停顿K
# 均为新增函数, 不改动线上 detect()/build_bi()。

def _bi_lohi(seq):
    """把分型序列转成每一笔的 (low, high)。第 i 笔连接 seq[i]、seq[i+1]。"""
    out = []
    for i in range(len(seq) - 1):
        a, b = seq[i].extreme_price, seq[i + 1].extreme_price
        out.append((min(a, b), max(a, b)))
    return out


def build_zhongshu(seq, min_bis: int = 3):
    """标准中枢。连续三笔区间重叠成枢: ZG=min(三高), ZD=max(三低), 需 ZG>ZD;
    GG/DD=延伸期间的最高/最低; 后续笔只要与[ZD,ZG]相交则延伸, 完全离开即结束。
    返回中枢列表, 每个: {start_fx, end_fx, ZG, ZD, GG, DD, start_time, end_time, bis}。
    start_fx/end_fx 为 seq 下标; 一个中枢覆盖 seq[start_fx..end_fx]。"""
    bis = _bi_lohi(seq)
    out = []
    i = 0
    while i + (min_bis - 1) < len(bis):
        lo3 = [bis[i + k][0] for k in range(min_bis)]
        hi3 = [bis[i + k][1] for k in range(min_bis)]
        ZG = min(hi3); ZD = max(lo3)
        if ZG > ZD:                                    # 前三笔重叠 → 成枢
            GG = max(hi3); DD = min(lo3)
            j = i + min_bis                            # 尝试延伸
            while j < len(bis) and bis[j][0] <= ZG and bis[j][1] >= ZD:
                GG = max(GG, bis[j][1]); DD = min(DD, bis[j][0]); j += 1
            end_fx = j + 1 if j < len(bis) else len(seq) - 1   # 覆盖到最后一笔的右端分型
            end_fx = min(end_fx, len(seq) - 1)
            out.append({"start_fx": i, "end_fx": end_fx, "ZG": ZG, "ZD": ZD,
                        "GG": GG, "DD": DD, "start_time": seq[i].open_time,
                        "end_time": seq[end_fx].open_time, "bis": j - i})
            i = j                                      # 中枢结束后继续找下一个
        else:
            i += 1
    return out


def significant_zhongshu(zs: list, ref_price: float, min_width_pct: float = 0.0,
                         min_bis: int = 0) -> list:
    """过滤掉噪音级微中枢。2026-08-01: 实测5m上会出现89个中枢, 部分宽度仅万分之一
    (三笔勉强重叠出的极窄区间), 拿它们比高低判趋势纯属噪音。
      min_width_pct: (ZG-ZD)/参考价 的最小百分比
      min_bis:       中枢至少包含几笔(延伸得越久越有意义)
    """
    out = []
    for z in zs:
        if min_width_pct > 0 and ref_price > 0:
            if (z["ZG"] - z["ZD"]) / ref_price * 100.0 < min_width_pct:
                continue
        if min_bis and z.get("bis", 0) < min_bis:
            continue
        out.append(z)
    return out


def trend_by_zhongshu(klines: list, min_merged: int = 5, min_bis: int = 3,
                      min_width_pct: float = 0.0, zs_min_bis: int = 0):
    """缠论正统趋势判定(2026-08-01)：**趋势 = 同方向至少两个中枢**。

    - 上涨：后一个中枢【完全在前一个之上】(后枢下沿 ZD > 前枢上沿 ZG)
    - 下跌：后一个中枢【完全在前一个之下】(后枢上沿 ZG < 前枢下沿 ZD)
    - 只有一个中枢、或两枢有重叠 → 盘整(无序)
    与均线法(trend_state/trend_direction)的区别：这是结构口径, 不会因为一根长阳/长阴
    就翻向, 也不会在窄幅震荡里被均线斜率骗出方向。

    返回 dict:
      state      'up' / 'down' / 'range'
      n_zs       中枢个数
      last/prev  最近两个中枢 (ZG/ZD/GG/DD/bis/start_time/end_time)
      pos        当前收盘相对【最后一个中枢】的位置: 'above'/'inside'/'below'
                 (三买/三卖看这个: 离枢向上且回调不回枢 = 三买)
    """
    _, seq = build_bi(klines, min_merged)
    zs = build_zhongshu(seq, min_bis)
    close = float(klines[-1]["close"]) if klines else 0.0
    if min_width_pct > 0 or zs_min_bis:
        zs = significant_zhongshu(zs, close, min_width_pct, zs_min_bis)
    out = {"state": "range", "n_zs": len(zs), "last": None, "prev": None, "pos": None}
    if not zs:
        return out
    last = zs[-1]
    out["last"] = last
    out["pos"] = ("above" if close > last["ZG"] else
                  "below" if close < last["ZD"] else "inside")
    if len(zs) >= 2:
        prev = zs[-2]
        out["prev"] = prev
        if last["ZD"] > prev["ZG"]:
            out["state"] = "up"
        elif last["ZG"] < prev["ZD"]:
            out["state"] = "down"
    return out


def stall_after(klines, merged, fx, max_gap: int = 3):
    """通用停顿K(可枚举历史, 不要求是最后一根): 分型确认后 max_gap 根内, 第一根
    收盘突破右侧合并K极值的K。底分型→收盘>右合并K最高; 顶分型→收盘<右合并K最低。
    返回该K原始下标 或 None。"""
    rk = fx.mid_merged_idx + 1
    if rk >= len(merged):
        return None
    ref_high, ref_low = merged[rk].high, merged[rk].low
    start = fx.confirm_src_idx + 1
    for idx in range(start, min(len(klines), start + max_gap)):
        c = float(klines[idx]["close"])
        if fx.kind == "bottom" and c > ref_high:
            return idx
        if fx.kind == "top" and c < ref_low:
            return idx
    return None


def find_buy_points(klines, min_merged: int = 5, max_gap: int = 3,
                    vol_ma: int = 10, vol_mult: float = 2.0, apply_quality: bool = False):
    """枚举全历史 一买/二买/一卖/二卖(按 v1 锁定定义)。
    返回列表, 每条: {type, direction, fx_price, fx_time, fx_src_idx, stall_idx, stall_time,
                    ref_price(二买/二卖=一买/一卖低/高点), grade, vol_ratio}。
    一买(宽): 下跌笔末端底分型+停顿。 二买(严): B1(下跌笔末底)→T1顶(成笔)→B2底(成笔) 且 B2>B1 +停顿。"""
    merged, seq = build_bi(klines, min_merged)
    pts = []
    if len(seq) < 2:
        return pts, merged, seq

    def _q(fx):
        if not apply_quality:
            g = fractal_grade(klines, merged, fx)
            return True, g, round(front_vol_ratio(klines, merged, fx, vol_ma), 2)
        return quality_ok(klines, merged, fx, vol_ma, vol_mult)

    def _emit(fx, typ, direction, st, ref=None):
        ok, grade, vr = _q(fx)
        if not ok:
            return
        pts.append({"type": typ, "direction": direction, "fx_price": fx.extreme_price,
                    "fx_time": fx.open_time, "fx_src_idx": fx.extreme_src_idx,
                    "stall_idx": st, "stall_time": int(klines[st]["open_time"]),
                    "ref_price": ref, "grade": grade, "vol_ratio": vr})

    # ① 一买/一卖: 任意"末端笔"分型 + 停顿
    for k in range(1, len(seq)):
        fx, prev = seq[k], seq[k - 1]
        if fx.kind == "bottom" and prev.kind == "top":      # 下跌笔末端底分型
            st = stall_after(klines, merged, fx, max_gap)
            if st is not None:
                _emit(fx, "buy1", "long", st)
        elif fx.kind == "top" and prev.kind == "bottom":    # 上涨笔末端顶分型
            st = stall_after(klines, merged, fx, max_gap)
            if st is not None:
                _emit(fx, "sell1", "short", st)

    # ② 二买/二卖: 严格 B1→T1(成笔)→B2(成笔), B2 更高低点 / T2 更低高点 + 停顿
    for k in range(len(seq) - 2):
        A, M, B = seq[k], seq[k + 1], seq[k + 2]
        if A.kind == "bottom" and M.kind == "top" and B.kind == "bottom":
            if B.extreme_price > A.extreme_price:           # 不破一买低点
                st = stall_after(klines, merged, B, max_gap)
                if st is not None:
                    _emit(B, "buy2", "long", st, ref=A.extreme_price)
        elif A.kind == "top" and M.kind == "bottom" and B.kind == "top":
            if B.extreme_price < A.extreme_price:           # 不破一卖高点
                st = stall_after(klines, merged, B, max_gap)
                if st is not None:
                    _emit(B, "sell2", "short", st, ref=A.extreme_price)

    pts.sort(key=lambda p: p["stall_time"])
    return pts, merged, seq
