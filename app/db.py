"""SQLite 数据层。WAL 模式，单文件。操作均为微秒级，asyncio 下直接同步调用。"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    symbol TEXT NOT NULL, tf TEXT NOT NULL, open_time INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    quote_volume REAL, closed INTEGER DEFAULT 1,
    PRIMARY KEY (symbol, tf, open_time)
);
CREATE TABLE IF NOT EXISTS symbols (
    symbol TEXT PRIMARY KEY, status TEXT, quote_volume_24h REAL,
    enabled INTEGER DEFAULT 1, price_precision INTEGER, qty_precision INTEGER,
    tick_size REAL, step_size REAL, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL, symbol TEXT NOT NULL, tf TEXT NOT NULL,
    direction TEXT NOT NULL,            -- long / short
    kind TEXT NOT NULL,                 -- primary(RR>=5推送) / secondary(RR>=2.5仅统计)
    entry REAL, sl REAL, tp REAL, rr REAL,
    vol_ratio REAL, strength TEXT,      -- normal / strong
    suggested_qty REAL, risk_usdt REAL,
    status TEXT DEFAULT 'new',          -- new/notified/confirmed/ignored/expired/error
    reason TEXT, extra TEXT,            -- extra: json
    tg_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER, symbol TEXT, tf TEXT, direction TEXT,
    track TEXT NOT NULL,                -- rr5 / rr25 (两套门槛分别统计)
    entry REAL, sl REAL, tp REAL, qty REAL,
    opened_at INTEGER, closed_at INTEGER,
    exit_price REAL, pnl REAL, pnl_r REAL,
    result TEXT DEFAULT 'open'          -- open / tp / sl / expired
);
CREATE INDEX IF NOT EXISTS idx_paper_open ON paper_trades(result);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER, created_at INTEGER,
    binance_order_id TEXT, client_order_id TEXT,
    symbol TEXT, side TEXT, type TEXT, qty REAL, price REAL,
    status TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY, pwd_hash TEXT, salt TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS equity_curve (
    ts INTEGER, track TEXT, equity REAL, PRIMARY KEY (ts, track)
);
CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, level TEXT, source TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS playbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL, symbol TEXT NOT NULL, tf TEXT,
    direction TEXT,                     -- long / short / watch
    title TEXT,                         -- 形态/剧本描述
    entry REAL, tp REAL, sl REAL,       -- 预判买点/止盈目标/止损
    trigger_type TEXT,                  -- price_reach(到价) / sweep_reclaim(假突破回收)
    trigger_price REAL,                 -- 监控的关键位
    status TEXT DEFAULT 'active',       -- active / triggered / done / cancelled
    triggered_at INTEGER, source TEXT,  -- manual / auto
    extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks(status);
CREATE INDEX IF NOT EXISTS idx_playbooks_symbol ON playbooks(symbol, tf);
CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY, created_at INTEGER, note TEXT,
    source TEXT DEFAULT 'manual', active INTEGER DEFAULT 1
);
"""


class DB:
    """线程安全的 SQLite 封装（web 在 uvicorn 线程、引擎在主线程都会用）。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)
            cols = [r[1] for r in c.execute("PRAGMA table_info(klines)")]
            if "taker_buy" not in cols:
                c.execute("ALTER TABLE klines ADD COLUMN taker_buy REAL DEFAULT 0")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        c = self.conn()
        cur = c.execute(sql, params)
        c.commit()
        return cur

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        c = self.conn()
        c.executemany(sql, rows)
        c.commit()

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    # ---------- klines ----------
    def upsert_klines(self, symbol: str, tf: str, rows: list[tuple]) -> None:
        """rows: (open_time, open, high, low, close, volume, quote_volume, taker_buy, closed)"""
        self.executemany(
            "INSERT OR REPLACE INTO klines (symbol, tf, open_time, open, high, low, close, volume, quote_volume, taker_buy, closed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(symbol, tf, *r) for r in rows],
        )

    def get_klines(self, symbol: str, tf: str, limit: int = 500) -> list[sqlite3.Row]:
        rows = self.query(
            "SELECT * FROM klines WHERE symbol=? AND tf=? ORDER BY open_time DESC LIMIT ?",
            (symbol, tf, limit),
        )
        return list(reversed(rows))

    def latest_kline_time(self, symbol: str, tf: str) -> int | None:
        r = self.one("SELECT MAX(open_time) m FROM klines WHERE symbol=? AND tf=?", (symbol, tf))
        return r["m"] if r and r["m"] is not None else None

    def trim_klines(self, symbol: str, tf: str, keep: int) -> None:
        self.execute(
            "DELETE FROM klines WHERE symbol=? AND tf=? AND open_time < "
            "(SELECT MIN(open_time) FROM (SELECT open_time FROM klines WHERE symbol=? AND tf=? ORDER BY open_time DESC LIMIT ?))",
            (symbol, tf, symbol, tf, keep),
        )

    # ---------- symbols ----------
    def upsert_symbols(self, rows: list[dict]) -> None:
        self.executemany(
            "INSERT OR REPLACE INTO symbols (symbol, status, quote_volume_24h, enabled, price_precision, qty_precision, tick_size, step_size, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (r["symbol"], r["status"], r["quote_volume_24h"], r.get("enabled", 1),
                 r.get("price_precision"), r.get("qty_precision"), r.get("tick_size"), r.get("step_size"),
                 int(time.time()))
                for r in rows
            ],
        )

    def enabled_symbols(self) -> list[str]:
        return [r["symbol"] for r in self.query("SELECT symbol FROM symbols WHERE enabled=1 ORDER BY symbol")]

    # ---------- signals ----------
    def insert_signal(self, s: dict) -> int:
        cur = self.execute(
            "INSERT INTO signals (created_at, symbol, tf, direction, kind, entry, sl, tp, rr, vol_ratio, strength, suggested_qty, risk_usdt, status, reason, extra) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["created_at"], s["symbol"], s["tf"], s["direction"], s["kind"], s["entry"], s["sl"], s["tp"],
             s["rr"], s["vol_ratio"], s["strength"], s.get("suggested_qty"), s.get("risk_usdt"),
             s.get("status", "new"), s.get("reason", ""), json.dumps(s.get("extra", {}), ensure_ascii=False)),
        )
        return cur.lastrowid

    def update_signal(self, sid: int, **fields: Any) -> None:
        keys = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE signals SET {keys} WHERE id=?", (*fields.values(), sid))

    def recent_signal(self, symbol: str, tf: str, direction: str, since_ts: int) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM signals WHERE symbol=? AND tf=? AND direction=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
            (symbol, tf, direction, since_ts),
        )

    # ---------- playbooks (预演) ----------
    def insert_playbook(self, p: dict) -> int:
        cur = self.execute(
            "INSERT INTO playbooks (created_at, symbol, tf, direction, title, entry, tp, sl, "
            "trigger_type, trigger_price, status, source, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), p["symbol"], p.get("tf"), p.get("direction"), p.get("title", ""),
             p.get("entry"), p.get("tp"), p.get("sl"), p.get("trigger_type", "price_reach"),
             p.get("trigger_price"), p.get("status", "active"), p.get("source", "manual"),
             json.dumps(p.get("extra", {}), ensure_ascii=False)),
        )
        return cur.lastrowid

    def active_playbooks(self, symbol: str | None = None, tf: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM playbooks WHERE status='active'"
        args: list = []
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        if tf:
            sql += " AND (tf=? OR tf IS NULL OR tf='')"
            args.append(tf)
        return self.query(sql, args)

    def list_playbooks(self, status: str = "", limit: int = 200) -> list[sqlite3.Row]:
        sql = "SELECT * FROM playbooks"
        args: list = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self.query(sql, args)

    def update_playbook(self, pid: int, **fields: Any) -> None:
        keys = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE playbooks SET {keys} WHERE id=?", (*fields.values(), pid))

    # ---------- watchlist (关注列表) ----------
    def add_watch(self, symbol: str, note: str = "", source: str = "manual") -> None:
        self.execute(
            "INSERT INTO watchlist (symbol, created_at, note, source, active) VALUES (?,?,?,?,1) "
            "ON CONFLICT(symbol) DO UPDATE SET note=excluded.note, active=1",
            (symbol, int(time.time()), note, source),
        )

    def remove_watch(self, symbol: str) -> None:
        self.execute("UPDATE watchlist SET active=0 WHERE symbol=?", (symbol,))

    def list_watch(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM watchlist WHERE active=1 ORDER BY created_at DESC")

    def watch_symbols(self) -> set[str]:
        return {r["symbol"] for r in self.query("SELECT symbol FROM watchlist WHERE active=1")}

    # ---------- settings ----------
    def get_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.query("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?,?,?)",
            (key, value, int(time.time())),
        )

    def log(self, level: str, source: str, message: str) -> None:
        self.execute(
            "INSERT INTO event_log (ts, level, source, message) VALUES (?,?,?,?)",
            (int(time.time()), level, source, message[:2000]),
        )
