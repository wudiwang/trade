"""Manual BTC macro direction -> altcoin second-buy/second-sell pullback signals."""
import time

from .chan import ema
from .volume_profile import build_profile, nearest_hvn_above, nearest_hvn_below


def _f(k, name: str) -> float:
    return float(k[name])


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _slice_avg_volume(klines: list, start: int, end: int) -> float:
    if end < start:
        return 0.0
    return _avg([_f(k, "volume") for k in klines[start:end + 1]])


def _ma_extension_ok(klines: list, idx: int, direction: str, period: int, min_pct: float) -> bool:
    if period <= 1 or len(klines[:idx + 1]) < period:
        return True
    closes = [_f(k, "close") for k in klines[:idx + 1]]
    e = ema(closes, period)[-1]
    if e <= 0:
        return True
    price = _f(klines[idx], "high" if direction == "short" else "low")
    if direction == "short":
        return (price - e) / e * 100.0 >= min_pct
    return (e - price) / e * 100.0 >= min_pct


def _bearish_trigger(trigger_klines: list) -> str | None:
    if len(trigger_klines) < 3:
        return None
    a, b, c = trigger_klines[-3], trigger_klines[-2], trigger_klines[-1]
    if _f(b, "high") > _f(a, "high") and _f(b, "high") > _f(c, "high") and _f(c, "close") < _f(b, "low"):
        return "5m_top_fractal"
    lows = [_f(k, "low") for k in trigger_klines[-5:-1]]
    if lows and _f(c, "close") < min(lows):
        return "5m_range_breakdown"
    if _f(c, "close") < _f(c, "open") and _f(c, "close") < _f(b, "close"):
        return "5m_bearish_close"
    return None


def _bullish_trigger(trigger_klines: list) -> str | None:
    if len(trigger_klines) < 3:
        return None
    a, b, c = trigger_klines[-3], trigger_klines[-2], trigger_klines[-1]
    if _f(b, "low") < _f(a, "low") and _f(b, "low") < _f(c, "low") and _f(c, "close") > _f(b, "high"):
        return "5m_bottom_fractal"
    highs = [_f(k, "high") for k in trigger_klines[-5:-1]]
    if highs and _f(c, "close") > max(highs):
        return "5m_range_breakout"
    if _f(c, "close") > _f(c, "open") and _f(c, "close") > _f(b, "close"):
        return "5m_bullish_close"
    return None


def _short_setup(struct_klines: list, trigger_klines: list, params: dict) -> dict | None:
    win = int(params.get("impulse_window", 24))
    ks = struct_klines[-win:] if len(struct_klines) > win else struct_klines
    if len(ks) < 8:
        return None
    search_end = max(2, len(ks) - 3)
    h1_idx = max(range(search_end), key=lambda i: _f(ks[i], "high"))
    if h1_idx < 2 or h1_idx >= len(ks) - 4:
        return None
    pre_low = min(_f(k, "low") for k in ks[:h1_idx + 1])
    h1 = _f(ks[h1_idx], "high")
    impulse_pct = (h1 - pre_low) / max(pre_low, 1e-12) * 100.0
    if impulse_pct < float(params.get("impulse_min_pct", 4.0)):
        return None
    if not _ma_extension_ok(ks, h1_idx, "short", int(params.get("ma_period", 20)),
                            float(params.get("ma_extension_pct", 1.5))):
        return None
    l1_rel = min(range(h1_idx + 1, len(ks)), key=lambda i: _f(ks[i], "low"))
    if l1_rel >= len(ks) - 2:
        return None
    h2_rel = max(range(l1_rel + 1, len(ks)), key=lambda i: _f(ks[i], "high"))
    h2 = _f(ks[h2_rel], "high")
    tol = float(params.get("retest_tolerance_pct", 0.4)) / 100.0
    if h2 >= h1 * (1 + tol):
        return None
    imp_vol = _slice_avg_volume(ks, max(0, h1_idx - 5), h1_idx)
    reb_vol = _slice_avg_volume(ks, l1_rel + 1, h2_rel)
    if imp_vol > 0 and reb_vol > imp_vol * float(params.get("volume_decay_ratio", 0.8)):
        return None
    trigger = _bearish_trigger(trigger_klines)
    if not trigger:
        return None
    entry = _f(trigger_klines[-1], "close")
    sl = h2 * (1 + float(params.get("stop_buffer_pct", 0.3)) / 100.0)
    risk = sl - entry
    if risk <= 0:
        return None
    profile = build_profile(ks[-int(params.get("tp_lookback", 100)):], int(params.get("vp_bins", 50)))
    hvn = nearest_hvn_below(profile, entry)
    tp = hvn if hvn and hvn < entry else entry - 2 * risk
    rr = (entry - tp) / risk
    if rr < float(params.get("min_rr", 1.5)):
        tp = entry - max(float(params.get("min_rr", 1.5)), 2.0) * risk
        rr = (entry - tp) / risk
    return {
        "direction": "short", "entry": entry, "sl": sl, "tp": tp, "rr": rr,
        "trigger": trigger, "vol_ratio": round(reb_vol / imp_vol, 3) if imp_vol > 0 else 0.0,
        "structure": {
            "H1": h1, "L1": _f(ks[l1_rel], "low"), "H2": h2,
            "H1_time": int(ks[h1_idx]["open_time"]),
            "L1_time": int(ks[l1_rel]["open_time"]),
            "H2_time": int(ks[h2_rel]["open_time"]),
            "impulse_pct": round(impulse_pct, 3),
        },
    }


def _long_setup(struct_klines: list, trigger_klines: list, params: dict) -> dict | None:
    win = int(params.get("impulse_window", 24))
    ks = struct_klines[-win:] if len(struct_klines) > win else struct_klines
    if len(ks) < 8:
        return None
    search_end = max(2, len(ks) - 3)
    l1_idx = min(range(search_end), key=lambda i: _f(ks[i], "low"))
    if l1_idx < 2 or l1_idx >= len(ks) - 4:
        return None
    pre_high = max(_f(k, "high") for k in ks[:l1_idx + 1])
    l1 = _f(ks[l1_idx], "low")
    impulse_pct = (pre_high - l1) / max(pre_high, 1e-12) * 100.0
    if impulse_pct < float(params.get("impulse_min_pct", 4.0)):
        return None
    if not _ma_extension_ok(ks, l1_idx, "long", int(params.get("ma_period", 20)),
                            float(params.get("ma_extension_pct", 1.5))):
        return None
    h1_rel = max(range(l1_idx + 1, len(ks)), key=lambda i: _f(ks[i], "high"))
    if h1_rel >= len(ks) - 2:
        return None
    l2_rel = min(range(h1_rel + 1, len(ks)), key=lambda i: _f(ks[i], "low"))
    l2 = _f(ks[l2_rel], "low")
    tol = float(params.get("retest_tolerance_pct", 0.4)) / 100.0
    if l2 <= l1 * (1 - tol):
        return None
    imp_vol = _slice_avg_volume(ks, max(0, l1_idx - 5), l1_idx)
    pb_vol = _slice_avg_volume(ks, h1_rel + 1, l2_rel)
    if imp_vol > 0 and pb_vol > imp_vol * float(params.get("volume_decay_ratio", 0.8)):
        return None
    trigger = _bullish_trigger(trigger_klines)
    if not trigger:
        return None
    entry = _f(trigger_klines[-1], "close")
    sl = l2 * (1 - float(params.get("stop_buffer_pct", 0.3)) / 100.0)
    risk = entry - sl
    if risk <= 0:
        return None
    profile = build_profile(ks[-int(params.get("tp_lookback", 100)):], int(params.get("vp_bins", 50)))
    hvn = nearest_hvn_above(profile, entry)
    tp = hvn if hvn and hvn > entry else entry + 2 * risk
    rr = (tp - entry) / risk
    if rr < float(params.get("min_rr", 1.5)):
        tp = entry + max(float(params.get("min_rr", 1.5)), 2.0) * risk
        rr = (tp - entry) / risk
    return {
        "direction": "long", "entry": entry, "sl": sl, "tp": tp, "rr": rr,
        "trigger": trigger, "vol_ratio": round(pb_vol / imp_vol, 3) if imp_vol > 0 else 0.0,
        "structure": {
            "L1": l1, "H1": _f(ks[h1_rel], "high"), "L2": l2,
            "L1_time": int(ks[l1_idx]["open_time"]),
            "H1_time": int(ks[h1_rel]["open_time"]),
            "L2_time": int(ks[l2_rel]["open_time"]),
            "impulse_pct": round(impulse_pct, 3),
        },
    }


def detect_macro_pullback(symbol: str, macro_direction: str, struct_klines: list,
                          trigger_klines: list, params: dict):
    if not params.get("enabled", True) or macro_direction not in ("long", "short"):
        return None
    setup = (_short_setup(struct_klines, trigger_klines, params)
             if macro_direction == "short"
             else _long_setup(struct_klines, trigger_klines, params))
    if not setup:
        return None

    from .signals import Signal

    equity = float(params.get("account_equity", 1000))
    risk_pct = float(params.get("risk_pct", 0.5))
    risk_usdt = equity * risk_pct / 100.0
    risk = abs(setup["entry"] - setup["sl"])
    qty = risk_usdt / risk if risk > 0 else 0.0
    label = "second_sell" if setup["direction"] == "short" else "second_buy"
    side = "做空" if setup["direction"] == "short" else "做多"
    reason = (
        f"BTC手动{macro_direction} | 15m类{'二卖' if setup['direction'] == 'short' else '二买'}"
        f" + {setup['trigger']}触发 {side}"
    )
    return Signal(
        symbol=symbol, tf=str(params.get("structure_tf", "15m")),
        direction=setup["direction"], kind="primary",
        entry=round(setup["entry"], 8), sl=round(setup["sl"], 8), tp=round(setup["tp"], 8),
        rr=round(setup["rr"], 2), vol_ratio=setup["vol_ratio"], strength="normal",
        suggested_qty=round(qty, 8), risk_usdt=round(risk_usdt, 2),
        reason=reason, created_at=int(time.time()),
        extra={
            "path": "macro_chan_pullback",
            "type": label,
            "trigger": setup["trigger"],
            "structure": setup["structure"],
            "macro": macro_direction,
        },
    )
