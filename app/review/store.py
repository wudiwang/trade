from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, fields
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .models import ClosedPnl, Execution, OrderRecord, Transaction


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
CREATE TABLE IF NOT EXISTS bybit_review_deliveries (
  review_id TEXT, content_hash TEXT, sent_ms INTEGER,
  PRIMARY KEY(review_id, content_hash)
);
-- Every distinct state of an order that any sync has seen.  The exchange keeps only the latest
-- state of an order and drops cancelled stops after 24 hours, so this table is the only record
-- of "a stop existed" / "its trigger price was X at that time".  Rows are never overwritten.
CREATE TABLE IF NOT EXISTS bybit_order_observations (
  account_id TEXT NOT NULL, category TEXT NOT NULL, order_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL, symbol TEXT, created_ms INTEGER, updated_ms INTEGER,
  first_seen_ms INTEGER, payload TEXT,
  PRIMARY KEY(account_id, category, order_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_order_obs_time ON bybit_order_observations(updated_ms);
-- When order states were synced (every review run and every snapshot tick).  The exchange only
-- returns orders cancelled in the last 24 hours, so a gap between two syncs longer than that is a
-- period in which a cancelled stop could have vanished unseen.
CREATE TABLE IF NOT EXISTS bybit_order_sync_log (seen_ms INTEGER PRIMARY KEY);
-- One rendered page + headline numbers per review day (what the web console lists and serves).
CREATE TABLE IF NOT EXISTS daily_review_pages (
  review_id TEXT PRIMARY KEY, generated_ms INTEGER, window_start_ms INTEGER,
  window_end_ms INTEGER, status TEXT, partial INTEGER, total INTEGER, judged INTEGER,
  compliant INTEGER, most_broken TEXT, html TEXT
);
"""

_ORDER_DECIMALS = ("qty", "filled_qty", "price", "avg_fill_price", "trigger_price",
                   "attached_sl", "attached_tp")
_ORDER_FIELDS = tuple(item.name for item in fields(OrderRecord))


def order_from_payload(payload: str) -> OrderRecord:
    """Inverse of ReviewStore._payload for an OrderRecord (Decimal fields restored)."""
    data = json.loads(payload)
    for name in _ORDER_DECIMALS:
        if data.get(name) is not None:
            data[name] = Decimal(str(data[name]))
    return OrderRecord(**{name: data[name] for name in _ORDER_FIELDS})


def _read_only(path: str | Path) -> sqlite3.Connection | None:
    """Open an existing review database read-only; None when the file does not exist.
    Never creates the file or its schema (used by the web console's read routes)."""
    path = Path(path)
    if not path.is_file():
        return None
    return sqlite3.connect(f"file:{quote(path.as_posix(), safe='/:')}?mode=ro", uri=True, timeout=2)


def read_pages(path: str | Path, limit: int = 60) -> list[dict[str, Any]]:
    """Headline rows of the stored daily reviews, newest day first."""
    connection = _read_only(path)
    if connection is None:
        return []
    try:
        cursor = connection.execute(
            """SELECT review_id,generated_ms,status,partial,total,judged,compliant,most_broken
            FROM daily_review_pages ORDER BY review_id DESC LIMIT ?""", (max(1, int(limit)),))
        return [{"day": row[0], "generated_ms": row[1], "status": row[2], "partial": bool(row[3]),
                 "total": row[4], "judged": row[5], "compliant": row[6], "most_broken": row[7]}
                for row in cursor.fetchall()]
    except sqlite3.Error:
        return []                       # database from before the pages table existed
    finally:
        connection.close()


def read_page_html(path: str | Path, review_id: str) -> str | None:
    connection = _read_only(path)
    if connection is None:
        return None
    try:
        row = connection.execute(
            "SELECT html FROM daily_review_pages WHERE review_id=?", (review_id,)).fetchone()
        return None if row is None else str(row[0])
    except sqlite3.Error:
        return None
    finally:
        connection.close()


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

    def upsert_orders(self, rows: Iterable[OrderRecord], category: str = "linear",
                      seen_ms: int | None = None) -> int:
        """Keep the latest state per order AND every distinct state ever seen.

        Returns how many observations were new.  Re-syncing an unchanged order adds nothing,
        so the call is idempotent; a changed trigger price or status adds one more row.
        """
        seen_ms = int(time.time() * 1000) if seen_ms is None else int(seen_ms)
        added = 0
        for row in rows:
            payload = self._payload(row)
            fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
            self.connection.execute(
                """INSERT OR REPLACE INTO bybit_orders
                (account_id,category,order_id,payload) VALUES (?,?,?,?)""",
                (row.account_id, category, row.order_id, payload),
            )
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO bybit_order_observations
                (account_id,category,order_id,fingerprint,symbol,created_ms,updated_ms,
                 first_seen_ms,payload) VALUES (?,?,?,?,?,?,?,?,?)""",
                (row.account_id, category, row.order_id, fingerprint, row.symbol,
                 row.created_ms, row.updated_ms, seen_ms, payload),
            )
            added += max(cursor.rowcount, 0)
        self.connection.execute(
            "INSERT OR IGNORE INTO bybit_order_sync_log (seen_ms) VALUES (?)", (seen_ms,))
        self.connection.commit()
        return added

    def order_sync_times(self, since_ms: int = 0) -> list[int]:
        """Moments at which order states were stored (ascending), at/after ``since_ms``."""
        cursor = self.connection.execute(
            "SELECT seen_ms FROM bybit_order_sync_log WHERE seen_ms>=? ORDER BY seen_ms",
            (int(since_ms),))
        return [int(row[0]) for row in cursor.fetchall()]

    def order_observations(self, since_ms: int = 0, category: str = "linear") -> list[OrderRecord]:
        """Every stored observation last updated at/after ``since_ms``, in the order the states
        were first seen (the same order id appears once per distinct state)."""
        cursor = self.connection.execute(
            """SELECT payload FROM bybit_order_observations
            WHERE category=? AND updated_ms>=? ORDER BY first_seen_ms, updated_ms, order_id""",
            (category, int(since_ms)),
        )
        return [order_from_payload(row[0]) for row in cursor.fetchall()]

    def replace_review(self, review_id: str, report: dict) -> None:
        self.connection.execute(
            """INSERT OR REPLACE INTO daily_trade_reviews
            (review_id,window_start_ms,window_end_ms,payload) VALUES (?,?,?,?)""",
            (review_id, report.get("window_start_ms"), report.get("window_end_ms"),
             json.dumps(report, ensure_ascii=False, sort_keys=True)),
        )
        self.connection.commit()

    def replace_page(self, review_id: str, report: dict, html: str) -> None:
        """Store the rendered page and its headline numbers; one row per review day."""
        score = report.get("score") if isinstance(report.get("score"), dict) else {}
        broken = score.get("most_broken") if isinstance(score.get("most_broken"), dict) else {}
        self.connection.execute(
            """INSERT OR REPLACE INTO daily_review_pages
            (review_id,generated_ms,window_start_ms,window_end_ms,status,partial,total,judged,
             compliant,most_broken,html) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (review_id, report.get("generated_ms") or int(time.time() * 1000),
             report.get("window_start_ms"), report.get("window_end_ms"),
             str(report.get("status") or ""), 1 if report.get("partial") else 0,
             int(score.get("total") or 0), int(score.get("judged") or 0),
             int(score.get("compliant") or 0), str(broken.get("label") or ""), html),
        )
        self.connection.commit()

    def list_pages(self, limit: int = 60) -> list[dict[str, Any]]:
        cursor = self.connection.execute(
            """SELECT review_id,generated_ms,window_start_ms,window_end_ms,status,partial,total,
            judged,compliant,most_broken FROM daily_review_pages
            ORDER BY review_id DESC LIMIT ?""", (max(1, int(limit)),))
        return [{
            "day": row[0], "generated_ms": row[1], "window_start_ms": row[2],
            "window_end_ms": row[3], "status": row[4], "partial": bool(row[5]),
            "total": row[6], "judged": row[7], "compliant": row[8], "most_broken": row[9],
        } for row in cursor.fetchall()]

    def page_html(self, review_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT html FROM daily_review_pages WHERE review_id=?", (review_id,)).fetchone()
        return None if row is None else str(row[0])

    def start_sync(self, window_start_ms: int, window_end_ms: int) -> int:
        cursor = self.connection.execute(
            """INSERT INTO bybit_sync_runs
            (started_ms,window_start_ms,window_end_ms,status) VALUES (?,?,?,?)""",
            (int(time.time() * 1000), window_start_ms, window_end_ms, "running"),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_sync(self, run_id: int, status: str, error: str = "") -> None:
        self.connection.execute(
            "UPDATE bybit_sync_runs SET finished_ms=?,status=?,error=? WHERE id=?",
            (int(time.time() * 1000), status, error[:1000], run_id),
        )
        self.connection.commit()

    def sync_status(self, run_id: int) -> str:
        row = self.connection.execute(
            "SELECT status FROM bybit_sync_runs WHERE id=?", (run_id,)
        ).fetchone()
        return str(row[0]) if row else ""

    def was_delivered(self, review_id: str, content_hash: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM bybit_review_deliveries WHERE review_id=? AND content_hash=?",
            (review_id, content_hash),
        ).fetchone()
        return row is not None

    def mark_delivered(self, review_id: str, content_hash: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO bybit_review_deliveries VALUES (?,?,?)",
            (review_id, content_hash, int(time.time() * 1000)),
        )
        self.connection.commit()

    def has_delivery(self, review_id: str, prefix: str = "") -> bool:
        """Was anything delivered for this review whose hash starts with ``prefix``?"""
        row = self.connection.execute(
            "SELECT 1 FROM bybit_review_deliveries WHERE review_id=? AND substr(content_hash,1,?)=?",
            (review_id, len(prefix), prefix),
        ).fetchone()
        return row is not None

    def close(self) -> None:
        self.connection.close()

    def count(self, table: str) -> int:
        allowed = {"bybit_transactions", "bybit_executions", "bybit_orders", "bybit_closed_pnl",
                   "daily_trade_reviews", "bybit_order_observations", "daily_review_pages",
                   "bybit_review_deliveries", "bybit_order_sync_log"}
        if table not in allowed:
            raise ValueError("unsupported table")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
