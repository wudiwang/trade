"""预演监控单元测试。python tests/test_playbook.py 运行。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import DB
from app.engine.playbook import check_bar


def bar(o, h, l, c):
    return (1000, o, h, l, c, 100, 1000, 50, 1)


def fresh_db():
    return DB(Path(tempfile.mkdtemp()) / "t.db")


def test_price_reach():
    db = fresh_db()
    db.insert_playbook({"symbol": "BTCUSDT", "tf": "15m", "direction": "long",
                        "trigger_type": "price_reach", "trigger_price": 100.0,
                        "entry": 100, "tp": 120, "sl": 95, "title": "回踩100买"})
    # 本K区间没碰到100 → 不触发
    assert check_bar(db, "BTCUSDT", "15m", bar(110, 112, 105, 108)) == []
    # 本K下探到99(区间含100) → 触发
    fired = check_bar(db, "BTCUSDT", "15m", bar(105, 106, 99, 101))
    assert len(fired) == 1 and fired[0]["id"] == 1
    # 已触发后不再重复
    assert check_bar(db, "BTCUSDT", "15m", bar(100, 101, 99, 100)) == []
    print(f"  price_reach: {fired[0]['message'][:40]}")


def test_sweep_reclaim_long():
    db = fresh_db()
    db.insert_playbook({"symbol": "ETHUSDT", "tf": "", "direction": "long",
                        "trigger_type": "sweep_reclaim", "trigger_price": 50.0,
                        "entry": 50, "tp": 60, "sl": 47})
    # 没下破 → 不触发
    assert check_bar(db, "ETHUSDT", "5m", bar(52, 53, 50.5, 52)) == []
    # 下破50但收回50上方 → 假突破回收触发
    fired = check_bar(db, "ETHUSDT", "5m", bar(51, 51.5, 48, 50.5))
    assert len(fired) == 1
    print(f"  sweep_reclaim: {fired[0]['message'][:40]}")


def test_tf_filter():
    db = fresh_db()
    db.insert_playbook({"symbol": "SOLUSDT", "tf": "1h", "direction": "long",
                        "trigger_type": "price_reach", "trigger_price": 20.0})
    # 剧本限定1h，5m的K不应触发
    assert check_bar(db, "SOLUSDT", "5m", bar(21, 22, 19, 20)) == []
    # 1h的K触发
    assert len(check_bar(db, "SOLUSDT", "1h", bar(21, 22, 19, 20))) == 1


def test_short_sweep():
    db = fresh_db()
    db.insert_playbook({"symbol": "XUSDT", "tf": "", "direction": "short",
                        "trigger_type": "sweep_reclaim", "trigger_price": 10.0})
    # 上破10后收回10下方 → 做空假突破触发
    assert len(check_bar(db, "XUSDT", "15m", bar(9.5, 10.5, 9.4, 9.8))) == 1


def main():
    fns = [v for n, v in globals().items() if n.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL {len(fns)} TESTS PASSED")


if __name__ == "__main__":
    main()
