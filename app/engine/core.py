"""引擎主循环：币种管理 → 回填 → WS实时 → 收盘评估 → 信号入库/推送 → paper结算。"""
import asyncio
import collections
import logging
import time
from typing import Awaitable, Callable

from .alerts import AlertEngine
from .binance_rest import BinanceRest
from .binance_ws import KlineWS
from .linked_rules import SIGNAL_STATUS as LINKED_SIGNAL_STATUS
from .linked_rules import LinkedRuleRunner, rule_entries
from .paper import PaperBroker
from .signals import SignalEngine

log = logging.getLogger("core")

COLS = ("open_time", "open", "high", "low", "close", "volume", "quote_volume", "taker_buy", "closed")
RISK_CACHE_SECONDS = 5
TRANSFER_HISTORY_MAX_AGE_SECONDS = 89 * 24 * 60 * 60
POSITION_RESERVATION_SECONDS = 15
MAX_PENDING_NOTICES = 64          # 后台待发的提醒任务上限(Telegram 卡住时不让任务无限堆积)
LINKED_TOPUP_TIMEOUT_S = 20       # 启动时为联动规则补历史K线, 每个品种最多等这么久(被限流时不拖住启动)
# 提醒发送失败后的重试间隔(秒): 一条提醒是一次性的(价位已标记 fired / 待命已占位), 发不出去就永远没了,
# 所以在后台任务里多试几次。全部试完仍失败 → notice_failed 计数, 在「提醒」列表和网页卡片上能看到。
NOTICE_RETRY_DELAYS_S = (2.0, 6.0, 20.0)
NOTICE_RETRY_MAX_S = 60.0         # 对方要求的等待(Telegram 429 retry_after)最多照办这么久
NOTICE_BACKLOG_MAX = 50           # 重试用尽仍没发出去的提醒, 最多留这么多条等补发
NOTICE_BACKLOG_RETRY_S = 60.0     # 待补发的每隔这么久再试一轮
NOTICE_BACKLOG_MAX_AGE_S = 3600.0  # 超过这么久还发不出去的不再补发(过时的提醒没有意义), 计为丢失
LINKED_REFETCH_DELAY_S = 2.0      # 发现缺K后稍等一下再补拉(让同一时刻另一个品种的K先到)


def _on(v, default: bool = False) -> bool:
    """配置开关: 认 true/false、1/0、"true"/"off" 这些写法。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    return default


class Engine:
    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self.rest = BinanceRest(cfg.get("binance.rest_base"), cfg.binance_key, cfg.binance_secret)
        self.signal_engine = SignalEngine(cfg, db)
        self.paper = PaperBroker(cfg, db)
        # 内存K线缓存 (symbol, tf) -> deque[dict]
        self.cache: dict[tuple, collections.deque] = {}
        self.symbols: list[str] = []
        # ---- 近实时提醒 + 联动规则: 只产出文字通知 / 模拟记账, 与下单路径没有交集 ----
        self.alerts: AlertEngine | None = None           # alerts.enabled=false 时为 None
        self.linked: LinkedRuleRunner | None = None      # 没有启用的 linked_rules 时为 None
        self.data_only: set[str] = set()                 # 仅为提醒/联动规则订阅行情的币: 不评估信号
        self._bg_notices: set[asyncio.Task] = set()      # 后台发送中的提醒(强引用, 防止被回收)
        self.notice_dropped = 0                          # 积压超限直接丢掉的条数
        self.notice_failed = 0                           # 重试用尽仍没发出去的条数
        self.notice_retried = 0                          # 重试过的次数
        self.notice_last_fail_at = 0                     # 最近一次彻底发送失败的时刻(秒)
        self.notice_recovered = 0                        # 之后补发成功的条数
        self.notice_lost = 0                             # 待补发里因太久/太多被放弃的条数
        self._notice_backlog: collections.deque = collections.deque()   # 待补发: (时刻, 文本, 没送达的订阅者)
        self._backlog_task: asyncio.Task | None = None
        self._linked_refetch_task: asyncio.Task | None = None   # 联动规则后台补拉缺K(同一时间只跑一个)
        self.hook_errors: dict[str, int] = {}
        self._hook_err_logged: dict[str, float] = {}
        self._build_alerts()
        if self.alerts is not None:
            # 实时报价通路: 未收盘K的推送 → _on_tick(同步、纯内存)。只取提醒引擎基准级别那一路流。
            self.ws = KlineWS(cfg.get("binance.ws_base"), self.on_closed_bar,
                              on_tick=self._on_tick, tick_symbols=self.alerts.tick_symbols,
                              tick_tf=self._alert_tick_tf())
        else:
            self.ws = KlineWS(cfg.get("binance.ws_base"), self.on_closed_bar)
        # 新信号订阅者（telegram bot、web 推送都挂这里）
        self.signal_subscribers: list[Callable[[int, object], Awaitable[None]]] = []
        self.trade_close_subscribers: list[Callable[[dict], Awaitable[None]]] = []
        self.notice_subscribers: list[Callable[[str], Awaitable[None]]] = []
        self._tasks: list[asyncio.Task] = []
        self.started_at = 0
        self.last_eval_ms = 0.0
        self.squeeze: dict[str, dict] = {}   # 逼空候选 symbol -> 明细
        self.trader = None                   # live 模式注入 LiveTrader(main.py)
        self._auto_halt = False              # 熔断标志(达最大亏损后停自动)
        self._bal_cache = (0.0, 0.0)         # (余额, 取数时间) 短缓存
        self._risk_equity_cache = (None, 0.0)  # (划转校正余额明细, 取数时间)
        self._auto_order_lock = asyncio.Lock()  # 风控检查到下单必须串行
        self._position_reservations: dict[str, float] = {}  # 等待交易所持仓可见

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        self.started_at = int(time.time())
        self.signal_engine.load_macro(self.db)
        await self.refresh_universe()
        await self.backfill_all()
        await self._prepare_linked()
        self._after_backfill(linked=False)   # 停机期间被触及的价位: 用刚回填的K线补查(联动规则已在上一步预热)
        btc15 = self.cache.get(("BTCUSDT", "15m"))
        if btc15:
            from .chan import trend_direction
            self.signal_engine.btc_trend = trend_direction(
                list(btc15), self.cfg.get("signal.trend_ema_period", 50))
        await self.ws.start(self.symbols, self.cfg.timeframes)
        self._tasks.append(asyncio.create_task(self._universe_loop()))
        self._tasks.append(asyncio.create_task(self._watchdog_loop()))
        self._tasks.append(asyncio.create_task(self._maintenance_loop()))
        self._tasks.append(asyncio.create_task(self._funding_loop()))
        self._tasks.append(asyncio.create_task(self._squeeze_loop()))
        log.info("engine started: %d symbols, tfs=%s", len(self.symbols), self.cfg.timeframes)
        self.db.log("info", "engine", f"started with {len(self.symbols)} symbols")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in list(self._bg_notices):
            t.cancel()
        if self._linked_refetch_task is not None:
            self._linked_refetch_task.cancel()
        if self._backlog_task is not None:
            self._backlog_task.cancel()
        got = await self._order_quiet(15.0)      # 停机前同样先等在途下单序列走完
        try:
            await self.ws.stop()
        finally:
            if got:
                self._auto_order_lock.release()
        await self.rest.close()

    # ---------- 行情流重启必须避开在途的实盘下单序列 ----------
    async def _order_quiet(self, timeout: float = 30.0) -> bool:
        """拿到自动下单锁 = 此刻没有「市价进场→挂止损→挂止盈」序列在途。返回是否拿到(调用方负责释放)。

        为什么需要: 自动下单是在行情流的任务里 await 的(on_closed_bar→_auto_execute→trader.execute_signal)。
        ws.start()/stop() 会 cancel 这些任务; 如果恰好卡在"进场已成交、止损还没挂"之间, CancelledError
        不是 Exception, trader 里的紧急平仓分支接不住 → 留下一张没有止损的真实持仓(2026-11-17 审查发现)。
        超时拿不到(下单序列卡住)就照旧继续, 不让行情流永远停着。"""
        try:
            await asyncio.wait_for(self._auto_order_lock.acquire(), timeout)
            return True
        except asyncio.TimeoutError:
            log.warning("等待在途下单序列超过 %.0fs, 仍继续操作行情流", timeout)
            return False

    async def restart_ws(self) -> None:
        """重启行情流(币种集合变化/看门狗/加关注)。先等在途下单序列走完, 见 _order_quiet。"""
        got = await self._order_quiet()
        try:
            await self.ws.start(self.symbols, self.cfg.timeframes)
        finally:
            if got:
                self._auto_order_lock.release()

    # ---------- 近实时提醒 / 联动规则(只发文字、只记模拟; 全部异常隔离) ----------
    def _build_alerts(self) -> None:
        """按配置创建 self.alerts / self.linked。任何一个创建失败只记日志并留 None, 引擎照常启动。"""
        def get_bars(symbol: str, tf: str) -> list:
            return list(self.cache.get((symbol, tf), ()))

        try:
            if _on(self.cfg.get("alerts.enabled", False)):
                self.alerts = AlertEngine(self.cfg, self.db, get_bars)
        except Exception:
            self.alerts = None
            log.exception("提醒引擎创建失败(不影响交易引擎)")
        try:
            if rule_entries(self.cfg):
                self.linked = LinkedRuleRunner(self.cfg, self.db, self.paper, get_bars)
        except Exception:
            self.linked = None
            log.exception("联动规则创建失败(不影响交易引擎)")

    def _alert_tick_tf(self) -> str | None:
        try:
            tf = self.alerts.status().get("base_tf")
            return tf if isinstance(tf, str) and tf in self.cfg.timeframes else None
        except Exception:
            return None

    def _hook_error(self, where: str, exc: bool = True) -> None:
        """提醒/联动规则挂钩里的异常: 计数 + 每分钟最多一条堆栈。自身绝不抛。
        exc=False: 不是异常而是对方明确报告失败(没有堆栈可打)。"""
        try:
            n = self.hook_errors.get(where, 0) + 1
            self.hook_errors[where] = n
            now = time.monotonic()
            if now - self._hook_err_logged.get(where, -1e9) >= 60:
                self._hook_err_logged[where] = now
                if exc:
                    log.exception("%s hook failed (累计 %d 次)", where, n)
                else:
                    log.warning("%s hook reported failure (累计 %d 次)", where, n)
        except Exception:
            pass

    def _forced_symbols(self) -> set[str]:
        """提醒币种 + 有活动价位提醒的币 + 联动规则用到的币: 不管成交额过滤, 一律要有行情。"""
        out: set[str] = set()
        try:
            if self.alerts is not None:
                out |= set(self.alerts.tick_symbols())
        except Exception:
            self._hook_error("forced_symbols")
        try:
            if self.linked is not None:
                out |= set(self.linked.symbols())
        except Exception:
            self._hook_error("forced_symbols")
        return out

    def _protected_tracks(self) -> tuple:
        """联动规则的模拟单所在 track: 别的策略出反向信号时不平它们(只认止盈/止损)。"""
        try:
            return self.linked.paper_tracks() if self.linked is not None else ()
        except Exception:
            return ()

    def _on_tick(self, symbol: str, price: float, ts_ms: int) -> None:
        """实时报价回调: 同步, 在 websocket 处理函数里被调用。只跑 AlertEngine.on_tick(纯内存);
        有消息就排后台任务去发 —— 这里绝不 await 网络, 也绝不向外抛异常。"""
        try:
            alerts = self.alerts
            if alerts is None:
                return
            msgs = alerts.on_tick(symbol, price, ts_ms)
            if msgs:
                self._notify_bg(msgs)
        except Exception:
            self._hook_error("tick")

    def _notify_bg(self, msgs) -> None:
        """把几条文字通知交给后台任务发(notice_subscribers = 纯文字通知, 没有任何按钮)。
        调用方不等待发送结果; 积压超过上限直接丢弃并计数。"""
        try:
            texts = [m for m in msgs if isinstance(m, str) and m]
            if not texts or not self.notice_subscribers:
                return
            if len(self._bg_notices) >= MAX_PENDING_NOTICES:
                self.notice_dropped += len(texts)
                return
            task = asyncio.get_running_loop().create_task(self._deliver_notices(texts))
            self._bg_notices.add(task)
            task.add_done_callback(self._bg_notices.discard)
        except Exception:
            self._hook_error("notify")

    async def _deliver_notices(self, texts: list) -> None:
        """后台发送。订阅者抛异常或明确返回 False(= 没送达, 例如 Telegram 回 ok=false)都算失败, 按
        NOTICE_RETRY_DELAYS_S 重试; 试完仍失败记 notice_failed。只调用 notice_subscribers(纯文字)。"""
        for text in texts:
            pending = list(self.notice_subscribers)
            for attempt in range(len(NOTICE_RETRY_DELAYS_S) + 1):
                failed, wait = [], None
                for sub in pending:
                    try:
                        if await sub(text) is not False:
                            continue
                        owner = getattr(sub, "__self__", None)       # bot.notify: 对方要求的等待秒数(429)
                        wait = getattr(owner, "notify_retry_after", None)
                        self._hook_error("notice_send", exc=False)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._hook_error("notice_send")
                    failed.append(sub)
                if not failed:
                    break
                if attempt >= len(NOTICE_RETRY_DELAYS_S):
                    self.notice_failed += 1
                    self.notice_last_fail_at = int(time.time())
                    log.error("提醒发送失败(已重试 %d 次, 转入待补发): %s", attempt, " ".join(text[:80].split()))
                    self._stash_notice(text, failed)
                    break
                delay = NOTICE_RETRY_DELAYS_S[attempt]
                try:
                    if wait is not None and float(wait) > delay:
                        delay = float(wait)
                except (TypeError, ValueError):
                    pass
                self.notice_retried += 1
                pending = failed                                     # 只重试没送达的订阅者
                await asyncio.sleep(min(delay, NOTICE_RETRY_MAX_S))

    def _stash_notice(self, text: str, subs: list) -> None:
        """重试用尽仍没发出去的提醒: 留在内存里(有上限), 由一个后台任务每隔 NOTICE_BACKLOG_RETRY_S 再试,
        通了就补发(标明是延迟送达)。Telegram 断上几分钟, 一次性的提醒也不至于就此消失。"""
        try:
            if len(self._notice_backlog) >= NOTICE_BACKLOG_MAX:
                self._notice_backlog.popleft()
                self.notice_lost += 1
            self._notice_backlog.append((time.time(), text, list(subs)))
            t = self._backlog_task
            if t is None or t.done():
                self._backlog_task = asyncio.get_running_loop().create_task(self._backlog_loop())
        except Exception:
            self._hook_error("notice_backlog")

    async def _backlog_loop(self) -> None:
        """后台: 按顺序补发待补发的提醒。只调用 notice_subscribers 里的那几个纯文字订阅者。"""
        try:
            while self._notice_backlog:
                await asyncio.sleep(NOTICE_BACKLOG_RETRY_S)
                while self._notice_backlog:
                    ts, text, subs = self._notice_backlog[0]
                    if time.time() - ts > NOTICE_BACKLOG_MAX_AGE_S:      # 太久了: 不再补发, 计为丢失
                        self._notice_backlog.popleft()
                        self.notice_lost += 1
                        continue
                    late = f"{text}\n(延迟送达: 当时未能发出, 原定 {time.strftime('%m-%d %H:%M', time.gmtime(ts + 7 * 3600))})"
                    left = []
                    for sub in subs:
                        try:
                            if await sub(late) is False:
                                left.append(sub)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            left.append(sub)
                    if left:                                             # 还是不通: 下一轮再试(只留没送达的订阅者)
                        self._notice_backlog[0] = (ts, text, left)
                        break
                    self._notice_backlog.popleft()
                    self.notice_recovered += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self._hook_error("notice_backlog")

    def notice_health(self) -> dict:
        """提醒通路的健康计数(只给登录后的「提醒」列表/卡片看): 全是 0 = 正常。"""
        try:
            return {
                "notice_failed": self.notice_failed, "notice_dropped": self.notice_dropped,
                "notice_retried": self.notice_retried, "notice_pending": len(self._bg_notices),
                "notice_recovered": self.notice_recovered, "notice_lost": self.notice_lost,
                "notice_backlog": len(self._notice_backlog),
                "notice_last_fail_at": self.notice_last_fail_at,
                "hook_errors": dict(self.hook_errors),
                "tick_errors": int(getattr(self.ws, "tick_errors", 0) or 0),
            }
        except Exception:
            return {}

    def _after_backfill(self, linked: bool = True) -> None:
        """K线刚被回填(启动 / 看门狗重连)之后: 联动规则按补全的历史重建; 价位提醒补查中断期间被触及的价位。
        同步、不联网、各自隔离; 只产出文字通知。"""
        if linked and self.linked is not None:
            try:
                msgs = self.linked.resync()
                if msgs:
                    self._notify_bg(msgs)
            except Exception:
                self._hook_error("linked_resync")
        if self.alerts is not None:
            try:
                msgs = self.alerts.rescan_levels()
                if msgs:
                    self._notify_bg(msgs)
            except Exception:
                self._hook_error("alerts_rescan")

    def _linked_refetch_kick(self) -> None:
        """联动规则发现历史里也找不到的缺K(短暂重连恰好跨过一根收盘) → 起一个后台任务去补拉。
        收盘K回调里只做这一次布尔判断; 网络请求全在后台任务里。同一时间只跑一个。"""
        try:
            linked = self.linked
            if linked is None or not linked.wants_refetch():
                return
            t = self._linked_refetch_task
            if t is not None and not t.done():
                return
            self._linked_refetch_task = asyncio.get_running_loop().create_task(self._linked_refetch())
        except Exception:
            self._hook_error("linked_refetch")

    async def _linked_refetch(self) -> None:
        """后台: 用公开K线接口把联动规则缺的K补进 klines 表, 再让规则按补全的历史重建。
        只读公开行情、只写 klines 表、只发文字; 不动内存K线缓存(别的策略读的那份不因此改变)。"""
        try:
            await asyncio.sleep(LINKED_REFETCH_DELAY_S)
            linked = self.linked
            if linked is None:
                return
            reqs = list(linked.refetch_requests())
            if not reqs:
                return
            for sym, tf, n in reqs:
                try:
                    ks = await asyncio.wait_for(
                        self.rest.klines(sym, tf, limit=max(1, min(1500, int(n)))), LINKED_TOPUP_TIMEOUT_S)
                    closed = [r for r in ks if r[8] == 1]
                    if closed:
                        self.db.upsert_klines(sym, tf, closed)
                    log.info("linked gap refetch %s %s: %d bars", sym, tf, len(closed))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._hook_error("linked_refetch")
            msgs = linked.resync()
            if msgs:
                self._notify_bg(msgs)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._hook_error("linked_refetch")

    async def _prepare_linked(self) -> None:
        """联动规则启动预热: klines 表历史不够就补拉(公开K线接口, 每个品种至多一次请求), 然后 seed。
        全程隔离: 失败只记日志; 规则在历史不足时自己保持空闲(status 里写明)。"""
        linked = self.linked
        if linked is None:
            return
        try:
            needs = list(linked.history_needs())
        except Exception:
            needs = []
            log.exception("联动规则历史需求读取失败")
        now_ms = int(time.time() * 1000)
        for sym, tf, need, tf_ms in needs:
            try:
                if sym not in self.symbols:
                    log.warning("联动规则币种 %s 不在行情订阅里(交易所无此合约?)", sym)
                    continue
                row = self.db.one(
                    "SELECT COUNT(*) c FROM klines WHERE symbol=? AND tf=? AND open_time>=?",
                    (sym, tf, now_ms - need * tf_ms))
                have = row["c"] if row else 0
                if have >= need - 3:
                    continue
                ks = await asyncio.wait_for(
                    self.rest.klines(sym, tf, limit=min(1500, need + 5)), LINKED_TOPUP_TIMEOUT_S)
                closed = [r for r in ks if r[8] == 1]
                self.db.upsert_klines(sym, tf, closed)
                log.info("linked history top-up %s %s: %d -> %d bars", sym, tf, have, len(closed))
            except Exception:
                log.exception("联动规则历史补齐失败 %s %s(规则会在历史不足时保持空闲)", sym, tf)
        try:
            msgs = linked.seed()
            if msgs:
                self._notify_bg(msgs)
            log.info("linked rules seeded: %s", linked.status().get("summary"))
        except Exception:
            log.exception("联动规则预热失败")

    # ---------- 币种 ----------
    async def refresh_universe(self) -> None:
        syms = await self.rest.usdt_perp_symbols()
        vols = await self.rest.ticker_24h()
        min_vol = self.cfg.get("universe.min_quote_volume_24h", 5e7)
        exclude = set(self.cfg.get("universe.exclude", []) or [])
        rows = []
        for s in syms:
            qv = vols.get(s["symbol"], 0.0)
            enabled = 1 if qv >= min_vol and s["symbol"] not in exclude else 0
            rows.append({**s, "quote_volume_24h": qv, "enabled": enabled})
        self.db.upsert_symbols(rows)
        # 关注列表的币强制纳入监控（即使24h成交额不达标）
        valid = {r["symbol"] for r in syms}
        watch = self.db.watch_symbols() & valid
        normal = set(self.db.enabled_symbols()) | watch
        # 提醒币种 / 联动规则币种: 无论成交额过滤、exclude 名单如何, 行情必须有(否则规则悄悄看不到K线)。
        # 但它们若本来不在监控范围内, 只作「仅行情」: 订阅+缓存+入库+模拟结算, 不评估信号 →
        # 不会因为这次接入而多出任何交易信号或下单。
        forced = self._forced_symbols()
        if forced - valid:
            log.warning("提醒/联动规则币种不在交易所合约列表里, 无法订阅: %s", sorted(forced - valid))
        self.data_only = (forced & valid) - normal
        self.symbols = sorted(normal | self.data_only)
        self.signal_engine.watch_set = watch

    async def _universe_loop(self) -> None:
        interval = self.cfg.get("universe.refresh_minutes", 60) * 60
        while True:
            await asyncio.sleep(interval)
            try:
                old = set(self.symbols)
                await self.refresh_universe()
                new = set(self.symbols)
                if new != old:
                    log.info("universe changed %d -> %d, restarting ws", len(old), len(new))
                    await self.backfill_all(only=sorted(new - old))
                    await self.restart_ws()
            except Exception:
                log.exception("universe refresh failed")

    async def _watchdog_loop(self) -> None:
        """3分钟没有任何ws消息则强制重启ws并补缺口。"""
        while True:
            await asyncio.sleep(60)
            if self.ws.last_msg_at and time.time() - self.ws.last_msg_at > 180:
                log.warning("watchdog: no ws message for 3min, restarting")
                self.db.log("warn", "watchdog", "ws silent 3min, restart + gap fill")
                try:
                    await self.backfill_all()
                    await self.restart_ws()
                except Exception:
                    log.exception("watchdog restart failed")
                # 断线期间的K线已回填进库/缓存, 但它们不会再走收盘回调: 让联动规则/价位提醒按回填结果补一遍
                self._after_backfill()

    async def _funding_loop(self) -> None:
        """每5分钟刷新资金费率，供因子使用。"""
        while True:
            try:
                self.signal_engine.funding = await self.rest.funding_rates()
            except Exception as e:
                log.warning("funding refresh failed: %s", e)
            await asyncio.sleep(self.cfg.get("factors.refresh_funding_minutes", 5) * 60)

    async def _squeeze_loop(self) -> None:
        """逼空候选扫描：OI骤增+费率极值+价格低位。"""
        await asyncio.sleep(30)
        while True:
            try:
                await self._scan_squeeze()
            except Exception:
                log.exception("squeeze scan failed")
            await asyncio.sleep(self.cfg.get("squeeze.refresh_minutes", 15) * 60)

    async def _scan_squeeze(self) -> None:
        from .squeeze import price_position, squeeze_score
        sem = asyncio.Semaphore(6)
        new_cands: list[dict] = []

        async def one(sym: str):
            async with sem:
                oi = await self.rest.open_interest_hist(sym, "15m", 12)
            if not oi:
                return
            kl = list(self.cache.get((sym, "1h"), ())) or list(self.cache.get((sym, "15m"), ()))
            pos = price_position(kl) if kl else 0.5
            is_cand, detail = squeeze_score(
                oi, self.signal_engine.funding.get(sym), pos,
                oi_surge=self.cfg.get("squeeze.oi_surge_pct", 30.0),
                funding_extreme=self.cfg.get("squeeze.funding_extreme", 0.0005),
                low_pos=self.cfg.get("squeeze.low_pos", 0.35))
            if is_cand:
                new = sym not in self.squeeze
                self.squeeze[sym] = {"symbol": sym, "at": int(time.time()), **detail}
                if new:
                    new_cands.append(self.squeeze[sym])
                # 🔥强候选 → 自动建草稿预演(无重复时)，用户审核
                if detail.get("strong") and new and kl and not self.db.active_playbooks(sym):
                    seg = kl[-200:]
                    hi = max(float(k["high"]) for k in seg)
                    lo = min(float(k["low"]) for k in seg)
                    cur = float(seg[-1]["close"])
                    self.db.insert_playbook({
                        "symbol": sym, "tf": "1h", "direction": "long",
                        "title": f"逼空自动建档 OI+{detail['oi_change_pct']:.0f}% "
                                 f"费率{(detail['funding'] or 0)*100:.3f}% 低位{int(detail['pos']*100)}%",
                        "trigger_type": "price_reach", "trigger_price": round(hi, 8),
                        "entry": round(cur, 8), "sl": round(lo, 8), "source": "auto"})
                    self.db.log("info", "squeeze", f"auto-playbook {sym}")
            else:
                self.squeeze.pop(sym, None)

        await asyncio.gather(*(one(s) for s in self.symbols if s not in self.data_only))
        for c in new_cands:
            f = (c.get("funding") or 0) * 100
            msg = (f"⚠<b>逼空候选</b> {c['symbol']}{'(强)' if c['strong'] else ''}: "
                   f"OI {c['oi_change_pct']:+.0f}%骤增 · 费率{f:.3f}% · 价格位{int(c['pos']*100)}%")
            self.db.log("info", "squeeze", c["symbol"])
            for sub in self.notice_subscribers:
                await sub(msg)

    async def _maintenance_loop(self) -> None:
        """每小时：修剪K线表防膨胀；把超过TTL未处理的信号标记为过期。"""
        while True:
            await asyncio.sleep(3600)
            try:
                keep = self.cfg.get("data.kline_keep_bars", 1500)
                for sym in self.symbols:
                    for tf in self.cfg.timeframes:
                        self.db.trim_klines(sym, tf, keep)
                ttl_s = self.cfg.get("telegram.confirm_ttl_minutes", 30) * 60
                cur = self.db.execute(
                    "UPDATE signals SET status='expired' WHERE status IN ('new','notified') AND created_at < ?",
                    (int(time.time()) - ttl_s,),
                )
                if cur.rowcount:
                    log.info("maintenance: expired %d stale signals", cur.rowcount)
                log.info("maintenance done")
            except Exception:
                log.exception("maintenance failed")

    # ---------- 数据 ----------
    async def backfill_all(self, only: list[str] | None = None) -> None:
        bars = self.cfg.get("data.backfill_bars", 500)
        targets = only or self.symbols
        sem = asyncio.Semaphore(8)

        async def fill(sym: str, tf: str):
            async with sem:
                try:
                    ks = await self.rest.klines(sym, tf, limit=bars)
                    closed = [r for r in ks if r[8] == 1]  # r[8]=closed (r[7]=taker_buy)
                    self.db.upsert_klines(sym, tf, closed)
                    self.cache[(sym, tf)] = collections.deque(
                        (dict(zip(COLS, r)) for r in closed), maxlen=bars + 50
                    )
                except Exception as e:
                    log.warning("backfill %s %s failed: %s", sym, tf, e)

        t0 = time.time()
        await asyncio.gather(*(fill(s, tf) for s in targets for tf in self.cfg.timeframes))
        log.info("backfill %d symbols done in %.1fs", len(targets), time.time() - t0)

    # ---------- 核心回调：K线收盘 ----------
    async def on_closed_bar(self, symbol: str, tf: str, bar: tuple) -> None:
        t0 = time.perf_counter()
        key = (symbol, tf)
        dq = self.cache.get(key)
        if dq is None:
            dq = collections.deque(maxlen=self.cfg.get("data.backfill_bars", 500) + 50)
            self.cache[key] = dq
        bd = dict(zip(COLS, bar))
        if dq and dq[-1]["open_time"] == bd["open_time"]:
            dq[-1] = bd
        else:
            dq.append(bd)
        self.db.upsert_klines(symbol, tf, [bar])

        # BTC 15m 收盘 → 刷新大盘趋势（btc_resonance 因子用）
        if symbol == "BTCUSDT" and tf == "15m":
            try:
                from .chan import trend_direction
                self.signal_engine.btc_trend = trend_direction(
                    list(dq), self.cfg.get("signal.trend_ema_period", 50))
            except Exception:
                log.exception("btc trend update failed")

        # paper 结算优先（先看持仓有没有打到TP/SL）
        try:
            for closed in self.paper.on_closed_bar(symbol, tf, bar):
                for sub in self.trade_close_subscribers:
                    await sub(closed)
        except Exception:
            log.exception("paper settle failed %s %s", symbol, tf)

        # 近实时提醒(价位/急跌/破位)的收盘K维护 + 联动规则: 只返回文字, 交后台任务发送(这里不等网络)。
        # 两段各自 try/except: 它们出任何错都不影响下面的预演检查与信号评估。
        if self.alerts is not None:
            try:
                msgs = self.alerts.on_closed_bar(symbol, tf, bar)
                if msgs:
                    self._notify_bg(msgs)
            except Exception:
                self._hook_error("alerts")
        if self.linked is not None:
            try:
                msgs = self.linked.on_closed_bar(symbol, tf, bar)
                if msgs:
                    self._notify_bg(msgs)
            except Exception:
                self._hook_error("linked")
            self._linked_refetch_kick()

        # 预演(Playbook)检查：到预演位/假突破回收即推提醒
        try:
            from .playbook import check_bar
            for fired in check_bar(self.db, symbol, tf, bar):
                self.db.log("info", "playbook", f"triggered #{fired['id']} {symbol} {tf}")
                for sub in self.notice_subscribers:
                    await sub(fired["message"])
        except Exception:
            log.exception("playbook check failed %s %s", symbol, tf)

        # 状态机文字通知（失效/解除）转发
        try:
            while self.signal_engine.notices:
                note = self.signal_engine.notices.pop(0)
                for sub in self.notice_subscribers:
                    await sub(note)
        except Exception:
            log.exception("notice forward failed")

        # 仅行情币种(只因提醒/联动规则才订阅): 到此为止, 不评估信号 → 不产生交易信号、不下单
        if symbol in self.data_only:
            self.last_eval_ms = (time.perf_counter() - t0) * 1000
            return

        # 信号评估（多级别联立：一次收盘可能产出多个信号）
        try:
            kbt = {t: list(self.cache.get((symbol, t), ())) for t in self.cfg.timeframes}
            # 更新"试"信号生命周期(试→成立/失败)
            for sid, st in self.signal_engine.update_lifecycle(symbol, tf, kbt.get(tf, [])):
                log.info("SIGNAL #%d -> %s", sid, st)
            new_sigs = []
            td = self.cfg.get("trade_direction", "both")   # both/long/short: 只交易某方向
            for sig in self.signal_engine.evaluate_all(symbol, tf, kbt):
                sid = self.db.insert_signal(sig.to_db())
                # 方向闸: alert只提示不交易; 选了单向则只交易该方向(其余只入库展示)
                tradable = sig.kind != "alert" and (td == "both" or sig.direction == td)
                if tradable:
                    # 反向信号平仓：一卖平掉同币同级别的多单(一买)，一买平掉空单
                    for rev in self.paper.close_opposite(sig.symbol, sig.tf, sig.direction, sig.entry,
                                                         exclude_tracks=self._protected_tracks()):
                        for sub in self.trade_close_subscribers:
                            await sub(rev)
                    self.paper.open_from_signal(sid, sig)
                new_sigs.append((sid, sig))
                log.info("SIGNAL #%d %s %s %s %s rr=%.2f trade=%s", sid, sig.kind, sig.symbol, sig.tf, sig.direction, sig.rr, tradable)
                self.db.log("info", "signal", f"#{sid} {sig.symbol} {sig.tf} {sig.direction} rr={sig.rr}")
                if tradable:
                    placed = await self._auto_execute(sid, sig)   # auto_trade开→直接下单
                    if not placed:
                        for sub in self.signal_subscribers:        # 否则推TG卡片走人工二次确认
                            try:
                                await sub(sid, sig)
                            except Exception:
                                log.exception("signal subscriber failed")
            await self._check_dual(symbol, new_sigs)
        except Exception:
            log.exception("evaluate failed %s %s", symbol, tf)
        self.last_eval_ms = (time.perf_counter() - t0) * 1000

    async def _check_dual(self, symbol: str, new_sigs: list) -> None:
        """双信号重叠: 同币同级别同方向, 缠论买点与威科夫买点在 overlap_bars 根内同时出现
        → 双重加强, 标记 dual 并额外提醒(模拟仓此时已进场)。"""
        if not new_sigs:
            return
        import json as _json
        from .signals import TF_MS
        win_bars = self.cfg.get("wyckoff.overlap_bars", 3)
        for sid, sig in new_sigs:
            is_wy = isinstance(sig.extra, dict) and sig.extra.get("path") == "威科夫"
            win = win_bars * TF_MS.get(sig.tf, 300)
            try:
                other = self.db.one(
                    "SELECT id, extra FROM signals WHERE symbol=? AND tf=? AND direction=? AND id<>? "
                    "AND created_at >= ? AND COALESCE(status,'')<>? ORDER BY id DESC LIMIT 1",
                    (symbol, sig.tf, sig.direction, sid, sig.created_at - win, LINKED_SIGNAL_STATUS))
            except Exception:
                continue
            if not other:
                continue
            try:
                oex = _json.loads(other["extra"] or "{}")
            except (ValueError, TypeError):
                oex = {}
            if is_wy == (oex.get("path") == "威科夫"):
                continue   # 同路径, 不算重叠
            for x in (sid, other["id"]):
                self._set_dual(x)
            side = "做多" if sig.direction == "long" else "做空"
            msg = (f"🔔双信号重叠 {symbol} {sig.tf} {side}: 缠论买点 + 威科夫买点 同位共振(双重加强), "
                   f"模拟仓已进场, 重点关注后续走势!")
            for sub in self.notice_subscribers:
                try:
                    await sub(msg)
                except Exception:
                    log.exception("dual notice failed")

    def _set_dual(self, sid: int) -> None:
        import json as _json
        row = self.db.one("SELECT extra FROM signals WHERE id=?", (sid,))
        if not row:
            return
        try:
            ex = _json.loads(row["extra"] or "{}")
        except (ValueError, TypeError):
            ex = {}
        if ex.get("dual"):
            return
        ex["dual"] = 1
        self.db.execute("UPDATE signals SET extra=? WHERE id=?",
                        (_json.dumps(ex, ensure_ascii=False), sid))

    async def _wallet_balance(self):
        """合约总钱包余额, 短缓存。失败返回 None。"""
        bal, ts = self._bal_cache
        if time.time() - ts < RISK_CACHE_SECONDS:
            return bal
        try:
            acc = await self.rest.account_info()
            bal = float(acc.get("totalWalletBalance") or 0)
            self._bal_cache = (bal, time.time())
            return bal
        except Exception:
            return None

    async def _transfer_adjusted_wallet(self):
        """返回划转校正后的钱包明细；外部资金进出不计作交易盈亏。"""
        cached, ts = self._risk_equity_cache
        if cached is not None and time.time() - ts < RISK_CACHE_SECONDS:
            return cached
        raw = await self._wallet_balance()
        if raw is None:
            return None
        end_ms = int(time.time() * 1000)
        row = self.db.one(
            "SELECT updated_at FROM settings WHERE key=?", ("risk.account_equity",))
        if not row or not row["updated_at"]:
            raise RuntimeError("risk.account_equity 缺少有效基准时间")
        baseline_s = int(row["updated_at"])
        now_s = int(time.time())
        if baseline_s > now_s:
            raise RuntimeError("risk.account_equity 基准时间位于未来")
        if baseline_s < now_s - TRANSFER_HISTORY_MAX_AGE_SECONDS:
            raise RuntimeError("risk.account_equity 基准超过 Binance 89天安全查询窗口")
        start_ms = baseline_s * 1000
        transfer_net = 0.0
        page = 1
        while True:
            batch = await self.rest.income(
                start_ms=start_ms, end_ms=end_ms, limit=1000, page=page,
                income_type="TRANSFER")
            transfer_net += sum(
                float(item.get("income") or 0)
                for item in batch
                if item.get("asset") == "USDT"
                and item.get("incomeType") == "TRANSFER"
            )
            if len(batch) < 1000:
                break
            page += 1
        result = {
            "raw": raw,
            "transfer_net": transfer_net,
            "adjusted": raw - transfer_net,
        }
        self._risk_equity_cache = (result, time.time())
        return result

    async def _auto_execute(self, sid: int, sig) -> bool:
        """自动下单(带熔断+持仓上限)。返回是否已下单(False→回退人工确认)。"""
        if self.trader is None or self.cfg.mode != "live" or not self.cfg.get("live.auto_trade", False):
            return False
        async with self._auto_order_lock:
            return await self._auto_execute_locked(sid, sig)

    async def _auto_execute_locked(self, sid: int, sig) -> bool:
        """串行完成风控检查与下单，避免并发信号突破持仓上限。"""
        if self.trader is None or self.cfg.mode != "live" or not self.cfg.get("live.auto_trade", False):
            return False
        trade_direction = self.cfg.get("trade_direction", "both")
        if trade_direction != "both" and sig.direction != trade_direction:
            self.db.log("info", "auto", f"方向已切换为{trade_direction}, 跳过 {sig.symbol} {sig.direction}")
            return False
        # 安全阀1: 亏损熔断。资金划转不计作交易盈亏。
        base = float(self.cfg.get("risk.account_equity", 100) or 100)
        loss_pct = float(self.cfg.get("live.max_loss_pct", 50) or 50)
        try:
            equity = await self._transfer_adjusted_wallet()
        except Exception as exc:
            log.exception("transfer-adjusted equity check failed")
            if not self._auto_halt:
                self._auto_halt = True
                self.db.log("error", "auto", f"熔断: 无法核验划转校正余额，停自动: {exc}")
                for sub in self.notice_subscribers:
                    await sub("🛑熔断: 无法核验划转校正余额，已停止自动交易")
            return False
        if equity is None:
            if not self._auto_halt:
                self._auto_halt = True
                self.db.log("error", "auto", "熔断: 无法读取合约钱包余额，停自动")
                for sub in self.notice_subscribers:
                    await sub("🛑熔断: 无法读取合约钱包余额，已停止自动交易")
            return False
        adjusted = equity["adjusted"]
        if base > 0 and adjusted < base * (1 - loss_pct / 100.0):
            if not self._auto_halt:
                self._auto_halt = True
                detail = (f"熔断: 校正余额{adjusted:.2f}<本金{base}的{100-loss_pct:.0f}% "
                          f"(钱包{equity['raw']:.2f}, 划转{equity['transfer_net']:+.2f}), 停自动")
                self.db.log("error", "auto", detail)
                for sub in self.notice_subscribers:
                    await sub(f"🛑熔断: 校正余额{adjusted:.2f}U 跌破本金{base}的"
                              f"{100-loss_pct:.0f}%, 已停止自动交易")
            return False
        if self._auto_halt:
            self._auto_halt = False
            self.db.log("info", "auto", f"熔断解除: 划转校正余额{adjusted:.2f}U")
            for sub in self.notice_subscribers:
                await sub(f"✅熔断解除: 划转校正余额{adjusted:.2f}U，自动交易恢复")
        # 安全阀2: 最大同时持仓
        try:
            raw = await self.rest.position_risk()
            nopen = sum(1 for p in raw if float(p.get("positionAmt") or 0) != 0)
        except Exception as exc:
            self.db.log("error", "auto", f"持仓核验失败，跳过 {sig.symbol}: {exc}")
            for sub in self.notice_subscribers:
                await sub(f"🛑持仓核验失败，已跳过 {sig.symbol} 自动下单")
            return False
        now_mono = time.monotonic()
        self._position_reservations = {
            symbol: expires for symbol, expires in self._position_reservations.items()
            if expires > now_mono
        }
        visible_symbols = {
            p.get("symbol") for p in raw
            if float(p.get("positionAmt") or 0) != 0 and p.get("symbol")
        }
        for symbol in visible_symbols:
            self._position_reservations.pop(symbol, None)
        nopen += sum(1 for symbol in self._position_reservations
                     if symbol not in visible_symbols)
        if nopen >= int(self.cfg.get("live.max_positions", 10)):
            self.db.log("info", "auto", f"持仓达上限{nopen}, 跳过 {sig.symbol}")
            return False
        # 下单
        row = sig.to_db(); row["id"] = sid
        res = await self.trader.execute_signal(sid, row)
        if res.get("ok"):
            self._position_reservations[sig.symbol] = (
                time.monotonic() + POSITION_RESERVATION_SECONDS)
        self.db.update_signal(sid, status=("confirmed" if res.get("ok") else "error"))
        for sub in self.notice_subscribers:
            await sub(("🚀自动下单 " if res.get("ok") else "⚠️自动下单失败 ") + res.get("message", ""))
        log.info("AUTO #%d %s %s -> %s", sid, sig.symbol, sig.direction, res.get("ok"))
        return True

    # ---------- 状态 ----------
    def status(self) -> dict:
        return {
            "started_at": self.started_at,
            "uptime_s": int(time.time()) - self.started_at if self.started_at else 0,
            "symbols": len(self.symbols),
            "ws_conns": self.ws.connected_conns,
            "ws_last_msg_age_s": round(time.time() - self.ws.last_msg_at, 1) if self.ws.last_msg_at else None,
            "last_eval_ms": round(self.last_eval_ms, 1),
            "mode": self.cfg.mode,
            "tracks": {t: self.paper.stats(t) for t in ("buy1", "buy2", "second_buy", "second_sell")},
            "macro": self.signal_engine.macro_view,
            "squeeze": sorted(self.squeeze.values(), key=lambda x: -x.get("oi_change_pct", 0)),
            "funnel": dict(self.signal_engine.funnel),
        }
