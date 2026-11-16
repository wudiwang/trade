"""联动规则的实盘适配层 —— 只提醒 + 模拟记账, 没有任何实盘通道。

这里把纯规则模块(strat_btcbreak.BtcBreakRule)接到引擎的「已收盘K线」上:
  - 启动时用 sqlite klines 表的历史预热(seed), 重启后「空闲/待命」状态不丢, 也不重放旧事件;
  - 每根 5m 收盘K喂给规则, 把规则吐出的事件变成 文字通知 / signals 表的一行 / 一笔模拟单。

安全边界(刻意为之, 改动前请先读):
  - 本模块不 import、不引用下单模块, 也拿不到引擎对象; 对外只有三样产出:
    返回的文字(list[str], 由引擎走 notice_subscribers 发出)、signals 表里 kind="alert" 的一行、
    PaperBroker 里的一笔模拟单(track = 规则名)。
  - signals 行固定 kind="alert" + status="sim" + extra.paper_only=true。引擎只对
    SignalEngine.evaluate_all 返回的信号判定是否可交易, 这一行不经过那条路; 即便经过, kind="alert"
    也会被方向闸挡下。
  - PaperBroker.open_from_signal 对 kind="alert" 不开仓, 所以开模拟单时传一份 kind="linked_paper"
    的副本(只存在于这次函数调用里, 不入库、不外传)。
  - on_closed_bar / seed / status 全部吞异常; 热路径上没有网络调用。只有规则品种的 5m 收盘K才会做事,
    其余(币种, 级别)一次比较就返回。

幂等(重启/重复K不重复记账): 用 signal_anchors 表占位(主键里含 规则名 + 分型低点K的 open_time)。
  规则名            + 目标币 + 分型低点时间 → 这次入场只入库/开模拟单一次
  规则名:armed      + 触发币 + 首次触发K时间 → 「已待命」只通知一次(重启在一轮中间不重发)
  规则名:expired    + 触发币 + 首次触发K时间 → 「到期」只通知一次

历史不足: 规则自己要求两日窗口铺满(≥98% 根数)才会触发。引擎 5m 内存缓存只有约 41.7h, 所以预热读
  klines 表(最多 1500 根 ≈ 125h)。表里也不够时规则保持空闲, status() 的 history_ok=false 并写明差多少;
  之后实时K一根根进来, 铺满后自动生效, 不需要重启。

断线缺K(重建, 不是单边补喂): 任一品种新到的K与它的上一根之间有空档, 就用
  「本运行器喂过的K ∪ klines 表 ∪ 引擎缓存 ∪ 当前这根」把【两个品种】按时间顺序(同一时刻触发币在前)
  从头重放一遍, 重建规则状态 —— 与整段回放(replay)同一个顺序, 所以结果与哪个品种的K先到无关。
  (旧做法是谁的K先到就先单独补谁: 目标币先到时, 它缺口里的K在「还没看到触发K」的状态下被喂掉,
   真正的入场就此丢失, 之后还可能把更晚的分型当成「第一个底分型」开模拟单。)
  重放出来的事件里, 只处理「判定时刻晚于缺口起点、且本运行器没处理过」的(早于 LATE_REPORT_MAX_S 的旧事件
  只记账不通知); 不在当前这根K上的入场 = 过期信号:
  入库留痕并通知(标「补报」), 不开模拟单(模拟单入场价必须是信号K收盘价, 中间的K也没经过模拟结算)。
  历史里也找不到的K(短暂重连恰好跨过一根收盘): 记在 status().counts.gap_missing 并在摘要里写明, 同时
  wants_refetch() 变真 —— 引擎据此在【后台任务】里用公开K线接口补拉(本模块不联网), 入库后调 resync() 再重建一次;
  补回的K若改变了结论(例如缺的那根才是真正的低点, 之前的「待命」不成立), 发一条更正。
  试 REFETCH_MAX_ATTEMPTS 次或超过 HOLE_MAX_AGE_S 仍补不回 → 放弃, 记入 counts.gap_lost。
停机期间出结果: 启动预热时, 若某一轮「待命」当时通知过(库里有占位)而结果(入场/到期)没报过 → 补报一次
  (入场补报照样入库留痕、不开模拟单)。从没通知过的旧轮次不重放。

未审核期间不出统计(CLAUDE.md 第七条): unaudited_tracks(cfg) 给出还没通过出图审核的规则的 track 名
  (配置里 audited: true 才算通过), 日报与统计接口据此只报笔数, 不给胜率/盈亏/期望。

配置(config.yaml 的 linked_rules 列表, 每项一条规则; 改动需重启生效):
  name / enabled / paper / audited + BtcBreakParams 的全部字段。
"""
from __future__ import annotations

import dataclasses
import html
import logging
import time

from .alerts import bkk, fmt_price
from .signals import Signal
from .strat_btcbreak import (
    PRE_BARS, BtcBreakParams, BtcBreakRule, _merged_feed, _norm_bar, event_instant, tf_to_ms)

log = logging.getLogger("linked")

PAPER_KIND = "linked_paper"      # 只用于调用 PaperBroker.open_from_signal 的那份副本
SIGNAL_KIND = "alert"            # 入库的 signals 行: 只展示, 不交易
SIGNAL_STATUS = "sim"            # 不是 new/notified → 不参与「确认按钮」的过期清理, 也一眼看得出是模拟
SEED_LIMIT = 1500                # 预热时每个品种从 klines 表读多少根
SEED_MAX_ATTEMPTS = 3
LATE_AFTER_BARS = 2              # 信号K收盘后超过这么多根才处理到 → 视为过期信号, 不开模拟单
HISTORY_MARGIN_BARS = 12         # 「重启后状态可信」所需历史在 lookback+ttl 之外再多留的余量
FED_KEEP = SEED_LIMIT            # 每个品种在内存里留多少根喂过的K(重建用; 6 个浮点一根, 很小)
SEEN_KEEP = 4000                 # 处理过的事件键留多少个(防重复)
REFETCH_MAX_ATTEMPTS = 3         # 缺的K最多请引擎补拉几次
REFETCH_MIN_INTERVAL_S = 30.0    # 两次补拉请求的最小间隔
HOLE_MAX_AGE_S = 1800.0          # 缺口超过这么久还没补回 → 放弃(不再重放那么久以前的事件)
LATE_REPORT_MAX_S = 6 * 3600.0   # 重建时发现的事件若已是这么久以前的 → 只记账不通知(某个品种断流几天后恢复时不刷屏)

_PARAM_FIELDS = {f.name: f for f in dataclasses.fields(BtcBreakParams)}
_COUNT_KEYS = ("armed", "retrigger", "entry", "expired", "paper_opened", "late_entries",
               "dup_skipped", "gap_filled", "gap_missing", "gap_lost", "resync", "retracted", "bars")
_TRUE = ("1", "true", "yes", "on")


def _coerce(field: dataclasses.Field, value):
    """配置值 → 字段类型(yaml 里 2 和 2.0、true 和 "true" 都接受); 转不了就抛 ValueError。"""
    default = field.default
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{field.name}={value!r}")
    if isinstance(default, int):
        f = float(value)
        if not f.is_integer():
            raise ValueError(f"{field.name}={value!r}")
        return int(f)
    if isinstance(default, float):
        return float(value)
    s = str(value).strip()
    if not s:
        raise ValueError(f"{field.name} 为空")
    return s


def params_from_entry(entry: dict) -> BtcBreakParams:
    """配置里的一项 → BtcBreakParams(只取认识的字段, 其余忽略)。字段不合法抛 ValueError。"""
    kw = {}
    for name, field in _PARAM_FIELDS.items():
        if name in entry and entry[name] is not None:
            kw[name] = _coerce(field, entry[name])
    p = BtcBreakParams(**kw)
    p.trigger_symbol = p.trigger_symbol.upper()
    p.target_symbol = p.target_symbol.upper()
    if p.trigger_symbol == p.target_symbol:
        raise ValueError("trigger_symbol 与 target_symbol 不能相同")
    return p


def rule_entries(cfg) -> list[dict]:
    """cfg 里启用的联动规则配置项(字典列表)。读不出来/格式不对 → 空列表。"""
    try:
        raw = cfg.get("linked_rules", None)
    except Exception:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        en = e.get("enabled", True)
        if isinstance(en, str):
            en = en.strip().lower() in ("1", "true", "yes", "on")
        if en:
            out.append(e)
    return out


def unaudited_tracks(cfg) -> frozenset:
    """还没通过出图审核的联动规则的 track 名(= 规则名)。配置里写了 audited: true 才算通过;
    停用(enabled: false)的规则也算在内 —— 它以前留下的模拟单同样不该进统计。读不出配置 → 空集合。

    用途(CLAUDE.md 第七条: 先出图审核, 通过后才算期望): 日报、/api/strategy_stats、/api/stats_by_tf、
    /api/equity 对这些 track 只报笔数, 不给胜率/盈亏/期望, 也不把它们并进别的策略的合计。"""
    try:
        raw = cfg.get("linked_rules", None)
    except Exception:
        return frozenset()
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    out = set()
    for e in raw:
        if not isinstance(e, dict):
            continue
        ok = e.get("audited", False)
        if isinstance(ok, str):
            ok = ok.strip().lower() in _TRUE
        if ok is True or (isinstance(ok, (int, float)) and not isinstance(ok, bool) and ok == 1):
            continue
        name = str(e.get("name") or BtcBreakParams.name).strip()
        if name:
            out.add(name)
    return frozenset(out)


def rule_symbols(cfg) -> set[str]:
    """启用的联动规则要用到的全部币种(引擎据此把它们强制纳入行情订阅)。"""
    syms: set[str] = set()
    for e in rule_entries(cfg):
        try:
            p = params_from_entry(e)
        except Exception:
            continue
        syms.add(p.trigger_symbol)
        syms.add(p.target_symbol)
    return syms


def _ms_bkk(ms) -> str | None:
    return None if ms is None else bkk(float(ms) / 1000.0)


def _bar_label(open_ms) -> str:
    """K线的标签 = 它的开盘时刻(曼谷 HH:MM), 与看盘软件上那根K的时间一致。"""
    return bkk(float(open_ms) / 1000.0, "%H:%M")


def _g(x) -> str:
    return f"{float(x):g}"


def _ev_key(ev: dict) -> tuple:
    """事件的去重键。入场按「分型低点K」(与库里的占位同一个键), 其余按 轮次 id(=首次触发K)。"""
    kind = ev.get("type")
    if kind == "entry":
        return ("entry", int(ev["entry"]["fx_low_time"]))
    if kind == "retrigger":
        return ("retrigger", int(ev["episode_id"]), int(ev["trigger"]["time"]))
    return (str(kind), int(ev["episode_id"]))


class _Rt:
    """一条规则的运行时状态。"""
    __slots__ = ("name", "params", "rule", "paper", "tf", "tf_ms", "trigger", "target", "symbols",
                 "seeded", "seed_attempts", "seed_error", "seed_info", "last", "counts",
                 "last_trigger", "last_entry", "last_expired",
                 "fed", "seen", "sent", "hole_w", "hole_from", "hole_at", "refetch_attempts", "refetch_at")

    def __init__(self, params: BtcBreakParams, paper: bool):
        self.name = params.name
        self.params = params
        self.rule = BtcBreakRule(params)
        self.paper = bool(paper)
        self.tf = params.tf
        self.tf_ms = tf_to_ms(params.tf)
        self.trigger = params.trigger_symbol
        self.target = params.target_symbol
        self.symbols = frozenset((self.trigger, self.target))
        self.seeded = False
        self.seed_attempts = 0
        self.seed_error = ""
        self.seed_info: dict = {}
        self.last: dict[str, int | None] = {self.trigger: None, self.target: None}
        self.counts = dict.fromkeys(_COUNT_KEYS, 0)
        self.last_trigger = None
        self.last_entry = None
        self.last_expired = None
        self.fed: dict[str, dict] = {self.trigger: {}, self.target: {}}   # 喂过的K: open_time -> 规整后的K
        self.seen: dict[tuple, int] = {}             # 处理过(或预热时已成历史)的事件键 -> 判定时刻(ms)
        self.sent: dict[tuple, str] = {}             # 发过通知的 待命/入场: 键 -> 一小段描述(更正时用)
        self.hole_w: int | None = None               # 有没补回的缺口时: 缺口起点的判定时刻(ms)
        self.hole_from: dict[str, int] = {}          # 缺口出现时各品种的最后一根K
        self.hole_at = 0.0                           # 缺口出现的本机时间
        self.refetch_attempts = 0
        self.refetch_at = 0.0


class LinkedRuleRunner:
    """见模块说明。公开方法都不抛异常。"""

    def __init__(self, cfg, db, paper, get_bars, now=time.time):
        self.cfg = cfg
        self.db = db
        self.paper = paper
        self._get_bars = get_bars
        self._now = now
        self._rules: list[_Rt] = []
        self._by_key: dict[tuple, tuple] = {}        # (symbol, tf) -> (_Rt, ...)
        self._config_errors: list[str] = []
        self._errors = 0
        self._err_logged_at = 0.0
        self._last_status: dict = {}
        self._refetch_wanted = False                 # 有缺口等引擎在后台补拉(收盘K回调里只读这个布尔值)
        seen = set()
        for e in rule_entries(cfg):
            try:
                p = params_from_entry(e)
                if p.name in seen:
                    raise ValueError(f"规则名重复: {p.name}")
                rt = _Rt(p, e.get("paper", True) not in (False, 0, "false", "0", "off", "no"))
                seen.add(p.name)
                self._rules.append(rt)
            except Exception as exc:                 # 配置写错只停这一条, 不影响别的规则和引擎
                name = str(e.get("name") or "?")
                self._config_errors.append(f"{name}: {exc}")
                log.error("联动规则 %s 配置无效, 已跳过: %s", name, exc)
        for rt in self._rules:
            for sym in rt.symbols:
                key = (sym, rt.tf)
                self._by_key[key] = self._by_key.get(key, ()) + (rt,)

    # ------------------------------------------------------------ 基础
    def _safe_now(self) -> float:
        try:
            return float(self._now())
        except Exception:
            return time.time()

    def _note_error(self, where: str) -> None:
        try:
            self._errors += 1
            t = time.time()
            if t - self._err_logged_at >= 60:
                self._err_logged_at = t
                log.exception("linked.%s 异常(累计 %d 次)", where, self._errors)
        except Exception:
            pass

    def _log(self, message: str) -> None:
        try:
            self.db.log("info", "linked", message)
        except Exception:
            pass

    def symbols(self) -> set[str]:
        """本运行器用到的全部币种。"""
        out: set[str] = set()
        for rt in self._rules:
            out |= rt.symbols
        return out

    def paper_tracks(self) -> tuple:
        """模拟单所在的 track 名(引擎据此不让别的策略的反向信号把它们平掉: 只认止盈/止损)。"""
        return tuple(rt.name for rt in self._rules)

    def history_needs(self) -> list[tuple]:
        """[(symbol, tf, 根数, tf毫秒)]: 让「重启后状态可信」所需的 klines 表历史。引擎启动时据此补齐。"""
        out = []
        for rt in self._rules:
            p = rt.params
            look = int(round(float(p.lookback_hours) * 3_600_000)) // rt.tf_ms
            ttl = int(round(float(p.ttl_hours) * 3_600_000)) // rt.tf_ms
            out.append((rt.trigger, rt.tf, look + ttl + int(p.vol_ma) + HISTORY_MARGIN_BARS, rt.tf_ms))
            out.append((rt.target, rt.tf, ttl + PRE_BARS + HISTORY_MARGIN_BARS, rt.tf_ms))
        return out

    # ------------------------------------------------------------ 预热
    def seed(self) -> list[str]:
        """从 klines 表预热全部规则。不重放旧事件; 只在「此刻正处于待命、且这次待命从没通知过」时
        返回一条补报。可重复调用(每次都从头重建规则状态)。"""
        msgs: list[str] = []
        for rt in self._rules:
            msgs.extend(self._seed_rule(rt))
        return msgs

    def _db_bars(self, rt: _Rt, symbol: str, now_ms: float) -> list:
        """klines 表里这个品种最近 SEED_LIMIT 根已收盘K(规整成 6 元组)。库不可用时抛异常。"""
        out = []
        for r in self.db.get_klines(symbol, rt.tf, SEED_LIMIT):
            n = _norm_bar(r)
            if n is None or n[0] + rt.tf_ms > now_ms + 1000:     # 按本机时钟还没收盘的不要
                continue
            out.append(n)
        return out

    def _seed_rule(self, rt: _Rt) -> list[str]:
        rt.seed_attempts += 1
        try:
            now_ms = self._safe_now() * 1000.0
            tf_ms = rt.tf_ms
            trig = self._db_bars(rt, rt.trigger, now_ms)
            tgt = self._db_bars(rt, rt.target, now_ms)
            rule = BtcBreakRule(rt.params)
            events: list[dict] = []
            for sym, n in _merged_feed(rt.params, trig, tgt):   # 与 rule.seed() 同一个顺序, 只是把事件留下来
                events.extend(rule.on_bar(sym, n))
            # 库里的历史可能是旧的(回填失败/停机很久): 按本机时钟早已过了有效期的那一轮, 悄悄让它到期
            # (多留一根K的余量: 收盘时刻恰好等于到期时刻的那根K仍算有效)。旧事件不通知。
            silent = rule.expire_until(int(now_ms) - tf_ms)
            rt.rule = rule
            rt.fed = {rt.trigger: {n[0]: n for n in trig}, rt.target: {n[0]: n for n in tgt}}
            rt.hole_w, rt.hole_from, rt.refetch_attempts = None, {}, 0
            st = rule.state()
            rt.last = {rt.trigger: st["last_bar"]["trigger"], rt.target: st["last_bar"]["target"]}
            rt.seed_info = {
                "at": int(now_ms // 1000),
                "trigger_bars": len(trig), "target_bars": len(tgt),
                "trigger_first": trig[0][0] if trig else None,
                "trigger_last": trig[-1][0] if trig else None,
                "target_last": tgt[-1][0] if tgt else None,
            }
            rt.seed_error = ""
            rt.seeded = True
            msgs: list[str] = []
            # ---- 停机期间出了结果的轮次: 「待命」当时通知过(库里有占位)、结果却没报过 → 现在补报一次 ----
            for ev in events:
                kind = ev.get("type")
                if kind in ("entry", "expired") and self._outcome_owed(rt, ev):
                    self._handle(rt, ev, True, msgs)
                else:
                    self._mark_seen(rt, ev)                    # 历史: 只记键, 之后的重建不会再把它当新事件
            for ev in silent:
                self._mark_seen(rt, ev)
            if st["phase"] == "armed":
                rt.last_trigger = dict(st["trigger"])
                if self._claim_soft(f"{rt.name}:armed", rt.trigger, rt.tf, st["episode_id"]):
                    msgs.append(self._armed_msg(rt, st["trigger"], st["expires_at"], restored=True))
                    rt.sent[("armed", int(st["episode_id"]))] = f"待命(破位K {_ms_bkk(st['trigger']['time'])})"
            else:
                le = st.get("last_episode")
                if le:
                    rt.last_trigger = dict(le["trigger"])
                    if le.get("outcome") == "entry" and le.get("entry"):
                        # 历史里最近一次入场: 库里有占位 = 已入库(当时, 或刚才补报时); 没有 = 发生在停机期间
                        # 且那一轮从没通知过(不补记、不补开模拟单)
                        ent = le["entry"]
                        sid = self._recorded_signal_id(rt, ent)
                        cur = rt.last_entry
                        if not (cur and cur.get("fx_low_time") == ent.get("fx_low_time")
                                and cur.get("signal_id") is not None):
                            rt.last_entry = self._entry_brief(ent, sid, None, None)
                        rt.last_entry["recorded"] = sid is not None
            self._log(f"{rt.name} seeded: {rt.trigger} {len(trig)} 根 / {rt.target} {len(tgt)} 根, "
                      f"phase={st['phase']} warm={st['warm']}")
            return msgs
        except Exception as exc:
            self._note_error("seed")
            rt.seed_error = f"{type(exc).__name__}: {exc}"[:200]
            rt.rule = BtcBreakRule(rt.params)            # 不留半截状态: 回到「全新、未铺满」→ 不会触发
            rt.last = {rt.trigger: None, rt.target: None}
            rt.fed = {rt.trigger: {}, rt.target: {}}
            rt.seeded = rt.seed_attempts >= SEED_MAX_ATTEMPTS
            return []

    def _mark_seen(self, rt: _Rt, ev: dict) -> tuple:
        key = _ev_key(ev)
        rt.seen[key] = event_instant(ev, rt.tf_ms)
        if len(rt.seen) > SEEN_KEEP:                         # 只留最近的一半(按判定时刻)
            keep = sorted(rt.seen.items(), key=lambda kv: kv[1])[-SEEN_KEEP // 2:]
            rt.seen = dict(keep)
            rt.sent = {k: v for k, v in rt.sent.items() if k in rt.seen}
        return key

    def _claim_exists(self, strategy: str, symbol: str, tf: str, anchor_ms) -> bool:
        """库里有没有这个占位。查不了(库坏了)按「没有」算 —— 预热补报宁可不报, 不乱报。"""
        try:
            return self.db.one(
                "SELECT 1 AS x FROM signal_anchors WHERE strategy=? AND symbol=? AND tf=? "
                "AND direction='long' AND anchor_time=?", (strategy, symbol, tf, int(anchor_ms))) is not None
        except Exception:
            return False

    def _outcome_owed(self, rt: _Rt, ev: dict) -> bool:
        """预热时: 这个结果事件(入场/到期)是否「欠一条通知」= 那一轮的待命通知过, 而这个结果没处理过。"""
        eid = ev.get("episode_id")
        if eid is None or not self._claim_exists(f"{rt.name}:armed", rt.trigger, rt.tf, eid):
            return False
        if ev.get("type") == "entry":
            e = ev["entry"]
            return not self._claim_exists(rt.name, str(e["symbol"]), str(e["tf"]), e["fx_low_time"])
        return not self._claim_exists(f"{rt.name}:expired", rt.trigger, rt.tf, eid)

    # ------------------------------------------------------------ 幂等占位
    def _claim(self, strategy: str, symbol: str, tf: str, anchor_ms) -> bool:
        """占位成功(=第一次见到)返回 True。库不可用时抛异常, 由调用方决定怎么降级。"""
        return bool(self.db.claim_signal_anchor(strategy, symbol, tf, "long", int(anchor_ms)))

    def _recorded_signal_id(self, rt: _Rt, entry: dict):
        """这次入场(规则名 + 分型低点K)当时入库的 signals.id; 没记过/查不到 → None。"""
        try:
            row = self.db.one(
                "SELECT signal_id FROM signal_anchors WHERE strategy=? AND symbol=? AND tf=? "
                "AND direction='long' AND anchor_time=?",
                (rt.name, str(entry["symbol"]), str(entry["tf"]), int(entry["fx_low_time"])))
            return row["signal_id"] if row else None
        except Exception:
            return None

    def _claim_soft(self, strategy: str, symbol: str, tf: str, anchor_ms) -> bool:
        """通知类占位: 库出问题时宁可多发一条, 也不漏发。"""
        try:
            return self._claim(strategy, symbol, tf, anchor_ms)
        except Exception:
            self._note_error("claim")
            return True

    # ------------------------------------------------------------ 对外: 收盘K
    def on_closed_bar(self, symbol: str, tf: str, bar) -> list[str]:
        rts = self._by_key.get((symbol, tf))
        if not rts:
            return []
        msgs: list[str] = []
        for rt in rts:
            try:
                self._feed(rt, symbol, bar, msgs)
            except Exception:
                self._note_error("on_closed_bar")
        return msgs

    def _feed(self, rt: _Rt, symbol: str, bar, msgs: list) -> None:
        if not rt.seeded:
            msgs.extend(self._seed_rule(rt))
        t = int(bar[0])
        tf_ms = rt.tf_ms
        last = rt.last.get(symbol)
        if last is not None and t <= last:
            return                                        # 重复/乱序(预热已含这根) → 规则本来也会忽略
        # ---- 断线缺K: 两个品种一起按时间顺序重建(见模块说明), 当前这根K在重建里一并处理 ----
        if last is not None and t > last + tf_ms:
            try:
                done = self._resync(rt, msgs, (symbol, bar))
            except Exception:
                done = False
                self._note_error("resync")
            self._update_refetch_flag()
            if done:
                return
            # 重建没做成(异常/这根K本身畸形): 退回最朴素的做法 —— 缺口跳过, 当前这根照常喂, 至少它不丢
            rt.counts["gap_missing"] += (t - last) // tf_ms - 1
        rt.last[symbol] = t
        rt.counts["bars"] += 1
        self._remember(rt, symbol, bar)
        for ev in rt.rule.on_bar(symbol, bar):
            self._handle(rt, ev, False, msgs)

    def _remember(self, rt: _Rt, symbol: str, bar) -> None:
        """记下喂过的K(重建时保证不比规则已经看到的少)。"""
        n = _norm_bar(bar)
        if n is None:
            return
        d = rt.fed.setdefault(symbol, {})
        d[n[0]] = n
        if len(d) > FED_KEEP + 64:
            for k in sorted(d)[:len(d) - FED_KEEP]:
                del d[k]

    # ------------------------------------------------------------ 重建(断线缺K / 回填之后 / 补拉之后)
    def resync(self) -> list[str]:
        """引擎在「klines 表刚被补过」之后调用(看门狗回填、后台补拉缺K): 按补全后的历史重建全部规则。
        返回要发的文字(补报/更正)。本身不联网; 一次约十几毫秒, 只在这些少见的时刻跑。永不抛异常。"""
        msgs: list[str] = []
        for rt in self._rules:
            try:
                if not rt.seeded or all(v is None for v in rt.last.values()):
                    msgs.extend(self._seed_rule(rt))
                else:
                    self._resync(rt, msgs, None)
            except Exception:
                self._note_error("resync")
        self._update_refetch_flag()
        return msgs

    def wants_refetch(self) -> bool:
        """有历史里也找不到的缺K、且还值得再补拉一次。只读一个布尔值(收盘K回调里每根都问)。"""
        return self._refetch_wanted

    def refetch_requests(self) -> list[tuple]:
        """→ [(symbol, tf, 根数)]: 请引擎用公开K线接口补拉最近这么多根并写进 klines 表, 然后调 resync()。
        每调用一次算一次尝试; 间隔不足 REFETCH_MIN_INTERVAL_S 或次数用完时返回空。永不抛异常。"""
        out: list[tuple] = []
        try:
            now = self._safe_now()
            for rt in self._rules:
                if rt.hole_w is None:
                    continue
                if rt.refetch_attempts >= REFETCH_MAX_ATTEMPTS or now - rt.hole_at > HOLE_MAX_AGE_S:
                    self._give_up_hole(rt)
                    continue
                if now < rt.refetch_at:
                    continue
                rt.refetch_attempts += 1
                rt.refetch_at = now + REFETCH_MIN_INTERVAL_S
                for sym in (rt.trigger, rt.target):
                    start = rt.hole_from.get(sym)
                    if start is None:
                        continue
                    n = int((now * 1000.0 - start) // rt.tf_ms) + 5
                    out.append((sym, rt.tf, max(10, min(SEED_LIMIT, n))))
        except Exception:
            self._note_error("refetch_requests")
        self._update_refetch_flag()
        return out

    def _give_up_hole(self, rt: _Rt) -> None:
        rt.counts["gap_lost"] += rt.counts["gap_missing"]
        rt.counts["gap_missing"] = 0
        rt.hole_w, rt.hole_from, rt.refetch_attempts = None, {}, 0
        self._log(f"{rt.name} 缺K未能补回, 放弃(累计 {rt.counts['gap_lost']} 根)")

    def _update_refetch_flag(self) -> None:
        try:
            self._refetch_wanted = any(rt.hole_w is not None for rt in self._rules)
        except Exception:
            self._refetch_wanted = False

    def _resync(self, rt: _Rt, msgs: list, cur) -> bool:
        """重建一条规则。cur = (symbol, bar): 触发这次重建的那根实时K(一并喂入); None = 引擎主动调用。
        做成返回 True。先在局部把新状态算完, 最后一步才替换 rt 上的东西 —— 中途抛异常不留半截状态。"""
        tf_ms = rt.tf_ms
        now = self._safe_now()
        now_ms = now * 1000.0
        if rt.hole_w is not None and now - rt.hole_at > HOLE_MAX_AGE_S:
            self._give_up_hole(rt)
        cur_sym = cur_t = cn = None
        if cur is not None:
            cur_sym = cur[0]
            cn = _norm_bar(cur[1])
            if cn is None or cn[0] % tf_ms:
                return False
            cur_t = cn[0]
        lasts = [v for v in rt.last.values() if v is not None]
        # 水位: 判定时刻不晚于它的事件, 之前都已按完整的数据处理过。有没补回的缺口时, 水位停在缺口起点。
        w = rt.hole_w if rt.hole_w is not None else (min(lasts) + tf_ms if lasts else None)
        if w is None:
            return False
        # ---- 历史 = klines 表 ∪ 引擎缓存 ∪ 本运行器喂过的K ∪ 当前这根 ----
        hist: dict[str, dict] = {}
        for sym in (rt.trigger, rt.target):
            by: dict[int, tuple] = {}
            try:
                for n in self._db_bars(rt, sym, now_ms):
                    by[n[0]] = n
            except Exception:
                self._note_error("resync_db")                 # 库读不了: 用缓存和喂过的K照样能重建
            try:
                for b in self._get_bars(sym, rt.tf) or ():
                    n = _norm_bar(b)
                    if n is not None and n[0] % tf_ms == 0:
                        by[n[0]] = n
            except Exception:
                self._note_error("resync_cache")
            by.update(rt.fed.get(sym) or {})                  # 规则实际看到过的那一份优先
            if sym == cur_sym:
                by[cur_t] = cn
            if cur_t is not None:                             # 晚于当前这根的不喂(保持「时间不减」)
                for k in [k for k in by if k > cur_t]:
                    del by[k]
            hist[sym] = by
        trig = [hist[rt.trigger][k] for k in sorted(hist[rt.trigger])]
        tgt = [hist[rt.target][k] for k in sorted(hist[rt.target])]
        rule = BtcBreakRule(rt.params)
        events: list[tuple] = []
        for sym, n in _merged_feed(rt.params, trig, tgt):
            live = cur_t is not None and sym == cur_sym and n[0] == cur_t
            for ev in rule.on_bar(sym, n):
                events.append((ev, live))
        # ---- 缺口统计: 这次补进来几根, 还缺几根 ----
        filled = missing = 0
        hole_from = dict(rt.hole_from)
        for sym in (rt.trigger, rt.target):
            keys = sorted(hist[sym])
            old = rt.last.get(sym)
            had = rt.fed.get(sym) or {}
            filled += sum(1 for k in keys if k not in had and not (sym == cur_sym and k == cur_t))
            start = hole_from.get(sym, old)
            if start is None or not keys or keys[-1] <= start:
                continue
            miss = (keys[-1] - start) // tf_ms - sum(1 for k in keys if k > start)
            if miss > 0:
                missing += miss
                hole_from.setdefault(sym, start)
            else:
                hole_from.pop(sym, None)
        # ---- 提交 ----
        rt.rule = rule
        for sym in (rt.trigger, rt.target):
            keys = sorted(hist[sym])[-FED_KEEP:]
            rt.fed[sym] = {k: hist[sym][k] for k in keys}
        st = rule.state()
        rt.last = {rt.trigger: st["last_bar"]["trigger"], rt.target: st["last_bar"]["target"]}
        rt.counts["resync"] += 1
        rt.counts["gap_filled"] += filled
        rt.counts["gap_missing"] = missing
        if cur_t is not None:
            rt.counts["bars"] += 1
        if missing > 0:
            if rt.hole_w is None:
                rt.hole_w, rt.hole_at, rt.refetch_attempts, rt.refetch_at = w, now, 0, 0.0
            rt.hole_from = {k: v for k, v in hole_from.items() if v is not None}
        else:
            rt.hole_w, rt.hole_from, rt.refetch_attempts = None, {}, 0
        # ---- 事件: 只处理水位之后、且没处理过的 ----
        replayed = set()
        for ev, live in events:
            key = _ev_key(ev)
            replayed.add(key)
            inst = event_instant(ev, tf_ms)
            if inst <= w or key in rt.seen:
                continue
            if now_ms - inst > LATE_REPORT_MAX_S * 1000.0:        # 太久以前的: 不补报、不入库, 只记下已处理
                self._mark_seen(rt, ev)
                continue
            self._handle(rt, ev, not live, msgs)
        # ---- 更正: 之前按不完整的K线发过的「待命 / 模拟入场」, 补全后重算已不成立 ----
        gone = [k for k, inst in rt.seen.items()
                if inst > w and k not in replayed and k[0] in ("armed", "entry")]
        if gone:
            told = [rt.sent[k] for k in gone if k in rt.sent]
            for k in gone:
                rt.seen.pop(k, None)
                rt.sent.pop(k, None)
            rt.counts["retracted"] += len(gone)
            if told:
                msgs.append(self._retract_msg(rt, told, now))
                self._log(f"{rt.name} 更正: {'; '.join(told)}")
        return True

    def _handle(self, rt: _Rt, ev: dict, late: bool, msgs: list) -> None:
        try:
            kind = ev.get("type")
            key = self._mark_seen(rt, ev)
            if kind == "armed":
                rt.counts["armed"] += 1
                rt.last_trigger = dict(ev["trigger"])
                if self._claim_soft(f"{rt.name}:armed", rt.trigger, rt.tf, ev["episode_id"]):
                    msgs.append(self._armed_msg(rt, ev["trigger"], ev["expires_at"], late=late))
                    rt.sent[key] = f"待命(破位K {_ms_bkk(ev['trigger']['time'])})"
                    self._log(f"{rt.name} armed @ {_ms_bkk(ev['trigger']['time'] + rt.tf_ms)}")
            elif kind == "retrigger":
                rt.counts["retrigger"] += 1
                rt.last_trigger = dict(ev["trigger"])
                self._log(f"{rt.name} retrigger, 有效期延至 {_ms_bkk(ev['expires_at'])}")
            elif kind == "entry":
                rt.counts["entry"] += 1
                m = self._on_entry(rt, ev, late)
                if m:
                    msgs.append(m)
                    sid = (rt.last_entry or {}).get("signal_id")
                    rt.sent[key] = (f"模拟入场(信号K {_ms_bkk(ev['entry']['signal_time'])}"
                                    + (f", 信号 #{sid}" if sid is not None else "") + ")")
            elif kind == "expired":
                rt.counts["expired"] += 1
                rt.last_expired = int(ev["expired_at"])
                if self._claim_soft(f"{rt.name}:expired", rt.trigger, rt.tf, ev["episode_id"]):
                    msgs.append(self._expired_msg(rt, ev, late))
                    self._log(f"{rt.name} expired @ {_ms_bkk(ev['expired_at'])}")
        except Exception:
            self._note_error(f"handle_{ev.get('type')}")

    # ------------------------------------------------------------ 入场: 入库 + 模拟单
    def _on_entry(self, rt: _Rt, ev: dict, late: bool) -> str | None:
        e, trig = ev["entry"], ev["trigger"]
        symbol, tf = str(e["symbol"]), str(e["tf"])
        entry, sl, tp = float(e["entry"]), float(e["sl"]), float(e["tp"])
        now = self._safe_now()
        signal_close = (int(e["signal_time"]) + rt.tf_ms) / 1000.0
        if now - signal_close > LATE_AFTER_BARS * rt.tf_ms / 1000.0:
            late = True                                   # 处理得太晚: 入场价(信号K收盘)已不是现在的价
        # 幂等: 同一规则 + 同一分型低点K 只记一次(重启、重复喂K、误起了第二个进程都挡得住)
        try:
            claimed = self._claim(rt.name, symbol, tf, e["fx_low_time"])
        except Exception:
            # 库不可用: 无法确认是否记过 → 不入库、不开模拟单, 但形态出现这件事仍然要让人知道
            self._note_error("entry_claim")
            rt.last_entry = self._entry_brief(e, None, None, late)
            return self._entry_msg(rt, ev, None, None, late)
        if not claimed:
            rt.counts["dup_skipped"] += 1
            return None
        risk = entry - sl
        equity = float(self.cfg.get("risk.account_equity", 1000) or 1000)
        risk_pct = float(self.cfg.get("risk.risk_pct", 0.5) or 0.5)
        risk_usdt = equity * risk_pct / 100.0
        qty = risk_usdt / risk if risk > 0 else 0.0
        sig = Signal(
            symbol=symbol, tf=tf, direction="long", kind=SIGNAL_KIND,
            entry=entry, sl=sl, tp=tp, rr=float(e["rr"]),
            vol_ratio=float(trig.get("vol_ratio") or 0.0), strength="normal",
            suggested_qty=round(qty, 8), risk_usdt=round(risk_usdt, 2),
            reason=(f"联动规则 {rt.name}(模拟, 无实盘): {rt.trigger} {tf} 放量跌破"
                    f"{_g(rt.params.lookback_hours)}h低点 → {symbol} 首个底分型"
                    + ("+停顿" if rt.params.require_stall else "")),
            created_at=int(now),
            extra={"type": rt.name, "paper_only": True, "linked_rule": rt.name,
                   "episode_id": ev.get("episode_id"), "late": bool(late), "event": ev},
        )
        row = sig.to_db()
        row["status"] = SIGNAL_STATUS
        try:
            sid = self.db.insert_signal(row)
            # state 默认是 try(缠论买点的生命周期); 这一行不走那套 → 标成 sim, 生命周期扫描不会碰它
            self.db.execute("UPDATE signals SET state='sim' WHERE id=?", (sid,))
            self.db.execute(
                "UPDATE signal_anchors SET signal_id=? WHERE strategy=? AND symbol=? AND tf=? "
                "AND direction='long' AND anchor_time=?", (sid, rt.name, symbol, tf, int(e["fx_low_time"])))
        except Exception:
            # 占位已成功但入库失败: 不开模拟单(没有信号行可挂), 通知照发并写明
            self._note_error("entry_insert")
            rt.last_entry = self._entry_brief(e, None, None, late)
            return self._entry_msg(rt, ev, None, None, late)
        trade_id = None
        if rt.paper and not late and qty > 0:
            try:
                self.paper.open_from_signal(sid, dataclasses.replace(sig, kind=PAPER_KIND))
                got = self.db.one(
                    "SELECT id FROM paper_trades WHERE signal_id=? AND track=? ORDER BY id DESC LIMIT 1",
                    (sid, rt.name))
                trade_id = got["id"] if got else None
            except Exception:
                self._note_error("paper_open")
            if trade_id is not None:
                rt.counts["paper_opened"] += 1
        if late:
            rt.counts["late_entries"] += 1
        rt.last_entry = self._entry_brief(e, sid, trade_id, late)
        self._log(f"{rt.name} entry #{sid} {symbol} {tf} entry={entry} sl={sl} tp={tp} "
                  f"paper={'#' + str(trade_id) if trade_id is not None else '-'} late={late}")
        return self._entry_msg(rt, ev, sid, trade_id, late)

    @staticmethod
    def _entry_brief(e: dict, sid, trade_id, late) -> dict:
        return {"symbol": e.get("symbol"), "signal_time": e.get("signal_time"),
                "signal_time_bkk": _ms_bkk(e.get("signal_time")),
                "entry": e.get("entry"), "sl": e.get("sl"), "tp": e.get("tp"),
                "risk_pct": e.get("risk_pct"), "fx_low": e.get("fx_low"),
                "fx_low_time": e.get("fx_low_time"),
                "signal_id": sid, "paper_trade_id": trade_id, "late": late}

    # ------------------------------------------------------------ 文案(Telegram HTML, 曼谷时间)
    def _armed_msg(self, rt: _Rt, trig: dict, expires_at, restored: bool = False, late: bool = False) -> str:
        p = rt.params
        close_ms = int(trig["time"]) + rt.tf_ms
        head = f"🔗 <b>联动规则待命</b> {html.escape(rt.name)}"
        if restored:
            head += "(启动时补报)"
        elif late:
            head += "(补报: 这根K是事后补回的)"
        return (f"{head}\n"
                f"{html.escape(rt.trigger)} {html.escape(rt.tf)} {_bar_label(trig['time'])}这根K收盘 "
                f"{fmt_price(trig['close'])} 跌破{_g(p.lookback_hours)}h低点 {fmt_price(trig['ref_low'])} 且放量"
                f"({float(trig.get('vol_ratio') or 0):.1f}倍均量)\n"
                f"等 {html.escape(rt.target)} 第一个底分型 · 有效至 {_ms_bkk(expires_at)}\n"
                f"{_ms_bkk(close_ms)}")

    def _entry_msg(self, rt: _Rt, ev: dict, sid, trade_id, late: bool) -> str:
        e, trig = ev["entry"], ev["trigger"]
        sym = html.escape(str(e["symbol"]))
        shape = "底分型+停顿" if rt.params.require_stall else "底分型"
        if sid is None:
            tail = "入库失败, 未开模拟单"
        elif trade_id is not None:
            tail = f"模拟单 #{trade_id} · 信号 #{sid}"
        elif late:
            tail = f"信号K已过, 未开模拟单 · 信号 #{sid}"
        elif not rt.paper:
            tail = f"未开模拟单(paper=false) · 信号 #{sid}"
        else:
            tail = f"模拟单未建立 · 信号 #{sid}"
        head = "模拟入场·补报" if late else "模拟入场"
        return (f"🧪 <b>{head}</b>(无实盘下单) {sym} {html.escape(str(e['tf']))} 做多\n"
                f"{html.escape(rt.name)}: {html.escape(rt.trigger)} 破位K {_bar_label(trig['time'])} → "
                f"{shape}, 低点 {fmt_price(e['fx_low'])}({_bar_label(e['fx_low_time'])}那根K)\n"
                f"入场 {fmt_price(e['entry'])} · 止损 {fmt_price(e['sl'])} · 止盈 {fmt_price(e['tp'])}\n"
                f"风险 {float(e['risk_pct']):.2f}% · 盈亏比 {_g(e['rr'])} · {tail}\n"
                f"{_ms_bkk(int(e['signal_time']) + rt.tf_ms)}")

    def _expired_msg(self, rt: _Rt, ev: dict, late: bool = False) -> str:
        head = "联动规则到期(补报)" if late else "联动规则到期"
        return (f"⌛ <b>{head}</b> {html.escape(rt.name)}: 待命期内 {html.escape(rt.target)} "
                f"未出现入场形态 · {_ms_bkk(ev['expired_at'])}")

    def _retract_msg(self, rt: _Rt, told: list, now: float) -> str:
        return (f"♻️ <b>联动规则更正</b> {html.escape(rt.name)}: 补回缺失的K线后重算, 之前的 "
                f"{html.escape('、'.join(told))} 不成立(已入库的记录与模拟单保留) · {bkk(now)}")

    # ------------------------------------------------------------ 对外: 状态
    def _rule_status(self, rt: _Rt) -> dict:
        p = rt.params
        st = rt.rule.state()
        now_ms = self._safe_now() * 1000.0
        look_ms = int(round(float(p.lookback_hours) * 3_600_000))
        warm = bool(st["warm"])
        first = rt.seed_info.get("trigger_first")
        ttl_ms = int(round(float(p.ttl_hours) * 3_600_000))
        # 重启后「此刻是不是待命」要可信, 历史得覆盖 lookback + ttl
        phase_certain = bool(warm and first is not None and first <= now_ms - look_ms - ttl_ms)
        last_t = st["last_bar"]["trigger"]
        last_g = st["last_bar"]["target"]
        stale = [s for s, t in ((rt.trigger, last_t), (rt.target, last_g))
                 if t is None or now_ms - (t + rt.tf_ms) > 3 * rt.tf_ms]
        if rt.seed_error:
            note = f"预热失败({rt.seed_error}), 规则从零开始积累K线"
        elif not rt.seeded:
            note = "尚未预热"
        elif not warm:
            note = (f"{rt.trigger} {rt.tf} 历史不足: {_g(p.lookback_hours)}h 窗口内 "
                    f"{st['window_bars']}/{st['window_need']} 根, 铺满前不触发")
        elif not phase_certain:
            note = f"窗口已铺满; 启动前 {_g(p.ttl_hours)}h 内若有过破位可能未被识别"
        else:
            note = "历史充足"
        phase = st["phase"]
        lt = rt.last_trigger
        out = {                                           # 键的先后 = 页面/Telegram 明细行的先后(只显示前十几行)
            "rule": rt.name, "status": "未审核 · 仅提醒与模拟, 无实盘通道",
            "phase": phase, "paper": rt.paper,
            "armed_at_bkk": _ms_bkk(st["armed_at"]), "expires_at_bkk": _ms_bkk(st["expires_at"]),
            "history_ok": warm, "history_note": note,
            "ref_low": st["ref_low"], "ref_low_time_bkk": _ms_bkk(st["ref_low_time"]),
            "vol_threshold": st["vol_threshold"],
            "last_trigger": None if lt is None else {
                "close_time_bkk": _ms_bkk(int(lt["time"]) + rt.tf_ms), "close": lt.get("close"),
                "ref_low": lt.get("ref_low"), "vol_ratio": lt.get("vol_ratio"), "time": lt.get("time")},
            "last_entry": rt.last_entry,
            "counts": dict(rt.counts),
            "last_bar_bkk": {rt.trigger: _ms_bkk(None if last_t is None else last_t + rt.tf_ms),
                             rt.target: _ms_bkk(None if last_g is None else last_g + rt.tf_ms)},
            "stale_symbols": stale,
            "armed_at": st["armed_at"], "expires_at": st["expires_at"],
            "phase_certain": phase_certain,
            "window_bars": st["window_bars"], "window_need": st["window_need"],
            "trigger_symbol": rt.trigger, "target_symbol": rt.target, "tf": rt.tf,
            "seed": dict(rt.seed_info),
        }
        # 一行摘要(网页卡片 / Telegram 列表直接用)
        if phase == "armed":
            s = (f"{rt.name} 待命中 · {rt.trigger} {_ms_bkk(st['armed_at'])} 破位 · "
                 f"等 {rt.target} 底分型 · 有效至 {_ms_bkk(st['expires_at'])}")
        elif not warm:
            s = f"{rt.name} 空闲 · {note}"
        else:
            s = f"{rt.name} 空闲"
            if st["ref_low"] is not None:
                s += (f" · 等 {rt.trigger} {rt.tf} 收盘跌破 {fmt_price(st['ref_low'])}"
                      f"({_g(p.lookback_hours)}h低点)且量≥{_g(p.vol_mult)}倍均量")
        if stale:
            s += " · 无最新K线: " + "/".join(stale)
        if rt.counts["gap_missing"]:
            s += f" · 缺K {rt.counts['gap_missing']} 根" + ("(补拉中)" if rt.hole_w is not None else "")
        if rt.counts["gap_lost"]:
            s += f" · 缺K {rt.counts['gap_lost']} 根未补回"
        s += " · 模拟记账" if rt.paper else " · 仅提醒"
        out["summary"] = s
        return out

    def status(self) -> dict:
        try:
            per = [self._rule_status(rt) for rt in self._rules]
            if len(per) == 1:
                out = dict(per[0])
            else:
                out = {"summary": " ｜ ".join(r["summary"] for r in per) if per else "没有启用的联动规则",
                       "rules": per}
            out["errors"] = self._errors
            if self._config_errors:
                out["config_errors"] = list(self._config_errors)
            self._last_status = out
            return out
        except Exception:
            self._note_error("status")
            return dict(self._last_status) or {"summary": "联动规则状态读取失败", "errors": self._errors}
