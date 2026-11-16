"""btcbreak_pump 形态审核出图(CLAUDE.md 铁律七: 先出图人工审, 过了才跑期望)。

把 scripts/btcbreak_run.py 导出的每一轮(episode)画成一张图, 标出规则里的每个条件:
  上半: BTCUSDT 5m —— 48h 参考低点(数值 + 何时创出)、破位K / 再次破位K、量柱 + 前20根均量 + 2倍门槛、武装期;
  下半: PUMPUSDT 5m —— 底分型 左/中/右 三根合并K、分型低点、确认K、停顿K(=信号K)、入场/止损/止盈、破位时刻竖线;
        本轮里更早出现但被放弃的底分型淡色标出(「分型但无停顿」/「低点早于破位」);
  右列: 两个放大图(破位K附近 / 分型与停顿附近), 逐根核对用。

只画形态、只列实测值; 【不结算、不标输赢、不算任何统计】。入场之后只按固定跨度多画约 3 小时。
想在审形态时完全看不到入场之后的走势(免得被结果带着走): 加 --after-bars 0, 图就停在信号K(到期图停在到期那根)。
分型/合并/停顿全部直接调用 app.engine.chan / chan_bi(与检测器同一套函数), 不另写一套规则;
并顺手把图上要标的每个数与 instances.json 逐项对账, 对不上的写进 manifest 的 checks 并打印出来。

用法:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/btcbreak_chart.py \
      --instances artifacts/btcbreak_audit/instances.json --outdir artifacts/btcbreak_audit/png
  可选: --only 7        只画 #7(也可 "1-20,45,E1"; 入场图按时间顺序编号 #1.., 到期图编号 E1..)
        --limit 12      只画前 12 张入场图(到期图照常)
        --jobs 4        多进程出图
        --after-bars 0  信号K之后不画任何K线(「盲审」版; 默认 36 根 ≈ 3 小时)
输出: <outdir>/entry_###_<时间>.png、expired_E##_<时间>.png、manifest.json(供 btcbreak_gallery.py 用)。
"""
import argparse
import bisect
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.gridspec import GridSpec, GridSpecFromSubplotSpec  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from app.engine.chan import find_fractals, merge_klines  # noqa: E402
from app.engine.chan_bi import stall_after  # noqa: E402
from app.engine.strat_btcbreak import PRE_BARS, tf_to_ms  # noqa: E402

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "PingFang SC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

BKK = timezone(timedelta(hours=7))
BEFORE_BARS = 72      # 首次破位K之前约 6 小时
AFTER_BARS = 36       # 信号K(或到期)之后约 3 小时, 固定跨度
FIGSIZE = (15, 10)
DPI = 110

BG = "#0e1116"
FG = "#d6dae0"
DIM = "#8b949e"
GRID = "#1a2029"
SPINE = "#30363d"
UP = "#3fb950"
DN = "#f85149"
C_REF = "#f0a500"      # 48h 参考低点
C_TRIG = "#ffd33d"     # 破位K
C_ARM = "#58a6ff"      # 武装期
C_FX = "#d2a8ff"       # 分型
C_STALL = "#79c0ff"    # 右K高 / 停顿
C_ENTRY = "#ffd700"
C_SL = "#ff7b72"
C_TP = "#56d364"
C_OLD = "#9aa4b1"      # 被放弃的分型
SHORT_REASON = {"分型但无停顿": "无停顿", "低点早于破位": "低点早于破位", "分型未等满(武装期结束)": "未等满"}


# ---------------------------------------------------------------- 小工具
def bkk(ms: int, fmt: str = "%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ms / 1000, BKK).strftime(fmt)


def px(v: float) -> str:
    """价格显示: 大数 1 位小数, 小数 6 位(PUMP 的最小价位)。"""
    a = abs(v)
    if a >= 1000:
        return f"{v:.1f}"
    if a >= 1:
        return f"{v:.4f}"
    return f"{v:.6f}"


def vol_s(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e4:
        return f"{v / 1e3:.0f}K"
    return f"{v:.0f}"


class Series:
    """一条已收盘K线序列(dict 行, 升序), 按 open_time 取段。"""

    def __init__(self, rows: list):
        self.rows = rows
        self.t = [int(r["open_time"]) for r in rows]

    def span(self, t_from: int, t_to: int) -> list:
        """open_time ∈ [t_from, t_to] 的K。"""
        return self.rows[bisect.bisect_left(self.t, t_from):bisect.bisect_right(self.t, t_to)]

    def idx(self, t: int):
        i = bisect.bisect_left(self.t, t)
        return i if i < len(self.t) and self.t[i] == t else None

    def prev_mean_vol(self, t: int, n: int):
        """t 这根K之前 n 根的均量(不含本K); 不足返回 None。"""
        i = self.idx(t)
        if i is None or i < n:
            return None
        return sum(float(r["volume"]) for r in self.rows[i - n:i]) / n


def load_rows(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_cache(meta_entry: dict) -> str:
    p = meta_entry.get("path") or ""
    if os.path.exists(p):
        return p
    alt = os.path.join(ROOT, ".btcache", os.path.basename(p))
    if os.path.exists(alt):
        return alt
    raise SystemExit(f"找不到K线缓存: {p} (先运行 scripts/btcbreak_run.py)")


# ---------------------------------------------------------------- 结构重算(只调用 chan 助手)
def _mk(win, mk) -> dict:
    """合并K → {times: 覆盖的原始K open_time, high, low}。"""
    return {"times": [int(win[j]["open_time"]) for j in mk.src_idx],
            "high": float(mk.high), "low": float(mk.low)}


def analyze_target(ep: dict, tgt: Series, params: dict, tf: int) -> dict:
    """在检测器用的同一段K上(首次破位K前 PRE_BARS 根 → 信号K/到期前最后一根)重跑
    merge_klines / find_fractals / stall_after, 取出画图要的合并K跨度, 并与 instances 对账。"""
    t0 = int(ep["trigger"]["time"])
    entry = ep.get("entry")
    gap = int(params.get("stall_max_gap", 3))
    need_stall = bool(params.get("require_stall", True))
    end_t = int(entry["signal_time"]) if entry else int(ep["expires_at"]) - tf
    win = tgt.span(t0 - PRE_BARS * tf, end_t)
    out = {"chosen": None, "discarded": [], "checks": [], "sl_touched_before_signal": False}
    if len(win) < 3:
        out["checks"].append("目标品种K线不足, 无法重算分型")
        return out
    last = len(win) - 1
    merged = merge_klines(win)
    bottoms = [f for f in find_fractals(win, merged) if f.kind == "bottom"]

    def info(fx):
        rk = fx.mid_merged_idx + 1
        st = stall_after(win, merged, fx, gap)
        return {
            "fx": fx, "rk": rk,
            "low": float(fx.extreme_price),
            "low_time": int(win[fx.extreme_src_idx]["open_time"]),
            "L": _mk(win, merged[rk - 2]), "M": _mk(win, merged[rk - 1]), "R": _mk(win, merged[rk]),
            "confirm_time": int(win[fx.confirm_src_idx]["open_time"]),
            "stall_idx": st,
            "stall_time": int(win[st]["open_time"]) if st is not None else None,
            "r_is_last": rk == len(merged) - 1,
        }

    infos = [info(f) for f in bottoms]
    chosen = None
    if entry:
        for it in infos:
            if it["M"]["times"][0] == int(entry["fx_mid_time"]) and it["low_time"] == int(entry["fx_low_time"]):
                chosen = it
                break
        if chosen is None:
            out["checks"].append("用 chan 助手重算找不到 instances 里的这个分型")
    out["chosen"] = chosen

    # 本轮里更早的底分型为什么没被采用
    for it in infos:
        if chosen is not None and it["fx"].mid_merged_idx >= chosen["fx"].mid_merged_idx:
            continue
        if it["confirm_time"] < t0:
            continue                                   # 破位之前就已走完的分型, 与本轮无关
        if it["low_time"] < t0:
            it["reason"] = "低点早于破位"
            # 并列低点: 同一个最低价在破位K当根/之后又出现了一次(检测器取最早的一根 → 判为早于破位)
            it["tie_after"] = [t for t in it["M"]["times"]
                               if t >= t0 and float(tgt.rows[tgt.idx(t)]["low"]) == it["low"]]
        elif not need_stall:
            it["reason"] = "更早的分型未被采用"
            out["checks"].append(f"不要停顿口径下, {bkk(it['low_time'])} 的更早分型没有被采用")
        elif it["stall_idx"] is None:
            waited = last - it["fx"].confirm_src_idx
            it["reason"] = "分型但无停顿" if waited >= gap or chosen is not None else "分型未等满(武装期结束)"
        else:
            it["reason"] = "有停顿却未入场?"
            out["checks"].append(f"{bkk(it['low_time'])} 的更早分型在 {bkk(it['stall_time'])} 已有停顿K, 却不是入场")
        out["discarded"].append(it)

    if chosen is not None:
        e = entry
        ck = out["checks"]
        sig_bar = win[last]

        def same(name, a, b, tol=1e-9):
            if a is None or b is None:
                if a != b:
                    ck.append(f"{name}: 重算 {a} ≠ instances {b}")
            elif abs(float(a) - float(b)) > tol * max(1.0, abs(float(b))):
                ck.append(f"{name}: 重算 {a} ≠ instances {b}")

        same("fx_low", chosen["low"], e["fx_low"])
        same("fx_left_time", chosen["L"]["times"][0], e["fx_left_time"])
        same("fx_right_time", chosen["R"]["times"][0], e["fx_right_time"])
        same("fx_right_high", chosen["R"]["high"], e.get("fx_right_high", chosen["R"]["high"]))
        same("entry=信号K收盘", float(sig_bar["close"]), e["entry"])
        sl = chosen["low"] * (1.0 - float(params.get("sl_buffer_pct", 0.3)) / 100.0)
        same("sl", sl, e["sl"], 1e-8)
        same("tp", float(e["entry"]) + float(params.get("rr", 2.0)) * (float(e["entry"]) - sl), e["tp"], 1e-8)
        if need_stall:
            same("fx_confirm_time", chosen["confirm_time"], e["fx_confirm_time"])
            if chosen["stall_idx"] != last:
                ck.append("stall_after 给出的停顿K不是 instances 的信号K")
            same("stall_time", chosen["stall_time"], e.get("stall_time"))
            if not float(sig_bar["close"]) > chosen["R"]["high"]:
                ck.append("信号K收盘没有高于右侧合并K最高价")
            n = last - chosen["fx"].confirm_src_idx
            if not 1 <= n <= gap:
                ck.append(f"停顿K在确认K之后第 {n} 根, 超出 {gap} 根")
        if chosen["low_time"] < t0:
            ck.append("分型低点早于首次破位K")
        if not (chosen["M"]["low"] < chosen["L"]["low"] and chosen["M"]["low"] < chosen["R"]["low"]
                and chosen["M"]["high"] < chosen["L"]["high"] and chosen["M"]["high"] < chosen["R"]["high"]):
            ck.append("左/中/右 不构成底分型")
        trig_before = [t0] + [int(x) for x in ep.get("retriggers", []) if int(x) < int(e["signal_time"])]
        ttl = int(round(float(params.get("ttl_hours", 4.0)) * 3_600_000)) // tf * tf
        if int(e["signal_time"]) + tf > max(trig_before) + tf + ttl:
            ck.append("信号K收盘晚于有效期")
        li = win.index(next(b for b in win if int(b["open_time"]) == chosen["low_time"]))
        after = win[li + 1:]
        out["sl_touched_before_signal"] = any(float(b["low"]) <= float(e["sl"]) for b in after)
        intact = all(float(b["low"]) >= chosen["low"] for b in after)
        if "fx_low_intact" in e and bool(e["fx_low_intact"]) != intact:
            ck.append("fx_low_intact 与重算不一致")
        out["stall_n"] = last - chosen["fx"].confirm_src_idx
    return out


def check_trigger(trig: dict, src: Series, params: dict, tf: int) -> list:
    """把一次触发的两个条件按规则文字逐根重算一遍, 与 instances 对账(只用于核对图上标的数)。"""
    ck = []
    t = int(trig["time"])
    i = src.idx(t)
    if i is None:
        return [f"{bkk(t)} 触发K不在K线缓存里"]
    bar = src.rows[i]
    lookback = int(round(float(params.get("lookback_hours", 48.0)) * 3_600_000))
    window = src.span(t - lookback, t - tf)
    if not window:
        return [f"{bkk(t)} 两日窗口为空"]
    lo = min(float(b["low"]) for b in window)
    lo_t = max(int(b["open_time"]) for b in window if float(b["low"]) == lo)
    if abs(lo - float(trig["ref_low"])) > 1e-9 * lo:
        ck.append(f"{bkk(t)} 48h低 重算 {lo} ≠ {trig['ref_low']}")
    if lo_t != int(trig["ref_low_time"]):
        ck.append(f"{bkk(t)} 48h低出现时间 重算 {bkk(lo_t)} ≠ {bkk(int(trig['ref_low_time']))}")
    if not float(bar["close"]) < lo:
        ck.append(f"{bkk(t)} 收盘 {bar['close']} 没有低于 48h低 {lo}")
    n = int(params.get("vol_ma", 20))
    vma = src.prev_mean_vol(t, n)
    if vma is None or abs(vma - float(trig["vol_ma"])) > 1e-6 * max(1.0, vma):
        ck.append(f"{bkk(t)} 均量 重算 {vma} ≠ {trig['vol_ma']}")
    elif float(bar["volume"]) < float(params.get("vol_mult", 2.0)) * vma:
        ck.append(f"{bkk(t)} 量 {bar['volume']} 不到门槛")
    return ck


# ---------------------------------------------------------------- 每轮的实测值(图注 + 画廊文字)
def build_record(n_label: str, kind: str, ep: dict, retrig: list, prev_signal_time,
                 btc: Series, tgt: Series, params: dict, tf: int) -> dict:
    trig = ep["trigger"]
    t0 = int(trig["time"])
    e = ep.get("entry")
    an = analyze_target(ep, tgt, params, tf)
    checks = list(an["checks"])
    for tr in [trig] + retrig:
        checks.extend(check_trigger(tr, btc, params, tf))
    mult = float(params.get("vol_mult", 2.0))
    below = (float(trig["ref_low"]) - float(trig["close"])) / float(trig["ref_low"]) * 100.0
    facts = [
        ("破位K", f"{bkk(t0, '%Y-%m-%d %H:%M')} (UTC+7)"),
        ("BTC 收盘", f"{px(trig['close'])} < 48h低 {px(trig['ref_low'])}, 低 {below:.3f}%"),
        ("48h低创出", bkk(int(trig["ref_low_time"]))
         + f" (破位前 {(t0 - int(trig['ref_low_time'])) / 3_600_000:.1f} 小时)"),
        ("放量", f"{vol_s(trig['volume'])} = {float(trig['vol_ratio']):.2f}× 前{params.get('vol_ma', 20)}根均量 "
                 f"{vol_s(trig['vol_ma'])} (门槛 {mult:g}×)"),
    ]
    flags = []
    if retrig:
        facts.append(("武装期内再破位", f"{len(retrig)} 次: " + " / ".join(
            f"{bkk(int(r['time']), '%H:%M')} {float(r['vol_ratio']):.2f}×" for r in retrig)))
        flags.append(f"再破位{len(retrig)}次")
    if prev_signal_time is not None and 0 <= t0 - int(prev_signal_time) < 4 * 3_600_000:
        flags.append("上一轮入场后4小时内")
        facts.append(("距上一轮信号K", f"{(t0 - int(prev_signal_time)) // tf} 根"))
    if e:
        ch = an["chosen"]
        k_low = (int(e["fx_low_time"]) - t0) // tf
        facts += [
            ("分型低点", f"{px(e['fx_low'])} @ {bkk(int(e['fx_low_time']), '%H:%M')} (破位K后第 {k_low} 根"
                         + (", 即破位K当根)" if k_low == 0 else ")")),
        ]
        if ch:
            def rng(m):
                a, b = m["times"][0], m["times"][-1]
                s = bkk(a, "%H:%M") if a == b else f"{bkk(a, '%H:%M')}~{bkk(b, '%H:%M')}"
                return f"{s}({len(m['times'])}根)" if a != b else s
            facts.append(("左/中/右", f"{rng(ch['L'])} / {rng(ch['M'])} / {rng(ch['R'])}"))
        facts.append(("确认K", bkk(int(e["fx_confirm_time"]), "%H:%M") + " (右侧合并K的最后一根)"))
        if e.get("stall_time") is not None:
            facts.append(("停顿K=信号K", f"{bkk(int(e['stall_time']), '%H:%M')} (确认K后第 {an.get('stall_n', '?')} 根), "
                                         f"收 {px(e['entry'])} > 右K高 {px(e.get('fx_right_high', 0))}"))
        else:
            facts.append(("信号K", bkk(int(e["signal_time"]), "%H:%M") + " (不要停顿口径: 形态首次成立的那根)"))
        facts += [
            ("破位K→信号K", f"{(int(e['signal_time']) - t0) // tf} 根"),
            ("入场 / 止损 / 止盈", f"{px(e['entry'])} / {px(e['sl'])} / {px(e['tp'])}"),
            ("风险(入场→止损)", f"{float(e['risk_pct']):.2f}%  盈亏比 {float(e['rr']):g}"),
        ]
        if k_low == 0:
            flags.append("低点在破位K当根")
        if e.get("fx_low_intact") is False:
            flags.append("停顿前分型低点已被跌破")
        if an["sl_touched_before_signal"]:
            flags.append("信号前已触及止损价")
        if float(e["risk_pct"]) > 5.0:
            flags.append("风险>5%")
        if any(int(r["time"]) == int(e["signal_time"]) for r in retrig):
            flags.append("同K再破位+入场")
    else:
        facts.append(("结果", f"到期未入场, 有效期止 {bkk(int(ep['expires_at']), '%H:%M')}"))
    if an["discarded"]:
        by = {}
        for it in an["discarded"]:
            by.setdefault(it["reason"], []).append(bkk(it["low_time"], "%H:%M"))
        facts.append(("本轮被放弃的分型", "; ".join(f"{k}: {'、'.join(v)}" for k, v in by.items())))
        n_ns = sum(1 for it in an["discarded"] if it["reason"] == "分型但无停顿")
        if n_ns:
            flags.append(f"放弃{n_ns}个无停顿分型")
        for it in an["discarded"]:
            if it.get("tie_after"):
                facts.append(("并列低点", f"{bkk(it['low_time'], '%H:%M')} 的分型: 最低价 {px(it['low'])} 在破位前"
                              f"({bkk(it['low_time'], '%H:%M')})和破位K当根/之后({bkk(it['tie_after'][0], '%H:%M')})"
                              f"各出现一次, 检测器取最早一根 → 判为低点早于破位"
                              + (f"; 该分型在 {bkk(it['stall_time'], '%H:%M')} 有停顿K" if it["stall_time"] else "")))
                if "并列低点判为早于破位" not in flags:
                    flags.append("并列低点判为早于破位")
    if checks:
        flags.append("自检不一致")
    stamp = bkk(t0, "%Y%m%d_%H%M")
    return {
        "label": n_label, "kind": kind, "episode_id": int(ep.get("episode_id", t0)),
        "trigger_time": t0, "trigger_local": bkk(t0, "%Y-%m-%d %H:%M"),
        "file": (f"entry_{n_label.lstrip('#').zfill(3)}_{stamp}.png" if kind == "entry"
                 else f"expired_{n_label}_{stamp}.png"),
        "vol_ratio": float(trig["vol_ratio"]),
        "risk_pct": float(e["risk_pct"]) if e else None,
        "facts": facts, "flags": flags, "checks": checks,
        "_an": an, "_ep": ep, "_retrig": retrig,
    }


# ---------------------------------------------------------------- 画图
def style_ax(ax):
    ax.set_facecolor(BG)
    ax.tick_params(colors=DIM, labelsize=11, length=3)
    for sp in ax.spines.values():
        sp.set_color(SPINE)
    ax.grid(True, color=GRID, lw=0.6)
    ax.set_axisbelow(True)


def tag(ax, x, y, s, color, ha="left", va="center", fs=12, alpha=0.78, **kw):
    return ax.text(x, y, s, color=color, fontsize=fs, ha=ha, va=va, zorder=12,
                   bbox=dict(boxstyle="round,pad=0.16", fc=BG, ec="none", alpha=alpha), **kw)


def candles(ax, rows, xof, w=0.66, lw=1.0):
    if not rows:
        return
    xs = np.array([xof(int(r["open_time"])) for r in rows], dtype=float)
    o = np.array([float(r["open"]) for r in rows])
    h = np.array([float(r["high"]) for r in rows])
    l = np.array([float(r["low"]) for r in rows])
    c = np.array([float(r["close"]) for r in rows])
    minh = max((h.max() - l.min()) * 0.0018, 1e-12)
    up = c >= o
    for mask, col in ((up, UP), (~up, DN)):
        if not mask.any():
            continue
        ax.vlines(xs[mask], l[mask], h[mask], color=col, lw=lw, zorder=3)
        ax.bar(xs[mask], np.maximum(np.abs(c - o)[mask], minh), bottom=np.minimum(o, c)[mask],
               width=w, color=col, linewidth=0, zorder=4)


def vol_bars(ax, rows, xof, hot=(), w=0.66):
    if not rows:
        return
    xs = np.array([xof(int(r["open_time"])) for r in rows], dtype=float)
    v = np.array([float(r["volume"]) for r in rows])
    up = np.array([float(r["close"]) >= float(r["open"]) for r in rows])
    is_hot = np.array([int(r["open_time"]) in hot for r in rows])
    ax.bar(xs[up & ~is_hot], v[up & ~is_hot], width=w, color="#2a6b3a", linewidth=0, zorder=3)
    ax.bar(xs[~up & ~is_hot], v[~up & ~is_hot], width=w, color="#8a3531", linewidth=0, zorder=3)
    if is_hot.any():
        ax.bar(xs[is_hot], v[is_hot], width=w, color=C_TRIG, linewidth=0, zorder=4)
    ax.set_ylim(0, v.max() * 1.22)
    ax.set_yticks([])


def vol_mean_lines(ax, src: Series, rows, xof, n, mult):
    """每根K位置画「它之前 n 根的均量」与 mult 倍门槛(与检测器同口径: 不含本K)。"""
    xs, ms = [], []
    for r in rows:
        m = src.prev_mean_vol(int(r["open_time"]), n)
        if m is not None:
            xs.append(xof(int(r["open_time"])))
            ms.append(m)
    if not xs:
        return
    ms = np.array(ms)
    ax.step(xs, ms, where="mid", color="#e6edf3", lw=1.3, zorder=5)
    ax.step(xs, ms * mult, where="mid", color=C_REF, lw=1.2, ls=(0, (4, 2)), zorder=5)


def time_ticks(ax, t_a, n_bars, tf, every, with_date=True, fs=11):
    """整点刻度(曼谷时间)。every = 每隔多少根一个刻度。"""
    ticks, labels = [], []
    step_ms = every * tf
    first = -(-(t_a + 7 * 3_600_000) // step_ms) * step_ms - 7 * 3_600_000   # 曼谷时间对齐
    t = first
    prev_day = None
    while t <= t_a + (n_bars - 1) * tf:
        d = bkk(t, "%m-%d")
        lab = bkk(t, "%H:%M")
        if with_date and d != prev_day:
            lab += "\n" + d
            prev_day = d
        ticks.append((t - t_a) / tf)
        labels.append(lab)
        t += step_ms
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, fontsize=fs)


def merged_box(ax, m, xof, color, label=None, fs=14, alpha=0.20, lw=1.3, y_label=None):
    x0, x1 = xof(m["times"][0]) - 0.46, xof(m["times"][-1]) + 0.46
    ax.add_patch(Rectangle((x0, m["low"]), x1 - x0, m["high"] - m["low"], facecolor=color,
                           alpha=alpha, edgecolor="none", zorder=2))
    ax.add_patch(Rectangle((x0, m["low"]), x1 - x0, m["high"] - m["low"], facecolor="none",
                           edgecolor=color, lw=lw, zorder=6))
    if label:
        ax.text((x0 + x1) / 2, y_label if y_label is not None else m["high"], label, color=color,
                fontsize=fs, fontweight="bold", ha="center", va="bottom", zorder=12)


def render(rec: dict, btc: Series, tgt: Series, params: dict, tf: int, out: str) -> None:
    ep, an, retrig = rec["_ep"], rec["_an"], rec["_retrig"]
    trig = ep["trigger"]
    e = ep.get("entry")
    ch = an["chosen"]
    t0 = int(trig["time"])
    n_ma = int(params.get("vol_ma", 20))
    mult = float(params.get("vol_mult", 2.0))
    need_stall = bool(params.get("require_stall", True))
    end_ref = int(e["signal_time"]) if e else int(ep["expires_at"]) - tf
    t_a = t0 - BEFORE_BARS * tf
    after = int(_G.get("after_bars", AFTER_BARS))
    t_b = end_ref + after * tf
    n_bars = (t_b - t_a) // tf + 1
    xof = lambda t: (t - t_a) / tf                                    # noqa: E731
    b_rows = btc.span(t_a, t_b)
    g_rows = tgt.span(t_a, t_b)
    trig_times = [t0] + [int(r["time"]) for r in retrig]
    pad = n_bars * 0.20                                               # 右侧留白放价位标签
    xlim = (-1.2, n_bars - 0.3 + pad)
    x_lab = n_bars + 0.4

    fig = plt.figure(figsize=FIGSIZE)
    fig.patch.set_facecolor(BG)
    outer = GridSpec(2, 2, figure=fig, width_ratios=[1.85, 1], height_ratios=[1, 1.12],
                     left=0.052, right=0.992, top=0.862, bottom=0.070, wspace=0.125, hspace=0.215)
    g_b = GridSpecFromSubplotSpec(2, 1, subplot_spec=outer[0, 0], height_ratios=[3.1, 1], hspace=0.03)
    g_p = GridSpecFromSubplotSpec(2, 1, subplot_spec=outer[1, 0], height_ratios=[3.6, 1], hspace=0.03)
    g_bz = GridSpecFromSubplotSpec(2, 1, subplot_spec=outer[0, 1], height_ratios=[3.1, 1], hspace=0.03)
    g_pz = GridSpecFromSubplotSpec(2, 1, subplot_spec=outer[1, 1], height_ratios=[3.6, 1], hspace=0.03)
    ax_b = fig.add_subplot(g_b[0])
    ax_bv = fig.add_subplot(g_b[1], sharex=ax_b)
    ax_p = fig.add_subplot(g_p[0], sharex=ax_b)
    ax_pv = fig.add_subplot(g_p[1], sharex=ax_b)
    ax_bz = fig.add_subplot(g_bz[0])
    ax_bzv = fig.add_subplot(g_bz[1], sharex=ax_bz)
    ax_pz = fig.add_subplot(g_pz[0])
    ax_pzv = fig.add_subplot(g_pz[1], sharex=ax_pz)
    for ax in (ax_b, ax_bv, ax_p, ax_pv, ax_bz, ax_bzv, ax_pz, ax_pzv):
        style_ax(ax)
    for ax in (ax_b, ax_p, ax_bz, ax_pz):
        plt.setp(ax.get_xticklabels(), visible=False)
        ax.tick_params(axis="x", length=0)

    # ============ 武装期 / 破位竖线(左列四个面板共用时间轴) ============
    arm_x0 = xof(t0) + 0.5                                            # 破位K收盘
    arm_end_t = (int(e["signal_time"]) + tf) if e else int(ep["expires_at"])
    arm_x1 = (arm_end_t - t_a) / tf - 0.5
    exp_x = (int(ep["expires_at"]) - t_a) / tf - 0.5
    for ax in (ax_b, ax_bv, ax_p, ax_pv):
        ax.axvspan(arm_x0, arm_x1, color=C_ARM, alpha=0.085, lw=0, zorder=0)
        for i, tt in enumerate(trig_times):
            ax.axvspan(xof(tt) - 0.5, xof(tt) + 0.5, color=C_TRIG, alpha=0.30 if i == 0 else 0.17,
                       lw=0, zorder=1)
        if e and exp_x <= n_bars - 0.5:
            ax.axvline(exp_x, color=C_ARM, lw=1.0, ls=(0, (2, 3)), zorder=1)
    ax_p.axvline(xof(t0), color=C_TRIG, lw=1.3, ls="--", zorder=2)

    # ============ 上左: BTC ============
    candles(ax_b, b_rows, xof)
    ref = float(trig["ref_low"])
    ref_t = int(trig["ref_low_time"])
    ax_b.axhline(ref, color=C_REF, lw=1.5, ls="--", zorder=5)
    if t_a <= ref_t <= t_b:
        i = btc.idx(ref_t)
        if i is not None:
            ax_b.scatter([xof(ref_t)], [float(btc.rows[i]["low"])], marker="o", s=70, facecolors="none",
                         edgecolors=C_REF, linewidths=1.8, zorder=8)
    tag(ax_b, x_lab, ref, f"48h低 {px(ref)}\n{bkk(ref_t)} 创", C_REF, va="center", fs=12.5)
    lo_b = min([float(r["low"]) for r in b_rows] + [ref])
    hi_b = max([float(r["high"]) for r in b_rows] + [ref])
    span_b = (hi_b - lo_b) or hi_b * 0.01
    ax_b.set_ylim(lo_b - span_b * 0.06, hi_b + span_b * 0.16)
    y_top = hi_b + span_b * 0.145
    tag(ax_b, xof(t0), y_top, f"破位K {bkk(t0, '%H:%M')}", C_TRIG, ha="right" if retrig else "center",
        va="top", fs=12.5)
    if retrig:
        tag(ax_b, xof(int(retrig[0]["time"])) + 0.8, y_top, f"再破位×{len(retrig)}", C_TRIG, ha="left",
            va="top", fs=11.5)
    tag(ax_b, arm_x0 + 0.6, lo_b - span_b * 0.045,
        "武装期(破位K收盘 → " + ("信号K收盘)" if e else "到期)"), C_ARM, ha="left", va="bottom", fs=11)
    if e and exp_x <= n_bars - 0.5:
        tag(ax_b, exp_x, y_top, f"有效期止 {bkk(int(ep['expires_at']), '%H:%M')}", C_ARM,
            ha="left" if exp_x < n_bars * 0.8 else "right", va="top", fs=10.5)
    ax_b.text(0.008, 0.975, "BTCUSDT 5m", transform=ax_b.transAxes, color=FG, fontsize=13,
              fontweight="bold", va="top", ha="left", zorder=12)

    vol_bars(ax_bv, b_rows, xof, hot=set(trig_times))
    vol_mean_lines(ax_bv, btc, b_rows, xof, n_ma, mult)
    i0 = btc.idx(t0)
    if i0 is not None:
        v0 = float(btc.rows[i0]["volume"])
        vmax = ax_bv.get_ylim()[1]
        tag(ax_bv, xof(t0) - 1.0, max(min(v0, vmax * 0.93), vmax * 0.52), f"{float(trig['vol_ratio']):.2f}×", C_TRIG,
            ha="right", va="top", fs=13, fontweight="bold")
    ax_bv.text(0.008, 0.93, f"量  白线=前{n_ma}根均量  橙虚线={mult:g}×门槛", transform=ax_bv.transAxes,
               color=DIM, fontsize=10.5, va="top", ha="left", zorder=12,
               bbox=dict(boxstyle="round,pad=0.12", fc=BG, ec="none", alpha=0.7))
    time_ticks(ax_bv, t_a, n_bars, tf, every=12 if n_bars <= 150 else 24)

    # ============ 下左: PUMP ============
    candles(ax_p, g_rows, xof)
    lo_p = min(float(r["low"]) for r in g_rows) if g_rows else 0.0
    hi_p = max(float(r["high"]) for r in g_rows) if g_rows else 1.0
    for it in an["discarded"]:
        col = C_OLD
        ax_p.scatter([xof(it["low_time"])], [it["low"]], marker="^", s=90, facecolors="none",
                     edgecolors=col, linewidths=1.5, zorder=8)
    if e:
        if ch:
            for key in ("L", "M", "R"):
                merged_box(ax_p, ch[key], xof, C_FX, alpha=0.28, lw=0.9)
        x_sig = xof(int(e["signal_time"]))
        tp_in = float(e["tp"]) <= hi_p + (hi_p - lo_p) * 0.9
        ys = [lo_p, hi_p, float(e["sl"])] + ([float(e["tp"])] if tp_in else [])
        lo_y, hi_y = min(ys), max(ys)
        span = (hi_y - lo_y) or hi_y * 0.01
        ax_p.set_ylim(lo_y - span * 0.16, hi_y + span * 0.15)
        ax_p.scatter([xof(int(e["fx_low_time"]))], [float(e["fx_low"]) - span * 0.012], marker="^", s=170,
                     c=C_FX, edgecolors="white", linewidths=0.9, zorder=9)
        ax_p.scatter([x_sig], [float(e["entry"])], marker="*", s=380, c=C_ENTRY, edgecolors="black",
                     linewidths=0.8, zorder=10)
        for y, col, ls in ((float(e["entry"]), C_ENTRY, "-"), (float(e["sl"]), C_SL, "--"),
                           (float(e["tp"]), C_TP, "--")):
            if y == float(e["tp"]) and not tp_in:
                continue
            ax_p.hlines(y, x_sig, n_bars + pad, color=col, lw=1.5, ls=ls, zorder=5)
        tag(ax_p, x_lab, float(e["entry"]), f"入场 {px(e['entry'])}", C_ENTRY, va="bottom", fs=12.5)
        tag(ax_p, x_lab, float(e["sl"]), f"止损 {px(e['sl'])}\n风险 {float(e['risk_pct']):.2f}%", C_SL,
            va="top", fs=12.5)
        if tp_in:
            tag(ax_p, x_lab, float(e["tp"]), f"止盈 {px(e['tp'])}\n({float(e['rr']):g}R)", C_TP,
                va="bottom", fs=12.5)
        else:
            tag(ax_p, x_lab, hi_y + span * 0.085, f"止盈 {px(e['tp'])} ↑图外", C_TP, va="top", fs=12.5)
        tag(ax_p, x_sig - 1.2, float(e["entry"]) + span * 0.05,
            "停顿K=信号K" if need_stall else "信号K", C_ENTRY, ha="right", va="bottom", fs=11.5)
    else:
        span = (hi_p - lo_p) or hi_p * 0.01
        ax_p.set_ylim(lo_p - span * 0.07, hi_p + span * 0.10)
        tag(ax_p, exp_x + 0.8, hi_p + span * 0.06, f"到期 {bkk(int(ep['expires_at']), '%H:%M')}  武装期内无合格分型",
            C_ARM, ha="left", va="top", fs=12)
    y0, y1 = ax_p.get_ylim()
    tag(ax_p, xof(t0) - 0.8, y1 - (y1 - y0) * 0.02, "BTC破位K", C_TRIG, ha="right", va="top", fs=11.5)
    if an["discarded"]:
        cnt = {}
        for it in an["discarded"]:
            k = SHORT_REASON.get(it["reason"], it["reason"])
            cnt[k] = cnt.get(k, 0) + 1
        tag(ax_p, 0.0, y0 + (y1 - y0) * 0.02, "△ 灰色 = 本轮被放弃的更早分型: "
            + " / ".join(f"{k} {v}个" for k, v in cnt.items()), C_OLD, ha="left", va="bottom", fs=10.5)
    ax_p.text(0.008, 0.975, "PUMPUSDT 5m", transform=ax_p.transAxes, color=FG, fontsize=13,
              fontweight="bold", va="top", ha="left", zorder=12)
    vol_bars(ax_pv, g_rows, xof)
    time_ticks(ax_pv, t_a, n_bars, tf, every=12 if n_bars <= 150 else 24)
    ax_b.set_xlim(*xlim)

    # ============ 右上: BTC 破位K放大 ============
    z_a = t0 - 13 * tf
    z_b = min(t0 + 8 * tf, t_b)
    zn = (z_b - z_a) // tf + 1
    zx = lambda t: (t - z_a) / tf                                     # noqa: E731
    zb_rows = btc.span(z_a, z_b)
    candles(ax_bz, zb_rows, zx, w=0.7, lw=1.4)
    for i, tt in enumerate(trig_times):
        if z_a <= tt <= z_b:
            for ax in (ax_bz, ax_bzv):
                ax.axvspan(zx(tt) - 0.5, zx(tt) + 0.5, color=C_TRIG, alpha=0.30 if i == 0 else 0.17,
                           lw=0, zorder=1)
    ax_bz.axhline(ref, color=C_REF, lw=1.6, ls="--", zorder=5)
    zl = min([float(r["low"]) for r in zb_rows] + [ref])
    zh = max([float(r["high"]) for r in zb_rows] + [ref])
    zs = (zh - zl) or zh * 0.005
    ax_bz.set_ylim(zl - zs * 0.30, zh + zs * 0.14)
    # 破位之前的K全部在 48h低 之上 → 破位K左侧、参考线下方一定是空白, 标签都放这里, 不遮K线
    tag(ax_bz, -0.5, ref - zs * 0.02, f"48h低 {px(ref)}", C_REF, ha="left", va="top", fs=12)
    c0 = float(trig["close"])
    ax_bz.hlines(c0, zx(t0) - 2.6, zx(t0) + 0.5, color=C_TRIG, lw=1.6, zorder=7)
    tag(ax_bz, zx(t0) - 0.7, min(c0, ref - zs * 0.115) - zs * 0.02,
        f"收 {px(c0)}\n比48h低 低 {(ref - c0) / ref * 100:.3f}%", C_TRIG, ha="right", va="top", fs=12)
    if ref_t >= z_a:
        i = btc.idx(ref_t)
        if i is not None:
            ax_bz.scatter([zx(ref_t)], [float(btc.rows[i]["low"])], marker="o", s=110, facecolors="none",
                          edgecolors=C_REF, linewidths=2.0, zorder=8)
    ax_bz.set_title("放大 · BTC 破位K附近", loc="left", color=FG, fontsize=12.5, fontweight="bold", pad=4)
    vol_bars(ax_bzv, zb_rows, zx, hot=set(trig_times), w=0.7)
    vol_mean_lines(ax_bzv, btc, zb_rows, zx, n_ma, mult)
    if i0 is not None:
        vma = float(trig["vol_ma"])
        ax_bzv.hlines(vma * mult, zx(t0) - 0.5, zx(t0) + 0.5, color=C_REF, lw=2.6, zorder=7)
        vmax = ax_bzv.get_ylim()[1]
        tag(ax_bzv, zx(t0) - 0.8, vmax * 0.95,
            f"量 {vol_s(trig['volume'])}\n= {float(trig['vol_ratio']):.2f}× 均量", C_TRIG, ha="right",
            va="top", fs=12)
    ax_bz.set_xlim(-0.8, zn - 0.2)
    time_ticks(ax_bzv, z_a, zn, tf, every=6, with_date=False)

    # ============ 右下: PUMP 分型与停顿放大 ============
    cands = ([ch] if ch else []) + an["discarded"]
    if e and ch:
        p_a = min(ch["L"]["times"][0], int(e["fx_low_time"])) - 4 * tf
        p_b = int(e["signal_time"]) + 4 * tf
        early = [it["L"]["times"][0] for it in an["discarded"]]
        if early and (p_b - (min(early) - 3 * tf)) // tf <= 26:
            p_a = min(early) - 3 * tf                                 # 放得下就把被放弃的分型一起框进来
        if p_a > t0 and (p_b - (t0 - 2 * tf)) // tf <= 24:
            p_a = t0 - 2 * tf                                         # 放得下就把破位K也框进来
        if (p_b - p_a) // tf > 40:
            p_a = p_b - 40 * tf
    elif cands:
        p_a = t0 - 3 * tf
        p_b = int(ep["expires_at"]) + 2 * tf
    else:
        p_a, p_b = t0 - 5 * tf, t0 + 36 * tf
    p_b = min(p_b, t_b)
    pn = (p_b - p_a) // tf + 1
    qx = lambda t: (t - p_a) / tf                                     # noqa: E731
    zp_rows = tgt.span(p_a, p_b)
    lw_c = 1.5 if pn <= 26 else 1.1
    candles(ax_pz, zp_rows, qx, w=0.7, lw=lw_c)
    ql = min(float(r["low"]) for r in zp_rows) if zp_rows else 0.0
    qh = max(float(r["high"]) for r in zp_rows) if zp_rows else 1.0
    zpad = max(pn * 0.56, 8.0) if e else pn * 0.04
    if e:
        tp_in_z = float(e["tp"]) <= qh + (qh - ql) * 0.6
        ys = [ql, qh, float(e["sl"])] + ([float(e["tp"])] if tp_in_z else [])
        ql2, qh2 = min(ys), max(ys)
    else:
        tp_in_z = False
        ql2, qh2 = ql, qh
    qs = (qh2 - ql2) or qh2 * 0.01
    ax_pz.set_xlim(-0.8, pn - 0.2 + zpad)
    lmr_row = None
    top_need = qh2 + qs * 0.13
    if e and ch:
        # 左/中/右 三个字排成一行放在分型上方(挤的时候左右推开, 用细线连回各自的合并K)
        lmr_row = max(ch[k]["high"] for k in ("L", "M", "R")) + qs * 0.05
        top_need = max(top_need, lmr_row + qs * 0.17)
    ax_pz.set_ylim(ql2 - qs * 0.13, top_need)
    in_z = lambda t: p_a <= t <= p_b                                  # noqa: E731

    n_disc = 0
    n_disc_in_zoom = sum(1 for it in an["discarded"] if in_z(it["M"]["times"][0]))
    for it in an["discarded"]:
        if not in_z(it["M"]["times"][0]):
            continue
        for key in ("L", "M", "R"):
            if in_z(it[key]["times"][0]):
                merged_box(ax_pz, it[key], qx, C_OLD, alpha=0.10, lw=0.9)
        ax_pz.scatter([qx(it["low_time"])], [it["low"] - qs * 0.02], marker="^", s=110, facecolors="none",
                      edgecolors=C_OLD, linewidths=1.6, zorder=8)
        if it["reason"] in ("分型但无停顿", "分型未等满(武装期结束)"):
            # 该分型的停顿门槛(右K高)画到确认K之后 gap 根为止: 这几根里没有一根收盘站上这条线
            x_c = qx(it["confirm_time"])
            ax_pz.hlines(it["R"]["high"], qx(it["R"]["times"][0]) - 0.46,
                         min(x_c + int(params.get("stall_max_gap", 3)), pn - 1) + 0.5,
                         color=C_OLD, lw=1.4, ls=(0, (3, 1.5)), zorder=7)
        n_disc += 1
        if it["reason"] != "分型但无停顿" or n_disc_in_zoom <= 2:
            ax_pz.text(qx(it["low_time"]), it["low"] - qs * (0.05 if n_disc % 2 else 0.105),
                       SHORT_REASON.get(it["reason"], it["reason"]), color=C_OLD, fontsize=10.5,
                       ha="center", va="top", zorder=12,
                       bbox=dict(boxstyle="round,pad=0.12", fc=BG, ec="none", alpha=0.7))
    for i, tt in enumerate(trig_times):
        if in_z(tt):
            for ax in (ax_pz, ax_pzv):
                ax.axvspan(qx(tt) - 0.5, qx(tt) + 0.5, color=C_TRIG, alpha=0.20 if i == 0 else 0.10,
                           lw=0, zorder=1)
    y0, y1 = ax_pz.get_ylim()
    if in_z(t0):
        ax_pz.axvline(qx(t0), color=C_TRIG, lw=1.3, ls="--", zorder=2)
        tag(ax_pz, qx(t0), y1 - (y1 - y0) * 0.012, "BTC破位K", C_TRIG, ha="center", va="top", fs=11)
    else:
        tag(ax_pz, -0.5, y1 - (y1 - y0) * 0.012, f"← BTC破位K在左侧 {(p_a - t0) // tf} 根外", C_TRIG,
            ha="left", va="top", fs=11)

    if e and ch:
        x_sig = qx(int(e["signal_time"]))
        ax_w_px = FIGSIZE[0] * DPI * ax_pz.get_position().width
        unit_px = ax_w_px / (pn + zpad + 0.6)
        min_dx = 1.35 * (15 * DPI / 72.0) / unit_px                # 一个字的宽度(换算成K线根数)
        cx = {k: (qx(ch[k]["times"][0]) + qx(ch[k]["times"][-1])) / 2 for k in ("L", "M", "R")}
        lx = dict(cx)
        lx["L"] = min(lx["L"], lx["M"] - min_dx)
        lx["R"] = max(lx["R"], lx["M"] + min_dx)
        for key, name in (("L", "左"), ("M", "中"), ("R", "右")):
            if not in_z(ch[key]["times"][0]):
                continue
            merged_box(ax_pz, ch[key], qx, C_FX, alpha=0.24, lw=1.6)
            ax_pz.plot([lx[key], cx[key]], [lmr_row, ch[key]["high"]], color=C_FX, lw=0.9, zorder=8)
            ax_pz.text(lx[key], lmr_row, name, color=C_FX, fontsize=15, fontweight="bold", ha="center",
                       va="bottom", zorder=13)
        # 分型低点
        x_low = qx(int(e["fx_low_time"]))
        ax_pz.scatter([x_low], [float(e["fx_low"]) - qs * 0.022], marker="^", s=210, c=C_FX,
                      edgecolors="white", linewidths=1.0, zorder=9)
        ax_pz.hlines(float(e["fx_low"]), x_low - 0.5, pn + zpad, color=C_FX, lw=1.0, ls=":", zorder=5)
        # 右K高(停顿门槛)
        rh = ch["R"]["high"]
        if need_stall:
            ax_pz.hlines(rh, qx(ch["R"]["times"][0]) - 0.46, x_sig + 0.5, color=C_STALL, lw=1.9, zorder=7)
            # 确认K
            x_cf = qx(int(e["fx_confirm_time"]))
            i_cf = tgt.idx(int(e["fx_confirm_time"]))
            y_cf = float(tgt.rows[i_cf]["low"]) if i_cf is not None else ch["R"]["low"]
            ax_pz.annotate("确认K", (x_cf, y_cf), xytext=(x_cf + 0.9, y_cf - qs * 0.10), color=C_STALL,
                           fontsize=12, ha="center", va="top", zorder=13,
                           bbox=dict(boxstyle="round,pad=0.12", fc=BG, ec="none", alpha=0.8),
                           arrowprops=dict(arrowstyle="-", color=C_STALL, lw=1.1))
        # 信号K
        ax_pz.scatter([x_sig], [float(e["entry"])], marker="*", s=420, c=C_ENTRY, edgecolors="black",
                      linewidths=0.8, zorder=10)
        for y, col, ls in ((float(e["entry"]), C_ENTRY, "-"), (float(e["sl"]), C_SL, "--"),
                           (float(e["tp"]), C_TP, "--")):
            if y == float(e["tp"]) and not tp_in_z:
                continue
            ax_pz.hlines(y, x_sig, pn + zpad, color=col, lw=1.5, ls=ls, zorder=5)
        # 右侧标签栏: 自上而下 止盈 / 入场 / 右K高 / 分型低 / 止损, 太挤时自动错开
        xr = pn + 0.3
        labs = []                                                     # (价位, 文字, 颜色, 行数)
        if tp_in_z:
            labs.append((float(e["tp"]), f"止盈 {px(e['tp'])}", C_TP, 1))
        labs.append((float(e["entry"]), ("★停顿K=信号K" if need_stall else "★信号K=确认K")
                     + f"\n入场 {px(e['entry'])}", C_ENTRY, 2))
        if need_stall:
            labs.append((rh, f"右K高 {px(rh)}", C_STALL, 1))
        labs.append((float(e["fx_low"]), f"分型低 {px(e['fx_low'])}", C_FX, 1))
        labs.append((float(e["sl"]), f"止损 {px(e['sl'])}", C_SL, 1))
        labs.sort(key=lambda r: -r[0])
        line_h = (y1 - y0) * 0.074
        gap = lambda i, j: line_h * (labs[i][3] + labs[j][3]) / 2     # noqa: E731
        ypos = [r[0] for r in labs]
        hi_lim = y1 - line_h * labs[0][3] / 2 - (y1 - y0) * 0.01 - (0 if tp_in_z else line_h * 1.1)
        ypos[0] = min(ypos[0], hi_lim)
        for k in range(1, len(ypos)):                                 # 自上而下推开
            if ypos[k - 1] - ypos[k] < gap(k - 1, k):
                ypos[k] = ypos[k - 1] - gap(k - 1, k)
        low_lim = y0 + (y1 - y0) * 0.035
        if ypos[-1] < low_lim:                                        # 底部越界则整体上推
            ypos[-1] = low_lim
            for k in range(len(ypos) - 2, -1, -1):
                if ypos[k] - ypos[k + 1] < gap(k, k + 1):
                    ypos[k] = ypos[k + 1] + gap(k, k + 1)
        for (y, txt, col, _), yp in zip(labs, ypos):
            tag(ax_pz, xr, yp, txt, col, ha="left", va="center", fs=12)
        if not tp_in_z:
            tag(ax_pz, xr, y1 - (y1 - y0) * 0.015, f"止盈 {px(e['tp'])} ↑图外", C_TP, ha="left", va="top",
                fs=12)
    ax_pz.set_title(("放大 · PUMP 分型与停顿" if e else "放大 · PUMP 武装期内的分型")
                    + ("  (灰=被放弃的分型)" if n_disc_in_zoom else ""),
                    loc="left", color=FG, fontsize=12.5, fontweight="bold", pad=4)
    vol_bars(ax_pzv, zp_rows, qx, w=0.7)
    time_ticks(ax_pzv, p_a, pn, tf, every=3 if pn <= 20 else (6 if pn <= 40 else 12), with_date=False)

    # ============ 标题 + 图注(每个条件的实测值) ============
    ratio = float(trig["vol_ratio"])
    when = bkk(t0, "%Y-%m-%d %H:%M")
    if e:
        title = (f"{rec['label']}  {when}  BTC 破48h低 + 放量 {ratio:.2f}x → PUMP 底分型"
                 + ("+停顿" if need_stall else ""))
    else:
        title = f"{rec['label']}  {when}  BTC 破48h低 + 放量 {ratio:.2f}x → PUMP 武装期内无合格分型(到期)"
    fig.text(0.5, 0.975, title, color="#f0f3f6", fontsize=19, fontweight="bold", ha="center", va="center")
    below = (ref - c0) / ref * 100
    cap1 = (f"BTC {bkk(t0, '%H:%M')}K 收 {px(c0)} < 48h低 {px(ref)}({bkk(ref_t)} 创, 低 {below:.3f}%)"
            f"  ·  量 {vol_s(trig['volume'])} = {ratio:.2f}× 前{n_ma}根均量 {vol_s(trig['vol_ma'])}(门槛 {mult:g}×)"
            + (f"  ·  武装期内再破位 {len(retrig)} 次" if retrig else ""))
    if e:
        k_low = (int(e["fx_low_time"]) - t0) // tf
        cap2 = (f"PUMP 分型低 {px(e['fx_low'])} @{bkk(int(e['fx_low_time']), '%H:%M')}(破位K后第{k_low}根)"
                f"  ·  确认K {bkk(int(e['fx_confirm_time']), '%H:%M')}")
        if need_stall and e.get("stall_time") is not None:
            cap2 += (f"  ·  停顿K {bkk(int(e['stall_time']), '%H:%M')}(确认后第{an.get('stall_n', '?')}根) "
                     f"收 {px(e['entry'])} > 右K高 {px(e.get('fx_right_high', 0))}")
        cap2 += f"  ·  入 {px(e['entry'])} 损 {px(e['sl'])} 盈 {px(e['tp'])} 风险 {float(e['risk_pct']):.2f}%"
    else:
        n_ns = sum(1 for it in an["discarded"] if it["low_time"] >= t0)
        cap2 = (f"PUMP 武装期(至 {bkk(int(ep['expires_at']))})内: 低点在破位之后的底分型 {n_ns} 个, "
                f"都没有在确认后 {params.get('stall_max_gap', 3)} 根内出现停顿K → 到期, 无入场")
    fig.text(0.5, 0.937, cap1, color=FG, fontsize=12.2, ha="center", va="center")
    fig.text(0.5, 0.907, cap2, color=FG, fontsize=12.2, ha="center", va="center")
    notes = [f for f in rec["flags"] if f in ("停顿前分型低点已被跌破", "信号前已触及止损价", "自检不一致",
                                               "同K再破位+入场", "并列低点判为早于破位")]
    tail_note = ("信号K之后的K线未画(盲审版)" if after <= 0 else
                 f"入场后仅按固定跨度多画约{after * tf / 3_600_000:g}小时")
    fig.text(0.992, 0.006, f"时间 = 曼谷 UTC+7 · 黄色竖条 = BTC 破位K/再破位K · 只标形态与实测值, {tail_note}",
             color="#6e7681", fontsize=9.5, ha="right", va="bottom")
    if notes:
        fig.text(0.008, 0.006, "注意: " + " / ".join(notes), color=C_SL, fontsize=12, ha="left",
                 va="bottom", fontweight="bold")
    fig.savefig(out, dpi=DPI, facecolor=BG)
    plt.close(fig)


# ---------------------------------------------------------------- 主流程
def parse_only(spec: str) -> set:
    """'7' / '1-20,45,E1' → {'#7'} / {'#1'..'#20','#45','E1'}。"""
    want = set()
    for part in (spec or "").replace(" ", "").split(","):
        if not part:
            continue
        part = part.lstrip("#")
        if part[:1] in ("E", "e"):
            want.add("E" + str(int(part[1:])))
        elif "-" in part:
            a, b = part.split("-", 1)
            want.update(f"#{k}" for k in range(int(a), int(b) + 1))
        else:
            want.add(f"#{int(part)}")
    return want


_G = {}


def _init(btc_path, tgt_path, params, tf, after_bars=AFTER_BARS):
    _G["btc"] = Series(load_rows(btc_path))
    _G["tgt"] = Series(load_rows(tgt_path))
    _G["params"], _G["tf"] = params, tf
    _G["after_bars"] = after_bars


def _job(args):
    rec, out = args
    render(rec, _G["btc"], _G["tgt"], _G["params"], _G["tf"], out)
    return rec["label"]


def strip_private(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_")}


def slim(rec: dict) -> dict:
    """子进程渲染要用的最小记录(去掉不可序列化的 Fractal 对象)。"""
    an = rec["_an"]

    def clean(it):
        return {k: v for k, v in it.items() if k != "fx"} if it else None

    r = dict(rec)
    r["_an"] = dict(an, chosen=clean(an["chosen"]), discarded=[clean(x) for x in an["discarded"]])
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="btcbreak_pump 形态审核出图(只画形态, 不结算)")
    ap.add_argument("--instances", required=True, help="scripts/btcbreak_run.py 导出的 JSON")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--only", default="", help="只画指定编号: 7 / 1-20,45 / E1")
    ap.add_argument("--limit", type=int, default=0, help="只画前 N 张入场图")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--after-bars", type=int, default=AFTER_BARS,
                    help=f"信号K(或到期)之后多画几根K(默认 {AFTER_BARS}); 0 = 停在信号K, 审形态时看不到之后的走势")
    a = ap.parse_args(argv)
    if a.after_bars < 0:
        ap.error("--after-bars 不能为负")
    _G["after_bars"] = a.after_bars

    with open(a.instances, encoding="utf-8") as f:
        inst = json.load(f)
    params = inst["params"]
    tf = int(inst["meta"].get("tf_ms") or tf_to_ms(params["tf"]))
    btc_path = resolve_cache(inst["meta"]["data"]["trigger"])
    tgt_path = resolve_cache(inst["meta"]["data"]["target"])
    btc, tgt = Series(load_rows(btc_path)), Series(load_rows(tgt_path))

    retrig_by_ep = {}
    for ev in inst["events"]:
        if ev["type"] == "retrigger":
            retrig_by_ep.setdefault(ev["episode_id"], []).append(ev["trigger"])
    episodes = sorted(inst["episodes"], key=lambda ep: int(ep["trigger"]["time"]))

    records, n_entry, n_exp, prev_sig = [], 0, 0, None
    for ep in episodes:
        rt = sorted(retrig_by_ep.get(ep.get("episode_id", ep["trigger"]["time"]), []),
                    key=lambda r: int(r["time"]))
        if ep["outcome"] == "entry":
            n_entry += 1
            rec = build_record(f"#{n_entry}", "entry", ep, rt, prev_sig, btc, tgt, params, tf)
            prev_sig = int(ep["entry"]["signal_time"])
        else:
            n_exp += 1
            rec = build_record(f"E{n_exp}", "expired", ep, rt, prev_sig, btc, tgt, params, tf)
        records.append(rec)

    os.makedirs(a.outdir, exist_ok=True)
    want = parse_only(a.only)
    todo = []
    for rec in records:
        if want and rec["label"] not in want:
            continue
        if a.limit and rec["kind"] == "entry" and int(rec["label"][1:]) > a.limit:
            continue
        todo.append(rec)

    if a.jobs > 1 and len(todo) > 4:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(a.jobs, initializer=_init,
                                 initargs=(btc_path, tgt_path, params, tf, a.after_bars)) as ex:
            work = [(slim(r), os.path.join(a.outdir, r["file"])) for r in todo]
            for k, lab in enumerate(ex.map(_job, work, chunksize=4), 1):
                if k % 25 == 0 or k == len(work):
                    print(f"  出图 {k}/{len(work)} (最近 {lab})", flush=True)
    else:
        for k, rec in enumerate(todo, 1):
            render(slim(rec), btc, tgt, params, tf, os.path.join(a.outdir, rec["file"]))
            if k % 25 == 0 or k == len(todo):
                print(f"  出图 {k}/{len(todo)} (最近 {rec['label']})", flush=True)

    manifest = {
        "rule": inst["meta"].get("rule"), "status": inst["meta"].get("status"),
        "range": inst["meta"].get("range"), "params": params,
        "instances": os.path.abspath(a.instances).replace("\\", "/"),
        "n_entry": n_entry, "n_expired": n_exp,
        "note": "只含形态与实测值, 未做任何结算或盈亏统计(铁律七)",
        "after_bars": a.after_bars,
        "charts": [strip_private(r) for r in records],
    }
    with open(os.path.join(a.outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    bad = [r for r in records if r["checks"]]
    print(f"入场图 {n_entry} 轮 / 到期图 {n_exp} 轮; 本次出图 {len(todo)} 张 → {os.path.abspath(a.outdir)}")
    print(f"与 instances 对账不一致的轮次: {len(bad)}")
    for r in bad:
        for c in r["checks"]:
            print(f"  {r['label']} {r['trigger_local']}: {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
