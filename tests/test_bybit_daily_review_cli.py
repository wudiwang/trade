"""命令行入口 scripts/bybit_daily_review.py：示例数据全链路、幂等、无密钥时的软失败。"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.bybit_daily_review import delivery_fingerprint


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bybit_daily_review.py"
FIXTURE = ROOT / "tests" / "fixtures" / "bybit_review" / "day_full.json"


def run(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, text=True,
                          encoding="utf-8", capture_output=True, env=env or os.environ.copy())


def without_bybit_keys():
    env = os.environ.copy()
    env.pop("BYBIT_API_KEY", None)
    env.pop("BYBIT_API_SECRET", None)
    return env


def count(db: Path, table: str) -> int:
    connection = sqlite3.connect(db)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


def test_fixture_run_produces_the_full_day_and_is_idempotent(tmp_path):
    args = ("--fixture", str(FIXTURE), "--output-dir", str(tmp_path))
    first = run(*args, env=without_bybit_keys())          # 示例数据不需要任何密钥
    target = tmp_path / "2026-11-16"
    before = (target / "review.json").read_bytes()
    second = run(*args, env=without_bybit_keys())
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert first.stderr == "" and second.stderr == ""

    summary = json.loads(second.stdout)
    assert summary["date"] == "2026-11-16" and summary["status"] == "reconciled"
    assert summary["trades"] == 4 and summary["compliant"] == 2
    assert summary["by_verdict"] == {"合规": 2, "中途下车": 1, "无止损": 1}

    assert sorted(path.name for path in target.iterdir()) == [
        "report.html", "review.json", "summary.png", "telegram.txt"]
    assert (target / "review.json").read_bytes() == before        # 同样的输入，同样的报告
    report = json.loads(before.decode("utf-8"))
    assert [row["principles"]["verdict"] for row in report["trades"]] == [
        "合规", "中途下车", "无止损", "合规"]
    assert any(check["key"] == "flip_after_exit" and check["state"] == "fail"
               for check in report["day_checks"])
    text = (target / "telegram.txt").read_text(encoding="utf-8")
    assert text.startswith("<b>2026-11-16 实盘纪律 · 4 笔，2 笔合规 · 当日层面 1 项违规</b>")
    html = (target / "report.html").read_text(encoding="utf-8")
    assert "离场之后（事后对照，不改变结论）" in html and "<script" not in html

    # 同一天跑两次：库里每张表仍然只有一份
    db = tmp_path / "fixture_review.db"
    assert count(db, "daily_trade_reviews") == 1
    assert count(db, "daily_review_pages") == 1
    assert count(db, "bybit_executions") == 8
    assert count(db, "bybit_order_observations") == 12
    assert count(db, "bybit_review_deliveries") == 0
    # 示例数据默认不写进真实复盘库
    assert "bybit_review.db" not in summary["json"]


def test_live_mode_without_credentials_says_what_to_do_and_nothing_else(tmp_path):
    result = run("--date", "2026-11-16", "--output-dir", str(tmp_path / "out"),
                 "--db", str(tmp_path / "review.db"), env=without_bybit_keys())
    assert result.returncode == 2
    assert result.stdout == ""
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1                                   # 一句话，没有堆栈
    assert "只读" in lines[0] and "BYBIT_API_KEY" in lines[0] and "BYBIT_API_SECRET" in lines[0]
    assert ".env" in lines[0] and "Traceback" not in result.stderr
    assert not (tmp_path / "out").exists() and not (tmp_path / "review.db").exists()


def test_send_telegram_without_credentials_never_sends(tmp_path):
    result = run("--today", "--send-telegram", "--output-dir", str(tmp_path / "out"),
                 "--db", str(tmp_path / "review.db"), env=without_bybit_keys())
    assert result.returncode == 2 and "只读" in result.stderr
    assert "Traceback" not in result.stderr and not (tmp_path / "out").exists()


def test_fixture_can_never_be_sent_to_real_telegram(tmp_path):
    result = run("--fixture", str(FIXTURE), "--send-telegram", "--output-dir", str(tmp_path / "out"))
    assert result.returncode == 4
    assert "fixture" in result.stderr and "Traceback" not in result.stderr
    assert not (tmp_path / "out").exists()


def test_bad_input_is_one_sentence_not_a_traceback(tmp_path):
    missing = run("--fixture", str(tmp_path / "nope.json"), "--output-dir", str(tmp_path / "out"))
    assert missing.returncode == 1 and "Traceback" not in missing.stderr
    assert len(missing.stderr.strip().splitlines()) == 1
    future = run("--fixture", str(FIXTURE), "--date", "2027-01-01", "--output-dir", str(tmp_path / "out"))
    assert future.returncode == 2 and "还没到" in future.stderr
    assert "Traceback" not in future.stderr


def test_delivery_idempotency_ignores_nondeterministic_ai_wording():
    trade = {"episode": {"episode_id": "ep", "fills": [{"exec_id": "e"}]},
             "principles": {"verdict": "合规"}, "narrative": {"summary": "A"}}
    left = {"date": "2026-11-16", "window_start_ms": 1, "window_end_ms": 2, "status": "reconciled",
            "ledger": {"net_pnl": "1", "fees": "0", "funding": "0"}, "trades": [trade]}
    right = json.loads(json.dumps(left, ensure_ascii=False))
    right["trades"][0]["narrative"]["summary"] = "B"
    assert delivery_fingerprint(left) == delivery_fingerprint(right)
    changed = json.loads(json.dumps(left, ensure_ascii=False))
    changed["trades"][0]["principles"]["verdict"] = "中途下车"
    assert delivery_fingerprint(left) != delivery_fingerprint(changed)
