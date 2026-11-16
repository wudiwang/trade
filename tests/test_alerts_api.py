"""价格提醒的操作面测试：Web API（/api/alerts*）的访问控制、入参校验与正常路径；Telegram 指令解析与回复文本。

运行: PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest tests/test_alerts_api.py -q
全程不触网：真实的 create_app(config.yaml + 临时 sqlite) + 按 CONTRACT A 写的假提醒引擎；
Telegram 侧测纯函数，以及把发消息的 api() 换成记录器之后的 _on_message。
假引擎上的下单相关属性全是「绊线」：谁碰谁断言失败。
"""
import asyncio
import json
import math
import re
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.bot.telegram as tg
import app.web.server as server
from app.bot.telegram import ALERT_USAGE, TgBot, alert_reply, kv_lines, parse_alert_command
from app.web.server import (ALERT_MAX_ACTIVE, ALERT_NOTE_MAX, alert_status_summary, fmt_alert_px, kv_text_lines,
                            norm_symbol, parse_price)
from app.config import Config
from app.db import DB

ALERT_PATHS = [("get", "/api/alerts"), ("post", "/api/alerts"), ("post", "/api/alerts/remove")]
AUTO_SUMMARY = "BTC · 5分钟跌≥1.5% · 15分钟跌≥2% · 破2日低 63,250.5 · 报价 0.4s前"


# ================================================================ 假对象
class Tripwire:
    """下单器的替身：任何属性访问都算越界。"""

    def __getattr__(self, name):
        raise AssertionError(f"提醒功能不该碰下单路径: trader.{name}")


class StubAlerts:
    """按 CONTRACT A 写的假提醒引擎（纯内存）。"""

    def __init__(self, prices=None):
        self.prices = {"BTCUSDT": 64000.0, "PUMPUSDT": 0.0027, **(prices or {})}
        self.rows: list[dict] = []
        self.next_id = 1
        self.calls: list[tuple] = []
        self.boom = False

    def _check(self):
        if self.boom:
            raise RuntimeError("stub exploded")

    def add_level(self, symbol, price, op=None, note=""):
        self.calls.append(("add", symbol, price, op, note))
        self._check()
        if op is None:
            last = self.prices.get(symbol)
            if last is None:
                return {"ok": False, "error": f"{symbol} 还没有行情，无法判断方向 <请写明跌破/涨破>"}
            op = "below" if price < last else "above"
        row = {"id": self.next_id, "symbol": symbol, "op": op, "price": float(price), "note": note,
               "status": "active", "created_at": 1763269500, "fired_at": None, "fired_price": None}
        self.next_id += 1
        self.rows.append(row)
        return {"ok": True, "alert": dict(row)}

    def list_levels(self, include_fired=False):
        self.calls.append(("list", include_fired))
        self._check()
        keep = ("active", "fired") if include_fired else ("active",)
        return [dict(r) for r in self.rows if r["status"] in keep]

    def remove_level(self, alert_id):
        self.calls.append(("remove", alert_id))
        self._check()
        for r in self.rows:
            if r["id"] == alert_id and r["status"] == "active":
                r["status"] = "removed"
                return True
        return False

    def last_price(self, symbol):
        return self.prices.get(symbol)

    def tick_symbols(self):
        return {"BTCUSDT"} | {r["symbol"] for r in self.rows if r["status"] == "active"}

    def status(self):
        """形状照抄 app/engine/alerts.py 目前的 status()（摘要行就是按这些键拼的）。"""
        self._check()
        return {
            "enabled": True, "symbols": ["BTCUSDT"], "tick_symbols": sorted(self.tick_symbols()), "base_tf": "5m",
            "thresholds": {
                "drop": {"enabled": True, "directions": ["down"],
                         "windows": [{"minutes": 5, "pct": 1.5, "cooldown_min": 30.0, "step_pct": 0.75},
                                     {"minutes": 15, "pct": 2.0, "cooldown_min": 60.0, "step_pct": 1.0}]},
                "range_break": {"enabled": True, "directions": ["down"], "days": [2], "cooldown_min": 240.0,
                                "buffer_pct": 0.0},
                "levels": {"max_active": 50}},
            "ref_lows": {"BTCUSDT": {"2d_low": {"price": 63250.5, "at": 1763200000, "at_bkk": "11-15 16:46",
                                                "tf": "15m", "fired": 0}}},
            "last_tick_age": {"BTCUSDT": 0.4},
            "last_price": {"BTCUSDT": 64000.0},
            "counts": {"levels_active": sum(r["status"] == "active" for r in self.rows), "ticks": 1234,
                       "fired": {"level": 0, "drop": 0, "range": 0}, "errors": 0, "unsaved": 0},
        }

    def mutations(self):
        return [c for c in self.calls if c[0] in ("add", "remove")]


class StubLinked:
    def __init__(self, state=None):
        self.state = state if state is not None else {"state": "等待 BTC 破位", "armed": False, "triggers_today": 0}
        self.boom = False

    def status(self):
        if self.boom:
            raise RuntimeError("linked exploded")
        return self.state


class StubEngine:
    """web 层与 bot 只会用到这些；下单相关的全是绊线。"""

    def __init__(self, alerts=None, linked=None):
        self.signal_subscribers = []
        self.trade_close_subscribers = []
        self.notice_subscribers = []
        self.alerts = alerts
        self.linked = linked
        self.trader = Tripwire()
        self.rest = Tripwire()

    def status(self):
        return {"mode": "paper", "symbols": 2, "uptime_s": 3725, "ws_conns": 1,
                "tracks": {"second_buy": {"closed": 3, "open": 1, "win_rate": 33.3, "total_pnl": -1.2,
                                          "expectancy_r": -0.1}}}

    async def _auto_execute(self, *a, **k):
        raise AssertionError("提醒功能不该触发自动下单")


class Env:
    def __init__(self, tmp_path, engine=None):
        self.cfg = Config()
        self.db = DB(tmp_path / "t.db")
        self.engine = engine
        self.app = server.create_app(self.cfg, self.db, engine=engine, bot=None)
        self.user = self.cfg.get("web.username", "admin")
        self.pwd = self.cfg.get("web.initial_password")

    def client(self, ip="10.0.0.1") -> TestClient:
        return TestClient(self.app, client=(ip, 50000))

    def login(self, ip="10.0.0.9") -> TestClient:
        c = self.client(ip)
        r = c.post("/api/login", json={"username": self.user, "password": self.pwd})
        assert r.status_code == 200
        return c


@pytest.fixture(autouse=True)
def _fast_fail(monkeypatch):
    monkeypatch.setattr(server, "LOGIN_FAIL_DELAY_S", 0)


@pytest.fixture
def alerts():
    return StubAlerts()


@pytest.fixture
def env(tmp_path, alerts):
    return Env(tmp_path, StubEngine(alerts, StubLinked()))


# ================================================================ 访问控制：三个接口全部要登录
def test_alert_paths_are_not_public():
    for path in ("/api/alerts", "/api/alerts/remove", "/api/alerts/"):
        assert path not in server.PUBLIC_GET and path not in server.PUBLIC_POST
        for method in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"):
            assert not server.anonymous_allowed(method, path), (method, path)
            assert not server.anonymous_allowed(method, path, has_macro_token=True), (method, path)


@pytest.mark.parametrize("method,path", ALERT_PATHS)
def test_anonymous_is_401(env, alerts, method, path):
    c = env.client()
    kw = {} if method == "get" else {"json": {"symbol": "btc", "price": 63000, "id": 1}}
    r = getattr(c, method)(path, **kw)
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    assert r.headers["cache-control"] == "no-store"
    assert alerts.calls == []                                  # 连读都没读到提醒引擎


def test_anonymous_variants_and_forged_cookie_are_401(env, alerts):
    alerts.add_level("BTCUSDT", 63000.0)
    alerts.calls.clear()
    c = env.client()
    for path in ("/api/alerts/", "/api/alerts?fired=1", "/api/alerts%0A", "/API/alerts",
                 "/api/alerts/remove", "/api/alerts/1", "/api%2Falerts"):
        assert c.get(path).status_code == 401, path
        assert c.post(path, json={"symbol": "btc", "price": 1, "id": 1}).status_code == 401, path
    for method in ("put", "patch", "delete"):
        assert getattr(c, method)("/api/alerts").status_code == 401
        assert getattr(c, method)("/api/alerts/remove").status_code == 401
    exp = int(time.time()) + 3600
    for tok in ("x", f"{env.user}|{exp}|" + "0" * 64, server.make_token(env.db, env.user, -1)):
        c.cookies.set("session", tok)
        assert c.get("/api/alerts").status_code == 401
        assert c.post("/api/alerts", json={"symbol": "btc", "price": 63000}).status_code == 401
        assert c.post("/api/alerts/remove", json={"id": 1}).status_code == 401
    token = env.login().get("/api/macro_token").json()["token"]           # 大盘观点的 token 不通用
    anon = env.client("10.0.0.2")
    assert anon.post(f"/api/alerts?token={token}", json={"symbol": "btc", "price": 63000}).status_code == 401
    assert anon.get(f"/api/alerts?token={token}").status_code == 401
    assert alerts.calls == [] and len(alerts.rows) == 1


def test_logout_drops_alert_access(env):
    c = env.login()
    assert c.get("/api/alerts").status_code == 200
    assert c.post("/api/logout").status_code == 200
    for method, path in ALERT_PATHS:
        assert getattr(c, method)(path).status_code == 401


def test_cross_origin_post_is_rejected(env, alerts):
    c = env.login()
    for origin in ("http://evil.example", "null"):
        r = c.post("/api/alerts", json={"symbol": "btc", "price": 63000}, headers={"Origin": origin})
        assert r.status_code == 403
        assert c.post("/api/alerts/remove", json={"id": 1}, headers={"Origin": origin}).status_code == 403
    assert alerts.calls == []
    r = c.post("/api/alerts", json={"symbol": "btc", "price": 63000}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


# ================================================================ 登录后：正常路径
def test_get_lists_levels_status_and_linked(env, alerts):
    c = env.login()
    d = c.get("/api/alerts").json()
    assert set(d) == {"enabled", "levels", "status", "status_summary", "status_lines", "linked_enabled", "linked",
                      "linked_summary", "linked_lines", "health", "health_summary", "errors", "note_max", "ts"}
    assert d["enabled"] is True and d["levels"] == [] and d["errors"] == []
    assert d["status"]["thresholds"]["drop"]["windows"][0] == {"minutes": 5, "pct": 1.5, "cooldown_min": 30.0,
                                                               "step_pct": 0.75}
    assert d["status"]["ref_lows"]["BTCUSDT"]["2d_low"]["price"] == 63250.5
    assert d["status_summary"] == AUTO_SUMMARY                 # 一行摘要：生效中的自动提醒
    assert ("thresholds.range_break: enabled=是, directions=[down], days=[2], cooldown_min=240, buffer_pct=0"
            in d["status_lines"])
    assert d["linked_enabled"] is True and d["linked"]["state"] == "等待 BTC 破位"
    assert d["linked_summary"] == "" and d["linked_lines"] == ["state: 等待 BTC 破位", "armed: 否", "triggers_today: 0"]
    assert d["note_max"] == ALERT_NOTE_MAX and abs(d["ts"] - time.time()) < 60

    alerts.add_level("BTCUSDT", 63000.0, None, "前低")
    alerts.add_level("PUMPUSDT", 0.00283, "above", "")
    alerts.rows[1]["status"] = "fired"
    alerts.rows[1]["fired_at"], alerts.rows[1]["fired_price"] = 1763270000, 0.002831
    d = c.get("/api/alerts").json()
    assert len(d["levels"]) == 1
    lv = d["levels"][0]
    assert lv == {"id": 1, "symbol": "BTCUSDT", "op": "below", "price": 63000.0, "note": "前低",
                  "status": "active", "created_at": 1763269500, "fired_at": None, "fired_price": None,
                  "last_price": 64000.0}
    d = c.get("/api/alerts?fired=1").json()                    # 可选：连同已触发的一起给
    assert [(x["id"], x["status"], x["last_price"]) for x in d["levels"]] == [(1, "active", 64000.0),
                                                                                (2, "fired", 0.0027)]
    assert c.get("/api/alerts?fired=maybe").status_code == 422
    assert c.get("/api/alerts").headers["cache-control"] == "no-store"


def test_add_normalises_symbol_and_infers_direction(env, alerts):
    c = env.login()
    r = c.post("/api/alerts", json={"symbol": " btc ", "price": 63000})
    assert r.status_code == 200, r.text
    a = r.json()["alert"]
    assert r.json()["ok"] is True
    assert (a["id"], a["symbol"], a["op"], a["price"], a["note"], a["status"]) == (1, "BTCUSDT", "below", 63000.0, "", "active")
    assert alerts.calls[-1] == ("add", "BTCUSDT", 63000.0, None, "")       # 方向留给引擎按现价判断

    # 写法与 Telegram 一致：pump → PUMPUSDT；"63k" / "63,000" / "0.00283" 都认；方向可明说
    r = c.post("/api/alerts", json={"symbol": "pump", "price": "0.00283", "op": "above", "note": "  前高\n回踩  "})
    assert r.status_code == 200 and r.json()["alert"]["symbol"] == "PUMPUSDT"
    assert alerts.calls[-1] == ("add", "PUMPUSDT", 0.00283, "above", "前高 回踩")
    for price, want in (("63k", 63000.0), ("63,000", 63000.0), (70000.5, 70000.5), ("6.3w", 63000.0)):
        r = c.post("/api/alerts", json={"symbol": "BTCUSDT", "price": price, "op": ""})
        assert r.status_code == 200 and r.json()["alert"]["price"] == want, price
    assert c.post("/api/alerts", json={"symbol": "btc", "price": 1, "op": "auto", "note": None}).status_code == 200
    assert alerts.calls[-1] == ("add", "BTCUSDT", 1.0, None, "")
    assert len(c.get("/api/alerts").json()["levels"]) == 7
    logs = [r["message"] for r in env.db.query("SELECT message FROM event_log WHERE source='alert'")]
    assert len(logs) == 7 and logs[0].startswith("web add #1 BTCUSDT below")


def test_add_reports_engine_refusal(env, alerts):
    c = env.login()
    r = c.post("/api/alerts", json={"symbol": "newcoin", "price": 1.5})    # 没行情 → 引擎说判断不了方向
    assert r.status_code == 400 and "无法判断方向" in r.json()["error"]
    assert alerts.rows == []
    assert c.post("/api/alerts", json={"symbol": "newcoin", "price": 1.5, "op": "below"}).status_code == 200


def test_add_is_capped(env, alerts):
    c = env.login()
    for i in range(ALERT_MAX_ACTIVE):
        alerts.add_level("BTCUSDT", 60000.0 + i, "below")
    alerts.calls.clear()
    r = c.post("/api/alerts", json={"symbol": "btc", "price": 50000})
    assert r.status_code == 400 and str(ALERT_MAX_ACTIVE) in r.json()["error"]
    assert alerts.mutations() == []
    assert c.post("/api/alerts/remove", json={"id": 1}).status_code == 200  # 删一条就又能加
    assert c.post("/api/alerts", json={"symbol": "btc", "price": 50000}).status_code == 200


def test_remove(env, alerts):
    c = env.login()
    alerts.add_level("BTCUSDT", 63000.0)
    alerts.add_level("BTCUSDT", 70000.0)
    r = c.post("/api/alerts/remove", json={"id": 1})
    assert r.status_code == 200 and r.json() == {"ok": True, "id": 1}
    assert [x["id"] for x in c.get("/api/alerts").json()["levels"]] == [2]
    assert c.post("/api/alerts/remove", json={"id": "2"}).status_code == 200      # 数字字符串也行
    r = c.post("/api/alerts/remove", json={"id": 2})                               # 已经删过
    assert r.status_code == 404 and r.json()["error"]
    assert c.post("/api/alerts/remove", json={"id": 999}).status_code == 404
    assert c.get("/api/alerts").json()["levels"] == []


# ================================================================ 入参校验
BAD_ADD_BODIES = [
    {}, {"price": 63000}, {"symbol": "", "price": 63000}, {"symbol": "   ", "price": 63000},
    {"symbol": None, "price": 63000}, {"symbol": 123, "price": 63000}, {"symbol": ["btc"], "price": 63000},
    {"symbol": "BTC USDT", "price": 63000}, {"symbol": "btc;drop", "price": 63000},
    {"symbol": "<script>", "price": 63000}, {"symbol": "../etc", "price": 63000},
    {"symbol": "A" * 33, "price": 63000}, {"symbol": "A" * 500, "price": 63000},
    {"symbol": "63000", "price": 63000}, {"symbol": "usdt", "price": 63000},
    {"symbol": "btc"}, {"symbol": "btc", "price": None}, {"symbol": "btc", "price": ""},
    {"symbol": "btc", "price": 0}, {"symbol": "btc", "price": -1}, {"symbol": "btc", "price": "-63000"},
    {"symbol": "btc", "price": "abc"}, {"symbol": "btc", "price": "1e5"}, {"symbol": "btc", "price": "nan"},
    {"symbol": "btc", "price": "inf"}, {"symbol": "btc", "price": True}, {"symbol": "btc", "price": [63000]},
    {"symbol": "btc", "price": {"v": 1}}, {"symbol": "btc", "price": 1e13}, {"symbol": "btc", "price": "9" * 40},
    {"symbol": "btc", "price": "63,00"}, {"symbol": "btc", "price": "0x10"},
    {"symbol": "btc", "price": 63000, "op": "sideways"}, {"symbol": "btc", "price": 63000, "op": 1},
    {"symbol": "btc", "price": 63000, "op": ["below"]}, {"symbol": "btc", "price": 63000, "op": "BELOW"},
    {"symbol": "btc", "price": 63000, "note": "x" * (ALERT_NOTE_MAX + 1)},
    {"symbol": "btc", "price": 63000, "note": 5}, {"symbol": "btc", "price": 63000, "note": ["a"]},
]


def test_add_rejects_bad_input(env, alerts):
    c = env.login()
    for body in BAD_ADD_BODIES:
        r = c.post("/api/alerts", json=body)
        assert r.status_code == 400, (body, r.text)
        assert isinstance(r.json()["error"], str) and r.json()["error"], body
    assert alerts.calls == []                                  # 校验没过就不碰提醒引擎


def test_add_rejects_malformed_or_oversized_body(env, alerts):
    c = env.login()
    hdr = {"Content-Type": "application/json"}
    assert c.post("/api/alerts", content=b"not json", headers=hdr).status_code == 400
    assert c.post("/api/alerts", json=["btc", 63000]).status_code == 400
    assert c.post("/api/alerts", json="btc 63000").status_code == 400
    for raw in (b'{"symbol":"btc","price":NaN}', b'{"symbol":"btc","price":Infinity}',
                b'{"symbol":"btc","price":-Infinity}', b'{"symbol":"btc","price":1e999}',
                b'{"symbol":"btc","price":' + b"9" * 400 + b"}"):
        assert c.post("/api/alerts", content=raw, headers=hdr).status_code == 400, raw[:40]
    big = {"symbol": "btc", "price": 63000, "note": "x" * (server.MAX_ALERT_BODY + 10)}
    assert c.post("/api/alerts", json=big).status_code == 400
    assert c.post("/api/alerts/remove", json={"id": 1, "pad": "x" * (server.MAX_ALERT_BODY + 10)}).status_code == 400
    assert alerts.calls == []
    # 边界上的合法值照常通过
    ok = {"symbol": "A" * 28, "price": "0.00000001", "note": "备" * ALERT_NOTE_MAX}
    r = c.post("/api/alerts", json={**ok, "op": "above"})
    assert r.status_code == 200 and r.json()["alert"]["symbol"] == "A" * 28 + "USDT"
    assert r.json()["alert"]["note"] == "备" * ALERT_NOTE_MAX


def test_remove_rejects_bad_id(env, alerts):
    alerts.add_level("BTCUSDT", 63000.0)
    alerts.calls.clear()
    c = env.login()
    for body in ({}, {"id": None}, {"id": 0}, {"id": -3}, {"id": 1.5}, {"id": True}, {"id": "abc"},
                 {"id": "1 OR 1=1"}, {"id": [1]}, {"id": 2**31}, {"id": "9" * 11}, {"id": ""}, {"id": {"$gt": 0}}):
        r = c.post("/api/alerts/remove", json=body)
        assert r.status_code == 400 and r.json()["error"], body
    assert alerts.calls == [] and alerts.rows[0]["status"] == "active"


def test_remove_rejects_non_object_body(env):
    c = env.login()
    assert c.post("/api/alerts/remove", content=b"3", headers={"Content-Type": "application/json"}).status_code == 400
    assert c.post("/api/alerts/remove", content=b"{", headers={"Content-Type": "application/json"}).status_code == 400


# ================================================================ 降级：引擎/提醒/联动 不在或出错
def test_no_engine_means_disabled(tmp_path):
    env = Env(tmp_path, engine=None)
    c = env.login()
    d = c.get("/api/alerts").json()
    assert d["enabled"] is False and d["levels"] == [] and d["status"] == {} and d["errors"] == []
    assert d["status_summary"] == "" and d["status_lines"] == []
    assert d["linked_enabled"] is False and d["linked"] == {} and d["linked_lines"] == []
    r = c.post("/api/alerts", json={"symbol": "btc", "price": 63000})
    assert r.status_code == 503 and r.json() == {"error": "价格提醒未启用"}
    assert c.post("/api/alerts/remove", json={"id": 1}).status_code == 503
    assert c.post("/api/alerts", json={"symbol": "btc", "price": -1}).status_code == 400   # 校验仍在前
    assert env.client().get("/api/alerts").status_code == 401                              # 没引擎也照样要登录


def test_engine_without_alerts_attribute(tmp_path):
    class Bare:                                                # 老引擎：压根没有 alerts / linked 属性
        signal_subscribers, trade_close_subscribers = [], []

        def status(self):
            return {}

    env = Env(tmp_path, engine=Bare())
    c = env.login()
    d = c.get("/api/alerts").json()
    assert d["enabled"] is False and d["linked_enabled"] is False
    assert c.post("/api/alerts", json={"symbol": "btc", "price": 63000}).status_code == 503

    sub = tmp_path / "second"
    sub.mkdir()
    env2 = Env(sub, engine=StubEngine(alerts=None, linked=StubLinked({"state": "armed"})))
    d = env2.login().get("/api/alerts").json()                 # 提醒关着、联动开着：各管各的
    assert d["enabled"] is False and d["linked_enabled"] is True and d["linked"] == {"state": "armed"}
    assert d["linked_lines"] == ["state: armed"]


def test_broken_alert_engine_never_500s_the_listing(env, alerts):
    c = env.login()
    alerts.add_level("BTCUSDT", 63000.0)
    alerts.boom = True
    env.engine.linked.boom = True
    r = c.get("/api/alerts")
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] is True and d["levels"] == [] and d["status"] == {} and d["linked"] == {}
    assert d["status_summary"] == "" and d["status_lines"] == [] and d["linked_lines"] == []
    assert sorted(d["errors"]) == ["levels", "linked", "status"]
    r = c.post("/api/alerts", json={"symbol": "btc", "price": 63000})
    assert r.status_code == 500 and r.json() == {"error": "提醒服务异常"}      # 不把异常原文带出去
    r = c.post("/api/alerts/remove", json={"id": 1})
    assert r.status_code == 500 and "stub exploded" not in r.text
    alerts.boom = False
    assert len(c.get("/api/alerts").json()["levels"]) == 1     # 恢复后照常


def test_status_payload_is_made_json_safe(env, alerts, monkeypatch):
    weird = {"nan": float("nan"), "inf": float("inf"), "set": {"b", "a"}, "tuple": (1, 2),
             "obj": object(), 7: "数字键", "long": "x" * 5000, "deep": {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}},
             "many": list(range(1000))}
    monkeypatch.setattr(alerts, "status", lambda: weird)
    env.engine.linked.state = ["不是字典", float("nan")]
    alerts.prices["BTCUSDT"] = float("nan")
    alerts.add_level("BTCUSDT", 63000.0, "below")
    r = env.login().get("/api/alerts")
    assert r.status_code == 200
    d = json.loads(r.text, parse_constant=lambda c: pytest.fail(f"响应里出现了非法 JSON 常量 {c}"))
    st = d["status"]
    assert st["nan"] is None and st["inf"] is None and st["set"] == ["a", "b"] and st["tuple"] == [1, 2]
    assert st["7"] == "数字键" and isinstance(st["obj"], str) and len(st["long"]) <= 501
    assert len(st["many"]) == 200 and isinstance(st["deep"]["a"]["b"]["c"]["d"], str)
    assert d["linked"] == {"status": ["不是字典", None]} and d["linked_lines"] == ["不是字典, –"]
    assert d["status_summary"] == "" and len(d["status_lines"]) == 9 and all(len(x) < 260 for x in d["status_lines"])
    assert d["levels"][0]["last_price"] is None and d["errors"] == []


# ================================================================ server 侧纯函数
def test_parse_alert_body_and_id_units():
    ok, why = server.parse_alert_body({"symbol": "$pump", "price": "0.00283", "op": "above", "note": "a\tb\x00c"})
    assert why == "" and ok == {"symbol": "PUMPUSDT", "price": 0.00283, "op": "above", "note": "a b c"}
    ok, why = server.parse_alert_body({"symbol": "币安人生", "price": 0.2})
    assert ok == {"symbol": "币安人生USDT", "price": 0.2, "op": None, "note": ""}
    bad, why = server.parse_alert_body({"symbol": "btc", "price": float("nan")})
    assert bad is None and "价格" in why
    bad, why = server.parse_alert_body({"symbol": "btc", "price": 10**400})
    assert bad is None and "价格" in why
    assert [server.parse_alert_id(x) for x in (1, "7", 2**31 - 1, 0, -1, 2**31, True, 1.0, "1.0", None, "٣")] == \
        [1, 7, 2**31 - 1, None, None, None, None, None, None, None, None]


def test_alerts_view_units():
    assert server.alerts_view(None)["enabled"] is False
    assert server.alerts_view(object())["enabled"] is False

    class Rowish(dict):
        pass

    class A(StubAlerts):
        def list_levels(self, include_fired=False):
            return (Rowish(id=i, symbol="BTCUSDT", op="below", price=1.0 + i) for i in range(500))   # 生成器 + 少键

    v = server.alerts_view(StubEngine(A(), None))
    assert len(v["levels"]) == server.ALERT_LEVELS_MAX and v["linked_enabled"] is False
    assert v["levels"][0]["note"] is None and v["levels"][0]["status"] is None and v["levels"][0]["last_price"] == 64000.0
    json.dumps(v, allow_nan=False)


# ================================================================ Telegram：指令解析（纯函数）
def add(symbol, price, op=None, note=""):
    return {"action": "add", "symbol": symbol, "price": price, "op": op, "note": note}


@pytest.mark.parametrize("text,want", [
    # 新增：方向留给引擎判断
    ("提醒 btc 63000", add("BTCUSDT", 63000.0)),
    ("/alert btc 63000", add("BTCUSDT", 63000.0)),
    ("/alert@MyTradeBot BTC 63000", add("BTCUSDT", 63000.0)),
    ("/ALERT btcusdt 63000", add("BTCUSDT", 63000.0)),
    ("alert btc 63000", add("BTCUSDT", 63000.0)),
    ("提醒 pump 0.00283", add("PUMPUSDT", 0.00283)),
    ("提醒 PUMPUSDT 0.00283", add("PUMPUSDT", 0.00283)),
    ("提醒btc 63000", add("BTCUSDT", 63000.0)),
    ("提醒：btc 63000", add("BTCUSDT", 63000.0)),
    ("  提醒   btc    63000  ", add("BTCUSDT", 63000.0)),
    ("提醒　ｂｔｃ　６３，０００", add("BTCUSDT", 63000.0)),         # 全角输入
    ("提醒 $btc $63,000", add("BTCUSDT", 63000.0)),
    ("提醒 btc/usdt 63k", add("BTCUSDT", 63000.0)),
    ("提醒 btc 6.3w", add("BTCUSDT", 63000.0)),
    ("提醒 btc 6.3万", add("BTCUSDT", 63000.0)),
    ("提醒 btc 63000u", add("BTCUSDT", 63000.0)),
    ("提醒 eth .5", add("ETHUSDT", 0.5)),
    ("提醒 1000pepe 0.012", add("1000PEPEUSDT", 0.012)),
    ("提醒 币安人生 0.2", add("币安人生USDT", 0.2)),
    # 备注：保持原文（大小写、全角标点都不动）
    ("提醒 btc 63000 前低", add("BTCUSDT", 63000.0, None, "前低")),
    ("/alert btc 63,000 Daily Low，放量 再看", add("BTCUSDT", 63000.0, None, "Daily Low，放量 再看")),
    ("提醒 btc 63000 " + "长" * 200, add("BTCUSDT", 63000.0, None, "长" * (ALERT_NOTE_MAX - 1) + "…")),
    # 明说方向
    ("提醒 btc 跌破 63000", add("BTCUSDT", 63000.0, "below")),
    ("提醒 btc 涨破 70k", add("BTCUSDT", 70000.0, "above")),
    ("提醒 btc below 63000 note", add("BTCUSDT", 63000.0, "below", "note")),
    ("提醒 btc ABOVE 70000", add("BTCUSDT", 70000.0, "above")),
    ("提醒 btc 跌破63000", add("BTCUSDT", 63000.0, "below")),
    ("提醒btc跌破63000", add("BTCUSDT", 63000.0, "below")),
    ("提醒 btc涨破 70000 前高", add("BTCUSDT", 70000.0, "above", "前高")),
    ("提醒 btc<63000", add("BTCUSDT", 63000.0, "below")),
    ("提醒 btc <= 63000", add("BTCUSDT", 63000.0, "below")),
    ("提醒 btc ＞70000", add("BTCUSDT", 70000.0, "above")),
    ("/alert pump above 0.00283 breakout", add("PUMPUSDT", 0.00283, "above", "breakout")),
    # 备注里出现方向词不改方向（方向只认价格之前的那个）
    ("提醒 btc 63000 跌破就看前低", add("BTCUSDT", 63000.0, None, "跌破就看前低")),
    # 列表
    ("提醒", {"action": "list"}), ("/alerts", {"action": "list"}), ("/alert", {"action": "list"}),
    ("/alerts@MyTradeBot", {"action": "list"}), ("提醒 列表", {"action": "list"}), ("/alert list", {"action": "list"}),
    ("ALERTS", {"action": "list"}),
    # 删除
    ("删除提醒 3", {"action": "remove", "id": 3}), ("删除提醒3", {"action": "remove", "id": 3}),
    ("删除提醒 #3", {"action": "remove", "id": 3}), ("取消提醒 12", {"action": "remove", "id": 12}),
    ("/alert del 3", {"action": "remove", "id": 3}), ("/alert DEL #3", {"action": "remove", "id": 3}),
    ("/alert delete 3", {"action": "remove", "id": 3}), ("提醒 删除 3", {"action": "remove", "id": 3}),
    ("删除提醒 ３", {"action": "remove", "id": 3}),
    # 以提醒关键词开头但没看懂 → 用法
    ("提醒 btc", {"action": "usage"}), ("提醒 63000", {"action": "usage"}), ("提醒 btc abc", {"action": "usage"}),
    ("提醒 btc 跌破", {"action": "usage"}), ("提醒 btc -5", {"action": "usage"}), ("提醒 btc 0", {"action": "usage"}),
    ("提醒 btc nan", {"action": "usage"}), ("提醒 btc inf", {"action": "usage"}), ("提醒 btc 1e5", {"action": "usage"}),
    ("提醒 btc 63,00", {"action": "usage"}), ("提醒 btc; 63000", {"action": "usage"}),
    ("提醒 <b>btc</b> 63000", {"action": "usage"}), ("提醒我明天开会", {"action": "usage"}),
    ("删除提醒", {"action": "usage"}), ("删除提醒 abc", {"action": "usage"}), ("删除提醒 3 4", {"action": "usage"}),
    ("/alert del", {"action": "usage"}), ("/alert del 0", {"action": "usage"}), ("/alert del -1", {"action": "usage"}),
    ("/alert " + "x" * 300 + " 1", {"action": "usage"}),
    # 与提醒无关 → None（保持沉默）
    ("", None), ("   ", None), ("hello", None), ("/status", None), ("状态", None), ("status", None),
    ("btc 63000", None), ("alertbtc 100", None), ("/alertx btc 1", None), ("alerting btc 1", None),
    ("请提醒 btc 63000", None), ("/start", None), ("63000", None),
])
def test_parse_alert_command(text, want):
    assert parse_alert_command(text) == want


def test_parse_alert_command_handles_none():
    assert parse_alert_command(None) is None


@pytest.mark.parametrize("raw,want", [
    ("63000", 63000.0), ("63,000", 63000.0), ("1,234,567.89", 1234567.89), ("63k", 63000.0), ("63K", 63000.0),
    ("0.07k", 70.0), ("6.3w", 63000.0), ("6.3万", 63000.0), ("0.00283", 0.00283), (".5", 0.5), ("$63000", 63000.0),
    ("63000u", 63000.0), ("63000USDT", 63000.0), ("１２３", 123.0), (" 42 ", 42.0), (5, 5.0), (0.1, 0.1),
    ("1000000000000", 1e12),
    ("", None), (None, None), (True, None), (False, None), ("0", None), ("0.0", None), ("-1", None), (-1, None),
    (0, None), ("abc", None), ("1e5", None), ("nan", None), ("inf", None), ("infinity", None), ("0x10", None),
    ("5.", None), ("1,00", None), ("1,2345", None), ("63 000", None), ("63000.5.1", None), ("k", None),
    ("u", None), ("1_000", None), ("+5", None), ("1000000000001", None), ("9" * 40, None),
    (float("nan"), None), (float("inf"), None), (10**400, None), ([1], None), ({"a": 1}, None),
])
def test_parse_price(raw, want):
    got = parse_price(raw)
    assert got == want and (got is None or (math.isfinite(got) and got > 0))


@pytest.mark.parametrize("raw,want", [
    ("btc", "BTCUSDT"), ("BTC", "BTCUSDT"), ("btcusdt", "BTCUSDT"), ("BtcUsdt", "BTCUSDT"), ("pump", "PUMPUSDT"),
    ("PUMPUSDT", "PUMPUSDT"), ("$eth", "ETHUSDT"), ("#sol", "SOLUSDT"), ("btc/usdt", "BTCUSDT"),
    ("BTC-USDT", "BTCUSDT"), (" doge ", "DOGEUSDT"), ("1000pepe", "1000PEPEUSDT"), ("币安人生", "币安人生USDT"),
    ("ｂｔｃ", "BTCUSDT"), ("A" * 28, "A" * 28 + "USDT"),
    ("", None), (None, None), ("usdt", None), ("123", None), ("0.5", None), ("a b", None), ("btc;", None),
    ("<b>", None), ("btc'--", None), ("A" * 29, None), ("x" * 200, None), ("../x", None), ("btc\nusdt", None),
])
def test_norm_symbol(raw, want):
    assert norm_symbol(raw) == want


def test_fmt_alert_px_and_kv_lines():
    assert [fmt_alert_px(x) for x in (63000, 64120.5, 100, 1234567.891, 99.5, 0.00283, 0.0000012345678)] == \
        ["63,000", "64,120.5", "100", "1,234,567.89", "99.5", "0.00283", "0.00000123457"]
    assert [fmt_alert_px(x) for x in (None, "x", float("nan"), 10**400)] == ["–", "–", "–", "–"]
    weird = {"enabled": True, "symbols": ["BTCUSDT", "PUMPUSDT"], "thr": {"drop": 1.5, "n": {"a": {"b": {"c": 1}}}},
             "age<s>": {"BTCUSDT": 0.4123456}, "ref": 63250.5, "none": None, "html": "<b>&",
             "nested": {"x": {"k": 1}, "y": {"k": [1, 2, {"z": True}]}}, "summary": "这行另有专门的位置"}
    assert kv_text_lines(weird) == [
        "enabled: 是", "symbols: BTCUSDT, PUMPUSDT", "thr: drop=1.5, n={a={b={…}}}", "age<s>: BTCUSDT=0.412346",
        "ref: 63250.5", "none: –", "html: <b>&",
        "nested.x: k=1", "nested.y: k=[1, 2, {z=是}]"]                    # 值全是字典的那层展开成 a.b
    assert kv_lines(weird)[3] == "age&lt;s&gt;: BTCUSDT=0.412346" and kv_lines(weird)[6] == "html: &lt;b&gt;&amp;"
    assert kv_lines(None) == [] and kv_lines("") == [] and kv_lines("idle <x>") == ["idle &lt;x&gt;"]
    many = kv_lines({f"k{i}": "v" * 1000 for i in range(40)})
    assert len(many) == 17 and all(len(x) < 260 for x in many) and "其余 24 项" in many[-1]


def test_alert_status_summary():
    st = StubAlerts().status()
    assert alert_status_summary(st) == AUTO_SUMMARY
    lines = kv_text_lines(st)
    assert lines[:4] == ["enabled: 是", "symbols: BTCUSDT", "tick_symbols: BTCUSDT", "base_tf: 5m"]
    assert lines[4] == ("thresholds.drop: enabled=是, directions=[down], windows=[{minutes=5, pct=1.5, cooldown_min=30, "
                        "step_pct=0.75}, {minutes=15, pct=2, cooldown_min=60, step_pct=1}]")
    assert lines[7] == "ref_lows.BTCUSDT: 2d_low={price=63250.5, at=1763200000, at_bkk=11-15 16:46, tf=15m, fired=0}"
    assert lines[-1] == "counts: levels_active=0, ticks=1234, fired={level=0, drop=0, range=0}, errors=0, unsaved=0"

    # 对方自己给了 summary → 原样用（压成一行）
    assert alert_status_summary({"summary": "  自己写的\n摘要  ", "thresholds": {}}) == "自己写的 摘要"
    # 双向、多币种、参照价没就绪、报价缺席、有内部异常
    st2 = {"enabled": True, "symbols": ["BTCUSDT", "PUMPUSDT"],
           "thresholds": {"drop": {"enabled": True, "directions": ["down", "up"], "windows": [{"minutes": 0.5, "pct": 0.8}]},
                          "range_break": {"enabled": True, "directions": ["down", "up"], "days": [2]}},
           "ref_lows": {"BTCUSDT": {"2d_low": {"price": 63250.5}, "2d_high": {"price": None}},
                        "PUMPUSDT": {"2d_low": {"price": 0.0026}}},
           "last_tick_age": {"BTCUSDT": 0.4, "PUMPUSDT": None}, "counts": {"errors": 3}}
    assert alert_status_summary(st2) == ("BTC/PUMP · 0.5分钟涨跌≥0.8% · 破2日低 BTC 63,250.5/PUMP 0.0026 · "
                                         "破2日高(参照价未就绪) · 部分币种尚无实时报价 · 内部异常 3 次")
    off = {"enabled": True, "symbols": ["BTCUSDT"],
           "thresholds": {"drop": {"enabled": False}, "range_break": {"enabled": False}}}
    assert alert_status_summary(off) == ""                      # 没有生效的自动规则 → 不硬编一句
    assert alert_status_summary({"enabled": False}) == "已关闭"
    cold = {**st, "ref_lows": {}, "last_tick_age": {}}          # 刚启动：参照价没算出来、一个报价都还没到
    assert alert_status_summary(cold) == "BTC · 5分钟跌≥1.5% · 15分钟跌≥2% · 破2日低(参照价未就绪) · 尚无实时报价"
    assert alert_status_summary({"enabled": False, "error": "status 读取失败"}) == ""   # 出错时让键值原样说话
    # 认不出的形状一律空串（调用方退回键值渲染），绝不抛
    for junk in (None, "x", 5, [], {}, {"thresholds": 3}, {"thresholds": {"drop": {"enabled": True, "windows": [1, 2]}}},
                 {"thresholds": {"drop": {"enabled": True, "windows": [{"minutes": "x", "pct": None}]}}},
                 {"thresholds": {"range_break": {"enabled": True, "days": ["two"]}}}):
        assert alert_status_summary(junk) == "", junk


# ================================================================ Telegram：回复文本
def test_alert_reply_add_list_remove():
    al, linked = StubAlerts(), StubLinked()
    msg = alert_reply(parse_alert_command("提醒 btc 63000 前低"), al, linked)
    assert msg == "🔔 已设提醒 <b>#1</b> BTCUSDT 跌破 <code>63,000</code> · 现价 64,000 (-1.56%) · 前低"
    msg = alert_reply(parse_alert_command("/alert pump 涨破 0.00283"), al, linked)
    assert msg == "🔔 已设提醒 <b>#2</b> PUMPUSDT 涨破 <code>0.00283</code> · 现价 0.0027 (+4.81%)"
    assert al.calls[-1] == ("add", "PUMPUSDT", 0.00283, "above", "")

    listing = alert_reply(parse_alert_command("提醒"), al, linked)
    lines = listing.split("\n")
    assert lines[0] == "🔔 <b>价格提醒</b> 2 条"
    assert lines[1] == "<b>#1</b> BTCUSDT 跌破 <code>63,000</code> · 现价 64,000 (-1.56%) · 前低"
    assert lines[2] == "<b>#2</b> PUMPUSDT 涨破 <code>0.00283</code> · 现价 0.0027 (+4.81%)"
    assert lines[3:] == ["", "⚙️ 自动提醒: " + AUTO_SUMMARY, "",
                         "<b>🔗 联动规则</b>", "state: 等待 BTC 破位", "armed: 否", "triggers_today: 0"]
    # 联动规则自己带 summary → 也只占一行；提醒引擎的 status 认不出形状 → 退回「键: 值」原样
    al2 = StubAlerts()
    al2.status = lambda: {"mode": "x<y", "limits": {"a": 1}}
    lines = alert_reply({"action": "list"}, al2, StubLinked({"summary": "BTC 破位后盯 <PUMP>", "armed": True})).split("\n")
    assert lines == ["🔔 <b>价格提醒</b>: 暂无", "", "<b>⚙️ 自动提醒</b>", "mode: x&lt;y", "limits: a=1", "",
                     "🔗 联动规则: BTC 破位后盯 &lt;PUMP&gt;"]

    assert alert_reply(parse_alert_command("删除提醒 1"), al, linked) == "🗑 已删除提醒 #1"
    assert alert_reply(parse_alert_command("/alert del 1"), al, linked) == "⚠️ 没有生效中的提醒 #1"
    assert alert_reply(parse_alert_command("/alert del 2"), al, linked) == "🗑 已删除提醒 #2"
    assert alert_reply({"action": "list"}, al, linked).split("\n")[0] == "🔔 <b>价格提醒</b>: 暂无"
    assert alert_reply({"action": "usage"}, al) == ALERT_USAGE and ALERT_USAGE.count("\n") == 1   # 两行用法
    assert "<" not in ALERT_USAGE and "&" not in ALERT_USAGE                                      # 用法文本本身不含 HTML


def test_alert_reply_escapes_dynamic_text():
    al = StubAlerts()
    msg = alert_reply(parse_alert_command("提醒 btc 63000 <b>x</b> & <a href='http://e'>y</a>"), al)
    assert "<a" not in msg and "&lt;b&gt;x&lt;/b&gt; &amp; &lt;a href=&#x27;http://e&#x27;&gt;y&lt;/a&gt;" in msg
    listing = alert_reply({"action": "list"}, al, StubLinked({"<i>k</i>": "<script>alert(1)</script>"}))
    assert "<script" not in listing and "<i>" not in listing and "&lt;script&gt;" in listing
    refused = alert_reply(parse_alert_command("提醒 newcoin 1.5"), al)      # 引擎给的原因里带尖括号
    assert refused.startswith("⚠️ 未添加: NEWCOINUSDT 还没有行情") and "<请" not in refused and "&lt;请" in refused


def test_alert_reply_when_disabled_or_broken():
    assert alert_reply({"action": "add", "symbol": "BTCUSDT", "price": 1.0, "op": None, "note": ""}, None) == tg.ALERT_DISABLED
    assert alert_reply({"action": "remove", "id": 1}, None) == tg.ALERT_DISABLED
    listing = alert_reply({"action": "list"}, None, None)
    assert listing.split("\n") == [tg.ALERT_DISABLED, "", "🔗 联动规则: 未启用"]
    al, linked = StubAlerts(), StubLinked()
    al.add_level("BTCUSDT", 63000.0)
    al.boom = linked.boom = True
    listing = alert_reply({"action": "list"}, al, linked)
    assert "⚠️ 提醒列表读取失败" in listing and "⚙️ 自动提醒: 状态读取失败" in listing
    assert "🔗 联动规则: 状态读取失败" in listing and "stub exploded" not in listing
    assert alert_reply({"action": "list"}, StubAlerts(), StubLinked({})).endswith("🔗 联动规则: 无")


def test_alert_reply_cap_and_long_listing():
    al = StubAlerts()
    for i in range(ALERT_MAX_ACTIVE):
        al.add_level("BTCUSDT", 60000.0 + i, "below", "备注" * 40)
    al.calls.clear()
    msg = alert_reply(parse_alert_command("提醒 btc 50000"), al)
    assert msg == f"⚠️ 未添加: 生效中的提醒已达上限 {ALERT_MAX_ACTIVE} 条" and al.mutations() == []
    listing = alert_reply({"action": "list"}, al, StubLinked())
    assert len(listing) <= 4096 and listing.count("<b>") == listing.count("</b>")   # 不超 Telegram 上限、标签成对
    assert listing.count("<code>") == listing.count("</code>")


# ================================================================ Telegram：_on_message 接线
class FakeCfg:
    tg_token = "TEST-TOKEN"
    tg_chat_id = "42"
    mode = "paper"

    def get(self, key, default=None):
        return default


def make_bot(engine):
    bot = TgBot(FakeCfg(), db=None, trader=Tripwire())
    bot.engine = engine
    sent: list[tuple] = []

    async def fake_api(method, **params):                      # 不触网：只记下要发什么
        sent.append((method, params))
        return {"ok": True, "result": {"message_id": 1}}

    bot.api = fake_api
    return bot, sent


def say(bot, text, chat_id=42):
    asyncio.run(bot._on_message({"chat": {"id": chat_id}, "text": text}))


def test_on_message_alert_commands_end_to_end():
    al = StubAlerts()
    bot, sent = make_bot(StubEngine(al, StubLinked()))
    say(bot, "提醒 BTC 63,000 Daily Low")
    assert len(sent) == 1 and sent[0][0] == "sendMessage"
    p = sent[0][1]
    assert p["chat_id"] == "42" and p["parse_mode"] == "HTML" and "reply_markup" not in p     # 纯文本，没有任何按钮
    assert p["text"] == "🔔 已设提醒 <b>#1</b> BTCUSDT 跌破 <code>63,000</code> · 现价 64,000 (-1.56%) · Daily Low"
    assert al.calls[-1] == ("add", "BTCUSDT", 63000.0, None, "Daily Low")                     # 备注大小写原样

    say(bot, "/alerts")
    assert sent[-1][1]["text"].startswith("🔔 <b>价格提醒</b> 1 条\n<b>#1</b> BTCUSDT 跌破")
    say(bot, "删除提醒 1")
    assert sent[-1][1]["text"] == "🗑 已删除提醒 #1" and al.rows[0]["status"] == "removed"
    say(bot, "提醒 btc")
    assert sent[-1][1]["text"] == ALERT_USAGE
    assert len(sent) == 4 and all(m == "sendMessage" and "reply_markup" not in q for m, q in sent)


def test_on_message_stays_silent_for_unrelated_text_and_other_chats():
    al = StubAlerts()
    bot, sent = make_bot(StubEngine(al, None))
    for text in ("hello", "btc 63000", "买入 btc", "", "/start", "请提醒我"):
        say(bot, text)
    asyncio.run(bot._on_message({"chat": {"id": 42}}))                        # 没有 text（贴纸/图片）
    asyncio.run(bot._on_message({"chat": {"id": 42}, "text": None}))
    assert sent == [] and al.calls == []
    say(bot, "提醒 btc 63000", chat_id=999)                                    # 别的聊天：一律不理
    say(bot, "/alerts", chat_id="-100123")
    asyncio.run(bot._on_message({"text": "提醒 btc 63000"}))
    assert sent == [] and al.calls == []


def test_on_message_status_still_works():
    bot, sent = make_bot(StubEngine(StubAlerts(), None))
    for text in ("/status", "STATUS", "状态"):
        say(bot, text)
    assert len(sent) == 3
    assert sent[0][1]["text"].split("\n") == ["⚙️ 运行 1h2m | 2币 | ws 1连接",
                                              "second_buy: 3平/1持 胜率33.3% pnl=-1.2U 期望-0.1R"]


def test_on_message_survives_engine_errors_and_send_failures(monkeypatch):
    al = StubAlerts()
    al.boom = True
    bot, sent = make_bot(StubEngine(al, None))
    say(bot, "提醒 btc 63000")
    assert sent[-1][1]["text"] == "⚠️ 提醒指令处理失败，稍后再试"

    async def broken_api(method, **params):
        raise RuntimeError("telegram down")

    bot.api = broken_api
    say(bot, "/alerts")                                                        # 发不出去也不抛到轮询循环里

    # 没挂 bot.engine 时沿用 web 层登记的引擎；两边都没有 → 明说未启用
    bot2, sent2 = make_bot(None)
    monkeypatch.setitem(server.APP_STATE, "engine", None)
    say(bot2, "提醒 btc 63000")
    assert sent2[-1][1]["text"] == tg.ALERT_DISABLED
    al2 = StubAlerts()
    monkeypatch.setitem(server.APP_STATE, "engine", StubEngine(al2, None))
    say(bot2, "提醒 btc 63000")
    assert sent2[-1][1]["text"].startswith("🔔 已设提醒 <b>#1</b> BTCUSDT") and len(al2.rows) == 1


def test_disabled_bot_sends_nothing():
    al = StubAlerts()
    bot, sent = make_bot(StubEngine(al, None))
    bot.enabled = False
    say(bot, "提醒 btc 63000")
    assert sent == []


# ================================================================ 控制台页面
def test_console_has_login_only_alert_card(env):
    c = env.client()
    page = c.get("/").text
    assert '<div class="panel auth-only" id="alerts-panel">' in page and "🔔 价格提醒" in page
    assert "body:not(.authed) .auth-only { display: none !important; }" in page             # 匿名看不到这张卡
    for el in ('id="al-list"', 'id="al-form"', 'id="al-symbol"', 'id="al-op"', 'id="al-price"', 'id="al-note-in"',
               'id="al-info"'):
        assert el in page, el
    assert f'id="al-note-in" placeholder="备注(可选)" maxlength="{ALERT_NOTE_MAX}"' in page
    ver = re.search(r'<script src="/app\.js\?v=([\w.]+)"></script>', page).group(1)
    assert ver != "35"                                                                       # 缓存戳已换

    js = c.get("/app.js").text
    start, end = js.index("// ---------- 价格提醒"), js.index("// ---------- 账号(仅登录后)")
    section = js[start:end]
    assert "/api/alerts" in section and "/api/alerts/remove" in section and "textContent" in section
    for banned in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write", "onclick="):
        assert banned not in section, banned                   # 用户数据只经 textContent 进页面
    assert "if (!ME.logged_in) return;" in section             # 匿名时根本不发请求
    assert "loadAlerts" in js[end:]                            # 启动时加载 + 定时刷新
