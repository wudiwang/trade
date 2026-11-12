"""BTC 订单流简报：CVD / 资金费率 / 持仓量 与价格的相互状态（用户 2026-11-12）。

用户要的是每天曼谷时间 09:00 与 21:00 各一条推送，核心看两件事：
  1. **CVD（累计主动买卖差）按 1 小时 K 线** —— 价涨而 CVD 跌 = 顶背离，反之底背离
  2. **资金费率按天** —— 正=多头付钱给空头(多头拥挤)，负=反之
另加 OI（持仓量），因为"价格×OI 四象限"才能区分【新空进场的趋势性下跌】和
【多头爆仓的挤压式下跌】——这两者后市含义完全相反。

CVD 口径：币安 K 线自带 takerBuyBaseAssetVolume，故
    每根 delta = taker_buy - (volume - taker_buy) = 2*taker_buy - volume
这是 taker(主动成交)口径的 CVD，与 CoinGlass 的 CVD 同源。

本模块只做计算与排版，不碰网络；取数在 brief_payload()，由调用方注入 rest。
"""
from __future__ import annotations

import time

# klines 元组下标（见 binance_rest.klines 的 docstring）
_T, _O, _H, _L, _C, _V, _QV, _TB, _CLOSED = range(9)

BKK_OFFSET = 7 * 3600          # 曼谷 UTC+7


def bar_delta(k: tuple) -> float:
    """单根 K 线的主动买卖差：主动买 - 主动卖。"""
    vol, taker_buy = float(k[_V]), float(k[_TB])
    return 2.0 * taker_buy - vol


def cvd_series(klines: list[tuple]) -> list[float]:
    """累计 CVD 序列（与 klines 等长，从 0 起累加）。"""
    out, acc = [], 0.0
    for k in klines:
        acc += bar_delta(k)
        out.append(acc)
    return out


def pct(a: float, b: float) -> float:
    """b→a 的涨跌百分比。"""
    return (a - b) / b * 100.0 if b else 0.0


def divergence(klines: list[tuple], bars: int, min_price_pct: float = 0.3) -> dict:
    """比较最近 bars 根内【价格方向】与【CVD 方向】。

    min_price_pct: 价格变动小于该幅度视为"横盘"，此时不判背离（避免把噪音叫成信号）。
    返回 {bars, price_pct, cvd_chg, cvd_pct_of_vol, state, text}
      state ∈ bear_div(顶背离) / bull_div(底背离) / confirm_up / confirm_down / flat
    """
    if len(klines) < bars + 1:
        return {"bars": bars, "state": "insufficient", "text": "数据不足"}
    seg = klines[-bars:]
    price_now = float(klines[-1][_C])
    price_then = float(seg[0][_O])
    p = pct(price_now, price_then)
    cvd = sum(bar_delta(k) for k in seg)
    vol = sum(float(k[_V]) for k in seg) or 1.0
    ratio = cvd / vol * 100.0          # CVD 占该段总成交量的比例，便于跨时段比较

    if abs(p) < min_price_pct:
        state = "flat"
        text = f"价格横盘({p:+.2f}%)，CVD {_arrow(cvd)}"
    elif p > 0 and cvd < 0:
        state = "bear_div"
        text = f"⚠️ 顶背离：价涨 {p:+.2f}% 但 CVD 下降 —— 主动买没跟上"
    elif p < 0 and cvd > 0:
        state = "bull_div"
        text = f"⚠️ 底背离：价跌 {p:+.2f}% 但 CVD 上升 —— 有人在承接"
    elif p > 0:
        state = "confirm_up"
        text = f"同向上涨：价 {p:+.2f}%，CVD 同步上升 —— 主动买推动"
    else:
        state = "confirm_down"
        text = f"同向下跌：价 {p:+.2f}%，CVD 同步下降 —— 主动卖推动"
    return {"bars": bars, "price_pct": p, "cvd_chg": cvd,
            "cvd_pct_of_vol": ratio, "state": state, "text": text}


def _arrow(x: float) -> str:
    return "↑上升" if x > 0 else ("↓下降" if x < 0 else "持平")


def funding_daily(rates: list[tuple], days: int = 7) -> list[tuple]:
    """把 8 小时一期的资金费聚合成按天（曼谷日界）。返回 [(YYYY-MM-DD, 均值, 期数), ...] 升序。"""
    buckets: dict[str, list[float]] = {}
    for ts, r in rates:
        day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000 + BKK_OFFSET))
        buckets.setdefault(day, []).append(r)
    rows = [(d, sum(v) / len(v), len(v)) for d, v in sorted(buckets.items())]
    return rows[-days:]


def funding_state(rates: list[tuple], look: int = 21) -> dict:
    """资金费整体状态。look=21 期 ≈ 近 7 天。"""
    if not rates:
        return {"state": "unknown", "text": "资金费数据不可用"}
    recent = [r for _, r in rates[-look:]]
    latest = rates[-1][1]
    avg = sum(recent) / len(recent)
    neg = sum(1 for r in recent if r < 0)
    if avg > 0.0005:                     # 日化 >0.15%，明显偏高
        state, label = "crowded_long_high", "多头严重拥挤(费率偏高)"
    elif avg > 0:
        state, label = "crowded_long", "多头拥挤未出清"
    elif avg < -0.0005:
        state, label = "crowded_short_high", "空头严重拥挤"
    else:
        state, label = "crowded_short", "空头占优/多头已出清"
    return {"state": state, "label": label, "latest": latest, "avg": avg,
            "neg": neg, "look": len(recent)}


def quadrant(price_pct: float, oi_pct: float, flat: float = 0.3) -> tuple[str, str]:
    """价格 × OI 四象限。返回 (key, 人话)。见 memory: orderflow-dashboards。"""
    if abs(price_pct) < flat:
        if abs(oi_pct) >= 1.0:
            side = "增仓" if oi_pct > 0 else "减仓"
            return "flat", (f"价格横盘但 OI {side} {oi_pct:+.1f}% → "
                            f"{'有人在横盘里建仓' if oi_pct > 0 else '有人在横盘里离场'}，"
                            "方向待价格选择")
        return "flat", "价格横盘且 OI 无明显变化"
    up, oi_up = price_pct > 0, oi_pct > 0
    if not up and oi_up:
        return "new_shorts", "跌+增仓 → 新空进场，趋势性下跌(有延续性)"
    if not up and not oi_up:
        return "long_squeeze", "跌+减仓 → 多头平仓/爆仓，挤压式下跌(跌完易反弹)"
    if up and oi_up:
        return "new_longs", "涨+增仓 → 新多进场，趋势性上涨"
    return "short_cover", "涨+减仓 → 空头回补，反弹质量差(易回落)"


def _fmt_k(x: float) -> str:
    """把 BTC 数量格式化得短一些。"""
    ax = abs(x)
    if ax >= 1000:
        return f"{x/1000:+.1f}K"
    return f"{x:+.0f}"


def build_brief(payload: dict) -> str:
    """把 brief_payload() 的结果排版成 Telegram HTML。短、无废话、先给结论。"""
    p = payload
    now = time.strftime("%m-%d %H:%M", time.gmtime(time.time() + BKK_OFFSET))
    L = [f"📊 <b>BTC 订单流简报</b> · {now} 曼谷", ""]

    # 价格
    L.append(f"<b>价格</b> {p['price']:,.0f}   "
             f"24h {p['chg_24h']:+.1f}%   7d {p['chg_7d']:+.1f}%")
    L.append("")

    # CVD 背离（用户的核心诉求）
    L.append("<b>━ CVD × 价格（1h线）━</b>")
    for d in p["divs"]:
        if d["state"] == "insufficient":
            continue
        L.append(f"近{d['bars']}h: {d['text']}")
    L.append(f"CVD净额 24h: <b>{_fmt_k(p['cvd_24h'])}</b> BTC"
             f"（占成交 {p['cvd_ratio_24h']:+.1f}%）")
    L.append("")

    # 资金费（按天）
    f = p["funding"]
    L.append("<b>━ 资金费率（按天）━</b>")
    for day, avg, n in p["funding_days"][-3:]:
        L.append(f"{day[5:]}  {avg*100:+.4f}%  ({n}期)")
    L.append(f"近{f['look']}期均 <b>{f['avg']*100:+.4f}%</b>，"
             f"其中 {f['neg']} 期为负")
    L.append(f"状态：<b>{f['label']}</b>")
    L.append("")

    # OI
    L.append("<b>━ 持仓量 OI ━</b>")
    L.append(f"{p['oi']:,.0f} BTC   24h {p['oi_24h']:+.1f}%   7d {p['oi_7d']:+.1f}%")
    L.append(f"<b>{p['quadrant_text']}</b>")
    L.append("")

    # 合成
    L.append("<b>━ 合起来 ━</b>")
    L.append(p["summary"])
    L.append("")
    L.append("<i>指标读数，非投资建议。持仓中请守第一准则：只有止盈与止损。</i>")
    return "\n".join(L)


def summarize(div24: dict, fund: dict, quad_key: str) -> str:
    """三者合成一句话。只描述状态，不给方向建议。"""
    bits = []
    if div24["state"] == "bear_div":
        bits.append("价格上行但主动买不足")
    elif div24["state"] == "bull_div":
        bits.append("价格下行但有主动承接")
    elif div24["state"] == "confirm_down":
        bits.append("主动卖推动下跌")
    elif div24["state"] == "confirm_up":
        bits.append("主动买推动上涨")
    else:
        bits.append("价格横盘")

    if fund["state"].startswith("crowded_long"):
        bits.append("多头仍在付资金费、未出清")
    else:
        bits.append("资金费转负、多头已出清")

    tail = {
        "new_shorts": "新空在进场",
        "long_squeeze": "多头在被清算",
        "new_longs": "新多在进场",
        "short_cover": "空头在回补",
        "flat": "持仓变化不明显",
    }.get(quad_key, "")
    if tail:
        bits.append(tail)
    return "；".join(bits) + "。"


async def brief_payload(rest, symbol: str = "BTCUSDT") -> dict:
    """取数 + 计算。rest 需提供 klines / open_interest_hist / funding_rate_hist。"""
    k1h = await rest.klines(symbol, "1h", limit=200)
    if len(k1h) < 30:
        raise RuntimeError("1h K 线数据不足")
    price = float(k1h[-1][_C])

    divs = [divergence(k1h, b) for b in (6, 24, 72)]
    seg24 = k1h[-24:]
    cvd_24h = sum(bar_delta(k) for k in seg24)
    vol24 = sum(float(k[_V]) for k in seg24) or 1.0

    oi_h = await rest.open_interest_hist(symbol, "1h", limit=200)
    oi = oi_h[-1] if oi_h else 0.0
    oi_24h = pct(oi, oi_h[-25]) if len(oi_h) >= 25 else 0.0
    oi_7d = pct(oi, oi_h[-169]) if len(oi_h) >= 169 else (
        pct(oi, oi_h[0]) if len(oi_h) >= 24 else 0.0)

    rates = await rest.funding_rate_hist(symbol, limit=100)
    fund = funding_state(rates)
    fdays = funding_daily(rates)

    chg_24h = divs[1].get("price_pct", 0.0)
    chg_7d = pct(price, float(k1h[-min(len(k1h), 168)][_O]))
    qkey, qtext = quadrant(chg_24h, oi_24h)

    return {
        "price": price, "chg_24h": chg_24h, "chg_7d": chg_7d,
        "divs": divs, "cvd_24h": cvd_24h, "cvd_ratio_24h": cvd_24h / vol24 * 100.0,
        "oi": oi, "oi_24h": oi_24h, "oi_7d": oi_7d,
        "quadrant": qkey, "quadrant_text": qtext,
        "funding": fund, "funding_days": fdays,
        "summary": summarize(divs[1], fund, qkey),
    }
