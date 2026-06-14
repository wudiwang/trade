"""Spring 策略专项回测：强制 strategy=spring_v4，统计买点触发后的胜率与期望(R)。

用法:
  python tests/backtest_spring.py [days] [tfs] [btc_on|btc_off] [symbols_csv]
例:
  python tests/backtest_spring.py 14                 # 最近14天, 默认级别, 监控池全部币种
  python tests/backtest_spring.py 14 5m,15m,1h btc_on BTCUSDT,ETHUSDT

输出:
  - 总体: 触发数/已结算/胜率/期望R
  - 按买点类型(一买buy1/二买buy2)、按级别、按方向分桶
期望R = 平均每笔的 R 倍数(avg_r)，>0 为正期望。
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.db import DB
from app.engine.binance_rest import BinanceRest
from app.engine.backtest import run_backtest

TYPE_LABEL = {"buy1": "一买", "buy2": "二买", "sell1": "一卖", "sell2": "二卖"}


def _fmt(b: dict) -> str:
    return (f"触发{b['signals']:>3} | 结算{b['closed']:>3} | "
            f"胜率{b['win_rate']:>5}% | 期望{b['avg_r']:>6}R | 累计{b['total_r']:>7}R | 未平{b['open']}")


async def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 14
    tfs = sys.argv[2].split(",") if len(sys.argv) > 2 else ["5m", "15m", "1h"]
    btc_filter = (sys.argv[3] == "btc_on") if len(sys.argv) > 3 else None

    cfg = get_config()
    cfg.set_override("strategy", "spring_v4")          # 关键：强制走 Spring 路径
    if btc_filter is not None:
        cfg.set_override("spring.btc_filter", btc_filter)

    if len(sys.argv) > 4:
        symbols = sys.argv[4].split(",")
    else:
        symbols = DB(cfg.db_path).enabled_symbols() or ["BTCUSDT", "ETHUSDT"]

    print(f"策略=spring_v4 | 天数={days} | 级别={tfs} | 币种数={len(symbols)} | "
          f"BTC过滤={cfg.get('spring.btc_filter')}\n")

    rest = BinanceRest(cfg.get("binance.rest_base"))
    res = await run_backtest(cfg, rest, symbols, tfs, days,
                             progress=lambda d, t, m: print(f"  进度 {d}/{t} ({m})"))
    await rest.close()

    print("\n===== Spring 回测结果 =====")
    print("总体 :", _fmt(res["total"]))
    print("\n-- 按买点类型 --")
    for k, b in sorted(res["by_type"].items()):
        print(f"{TYPE_LABEL.get(k, k):<4}:", _fmt(b))
    print("\n-- 按级别 --")
    for k, b in sorted(res["by_tf"].items()):
        print(f"{k:<4}:", _fmt(b))
    print("\n-- 按方向 --")
    for k, b in sorted(res["by_direction"].items()):
        print(f"{k:<5}:", _fmt(b))
    print(f"\n耗时 {res['elapsed_s']}s | 参数 {json.dumps(res['params'], ensure_ascii=False)}")


asyncio.run(main())
