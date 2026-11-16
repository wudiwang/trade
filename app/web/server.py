"""Web 控制台：公开只读浏览 + 登录后才可操作。

访问模型（默认拒绝，2026-11 起页面对外开放浏览）：
  - 匿名可访问：静态资源/页面；POST /api/login、/api/logout；以及 PUBLIC_GET 白名单内的 GET
    （外加 /api/flow/ 前缀的 GET）。
  - 其余一律要登录：所有非 GET 请求、白名单之外的任何 /api 路径（包括还不存在的路径）。
  - 白名单 GET 对匿名访客：只读本地库/内存，结果做字节级 TTL 缓存 + 全局「未命中」预算，
    并剥掉仓位规模、实盘执行痕迹等账户信息。绝不在请求路径上调用交易所接口。
  - 本进程同时跑着真钱交易引擎（单核 VPS）：凡是陌生人能触发的计算都必须有上限。
"""
import asyncio
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("web")
STATIC = Path(__file__).parent / "static"

APP_STATE: dict = {"engine": None, "bot": None, "cfg": None, "db": None}

# ---------- 访问控制：白名单（纯函数，便于单测） ----------

# 匿名可 GET 的接口（精确匹配）。新增接口默认不在名单内 = 默认要登录。
PUBLIC_GET = frozenset({
    "/api/me", "/api/status", "/api/signals", "/api/trades", "/api/stats",
    "/api/strategy_stats", "/api/stats_by_tf", "/api/equity", "/api/klines",
    "/api/watchlist", "/api/macro", "/api/playbooks", "/api/backtest",
})
PUBLIC_GET_PREFIX = "/api/flow/"          # 订单流页面的只读接口（仅 GET）
PUBLIC_POST = frozenset({"/api/login", "/api/logout"})
SAFE_METHODS = ("GET", "HEAD")
_FLOW_PATH_RE = re.compile(r"^/api/flow/[A-Za-z0-9_\-/.]+$")


def is_api_path(path: str) -> bool:
    """是否按 API 处理（默认拒绝的范围）。先归一化大小写/重复斜杠/反斜杠，
    宁可多拦：//api/x、/API/x、/api 都算。"""
    p = re.sub(r"/{2,}", "/", (path or "").replace("\\", "/")).lower()
    return p.lstrip("/").startswith("api")


def is_public_get(path: str) -> bool:
    """匿名 GET 白名单：精确路径，或 /api/flow/ 前缀下的规整路径（不含 .. 与 //）。"""
    if path in PUBLIC_GET:
        return True
    return bool(path.startswith(PUBLIC_GET_PREFIX) and _FLOW_PATH_RE.fullmatch(path)
                and ".." not in path and "//" not in path)


def anonymous_allowed(method: str, path: str, has_macro_token: bool = False) -> bool:
    """未登录请求是否放行到路由层。POST /api/macro 带 ?token= 时放行，由处理函数校验 token。"""
    method = (method or "").upper()
    if not is_api_path(path):
        return method in SAFE_METHODS                 # 页面与静态资源只读
    if method == "GET":
        return is_public_get(path)
    if method == "POST":
        return path in PUBLIC_POST or (path == "/api/macro" and has_macro_token)
    return False


# ---------- 限流 / 缓存小工具（纯内存、单事件循环内使用） ----------

def ip_key(host: str | None) -> str:
    """限流用的来源键：IPv4 用整址；IPv6 归并到 /64（否则一个前缀就有无穷多地址可换）。"""
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return host or "?"
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_address((int(ip) >> 64) << 64)) + "/64"
    return str(ip)


class LoginGuard:
    """登录失败限流：同一来源 window 秒内失败满 limit 次 → 拒绝，直到最早那次滑出窗口。
    另有全局失败上限：挡分布式爆破，也封住陌生人能消耗的 PBKDF2 算力。
    attempt() 在验证密码之前就先记一次失败（并发请求也逃不过），成功后 success() 撤销。"""

    def __init__(self, limit: int = 8, window: float = 600.0, global_limit: int = 200):
        self.limit, self.window, self.global_limit = limit, window, global_limit
        self._fails: dict[str, collections.deque] = {}
        self._all: collections.deque = collections.deque()

    def _prune(self, now: float) -> None:
        g = self._all
        while g and now - g[0] >= self.window:
            g.popleft()
        if len(self._fails) >= 512:                    # 偶尔整体清一遍，防字典无限增长
            for k in [k for k, dq in self._fails.items() if not dq or now - dq[-1] >= self.window]:
                del self._fails[k]

    def retry_after(self, key: str, now: float | None = None) -> int:
        """>0 表示当前被限流，值为还要等的秒数。"""
        now = time.monotonic() if now is None else now
        self._prune(now)
        dq = self._fails.get(key)
        if dq is not None:
            while dq and now - dq[0] >= self.window:
                dq.popleft()
            if not dq:
                del self._fails[key]
            elif len(dq) >= self.limit:
                return max(1, math.ceil(dq[0] + self.window - now))
        if len(self._all) >= self.global_limit:
            return max(1, math.ceil(self._all[0] + self.window - now))
        return 0

    def attempt(self, key: str, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        self._fails.setdefault(key, collections.deque()).append(now)
        self._all.append(now)
        return now

    def forget(self, key: str, stamp: float) -> None:
        """撤销某一次预记（那次请求根本没验到密码，比如服务忙被拒）。"""
        for dq in (self._fails.get(key), self._all):
            if dq is not None:
                try:
                    dq.remove(stamp)
                except ValueError:
                    pass
        if key in self._fails and not self._fails[key]:
            del self._fails[key]

    def success(self, key: str, stamp: float) -> None:
        """登录成功：撤销本次预记，并清掉本来源的失败记录（不影响其它来源）。"""
        self.forget(key, stamp)
        self._fails.pop(key, None)


class WindowLimiter:
    """固定窗口计数限流：每个键 window 秒内最多 limit 次。"""

    def __init__(self, limit: int, window: float = 60.0, max_keys: int = 20000):
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self._d: dict[str, list] = {}

    def hit(self, key: str, now: float | None = None) -> int:
        """计一次。返回 0=放行；>0=被限流，值为窗口剩余秒数。"""
        now = time.monotonic() if now is None else now
        ent = self._d.get(key)
        if ent is None or now - ent[0] >= self.window:
            if len(self._d) >= self.max_keys:
                self._d = {k: v for k, v in self._d.items() if now - v[0] < self.window}
                if len(self._d) >= self.max_keys:      # 仍然爆表(海量来源)：清空重来，靠全局预算兜底
                    self._d.clear()
            self._d[key] = [now, 1]
            return 0
        ent[1] += 1
        if ent[1] > self.limit:
            return max(1, math.ceil(ent[0] + self.window - now))
        return 0


class TokenBucket:
    """令牌桶：每秒补 rate 个，最多攒 burst 个。"""

    def __init__(self, rate: float, burst: float):
        self.rate, self.burst = float(rate), float(burst)
        self.tokens, self.ts = float(burst), time.monotonic()

    def take(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self.tokens = min(self.burst, self.tokens + max(0.0, now - self.ts) * self.rate)
        self.ts = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class ByteCache:
    """匿名响应缓存：key -> (过期时刻, 已序列化的 JSON 字节)。
    有总字节上限与条目上限，超了按插入顺序淘汰——参数可被陌生人随意变化，缓存不能无限长。"""

    def __init__(self, max_bytes: int = 8 * 1024 * 1024, max_items: int = 300):
        self.max_bytes, self.max_items = max_bytes, max_items
        self._d: collections.OrderedDict = collections.OrderedDict()
        self._size = 0

    def get(self, key, now: float | None = None) -> bytes | None:
        ent = self._d.get(key)
        if ent is None:
            return None
        now = time.monotonic() if now is None else now
        if now >= ent[0]:
            self._drop(key)
            return None
        return ent[1]

    def put(self, key, body: bytes, ttl: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._drop(key)
        if len(body) > self.max_bytes // 4:            # 单条过大就不缓存（照常返回）
            return
        self._d[key] = (now + ttl, body)
        self._size += len(body)
        while self._d and (self._size > self.max_bytes or len(self._d) > self.max_items):
            self._drop(next(iter(self._d)))

    def _drop(self, key) -> None:
        ent = self._d.pop(key, None)
        if ent is not None:
            self._size -= len(ent[1])

    def clear(self) -> None:
        self._d.clear()
        self._size = 0

    def __len__(self) -> int:
        return len(self._d)


# 可调常量（create_app 调用时读取；测试用 monkeypatch 改）
LOGIN_MAX_FAILS = 8            # 单来源 10 分钟内失败次数上限
LOGIN_WINDOW_S = 600
LOGIN_GLOBAL_MAX_FAILS = 200   # 全部来源合计的失败上限（同一窗口）
LOGIN_FAIL_DELAY_S = 1.0       # 失败后固定延迟（抗爆破）
ANON_API_PER_MIN = 240         # 单来源匿名 API 请求数/分钟
ANON_MISS_RATE = 10.0          # 匿名「缓存未命中」(=真正要算一次) 全局每秒上限
ANON_MISS_BURST = 40.0
MAX_ANON_WS = 60               # 匿名 WebSocket 总连接上限
MAX_ANON_WS_PER_IP = 4
WS_SEND_TIMEOUT_S = 5.0
MAX_LOGIN_BODY = 4096

# ---------- 匿名视图脱敏 ----------

_SIGNAL_PRIVATE = ("suggested_qty", "risk_usdt", "tg_message_id")   # 仓位规模 / TG 消息号


def public_signal(row: dict) -> dict:
    """匿名可见的信号：去掉仓位规模字段；status 记录的是「是否确认下单/忽略/下单失败」
    这类账户操作痕迹（live 下 confirmed = 真实开仓），整体抹掉。"""
    d = {k: v for k, v in row.items() if k not in _SIGNAL_PRIVATE}
    if "status" in d:
        d["status"] = ""
    return d


def public_trade(row: dict) -> dict:
    """匿名可见的模拟成交：去掉下单数量。"""
    d = dict(row)
    d.pop("qty", None)
    return d


def _dumps(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      default=str, allow_nan=False).encode("utf-8")


_TOKEN_RE = re.compile(r"^[\w\-]{0,32}$")                # track / kind / status 这类短标识
_TF_RE = re.compile(r"^(\d{1,2}[mhd])?$")
_SYMBOL_RE = re.compile(r"^\w{2,32}$")                      # 币种：字母数字下划线(含中文名合约)，不含任何符号


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(int(v), hi))


# ---------- 密码与会话 ----------

def hash_pwd(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def _secret(db) -> str:
    r = db.one("SELECT value FROM settings WHERE key='_session_secret'")
    s = r["value"] if r else None
    if not s:
        s = secrets.token_hex(32)
        db.set_setting("_session_secret", s)
    return s


def rotate_secret(db) -> None:
    """换会话签名密钥：所有已签发的会话（所有设备）立即失效。改密码时调用。"""
    db.set_setting("_session_secret", secrets.token_hex(32))


def make_token(db, username: str, hours: int) -> str:
    exp = int(time.time()) + hours * 3600
    payload = f"{username}|{exp}"
    sig = hmac.new(_secret(db).encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def check_token(db, token: str) -> str | None:
    try:
        if not token or len(token) > 512:
            return None
        username, exp, sig = token.rsplit("|", 2)
        payload = f"{username}|{exp}"
        good = hmac.new(_secret(db).encode(), payload.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig.encode(), good.encode()) and int(exp) > time.time():
            return username
    except Exception:
        pass
    return None


def session_hours(cfg) -> int:
    """会话有效期(小时)，取 web.session_hours；cookie max_age 与 token 过期时间都跟它走。"""
    try:
        h = int(cfg.get("web.session_hours", 4320))
    except (TypeError, ValueError):
        h = 4320
    return _clamp(h, 1, 24 * 400)


def ensure_admin(cfg, db) -> None:
    if not db.one("SELECT username FROM users LIMIT 1"):
        salt = secrets.token_hex(16)
        u = cfg.get("web.username", "admin")
        db.execute(
            "INSERT INTO users (username, pwd_hash, salt, created_at) VALUES (?,?,?,?)",
            (u, hash_pwd(cfg.get("web.initial_password", "trade@2026"), salt), salt, int(time.time())),
        )
        log.info("created initial web user '%s'", u)


def _origin_ok(conn, cfg) -> bool:
    """跨站请求防护（SameSite=Lax 之外再加一道）：带 Origin 头的请求，来源必须是本站。
    没有 Origin 头（脚本/curl/旧式同源请求）放行——那类请求拿不到浏览器里的会话 cookie。"""
    origin = conn.headers.get("origin")
    if origin is None:
        return True
    try:
        host = (urlsplit(origin).netloc or "").lower()
    except ValueError:
        return False
    if not host:                                       # "null" 等不透明来源
        return False
    allowed = {(conn.headers.get("host") or "").lower()}
    xfh = conn.headers.get("x-forwarded-host")         # 以后若放到反代后面，Host 可能被改写
    if xfh:
        allowed.add(xfh.split(",")[0].strip().lower())
    pub = str(cfg.get("web.public_url", "") or "")
    if pub:
        try:
            allowed.add(urlsplit(pub).netloc.lower())
        except ValueError:
            pass
    allowed.discard("")
    return host in allowed


def _err(status: int, msg: str, retry_after: int | None = None) -> JSONResponse:
    resp = JSONResponse({"error": msg}, status_code=status)
    if retry_after:
        resp.headers["Retry-After"] = str(int(retry_after))
    return resp


# ---------- 应用 ----------

def create_app(cfg, db, engine=None, bot=None) -> FastAPI:
    APP_STATE.update(engine=engine, bot=bot, cfg=cfg, db=db)
    ensure_admin(cfg, db)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    login_guard = LoginGuard(LOGIN_MAX_FAILS, LOGIN_WINDOW_S, LOGIN_GLOBAL_MAX_FAILS)
    anon_limiter = WindowLimiter(ANON_API_PER_MIN, 60.0)
    miss_bucket = TokenBucket(ANON_MISS_RATE, ANON_MISS_BURST)
    pub_cache = ByteCache()
    fail_delay = LOGIN_FAIL_DELAY_S
    max_anon_ws, max_anon_ws_per_ip = MAX_ANON_WS, MAX_ANON_WS_PER_IP

    ws_clients: dict[WebSocket, bool] = {}             # 连接 -> 是否已登录
    ws_anon_by_ip: collections.Counter = collections.Counter()
    bg_tasks: set = set()

    def client_key(conn) -> str:
        # 直连公网、前面没有反代：只认 TCP 对端地址，不信 X-Forwarded-For（可伪造）
        return ip_key(conn.client.host if conn.client else "")

    # 引擎事件 → 网页实时推送。
    # 注意：引擎在处理收盘K的协程里 await 这个回调，所以这里绝不能等任何客户端的 socket——
    # 只序列化一次、给每个连接起一个带超时的发送任务就返回；慢连接/恶意不读的连接超时即踢。
    async def _send(ws: WebSocket, text: str) -> None:
        try:
            await asyncio.wait_for(ws.send_text(text), WS_SEND_TIMEOUT_S)
        except Exception:
            ws_clients.pop(ws, None)
            try:
                await ws.close()
            except Exception:
                pass

    async def _close_quietly(ws: WebSocket) -> None:
        try:
            await asyncio.wait_for(ws.close(code=4401), WS_SEND_TIMEOUT_S)
        except Exception:
            pass

    async def broadcast(kind: str, data: dict):
        pub_cache.clear()                              # 有新信号/平仓 → 匿名缓存立刻作废
        if not ws_clients:
            return
        full = json.dumps({"kind": kind, "data": data}, ensure_ascii=False, default=str)
        pub = public_signal(data) if kind == "signal" else public_trade(data)
        anon = json.dumps({"kind": kind, "data": pub}, ensure_ascii=False, default=str)
        for ws, authed in list(ws_clients.items()):
            t = asyncio.create_task(_send(ws, full if authed else anon))
            bg_tasks.add(t)
            t.add_done_callback(bg_tasks.discard)

    if engine:
        async def on_sig(sid, s):
            await broadcast("signal", {"id": sid, **s.to_db()})
        async def on_close(trade):
            await broadcast("trade_close", trade)
        engine.signal_subscribers.append(on_sig)
        engine.trade_close_subscribers.append(on_close)

    # ---------- 鉴权 ----------
    def auth_user(conn) -> str | None:
        tok = conn.cookies.get("session", "")
        return check_token(db, tok) if tok else None

    def current_user(request: Request) -> str | None:
        """本次请求的登录用户（中间件已算过就复用）。"""
        st = request.state
        if not hasattr(st, "auth_user"):
            st.auth_user = auth_user(request)
        return st.auth_user

    def require_login(request: Request) -> str:
        """路由级兜底：即使中间件被改坏，敏感接口自己也拒绝未登录请求。"""
        user = current_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="unauthorized")
        return user

    LOGIN = [Depends(require_login)]

    def _secure(resp: Response, path: str, api: bool) -> Response:
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")        # 禁止被别的站点 iframe（防点击劫持）
        h.setdefault("Referrer-Policy", "same-origin")
        if api:
            # 同一 URL 对匿名/登录返回不同内容，且走明文 HTTP：禁止任何中间缓存保存
            h["Cache-Control"] = "no-store"
            h["Vary"] = "Cookie"
        elif path == "/" or path.endswith((".html", ".js", ".css")):
            # 前端资源禁缓存：部署后用户打开即拿到最新JS/HTML/CSS
            h["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        # 用路由层实际匹配的同一个 path（已解码），避免「中间件看一个、路由看另一个」
        path = request.scope.get("path") or ""
        method = request.method.upper()
        api = is_api_path(path)
        user = auth_user(request)
        request.state.auth_user = user
        if user is None:
            if api:
                wait = anon_limiter.hit(client_key(request))
                if wait:
                    return _secure(_err(429, "请求过于频繁，请稍后再试", wait), path, True)
            if not anonymous_allowed(method, path, bool(request.query_params.get("token"))):
                return _secure(_err(401, "unauthorized"), path, True)
        elif method not in SAFE_METHODS and not _origin_ok(request, cfg):
            log.warning("blocked cross-origin %s %s origin=%r", method, path,
                        request.headers.get("origin"))
            return _secure(_err(403, "跨站请求被拒绝"), path, True)
        resp = await call_next(request)
        if api and method not in SAFE_METHODS and resp.status_code < 400 and path not in PUBLIC_POST:
            # 有写入成功 → 匿名缓存作废。登录/退出不算写入：否则陌生人狂刷 /api/logout 就能让缓存形同虚设
            pub_cache.clear()
        return _secure(resp, path, api)

    def public(request: Request, key: tuple, ttl: float, produce):
        """白名单 GET 的统一出口。produce(anon) 只许读本地库/内存、不许 await。
        登录用户：实时计算、完整字段（与开放前一致）。
        匿名：命中缓存直接回字节；未命中先过全局预算再算——同步计算天然 single-flight。"""
        if current_user(request):
            return produce(False)
        body = pub_cache.get(key)
        if body is None:
            if not miss_bucket.take():
                return _err(429, "服务繁忙，请稍后再试", 2)
            body = _dumps(produce(True))
            pub_cache.put(key, body, ttl)
        return Response(content=body, media_type="application/json")

    async def read_json(request: Request, limit: int) -> dict | None:
        """带大小上限地读 JSON 对象体；超限/非法返回 None。"""
        cl = request.headers.get("content-length", "")
        if cl.isdigit() and int(cl) > limit:
            return None
        buf = bytearray()
        async for chunk in request.stream():
            buf += chunk
            if len(buf) > limit:
                return None
        try:
            data = json.loads(bytes(buf) or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    # PBKDF2 很重（VPS 上 0.1s 量级）：丢线程里算，不卡交易引擎的事件循环；同时只许一个在算，
    # 排队的超过 8 个直接拒绝——陌生人最多占住一个线程，不会把单核吃光。
    hash_state = {"busy": 0, "waiting": 0}

    async def hash_async(password: str, salt: str) -> str | None:
        if hash_state["waiting"] >= 8:
            return None
        hash_state["waiting"] += 1
        try:
            while hash_state["busy"] >= 1:
                await asyncio.sleep(0.02)
            hash_state["busy"] += 1
        finally:
            hash_state["waiting"] -= 1
        try:
            return await asyncio.to_thread(hash_pwd, password, salt)
        finally:
            hash_state["busy"] -= 1

    DUMMY_SALT = secrets.token_hex(16)                 # 用户名不存在时也照算一遍，耗时与响应都不泄露
    DUMMY_HASH = secrets.token_hex(32)
    default_pwd_cache: dict[str, bool] = {}            # 用户名 -> 是否仍是初始密码

    def set_session(resp: Response, request: Request, username: str) -> None:
        hours = session_hours(cfg)
        resp.set_cookie("session", make_token(db, username, hours), httponly=True, samesite="lax",
                        max_age=hours * 3600, path="/",
                        secure=request.url.scheme == "https")

    # ---------- API ----------
    @app.get("/api/me")
    async def me(request: Request):
        user = current_user(request)
        return {"logged_in": bool(user), "user": user}

    @app.post("/api/login")
    async def login(request: Request):
        ip = client_key(request)
        wait = login_guard.retry_after(ip)
        if wait:
            return _err(429, f"尝试次数过多，请 {math.ceil(wait / 60)} 分钟后再试", wait)
        body = await read_json(request, MAX_LOGIN_BODY)
        if body is None:
            return _err(400, "请求格式错误")
        username = str(body.get("username") or "")[:64]
        password = str(body.get("password") or "")[:256]
        stamp = login_guard.attempt(ip)                # 先记一次失败，成功再撤销
        u = db.one("SELECT * FROM users WHERE username=?", (username,))
        calc = await hash_async(password, u["salt"] if u else DUMMY_SALT)
        if calc is None:
            login_guard.forget(ip, stamp)
            return _err(429, "服务繁忙，请稍后再试", 2)
        good = u["pwd_hash"] if u else DUMMY_HASH
        if not (hmac.compare_digest(calc.encode(), str(good).encode()) and u):
            log.warning("login failed from %s", ip)
            await asyncio.sleep(fail_delay)            # 抗爆破
            return _err(401, "用户名或密码错误")         # 用户不存在/密码错：响应完全一致
        login_guard.success(ip, stamp)
        resp = JSONResponse({"ok": True})
        set_session(resp, request, u["username"])
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("session", path="/")
        return resp

    @app.get("/api/account", dependencies=LOGIN)
    async def account(request: Request):
        """登录后的账号状态：是否还在用初始密码（初始密码写在配置里，必须改掉）。"""
        user = current_user(request)
        if user not in default_pwd_cache:
            u = db.one("SELECT * FROM users WHERE username=?", (user,))
            calc = await hash_async(str(cfg.get("web.initial_password", "trade@2026")), u["salt"]) if u else None
            if calc is not None:
                default_pwd_cache[user] = hmac.compare_digest(calc.encode(), str(u["pwd_hash"]).encode())
        return {"user": user, "default_password": bool(default_pwd_cache.get(user, False)),
                "session_hours": session_hours(cfg)}

    @app.post("/api/password", dependencies=LOGIN)
    async def change_password(request: Request):
        user = current_user(request)
        ip = client_key(request)
        wait = login_guard.retry_after(ip)
        if wait:
            return _err(429, f"尝试次数过多，请 {math.ceil(wait / 60)} 分钟后再试", wait)
        body = await read_json(request, MAX_LOGIN_BODY)
        u = db.one("SELECT * FROM users WHERE username=?", (user,))
        if body is None or not u:
            return _err(400, "请求格式错误")
        stamp = login_guard.attempt(ip)
        calc = await hash_async(str(body.get("old") or "")[:256], u["salt"])
        if calc is None:
            login_guard.forget(ip, stamp)
            return _err(429, "服务繁忙，请稍后再试", 2)
        if not hmac.compare_digest(calc.encode(), str(u["pwd_hash"]).encode()):
            return _err(400, "旧密码错误")
        login_guard.success(ip, stamp)
        new = str(body.get("new") or "")
        if len(new) < 8:
            return _err(400, "新密码至少8位")
        if len(new) > 256:
            return _err(400, "新密码过长")
        if new == str(cfg.get("web.initial_password", "trade@2026")):
            return _err(400, "不能使用初始密码")
        salt = secrets.token_hex(16)
        new_hash = await hash_async(new, salt)
        if new_hash is None:
            return _err(429, "服务繁忙，请稍后再试", 2)
        db.execute("UPDATE users SET pwd_hash=?, salt=? WHERE username=?", (new_hash, salt, user))
        default_pwd_cache.pop(user, None)
        rotate_secret(db)                              # 所有旧会话(所有设备)立即失效
        for ws, authed in list(ws_clients.items()):    # 已连着的登录态推送也一并断开，让它们重连重新鉴权
            if authed:
                ws_clients.pop(ws, None)
                t = asyncio.create_task(_close_quietly(ws))
                bg_tasks.add(t)
                t.add_done_callback(bg_tasks.discard)
        db.log("info", "web", "password changed; all sessions revoked")
        resp = JSONResponse({"ok": True})
        set_session(resp, request, user)               # 当前浏览器换发新会话
        return resp

    @app.get("/api/status")
    async def status(request: Request):
        def produce(anon: bool):
            eng = APP_STATE["engine"]
            return eng.status() if eng else {"error": "engine not running"}
        return public(request, ("status",), 5, produce)

    @app.get("/api/signals")
    async def signals(request: Request, limit: int = 100, kind: str = ""):
        if not _TOKEN_RE.fullmatch(kind):
            return _err(400, "参数不合法")
        # 负数在 SQLite 里 = 不限行数，必须夹住；匿名上限更低
        lim = _clamp(limit, 1, 500 if current_user(request) else 200)

        def produce(anon: bool):
            sql = "SELECT * FROM signals"
            args: list = []
            if kind:
                sql += " WHERE kind=?"
                args.append(kind)
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(lim)
            rows = [dict(r) for r in db.query(sql, args)]
            return [public_signal(r) for r in rows] if anon else rows
        return public(request, ("signals", lim, kind), 5, produce)

    @app.get("/api/trades")
    async def trades(request: Request, track: str = "", result: str = "", tf: str = "",
                     state: str = "", limit: int = 500):
        """track 空=全部策略；tf 空=全部级别；state: 生命周期 try/ok/fail；result: open/closed。"""
        if not _TOKEN_RE.fullmatch(track) or not _TF_RE.fullmatch(tf):
            return _err(400, "参数不合法")
        lim = _clamp(limit, 1, 2000 if current_user(request) else 200)
        state = state if state in ("try", "ok", "fail") else ""
        result = result if result in ("open", "closed") else ""

        def produce(anon: bool):
            sql = ("SELECT pt.*, s.state AS sig_state, s.extra AS sig_extra FROM paper_trades pt "
                   "LEFT JOIN signals s ON s.id=pt.signal_id WHERE 1=1")
            args: list = []
            if track:
                sql += " AND pt.track=?"
                args.append(track)
            if tf:
                sql += " AND pt.tf=?"
                args.append(tf)
            if state:
                sql += " AND s.state=?"
                args.append(state)
            if result == "open":
                sql += " AND pt.result='open'"
            elif result == "closed":
                sql += " AND pt.result IN ('tp','sl','rev')"
            sql += " ORDER BY pt.id DESC LIMIT ?"
            args.append(lim)
            rows = [dict(r) for r in db.query(sql, args)]
            return [public_trade(r) for r in rows] if anon else rows
        return public(request, ("trades", track, result, tf, state, lim), 5, produce)

    @app.get("/api/stats")
    async def stats(request: Request):
        def produce(anon: bool):
            eng = APP_STATE["engine"]
            if eng:
                return {"rr5": eng.paper.stats("rr5"), "rr25": eng.paper.stats("rr25")}
            from app.engine.paper import PaperBroker
            pb = PaperBroker(cfg, db)
            return {"rr5": pb.stats("rr5"), "rr25": pb.stats("rr25")}
        return public(request, ("stats",), 15, produce)

    @app.get("/api/strategy_stats")
    async def strategy_stats(request: Request):
        def produce(anon: bool):
            from app.web.strategy_stats import build_strategy_stats
            rows = db.query(
                "SELECT pt.track, pt.result, pt.pnl, pt.pnl_r, s.extra AS sig_extra "
                "FROM paper_trades pt LEFT JOIN signals s ON s.id=pt.signal_id ORDER BY pt.id"
            )
            return build_strategy_stats([dict(r) for r in rows])
        return public(request, ("strategy_stats",), 15, produce)   # 全表扫描，匿名缓存久一点

    # ---------- 关注列表 Watchlist (P4) ----------
    @app.get("/api/watchlist")
    async def watch_list(request: Request):
        return public(request, ("watchlist",), 10, lambda anon: [dict(r) for r in db.list_watch()])

    @app.post("/api/watchlist", dependencies=LOGIN)
    async def watch_add(request: Request):
        b = await request.json()
        sym = (b.get("symbol") or "").upper().strip()
        if not sym:
            return JSONResponse({"error": "缺少币种"}, status_code=400)
        if not sym.endswith("USDT"):
            sym += "USDT"
        db.add_watch(sym, b.get("note", ""), b.get("source", "manual"))
        eng = APP_STATE.get("engine")
        if eng:
            try:
                await eng.refresh_universe()
                await eng.backfill_all(only=[sym])
                await eng.ws.start(eng.symbols, cfg.timeframes)
            except Exception:
                log.exception("watch add refresh failed")
        return {"ok": True, "symbol": sym}

    @app.post("/api/watchlist/remove", dependencies=LOGIN)
    async def watch_remove(request: Request):
        b = await request.json()
        db.remove_watch((b.get("symbol") or "").upper().strip())
        return {"ok": True}

    # ---------- 大盘观点 (P2a，提阿非罗每日例程写入) ----------
    def _macro_token() -> str:
        t = db.get_settings().get("macro_token")
        if not t:
            t = secrets.token_urlsafe(24)
            db.set_setting("macro_token", t)
        return t

    @app.get("/api/macro")
    async def macro_get(request: Request):
        def produce(anon: bool):
            s = db.get_settings()
            return {"direction": s.get("macro_view_direction", "neutral"),
                    "note": s.get("macro_view_note", ""),
                    "at": int(s.get("macro_view_at", 0) or 0)}
        return public(request, ("macro",), 10, produce)

    @app.post("/api/macro")
    async def macro_set(request: Request):
        # 鉴权：登录会话 或 ?token=（供每日云例程无人值守写入）
        if not current_user(request):
            ip = client_key(request)
            wait = login_guard.retry_after(ip)         # token 猜错也算失败，与登录共用限流
            if wait:
                return _err(429, "尝试次数过多，请稍后再试", wait)
            given = request.query_params.get("token") or ""
            if not hmac.compare_digest(given.encode(), _macro_token().encode()):
                login_guard.attempt(ip)
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        b = await request.json()
        d = b.get("direction", "neutral")
        if d not in ("long", "short", "neutral"):
            return JSONResponse({"error": "direction 须为 long/short/neutral"}, status_code=400)
        db.set_setting("macro_view_direction", d)
        db.set_setting("macro_view_note", str(b.get("note", ""))[:500])
        db.set_setting("macro_view_at", str(int(time.time())))
        eng = APP_STATE.get("engine")
        if eng:
            eng.signal_engine.load_macro(db)
        db.log("info", "macro", f"set {d} by {b.get('source', 'api')}: {b.get('note', '')[:60]}")
        return {"ok": True}

    @app.get("/api/macro_token", dependencies=LOGIN)
    async def macro_token_get(request: Request):
        return {"token": _macro_token()}

    # ---------- 预演 Playbook ----------
    @app.get("/api/playbooks")
    async def playbooks_list(request: Request, status: str = ""):
        if not _TOKEN_RE.fullmatch(status):
            return _err(400, "参数不合法")
        return public(request, ("playbooks", status), 10,
                      lambda anon: [dict(r) for r in db.list_playbooks(status)])

    @app.post("/api/playbooks", dependencies=LOGIN)
    async def playbook_create(request: Request):
        b = await request.json()
        if not b.get("symbol"):
            return JSONResponse({"error": "缺少币种"}, status_code=400)
        def num(x):
            try:
                return float(x) if x not in (None, "") else None
            except (ValueError, TypeError):
                return None
        pid = db.insert_playbook({
            "symbol": b["symbol"].upper().strip(), "tf": b.get("tf", ""),
            "direction": b.get("direction", "long"), "title": b.get("title", ""),
            "entry": num(b.get("entry")), "tp": num(b.get("tp")), "sl": num(b.get("sl")),
            "trigger_type": b.get("trigger_type", "price_reach"),
            "trigger_price": num(b.get("trigger_price")), "source": "manual",
        })
        db.log("info", "playbook", f"created #{pid} {b['symbol']}")
        return {"ok": True, "id": pid}

    @app.post("/api/playbooks/{pid}", dependencies=LOGIN)
    async def playbook_update(pid: int, request: Request):
        b = await request.json()
        allowed = {k: b[k] for k in ("status", "title", "entry", "tp", "sl",
                                     "trigger_price", "trigger_type", "direction") if k in b}
        if allowed:
            db.update_playbook(pid, **allowed)
        return {"ok": True}

    @app.post("/api/backtest", dependencies=LOGIN)
    async def backtest_start(request: Request):
        """启动回测（后台任务，GET /api/backtest 轮询进度/结果）。"""
        bt = APP_STATE.get("backtest") or {}
        if bt.get("running"):
            return JSONResponse({"error": "已有回测在运行"}, status_code=409)
        body = await request.json()
        days = max(1, min(int(body.get("days", 7)), 60))
        tfs = [t for t in (body.get("tfs") or ["5m", "15m", "1h", "4h"])
               if t in ("5m", "15m", "1h", "4h")]
        eng = APP_STATE["engine"]
        if not eng:
            return JSONResponse({"error": "engine 未运行"}, status_code=400)
        symbols = list(eng.symbols)
        state = {"running": True, "progress": "启动中…", "result": None, "started_at": int(time.time())}
        APP_STATE["backtest"] = state

        def prog(done, total, msg):
            state["progress"] = f"{done}/{total} ({msg})"

        async def job():
            from app.engine.backtest import run_backtest
            try:
                state["result"] = await run_backtest(cfg, eng.rest, symbols, tfs, days, prog)
                db.log("info", "backtest", f"{days}d {tfs} 完成: {state['result']['total']}")
            except Exception as e:
                log.exception("backtest failed")
                state["result"] = {"error": str(e)}
            finally:
                state["running"] = False
        asyncio.get_event_loop().create_task(job())
        return {"ok": True}

    @app.get("/api/backtest")
    async def backtest_status(request: Request):
        def produce(anon: bool):
            bt = APP_STATE.get("backtest")
            if not bt:
                return {"running": False, "result": None}
            result = bt.get("result")
            if anon and isinstance(result, dict):
                if "error" in result:                  # 异常原文可能带内部细节，匿名只给笼统提示
                    result = {"error": "回测失败"}
                else:                                  # 参数快照属于「参数配置」，登录后才可见
                    result = {k: v for k, v in result.items() if k != "params"}
            return {"running": bt["running"], "progress": bt.get("progress"),
                    "result": result, "started_at": bt.get("started_at")}
        return public(request, ("backtest",), 3, produce)

    @app.get("/api/stats_by_tf")
    async def stats_by_tf(request: Request):
        """按级别(可再按轨道)统计胜率，验证哪个级别准确率最高。"""
        def produce(anon: bool):
            rows = db.query(
                "SELECT tf, track, COUNT(*) n, SUM(result='tp') wins, "
                "ROUND(SUM(pnl),2) pnl, ROUND(AVG(pnl_r),3) avg_r "
                "FROM paper_trades WHERE result IN ('tp','sl') GROUP BY tf, track ORDER BY tf, track")
            agg = db.query(
                "SELECT tf, COUNT(*) n, SUM(result='tp') wins, "
                "ROUND(SUM(pnl),2) pnl, ROUND(AVG(pnl_r),3) avg_r, "
                "SUM(result IS NULL OR result='open') open_cnt "
                "FROM paper_trades GROUP BY tf ORDER BY tf")
            return {"by_tf_track": [dict(r) for r in rows], "by_tf": [dict(r) for r in agg]}
        return public(request, ("stats_by_tf",), 30, produce)

    @app.get("/api/equity")
    async def equity(request: Request, track: str = "rr25"):
        if not _TOKEN_RE.fullmatch(track):
            return _err(400, "参数不合法")

        def produce(anon: bool):
            if anon:                                   # 匿名只给最近 3000 个结算点，防无上限响应
                rows = db.query("SELECT ts, equity FROM equity_curve WHERE track=? "
                                "ORDER BY ts DESC LIMIT 3000", (track,))
                return [dict(r) for r in reversed(rows)]
            return [dict(r) for r in db.query(
                "SELECT ts, equity FROM equity_curve WHERE track=? ORDER BY ts", (track,))]
        return public(request, ("equity", track), 30, produce)

    _TF_MIN = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}

    @app.get("/api/klines")
    async def klines(request: Request, symbol: str, tf: str = "15m", limit: int = 300):
        symbol = symbol.upper().strip()
        if not _SYMBOL_RE.fullmatch(symbol) or (tf not in cfg.timeframes and tf not in _TF_MIN):
            return _err(400, "参数不合法")
        lim = _clamp(limit, 1, 500)

        def produce(anon: bool):
            if tf in cfg.timeframes:                      # 已存储的级别直接取
                kl = [dict(r) for r in db.get_klines(symbol, tf, lim)]
            else:                                         # 未存储(如30m)→ 从5m聚合
                from app.engine.chan import aggregate
                mult = max(2, _TF_MIN.get(tf, 30) // 5)
                base = [dict(r) for r in db.get_klines(symbol, "5m", min(lim * mult, 1500))]
                kl = aggregate(base, mult)[-lim:]
            if anon and not kl:                           # 库里没有的币：不再去扫信号表
                return {"klines": [], "signals": []}
            sigs = db.query(                              # 仅该级别自身的信号(30m无信号→价格线仍由前端ref画)
                "SELECT id, created_at, direction, kind, entry, sl, tp, rr, status, state, vol_ratio, extra FROM signals "
                "WHERE symbol=? AND tf=? ORDER BY id DESC LIMIT 50", (symbol, tf))
            sig_rows = [dict(r) for r in sigs]
            return {"klines": kl, "signals": [public_signal(r) for r in sig_rows] if anon else sig_rows}
        return public(request, ("klines", symbol, tf, lim), 5, produce)

    @app.get("/api/live_stats", dependencies=LOGIN)
    async def live_stats(days: int = 30):
        """实盘真实盈亏/胜率/手续费(取币安收益流水)。paper模式返回 live=False。"""
        eng = APP_STATE.get("engine")
        if not eng or cfg.mode != "live":
            return {"live": False}
        days = _clamp(days, 1, 90)
        # 只统计本系统下过的单(orders表的币种+首单时间), 排除账户自带的历史/手动交易
        orow = db.query("SELECT symbol, MIN(created_at) mn FROM orders GROUP BY symbol")
        zero = {"live": True, "trades": 0, "wins": 0, "win_rate": 0, "realized": 0,
                "commission": 0, "funding": 0, "net": 0, "avg": 0, "note": "本系统尚无实盘成交"}
        if not orow:
            return zero
        bot_syms = {r["symbol"] for r in orow}
        first_ts = min(r["mn"] for r in orow)
        start = max(int((time.time() - days * 86400) * 1000), first_ts * 1000)
        try:
            inc = await eng.rest.income(start, 1000)
        except Exception as e:
            return {"live": True, "error": str(e)[:200]}
        inc = [x for x in inc if x.get("symbol") in bot_syms]   # 只算本系统交易过的币
        pnls = [float(x["income"]) for x in inc if x.get("incomeType") == "REALIZED_PNL"]
        comm = sum(float(x["income"]) for x in inc if x.get("incomeType") == "COMMISSION")
        fund = sum(float(x["income"]) for x in inc if x.get("incomeType") == "FUNDING_FEE")
        realized = sum(pnls)
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        return {"live": True, "days": days, "trades": n, "wins": wins,
                "win_rate": round(wins / n * 100, 1) if n else 0,
                "realized": round(realized, 4), "commission": round(comm, 4), "funding": round(fund, 4),
                "net": round(realized + comm + fund, 4),
                "avg": round((realized + comm + fund) / n, 4) if n else 0}

    @app.get("/api/positions", dependencies=LOGIN)
    async def positions():
        """实盘真实持仓(live模式)+ 关联开仓策略。paper模式返回空。"""
        eng = APP_STATE.get("engine")
        if not eng or cfg.mode != "live":
            return {"live": False, "positions": []}
        try:
            raw = await eng.rest.position_risk()
        except Exception as e:
            return {"live": True, "error": str(e)[:200], "positions": []}
        out = []
        for p in raw:
            amt = float(p.get("positionAmt") or 0)
            if amt == 0:
                continue
            sym = p["symbol"]
            sig = db.one("SELECT tf, extra FROM signals WHERE symbol=? AND status='confirmed' "
                         "ORDER BY id DESC LIMIT 1", (sym,))
            strat, tf = "?", ""
            if sig:
                tf = sig["tf"]
                try:
                    strat = (json.loads(sig["extra"] or "{}")).get("path", "?")
                except (ValueError, TypeError):
                    pass
            out.append({
                "symbol": sym, "direction": "long" if amt > 0 else "short", "amt": abs(amt),
                "entry": float(p.get("entryPrice") or 0), "mark": float(p.get("markPrice") or 0),
                "pnl": round(float(p.get("unRealizedProfit") or 0), 4),
                "leverage": p.get("leverage"), "strategy": strat, "tf": tf,
            })
        return {"live": True, "positions": out}

    _bool = lambda v: str(v).lower() in ("1", "true", "yes")
    EDITABLE = {
        # 策略 chan_bi: 笔 + 底分型(最强/标准) + 放量 + 背驰 + 停顿
        "chan.bi_min_bars": int,           # 一笔至少N根合并K(去包含)
        "chan.stall_max_gap": int,         # 分型后N根内出停顿K才确认
        "chan.fractal_vol_mult": float,    # 底分型前2根放量倍数(x前10根均量)
        "chan.fractal_vol_ma": int,        # 放量均量回看根数
        "chan.require_divergence": _bool,  # 一买/一卖必须背驰
        "chan.strong_reversal_15m": _bool, # 仅15m:只认强反转形态
        "chan.reversal_body_ratio": float, # 强反转:右K实体≥振幅×该比例
        "chan.mtf_tol_pct": float,         # 多级别:停顿与高级别分型价位容差%
        "spring.min_rr": float,            # 最低盈亏比门槛(低于不进场)
        "spring.tp_lookback": int,         # 止盈回看根数
        "signal.sl_buffer_pct": float,     # 止损缓冲%
        "spring.btc_filter": _bool,        # 回测BTC大盘过滤
        # 风控与通用
        "risk.account_equity": float, "risk.risk_pct": float,
        "risk.max_positions": int, "risk.leverage": int,
        "universe.min_quote_volume_24h": float,
        "trade_direction": str,            # both/long/short: 只交易某方向
        "live.auto_trade": _bool,          # 自动下单(免TG二次确认)
        "live.fixed_margin_pct": float,    # 单笔保证金占账户本金%, 名义=本金×比例×杠杆
        "live.fixed_margin_u": float,      # 单笔固定保证金U(名义=保证金×杠杆, 优先)
        "live.fixed_notional_u": float,    # 单笔固定名义额U(margin=0时才用)
        "live.max_positions": int,         # 实盘最大持仓
        "live.max_loss_pct": float,        # 亏损熔断%
        "mode": str,
    }

    @app.get("/api/settings", dependencies=LOGIN)
    async def get_settings():
        return {k: cfg.get(k) for k in EDITABLE}

    @app.post("/api/settings", dependencies=LOGIN)
    async def set_settings(request: Request):
        body = await request.json()
        applied = {}
        for k, v in body.items():
            if k not in EDITABLE:
                continue
            if k == "mode" and v not in ("paper", "live"):
                return JSONResponse({"error": "mode 必须是 paper 或 live"}, status_code=400)
            if k == "trade_direction" and v not in ("both", "long", "short"):
                return JSONResponse({"error": "trade_direction 必须是 both/long/short"}, status_code=400)
            try:
                tv = EDITABLE[k](v)
            except (ValueError, TypeError):
                return JSONResponse({"error": f"{k} 类型错误"}, status_code=400)
            cfg.set_override(k, tv)
            db.set_setting(k, json.dumps(tv))
            applied[k] = tv
        db.log("info", "web", f"settings updated: {applied}")
        return {"ok": True, "applied": applied}

    @app.get("/api/events", dependencies=LOGIN)
    async def events(limit: int = 100):
        return [dict(r) for r in db.query(
            "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (_clamp(limit, 1, 500),))]

    @app.websocket("/api/ws")
    async def ws_endpoint(ws: WebSocket):
        """只推不收：服务端推 signal / trade_close；客户端发来的内容一律当心跳丢弃。
        匿名可连，但拿到的是脱敏后的事件（与白名单 GET 同一口径），且有连接数与发包频率上限。"""
        user = auth_user(ws)
        if user and not _origin_ok(ws, cfg):               # 跨站握手：不认它的 cookie，按匿名处理
            user = None
        ip = client_key(ws)
        if not user:
            n_anon = sum(1 for a in ws_clients.values() if not a)
            if n_anon >= max_anon_ws or ws_anon_by_ip[ip] >= max_anon_ws_per_ip:
                await ws.close(code=1013)                  # 匿名连接已满，稍后再试
                return
            ws_anon_by_ip[ip] += 1
        try:
            await ws.accept()
            ws_clients[ws] = bool(user)
            win_start, n_msgs = time.monotonic(), 0
            while True:
                msg = await ws.receive()
                if msg.get("type") != "websocket.receive":
                    break                                  # 断开
                if user:
                    continue                               # 客户端心跳
                now = time.monotonic()
                if now - win_start >= 60:
                    win_start, n_msgs = now, 0
                n_msgs += 1
                size = len(msg.get("text") or "") + len(msg.get("bytes") or b"")
                if size > 64 or n_msgs > 30:               # 匿名只该每 30s 发一个 ping
                    await ws.close(code=1008)
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            log.debug("ws closed abnormally", exc_info=True)
        finally:
            ws_clients.pop(ws, None)
            if not user:
                ws_anon_by_ip[ip] -= 1
                if ws_anon_by_ip[ip] <= 0:
                    ws_anon_by_ip.pop(ip, None)

    # 订单流页的只读接口（/api/flow/*，匿名可 GET，见上面的白名单）。处理函数只读内存缓存/本地库，
    # 后台任务由 app.main 在 engine.start() 之后另行启动。注册失败只记日志：不能连累控制台其余功能。
    try:
        from app.flow.api import register_flow
        register_flow(app, cfg, db, engine)
    except Exception:
        log.exception("订单流路由注册失败（控制台其余功能不受影响）")

    # ---- /api/flow/* (订单流) 路由请在这一行之上注册：必须排在静态挂载之前才匹配得到 ----

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/", StaticFiles(directory=STATIC), name="static")
    return app


def load_setting_overrides(cfg, db) -> None:
    """启动时把 settings 表里的覆盖值灌回 cfg。"""
    for k, v in db.get_settings().items():
        if k.startswith("_"):
            continue
        try:
            cfg.set_override(k, json.loads(v))
        except (ValueError, TypeError):
            cfg.set_override(k, v)
