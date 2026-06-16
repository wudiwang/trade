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
        """s: signals.Signal。track = 信号类型（watch/buy1/buy2/spring/chan），
        每种入场点独立统计胜率，用于验证哪个买点最有效。"""
        tracks = [s.extra.get("type", "other") if isinstance(s.extra, dict) else "other"]
        # 提示型信号(趋势反转/头肩顶等 kind=alert): 不开仓
        if getattr(s, "kind", "") == "alert":
            return
        # 威科夫确认型: 不单独开仓(只记信号, 用于与缠论重叠标注), 单独交易胜率太低
        if (isinstance(s.extra, dict) and s.extra.get("path") == "威科夫"
                and self.cfg.get("wyckoff.confirm_only", True)):
            return
        # paper 模式不设仓位上限：每个信号都开模拟单、都跟踪胜负，统计才完整(验证策略用)。
        # 仓位上限只在 live 实盘起作用(真钱风控)。
        live = self.cfg.mode == "live"
        for tr in tracks:
            if live:
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

    def close_opposite(self, symbol: str, tf: str, direction: str, price: float) -> list[dict]:
        """反向信号平仓：新信号方向为 direction 时，按市价(price)平掉同币同级别的反向持仓。
        即 持多(一买)遇一卖→平多；持空(一卖)遇一买→平空。result='rev'。返回平掉的单子。"""
        opp = "short" if direction == "long" else "long"
        rows = self.db.query(
            "SELECT * FROM paper_trades WHERE result='open' AND symbol=? AND tf=? AND direction=?",
            (symbol, tf, opp),
        )
        closed = []
        for r in rows:
            sign = 1 if r["direction"] == "long" else -1
            pnl = sign * (price - r["entry"]) * r["qty"]
            sl_dist = abs(r["entry"] - r["sl"])
            pnl_r = (sign * (price - r["entry"]) / sl_dist) if sl_dist > 0 else 0.0
            self.db.execute(
                "UPDATE paper_trades SET result='rev', exit_price=?, pnl=?, pnl_r=?, closed_at=? WHERE id=?",
                (price, round(pnl, 4), round(pnl_r, 3), int(time.time()), r["id"]),
            )
            self._update_equity(r["track"])
            closed.append({**dict(r), "result": "rev", "pnl": pnl, "pnl_r": pnl_r})
        return closed

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
            # 止损=打穿分型极值=买卖点失败, 直接把信号标记为失败一买/一卖
            if res == "sl" and r["signal_id"]:
                self.db.execute("UPDATE signals SET state='fail' WHERE id=? AND state!='ok'", (r["signal_id"],))
            self._update_equity(r["track"])
            closed.append({**dict(r), "result": res, "pnl": pnl, "pnl_r": pnl_r})
        return closed

    def _update_equity(self, track: str) -> None:
        base = self.cfg.get("risk.account_equity", 1000)
        s = self.db.one(
            "SELECT COALESCE(SUM(pnl),0) p FROM paper_trades WHERE track=? AND result IN ('tp','sl','rev')",
            (track,),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO equity_curve (ts, track, equity) VALUES (?,?,?)",
            (int(time.time()), track, round(base + s["p"], 4)),
        )

    def stats(self, track: str) -> dict:
        rows = self.db.query(
            "SELECT result, pnl, pnl_r FROM paper_trades WHERE track=? AND result IN ('tp','sl','rev')",
            (track,),
        )
        n = len(rows)
        wins = sum(1 for r in rows if (r["pnl_r"] or 0) > 0)   # tp/盈利反向平仓都算赢
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
