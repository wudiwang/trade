from decimal import Decimal

from app.review.discipline import EvidenceState, evaluate_episode
from app.review.models import EpisodeFill, Execution, Kline, TradeEpisode
from app.review.narrator import build_prompt, parse_or_fallback


def bar(i, open_, high, low, close, interval=900_000):
    return Kline(i * interval, Decimal(str(open_)), Decimal(str(high)), Decimal(str(low)),
                 Decimal(str(close)), Decimal("100"), Decimal("10000"))


def short_episode(realized=Decimal("0")):
    execution = Execution("e", "o", "linear", "BTCUSDT", "Sell", Decimal("1"),
                          Decimal("100"), Decimal("100"), Decimal("0.05"), 10_000_000)
    episode = TradeEpisode("ep", "linear", "BTCUSDT", "short", 0, 10_000_000,
                           status="closed", realized_pnl=realized)
    episode.fills.append(EpisodeFill(execution, "entry"))
    return episode


def bearish_shift_bars():
    # Pivot highs 110 then 108; pivot lows 101 then 99 => lower-high + lower-low.
    return [
        bar(0, 100, 105, 99, 104), bar(1, 104, 110, 103, 108), bar(2, 108, 106, 101, 102),
        bar(3, 102, 108, 102, 107), bar(4, 107, 107, 99, 100), bar(5, 100, 103, 98, 99),
        bar(6, 99, 102, 99, 101),
    ]


def one_minute_large_body():
    rows = [bar(i, 100, 101, 99, 100.2, 60_000) for i in range(20)]
    rows.append(bar(20, 100, 106, 99.5, 105.5, 60_000))
    return rows


def test_15m_lower_high_then_lower_low_is_present():
    review = evaluate_episode(short_episode(), one_minute_large_body(), bearish_shift_bars(),
                              {"rank": 2}, {"has_risk_boundary": True})
    assert review.evidence["bearish_15m_shift"].state is EvidenceState.PRESENT


def test_1m_large_body_is_reference_not_hard_violation():
    review = evaluate_episode(short_episode(), one_minute_large_body(), bearish_shift_bars(),
                              {"rank": 2}, {"has_risk_boundary": True})
    factor = review.evidence["large_1m_body"]
    assert factor.group == "reference"
    assert factor.state is EvidenceState.PRESENT
    assert factor.value > Decimal("10")


def test_missing_historical_rank_is_unknown_not_absent():
    review = evaluate_episode(short_episode(), one_minute_large_body(), bearish_shift_bars(),
                              {}, {"has_risk_boundary": True})
    assert review.evidence["high_volatility_candidate"].state is EvidenceState.UNKNOWN


def test_profit_does_not_change_compliance_verdict():
    context = {"rank": 2}
    account = {"has_risk_boundary": False}
    win = evaluate_episode(short_episode(Decimal("50")), [], bearish_shift_bars(), context, account)
    loss = evaluate_episode(short_episode(Decimal("-50")), [], bearish_shift_bars(), context, account)
    assert win.verdict == loss.verdict == "严重变形"


def test_prompt_contains_evidence_but_no_secret_or_account_id():
    review = evaluate_episode(short_episode(), one_minute_large_body(), bearish_shift_bars(),
                              {"rank": 2}, {"has_risk_boundary": True})
    prompt = build_prompt(review)
    assert "15m趋势转换" in prompt
    assert "api" not in prompt.lower()
    assert "account" not in prompt.lower()


def test_invalid_ai_json_falls_back_to_deterministic_summary():
    review = evaluate_episode(short_episode(), [], bearish_shift_bars(),
                              {"rank": 2}, {"has_risk_boundary": False})
    narrative = parse_or_fallback("not json", review)
    assert narrative.source == "deterministic"
    assert narrative.summary
    assert narrative.next_action


def test_ai_cannot_override_verdict_or_money_fields():
    review = evaluate_episode(short_episode(), [], bearish_shift_bars(),
                              {"rank": 2}, {"has_risk_boundary": False})
    raw = '{"summary":"ok","reasons":["x"],"next_action":"y","confidence":"high",' \
          '"verdict":"合规","pnl":999999}'
    narrative = parse_or_fallback(raw, review)
    assert narrative.source == "ai"
    assert not hasattr(narrative, "verdict")
    assert not hasattr(narrative, "pnl")
