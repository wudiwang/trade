"""逼空/操纵检测(P5a)：币安免费衍生数据组合识别 PEPE 式"筹码集中→逼空"特征。

持仓量(OI)骤增 + 资金费率极值 + 价格仍在低位 → ⚠逼空候选(暴涨后可能继续暴涨)。
全用免费 fapi 数据，无需 key。
"""


def price_position(klines: list, lookback: int = 200) -> float:
    """当前收盘在近 lookback 根区间的位置：0=最低，1=最高。"""
    seg = klines[-lookback:]
    if not seg:
        return 0.5
    hi = max(float(k["high"]) for k in seg)
    lo = min(float(k["low"]) for k in seg)
    if hi <= lo:
        return 0.5
    return (float(seg[-1]["close"]) - lo) / (hi - lo)


def squeeze_score(oi_hist: list, funding, pos: float, *,
                  oi_surge: float = 30.0, funding_extreme: float = 0.0005,
                  low_pos: float = 0.35):
    """返回 (是否逼空候选, 明细)。OI骤增为必要条件，叠加费率极值或价格低位。"""
    if len(oi_hist) < 3:
        return False, {}
    oi_now, oi_base = oi_hist[-1], oi_hist[0]
    oi_chg = (oi_now - oi_base) / oi_base * 100 if oi_base > 0 else 0.0
    cond_oi = oi_chg >= oi_surge
    cond_fund = funding is not None and abs(funding) >= funding_extreme
    cond_low = pos <= low_pos
    detail = {"oi_change_pct": round(oi_chg, 1), "funding": funding,
              "pos": round(pos, 2),
              "flags": {"oi": cond_oi, "funding": cond_fund, "low": cond_low},
              "strong": cond_oi and cond_fund and cond_low}
    return (cond_oi and (cond_fund or cond_low)), detail
