"""Paper 模拟盘：信号自动虚拟成交，按后续K线判定止盈/止损，统计两套门槛的真实期望。

track 说明：
  rr5  — 只收 RR>=5 的主信号（用户要求的门槛）
  rr25 — 收 RR>=2.5 的全部信号（对照组）
同一信号若 RR>=5 会同时进两个 track，各自独立结算。
"""
import logging
import time

log = logging.getLogger("paper")


class PaperBroker:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db

    def open_from_signal(self, signal_id: int, s) -> None:
        """s: signals.Signal。按 track 规则开虚拟仓。"""
        tracks = ["rr25"]
        if s.rr >= self.cfg.get("signal.min_rr_primary", 5.0):
            tracks.append("rr5")
        for tr in tracks:
            # 每个track检查最大持仓数
            open_cnt = self.db.one(
                "SELECT COUNT(*) c FROM paper_trades WHERE result='open' AND track=?", (tr,)
            )["c"]
            if open_cnt >= self.cfg.get("risk.max_positions", 5):
                log.info("track %s 已满仓(%d)，跳过 %s", tr, open_cnt, s.symbol)
                continue
            self.db.execute(
                "INSERT INTO paper_trades (signal_id, symbol, tf, direction, track, entry, sl, tp, qty, opened_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (signal_id, s.symbol, s.tf, s.direction, tr, s.entry, s.sl, s.tp,
                 s.suggested_qty, int(time.time())),
            )

    def on_closed_bar(self, symbol: str, tf: str, bar: tuple) -> list[dict]:
        """bar=(open_time,o,h,l,c,v,qv,closed)。检查该币种未平仓单的TP/SL。
        同一根K同时触及TP和SL时按SL算（保守）。返回平掉的单子列表。"""
        _, _, high, low, close, *_ = bar
        rows = self.db.query(
            "SELECT * FROM paper_trades WHERE result='open' AND symbol=? AND tf=?",
            (symbol, tf),
        )
        closed = []
        for r in rows:
            res = exit_price = None
            if r["direction"] == "long":
                if low <= r["sl"]:
                    res, exit_price = "sl", r["sl"]
                elif high >= r["tp"]:
                    res, exit_price = "tp", r["tp"]
            else:
                if high >= r["sl"]:
                    res, exit_price = "sl", r["sl"]
                elif low <= r["tp"]:
                    res, exit_price = "tp", r["tp"]
            if res is None:
                continue
            sign = 1 if r["direction"] == "long" else -1
            pnl = sign * (exit_price - r["entry"]) * r["qty"]
            sl_dist = abs(r["entry"] - r["sl"])
            pnl_r = (sign * (exit_price - r["entry"]) / sl_dist) if sl_dist > 0 else 0.0
            self.db.execute(
                "UPDATE paper_trades SET result=?, exit_price=?, pnl=?, pnl_r=?, closed_at=? WHERE id=?",
                (res, exit_price, round(pnl, 4), round(pnl_r, 3), int(time.time()), r["id"]),
            )
            self._update_equity(r["track"])
            closed.append({**dict(r), "result": res, "pnl": pnl, "pnl_r": pnl_r})
        return closed

    def _update_equity(self, track: str) -> None:
        base = self.cfg.get("risk.account_equity", 1000)
        s = self.db.one(
            "SELECT COALESCE(SUM(pnl),0) p FROM paper_trades WHERE track=? AND result IN ('tp','sl')",
            (track,),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO equity_curve (ts, track, equity) VALUES (?,?,?)",
            (int(time.time()), track, round(base + s["p"], 4)),
        )

    def stats(self, track: str) -> dict:
        rows = self.db.query(
            "SELECT result, pnl, pnl_r FROM paper_trades WHERE track=? AND result IN ('tp','sl')",
            (track,),
        )
        n = len(rows)
        wins = sum(1 for r in rows if r["result"] == "tp")
        total_pnl = sum(r["pnl"] or 0 for r in rows)
        total_r = sum(r["pnl_r"] or 0 for r in rows)
        open_cnt = self.db.one(
            "SELECT COUNT(*) c FROM paper_trades WHERE track=? AND result='open'", (track,)
        )["c"]
        return {
            "track": track, "closed": n, "open": open_cnt,
            "wins": wins, "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "total_pnl": round(total_pnl, 2),
            "avg_r": round(total_r / n, 3) if n else 0.0,
            "expectancy_r": round(total_r / n, 3) if n else 0.0,
        }
