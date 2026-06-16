import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.engine.macro_pullback import detect_macro_pullback
from app.engine.signals import Signal, SignalEngine


def k(o, h, l, c, v=100.0, t=0, step_ms=300000):
    return {
        "open_time": t * step_ms,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
        "quote_volume": v * c,
        "taker_buy": v / 2,
        "closed": 1,
    }


def cfg(**overrides):
    base = {
        "enabled": True,
        "exclusive": True,
        "timeframes": ["5m", "15m"],
        "vol_ma": 5,
        "vol_mult": 2.5,
        "lookback": 5,
        "reclaim_bars": 3,
        "reclaim_tolerance_pct": 0.5,
        "reclaim_body_pct": 80,
        "min_leg_pct": 1.0,
        "second_tolerance_pct": 0.2,
        "stop_buffer_pct": 0.3,
        "max_signal_bars_after_second": 2,
        "max_entry_distance_r": 0.3,
        "max_entry_distance_pct": 0.5,
        "missed_midpoint_filter": True,
        "min_effective_bars_between": 5,
        "cooldown_bars": 12,
        "min_rr": 1.5,
        "tp_rr_long": 2.0,
        "tp_rr_short": 0.8,
        "tp_lookback": 30,
        "vp_bins": 12,
        "account_equity": 1000,
        "risk_pct": 0.5,
        "tf": "5m",
    }
    base.update(overrides)
    return base


def spring_then_second_buy():
    vals = [
        (103, 104, 102, 103, 100),
        (102, 103, 101, 102, 100),
        (101, 102, 100, 101, 100),
        (100, 101, 99, 100, 100),
        (99, 100, 98, 99, 100),
        (99, 100, 94, 95, 360),       # Spring sweep below prior low, high volume
        (95, 101, 95, 99, 130),
        (99, 103, 97, 102, 140),
        (102, 105, 99, 104, 130),     # reclaim prior down-leg start area
        (104, 107, 101, 106, 100),    # up leg high after L1
        (106, 106, 102, 104, 100),
        (104, 105, 101, 102, 95),
        (102, 104, 100.5, 101, 90),
        (101, 103, 100.2, 100.6, 90),
        (100.6, 102, 100, 100.4, 90),  # L2, higher than L1
        (100.4, 100.45, 100.2, 100.3, 110),  # confirms second bottom near L2
        (100.3, 100.5, 100.25, 100.46, 110),  # stall bar: close > right K high
    ]
    return [k(*row, t=i) for i, row in enumerate(vals)]


def body_reclaim_spring_then_second_buy():
    vals = [
        (111, 112, 109, 110, 100),
        (109, 110, 106, 107, 100),
        (106, 107, 103, 104, 100),
        (103, 104, 100, 101, 100),
        (100, 101, 98, 99, 100),
        (99, 100, 94, 95, 360),       # body 80% reclaim level is 98.2
        (95, 101, 95, 99, 130),       # reclaims body, not prior down-leg start
        (99, 103, 97, 102, 140),
        (102, 105, 99, 104, 130),
        (104, 107, 101, 106, 100),
        (106, 106, 102, 104, 100),
        (104, 105, 101, 102, 95),
        (102, 104, 100.5, 101, 90),
        (101, 103, 100.2, 100.6, 90),
        (100.6, 102, 100, 100.4, 90),
        (100.4, 100.45, 100.2, 100.3, 110),
        (100.3, 100.5, 100.25, 100.46, 110),
    ]
    return [k(*row, t=i) for i, row in enumerate(vals)]


def old_missed_then_recent_body_reclaim_buy():
    old = body_reclaim_spring_then_second_buy()
    spacer = [k(130, 131, 129, 130, 80, t=len(old) + i) for i in range(8)]
    recent = body_reclaim_spring_then_second_buy()
    offset = len(old) + len(spacer)
    recent = [dict(row, open_time=(offset + i) * 300000) for i, row in enumerate(recent)]
    return old + spacer + recent


def utad_then_second_sell():
    vals = [
        (97, 98, 96, 97, 100),
        (98, 99, 97, 98, 100),
        (99, 100, 98, 99, 100),
        (100, 101, 99, 100, 100),
        (101, 102, 100, 101, 100),
        (101, 108, 100, 107, 380),    # UTAD sweep above prior high, high volume
        (107, 107, 99, 100, 140),
        (100, 105, 97, 98, 150),
        (98, 103, 95, 96.4, 130),     # reclaim prior up-leg start area
        (96.4, 101, 94, 95, 120),
        (95, 99, 93, 94, 100),        # down leg low after H1
        (94, 100, 94, 97, 100),
        (97, 101, 95, 100, 95),
        (100, 102, 96, 101, 90),
        (101, 103, 99, 102, 90),      # H2, below H1
        (102, 102.9, 102.6, 102.7, 110),  # confirms second top near H2
        (102.7, 102.8, 102.5, 102.55, 110),  # stall bar: close < right K low
    ]
    return [k(*row, t=i) for i, row in enumerate(vals)]


def late_after_second_sell():
    vals = list(utad_then_second_sell())
    start = len(vals)
    vals.extend([
        k(102.7, 102.8, 101.9, 102.2, 90, t=start),
        k(102.2, 102.3, 101.4, 101.6, 90, t=start + 1),
        k(101.6, 101.8, 100.9, 101.1, 90, t=start + 2),
    ])
    return vals


def far_from_second_sell():
    vals = list(utad_then_second_sell())
    vals[-1] = k(102, 102.5, 99.8, 100.0, 110, t=len(vals) - 1)
    return vals


def second_buy_without_stall():
    vals = list(spring_then_second_buy())
    return vals[:-1]


def second_sell_without_stall():
    vals = list(utad_then_second_sell())
    return vals[:-1]


def below_down_leg_midpoint_sell():
    vals = list(utad_then_second_sell())
    vals[-1] = k(102, 102.5, 99.0, 99.8, 110, t=len(vals) - 1)
    return vals


def no_utad_second_top_only():
    vals = [
        (100, 101, 99, 100, 100),
        (101, 102, 100, 101, 100),
        (102, 103, 101, 102, 100),
        (102, 104, 101, 103, 100),
        (103, 103.5, 99, 100, 100),
        (100, 101, 96, 97, 100),
        (97, 101, 97, 100, 100),
        (100, 102, 99, 101, 100),
        (101, 101.5, 98, 99, 100),
    ]
    return [k(*row, t=i) for i, row in enumerate(vals)]


def test_detect_second_buy_requires_prior_high_volume_spring():
    sig = detect_macro_pullback("SOLUSDT", "long", spring_then_second_buy(), spring_then_second_buy(), cfg())
    assert sig is not None
    assert sig.direction == "long"
    assert sig.tf == "5m"
    assert sig.extra["type"] == "second_buy"
    assert sig.extra["path"] == "macro_chan_pullback"
    assert sig.extra["wyckoff"]["kind"] == "spring"
    assert sig.extra["structure"]["L2"] > sig.extra["structure"]["L1"]
    markers = sig.extra["markers"]
    assert [m["key"] for m in markers] == ["first_fractal", "volume_sweep", "second_fractal"]
    assert markers[0]["label"] == "L1底分型"
    assert markers[1]["label"] == "爆量K"
    assert markers[2]["label"] == "L2底分型"
    assert markers[0]["time"] == sig.extra["structure"]["L1_time"]
    assert markers[1]["time"] == spring_then_second_buy()[sig.extra["wyckoff"]["sweep_idx"]]["open_time"]
    assert markers[2]["time"] == sig.extra["structure"]["L2_time"]
    assert sig.extra["structure"]["entry_time"] == spring_then_second_buy()[-1]["open_time"]
    assert sig.sl < sig.entry < sig.tp
    risk = sig.entry - sig.sl
    assert abs((sig.tp - sig.entry) / risk - 2.0) < 0.01


def test_spring_reclaims_when_price_stands_back_above_80pct_of_sweep_body():
    sig = detect_macro_pullback(
        "HUSDT", "long", body_reclaim_spring_then_second_buy(), body_reclaim_spring_then_second_buy(), cfg()
    )
    assert sig is not None
    assert sig.direction == "long"
    assert sig.extra["wyckoff"]["reclaim_level"] == 98.2
    assert sig.extra["wyckoff"]["reclaimed_at"] == 6


def test_uses_latest_valid_spring_in_window_not_stale_older_one():
    sig = detect_macro_pullback(
        "HUSDT", "long", old_missed_then_recent_body_reclaim_buy(), old_missed_then_recent_body_reclaim_buy(), cfg()
    )
    assert sig is not None
    assert sig.extra["wyckoff"]["sweep_idx"] > 20
    assert sig.extra["structure"]["L2_idx"] > sig.extra["wyckoff"]["sweep_idx"]


def test_detect_second_sell_requires_prior_high_volume_utad():
    sig = detect_macro_pullback("SOLUSDT", "short", utad_then_second_sell(), utad_then_second_sell(), cfg())
    assert sig is not None
    assert sig.direction == "short"
    assert sig.extra["type"] == "second_sell"
    assert sig.extra["wyckoff"]["kind"] == "utad"
    assert sig.extra["structure"]["H2"] < sig.extra["structure"]["H1"]
    assert sig.extra["structure"]["entry_time"] == utad_then_second_sell()[-1]["open_time"]
    markers = sig.extra["markers"]
    assert [m["key"] for m in markers] == ["first_fractal", "volume_sweep", "second_fractal"]
    assert markers[0]["label"] == "H1顶分型"
    assert markers[1]["label"] == "爆量K"
    assert markers[2]["label"] == "H2顶分型"
    assert markers[0]["time"] == sig.extra["structure"]["H1_time"]
    assert markers[1]["time"] == utad_then_second_sell()[sig.extra["wyckoff"]["sweep_idx"]]["open_time"]
    assert markers[2]["time"] == sig.extra["structure"]["H2_time"]
    assert sig.tp < sig.entry < sig.sl
    risk = sig.sl - sig.entry
    assert abs((sig.entry - sig.tp) / risk - 0.8) < 0.01


def test_second_buy_requires_stall_after_l2_confirmation():
    assert detect_macro_pullback("SOLUSDT", "long", second_buy_without_stall(), second_buy_without_stall(), cfg()) is None


def test_second_sell_requires_stall_after_h2_confirmation():
    assert detect_macro_pullback("SOLUSDT", "short", second_sell_without_stall(), second_sell_without_stall(), cfg()) is None


def test_second_sell_must_trigger_soon_after_h2_confirmation():
    sig = detect_macro_pullback("SOLUSDT", "short", late_after_second_sell(), late_after_second_sell(), cfg())
    assert sig is None


def test_second_sell_rejects_entry_too_far_from_h2():
    sig = detect_macro_pullback("SOLUSDT", "short", far_from_second_sell(), far_from_second_sell(), cfg())
    assert sig is None


def test_second_sell_rejects_price_below_down_leg_midpoint():
    sig = detect_macro_pullback("SOLUSDT", "short", below_down_leg_midpoint_sell(), below_down_leg_midpoint_sell(), cfg())
    assert sig is None


def test_second_top_without_utad_does_not_signal():
    sig = detect_macro_pullback("SOLUSDT", "short", no_utad_second_top_only(), no_utad_second_top_only(), cfg())
    assert sig is None


def test_neutral_macro_has_no_signal():
    assert detect_macro_pullback("SOLUSDT", "neutral", spring_then_second_buy(), spring_then_second_buy(), cfg()) is None


class MiniCfg:
    def __init__(self):
        self.values = {
            "strategy": "chan_bi",
            "macro_pullback.enabled": True,
            "macro_pullback.exclusive": True,
            "macro_pullback.timeframes": ["5m", "15m"],
            "macro_pullback.directions": ["long", "short"],
            "macro_pullback.vol_ma": 5,
            "macro_pullback.vol_mult": 2.5,
            "macro_pullback.lookback": 5,
            "macro_pullback.reclaim_bars": 3,
            "macro_pullback.reclaim_tolerance_pct": 0.5,
            "macro_pullback.reclaim_body_pct": 80,
            "macro_pullback.min_leg_pct": 1.0,
            "macro_pullback.second_tolerance_pct": 0.2,
            "macro_pullback.stop_buffer_pct": 0.3,
            "macro_pullback.cooldown_bars": 12,
            "macro_pullback.min_rr": 1.5,
            "macro_pullback.tp_rr_long": 2.0,
            "macro_pullback.tp_rr_short": 0.8,
            "macro_pullback.tp_lookback": 30,
            "macro_pullback.vp_bins": 12,
            "risk.account_equity": 1000,
            "risk.risk_pct": 0.5,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeDB:
    anchors = set()

    def __init__(self):
        self.anchors = set()

    def log(self, *args, **kwargs):
        pass

    def claim_signal_anchor(self, strategy, symbol, tf, direction, anchor_time):
        key = (strategy, symbol, tf, direction, anchor_time)
        if key in self.anchors:
            return False
        self.anchors.add(key)
        return True


def legacy_signal():
    return Signal(
        symbol="SOLUSDT", tf="5m", direction="long", kind="primary",
        entry=1, sl=0.9, tp=1.2, rr=2, vol_ratio=1, strength="normal",
        suggested_qty=1, risk_usdt=1, reason="legacy", created_at=1,
        extra={"path": "legacy", "type": "buy1"},
    )


def test_exclusive_mode_returns_only_macro_pullback_signal():
    eng = SignalEngine(MiniCfg(), FakeDB())
    eng.macro_view = {"direction": "short", "note": "manual", "at": 1}
    eng._eval_wyckoff = lambda *args, **kwargs: legacy_signal()
    eng._eval_trend_reversal = lambda *args, **kwargs: legacy_signal()
    eng._eval_head_shoulders = lambda *args, **kwargs: legacy_signal()
    eng._eval_chan_bi = lambda *args, **kwargs: legacy_signal()
    eng._eval_mtf = lambda *args, **kwargs: legacy_signal()

    out = eng.evaluate_all("SOLUSDT", "5m", {"15m": [], "5m": utad_then_second_sell(), "1h": []})

    assert len(out) == 1
    assert out[0].extra["path"] == "macro_chan_pullback"


def test_exclusive_mode_monitors_long_and_short_by_default():
    eng = SignalEngine(MiniCfg(), FakeDB())
    eng.macro_view = {"direction": "short", "note": "manual", "at": 1}

    out = eng.evaluate_all("HUSDT", "5m", {"15m": [], "5m": body_reclaim_spring_then_second_buy(), "1h": []})

    assert len(out) == 1
    assert out[0].direction == "long"
    assert out[0].extra["type"] == "second_buy"


def test_same_second_fractal_does_not_refire_after_cooldown_window():
    eng = SignalEngine(MiniCfg(), FakeDB())
    eng.macro_view = {"direction": "short", "note": "manual", "at": 1}
    base = utad_then_second_sell()
    first = eng.evaluate_all("SOLUSDT", "5m", {"15m": [], "5m": base, "1h": []})
    assert len(first) == 1

    extended = list(base)
    t0 = len(extended)
    for i in range(14):
        extended.append(k(99, 100, 98, 99, 80, t=t0 + i))

    second = eng.evaluate_all("SOLUSDT", "5m", {"15m": [], "5m": extended, "1h": []})
    assert second == []


def test_same_second_fractal_does_not_refire_after_engine_restart():
    db = FakeDB()
    first_engine = SignalEngine(MiniCfg(), db)
    first_engine.macro_view = {"direction": "short", "note": "manual", "at": 1}
    first = first_engine.evaluate_all("SOLUSDT", "5m", {"15m": [], "5m": utad_then_second_sell(), "1h": []})
    assert len(first) == 1

    restarted_engine = SignalEngine(MiniCfg(), db)
    restarted_engine.macro_view = {"direction": "short", "note": "manual", "at": 1}
    second = restarted_engine.evaluate_all("SOLUSDT", "5m", {"15m": [], "5m": utad_then_second_sell(), "1h": []})
    assert second == []


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")


if __name__ == "__main__":
    main()
