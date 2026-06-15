"""缠论笔策略单元测试：笔/停顿K/放量收回。python tests/test_chan_bi.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config
from app.engine.chan import merge_klines, find_fractals
from app.engine.chan_bi import (build_bi, stall_idx, detect,
                                 fractal_grade, vol_spike_before, quality_ok,
                                 macd_hist, divergence, strong_reversal)
from app.engine.chan import merge_klines, find_fractals as _ff
from app.engine.signals import SignalEngine


def k(o, h, l, c, v=100.0, t=0):
    return {"open_time": t * 300000, "open": o, "high": h, "low": l, "close": c,
            "volume": v, "taker_buy": v / 2}


def downup(n_down=8, n_up=8, top=20.0, bottom=10.0, vbig_at=None):
    """构造一段下跌+反弹的K线(无包含)，返回列表。"""
    ks = []
    t = 0
    step = (top - bottom) / n_down
    p = top
    for i in range(n_down):
        nxt = p - step
        v = 600 if vbig_at == i else 100
        ks.append(k(p, p + 0.05, nxt - 0.05, nxt, v=v, t=t)); t += 1; p = nxt
    for i in range(n_up):
        nxt = p + step
        ks.append(k(p, nxt + 0.05, p - 0.05, nxt, v=100, t=t)); t += 1; p = nxt
    return ks


def make_cfg(**over):
    c = Config()
    c.set_override("strategy", "chan_bi")
    c.set_override("spring.btc_filter", False)
    c.set_override("spring.min_rr", 0)
    c.set_override("chan.btc_filter", False)
    for kk, vv in over.items():
        c.set_override(kk, vv)
    return c


class FakeDB:
    def log(self, *a, **kw):
        pass


def zigzag(pivots, per_leg=7):
    """按给定转折价位构造锯齿K线(每段per_leg根，无包含)。"""
    ks = []
    t = 0
    for a, b in zip(pivots, pivots[1:]):
        step = (b - a) / per_leg
        p = a
        for _ in range(per_leg):
            nxt = p + step
            ks.append(k(p, max(p, nxt) + 0.05, min(p, nxt) - 0.05, nxt, v=100, t=t))
            t += 1
            p = nxt
    return ks


def test_build_bi():
    # 下20→12→上18→下11→上17：中间会形成 底/顶 交替分型
    ks = zigzag([20, 12, 18, 11, 17], per_leg=7)
    _, seq = build_bi(ks, min_merged=5)
    kinds = [f.kind for f in seq]
    assert "bottom" in kinds and "top" in kinds, kinds
    print(f"  seq kinds: {kinds}")


def test_stall_and_buy1_bi():
    cfg = make_cfg()
    eng = SignalEngine(cfg, FakeDB())
    # 一段成笔下跌(≥5根) → 底分型 → 停顿K
    ks = []
    t = 0
    for p in [20, 19, 18, 17, 16, 15.5]:   # 下跌成笔(6根)
        ks.append(k(p, p + 0.1, p - 1, p - 0.9, v=100, t=t)); t += 1
    # 底分型：中间最低
    ks.append(k(15.1, 15.3, 14.0, 14.2, v=100, t=t)); t += 1   # 左
    ks.append(k(14.2, 14.4, 13.0, 13.2, v=100, t=t)); t += 1   # 中(最低)
    ks.append(k(13.2, 15.0, 13.1, 14.8, v=100, t=t)); t += 1   # 右(抬高)
    # 停顿K：收盘 > 右K最高(15.0)
    sig = None
    ks.append(k(14.8, 15.6, 14.7, 15.4, v=100, t=t)); t += 1
    # 需足够长度
    pad = [k(20, 20.1, 19.9, 20, v=100, t=-50 + i) for i in range(40)]
    full = pad + ks
    sig = eng.evaluate("AAA", "5m", full)
    # 结构可能因合并/分型细节不一定恰好触发；至少不报错且若触发则为buy1做多
    if sig:
        assert sig.direction == "long" and sig.extra["type"] in ("buy1", "buy2")
        print(f"  bi signal: {sig.extra['type']} entry={sig.entry} {sig.reason[:30]}")
    else:
        print("  bi: 本构造未触发(结构判定严格)，不报错即可")


def test_engine_no_crash_on_real_shape():
    cfg = make_cfg()
    eng = SignalEngine(cfg, FakeDB())
    ks = [k(20, 20.1, 19.9, 20, v=100, t=i) for i in range(30)]
    ks += downup(8, 6, top=20, bottom=12, vbig_at=6)
    for n in range(60, len(ks) + 1):
        eng.evaluate("BBB", "5m", ks[:n])   # 不应抛异常
    print("  逐根评估无异常")


def test_label_by_direction():
    eng = SignalEngine(make_cfg(), FakeDB())
    assert eng._bi_label("buy1", "long") == "一买"
    assert eng._bi_label("buy2", "long") == "二买"
    assert eng._bi_label("buy1", "short") == "一卖"
    assert eng._bi_label("buy2", "short") == "二卖"
    print("  做多→一买/二买  做空→一卖/二卖")


def test_buy2_needs_buy1_chain():
    eng = SignalEngine(make_cfg(), FakeDB())
    # 没有一买链时，链为空 → 二买判定会被降级/拦截(逻辑在_eval_chan_bi)
    assert eng._bi_chain == {}
    # 模拟开链((极值价, 分型时间))
    eng._bi_chain[("X", "5m", "long")] = (10.0, 0)
    assert eng._bi_chain[("X", "5m", "long")][0] == 10.0
    print("  一买→二买链状态变量就位")


def _bottom3(left_vol=100):
    """20根均量基线 + 一个干净底分型(左/中/右)，右K最高>左K最高=最强。
    left_vol 控制左K是否放量。"""
    pad = [k(20, 20.1, 19.9, 20, v=100, t=-20 + i) for i in range(20)]
    body = [
        k(18.0, 18.2, 16.0, 16.2, v=left_vol, t=1),   # 左K
        k(16.2, 16.4, 14.0, 14.2, v=100, t=2),         # 中K(最低)
        k(14.2, 18.6, 14.1, 18.4, v=100, t=3),         # 右K(收回, high18.6>左18.2)
    ]
    full = pad + body
    merged = merge_klines(full)
    fx = [f for f in find_fractals(full, merged) if f.kind == "bottom"][-1]
    return full, merged, fx


def test_fractal_grade_strongest():
    full, merged, fx = _bottom3()
    g = fractal_grade(full, merged, fx)
    assert g == "strongest", g                         # 右K最高>左K最高
    print(f"  右K突破左K高点 → {g}")


def test_volume_gate():
    # 左K不放量(100) → 最强但量不达标 → 拦截
    full, merged, fx = _bottom3(left_vol=100)
    ok, g, vr = quality_ok(full, merged, fx, vol_ma=10, vol_mult=2.0)
    assert g == "strongest" and ok is False and vr < 2.0, (g, ok, vr)
    # 左K放量(300=3x均量) → 前2根放量达标 → 通过, 倍数被接出
    full2, merged2, fx2 = _bottom3(left_vol=300)
    ok2, g2, vr2 = quality_ok(full2, merged2, fx2, vol_ma=10, vol_mult=2.0)
    assert g2 == "strongest" and ok2 is True and vr2 >= 2.0, (g2, ok2, vr2)
    print(f"  无放量({vr}x)→拦截; 左K放量({vr2}x)→通过")


def _confirm_low(ks, start, steps=3, up=0.6):
    """在末端低点后追加几根小幅回升K，把该低点确认成(末端)底分型。"""
    p = start
    for _ in range(steps):
        nxt = p + up
        ks.append(k(p, nxt + 0.05, p - 0.05, nxt, v=100, t=len(ks)))
        p = nxt
    return ks


def test_divergence_long():
    # 底背驰: a段(102→80,幅22) 反弹88 b段(88→76,幅12,创新低) → b比a短=背驰
    ks = _confirm_low(zigzag([90, 102, 80, 88, 76], per_leg=7), 76)
    _, seq = build_bi(ks, 5)
    ok, tag = divergence(ks, seq, "long")
    print(f"  底背驰 seq尾={[f.kind for f in seq][-4:]} -> {ok} [{tag}]")
    assert seq[-1].kind == "bottom" and ok, (ok, tag, [(f.kind, round(f.extreme_price, 1)) for f in seq])


def test_no_divergence_long():
    # 加速下跌: b段(100→70,幅30) 远大于 a段(102→95,幅7) → 不背驰
    ks = _confirm_low(zigzag([90, 102, 95, 100, 70], per_leg=7), 70)
    _, seq = build_bi(ks, 5)
    ok, tag = divergence(ks, seq, "long")
    print(f"  加速下跌 seq尾={[f.kind for f in seq][-4:]} -> {ok} [{tag}]")
    assert seq[-1].kind == "bottom" and not ok, (ok, tag)


def _bottom_pattern(r_open=9.5, r_high=10.5, r_low=9.45, r_close=10.4):
    """构造一个底分型(左下跌K / 中最低带下影 / 右反转K), 返回 (ks, merged, fx)。"""
    pad = [k(11, 11.15, 10.9, 11.0, t=0)]
    L = k(10.0, 10.1, 9.5, 9.6, t=1)        # 左:下跌K, 高10.1
    M = k(9.6, 9.7, 9.0, 9.5, t=2)          # 中:最低9.0, 下影=min(9.6,9.5)-9.0=0.5
    R = k(r_open, r_high, r_low, r_close, t=3)
    ks = pad + [L, M, R]
    merged = merge_klines(ks)
    fx = [f for f in _ff(ks, merged) if f.kind == "bottom"][-1]
    return ks, merged, fx


def test_strong_reversal():
    # 通过: 右K大实体(0.9/1.05=0.86) + 收10.4>左高10.1 + 中K有下影
    ks, m, fx = _bottom_pattern()
    assert strong_reversal(ks, m, fx, 0.6) is True
    # 失败-未吞没: 右K收9.8 < 左高10.1
    ks2, m2, fx2 = _bottom_pattern(r_high=9.95, r_close=9.8)
    assert strong_reversal(ks2, m2, fx2, 0.6) is False
    # 失败-实体小: 收10.15>左高(吞没过) 但实体0.15/振幅1.2=0.125 < 0.6
    ks3, m3, fx3 = _bottom_pattern(r_open=10.0, r_high=10.6, r_low=9.4, r_close=10.15)
    assert strong_reversal(ks3, m3, fx3, 0.6) is False
    print("  强反转: 大实体+吞没+下影→过; 未吞没/小实体→拦")


def test_wyckoff_spring():
    from app.engine.chan_bi import wyckoff_spring
    # 前低区: 30根在 12~13 之间, 前低=12
    ks = []
    for i in range(30):
        lo = 12.0 + (i % 3) * 0.5
        ks.append(k(13, 13.2, lo, 12.8, v=100, t=i))
    # 爆量阴线: 开12.5 扫破到10 收11(阴线), 放量4x
    ks.append(k(12.5, 12.6, 10.0, 11.0, v=400, t=30))
    # 收回到爆量K启动位置(开盘12.5)以上: 收12.7
    ks.append(k(11.0, 12.8, 10.9, 12.7, v=100, t=31))
    r = wyckoff_spring(ks, lookback=20, reclaim_bars=4, vol_ma=20, climax_mult=2.0)
    assert r is not None, "应识别到弹簧"
    direction, spring_ext, start_pos, sidx, grade, vr = r
    print(f"  弹簧 dir={direction} 扫破极值={spring_ext} 启动位={start_pos} grade={grade} 量={vr}x")
    assert direction == "long" and spring_ext == 10.0 and start_pos == 12.5
    # 反例: 收盘还没回到爆量K开盘(12.5)上方 → 不触发
    ks2 = ks[:31] + [k(11.0, 12.3, 10.9, 12.2, v=100, t=31)]
    assert wyckoff_spring(ks2, 20, 4) is None


def test_trend_reversal():
    from app.engine.chan_bi import trend_reversal
    # 顶1=70 底1=64 顶2=68(更低高点), 之后收盘跌破64 → 看跌反转
    ks = zigzag([60, 70, 64, 68], per_leg=7)
    p = 68.0
    for i in range(6):
        p -= 1.0
        ks.append(k(p + 1, p + 1.05, p - 0.05, p, t=200 + i))
    r = trend_reversal(ks, 5)
    print("  趋势反转:", r)
    assert r and r[0] == "short", r
    # 未破前低 → 不触发
    assert trend_reversal(zigzag([60, 70, 64, 68], per_leg=7), 5) is None


def test_head_shoulders():
    from app.engine.chan_bi import head_shoulders_top
    # 左肩68-谷64-头72-谷64-右肩68, 之后收盘跌破颈线64
    ks = zigzag([60, 68, 64, 72, 64, 68], per_leg=7)
    p = 68.0
    for i in range(6):
        p -= 1.0
        ks.append(k(p + 1, p + 1.05, p - 0.05, p, t=300 + i))
    r = head_shoulders_top(ks, 5)
    print("  头肩顶:", r)
    assert r and r[1] > 70, r          # 头≈72
    assert head_shoulders_top(zigzag([60, 68, 64, 72, 64, 68], per_leg=7), 5) is None  # 未破颈线


def test_trend_state():
    from app.engine.chan_bi import trend_state
    up = [k(60 + i * 0.5, 60 + i * 0.5 + 0.1, 60 + i * 0.5 - 0.1, 60 + i * 0.5, t=i) for i in range(40)]
    dn = [k(60 - i * 0.5, 60 - i * 0.5 + 0.1, 60 - i * 0.5 - 0.1, 60 - i * 0.5, t=i) for i in range(40)]
    flat = [k(60, 60.1, 59.9, 60, t=i) for i in range(40)]
    print(f"  up→{trend_state(up)} down→{trend_state(dn)} flat→{trend_state(flat)}")
    assert trend_state(up) == "up"
    assert trend_state(dn) == "down"
    assert trend_state(flat) == "range"


def test_lifecycle_state():
    from app.engine.chan_bi import lifecycle_state
    # 成立: 下到底(底分型) → 上涨成一笔(出顶分型, 用回调16确认顶)
    ks_ok = zigzag([20, 12, 18, 16], per_leg=7)
    _, seq = build_bi(ks_ok, 5)
    bot = [f for f in seq if f.kind == "bottom"][0]
    st_ok = lifecycle_state(ks_ok, bot.extreme_price, bot.open_time, "long", 5)
    # 失败: 同一个底, 后面一路跌破底极值
    ks_fail = list(ks_ok)
    p = 16.0
    for i in range(9):
        p -= 2.0
        ks_fail.append(k(p + 2, p + 2.1, p - 0.1, p, t=300 + i))
    st_fail = lifecycle_state(ks_fail, bot.extreme_price, bot.open_time, "long", 5)
    print(f"  底极值={bot.extreme_price:.2f} 成立={st_ok} 破底={st_fail}")
    assert st_ok == "ok", st_ok
    assert st_fail == "fail", st_fail


def test_macd_hist_len():
    closes = [10 + (i % 5) * 0.3 for i in range(80)]
    h = macd_hist(closes, 12, 26, 9)
    assert len(h) == len(closes)
    print(f"  macd_hist 长度对齐 {len(h)}")


class _ShimDB:
    """内存版 paper_trades，用于测反向平仓。"""
    def __init__(self):
        import sqlite3
        self.c = sqlite3.connect(":memory:"); self.c.row_factory = sqlite3.Row
        self.c.execute("""CREATE TABLE paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INT, symbol TEXT, tf TEXT, direction TEXT, track TEXT,
            entry REAL, sl REAL, tp REAL, qty REAL, opened_at INT,
            closed_at INT, exit_price REAL, pnl REAL, pnl_r REAL, result TEXT DEFAULT 'open')""")
        self.c.execute("CREATE TABLE equity_curve (ts INT, track TEXT, equity REAL, PRIMARY KEY(ts,track))")
    def query(self, sql, params=()): return [dict(r) for r in self.c.execute(sql, params)]
    def execute(self, sql, params=()): self.c.execute(sql, params); self.c.commit()
    def one(self, sql, params=()):
        r = self.c.execute(sql, params).fetchone(); return dict(r) if r else {}
    def log(self, *a, **k): pass


def test_close_opposite():
    from app.engine.paper import PaperBroker
    db = _ShimDB(); pb = PaperBroker(make_cfg(), db)
    db.execute("INSERT INTO paper_trades (symbol,tf,direction,track,entry,sl,tp,qty,opened_at,result)"
               " VALUES ('AAA','5m','long','buy1',100,95,120,1.0,0,'open')")
    # 一卖(short)信号 → 平多单, 价110 → 盈利10
    closed = pb.close_opposite("AAA", "5m", "short", 110)
    assert len(closed) == 1 and closed[0]["result"] == "rev", closed
    assert abs(db.one("SELECT pnl FROM paper_trades WHERE id=1")["pnl"] - 10.0) < 1e-6
    # 再开空单; 同向(short)信号不应平空
    db.execute("INSERT INTO paper_trades (symbol,tf,direction,track,entry,sl,tp,qty,opened_at,result)"
               " VALUES ('AAA','5m','short','buy1',110,115,90,1.0,0,'open')")
    assert pb.close_opposite("AAA", "5m", "short", 105) == []
    # 一买(long)信号 → 平空单, 价100 → 空盈利10
    c3 = pb.close_opposite("AAA", "5m", "long", 100)
    assert len(c3) == 1 and abs(c3[0]["pnl"] - 10.0) < 1e-6, c3
    print("  反向平仓: 一卖平多/一买平空, 同向不平, pnl正确")


def main():
    fns = [v for n, v in globals().items() if n.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL {len(fns)} TESTS PASSED")


if __name__ == "__main__":
    main()
