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
        "structure_tf": "15m",
        "trigger_tf": "5m",
        "impulse_window": 18,
        "impulse_min_pct": 3.0,
        "ma_period": 5,
        "ma_extension_pct": 0.5,
        "retest_tolerance_pct": 0.4,
        "volume_decay_ratio": 1.2,
        "stop_buffer_pct": 0.3,
        "min_rr": 1.2,
        "tp_lookback": 30,
        "vp_bins": 12,
    }
    base.update(overrides)
    return base


def sell_structure():
    vals = [
        (70, 70.5, 69.8, 70.2, 100),
        (70.2, 71.2, 70.1, 71.0, 120),
        (71.0, 72.2, 70.9, 72.0, 150),
        (72.0, 73.4, 71.9, 73.2, 170),
        (73.2, 74.7, 73.0, 74.5, 190),
        (74.5, 76.1, 74.2, 75.8, 220),
        (75.8, 76.05, 74.8, 75.0, 180),
        (75.0, 75.2, 73.8, 74.0, 160),
        (74.0, 74.4, 73.2, 73.6, 150),
        (73.6, 74.4, 73.4, 74.1, 90),
        (74.1, 75.35, 73.9, 75.0, 85),
        (75.0, 75.45, 74.6, 74.8, 80),
    ]
    return [k(*row, t=i, step_ms=900000) for i, row in enumerate(vals)]


def sell_trigger():
    vals = [
        (74.6, 75.1, 74.5, 74.9, 60),
        (74.9, 75.35, 74.8, 75.25, 65),
        (75.25, 75.45, 75.0, 75.1, 60),
        (75.1, 75.15, 74.7, 74.8, 70),
        (74.8, 74.9, 74.2, 74.35, 90),
    ]
    return [k(*row, t=100 + i) for i, row in enumerate(vals)]


def test_detect_second_sell_after_failed_retest():
    sig = detect_macro_pullback("SOLUSDT", "short", sell_structure(), sell_trigger(), cfg())
    assert sig is not None
    assert sig.direction == "short"
    assert sig.extra["type"] == "second_sell"
    assert sig.extra["path"] == "macro_chan_pullback"
    assert sig.sl > sig.entry
    assert sig.tp < sig.entry
    assert sig.extra["structure"]["H2"] < sig.extra["structure"]["H1"]


def buy_structure():
    vals = [
        (76, 76.2, 75.5, 75.6, 100),
        (75.6, 75.8, 74.5, 74.7, 130),
        (74.7, 74.9, 73.2, 73.5, 170),
        (73.5, 73.8, 72.1, 72.4, 210),
        (72.4, 72.6, 70.8, 71.2, 240),
        (71.2, 72.0, 70.7, 71.8, 180),
        (71.8, 73.2, 71.6, 72.9, 130),
        (72.9, 73.4, 72.4, 73.1, 110),
        (73.1, 73.2, 71.4, 71.8, 90),
        (71.8, 72.1, 71.3, 71.9, 85),
    ]
    return [k(*row, t=i, step_ms=900000) for i, row in enumerate(vals)]


def buy_trigger():
    vals = [
        (71.8, 72.0, 71.35, 71.5, 60),
        (71.5, 71.7, 71.3, 71.45, 58),
        (71.45, 72.0, 71.4, 71.9, 65),
        (71.9, 72.35, 71.85, 72.25, 90),
    ]
    return [k(*row, t=100 + i) for i, row in enumerate(vals)]


def test_detect_second_buy_after_higher_low():
    sig = detect_macro_pullback("SOLUSDT", "long", buy_structure(), buy_trigger(), cfg())
    assert sig is not None
    assert sig.direction == "long"
    assert sig.extra["type"] == "second_buy"
    assert sig.sl < sig.entry
    assert sig.tp > sig.entry
    assert sig.extra["structure"]["L2"] > sig.extra["structure"]["L1"]


def test_neutral_macro_has_no_signal():
    assert detect_macro_pullback("SOLUSDT", "neutral", sell_structure(), sell_trigger(), cfg()) is None


class MiniCfg:
    def __init__(self):
        self.values = {
            "strategy": "chan_bi",
            "macro_pullback.enabled": True,
            "macro_pullback.exclusive": True,
            "macro_pullback.structure_tf": "15m",
            "macro_pullback.trigger_tf": "5m",
            "macro_pullback.context_tf": "1h",
            "macro_pullback.impulse_window": 18,
            "macro_pullback.impulse_min_pct": 3.0,
            "macro_pullback.ma_period": 5,
            "macro_pullback.ma_extension_pct": 0.5,
            "macro_pullback.retest_tolerance_pct": 0.4,
            "macro_pullback.volume_decay_ratio": 1.2,
            "macro_pullback.stop_buffer_pct": 0.3,
            "macro_pullback.cooldown_bars": 12,
            "macro_pullback.min_rr": 1.2,
            "macro_pullback.tp_lookback": 30,
            "macro_pullback.vp_bins": 12,
            "risk.account_equity": 1000,
            "risk.risk_pct": 0.5,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeDB:
    def log(self, *args, **kwargs):
        pass


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

    out = eng.evaluate_all("SOLUSDT", "5m", {"15m": sell_structure(), "5m": sell_trigger(), "1h": []})

    assert len(out) == 1
    assert out[0].extra["path"] == "macro_chan_pullback"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")


if __name__ == "__main__":
    main()
