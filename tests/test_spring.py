"""策略V4(破位+底分型)单元测试。python tests/test_spring.py 运行。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config
from app.engine.spring import (
    detect_breakdown, is_bottom_fractal, is_top_fractal, is_main_k,
)
from app.engine.signals import SignalEngine


def k(o, h, l, c, v=100.0, t=0):
    return {"open_time": t * 300000, "open": o, "high": h, "low": l, "close": c,
            "volume": v, "taker_buy": v / 2}


def base(n=60, price=10.0, vol=100.0):
    return [k(price, price + 0.1, price - 0.1, price, v=vol, t=i) for i in range(n)]


def make_cfg(**over):
    cfg = Config()
    cfg.set_override("strategy", "spring_v4")
    cfg.set_override("spring.btc_filter", False)
    cfg.set_override("spring.min_rr", 0)      # 构造数据无前高,放开RR门槛专测形态
    cfg.set_override("spring.vol_mult", 4.0)
    cfg.set_override("spring.newlow_lookback", 20)
    for kk, vv in over.items():
        cfg.set_override(kk, vv)
    return cfg


class FakeDB:
    def log(self, *a, **kw):
        pass


def test_detect_breakdown_long():
    ks = base(60)
    ks.append(k(10.0, 10.0, 9.0, 9.2, v=500, t=60))   # 放量长阴破前低
    d, detail = detect_breakdown(ks, 60, vol_mult=4, newlow_lookback=20)
    assert d == "long", (d, detail)
    assert detail["vol_ratio"] >= 4


def test_detect_breakdown_short():
    ks = base(60)
    ks.append(k(10.0, 11.0, 10.0, 10.8, v=500, t=60))  # 放量长阳破前高
    d, _ = detect_breakdown(ks, 60, vol_mult=4, newlow_lookback=20)
    assert d == "short"


def test_breakdown_needs_newlow():
    ks = base(60)
    ks.append(k(10.0, 10.0, 9.95, 9.2, v=500, t=60))   # 放量但没破前低(9.95>9.9)
    d, _ = detect_breakdown(ks, 60, vol_mult=4, newlow_lookback=20)
    assert d is None


def test_fractals():
    fb = [k(12, 13, 11, 11.5), k(11.5, 11.8, 9.0, 9.5), k(9.5, 12.5, 9.4, 12.2)]
    assert is_bottom_fractal(fb, 2) is True
    ft = [k(10, 11, 9.5, 10.8), k(10.8, 13, 10.5, 12.5), k(12.5, 12, 10, 10.2)]
    assert is_top_fractal(ft, 2) is True


def test_main_k():
    assert is_main_k([k(10, 11, 9, 10.5, v=600)], 0, ref_vol=500, atr_val=1.0, range_atr_min=1.2) is True
    assert is_main_k([k(10, 10.2, 9.9, 10, v=600)], 0, ref_vol=500, atr_val=1.0, range_atr_min=1.2) is False  # 振幅不足
    assert is_main_k([k(10, 12, 9, 11, v=400)], 0, ref_vol=500, atr_val=1.0, range_atr_min=1.2) is False  # 量不足


def test_buy1_long():
    eng = SignalEngine(make_cfg(), FakeDB())
    sym, tf = "AAA", "5m"
    ks = base(60)
    ks.append(k(10.0, 10.0, 9.0, 9.2, v=500, t=60))      # 破位K 顶部=10.0
    assert eng.evaluate(sym, tf, list(ks)) is None
    assert eng._state[(sym, tf)]["phase"] == "await_buy1"
    ks.append(k(9.2, 9.3, 8.8, 9.0, v=100, t=61))        # 继续探底(底分型中间K)
    assert eng.evaluate(sym, tf, list(ks)) is None
    ks.append(k(9.0, 10.2, 8.95, 10.1, v=100, t=62))     # 收回到破位K顶部之上 → 一买
    sig = eng.evaluate(sym, tf, list(ks))
    assert sig is not None and sig.extra["type"] == "buy1", sig
    assert abs(sig.entry - 10.0) < 1e-9, f"入场应=破位K开盘10.0, got {sig.entry}"
    assert sig.sl < 8.8, sig.sl                          # 止损在底分型低点下方
    assert eng._state[(sym, tf)]["phase"] == "await_buy2"
    print(f"  buy1 entry={sig.entry} sl={sig.sl} tp={sig.tp} rr={sig.rr}")


def feed(eng, sym, tf, ks, bars):
    """逐根追加并 evaluate，返回每根的信号(状态机逐根驱动)。"""
    out = []
    for b in bars:
        ks.append(b)
        out.append(eng.evaluate(sym, tf, list(ks)))
    return out


def test_buy2_higher_low():
    eng = SignalEngine(make_cfg(), FakeDB())
    sym, tf = "BBB", "5m"
    ks = base(60)
    sigs = feed(eng, sym, tf, ks, [
        k(10.0, 10.0, 9.0, 9.2, v=500, t=60),   # 破位
        k(9.2, 9.3, 8.8, 9.0, v=100, t=61),     # 探底
        k(9.0, 10.2, 8.95, 10.1, v=100, t=62),  # 一买, prot=8.8
        k(10.1, 10.2, 9.5, 9.6, v=100, t=63),
        k(9.6, 9.7, 9.2, 9.3, v=100, t=64),     # 中间K 低点9.2 > prot8.8
        k(9.3, 10.0, 9.25, 9.8, v=100, t=65),   # 收回过中间K高点 → 二买
    ])
    assert sigs[2] and sigs[2].extra["type"] == "buy1", sigs[2]
    sig = sigs[5]
    assert sig is not None and sig.extra["type"] == "buy2", sig
    assert 8.8 < sig.sl < 9.2, f"二买止损应在更高的低点9.2下方: {sig.sl}"
    assert (sym, tf) not in eng._state
    print(f"  buy2 entry={sig.entry} sl={sig.sl}")


def test_invalidation():
    eng = SignalEngine(make_cfg(), FakeDB())
    sym, tf = "CCC", "5m"
    ks = base(60)
    sigs = feed(eng, sym, tf, ks, [
        k(10.0, 10.0, 9.0, 9.2, v=500, t=60),
        k(9.2, 9.3, 8.8, 9.0, v=100, t=61),
        k(9.0, 10.2, 8.95, 10.1, v=100, t=62),   # 一买, prot=8.8
        k(10.1, 10.2, 8.5, 8.6, v=100, t=63),    # 收盘8.6 < prot8.8 → 失效
    ])
    assert sigs[2] and sigs[2].extra["type"] == "buy1"
    assert sigs[3] is None
    assert (sym, tf) not in eng._state
    assert any("失效" in n for n in eng.notices)
    print(f"  notice={eng.notices[-1][:36]}")


def test_buy1_short():
    eng = SignalEngine(make_cfg(), FakeDB())
    sym, tf = "DDD", "5m"
    ks = base(60)
    ks.append(k(10.0, 11.0, 10.0, 10.8, v=500, t=60))     # 放量破前高 顶部=10.0(开盘)
    eng.evaluate(sym, tf, list(ks))
    assert eng._state[(sym, tf)]["direction"] == "short"
    ks.append(k(10.8, 11.2, 10.7, 11.0, v=100, t=61))     # 顶分型中间K(最高11.2)
    eng.evaluate(sym, tf, list(ks))
    ks.append(k(11.0, 11.05, 9.8, 9.9, v=100, t=62))      # 收回到破位K开盘10.0之下 → 一卖
    sig = eng.evaluate(sym, tf, list(ks))
    assert sig is not None and sig.direction == "short" and sig.extra["type"] == "buy1"
    print(f"  short buy1 entry={sig.entry} sl={sig.sl}")


def main():
    fns = [v for n, v in globals().items() if n.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL {len(fns)} TESTS PASSED")


if __name__ == "__main__":
    main()
