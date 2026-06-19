import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.web.strategy_stats import build_strategy_stats, strategy_name


def test_strategy_name_maps_known_tracks():
    assert strategy_name("second_buy", json.dumps({"path": "macro_chan_pullback", "type": "second_buy"})) == "反转战法"
    assert strategy_name("smallbig_long", json.dumps({"path": "小转大", "type": "entry2"})) == "小转大战法"
    assert strategy_name("custom", json.dumps({"path": "自定义策略"})) == "自定义策略"


def test_build_strategy_stats_groups_trades_by_strategy():
    rows = [
        {"track": "second_buy", "result": "tp", "pnl": 2, "pnl_r": 2, "sig_extra": '{"path":"macro_chan_pullback"}'},
        {"track": "second_sell", "result": "sl", "pnl": -1, "pnl_r": -1, "sig_extra": '{"path":"macro_chan_pullback"}'},
        {"track": "second_buy", "result": "open", "pnl": None, "pnl_r": None, "sig_extra": '{"path":"macro_chan_pullback"}'},
        {"track": "smallbig_long", "result": "tp", "pnl": 3, "pnl_r": 1.5, "sig_extra": '{"path":"小转大"}'},
    ]

    stats = build_strategy_stats(rows)

    reversal = next(x for x in stats if x["strategy"] == "反转战法")
    assert reversal["signals"] == 3
    assert reversal["closed"] == 2
    assert reversal["open"] == 1
    assert reversal["win_rate"] == 50.0
    assert reversal["expectancy_r"] == 0.5
    smallbig = next(x for x in stats if x["strategy"] == "小转大战法")
    assert smallbig["signals"] == 1
    assert smallbig["win_rate"] == 100.0


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")


if __name__ == "__main__":
    main()
