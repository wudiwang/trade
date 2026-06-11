"""信号引擎：K线收盘后逐币种评估，组装买/卖信号。

链路：分型 → 量能放大 → 跌破收回 → 趋势过滤 → 止损/止盈 → RR 门槛 → 仓位建议。
"""
import logging
import time
from dataclasses import dataclass, asdict

from .chan import (
    find_fractals, merge_klines, volume_ratio, prior_support,
    is_break_reclaim, trend_direction,
)
from .volume_profile import build_profile, nearest_hvn_above, nearest_hvn_below

log = logging.getLogger("signals")


@dataclass
class Signal:
    symbol: str
    tf: str
    direction: str       # long / short
    kind: str            # primary / secondary
    entry: float
    sl: float
    tp: float
    rr: float
    vol_ratio: float
    strength: str        # normal / strong
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
        # 冷却: (symbol, tf, direction) -> 最后触发的 open_time
        self._cooldown: dict[tuple, int] = {}

    def _p(self, key: str, default=None):
        return self.cfg.get(key, default)

    def evaluate(self, symbol: str, tf: str, klines: list,
                 klines_15m: list | None = None) -> Signal | None:
        """klines: 该币该级别已收盘K线（升序，最后一根=刚收盘的）。
        只在最新分型的确认K == 最后一根K时出信号（实时触发，不翻旧账）。"""
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
            return None  # 分型尚未确认在本根收盘

        direction = "long" if f.kind == "bottom" else "short"

        # 冷却
        cd_bars = self._p("signal.cooldown_bars", 10)
        key = (symbol, tf, direction)
        tf_ms = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}.get(tf, 900) * 1000
        last_t = self._cooldown.get(key, 0)
        cur_t = int(klines[last_idx]["open_time"])
        if cur_t - last_t < cd_bars * tf_ms:
            return None

        # 量能放大（分型极值K）
        vr = volume_ratio(klines, f.extreme_src_idx, self._p("signal.vol_ma_period", 20))
        vol_min = self._p("signal.vol_multiplier", 1.5)
        if vr < vol_min:
            return None
        strength = "strong" if vr >= self._p("signal.vol_strong", 2.0) else "normal"

        # 跌破收回 / 冲高回落
        support = prior_support(klines, fractals, f, self._p("signal.break_reclaim_lookback", 30))
        if support is None or not is_break_reclaim(klines, f, support):
            return None

        # 趋势过滤（用15m聚合1h EMA50；short 要求趋势向下，long 向上）
        if self._p("signal.trend_filter", True) and klines_15m:
            td = trend_direction(klines_15m, self._p("signal.trend_ema_period", 50))
            if direction == "long" and td == -1:
                return None
            if direction == "short" and td == 1:
                return None

        entry = float(klines[last_idx]["close"])
        buf = self._p("signal.sl_buffer_pct", 0.3) / 100.0
        profile = build_profile(
            klines[-self._p("signal.tp_vp_lookback", 200):],
            self._p("signal.tp_vp_bins", 50),
        )
        if direction == "long":
            sl = f.extreme_price * (1 - buf)
            tp = nearest_hvn_above(profile, entry)
            if tp is None or sl >= entry or tp <= entry:
                return None
            rr = (tp - entry) / (entry - sl)
        else:
            sl = f.extreme_price * (1 + buf)
            tp = nearest_hvn_below(profile, entry)
            if tp is None or sl <= entry or tp >= entry:
                return None
            rr = (entry - tp) / (sl - entry)

        rr_pri = self._p("signal.min_rr_primary", 5.0)
        rr_sec = self._p("signal.min_rr_secondary", 2.5)
        if rr < rr_sec:
            return None
        kind = "primary" if rr >= rr_pri else "secondary"

        # 仓位：单笔风险 = 账户 * risk_pct
        equity = self._p("risk.account_equity", 1000)
        risk_usdt = equity * self._p("risk.risk_pct", 0.5) / 100.0
        sl_dist = abs(entry - sl)
        qty = risk_usdt / sl_dist if sl_dist > 0 else 0.0

        self._cooldown[key] = cur_t
        reason = (
            f"{'底' if direction == 'long' else '顶'}分型确认 + "
            f"量能{vr:.1f}x均量 + "
            f"{'跌破前低' + format(support, '.6g') + '后收回' if direction == 'long' else '冲破前高' + format(support, '.6g') + '后回落'}"
        )
        return Signal(
            symbol=symbol, tf=tf, direction=direction, kind=kind,
            entry=entry, sl=round(sl, 8), tp=round(tp, 8), rr=round(rr, 2),
            vol_ratio=round(vr, 2), strength=strength,
            suggested_qty=round(qty, 8), risk_usdt=round(risk_usdt, 2),
            reason=reason, created_at=int(time.time()),
            extra={
                "support": support,
                "fractal_price": f.extreme_price,
                "fractal_open_time": f.open_time,
            },
        )
