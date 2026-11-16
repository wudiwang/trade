"""合约 vs 现货 CVD 对比 —— /api/flow/cvd 的数据源（2026-11-16）。

想一眼看清的事：这段涨跌是【杠杆推出来的】还是【现货跟着一起买/卖出来的】。
  合约 CVD 升、现货 CVD 降 → 杠杆在推，现货没跟
  两边同向               → 两边都有主动盘
这里只描述状态，不给任何方向结论。

口径（全部是 taker / 主动成交口径，单位 BTC）
  币安合约 /fapi/v1/klines 与币安现货 /api/v3/klines 列布局相同，下标 9 是
  takerBuyBaseAssetVolume，所以每根 delta = 主动买 - 主动卖 = 2*taker_buy - volume。
  两边按 open_time 内连接；任一侧缺的棒整根丢掉（补 0 等于编造"这段没有主动成交"）。

未收盘的棒：一律丢弃，只用已收盘的棒。
  没收完的棒成交量是半截的，三个来源抓取时刻又不同，半截对半截没法比，
  背离状态也会在一根棒内来回跳。代价是图上最新一点最多滞后一根棒。

OKX（2026-11-16 用真实返回逐项核对，不是照文档猜的）
  用的是 /api/v5/rubik/stat/taker-volume-contract（instId=BTC-USDT-SWAP, unit=0）：
    - 单一合约口径，不是全所汇总；unit 0=币 1=张 2=U，行是 [ts, sellVol, buyVol]，ts=开盘时间
    - 单位核对：unit=0 时 sell+buy 与同一根 K 线的 volCcy(币) 逐根相等
      （如 1120.9783 + 1054.0886 = 2175.0669）；unit=1 等于 K 线的张数，unit=2 等于 USDT 成交额
    - 列顺序核对：把一根 5m 棒的全部逐笔成交(5381 笔)按主动方向聚合，
      主动卖 7600.47 张 = 第 2 列，主动买 3959.25 张 = 第 3 列，分毫不差
    - 周期 5m/15m/1H/4H 都有；4H 开在 UTC 0/4/8…点，与币安 4h 对齐
    - 每次最多 100 行、新→旧，用 end= 翻页；限频 5 次/2 秒
  没用 /api/v5/rubik/stat/taker-volume（ccy=BTC, instType=CONTRACTS）：
    - 只有 5m/1H/1D，没有 15m/4H
    - 文档不写单位。实测是美元量级的"全部 BTC 合约汇总"，而且时间戳比 K 线开盘时间晚一根：
      按同一时间戳对，它与单合约 U 成交额的比值在 0.4~2.1 之间乱跳；错开一根再对，稳定在 1.1~1.2 倍
    - 要变成 BTC 只能拿价格去除，口径和时间对齐都得靠推断 —— 不碰。
    （同接口 instType=SPOT 实测是 BTC 量级的全部现货币对汇总，同样晚一根；本模块也没用。）

  OKX 这份统计有两个毛病，决定了下面的取舍：
    1. 滞后：收盘后要等几分钟才补齐（实测见 SETTLE_MS 处的注释）。
       所以 okx_swap 只在 1h / 4h 提供，并且这两个周期整体晚几分钟发布，三条线同时更新；
       5m / 15m 不带 OKX（滞后已经接近一整根棒）。
    2. 偶有缺量：剧烈波动的个别小时，sell+buy 比 K 线成交量少 0.5%~5%。
       每次刷新都拿 OKX 自己的 K 线成交量逐根对账（顺带持续验证单位没变）：
       刚收盘的棒必须对平，老棒允许少量缺口；对不上就整条线不出，绝不拿对不上的数画图。

结构
  纯计算（不碰网络，可离线单测）：parse_* / align / cumulative / divergences / okx_deltas / build_payload
  取数（注入 aiohttp session）：fetch_binance_klines / fetch_okx
  缓存：refresh()（后台循环里调）＋ get_cached() / get_cached_json()（请求路径，永不触网）

给集成方
  - 后台每 30~60 秒调一次 `await refresh(session)` 即可：缓存已含最新收盘棒时它直接返回，
    不发任何请求；实际大约每根棒收盘才取一次数（每次币安合约权重 2 + 现货权重 2）。
  - 注入的 session 必须是不带密钥头的普通会话，【不要】把 BinanceRest 的会话传进来
    （那个会话默认带 X-MBX-APIKEY；本模块发现密钥头会拒绝向 OKX 发请求）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Sequence

import aiohttp

from app.engine.orderflow import divergence as _divergence_vs_price

log = logging.getLogger("flow.cvd")

# ───────────────────────── 常量 ─────────────────────────
SYMBOL = "BTCUSDT"
OKX_INST = "BTC-USDT-SWAP"

TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
SUPPORTED_TFS = tuple(TF_MS)
DEFAULT_TF = "1h"

FETCH_BARS = 200          # 每次向交易所要的根数（含未收盘的那根）；币安合约 limit<500 权重 2
OUT_BARS = 192            # 输出根数（≈200；比取数少几根，容许各来源首尾差一两根）
DIV_BARS = 24             # 背离/对比的观察窗口（根数）

# 一根棒"收盘后再等多久"才算数。
#   5m/15m 只有币安两路，K 线收盘即定稿，留 3 秒防本机时钟略快于交易所。
#   1h/4h 要等 OKX 的统计补齐，三条线才能同时更新（见模块说明"滞后"一条）。
#   实测(2026-11-16，5 次收盘)：OKX 在收盘后 83 / 103 / 133 / 143 秒补齐，另有一次 +150 秒仍未补齐
#   （5 分钟时已齐）。取 6 分钟；万一那时还没齐，对账会拦下来，稍后只补取 OKX。
SETTLE_MS = {"5m": 3_000, "15m": 3_000, "1h": 360_000, "4h": 360_000}
OKX_TFS = ("1h", "4h")    # 只有这些周期带 okx_swap
_OKX_PERIOD = {"1h": "1H", "4h": "4H"}

# 价格"横盘"门槛(%)：窗口内涨跌幅小于它就不判背离（与 orderflow.divergence 同一语义）。
#   1h 沿用 orderflow 的 0.3；其余周期取 2026-11-16 时最近约 1000 根样本里
#   "24 根窗口涨跌幅绝对值"的 20 分位附近（5m 0.16 / 15m 0.16 / 1h 0.34 / 4h 0.78）。
#   这只是显示用的降噪门槛，没有做过任何预测力验证。
MIN_PRICE_PCT = {"5m": 0.15, "15m": 0.2, "1h": 0.3, "4h": 0.7}

# CVD"无明显方向"门槛：窗口内净主动量占窗口总成交量的百分比，(合约, 现货)。
#   同一批样本的 20 分位附近（合约 1.77/1.45/0.85/0.47，现货 3.54/2.16/2.13/1.22）。
#   现货的买卖失衡天然比合约大，所以门槛分开给。同样只是降噪，不是信号阈值。
CVD_FLAT_PCT = {"5m": (1.5, 3.0), "15m": (1.2, 2.5), "1h": (0.8, 2.0), "4h": (0.5, 1.2)}

# OKX 对账容差：拿 sell+buy 对同一根 K 线的成交量(币)
OKX_TIGHT_MS = 12 * 60_000    # 收盘不足 12 分钟的棒按"必须对平"查（防没补齐的半截数）
OKX_TIGHT_TOL = 0.001         # 对平 = 相差不超过 0.1%
OKX_BAR_RATIO = (0.5, 1.02)   # 任何一根的比值出了这个范围 = 单位/口径不对（单位错了会差 100 倍以上）
OKX_MAX_MISSING_PCT = 1.0     # 整个窗口里 OKX 统计漏掉的成交量最多占 1%（它就是累计线的最大误差）
                              # 实测 1h 窗口约 0.3%：个别剧烈小时单根漏 0.5%~5%，其余分毫不差

FAPI_BASE = "https://fapi.binance.com"
SPOT_BASE = "https://api.binance.com"
OKX_BASE = "https://www.okx.com"
HTTP_TIMEOUT_S = 10
WEIGHT_GUARD = 1200           # 币安 1 分钟已用权重超过它就停手，额度先让给实盘引擎
MIN_ATTEMPT_GAP_MS = 20_000   # 同一周期两次取数的最小间隔（连续没进展时按 2 倍退避，封顶 320 秒）
OKX_RETRY_MS = 60_000         # 只差 OKX 时多久补取一次（连续不成按 2 倍退避，封顶 16 分钟）
OKX_MIN_GAP_S = 0.5           # OKX 统计接口限频 5 次/2 秒，这里压到最多 4 次/2 秒

# 解析后的棒：(open_time, open, close, volume, taker_buy)
_T, _O, _C, _V, _TB = range(5)

_DIV_KEYS = ("fut_vs_price", "spot_vs_price", "fut_vs_spot")
_INSUFFICIENT = {"state": "insufficient", "text": "数据不足"}


# ───────────────────────── 纯计算 ─────────────────────────
def normalize_tf(tf) -> str:
    """不认识的周期按 1h 处理（公开接口的参数别让它变成 500）。"""
    return tf if tf in TF_MS else DEFAULT_TF


def last_closed_open(now_ms: int, tf: str) -> int:
    """此刻"最新一根已收盘且过了结算宽限"的棒的开盘时间。"""
    step = TF_MS[tf]
    return ((int(now_ms) - SETTLE_MS[tf]) // step - 1) * step


def bar_delta(volume: float, taker_buy: float) -> float:
    """单根主动买卖差：主动买 - 主动卖 = 2*taker_buy - volume。"""
    return 2.0 * taker_buy - volume


def parse_binance_klines(raw, tf: str, now_ms: int) -> list[tuple]:
    """币安原始 klines（合约/现货同构）→ [(open_time, open, close, volume, taker_buy)] 升序。

    只留已收盘（且过了结算宽限）的棒；坏行跳过；同一 open_time 重复时取后出现的。
    """
    step, last = TF_MS[tf], last_closed_open(now_ms, tf)
    rows: dict[int, tuple] = {}
    for k in raw or ():
        try:
            t = int(k[0])
            if t > last or t % step:
                continue
            rows[t] = (t, float(k[1]), float(k[4]), float(k[5]), float(k[9]))
        except (TypeError, ValueError, IndexError):
            continue
    return [rows[t] for t in sorted(rows)]


def align(fut: Sequence[tuple], spot: Sequence[tuple]) -> tuple[list[tuple], list[tuple]]:
    """按 open_time 内连接，返回等长的两条序列。任一侧缺的棒整根丢掉。"""
    smap = {b[_T]: b for b in spot}
    f = [b for b in fut if b[_T] in smap]
    return f, [smap[b[_T]] for b in f]


def cumulative(deltas: Sequence[float]) -> list[float]:
    """累计序列，首点恒为 0：out[i] = deltas[1] + … + deltas[i]。

    含义是"自第 0 根收盘以来的净主动买卖差"。各条线共用同一个零点，才好比走向。
    """
    out: list[float] = []
    acc = 0.0
    for i, d in enumerate(deltas):
        if i:
            acc += d
        out.append(acc)
    return out


def window_label(tf: str, bars: int = DIV_BARS) -> str:
    """观察窗口的人话：近2h / 近6h / 近24h / 近4天。"""
    mins = TF_MS[tf] // 60_000 * bars
    if mins % 1440 == 0 and mins > 2880:
        return f"近{mins // 1440}天"
    if mins % 60 == 0:
        return f"近{mins // 60}h"
    return f"近{mins}分钟"


def fmt_btc(x: float) -> str:
    """带符号的 BTC 数量，尽量短：+1.2K / -340 / +3.4。"""
    ax = abs(x)
    if ax >= 1000:
        return f"{x / 1000:+.1f}K"
    if ax >= 10:
        return f"{x:+.0f}"
    return f"{x:+.1f}"


_VS_PRICE_LABEL = {
    "bear_div": "顶背离", "bull_div": "底背离",
    "confirm_up": "同向上涨", "confirm_down": "同向下跌", "flat": "价格横盘",
}


def cvd_vs_price(price_bars: Sequence[tuple], flow_bars: Sequence[tuple], tf: str,
                 name: str, weak_pct: float = 0.0, bars: int = DIV_BARS) -> dict:
    """最近 bars 根内【价格方向】对【某一路 CVD 方向】。

    判定直接调用 app.engine.orderflow.divergence（同一套语义，不另写一份）：
    bear_div / bull_div / confirm_up / confirm_down / flat，数据不足为 insufficient。
    price_bars 提供开/收盘价，flow_bars 提供成交量与主动买量，两者须已对齐。

    orderflow 的规则对 CVD 只看正负号，净量再小也会判背离/同向。state 不改（与状态卡保持一致），
    但净主动量占成交量不到 weak_pct% 时在文字里标"(弱)"，免得把几十个 BTC 的净量读成大信号。
    """
    # orderflow 的 K 线元组布局：(open_time, open, high, low, close, volume, quote_vol, taker_buy, closed)
    ks = [(p[_T], p[_O], 0.0, 0.0, p[_C], f[_V], 0.0, f[_TB], 1)
          for p, f in zip(price_bars, flow_bars)]
    d = _divergence_vs_price(ks, bars, MIN_PRICE_PCT[tf])
    label = _VS_PRICE_LABEL.get(d["state"])
    if label is None:
        return dict(_INSUFFICIENT)
    if d["state"] != "flat" and abs(d["cvd_pct_of_vol"]) < weak_pct:
        label += "(弱)"
    text = (f"{window_label(tf, bars)} {label}：价 {d['price_pct']:+.2f}%，"
            f"{name}CVD {fmt_btc(d['cvd_chg'])}")
    return {"state": d["state"], "text": text}


def flow_direction(cvd: float, volume: float, flat_pct: float) -> int:
    """净主动量的方向：+1 升 / -1 降 / 0 无明显方向（占成交量比例不到 flat_pct%）。"""
    if volume <= 0:
        return 0
    ratio = cvd / volume * 100.0
    if ratio >= flat_pct:
        return 1
    if ratio <= -flat_pct:
        return -1
    return 0


# (合约方向, 现货方向) → (state, 一句话)
_FVS = {
    (1, 1): ("both_up", "两边都有主动买盘"),
    (-1, -1): ("both_down", "两边都有主动卖盘"),
    (1, -1): ("fut_up_spot_down", "杠杆在推，现货反向"),
    (-1, 1): ("fut_down_spot_up", "合约在抛，现货有承接"),
    (1, 0): ("fut_only_up", "杠杆在推，现货没跟"),
    (-1, 0): ("fut_only_down", "抛压来自合约，现货没跟"),
    (0, 1): ("spot_only_up", "主动买盘来自现货"),
    (0, -1): ("spot_only_down", "主动卖盘来自现货"),
    (0, 0): ("flat", "两边都没有明显方向"),
}
_DIR_WORD = {1: "升", -1: "降", 0: "平"}


def fut_vs_spot(fut: Sequence[tuple], spot: Sequence[tuple], tf: str,
                bars: int = DIV_BARS) -> dict:
    """最近 bars 根内，合约 CVD 方向对现货 CVD 方向。fut/spot 须已对齐。

    state ∈ both_up / both_down / fut_up_spot_down / fut_down_spot_up /
            fut_only_up / fut_only_down / spot_only_up / spot_only_down / flat / insufficient
    """
    if len(fut) < bars + 1 or len(spot) < bars + 1:      # 与 orderflow.divergence 同一数据量门槛
        return dict(_INSUFFICIENT)
    f_flat, s_flat = CVD_FLAT_PCT[tf]
    fseg, sseg = fut[-bars:], spot[-bars:]
    fd = flow_direction(sum(bar_delta(b[_V], b[_TB]) for b in fseg),
                        sum(b[_V] for b in fseg), f_flat)
    sd = flow_direction(sum(bar_delta(b[_V], b[_TB]) for b in sseg),
                        sum(b[_V] for b in sseg), s_flat)
    state, words = _FVS[(fd, sd)]
    text = (f"{window_label(tf, bars)} 合约CVD{_DIR_WORD[fd]}、现货CVD{_DIR_WORD[sd]}："
            f"{words}")
    return {"state": state, "text": text}


def divergences(tf: str, fut: Sequence[tuple], spot: Sequence[tuple],
                bars: int = DIV_BARS) -> dict:
    """三个对比结果。价格一律用合约价（与 payload 里那条 price 线一致，图和文字说的是同一件事）。"""
    f_flat, s_flat = CVD_FLAT_PCT[tf]
    return {
        "fut_vs_price": cvd_vs_price(fut, fut, tf, "合约", f_flat, bars),
        "spot_vs_price": cvd_vs_price(fut, spot, tf, "现货", s_flat, bars),
        "fut_vs_spot": fut_vs_spot(fut, spot, tf, bars),
    }


def parse_okx_taker(rows) -> dict[int, tuple[float, float]]:
    """taker-volume-contract(unit=0) 原始行 [ts, sellVol, buyVol] → {open_time: (主动卖BTC, 主动买BTC)}。"""
    out: dict[int, tuple[float, float]] = {}
    for r in rows or ():
        try:
            out[int(r[0])] = (float(r[1]), float(r[2]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def parse_okx_candles(rows) -> dict[int, float]:
    """market/candles 原始行 [ts,o,h,l,c,vol(张),volCcy(币),volCcyQuote,confirm] → {open_time: 成交量BTC}。
    只收 confirm=="1"（已收盘）的棒。"""
    out: dict[int, float] = {}
    for r in rows or ():
        try:
            if str(r[8]) == "1":
                out[int(r[0])] = float(r[6])
        except (TypeError, ValueError, IndexError):
            continue
    return out


def okx_deltas(taker: dict[int, tuple[float, float]], candle_vol: dict[int, float],
               times: Sequence[int], tf: str, now_ms: int
               ) -> tuple[dict[int, float] | None, str | None, float | None]:
    """OKX 每根 delta(BTC) = 主动买 - 主动卖，前提是 times 里的棒能和 K 线成交量对上账。

    返回 ({open_time: delta}, None, 缺量%)；对不上则 (None, 原因, 缺量%或None) —— 整条线不出。
      - 每一根都得有统计行和 K 线；sell+buy 与成交量的比值须在 OKX_BAR_RATIO 内（防单位/口径错）
      - 收盘不足 OKX_TIGHT_MS 的棒必须对平（相差 ≤0.1%），否则是还没补齐的半截数
      - 整个窗口漏掉的成交量不超过 OKX_MAX_MISSING_PCT%（容许个别老棒的少量缺口）
    """
    if not times:
        return None, "无数据", None
    step = TF_MS[tf]
    lo, hi = OKX_BAR_RATIO
    out: dict[int, float] = {}
    vol_sum = missing = 0.0
    for t in times:
        row, vol = taker.get(t), candle_vol.get(t)
        if row is None:
            return None, f"OKX 缺主动成交统计({_hm(t)})", None
        if vol is None:
            return None, f"OKX 缺 K 线，无法对账({_hm(t)})", None
        sell, buy = row
        total = sell + buy
        if vol <= 0:
            ratio = 1.0 if total <= 1e-9 else float("inf")
        else:
            ratio = total / vol
        if not lo <= ratio <= hi:
            return None, f"OKX 主动成交量与 K 线成交量对不上({_hm(t)} 比值 {ratio:.3f})", None
        if now_ms - (t + step) < OKX_TIGHT_MS and abs(ratio - 1.0) > OKX_TIGHT_TOL:
            return None, f"OKX 最新棒尚未补齐({_hm(t)} 比值 {ratio:.3f})", None
        vol_sum += vol
        missing += max(0.0, vol - total)
        out[t] = buy - sell
    missing_pct = missing / vol_sum * 100.0 if vol_sum > 0 else 0.0
    if missing_pct > OKX_MAX_MISSING_PCT:
        return None, f"OKX 统计缺量过多(窗口内漏掉 {missing_pct:.2f}% 成交量)", missing_pct
    return out, None, missing_pct


def _hm(ts_ms: int) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ts_ms / 1000)) + "Z"


def empty_payload(tf: str = DEFAULT_TF, ts: int = 0) -> dict:
    """还没有数据时的合法空 payload（ts=0 表示从未算出过）。"""
    return {
        "tf": normalize_tf(tf), "times": [], "price": [],
        "series": {"binance_fut": [], "binance_spot": []},
        "div": {k: dict(_INSUFFICIENT) for k in _DIV_KEYS},
        "ts": int(ts),
    }


def build_payload(tf: str, fut: Sequence[tuple], spot: Sequence[tuple],
                  okx: dict[int, float] | None = None, *, now_ms: int | None = None,
                  out_bars: int = OUT_BARS) -> dict:
    """组装 /api/flow/cvd 的 payload。

    fut / spot: parse_binance_klines 的结果（已收盘、升序）。
    okx: {open_time: delta_btc}（okx_deltas 的结果）；必须覆盖输出窗口的每一根才会带上 okx_swap。
    """
    tf = normalize_tf(tf)
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    f, s = align(fut, spot)
    if not f:
        return empty_payload(tf, ts)
    div = divergences(tf, f, s)             # 背离用对齐后的全部数据判，再截输出窗口
    f, s = f[-out_bars:], s[-out_bars:]
    times = [b[_T] for b in f]
    series = {
        "binance_fut": _r2(cumulative([bar_delta(b[_V], b[_TB]) for b in f])),
        "binance_spot": _r2(cumulative([bar_delta(b[_V], b[_TB]) for b in s])),
    }
    if okx and all(t in okx for t in times):
        series["okx_swap"] = _r2(cumulative([okx[t] for t in times]))
    return {"tf": tf, "times": times, "price": [round(b[_C], 2) for b in f],
            "series": series, "div": div, "ts": ts}


def _r2(xs: Sequence[float]) -> list[float]:
    return [round(x, 2) for x in xs]


# ───────────────────────── 取数（只在后台刷新里调用，绝不在请求路径上） ─────────────────────────
class RateLimited(RuntimeError):
    """币安限流/封禁(429/418)，或我们自己的权重保护触发。retry_after 单位秒。"""

    def __init__(self, status: int, retry_after: float):
        super().__init__(f"币安限流 status={status} retry_after={retry_after:.0f}s")
        self.status = status
        self.retry_after = retry_after


_used_weight = {"value": 0, "at": 0.0}       # 最近一次合约接口响应头里的 1 分钟已用权重
_okx_pace = {"next": 0.0}                    # OKX 统计接口下一次允许发请求的时刻(monotonic)


async def _binance_get(session, base: str, path: str, params: dict):
    # 权重保护只看合约接口：实盘引擎用的是 fapi 的额度（现货 api 是另一套计数）
    is_fapi = "fapi" in base
    if is_fapi and _used_weight["value"] > WEIGHT_GUARD and time.time() - _used_weight["at"] < 60:
        raise RateLimited(0, 60)
    async with session.get(base + path, params=params,
                           timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)) as resp:
        if is_fapi:
            used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
            if used is not None:
                try:
                    _used_weight["value"], _used_weight["at"] = int(used), time.time()
                except ValueError:
                    pass
        if resp.status in (418, 429):
            try:
                ra = float(resp.headers.get("Retry-After", "60"))
            except ValueError:
                ra = 60.0
            raise RateLimited(resp.status, ra)
        if resp.status != 200:
            raise RuntimeError(f"币安 {path} HTTP {resp.status}")
        return await resp.json()


async def fetch_binance_klines(session, market: str, tf: str, limit: int = FETCH_BARS) -> list:
    """取币安原始 klines。market: "fut"(USDT 永续) | "spot"。"""
    if market == "fut":
        base, path = FAPI_BASE, "/fapi/v1/klines"
    else:
        base, path = SPOT_BASE, "/api/v3/klines"
    data = await _binance_get(session, base, path,
                              {"symbol": SYMBOL, "interval": tf, "limit": limit})
    if not isinstance(data, list):
        raise RuntimeError(f"币安 {path} 返回的不是数组")
    return data


def _has_secret_headers(session) -> bool:
    """会话默认头里带密钥(如 BinanceRest 的 X-MBX-APIKEY)就不能拿去访问第三方域名。"""
    headers = getattr(session, "headers", None) or {}
    for k in headers:
        ku = str(k).upper()
        if "APIKEY" in ku or "API-KEY" in ku or ku == "AUTHORIZATION":
            return True
    return False


async def _okx_get(session, path: str, params: dict, paced: bool = False) -> list:
    if paced:      # 先占好发车时刻再睡，并发调用也不会挤在一起
        now = time.monotonic()
        slot = max(now, _okx_pace["next"])
        _okx_pace["next"] = slot + OKX_MIN_GAP_S
        if slot > now:
            await asyncio.sleep(slot - now)
    async with session.get(OKX_BASE + path, params=params,
                           timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"OKX {path} HTTP {resp.status}")
        body = await resp.json(content_type=None)
    if not isinstance(body, dict) or str(body.get("code")) != "0":
        code = body.get("code") if isinstance(body, dict) else "?"
        msg = body.get("msg") if isinstance(body, dict) else ""
        raise RuntimeError(f"OKX {path} code={code} {msg}"[:160])
    data = body.get("data")
    return data if isinstance(data, list) else []


async def fetch_okx(session, tf: str) -> tuple[list, list]:
    """OKX BTC-USDT-SWAP：两页主动成交统计(各 100 行，新→旧，单位=币) + 一次 K 线(对账用)。
    返回 (taker_rows, candle_rows)，都是交易所原始行。"""
    if _has_secret_headers(session):
        raise RuntimeError("注入的会话带密钥头，拒绝向 OKX 发请求（请换成不带密钥的普通会话）")
    period = _OKX_PERIOD[tf]
    path = "/api/v5/rubik/stat/taker-volume-contract"
    q = {"instId": OKX_INST, "period": period, "unit": "0", "limit": "100"}
    page1 = await _okx_get(session, path, q, paced=True)
    page2: list = []
    if page1:
        page2 = await _okx_get(session, path, {**q, "end": str(page1[-1][0])}, paced=True)
    candles = await _okx_get(session, "/api/v5/market/candles",
                             {"instId": OKX_INST, "bar": period, "limit": "300"})
    return list(page1) + list(page2), candles


# ───────────────────────── 缓存：后台 refresh() 写，请求路径只读 ─────────────────────────
def _new_state() -> dict:
    return {
        "payload": None, "json": None,
        "fut": [], "spot": [],              # 最近一次取到的已收盘棒（只差 OKX 时免得再打币安）
        "last_bar": -1,                     # 缓存 payload 里最新一根的 open_time
        "okx_ok": False, "okx_note": None, "okx_attempt_ms": 0, "okx_streak": 0,
        "okx_missing_pct": None,            # 最近一次对账：窗口内 OKX 统计漏掉的成交量占比
        "attempt_ms": 0, "ok_ms": 0, "streak": 0, "last_error": None, "busy": False,
    }


_state: dict[str, dict] = {tf: _new_state() for tf in SUPPORTED_TFS}
_backoff = {"until_ms": 0}                  # 币安限流后的静默截止时刻（所有周期共用）


def _dumps(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


async def _okx_for(session, tf: str, st: dict, times: Sequence[int], now: int
                   ) -> dict[int, float] | None:
    """取 OKX 并对账，把结果记进 st。永不抛异常：取不到/对不上返回 None（原因在 st["okx_note"]）。"""
    st["okx_attempt_ms"] = now
    try:
        taker_rows, candle_rows = await fetch_okx(session, tf)
        okx, note, missing = okx_deltas(parse_okx_taker(taker_rows),
                                        parse_okx_candles(candle_rows), times, tf, now)
    except Exception as e:                  # 网络抖动/限频等：这一轮不带 OKX，稍后补取
        okx, note, missing = None, f"{type(e).__name__}: {e}"[:200], None
    st["okx_note"], st["okx_missing_pct"] = note, missing
    st["okx_streak"] = 0 if okx is not None else st["okx_streak"] + 1
    return okx


def _publish(tf: str, st: dict, okx: dict[int, float] | None, now: int) -> None:
    payload = build_payload(tf, st["fut"], st["spot"], okx, now_ms=now)
    st["payload"], st["json"] = payload, _dumps(payload)
    st["last_bar"] = payload["times"][-1] if payload["times"] else -1
    st["okx_ok"] = "okx_swap" in payload["series"]
    st["ok_ms"] = now


async def _refresh_one(session, tf: str, now: int, force: bool) -> bool:
    st = _state[tf]
    if st["busy"]:                          # 上一轮还没跑完（网络慢），这一轮让路
        return False
    target = last_closed_open(now, tf)
    need_binance = force or st["payload"] is None or st["last_bar"] < target
    want_okx = tf in OKX_TFS
    if not need_binance and (not want_okx or st["okx_ok"]):
        return True                         # 缓存已含最新收盘棒：不发任何请求
    if need_binance and now < _backoff["until_ms"]:
        return False                        # 被币安限流后的静默期：force 也不许破例
    if not force:
        if need_binance:
            if now - st["attempt_ms"] < MIN_ATTEMPT_GAP_MS * 2 ** min(st["streak"], 4):
                return False
        elif now - st["okx_attempt_ms"] < OKX_RETRY_MS * 2 ** min(max(st["okx_streak"] - 1, 0), 4):
            return True                     # 币安两路是新的，只是 OKX 还没到补取时间

    st["busy"] = True
    try:
        if not need_binance:                # 只补 OKX：用上次的币安数据，不再打币安
            okx = await _okx_for(session, tf, st, st["payload"]["times"], now)
            if okx is not None:
                _publish(tf, st, okx, now)
            return True

        st["attempt_ms"] = now
        res = await asyncio.gather(fetch_binance_klines(session, "fut", tf),
                                   fetch_binance_klines(session, "spot", tf),
                                   return_exceptions=True)
        err = next((r for r in res if isinstance(r, BaseException)), None)
        if err is not None:
            if not isinstance(err, Exception):          # CancelledError 等照常上抛
                raise err
            if isinstance(err, RateLimited):
                wait_s = max(60.0, err.retry_after)
                _backoff["until_ms"] = now + int(wait_s * 1000)
                log.warning("CVD 对比取数被币安限流，静默 %.0fs", wait_s)
            st["last_error"] = f"{type(err).__name__}: {err}"[:200]
            st["streak"] += 1
            log.warning("CVD 对比(%s)取数失败: %s", tf, st["last_error"])
            return False

        fut = parse_binance_klines(res[0], tf, now)
        spot = parse_binance_klines(res[1], tf, now)
        f, _s = align(fut, spot)
        if not f:
            st["last_error"] = "币安合约/现货 K 线对不齐或为空"
            st["streak"] += 1
            return False
        st["fut"], st["spot"] = fut, spot
        okx = None
        if want_okx:
            st["okx_streak"] = 0            # 新的一根棒，OKX 的补取退避重新计
            okx = await _okx_for(session, tf, st, [b[_T] for b in f[-OUT_BARS:]], now)
        _publish(tf, st, okx, now)
        st["last_error"] = None
        # 交易所还没给出目标棒（维护/延迟）算"没进展"，下次间隔加倍，免得空转
        st["streak"] = 0 if st["last_bar"] >= target else st["streak"] + 1
        return True
    finally:
        st["busy"] = False


async def refresh(session, tf=None, *, now_ms: int | None = None, force: bool = False) -> bool:
    """后台刷新。tf=None 依次刷新全部周期；也可以只传一个周期。

    集成方每 30~60 秒调一次即可：缓存已含最新收盘棒时直接返回 True、不发请求。
    永不因网络/数据问题抛异常：失败时保留旧缓存、把原因记到 status()，返回 False。
    被币安限流(429/418)后按 Retry-After（至少 60 秒）静默，期间不再向币安发请求（force 也不行）。
    OKX 取不到/对不上账不算失败：payload 照出，只是不带 okx_swap，原因见 status()。
    """
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    tfs = SUPPORTED_TFS if tf is None else (normalize_tf(tf),)
    ok = True
    for one in tfs:                         # 顺序执行：对交易所不并发
        try:
            ok = await _refresh_one(session, one, now, force) and ok
        except Exception as e:              # 兜底：计算/解析里的意外也不许打断后台循环
            _state[one]["last_error"] = f"{type(e).__name__}: {e}"[:200]
            log.exception("CVD 对比(%s)刷新异常", one)
            ok = False
    return ok


def get_cached(tf=DEFAULT_TF) -> dict:
    """同步读取最近一次算好的 payload；还没算出来就返回合法的空 payload。绝不触网。
    tf 不在 {5m,15m,1h,4h} 时按 1h 处理。返回的是缓存里的同一个对象，调用方只读、别改。"""
    tf = normalize_tf(tf)
    return _state[tf]["payload"] or empty_payload(tf)


def get_cached_json(tf=DEFAULT_TF) -> bytes:
    """同上，但直接给序列化好的 JSON 字节（UTF-8），省得每个请求重新序列化。"""
    tf = normalize_tf(tf)
    return _state[tf]["json"] or _dumps(empty_payload(tf))


def last_updated_ms() -> int:
    """最近一次成功重算的时间(ms)，从未成功为 0。给 /api/flow/health 的 caches.cvd 用。"""
    return max(int(st["ok_ms"]) for st in _state.values())


def status() -> dict:
    """刷新状态（健康检查用）。"""
    tfs = {}
    for tf, st in _state.items():
        p = st["payload"]
        tfs[tf] = {
            "ts": p["ts"] if p else 0, "bars": len(p["times"]) if p else 0,
            "last_bar": st["last_bar"] if st["last_bar"] >= 0 else None,
            "okx": st["okx_ok"], "okx_note": st["okx_note"],
            "okx_missing_pct": (round(st["okx_missing_pct"], 3)
                                if st["okx_missing_pct"] is not None else None),
            "last_attempt_ms": st["attempt_ms"], "last_error": st["last_error"],
            "bytes": len(st["json"]) if st["json"] else 0,
        }
    return {"tfs": tfs, "backoff_until_ms": _backoff["until_ms"],
            "binance_used_weight_1m": _used_weight["value"]}


def _reset_cache() -> None:
    """清空模块内状态（单测用）。"""
    for tf in SUPPORTED_TFS:
        _state[tf] = _new_state()
    _backoff["until_ms"] = 0
    _used_weight.update({"value": 0, "at": 0.0})
    _okx_pace["next"] = 0.0


# ───────────────────────── 冒烟：python -m app.flow.cvd ─────────────────────────
async def _smoke(session=None, tfs: Sequence[str] = ("1h", "15m")) -> None:
    """拉真实数据跑一遍，打印长度/末值/三个对比结果/体积。只读公开行情，不启动应用。"""
    own = session is None
    s = session or aiohttp.ClientSession()
    try:
        for tf in tfs:
            t0 = time.perf_counter()
            ok = await refresh(s, tf, force=True)
            p, st = get_cached(tf), status()["tfs"][tf]
            print(f"\n[{tf}] ok={ok} 用时={time.perf_counter() - t0:.2f}s 根数={len(p['times'])} "
                  f"字节={st['bytes']} 错误={st['last_error']}")
            if not p["times"]:
                continue
            print(f"  时间 {_hm(p['times'][0])} → {_hm(p['times'][-1])}  价格末值 {p['price'][-1]}")
            for k, v in p["series"].items():
                print(f"  {k:13s} 长度={len(v)} 首={v[0]} 末={v[-1]}")
            if "okx_swap" not in p["series"]:
                why = st["okx_note"] or ("该周期不带 OKX" if tf not in OKX_TFS else "未知")
                print(f"  okx_swap      未提供：{why}")
            else:
                print(f"  okx_swap      对账通过，窗口内 OKX 统计漏掉的成交量 {st['okx_missing_pct']}%")
            for k in _DIV_KEYS:
                print(f"  {k:13s} {p['div'][k]['state']:17s} {p['div'][k]['text']}")
        print(f"\n币安合约 1 分钟已用权重: {status()['binance_used_weight_1m']}")
    finally:
        if own:
            await s.close()


if __name__ == "__main__":
    asyncio.run(_smoke())
