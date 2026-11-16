"""Web 控制台访问模型测试：免登录可看、所有控制与账户数据仍在登录之后。

运行: PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest tests/test_web_auth.py -q
用真实的 Config(config.yaml) + 临时 sqlite 上的真实 DB + create_app(engine=None)，不打任何网络。
"""
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.web.server as server
from app.config import Config
from app.db import DB

PUBLIC_GETS = [
    "/api/me", "/api/status", "/api/signals", "/api/trades", "/api/stats",
    "/api/strategy_stats", "/api/stats_by_tf", "/api/equity", "/api/klines?symbol=BTCUSDT",
    "/api/watchlist", "/api/macro", "/api/playbooks", "/api/backtest",
]
LOGIN_ONLY_GETS = ["/api/settings", "/api/macro_token", "/api/positions", "/api/live_stats",
                   "/api/events", "/api/account"]
LOGIN_ONLY_POSTS = ["/api/settings", "/api/watchlist", "/api/watchlist/remove", "/api/playbooks",
                    "/api/playbooks/1", "/api/backtest", "/api/password", "/api/macro"]


class Env:
    """一套独立的 cfg + 临时库 + app。"""

    def __init__(self, tmp_path, engine=None):
        self.cfg = Config()                      # 每个测试一份，覆盖值互不串
        self.db = DB(tmp_path / "t.db")
        self.engine = engine
        self.app = server.create_app(self.cfg, self.db, engine=engine, bot=None)
        self.user = self.cfg.get("web.username", "admin")
        self.pwd = self.cfg.get("web.initial_password")

    def client(self, ip="10.0.0.1") -> TestClient:
        return TestClient(self.app, client=(ip, 50000))

    def login(self, ip="10.0.0.9", pwd=None) -> TestClient:
        c = self.client(ip)
        r = c.post("/api/login", json={"username": self.user, "password": pwd or self.pwd})
        assert r.status_code == 200, r.text
        return c

    def add_signal(self, symbol="BTCUSDT", status="confirmed") -> int:
        sid = self.db.insert_signal({
            "created_at": int(time.time()), "symbol": symbol, "tf": "15m", "direction": "long",
            "kind": "primary", "entry": 100.0, "sl": 95.0, "tp": 110.0, "rr": 2.0, "vol_ratio": 3.1,
            "strength": "strong", "suggested_qty": 12.5, "risk_usdt": 5.0, "status": "new",
            "reason": "测试信号", "extra": {"type": "second_buy", "path": "macro_chan_pullback"},
        })
        self.db.update_signal(sid, status=status, tg_message_id=4242)
        return sid

    def add_trade(self, signal_id: int) -> None:
        self.db.execute(
            "INSERT INTO paper_trades (signal_id, symbol, tf, direction, track, entry, sl, tp, qty, opened_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (signal_id, "BTCUSDT", "15m", "long", "second_buy", 100.0, 95.0, 110.0, 12.5, int(time.time())))


@pytest.fixture(autouse=True)
def _fast_fail(monkeypatch):
    monkeypatch.setattr(server, "LOGIN_FAIL_DELAY_S", 0)     # 测试里不等 1 秒的失败延迟


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ---------------------------------------------------------------- 纯函数

def test_is_api_path_is_greedy():
    for p in ("/api", "/api/", "/api/x", "//api/x", "/API/settings", "\\api\\x", "/Api/ws", "/api.json"):
        assert server.is_api_path(p), p
    for p in ("/", "/index.html", "/app.js", "/style.css", "/lib/lwc.js", "/flow.html", "/favicon.ico"):
        assert not server.is_api_path(p), p


def test_anonymous_allowed_matrix():
    ok = server.anonymous_allowed
    for p in server.PUBLIC_GET:
        assert ok("GET", p), p
        for m in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            if not (m == "POST" and p in server.PUBLIC_POST):
                assert not ok(m, p, has_macro_token=(p != "/api/macro")), (m, p)
    # 订单流前缀：只放 GET，路径必须规整
    assert ok("GET", "/api/flow/summary") and ok("GET", "/api/flow/depth")
    for p in ("/api/flow", "/api/flow/", "/api/flow/../settings", "/api/flow//x", "/api/flow/a b",
              "/api/flowx/y", "/API/flow/summary", "/api/flow/x\n", "/api/status\n", "/api/me "):
        assert not ok("GET", p), p
    assert not ok("POST", "/api/flow/summary") and not ok("DELETE", "/api/flow/depth")
    # 登录/退出
    assert ok("POST", "/api/login") and ok("POST", "/api/logout")
    assert not ok("GET", "/api/login") and not ok("GET", "/api/logout")
    # 大盘观点写入：只有带 token 才放到处理函数去校验
    assert ok("POST", "/api/macro", has_macro_token=True)
    assert not ok("POST", "/api/macro") and not ok("POST", "/api/settings", has_macro_token=True)
    # 默认拒绝：名单外、变体、不存在的路径
    for p in ("/api/settings", "/api/positions", "/api/live_stats", "/api/events", "/api/macro_token",
              "/api/whatever", "/api", "/api/", "/api/status/", "/api/status/x", "/API/status",
              "//api/status", "/api/ws"):
        assert not ok("GET", p), p
    # 静态：只读
    assert ok("GET", "/") and ok("HEAD", "/index.html") and ok("GET", "/lib/lwc.js")
    assert not ok("POST", "/") and not ok("DELETE", "/app.js")


def test_login_guard_window_and_isolation():
    g = server.LoginGuard(limit=8, window=600, global_limit=1000)
    for i in range(7):
        g.attempt("a", now=float(i))
    assert g.retry_after("a", now=7.0) == 0                  # 7 次还没到
    g.attempt("a", now=7.0)
    assert g.retry_after("a", now=8.0) == 592                # 第 8 次后封到最早那次滑出窗口
    assert g.retry_after("b", now=8.0) == 0                  # 别的来源不受影响
    assert g.retry_after("a", now=599.9) > 0
    assert g.retry_after("a", now=600.0) == 0                # 窗口过了
    # 成功只清自己
    g2 = server.LoginGuard(limit=2, window=600, global_limit=1000)
    g2.attempt("a", now=0.0); g2.attempt("a", now=1.0)
    g2.attempt("b", now=1.0); s = g2.attempt("b", now=2.0)
    g2.success("b", s)
    assert g2.retry_after("b", now=3.0) == 0 and g2.retry_after("a", now=3.0) > 0


def test_login_guard_global_cap():
    g = server.LoginGuard(limit=8, window=600, global_limit=3)
    for i in range(3):
        g.attempt(f"ip{i}", now=float(i))
    assert g.retry_after("fresh", now=5.0) > 0               # 全局失败数到顶，新来源也先挡住
    assert g.retry_after("fresh", now=601.0) == 0


def test_limiters_and_cache_units():
    w = server.WindowLimiter(limit=3, window=60)
    assert [w.hit("k", now=float(t)) for t in range(3)] == [0, 0, 0]
    assert w.hit("k", now=3.0) == 57 and w.hit("other", now=3.0) == 0
    assert w.hit("k", now=60.0) == 0                         # 新窗口
    b = server.TokenBucket(rate=1.0, burst=2.0)
    t0 = b.ts
    assert b.take(now=t0) and b.take(now=t0) and not b.take(now=t0)
    assert b.take(now=t0 + 1.0) and not b.take(now=t0 + 1.0)
    c = server.ByteCache(max_bytes=100, max_items=3)
    c.put("a", b"x" * 10, ttl=5, now=0.0)
    assert c.get("a", now=4.9) == b"x" * 10 and c.get("a", now=5.0) is None
    for k in "bcde":
        c.put(k, b"y" * 10, ttl=5, now=0.0)
    assert len(c) == 3 and c.get("b", now=1.0) is None        # 条目上限：最早的被淘汰
    c.put("big", b"z" * 60, ttl=5, now=0.0)
    assert c.get("big", now=1.0) is None                     # 单条过大不进缓存
    for k in "fgh":
        c.put(k, b"w" * 24, ttl=5, now=0.0)
    assert c._size <= 100                                    # 总字节上限


def test_ip_key_groups_ipv6_by_64():
    assert server.ip_key("1.2.3.4") == "1.2.3.4"
    assert server.ip_key("2001:db8:1:2:aaaa::1") == server.ip_key("2001:db8:1:2:bbbb::9")
    assert server.ip_key("2001:db8:1:2::1") != server.ip_key("2001:db8:1:3::1")
    assert server.ip_key("::ffff:1.2.3.4") == "1.2.3.4"
    assert server.ip_key("testclient") == "testclient" and server.ip_key(None) == "?"


def test_public_views_strip_account_fields():
    s = server.public_signal({"id": 1, "entry": 1.0, "suggested_qty": 9, "risk_usdt": 5,
                              "tg_message_id": 7, "status": "confirmed", "extra": "{}"})
    assert s == {"id": 1, "entry": 1.0, "status": "", "extra": "{}"}
    assert server.public_trade({"id": 1, "qty": 3, "pnl": 1.5}) == {"id": 1, "pnl": 1.5}


# ---------------------------------------------------------------- 匿名：能看什么

@pytest.mark.parametrize("path", PUBLIC_GETS)
def test_anonymous_can_get_whitelist(env, path):
    r = env.client().get(path)
    assert r.status_code == 200, (path, r.status_code, r.text[:200])
    assert r.headers["cache-control"] == "no-store"          # 匿名/登录内容不同，禁止中间缓存


def test_anonymous_flow_prefix_is_not_401(env):
    # /api/flow/ 下的路由由别的模块注册；这里只确认鉴权层不拦 GET(未注册时是 404)、拦非 GET
    c = env.client()
    assert c.get("/api/flow/summary").status_code != 401
    assert c.get("/api/flow/depth?hours=6").status_code != 401
    assert c.post("/api/flow/summary", json={}).status_code == 401
    assert c.delete("/api/flow/depth").status_code == 401


def test_me_reports_login_state(env):
    c = env.client()
    assert c.get("/api/me").json() == {"logged_in": False, "user": None}
    c = env.login()
    assert c.get("/api/me").json() == {"logged_in": True, "user": env.user}
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/me").json() == {"logged_in": False, "user": None}


def test_static_pages_served_to_anonymous(env):
    c = env.client()
    r = c.get("/")
    assert r.status_code == 200 and 'id="stat-cards"' in r.text and "login-box" not in r.text
    r = c.get("/index.html")
    assert r.status_code == 200 and 'id="stat-cards"' in r.text and "login-box" not in r.text
    assert 'href="/flow.html"' in r.text and "订单流" in r.text          # 头部导航
    assert "/app.js?v=35" in r.text and "/style.css?v=35" in r.text      # 缓存戳已更新
    r = c.get("/login.html")
    assert r.status_code == 200 and "login-box" in r.text
    r = c.get("/app.js")
    assert r.status_code == 200 and "/api/me" in r.text
    assert "location.href = '/login.html'" not in r.text                  # 401 不再跳登录页
    assert c.get("/style.css").status_code == 200
    flow = c.get("/flow.html")                                            # 该页由别的模块提供
    assert flow.status_code == (200 if (server.STATIC / "flow.html").exists() else 404)
    assert "login-box" not in flow.text
    assert c.get("/no-such-page.html").status_code == 404


def test_security_headers(env):
    c = env.client()
    for path in ("/", "/api/me", "/api/settings"):
        h = c.get(path).headers
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff", path
    assert c.get("/").headers["cache-control"] == "no-cache, must-revalidate"
    assert c.get("/api/settings").headers["cache-control"] == "no-store"
    assert c.get("/openapi.json").status_code == 404 and c.get("/docs").status_code == 404


# ---------------------------------------------------------------- 匿名：不能做什么

@pytest.mark.parametrize("path", LOGIN_ONLY_GETS)
def test_anonymous_login_only_get_is_401(env, path):
    r = env.client().get(path)
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}


@pytest.mark.parametrize("path", LOGIN_ONLY_POSTS)
def test_anonymous_post_is_401(env, path):
    r = env.client().post(path, json={"symbol": "BTCUSDT", "direction": "long", "days": 1})
    assert r.status_code == 401, (path, r.text)


@pytest.mark.parametrize("payload", [{"live.auto_trade": True}, {"live.auto_trade": "true"},
                                     {"mode": "live"}, {"risk.leverage": 50}])
def test_anonymous_cannot_change_settings(env, payload):
    key = next(iter(payload))
    before = env.cfg.get(key)
    r = env.client().post("/api/settings", json=payload)
    assert r.status_code == 401
    assert env.cfg.get(key) == before                         # 内存配置没变
    assert key not in env.db.get_settings()                   # 也没落库
    assert not env.db.query("SELECT 1 FROM event_log WHERE source='web'")


@pytest.mark.parametrize("path", ["/api/whatever", "/api/", "/api", "/api/status/", "/api/status/extra",
                                  "/API/settings", "/api/settings/", "/api/v2/orders", "/api/ws",
                                  "/api/login", "/api/flow"])
def test_unknown_api_paths_are_401_not_fallthrough(env, path):
    assert env.client().get(path).status_code == 401, path


def test_encoded_path_cannot_dodge_the_whitelist(env):
    # %2F 解码后就是 /api/settings：中间件与路由看到的是同一个已解码路径
    c = env.client()
    assert c.get("/api%2Fsettings").status_code == 401
    assert c.get("/api/status%2F..%2Fsettings").status_code == 401
    assert c.post("/api%2Fsettings", json={"mode": "live"}).status_code == 401
    # 路由正则的 $ 会放过结尾换行(/api/settings%0A 能匹配到 settings 路由)：白名单是精确比对，照样拦住
    for tail in ("%0A", "%0a%0a", "%00", "%20", ";x=1", "%3F"):
        assert c.get("/api/settings" + tail).status_code == 401, tail
        assert c.post("/api/settings" + tail, json={"mode": "live"}).status_code == 401, tail
        assert c.get("/api/positions" + tail).status_code == 401, tail
    assert c.get("/api/flow/x%0A").status_code == 401
    assert env.cfg.mode == Config().mode and "mode" not in env.db.get_settings()


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_anonymous_other_methods_are_401(env, method):
    c = env.client()
    for path in ("/api/status", "/api/watchlist", "/api/settings", "/api/flow/depth", "/", "/app.js"):
        assert getattr(c, method)(path).status_code == 401, (method, path)
    assert c.post("/", json={}).status_code == 401


def test_garbage_or_forged_cookie_is_anonymous(env):
    c = env.client()
    exp = int(time.time()) + 3600
    for tok in ("x", "admin|9999999999|deadbeef", f"{env.user}|{exp}|" + "0" * 64, "a|b|c", "|" * 600):
        c.cookies.set("session", tok)
        assert c.get("/api/me").json()["logged_in"] is False
        assert c.get("/api/settings").status_code == 401
        assert c.post("/api/settings", json={"mode": "live"}).status_code == 401
    # 过期的真签名也不行
    stale = server.make_token(env.db, env.user, -1)
    c.cookies.set("session", stale)
    assert c.get("/api/settings").status_code == 401


# ---------------------------------------------------------------- 登录后：一切照旧

def test_login_then_same_calls_succeed(env):
    c = env.login()
    for path in LOGIN_ONLY_GETS:
        assert c.get(path).status_code == 200, path
    assert c.get("/api/positions").json() == {"live": False, "positions": []}
    assert set(c.get("/api/settings").json()) >= {"mode", "live.auto_trade", "risk.leverage"}

    r = c.post("/api/settings", json={"live.auto_trade": True})
    assert r.status_code == 200 and r.json()["applied"] == {"live.auto_trade": True}
    assert env.cfg.get("live.auto_trade") is True
    r = c.post("/api/settings", json={"mode": "live"})
    assert r.status_code == 200 and env.cfg.mode == "live"
    assert env.db.get_settings()["mode"] == '"live"'

    assert c.post("/api/watchlist", json={"symbol": "pepe", "note": "n"}).json() == {"ok": True, "symbol": "PEPEUSDT"}
    assert [w["symbol"] for w in c.get("/api/watchlist").json()] == ["PEPEUSDT"]
    assert c.post("/api/watchlist/remove", json={"symbol": "PEPEUSDT"}).status_code == 200
    r = c.post("/api/playbooks", json={"symbol": "btcusdt", "entry": "100", "tp": "", "sl": None})
    assert r.status_code == 200 and r.json()["ok"]
    assert c.post(f"/api/playbooks/{r.json()['id']}", json={"status": "cancelled"}).status_code == 200
    assert c.get("/api/playbooks").json()[0]["status"] == "cancelled"
    assert c.post("/api/backtest", json={"days": 1}).status_code == 400      # 过了鉴权，只是没引擎
    assert c.post("/api/macro", json={"direction": "short", "note": "手动"}).status_code == 200
    assert c.get("/api/macro").json()["direction"] == "short"
    for path in PUBLIC_GETS:
        assert c.get(path).status_code == 200, path
    assert c.get("/api/whatever").status_code == 404                        # 登录后未知路径才是普通 404


def test_logout_drops_access(env):
    c = env.login()
    assert c.get("/api/settings").status_code == 200
    r = c.post("/api/logout")
    assert r.status_code == 200 and "session=" in r.headers["set-cookie"]
    assert c.get("/api/settings").status_code == 401
    assert c.post("/api/settings", json={"mode": "live"}).status_code == 401


# ---------------------------------------------------------------- 登录加固

def test_wrong_password_throttled_per_ip(env):
    a, b = env.client("10.1.1.1"), env.client("10.2.2.2")
    for i in range(server.LOGIN_MAX_FAILS):
        # 伪造 X-Forwarded-For 换不来新的计数
        r = a.post("/api/login", json={"username": env.user, "password": f"wrong{i}"},
                   headers={"X-Forwarded-For": f"9.9.9.{i}"})
        assert r.status_code == 401, (i, r.text)
    r = a.post("/api/login", json={"username": env.user, "password": "wrong-again"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    # 被限流期间即使密码正确也不放行（否则可以边被限流边继续试）
    r = a.post("/api/login", json={"username": env.user, "password": env.pwd})
    assert r.status_code == 429 and "set-cookie" not in r.headers
    assert a.get("/api/me").json()["logged_in"] is False
    # 另一个来源不受影响；它登录成功也不会给前一个来源解封
    assert b.post("/api/login", json={"username": env.user, "password": env.pwd}).status_code == 200
    assert a.post("/api/login", json={"username": env.user, "password": env.pwd}).status_code == 429
    assert a.get("/api/status").status_code == 200           # 只是登录被限，页面照常能看


def test_successful_login_clears_own_failures_only(env):
    a = env.client("10.1.1.1")
    for i in range(server.LOGIN_MAX_FAILS - 1):
        assert a.post("/api/login", json={"username": env.user, "password": "nope"}).status_code == 401
    assert a.post("/api/login", json={"username": env.user, "password": env.pwd}).status_code == 200
    a2 = env.client("10.1.1.1")
    for i in range(server.LOGIN_MAX_FAILS - 1):
        assert a2.post("/api/login", json={"username": env.user, "password": "nope"}).status_code == 401


def test_global_failure_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LOGIN_GLOBAL_MAX_FAILS", 3)
    env = Env(tmp_path)
    for i in range(3):
        r = env.client(f"10.3.0.{i}").post("/api/login", json={"username": env.user, "password": "x"})
        assert r.status_code == 401
    r = env.client("10.3.0.99").post("/api/login", json={"username": env.user, "password": env.pwd})
    assert r.status_code == 429


def test_unknown_user_and_wrong_password_look_identical(env):
    c = env.client()
    r1 = c.post("/api/login", json={"username": env.user, "password": "wrong"})
    r2 = c.post("/api/login", json={"username": "nobody-here", "password": "wrong"})
    r3 = c.post("/api/login", json={"username": "", "password": ""})
    assert r1.status_code == r2.status_code == r3.status_code == 401
    assert r1.content == r2.content == r3.content
    for r in (r1, r2, r3):
        assert "set-cookie" not in r.headers
    assert sorted(k for k in r1.headers if k != "date") == sorted(k for k in r2.headers if k != "date")


def test_login_rejects_malformed_or_oversized_body(env):
    c = env.client()
    assert c.post("/api/login", content=b"not json").status_code == 400
    assert c.post("/api/login", json=["a", "b"]).status_code == 400
    big = {"username": env.user, "password": "x" * (server.MAX_LOGIN_BODY + 10)}
    assert c.post("/api/login", json=big).status_code == 400
    assert c.post("/api/login", json={"username": env.user, "password": env.pwd}).status_code == 200


def test_cookie_lifetime_follows_config(env):
    hours = env.cfg.get("web.session_hours")
    assert hours == 4320                                      # config.yaml: 180 天
    r = env.client().post("/api/login", json={"username": env.user, "password": env.pwd})
    sc = r.headers["set-cookie"]
    assert f"Max-Age={4320 * 3600}" in sc and "HttpOnly" in sc and "samesite=lax" in sc.lower()
    assert "Path=/" in sc and "secure" not in sc.lower()      # 站点是明文 HTTP，不能带 Secure
    exp = int(sc.split("session=")[1].split(";")[0].strip('"').split("|")[1])
    assert abs(exp - (time.time() + 4320 * 3600)) < 60        # token 自身的过期时间也一致

    env.cfg.set_override("web.session_hours", 5)
    sc = env.client("10.0.0.2").post("/api/login", json={"username": env.user, "password": env.pwd}).headers["set-cookie"]
    assert "Max-Age=18000" in sc


def test_password_change_revokes_other_sessions(env):
    a, b = env.login("10.0.0.1"), env.login("10.0.0.2")
    assert a.get("/api/account").json()["default_password"] is True
    assert a.post("/api/password", json={"old": "wrong", "new": "new-secret-123"}).status_code == 400
    assert a.post("/api/password", json={"old": env.pwd, "new": "short"}).status_code == 400
    assert a.post("/api/password", json={"old": env.pwd, "new": env.pwd}).status_code == 400   # 不许改回初始密码
    assert b.get("/api/settings").status_code == 200          # 失败的尝试不影响现有会话
    r = a.post("/api/password", json={"old": env.pwd, "new": "new-secret-123"})
    assert r.status_code == 200 and "session=" in r.headers["set-cookie"]
    assert a.get("/api/settings").status_code == 200          # 当前浏览器换发了新会话
    assert b.get("/api/settings").status_code == 401          # 其它设备全部失效
    assert a.get("/api/account").json()["default_password"] is False
    c = env.client("10.0.0.3")
    assert c.post("/api/login", json={"username": env.user, "password": env.pwd}).status_code == 401
    assert c.post("/api/login", json={"username": env.user, "password": "new-secret-123"}).status_code == 200
    assert env.client().post("/api/password", json={"old": "x", "new": "y" * 9}).status_code == 401


def test_cross_origin_post_with_session_is_rejected(env):
    c = env.login()
    before = env.cfg.get("live.auto_trade")
    for origin in ("http://evil.example", "null", "http://testserver.evil.example", "https://evil.example:8488"):
        r = c.post("/api/settings", json={"live.auto_trade": True}, headers={"Origin": origin})
        assert r.status_code == 403, origin
    assert env.cfg.get("live.auto_trade") == before and "live.auto_trade" not in env.db.get_settings()
    # 本站来源照常：Host 相同，或配置里的 public_url
    assert c.post("/api/settings", json={"risk.leverage": 3}, headers={"Origin": "http://testserver"}).status_code == 200
    pub = env.cfg.get("web.public_url")
    assert c.post("/api/settings", json={"risk.leverage": 4}, headers={"Origin": pub}).status_code == 200
    assert env.cfg.get("risk.leverage") == 4


# ---------------------------------------------------------------- 大盘观点 token 通道

def test_macro_token_path(env):
    owner = env.login()
    token = owner.get("/api/macro_token").json()["token"]
    assert len(token) >= 24
    anon = env.client("10.5.5.5")
    r = anon.post("/api/macro?token=wrong", json={"direction": "long", "note": "x"})
    assert r.status_code == 401 and anon.get("/api/macro").json()["direction"] == "neutral"
    assert anon.post("/api/macro", json={"direction": "long"}).status_code == 401
    assert anon.post("/api/macro?token=", json={"direction": "long"}).status_code == 401

    r = anon.post(f"/api/macro?token={token}", json={"direction": "long", "note": "日更", "source": "routine"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    m = anon.get("/api/macro").json()                         # 写入成功后匿名缓存立刻作废
    assert m["direction"] == "long" and m["note"] == "日更" and m["at"] > 0
    assert anon.post(f"/api/macro?token={token}", json={"direction": "sideways"}).status_code == 400

    # token 只管这一个接口，别处一概不认
    assert anon.post(f"/api/settings?token={token}", json={"mode": "live"}).status_code == 401
    assert anon.get(f"/api/settings?token={token}").status_code == 401
    assert anon.get(f"/api/macro_token?token={token}").status_code == 401
    assert env.cfg.mode == Config().mode


def test_macro_token_guessing_is_throttled(env):
    anon = env.client("10.6.6.6")
    for i in range(server.LOGIN_MAX_FAILS):
        assert anon.post(f"/api/macro?token=guess{i}", json={"direction": "long"}).status_code == 401
    assert anon.post("/api/macro?token=guess-more", json={"direction": "long"}).status_code == 429
    assert anon.get("/api/macro").json()["direction"] == "neutral"


# ---------------------------------------------------------------- 匿名视图：脱敏 / 夹参数 / 缓存 / 限流

def test_anonymous_view_hides_sizing_and_execution_traces(env):
    sid = env.add_signal(status="confirmed")
    env.add_trade(sid)
    env.db.upsert_klines("BTCUSDT", "15m", [(1_700_000_000_000 + i * 900_000, 1, 2, 0.5, 1.5, 10, 15, 5, 1)
                                            for i in range(5)])
    anon, owner = env.client(), env.login()

    s = anon.get("/api/signals").json()[0]
    assert s["id"] == sid and s["entry"] == 100.0 and s["status"] == ""
    assert not {"suggested_qty", "risk_usdt", "tg_message_id"} & set(s)
    full = owner.get("/api/signals").json()[0]
    assert full["status"] == "confirmed" and full["suggested_qty"] == 12.5 and full["tg_message_id"] == 4242

    t = anon.get("/api/trades").json()[0]
    assert "qty" not in t and t["sig_state"] == "try" and t["entry"] == 100.0
    assert owner.get("/api/trades").json()[0]["qty"] == 12.5

    k = anon.get("/api/klines?symbol=BTCUSDT&tf=15m").json()
    assert len(k["klines"]) == 5 and k["signals"][0]["status"] == ""
    assert owner.get("/api/klines?symbol=BTCUSDT&tf=15m").json()["signals"][0]["status"] == "confirmed"

    # 任何匿名响应里都不该出现密钥类内容
    secret = env.db.get_settings()["_session_secret"]
    pwd_hash = env.db.one("SELECT pwd_hash, salt FROM users")
    token = owner.get("/api/macro_token").json()["token"]
    for path in PUBLIC_GETS:
        body = anon.get(path).text
        for needle in (secret, pwd_hash["pwd_hash"], pwd_hash["salt"], token):
            assert needle not in body, path


def test_backtest_result_sanitised_for_anonymous(env, monkeypatch):
    state = {"running": False, "progress": "2/2", "started_at": 1,
             "result": {"total": {"signals": 1}, "params": {"chan.bi_min_bars": 5}, "signals": []}}
    monkeypatch.setitem(server.APP_STATE, "backtest", state)
    anon, owner = env.client(), env.login()
    assert "params" not in anon.get("/api/backtest").json()["result"]
    assert owner.get("/api/backtest").json()["result"]["params"] == {"chan.bi_min_bars": 5}
    state["result"] = {"error": "binance /fapi/v1/klines 418: banned until 123 (key=abc)"}
    assert owner.post("/api/watchlist/remove", json={"symbol": "NOPE"}).status_code == 200   # 一次成功写入 → 匿名缓存作废
    assert anon.get("/api/backtest").json()["result"] == {"error": "回测失败"}


def test_expensive_parameters_are_clamped(env):
    for _ in range(3):
        env.add_trade(env.add_signal())
    env.db.upsert_klines("BTCUSDT", "5m", [(1_700_000_000_000 + i * 300_000, 1, 2, 0.5, 1.5, 10, 15, 5, 1)
                                           for i in range(30)])
    env.db.log("info", "test", "e1")
    env.db.log("info", "test", "e2")
    anon, owner = env.client(), env.login()
    # 负数 limit 在 SQLite 里是「不限」——必须被夹成 1
    assert len(anon.get("/api/signals?limit=-1").json()) == 1
    assert len(anon.get("/api/signals?limit=0").json()) == 1
    assert len(anon.get("/api/trades?limit=-7").json()) == 1
    assert len(owner.get("/api/signals?limit=-1").json()) == 1
    assert len(owner.get("/api/events?limit=-1").json()) == 1
    assert len(anon.get("/api/klines?symbol=BTCUSDT&tf=5m&limit=-1").json()["klines"]) == 1
    assert len(anon.get("/api/klines?symbol=btcusdt&tf=5m&limit=99999").json()["klines"]) == 30
    assert len(anon.get("/api/klines?symbol=BTCUSDT&tf=30m&limit=3").json()["klines"]) == 3   # 5m 聚合
    assert len(anon.get("/api/signals?limit=100000").json()) == 3
    # 不合法的参数直接 400，不进库也不占缓存
    for bad in ("/api/klines?symbol=BTC/USDT", "/api/klines?symbol=" + "A" * 40, "/api/klines?symbol=BTCUSDT&tf=7w",
                "/api/klines?symbol=BTCUSDT&tf=4h", "/api/equity?track=a%20b", "/api/equity?track=" + "x" * 40,
                "/api/signals?kind=a'b", "/api/trades?track=%3Cscript%3E", "/api/trades?tf=abc",
                "/api/playbooks?status=a;b"):
        assert anon.get(bad).status_code == 400, bad
    assert anon.get("/api/signals?limit=abc").status_code == 422
    assert anon.get("/api/klines").status_code == 422


def test_anonymous_responses_cached_and_invalidated_on_write(env):
    anon, owner = env.client(), env.login()
    assert anon.get("/api/watchlist").json() == []
    env.db.add_watch("AAAUSDT", "直接写库")                    # 绕过 API 写库：匿名在 TTL 内仍是缓存
    assert anon.get("/api/watchlist").json() == []
    assert [w["symbol"] for w in owner.get("/api/watchlist").json()] == ["AAAUSDT"]   # 登录用户永远实时
    assert anon.post("/api/logout").status_code == 200         # 匿名能调的 POST 不会冲掉缓存
    assert anon.post("/api/login", json={"username": "x", "password": "y"}).status_code == 401
    assert anon.get("/api/watchlist").json() == []
    assert owner.post("/api/watchlist", json={"symbol": "BBB"}).status_code == 200    # 经 API 写入 → 缓存作废
    assert {w["symbol"] for w in anon.get("/api/watchlist").json()} == {"AAAUSDT", "BBBUSDT"}


def test_anonymous_api_rate_limit_per_ip(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ANON_API_PER_MIN", 5)
    env = Env(tmp_path)
    owner = env.login("10.7.0.1")
    flood = env.client("10.7.0.2")
    assert [flood.get("/api/status").status_code for _ in range(5)] == [200] * 5
    r = flood.get("/api/status")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert flood.get("/api/settings").status_code == 429       # 超限后一律 429，包括本来会 401 的
    assert flood.get("/").status_code == 200                   # 静态页不计入
    assert env.client("10.7.0.3").get("/api/status").status_code == 200    # 别的来源不受影响
    assert [owner.get("/api/status").status_code for _ in range(12)] == [200] * 12   # 登录用户不限


def test_anonymous_cache_miss_budget_is_global(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ANON_MISS_BURST", 2.0)
    monkeypatch.setattr(server, "ANON_MISS_RATE", 0.0001)
    env = Env(tmp_path)
    owner = env.login()
    a, b = env.client("10.8.0.1"), env.client("10.8.0.2")
    assert a.get("/api/status").status_code == 200             # 未命中 1
    assert a.get("/api/macro").status_code == 200              # 未命中 2
    r = b.get("/api/watchlist")                                # 预算用完：换个来源也一样
    assert r.status_code == 429 and r.headers["retry-after"] == "2"
    assert b.get("/api/status").status_code == 200             # 已缓存的不花预算
    assert b.get("/api/me").status_code == 200                 # 不走缓存的轻接口不受影响
    assert owner.get("/api/watchlist").status_code == 200      # 登录用户不受匿名预算限制


# ---------------------------------------------------------------- WebSocket

class StubEngine:
    """只带 web 层要订阅的两个回调列表。真 Engine 一构造就会建交易所连接，这里用不上。"""

    def __init__(self):
        self.signal_subscribers = []
        self.trade_close_subscribers = []

    def status(self):
        return {"mode": "paper", "symbols": 0}


def _signal():
    from app.engine.signals import Signal
    return Signal(symbol="BTCUSDT", tf="15m", direction="long", kind="primary", entry=100.0, sl=95.0,
                  tp=110.0, rr=2.0, vol_ratio=3.0, strength="strong", suggested_qty=12.5, risk_usdt=5.0,
                  reason="测试", created_at=int(time.time()), extra={"type": "second_buy"})


def test_ws_anonymous_gets_sanitised_push(tmp_path):
    eng = StubEngine()
    env = Env(tmp_path, engine=eng)
    on_sig, on_close = eng.signal_subscribers[0], eng.trade_close_subscribers[0]
    trade = {"id": 3, "symbol": "BTCUSDT", "result": "tp", "pnl": 1.5, "pnl_r": 2.0, "qty": 12.5}

    with env.client().websocket_connect("/api/ws") as ws:      # 匿名：能连、只收推送
        ws.send_text("ping")                                   # 心跳被忽略，不回任何东西
        ws.portal.call(on_sig, 7, _signal())
        msg = ws.receive_json()
        assert msg["kind"] == "signal" and msg["data"]["id"] == 7 and msg["data"]["entry"] == 100.0
        assert msg["data"]["status"] == "" and "suggested_qty" not in msg["data"] and "risk_usdt" not in msg["data"]
        ws.portal.call(on_close, trade)
        msg = ws.receive_json()
        assert msg["kind"] == "trade_close" and msg["data"]["pnl"] == 1.5 and "qty" not in msg["data"]

    with env.login().websocket_connect("/api/ws") as ws:       # 登录：完整字段，与开放前一致
        ws.portal.call(on_sig, 8, _signal())
        msg = ws.receive_json()
        assert msg["data"]["id"] == 8 and msg["data"]["suggested_qty"] == 12.5 and msg["data"]["status"] == "new"
        ws.portal.call(on_close, trade)
        assert ws.receive_json()["data"]["qty"] == 12.5

    # 跨站页面带着 cookie 来握手：不认它的登录态，只给匿名视图
    with env.login().websocket_connect("/api/ws", headers={"Origin": "http://evil.example"}) as ws:
        ws.portal.call(on_sig, 9, _signal())
        assert "suggested_qty" not in ws.receive_json()["data"]


def test_ws_anonymous_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_ANON_WS_PER_IP", 1)
    env = Env(tmp_path, engine=StubEngine())
    owner = env.login("10.9.0.1")
    with env.client("10.9.0.1").websocket_connect("/api/ws"):
        with pytest.raises(WebSocketDisconnect):               # 同一来源第二条匿名连接被拒
            with env.client("10.9.0.1").websocket_connect("/api/ws"):
                pass
        with env.client("10.9.0.2").websocket_connect("/api/ws"):   # 别的来源可以
            pass
        with owner.websocket_connect("/api/ws"):               # 登录用户不占匿名名额
            pass
    with env.client("10.9.0.1").websocket_connect("/api/ws"):  # 断开后名额归还
        pass
    # 匿名连接乱发大包/刷屏：直接断开
    with env.client("10.9.0.3").websocket_connect("/api/ws") as ws:
        ws.send_text("x" * 500)
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_text()
        assert ei.value.code == 1008
    with env.client("10.9.0.4").websocket_connect("/api/ws") as ws:
        for _ in range(31):
            ws.send_text("ping")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_ws_total_anonymous_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_ANON_WS", 2)
    env = Env(tmp_path, engine=StubEngine())
    with env.client("10.9.1.1").websocket_connect("/api/ws"), env.client("10.9.1.2").websocket_connect("/api/ws"):
        with pytest.raises(WebSocketDisconnect):
            with env.client("10.9.1.3").websocket_connect("/api/ws"):
                pass
        with env.login("10.9.1.4").websocket_connect("/api/ws"):
            pass
