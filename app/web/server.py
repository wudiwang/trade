"""Web 控制台：登录鉴权 + 信号/持仓/统计/配置 API + WebSocket 实时推送。"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

log = logging.getLogger("web")
STATIC = Path(__file__).parent / "static"

APP_STATE: dict = {"engine": None, "bot": None, "cfg": None, "db": None}

# ---------- 密码与会话 ----------

def hash_pwd(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()


def _secret(db) -> str:
    s = db.get_settings().get("_session_secret")
    if not s:
        s = secrets.token_hex(32)
        db.set_setting("_session_secret", s)
    return s


def make_token(db, username: str, hours: int) -> str:
    exp = int(time.time()) + hours * 3600
    payload = f"{username}|{exp}"
    sig = hmac.new(_secret(db).encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def check_token(db, token: str) -> str | None:
    try:
        username, exp, sig = token.rsplit("|", 2)
        payload = f"{username}|{exp}"
        good = hmac.new(_secret(db).encode(), payload.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, good) and int(exp) > time.time():
            return username
    except Exception:
        pass
    return None


def ensure_admin(cfg, db) -> None:
    if not db.one("SELECT username FROM users LIMIT 1"):
        salt = secrets.token_hex(16)
        u = cfg.get("web.username", "admin")
        db.execute(
            "INSERT INTO users (username, pwd_hash, salt, created_at) VALUES (?,?,?,?)",
            (u, hash_pwd(cfg.get("web.initial_password", "trade@2026"), salt), salt, int(time.time())),
        )
        log.info("created initial web user '%s'", u)


# ---------- 应用 ----------

def create_app(cfg, db, engine=None, bot=None) -> FastAPI:
    APP_STATE.update(engine=engine, bot=bot, cfg=cfg, db=db)
    ensure_admin(cfg, db)
    app = FastAPI(docs_url=None, redoc_url=None)
    ws_clients: set[WebSocket] = set()

    # 引擎事件 → 网页实时推送
    async def broadcast(kind: str, data: dict):
        dead = []
        for ws in ws_clients:
            try:
                await ws.send_text(json.dumps({"kind": kind, "data": data}, ensure_ascii=False, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_clients.discard(ws)

    if engine:
        async def on_sig(sid, s):
            await broadcast("signal", {"id": sid, **s.to_db()})
        async def on_close(trade):
            await broadcast("trade_close", trade)
        engine.signal_subscribers.append(on_sig)
        engine.trade_close_subscribers.append(on_close)

    def auth_user(request: Request) -> str | None:
        tok = request.cookies.get("session", "")
        return check_token(db, tok) if tok else None

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        path = request.url.path
        open_paths = ("/api/login", "/login.html", "/style.css", "/favicon.ico")
        if path.startswith("/api") and path != "/api/login":
            if not auth_user(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        elif path == "/" or path.endswith(".html") or path.endswith(".js"):
            if path not in open_paths and not auth_user(request):
                return FileResponse(STATIC / "login.html")
        resp = await call_next(request)
        # 前端资源禁缓存：部署后用户打开即拿到最新JS/HTML/CSS
        if path == "/" or path.endswith((".html", ".js", ".css")):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    # ---------- API ----------
    @app.post("/api/login")
    async def login(request: Request):
        body = await request.json()
        u = db.one("SELECT * FROM users WHERE username=?", (body.get("username", ""),))
        if not u or hash_pwd(body.get("password", ""), u["salt"]) != u["pwd_hash"]:
            await asyncio.sleep(1)  # 抗爆破
            return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
        tok = make_token(db, u["username"], cfg.get("web.session_hours", 72))
        resp = JSONResponse({"ok": True})
        resp.set_cookie("session", tok, httponly=True, samesite="lax",
                        max_age=cfg.get("web.session_hours", 72) * 3600)
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("session")
        return resp

    @app.post("/api/password")
    async def change_password(request: Request):
        user = auth_user(request)
        body = await request.json()
        u = db.one("SELECT * FROM users WHERE username=?", (user,))
        if hash_pwd(body.get("old", ""), u["salt"]) != u["pwd_hash"]:
            return JSONResponse({"error": "旧密码错误"}, status_code=400)
        new = body.get("new", "")
        if len(new) < 8:
            return JSONResponse({"error": "新密码至少8位"}, status_code=400)
        salt = secrets.token_hex(16)
        db.execute("UPDATE users SET pwd_hash=?, salt=? WHERE username=?",
                   (hash_pwd(new, salt), salt, user))
        return {"ok": True}

    @app.get("/api/status")
    async def status():
        eng = APP_STATE["engine"]
        return eng.status() if eng else {"error": "engine not running"}

    @app.get("/api/signals")
    async def signals(limit: int = 100, kind: str = ""):
        sql = "SELECT * FROM signals"
        args: list = []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(limit, 500))
        return [dict(r) for r in db.query(sql, args)]

    @app.get("/api/trades")
    async def trades(track: str = "", result: str = "", limit: int = 500):
        """track 空=全部策略；result: ''=全部, open=持仓中, closed=已结束。"""
        sql = "SELECT * FROM paper_trades WHERE 1=1"
        args: list = []
        if track:
            sql += " AND track=?"
            args.append(track)
        if result == "open":
            sql += " AND result='open'"
        elif result == "closed":
            sql += " AND result IN ('tp','sl')"
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(min(limit, 2000))
        return [dict(r) for r in db.query(sql, args)]

    @app.get("/api/stats")
    async def stats():
        eng = APP_STATE["engine"]
        if eng:
            return {"rr5": eng.paper.stats("rr5"), "rr25": eng.paper.stats("rr25")}
        from app.engine.paper import PaperBroker
        pb = PaperBroker(cfg, db)
        return {"rr5": pb.stats("rr5"), "rr25": pb.stats("rr25")}

    # ---------- 关注列表 Watchlist (P4) ----------
    @app.get("/api/watchlist")
    async def watch_list():
        return [dict(r) for r in db.list_watch()]

    @app.post("/api/watchlist")
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

    @app.post("/api/watchlist/remove")
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
    async def macro_get():
        s = db.get_settings()
        return {"direction": s.get("macro_view_direction", "neutral"),
                "note": s.get("macro_view_note", ""),
                "at": int(s.get("macro_view_at", 0) or 0)}

    @app.post("/api/macro")
    async def macro_set(request: Request):
        # 鉴权：登录会话 或 ?token=（供每日云例程无人值守写入）
        if not auth_user(request) and request.query_params.get("token") != _macro_token():
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

    @app.get("/api/macro_token")
    async def macro_token_get(request: Request):
        if not auth_user(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return {"token": _macro_token()}

    # ---------- 预演 Playbook ----------
    @app.get("/api/playbooks")
    async def playbooks_list(status: str = ""):
        return [dict(r) for r in db.list_playbooks(status)]

    @app.post("/api/playbooks")
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

    @app.post("/api/playbooks/{pid}")
    async def playbook_update(pid: int, request: Request):
        b = await request.json()
        allowed = {k: b[k] for k in ("status", "title", "entry", "tp", "sl",
                                     "trigger_price", "trigger_type", "direction") if k in b}
        if allowed:
            db.update_playbook(pid, **allowed)
        return {"ok": True}

    @app.post("/api/backtest")
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
    async def backtest_status():
        bt = APP_STATE.get("backtest")
        if not bt:
            return {"running": False, "result": None}
        return {"running": bt["running"], "progress": bt.get("progress"),
                "result": bt.get("result"), "started_at": bt.get("started_at")}

    @app.get("/api/stats_by_tf")
    async def stats_by_tf():
        """按级别(可再按轨道)统计胜率，验证哪个级别准确率最高。"""
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

    @app.get("/api/equity")
    async def equity(track: str = "rr25"):
        return [dict(r) for r in db.query(
            "SELECT ts, equity FROM equity_curve WHERE track=? ORDER BY ts", (track,))]

    @app.get("/api/klines")
    async def klines(symbol: str, tf: str = "15m", limit: int = 300):
        rows = db.get_klines(symbol, tf, min(limit, 500))
        sigs = db.query(
            "SELECT id, created_at, direction, kind, entry, sl, tp, rr, status, extra FROM signals "
            "WHERE symbol=? AND tf=? ORDER BY id DESC LIMIT 50", (symbol, tf))
        return {"klines": [dict(r) for r in rows], "signals": [dict(r) for r in sigs]}

    _bool = lambda v: str(v).lower() in ("1", "true", "yes")
    EDITABLE = {
        # 策略V4: 破位 + 底分型（仅保留本策略参数）
        "spring.vol_mult": float,          # 破位K量倍数(x均量)
        "spring.newlow_lookback": int,     # 破位回看根数(创新低)
        "spring.body_min": float,          # 破位K实体占比
        "spring.fractal_window": int,      # 底分型窗口(根)
        "spring.buy2_window": int,         # 二买跟踪根数
        "spring.maink_range_atr": float,   # 主力K振幅(xATR)
        "spring.tp_lookback": int,         # 止盈回看根数
        "spring.min_rr": float,            # 最低盈亏比门槛
        "spring.btc_filter": _bool,        # BTC大盘过滤
        "signal.sl_buffer_pct": float,     # 止损缓冲%
        # 风控与通用
        "risk.account_equity": float, "risk.risk_pct": float,
        "risk.max_positions": int, "risk.leverage": int,
        "universe.min_quote_volume_24h": float,
        "mode": str,
    }

    @app.get("/api/settings")
    async def get_settings():
        return {k: cfg.get(k) for k in EDITABLE}

    @app.post("/api/settings")
    async def set_settings(request: Request):
        body = await request.json()
        applied = {}
        for k, v in body.items():
            if k not in EDITABLE:
                continue
            if k == "mode" and v not in ("paper", "live"):
                return JSONResponse({"error": "mode 必须是 paper 或 live"}, status_code=400)
            try:
                tv = EDITABLE[k](v)
            except (ValueError, TypeError):
                return JSONResponse({"error": f"{k} 类型错误"}, status_code=400)
            cfg.set_override(k, tv)
            db.set_setting(k, json.dumps(tv))
            applied[k] = tv
        db.log("info", "web", f"settings updated: {applied}")
        return {"ok": True, "applied": applied}

    @app.get("/api/events")
    async def events(limit: int = 100):
        return [dict(r) for r in db.query(
            "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (min(limit, 500),))]

    @app.websocket("/api/ws")
    async def ws_endpoint(ws: WebSocket):
        tok = ws.cookies.get("session", "")
        if not check_token(db, tok):
            await ws.close(code=4401)
            return
        await ws.accept()
        ws_clients.add(ws)
        try:
            while True:
                await ws.receive_text()   # 客户端心跳
        except WebSocketDisconnect:
            pass
        finally:
            ws_clients.discard(ws)

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
