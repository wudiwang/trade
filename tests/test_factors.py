"""因子库单元测试。python tests/test_factors.py 运行。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.engine.chan import find_fractals
from app.engine.factors import (
    rsi, atr, f_rsi_extreme, f_rsi_divergence, f_wick_rejection,
    f_taker_ratio, f_funding, f_btc_resonance, sl_atr_sane, score_signal,
)


def k(o, h, l, c, v=100, tb=50, t=0):
    return {"open_time": t, "open": o, "high": h, "low": l, "close": c,
            "volume": v, "taker_buy": tb}


def test_rsi_bounds():
    up = [float(i) for i in range(1, 40)]      # 单边涨 → RSI 接近 100
    dn = [float(40 - i) for i in range(1, 40)]  # 单边跌 → RSI 接近 0
    assert rsi(up)[-1] > 95, rsi(up)[-1]
    assert rsi(dn)[-1] < 5, rsi(dn)[-1]
    assert all(v == 50.0 for v in rsi([1.0, 2.0]))  # 数据不足


def test_atr_known():
    ks = [k(10, 11, 9, 10, t=i) for i in range(20)]  # 每根TR=2
    assert abs(atr(ks, 14) - 2.0) < 1e-9


def test_rsi_extreme():
    assert f_rsi_extreme("long", 25, 30, 70)[0] == 1
    assert f_rsi_extreme("long", 35, 30, 70)[0] == 0
    assert f_rsi_extreme("short", 75, 30, 70)[0] == 1


def test_wick_rejection():
    # 底分型极值K：长下影（低点9，实体10-10.5）
    bar = k(10.5, 10.6, 9.0, 10.2)
    s, note = f_wick_rejection("long", bar, 0.5)
    assert s == 1, (s, note)
    # 无下影线
    bar2 = k(10, 11, 10, 10.8)
    assert f_wick_rejection("long", bar2, 0.5)[0] == 0


def test_taker_ratio():
    assert f_taker_ratio("long", k(1, 1, 1, 1, v=100, tb=70), 0.58)[0] == 1
    assert f_taker_ratio("long", k(1, 1, 1, 1, v=100, tb=40), 0.58)[0] == 0
    assert f_taker_ratio("short", k(1, 1, 1, 1, v=100, tb=30), 0.58)[0] == 1
    assert f_taker_ratio("long", k(1, 1, 1, 1, v=100, tb=0), 0.58)[0] == 0  # 无数据


def test_funding():
    assert f_funding("long", -0.001, 0.0005)[0] == 1
    assert f_funding("long", 0.0001, 0.0005)[0] == 0
    assert f_funding("short", 0.001, 0.0005)[0] == 1
    assert f_funding("long", None, 0.0005)[0] == 0


def test_btc_resonance():
    assert f_btc_resonance("long", "ETHUSDT", 1)[0] == 1
    assert f_btc_resonance("long", "BTCUSDT", 1)[0] == 0  # BTC自身不计
    assert f_btc_resonance("long", "ETHUSDT", -1)[0] == 0


def test_sl_atr_sane():
    assert sl_atr_sane(100, 99, 1.0, 0.5, 3.0)[0] is True     # 1xATR ok
    assert sl_atr_sane(100, 99.9, 1.0, 0.5, 3.0)[0] is False  # 0.1xATR 噪音
    assert sl_atr_sane(100, 95, 1.0, 0.5, 3.0)[0] is False    # 5xATR 过远
    assert sl_atr_sane(100, 99, 0.0, 0.5, 3.0)[0] is True     # 无ATR放行


def test_rsi_divergence():
    # 构造：先跌出一个底（前低10.0），反弹，再跌出更低的底（9.5），
    # 但第二段下跌斜率小 → RSI 抬高 → 底背离
    closes = []
    closes += [30 - i * 0.7 for i in range(25)]     # 急跌(超过RSI预热期), 底在 idx24≈13.2
    closes += [13.5 + i * 0.5 for i in range(8)]    # 反弹到 ~17
    closes += [17 - i * 0.18 for i in range(31)]    # 缓跌出更低的低点 ~11.6
    ks = [k(c, c + 0.3, c - 0.3, c, t=i) for i, c in enumerate(closes)]
    # 手工设两个分型位置做背离判定（不依赖 find_fractals 的具体输出）
    from app.engine.chan import Fractal
    prev = Fractal("bottom", 0, closes[24] - 0.3, 24, 26, 0)
    cur = Fractal("bottom", 0, closes[-1] - 0.35, len(closes) - 1, len(closes) - 1, 0)
    seq = rsi(closes, 14)
    assert cur.extreme_price < prev.extreme_price, "应是更低的低点"
    s, note = f_rsi_divergence("long", [prev, cur], cur, seq)
    assert s == 2, (s, note, seq[24], seq[-1])


def test_score_signal_aggregates():
    cfg = get_config()
    closes = [20 - i * 0.2 for i in range(60)]
    ks = [k(c + 0.1, c + 0.4, c - 0.4, c, v=100, tb=70, t=i) for i, c in enumerate(closes)]
    fractals = find_fractals(ks)
    from app.engine.chan import Fractal
    cur = fractals[-1] if fractals else Fractal("bottom", 0, closes[-1], len(ks) - 1, len(ks) - 1, 0)
    score, reasons, detail = score_signal(
        cfg, direction="long", symbol="ETHUSDT", tf="5m", klines=ks,
        fractals=fractals, cur=cur, confirm_bar=ks[-1],
        funding_rate=-0.001, trend_15m=1, btc_trend=1)
    # 单边下跌：RSI超卖+1, funding+1, taker70%+1, 5m共振+1, BTC共振+1 至少5分
    assert score >= 4, (score, reasons)
    assert len(detail) == 7, f"应有7个因子结果, got {list(detail)}"
    print(f"  score={score} reasons={reasons}")


def main():
    fns = [v for n, v in globals().items() if n.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL {len(fns)} TESTS PASSED")


if __name__ == "__main__":
    main()
