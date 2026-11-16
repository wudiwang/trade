"""Daily live-trade review renderers: Telegram text and a self-contained HTML page.

The whole report answers one question: "今天我有没有按规矩做?".  Discipline is the
subject everywhere; money appears once, last, in a neutral line, and is never
used as evidence for or against a trade.

Report dict (assembled elsewhere, see the shared contract).  This module reads:

* ``date``, ``window_label``, ``status`` ("reconciled" or anything else; a missing
  status is treated as not reconciled);
* ``trades`` (preferred) or ``episodes``: a list whose items are
  ``{"episode": {...}, "principles": asdict(PrincipleReview),
  "fit": asdict(SystemFit), "charts": {"15m": {"bars": [...]}, "1m": {...}}}``;
  ``episode`` carries episode_id / symbol / direction / status / net_pnl / fills
  (``{"exec_id", "action", "side", "time", "price", "qty"}``); a legacy flat
  episode dict (verdict / evidence / k1m / k15m on the episode) is tolerated;
* chart ``bars`` are ``[start_ms, open, high, low, close]`` rows; entry fills and
  exit legs are drawn from the episode and the principles.  A non-empty chart
  ``markers`` list (``{"time_ms", "price", "kind"}``) replaces them for that chart;
* ``score`` (``principles.day_score``; recomputed from the trades when absent),
  ``day_checks`` (list of Check dicts);
* ``ledger`` (or the legacy top-level keys) for net_pnl / closed_pnl / fees / funding;
* optional ``tz_offset_hours`` for clock times (default: Beijing, like the window);
* optional ``reconciliation_reasons``; otherwise the reasons are derived from the
  reconciliation fields the CLI already writes (grace_elapsed, funding_present, ...).

Every value may be a Decimal, an Enum, or its JSON round-tripped string form.
"""
from __future__ import annotations

import html
import math
from bisect import bisect_right
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .models import BEIJING
from .narrator import deterministic_narrative

VERDICT_ORDER = ("合规", "中途下车", "无止损", "止损被放宽", "逆向加仓",
                 "证据不足", "继承仓位不评价", "持仓中")
_VERDICT_CLASS = {
    "合规": "ok", "中途下车": "bad", "无止损": "bad", "止损被放宽": "bad", "逆向加仓": "bad",
    "证据不足": "unk", "继承仓位不评价": "na", "持仓中": "na",
}
_NOT_JUDGED = ("证据不足", "继承仓位不评价", "持仓中")
EXIT_LABELS = {
    "stop_loss": "止损触发", "take_profit": "止盈触发", "manual": "手动平仓",
    "liquidation": "强平", "trailing_stop": "移动止损触发", "adl": "ADL 减仓（交易所）",
    "unknown": "离场方式未知",
}
_ACTION_LABELS = {"entry": "入场", "add": "加仓", "reduce": "减仓", "close": "平仓"}
_STATE_ALIASES = {"pass": "pass", "fail": "fail", "unknown": "unknown", "na": "na",
                  "present": "pass", "absent": "fail"}
_STATE_ICON = {"pass": "✓", "fail": "✗", "unknown": "?", "na": "–"}
_STATE_WORD = {"pass": "通过", "fail": "未过", "unknown": "未知", "na": "不适用"}
_TREND_LABELS = {"up": "上涨", "down": "下跌", "range": "无序", "unknown": "未知"}
TELEGRAM_MAX_LINES = 18
_TELEGRAM_CAPTION_LIMIT = 1000  # Telegram allows 1024 characters on a photo caption.
_DASH = "—"
_COST_HEADING = "离场之后（事后对照，不改变结论）"


class DeliveryIncomplete(RuntimeError):
    """Telegram 只送达了一部分。``sent``：已经送达的部分（"summary" / "document"）。"""

    def __init__(self, message: str, sent=frozenset()):
        super().__init__(message)
        self.sent = frozenset(sent)


# --------------------------------------------------------------------------- formatting

def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _int(value: Any) -> int | None:
    number = _dec(value)
    return None if number is None else int(number)


def _num(value: Any, places: int | None = None) -> str:
    """Plain decimal text (no exponent); digits only, so it is safe to embed."""
    number = _dec(value)
    if number is None:
        return _DASH
    if places is not None:
        return format(number.quantize(Decimal(1).scaleb(-places)), "f")
    if number.as_tuple().exponent < -8:
        number = number.quantize(Decimal("0.00000001"))
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _money(value: Any) -> str:
    number = _dec(value)
    return _DASH if number is None else f"{number:+.2f}U"


def _fee(value: Any) -> str:
    number = _dec(value)
    return _DASH if number is None else f"{number:.2f}U"


def _r(value: Any) -> str:
    number = _dec(value)
    return _DASH if number is None else f"{number:+.2f}R"


def _rr(value: Any) -> str:
    number = _dec(value)
    return _DASH if number is None else _num(number.quantize(Decimal("0.01")))


def _zone(report: Mapping) -> tuple[tzinfo, str]:
    """Display time zone: ``tz_offset_hours`` in the report, else Beijing like the day window."""
    offset = _dec(report.get("tz_offset_hours"))
    if offset is None or not -12 <= offset <= 14:
        return BEIJING, "北京时间"
    name = {Decimal(8): "北京时间", Decimal(7): "曼谷时间"}.get(offset, f"UTC{offset:+g}")
    return timezone(timedelta(hours=float(offset))), name


def _clock(ms: Any, with_date: bool = False, tz: tzinfo = BEIJING) -> str:
    stamp = _int(ms)
    if stamp is None:
        return _DASH
    try:
        moment = datetime.fromtimestamp(stamp / 1000, tz)
    except (OverflowError, OSError, ValueError):
        return _DASH
    return moment.strftime("%m-%d %H:%M" if with_date else "%H:%M:%S")


def _duration(seconds: Any) -> str:
    total = _int(seconds)
    if total is None or total < 0:
        return _DASH
    if total < 60:
        return f"{total}秒"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时{minutes}分" if minutes else f"{hours}小时"
    days, hours = divmod(hours, 24)
    return f"{days}天{hours}小时" if hours else f"{days}天"


def _clip(text: Any, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _state(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "").strip().lower().rsplit(".", 1)[-1]
    return _STATE_ALIASES.get(raw, "unknown")


def _direction_word(direction: str, short: bool = False) -> str:
    if direction == "long":
        return "多" if short else "做多"
    if direction == "short":
        return "空" if short else "做空"
    return direction or "方向未知"


# --------------------------------------------------------------------------- view model

def _checks(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, Mapping):
        rows = list(raw.values())
    elif isinstance(raw, (list, tuple)):
        rows = list(raw)
    else:
        rows = []
    out = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("label") or row.get("key") or "").strip()
        if not label:
            continue
        value = row.get("value")
        out.append({
            "key": str(row.get("key") or label),
            "label": label,
            "tier": str(row.get("tier") or row.get("group") or ""),
            "state": _state(row.get("state")),
            "detail": str(row.get("detail") or "").strip(),
            "value": None if value is None else str(value),
        })
    return out


def _legs(raw: Any) -> list[dict[str, Any]]:
    out = []
    for row in raw or []:
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("kind") or "unknown")
        out.append({
            "time_ms": _int(row.get("time_ms", row.get("time"))),
            "qty": _dec(row.get("qty")),
            "price": _dec(row.get("price")),
            "kind": kind,
        })
    out.sort(key=lambda leg: leg["time_ms"] if leg["time_ms"] is not None else 0)
    return out


def _fills(raw: Any) -> list[dict[str, Any]]:
    out = []
    for row in raw or []:
        if not isinstance(row, Mapping):
            continue
        out.append({
            "exec_id": str(row.get("exec_id") or ""),
            "action": str(row.get("action") or ""),
            "side": str(row.get("side") or ""),
            "time_ms": _int(row.get("time_ms", row.get("time"))),
            "price": _dec(row.get("price")),
            "qty": _dec(row.get("qty")),
        })
    return out


def _weighted_price(rows: list[dict[str, Any]]) -> Decimal | None:
    qty = value = Decimal("0")
    for row in rows:
        if row["qty"] is None or row["price"] is None:
            continue
        qty += row["qty"]
        value += row["qty"] * row["price"]
    return value / qty if qty > 0 else None


def _is_cost_check(check: dict[str, Any]) -> bool:
    return "cost" in check["key"].lower() or "代价" in check["label"]


def _trade_view(row: Mapping, number: int, tz: tzinfo = BEIJING) -> dict[str, Any]:
    episode = row.get("episode") if isinstance(row.get("episode"), Mapping) else row
    principles = row.get("principles") if isinstance(row.get("principles"), Mapping) else {}
    fit = row.get("fit") if isinstance(row.get("fit"), Mapping) else {}
    charts = row.get("charts") if isinstance(row.get("charts"), Mapping) else {}
    if not charts:
        charts = {"15m": {"bars": episode.get("k15m") or []},
                  "1m": {"bars": episode.get("k1m") or []}}
    checks = _checks(principles.get("checks") if principles else episode.get("evidence"))
    fills = _fills(episode.get("fills"))
    exits = _legs(principles.get("exits"))
    if not exits:
        exits = [{"time_ms": fill["time_ms"], "qty": fill["qty"], "price": fill["price"],
                  "kind": "unknown"}
                 for fill in fills if fill["action"] in ("reduce", "close")]
    verdict = str(principles.get("verdict") or episode.get("verdict") or "证据不足")
    narrative = row.get("narrative") or episode.get("narrative")
    if not (isinstance(narrative, Mapping) and narrative.get("summary")):
        narrative = None
        if principles:
            built = deterministic_narrative(principles)
            narrative = {"summary": built.summary, "reasons": list(built.reasons),
                         "next_action": built.next_action, "source": built.source}
    cost = next((item for item in checks if _is_cost_check(item)), None)
    hard = [item for item in checks if item["tier"] == "hard" and item is not cost]
    # principles reports the comparison itself with state "na" plus an outcome code in
    # ``value``; "na" without a value means there was nothing to compare.
    show_cost = bool(cost and cost["detail"]
                     and (cost["value"] is not None or cost["state"] != "na"))
    return {
        "number": number,
        "tz": tz,
        "episode_id": str(episode.get("episode_id") or principles.get("episode_id") or number),
        "symbol": str(episode.get("symbol") or "未知标的"),
        "direction": str(episode.get("direction") or ""),
        "status": str(episode.get("status") or ""),
        "verdict": verdict,
        "verdict_class": _VERDICT_CLASS.get(verdict, "unk"),
        "hard": hard,
        "other": [item for item in checks if item["tier"] != "hard" and item is not cost],
        "failed": next((item for item in hard if item["state"] == "fail"), None),
        "cost": cost if show_cost else None,
        "exits": exits,
        "fills": fills,
        "entry_price": _weighted_price([f for f in fills if f["action"] in ("entry", "add")]),
        "exit_price": _weighted_price(exits),
        "planned_sl": _dec(principles.get("planned_sl")),
        "planned_tp": _dec(principles.get("planned_tp")),
        "planned_rr": _dec(principles.get("planned_rr")),
        "realized_r": _dec(principles.get("realized_r")),
        "hold_seconds": _int(principles.get("hold_seconds")),
        "net_pnl": _dec(episode.get("net_pnl")),
        "price_return_pct": _dec(episode.get("price_return_pct")),
        "fit": fit,
        "fit_evidence": _checks(fit.get("evidence")),
        "charts": charts,
        "narrative": narrative,
    }


def _exit_words(trade: dict[str, Any]) -> str:
    kinds: list[str] = []
    for leg in trade["exits"]:
        if leg["kind"] not in kinds:
            kinds.append(leg["kind"])
    if not kinds:
        return "未离场" if trade["verdict"] == "持仓中" or trade["status"] == "open" else "离场方式未知"
    return "+".join(EXIT_LABELS.get(kind, kind) for kind in kinds)


def _score(report: Mapping, trades: list[dict[str, Any]]) -> dict[str, Any]:
    by_verdict: dict[str, int] = {}
    broken: dict[str, dict[str, Any]] = {}
    for trade in trades:
        by_verdict[trade["verdict"]] = by_verdict.get(trade["verdict"], 0) + 1
        if trade["verdict"] in _NOT_JUDGED:
            continue                      # 持仓中 / 继承仓位 / 证据不足 不计入「最常破的规矩」
        for check in trade["hard"]:
            if check["state"] == "fail":
                entry = broken.setdefault(check["key"], {"key": check["key"],
                                                         "label": check["label"], "count": 0})
                entry["count"] += 1
    score: dict[str, Any] = {
        "total": len(trades),
        "judged": sum(1 for trade in trades if trade["verdict"] not in _NOT_JUDGED),
        "compliant": by_verdict.get("合规", 0),
        "by_verdict": by_verdict,
        "most_broken": max(broken.values(), key=lambda item: item["count"]) if broken else None,
    }
    given = report.get("score")
    if isinstance(given, Mapping):
        for key in ("total", "judged", "compliant"):
            value = _int(given.get(key))
            if value is not None:
                score[key] = value
        if isinstance(given.get("by_verdict"), Mapping):
            score["by_verdict"] = {str(k): _int(v) or 0 for k, v in given["by_verdict"].items()}
        if "most_broken" in given:
            top = given["most_broken"]
            score["most_broken"] = ({"key": str(top.get("key") or ""),
                                     "label": str(top.get("label") or top.get("key") or ""),
                                     "count": _int(top.get("count")) or 0}
                                    if isinstance(top, Mapping) else None)
    return score


def _rule_counts(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for trade in trades:
        for check in trade["hard"]:
            row = rules.setdefault(check["key"], {"key": check["key"], "label": check["label"],
                                                  "pass": 0, "fail": 0, "unknown": 0, "na": 0})
            row[check["state"]] += 1
    return list(rules.values())


def reconciliation_reasons(report: Mapping) -> list[str]:
    """Why the day is not an approved report; empty when it reconciled."""
    ledger = report.get("ledger") if isinstance(report.get("ledger"), Mapping) else {}
    status = report.get("status") or ledger.get("status")
    if status == "reconciled":
        return []
    given = report.get("reconciliation_reasons") or ledger.get("reconciliation_reasons")
    if isinstance(given, (list, tuple)) and given:
        return [str(item) for item in given if str(item).strip()]

    def get(name: str) -> Any:
        return report[name] if name in report else ledger.get(name)

    def has(name: str) -> bool:
        return name in report or name in ledger

    def over(name: str) -> Decimal | None:
        value = _dec(get(name))
        return value if value is not None and abs(value) > Decimal("0.01") else None

    reasons: list[str] = []
    if get("grace_elapsed") is False:
        reasons.append("窗口还没结束或结束不足 10 分钟，流水可能未到齐")
    if get("position_snapshot_stable") is False:
        reasons.append("拉取数据期间持仓发生了变化")
    if get("funding_present") is True:
        reasons.append("窗口内有资金费，暂不能逐笔归属")
    if (value := over("reconciliation_difference")) is not None:
        reasons.append(f"账户流水自检差额 {_fee(abs(value))}")
    if (value := over("execution_fee_difference")) is not None:
        reasons.append(f"成交手续费与流水手续费相差 {_fee(abs(value))}")
    if (value := over("closed_pnl_difference")) is not None:
        reasons.append(f"回合盈亏与交易所已结盈亏相差 {_fee(abs(value))}")
    elif has("closed_pnl_difference") and get("closed_pnl_difference") is None:
        reasons.append("有回合无法逐笔核对（继承仓位、未平仓或成交缺失）")
    if (value := over("ledger_closed_difference")) is not None:
        reasons.append(f"流水净变化与已结盈亏相差 {_fee(abs(value))}")
    if unmatched := get("unmatched_closed_orders"):
        reasons.append(f"{len(unmatched)} 笔已结盈亏找不到对应成交")
    if missing := get("missing_closed_pnl_orders"):
        reasons.append(f"{len(missing)} 笔平仓成交缺少已结盈亏记录")
    if not reasons:
        reasons.append(f"对账状态为 {status}" if status else "报告缺少对账状态")
    return reasons


def day_view(report: Mapping) -> dict[str, Any]:
    """Normalize a report dict once; all three renderers read this view."""
    rows = report.get("trades")
    if rows is None:
        rows = report.get("episodes")
    tz, tz_name = _zone(report)
    trades = [_trade_view(row, index, tz)
              for index, row in enumerate((r for r in rows or [] if isinstance(r, Mapping)), 1)]
    ledger = report.get("ledger") if isinstance(report.get("ledger"), Mapping) else {}

    def pick(*names: str) -> Decimal | None:
        for source in (ledger, report):
            for name in names:
                value = _dec(source.get(name))
                if value is not None:
                    return value
        return None

    status = report.get("status") or ledger.get("status")
    notes = report.get("data_notes")
    for trade in trades:
        trade["show_money"] = status == "reconciled"
    return {
        "date": str(report.get("date") or ""),
        "window_label": str(report.get("window_label") or ""),
        "tz_name": tz_name,
        "scope": str(report.get("scope_label") or "").strip(),
        "notes": [str(item).strip() for item in notes if str(item).strip()]
                 if isinstance(notes, (list, tuple)) else [],
        "trades": trades,
        "score": _score(report, trades),
        "rules": _rule_counts(trades),
        "day_checks": _checks(report.get("day_checks")),
        "reconciled": status == "reconciled",
        "reasons": reconciliation_reasons(report),
        "net_pnl": pick("net_pnl", "net_change"),
        "closed_pnl": pick("closed_pnl"),
        "fees": pick("fees"),
        "funding": pick("funding"),
    }


def _most_broken_text(score: Mapping, judged_any: bool) -> str:
    top = score.get("most_broken")
    if top and top.get("count"):
        return f"今日最常破的规矩：{top['label']}（{top['count']} 次）"
    return "今日没有破规矩的交易" if judged_any else "今日没有可评价的交易"


def headline(view: Mapping) -> tuple[str, str]:
    """(一句话, 语气 "ok" | "bad" | "warn")。逐笔之外，当日层面的违规和持仓中的未过项也要算进来：
    两笔各自合规、但止损后几分钟就反手，不能写成「今日没有破规矩的交易」。"""
    score = view["score"]
    judged_any = score["judged"] > 0
    failed_day = [check["label"] for check in view["day_checks"] if check["state"] == "fail"]
    open_failed = [trade for trade in view["trades"]
                   if trade["verdict"] == "持仓中" and trade["failed"]]
    extras = []
    if failed_day:
        extras.append(f"当日层面有 {len(failed_day)} 项违规：{'、'.join(failed_day)}")
    if open_failed:
        extras.append(f"持仓中的 {len(open_failed)} 笔有未过的规矩：{open_failed[0]['failed']['label']}")
    top = score.get("most_broken")
    if top and top.get("count"):
        # 当日层面的违规另有专门的一行（Telegram 的「⚠ 当日」、页面的「当日层面」），这里不重复
        return _most_broken_text(score, judged_any), "bad"
    if extras:
        lead = "已离场的交易逐笔规矩都过了" if judged_any else "今日没有可评价的已离场交易"
        return "；".join([lead] + extras), "warn"
    return _most_broken_text(score, judged_any), "ok"


def _empty_day_text(view: Mapping) -> str:
    return "没有交易需要评价" + (f"（只统计 {view['scope']}）" if view.get("scope") else "")


def _not_judged_text(score: Mapping) -> str:
    skipped = [(name, score["by_verdict"].get(name, 0)) for name in _NOT_JUDGED]
    parts = [f"{name} {count}" for name, count in skipped if count]
    return "不计入评价：" + " · ".join(parts) if parts else ""


# --------------------------------------------------------------------------- Telegram

def _telegram_trade_line(trade: dict[str, Any]) -> str:
    parts = [
        f"{trade['number']}. {_e(trade['symbol'])} {_e(_direction_word(trade['direction'], True))}",
        f"<b>{_e(trade['verdict'])}</b>",
        _e(_exit_words(trade)),
    ]
    if (trade["planned_rr"] is not None or trade["realized_r"] is not None
            or trade["verdict"] != "继承仓位不评价"):
        parts.append(f"计划RR {_rr(trade['planned_rr'])} · 实际 {_r(trade['realized_r'])}")
    if trade["failed"]:
        parts.append(f"未过：{_e(_clip(trade['failed']['label'], 16))}")
    return " · ".join(parts)


def telegram_summary(report: Mapping) -> str:
    """Telegram HTML, at most TELEGRAM_MAX_LINES lines, discipline first, money last."""
    view = day_view(report)
    trades, score = view["trades"], view["score"]
    date = _e(view["date"])
    head: list[str] = []
    tail: list[str] = []

    if not view["reconciled"]:
        head.append(f"<b>⚠ {date} 未核准 · 对账未通过</b>")
        head.extend(f"· {_e(_clip(reason, 40))}" for reason in view["reasons"][:3])
        if len(view["reasons"]) > 3:
            head.append(f"· 另有 {len(view['reasons']) - 3} 项，见完整报告")
        head.append(f"已识别 {len(trades)} 笔，以下纪律结论仅供排查，不计入当日成绩："
                    if trades else "今天没有识别到成交")
        tail.append("净盈亏未通过对账，不给数字")
    elif not trades:
        head.append(f"<b>{date} 实盘纪律 · 今天没有成交</b>")
        head.append(_e(_empty_day_text(view)))
    else:
        failed_count = sum(1 for check in view["day_checks"] if check["state"] == "fail")
        day_flag = f" · 当日层面 {failed_count} 项违规" if failed_count else ""
        head.append(f"<b>{date} 实盘纪律 · {score['total']} 笔，{score['compliant']} 笔合规"
                    f"{day_flag}</b>")
        head.append(_e(headline(view)[0]))
        if skipped := _not_judged_text(score):
            head.append(_e(skipped))
    if view["notes"]:
        more = f"（共 {len(view['notes'])} 条，见完整报告）" if len(view["notes"]) > 1 else ""
        tail.append(f"ℹ 数据：{_e(_clip(view['notes'][0], 60))}{more}")

    failed_day = [check for check in view["day_checks"] if check["state"] == "fail"]
    flags = [f"⚠ 当日：{_e(check['label'])}" + (f"（{_e(_clip(check['detail'], 36))}）"
                                                if check["detail"] else "")
             for check in failed_day[:2]]
    if len(failed_day) > 2:
        flags[-1] += f" 等 {len(failed_day)} 项"
    if view["reconciled"]:
        tail.append(f"当日净盈亏 {_money(view['net_pnl'])}（手续费 {_fee(view['fees'])}，"
                    f"资金费 {_money(view['funding'])}）")

    room = max(0, TELEGRAM_MAX_LINES - len(head) - len(flags) - len(tail))
    lines = [_telegram_trade_line(trade) for trade in trades]
    if len(lines) > room:
        # Keep every line that is not plainly compliant first, in time order.
        keep = max(0, room - 1)
        flagged = [trade for trade in trades if trade["verdict"] != "合规"]
        chosen = sorted((flagged + [t for t in trades if t["verdict"] == "合规"])[:keep],
                        key=lambda trade: trade["number"])
        hidden = [trade for trade in trades if trade not in chosen]
        hidden_ok = sum(1 for trade in hidden if trade["verdict"] == "合规")
        lines = [_telegram_trade_line(trade) for trade in chosen]
        lines.append(f"另有 {len(hidden)} 笔未列出（其中合规 {hidden_ok} 笔），见完整报告")
    return "\n".join(head + lines + flags + tail)


# --------------------------------------------------------------------------- charts

_CHART_W, _CHART_H = 360, 240
# 标签字号按「375px 手机上不小于 9px」定：图在手机上约 294px 宽，viewBox 的 11 个单位 ≈ 9px。
_PLOT_L, _PLOT_R, _PLOT_T, _PLOT_B = 6, 84, 18, 28
_FS, _FS_BOLD = 11, 11.5
_UP, _DOWN = "#3c9a82", "#c05862"
_SL_COLOR, _TP_COLOR, _MANUAL_COLOR = "#ff6b76", "#2fd39a", "#ff9f43"
_MARKER_STYLE = {
    "entry": ("入", "#f0b452"), "add": ("加", "#f0b452"),
    "take_profit": ("止盈", _TP_COLOR), "stop_loss": ("止损", _SL_COLOR),
    "manual": ("手动", _MANUAL_COLOR), "liquidation": ("强平", "#ff4d5e"),
    "adl": ("ADL", "#b58cff"),
    "trailing_stop": ("移损", "#75a7ff"), "unknown": ("离场", "#9aa6b8"),
    "reduce": ("减", "#9aa6b8"), "close": ("平", "#9aa6b8"),
}
_TF_WINDOW = {
    "1m": {"before": 30, "pad": 8, "after": 120, "max_bars": 150},
    "15m": {"before": 64, "pad": 4, "after": 32, "max_bars": 130},
}


def _norm_bars(bars: Any) -> list[tuple[int, float, float, float, float]]:
    out = []
    for row in bars or []:
        try:
            if isinstance(row, Mapping):
                stamp = row.get("start_ms", row.get("time", row.get("t")))
                item = (int(stamp), float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"]))
            else:
                item = (int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if all(math.isfinite(value) for value in item[1:]):
            out.append(item)
    out.sort()
    return out


def _aggregate(rows: list[tuple], factor: int) -> list[tuple]:
    if factor <= 1:
        return rows
    out = []
    for start in range(0, len(rows), factor):
        chunk = rows[start:start + factor]
        out.append((chunk[0][0], chunk[0][1], max(r[2] for r in chunk),
                    min(r[3] for r in chunk), chunk[-1][4]))
    return out


def _chart_points(fills: Any, exits: Any, markers: Any) -> list[dict[str, Any]]:
    """Point markers: explicit chart markers win, else entry fills + exit legs."""
    points: list[dict[str, Any]] = []
    if markers:
        for row in markers:
            if not isinstance(row, Mapping):
                continue
            stamp, price = _int(row.get("time_ms", row.get("time"))), _dec(row.get("price"))
            if stamp is None or price is None:
                continue
            points.append({"t": stamp, "price": float(price),
                           "kind": str(row.get("kind") or row.get("action") or "unknown"),
                           "side": str(row.get("side") or ""),
                           "id": str(row.get("exec_id") or "")})
        return points
    fill_rows = _fills(fills)
    for fill in fill_rows:
        if fill["action"] in ("entry", "add") and fill["time_ms"] is not None \
                and fill["price"] is not None:
            points.append({"t": fill["time_ms"], "price": float(fill["price"]),
                           "kind": fill["action"], "side": fill["side"], "id": fill["exec_id"]})
    legs = _legs(exits)
    if legs:
        for leg in legs:
            if leg["time_ms"] is not None and leg["price"] is not None:
                points.append({"t": leg["time_ms"], "price": float(leg["price"]),
                               "kind": leg["kind"], "side": "", "id": ""})
    else:
        for fill in fill_rows:
            if fill["action"] in ("reduce", "close") and fill["time_ms"] is not None \
                    and fill["price"] is not None:
                points.append({"t": fill["time_ms"], "price": float(fill["price"]),
                               "kind": fill["action"], "side": fill["side"],
                               "id": fill["exec_id"]})
    return points


def _aftermath(rows: list[tuple], exit_ms: int, direction: str,
               stop: float | None, target: float | None) -> dict[str, Any] | None:
    """What price did after a manual exit, until the original stop or target was touched."""
    if direction not in ("long", "short") or (stop is None and target is None):
        return None
    later = [(index, row) for index, row in enumerate(rows) if row[0] >= exit_ms]
    if not later:
        return {"outcome": "no_data"}
    # 只看首尾相接的 K 线：中间缺了一段就停在缺口前，不跨缺口说「此前未触及」。
    step = min((b[0] - a[0] for a, b in zip(rows, rows[1:]) if b[0] > a[0]), default=0)
    for position in range(1, len(later)):
        if step and later[position][1][0] - later[position - 1][1][0] > step * 1.5:
            later = later[:position]
            break
    for index, row in later:
        high, low = row[2], row[3]
        if direction == "long":
            hit_stop = stop is not None and low <= stop
            hit_target = target is not None and high >= target
        else:
            hit_stop = stop is not None and high >= stop
            hit_target = target is not None and low <= target
        if hit_stop or hit_target:
            outcome = "both" if hit_stop and hit_target else "target" if hit_target else "stop"
            return {"outcome": outcome, "index": index, "time": row[0]}
    return {"outcome": "none", "index": later[-1][0], "time": later[-1][1][0]}


def _aftermath_note(after: dict[str, Any] | None, stop: Any, target: Any,
                    basis: str = "", tz: tzinfo = BEIJING) -> str:
    """One factual sentence about the shaded stretch after a manual exit."""
    if not after:
        return ""
    outcome = after["outcome"]
    if outcome == "no_data":
        return "手动平仓之后没有K线数据，看不到后续走势"
    when = _clock(after["time"], True, tz)
    tail = f"（按{basis}K线）" if basis else ""
    if outcome == "none":
        known = "、".join(name for name, level in (("原止损", stop), ("原止盈", target))
                          if level is not None)
        return f"手动平仓后，到已有K线的末尾（{when}）为止，{known}未被触及{tail}"
    if outcome == "both":
        return f"手动平仓后，{when} 这根K线同时触及原止损与原止盈，先后无法分辨{tail}"
    if outcome == "target":
        other = "，此前未触及原止损" if stop is not None else ""
        return f"手动平仓后，价格在 {when} 触及原止盈 {_num(target)}{other}{tail}"
    other = "，此前未触及原止盈" if target is not None else ""
    return f"手动平仓后，价格在 {when} 触及原止损 {_num(stop)}{other}{tail}"


def _manual_exit_ms(points: list[dict[str, Any]]) -> int | None:
    stamps = [point["t"] for point in points if point["kind"] == "manual"]
    return min(stamps) if stamps else None


_AUTO: Any = object()


def _marker_shape(kind: str, x: float, y: float, side: str, direction: str, color: str) -> str:
    edge = 'stroke="#0b1018" stroke-width="1"'
    if kind in ("entry", "add"):
        buy = side.lower() == "buy" if side else direction != "short"
        tip = -6 if not buy else 6  # Buy: tip up at the price, body below it.
        return (f'<path d="M{x:.1f} {y:.1f} l-4.5 {tip} h9 z" fill="{color}" {edge}/>')
    if kind == "take_profit":
        return f'<path d="M{x:.1f} {y-5:.1f} l5 5 l-5 5 l-5 -5 z" fill="{color}" {edge}/>'
    if kind == "trailing_stop":
        return (f'<path d="M{x:.1f} {y-5:.1f} l5 5 l-5 5 l-5 -5 z" fill="#0b1018" '
                f'stroke="{color}" stroke-width="1.6"/>')
    if kind == "stop_loss":
        return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#0b1018" stroke="{color}" '
                f'stroke-width="1.6"/><path d="M{x-2.6:.1f} {y-2.6:.1f} l5.2 5.2 m0 -5.2 l-5.2 5.2" '
                f'stroke="{color}" stroke-width="1.6" fill="none"/>')
    if kind == "manual":
        return (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.6" fill="{color}" stroke="#ffffff" '
                f'stroke-width="1.4"/>')
    if kind == "liquidation":
        return (f'<rect x="{x-4.5:.1f}" y="{y-4.5:.1f}" width="9" height="9" fill="{color}" '
                f'stroke="#ffffff" stroke-width="1.2"/>')
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.6" fill="{color}" {edge}/>'


def _chart(bars: Any, fills: Any, tf: str, *, exits: Any = (), planned_sl: Any = None,
           planned_tp: Any = None, direction: str = "", markers: Any = None,
           after: Any = _AUTO, tz: tzinfo = BEIJING) -> tuple[str, str]:
    """Return (svg markup, plain-text note about what followed a manual exit).

    ``after`` is the result of one ``_aftermath`` scan shared by all charts of a
    trade; when omitted it is scanned from this chart's own bars.
    """
    rows = _norm_bars(bars)
    if not rows:
        return '<div class="empty">K线数据缺失，无法画图</div>', ""
    settings = _TF_WINDOW["15m" if str(tf).strip().startswith("15") else "1m"]
    stop_dec, target_dec = _dec(planned_sl), _dec(planned_tp)
    stop = float(stop_dec) if stop_dec is not None else None
    target = float(target_dec) if target_dec is not None else None
    points = _chart_points(fills, exits, markers)
    times = [row[0] for row in rows]
    step = min((b - a for a, b in zip(times, times[1:]) if b > a), default=60_000)
    locate = lambda stamp: max(0, bisect_right(times, stamp) - 1)

    manual_ms = _manual_exit_ms(points)
    note = ""
    if manual_ms is None:
        after = None
    elif after is _AUTO:
        after = _aftermath(rows, manual_ms, direction, stop, target)
        note = _aftermath_note(after, stop_dec, target_dec, tz=tz)

    if points:
        first, last = min(p["t"] for p in points), max(p["t"] for p in points)
        lo = max(0, locate(first) - settings["before"])
        hi = locate(last)
        if after and "time" in after:
            reach = locate(after["time"])
            if after["outcome"] == "none":
                reach = min(reach, hi + settings["after"])
            hi = max(hi, reach)
        hi = min(len(rows) - 1, hi + settings["pad"])
    else:
        hi = len(rows) - 1
        lo = max(0, hi - settings["max_bars"] + 1)
    window = rows[lo:hi + 1]
    factor = max(1, math.ceil(len(window) / settings["max_bars"]))
    shown = _aggregate(window, factor)
    shown_times = [row[0] for row in shown]
    bucket = step * factor
    count = len(shown)

    def index_of(stamp: int) -> int | None:
        # A fill just past the last candle (its bar not published yet) still belongs on it.
        if stamp < shown_times[0] or stamp >= shown_times[-1] + 2 * bucket:
            return None
        return max(0, bisect_right(shown_times, stamp) - 1)

    placed = [(point, index_of(point["t"])) for point in points]
    placed = [(point, index) for point, index in placed if index is not None]

    low = min(row[3] for row in shown)
    high = max(row[2] for row in shown)
    for point, _ in placed:
        low, high = min(low, point["price"]), max(high, point["price"])
    span = high - low or max(abs(high), 1.0) * 0.01
    levels = []
    for name, value, color in (("计划止损", stop, _SL_COLOR), ("计划止盈", target, _TP_COLOR)):
        if value is None:
            continue
        inside = low - span <= value <= high + span
        levels.append({"name": name, "value": value, "color": color, "inside": inside})
    for level in levels:
        if level["inside"]:
            low, high = min(low, level["value"]), max(high, level["value"])
    span = high - low or max(abs(high), 1.0) * 0.01
    low, high = low - span * 0.05, high + span * 0.05
    span = high - low

    plot_w = _CHART_W - _PLOT_L - _PLOT_R
    plot_h = _CHART_H - _PLOT_T - _PLOT_B
    right = _PLOT_L + plot_w
    x = lambda index: _PLOT_L + (index + 0.5) * plot_w / count
    y = lambda price: _PLOT_T + (high - price) * plot_h / span
    body_w = max(1.0, plot_w / count * 0.64)

    tf_label = str(tf)
    title = tf_label + (f" · 每根合并 {factor} 根" if factor > 1 else "")
    parts = [f'<svg viewBox="0 0 {_CHART_W} {_CHART_H}" role="img" '
             f'aria-label="{_e(tf_label)} K线，标出入场、离场、计划止损与计划止盈">',
             f'<rect x="{_PLOT_L}" y="{_PLOT_T}" width="{plot_w}" height="{plot_h}" '
             f'fill="#0e151f" rx="4"/>']

    exit_index = index_of(manual_ms) if manual_ms is not None else None
    if after and exit_index is not None and after["outcome"] != "no_data":
        end_index = index_of(after["time"])
        end_x = x(end_index) if end_index is not None and after["time"] >= manual_ms else right
        start_x = x(exit_index)
        parts.append(f'<rect class="after" x="{start_x:.1f}" y="{_PLOT_T}" '
                     f'width="{max(end_x - start_x, 2):.1f}" height="{plot_h}" '
                     f'fill="{_MANUAL_COLOR}" fill-opacity="0.13"/>')
        if end_x - start_x > 72:
            # Put the caption at whichever edge is farther from the planned levels.
            level_ys = [y(level["value"]) for level in levels if level["inside"]]
            # top edge, bottom edge, or one line further in when a planned level sits on the edge
            spots = (_PLOT_T + 12, _PLOT_T + plot_h - 4, _PLOT_T + 28, _PLOT_T + plot_h - 20)
            gap = lambda at: min((abs(at - 4 - ly) for ly in level_ys), default=99)
            caption_y = max(spots, key=gap)       # 离计划止损/止盈线最远的那个位置
            parts.append(f'<text x="{start_x + 3:.1f}" y="{caption_y:.1f}" '
                         f'fill="{_MANUAL_COLOR}" font-size="{_FS}">手动平仓之后</text>')

    wicks = {_UP: [], _DOWN: []}
    bodies = {_UP: [], _DOWN: []}
    for index, (_, open_, high_, low_, close) in enumerate(shown):
        color = _UP if close >= open_ else _DOWN
        cx = x(index)
        top, bottom = y(max(open_, close)), y(min(open_, close))
        wicks[color].append(f"M{cx:.1f} {y(high_):.1f}V{y(low_):.1f}")
        bodies[color].append(f"M{cx - body_w / 2:.1f} {top:.1f}h{body_w:.1f}"
                             f"v{max(bottom - top, 0.8):.1f}h{-body_w:.1f}z")
    for color in (_UP, _DOWN):
        if wicks[color]:
            parts.append(f'<path d="{"".join(wicks[color])}" stroke="{color}" '
                         f'stroke-width="0.8" fill="none"/>')
            parts.append(f'<path d="{"".join(bodies[color])}" fill="{color}"/>')

    gutter: list[float] = []
    entry_indices = [index for point, index in placed if point["kind"] in ("entry", "add")]
    line_start = x(min(entry_indices)) if entry_indices else _PLOT_L
    for level in levels:
        if level["inside"]:
            ly, suffix = y(level["value"]), ""
        else:
            above = level["value"] > high
            ly = _PLOT_T + 3 if above else _PLOT_T + plot_h - 3
            suffix = "↑图外" if above else "↓图外"
        parts.append(f'<line class="level" x1="{line_start:.1f}" x2="{right}" y1="{ly:.1f}" '
                     f'y2="{ly:.1f}" stroke="{level["color"]}" stroke-width="1.2" '
                     f'stroke-dasharray="5 3"/>')
        # Name and price sit in the right gutter, clear of candles and markers.
        label_y = min(max(ly + 10, _PLOT_T + 24), _PLOT_T + plot_h)
        shift = 27 if label_y < _PLOT_T + plot_h / 2 else -27
        while any(abs(label_y - used) < 27 for used in gutter):
            label_y += shift
        gutter.append(label_y)
        parts.append(f'<text x="{right + 4}" y="{label_y - 13:.1f}" fill="{level["color"]}" '
                     f'font-size="{_FS}">{level["name"]}{suffix}</text>')
        parts.append(f'<text x="{right + 4}" y="{label_y:.1f}" fill="{level["color"]}" '
                     f'font-size="{_FS_BOLD}" font-weight="600">{_num(level["value"])}</text>')
        if after and after.get("outcome") in ("both", "target" if level["name"] == "计划止盈"
                                              else "stop") and level["inside"]:
            touch = index_of(after["time"])
            if touch is not None:
                parts.append(f'<circle class="touch" cx="{x(touch):.1f}" cy="{ly:.1f}" r="6" '
                             f'fill="none" stroke="{level["color"]}" stroke-width="1.8"/>')
    for label_y in (_PLOT_T + 10, _PLOT_T + plot_h):
        price = high - (label_y - 4 - _PLOT_T) * span / plot_h  # price at the label's mid-height
        # a level label occupies [used - 24, used] (name above its price); an axis label [y - 10, y]
        if all(not -26 < label_y - used < 12 for used in gutter):
            parts.append(f'<text x="{right + 4}" y="{label_y:.1f}" fill="#7f8b9d" '
                         f'font-size="{_FS}">{_num(Decimal(repr(price)), _price_places(price))}</text>')

    labels: list[tuple[float, float, str]] = []
    for point, index in placed:
        text, color = _MARKER_STYLE.get(point["kind"], (point["kind"][:4], "#9aa6b8"))
        px, py = x(index), y(point["price"])
        shape = _marker_shape(point["kind"], px, py, point["side"], direction, color)
        exec_attr = f' data-exec-id="{_e(point["id"])}"' if point["id"] else ""
        parts.append(f'<g class="mk" data-kind="{_e(point["kind"])}"{exec_attr}>{shape}</g>')
        if any(other == text and abs(px - ox) < 26 for ox, _, other in labels):
            continue
        below = point["kind"] in ("entry", "add") and (
            point["side"].lower() == "buy" if point["side"] else direction != "short")
        ty = py + 19 if below else py - 9
        for _ in range(3):
            if not any(abs(px - ox) < 24 and abs(ty - oy) < 11 for ox, oy, _ in labels):
                break
            ty += 12 if below else -12
        ty = min(max(ty, _PLOT_T + 10), _PLOT_T + plot_h - 2)
        labels.append((px, ty, text))
        anchor = "end" if px > right - 16 else "start" if px < _PLOT_L + 16 else "middle"
        parts.append(f'<text x="{px:.1f}" y="{ty:.1f}" text-anchor="{anchor}" fill="{color}" '
                     f'font-size="{_FS_BOLD}" font-weight="600" stroke="#0e151f" stroke-width="2.4" '
                     f'paint-order="stroke">{_e(text)}</text>')

    parts.append(f'<text x="{_PLOT_L + 2}" y="12" fill="#7f8b9d" font-size="{_FS}">{_e(title)}</text>')
    axis_y = _CHART_H - 9
    parts.append(f'<text x="{_PLOT_L}" y="{axis_y}" fill="#7f8b9d" font-size="{_FS}">'
                 f'{_clock(shown_times[0], True, tz)}</text>')
    parts.append(f'<text x="{right}" y="{axis_y}" text-anchor="end" fill="#7f8b9d" '
                 f'font-size="{_FS}">{_clock(shown_times[-1], True, tz)}</text>')
    parts.append("</svg>")
    return "".join(parts), note


def _price_places(price: float) -> int:
    magnitude = abs(price)
    if magnitude >= 1000:
        return 1
    if magnitude >= 10:
        return 2
    if magnitude >= 1:
        return 3
    return 5


def _chart_svg(bars: Any, fills: Any, tf: str, *, exits: Any = (), planned_sl: Any = None,
               planned_tp: Any = None, direction: str = "", markers: Any = None,
               tz: tzinfo = BEIJING) -> str:
    """Candles + entry fills + exit legs by kind + planned stop/target + post-exit path."""
    return _chart(bars, fills, tf, exits=exits, planned_sl=planned_sl, planned_tp=planned_tp,
                  direction=direction, markers=markers, tz=tz)[0]


# --------------------------------------------------------------------------- HTML

_CSS = """
:root{--bg:#0b1018;--card:#131b27;--inset:#0e151f;--line:#263243;--text:#e9eef6;--muted:#8b97aa;
--ok:#2fd39a;--bad:#ff6b76;--warn:#f0b452;--manual:#ff9f43;--na:#8fa3c2}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.6 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1040px;margin:0 auto;padding:18px 12px 56px}
h1{font-size:24px;line-height:1.3;margin:2px 0 4px}
h2{font-size:15px;margin:0 0 8px}
h3{font-size:12px;font-weight:600;color:var(--muted);letter-spacing:.06em;margin:16px 0 6px}
p{margin:6px 0}
.eyebrow,.muted{color:var(--muted);font-size:13px}
.warning{background:#3a1c22;border:1px solid var(--bad);border-radius:12px;padding:10px 12px;margin:12px 0}
.warning b{display:block}
.warning ul{margin:4px 0 0;padding-left:18px;font-size:13px}
.panel,.trade{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
.top{display:grid;gap:12px;margin:14px 0}
.big{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.big b{font-size:44px;line-height:1.1;font-variant-numeric:tabular-nums}
.big span{font-size:17px}
.broken{font-weight:600;margin-top:8px}
.broken.bad{color:var(--bad)}.broken.ok{color:var(--ok)}.broken.warn{color:var(--warn)}
.notes{border:1px solid var(--line);border-radius:12px;padding:8px 12px;margin:12px 0;font-size:13px;
color:var(--muted)}
.notes b{color:var(--text);font-weight:600}
.notes ul{margin:4px 0 0;padding-left:18px}
.checks li.info,.checks li.soft{outline:0}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px;padding:0;list-style:none}
.chip{border:1px solid var(--line);border-radius:999px;padding:1px 10px;font-size:13px;white-space:nowrap}
.chip.ok{border-color:var(--ok);color:var(--ok)}.chip.bad{border-color:var(--bad);color:var(--bad)}
.chip.unk{border-color:var(--warn);color:var(--warn)}.chip.na{color:var(--na)}
.rules,.checks,.legs,.daylist{list-style:none;margin:0;padding:0}
.rules li,.daylist li{display:flex;justify-content:space-between;align-items:center;gap:10px;
padding:7px 0;border-top:1px solid var(--line)}
.rules li:first-child,.daylist li:first-child{border-top:0}
.counts{display:flex;gap:6px;white-space:nowrap;font-variant-numeric:tabular-nums;font-size:13px}
.counts span{min-width:38px;text-align:center;border-radius:6px;padding:0 6px;background:var(--inset)}
.pass,.c-pass{color:var(--ok)}.fail,.c-fail{color:var(--bad)}.unknown,.c-unknown{color:var(--warn)}
.na,.c-na,.c-info{color:var(--muted)}.c-soft{color:var(--warn)}
.counts .zero{color:#5c6779}
.daylist small{display:block;color:var(--muted)}
.daylist .st{white-space:nowrap;font-size:13px}
.trade{margin:14px 0}
.trade>header{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.who{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;min-width:0}
.who b{font-size:18px;overflow-wrap:anywhere}
.idx{color:var(--muted);font-variant-numeric:tabular-nums}
.badge{flex:0 1 auto;max-width:60%;overflow-wrap:anywhere;border-radius:8px;padding:3px 10px;font-weight:700;font-size:14px}
.badge.ok{background:#12372c;color:var(--ok)}.badge.bad{background:#451d24;color:var(--bad)}
.badge.unk{background:#3f3115;color:var(--warn)}.badge.na{background:#1e2a3c;color:var(--na)}
.say{margin:10px 0 0}
.say ul{margin:4px 0 0;padding-left:18px}
.rule{color:var(--muted);font-size:13px}
.checks li{display:grid;grid-template-columns:20px 1fr auto;gap:8px;align-items:start;
padding:8px 10px;background:var(--inset);border-radius:10px;margin-top:6px}
.checks i{font-style:normal;font-weight:700;text-align:center}
.checks b{font-weight:600;color:var(--text)}
.checks small{display:block;color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.checks em{font-style:normal;font-size:12px;white-space:nowrap}
.checks li.fail{outline:1px solid #6a2a33}
.plan{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin:0}
.plan div{background:var(--inset);border-radius:10px;padding:7px 10px;min-width:0}
.plan dt{color:var(--muted);font-size:12px}
.plan dd{margin:0;font-variant-numeric:tabular-nums;font-weight:600;overflow-wrap:anywhere}
.legs li{display:grid;grid-template-columns:64px 1fr auto;gap:8px;padding:6px 0;
border-top:1px solid var(--line);font-variant-numeric:tabular-nums}
.legs li:first-child{border-top:0}
.legs time{color:var(--muted)}
.legs .in{color:var(--muted)}
.k-stop_loss .kind{color:var(--bad)}.k-take_profit .kind{color:var(--ok)}
.k-manual .kind{color:var(--manual);font-weight:700}.k-liquidation .kind{color:var(--bad);font-weight:700}
.k-adl .kind{color:var(--na);font-weight:700}
.cost{border-left:3px solid var(--manual);background:#2a2015;border-radius:0 10px 10px 0;
padding:8px 10px;margin:12px 0 0}
.cost{font-size:14px}.cost b{display:block;color:var(--manual)}
.fit{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 0}
.fit .chip{white-space:normal;border-radius:14px;overflow-wrap:anywhere}
.fit .chip.flag{border-color:var(--warn);color:var(--warn)}
.charts{display:grid;gap:10px;margin-top:14px}
figure{margin:0;background:var(--inset);border-radius:12px;padding:8px 6px 6px}
figcaption{font-size:13px;color:var(--muted);padding:0 4px 4px}
figure svg{display:block;width:100%;height:auto}
.after-note{font-size:13px;color:var(--manual);margin:8px 2px 0}
.empty{color:var(--muted);padding:26px 8px;text-align:center}
.legend{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px;color:var(--muted);margin:8px 0 0}
.legend svg{width:14px;height:12px;vertical-align:-1px;margin-right:3px}
details{margin-top:10px;border-top:1px solid var(--line);padding-top:8px}
summary{cursor:pointer;color:var(--muted);font-size:13px}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
td,th{padding:6px 8px 6px 0;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
.result{color:var(--muted);font-size:13px;margin:12px 0 0;padding-top:10px;border-top:1px dashed var(--line)}
.ledger dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin:0}
.ledger dl div{background:var(--inset);border-radius:10px;padding:7px 10px}
.ledger dt{color:var(--muted);font-size:12px}
.ledger dd{margin:0;font-variant-numeric:tabular-nums;font-weight:600}
footer{color:var(--muted);font-size:12px;margin-top:18px;text-align:center}
@media(min-width:860px){
main{padding:28px 20px 72px}
h1{font-size:30px}
.top{grid-template-columns:minmax(0,5fr) minmax(0,7fr);align-items:start}
.trade{padding:20px}
.body{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:0 24px}
.charts{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
.plan{grid-template-columns:repeat(3,minmax(0,1fr))}
.ledger dl{grid-template-columns:repeat(4,minmax(0,1fr))}
}
"""

_LEGEND_ITEMS = (
    ("entry", '<path d="M7 2l-4.5 7h9z" fill="#f0b452"/>', "入场/加仓"),
    ("take_profit", '<path d="M7 1l5 5-5 5-5-5z" fill="#2fd39a"/>', "止盈触发"),
    ("stop_loss", '<circle cx="7" cy="6" r="4.4" fill="none" stroke="#ff6b76" stroke-width="1.5"/>'
                  '<path d="M4.6 3.6l4.8 4.8m0-4.8l-4.8 4.8" stroke="#ff6b76" stroke-width="1.5"/>',
     "止损触发"),
    ("manual", '<circle cx="7" cy="6" r="4" fill="#ff9f43" stroke="#fff" stroke-width="1.2"/>',
     "手动平仓"),
    ("liquidation", '<rect x="2.5" y="1.5" width="9" height="9" fill="#ff4d5e" stroke="#fff" '
                    'stroke-width="1.2"/>', "强平"),
    ("trailing_stop", '<path d="M7 1l5 5-5 5-5-5z" fill="none" stroke="#75a7ff" '
                      'stroke-width="1.5"/>', "移动止损触发"),
    ("adl", '<circle cx="7" cy="6" r="3.6" fill="#b58cff"/>', "ADL 减仓（交易所）"),
    ("unknown", '<circle cx="7" cy="6" r="3.6" fill="#9aa6b8"/>', "离场（方式未知）"),
    ("planned_sl", '<path d="M0 6h14" stroke="#ff6b76" stroke-width="1.4" stroke-dasharray="4 2"/>',
     "计划止损"),
    ("planned_tp", '<path d="M0 6h14" stroke="#2fd39a" stroke-width="1.4" stroke-dasharray="4 2"/>',
     "计划止盈"),
    ("after", '<rect x="1" y="1" width="12" height="10" fill="#ff9f43" fill-opacity=".3"/>',
     "手动平仓之后的走势"),
)


def _legend(trade: dict[str, Any]) -> str:
    present = {leg["kind"] for leg in trade["exits"]}
    if any(fill["action"] in ("entry", "add") for fill in trade["fills"]):
        present.add("entry")
    if trade["planned_sl"] is not None:
        present.add("planned_sl")
    if trade["planned_tp"] is not None:
        present.add("planned_tp")
    if "manual" in present and (trade["planned_sl"] is not None or trade["planned_tp"] is not None):
        present.add("after")
    items = "".join(f'<span><svg viewBox="0 0 14 12">{shape}</svg>{label}</span>'
                    for key, shape, label in _LEGEND_ITEMS if key in present)
    return f'<p class="legend">{items}</p>' if items else ""


_TIER_WORDS = {
    # Hard rules pass or fail; reference items only describe; day items name the lapse itself.
    "hard": _STATE_WORD,
    "reference": {"pass": "符合", "fail": "不符合", "unknown": "未知", "na": "不适用"},
    "day": {"pass": "没有违规", "fail": "违规", "unknown": "未知", "na": "不适用"},
}


def _check_display(item: dict[str, Any], tier: str) -> tuple[str, str, str]:
    """(css class, icon, state word) for a check shown under the given tier."""
    state = item["state"]
    if tier != "hard" and state == "na" and item["detail"]:
        return "info", "·", ""  # informational row: the detail is the content
    css = "soft" if tier == "reference" and state == "fail" else state
    return css, _STATE_ICON[state], _TIER_WORDS.get(tier, _STATE_WORD)[state]


def _check_items(checks: list[dict[str, Any]], tier: str = "hard") -> str:
    rows = []
    for item in checks:
        css, icon, word = _check_display(item, tier)
        rows.append(
            f'<li class="{css}"><i class="c-{css}">{icon}</i><div><b>{_e(item["label"])}</b>'
            + (f'<small>{_e(item["detail"])}</small>' if item["detail"] else "")
            + f'</div><em class="c-{css}">{word}</em></li>')
    return "".join(rows)


# 系统匹配只描述，不评判：没有绿色的「✓ 符合」。有/无类证据写「有 / 无」；
# 需要留意的（逆势、无序、在盘整区内、禁入、系统外）用提示色，其余一律中性。
_FIT_WORDS = {
    "pullback": ("已成笔", "未成笔"), "shift_retest": ("到位", "未到位"),
    "consolidation": ("在盘整区内", "不在盘整区内"),
}


def _fit_display(item: dict[str, Any]) -> tuple[str, str, str]:
    key, state = item["key"], item["state"]
    if state == "unknown":
        return "unknown", "?", "未知"
    if state == "na":
        return "info", "·", ""
    if key in ("alignment", "setup"):
        flagged = state == "fail"
        return ("soft" if flagged else "info"), ("!" if flagged else "·"), str(item["value"] or "")
    present, absent = _FIT_WORDS.get(key, ("有", "无"))
    flagged = key == "consolidation" and state == "pass"
    return ("soft" if flagged else "info"), ("!" if flagged else "·"), \
        present if state == "pass" else absent


def _fit_items(checks: list[dict[str, Any]]) -> str:
    rows = []
    for item in checks:
        css, icon, word = _fit_display(item)
        rows.append(
            f'<li class="{css}"><i class="c-{css}">{icon}</i><div><b>{_e(item["label"])}</b>'
            + (f'<small>{_e(item["detail"])}</small>' if item["detail"] else "")
            + f'</div><em class="c-{css}">{_e(word)}</em></li>')
    return "".join(rows)


def _plan_block(trade: dict[str, Any]) -> str:
    cells = [
        ("入场均价", _num(trade["entry_price"])),
        ("计划止损", _num(trade["planned_sl"]) if trade["planned_sl"] is not None else "无记录"),
        ("计划止盈", _num(trade["planned_tp"]) if trade["planned_tp"] is not None else "无记录"),
        ("计划盈亏比", _rr(trade["planned_rr"])),
        ("实际离场均价", _num(trade["exit_price"]) if trade["exits"] else "未离场"),
        ("实际结果（R）", _r(trade["realized_r"])),
    ]
    block = '<dl class="plan">' + "".join(
        f"<div><dt>{name}</dt><dd>{_e(value)}</dd></div>" for name, value in cells) + "</dl>"
    notes = []
    if trade["hold_seconds"] is not None:
        notes.append(f"持仓 {_duration(trade['hold_seconds'])}")
    manual = any(leg["kind"] == "manual" for leg in trade["exits"])
    if manual and trade["exit_price"] is not None and trade["planned_tp"] is not None:
        gap = abs(trade["planned_tp"] - trade["exit_price"])
        pct = gap / trade["exit_price"] * 100 if trade["exit_price"] else None
        notes.append(f"离场均价距计划止盈还差 {_num(gap)}"
                     + (f"（{_num(pct, 2)}%）" if pct is not None else ""))
    if notes:
        block += f'<p class="muted">{_e(" · ".join(notes))}</p>'
    return block


def _timeline(trade: dict[str, Any]) -> str:
    rows = []
    for fill in trade["fills"]:
        if fill["action"] in ("entry", "add"):
            rows.append((fill["time_ms"] or 0, "in", _ACTION_LABELS[fill["action"]],
                         fill["qty"], fill["price"], fill["time_ms"]))
    for leg in trade["exits"]:
        rows.append((leg["time_ms"] or 0, f"k-{leg['kind']}",
                     EXIT_LABELS.get(leg["kind"], leg["kind"]), leg["qty"], leg["price"],
                     leg["time_ms"]))
    if not rows:
        return '<p class="muted">没有成交记录</p>'
    rows.sort(key=lambda row: row[0])
    return '<ol class="legs">' + "".join(
        f'<li class="{_e(css)}"><time>{_clock(stamp, tz=trade["tz"])}</time>'
        f'<span class="kind">{_e(label)}</span>'
        f'<span>{_num(qty)} @ {_num(price)}</span></li>'
        for _, css, label, qty, price, stamp in rows) + "</ol>"


def _fit_block(trade: dict[str, Any]) -> str:
    fit = trade["fit"]
    if not fit:
        return ""
    trend = lambda key: _TREND_LABELS.get(str(fit.get(key) or "unknown"), str(fit.get(key)))
    alignment = str(fit.get("alignment") or "未知")
    setup = str(fit.get("setup") or "未知")
    trigger = str(fit.get("trigger") or "无")
    setup_flag = " flag" if ("禁入" in setup or "系统外" in setup) else ""
    align_flag = " flag" if alignment in ("逆势", "无序") else ""
    align_text = alignment
    if alignment in ("顺势", "逆势") and str(fit.get("trend_4h") or "unknown") not in ("up", "down"):
        # 方向不是 4h 给的：写在标签上，不藏在折叠的依据里
        align_text = f"{alignment}（按 1h；4h {trend('trend_4h')}）"
        align_flag = " flag"
    out = ('<h3>系统匹配（参考，不改变纪律结论）</h3><p class="fit">'
           f'<span class="chip">1h {_e(trend("trend_1h"))}</span>'
           f'<span class="chip">4h {_e(trend("trend_4h"))}</span>'
           f'<span class="chip{align_flag}">{_e(align_text)}</span>'
           f'<span class="chip">触发：{_e(trigger)}</span>'
           f'<span class="chip{setup_flag}">形态：{_e(setup)}</span></p>')
    if trade["fit_evidence"]:
        out += ('<details><summary>系统匹配依据</summary><ul class="checks">'
                + _fit_items(trade["fit_evidence"]) + "</ul></details>")
    return out


def _shared_aftermath(trade: dict[str, Any], bars_by_tf: dict[str, Any]) -> tuple[Any, str]:
    """Scan once what followed the first manual exit, preferring the finest bars."""
    stamps = [leg["time_ms"] for leg in trade["exits"]
              if leg["kind"] == "manual" and leg["time_ms"] is not None]
    if not stamps:
        return None, ""
    stop = float(trade["planned_sl"]) if trade["planned_sl"] is not None else None
    target = float(trade["planned_tp"]) if trade["planned_tp"] is not None else None
    scans = []
    for key, basis in (("1m", "1分钟"), ("15m", "15分钟")):
        rows = _norm_bars(bars_by_tf.get(key))
        result = _aftermath(rows, min(stamps), trade["direction"], stop, target) if rows else None
        if result:
            scans.append((result, basis))
    touched = [scan for scan in scans if scan[0]["outcome"] in ("target", "stop", "both")]
    if touched:
        return touched[0]
    unresolved = [scan for scan in scans if scan[0]["outcome"] == "none"]
    if unresolved:
        return max(unresolved, key=lambda scan: scan[0]["time"])
    return scans[0] if scans else (None, "")


def _charts_block(trade: dict[str, Any]) -> str:
    sources = {}
    for key in ("15m", "1m"):
        chart = trade["charts"].get(key)
        sources[key] = ((chart.get("bars"), chart.get("markers")) if isinstance(chart, Mapping)
                        else (chart, None))
    after, basis = _shared_aftermath(trade, {key: bars for key, (bars, _) in sources.items()})
    figures = []
    for key, caption in (("15m", "15分钟 · 环境"), ("1m", "1分钟 · 执行")):
        bars, markers = sources[key]
        svg, _ = _chart(bars, trade["fills"], key, exits=trade["exits"],
                        planned_sl=trade["planned_sl"], planned_tp=trade["planned_tp"],
                        direction=trade["direction"], markers=markers, after=after,
                        tz=trade["tz"])
        figures.append(f'<figure><figcaption>{caption}</figcaption>{svg}</figure>')
    note = ""
    if after and not trade["cost"]:
        # The cost line (from the principles layer) already tells this story when present.
        text = _aftermath_note(after, trade["planned_sl"], trade["planned_tp"], basis, trade["tz"])
        note = f'<p class="after-note">{_e(text)}</p>'
    return f'<div class="charts">{"".join(figures)}</div>{note}{_legend(trade)}'


def _trade_card(trade: dict[str, Any]) -> str:
    narrative = trade["narrative"] or {}
    reasons = narrative.get("reasons") or []
    say = ""
    # The deterministic reasons are the check rows shown right below, so only an
    # AI-worded narrative is spelled out; otherwise the card just quotes the rule.
    if narrative.get("source") != "deterministic" and narrative.get("summary"):
        say = f'<div class="say">{_e(narrative["summary"])}'
        if reasons:
            say += "<ul>" + "".join(f"<li>{_e(reason)}</li>" for reason in reasons) + "</ul>"
        say += "</div>"
    if narrative.get("next_action"):
        say += f'<p class="rule">{_e(narrative["next_action"])}</p>'
    hard = (f'<ul class="checks">{_check_items(trade["hard"])}</ul>' if trade["hard"]
            else '<p class="muted">没有硬规矩检查结果</p>')
    cost = ""
    if trade["cost"]:
        text = trade["cost"]["detail"] or trade["cost"]["value"]
        cost = f'<p class="cost"><b>{_COST_HEADING}</b>{_e(text)}</p>'
    other = ""
    if trade["other"]:
        other = ('<details><summary>其他检查项（参考）</summary><ul class="checks">'
                 + _check_items(trade["other"], "reference") + "</ul></details>")
    fills = "".join(
        f'<tr data-exec-id="{_e(fill["exec_id"])}"><td>{_clock(fill["time_ms"], tz=trade["tz"])}</td>'
        f'<td>{_e(_ACTION_LABELS.get(fill["action"], fill["action"]))}</td>'
        f'<td>{_num(fill["price"])}</td><td>{_num(fill["qty"])}</td>'
        f'<td>{_e(fill["exec_id"])}</td></tr>' for fill in trade["fills"])
    fills_block = ""
    if fills:
        fills_block = ('<details><summary>全部成交明细</summary><div class="scroll"><table><thead>'
                       '<tr><th>时间</th><th>动作</th><th>价格</th><th>数量</th><th>成交ID</th></tr>'
                       f'</thead><tbody>{fills}</tbody></table></div></details>')
    result_bits = []
    if trade["net_pnl"] is not None and trade.get("show_money", True):
        result_bits.append(f"本笔已结盈亏 {_money(trade['net_pnl'])}")
    if trade["price_return_pct"] is not None:
        # 已按方向折算：空单价格下跌记为正
        result_bits.append(f"按方向计的价格收益 {trade['price_return_pct']:+.2f}%")
    result = ""
    if result_bits:
        result = f'<p class="result">结果（不参与纪律判定）：{_e(" · ".join(result_bits))}</p>'
    return (
        f'<article class="trade v-{trade["verdict_class"]}" id="trade-{trade["number"]}" '
        f'data-episode-id="{_e(trade["episode_id"])}">'
        f'<header><div class="who"><span class="idx">#{trade["number"]}</span>'
        f'<b>{_e(trade["symbol"])}</b><span>{_e(_direction_word(trade["direction"]))}</span></div>'
        f'<span class="badge {trade["verdict_class"]}">{_e(trade["verdict"])}</span></header>'
        f'{say}<div class="body"><section><h3>硬规矩</h3>{hard}{cost}</section>'
        f'<section><h3>计划与实际</h3>{_plan_block(trade)}'
        f'<h3>成交时间线</h3>{_timeline(trade)}</section></div>'
        f'{_fit_block(trade)}{_charts_block(trade)}{other}{fills_block}{result}</article>'
    )


def _top_block(view: dict[str, Any]) -> str:
    score, trades = view["score"], view["trades"]
    if not trades:
        score_html = ('<div class="big"><b>今天没有成交</b></div>'
                      f'<p class="muted">{_e(_empty_day_text(view))}。</p>')
    else:
        broken_text, broken_class = headline(view)
        chips = "".join(
            f'<li class="chip {_VERDICT_CLASS.get(name, "unk")}">{_e(name)} {count}</li>'
            for name, count in sorted(
                score["by_verdict"].items(),
                key=lambda item: (VERDICT_ORDER.index(item[0]) if item[0] in VERDICT_ORDER
                                  else len(VERDICT_ORDER)))
            if count)
        heading = "" if view["reconciled"] else '<p class="muted">初步结果 · 未核准</p>'
        score_html = (
            f'{heading}<div class="big"><b>{score["compliant"]}</b>'
            f'<span>/ {score["total"]} 笔合规</span></div>'
            f'<p class="muted">参与评价 {score["judged"]} 笔</p>'
            f'<p class="broken {broken_class}">{_e(broken_text)}</p>'
            f'<ul class="chips">{chips}</ul>')

    def count_cell(state: str, value: int) -> str:
        zero = " zero" if not value else ""
        return f'<span class="c-{state}{zero}">{_STATE_ICON[state]} {value}</span>'

    rules = "".join(
        f'<li><span>{_e(rule["label"])}</span><span class="counts">'
        f'{count_cell("pass", rule["pass"])}{count_cell("fail", rule["fail"])}'
        f'{count_cell("unknown", rule["unknown"])}</span></li>' for rule in view["rules"])
    rules_html = (f'<ul class="rules">{rules}</ul>' if rules
                  else '<p class="muted">今天没有逐笔规矩检查结果。</p>')
    day = ""
    if view["day_checks"]:
        rows = []
        for check in view["day_checks"]:
            css, icon, word = _check_display(check, "day")
            rows.append(
                f'<li><span>{_e(check["label"])}'
                + (f'<small>{_e(check["detail"])}</small>' if check["detail"] else "")
                + "</span>"
                + (f'<span class="st c-{css}">{icon} {word}</span>' if word else "")
                + "</li>")
        day = '<h3>当日层面</h3><ul class="daylist">' + "".join(rows) + "</ul>"
    return (f'<div class="top"><section class="panel score">{score_html}</section>'
            f'<section class="panel"><h2>规矩清单</h2>'
            f'<p class="muted">每条规矩今天几笔通过 ✓、几笔没过 ✗、几笔记录不足 ?</p>'
            f'{rules_html}{day}</section></div>')


def render_html(report: Mapping) -> str:
    """Self-contained dark page (inline CSS and SVG, no scripts, no external requests)."""
    view = day_view(report)
    warning = ""
    if not view["reconciled"]:
        warning = ('<div class="warning"><b>⚠ 数据尚未通过对账，以下内容仅供排查，不是已核准日报。</b>'
                   "<ul>" + "".join(f"<li>{_e(reason)}</li>" for reason in view["reasons"])
                   + "</ul></div>")
    if view["reconciled"]:
        cells = (("当日净盈亏", _money(view["net_pnl"])), ("已结盈亏", _money(view["closed_pnl"])),
                 ("手续费", _fee(view["fees"])), ("资金费", _money(view["funding"])))
        ledger = ('<section class="panel ledger"><h2>账本</h2>'
                  '<p class="muted">来自已对账的账户流水。盈亏不参与纪律判定。</p><dl>'
                  + "".join(f"<div><dt>{name}</dt><dd>{value}</dd></div>" for name, value in cells)
                  + "</dl></section>")
    else:
        # 与 Telegram 摘要、摘要图同一口径：没对上账就不给任何金额（逐笔的已结盈亏也不显示）。
        ledger = ('<section class="panel ledger"><h2>账本</h2>'
                  '<p class="muted">账本未通过对账，不给数字。盈亏不参与纪律判定。</p></section>')
    notes = ""
    if view["notes"]:
        notes = ('<div class="notes"><b>数据说明</b><ul>'
                 + "".join(f"<li>{_e(note)}</li>" for note in view["notes"]) + "</ul></div>")
    scope = f' · 范围：{_e(view["scope"])}' if view["scope"] else ""
    cards = "".join(_trade_card(trade) for trade in view["trades"])
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>每日实盘复盘 · {_e(view["date"])}</title><style>{_CSS}</style></head><body><main>'
        f'<header><p class="eyebrow">每日实盘复盘 · {_e(view["window_label"] or view["date"])}'
        f'{scope}</p>'
        '<h1>今天我有没有按规矩做？</h1>'
        '<p class="muted">只看一件事：每笔交易有没有按规矩进、按规矩出。盈亏放在最后，不参与判定。'
        f'图表与时间线为{_e(view["tz_name"])}。</p>'
        f'</header>{warning}{notes}{_top_block(view)}{cards}{ledger}'
        '<footer>离场只有两种：止盈触发，或止损触发。</footer></main></body></html>'
    )


# --------------------------------------------------------------------------- delivery

def _ok(reply: Any) -> bool:
    return isinstance(reply, Mapping) and bool(reply.get("ok"))


async def deliver_review(bot, report: dict, png_path: Path | None, html_path: Path,
                         headline: str = "", skip=frozenset()) -> None:
    """Send the summary (photo with caption, or text) and the full HTML document.

    ``headline`` (already Telegram-HTML) is put above the summary, e.g. "今日未结束".
    ``png_path`` may be None (no font able to draw Chinese on this host): text summary only.
    ``skip`` names parts that already arrived ("summary" / "document") and must not be re-sent.
    Raises ``DeliveryIncomplete`` (with the parts that did arrive) unless both parts arrived.
    """
    sent = set(skip)
    if "summary" not in sent:
        summary = telegram_summary(report)
        if headline:
            summary = f"{headline}\n{summary}"
        fits_caption = len(summary) <= _TELEGRAM_CAPTION_LIMIT
        caption = summary if fits_caption else summary.split("\n", 1)[0]
        photo_ok = False
        if png_path is not None and Path(png_path).is_file():
            photo_ok = _ok(await bot.send_photo(str(png_path), caption=caption))
        if photo_ok and fits_caption:
            sent.add("summary")
        elif _ok(await bot.notify(summary)):
            sent.add("summary")
    if "document" not in sent:
        if _ok(await bot.send_document(str(html_path), caption="完整复盘：逐笔规矩检查与图表")):
            sent.add("document")
    if "summary" in sent and "document" in sent:
        return
    if "summary" in sent:
        raise DeliveryIncomplete("Telegram 完整 HTML 复盘发送失败（摘要已送达）", sent)
    if "document" in sent:
        raise DeliveryIncomplete("Telegram 摘要（图片和文本）发送失败（HTML 附件已送达）", sent)
    raise DeliveryIncomplete("Telegram 图片、文本和文档发送均失败", sent)
