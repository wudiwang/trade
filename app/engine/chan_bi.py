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
