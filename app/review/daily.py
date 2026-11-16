"""每日实盘复盘总装：同步 Bybit(只读) → 重建回合 → 第一准则 → 系统匹配 → 报告 → 入库 / 推送。

回答的只有一个问题：「今天我有没有按规矩做？」。

三个入口共用本模块，行为完全一致：
  - 命令行        scripts/bybit_daily_review.py
  - 主进程后台任务  app/main.py  → scheduler_tick()      (每天 review.send_time 推送上一自然日)
  - Telegram 指令  app/bot/telegram.py → parse_review_command() / run_review() / deliver()

边界：
  - 对交易所严格只读：只通过 BybitReadClient（GET + 路径白名单 + 只读 Key 校验）。本模块不含
    任何下单、改单、撤单、划转代码。
  - 没有密钥时软失败：抛 ReviewUnavailable，message 是一句可以直接给本人看的中文；
    不会推送任何「已复盘」的消息。
  - 所有取数是 await 的网络 I/O；重建、判定、渲染、落库在工作线程里做（asyncio.to_thread），
    不占用交易引擎的事件循环。
  - 盈亏不参与任何合规判定：判定在 principles.py 里只看订单；这里只负责把数据送到位。

两套回合重建，各管各的：
  - 对账口径（沿用 2026-08 的闸门，一字未松）：只用窗口内成交 + 窗口起点的仓位，
    与流水、已结盈亏三方核对。
  - 复盘口径：往前多看 review.lookback_days 天的成交。这样隔夜持有、今天才离场的交易能看到
    它当初的进场与止损，而不是被当成「继承仓位不评价」——按第一准则拿到目标位的单子本来就常常过夜。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, time as dtime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from .bybit_client import BybitError, BybitReadClient, ReadOnlyKeyRequired, assign_position_idx
from .episodes import (closed_pnl_for_episode, derive_initial_positions,
                       episode_closed_pnl_difference, episode_net_from_executions,
                       episode_price_return_pct, missing_closed_pnl_orders, reconstruct,
                       unmatched_closed_orders)
from .models import (BEIJING, TRADING_TYPES, ClosedPnl, Execution, Kline, OrderRecord,
                     TradeEpisode, Transaction, summarize_ledger,
                     transaction_reconciliation_difference)
from .principles import (CheckState, PrincipleParams, PrincipleReview, day_score, review_day,
                         review_episode)
from .report import DeliveryIncomplete
from .store import ReviewStore
from .system_match import MatchParams, SystemFit, match_episode

log = logging.getLogger("review")

ROOT = Path(__file__).resolve().parents[2]
CATEGORY = "linear"
DAY_MS = 86_400_000
MINUTE_MS = 60_000
GRACE_MS = 600_000                 # 窗口结束不足 10 分钟：流水可能未到齐，不算已核对
DAILY_PREFIX = "daily:"            # 自动日报的投递记录前缀（与手动/命令行的区分开）
DAILY_PART_PREFIX = "daily-part:"  # 自动日报只送达了一部分时，记下已送达的那部分
DELIVERY_PARTS = ("summary", "document")

NO_CREDENTIALS_TEXT = (
    "还没配置 Bybit 只读密钥，复盘没有生成：请在 Bybit 新建一个【只读】API Key，"
    "自己把 BYBIT_API_KEY 和 BYBIT_API_SECRET 两行写进服务器的 .env 后重启，再发“复盘”。"
)
NOT_READ_ONLY_TEXT = (
    "这把 Bybit API Key 不是只读的，已拒绝使用，复盘没有生成：请在 Bybit 把它换成【只读】Key，"
    "更新服务器 .env 里的 BYBIT_API_KEY 和 BYBIT_API_SECRET 后重启。"
)
DISABLED_TEXT = (
    "实盘复盘还没开启，复盘没有生成：把 config.yaml 里 review.enabled 改成 true 后重启即可。"
)
BUSY_TEXT = "上一份复盘还在生成，请稍等它发出来。"


class BuildTimeout(TimeoutError):
    """整份复盘在 build_timeout_seconds 内没有生成完（不一定是 Bybit 慢）。"""


class ReviewUnavailable(RuntimeError):
    """软失败：message 是一句可以直接给本人看的中文，说明该做什么。"""

    def __init__(self, message: str, code: str = "unavailable"):
        super().__init__(message)
        self.message = message
        self.code = code


# --------------------------------------------------------------------------- 配置

@dataclass(frozen=True)
class ReviewSettings:
    enabled: bool = False
    telegram_enabled: bool = False
    timezone_name: str = "Asia/Shanghai"
    tz: tzinfo = BEIJING
    tz_offset_hours: float = 8
    tz_label: str = "北京时间"
    send_time: dtime = dtime(0, 10)
    db_path: Path = ROOT / "data" / "bybit_review.db"
    output_dir: Path = ROOT / "artifacts" / "bybit_reviews"
    lookback_days: int = 7             # 复盘口径往前多看几天成交/订单（找隔夜仓的进场）
    order_sync_minutes: int = 30       # 每隔多久留存一次挂单快照；0 = 不留
    max_late_hours: int = 6            # 错过 send_time 后多久之内还补发
    retry_attempts: int = 3
    retry_minutes: int = 5
    build_timeout_seconds: int = 600
    kline_timeout_seconds: int = 240   # 取 K 线一共最多花多久；到点就用已取到的出报告
    kline_pause_seconds: float = 0.2   # 相邻两次 K 线请求之间的间隔
    max_kline_requests: int = 300      # 单次复盘最多发多少次 K 线请求
    max_1m_bars: int = 12_000          # 单笔交易最多取多少根 1m
    trend_bars: int = 300              # 1h / 4h 各取入场前多少根
    ai_timeout_seconds: int = 120
    principles: PrincipleParams = PrincipleParams()
    match: MatchParams = MatchParams()


_ZONE_HOURS = {
    "asia/shanghai": 8, "asia/hong_kong": 8, "asia/singapore": 8, "asia/taipei": 8,
    "asia/bangkok": 7, "asia/ho_chi_minh": 7, "asia/jakarta": 7, "asia/tokyo": 9,
    "asia/seoul": 9, "utc": 0, "etc/utc": 0,
}
_ZONE_LABELS = {8: "北京时间", 7: "曼谷时间", 0: "UTC"}


def resolve_timezone(name: Any) -> tuple[tzinfo, float, str, str]:
    """(tzinfo, 小时偏移, 中文名, 规范名)。只支持固定偏移（没有夏令时的地区）。"""
    text = str(name or "").strip() or "Asia/Shanghai"
    key = text.lower()
    hours: float | None = _ZONE_HOURS.get(key)
    if hours is None:
        match = re.fullmatch(r"(?:utc|gmt)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", key)
        if match:
            hours = int(match.group(2)) + int(match.group(3) or 0) / 60
            hours = -hours if match.group(1) == "-" else hours
    if hours is None:
        try:
            from zoneinfo import ZoneInfo
            delta = datetime.now(ZoneInfo(text)).utcoffset() or timedelta(0)
            hours = delta.total_seconds() / 3600
        except Exception:  # noqa: BLE001 - 未知时区名：退回北京时间，并写日志
            log.warning("review.timezone=%r 无法识别，按 Asia/Shanghai 处理", text)
            text, hours = "Asia/Shanghai", 8
    if not -12 <= hours <= 14:
        log.warning("review.timezone=%r 偏移越界，按 Asia/Shanghai 处理", text)
        text, hours = "Asia/Shanghai", 8
    if hours == int(hours):
        hours = int(hours)
    zone = BEIJING if hours == 8 else timezone(timedelta(hours=hours), name=text)
    return zone, hours, _ZONE_LABELS.get(hours, f"UTC{hours:+g}"), text


def parse_send_time(value: Any) -> dtime:
    """"00:10" → time(0, 10)。YAML 不加引号时 00:10 会被读成整数 10（六十进制），也认。"""
    if isinstance(value, dtime):
        return value
    if isinstance(value, bool) or value is None:
        return dtime(0, 10)
    if isinstance(value, int):
        hours, minutes = divmod(value, 60)
        if 0 <= hours < 24:
            return dtime(hours, minutes)
    match = re.fullmatch(r"\s*(\d{1,2})[:：](\d{2})\s*", str(value))
    if match and int(match.group(1)) < 24 and int(match.group(2)) < 60:
        return dtime(int(match.group(1)), int(match.group(2)))
    log.warning("review.send_time=%r 无法识别，按 00:10 处理", value)
    return dtime(0, 10)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _coerce(current: Any, value: Any) -> Any:
    if isinstance(current, bool):
        return _truthy(value)
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, Decimal):
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("not finite")
        return number
    return value


def _params_from(cfg: Any, prefix: str, base: Any, skip: tuple[str, ...] = ()) -> Any:
    """按字段逐个 cfg.get（这样 settings 表的热覆盖也生效）；认不出的值保留默认并写日志。"""
    changes: dict[str, Any] = {}
    for item in fields(base):
        if item.name in skip:
            continue
        value = cfg.get(f"{prefix}.{item.name}")
        if value is None:
            continue
        try:
            changes[item.name] = _coerce(getattr(base, item.name), value)
        except (TypeError, ValueError, InvalidOperation):
            log.warning("配置 %s.%s=%r 无法识别，保留默认值", prefix, item.name, value)
    return replace(base, **changes) if changes else base


def _path(value: Any, default: Path) -> Path:
    if not value:
        return default
    path = Path(str(value))
    return path if path.is_absolute() else ROOT / path


def settings_from_config(cfg: Any) -> ReviewSettings:
    zone, hours, label, name = resolve_timezone(cfg.get("review.timezone", "Asia/Shanghai"))
    base = ReviewSettings()

    def number(key: str, default: Any, minimum: Any = 0) -> Any:
        raw = cfg.get(f"review.{key}")
        if raw is None:
            return default
        try:
            return max(minimum, type(default)(raw))
        except (TypeError, ValueError):
            log.warning("配置 review.%s=%r 无法识别，保留默认值", key, raw)
            return default

    principles = _params_from(cfg, "review.principles", PrincipleParams(tz_offset_hours=hours),
                              skip=("orders_synced_ms", "tz_offset_hours", "max_trades_per_day",
                                    "orders_complete_since_ms", "as_of_ms"))
    limit = cfg.get("review.principles.max_trades_per_day")
    try:
        limit = int(limit) if limit not in (None, "", False) else 0
    except (TypeError, ValueError):
        limit = 0
    principles = replace(principles, max_trades_per_day=limit if limit > 0 else None)
    match = _params_from(cfg, "review.match", MatchParams(tz_offset_hours=hours),
                         skip=("tz_offset_hours",))
    return ReviewSettings(
        enabled=_truthy(cfg.get("review.enabled", False)),
        telegram_enabled=_truthy(cfg.get("review.telegram_enabled", False)),
        timezone_name=name, tz=zone, tz_offset_hours=hours, tz_label=label,
        send_time=parse_send_time(cfg.get("review.send_time", "00:10")),
        db_path=_path(cfg.get("review.db_path"), base.db_path),
        output_dir=_path(cfg.get("review.output_dir"), base.output_dir),
        lookback_days=number("lookback_days", base.lookback_days, 1),
        order_sync_minutes=number("order_sync_minutes", base.order_sync_minutes),
        max_late_hours=number("max_late_hours", base.max_late_hours, 1),
        retry_attempts=number("retry_attempts", base.retry_attempts, 1),
        retry_minutes=number("retry_minutes", base.retry_minutes, 1),
        build_timeout_seconds=number("build_timeout_seconds", base.build_timeout_seconds, 30),
        kline_timeout_seconds=number("kline_timeout_seconds", base.kline_timeout_seconds, 5),
        kline_pause_seconds=number("kline_pause_seconds", base.kline_pause_seconds, 0.0),
        max_kline_requests=number("max_kline_requests", base.max_kline_requests),
        max_1m_bars=number("max_1m_bars", base.max_1m_bars, 1000),
        trend_bars=number("trend_bars", base.trend_bars, 150),
        ai_timeout_seconds=number("ai_timeout_seconds", base.ai_timeout_seconds, 1),
        principles=principles, match=match,
    )


def read_credentials(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """只从进程环境变量取（服务器 .env 由 app.config 在启动时载入）。从不打印、不落盘。"""
    env = os.environ if env is None else env
    key, secret = env.get("BYBIT_API_KEY", ""), env.get("BYBIT_API_SECRET", "")
    return (key, secret) if key and secret else None


def precheck(settings: ReviewSettings, env: Mapping[str, str] | None = None) -> str | None:
    """现在能不能生成复盘；不能的话返回一句该怎么做。"""
    if read_credentials(env) is None:
        return NO_CREDENTIALS_TEXT
    if not settings.enabled:
        return DISABLED_TEXT
    return None


_SECRET_PATTERNS = (
    (re.compile(r"https?://[^\s'\"<>]+"), "[链接已隐去]"),       # Telegram 的地址里带着 bot token
    (re.compile(r"bot\d+:[\w-]+"), "bot[已隐去]"),
)


def _scrub(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _is_http_client_error(exc: BaseException) -> bool:
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - aiohttp 是运行依赖
        return False
    return isinstance(exc, aiohttp.ClientError)


def describe_failure(exc: BaseException) -> str:
    """把异常压成一句不含密钥的中文。"""
    if isinstance(exc, ReviewUnavailable):
        return exc.message
    if isinstance(exc, ReadOnlyKeyRequired):
        return NOT_READ_ONLY_TEXT
    if isinstance(exc, DeliveryIncomplete):
        return _scrub(str(exc))[:160]
    if isinstance(exc, BuildTimeout):
        return f"生成复盘超时（{exc}），稍后可以重试。"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "连接 Bybit 超时，稍后可以重试。"
    if isinstance(exc, BybitError):
        return f"Bybit 返回错误：{_scrub(str(exc))[:160]}"
    name = type(exc).__name__
    # aiohttp 的异常文本里带请求地址（Telegram 的地址含 bot token）：只报类名，不带原文。
    if _is_http_client_error(exc):
        return f"连不上（{name}），稍后可以重试。"
    if name.startswith("Client") or isinstance(exc, OSError):
        return f"连不上 Bybit（{name}），稍后可以重试。"
    return f"{name}：{_scrub(str(exc))[:160]}"


# --------------------------------------------------------------------------- 请求与指令

@dataclass(frozen=True)
class ReviewRequest:
    day: str
    start_ms: int
    end_ms: int
    partial: bool
    label: str


def valid_day(text: Any) -> bool:
    if not isinstance(text, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def day_window(day: str | date, tz: tzinfo) -> tuple[int, int]:
    value = date.fromisoformat(day) if isinstance(day, str) else day
    start = datetime.combine(value, dtime.min, tzinfo=tz)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def make_request(day: str, settings: ReviewSettings, now_ms: int) -> ReviewRequest:
    """一个自然日的复盘窗口；这一天还没过完时，窗口截到 now，并标明「当日未结束」。"""
    if not valid_day(day):
        raise ReviewUnavailable(f"日期“{day}”不对，要写成 2026-11-16 这样。", "bad_day")
    start_ms, end_ms = day_window(day, settings.tz)
    if start_ms > now_ms:
        raise ReviewUnavailable(f"{day} 还没到，没有可复盘的成交。", "future_day")
    if end_ms > now_ms:
        clock = datetime.fromtimestamp(now_ms / 1000, settings.tz).strftime("%H:%M")
        return ReviewRequest(day, start_ms, max(start_ms + 1, now_ms), True,
                             f"{settings.tz_label} {day} 00:00–截至 {clock}（当日未结束）")
    return ReviewRequest(day, start_ms, end_ms, False, f"{settings.tz_label} {day} 00:00–24:00")


@dataclass(frozen=True)
class ReviewCommand:
    day: str | None
    error: str = ""


# 「复盘昨天」可以连写；英文 review 后面必须有空格（否则 reviewer 之类的词也会被当成指令）
_COMMAND_RE = re.compile(
    r"/?(?:(?:复盘|review)(?:@\w+)?(?:\s+(.*))?|复盘(\S.*))", re.I | re.S)
_RELATIVE_DAYS = {"": 0, "今天": 0, "今日": 0, "today": 0, "昨天": 1, "昨日": 1, "yesterday": 1,
                  "前天": 2}
_COMMAND_HELP = "没看懂要复盘哪一天：发“复盘”看今天到目前为止，或“复盘 昨天”、“复盘 2026-11-16”。"


def parse_review_command(text: Any, today: date) -> ReviewCommand | None:
    """识别「复盘」/「/review」指令。不是这类指令返回 None；是但日期看不懂时带一句 error。"""
    match = _COMMAND_RE.fullmatch(str(text or "").strip())
    if not match:
        return None
    arg = (match.group(1) or match.group(2) or "").strip().lower()
    if arg in _RELATIVE_DAYS:
        return ReviewCommand((today - timedelta(days=_RELATIVE_DAYS[arg])).isoformat())
    found = (re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", arg)
             or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", arg))
    short = re.fullmatch(r"(\d{1,2})[-/.月](\d{1,2})日?", arg)
    try:
        if found:
            day = date(int(found.group(1)), int(found.group(2)), int(found.group(3)))
        elif short:
            day = date(today.year, int(short.group(1)), int(short.group(2)))
            if day > today:                      # “12-30” 在 1 月说的是去年
                day = date(today.year - 1, day.month, day.day)
        else:
            # 「复盘」后面跟着一长句话，多半是在聊天而不是下指令
            return None if len(arg) > 12 else ReviewCommand(None, _COMMAND_HELP)
    except ValueError:
        return ReviewCommand(None, _COMMAND_HELP)
    if day > today:
        return ReviewCommand(None, f"{day.isoformat()} 还没到，没有可复盘的成交。")
    return ReviewCommand(day.isoformat())


# --------------------------------------------------------------------------- 取数

@dataclass
class DayInputs:
    """一次同步从交易所读到的全部账户数据（都是只读接口的结果）。"""
    now_ms: int
    lookback_start_ms: int
    executions: list[Execution]
    transactions: list[Transaction]
    closed: list[ClosedPnl]
    orders: list[OrderRecord]                      # 订单历史 + 当前挂单（本次同步看到的状态）
    positions_before: dict[tuple[str, str, int], Decimal]
    positions_after: dict[tuple[str, str, int], Decimal]


async def fetch_account(client: Any, request: ReviewRequest, settings: ReviewSettings,
                        now_ms: int, clock: Callable[[], int] | None = None) -> DayInputs:
    """顺序发起的十来个只读 GET。先验 Key 只读；不是只读直接抛 ReadOnlyKeyRequired。

    成交必须和仓位快照对得上，否则「由当前仓位倒推期初仓位」会凭空造出一笔继承仓位：
      - clock（实盘运行时传入）：读完仓位之后再定成交的截止时刻，避免「成交落在 now_ms 与读仓位之间」；
      - 读成交前后两次仓位不一致（取数期间又成交了）：整体重取一次。
    """
    await client.verify_read_only()
    lookback_start = request.start_ms - settings.lookback_days * DAY_MS
    for attempt in (1, 2):
        positions_before = await client.positions(CATEGORY)
        if clock is not None:
            now_ms = max(now_ms, int(clock()))
        end = max(lookback_start + 1, now_ms)
        executions = await client.executions(CATEGORY, lookback_start, end)
        transactions = await client.transactions(CATEGORY, request.start_ms, request.end_ms)
        closed = await client.closed_pnl(CATEGORY, request.start_ms, request.end_ms)
        history = await client.order_history(CATEGORY, lookback_start, end)
        open_now = await client.open_orders(CATEGORY)
        positions_after = await client.positions(CATEGORY)
        if dict(positions_before) == dict(positions_after):
            break
        if attempt == 1:
            log.info("取数期间仓位发生变化，重取一次")
    return DayInputs(now_ms, lookback_start, list(executions), list(transactions), list(closed),
                     [*history, *open_now], dict(positions_before), dict(positions_after))


class KlineBook:
    """按需取 K 线：同一品种同一级别不重复取；有请求上限和间隔；取不到只记一笔，不抛。"""

    def __init__(self, client: Any, *, now_ms: int, pause: float = 0.2, budget: int = 300):
        self._client = client
        self._now_ms = now_ms
        self._pause = max(0.0, float(pause))
        self._budget = max(0, int(budget))
        self._bars: dict[tuple[str, int], dict[int, Kline]] = {}
        self._covered: dict[tuple[str, int], list[tuple[int, int]]] = {}
        self.requests = 0
        self.exhausted = False
        self.dead = False                 # 行情接口超时或连续出错：这次复盘不再取 K 线
        self.timed_out = False            # 取 K 线的总时限到了
        self._failures = 0
        self.errors: list[str] = []

    def _missing(self, key: tuple[str, int], start: int, end: int) -> list[tuple[int, int]]:
        gaps, cursor = [], start
        for left, right in sorted(self._covered.get(key, [])):
            if right <= cursor:
                continue
            if left >= end:
                break
            if left > cursor:
                gaps.append((cursor, left))
            cursor = max(cursor, right)
        if cursor < end:
            gaps.append((cursor, end))
        return gaps

    async def ensure(self, symbol: str, minutes: int, start_ms: int, end_ms: int) -> bool:
        """保证 [start_ms, end_ms) 已取过（截到 now）。返回是否完整取到。"""
        step = minutes * MINUTE_MS
        start = start_ms // step * step
        end = min(-(-end_ms // step) * step, -(-self._now_ms // step) * step)
        if start >= end:
            return True
        key = (symbol, minutes)
        for left, right in self._missing(key, start, end):
            cursor = left
            while cursor < right:
                chunk_end = min(right, cursor + 1000 * step)      # 一次请求最多 1000 根
                if self.dead:
                    return False
                if self.requests >= self._budget:
                    self.exhausted = True
                    return False
                self.requests += 1
                try:
                    rows = await self._client.klines(CATEGORY, symbol, str(minutes), cursor, chunk_end)
                except Exception as exc:  # noqa: BLE001 - 行情取不到不该让整份复盘失败
                    self.errors.append(f"{symbol} {minutes}m：{describe_failure(exc)}")
                    self._failures += 1
                    # 一次超时就要等几十秒：行情接口挂住时不再一笔一笔地等，硬规矩照常出。
                    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or self._failures >= 3:
                        self.dead = True
                    return False
                self._failures = 0
                store = self._bars.setdefault(key, {})
                for row in rows:
                    store[row.start_ms] = row
                self._covered.setdefault(key, []).append((cursor, chunk_end))
                cursor = chunk_end
                if self._pause:
                    await asyncio.sleep(self._pause)
        return True

    def get(self, symbol: str, minutes: int, start_ms: int | None = None,
            end_ms: int | None = None) -> list[Kline]:
        rows = self._bars.get((symbol, minutes), {})
        return [rows[key] for key in sorted(rows)
                if (start_ms is None or key >= start_ms) and (end_ms is None or key < end_ms)]


# --------------------------------------------------------------------------- 重建与判定

@dataclass
class Prepared:
    inputs: DayInputs
    orders: list[OrderRecord]                # 历史留存的快照 + 本次同步
    episodes: list[TradeEpisode]             # 复盘口径：与窗口有交集的回合
    gate_episodes: list[TradeEpisode]        # 对账口径：只用窗口内成交
    window_executions: list[Execution]
    params: PrincipleParams
    first_pass: dict[str, PrincipleReview]
    extra_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # 窗口开始前不久才平掉的回合：只给当日检查当「前一笔」用（跨零点的反手 / 止损后重进）
    context_episodes: list[TradeEpisode] = field(default_factory=list)
    context_reviews: list[PrincipleReview] = field(default_factory=list)


def _first_fill_ms(episode: TradeEpisode) -> int:
    stamps = [fill.execution.timestamp_ms for fill in episode.fills]
    return min(stamps) if stamps else episode.opened_ms


def _last_fill_ms(episode: TradeEpisode) -> int:
    stamps = [fill.execution.timestamp_ms for fill in episode.fills]
    return max(stamps) if stamps else episode.opened_ms


def _entry_ms(episode: TradeEpisode) -> int | None:
    if episode.inherited_position:
        return None
    stamps = [fill.execution.timestamp_ms for fill in episode.fills if fill.action == "entry"]
    return min(stamps) if stamps else None


def orders_complete_since(sync_times: list[int] | None, now_ms: int, retention_hours: int) -> int:
    """从哪个时刻起，被取消的订单保证都留在手里（本次同步 + 本地历次快照）。

    交易所只返回最近 retention 小时内被取消的订单，所以一次同步覆盖 [同步时刻 - retention, 同步时刻]。
    从这次同步往回数，只要相邻两次同步的覆盖区间接得上，覆盖就一直连续到更早。
    """
    keep_ms = max(0, int(retention_hours) * 3_600_000 - 10 * MINUTE_MS)      # 留 10 分钟余量
    covered = now_ms - keep_ms
    for stamp in sorted({int(value) for value in sync_times or [] if int(value) <= now_ms},
                        reverse=True):
        if stamp < covered:
            break
        covered = min(covered, stamp - keep_ms)
    return covered


def prepare(inputs: DayInputs, request: ReviewRequest, settings: ReviewSettings,
            stored_orders: list[OrderRecord] | None = None,
            sync_times: list[int] | None = None) -> Prepared:
    orders = [*(stored_orders or []), *inputs.orders]
    # 成交接口不带 positionIdx：先按订单补上，否则双向持仓的多空两条腿会被并成一笔。
    fills, unresolved = assign_position_idx(inputs.executions, orders)
    extra_reasons: list[str] = []
    notes: list[str] = []
    hedge_symbols = ({order.symbol for order in orders if order.position_idx in (1, 2)}
                     | {key[1] for key in inputs.positions_after if key[2] in (1, 2)})
    unresolved_ids = set(unresolved)
    blind = [row for row in fills if row.exec_id in unresolved_ids
             and row.symbol in hedge_symbols and row.timestamp_ms < request.end_ms]
    if blind:
        symbols = "、".join(sorted({row.symbol for row in blind}))
        extra_reasons.append(f"{symbols} 用了双向持仓，其中 {len(blind)} 笔成交找不到对应订单，"
                             f"分不清属于多头还是空头")

    # 对账口径：与 2026-08 的实现相同——窗口内成交 + 由当前仓位倒推的窗口起点仓位。
    window = [row for row in fills if request.start_ms <= row.timestamp_ms < request.end_ms]
    since_start = [row for row in fills if row.timestamp_ms >= request.start_ms]
    gate_episodes = reconstruct(window, derive_initial_positions(inputs.positions_after, since_start),
                                window_start_ms=request.start_ms)

    # 复盘口径：从 lookback 起点重建，只截到窗口结束（报告反映的是「那一天结束时」的状态）。
    until_end = [row for row in fills if row.timestamp_ms < request.end_ms]
    initial = derive_initial_positions(inputs.positions_after, fills)
    lookback = reconstruct(until_end, initial, window_start_ms=inputs.lookback_start_ms)
    episodes = [episode for episode in lookback
                if episode.opened_ms < request.end_ms
                and (episode.closed_ms is None or episode.closed_ms >= request.start_ms)]
    touched = {(episode.category, episode.symbol, episode.position_idx) for episode in episodes
               if episode.inherited_position}
    for key in sorted(key for key in initial if key in touched):
        notes.append(f"{key[1]}：{settings.lookback_days} 天回看期开始之前就有仓位，或成交记录与当前仓位"
                     f"对不上；这段成交按继承仓位处理，不评价")
    if inputs.positions_before != inputs.positions_after:
        notes.append("取数期间仓位仍在变化，最近的成交可能没有取全，逐笔重建可能不准")

    # 成交截到窗口结束，订单证据也截到同一刻：窗口结束后才发生的撤单/改单不记到这一天头上。
    cutoff = request.end_ms if request.end_ms < inputs.now_ms else None
    params = replace(
        settings.principles, orders_synced_ms=inputs.now_ms,
        tz_offset_hours=settings.tz_offset_hours, as_of_ms=cutoff,
        orders_complete_since_ms=orders_complete_since(
            sync_times, inputs.now_ms, settings.principles.cancelled_order_retention_hours))
    first_pass = {episode.episode_id: review_episode(episode, orders, [], params)
                  for episode in episodes}
    # 跨零点的反手 / 止损后重进：把窗口开始前不久平掉的回合也交给当日检查，只当「前一笔」。
    reach = max(settings.principles.flip_minutes, settings.principles.reentry_minutes) * MINUTE_MS
    context = [episode for episode in lookback
               if episode.closed_ms is not None
               and request.start_ms - reach <= episode.closed_ms < request.start_ms]
    context_reviews = [review_episode(episode, orders, [], params) for episode in context]
    return Prepared(inputs, orders, episodes, gate_episodes, window, params, first_pass,
                    extra_reasons, notes, context, context_reviews)


def _manual_exit_to_compare(review: PrincipleReview) -> int | None:
    """这笔是否需要「中途下车的代价」：有手动离场，且事前的止损和止盈都在。返回首次手动离场时刻。"""
    check = review.checks.get("exit_by_plan")
    if check is None or check.state is not CheckState.FAIL:
        return None
    if review.planned_sl is None or review.planned_tp is None:
        return None
    stamps = [leg.time_ms for leg in review.exits if leg.kind == "manual"]
    return min(stamps) if stamps else None


def _touched(bars: list[Kline], review: PrincipleReview, direction: str) -> bool:
    """只用来决定「还要不要继续往后取 K 线」；真正的先后判定在 principles 里。"""
    stop, target = review.planned_sl, review.planned_tp
    for bar in bars:
        if direction == "long" and (bar.low <= stop or bar.high >= target):
            return True
        if direction == "short" and (bar.high >= stop or bar.low <= target):
            return True
    return False


async def fetch_klines(book: KlineBook, prepared: Prepared, settings: ReviewSettings) -> None:
    """每笔交易：1m（图 + 离场后的走势）、15m（图 + 结构）、5m / 1h / 4h（入场前的趋势与触发）。"""
    now_ms = prepared.inputs.now_ms
    trend = settings.trend_bars + 5
    for episode in prepared.episodes:
        symbol = episode.symbol
        first, last = _first_fill_ms(episode), _last_fill_ms(episode)
        start_1m = first - 60 * MINUTE_MS
        end_1m = min(now_ms, last + 240 * MINUTE_MS)
        start_1m = max(start_1m, end_1m - settings.max_1m_bars * MINUTE_MS)
        await book.ensure(symbol, 1, start_1m, end_1m)
        await book.ensure(symbol, 15, first - 330 * 15 * MINUTE_MS, min(now_ms, last + 720 * MINUTE_MS))
        entry = _entry_ms(episode)
        if entry is not None:
            await book.ensure(symbol, 5, entry - 330 * 5 * MINUTE_MS, entry + 5 * MINUTE_MS)
            await book.ensure(symbol, 60, entry - trend * 60 * MINUTE_MS, entry + 60 * MINUTE_MS)
            await book.ensure(symbol, 240, entry - trend * 240 * MINUTE_MS, entry + 240 * MINUTE_MS)
        review = prepared.first_pass[episode.episode_id]
        exit_ms = _manual_exit_to_compare(review)
        if exit_ms is None:
            continue
        # 手动离场之后：一段一段往后取，直到原止损/原止盈被碰到，或到了观察期上限。
        limit = min(now_ms, exit_ms + settings.principles.horizon_hours * 60 * MINUTE_MS)
        cursor = max(end_1m, exit_ms)
        if _touched(book.get(symbol, 1, exit_ms + MINUTE_MS, cursor), review, episode.direction):
            continue
        while cursor < limit:
            chunk_end = min(limit, cursor + 1000 * MINUTE_MS)
            if not await book.ensure(symbol, 1, cursor, chunk_end):
                break
            if _touched(book.get(symbol, 1, cursor, chunk_end), review, episode.direction):
                break
            cursor = chunk_end


def _plain(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def _chart_rows(bars: list[Kline]) -> list[list[Any]]:
    return [[bar.start_ms, format(bar.open, "f"), format(bar.high, "f"), format(bar.low, "f"),
             format(bar.close, "f")] for bar in bars]


def _episode_net(episode: TradeEpisode, closed: list[ClosedPnl]) -> Decimal | None:
    """本笔已结盈亏（只用于展示，不参与任何判定）。"""
    if episode.status != "closed":
        return None
    closing = {fill.execution.order_id for fill in episode.fills if fill.action in ("reduce", "close")}
    if closing and closing <= {row.order_id for row in closed}:
        return closed_pnl_for_episode(episode, closed)
    return episode_net_from_executions(episode)      # 平仓跨了多天：用成交价与手续费自算


def _episode_dict(episode: TradeEpisode, closed: list[ClosedPnl]) -> dict[str, Any]:
    return {
        "episode_id": episode.episode_id, "category": episode.category, "symbol": episode.symbol,
        "direction": episode.direction, "position_idx": episode.position_idx,
        "status": episode.status, "inherited_position": episode.inherited_position,
        "opened_ms": episode.opened_ms, "closed_ms": episode.closed_ms,
        "max_qty": episode.max_abs_qty,
        "net_pnl": _episode_net(episode, closed),
        "price_return_pct": episode_price_return_pct(episode),
        "fills": [{
            "exec_id": fill.execution.exec_id, "order_id": fill.execution.order_id,
            "action": fill.action, "side": fill.execution.side,
            "time": fill.execution.timestamp_ms, "price": fill.execution.price,
            "qty": fill.execution.qty,
        } for fill in episode.fills],
    }


def _ledger(prepared: Prepared, request: ReviewRequest) -> dict[str, Any]:
    """三方对账。判定条件与 2026-08 的实现逐条相同；唯一新增的是「当天完全没有交易」的空日。"""
    inputs = prepared.inputs
    transactions, closed, episodes = inputs.transactions, inputs.closed, prepared.gate_episodes
    ledger = summarize_ledger(transactions)
    unmatched = unmatched_closed_orders(episodes, closed)
    missing = missing_closed_pnl_orders(episodes, closed)
    difference = transaction_reconciliation_difference(transactions)
    execution_fee_difference = abs(
        sum((row.fee for row in prepared.window_executions), Decimal("0")) - ledger.fees)
    independent = [episode_net_from_executions(episode) for episode in episodes]
    fully_observed = bool(episodes) and all(value is not None for value in independent)
    closed_total = sum((row.closed_pnl for row in closed), Decimal("0"))
    closed_pnl_difference = episode_closed_pnl_difference(episodes, closed)
    ledger_closed_difference = abs(ledger.net_change - closed_total) if fully_observed else None
    funding_present = any(row.funding != 0 for row in transactions)
    grace_elapsed = request.end_ms <= inputs.now_ms - GRACE_MS
    stable = inputs.positions_before == inputs.positions_after
    cent = Decimal("0.01")
    reconciled = (
        grace_elapsed and stable and fully_observed and not funding_present
        and difference <= cent and execution_fee_difference <= cent
        and closed_pnl_difference is not None and closed_pnl_difference <= cent
        and ledger_closed_difference is not None and ledger_closed_difference <= cent
        and not unmatched and not missing
    )
    trading_rows = [row for row in transactions if row.tx_type.upper() in TRADING_TYPES]
    empty_day = not prepared.window_executions and not closed and not trading_rows
    if empty_day and grace_elapsed and stable:
        reconciled = True                 # 没有成交、没有已结盈亏、没有交易流水：没有可对的账
    if prepared.extra_reasons:
        reconciled = False
    return {
        "status": "reconciled" if reconciled else "mismatch",
        "net_pnl": ledger.net_change, "closed_pnl": closed_total,
        "fees": ledger.fees, "funding": ledger.funding,
        "external_change": ledger.external_change,
        "reconciliation_difference": difference,
        "execution_fee_difference": execution_fee_difference,
        "closed_pnl_difference": closed_pnl_difference,
        "ledger_closed_difference": ledger_closed_difference,
        "position_snapshot_stable": stable, "funding_present": funding_present,
        "grace_elapsed": grace_elapsed, "empty_day": empty_day,
        "unmatched_closed_orders": sorted(unmatched),
        "missing_closed_pnl_orders": sorted(missing),
    }


def assemble(prepared: Prepared, book: KlineBook, request: ReviewRequest,
             settings: ReviewSettings, source: str = "bybit") -> dict[str, Any]:
    """纯计算：逐笔第一准则 + 系统匹配 + 当日检查 + 对账，拼成约定的 report 字典。"""
    from .narrator import AI_ENV_FLAG, generate_narrative
    from .report import reconciliation_reasons

    inputs = prepared.inputs
    ledger = _ledger(prepared, request)
    # 趋势判定用的根数封顶在「每笔自己取的那么多根」：同品种别的交易多取到的 K 线不会改变结果。
    match_params = replace(settings.match, tz_offset_hours=settings.tz_offset_hours,
                           trend_max_bars=min(settings.match.trend_max_bars, settings.trend_bars))
    use_ai = os.environ.get(AI_ENV_FLAG) == "1" and ledger["status"] == "reconciled"

    trades: list[dict[str, Any]] = []
    reviews: list[PrincipleReview] = []
    trend = settings.trend_bars + 5
    ai_deadline = time.monotonic() + settings.ai_timeout_seconds     # AI 润色：整份报告共用一个时限
    for episode in prepared.episodes:
        symbol = episode.symbol
        first, last = _first_fill_ms(episode), _last_fill_ms(episode)
        entry = _entry_ms(episode)

        def before(minutes: int, count: int) -> list[Kline]:
            """这一笔自己取的那段 K 线（与 fetch_klines 的范围相同）。同品种别的交易取到的
            更早的 K 线不能混进来，否则同一笔交易的趋势判定会随「当天还有没有别的单」而变。"""
            if entry is None:
                return []
            step = minutes * MINUTE_MS
            return book.get(symbol, minutes, (entry - count * step) // step * step, entry + step)

        review = review_episode(episode, prepared.orders, book.get(symbol, 1), prepared.params)
        fit: SystemFit = match_episode(episode, {
            "5m": before(5, 330), "15m": before(15, 330),
            "1h": before(60, trend), "4h": before(240, trend),
        }, match_params)
        reviews.append(review)
        item: dict[str, Any] = {
            "episode": _episode_dict(episode, inputs.closed),
            "principles": asdict(review),
            "fit": asdict(fit),
            "charts": {
                "15m": {"bars": _chart_rows(book.get(
                    symbol, 15, first - 80 * 15 * MINUTE_MS, last + 720 * MINUTE_MS)), "markers": []},
                "1m": {"bars": _chart_rows(book.get(
                    symbol, 1, first - 45 * MINUTE_MS, last + 240 * MINUTE_MS)), "markers": []},
            },
        }
        remaining = ai_deadline - time.monotonic()
        if use_ai and remaining >= 5:
            # 这里在工作线程里，外层的超时取消不了它：所有交易共用一个时限，到点就只用规则文字。
            narrative = generate_narrative(review, timeout_seconds=max(1, int(remaining)))
            if narrative.source == "ai":
                item["narrative"] = asdict(narrative)
        trades.append(item)

    # 当日层面：窗口开始前就已开着的仓位不算「今天新开」，也不会被算成今天的反手/重进；
    # 窗口开始前不久平掉的回合只当「前一笔」（跨零点的反手记在后一笔开仓的这一天）。
    day_checks = review_day([*prepared.context_episodes, *prepared.episodes],
                            [*prepared.context_reviews, *reviews], prepared.params,
                            window_start_ms=request.start_ms)

    notes = list(prepared.notes)
    if book.exhausted:
        notes.append(f"K线请求数达到上限 {settings.max_kline_requests} 次，部分交易缺图或缺趋势判定")
    if book.timed_out:
        notes.append(f"取K线超过 {settings.kline_timeout_seconds} 秒，已按取到的部分出报告；"
                     f"部分交易缺图或缺趋势判定，硬规矩不受影响")
    if book.errors:
        notes.append("部分K线没有取到（硬规矩不受影响）：" + "；".join(book.errors[:3]))

    report: dict[str, Any] = {
        "schema": 2,
        "source": source,
        "date": request.day,
        "window_label": request.label + (" · 示例数据" if source == "fixture" else ""),
        "window_start_ms": request.start_ms, "window_end_ms": request.end_ms,
        "generated_ms": inputs.now_ms,
        "partial": request.partial,
        "timezone": settings.timezone_name, "tz_offset_hours": settings.tz_offset_hours,
        "status": ledger["status"],
        "trades": trades,
        "score": day_score(reviews),
        "day_checks": [asdict(check) for check in day_checks],
        "ledger": ledger,
        "data_notes": notes,
        "covered_categories": ["linear/USDT"],
        "scope_label": "Bybit USDT 永续",
    }
    if ledger["status"] != "reconciled":
        reasons = reconciliation_reasons(report)
        if prepared.extra_reasons:
            generic = f"对账状态为 {ledger['status']}"
            reasons = [text for text in reasons if text != generic] + prepared.extra_reasons
        report["reconciliation_reasons"] = reasons
    # 落盘前先过一遍 JSON：内存里的报告与库里读回来的报告完全相同（Decimal 以字符串保存）。
    return json.loads(json.dumps(report, ensure_ascii=False, default=_json_default))


async def build_report(client: Any, request: ReviewRequest, settings: ReviewSettings, *,
                       now_ms: int, stored_orders: list[OrderRecord] | None = None,
                       source: str = "bybit", sync_times: list[int] | None = None,
                       clock: Callable[[], int] | None = None) -> tuple[dict[str, Any], DayInputs]:
    inputs = await fetch_account(client, request, settings, now_ms, clock)
    now_ms = inputs.now_ms
    prepared = await asyncio.to_thread(prepare, inputs, request, settings, stored_orders, sync_times)
    book = KlineBook(client, now_ms=now_ms, pause=settings.kline_pause_seconds,
                     budget=settings.max_kline_requests)
    try:
        # K 线只用于图、趋势和事后对照；它慢或挂住都不能拖垮整份复盘（硬规矩只看订单）。
        await asyncio.wait_for(fetch_klines(book, prepared, settings), settings.kline_timeout_seconds)
    except (asyncio.TimeoutError, TimeoutError):
        book.dead = book.timed_out = True
        log.warning("取 K 线超过 %s 秒，按已取到的部分出报告", settings.kline_timeout_seconds)
    report = await asyncio.to_thread(assemble, prepared, book, request, settings, source)
    return report, inputs


# --------------------------------------------------------------------------- 落盘 / 入库

@dataclass(frozen=True)
class ReviewResult:
    day: str
    report: dict[str, Any]
    json_path: Path
    html_path: Path
    png_path: Path | None          # None：这台机器没有能画中文的字体，不出图（只发文字摘要）
    text_path: Path


def write_report(report: dict[str, Any], output_root: Path) -> ReviewResult:
    from .image import NoCjkFont, render_summary_png
    from .report import render_html, telegram_summary

    target = Path(output_root) / str(report["date"])
    target.mkdir(parents=True, exist_ok=True)
    json_path, html_path = target / "review.json", target / "report.html"
    png_path, text_path = target / "summary.png", target / "telegram.txt"
    # newline="\n"：同一份报告在 Windows 和 Linux 上写出的文件逐字节相同
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
                         newline="\n")
    html_path.write_text(render_html(report), encoding="utf-8", newline="\n")
    text_path.write_text(telegram_summary(report) + "\n", encoding="utf-8", newline="\n")
    try:
        render_summary_png(report, png_path)
    except NoCjkFont as exc:
        # 画出来只会是一排方框：宁可不发图，摘要文字照发。
        log.warning("摘要图没有生成：%s", exc)
        png_path.unlink(missing_ok=True)
        return ReviewResult(str(report["date"]), report, json_path, html_path, None, text_path)
    return ReviewResult(str(report["date"]), report, json_path, html_path, png_path, text_path)


def delivery_fingerprint(report: Mapping[str, Any]) -> str:
    """同一份结论只推一次：只看日期、窗口、对账状态、金额、每笔的结论与成交，不看措辞。"""
    ledger = report.get("ledger") if isinstance(report.get("ledger"), Mapping) else {}
    basis = {
        "date": report.get("date"), "window_start_ms": report.get("window_start_ms"),
        "window_end_ms": report.get("window_end_ms"), "status": report.get("status"),
        "net_pnl": ledger.get("net_pnl"), "fees": ledger.get("fees"),
        "funding": ledger.get("funding"),
        "trades": [{
            "episode_id": (row.get("episode") or {}).get("episode_id"),
            "verdict": (row.get("principles") or {}).get("verdict"),
            "exec_ids": [fill.get("exec_id") for fill in (row.get("episode") or {}).get("fills", [])],
        } for row in report.get("trades", [])],
    }
    payload = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_stored_orders(db_path: Path, since_ms: int) -> list[OrderRecord]:
    store = ReviewStore(db_path)
    try:
        return store.order_observations(since_ms)
    finally:
        store.close()


def _load_sync_times(db_path: Path, since_ms: int) -> list[int]:
    store = ReviewStore(db_path)
    try:
        return store.order_sync_times(since_ms)
    finally:
        store.close()


def _start_sync(db_path: Path, request: ReviewRequest) -> int:
    store = ReviewStore(db_path)
    try:
        return store.start_sync(request.start_ms, request.end_ms)
    finally:
        store.close()


def _finish_sync(db_path: Path, run_id: int, status: str, error: str = "") -> None:
    store = ReviewStore(db_path)
    try:
        store.finish_sync(run_id, status, error)
    finally:
        store.close()


def _persist(report: dict[str, Any], inputs: DayInputs, db_path: Path, output_dir: Path,
             run_id: int) -> ReviewResult:
    result = write_report(report, output_dir)
    store = ReviewStore(db_path)
    try:
        store.upsert_executions(inputs.executions)
        store.upsert_transactions(inputs.transactions)
        store.upsert_closed_pnl(inputs.closed)
        store.upsert_orders(inputs.orders, CATEGORY, seen_ms=inputs.now_ms)
        store.replace_review(result.day, report)
        store.replace_page(result.day, report, result.html_path.read_text(encoding="utf-8"))
        store.finish_sync(run_id, "success")
    finally:
        store.close()
    return result


def _store_orders(db_path: Path, orders: list[OrderRecord], seen_ms: int) -> int:
    store = ReviewStore(db_path)
    try:
        return store.upsert_orders(orders, CATEGORY, seen_ms=seen_ms)
    finally:
        store.close()


def _daily_delivered(db_path: Path, day: str) -> bool:
    store = ReviewStore(db_path)
    try:
        return store.has_delivery(day, DAILY_PREFIX)
    finally:
        store.close()


def _delivered_parts(db_path: Path, day: str) -> frozenset[str]:
    """自动日报已经送达的部分（重试时只补没送到的，不重发已经收到的摘要）。"""
    store = ReviewStore(db_path)
    try:
        return frozenset(part for part in DELIVERY_PARTS
                         if store.has_delivery(day, DAILY_PART_PREFIX + part))
    finally:
        store.close()


def _mark_delivered(db_path: Path, day: str, content_hash: str) -> None:
    store = ReviewStore(db_path)
    try:
        store.mark_delivered(day, content_hash)
    finally:
        store.close()


# --------------------------------------------------------------------------- 运行

_LOCKS: dict[int, asyncio.Lock] = {}


def build_lock() -> asyncio.Lock:
    """同一事件循环里同时只生成一份复盘（后台任务、Telegram 指令共用）。"""
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(id(loop))
    if lock is None:
        _LOCKS.clear()
        lock = _LOCKS[id(loop)] = asyncio.Lock()
    return lock


def client_from_env(env: Mapping[str, str] | None = None) -> BybitReadClient:
    credentials = read_credentials(env)
    if credentials is None:
        raise ReviewUnavailable(NO_CREDENTIALS_TEXT, "no_credentials")
    return BybitReadClient(*credentials)


async def run_review(settings: ReviewSettings, request: ReviewRequest, *, client: Any = None,
                     now_ms: int | None = None, env: Mapping[str, str] | None = None,
                     source: str = "bybit") -> ReviewResult:
    """生成并保存一天的复盘。没有密钥 / Key 不是只读 → ReviewUnavailable（一句中文）。"""
    if client is None:
        client = client_from_env(env)
    clock: Callable[[], int] | None = None
    if now_ms is None:
        # 实盘运行：成交的截止时刻在读完仓位之后再定（见 fetch_account）
        clock = lambda: int(datetime.now(timezone.utc).timestamp() * 1000)  # noqa: E731
        now_ms = clock()
    async with build_lock():
        run_id = await asyncio.to_thread(_start_sync, settings.db_path, request)
        try:
            since = request.start_ms - settings.lookback_days * DAY_MS
            stored = await asyncio.to_thread(_load_stored_orders, settings.db_path, since)
            sync_times = await asyncio.to_thread(_load_sync_times, settings.db_path, since - DAY_MS)
            try:
                report, inputs = await asyncio.wait_for(
                    build_report(client, request, settings, now_ms=now_ms, stored_orders=stored,
                                 source=source, sync_times=sync_times, clock=clock),
                    settings.build_timeout_seconds)
            except (asyncio.TimeoutError, TimeoutError) as exc:
                if isinstance(exc, BuildTimeout):
                    raise
                raise BuildTimeout(f"超过 {settings.build_timeout_seconds} 秒") from exc
            return await asyncio.to_thread(_persist, report, inputs, settings.db_path,
                                           settings.output_dir, run_id)
        except ReadOnlyKeyRequired as exc:
            await asyncio.to_thread(_finish_sync, settings.db_path, run_id, "failed", "not read-only")
            raise ReviewUnavailable(NOT_READ_ONLY_TEXT, "not_read_only") from exc
        except Exception as exc:
            await asyncio.to_thread(_finish_sync, settings.db_path, run_id, "failed",
                                    describe_failure(exc))
            raise


async def deliver(bot: Any, result: ReviewResult, settings: ReviewSettings, *,
                  daily: bool = False, skip: frozenset[str] = frozenset()) -> None:
    """把一份已生成的复盘发到 Telegram。未结束的日子在最上面写明「未结束」。

    skip：已经送达、这次不要再发的部分（"summary" / "document"）。只送达一部分时抛
    DeliveryIncomplete；自动日报会把已送达的部分记进库里，重试只补缺的。
    """
    from .report import deliver_review, telegram_summary

    report = result.report
    if report.get("source") == "fixture":
        raise ReviewUnavailable("示例数据不能发到 Telegram。", "fixture")
    headline = ""
    if report.get("partial"):
        clock = datetime.fromtimestamp(int(report["window_end_ms"]) / 1000, settings.tz)
        headline = (f"⏳ <b>{report['date']} 未结束</b> · 截至 {clock:%H:%M}（{settings.tz_label}），"
                    f"以下只是到目前为止")
    try:
        if report.get("trades"):
            await deliver_review(bot, report, result.png_path, result.html_path, headline=headline,
                                 skip=skip)
        elif "summary" not in skip:
            text = telegram_summary(report)
            sent = await bot.notify(f"{headline}\n{text}" if headline else text)
            if not (sent is True or (isinstance(sent, dict) and sent.get("ok"))):
                raise DeliveryIncomplete("Telegram 文本发送失败")
    except DeliveryIncomplete as exc:
        if daily:
            for part in sorted(exc.sent - skip):
                await asyncio.to_thread(_mark_delivered, settings.db_path, result.day,
                                        DAILY_PART_PREFIX + part)
        raise
    if daily:
        await asyncio.to_thread(_mark_delivered, settings.db_path, result.day,
                                DAILY_PREFIX + delivery_fingerprint(report))


async def sync_orders(settings: ReviewSettings, *, client: Any = None, now_ms: int | None = None,
                      env: Mapping[str, str] | None = None) -> int:
    """留存一次挂单/订单快照（只读）。交易所只保留订单的最新状态，且已取消的止损单 24 小时后
    就查不到了；想判断「止损有没有被放宽」「当时有没有止损」，只能靠自己定时记下来。"""
    if client is None:
        client = client_from_env(env)
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    await client.verify_read_only()
    history = await client.order_history(CATEGORY, now_ms - 2 * DAY_MS, now_ms)
    open_now = await client.open_orders(CATEGORY)
    return await asyncio.to_thread(_store_orders, settings.db_path, [*history, *open_now], now_ms)


# --------------------------------------------------------------------------- 后台调度

@dataclass
class SchedulerState:
    done_day: str | None = None                    # 已推送（或已确定不再尝试）的那一天
    attempts: dict[str, int] = field(default_factory=dict)
    last_attempt: datetime | None = None
    last_order_sync: datetime | None = None
    warned_no_credentials: bool = False
    pending: ReviewResult | None = None            # 已生成、还没完整送达的那一份（重试时不再重建）


def due_review_day(now: datetime, send_time: dtime, max_late_hours: int = 6) -> str | None:
    """now（review.timezone 下的时刻）是否落在某天 send_time 起 max_late_hours 小时之内；
    是的话返回「刚结束的那一天」= 该 send_time 所在日期的前一天。"""
    for offset in (0, 1):
        day = now.date() - timedelta(days=offset)
        fire = datetime.combine(day, send_time, tzinfo=now.tzinfo)
        if fire <= now < fire + timedelta(hours=max_late_hours):
            return (day - timedelta(days=1)).isoformat()
    return None


async def review_tick(cfg: Any, bot: Any, state: SchedulerState, *, now: datetime | None = None,
                      env: Mapping[str, str] | None = None, runner: Any = None,
                      sender: Any = None) -> str:
    """后台任务每分钟调用一次。返回本次做了什么（便于测试与日志）。"""
    settings = settings_from_config(cfg)
    if not (settings.enabled and settings.telegram_enabled):
        return "off"
    if not getattr(bot, "enabled", False):
        return "bot_off"
    if read_credentials(env) is None:
        if not state.warned_no_credentials:
            state.warned_no_credentials = True
            log.warning("实盘复盘已开启但没有 Bybit 只读密钥，不会生成也不会推送：%s", NO_CREDENTIALS_TEXT)
        return "no_credentials"
    now = now.astimezone(settings.tz) if now is not None else datetime.now(settings.tz)
    day = due_review_day(now, settings.send_time, settings.max_late_hours)
    if day is None or state.done_day == day:
        return "idle"
    attempts = state.attempts.get(day, 0)
    if attempts >= settings.retry_attempts:
        return "gave_up"
    if attempts and state.last_attempt is not None \
            and now - state.last_attempt < timedelta(minutes=settings.retry_minutes):
        return "waiting"
    if await asyncio.to_thread(_daily_delivered, settings.db_path, day):
        state.done_day = day                      # 重启后不重复推送
        return "already_sent"
    state.attempts = {day: attempts + 1}
    state.last_attempt = now
    runner = runner or run_review
    sender = sender or deliver
    already = await asyncio.to_thread(_delivered_parts, settings.db_path, day)
    try:
        if state.pending is not None and getattr(state.pending, "day", None) == day and already:
            result = state.pending                 # 摘要已经送到：只补发缺的部分，不重新同步
        else:
            request = make_request(day, settings, int(now.timestamp() * 1000))
            result = await runner(settings, request, env=env)
        state.pending = result
        if already:
            await sender(bot, result, settings, daily=True, skip=already)
        else:
            await sender(bot, result, settings, daily=True)
    except ReviewUnavailable as exc:
        state.done_day = day
        state.pending = None
        log.warning("%s 的实盘复盘没有生成：%s", day, exc.message)
        await bot.notify(f"⚠️ {day} 的实盘复盘没有生成：{exc.message}")
        return "unavailable"
    except Exception as exc:  # noqa: BLE001 - 复盘失败不该影响主进程，记日志后按次数重试
        log.warning("%s 的实盘复盘第 %d 次尝试失败：%s", day, attempts + 1, describe_failure(exc))
        if attempts + 1 >= settings.retry_attempts:
            arrived = already | (exc.sent if isinstance(exc, DeliveryIncomplete) else frozenset())
            if "summary" in arrived:
                await bot.notify(f"⚠️ {day} 的复盘摘要已发出，完整 HTML 没发出去"
                                 f"（试了 {attempts + 1} 次）；可以发“复盘 {day}”再要一次。")
            elif "document" in arrived:
                await bot.notify(f"⚠️ {day} 的完整复盘（HTML 附件）已发出，但摘要没发出去"
                                 f"（试了 {attempts + 1} 次）。")
            else:
                await bot.notify(f"⚠️ {day} 的实盘复盘没有生成（试了 {attempts + 1} 次）："
                                 f"{describe_failure(exc)}")
        return "failed"
    state.done_day = day
    state.pending = None
    log.info("%s 的实盘复盘已推送", day)
    return "sent"


async def order_sync_tick(cfg: Any, state: SchedulerState, *, now: datetime | None = None,
                          env: Mapping[str, str] | None = None, syncer: Any = None) -> str:
    settings = settings_from_config(cfg)
    if not settings.enabled or settings.order_sync_minutes <= 0:
        return "off"
    if read_credentials(env) is None:
        return "no_credentials"
    now = now or datetime.now(settings.tz)
    if state.last_order_sync is not None \
            and now - state.last_order_sync < timedelta(minutes=settings.order_sync_minutes):
        return "idle"
    if build_lock().locked():
        return "busy"
    state.last_order_sync = now
    added = await (syncer or sync_orders)(settings, env=env)
    return f"synced:{added}"


async def scheduler_tick(cfg: Any, bot: Any, state: SchedulerState, *,
                         now: datetime | None = None,
                         env: Mapping[str, str] | None = None) -> dict[str, str]:
    """两件事各自隔离：一件失败不影响另一件，也不向调用方抛异常。"""
    outcome: dict[str, str] = {}
    for name, call in (("orders", lambda: order_sync_tick(cfg, state, now=now, env=env)),
                       ("review", lambda: review_tick(cfg, bot, state, now=now, env=env))):
        try:
            outcome[name] = await call()
        except Exception as exc:  # noqa: BLE001
            outcome[name] = "error"
            log.warning("实盘复盘后台任务（%s）出错：%s", name, describe_failure(exc))
    return outcome
