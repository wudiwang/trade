import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bybit_daily_review.py"
FIXTURE = ROOT / "tests" / "fixtures" / "bybit_review" / "sample.json"


def run(*args, env=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, text=True,
                          encoding="utf-8", capture_output=True, env=env or os.environ.copy())


def test_fixture_run_generates_idempotent_report(tmp_path):
    args = ("--fixture", str(FIXTURE), "--date", "2026-08-27", "--output-dir", str(tmp_path))
    first = run(*args)
    second = run(*args)
    assert first.returncode == second.returncode == 0
    manifest = json.loads((tmp_path / "2026-08-27" / "review.json").read_text(encoding="utf-8"))
    assert manifest["net_pnl"] == 9.25
    assert len(list((tmp_path / "2026-08-27").glob("report.html"))) == 1
    assert len(list((tmp_path / "2026-08-27").glob("summary.png"))) == 1


def test_live_mode_without_credentials_is_blocked(tmp_path):
    env = os.environ.copy()
    env.pop("BYBIT_API_KEY", None)
    env.pop("BYBIT_API_SECRET", None)
    result = run("--date", "2026-08-27", "--output-dir", str(tmp_path), env=env)
    assert result.returncode == 2
    assert "只读" in result.stderr


def test_send_rejects_unreconciled_fixture(tmp_path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["status"] = "mismatch"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = run("--fixture", str(bad), "--date", "2026-08-27", "--send-telegram",
                 "--output-dir", str(tmp_path / "out"))
    assert result.returncode == 3
    assert "对账" in result.stderr
