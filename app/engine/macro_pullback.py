"""Manual BTC macro direction -> Wyckoff first entry -> Chan second entry."""
import time


def _f(k, name: str) -> float:
    return float(k[name])


def _avg(nums: list[float]) -> float:
    return sum(nums) / len(nums) if nums else 0.0


def _vol_ratio(klines: list, idx: int, period: int) -> float:
    if idx < period:
        return 0.0
    base = _avg([_f(k, "volume") for k in klines[idx - period:idx]])
    return _f(klines[idx], "volume") / base if base > 0 else 0.0


def _is_bottom(klines: list, idx: int) -> bool:
    if idx <= 0 or idx >= len(klines) - 1:
        return False
    return _f(klines[idx], "low") < _f(klines[idx - 1], "low") and _f(klines[idx], "low") < _f(klines[idx + 1], "low")


def _is_top(klines: list, idx: int) -> bool:
    if idx <= 0 or idx >= len(klines) - 1:
        return False
    return _f(klines[idx], "high") > _f(klines[idx - 1], "high") and _f(klines[idx], "high") > _f(klines[idx + 1], "high")


def _bottoms_after(klines: list, start: int) -> list[int]:
    return [i for i in range(max(1, start), len(klines) - 1) if _is_bottom(klines, i)]


def _tops_after(klines: list, start: int) -> list[int]:
    return [i for i in range(max(1, start), len(klines) - 1) if _is_top(klines, i)]


def _effective_bar_count(klines: list, start_idx: int, end_idx: int) -> int:
    if end_idx < start_idx:
        start_idx, end_idx = end_idx, start_idx
    from .chan import merge_klines
    return len(merge_klines(klines[start_idx:end_idx + 1]))


def _is_chan_fractal_extreme(klines: list, idx: int, kind: str) -> bool:
    from .chan import find_fractals, merge_klines
    for fx in find_fractals(klines, merge_klines(klines)):
        if fx.kind == kind and int(fx.extreme_src_idx) == idx:
            return True
    return False


def _invalidated_extreme(klines: list, idx: int, direction: str, tolerance_pct: float = 0.0) -> bool:
    if direction == "long":
        base = _f(klines[idx], "low") * (1 - tolerance_pct / 100.0)
        return any(_f(k, "low") < base for k in klines[idx + 1:])
    base = _f(klines[idx], "high") * (1 + tolerance_pct / 100.0)
    return any(_f(k, "high") > base for k in klines[idx + 1:])


def _body_reclaim_level(kline: dict, direction: str, pct: float) -> float:
    body_low = min(_f(kline, "open"), _f(kline, "close"))
    body_high = max(_f(kline, "open"), _f(kline, "close"))
    body = body_high - body_low
    if direction == "long":
        return body_low + body * pct
    return body_high - body * pct


def _find_spring(klines: list, params: dict) -> dict | None:
    lookback = int(params.get("lookback", 20))
    vol_ma = int(params.get("vol_ma", 20))
    vol_mult = float(params.get("vol_mult", 3.0))
    reclaim_bars = int(params.get("reclaim_bars", 4))
    body_pct = float(params.get("reclaim_body_pct", 80)) / 100.0
    end = len(klines) - 3
    for i in range(end - 1, vol_ma - 1, -1):
        start = max(0, i - lookback)
        if i - start < 3:
            continue
        prior_low = min(_f(k, "low") for k in klines[start:i])
        if _f(klines[i], "low") >= prior_low:
            continue
        vr = _vol_ratio(klines, i, vol_ma)
        if vr < vol_mult:
            continue
        reclaim_level = _body_reclaim_level(klines[i], "long", body_pct)
        reclaim_end = min(len(klines) - 1, i + reclaim_bars)
        reclaimed_at = None
        for j in range(i + 1, reclaim_end + 1):
            if _f(klines[j], "close") >= reclaim_level:
                reclaimed_at = j
                break
        if reclaimed_at is None:
            continue
        bottom_candidates = [x for x in range(max(1, i - 1), min(len(klines) - 1, i + 2)) if _is_bottom(klines, x)]
        if not bottom_candidates:
            continue
        l1 = min(bottom_candidates, key=lambda x: _f(klines[x], "low"))
        if not _is_chan_fractal_extreme(klines, l1, "bottom"):
            continue
        if _invalidated_extreme(klines, l1, "long"):
            continue
        return {
            "kind": "spring", "idx": l1, "sweep_idx": i, "reclaimed_at": reclaimed_at,
            "level": prior_low, "reclaim_level": round(reclaim_level, 8), "vol_ratio": round(vr, 2),
        }
    return None


def _find_utad(klines: list, params: dict) -> dict | None:
    lookback = int(params.get("lookback", 20))
    vol_ma = int(params.get("vol_ma", 20))
    vol_mult = float(params.get("vol_mult", 3.0))
    reclaim_bars = int(params.get("reclaim_bars", 4))
    body_pct = float(params.get("reclaim_body_pct", 80)) / 100.0
    end = len(klines) - 3
    for i in range(end - 1, vol_ma - 1, -1):
        start = max(0, i - lookback)
        if i - start < 3:
            continue
        prior_high = max(_f(k, "high") for k in klines[start:i])
        if _f(klines[i], "high") <= prior_high:
            continue
        vr = _vol_ratio(klines, i, vol_ma)
        if vr < vol_mult:
            continue
        reclaim_level = _body_reclaim_level(klines[i], "short", body_pct)
        reclaim_end = min(len(klines) - 1, i + reclaim_bars)
        reclaimed_at = None
        for j in range(i + 1, reclaim_end + 1):
            if _f(klines[j], "close") <= reclaim_level:
                reclaimed_at = j
                break
        if reclaimed_at is None:
            continue
        top_candidates = [x for x in range(max(1, i - 1), min(len(klines) - 1, i + 2)) if _is_top(klines, x)]
        if not top_candidates:
            continue
        h1 = max(top_candidates, key=lambda x: _f(klines[x], "high"))
        if not _is_chan_fractal_extreme(klines, h1, "top"):
            continue
        if _invalidated_extreme(klines, h1, "short"):
            continue
        return {
            "kind": "utad", "idx": h1, "sweep_idx": i, "reclaimed_at": reclaimed_at,
            "level": prior_high, "reclaim_level": round(reclaim_level, 8), "vol_ratio": round(vr, 2),
        }
    return None


def _long_second(klines: list, first: dict, params: dict) -> dict | None:
    l1_idx = int(first["idx"])
    l1 = _f(klines[l1_idx], "low")
    min_leg = float(params.get("min_leg_pct", 0.8)) / 100.0
    tol = float(params.get("second_tolerance_pct", 0.2)) / 100.0
    min_bars = int(params.get("min_effective_bars_between", 5))
    bottoms = _bottoms_after(klines, l1_idx + 3)
    for l2_idx in bottoms:
        if _f(klines[l2_idx], "low") < l1 * (1 - tol):
            continue
        leg_high_idx = max(range(l1_idx + 1, l2_idx + 1), key=lambda x: _f(klines[x], "high"))
        leg_high = _f(klines[leg_high_idx], "high")
        if _effective_bar_count(klines, l1_idx, leg_high_idx) < min_bars:
            continue
        if _effective_bar_count(klines, leg_high_idx, l2_idx) < min_bars:
            continue
        if (leg_high - l1) / max(l1, 1e-12) < min_leg:
            continue
        return {"L1": l1, "H1": leg_high, "L2": _f(klines[l2_idx], "low"),
                "L1_time": int(klines[l1_idx]["open_time"]), "L2_time": int(klines[l2_idx]["open_time"]),
                "L1_idx": l1_idx, "H1_idx": leg_high_idx, "L2_idx": l2_idx}
    return None


def _short_second(klines: list, first: dict, params: dict) -> dict | None:
    h1_idx = int(first["idx"])
    h1 = _f(klines[h1_idx], "high")
    min_leg = float(params.get("min_leg_pct", 0.8)) / 100.0
    tol = float(params.get("second_tolerance_pct", 0.2)) / 100.0
    min_bars = int(params.get("min_effective_bars_between", 5))
    tops = _tops_after(klines, h1_idx + 3)
    for h2_idx in tops:
        if _f(klines[h2_idx], "high") > h1 * (1 + tol):
            continue
        leg_low_idx = min(range(h1_idx + 1, h2_idx + 1), key=lambda x: _f(klines[x], "low"))
        leg_low = _f(klines[leg_low_idx], "low")
        if _effective_bar_count(klines, h1_idx, leg_low_idx) < min_bars:
            continue
        if _effective_bar_count(klines, leg_low_idx, h2_idx) < min_bars:
            continue
        if (h1 - leg_low) / max(h1, 1e-12) < min_leg:
            continue
        return {"H1": h1, "L1": leg_low, "H2": _f(klines[h2_idx], "high"),
                "H1_time": int(klines[h1_idx]["open_time"]), "H2_time": int(klines[h2_idx]["open_time"]),
                "H1_idx": h1_idx, "L1_idx": leg_low_idx, "H2_idx": h2_idx}
    return None


def _stall_entry_idx(direction: str, klines: list, second: dict, params: dict) -> int | None:
    max_bars = int(params.get("max_signal_bars_after_second", 2))
    last_idx = len(klines) - 1
    if direction == "long":
        second_idx = int(second["L2_idx"])
        right_idx = second_idx + 1
        stall_idx = right_idx + 1
        entry_idx = stall_idx + 1
        if entry_idx >= len(klines) or last_idx != entry_idx:
            return None
        if stall_idx - second_idx > max_bars:
            return None
        return entry_idx if _f(klines[stall_idx], "close") > _f(klines[right_idx], "high") else None
    second_idx = int(second["H2_idx"])
    right_idx = second_idx + 1
    stall_idx = right_idx + 1
    entry_idx = stall_idx + 1
    if entry_idx >= len(klines) or last_idx != entry_idx:
        return None
    if stall_idx - second_idx > max_bars:
        return None
    return entry_idx if _f(klines[stall_idx], "close") < _f(klines[right_idx], "low") else None


def _entry_near_second(direction: str, klines: list, second: dict, entry: float, sl: float, params: dict) -> bool:
    max_bars = int(params.get("max_signal_bars_after_second", 2))
    max_r = float(params.get("max_entry_distance_r", 0.3))
    max_pct = float(params.get("max_entry_distance_pct", 0.5)) / 100.0
    midpoint_filter = bool(params.get("missed_midpoint_filter", True))

    if direction == "long":
        second_idx = int(second.get("L2_idx", len(klines) - 1))
        second_price = float(second["L2"])
        midpoint = (float(second["L1"]) + float(second["H1"])) / 2.0
        missed_midpoint = entry >= midpoint
    else:
        second_idx = int(second.get("H2_idx", len(klines) - 1))
        second_price = float(second["H2"])
        midpoint = (float(second["H1"]) + float(second["L1"])) / 2.0
        missed_midpoint = entry <= midpoint

    freshness_idx = int(second.get("stall_idx", len(klines) - 1))
    if freshness_idx - second_idx > max_bars:
        return False
    if midpoint_filter and missed_midpoint:
        return False

    distance = abs(entry - second_price)
    risk = abs(entry - sl)
    near_by_r = risk > 0 and distance <= max_r * risk
    near_by_pct = second_price > 0 and distance / second_price <= max_pct
    return near_by_r or near_by_pct


def _tp_for(klines: list, direction: str, entry: float, sl: float, params: dict) -> tuple[float, float]:
    risk = abs(entry - sl)
    if direction == "long":
        rr = float(params.get("tp_rr_long", 2.0))
        tp = entry + rr * risk
    else:
        rr = float(params.get("tp_rr_short", 0.8))
        tp = entry - rr * risk
    return tp, rr


def _structure_markers(klines: list, direction: str, first: dict, second: dict) -> list[dict]:
    sweep_idx = int(first["sweep_idx"])
    sweep_time = int(klines[sweep_idx]["open_time"])
    if direction == "long":
        return [
            {"key": "first_fractal", "label": "L1底分型", "time": int(second["L1_time"]),
             "price": float(second["L1"]), "position": "belowBar"},
            {"key": "volume_sweep", "label": "爆量K", "time": sweep_time,
             "price": float(klines[sweep_idx]["low"]), "position": "belowBar",
             "vol_ratio": float(first["vol_ratio"])},
            {"key": "second_fractal", "label": "L2底分型", "time": int(second["L2_time"]),
             "price": float(second["L2"]), "position": "belowBar"},
        ]
    return [
        {"key": "first_fractal", "label": "H1顶分型", "time": int(second["H1_time"]),
         "price": float(second["H1"]), "position": "aboveBar"},
        {"key": "volume_sweep", "label": "爆量K", "time": sweep_time,
         "price": float(klines[sweep_idx]["high"]), "position": "aboveBar",
         "vol_ratio": float(first["vol_ratio"])},
        {"key": "second_fractal", "label": "H2顶分型", "time": int(second["H2_time"]),
         "price": float(second["H2"]), "position": "aboveBar"},
    ]


def detect_macro_pullback(symbol: str, macro_direction: str, struct_klines: list,
                          trigger_klines: list, params: dict):
    klines = trigger_klines or struct_klines
    if not params.get("enabled", True) or macro_direction not in ("long", "short") or len(klines) < 8:
        return None

    if macro_direction == "long":
        first = _find_spring(klines, params)
        second = _long_second(klines, first, params) if first else None
        if not first or not second:
            return None
        entry_idx = _stall_entry_idx("long", klines, second, params)
        if entry_idx is None:
            return None
        second["entry_idx"] = entry_idx
        second["stall_idx"] = entry_idx - 1
        second["stall_time"] = int(klines[entry_idx - 1]["open_time"])
        second["entry_time"] = int(klines[entry_idx]["open_time"])
        direction = "long"
        entry = _f(klines[entry_idx], "close")
        sl = second["L2"] * (1 - float(params.get("stop_buffer_pct", 0.3)) / 100.0)
        if sl >= entry:
            return None
        if not _entry_near_second(direction, klines, second, entry, sl, params):
            return None
        label = "second_buy"
        side = "做多"
    else:
        first = _find_utad(klines, params)
        second = _short_second(klines, first, params) if first else None
        if not first or not second:
            return None
        entry_idx = _stall_entry_idx("short", klines, second, params)
        if entry_idx is None:
            return None
        second["entry_idx"] = entry_idx
        second["stall_idx"] = entry_idx - 1
        second["stall_time"] = int(klines[entry_idx - 1]["open_time"])
        second["entry_time"] = int(klines[entry_idx]["open_time"])
        direction = "short"
        entry = _f(klines[entry_idx], "close")
        sl = second["H2"] * (1 + float(params.get("stop_buffer_pct", 0.3)) / 100.0)
        if sl <= entry:
            return None
        if not _entry_near_second(direction, klines, second, entry, sl, params):
            return None
        label = "second_sell"
        side = "做空"

    tp, rr = _tp_for(klines, direction, entry, sl, params)

    from .signals import Signal

    risk = abs(entry - sl)
    risk_usdt = float(params.get("account_equity", 1000)) * float(params.get("risk_pct", 0.5)) / 100.0
    qty = risk_usdt / risk if risk > 0 else 0.0
    tf = str(params.get("tf") or params.get("structure_tf") or "15m")
    reason = (
        f"反转战法{macro_direction} | {tf}威科夫{first['kind']}一{'买' if direction == 'long' else '卖'}"
        f"后缠论{'二买' if direction == 'long' else '二卖'} {side}"
    )
    return Signal(
        symbol=symbol, tf=tf, direction=direction, kind="primary",
        entry=round(entry, 8), sl=round(sl, 8), tp=round(tp, 8), rr=round(rr, 2),
        vol_ratio=float(first["vol_ratio"]), strength="strong",
        suggested_qty=round(qty, 8), risk_usdt=round(risk_usdt, 2),
        reason=reason, created_at=int(time.time()),
        extra={
            "path": "macro_chan_pullback",
            "type": label,
            "wyckoff": first,
            "structure": second,
            "markers": _structure_markers(klines, direction, first, second),
            "macro": macro_direction,
        },
    )
