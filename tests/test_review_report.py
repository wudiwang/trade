import asyncio

from app.review.image import render_summary_png
from app.review.report import deliver_review, render_html, telegram_summary


def sample_report():
    return {
        "date": "2026-08-27",
        "window_label": "北京时间 2026-08-27 00:00–24:00",
        "status": "reconciled",
        "net_pnl": 12.34,
        "closed_pnl": 15.00,
        "fees": 2.00,
        "funding": -0.66,
        "account_return_pct": 0.82,
        "episodes": [{
            "episode_id": "ep-1",
            "symbol": "BTCUSDT",
            "direction": "short",
            "verdict": "部分合规",
            "net_pnl": 12.34,
            "price_return_pct": 1.2,
            "narrative": {"summary": "方向正确，但入场偏早。", "next_action": "等待15m结构确认。"},
            "evidence": [{"label": "15m趋势转换", "state": "present", "group": "structure"}],
            "fills": [
                {"exec_id": "entry-1", "action": "entry", "time": 1000, "price": 100, "qty": 2},
                {"exec_id": "exit-1", "action": "close", "time": 2000, "price": 98.8, "qty": 2},
            ],
            "k1m": [[0, 99, 101, 98, 100], [1000, 100, 101, 98, 99]],
            "k15m": [[0, 95, 102, 94, 100], [900000, 100, 101, 96, 97]],
        }],
        "verdict_counts": {"合规": 0, "部分合规": 1, "冲动交易": 0, "严重变形": 0},
        "tomorrow_action": "等待15m结构确认。",
    }


def test_report_separates_result_and_compliance():
    html = render_html(sample_report())
    assert "交易结果" in html
    assert "纪律复盘" in html
    assert "部分合规" in html


def test_report_contains_timeframes_and_every_execution_marker():
    html = render_html(sample_report())
    assert "1分钟" in html and "15分钟" in html
    assert "entry-1" in html and "exit-1" in html


def test_summary_image_is_valid_png(tmp_path):
    path = render_summary_png(sample_report(), tmp_path / "summary.png")
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_photo_failure_falls_back_to_text_and_document(tmp_path):
    class Bot:
        def __init__(self): self.calls = []
        async def send_photo(self, path, caption=""):
            self.calls.append("photo")
            return {"ok": False}
        async def notify(self, text): self.calls.append("text")
        async def send_document(self, path, caption=""):
            self.calls.append("document")
            return {"ok": True}

    html_path = tmp_path / "report.html"
    html_path.write_text(render_html(sample_report()), encoding="utf-8")
    png_path = render_summary_png(sample_report(), tmp_path / "summary.png")
    bot = Bot()
    asyncio.run(deliver_review(bot, sample_report(), png_path, html_path))
    assert bot.calls == ["photo", "text", "document"]


def test_telegram_summary_is_concise():
    text = telegram_summary(sample_report())
    assert "+12.34U" in text
    assert "部分合规 1" in text
