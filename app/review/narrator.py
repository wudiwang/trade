"""Turn fixed review verdicts and their checks into short Chinese prose.

The deterministic narrative is the default and needs no external tool.  The
optional AI path may only rephrase the reasons: the verdict, the summary line
and every number stay under the control of the rule engine.

Accepted ``review`` inputs (duck-typed, so this module does not import the
principles layer):

* ``principles.PrincipleReview`` or ``dataclasses.asdict`` of it
  (``verdict`` + ``checks`` keyed by check key, states pass/fail/unknown/na);
* reports stored before 2026-11 by the retired discipline layer
  (``verdict`` + ``evidence``, states present/absent/unknown) still read.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Mapping

# Set to "1" to let generate_narrative() call a local AI CLI when one exists.
AI_ENV_FLAG = "REVIEW_NARRATOR_AI"

PRINCIPLE_VERDICTS = ("合规", "中途下车", "无止损", "止损被放宽", "逆向加仓",
                      "证据不足", "继承仓位不评价", "持仓中")
_LEGACY_VERDICTS = ("部分合规", "冲动交易", "严重变形", "暂不适用")

# One line per verdict that restates the owner's own rule (CLAUDE.md 第一准则).
# These are quotations of his rulebook, never trade advice.
VERDICT_RULES: dict[str, str] = {
    "合规": "对应准则：进场即设止损，锚定止盈位，只以止盈触发或止损触发离场。",
    "中途下车": "对应准则：只有止盈触发或止损触发两种离场方式，持仓期杂波不得作为平仓依据。",
    "无止损": "对应准则：进场即设止损，提前规划允许承受的最坏结果。",
    "止损被放宽": "对应准则：进场即设止损，触发立刻离场；最坏结果以进场时规划的为准。",
    "逆向加仓": "对应准则：进场时已规划允许承受的最坏结果；亏损方向加仓会改变它。",
    "证据不足": "未知项不按违规处理；补齐缺少的记录后可重新评价。",
    "继承仓位不评价": "入场不在本窗口；如需评价入场，请选择包含首次建仓的日期。",
    "持仓中": "仓位尚未离场，离场相关条目待平仓后评价。",
}

_STATE_ALIASES = {
    "pass": "pass", "fail": "fail", "unknown": "unknown", "na": "na",
    "present": "pass", "absent": "fail",
}
_STATE_WORDS = {"pass": "已满足", "fail": "未满足", "unknown": "记录不足，未知", "na": "不适用"}

# Words that would turn a description into a forecast or a trade call.
_FORBIDDEN_AI_WORDS = ("建议买", "建议卖", "建议做多", "建议做空", "看涨", "看跌", "将会上涨",
                       "将会下跌", "目标价", "预计", "预测", "必涨", "必跌")
_MAX_REASON_CHARS = 120
_MAX_ACTION_CHARS = 80


@dataclass(frozen=True)
class Narrative:
    summary: str
    reasons: tuple[str, ...]
    next_action: str
    confidence: str
    source: str


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _state(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "").strip().lower().rsplit(".", 1)[-1]
    return _STATE_ALIASES.get(raw, "unknown")


def review_facts(review: Any) -> tuple[str, list[dict[str, Any]]]:
    """Normalize any supported review object into (verdict, checks)."""
    verdict = str(_field(review, "verdict", "") or "证据不足")
    raw = _field(review, "checks")
    if raw is None:
        raw = _field(review, "evidence")
    if isinstance(raw, Mapping):
        rows = list(raw.values())
    elif isinstance(raw, (list, tuple)):
        rows = list(raw)
    else:
        rows = []
    checks = []
    for row in rows:
        label = str(_field(row, "label", "") or "").strip()
        if not label:
            continue
        value = _field(row, "value")
        checks.append({
            "key": str(_field(row, "key", "") or ""),
            "label": label,
            "tier": str(_field(row, "tier", None) or _field(row, "group", "") or ""),
            "state": _state(_field(row, "state")),
            "detail": str(_field(row, "detail", "") or "").strip(),
            "value": None if value is None else str(value),
        })
    return verdict, checks


def fixed_summary(verdict: str) -> str:
    return f"本笔纪律结论为“{verdict}”。"


def build_prompt(review: Any) -> str:
    verdict, checks = review_facts(review)
    payload = json.dumps({
        "fixed_verdict": verdict,
        "checks": [{
            "label": item["label"], "tier": item["tier"], "state": item["state"],
            "value": item["value"], "detail": item["detail"],
        } for item in checks],
    }, ensure_ascii=False)
    return (
        "你是交易纪律复盘的文字整理助手。结论已由规则固定为 fixed_verdict，"
        "不得修改、弱化或质疑它，也不得出现其他结论词。只能依据下面的检查项；"
        "盈亏不是证据，不得把盈利当作做对、把亏损当作做错。不得给出买卖建议或行情预测。"
        "输出严格 JSON，字段仅限 summary、reasons(最多3条)、next_action、confidence"
        "(low/medium/high)；每条 reason 必须以检查项的原始 label 开头；"
        "next_action 只能复述与未过条目对应的既有准则。\n"
        + payload
    )


def _reason(item: dict[str, Any]) -> str:
    return f"{item['label']}：{item['detail'] or _STATE_WORDS[item['state']]}"


def deterministic_narrative(review: Any) -> Narrative:
    verdict, checks = review_facts(review)
    hard = [item for item in checks if item["tier"] == "hard"]
    failed = [item for item in hard if item["state"] == "fail"]
    unknown = [item for item in hard if item["state"] == "unknown"]
    passed = [item for item in hard if item["state"] == "pass"]
    other_failed = [item for item in checks if item["tier"] != "hard" and item["state"] == "fail"]
    other_passed = [item for item in checks if item["tier"] != "hard" and item["state"] == "pass"]

    if verdict == "证据不足":
        ordered = unknown + failed
    elif verdict == "合规":
        ordered = passed
    else:
        ordered = failed + unknown
    if not ordered:
        ordered = failed + other_failed or unknown or passed or other_passed
    reasons = [_reason(item) for item in ordered[:3]] or ["没有可引用的检查项，暂不下结论"]

    if verdict in VERDICT_RULES:
        next_action = VERDICT_RULES[verdict]
    elif failed or other_failed:
        next_action = f"先对照未过的条目：{(failed or other_failed)[0]['label']}。"
    else:
        next_action = "本笔没有未过的硬规矩；未知项待补齐记录。"

    if not checks or verdict == "证据不足":
        confidence = "low"
    elif verdict in PRINCIPLE_VERDICTS and not unknown:
        confidence = "high"
    else:
        confidence = "medium"
    return Narrative(fixed_summary(verdict), tuple(reasons), next_action, confidence,
                     "deterministic")


def _strip_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
        if text[:4].lower() == "json":
            text = text[4:].strip()
    return text


def _mentions_other_verdict(text: str, verdict: str, known_strings: list[str]) -> bool:
    """True when free text names a verdict other than the fixed one."""
    scrubbed = text
    for known in sorted({verdict, *known_strings}, key=len, reverse=True):
        if known:
            scrubbed = scrubbed.replace(known, " ")
    return any(word in scrubbed for word in PRINCIPLE_VERDICTS + _LEGACY_VERDICTS
               if word != verdict)


def parse_or_fallback(raw: str, review: Any) -> Narrative:
    """Accept AI prose only when it stays inside the fixed verdict and checks."""
    verdict, checks = review_facts(review)
    try:
        value = json.loads(_strip_fence(raw))
        if not isinstance(value, dict) or set(value) != {
            "summary", "reasons", "next_action", "confidence"
        }:
            raise ValueError("unexpected narrative fields")
        if not isinstance(value["reasons"], list):
            raise ValueError("reasons must be a list")
        reasons = tuple(" ".join(str(item).split()) for item in value["reasons"][:3]
                        if str(item).strip())
        next_action = " ".join(str(value["next_action"]).split())
        confidence = str(value["confidence"])
        if (not str(value["summary"]).strip() or not reasons or not next_action
                or confidence not in {"low", "medium", "high"}):
            raise ValueError("invalid narrative")
        if (any(len(reason) > _MAX_REASON_CHARS for reason in reasons)
                or len(next_action) > _MAX_ACTION_CHARS):
            raise ValueError("narrative too long")
        labels = tuple(item["label"] for item in checks)
        if not labels or any(not reason.startswith(labels) for reason in reasons):
            raise ValueError("AI reason does not cite a check label")
        free_text = " ".join((*reasons, next_action))
        known = [item["label"] for item in checks] + [item["detail"] for item in checks]
        if _mentions_other_verdict(free_text, verdict, known):
            raise ValueError("AI text names a different verdict")
        if any(word in free_text for word in _FORBIDDEN_AI_WORDS):
            raise ValueError("AI text contains advice or a forecast")
        # The summary is never taken from the AI: the verdict line is fixed.
        return Narrative(fixed_summary(verdict), reasons, next_action, confidence, "ai")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return deterministic_narrative(review)


def generate_narrative(
    review: Any,
    *,
    use_ai: bool | None = None,
    timeout_seconds: int = 120,
    executable: str | None = None,
    runner=subprocess.run,
) -> Narrative:
    """Return the deterministic narrative unless the AI path is explicitly enabled.

    The AI path is opt-in: pass ``use_ai=True``, pass an ``executable``, or set
    the environment variable named by ``AI_ENV_FLAG`` to "1".  Any failure, a
    missing tool, or output that leaves the fixed verdict falls back to the
    deterministic narrative, so the report never depends on an AI tool.
    """
    if use_ai is None:
        use_ai = executable is not None or os.environ.get(AI_ENV_FLAG) == "1"
    if not use_ai:
        return deterministic_narrative(review)
    command = executable or shutil.which("claude")
    if not command:
        return deterministic_narrative(review)
    try:
        result = runner(
            [command, "-p", build_prompt(review)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            return deterministic_narrative(review)
        return parse_or_fallback(result.stdout, review)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return deterministic_narrative(review)
