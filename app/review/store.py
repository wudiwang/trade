from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .models import ClosedPnl, Execution, Transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS bybit_sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_ms INTEGER, finished_ms INTEGER,
  window_start_ms INTEGER, window_end_ms INTEGER, status TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS bybit_executions (
  account_id TEXT, category TEXT, exec_id TEXT, payload TEXT,
  PRIMARY KEY(account_id, category, exec_id)
);
CREATE TABLE IF NOT EXISTS bybit_orders (
  account_id TEXT, category TEXT, order_id TEXT, payload TEXT,
  PRIMARY KEY(account_id, category, order_id)
);
CREATE TABLE IF NOT EXISTS bybit_transactions (
  account_id TEXT NOT NULL, category TEXT NOT NULL, tx_id TEXT NOT NULL,
  symbol TEXT, timestamp_ms INTEGER, tx_type TEXT, cash_flow TEXT,
  funding TEXT, fee TEXT, change_amount TEXT, currency TEXT,
  PRIMARY KEY(account_id, category, tx_id)
);
CREATE TABLE IF NOT EXISTS bybit_closed_pnl (
  account_id TEXT, category TEXT, order_id TEXT, payload TEXT,
  PRIMARY KEY(account_id, category, order_id)
);
CREATE TABLE IF NOT EXISTS bybit_equity_snapshots (
  account_id TEXT, timestamp_ms INTEGER, total_equity TEXT, payload TEXT,
  PRIMARY KEY(account_id, timestamp_ms)
);
CREATE TABLE IF NOT EXISTS trade_episodes (
  episode_id TEXT PRIMARY KEY, payload TEXT
);
CREATE TABLE IF NOT EXISTS trade_episode_factors (
  episode_id TEXT, factor_key TEXT, state TEXT, payload TEXT,
  PRIMARY KEY(episode_id, factor_key)
);
CREATE TABLE IF NOT EXISTS daily_trade_reviews (
  review_id TEXT PRIMARY KEY, window_start_ms INTEGER, window_end_ms INTEGER, payload TEXT
);
CREATE TABLE IF NOT EXISTS review_feedback (
  review_id TEXT, episode_id TEXT, verdict TEXT, note TEXT, updated_ms INTEGER,
  PRIMARY KEY(review_id, episode_id)
);
"""


class ReviewStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript(SCHEMA)

    @staticmethod
    def _payload(row) -> str:
        return json.dumps(
            asdict(row), ensure_ascii=False, sort_keys=True,
            default=lambda value: str(value) if isinstance(value, Decimal) else value,
        )

    def upsert_executions(self, rows: Iterable[Execution]) -> None:
        self.connection.executemany(
            """INSERT OR REPLACE INTO bybit_executions
            (account_id,category,exec_id,payload) VALUES (?,?,?,?)""",
            [(r.account_id, r.category, r.exec_id, self._payload(r)) for r in rows],
        )
        self.connection.commit()

    def upsert_transactions(self, rows: Iterable[Transaction]) -> None:
        self.connection.executemany(
            """INSERT OR REPLACE INTO bybit_transactions
            (account_id,category,tx_id,symbol,timestamp_ms,tx_type,cash_flow,funding,fee,change_amount,currency)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(r.account_id, r.category, r.tx_id, r.symbol, r.timestamp_ms, r.tx_type,
              str(r.cash_flow), str(r.funding), str(r.fee), str(r.change), r.currency) for r in rows],
        )
        self.connection.commit()

    def upsert_closed_pnl(self, rows: Iterable[ClosedPnl]) -> None:
        self.connection.executemany(
            """INSERT OR REPLACE INTO bybit_closed_pnl
            (account_id,category,order_id,payload) VALUES (?,?,?,?)""",
            [("default", r.category, r.order_id, self._payload(r)) for r in rows],
        )
        self.connection.commit()

    def replace_review(self, review_id: str, report: dict) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO daily_trade_reviews
            (review_id,window_start_ms,window_end_ms,payload) VALUES (?,?,?,?)""",
            (review_id, report.get("window_start_ms"), report.get("window_end_ms"),
             json.dumps(report, ensure_ascii=False, sort_keys=True)),
        )
        self.connection.commit()

    def count(self, table: str) -> int:
        allowed = {"bybit_transactions", "bybit_executions", "bybit_orders", "bybit_closed_pnl",
                   "daily_trade_reviews"}
        if table not in allowed:
            raise ValueError("unsupported table")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
