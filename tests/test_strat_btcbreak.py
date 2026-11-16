"""btcbreak_pump 联动规则单元测试。只验证形态判定与因果性, 不涉及任何盈亏统计。
python -m pytest tests/test_strat_btcbreak.py -q
"""
import functools
import importlib.util
import json
import math
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engine.chan import find_fractals, merge_klines  # noqa: E402
from app.engine.chan_bi import stall_after  # noqa: E402
from app.engine.strat_btcbreak import (  # noqa: E402
    MIN_WINDOW_COVERAGE, PRE_BARS, BtcBreakParams, BtcBreakRule, episodes_from_events,
    event_instant, replay, sort_events, tf_to_ms)

TF = 300_000
BTC, PUMP = "BTCUSDT", "PUMPUSDT"
SMALL = dict(lookback_hours=2.0, vol_ma=5, ttl_hours=1.0)      # 窗口 24 根, 有效期 12 根
T = 30                                                         # 默认破位K下标(窗口已铺满)


def P(**kw):
    d = dict(SMALL)
    d.update(kw)
    return BtcBreakParams(**d)


def kbar(i, o, h, l, c, v=100.0):
    return {"open_time": i * TF, "open": o, "high": h, "low": l, "close": c, "volume": v}


def btc_series(n, breaks=(), level=100.0, break_vol=300.0):
    """平盘(low=level-0.5), 在 breaks 下标处放量破位: 收盘比之前所有 low 都低, 之后在新台阶平盘。"""
    out, cur = [], level
    for i in range(n):
        if i in breaks:
            new = cur - 2.0
            out.append(kbar(i, cur, cur + 0.2, new - 0.5, new, v=break_vol))
            cur = new
        else:
            out.append(kbar(i, cur, cur + 0.5, cur - 0.5, cur))
    return out


def pump_series(spec, h0=2000):
    """每个字符一根K(相对上一根, 单位 0.01):
       d=阶梯下行(不包含) u=阶梯上行(弱收, 收不过前高) S=强阳(收盘 > 前高) i=内包K。"""
    out, h, l = [], h0, h0 - 20
    for i, ch in enumerate(spec):
        if ch == "d":
            h, l = h - 10, l - 10
            hi, lo, c = h, l, l + 2
        elif ch == "u":
            h, l = h + 10, l + 10
            hi, lo, c = h, l, l + 2
        elif ch == "S":
            h, l = h + 30, l + 10
            hi, lo, c = h, l, h - 2
        elif ch == "i":
            hi, lo = h - 3, l + 3
            c = (hi + lo) // 2
        else:
            raise ValueError(ch)
        out.append(kbar(i, c / 100, hi / 100, lo / 100, c / 100))
    return out


def feed(rule, btc, pump, order="bp"):
    """按 open_time 升序喂; order: 'bp'=BTC先 'pb'=PUMP先, 或 callable(t)->'bp'/'pb'。"""
    bt = {b["open_time"]: b for b in btc}
    pt = {b["open_time"]: b for b in pump}
    events = []
    for t in sorted(set(bt) | set(pt)):
        o = order(t) if callable(order) else order
        for who in o:
            src, sym = (bt, BTC) if who == "b" else (pt, PUMP)
            if t in src:
                events.extend(rule.on_bar(sym, src[t]))
    return events


def kinds(events):
    return [e["type"] for e in events]


def one(events, kind):
    got = [e for e in events if e["type"] == kind]
    assert len(got) == 1, kinds(events)
    return got[0]


# ============================ 触发条件 ============================

def test_trigger_fires_when_both_conditions_hold():
    r = replay(btc_series(40, {T}), [], P())
    ev = one(r["events"], "armed")
    tr = ev["trigger"]
    assert tr["symbol"] == BTC and tr["time"] == T * TF
    assert tr["close"] == 98.0
    assert tr["ref_low"] == 99.5
    assert tr["ref_low_time"] == (T - 1) * TF          # 并列最低取最近一根
    assert tr["volume"] == 300.0 and tr["vol_ma"] == 100.0 and tr["vol_ratio"] == 3.0
    assert ev["expires_at"] == (T + 1) * TF + 3_600_000
    assert ev["episode_id"] == T * TF and ev["rule"] == "btcbreak_pump"


def test_trigger_needs_volume():
    assert replay(btc_series(40, {T}, break_vol=199.0), [], P())["events"] == []


def test_trigger_needs_close_below_low():
    btc = btc_series(40)
    btc[T] = kbar(T, 100, 100.2, 97.0, 99.6, v=300.0)   # 放量、下影刺破, 但收盘没破
    assert replay(btc, [], P())["events"] == []
    btc[T] = kbar(T, 100, 100.2, 97.0, 99.5, v=300.0)   # 收盘 == 两日低: 相等不算跌破
    assert replay(btc, [], P())["events"] == []
    btc[T] = kbar(T, 100, 100.2, 97.0, 99.49, v=300.0)
    assert kinds(replay(btc, [], P())["events"]) == ["armed"]


def test_volume_mean_excludes_trigger_bar():
    # 前 5 根均量 100; 触发K量恰为 200 = 2.0 倍 → 成立(含等号)。若把触发K算进均量(120)则不成立。
    assert kinds(replay(btc_series(40, {T}, break_vol=200.0), [], P())["events"]) == ["armed"]
    assert replay(btc_series(40, {T}, break_vol=199.99), [], P())["events"] == []


def test_low_window_excludes_trigger_bar_and_is_time_bounded():
    # 窗口 = [T-24, T-1]。更低的低点落在窗口内 → 不算破位; 落在窗口外一根 → 算。
    inside = btc_series(40, {T})
    inside[T - 24] = kbar(T - 24, 100, 100.5, 90.0, 100)
    assert replay(inside, [], P())["events"] == []
    outside = btc_series(40, {T})
    outside[T - 25] = kbar(T - 25, 100, 100.5, 90.0, 100)
    ev = one(replay(outside, [], P())["events"], "armed")
    assert ev["trigger"]["ref_low"] == 99.5
    # 触发K自己的 low(97.5) 低于收盘(98): 如果窗口含本K, 永远不可能触发
    assert ev["trigger"]["close"] > outside[T]["low"]


def test_no_trigger_until_window_is_covered():
    assert replay(btc_series(40, {23}), [], P())["events"] == []       # 历史只有 23 根 < 24
    assert kinds(replay(btc_series(30, {24}), [], P())["events"]) == ["armed"]
    holed = [b for b in btc_series(40, {T}) if b["open_time"] != (T - 10) * TF]
    assert replay(holed, [], P())["events"] == []                      # 窗口缺K → 覆盖率不够


def test_default_params_need_two_full_days():
    assert BtcBreakParams().lookback_hours == 48.0 and BtcBreakParams().ttl_hours == 4.0
    assert replay(btc_series(620, {575}), [], None)["events"] == []
    ev = one(replay(btc_series(620, {590}), [], None)["events"], "armed")
    assert ev["expires_at"] == 591 * TF + 4 * 3_600_000


# ============================ 入场 ============================

def basic_pump(extra="uuuu"):
    # d×(T+2): 下标 0..T+1 一路下行(M = T+1), u = R(T+2), S = 停顿K(T+3)
    return pump_series("d" * (T + 2) + "u" + "S" + extra)


def test_entry_bottom_fractal_plus_stall():
    btc, pump = btc_series(60, {T}), basic_pump()
    r = replay(btc, pump, P())
    assert kinds(r["events"]) == ["armed", "entry"]
    ev = r["events"][1]
    e = ev["entry"]
    assert ev["trigger"]["time"] == T * TF and ev["last_trigger_time"] == T * TF
    assert e["symbol"] == PUMP and e["tf"] == "5m"
    assert e["fx_left_time"] == T * TF
    assert e["fx_low_time"] == e["fx_mid_time"] == (T + 1) * TF
    assert e["fx_right_time"] == e["fx_confirm_time"] == (T + 2) * TF
    assert e["stall_time"] == e["signal_time"] == (T + 3) * TF
    assert e["fx_low"] == pump[T + 1]["low"]
    assert e["fx_right_high"] == pump[T + 2]["high"]
    assert e["entry"] == pump[T + 3]["close"] > e["fx_right_high"]
    assert e["fx_low_intact"] is True
    assert len(r["episodes"]) == 1 and r["open_episode"] is None
    ep = r["episodes"][0]
    assert ep["outcome"] == "entry" and ep["entry"] == e and ep["retriggers"] == []
    assert ep["trigger"] == ev["trigger"]


def test_sl_tp_arithmetic():
    btc, pump = btc_series(60, {T}), basic_pump()
    for buf, rr in ((0.3, 2.0), (1.0, 3.0), (0.0, 1.5)):
        e = one(replay(btc, pump, P(sl_buffer_pct=buf, rr=rr))["events"], "entry")["entry"]
        entry, low = pump[T + 3]["close"], pump[T + 1]["low"]
        sl = low * (1 - buf / 100)
        assert e["entry"] == entry
        assert e["sl"] == pytest.approx(sl, rel=1e-9)
        assert e["tp"] == pytest.approx(entry + rr * (entry - sl), rel=1e-9)
        assert e["rr"] == rr
        assert e["risk_pct"] == pytest.approx((entry - sl) / entry * 100, abs=1e-4)
        assert e["sl"] < e["fx_low"] <= e["entry"] < e["tp"] or buf == 0.0


def test_stall_required_vs_not():
    btc, pump = btc_series(60, {T}), basic_pump()
    with_stall = one(replay(btc, pump, P(require_stall=True))["events"], "entry")["entry"]
    no_stall = one(replay(btc, pump, P(require_stall=False))["events"], "entry")["entry"]
    assert with_stall["signal_time"] == (T + 3) * TF and with_stall["stall_time"] == (T + 3) * TF
    # 不要求停顿: 信号K = 形态首次成立的那根(R 的第一根原始K)
    assert no_stall["signal_time"] == (T + 2) * TF and no_stall["stall_time"] is None
    assert no_stall["fx_confirm_time"] == no_stall["fx_right_time"] == (T + 2) * TF
    assert no_stall["entry"] == pump[T + 2]["close"]
    assert no_stall["fx_low"] == with_stall["fx_low"]


def test_fractal_without_stall_is_discarded_and_next_one_used():
    # F1 = (T, T+1, T+2), 之后三根都收不过 R 的高点 → 放弃; F2 = (T+4, T+5, T+6), 停顿 T+7
    pump = pump_series("d" * (T + 2) + "u" + "ddd" + "u" + "S" + "uu")
    r = replay(btc_series(60, {T}), pump, P())
    e = one(r["events"], "entry")["entry"]
    assert e["fx_low_time"] == (T + 5) * TF and e["signal_time"] == (T + 7) * TF
    # 停顿只在确认后的 stall_max_gap 根内有效
    late = pump_series("d" * (T + 2) + "u" + "uS")        # R 之后第 2 根才收过去
    assert one(replay(btc_series(60, {T}), late, P(stall_max_gap=2))["events"],
               "entry")["entry"]["signal_time"] == (T + 4) * TF
    assert "entry" not in kinds(replay(btc_series(60, {T}), late, P(stall_max_gap=1))["events"])


def test_stall_counts_from_end_of_right_merged_bar():
    # R 之后跟一根内包K(并入 R), 停顿从 R 的最后一根原始K之后数
    pump = pump_series("d" * (T + 2) + "u" + "i" + "S" + "u")
    e = one(replay(btc_series(60, {T}), pump, P(stall_max_gap=1))["events"], "entry")["entry"]
    assert e["fx_right_time"] == (T + 2) * TF
    assert e["fx_confirm_time"] == (T + 3) * TF           # 内包K是 R 的最后一根
    assert e["signal_time"] == (T + 4) * TF


def test_fractal_low_before_trigger_bar_is_rejected():
    # F1 的低点在 T-1(破位K之前), 它的停顿K在 T+1(已武装)也不算; F2 的低点在 T+4 → 入场
    pump = pump_series("d" * T + "u" + "S" + "ddd" + "u" + "S" + "uu")
    r = replay(btc_series(60, {T}), pump, P())
    e = one(r["events"], "entry")["entry"]
    assert e["fx_low_time"] == (T + 4) * TF and e["signal_time"] == (T + 6) * TF
    assert e["fx_low_time"] >= r["events"][0]["trigger"]["time"]
    # 不要求停顿时同样拒绝
    e2 = one(replay(btc_series(60, {T}), pump, P(require_stall=False))["events"], "entry")["entry"]
    assert e2["fx_low_time"] == (T + 4) * TF and e2["signal_time"] == (T + 5) * TF


def test_fractal_low_on_the_trigger_bar_is_accepted():
    pump = pump_series("d" * (T + 1) + "u" + "S" + "uu")
    e = one(replay(btc_series(60, {T}), pump, P())["events"], "entry")["entry"]
    assert e["fx_low_time"] == T * TF and e["signal_time"] == (T + 2) * TF


def test_fx_low_intact_flag_and_filter():
    # R 之后一根大K吞没 R(并入 R)且刺破分型低点, 再出停顿K
    pump = pump_series("d" * (T + 2) + "u")
    r_bar, m_bar = pump[T + 2], pump[T + 1]
    pump.append(kbar(T + 3, r_bar["close"], r_bar["high"] + 0.05, m_bar["low"] - 0.5, r_bar["close"]))
    pump.append(kbar(T + 4, r_bar["close"], r_bar["high"] + 0.6, r_bar["low"] + 0.1, r_bar["high"] + 0.5))
    pump.append(kbar(T + 5, r_bar["high"] + 0.5, r_bar["high"] + 0.7, r_bar["low"] + 0.2, r_bar["high"] + 0.6))
    btc = btc_series(60, {T})
    e = one(replay(btc, pump, P())["events"], "entry")["entry"]
    assert e["signal_time"] == (T + 4) * TF and e["fx_low_intact"] is False
    assert e["fx_low"] == m_bar["low"]
    assert "entry" not in kinds(replay(btc, pump, P(require_low_intact=True))["events"])


# ============================ 有效期 / 轮次 ============================

def test_ttl_boundary_and_expiry():
    x = (T + 13) * TF                                       # 触发K收盘 + 1h
    # 信号K T+12 的收盘时刻恰为 expires_at → 有效
    ok = pump_series("d" * (T + 11) + "u" + "S" + "uuu")
    r = replay(btc_series(60, {T}), ok, P())
    assert kinds(r["events"]) == ["armed", "entry"]
    assert r["events"][1]["entry"]["signal_time"] == (T + 12) * TF
    # 晚一根 → 到期
    late = pump_series("d" * (T + 12) + "u" + "S" + "uuu")
    r = replay(btc_series(60, {T}), late, P())
    assert kinds(r["events"]) == ["armed", "expired"]
    ex = r["events"][1]
    assert ex["expired_at"] == x == r["events"][0]["expires_at"]
    assert ex["trigger"]["time"] == T * TF
    assert r["episodes"][0]["outcome"] == "expired" and r["episodes"][0]["entry"] is None
    assert r["episodes"][0]["expired_at"] == x


def test_expired_is_emitted_as_soon_as_both_symbols_passed_the_deadline():
    btc, pump = btc_series(60, {T}), pump_series("d" * 60)
    rule = BtcBreakRule(P())
    seen = {}
    for i in range(60):
        for sym, src in ((BTC, btc), (PUMP, pump)):
            for ev in rule.on_bar(sym, src[i]):
                seen[ev["type"]] = (i, sym)
    assert seen["armed"] == (T, BTC)
    assert seen["expired"] == (T + 12, PUMP)                # 收盘时刻 = expires_at 的那根到齐即发
    assert rule.state()["phase"] == "idle"
    # 只喂 BTC(目标品种缺K): 下一根更晚的K到来时补发
    rule = BtcBreakRule(P())
    at = [i for i in range(60) for ev in rule.on_bar(BTC, btc[i]) if ev["type"] == "expired"]
    assert at == [T + 13]


@pytest.mark.parametrize("missing", ["target", "trigger"])
@pytest.mark.parametrize("order", ["bp", "pb"])
def test_signal_after_deadline_is_not_an_entry_even_with_a_missing_bar(missing, order):
    # 有效期最后一根是 T+12。某个品种恰好缺这一根, 而 PUMP 的停顿K出在 T+13(已过期) → 只能是到期
    pump = pump_series("d" * (T + 11) + "u" + "S")           # 低点 T+10, R = T+11, 强阳在下标 T+12
    pump[-1] = dict(pump[-1], open_time=(T + 13) * TF)       # 把强阳挪到 T+13
    btc = btc_series(60, {T})
    if missing == "target":
        pass                                                 # PUMP 本来就没有 T+12
    else:
        pump.insert(-1, kbar(T + 12, *[pump[T + 11][k] for k in ("close", "high", "low", "close")]))
        pump[-2] = dict(pump[-2], high=pump[T + 11]["high"] - 0.01, low=pump[T + 11]["low"] + 0.01)
        btc = [b for b in btc if b["open_time"] != (T + 12) * TF]
    rule = BtcBreakRule(P())
    events = feed(rule, btc, pump, order)
    assert kinds(events) == ["armed", "expired"]
    assert events[1]["expired_at"] == (T + 13) * TF
    assert rule.state()["phase"] == "idle"
    # 对照: 同一根强阳若落在 T+12(有效期内)就是入场
    ok = pump_series("d" * (T + 11) + "u" + "S")
    assert kinds(feed(BtcBreakRule(P()), btc_series(60, {T}), ok, order)) == ["armed", "entry"]


def test_retrigger_extends_window():
    t2 = T + 8
    pump = pump_series("d" * (T + 15) + "u" + "S" + "uuu")  # 停顿K在 T+16, 晚于第一次的有效期(T+12)
    alone = replay(btc_series(70, {T}), pump, P())
    assert kinds(alone["events"]) == ["armed", "expired"]
    r = replay(btc_series(70, {T, t2}), pump, P())
    assert kinds(r["events"]) == ["armed", "retrigger", "entry"]
    armed, re_, entry = r["events"]
    assert re_["trigger"]["time"] == t2 * TF and re_["episode_id"] == T * TF
    assert re_["expires_at"] == (t2 + 13) * TF > armed["expires_at"]
    assert entry["trigger"]["time"] == T * TF               # 入场事件带的是本轮首次触发
    assert entry["last_trigger_time"] == t2 * TF
    assert entry["entry"]["signal_time"] == (T + 16) * TF
    ep = r["episodes"][0]
    assert ep["retriggers"] == [t2 * TF] and ep["expires_at"] == (t2 + 13) * TF


def test_retrigger_does_not_move_the_fractal_anchor():
    # 分型低点在 T+1(首次触发之后、再次触发 T+3 之前), 停顿在 T+4 → 仍然有效
    pump = pump_series("d" * (T + 2) + "u" + "u" + "S" + "uu")
    r = replay(btc_series(60, {T, T + 3}), pump, P())
    assert kinds(r["events"]) == ["armed", "retrigger", "entry"]
    assert r["events"][2]["entry"]["fx_low_time"] == (T + 1) * TF


def test_one_entry_per_episode_then_needs_fresh_trigger():
    t3 = T + 10
    spec = "d" * (T + 2) + "u" + "S" + "ddd" + "u" + "S" + "ddd" + "u" + "S" + "uu"
    pump = pump_series(spec)
    # 只有第一次破位: 第二、第三个「底分型+停顿」都不再入场
    r1 = replay(btc_series(80, {T}), pump, P())
    assert kinds(r1["events"]) == ["armed", "entry"]
    assert r1["events"][1]["entry"]["signal_time"] == (T + 3) * TF
    # T+10 再次破位 = 新的一轮, 低点在 T+11 的分型入场
    r2 = replay(btc_series(80, {T, t3}), pump, P())
    assert kinds(r2["events"]) == ["armed", "entry", "armed", "entry"]
    second = r2["events"][3]
    assert second["episode_id"] == t3 * TF and second["trigger"]["time"] == t3 * TF
    assert second["entry"]["fx_low_time"] == (T + 11) * TF
    assert second["entry"]["signal_time"] == (T + 13) * TF
    assert [e["outcome"] for e in r2["episodes"]] == ["entry", "entry"]


def test_open_episode_at_end_of_data():
    r = replay(btc_series(T + 3, {T}), pump_series("d" * (T + 3)), P())
    assert kinds(r["events"]) == ["armed"] and r["episodes"] == []
    assert r["open_episode"]["episode_id"] == T * TF and r["open_episode"]["outcome"] is None


# ============================ 到达顺序 / 幂等 ============================

def canon(events):
    return sort_events(events, TF)


def test_same_bar_retrigger_and_entry_is_order_independent():
    # BTC 在 T+3 再次破位, PUMP 的停顿K也在 T+3
    btc = btc_series(60, {T, T + 3, T + 6})
    pump = basic_pump("u" * 10)
    out = {}
    for order in ("bp", "pb"):
        rule = BtcBreakRule(P())
        out[order] = (feed(rule, btc, pump, order), rule.state())
    assert kinds(out["bp"][0]) == ["armed", "retrigger", "entry", "armed", "expired"]
    assert kinds(out["pb"][0]) == ["armed", "entry", "retrigger", "armed", "expired"]
    assert canon(out["bp"][0]) == canon(out["pb"][0]) == out["bp"][0]
    assert out["bp"][1] == out["pb"][1]
    entry = one(out["pb"][0], "entry")
    assert entry["last_trigger_time"] == T * TF             # 只看信号K之前的触发
    eps, open_ep = episodes_from_events(out["pb"][0])
    assert open_ep is None and [e["outcome"] for e in eps] == ["entry", "expired"]
    assert eps[0]["retriggers"] == [(T + 3) * TF]           # 同K的破位记在这一轮
    assert eps[1]["episode_id"] == (T + 6) * TF             # 之后的破位才是新一轮
    assert replay(btc, pump, P())["events"] == out["bp"][0]


def random_market(seed, n=1400, gaps=False):
    rng = random.Random(seed)
    btc, pump = [], []
    pb, pp = 100.0, 10.0
    for i in range(n):
        crash = rng.random() < 0.03
        o = pb
        c = max(5.0, o + (-abs(rng.gauss(0.8, 0.3)) if crash else rng.gauss(0, 0.15)))
        h = max(o, c) + abs(rng.gauss(0, 0.05))
        l = min(o, c) - abs(rng.gauss(0, 0.05))
        v = rng.uniform(80, 120) * (rng.uniform(2.2, 5) if crash else 1)
        btc.append(kbar(i, o, h, l, c, v))
        pb = c
        o = pp
        c = max(1.0, o + rng.gauss(0, 0.05))
        h = max(o, c) + abs(rng.gauss(0, 0.02))
        l = min(o, c) - abs(rng.gauss(0, 0.02))
        pump.append(kbar(i, o, h, l, c, rng.uniform(50, 150)))
        pp = c
    if gaps:
        btc = [b for k, b in enumerate(btc) if k % 211 != 17]
        pump = [b for b in pump if rng.random() > 0.04]
    return btc, pump


MARKETS = [
    ("stall", P(), 11, False),
    ("short_ttl", P(ttl_hours=0.25), 12, False),
    ("no_stall", P(require_stall=False, ttl_hours=0.5), 13, False),
    ("gaps", P(ttl_hours=0.5), 14, True),
    ("odd_ttl", P(ttl_hours=0.3, lookback_hours=1.0), 15, True),
]


@pytest.mark.parametrize("name,params,seed,gaps", MARKETS, ids=[m[0] for m in MARKETS])
def test_random_market_is_not_vacuous(name, params, seed, gaps):
    r = replay(*random_market(seed, gaps=gaps), params)
    k = kinds(r["events"])
    assert k.count("armed") >= 8 and k.count("entry") >= (1 if name == "odd_ttl" else 3), k
    if name in ("short_ttl", "gaps", "odd_ttl"):
        assert k.count("expired") >= 2
    assert k.count("armed") == len(r["episodes"]) + (1 if r["open_episode"] else 0)
    assert k.count("entry") + k.count("expired") == len(r["episodes"])
    for e in r["events"]:
        if e["type"] == "entry":
            assert e["entry"]["fx_low_time"] >= e["trigger"]["time"]
            assert e["entry"]["signal_time"] + TF <= max(
                x["expires_at"] for x in r["events"]
                if x["type"] in ("armed", "retrigger") and x["episode_id"] == e["episode_id"])


@pytest.mark.parametrize("name,params,seed,gaps", MARKETS, ids=[m[0] for m in MARKETS])
def test_arrival_order_independent(name, params, seed, gaps):
    btc, pump = random_market(seed, gaps=gaps)
    rng = random.Random(seed)
    results = []
    for order in ("bp", "pb", lambda t: rng.choice(("bp", "pb"))):
        rule = BtcBreakRule(params)
        ev = feed(rule, btc, pump, order)
        results.append((canon(ev), rule.state()))
    assert results[0] == results[1] == results[2]
    # replay(BTC 先) 与逐根喂入一致(末尾仍在武装的不强行到期)
    r = replay(btc, pump, params)["events"]
    assert [e for e in r if e in results[0][0]] == results[0][0]


@pytest.mark.parametrize("name,params,seed,gaps", MARKETS, ids=[m[0] for m in MARKETS])
def test_truncation_causality_synthetic(name, params, seed, gaps):
    btc, pump = random_market(seed, gaps=gaps)
    full = replay(btc, pump, params)["events"]
    rng = random.Random(seed + 1000)
    cuts = set(rng.sample(range(30, len(btc)), 25))
    for e in full[:24]:                                      # 事件所在K及其前两根都切一刀
        i = event_instant(e, TF) // TF
        cuts.update({i - 2, i - 1, i})
    for cut in sorted(c for c in cuts if c >= 0):
        t = cut * TF
        part = replay([b for b in btc if b["open_time"] <= t],
                      [b for b in pump if b["open_time"] <= t], params)["events"]
        expect = [e for e in full if event_instant(e, TF) <= t + TF]
        assert part == expect, cut


@pytest.mark.parametrize("name,params,seed,gaps", MARKETS, ids=[m[0] for m in MARKETS])
def test_seed_then_live_equals_all_live(name, params, seed, gaps):
    btc, pump = random_market(seed, gaps=gaps)
    full = replay(btc, pump, params)["events"]
    splits = {200, 901}
    for e in full[:5]:                                       # 也在武装期中间切
        splits.add(event_instant(e, TF) // TF + 1)
    for k in sorted(splits):
        t = k * TF
        live = BtcBreakRule(params)
        feed(live, [b for b in btc if b["open_time"] < t], [b for b in pump if b["open_time"] < t])
        seeded = BtcBreakRule(params)
        assert seeded.seed([b for b in btc if b["open_time"] < t],
                           [b for b in pump if b["open_time"] < t]) is None
        assert seeded.state() == live.state()
        rest_b = [b for b in btc if b["open_time"] >= t]
        rest_p = [b for b in pump if b["open_time"] >= t]
        assert feed(seeded, rest_b, rest_p) == feed(live, rest_b, rest_p)
        assert seeded.state() == live.state()


def test_duplicate_out_of_order_and_malformed_bars_are_ignored():
    btc, pump = btc_series(60, {T}), basic_pump("u" * 20)
    rule = BtcBreakRule(P())
    events = []
    for i in range(T + 2):
        events += rule.on_bar(BTC, btc[i])
        events += rule.on_bar(PUMP, pump[i])
    assert kinds(events) == ["armed"]
    snap = json.dumps(rule.state(), sort_keys=True)
    crash = kbar(T, 100, 100, 1.0, 1.0, v=9e9)
    junk = [
        (BTC, btc[T + 1]), (PUMP, pump[T + 1]),              # 重复
        (BTC, crash), (PUMP, crash),                         # 同一 open_time 的不同内容: 以先到的为准
        (BTC, kbar(3, 100, 100, 1.0, 1.0, v=9e9)), (PUMP, kbar(3, 9, 9, 0.1, 0.1)),   # 乱序(更早)
        (BTC, None), (BTC, {}), (PUMP, {"open_time": (T + 2) * TF}),
        (BTC, ((T + 2) * TF, 1, 1, float("nan"), 1, 1)),
        (PUMP, ((T + 2) * TF, "x", 1, 1, 1, 1)),
        (PUMP, dict(pump[T + 2], closed=0)),                 # 未收盘
        (BTC, ((T + 2) * TF, 98, 98, 90, 90, 9e9, 0, 0, 0)),  # 引擎 tuple, closed=0
        (BTC, kbar(T + 2, 98, 97, 99, 98)),                  # high < low
        (PUMP, dict(pump[T + 2], open_time=(T + 2) * TF + 1000)),   # 不在 5m 网格上
        ("ETHUSDT", btc[T + 2]),
    ]
    for sym, bar in junk:
        assert rule.on_bar(sym, bar) == []
        assert json.dumps(rule.state(), sort_keys=True) == snap
    for i in range(T + 2, 60):
        events += rule.on_bar(BTC, btc[i])
        if i < len(pump):
            events += rule.on_bar(PUMP, pump[i])
    assert events == replay(btc, pump, P())["events"]
    assert kinds(events) == ["armed", "entry"]


def test_engine_tuple_rows_equal_dict_rows():
    btc, pump = random_market(11)
    tup = lambda b: (b["open_time"], b["open"], b["high"], b["low"], b["close"],  # noqa: E731
                     b["volume"], b["volume"] * b["close"], b["volume"] / 2, 1)
    a = replay(btc, pump, P())
    b = replay([tup(x) for x in btc], [tup(x) for x in pump], P())
    cached = [dict(x, quote_volume=1.0, taker_buy=1.0, closed=1) for x in btc]
    c = replay(cached, [list(tup(x)) for x in pump], P())
    assert a == b == c


def test_state_shape_and_json():
    rule = BtcBreakRule(P())
    s = rule.state()
    assert s["phase"] == "idle" and s["trigger"] is None and s["ref_low"] is None and not s["warm"]
    btc, pump = btc_series(60, {T}), basic_pump()
    for i in range(T):
        rule.on_bar(BTC, btc[i])
        rule.on_bar(PUMP, pump[i])
    s = rule.state()
    assert s["phase"] == "idle" and s["warm"] and s["window_bars"] == 24
    assert s["ref_low"] == 99.5 and s["vol_ma"] == 100.0 and s["vol_threshold"] == 200.0
    rule.on_bar(BTC, btc[T])
    s = rule.state()
    assert s["phase"] == "armed" and s["armed_at"] == (T + 1) * TF
    assert s["expires_at"] == (T + 13) * TF and s["trigger"]["time"] == T * TF
    assert s["ref_low"] == btc[T]["low"] and s["ref_low_time"] == T * TF
    for i in range(T, T + 4):
        rule.on_bar(BTC, btc[i])
        rule.on_bar(PUMP, pump[i])
    s = rule.state()
    assert s["phase"] == "idle" and s["trigger"] is None
    assert s["last_episode"]["outcome"] == "entry"
    assert s["last_episode"]["entry"]["signal_time"] == (T + 3) * TF
    assert s["last_bar"] == {"trigger": (T + 3) * TF, "target": (T + 3) * TF}
    json.dumps(s)
    json.dumps(replay(btc, pump, P()))


def test_events_are_copies_not_internal_state():
    rule = BtcBreakRule(P())
    btc = btc_series(60, {T})
    ev = [e for i in range(T + 1) for e in rule.on_bar(BTC, btc[i])]
    ev[0]["trigger"]["time"] = -1
    assert rule.state()["trigger"]["time"] == T * TF


def test_invalid_params_rejected():
    for bad in (dict(tf="5x"), dict(lookback_hours=0), dict(ttl_hours=0), dict(ttl_hours=0.05),
                dict(vol_ma=0),
                dict(vol_mult=0), dict(stall_max_gap=0), dict(rr=0), dict(sl_buffer_pct=-1)):
        with pytest.raises(ValueError):
            BtcBreakRule(BtcBreakParams(**bad))
    assert tf_to_ms("5m") == TF and tf_to_ms("1h") == 3_600_000
    # ttl 向下取整到整根K: 0.3h = 18 分钟 → 15 分钟
    ev = one(replay(btc_series(26, {24}), [], P(ttl_hours=0.3))["events"], "armed")
    assert ev["expires_at"] == 25 * TF + 3 * TF


# ============================ 与 chan 既有函数的一致性 ============================

def assert_entries_match_chan_helpers(pump, events, params, lookahead=40):
    """每个入场都能在「同一段K + 后面再多看 lookahead 根」上用 chan.find_fractals / chan_bi.stall_after
    找到同一个分型和同一根停顿K → 流式判定 = 既有全历史函数, 且不受后面K的影响。"""
    by_t = {b["open_time"]: i for i, b in enumerate(pump)}
    n = 0
    for ev in events:
        if ev["type"] != "entry":
            continue
        e = ev["entry"]
        anchor = ev["trigger"]["time"] - PRE_BARS * TF
        lo = next(i for i, b in enumerate(pump) if b["open_time"] >= anchor)
        hi = by_t[e["signal_time"]] + lookahead
        win = pump[lo:hi + 1]
        merged = merge_klines(win)
        hits = [fx for fx in find_fractals(win, merged)
                if fx.kind == "bottom" and win[fx.extreme_src_idx]["open_time"] == e["fx_low_time"]]
        assert len(hits) == 1
        fx = hits[0]
        assert fx.extreme_price == e["fx_low"]
        assert merged[fx.mid_merged_idx + 1].open_time == e["fx_right_time"]
        assert merged[fx.mid_merged_idx - 1].open_time == e["fx_left_time"]
        if params.require_stall:
            st = stall_after(win, merged, fx, params.stall_max_gap)
            assert st is not None and win[st]["open_time"] == e["stall_time"] == e["signal_time"]
            assert win[fx.confirm_src_idx]["open_time"] == e["fx_confirm_time"]
            assert merged[fx.mid_merged_idx + 1].high == e["fx_right_high"]
            assert win[st]["close"] == e["entry"] > e["fx_right_high"]
        else:
            assert e["signal_time"] == e["fx_right_time"]
        n += 1
    return n


@pytest.mark.parametrize("name,params,seed,gaps", MARKETS[:3], ids=[m[0] for m in MARKETS[:3]])
def test_entries_match_chan_helpers_synthetic(name, params, seed, gaps):
    btc, pump = random_market(seed, gaps=gaps)
    assert assert_entries_match_chan_helpers(pump, replay(btc, pump, params)["events"], params) >= 3


# ============================ 与暴力参照实现对照 ============================

def brute_trigger_times(btc, params):
    """逐根暴力重算触发条件(整段切片取 min / 均值), 与规则内部的单调队列实现互相独立。"""
    tf = tf_to_ms(params.tf)
    lookback = int(params.lookback_hours * 3_600_000)
    need = math.ceil(MIN_WINDOW_COVERAGE * (lookback // tf))
    span = lookback // tf + 2
    out = []
    for i, b in enumerate(btc):
        t = b["open_time"]
        lo = t - lookback
        win = [x for x in btc[max(0, i - span):i] if lo <= x["open_time"] < t]
        if btc[0]["open_time"] > lo or len(win) < need or i < params.vol_ma:
            continue
        vma = sum(x["volume"] for x in btc[i - params.vol_ma:i]) / params.vol_ma
        if vma > 0 and b["close"] < min(x["low"] for x in win) and b["volume"] >= params.vol_mult * vma:
            out.append(t)
    return out


def first_signal_hindsight(pump, trigger_time, params, lookahead=40, until=None):
    """用 chan 既有函数在整段(含后面的K)上枚举: 低点不早于破位K的底分型里, 最早的信号K时间。"""
    anchor = trigger_time - PRE_BARS * TF
    win = [b for b in pump if anchor <= b["open_time"] <= until + lookahead * TF]
    merged = merge_klines(win)
    cands = []
    for fx in find_fractals(win, merged):
        if fx.kind != "bottom" or win[fx.extreme_src_idx]["open_time"] < trigger_time:
            continue
        if params.require_stall:
            st = stall_after(win, merged, fx, params.stall_max_gap)
            if st is not None:
                cands.append(win[st]["open_time"])
        else:
            cands.append(merged[fx.mid_merged_idx + 1].open_time)
    return min(cands) if cands else None


def assert_matches_brute_force(btc, pump, params):
    r = replay(btc, pump, params)
    fired = sorted(e["trigger"]["time"] for e in r["events"] if e["type"] in ("armed", "retrigger"))
    assert fired == brute_trigger_times(btc, params)         # 不多报、不漏报
    for ep in r["episodes"]:
        t0 = ep["trigger"]["time"]
        if ep["outcome"] == "entry":
            sig = ep["entry"]["signal_time"]
            # 入场的就是「第一个」: 整段回看也找不到更早的信号
            assert first_signal_hindsight(pump, t0, params, until=sig) == sig
            assert sig + TF <= ep["expires_at"]
        else:
            first = first_signal_hindsight(pump, t0, params, until=ep["expired_at"])
            assert first is None or first + TF > ep["expired_at"]    # 到期的那一轮确实没有信号
    return len(fired), len(r["episodes"])


@pytest.mark.parametrize("name,params,seed,gaps", MARKETS, ids=[m[0] for m in MARKETS])
def test_matches_brute_force_synthetic(name, params, seed, gaps):
    n_trig, n_eps = assert_matches_brute_force(*random_market(seed, gaps=gaps), params)
    assert n_trig >= 8 and n_eps >= 8


# ============================ 真实数据(有缓存才跑) ============================

CACHE = ROOT / ".btcache"
REAL_BARS = 9000                                             # 约 31 天


@functools.lru_cache(maxsize=1)
def _read_real():
    fb, fp = CACHE / "BTCUSDT_5m_btcbreak.json", CACHE / "PUMPUSDT_5m_btcbreak.json"
    if not (fb.exists() and fp.exists()):
        return None
    with open(fb, encoding="utf-8") as f:
        btc = json.load(f)
    with open(fp, encoding="utf-8") as f:
        pump = json.load(f)
    # 固定取缓存最前面的一段(PUMP 上市后约 31 天, BTC 另带 48h 预热), 不随缓存增长而变
    end = pump[0]["open_time"] + REAL_BARS * TF
    return ([b for b in btc if b["open_time"] < end], [b for b in pump if b["open_time"] < end])


def _load_real():
    data = _read_real()
    if data is None:
        pytest.skip("没有 .btcache/*_5m_btcbreak.json (先跑 scripts/btcbreak_run.py)")
    return data


@pytest.mark.parametrize("stall", [True, False])
def test_truncation_causality_real_series(stall):
    btc, pump = _load_real()
    params = BtcBreakParams(require_stall=stall)
    full = replay(btc, pump, params)["events"]
    assert kinds(full).count("armed") >= 5 and kinds(full).count("entry") >= 5
    rng = random.Random(7)
    t0, t1 = pump[0]["open_time"] // TF, pump[-1]["open_time"] // TF
    cuts = set(rng.sample(range(t0, t1), 10))
    for e in full[:14]:
        i = event_instant(e, TF) // TF
        cuts.update({i - 1, i})
    for cut in sorted(cuts):
        t = cut * TF
        part = replay([b for b in btc if b["open_time"] <= t],
                      [b for b in pump if b["open_time"] <= t], params)["events"]
        assert part == [e for e in full if event_instant(e, TF) <= t + TF], cut
    assert assert_entries_match_chan_helpers(pump, full, params) >= 5


def test_matches_brute_force_real_series():
    btc, pump = _load_real()
    btc, pump = btc[:5600], [b for b in pump if b["open_time"] <= btc[5599]["open_time"]]
    n_trig, n_eps = assert_matches_brute_force(btc, pump, BtcBreakParams())
    assert n_trig >= 5 and n_eps >= 3


def test_arrival_order_and_seed_real_series():
    btc, pump = _load_real()
    params = BtcBreakParams()
    runs = []
    for order in ("bp", "pb"):
        rule = BtcBreakRule(params)
        runs.append((canon(feed(rule, btc, pump, order)), rule.state()))
    assert runs[0] == runs[1]
    assert len(runs[0][0]) >= 1
    split = btc[len(btc) // 2]["open_time"]
    seeded = BtcBreakRule(params)
    seeded.seed([b for b in btc if b["open_time"] < split], [b for b in pump if b["open_time"] < split])
    live = BtcBreakRule(params)
    feed(live, [b for b in btc if b["open_time"] < split], [b for b in pump if b["open_time"] < split])
    rest = ([b for b in btc if b["open_time"] >= split], [b for b in pump if b["open_time"] >= split])
    assert feed(seeded, *rest) == feed(live, *rest)
    assert seeded.state() == live.state() == runs[0][1]


# ============================ 脚本(不联网) ============================

def _load_script():
    spec = importlib.util.spec_from_file_location("btcbreak_run", ROOT / "scripts" / "btcbreak_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeExchange:
    """假行情源: 只实现 klines / time, 记录请求次数。"""

    def __init__(self, bars):
        self.bars = {b["open_time"]: b for b in bars}
        self.calls = []

    def __call__(self, path, params):
        self.calls.append((path, dict(params)))
        if path.endswith("/time"):
            return {"serverTime": max(self.bars) + TF}, 0
        start, limit = int(params["startTime"]), int(params["limit"])
        rows = [b for t, b in sorted(self.bars.items()) if t >= start][:limit]
        return [[b["open_time"], str(b["open"]), str(b["high"]), str(b["low"]), str(b["close"]),
                 str(b["volume"]), b["open_time"] + TF - 1, "0", 1, "0", "0", "0"] for b in rows], 10


def test_script_download_cache_and_counts(tmp_path):
    run = _load_script()
    btc = btc_series(3200, {3000})
    # 目标品种「上市」于 2990: 低点 3001(破位K 3000 之后), R = 3002, 停顿 3003
    pump = [dict(b, open_time=(2990 + i) * TF)
            for i, b in enumerate(pump_series("d" * 12 + "u" + "S" + "u" * 20))]
    ex_p, ex_b = FakeExchange(pump), FakeExchange(btc)
    assert run.first_bar_time(PUMP, "5m", ex_p) == 2990 * TF
    now = 3100 * TF + 1000                                    # 3099 已收盘, 3100 还在走
    path = str(tmp_path / "BTCUSDT_5m_btcbreak.json")
    naps = []
    rows = run.ensure_history(BTC, "5m", 100 * TF, now, path, fetch=ex_b, sleep=naps.append)
    assert [r["open_time"] for r in rows] == [i * TF for i in range(100, 3100)]
    assert set(rows[0]) == set(run.ROW_KEYS)
    assert len(ex_b.calls) == 2 and all(n >= 0.25 for n in naps)
    # 再跑: 只补尾部; 起点提前: 只补头部
    ex_b.calls.clear()
    rows = run.ensure_history(BTC, "5m", 100 * TF, 3150 * TF, path, fetch=ex_b, sleep=naps.append)
    assert len(rows) == 3050 and len(ex_b.calls) == 1 and ex_b.calls[0][1]["startTime"] == 3100 * TF
    ex_b.calls.clear()
    rows = run.ensure_history(BTC, "5m", 0, 3150 * TF, path, fetch=ex_b, sleep=naps.append)
    assert len(rows) == 3150 and ex_b.calls[0][1]["startTime"] == 0
    assert run.load_cache(path) == rows
    offline = run.ensure_history(BTC, "5m", 2000 * TF, 3150 * TF, path, fetch=None, offline=True)
    assert [r["open_time"] for r in offline] == [i * TF for i in range(2000, 3150)]

    params = P()
    result = replay(rows, pump, params)
    out = run.build_output(result, params, rows, pump, path, "p.json", now)
    c = out["meta"]["counts"]
    assert c == {"trigger_bars": 1, "armed": 1, "retrigger": 0, "episodes": 1, "entries": 1,
                 "expired": 0, "open": 0, "entries_fx_low_broken_before_signal": 0}
    assert out["meta"]["data"]["target"]["first_open_time"] == 2990 * TF
    assert out["meta"]["range"]["start"] == 2990 * TF and out["meta"]["tf_ms"] == TF
    assert out["events"] == result["events"] and out["episodes"] == result["episodes"]
    # 铁律七: 输出里不能出现任何结算/盈亏口径的字段
    text = json.dumps(out, ensure_ascii=False).lower()
    for word in ("win_rate", "winrate", "expectancy", "pnl", "avg_r", "outcome_r", "胜率", "期望值"):
        assert word not in text
    a = run.parse_args(["--out", "x.json", "--no-require-stall", "--rr", "3", "--lookback-hours", "24"])
    p2 = run.params_from_args(a)
    assert p2.require_stall is False and p2.rr == 3.0 and p2.lookback_hours == 24.0
    assert p2.target_symbol == PUMP and p2.vol_ma == 20
