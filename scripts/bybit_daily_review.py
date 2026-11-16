"""Generate a read-only Bybit daily trade review."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
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
from app.review.episodes import (closed_pnl_for_episode, derive_initial_positions,
                                 episode_closed_pnl_difference, episode_net_from_executions,
                                 episode_price_return_pct, missing_closed_pnl_orders, reconstruct,
                                 unmatched_closed_orders)
from app.review.image import render_summary_png
from app.review.models import (BEIJING, beijing_day, summarize_ledger,
                               transaction_reconciliation_difference)
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
    positions_before = await client.positions("linear")
    as_of_ms = int(datetime.now(BEIJING).timestamp() * 1000) + 1
    all_executions, transactions, closed, wallet = await asyncio.gather(
        client.executions("linear", start_ms, max(start_ms + 1, as_of_ms)),
        client.transactions("linear", start_ms, end_ms),
        client.closed_pnl("linear", start_ms, end_ms),
        client.wallet_balance(),
    )
    current_positions = await client.positions("linear")
    position_snapshot_stable = positions_before == current_positions
    executions = [row for row in all_executions if start_ms <= row.timestamp_ms < end_ms]
    if store is not None:
        store.upsert_executions(all_executions)
        store.upsert_transactions(transactions)
        store.upsert_closed_pnl(closed)
    initial_positions = derive_initial_positions(current_positions, all_executions)
    episodes = reconstruct(executions, initial_positions, window_start_ms=start_ms)
    ledger = summarize_ledger(transactions)
    unmatched = unmatched_closed_orders(episodes, closed)
    missing = missing_closed_pnl_orders(episodes, closed)
    difference = transaction_reconciliation_difference(transactions)
    execution_fee_difference = abs(
        sum((row.fee for row in executions), Decimal("0")) - ledger.fees
    )
    independent_nets = [episode_net_from_executions(episode) for episode in episodes]
    fully_observed = bool(episodes) and all(value is not None for value in independent_nets)
    closed_total = sum((row.closed_pnl for row in closed), Decimal("0"))
    closed_pnl_difference = episode_closed_pnl_difference(episodes, closed)
    ledger_closed_difference = abs(ledger.net_change - closed_total) if fully_observed else None
    funding_present = any(row.funding != 0 for row in transactions)
    grace_elapsed = end_ms <= int(datetime.now(BEIJING).timestamp() * 1000) - 600_000
    reconciled = (
        grace_elapsed and position_snapshot_stable and fully_observed and not funding_present and
        difference <= Decimal("0.01") and execution_fee_difference <= Decimal("0.01") and
        closed_pnl_difference is not None and closed_pnl_difference <= Decimal("0.01") and
        ledger_closed_difference is not None and ledger_closed_difference <= Decimal("0.01") and
        not unmatched and not missing
    )
    report_episodes = []
    verdict_counts = {"合规": 0, "部分合规": 0, "冲动交易": 0, "严重变形": 0,
                      "证据不足": 0, "暂不适用": 0}
    for episode in episodes:
        if not (start_ms <= episode.opened_ms < end_ms or
                episode.closed_ms is not None and start_ms <= episode.closed_ms < end_ms):
            continue
        if any(fill.execution.order_id in unmatched for fill in episode.fills):
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
        if episode.inherited_position:
            narrative = {
                "summary": "本笔持仓在复盘窗口开始前已经存在，不评价原始入场纪律。",
                "reasons": ["窗口内仅能确认加减仓或平仓动作"],
                "next_action": "如需评价入场，请选择包含首次建仓的更早日期。",
                "confidence": "high", "source": "window_guard",
            }
            verdict = "入场不在窗口"
        elif reconciled:
            narrative = asdict(await asyncio.to_thread(generate_narrative, review))
            verdict = review.verdict
        else:
            narrative = {
                "summary": "底层数据尚未通过对账，暂不生成纪律结论。",
                "reasons": ["先修复成交回合或流水差异"],
                "next_action": "完成对账后重新生成本笔复盘。",
                "confidence": "low", "source": "reconciliation_guard",
            }
            verdict = "数据待核对"
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        pnl = closed_pnl_for_episode(episode, closed)
        move = episode_price_return_pct(episode)
        report_episodes.append({
            "episode_id": episode.episode_id, "symbol": episode.symbol,
            "direction": episode.direction, "verdict": verdict,
            "status": episode.status,
            "inherited_position": episode.inherited_position,
            "net_pnl": float(pnl) if episode.status == "closed" else None,
            "price_return_pct": float(move) if move is not None else None,
            "narrative": narrative, "evidence": _evidence(review),
            "fills": _fills(episode), "k1m": _bars(k1m), "k15m": _bars(k15m),
        })
    action = "下单前先确认15分钟结构，并写明失效价。"
    return {
        "date": day,
        "window_label": f"北京时间 {day} 00:00–24:00",
        "window_start_ms": start_ms, "window_end_ms": end_ms,
        "status": "reconciled" if reconciled else "mismatch",
        "reconciliation_difference": float(difference),
        "execution_fee_difference": float(execution_fee_difference),
        "closed_pnl_difference": (float(closed_pnl_difference)
                                  if closed_pnl_difference is not None else None),
        "ledger_closed_difference": (float(ledger_closed_difference)
                                      if ledger_closed_difference is not None else None),
        "position_snapshot_stable": position_snapshot_stable,
        "funding_present": funding_present,
        "grace_elapsed": grace_elapsed,
        "net_pnl": float(ledger.net_change),
        "closed_pnl": float(sum((row.closed_pnl for row in closed), Decimal("0"))),
        "fees": float(ledger.fees), "funding": float(ledger.funding),
        "account_return_pct": None, "episodes": report_episodes,
        "verdict_counts": verdict_counts, "tomorrow_action": action,
        "equity_basis": "unavailable_without_midnight_snapshot",
        "current_equity": float(wallet["total_equity"]),
        "unmatched_closed_orders": sorted(unmatched),
        "missing_closed_pnl_orders": sorted(missing),
        "covered_categories": ["linear/USDT"],
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


def delivery_fingerprint(report: dict) -> str:
    basis = {
        "date": report.get("date"), "window_start_ms": report.get("window_start_ms"),
        "window_end_ms": report.get("window_end_ms"), "status": report.get("status"),
        "net_pnl": report.get("net_pnl"), "fees": report.get("fees"),
        "funding": report.get("funding"),
        "episodes": [{
            "episode_id": row.get("episode_id"), "net_pnl": row.get("net_pnl"),
            "verdict": row.get("verdict"),
            "exec_ids": [fill.get("exec_id") for fill in row.get("fills", [])],
        } for row in report.get("episodes", [])],
    }
    payload = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def send(report: dict, png_path: Path, html_path: Path) -> None:
    from app.bot.telegram import TgBot

    class EnvTelegramConfig:
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

        @staticmethod
        def get(key, default=None):
            return True if key == "telegram.enabled" else default

    class NoopDB:
        pass

    config = EnvTelegramConfig()
    if not config.tg_token or not config.tg_chat_id:
        raise PermissionError("缺少 Telegram 环境变量；不会读取 .env")
    bot = TgBot(config, NoopDB())
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
    partial_today = bool(args.today)
    if partial_today:
        end_ms = int(now.timestamp() * 1000)
    if args.last_24h:
        end_ms = int(now.timestamp() * 1000)
        start_ms = end_ms - 86_400_000
        day = f"last24h-{now:%Y%m%d-%H%M}"
    store = ReviewStore(args.db)
    run_id = store.start_sync(start_ms, end_ms)
    try:
        if args.fixture:
            report = json.loads(args.fixture.read_text(encoding="utf-8"))
            report.setdefault("window_label", f"北京时间 {day} 00:00–24:00")
        else:
            report = asyncio.run(build_live_report(day, start_ms, end_ms, store))
        if partial_today:
            report["window_label"] = f"北京时间 {day} 00:00–截至 {now:%H:%M}"
        json_path, html_path, png_path = write_report(report, args.output_dir)
        store.replace_review(day, report)
        if args.send_telegram:
            if args.fixture:
                store.finish_sync(run_id, "success")
                print("合成 fixture 禁止发送到 Telegram", file=sys.stderr)
                return 4
            if report.get("status") != "reconciled":
                store.finish_sync(run_id, "success")
                print("日报尚未通过对账，禁止发送已核准报告", file=sys.stderr)
                return 3
            content_hash = delivery_fingerprint(report)
            if not store.was_delivered(day, content_hash):
                asyncio.run(send(report, png_path, html_path))
                store.mark_delivered(day, content_hash)
        store.finish_sync(run_id, "success")
        print(json.dumps({"json": str(json_path), "html": str(html_path), "png": str(png_path),
                          "net_pnl": report["net_pnl"], "status": report.get("status")}, ensure_ascii=False))
        return 0
    except PermissionError as exc:
        store.finish_sync(run_id, "failed", str(exc))
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        store.finish_sync(run_id, "failed", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
