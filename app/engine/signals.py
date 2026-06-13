"""信号引擎。

默认策略 spring_v4（用户策略）：放量破前低 → 底分型(倒三角)收回破位K顶部 → 一买；
之后更高低点的底分型 → 二买（主力K可选标注）。每个 (币种,级别) 一个状态机。
保留 chan_v1（缠论分型+因子）可通过 strategy=chan_v1 切回。
"""
import logging
import time
from dataclasses import dataclass, asdict

from .chan import (
    find_fractals, merge_klines, volume_ratio, prior_support,
    is_break_reclaim, trend_direction,
)
from .factors import atr, score_signal, sl_atr_sane
from .spring import (
    vol_avg, detect_breakdown, is_bottom_fractal, is_top_fractal,
    is_main_k, prior_peak,
)
from .volume_profile import (
    build_profile, hvn_list_above, hvn_list_below,
    nearest_hvn_above, nearest_hvn_below,
)

log = logging.getLogger("signals")

TF_MS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

# paper 统计轨道
SIGNAL_TYPES = ("buy1", "buy2")
TYPE_LABEL = {"buy1": "✅一买", "buy2": "🔁二买"}


@dataclass
class Signal:
    symbol: str
    tf: str
    direction: str       # long / short
    kind: str            # primary(推送TG)
    entry: float
    sl: float
    tp: float
    rr: float
    vol_ratio: float
    strength: str
    suggested_qty: float
    risk_usdt: float
    reason: str
    created_at: int
    extra: dict

    def to_db(self) -> dict:
        d = asdict(self)
        d["status"] = "new"
        return d


class SignalEngine:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self._cooldown: dict[tuple, int] = {}
        self.funding: dict[str, float] = {}
        self.btc_trend: int = 0
        self.funnel: dict[str, int] = {}
        # 状态机: (symbol, tf) -> state dict
        self._state: dict[tuple, dict] = {}
        # 失效/解除等文字通知（engine 转发到 TG）
        self.notices: list[str] = []
        # 大盘观点(提阿非罗/手动)：direction=long/short/neutral
        self.macro_view: dict = {"direction": "neutral", "note": "", "at": 0}

    def load_macro(self, db) -> None:
        s = db.get_settings()
        self.macro_view = {
            "direction": s.get("macro_view_direction", "neutral"),
            "note": s.get("macro_view_note", ""),
            "at": int(s.get("macro_view_at", 0) or 0),
        }

    def _macro_tag(self, direction: str) -> str:
        """逆大盘则返回警告标签，否则空。"""
        mv = (self.macro_view or {}).get("direction", "neutral")
        if mv not in ("long", "short"):
            return ""
        if direction != mv:
            return f" ⚠逆大盘(提阿非罗看{'多' if mv == 'long' else '空'})"
        return f" ✓顺大盘"

    def _p(self, key: str, default=None):
        return self.cfg.get(key, default)

    def _drop(self, stage: str):
        self.funnel[stage] = self.funnel.get(stage, 0) + 1
        return None

    # ======================= 路由 =======================

    def evaluate(self, symbol: str, tf: str, klines: list,
                 klines_15m: list | None = None) -> Signal | None:
        if self._p("strategy", "spring_v4") == "spring_v4":
            return self._eval_spring(symbol, tf, klines)
        return self._evaluate_chan(symbol, tf, klines, klines_15m)

    # ======================= 策略V4: 破位+底分型 =======================

    def _btc_ok(self, direction: str) -> bool:
        if not self._p("spring.btc_filter", True):
            return True
        return self.btc_trend != (-1 if direction == "long" else 1)

    def _eval_spring(self, symbol: str, tf: str, klines: list) -> "Signal | None":
        need = max(self._p("spring.newlow_lookback", 20) + 30, 60)
        if len(klines) < need:
            return None
        i = len(klines) - 1
        key = (symbol, tf)
        st = self._state.get(key)
        if st:
            if st["phase"] == "await_buy1":
                return self._spring_buy1(key, st, klines, i)
            return self._spring_buy2(key, st, klines, i)
        return self._spring_seek(key, klines, i)

    # ---------- 找放量破位K ----------

    def _spring_seek(self, key, klines, i) -> None:
        direction, detail = detect_breakdown(
            klines, i,
            vol_mult=self._p("spring.vol_mult", 4.0),
            newlow_lookback=self._p("spring.newlow_lookback", 20),
            body_min=self._p("spring.body_min", 0.5))
        if not direction:
            return None
        self.funnel["breakdown"] = self.funnel.get("breakdown", 0) + 1
        if not self._btc_ok(direction):
            return self._drop("btc_filter")
        k = klines[i]
        self._state[key] = {
            "direction": direction, "phase": "await_buy1",
            "bd": {"time": int(k["open_time"]), "open": float(k["open"]),
                   "high": float(k["high"]), "low": float(k["low"]),
                   "vol": float(k["volume"]), "detail": detail},
            "bars": 0, "prot": float(k["low"]) if direction == "long" else float(k["high"]),
            "main_k": None,
        }
        return None  # 破位本身不出信号，等底分型

    # ---------- 一买：破位后第一个底分型收回破位K顶部 ----------

    def _spring_buy1(self, key, st, klines, i) -> "Signal | None":
        symbol, tf = key
        d = st["direction"]
        bd = st["bd"]
        st["bars"] += 1
        k = klines[i]
        c, l, h = float(k["close"]), float(k["low"]), float(k["high"])

        # 期间出现更极端的新破位 → 参考下移并重新计时
        nd, ndetail = detect_breakdown(
            klines, i, vol_mult=self._p("spring.vol_mult", 4.0),
            newlow_lookback=self._p("spring.newlow_lookback", 20),
            body_min=self._p("spring.body_min", 0.5))
        if nd == d and ((d == "long" and l < bd["low"]) or (d == "short" and h > bd["high"])):
            st["bd"] = {"time": int(k["open_time"]), "open": float(k["open"]),
                        "high": h, "low": l, "vol": float(k["volume"]), "detail": ndetail}
            st["prot"] = l if d == "long" else h
            st["bars"] = 0
            return None

        if st["bars"] > self._p("spring.fractal_window", 8):
            del self._state[key]
            return self._drop("buy1_window")

        buf = self._p("signal.sl_buffer_pct", 0.3) / 100.0
        if d == "long":
            if not is_bottom_fractal(klines, i) or c < bd["open"]:
                return None  # 没出底分型 或 还没收回到破位K顶部
            frac_low = float(klines[i - 1]["low"])
            prot = min(frac_low, bd["low"])
            entry, sl = bd["open"], prot * (1 - buf)
        else:
            if not is_top_fractal(klines, i) or c > bd["open"]:
                return None
            frac_high = float(klines[i - 1]["high"])
            prot = max(frac_high, bd["high"])
            entry, sl = bd["open"], prot * (1 + buf)

        reason = (f"✅一买 {'做多' if d == 'long' else '做空'}: "
                  f"放量{bd['detail']['vol_ratio']}x破{'前低' if d == 'long' else '前高'} → "
                  f"底分型收回{'破位K顶部' if d == 'long' else '破位K底部'}")
        sig = self._spring_make(symbol, tf, d, entry, sl, "buy1", bd, klines,
                                extra={"breakdown": bd}, reason=reason)
        # 进入二买跟踪期
        st.update(phase="await_buy2", prot=prot, buy2_bars=0)
        return sig

    # ---------- 二买：更高低点的底分型(主力K可选) ----------

    def _spring_buy2(self, key, st, klines, i) -> "Signal | None":
        symbol, tf = key
        d = st["direction"]
        st["buy2_bars"] += 1
        k = klines[i]
        c, l, h = float(k["close"]), float(k["low"]), float(k["high"])

        # 实质跌破保护位 → 整组失效
        if (d == "long" and c < st["prot"]) or (d == "short" and c > st["prot"]):
            del self._state[key]
            self.notices.append(
                f"❌失效 {symbol} {tf} 收盘{'跌破' if d == 'long' else '升破'}保护位，解除跟踪")
            return self._drop("invalidated")
        if st["buy2_bars"] > self._p("spring.buy2_window", 60):
            del self._state[key]
            return self._drop("buy2_window")

        # 主力K（可选标注，量超破位K且振幅够）
        if st["main_k"] is None:
            atr_val = atr(klines, self._p("factors.atr_period", 14))
            if is_main_k(klines, i, st["bd"]["vol"], atr_val,
                         self._p("spring.maink_range_atr", 1.2)):
                st["main_k"] = {"time": int(k["open_time"]), "low": l, "high": h}
                self.db.log("info", "spring", f"🚩主力K {symbol} {tf}")

        # 更高低点的底分型
        buf = self._p("signal.sl_buffer_pct", 0.3) / 100.0
        if d == "long":
            if not is_bottom_fractal(klines, i):
                return None
            frac_low = float(klines[i - 1]["low"])
            if frac_low <= st["prot"] or c < float(klines[i - 1]["high"]):
                return None  # 必须更高低点 + 收回过中间K高点
            entry, sl = float(klines[i - 1]["open"]), frac_low * (1 - buf)
        else:
            if not is_top_fractal(klines, i):
                return None
            frac_high = float(klines[i - 1]["high"])
            if frac_high >= st["prot"] or c > float(klines[i - 1]["low"]):
                return None
            entry, sl = float(klines[i - 1]["open"]), frac_high * (1 + buf)

        tag = "(含主力K)" if st["main_k"] else ""
        reason = (f"🔁二买 {'做多' if d == 'long' else '做空'}{tag}: "
                  f"更高低点的底分型，回测不破保护位")
        sig = self._spring_make(symbol, tf, d, entry, sl, "buy2", st["bd"], klines,
                                extra={"breakdown": st["bd"], "main_k": st["main_k"]},
                                reason=reason)
        del self._state[key]   # 一轮结束
        return sig

    # ---------- 组装：止盈=下跌前的顶/密集区 ----------

    def _spring_make(self, symbol, tf, direction, entry, sl, sig_type, bd,
                     klines, *, extra, reason) -> "Signal | None":
        i = len(klines) - 1
        look = self._p("spring.tp_lookback", 100)
        seg = klines[max(0, i - look): i + 1]
        profile = build_profile(seg, self._p("signal.tp_vp_bins", 50))
        risk = abs(entry - sl)
        peak = prior_peak(klines, i, look, direction)
        if direction == "long":
            hvns = [p for p in hvn_list_above(profile, entry) if entry < p <= (peak or 1e18)]
            tp = max(hvns) if hvns else (peak if peak and peak > entry else entry + 2 * risk)
            rr = (tp - entry) / max(risk, 1e-12)
        else:
            hvns = [p for p in hvn_list_below(profile, entry) if (peak or 0) <= p < entry]
            tp = min(hvns) if hvns else (peak if peak and peak < entry else entry - 2 * risk)
            rr = (entry - tp) / max(risk, 1e-12)
        if rr < self._p("spring.min_rr", 1.5):
            return self._drop("rr_too_low")

        equity = self._p("risk.account_equity", 1000)
        risk_usdt = equity * self._p("risk.risk_pct", 0.5) / 100.0
        qty = risk_usdt / risk if risk > 0 else 0.0
        self.funnel[f"signal_{sig_type}"] = self.funnel.get(f"signal_{sig_type}", 0) + 1
        macro_tag = self._macro_tag(direction)
        against = "⚠逆大盘" in macro_tag
        return Signal(
            symbol=symbol, tf=tf, direction=direction, kind="primary",
            entry=round(entry, 8), sl=round(sl, 8), tp=round(tp, 8), rr=round(rr, 2),
            vol_ratio=round(bd["detail"].get("vol_ratio", 0), 2),
            strength="strong" if sig_type == "buy1" else "normal",
            suggested_qty=round(qty, 8), risk_usdt=round(risk_usdt, 2),
            reason=reason + macro_tag, created_at=int(time.time()),
            extra={"type": sig_type, "btc_trend": self.btc_trend,
                   "macro": self.macro_view.get("direction"), "against_macro": against, **extra},
        )

    # ======================= 策略V1: 缠论(保留) =======================

    def _evaluate_chan(self, symbol: str, tf: str, klines: list,
                       klines_15m: list | None = None) -> Signal | None:
        need = max(self._p("signal.vol_ma_period", 20) + 5, 60)
        if len(klines) < need:
            return None
        merged = merge_klines(klines)
        fractals = find_fractals(klines, merged)
        if not fractals:
            return None
        f = fractals[-1]
        last_idx = len(klines) - 1
        if f.confirm_src_idx != last_idx:
            return None

        direction = "long" if f.kind == "bottom" else "short"
        cd_bars = self._p("signal.cooldown_bars", 10)
        keyc = (symbol, tf, direction)
        tf_ms = TF_MS.get(tf, 900) * 1000
        cur_t = int(klines[last_idx]["open_time"])
        if cur_t - self._cooldown.get(keyc, 0) < cd_bars * tf_ms:
            return self._drop("cooldown")

        vr = volume_ratio(klines, f.extreme_src_idx, self._p("signal.vol_ma_period", 20))
        if vr < self._p("signal.vol_multiplier", 1.5):
            return self._drop("volume")
        strength = "strong" if vr >= self._p("signal.vol_strong", 2.0) else "normal"

        support = prior_support(klines, fractals, f, self._p("signal.break_reclaim_lookback", 30))
        if support is None or not is_break_reclaim(klines, f, support):
            return self._drop("break_reclaim")

        if self._p("signal.trend_filter", True) and klines_15m:
            td = trend_direction(klines_15m, self._p("signal.trend_ema_period", 50))
            if (direction == "long" and td == -1) or (direction == "short" and td == 1):
                return self._drop("trend")

        trend_15 = trend_direction(klines_15m, self._p("signal.trend_ema_period", 50)) if klines_15m else 0
        score, hits, factor_detail = score_signal(
            self.cfg, direction=direction, symbol=symbol, tf=tf, klines=klines,
            fractals=fractals, cur=f, confirm_bar=klines[last_idx],
            funding_rate=self.funding.get(symbol), trend_15m=trend_15,
            btc_trend=self.btc_trend)
        if score < self._p("factors.min_score", 0):
            return self._drop("factor_score")

        entry = float(klines[last_idx]["close"])
        atr_val = atr(klines, self._p("factors.atr_period", 14))
        buf = self._p("signal.sl_buffer_pct", 0.3) / 100.0
        profile = build_profile(klines[-self._p("signal.tp_vp_lookback", 200):],
                                self._p("signal.tp_vp_bins", 50))
        if direction == "long":
            sl = f.extreme_price * (1 - buf)
            tp = nearest_hvn_above(profile, entry)
            if tp is None or sl >= entry or tp <= entry:
                return self._drop("no_tp_hvn")
            rr = (tp - entry) / (entry - sl)
        else:
            sl = f.extreme_price * (1 + buf)
            tp = nearest_hvn_below(profile, entry)
            if tp is None or sl <= entry or tp >= entry:
                return self._drop("no_tp_hvn")
            rr = (entry - tp) / (sl - entry)

        ok, _why = sl_atr_sane(entry, sl, atr_val,
                               self._p("factors.sl_atr_min", 0.5), self._p("factors.sl_atr_max", 3.0))
        if not ok:
            return self._drop("atr_sanity")
        rr_sec = self._p("signal.min_rr_secondary", 2.5)
        if rr < rr_sec:
            return self._drop("rr_too_low")
        kind = "primary" if rr >= self._p("signal.min_rr_primary", 5.0) else "secondary"
        self.funnel["passed"] = self.funnel.get("passed", 0) + 1

        equity = self._p("risk.account_equity", 1000)
        risk_usdt = equity * self._p("risk.risk_pct", 0.5) / 100.0
        sl_dist = abs(entry - sl)
        qty = risk_usdt / sl_dist if sl_dist > 0 else 0.0
        self._cooldown[keyc] = cur_t
        reason = (f"{'底' if direction == 'long' else '顶'}分型确认 + 量能{vr:.1f}x均量")
        if hits:
            reason += " | 因子: " + "、".join(hits) + f" (分{score})"
        return Signal(
            symbol=symbol, tf=tf, direction=direction, kind=kind,
            entry=entry, sl=round(sl, 8), tp=round(tp, 8), rr=round(rr, 2),
            vol_ratio=round(vr, 2), strength=strength,
            suggested_qty=round(qty, 8), risk_usdt=round(risk_usdt, 2),
            reason=reason, created_at=int(time.time()),
            extra={"type": "chan", "support": support, "fractal_price": f.extreme_price,
                   "factor_score": score, "factors": hits, "factor_detail": factor_detail,
                   "atr": atr_val, "btc_trend": self.btc_trend},
        )
