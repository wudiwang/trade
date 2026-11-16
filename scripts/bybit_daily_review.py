"""每日实盘复盘（命令行）：只读拉取 Bybit 成交 → 逐笔对照第一准则 → 生成报告。

用法：
  python scripts/bybit_daily_review.py --date 2026-11-16      某个自然日（review.timezone）
  python scripts/bybit_daily_review.py --today                今天到目前为止（标「当日未结束」）
  python scripts/bybit_daily_review.py --fixture tests/fixtures/bybit_review/day_full.json
                                                              用示例数据跑一遍（不联网、不要密钥）
  加 --send-telegram 把结果发到 Telegram（示例数据永远不发）。

对交易所严格只读。没有密钥时只打印一句该怎么做，退出码 2，不会有任何推送。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Keep Chinese diagnostics readable when Windows redirects stdout/stderr (for
# example from Task Scheduler or a supervising process).
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.review import daily
from app.review.bybit_client import BybitReadClient
from app.review.daily import ReviewUnavailable, delivery_fingerprint  # noqa: F401 (re-exported)
from app.review.fixture_exchange import FixtureError, FixtureExchange
from app.review.store import ReviewStore

EXIT_OK, EXIT_FAILED, EXIT_UNAVAILABLE, EXIT_FIXTURE_SEND = 0, 1, 2, 4


def load_settings() -> daily.ReviewSettings:
    """读 config.yaml 的 review 段（app.config 启动时也会把服务器 .env 载入环境变量）。"""
    from app.config import Config
    return daily.settings_from_config(Config())


async def send(result: daily.ReviewResult, settings: daily.ReviewSettings) -> None:
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
        raise ReviewUnavailable(
            "没有 Telegram 配置，报告已生成但没有发送：请确认服务器 .env 里有 "
            "TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID。", "no_telegram")
    bot = TgBot(config, NoopDB())
    try:
        await daily.deliver(bot, result, settings)
    finally:
        await bot.stop()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="每日实盘复盘（Bybit 只读）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date", help="自然日 YYYY-MM-DD（默认：示例数据用它自带的日期，否则今天）")
    group.add_argument("--today", action="store_true", help="今天到目前为止")
    parser.add_argument("--fixture", type=Path, help="示例数据文件（不联网、不需要密钥）")
    parser.add_argument("--output-dir", type=Path, help="报告输出目录")
    parser.add_argument("--db", type=Path, help="复盘库路径")
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument("--debug", action="store_true", help="出错时打印完整堆栈")
    return parser.parse_args(argv)


async def run(args) -> int:
    settings = load_settings()
    fixture = FixtureExchange.load(args.fixture) if args.fixture else None
    if fixture is not None and args.send_telegram:
        print("示例数据（fixture）不会发到 Telegram，本次没有生成也没有发送。", file=sys.stderr)
        return EXIT_FIXTURE_SEND
    if fixture is not None:
        # 示例数据默认写到自己的目录和库里，不混进真实复盘。
        output_dir = args.output_dir or settings.output_dir.parent / "review_sample"
        settings = replace(settings, output_dir=output_dir,
                           db_path=args.db or output_dir / "fixture_review.db",
                           kline_pause_seconds=0.0)
        now_ms = fixture.now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
        client = BybitReadClient("fixture", "fixture", requester=fixture.requester)
        day = args.date or fixture.window.get("day")
        source = "fixture"
    else:
        settings = replace(settings, output_dir=args.output_dir or settings.output_dir,
                           db_path=args.db or settings.db_path)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        client = None                                  # run_review 从环境变量取只读密钥
        day = args.date
        source = "bybit"
    if not day:
        day = datetime.fromtimestamp(now_ms / 1000, settings.tz).date().isoformat()
    request = daily.make_request(day, settings, now_ms)
    # 实盘不传 now_ms：成交的截止时刻由 run_review 在读完仓位之后再定，成交与仓位才对得上。
    result = await daily.run_review(settings, request, client=client,
                                    now_ms=now_ms if fixture is not None else None, source=source)
    report = result.report
    if args.send_telegram:
        content_hash = delivery_fingerprint(report)
        store = ReviewStore(settings.db_path)
        try:
            already = store.was_delivered(result.day, content_hash)
        finally:
            store.close()
        if not already:
            await send(result, settings)
            store = ReviewStore(settings.db_path)
            try:
                store.mark_delivered(result.day, content_hash)
            finally:
                store.close()
    score = report.get("score") or {}
    print(json.dumps({
        "json": str(result.json_path), "html": str(result.html_path),
        "png": None if result.png_path is None else str(result.png_path),   # None：本机没有中文字体
        "telegram_text": str(result.text_path), "date": result.day,
        "status": report.get("status"), "partial": report.get("partial"),
        "trades": score.get("total"), "compliant": score.get("compliant"),
        "by_verdict": {key: value for key, value in (score.get("by_verdict") or {}).items() if value},
    }, ensure_ascii=False))
    return EXIT_OK


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except ReviewUnavailable as exc:
        print(exc.message, file=sys.stderr)
        return EXIT_UNAVAILABLE
    except FixtureError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - 命令行只给一句话；要堆栈加 --debug
        if args.debug:
            raise
        print(f"复盘没有生成：{daily.describe_failure(exc)}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
