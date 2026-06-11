"""缠论引擎单元测试。直接 python tests/test_chan.py 运行。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.chan import (
    merge_klines, find_fractals, volume_ratio, prior_support,
    is_break_reclaim, aggregate, ema,
)
from app.engine.volume_profile import build_profile, nearest_hvn_above


def k(o, h, l, c, v=100, t=0):
    return {"open_time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


def seq(*bars):
    return [k(*b, t=i * 900000) for i, b in enumerate(bars)]


def test_merge_contains():
    # 第2根被第1根包含（上升方向：高取高、低取高）
    ks = seq((10, 12, 9, 11), (10.5, 11.5, 9.5, 10), (11, 13, 10, 12.5))
    m = merge_klines(ks)
    assert len(m) == 2, f"expect 2 merged, got {len(m)}"
    assert m[0].high == 12 and m[0].low == 9.5, f"merged wrong: {m[0]}"
    assert m[0].src_idx == [0, 1]


def test_bottom_fractal():
    # 明确的底分型：中间K低点和高点都最低
    ks = seq(
        (12, 13, 11, 11.5),   # 左
        (11.5, 11.8, 9.0, 9.5),  # 中（最低）
        (9.5, 12.5, 9.4, 12.2),  # 右（确认）
    )
    fs = find_fractals(ks)
    assert len(fs) == 1 and fs[0].kind == "bottom", f"got {fs}"
    assert fs[0].extreme_price == 9.0
    assert fs[0].extreme_src_idx == 1
    assert fs[0].confirm_src_idx == 2


def test_top_fractal():
    ks = seq(
        (10, 11, 9.5, 10.8),
        (10.8, 13, 10.5, 12.5),  # 中（最高）
        (12.5, 12.0, 10.0, 10.2),
    )
    fs = find_fractals(ks)
    assert len(fs) == 1 and fs[0].kind == "top", f"got {fs}"
    assert fs[0].extreme_price == 13


def test_no_fractal_on_trend():
    # 单边上涨无分型
    ks = seq((10, 11, 9, 10.8), (10.8, 12, 10.5, 11.8), (11.8, 13, 11.5, 12.8), (12.8, 14, 12.5, 13.8))
    fs = find_fractals(ks)
    assert fs == [], f"trend should have no fractal, got {fs}"


def test_volume_ratio():
    ks = [k(10, 11, 9, 10, v=100, t=i) for i in range(20)]
    ks.append(k(10, 11, 9, 10, v=300, t=20))
    assert abs(volume_ratio(ks, 20, 20) - 3.0) < 1e-9
    assert volume_ratio(ks, 5, 20) == 0.0  # 数据不足


def test_break_reclaim():
    # 前低 9.0；分型极值K跌破至 8.5；确认K收回到 9.6 之上
    bars = [
        (12, 13, 10, 11), (11, 11.5, 9.0, 10.5), (10.5, 12, 10, 11.8),  # 先形成前低9.0的底分型
        (11.8, 12.5, 11, 11.2), (11.2, 11.4, 10.2, 10.5),
        (10.5, 10.8, 9.5, 9.8),
        (9.8, 10.0, 8.5, 8.8),    # 跌破前低9.0
        (8.8, 10.5, 8.6, 10.2),   # 收回 → 确认K
    ]
    ks = seq(*bars)
    fs = find_fractals(ks)
    cur = fs[-1]
    assert cur.kind == "bottom", f"last fractal should be bottom: {fs}"
    sup = prior_support(ks, fs, cur, 30)
    assert sup == 9.0, f"support expect 9.0 got {sup}"
    assert is_break_reclaim(ks, cur, sup) is True


def test_aggregate_and_ema():
    ks = seq(*[(10 + i, 11 + i, 9 + i, 10.5 + i) for i in range(8)])
    h1 = aggregate(ks, 4)
    assert len(h1) == 2
    assert h1[0]["high"] == 14 and h1[0]["low"] == 9 and h1[0]["close"] == 13.5
    e = ema([1.0] * 10, 5)
    assert abs(e[-1] - 1.0) < 1e-9


def test_volume_profile_hvn():
    # 在 100-110 区间堆积大量成交，在 90-100 少量 → HVN 应该在上方密集区
    ks = []
    for i in range(50):
        ks.append(k(105, 108, 103, 106, v=1000, t=i))   # 密集区 103-108
    for i in range(10):
        ks.append(k(95, 97, 93, 95, v=50, t=50 + i))
    prof = build_profile(ks, bins=40)
    hvn = nearest_hvn_above(prof, 95.0)
    # 桶宽=(108-93)/40=0.375, 密集区起点103所在桶中心可低至102.75附近
    assert hvn is not None and 102.5 <= hvn <= 108.5, f"hvn={hvn}"


def main():
    fns = [v for n, v in globals().items() if n.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL {len(fns)} TESTS PASSED")


if __name__ == "__main__":
    main()
