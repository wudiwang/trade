import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.db import DB


TABLES = ("signals", "paper_trades", "equity_curve", "signal_anchors")


def main():
    cfg = get_config()
    db = DB(cfg.db_path)
    for table in TABLES:
        db.execute(f"DELETE FROM {table}")
    db.log("info", "reset", "cleared paper data and signal anchors for macro_pullback rollout")
    print("cleared: " + ", ".join(TABLES))


if __name__ == "__main__":
    main()
