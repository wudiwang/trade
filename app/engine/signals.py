"""信号引擎。

默认策略 spring_v3（用户的弹簧策略）：触发巨量K → 收回打分(50观察/100一买)
→ 坐标K跟踪 → 二买/二次弹簧。每个 (币种,级别) 一个状态机。
保留 chan_v1（缠论分型+因子）可通过 strategy 配置切回。
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
    detect_trigger, recovery_score, upper_wick_pct, lower_wick_pct,
    is_bottom_fractal_3, is_top_fractal_3, vol_avg,
)
from .volume_profile import (
    build_profile, hvn_list_above, hvn_list_below,
    nearest_hvn_above, nearest_hvn_below,
)

log = logging.getLogger("signals")

TF_MS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

# 信号类型 → paper统计轨道
SIGNAL_TYPES = ("watch", "buy1", "buy2", "spring")
TYPE_LABEL = {"watch": "⚡观察(50+分)", "buy1": "✅一买(吞没100分)",
              "buy2": "🔁二买(回测确认)", "spring": "💎二次弹簧(假破收回)"}


@dataclass
class Signal:
    symbol: str
    tf: str
    direction: str       # long / short
    kind: str            # primary(推送TG) / secondary
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
        self.btc_trend: int = 0          # BTC 15m聚合1h EMA50 趋势
        self.funnel: dict[str, int] = {}
        # V3 状态机: (symbol, tf) -> state dict
        self._state: dict[tuple, dict] = {}
        # 失效/解除等文字通知回调（engine 转发到 TG）
        self.notices: list[str] = []

    def _p(self, key: str, default=None):
        return self.cfg.get(key, default)

    def _drop(self, stage: str):
        self.funnel[stage] = self.funnel.get(stage, 0) + 1
        return None

    # ======================= 路由 =======================

    def evaluate(self, symbol: str, tf: str, klines: list,
                 klines_15m: list | None = None) -> Signal | None:
        if self._p("strategy", "spring_v3") == "spring_v3":
            return self._eval_spring(symbol, tf, klines)
        return self._evaluate_chan(symbol, tf, klines, klines_15m)

    # ======================= 策略V3 =======================

    def _btc_ok(self, direction: str) -> bool:
        if not self._p("spring.btc_filter", True):
            return True
        return self.btc_trend != (-1 if direction == "long" else 1)

    def _make(self, *, symbol, tf, direction, entry, sl, vol_ratio, reason,
              sig_type, score, extra, klines) -> "Signal | None":
        """组装信号：止盈=第二密集成交区（回测证明第一密集区太近，赢单只有0.59R），
        且预期 RR < spring.min_rr 的信号不进场。"""
        profile = build_profile(
            klines[-self._p("signal.tp_vp_lookback", 200):],
            self._p("signal.tp_vp_bins", 50))
        risk = abs(entry - sl)
        if direction == "long":
            hvns = [p for p in hvn_list_above(profile, entry) if p > entry]
            tp = hvns[1] if len(hvns) >= 2 else (hvns[0] if hvns else None)
            if tp is None:
                tp = entry + 2 * risk   # 上方无密集区(新低区)→2R目标
            rr = (tp - entry) / max(risk, 1e-12)
        else:
            hvns = [p for p in hvn_list_below(profile, entry) if p < entry]
            tp = hvns[1] if len(hvns) >= 2 else (hvns[0] if hvns else None)
            if tp is None:
                tp = entry - 2 * risk
            rr = (entry - tp) / max(risk, 1e-12)
        if rr < self._p("spring.min_rr", 1.5):
            return self._drop("rr_too_low")

        equity = self._p("risk.account_equity", 1000)
        risk_usdt = equity * self._p("risk.risk_pct", 0.5) / 100.0
        sl_dist = abs(entry - sl)
        qty = risk_usdt / sl_dist if sl_dist > 0 else 0.0
        self.funnel[f"signal_{sig_type}"] = self.funnel.get(f"signal_{sig_type}", 0) + 1
        return Signal(
            symbol=symbol, tf=tf, direction=direction, kind="primary",
            entry=entry, sl=round(sl, 8), tp=round(tp, 8), rr=round(rr, 2),
            vol_ratio=round(vol_ratio, 2), strength="strong" if score >= 100 else "normal",
            suggested_qty=round(qty, 8), risk_usdt=round(risk_usdt, 2),
            reason=reason, created_at=int(time.time()),
            extra={"type": sig_type, "score": round(score, 1),
                   "btc_trend": self.btc_trend, **extra},
        )

    def _eval_spring(self, symbol: str, tf: str, klines: list) -> "Signal | None":
        need = max(self._p("spring.newlow_lookback", 50) + 5, 60)
        if len(klines) < need:
            return None
        i = len(klines) - 1
        key = (symbol, tf)
        st = self._state.get(key)
        if st:
            return self._advance_state(key, st, klines, i)
        return self._try_trigger(key, klines, i)

    # ---------- 阶段2: 触发 ----------

    def _try_trigger(self, key, klines, i) -> None:
        symbol, tf = key
        atr_val = atr(klines, self._p("factors.atr_period", 14))
        direction, detail = detect_trigger(
            klines, i, atr_val=atr_val,
            vol_mult=self._p("spring.vol_mult", 3.0),
            vol_max_lookback=self._p("spring.vol_max_lookback", 30),
            body_min=self._p("spring.body_min", 0.5),
            range_atr_min=self._p("spring.range_atr_min", 1.5),
            newlow_lookback=self._p("spring.newlow_lookback", 50),
            quiet_bars=self._p("spring.quiet_bars", 15),
            quiet_mult=self._p("spring.quiet_mult", 1.5),
        )
        if not direction:
            return None
        self.funnel["trigger"] = self.funnel.get("trigger", 0) + 1
        if not self._btc_ok(direction):
            return self._drop("btc_filter")
        k = klines[i]
        self._state[(symbol, tf)] = {
            "phase": "recovery", "direction": direction,
            "trig": {"time": int(k["open_time"]), "open": float(k["open"]),
                     "high": float(k["high"]), "low": float(k["low"]),
                     "close": float(k["close"]), "vol": float(k["volume"]),
                     "detail": detail},
            "bars": 0, "watch_sent": False, "buy1_sent": False,
        }
        self.db.log("info", "spring", f"⚡触发 {symbol} {tf} {direction} 巨量破位K 量{detail['vol_ratio']}x")
        return None  # 触发本身不出信号，只建档观察

    # ---------- 状态推进 ----------

    def _advance_state(self, key, st, klines, i) -> "Signal | None":
        if st["phase"] == "recovery":
            return self._phase_recovery(key, st, klines, i)
        return self._phase_coord(key, st, klines, i)

    # ---------- 阶段3: 收回打分 ----------

    def _phase_recovery(self, key, st, klines, i) -> "Signal | None":
        symbol, tf = key
        d = st["direction"]
        trig = st["trig"]
        k = klines[i]
        o, h, l, c, v = (float(k["open"]), float(k["high"]), float(k["low"]),
                         float(k["close"]), float(k["volume"]))
        st["bars"] += 1

        # 再创极值 → 观察作废，且本K可能是新触发
        if (d == "long" and l < trig["low"]) or (d == "short" and h > trig["high"]):
            del self._state[key]
            self._drop("recovery_newlow")
            return self._try_trigger(key, klines, i)

        # 路径b: 量比触发K更大的反向K → 坐标升级 + 直接按其收盘打分
        bigger = v > trig["vol"] and ((d == "long" and c > o) or (d == "short" and c < o))
        # 路径a: 常规缩量收回（反弹K量必须 < 0.8 x 触发量）
        vol_cap_ok = v < self._p("spring.recovery_vol_max", 0.8) * trig["vol"]

        if not bigger and not vol_cap_ok:
            # 量处于灰色区间：既不算缩量收回也不是坐标升级，跳过本K
            if st["bars"] >= self._p("spring.recovery_bars", 3):
                del self._state[key]
                return self._drop("recovery_timeout")
            return None

        score = recovery_score(d, trig, c)
        coord_k = k if bigger else None

        if score >= 100 and not st["buy1_sent"]:
            sig_type = "buy1"
        elif score >= self._p("spring.watch_score", 50) and not st["watch_sent"] and not st["buy1_sent"]:
            sig_type = "watch"
        else:
            sig_type = None

        if sig_type:
            labels = []
            if bigger:
                labels.append("坐标升级(量超触发K)")
                if (d == "long" and upper_wick_pct(k) >= 0.35) or \
                   (d == "short" and lower_wick_pct(k) >= 0.35):
                    labels.append("长影线(天量换手)")
            if (d == "long" and is_bottom_fractal_3(klines, i)) or \
               (d == "short" and is_top_fractal_3(klines, i)):
                labels.append("分型形态")
            if v < self._p("spring.easy_vol", 0.5) * trig["vol"]:
                labels.append("轻松收回(缩量)")
            if st["bars"] <= 2:
                labels.append(f"第{st['bars']}根完成")

            buf = self._p("signal.sl_buffer_pct", 0.3) / 100.0
            sl = trig["low"] * (1 - buf) if d == "long" else trig["high"] * (1 + buf)
            emoji = "✅一买" if sig_type == "buy1" else "⚡观察"
            reason = (f"{emoji}[{score:.0f}分] {'做多' if d == 'long' else '做空'} "
                      f"巨量破位后{'吞没收回' if score >= 100 else '收回过中点'} | "
                      f"触发量{trig['detail']['vol_ratio']}x | {'、'.join(labels) if labels else '常规收回'}")
            sig = self._make(symbol=symbol, tf=tf, direction=d, entry=c, sl=sl,
                             vol_ratio=v / max(trig["vol"], 1e-12), reason=reason,
                             sig_type=sig_type, score=score,
                             extra={"trigger": trig, "labels": labels,
                                    "coord_upgraded": bool(coord_k)},
                             klines=klines)
            if sig_type == "buy1":
                st["buy1_sent"] = True
            else:
                st["watch_sent"] = True
            # 进入坐标期（坐标K = 升级K 或 触发K）
            ck = coord_k or None
            st.update(phase="coord",
                      coord={"time": int((ck or {}).get("open_time", trig["time"])),
                             "low": float(ck["low"]) if ck else trig["low"],
                             "high": float(ck["high"]) if ck else trig["high"],
                             "close": float(ck["close"]) if ck else trig["close"],
                             "vol": float(ck["volume"]) if ck else trig["vol"]},
                      coord_bars=0, fake_break=0, pulled=False, buy2_sent=False)
            return sig

        if st["bars"] >= self._p("spring.recovery_bars", 3):
            del self._state[key]
            return self._drop("recovery_timeout")
        return None

    # ---------- 阶段4/5: 坐标期（升级/二买/弹簧/失效） ----------

    def _phase_coord(self, key, st, klines, i) -> "Signal | None":
        symbol, tf = key
        d = st["direction"]
        co = st["coord"]
        k = klines[i]
        o, h, l, c, v = (float(k["open"]), float(k["high"]), float(k["low"]),
                         float(k["close"]), float(k["volume"]))
        st["coord_bars"] += 1
        if st["coord_bars"] > self._p("spring.coord_expire_bars", 60):
            del self._state[key]
            return self._drop("coord_expired")

        # 坐标升级：量更大 → 坐标转移
        if v > co["vol"]:
            st["coord"] = {"time": int(k["open_time"]), "low": l, "high": h,
                           "close": c, "vol": v}
            co = st["coord"]
            self.db.log("info", "spring", f"🚩坐标升级 {symbol} {tf} 新坐标量更大")

        breach = (d == "long" and l < co["low"]) or (d == "short" and h > co["high"])
        close_breach = (d == "long" and c < co["low"]) or (d == "short" and c > co["high"])

        if close_breach:
            st["fake_break"] += 1
            if st["fake_break"] > 2:   # 收盘破且2根内没收回 = 实质跌破
                del self._state[key]
                self.notices.append(
                    f"❌失效 {symbol} {tf} 收盘实质跌破坐标K{'低点' if d == 'long' else '高点'}，解除跟踪")
                return self._drop("coord_invalidated")
            return None
        elif breach:
            # 盘中插破但收盘收回 → 武装二次弹簧
            st["spring_armed"] = True
            st["pull_ext"] = l if d == "long" else h
            st["fake_break"] = 0
            return None
        elif st["fake_break"] > 0:
            # 之前收盘破过、本根收盘收回了 → 也是假破收回
            st["fake_break"] = 0
            st["spring_armed"] = True
            st["pull_ext"] = l if d == "long" else h

        # 回测跟踪（缩量回落到坐标K收盘价内侧）
        shrink = v <= self._p("spring.pull_shrink", 0.6) * co["vol"]
        if d == "long" and l <= co["close"] and shrink:
            st["pulled"] = True
            st["pull_ext"] = min(st.get("pull_ext") or l, l)
        if d == "short" and h >= co["close"] and shrink:
            st["pulled"] = True
            st["pull_ext"] = max(st.get("pull_ext") or h, h)

        # 重启K：方向K + 收过前K极值 + 量大于前K
        prev = klines[i - 1]
        if d == "long":
            restart = c > o and c > float(prev["high"]) and v > float(prev["volume"])
        else:
            restart = c < o and c < float(prev["low"]) and v > float(prev["volume"])
        if not restart or st.get("buy2_sent"):
            return None

        armed_spring = st.get("spring_armed", False)
        if not (armed_spring or st.get("pulled")):
            return None

        sig_type = "spring" if armed_spring else "buy2"
        buf = self._p("signal.sl_buffer_pct", 0.3) / 100.0
        ref = st.get("pull_ext") or (co["low"] if d == "long" else co["high"])
        sl = ref * (1 - buf) if d == "long" else ref * (1 + buf)
        vr = v / max(vol_avg(klines, i, 20), 1e-12)
        reason = (f"{'💎二次弹簧' if sig_type == 'spring' else '🔁二买'} "
                  f"{'做多' if d == 'long' else '做空'}: "
                  f"{'假破坐标K后快速收回' if sig_type == 'spring' else '缩量回测坐标K不破'}"
                  f" + 放量重启({vr:.1f}x均量)")
        sig = self._make(symbol=symbol, tf=tf, direction=d, entry=c, sl=sl,
                         vol_ratio=vr, reason=reason, sig_type=sig_type,
                         score=100 if sig_type == "spring" else 80,
                         extra={"coord": co, "trigger": st["trig"],
                                "pull_ext": st.get("pull_ext")},
                         klines=klines)
        st["buy2_sent"] = True
        del self._state[key]   # 一轮完整结束
        return sig

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
