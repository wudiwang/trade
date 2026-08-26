"""Generate a read-only Bybit daily trade review."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Keep Chinese diagnostics readable when Windows redirects stdout/stderr (for
# example from Task Scheduler or a supervising process).
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.review.bybit_client import BybitReadClient
from app.review.discipline import evaluate_episode
from app.review.episodes import closed_pnl_for_episode, reconstruct
from app.review.image import render_summary_png
from app.review.models import BEIJING, beijing_day, summarize_ledger
from app.review.narrator import generate_narrative
from app.review.report import deliver_review, render_html
from app.review.store import ReviewStore


def _bars(rows):
    return [[row.start_ms, float(row.open), float(row.high), float(row.low), float(row.close)] for row in rows]


def _fills(episode):
    return [{
        "exec_id": item.execution.exec_id,
        "action": item.action,
        "time": item.execution.timestamp_ms,
        "price": float(item.execution.price),
        "qty": float(item.execution.qty),
    } for item in episode.fills]


def _evidence(review):
    return [{
        "label": item.label, "state": item.state.value, "group": item.group,
        "value": str(item.value) if item.value is not None else None, "detail": item.detail,
    } for item in review.evidence.values()]


async def build_live_report(day: str, start_ms: int, end_ms: int,
                            store: ReviewStore | None = None) -> dict:
    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")
    if not api_key or not api_secret:
        raise PermissionError("缺少 Bybit 只读 API 配置；未读取或修改 .env")
    client = BybitReadClient(api_key, api_secret)
    await client.verify_read_only()
    executions, transactions, closed, wallet = await asyncio.gather(
        client.executions("linear", start_ms - 86_400_000, end_ms),
        client.transactions("linear", start_ms, end_ms),
        client.closed_pnl("linear", start_ms, end_ms),
        client.wallet_balance(),
    )
    if store is not None:
        store.upsert_executions(executions)
        store.upsert_transactions(transactions)
        store.upsert_closed_pnl(closed)
    episodes = reconstruct(executions)
    ledger = summarize_ledger(transactions)
    report_episodes = []
    verdict_counts = {"合规": 0, "部分合规": 0, "冲动交易": 0, "严重变形": 0}
    for episode in episodes:
        if not (start_ms <= episode.opened_ms < end_ms or
                episode.closed_ms is not None and start_ms <= episode.closed_ms < end_ms):
            continue
        context_start = episode.opened_ms - 12 * 60 * 60 * 1000
        context_end = (episode.closed_ms or end_ms) + 2 * 60 * 60 * 1000
        k1m, k15m = await asyncio.gather(
            client.klines(episode.category, episode.symbol, "1", context_start, context_end),
            client.klines(episode.category, episode.symbol, "15", context_start, context_end),
        )
        review = evaluate_episode(
            episode, k1m, k15m, {}, {"has_risk_boundary": None, "light_limit_pct": None}
        )
        narrative = await asyncio.to_thread(generate_narrative, review)
        verdict_counts[review.verdict] += 1
        pnl = closed_pnl_for_episode(episode, closed)
        entry = episode.fills[0].execution.price
        exit_price = episode.fills[-1].execution.price
        move = ((entry - exit_price) / entry if episode.direction == "short" else
                (exit_price - entry) / entry) * 100 if entry else Decimal("0")
        report_episodes.append({
            "episode_id": episode.episode_id, "symbol": episode.symbol,
            "direction": episode.direction, "verdict": review.verdict,
            "net_pnl": float(pnl), "price_return_pct": float(move),
            "narrative": asdict(narrative), "evidence": _evidence(review),
            "fills": _fills(episode), "k1m": _bars(k1m), "k15m": _bars(k15m),
        })
    start_equity = wallet["total_equity"] - ledger.net_change
    account_return = ledger.net_change / start_equity * 100 if start_equity else Decimal("0")
    difference = ledger.net_change - sum((row.closed_pnl for row in closed), Decimal("0")) \
        - ledger.funding + ledger.fees
    reconciled = abs(difference) <= Decimal("0.01")
    action = "下单前先确认15分钟结构，并写明失效价。"
    return {
        "date": day,
        "window_label": f"北京时间 {day} 00:00–24:00",
        "window_start_ms": start_ms, "window_end_ms": end_ms,
        "status": "reconciled" if reconciled else "mismatch",
        "reconciliation_difference": float(difference),
        "net_pnl": float(ledger.net_change),
        "closed_pnl": float(ledger.closed_cash_flow),
        "fees": float(ledger.fees), "funding": float(ledger.funding),
        "account_return_pct": float(account_return), "episodes": report_episodes,
        "verdict_counts": verdict_counts, "tomorrow_action": action,
        "equity_basis": "reconstructed_from_current_equity",
    }


def write_report(report: dict, output_root: Path) -> tuple[Path, Path, Path]:
    target = output_root / report["date"]
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "review.json"
    html_path = target / "report.html"
    png_path = target / "summary.png"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    render_summary_png(report, png_path)
    return json_path, html_path, png_path


async def send(report: dict, png_path: Path, html_path: Path) -> None:
    from app.bot.telegram import TgBot
    from app.config import get_config

    class NoopDB:
        pass

    bot = TgBot(get_config(), NoopDB())
    try:
        await deliver_review(bot, report, png_path, html_path)
    finally:
        await bot.stop()


def parse_args():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date")
    group.add_argument("--today", action="store_true")
    group.add_argument("--last-24h", action="store_true")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts" / "bybit_reviews")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "bybit_review.db")
    parser.add_argument("--send-telegram", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = datetime.now(BEIJING)
    day = args.date or now.date().isoformat()
    start_ms, end_ms = beijing_day(day)
    if args.last_24h:
        end_ms = int(now.timestamp() * 1000)
        start_ms = end_ms - 86_400_000
        day = f"last24h-{now:%Y%m%d-%H%M}"
    store = ReviewStore(args.db)
    try:
        if args.fixture:
            report = json.loads(args.fixture.read_text(encoding="utf-8"))
            report.setdefault("window_label", f"北京时间 {day} 00:00–24:00")
        else:
            report = asyncio.run(build_live_report(day, start_ms, end_ms, store))
    except PermissionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    json_path, html_path, png_path = write_report(report, args.output_dir)
    store.replace_review(day, report)
    if args.send_telegram:
        if report.get("status") != "reconciled":
            print("日报尚未通过对账，禁止发送已核准报告", file=sys.stderr)
            return 3
        asyncio.run(send(report, png_path, html_path))
    print(json.dumps({"json": str(json_path), "html": str(html_path), "png": str(png_path),
                      "net_pnl": report["net_pnl"], "status": report.get("status")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
