"""阶段D冒烟：发送带按钮的测试信号卡片 + 轮询启动。卡片对应DB里真实的测试信号行，按钮可点。"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.db import DB
from app.bot.telegram import TgBot


class FakeSignal:
    def to_db(self):
        return {
            "created_at": int(time.time()), "symbol": "TESTUSDT", "tf": "15m",
            "direction": "long", "kind": "primary", "entry": 1.2345, "sl": 1.2160,
            "tp": 1.3370, "rr": 5.54, "vol_ratio": 2.1, "strength": "strong",
            "suggested_qty": 270.27, "risk_usdt": 5.0,
            "reason": "底分型确认 + 量能2.1x均量 + 跌破前低1.218后收回（这是施工测试卡片）",
            "extra": {},
        }


async def main():
    cfg = get_config()
    db = DB(cfg.db_path)
    bot = TgBot(cfg, db)
    assert bot.enabled, "tg 未配置"
    sig = FakeSignal()
    sid = db.insert_signal(sig.to_db())
    await bot.start()
    await bot.on_signal(sid, sig)
    row = db.one("SELECT status, tg_message_id FROM signals WHERE id=?", (sid,))
    assert row["status"] == "notified" and row["tg_message_id"], f"推送状态异常: {dict(row)}"
    print(f"信号#{sid} 卡片已发送, tg_message_id={row['tg_message_id']}")
    await asyncio.sleep(3)  # 轮询跑一下确认无异常
    await bot.stop()
    print("TG SMOKE OK")


asyncio.run(main())
