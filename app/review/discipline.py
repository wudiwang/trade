from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from statistics import median
from typing import Any

from .models import Kline, TradeEpisode


class EvidenceState(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Evidence:
    key: str
    label: str
    group: str
    state: EvidenceState
    value: Decimal | str | None = None
    detail: str = ""


@dataclass(frozen=True)
class DisciplineReview:
    episode_id: str
    verdict: str
    evidence: dict[str, Evidence]


def _bearish_shift(bars: list[Kline], entry_ms: int) -> Evidence:
    past = [bar for bar in bars if bar.start_ms + 900_000 <= entry_ms]
    highs = [i for i in range(1, len(past) - 1)
             if past[i].high > past[i - 1].high and past[i].high >= past[i + 1].high]
    lows = [i for i in range(1, len(past) - 1)
            if past[i].low < past[i - 1].low and past[i].low <= past[i + 1].low]
    if len(highs) < 2 or len(lows) < 2:
        return Evidence("bearish_15m_shift", "15m趋势转换", "structure",
                        EvidenceState.UNKNOWN, detail="可确认的高低点不足")
    h1, h2 = highs[-2], highs[-1]
    l1, l2 = lows[-2], lows[-1]
    present = h1 < h2 < l2 and past[h2].high <= past[h1].high and past[l2].low < past[l1].low
    state = EvidenceState.PRESENT if present else EvidenceState.ABSENT
    detail = f"H1={past[h1].high}, H2={past[h2].high}, L1={past[l1].low}, L2={past[l2].low}"
    return Evidence("bearish_15m_shift", "15m趋势转换", "structure", state, detail=detail)


def _large_body(bars: list[Kline], entry_ms: int) -> Evidence:
    past = [bar for bar in bars if bar.start_ms + 60_000 <= entry_ms]
    if len(past) < 6:
        return Evidence("large_1m_body", "1m实体放大", "reference", EvidenceState.UNKNOWN)
    current = abs(past[-1].close - past[-1].open)
    base_values = [abs(bar.close - bar.open) for bar in past[-21:-1]]
    base = Decimal(str(median(base_values))) if base_values else Decimal("0")
    if base <= 0:
        return Evidence("large_1m_body", "1m实体放大", "reference", EvidenceState.UNKNOWN)
    ratio = current / base
    state = EvidenceState.PRESENT if ratio >= Decimal("3") else EvidenceState.ABSENT
    return Evidence("large_1m_body", "1m实体放大", "reference", state,
                    ratio.quantize(Decimal("0.01")), f"相对前序实体中位数 {ratio:.2f}x")


def evaluate_episode(
    episode: TradeEpisode,
    k1m: list[Kline],
    k15m: list[Kline],
    market_context: dict[str, Any],
    account_context: dict[str, Any],
) -> DisciplineReview:
    evidence: dict[str, Evidence] = {}
    rank = market_context.get("rank")
    if rank is None:
        rank_state = EvidenceState.UNKNOWN
        rank_detail = "历史涨跌幅榜未留存"
    else:
        rank_state = EvidenceState.PRESENT if int(rank) <= 20 else EvidenceState.ABSENT
        rank_detail = f"24h波动榜排名 {rank}"
    evidence["high_volatility_candidate"] = Evidence(
        "high_volatility_candidate", "高波动候选", "hard", rank_state, rank, rank_detail
    )

    risk = account_context.get("has_risk_boundary")
    risk_state = EvidenceState.UNKNOWN if risk is None else (
        EvidenceState.PRESENT if risk else EvidenceState.ABSENT
    )
    evidence["risk_boundary"] = Evidence(
        "risk_boundary", "风险边界/止损", "hard", risk_state,
        detail="存在明确风险边界" if risk else "未识别到风险边界",
    )

    equity_pct = account_context.get("position_equity_pct")
    light_limit = account_context.get("light_limit_pct")
    if equity_pct is None or light_limit is None:
        light_state = EvidenceState.UNKNOWN
    else:
        light_state = EvidenceState.PRESENT if Decimal(str(equity_pct)) <= Decimal(str(light_limit)) else EvidenceState.ABSENT
    evidence["light_position"] = Evidence(
        "light_position", "轻仓介入", "hard", light_state,
        Decimal(str(equity_pct)) if equity_pct is not None else None,
        "轻仓阈值尚未由用户拍板" if light_limit is None else f"阈值 {light_limit}%",
    )

    evidence["bearish_15m_shift"] = _bearish_shift(k15m, episode.opened_ms)
    evidence["large_1m_body"] = _large_body(k1m, episode.opened_ms)

    if episode.direction != "short":
        return DisciplineReview(episode.episode_id, "暂不适用", evidence)

    hard = [item for item in evidence.values() if item.group == "hard"]
    hard_absent = [item for item in hard if item.state is EvidenceState.ABSENT]
    hard_unknown = [item for item in hard if item.state is EvidenceState.UNKNOWN]
    structure = evidence["bearish_15m_shift"]
    if any(item.key == "risk_boundary" for item in hard_absent):
        verdict = "严重变形"
    elif len(hard_absent) >= 2:
        verdict = "冲动交易"
    elif hard_absent:
        verdict = "部分合规"
    elif hard_unknown or structure.state is EvidenceState.UNKNOWN:
        verdict = "证据不足"
    elif structure.state is EvidenceState.ABSENT:
        verdict = "部分合规"
    else:
        verdict = "合规"
    return DisciplineReview(episode.episode_id, verdict, evidence)
