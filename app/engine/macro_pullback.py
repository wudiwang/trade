"""Manual BTC macro direction -> Wyckoff first entry -> Chan second entry."""
import time

from .volume_profile import build_profile, nearest_hvn_above, nearest_hvn_below


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


def _find_spring(klines: list, params: dict) -> dict | None:
    lookback = int(params.get("lookback", 20))
    vol_ma = int(params.get("vol_ma", 20))
    vol_mult = float(params.get("vol_mult", 3.0))
    reclaim_bars = int(params.get("reclaim_bars", 4))
    tol = float(params.get("reclaim_tolerance_pct", 0.5)) / 100.0
    end = len(klines) - 3
    for i in range(vol_ma, end):
        start = max(0, i - lookback)
        if i - start < 3:
            continue
        prior_low = min(_f(k, "low") for k in klines[start:i])
        if _f(klines[i], "low") >= prior_low:
            continue
        vr = _vol_ratio(klines, i, vol_ma)
        if vr < vol_mult:
            continue
        down_start = max(_f(k, "high") for k in klines[start:i])
        reclaim_end = min(len(klines) - 1, i + reclaim_bars)
        reclaimed_at = None
        for j in range(i + 1, reclaim_end + 1):
            if _f(klines[j], "close") >= down_start * (1 - tol):
                reclaimed_at = j
                break
        if reclaimed_at is None:
            continue
        bottom_candidates = [x for x in range(max(1, i - 1), min(len(klines) - 1, i + 2)) if _is_bottom(klines, x)]
        if not bottom_candidates:
            continue
        l1 = min(bottom_candidates, key=lambda x: _f(klines[x], "low"))
        return {
            "kind": "spring", "idx": l1, "sweep_idx": i, "reclaimed_at": reclaimed_at,
            "level": prior_low, "start_price": down_start, "vol_ratio": round(vr, 2),
        }
    return None


def _find_utad(klines: list, params: dict) -> dict | None:
    lookback = int(params.get("lookback", 20))
    vol_ma = int(params.get("vol_ma", 20))
    vol_mult = float(params.get("vol_mult", 3.0))
    reclaim_bars = int(params.get("reclaim_bars", 4))
    tol = float(params.get("reclaim_tolerance_pct", 0.5)) / 100.0
    end = len(klines) - 3
    for i in range(vol_ma, end):
        start = max(0, i - lookback)
        if i - start < 3:
            continue
        prior_high = max(_f(k, "high") for k in klines[start:i])
        if _f(klines[i], "high") <= prior_high:
            continue
        vr = _vol_ratio(klines, i, vol_ma)
        if vr < vol_mult:
            continue
        up_start = min(_f(k, "low") for k in klines[start:i])
        reclaim_end = min(len(klines) - 1, i + reclaim_bars)
        reclaimed_at = None
        for j in range(i + 1, reclaim_end + 1):
            if _f(klines[j], "close") <= up_start * (1 + tol):
                reclaimed_at = j
                break
        if reclaimed_at is None:
            continue
        top_candidates = [x for x in range(max(1, i - 1), min(len(klines) - 1, i + 2)) if _is_top(klines, x)]
        if not top_candidates:
            continue
        h1 = max(top_candidates, key=lambda x: _f(klines[x], "high"))
        return {
            "kind": "utad", "idx": h1, "sweep_idx": i, "reclaimed_at": reclaimed_at,
            "level": prior_high, "start_price": up_start, "vol_ratio": round(vr, 2),
        }
    return None


def _long_second(klines: list, first: dict, params: dict) -> dict | None:
    l1_idx = int(first["idx"])
    l1 = _f(klines[l1_idx], "low")
    min_leg = float(params.get("min_leg_pct", 0.8)) / 100.0
    tol = float(params.get("second_tolerance_pct", 0.2)) / 100.0
    bottoms = _bottoms_after(klines, l1_idx + 3)
    for l2_idx in bottoms:
        if _f(klines[l2_idx], "low") < l1 * (1 - tol):
            continue
        leg_high = max(_f(k, "high") for k in klines[l1_idx + 1:l2_idx + 1])
        if (leg_high - l1) / max(l1, 1e-12) < min_leg:
            continue
        return {"L1": l1, "H1": leg_high, "L2": _f(klines[l2_idx], "low"),
                "L1_time": int(klines[l1_idx]["open_time"]), "L2_time": int(klines[l2_idx]["open_time"])}
    return None


def _short_second(klines: list, first: dict, params: dict) -> dict | None:
    h1_idx = int(first["idx"])
    h1 = _f(klines[h1_idx], "high")
    min_leg = float(params.get("min_leg_pct", 0.8)) / 100.0
    tol = float(params.get("second_tolerance_pct", 0.2)) / 100.0
    tops = _tops_after(klines, h1_idx + 3)
    for h2_idx in tops:
        if _f(klines[h2_idx], "high") > h1 * (1 + tol):
            continue
        leg_low = min(_f(k, "low") for k in klines[h1_idx + 1:h2_idx + 1])
        if (h1 - leg_low) / max(h1, 1e-12) < min_leg:
            continue
        return {"H1": h1, "L1": leg_low, "H2": _f(klines[h2_idx], "high"),
                "H1_time": int(klines[h1_idx]["open_time"]), "H2_time": int(klines[h2_idx]["open_time"])}
    return None


def _tp_for(klines: list, direction: str, entry: float, sl: float, params: dict) -> tuple[float, float]:
    risk = abs(entry - sl)
    profile = build_profile(klines[-int(params.get("tp_lookback", 100)):], int(params.get("vp_bins", 50)))
    min_rr = float(params.get("min_rr", 1.5))
    fallback_rr = max(min_rr, 2.0)
    if direction == "long":
        hvn = nearest_hvn_above(profile, entry)
        tp = hvn if hvn and hvn > entry else entry + fallback_rr * risk
        rr = (tp - entry) / max(risk, 1e-12)
        if rr < min_rr:
            tp = entry + fallback_rr * risk
            rr = (tp - entry) / max(risk, 1e-12)
    else:
        hvn = nearest_hvn_below(profile, entry)
        tp = hvn if hvn and hvn < entry else entry - fallback_rr * risk
        rr = (entry - tp) / max(risk, 1e-12)
        if rr < min_rr:
            tp = entry - fallback_rr * risk
            rr = (entry - tp) / max(risk, 1e-12)
    return tp, rr


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
        direction = "long"
        entry = _f(klines[-1], "close")
        sl = second["L2"] * (1 - float(params.get("stop_buffer_pct", 0.3)) / 100.0)
        if sl >= entry:
            return None
        label = "second_buy"
        side = "做多"
    else:
        first = _find_utad(klines, params)
        second = _short_second(klines, first, params) if first else None
        if not first or not second:
            return None
        direction = "short"
        entry = _f(klines[-1], "close")
        sl = second["H2"] * (1 + float(params.get("stop_buffer_pct", 0.3)) / 100.0)
        if sl <= entry:
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
        f"BTC手动{macro_direction} | {tf}威科夫{first['kind']}一{'买' if direction == 'long' else '卖'}"
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
            "macro": macro_direction,
        },
    )
