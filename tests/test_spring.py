"""弹簧策略V3单元测试。python tests/test_spring.py 运行。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config
from app.engine.spring import detect_trigger, recovery_score, is_quiet
from app.engine.signals import SignalEngine


def k(o, h, l, c, v=100.0, t=0):
    return {"open_time": t * 900000, "open": o, "high": h, "low": l, "close": c,
            "volume": v, "taker_buy": v / 2}


def base_klines(n=60, price=10.0, vol=100.0):
    """横盘稳态序列。"""
    return [k(price, price + 0.1, price - 0.1, price, v=vol, t=i) for i in range(n)]


def make_cfg(**over):
    cfg = Config()
    cfg.set_override("strategy", "spring_v3")
    cfg.set_override("spring.btc_filter", False)
    cfg.set_override("spring.min_rr", 0)   # 状态机测试不受RR门槛影响(构造数据VP贴近入场)
    for kk, vv in over.items():
        cfg.set_override(kk, vv)
    return cfg


class FakeDB:
    def log(self, *a, **kw):
        pass


def test_quiet():
    ks = base_klines(40)
    assert is_quiet(ks, 39, 15, 1.5) is True
    ks[30]["volume"] = 200  # 中途放过量 → 非稳态
    assert is_quiet(ks, 39, 15, 1.5) is False


def test_trigger_detect():
    ks = base_klines(60)
    # 巨量长阴破位: 量500(5x), 振幅大, 新低
    ks.append(k(10.0, 10.05, 9.0, 9.2, v=500, t=60))
    d, detail = detect_trigger(ks, 60, atr_val=0.2)
    assert d == "long", (d, detail)
    assert detail["vol_ratio"] >= 3


def test_trigger_needs_quiet():
    ks = base_klines(60)
    ks[55]["volume"] = 300  # 稳态被破坏
    ks.append(k(10.0, 10.05, 9.0, 9.2, v=500, t=60))
    d, _ = detect_trigger(ks, 60, atr_val=0.2)
    assert d is None


def test_recovery_score():
    trig = {"high": 10.0, "low": 9.0, "open": 9.9}   # 阴线 中点9.5 实体顶9.9
    assert recovery_score("long", trig, 9.4) == 0          # 未到中点
    assert abs(recovery_score("long", trig, 9.5) - 50) < 1e-9
    mid = recovery_score("long", trig, 9.7)
    assert 50 < mid < 100
    assert recovery_score("long", trig, 9.9) == 100        # 吞没实体顶
    assert recovery_score("long", trig, 12.0) == 100       # 封顶


def test_state_machine_watch_then_buy1():
    cfg = make_cfg()
    eng = SignalEngine(cfg, FakeDB())
    ks = base_klines(60)
    sym, tf = "TESTUSDT", "15m"
    # 触发K
    ks.append(k(10.0, 10.05, 9.0, 9.2, v=500, t=60))
    assert eng.evaluate(sym, tf, list(ks)) is None
    assert (sym, tf) in eng._state and eng._state[(sym, tf)]["phase"] == "recovery"
    # 第1根反弹: 收回到中点上方(9.6 > 9.525) 缩量 → watch信号
    ks.append(k(9.2, 9.65, 9.15, 9.6, v=120, t=61))
    sig = eng.evaluate(sym, tf, list(ks))
    assert sig is not None and sig.extra["type"] == "watch", sig
    assert 50 <= sig.extra["score"] < 100
    assert eng._state[(sym, tf)]["phase"] == "coord"
    print(f"  watch score={sig.extra['score']} reason={sig.reason[:50]}")


def test_state_machine_engulf_buy1():
    cfg = make_cfg()
    eng = SignalEngine(cfg, FakeDB())
    ks = base_klines(60)
    sym, tf = "ENGULF", "15m"
    ks.append(k(10.0, 10.05, 9.0, 9.2, v=500, t=60))  # 触发: 开10 收9.2(实体顶=10)
    eng.evaluate(sym, tf, list(ks))
    # 巨量吞没K: 量600>500, 收盘10.1 ≥ 开盘10 → 坐标升级 + 100分一买
    ks.append(k(9.2, 10.2, 9.1, 10.1, v=600, t=61))
    sig = eng.evaluate(sym, tf, list(ks))
    assert sig is not None and sig.extra["type"] == "buy1", sig
    assert sig.extra["score"] == 100
    assert "坐标升级" in "".join(sig.extra["labels"])
    print(f"  buy1 labels={sig.extra['labels']}")


def test_spring_fake_break():
    cfg = make_cfg()
    eng = SignalEngine(cfg, FakeDB())
    ks = base_klines(60)
    sym, tf = "SPRING", "15m"
    ks.append(k(10.0, 10.05, 9.0, 9.2, v=500, t=60))   # 触发 L=9.0
    eng.evaluate(sym, tf, list(ks))
    ks.append(k(9.2, 9.7, 9.15, 9.6, v=120, t=61))      # watch → coord(坐标=触发K)
    sig = eng.evaluate(sym, tf, list(ks))
    assert sig and sig.extra["type"] == "watch"
    # 盘中假破坐标低点9.0但收盘收回 → 武装弹簧
    ks.append(k(9.5, 9.55, 8.95, 9.3, v=80, t=62))
    assert eng.evaluate(sym, tf, list(ks)) is None
    assert eng._state[(sym, tf)].get("spring_armed") is True
    # 放量重启K: 阳线 收过前K高点 量更大 → 💎spring
    ks.append(k(9.3, 9.8, 9.25, 9.75, v=200, t=63))
    sig = eng.evaluate(sym, tf, list(ks))
    assert sig is not None and sig.extra["type"] == "spring", sig
    assert (sym, tf) not in eng._state
    print(f"  spring sl={sig.sl} entry={sig.entry}")


def test_invalidation_close_break():
    cfg = make_cfg()
    eng = SignalEngine(cfg, FakeDB())
    ks = base_klines(60)
    sym, tf = "DEAD", "15m"
    ks.append(k(10.0, 10.05, 9.0, 9.2, v=500, t=60))
    eng.evaluate(sym, tf, list(ks))
    ks.append(k(9.2, 9.7, 9.15, 9.6, v=120, t=61))
    eng.evaluate(sym, tf, list(ks))
    # 连续3根收盘破坐标低点 → 实质跌破失效
    for j in range(3):
        ks.append(k(9.0, 9.05, 8.7 - j * 0.1, 8.8 - j * 0.1, v=90, t=62 + j))
        eng.evaluate(sym, tf, list(ks))
    assert (sym, tf) not in eng._state
    assert any("失效" in n for n in eng.notices)
    print(f"  notice={eng.notices[-1][:40]}")


def test_short_direction():
    cfg = make_cfg()
    eng = SignalEngine(cfg, FakeDB())
    ks = base_klines(60)
    sym, tf = "SHORTX", "15m"
    # 巨量长阳冲高(做空布局触发)
    ks.append(k(10.0, 11.0, 9.95, 10.9, v=500, t=60))
    assert eng.evaluate(sym, tf, list(ks)) is None
    st = eng._state.get((sym, tf))
    assert st and st["direction"] == "short"
    # 回落过中点(10.5)下方 缩量 → watch(做空)
    ks.append(k(10.9, 10.95, 10.3, 10.35, v=120, t=61))
    sig = eng.evaluate(sym, tf, list(ks))
    assert sig is not None and sig.direction == "short" and sig.extra["type"] == "watch"
    print(f"  short watch score={sig.extra['score']}")


def main():
    fns = [v for n, v in globals().items() if n.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL {len(fns)} TESTS PASSED")


if __name__ == "__main__":
    main()
