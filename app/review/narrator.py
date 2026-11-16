from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from .discipline import DisciplineReview, EvidenceState


@dataclass(frozen=True)
class Narrative:
    summary: str
    reasons: tuple[str, ...]
    next_action: str
    confidence: str
    source: str


def build_prompt(review: DisciplineReview) -> str:
    evidence = [
        {
            "label": item.label,
            "group": item.group,
            "state": item.state.value,
            "value": str(item.value) if item.value is not None else None,
            "detail": item.detail,
        }
        for item in review.evidence.values()
    ]
    payload = json.dumps({"fixed_verdict": review.verdict, "evidence": evidence}, ensure_ascii=False)
    return (
        "你是交易纪律复盘助手。只能依据下面证据，不得改动固定结论，不得把盈利等同于正确。"
        "输出严格 JSON，字段仅限 summary、reasons(最多3条)、next_action、confidence；"
        "每条 reason 必须以证据里的原始 label 开头。\n"
        + payload
    )


def deterministic_narrative(review: DisciplineReview) -> Narrative:
    absent = [item for item in review.evidence.values() if item.state is EvidenceState.ABSENT]
    present = [item for item in review.evidence.values() if item.state is EvidenceState.PRESENT]
    reasons = [f"{item.label}：{item.detail or '未满足'}" for item in absent[:3]]
    if not reasons:
        reasons = [f"{item.label}已有证据" for item in present[:3]] or ["证据不足，暂不下结论"]
    if any(item.key == "risk_boundary" for item in absent):
        next_action = "下一笔下单前先写明失效价和最大可承受亏损。"
    elif absent:
        next_action = f"下一笔重点等待“{absent[0].label}”出现后再介入。"
    else:
        next_action = "继续按同一套纪律执行，并补记尚为未知的证据。"
    return Narrative(
        summary=f"本笔纪律结论为“{review.verdict}”。",
        reasons=tuple(reasons),
        next_action=next_action,
        confidence="medium" if absent or present else "low",
        source="deterministic",
    )


def parse_or_fallback(raw: str, review: DisciplineReview) -> Narrative:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "summary", "reasons", "next_action", "confidence"
        }:
            raise ValueError("unexpected narrative fields")
        summary = str(value["summary"]).strip()
        reasons = tuple(str(item).strip() for item in value["reasons"][:3] if str(item).strip())
        next_action = str(value["next_action"]).strip()
        confidence = str(value["confidence"])
        if not summary or not reasons or not next_action or confidence not in {"low", "medium", "high"}:
            raise ValueError("invalid narrative")
        labels = tuple(item.label for item in review.evidence.values())
        if any(not reason.startswith(labels) for reason in reasons):
            raise ValueError("AI reason does not cite an evidence label")
        fixed_summary = f"本笔纪律结论为“{review.verdict}”。"
        return Narrative(fixed_summary, reasons, next_action, confidence, "ai")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return deterministic_narrative(review)


def generate_narrative(
    review: DisciplineReview,
    *,
    timeout_seconds: int = 120,
    executable: str | None = None,
    runner=subprocess.run,
) -> Narrative:
    """Ask a local AI CLI for prose while keeping verdict and money out of its control."""
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
