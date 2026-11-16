"""合约 vs 现货 CVD 对比（app/flow/cvd.py）的单测。全部离线：网络用假会话顶替。"""
import asyncio
import json
import random

import pytest

from app.engine.orderflow import divergence as orderflow_divergence
from app.flow import cvd

M = 60_000
H = 3_600_000
T0 = 1_794_787_200_000            # 2026-11-16 00:00 UTC，对 5m/15m/1h/4h 都是整棒边界
BANNED = ("建议", "应该", "做多", "做空", "买入", "卖出")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    cvd._reset_cache()
    monkeypatch.setattr(cvd, "OKX_MIN_GAP_S", 0.0)      # 测试里不真的等限频间隔
    yield
    cvd._reset_cache()


# ───────────────────────── 造数 ─────────────────────────
def B(t, o, c, vol, tb):
    """解析后的棒：(open_time, open, close, volume, taker_buy)。"""
    return (t, float(o), float(c), float(vol), float(tb))


def raw_k(t, o, c, vol, tb, step=H):
    """币安原始 kline 行（合约/现货同构，数值是字符串）。"""
    return [t, str(o), str(max(o, c)), str(min(o, c)), str(c), str(vol), t + step - 1,
            str(vol * c), 100, str(tb), str(tb * c), "0"]


def trend(n, start, step_px, buy_ratio, vol=100.0, tf="1h", t0=T0):
    """n 根单边棒：每根涨/跌 step_px，主动买占比 buy_ratio。"""
    ms = cvd.TF_MS[tf]
    return [B(t0 + i * ms, start + i * step_px, start + (i + 1) * step_px, vol, vol * buy_ratio)
            for i in range(n)]


# ───────────────────────── delta / 解析 ─────────────────────────
def test_bar_delta_主动买减主动卖():
    assert cvd.bar_delta(10.0, 7.0) == 4.0        # 买 7 卖 3
    assert cvd.bar_delta(10.0, 3.0) == -4.0       # 买 3 卖 7
    assert cvd.bar_delta(10.0, 5.0) == 0.0


def test_last_closed_open_含结算宽限():
    # 15m：宽限 3 秒。00:15:02 时 00:00 那根还不算数，00:15:03 起算数
    assert cvd.last_closed_open(T0 + 15 * M + 2_000, "15m") == T0 - 15 * M
    assert cvd.last_closed_open(T0 + 15 * M + 3_000, "15m") == T0
    # 1h：要等 OKX 补齐，宽限 SETTLE_MS["1h"]
    settle = cvd.SETTLE_MS["1h"]
    assert cvd.last_closed_open(T0 + H + settle - 1, "1h") == T0 - H
    assert cvd.last_closed_open(T0 + H + settle, "1h") == T0


def test_parse_丢未收盘棒和坏行():
    now = T0 + 3 * H + cvd.SETTLE_MS["1h"] + 1_000       # T0+3h 那根正在走
    raw = [
        raw_k(T0, 100, 101, 10, 7),
        raw_k(T0 + H, 101, 102, 10, 6),
        ["坏行"],                                         # 列数不够
        raw_k(T0 + H + 123, 1, 1, 1, 1),                  # 开盘时间不在整棒边界
        raw_k(T0 + 2 * H, 102, 103, 10, 4),
        raw_k(T0 + 3 * H, 103, 104, 2, 1),                # 未收盘
    ]
    bars = cvd.parse_binance_klines(raw, "1h", now)
    assert [b[0] for b in bars] == [T0, T0 + H, T0 + 2 * H]
    assert bars[0] == (T0, 100.0, 101.0, 10.0, 7.0)
    assert [cvd.bar_delta(b[3], b[4]) for b in bars] == [4.0, 2.0, -2.0]


def test_parse_刚收盘未过宽限的棒不算():
    raw = [raw_k(T0, 100, 101, 10, 7), raw_k(T0 + H, 101, 102, 10, 6)]
    just = T0 + 2 * H + 60_000                            # T0+1h 那根收盘才 1 分钟
    assert [b[0] for b in cvd.parse_binance_klines(raw, "1h", just)] == [T0]
    later = T0 + 2 * H + cvd.SETTLE_MS["1h"]
    assert [b[0] for b in cvd.parse_binance_klines(raw, "1h", later)] == [T0, T0 + H]


def test_parse_空输入():
    assert cvd.parse_binance_klines([], "1h", T0) == []
    assert cvd.parse_binance_klines(None, "1h", T0) == []


# ───────────────────────── 对齐 / 累计 ─────────────────────────
def test_align_一侧缺棒整根丢掉():
    fut = [B(T0 + i * H, 100, 101, 10, 6) for i in range(6)]
    spot = [B(T0 + i * H, 100, 101, 2, 1.5) for i in (0, 1, 3, 4, 5, 6)]   # 缺第 2 根，多第 6 根
    f, s = cvd.align(fut, spot)
    want = [T0 + i * H for i in (0, 1, 3, 4, 5)]
    assert [b[0] for b in f] == want and [b[0] for b in s] == want
    assert all(b[3] == 10 for b in f) and all(b[3] == 2 for b in s)        # 各自的数据没串


def test_align_缺棒后累计只算两边都有的棒():
    fut = [B(T0 + i * H, 100, 101, 10, 7) for i in range(5)]               # 每根 +4
    spot = [B(T0 + i * H, 100, 101, 10, 6) for i in (0, 1, 3, 4)]          # 每根 +2，缺第 2 根
    p = cvd.build_payload("1h", fut, spot, now_ms=T0 + 9 * H)
    assert p["times"] == [T0, T0 + H, T0 + 3 * H, T0 + 4 * H]
    assert p["series"]["binance_fut"] == [0.0, 4.0, 8.0, 12.0]
    assert p["series"]["binance_spot"] == [0.0, 2.0, 4.0, 6.0]


def test_cumulative_首点为零():
    assert cvd.cumulative([5.0, 1.0, -3.0, 2.0]) == [0.0, 1.0, -2.0, 0.0]
    assert cvd.cumulative([7.0]) == [0.0]
    assert cvd.cumulative([]) == []


# ───────────────────────── CVD × 价格（与 orderflow.divergence 同语义） ─────────────────────────
VS_PRICE_CASES = {
    "bear_div": trend(30, 60000, +50, 0.3),        # 价涨、主动卖占优
    "bull_div": trend(30, 60000, -50, 0.7),        # 价跌、主动买占优
    "confirm_up": trend(30, 60000, +50, 0.7),
    "confirm_down": trend(30, 60000, -50, 0.3),
    "flat": trend(30, 60000, +0.01, 0.3),          # 价格几乎不动 → 不判背离
}


@pytest.mark.parametrize("state", list(VS_PRICE_CASES))
def test_cvd_vs_price_各状态(state):
    bars = VS_PRICE_CASES[state]
    d = cvd.cvd_vs_price(bars, bars, "1h", "合约")
    assert d["state"] == state
    assert set(d) == {"state", "text"}
    assert "合约CVD" in d["text"] and d["text"].startswith("近24h")


def test_cvd_vs_price_数据不足():
    bars = trend(24, 60000, 50, 0.7)               # 需要 bars+1 根
    assert cvd.cvd_vs_price(bars, bars, "1h", "合约") == {"state": "insufficient", "text": "数据不足"}


def test_cvd_vs_price_与orderflow逐例一致():
    """随机数据上，状态必须与 orderflow.divergence 的规则逐例相同（防元组布局对错位）。"""
    rnd = random.Random(7)
    for tf in cvd.SUPPORTED_TFS:
        for _ in range(60):
            px, bars = 60000.0, []
            for i in range(40):
                nxt = px * (1 + rnd.uniform(-0.002, 0.002))
                vol = rnd.uniform(50, 500)
                bars.append(B(T0 + i * cvd.TF_MS[tf], px, nxt, vol, vol * rnd.uniform(0.35, 0.65)))
                px = nxt
            seg = bars[-24:]
            p = (bars[-1][2] - seg[0][1]) / seg[0][1] * 100
            c = sum(2 * b[4] - b[3] for b in seg)
            if abs(p) < cvd.MIN_PRICE_PCT[tf]:
                want = "flat"
            elif p > 0:
                want = "bear_div" if c < 0 else "confirm_up"
            else:
                want = "bull_div" if c > 0 else "confirm_down"
            assert cvd.cvd_vs_price(bars, bars, tf, "合约")["state"] == want
            ks = [(b[0], b[1], 0, 0, b[2], b[3], 0, b[4], 1) for b in bars]
            assert want == orderflow_divergence(ks, 24, cvd.MIN_PRICE_PCT[tf])["state"]


def test_cvd_vs_price_净量很小时文字标弱但状态不变():
    """价涨 + CVD 只是略负：orderflow 的规则仍判顶背离（state 不动），文字要标"(弱)"。"""
    bars = trend(30, 60000, +50, 0.499)            # 净主动量 = 成交量的 -0.2%
    strong = trend(30, 60000, +50, 0.3)            # 净主动量 = 成交量的 -40%
    weak = cvd.cvd_vs_price(bars, bars, "1h", "合约", weak_pct=0.8)
    assert weak["state"] == "bear_div" and "顶背离(弱)" in weak["text"]
    assert "(弱)" not in cvd.cvd_vs_price(strong, strong, "1h", "合约", weak_pct=0.8)["text"]
    flat = trend(30, 60000, +0.01, 0.499)          # 价格横盘时不加标注
    assert "(弱)" not in cvd.cvd_vs_price(flat, flat, "1h", "合约", weak_pct=0.8)["text"]
    div = cvd.divergences("1h", bars, bars)        # divergences 会按周期带上门槛
    assert "(弱)" in div["fut_vs_price"]["text"] and "(弱)" in div["spot_vs_price"]["text"]


def test_spot_vs_price_用合约价配现货流():
    fut = trend(30, 60000, +50, 0.7)               # 价涨，合约主动买
    spot = trend(30, 60010, +50, 0.3, vol=10)      # 现货主动卖
    div = cvd.divergences("1h", fut, spot)
    assert div["fut_vs_price"]["state"] == "confirm_up"
    assert div["spot_vs_price"]["state"] == "bear_div"
    assert "现货CVD" in div["spot_vs_price"]["text"]


# ───────────────────────── 合约 × 现货 ─────────────────────────
def _ratio(direction, flat_pct):
    """造一个主动买占比，使净主动量占成交量的比例明显在门槛的某一侧。"""
    if direction == 0:
        return 0.5 + flat_pct * 0.25 / 100 / 2     # 比例 = 门槛的 1/4 → 无明显方向
    return 0.5 + direction * flat_pct * 3 / 100 / 2  # 比例 = 门槛的 3 倍


FVS_CASES = {
    (1, 1): "both_up", (-1, -1): "both_down",
    (1, -1): "fut_up_spot_down", (-1, 1): "fut_down_spot_up",
    (1, 0): "fut_only_up", (-1, 0): "fut_only_down",
    (0, 1): "spot_only_up", (0, -1): "spot_only_down",
    (0, 0): "flat",
}


@pytest.mark.parametrize("tf", cvd.SUPPORTED_TFS)
@pytest.mark.parametrize("dirs", list(FVS_CASES))
def test_fut_vs_spot_各状态(tf, dirs):
    f_flat, s_flat = cvd.CVD_FLAT_PCT[tf]
    fut = trend(30, 60000, 10, _ratio(dirs[0], f_flat), tf=tf)
    spot = trend(30, 60000, 10, _ratio(dirs[1], s_flat), vol=12, tf=tf)
    d = cvd.fut_vs_spot(fut, spot, tf)
    assert d["state"] == FVS_CASES[dirs]
    assert set(d) == {"state", "text"}
    assert "合约CVD" in d["text"] and "现货CVD" in d["text"]


def test_fut_vs_spot_杠杆推动现货没跟():
    fut = trend(30, 60000, 10, 0.6)                # 合约净主动买 = 成交量的 20%
    spot = trend(30, 60000, 10, 0.4, vol=12)       # 现货净主动卖
    d = cvd.fut_vs_spot(fut, spot, "1h")
    assert d["state"] == "fut_up_spot_down"
    assert "杠杆" in d["text"]


def test_fut_vs_spot_只看最近24根():
    old = trend(20, 60000, 10, 0.3)                              # 很早以前在卖
    new_f = trend(24, 60200, 10, 0.6, t0=T0 + 20 * H)            # 最近 24 根两边都在买
    new_s = trend(24, 60200, 10, 0.6, vol=12, t0=T0 + 20 * H)
    assert cvd.fut_vs_spot(old + new_f, old + new_s, "1h")["state"] == "both_up"


def test_fut_vs_spot_数据不足():
    fut = trend(24, 60000, 10, 0.6)
    assert cvd.fut_vs_spot(fut, fut, "1h")["state"] == "insufficient"


def test_flow_direction_门槛():
    assert cvd.flow_direction(1.0, 100.0, 0.8) == 1
    assert cvd.flow_direction(-1.0, 100.0, 0.8) == -1
    assert cvd.flow_direction(0.5, 100.0, 0.8) == 0
    assert cvd.flow_direction(5.0, 0.0, 0.8) == 0              # 没成交量不判方向


def test_window_label():
    assert [cvd.window_label(tf) for tf in ("5m", "15m", "1h", "4h")] == \
        ["近2h", "近6h", "近24h", "近4天"]


# ───────────────────────── 文案禁词 ─────────────────────────
def _all_texts():
    texts = ["数据不足"]
    for tf in cvd.SUPPORTED_TFS:
        f_flat, s_flat = cvd.CVD_FLAT_PCT[tf]
        for name in ("合约", "现货"):
            for bars in VS_PRICE_CASES.values():
                rebased = [B(T0 + i * cvd.TF_MS[tf], b[1], b[2], b[3], b[4])
                           for i, b in enumerate(bars)]
                texts.append(cvd.cvd_vs_price(rebased, rebased, tf, name)["text"])
                texts.append(cvd.cvd_vs_price(rebased, rebased, tf, name, weak_pct=99.0)["text"])
        for dirs in FVS_CASES:
            fut = trend(30, 60000, 10, _ratio(dirs[0], f_flat), tf=tf)
            spot = trend(30, 60000, 10, _ratio(dirs[1], s_flat), vol=12, tf=tf)
            texts.append(cvd.fut_vs_spot(fut, spot, tf)["text"])
        texts += [v["text"] for v in cvd.empty_payload(tf)["div"].values()]
    texts += [words for _state, words in cvd._FVS.values()]
    texts += list(cvd._VS_PRICE_LABEL.values())
    return texts


def test_文案只描述不给方向():
    texts = _all_texts()
    assert len(texts) > 80
    for t in texts:
        assert t and isinstance(t, str)
        for w in BANNED:
            assert w not in t, f"文案出现禁词 {w!r}: {t}"


def test_状态名覆盖完整():
    assert {s for s, _ in cvd._FVS.values()} == set(FVS_CASES.values())
    assert len(cvd._FVS) == 9                       # 3×3 方向组合一个不漏


# ───────────────────────── payload ─────────────────────────
def test_build_payload_形状与契约一致():
    fut = trend(60, 60000, 5, 0.6)
    spot = trend(60, 60005, 5, 0.45, vol=11)
    p = cvd.build_payload("1h", fut, spot, now_ms=T0 + 100 * H)
    assert set(p) == {"tf", "times", "price", "series", "div", "ts"}
    assert p["tf"] == "1h" and p["ts"] == T0 + 100 * H
    assert set(p["series"]) == {"binance_fut", "binance_spot"}
    n = len(p["times"])
    assert n == 60 and len(p["price"]) == n
    assert all(len(v) == n for v in p["series"].values())
    assert all(v[0] == 0.0 for v in p["series"].values())          # 每条线从 0 起
    assert p["times"] == sorted(p["times"])
    assert p["price"][-1] == fut[-1][2]                             # 价格 = 合约收盘价
    assert p["series"]["binance_fut"][-1] == pytest.approx(59 * 20.0)       # 每根 +20
    assert p["series"]["binance_spot"][-1] == pytest.approx(59 * -1.1)      # 每根 -1.1
    assert set(p["div"]) == {"fut_vs_price", "spot_vs_price", "fut_vs_spot"}
    for d in p["div"].values():
        assert set(d) == {"state", "text"}
    json.dumps(p)                                                    # 可序列化


def test_build_payload_截取输出窗口():
    fut = trend(250, 60000, 1, 0.6)
    p = cvd.build_payload("1h", fut, fut, now_ms=T0 + 300 * H)
    assert len(p["times"]) == cvd.OUT_BARS
    assert p["times"][-1] == fut[-1][0]
    assert p["series"]["binance_fut"][0] == 0.0


def test_build_payload_okx只在覆盖全部棒时带上():
    fut = trend(30, 60000, 5, 0.6)
    okx = {b[0]: 3.0 for b in fut}
    p = cvd.build_payload("1h", fut, fut, okx, now_ms=T0 + 50 * H)
    assert p["series"]["okx_swap"] == [3.0 * i for i in range(30)]
    del okx[fut[10][0]]                                              # 中间缺一根 → 整条不出
    p = cvd.build_payload("1h", fut, fut, okx, now_ms=T0 + 50 * H)
    assert "okx_swap" not in p["series"]


def test_空输入给合法空payload():
    for p in (cvd.build_payload("15m", [], [], now_ms=123), cvd.empty_payload("15m", 123)):
        assert p == {
            "tf": "15m", "times": [], "price": [],
            "series": {"binance_fut": [], "binance_spot": []},
            "div": {k: {"state": "insufficient", "text": "数据不足"}
                    for k in ("fut_vs_price", "spot_vs_price", "fut_vs_spot")},
            "ts": 123,
        }
    # 两边完全对不上也算空
    fut = [B(T0, 1, 1, 1, 1)]
    spot = [B(T0 + H, 1, 1, 1, 1)]
    assert cvd.build_payload("1h", fut, spot, now_ms=5)["times"] == []


def test_get_cached_未刷新时为空且不认识的周期按1h():
    p = cvd.get_cached("4h")
    assert p["tf"] == "4h" and p["times"] == [] and p["ts"] == 0
    assert cvd.get_cached("乱写")["tf"] == "1h"
    assert json.loads(cvd.get_cached_json("5m"))["tf"] == "5m"
    assert cvd.last_updated_ms() == 0


# ───────────────────────── OKX 解析与对账 ─────────────────────────
def test_parse_okx():
    taker = cvd.parse_okx_taker([[str(T0), "7.5", "2.5"], ["坏"], [str(T0 + H), "1", "4"]])
    assert taker == {T0: (7.5, 2.5), T0 + H: (1.0, 4.0)}
    candles = cvd.parse_okx_candles([
        [str(T0 + H), "1", "1", "1", "1", "500", "5.0", "300000", "0"],     # 未收盘 → 不收
        [str(T0), "1", "1", "1", "1", "1000", "10.0", "600000", "1"],
    ])
    assert candles == {T0: 10.0}


def _okx_fixture(n=30, gap=None, last_factor=1.0):
    """n 根 1h：主动卖 30、主动买 33，K 线成交量 63。gap={下标: 系数} 模拟统计缺量。"""
    times = [T0 + i * H for i in range(n)]
    taker = {t: (30.0, 33.0) for t in times}
    vol = {t: 63.0 for t in times}
    for i, k in (gap or {}).items():
        taker[times[i]] = (30.0 * k, 33.0 * k)
    if last_factor != 1.0:
        taker[times[-1]] = (30.0 * last_factor, 33.0 * last_factor)
    return times, taker, vol


def test_okx_deltas_对平则给出买减卖():
    times, taker, vol = _okx_fixture()
    now = times[-1] + H + cvd.SETTLE_MS["1h"]
    out, why, missing = cvd.okx_deltas(taker, vol, times, "1h", now)
    assert why is None and missing == 0.0
    assert out == {t: 3.0 for t in times}                           # 33 买 - 30 卖


def test_okx_deltas_最新棒没补齐则整条不出():
    times, taker, vol = _okx_fixture(last_factor=0.97)              # 最新一根只到 97%
    now = times[-1] + H + cvd.SETTLE_MS["1h"]
    out, why, _ = cvd.okx_deltas(taker, vol, times, "1h", now)
    assert out is None and "尚未补齐" in why


def test_okx_deltas_老棒容许少量缺口():
    times, taker, vol = _okx_fixture(gap={5: 0.95})                 # 很早的一根漏了 5%
    now = times[-1] + H + cvd.SETTLE_MS["1h"]
    out, why, missing = cvd.okx_deltas(taker, vol, times, "1h", now)
    assert why is None and out[times[5]] == pytest.approx(3.0 * 0.95)
    assert missing == pytest.approx(63 * 0.05 / (63 * 30) * 100)    # 整窗只漏 0.17%


def test_okx_deltas_整窗缺量过多则不出():
    times, taker, vol = _okx_fixture(gap={i: 0.9 for i in range(10)})   # 10 根各漏 10% → 整窗 3.3%
    now = times[-1] + 5 * H
    out, why, missing = cvd.okx_deltas(taker, vol, times, "1h", now)
    assert out is None and "缺量过多" in why
    assert missing == pytest.approx(10 * 6.3 / (63 * 30) * 100)


def test_okx_deltas_过了收紧期的最新棒按整窗缺量算():
    times, taker, vol = _okx_fixture(last_factor=0.97)
    just_before = times[-1] + H + cvd.OKX_TIGHT_MS - 1
    assert cvd.okx_deltas(taker, vol, times, "1h", just_before)[0] is None
    after = times[-1] + H + cvd.OKX_TIGHT_MS                         # 收紧期已过：视为永久小缺口
    out, why, missing = cvd.okx_deltas(taker, vol, times, "1h", after)
    assert why is None and len(out) == len(times) and 0 < missing < cvd.OKX_MAX_MISSING_PCT


def test_okx_deltas_单位不对必然出界():
    times, taker, vol = _okx_fixture()
    now = times[-1] + 5 * H
    for factor in (100, 65000, 0.01):          # 张数当成币 / 美元当成币 / 币当成张
        wrong = {t: (s * factor, b * factor) for t, (s, b) in taker.items()}
        out, why, _ = cvd.okx_deltas(wrong, vol, times, "1h", now)
        assert out is None and "对不上" in why
    half = {t: (s * 0.5, b * 0.5) for t, (s, b) in taker.items()}      # 只统计到一半
    assert cvd.okx_deltas(half, vol, times, "1h", now)[0] is None


def test_okx_deltas_缺行或缺K线():
    times, taker, vol = _okx_fixture()
    now = times[-1] + 5 * H
    t_miss = dict(taker)
    del t_miss[times[3]]
    out, why, _ = cvd.okx_deltas(t_miss, vol, times, "1h", now)
    assert out is None and "缺主动成交统计" in why
    v_miss = dict(vol)
    del v_miss[times[3]]
    out, why, _ = cvd.okx_deltas(taker, v_miss, times, "1h", now)
    assert out is None and "无法对账" in why
    assert cvd.okx_deltas(taker, vol, [], "1h", now)[0] is None


# ───────────────────────── refresh：假会话，不触网 ─────────────────────────
class FakeResp:
    def __init__(self, data, status=200, headers=None):
        self._data, self.status, self.headers = data, status, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._data


class FakeMarket:
    """假会话：按 self.now 生成三家交易所的原始返回，并记下每次请求。"""

    def __init__(self, now, headers=None):
        self.now = now
        self.calls: list[str] = []
        self.fail: set[str] = set()          # 这些来源抛网络异常
        self.http: dict[str, tuple] = {}     # 来源 → (status, headers)
        self.okx_last_factor = 1.0           # <1 模拟 OKX 最新收盘棒没补齐
        self.okx_code = "0"
        self.fut_weight = "7"
        if headers is not None:
            self.headers = headers           # 模拟带默认头的会话

    # 每根棒的合成数据（只由棒序号决定，可重复）
    @staticmethod
    def _bar(i):
        o = 60000.0 + (i % 40) * 10
        return o, o + 10, 100.0 + (i % 7) * 10, (0.56 if (i // 6) % 2 == 0 else 0.46)

    def _klines(self, params, spot):
        step = cvd.TF_MS[params["interval"]]
        cur = self.now // step * step
        out = []
        for j in range(int(params["limit"])):
            t = cur - (int(params["limit"]) - 1 - j) * step
            o, c, vol, ratio = self._bar(t // step)
            if spot:
                vol, ratio = vol / 10, 1.02 - ratio
            out.append(raw_k(t, o, c, vol, vol * ratio, step))
        return out

    def _okx_step(self, params):
        key = params.get("period") or params.get("bar")
        return {"1H": H, "4H": 4 * H}[key]

    def _okx_taker(self, params):
        step = self._okx_step(params)
        cur = self.now // step * step
        newest = int(params["end"]) - step if "end" in params else cur
        rows = []
        for j in range(int(params["limit"])):
            t = newest - j * step
            i = t // step
            sell, buy = 30.0 + i % 5, 33.0 + i % 3
            if t == cur - step:
                sell, buy = sell * self.okx_last_factor, buy * self.okx_last_factor
            rows.append([str(t), str(sell), str(buy)])
        return {"code": self.okx_code, "data": rows, "msg": "x" if self.okx_code != "0" else ""}

    def _okx_candles(self, params):
        step = self._okx_step(params)
        cur = self.now // step * step
        rows = []
        for j in range(int(params["limit"])):
            t = cur - j * step
            i = t // step
            vol = 30.0 + i % 5 + 33.0 + i % 3
            rows.append([str(t), "1", "1", "1", "1", str(vol * 100), str(vol), "0",
                         "0" if j == 0 else "1"])
        return {"code": "0", "data": rows, "msg": ""}

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        if "/fapi/v1/klines" in url:
            src = "fut"
        elif "/api/v3/klines" in url:
            src = "spot"
        elif "taker-volume-contract" in url:
            src = "okx_taker"
        elif "/market/candles" in url:
            src = "okx_candles"
        else:
            raise AssertionError(f"意外的请求 {url}")
        self.calls.append(src)
        if src in self.fail:
            raise OSError(f"{src} 网络不通")
        if src in self.http:
            status, headers = self.http[src]
            return FakeResp({}, status, headers)
        if src == "fut":
            return FakeResp(self._klines(params, False),
                            headers={"X-MBX-USED-WEIGHT-1M": self.fut_weight})
        if src == "spot":
            return FakeResp(self._klines(params, True))
        if src == "okx_taker":
            return FakeResp(self._okx_taker(params))
        return FakeResp(self._okx_candles(params))


def run(coro):
    return asyncio.run(coro)


NOW_15M = T0 + 3_000 + 7 * M               # 00:07:03，15m 周期最新收盘棒 = 昨天 23:45 那根
NOW_1H = T0 + cvd.SETTLE_MS["1h"] + 5_000   # 整点过后刚出宽限


def test_refresh_15m_只打币安两路并缓存():
    mk = FakeMarket(NOW_15M)
    assert run(cvd.refresh(mk, "15m", now_ms=NOW_15M)) is True
    assert sorted(mk.calls) == ["fut", "spot"]                      # 15m 不带 OKX
    p = cvd.get_cached("15m")
    assert p["tf"] == "15m" and p["ts"] == NOW_15M
    assert len(p["times"]) == cvd.OUT_BARS
    assert p["times"][-1] == T0 - 15 * M                            # 未收盘的 00:00 那根被丢掉
    assert set(p["series"]) == {"binance_fut", "binance_spot"}
    assert all(v[0] == 0.0 and len(v) == cvd.OUT_BARS for v in p["series"].values())
    assert json.loads(cvd.get_cached_json("15m")) == p
    assert cvd.last_updated_ms() == NOW_15M
    assert cvd.status()["tfs"]["15m"]["last_error"] is None
    assert cvd.status()["binance_used_weight_1m"] == 7


def test_refresh_缓存已是最新时不再发请求():
    mk = FakeMarket(NOW_15M)
    run(cvd.refresh(mk, "15m", now_ms=NOW_15M))
    n = len(mk.calls)
    for dt in (1_000, 30_000, 5 * M):                               # 同一根棒内反复调
        mk.now = NOW_15M + dt
        assert run(cvd.refresh(mk, "15m", now_ms=NOW_15M + dt)) is True
    assert len(mk.calls) == n
    mk.now = NOW_15M + 15 * M                                       # 下一根收盘了 → 才再取
    run(cvd.refresh(mk, "15m", now_ms=mk.now))
    assert len(mk.calls) == n + 2
    assert cvd.get_cached("15m")["times"][-1] == T0


def test_refresh_1h_带okx且数值为买减卖的累计():
    mk = FakeMarket(NOW_1H)
    assert run(cvd.refresh(mk, "1h", now_ms=NOW_1H)) is True
    assert mk.calls.count("okx_taker") == 2 and mk.calls.count("okx_candles") == 1
    p = cvd.get_cached("1h")
    assert set(p["series"]) == {"binance_fut", "binance_spot", "okx_swap"}
    assert p["times"][-1] == T0 - H
    want, acc = [], 0.0
    for k, t in enumerate(p["times"]):
        i = t // H
        if k:
            acc += (33.0 + i % 3) - (30.0 + i % 5)
        want.append(round(acc, 2))
    assert p["series"]["okx_swap"] == want
    st = cvd.status()["tfs"]["1h"]
    assert st["okx"] is True and st["okx_note"] is None and st["okx_missing_pct"] == 0.0


def test_refresh_okx没补齐时先不带_之后只补okx不再打币安():
    mk = FakeMarket(NOW_1H)
    mk.okx_last_factor = 0.9
    assert run(cvd.refresh(mk, "1h", now_ms=NOW_1H)) is True
    p = cvd.get_cached("1h")
    assert "okx_swap" not in p["series"] and len(p["times"]) == cvd.OUT_BARS
    assert "尚未补齐" in cvd.status()["tfs"]["1h"]["okx_note"]
    n = len(mk.calls)

    run(cvd.refresh(mk, "1h", now_ms=NOW_1H + 30_000))              # 还没到补取时间
    assert len(mk.calls) == n

    mk.okx_last_factor = 1.0                                         # OKX 补齐了
    mk.now = NOW_1H + 61_000
    assert run(cvd.refresh(mk, "1h", now_ms=mk.now)) is True
    assert mk.calls[n:] == ["okx_taker", "okx_taker", "okx_candles"]         # 没有再打币安
    p2 = cvd.get_cached("1h")
    assert "okx_swap" in p2["series"] and p2["times"] == p["times"]
    assert p2["series"]["binance_fut"] == p["series"]["binance_fut"]
    assert cvd.status()["tfs"]["1h"]["okx_note"] is None


def test_refresh_okx连续补不上时补取间隔加倍():
    mk = FakeMarket(NOW_1H)
    mk.okx_last_factor = 0.9
    run(cvd.refresh(mk, "1h", now_ms=NOW_1H))                       # 第 1 次对不上
    n = len(mk.calls)
    run(cvd.refresh(mk, "1h", now_ms=NOW_1H + 61_000))              # 60 秒后补取，第 2 次对不上
    assert mk.calls[n:] == ["okx_taker", "okx_taker", "okx_candles"]
    n = len(mk.calls)
    run(cvd.refresh(mk, "1h", now_ms=NOW_1H + 61_000 + 90_000))     # 间隔已翻倍到 120 秒
    assert len(mk.calls) == n
    run(cvd.refresh(mk, "1h", now_ms=NOW_1H + 61_000 + 121_000))
    assert mk.calls[n:] == ["okx_taker", "okx_taker", "okx_candles"]
    assert "fut" not in mk.calls[2:] and "spot" not in mk.calls[2:]  # 自始至终只打过一次币安


def test_refresh_okx报错不影响币安两条线():
    mk = FakeMarket(NOW_1H)
    mk.okx_code = "50011"                                            # OKX 限频
    assert run(cvd.refresh(mk, "1h", now_ms=NOW_1H)) is True
    p = cvd.get_cached("1h")
    assert set(p["series"]) == {"binance_fut", "binance_spot"} and len(p["times"]) == cvd.OUT_BARS
    assert "50011" in cvd.status()["tfs"]["1h"]["okx_note"]

    cvd._reset_cache()
    mk = FakeMarket(NOW_1H)
    mk.fail.add("okx_candles")                                       # 取不到 K 线 → 无法对账 → 不出
    assert run(cvd.refresh(mk, "1h", now_ms=NOW_1H)) is True
    assert "okx_swap" not in cvd.get_cached("1h")["series"]


def test_refresh_带密钥头的会话不向okx发请求():
    mk = FakeMarket(NOW_1H, headers={"X-MBX-APIKEY": "k"})
    assert run(cvd.refresh(mk, "1h", now_ms=NOW_1H)) is True
    assert "okx_taker" not in mk.calls and "okx_candles" not in mk.calls
    assert "okx_swap" not in cvd.get_cached("1h")["series"]
    assert "密钥" in cvd.status()["tfs"]["1h"]["okx_note"]


def test_refresh_币安失败保留旧缓存并返回False():
    mk = FakeMarket(NOW_15M)
    run(cvd.refresh(mk, "15m", now_ms=NOW_15M))
    old = cvd.get_cached("15m")
    mk.now = NOW_15M + 15 * M
    mk.fail.add("spot")
    assert run(cvd.refresh(mk, "15m", now_ms=mk.now)) is False
    assert cvd.get_cached("15m") is old                             # 旧缓存原样保留
    assert "网络不通" in cvd.status()["tfs"]["15m"]["last_error"]
    n = len(mk.calls)
    run(cvd.refresh(mk, "15m", now_ms=mk.now + 5_000))              # 20 秒内不重试
    assert len(mk.calls) == n
    mk.fail.clear()
    mk.now += 41_000                                                # 失败过一次 → 间隔翻倍到 40 秒
    assert run(cvd.refresh(mk, "15m", now_ms=mk.now)) is True
    assert cvd.get_cached("15m")["times"][-1] == T0
    assert cvd.status()["tfs"]["15m"]["last_error"] is None


def test_refresh_从未成功且失败时仍给空payload():
    mk = FakeMarket(NOW_15M)
    mk.fail.update({"fut", "spot"})
    assert run(cvd.refresh(mk, "15m", now_ms=NOW_15M)) is False
    assert cvd.get_cached("15m") == cvd.empty_payload("15m")


def test_refresh_被限流后静默期内不再碰币安():
    mk = FakeMarket(NOW_15M)
    mk.http["fut"] = (429, {"Retry-After": "120"})
    assert run(cvd.refresh(mk, "15m", now_ms=NOW_15M)) is False
    assert cvd.status()["backoff_until_ms"] == NOW_15M + 120_000
    n = len(mk.calls)
    mk.http.clear()
    for tf in cvd.SUPPORTED_TFS:                                    # 其它周期也一起静默
        assert run(cvd.refresh(mk, tf, now_ms=NOW_15M + 100_000)) is False
    assert run(cvd.refresh(mk, "15m", now_ms=NOW_15M + 110_000, force=True)) is False
    assert len(mk.calls) == n                                       # force 也不能破静默期
    mk.now = NOW_15M + 121_000
    assert run(cvd.refresh(mk, "15m", now_ms=mk.now)) is True
    assert len(cvd.get_cached("15m")["times"]) == cvd.OUT_BARS


def test_refresh_418封禁同样静默至少60秒():
    mk = FakeMarket(NOW_15M)
    mk.http["spot"] = (418, {"Retry-After": "5"})
    assert run(cvd.refresh(mk, "15m", now_ms=NOW_15M)) is False
    assert cvd.status()["backoff_until_ms"] == NOW_15M + 60_000


def test_refresh_合约权重过高时让路给实盘引擎():
    mk = FakeMarket(NOW_15M)
    mk.fut_weight = str(cvd.WEIGHT_GUARD + 300)
    assert run(cvd.refresh(mk, "15m", now_ms=NOW_15M)) is True      # 这一次照常，但记下了权重
    n = len(mk.calls)
    mk.now = NOW_15M + 15 * M
    assert run(cvd.refresh(mk, "5m", now_ms=NOW_15M + 1_000)) is False
    assert "fut" not in mk.calls[n:]                                # 没有再打合约接口
    assert cvd.status()["backoff_until_ms"] > NOW_15M


def test_refresh_交易所没给出新棒时拉长重试间隔():
    mk = FakeMarket(NOW_15M - 30 * M)                               # 交易所数据停在更早的一根
    assert run(cvd.refresh(mk, "15m", now_ms=NOW_15M)) is True
    assert cvd.get_cached("15m")["times"][-1] == T0 - 30 * M        # 比目标棒旧一根
    n = len(mk.calls)
    run(cvd.refresh(mk, "15m", now_ms=NOW_15M + 25_000))            # 没进展 → 间隔 40 秒
    assert len(mk.calls) == n
    mk.now = NOW_15M + 41_000
    run(cvd.refresh(mk, "15m", now_ms=NOW_15M + 41_000))
    assert len(mk.calls) == n + 2
    assert cvd.get_cached("15m")["times"][-1] == T0 - 15 * M


def test_refresh_全部周期与force():
    mk = FakeMarket(NOW_1H)
    assert run(cvd.refresh(mk, now_ms=NOW_1H)) is True
    for tf in cvd.SUPPORTED_TFS:
        p = cvd.get_cached(tf)
        assert p["tf"] == tf and len(p["times"]) == cvd.OUT_BARS
        assert ("okx_swap" in p["series"]) == (tf in cvd.OKX_TFS)
        step = cvd.TF_MS[tf]
        assert all(b - a == step for a, b in zip(p["times"], p["times"][1:]))
        for d in p["div"].values():
            assert d["state"] != "insufficient"
    assert mk.calls.count("fut") == 4 and mk.calls.count("spot") == 4
    n = len(mk.calls)
    run(cvd.refresh(mk, now_ms=NOW_1H + 1_000))                     # 都是新的 → 0 请求
    assert len(mk.calls) == n
    run(cvd.refresh(mk, "5m", now_ms=NOW_1H + 2_000, force=True))   # force 无视缓存
    assert len(mk.calls) == n + 2


def test_refresh_不认识的周期按1h():
    mk = FakeMarket(NOW_1H)
    assert run(cvd.refresh(mk, "3m", now_ms=NOW_1H)) is True
    assert cvd.get_cached("1h")["times"] and not cvd.get_cached("5m")["times"]


def test_refresh_生成的文案也不含禁词():
    mk = FakeMarket(NOW_1H)
    run(cvd.refresh(mk, now_ms=NOW_1H))
    for tf in cvd.SUPPORTED_TFS:
        for d in cvd.get_cached(tf)["div"].values():
            assert not any(w in d["text"] for w in BANNED)
