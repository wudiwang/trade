"""订单流简报的纯计算单测（用户 2026-11-12 的每日两次推送）。"""
from app.engine.orderflow import (
    bar_delta, cvd_series, divergence, funding_daily, funding_state,
    quadrant, summarize,
)

# klines 元组: (open_time, open, high, low, close, volume, quote_vol, taker_buy, closed)
def K(t, o, c, vol, taker_buy):
    return (t, o, max(o, c), min(o, c), c, vol, vol * c, taker_buy, 1)


def test_bar_delta_买卖方向():
    assert bar_delta(K(0, 100, 101, 10.0, 7.0)) == 4.0      # 买7 卖3 → +4
    assert bar_delta(K(0, 100, 99, 10.0, 3.0)) == -4.0      # 买3 卖7 → -4
    assert bar_delta(K(0, 100, 100, 10.0, 5.0)) == 0.0      # 对半 → 0


def test_cvd_series_累加():
    ks = [K(0, 100, 101, 10, 7), K(1, 101, 102, 10, 7), K(2, 102, 101, 10, 3)]
    assert cvd_series(ks) == [4.0, 8.0, 4.0]


def test_divergence_顶背离():
    """价格一路涨，但每根都是主动卖占优 → 顶背离。"""
    ks = [K(i, 100 + i, 101 + i, 10, 3) for i in range(30)]
    d = divergence(ks, 24)
    assert d["state"] == "bear_div"
    assert d["price_pct"] > 0 and d["cvd_chg"] < 0


def test_divergence_底背离():
    ks = [K(i, 200 - i, 199 - i, 10, 7) for i in range(30)]
    d = divergence(ks, 24)
    assert d["state"] == "bull_div"
    assert d["price_pct"] < 0 and d["cvd_chg"] > 0


def test_divergence_同向():
    ks = [K(i, 100 + i, 101 + i, 10, 7) for i in range(30)]
    assert divergence(ks, 24)["state"] == "confirm_up"
    ks = [K(i, 200 - i, 199 - i, 10, 3) for i in range(30)]
    assert divergence(ks, 24)["state"] == "confirm_down"


def test_divergence_横盘不判背离():
    """价格几乎不动时不应报背离——否则噪音会被当信号。"""
    ks = [K(i, 100, 100.01, 10, 3) for i in range(30)]
    assert divergence(ks, 24)["state"] == "flat"


def test_divergence_数据不足():
    assert divergence([K(0, 100, 101, 10, 7)], 24)["state"] == "insufficient"


def test_funding_daily_按曼谷日界聚合():
    day = 86400_000
    base = 1762900000000          # 任意时间点
    rates = [(base, 0.0001), (base + 8 * 3600_000, 0.0003),
             (base + day, 0.0002)]
    rows = funding_daily(rates)
    assert len(rows) >= 2
    assert all(len(r) == 3 for r in rows)      # (日期, 均值, 期数)


def test_funding_state_多空拥挤():
    pos = [(i, 0.0008) for i in range(21)]
    assert funding_state(pos)["state"] == "crowded_long_high"
    mild = [(i, 0.0001) for i in range(21)]
    assert funding_state(mild)["state"] == "crowded_long"
    neg = [(i, -0.0008) for i in range(21)]
    assert funding_state(neg)["state"] == "crowded_short_high"
    assert funding_state([])["state"] == "unknown"


def test_funding_state_统计负期数():
    rates = [(i, 0.0001) for i in range(18)] + [(i, -0.0002) for i in range(3)]
    assert funding_state(rates)["neg"] == 3


def test_quadrant_四象限():
    assert quadrant(-5, +3)[0] == "new_shorts"      # 跌+增仓
    assert quadrant(-5, -3)[0] == "long_squeeze"    # 跌+减仓
    assert quadrant(+5, +3)[0] == "new_longs"       # 涨+增仓
    assert quadrant(+5, -3)[0] == "short_cover"     # 涨+减仓


def test_quadrant_横盘仍保留OI信息():
    """价格横盘时不能把 OI 的真实变动丢掉。"""
    key, text = quadrant(0.1, -2.6)
    assert key == "flat" and "减仓" in text and "2.6" in text
    key, text = quadrant(0.1, 0.2)
    assert key == "flat" and "无明显变化" in text


def test_summarize_不给方向建议():
    div = divergence([K(i, 100 + i, 101 + i, 10, 3) for i in range(30)], 24)
    s = summarize(div, funding_state([(i, 0.0003) for i in range(21)]), "new_shorts")
    assert "主动买不足" in s and "未出清" in s and "新空" in s
    for banned in ("建议", "应该", "买入", "卖出", "做多", "做空"):
        assert banned not in s
