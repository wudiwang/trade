"""本地回测可视化看图器(多策略版,用户 2026-06-16)。

读 .btcache/ 缓存 + 跑「策略注册表」里所有策略 → 信号表(带策略名)。
点一条 → 画当时K线(蜡烛+量)+ 入场/止损/止盈线 + 锚点/入场标记,按盈亏上色;
侧栏展示该信号所属策略的「思路逻辑」。

运行:  .venv/Scripts/python scripts/bt_viewer.py --days 30
浏览器:http://127.0.0.1:8530   纯本地、只读缓存。
"""
import argparse
import base64
import bisect
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from functools import lru_cache

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bt_registry as R

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

SIGNALS = []
META = {}
STATS = {}
DAYS = 30

# 策略详情元数据(看图器自有, 不改动并发编辑中的 bt_registry.py)。
DETAIL = {
    "smallbig": {"desc": "纯5m量能高潮反转(持续放量恐慌后的第一根反弹)",
                 "idea": "深跌中量能不断放大→巨量恐慌出尽→缩量→第一根反弹K进场",
                 "updated": "2026-06-17", "code": "app/engine/strat_smallbig.py",
                 "doc": "docs/agents/notes_smallbig.md"},
    "pullback": {"desc": "5m三笔浅回调二买/二卖",
                 "idea": "下跌笔→反弹笔→再跌不破新低+浅回调≤50%+放量分型",
                 "updated": "2026-06-16", "code": "app/engine/strat_pullback.py", "doc": ""},
    "deepbase": {"desc": "深跌后企稳(1h深跌 + 15m恐慌放量低点企稳)",
                 "idea": "高级别深跌后, 低级别急跌见底+缩量企稳, 抄底反弹",
                 "updated": "2026-06-16", "code": "app/engine/strat_deepbase.py", "doc": ""},
    "reversal": {"desc": "反转战法(弹簧+起跌位收回+二段建仓)",
                 "idea": "爆量标志K插穿→收回起跌位→横盘企稳轻仓→平台分型加仓",
                 "updated": "2026-06-16", "code": "app/engine/strat_reversal.py",
                 "doc": "docs/strat_spring_reclaim.md"},
    "macro_pullback": {"desc": "BTC大趋势下的山寨二买/二卖(威科夫弹簧/UTAD触发)",
                       "idea": "大盘方向+山寨结构二买二卖, 弹簧/UTAD确认入场",
                       "updated": "2026-06-19", "code": "app/engine/macro_pullback.py", "doc": ""},
    "macro_pullback_15m": {"desc": "线上 macro_pullback 策略·15分钟级别",
                           "idea": "同一套二买二卖, 结构+触发都在15m(噪音少于5m)",
                           "updated": "2026-06-26", "code": "app/engine/macro_pullback.py",
                           "doc": "近7天: 383点/34.8%胜/扣费-0.14R(仍负)"},
    "macrofvg": {"desc": "FVG二买二卖: 一买后的上涨一笔必须留下FVG, 二买回落进FVG不跌穿",
                 "idea": "线上二买二卖结构 + FVG确认 + 止损放一买低点 + 固定1:3",
                 "updated": "2026-07-11", "code": "scripts/strat_macrofvg.py",
                 "doc": "30天: 969点/25.0%胜/扣费-0.058R(做多+0.054R但t=0.66, 未证实)"},
}
app = FastAPI()

# 可选 Basic Auth: 只有设了 BT_USER/BT_PASS 才生效(本地裸跑不受影响)。
# 一旦把看图器挂到公网(Cloudflare Tunnel), 必须设 —— 它有 POST /api/label 这种写接口。
BT_USER = os.environ.get("BT_USER", "")
BT_PASS = os.environ.get("BT_PASS", "")


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    if not (BT_USER and BT_PASS):
        return await call_next(request)
    hdr = request.headers.get("authorization", "")
    ok = False
    if hdr.startswith("Basic "):
        try:
            raw = base64.b64decode(hdr[6:]).decode("utf-8", "replace")
            u, _, p = raw.partition(":")
            ok = secrets.compare_digest(u, BT_USER) and secrets.compare_digest(p, BT_PASS)
        except Exception:
            ok = False
    if not ok:
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="bt-viewer"'})
    return await call_next(request)


@app.get("/api/meta")
def api_meta():
    return {"meta": META, "stats": STATS, "detail": DETAIL}


@app.get("/api/cache_status")
def api_cache_status():
    return JSONResponse(R.cache_status(DAYS))


def _agents_data():
    """Agent工作台数据:已注册子Agent(.claude/agents) + 各策略研究笔记/交接(docs/agents)。纯本地, 无外部服务。"""
    import glob as _g
    rows = []
    for f in sorted(_g.glob(os.path.join(R.ROOT, ".claude", "agents", "*.md"))):
        txt = open(f, encoding="utf-8").read()
        name, desc = os.path.basename(f)[:-3], ""
        if txt.startswith("---"):
            fm = txt.split("---", 2)[1]
            for ln in fm.splitlines():
                if ln.startswith("name:"):
                    name = ln.split(":", 1)[1].strip()
                elif ln.startswith("description:"):
                    desc = ln.split(":", 1)[1].strip()
        # 找该agent的笔记/交接
        note_path, note_tail, mtime = "", "", None
        for cand in (f"notes_{name}.md", f"{name}/handoff.md"):
            p = os.path.join(R.ROOT, "docs", "agents", cand)
            if os.path.exists(p):
                note_path = os.path.relpath(p, R.ROOT).replace("\\", "/")
                body = open(p, encoding="utf-8").read().strip()
                note_tail = body[-400:]
                mtime = int(os.path.getmtime(p))
                break
        rows.append({"name": name, "responsibility": desc[:160], "handoff": note_path,
                     "latest_note": note_tail, "note_mtime": mtime})
    # 还没有专属agent定义、但有研究笔记的策略
    for p in sorted(_g.glob(os.path.join(R.ROOT, "docs", "agents", "notes_*.md"))):
        strat = os.path.basename(p)[len("notes_"):-3]
        if any(strat in r["name"] for r in rows):
            continue
        rel = os.path.relpath(p, R.ROOT).replace("\\", "/")
        rows.append({"name": f"researcher:{strat}", "responsibility": f"研究策略 {strat}",
                     "handoff": rel, "latest_note": open(p, encoding="utf-8").read().strip()[-400:],
                     "note_mtime": int(os.path.getmtime(p))})
    return rows


@app.get("/api/agents")
def api_agents():
    return JSONResponse(_agents_data())


LABELS_PATH = os.path.join(R.ROOT, "pattern_cases", "labels.jsonl")


@app.post("/api/label")
async def api_label(request: Request):
    """审美打标:对信号标 good/bad + 理由 → 追加到 pattern_cases/labels.jsonl(审计留痕)。"""
    body = await request.json()
    sid, verdict, reason = body.get("id"), body.get("verdict"), (body.get("reason") or "").strip()
    s = next((x for x in SIGNALS if x["id"] == sid), None)
    if not s or verdict not in ("good", "bad"):
        return JSONResponse({"error": "bad request"}, status_code=400)
    rec = {"labeled_at": int(time.time()), "source": "local-viewer", "strat": s.get("strat"),
           "symbol": s["symbol"], "tf": "15m" if str((META.get(s.get("strat")) or {}).get("tf", "")).startswith("15m") else "5m",
           "signal_time": s["created_at"], "direction": s["direction"],
           "entry": s["entry"], "sl": s["sl"], "tp": s["tp"], "result": s.get("result"),
           "verdict": verdict, "reason": reason}
    os.makedirs(os.path.dirname(LABELS_PATH), exist_ok=True)
    with open(LABELS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True}


@app.get("/api/labels")
def api_labels():
    """返回每个信号的最新标签(键: strat|symbol|signal_time|direction)。"""
    out = {}
    if os.path.exists(LABELS_PATH):
        for line in open(LABELS_PATH, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            out[f"{r['strat']}|{r['symbol']}|{r['signal_time']}|{r['direction']}"] = {"v": r["verdict"], "r": r.get("reason", "")}
    return JSONResponse(out)


@app.post("/api/save_case")
async def save_case(request: Request):
    """把选中信号存为经典/反例案例 → pattern_cases/<strategy>/<symbol>_<ts>.json。"""
    body = await request.json()
    sid, label = body.get("id"), (body.get("label") or "").strip()
    s = next((x for x in SIGNALS if x["id"] == sid), None)
    if not s:
        return JSONResponse({"error": "signal not found"}, status_code=404)
    strat = s.get("strat", "unknown")
    tf = "15m" if str((META.get(strat) or {}).get("tf", "")).startswith("15m") else "5m"
    case = {"symbol": s["symbol"], "strategy": strat, "timeframe": tf,
            "timestamp": s["created_at"], "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
            "result": s.get("result"), "pnl_r": s.get("pnl_r"),
            "markers": {"anchor": s.get("anchor"), "entry_time": s["created_at"],
                        "climaxX": s.get("climaxX"), "movePct": s.get("movePct")},
            "direction": s["direction"], "label": label, "saved_at": int(time.time())}
    d = os.path.join(R.ROOT, "pattern_cases", strat)
    os.makedirs(d, exist_ok=True)
    fname = f"{s['symbol']}_{s['created_at']}.json"
    json.dump(case, open(os.path.join(d, fname), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return {"ok": True, "path": f"pattern_cases/{strat}/{fname}"}


@app.get("/agents", response_class=HTMLResponse)
def agents_page():
    return AGENTS_HTML


@app.get("/ideas", response_class=HTMLResponse)
def ideas_page():
    return IDEAS_HTML


@app.get("/api/ideas")
def api_ideas():
    import idea_lib
    return JSONResponse({"ideas": idea_lib.load_ideas(), "principles": idea_lib.load_principles()})


@app.get("/research/asset")
def research_asset(p: str):
    import idea_lib
    from fastapi.responses import FileResponse
    path = idea_lib.asset_path(p)
    if not path:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path)


# ---- 研究闭环: 策略迭代 → 回测记录 → 标注 (设计见 docs/knowledge/idea_lab.md) ----

@app.get("/api/idea/{slug}")
def api_idea(slug: str):
    import idea_lib
    if not idea_lib.idea_dir(slug):
        return JSONResponse({"error": "not found"}, status_code=404)
    idea = next((x for x in idea_lib.load_ideas() if x["slug"] == slug), None)
    judged = idea_lib.load_judged(slug)          # 跨所有版本, 键=市场点位身份
    bts = idea_lib.load_backtests(slug)
    # 给每条信号挂上"这个点位你以前判过没有" —— 换版本重跑时不该再问你一遍
    for b in bts:
        n_prior = 0
        for s in b.get("signals", []):
            p = judged.get(idea_lib.akey(s["symbol"], s["t"], s["dir"]))
            if p:
                s["prior"] = {"verdict": p["verdict"], "reason": p.get("reason", ""),
                              "from": p.get("from", "")}
                n_prior += 1
        b["n_prior"] = n_prior
    return JSONResponse({
        "idea": idea,
        "desc": idea_lib.load_desc(slug),
        "spec": idea_lib.load_spec(slug),          # 持久化回显, 别让用户以为①失败了
        "charts": idea_lib.load_charts(slug),
        "versions": idea_lib.load_versions(slug),
        "backtests": bts,
        "n_judged": len(judged),
    })


@app.post("/api/idea/new")
async def api_idea_new(request: Request):
    """新建灵感: 传一张图 + 写下你的想法 → 建 research/ideas/<NNN>-.../

    图可以没有时间(那只是右侧动态K线画不出来), 但想法不能空 —— 灵感库存的是思考链路, 不是图片墙。
    """
    import idea_lib
    b = await request.json()
    note = (b.get("note") or "").strip()
    if not note:
        return JSONResponse({"error": "写下你看到了什么 —— 没有想法的图只是张图, 不是灵感。"},
                            status_code=400)
    r = idea_lib.new_idea(b.get("title", ""), note, b.get("data", ""),
                          b.get("symbol", ""), b.get("tf", "5m"), b.get("center", 0))
    return {"ok": True, **r}


@app.post("/api/idea/{slug}/chart")
async def api_idea_chart(slug: str, request: Request):
    """原始图上传(粘贴或选文件)。symbol/tf 由用户给, 不做图像识别。"""
    import idea_lib
    b = await request.json()
    rel = idea_lib.save_chart(slug, b.get("data", ""), b.get("symbol", ""),
                              b.get("tf", "5m"), b.get("center", 0), b.get("note", ""))
    if not rel:
        return JSONResponse({"error": "保存失败(灵感不存在/不是图片/超过12MB)"}, status_code=400)
    return {"ok": True, "path": rel}


def _register_fvg1m():
    """把灵感001(5m FVG回踩 + 1m缠论底分型入场)注册成可一键回测的 scanner。

    代码一直躺在 scripts/strat_fvg1m.py, 只是没接进 SCANS。它需要 1m 数据 ——
    本地只有 198 个币有 1m 缓存, 所以它的样本天然比 5m 策略小, 这不是 bug。
    """
    try:
        import strat_fvg1m as S
    except Exception:
        return

    def scan_fvg1m(C):
        k5all, k1all = C("5m"), C("1m")
        rows = []
        for sym, k1 in k1all.items():
            k5 = k5all.get(sym)
            if not k5:
                continue
            try:
                rows.extend(S.walk(sym, k5, k1, S.BASE))
            except Exception:
                pass
        return rows

    R.SCANS["fvg1m"] = scan_fvg1m
    R.META.setdefault("fvg1m", {
        "label": "FVG回踩·1m止跌入场", "tf": "1m",
        "logic": ["① 5m 一段≥2%的上涨, 留下 FVG(缺口)",
                  "② 回踩进 FVG 区间(跌穿下沿则作废)",
                  "③ 1m 出现缠论底分型 = 止跌信号",
                  "④ 确认K收盘那一刻买入(无未来函数)",
                  "⑤ 止损 A=1m分型低点 / B=FVG下沿 / C=回踩最低点-0.3%"],
    })


_register_fvg1m()


LIVE_REPLAY = os.path.join(R.CACHE, "live_replay.jsonl")
LIVE_FEEDBACK = os.path.join(R.CACHE, "live_feedback.jsonl")   # 触发精准性反馈
LIVE_RECHECK = os.path.join(R.CACHE, "live_recheck.json")      # 最近一次本地重测结果


def _load_replays():
    """你手动逐根走出来的止盈止损决策(键=线上信号id)。同一条以最后一次为准。"""
    out = {}
    if os.path.exists(LIVE_REPLAY):
        for ln in open(LIVE_REPLAY, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
                # 2026-07-19 修盲测泄露前的记录: 当时 buf 少了第一根(旧的
                # t>created_at 过滤会吞掉它), 旧 exit_bar=N 指向今天索引下的第 N+1 根。
                # 读时归一化, jsonl 本身保持 append-only 不动; 新记录带 bar_base 不再修正。
                if r.get("bar_base") != "cut" and isinstance(r.get("exit_bar"), int):
                    r["exit_bar"] += 1
                out[str(r["id"])] = r
            except Exception:
                pass
    return out


@app.get("/api/live")
def api_live():
    """线上实盘系统【真实打出】的信号(从 VPS 的 signals/paper_trades 拉的),
    不是本地扫出来的模拟信号。"""
    import live_sync
    d = live_sync.load()
    if not d:
        return JSONResponse({"rows": [], "msg": "还没同步过 —— 点「刷新」从 VPS 拉。"})
    d["kline_until"] = _kline_latest()
    rep = _load_replays()
    fbs = _load_feedback()
    for r in d.get("rows", []):
        mine = rep.get(str(r["id"]))
        if mine:
            r["mine"] = mine                       # 你亲自走过的结果(手动止盈止损)
        f = fbs.get(str(r["id"]))
        if f:
            r["fb"] = f                            # 你对触发本身的判定
    d["n_replayed"] = sum(1 for r in d.get("rows", []) if r.get("mine"))
    rows = d.get("rows", [])
    d["n_fb"] = sum(1 for r in rows if r.get("fb"))
    d["n_fb_bad"] = sum(1 for r in rows if (r.get("fb") or {}).get("verdict") == "bad")
    tagc = {}
    for r in rows:
        for t in (r.get("fb") or {}).get("tags") or []:
            tagc[t] = tagc.get(t, 0) + 1
    d["fb_tags"] = tagc
    if os.path.exists(LIVE_RECHECK):
        try:
            d["recheck"] = json.load(open(LIVE_RECHECK, encoding="utf-8"))
        except Exception:
            pass
    return JSONResponse(d)


@app.post("/api/live/replay")
async def api_live_replay(request: Request):
    """保存你逐根走完一笔的手动决策: 在第几根离场、离场价、你自己的R。"""
    b = await request.json()
    if b.get("id") is None:
        return JSONResponse({"error": "缺 id"}, status_code=400)
    rec = {"id": b["id"], "symbol": b.get("symbol", ""),
           "exit_bar": b.get("exit_bar"), "exit_price": b.get("exit_price"),
           "my_r": b.get("my_r"), "verdict": b.get("verdict", ""),
           "note": (b.get("note") or "").strip(),
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    # 出场实验用: 你自己设的止损 + 这笔是主动止盈还是被止损打掉。
    # 有了这两样才能反推"你的出场规则", 并与策略自带的 sl/tp 做对照。
    for k in ("bar_base", "exit_kind", "my_sl", "sl_source", "my_sl_pct",
              "strat_sl", "strat_tp", "entry", "tf", "direction"):
        if b.get(k) is not None:
            rec[k] = b[k]
    os.makedirs(os.path.dirname(LIVE_REPLAY), exist_ok=True)
    with open(LIVE_REPLAY, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "rec": rec}


def _load_feedback():
    """触发精准性反馈(键=线上信号id)。判断的是【触发点对不对】, 不是走单盈亏。"""
    out = {}
    if os.path.exists(LIVE_FEEDBACK):
        for ln in open(LIVE_FEEDBACK, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
                out[str(r["id"])] = r
            except Exception:
                pass
    return out


@app.post("/api/live/feedback")
async def api_live_feedback(request: Request):
    """保存一条触发反馈: verdict=ok(触发对)/bad(触发有问题) + 拒因标签 + 备注。

    append-only, 同 id 以最后一条为准 —— 反馈全集就是策略迭代的原料。
    """
    b = await request.json()
    if b.get("id") is None:
        return JSONResponse({"error": "缺 id"}, status_code=400)
    rec = {"id": b["id"], "symbol": b.get("symbol", ""),
           "verdict": b.get("verdict", ""), "tags": b.get("tags") or [],
           "note": (b.get("note") or "").strip(),
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    os.makedirs(os.path.dirname(LIVE_FEEDBACK), exist_ok=True)
    with open(LIVE_FEEDBACK, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "rec": rec}


def _recheck_all():
    """用【本地当前】策略代码 + config.yaml 参数, 对每条线上触发重放"当时那一刻":
    只喂 <=触发时刻 的K线, 看现在的规则还会不会在同一根K上触发。

    改完策略点「重测」→ 对照你的反馈:
      满意的还在(ok_kept) / 满意的被误杀(ok_lost, 回归!) /
      不满意的消失了(bad_gone, 改对了) / 不满意的还在(bad_still, 没改到位)
    """
    import yaml
    import live_sync

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from app.engine.macro_pullback import detect_macro_pullback

    cfg = (yaml.safe_load(open(os.path.join(root, "config.yaml"), encoding="utf-8"))
           or {}).get("macro_pullback") or {}
    d = live_sync.load() or {}
    rows = d.get("rows") or []
    fb = _load_feedback()

    per, why, errs = {}, {}, 0
    for r in rows:
        tf = r.get("tf") or "5m"
        k = _klines_of(r["symbol"], tf, DAYS)
        end_ms = int(r["created_at"]) * 1000
        # created_at = 入场K收盘的评估时刻。必须按【收盘时间 <= created_at】筛,
        # 不能用 open_time < created_at: 有 53/176 条 created_at 带 0~3 秒延迟(未对齐
        # bar边界), 用开盘时间筛会把"恰在此刻开盘、触发之后才收盘"的那根也喂进去,
        # 于是入场K不再是窗口最后一根, _stall_entry_idx 直接判不成立 —— 这 53 条
        # 曾被误记为"本地不复现", 实为窗口多喂一根所致。
        dur_ms = TF_SEC.get(tf, 300) * 1000
        win = [b for b in (k or []) if int(b["open_time"]) + dur_ms <= end_ms][-400:]
        if len(win) < 60:
            per[str(r["id"])] = "no_data"
            continue
        params = dict(cfg)
        params["tf"] = tf
        params["enabled"] = True
        params["_rejects"] = []
        try:
            sig = detect_macro_pullback(r["symbol"], r.get("direction", "long"),
                                        win, win, params)
        except Exception:
            errs += 1
            per[str(r["id"])] = "err"
            continue
        if sig is None:
            per[str(r["id"])] = "gone"
            # 哪条门槛把它挡下的(三条同时上线时仍可事后拆分各自贡献)
            rj = params.get("_rejects") or []
            if rj:
                why[str(r["id"])] = sorted(set(rj))
        else:
            # entry_time 是入场K的【开盘】时间, created_at 是它【收盘】的评估时刻
            # (还带 0~3 秒延迟) —— 必须补上一个周期再比, 否则天生差 dur, 全判成 near。
            ent = int((sig.extra.get("structure") or {}).get("entry_time") or 0)
            tol = TF_SEC.get(tf, 300) * 1000
            per[str(r["id"])] = "hit" if abs((ent + dur_ms) - end_ms) <= tol else "near"

    # 基线: 第一次重测(策略未改动时)的结果。VPS 参数可能被在线改过、K线来源也有
    # 细微差异, ~30% 线上触发本地本来就不复现 —— 这些点不能算"被你的修改杀掉"。
    # 之后的重测只对【基线里能复现(hit)】的点统计 误杀/已消除。
    base_file = os.path.join(R.CACHE, "live_recheck_baseline.json")
    if not os.path.exists(base_file):
        with open(base_file, "w", encoding="utf-8") as fh:
            json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "per": per},
                      fh, ensure_ascii=False)
    try:
        base_per = json.load(open(base_file, encoding="utf-8")).get("per") or {}
    except Exception:
        base_per = {}

    def _n(pred):
        n = 0
        for r in rows:
            i = str(r["id"])
            if base_per and base_per.get(i) != "hit":
                continue                      # 基线不复现的点不参与统计
            if pred(per.get(i), (fb.get(i) or {}).get("verdict")):
                n += 1
        return n
    n_unrepro = sum(1 for r in rows if base_per.get(str(r["id"])) not in (None, "hit"))
    summ = {
        "total": len(rows), "unreproducible": n_unrepro,
        "hit": sum(1 for v in per.values() if v == "hit"),
        "gone": sum(1 for v in per.values() if v in ("gone", "near")),
        "no_data": sum(1 for v in per.values() if v == "no_data"), "err": errs,
        "ok_kept": _n(lambda s, v: v == "ok" and s == "hit"),
        "ok_lost": _n(lambda s, v: v == "ok" and s in ("gone", "near")),
        "bad_gone": _n(lambda s, v: v == "bad" and s in ("gone", "near")),
        "bad_still": _n(lambda s, v: v == "bad" and s == "hit"),
    }
    # 各条门槛各挡下多少(只统计基线里本来能复现的, 否则混入环境差异)
    rej_count = {}
    for i, codes in why.items():
        if base_per and base_per.get(i) != "hit":
            continue
        for c in codes:
            rej_count[c] = rej_count.get(c, 0) + 1
    summ["reject_by_rule"] = rej_count

    out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "per": per, "why": why,
           "base_per": base_per, "summary": summ,
           "msg": (f"重测{summ['total']}条(基线可复现{summ['total']-n_unrepro}): "
                   f"仍触发{summ['hit']} | 满意保留{summ['ok_kept']} 满意误杀{summ['ok_lost']}"
                   f" | 问题已消{summ['bad_gone']} 问题仍在{summ['bad_still']}"
                   + (" | 拒因: " + " ".join(f"{k}×{v}" for k, v in
                      sorted(rej_count.items(), key=lambda x: -x[1])) if rej_count else ""))}
    with open(LIVE_RECHECK, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    return out


@app.post("/api/live/recheck")
def api_live_recheck():
    job = "live:recheck"
    if JOBS.get(job, {}).get("state") == "running":
        return {"ok": True, "job": job}
    JOBS[job] = {"state": "running", "msg": "本地重测中…"}

    def run():
        try:
            res = _recheck_all()
            JOBS[job] = {"state": "done", "msg": res["msg"]}
        except Exception as e:
            JOBS[job] = {"state": "error", "msg": f"{type(e).__name__}: {e}"}
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "job": job}


def _kline_latest():
    # 不缓存(底层 _klines_of 已按 mtime 缓存): 否则 bt_refresh 后
    # "本地最新K线"永远停在进程启动那一刻, 新触发的线上单全被误判为画不出。
    k = _klines_of("BTCUSDT", "5m", DAYS) or []
    return int(k[-1]["open_time"]) // 1000 if k else 0


@app.post("/api/live/refresh")
def api_live_refresh():
    import threading
    import live_sync
    job = "live:refresh"
    if JOBS.get(job, {}).get("state") == "running":
        return {"ok": True, "job": job}
    JOBS[job] = {"state": "running", "msg": "SSH 连 VPS 拉取中…"}

    def run():
        ok, msg = live_sync.pull(50, days=7)          # 默认拉最近一周全部触发
        JOBS[job] = {"state": "done" if ok else "error", "msg": msg}
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "job": job}


@app.get("/api/scanners")
def api_scanners():
    """可执行策略(能被一键回测跑起来的) + 缓存实际覆盖的月份 —— 别让用户选一个跑不出数据的月份。"""
    return JSONResponse({
        "scanners": [{"name": k, "label": (R.META.get(k) or {}).get("label", k),
                      "tf": trig_tf(k)} for k in sorted(R.SCANS)],
        "coverage": _coverage(),
    })


def _coverage():
    """每个月实际能跑到什么数据 —— 别让用户选一个跑不出东西的月份还不知道为什么。

    30d 档: 663 币, 只覆盖最近30天(所以"当月/上月"往往只有半个月);
    365d 档: 只有 50 个币有一年数据。
    """
    import datetime as dt
    now = dt.datetime.now()
    c30 = now - dt.timedelta(days=30)          # 30d 缓存的起点
    months = []
    y, m = now.year, now.month
    for _ in range(13):
        ms = dt.datetime(y, m, 1)
        me = dt.datetime(y + (m == 12), (m % 12) + 1, 1)
        ov_s, ov_e = max(ms, c30), min(me, now)          # 与30d缓存的交集
        ov_days = max(0, (ov_e - ov_s).days)
        full = ov_days >= (me - ms).days - 1
        if ov_days <= 0:
            months.append({"m": f"{y:04d}-{m:02d}", "src": "365d", "symbols": 50,
                           "note": "只有50个币有一年数据"})
        elif full:
            months.append({"m": f"{y:04d}-{m:02d}", "src": "30d", "symbols": 663, "note": ""})
        else:
            months.append({"m": f"{y:04d}-{m:02d}", "src": "30d", "symbols": 663,
                           "note": f"仅 {ov_s:%m-%d}~{ov_e:%m-%d} 有数据"})
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return {"months": months, "cache_30d_from": c30.strftime("%Y-%m-%d")}


JOBS = {}


def _run_backtest(job, slug, version, scanner, month):
    """后台跑: 扫描(约30s) → 按月过滤 → 算扣费后期望 → 写 backtests/<v>_<month>.json。"""
    import datetime as dt
    import math
    import statistics
    import idea_lib
    try:
        y, mo = int(month[:4]), int(month[5:7])
        ms = dt.datetime(y, mo, 1)
        me = dt.datetime(y + (mo == 12), (mo % 12) + 1, 1)
        t0, t1 = int(ms.timestamp()), int(me.timestamp())
        # 和 _coverage() 必须用同一套判断 —— 否则界面标"663币"、实际却跑50币的档, 结果对不上
        days = 30 if me > (dt.datetime.now() - dt.timedelta(days=30)) else 365

        t_start = time.time()
        el = lambda: f"{int(time.time() - t_start)}秒"      # noqa: E731

        JOBS[job] = {"state": "running", "msg": f"加载缓存({days}d)… [{el()}]"}
        C = R.cache_loader(days)

        fn = _resolve_scanner(scanner)          # 内置策略 或 灵感目录里 Claude 生成的
        if fn is None:
            raise RuntimeError(f"找不到 scanner: {scanner}")

        # 扫描期间起个心跳线程报真实耗时 —— 别再写"约30秒"这种拍脑袋的估计骗人:
        # 内置策略约30秒, 但用了 hvn(密集成交区) 的生成策略要 2~3 分钟(每根K都要做成交量分桶)。
        import threading as _th
        done_flag = {"v": False}

        def _beat():
            while not done_flag["v"]:
                JOBS[job] = {"state": "running",
                             "msg": f"扫描 {scanner} 中… 已用 {el()}（663币全量, 请等它跑完）"}
                time.sleep(2)
        hb = _th.Thread(target=_beat, daemon=True)
        hb.start()
        try:
            rows = fn(C)
        finally:
            done_flag["v"] = True

        JOBS[job] = {"state": "running", "msg": f"结算与统计… [{el()}]"}
        FEE = 0.045                                   # 每边手续费%, 与 bt_registry.score 一致
        sigs, nets = [], []
        for s in rows:
            t = s["created_at"]
            if not (t0 <= t < t1):
                continue
            net = None
            if s.get("result") in ("tp", "sl", "timeout") and s.get("pnl_r") is not None:
                risk = abs(s["entry"] - s["sl"]) or 1e-9
                cost = 2 * (FEE / 100.0) * s["entry"] / risk
                net = s["pnl_r"] - cost
                nets.append(net)
            sigs.append({"id": len(sigs), "symbol": s["symbol"], "t": t,
                         "dir": s["direction"], "tf": s.get("tf") or trig_tf(scanner),
                         "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
                         "result": s.get("result"), "pnl_r": s.get("pnl_r"),
                         "net_r": round(net, 3) if net is not None else None})

        n_closed = len(nets)
        wins = sum(1 for s in sigs if s.get("result") == "tp")
        exp_net = round(sum(nets) / n_closed, 4) if n_closed else None
        tstat = None
        if n_closed > 1:
            sd = statistics.stdev(nets)
            if sd > 0:
                tstat = round((sum(nets) / n_closed) / (sd / math.sqrt(n_closed)), 2)

        win_rate = round(wins / n_closed * 100, 1) if n_closed else None
        if not sigs:
            verdict = f"这个月没有触发信号。{'(该月只有50个币有数据)' if days == 365 else ''}"
        elif exp_net is None:
            verdict = f"{len(sigs)} 个信号, 但都还没结算(持仓中), 无法评估。"
        elif win_rate is not None and win_rate >= 90:
            # 幸存者偏差护栏: 正常机械策略胜率 30~50%。90%+ 说明这批信号不是一次真实的
            # 样本外扫描, 而是"挑出来的赢单"(如 online_regress = 线上盈利单原样回看)。
            # 对只由赢家组成的样本算期望, 必然为正 —— 这个数字没有意义, 不能拿去决策。
            verdict = (f"⚠ 胜率 {win_rate}% —— 这不是随机样本。扣费后期望 {exp_net:+.3f}R "
                       f"(t={tstat}) 是**幸存者偏差**的产物, 不能当作边际证据。"
                       f"这批信号八成是'挑出来的赢单'而非一次真实的样本外扫描。")
        else:
            good = exp_net > 0 and (tstat or 0) > 2
            verdict = (f"扣费后期望 {exp_net:+.3f}R/单 (n={n_closed}, t={tstat}, 胜率{win_rate}%). "
                       + ("统计上站得住(t>2)。" if good
                          else ("为正但样本不足以坐实(t≤2), 别急着上线。" if exp_net > 0
                                else "为负 —— 这一版没有边际。")))

        payload = {"version": version, "scanner": scanner, "range": month,
                   "days_src": f"{days}d", "symbols": 663 if days == 30 else 50,
                   "n": len(sigs), "n_closed": n_closed,
                   "win_rate": round(wins / n_closed * 100, 1) if n_closed else None,
                   "exp_r": exp_net, "tstat": tstat, "fee_pct_side": FEE,
                   "verdict": verdict,
                   "truncated": len(sigs) > 300,
                   "signals": sigs[:300],           # 页面只画前300张; 不静默截断, truncated 会标出来
                   "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        payload["took_sec"] = int(time.time() - t_start)
        bid = idea_lib.write_backtest(slug, payload)
        JOBS[job] = {"state": "done", "msg": f"[耗时 {el()}] {verdict}", "bt_id": bid}
    except Exception as e:
        JOBS[job] = {"state": "error", "msg": f"{type(e).__name__}: {e}"}


@app.post("/api/idea/{slug}/version")
async def api_idea_version(slug: str, request: Request):
    import idea_lib
    b = await request.json()
    if b.get("scanner") and b["scanner"] not in R.SCANS:
        return JSONResponse({"error": "未知策略"}, status_code=400)
    v = idea_lib.save_version(slug, b.get("title", ""), b.get("scanner", ""),
                              b.get("why", ""), b.get("body", ""))
    if not v:
        return JSONResponse({"error": "保存失败"}, status_code=400)
    return {"ok": True, "v": v}


@app.post("/api/idea/{slug}/backtest")
async def api_idea_backtest(slug: str, request: Request):
    import threading
    import idea_lib
    b = await request.json()
    version, month = b.get("version", ""), b.get("month", "")
    v = next((x for x in idea_lib.load_versions(slug) if x["v"] == version), None)
    if not v:
        return JSONResponse({"error": "版本不存在"}, status_code=400)
    if not v.get("scanner") or _resolve_scanner(v["scanner"]) is None:
        return JSONResponse({"error": f"{version} 没有可执行的代码 —— 先在「原始图」里写下点位描述, "
                                      f"然后点「① 转成筛选条件」→「② 生成策略代码」。"},
                            status_code=400)
    if not re.match(r"^\d{4}-\d{2}$", month or ""):
        return JSONResponse({"error": "月份格式应为 YYYY-MM"}, status_code=400)
    job = f"{slug}:{version}:{month}"
    if JOBS.get(job, {}).get("state") == "running":
        return {"ok": True, "job": job}
    JOBS[job] = {"state": "running", "msg": "排队中…"}
    threading.Thread(target=_run_backtest, args=(job, slug, version, v["scanner"], month),
                     daemon=True).start()
    return {"ok": True, "job": job}


@app.get("/api/job")
def api_job(id: str):
    return JSONResponse(JOBS.get(id) or {"state": "unknown"})


def _load_idea_scanner(name):
    """把灵感目录里 Claude 生成的 strategy/vN.py 装成一个可回测的 scanner。

    name 形如 idea:<slug>:v2。代码是【被执行】的 —— 所以只在用户明确点「跑回测」时才走到这里,
    生成的时候只写文件、在页面上全文展示, 不碰。
    """
    import idea_lib
    if not name.startswith("idea:"):
        return None
    try:
        _, slug, v = name.split(":", 2)
    except ValueError:
        return None
    d = idea_lib.idea_dir(slug)
    if not d:
        return None
    p = os.path.join(d, "strategy", f"{v}.py")
    if not os.path.exists(p):
        return None
    ns = {"__name__": f"idea_{slug}_{v}"}
    src = open(p, encoding="utf-8").read()
    exec(compile(src, p, "exec"), ns)          # noqa: S102 —— 用户自己机器上、自己点的按钮
    fn = ns.get("scan")
    return fn if callable(fn) else None


def _resolve_scanner(name):
    """先查内置 SCANS, 再查灵感目录里生成的。"""
    if name in R.SCANS:
        return R.SCANS[name]
    return _load_idea_scanner(name)


@app.post("/api/idea/{slug}/desc")
async def api_idea_desc(slug: str, request: Request):
    """保存用户对这个点位的自然语言描述。"""
    import idea_lib
    b = await request.json()
    if not idea_lib.save_desc(slug, b.get("text", "")):
        return JSONResponse({"error": "灵感不存在"}, status_code=404)
    return {"ok": True}


@app.post("/api/idea/{slug}/spec")
async def api_idea_spec(slug: str, request: Request):
    """① 自然语言 → 结构化筛选条件(调本机 claude -p)。"""
    import threading
    import claude_gen
    import idea_lib
    b = await request.json()
    note = (b.get("text") or idea_lib.load_desc(slug)).strip()
    if not note:
        return JSONResponse({"error": "先写下你对这个点位的描述。"}, status_code=400)
    idea_lib.save_desc(slug, note)
    idea = next((x for x in idea_lib.load_ideas() if x["slug"] == slug), {}) or {}
    job = f"spec:{slug}"
    if JOBS.get(job, {}).get("state") == "running":
        return {"ok": True, "job": job}
    JOBS[job] = {"state": "running", "msg": "本机 Claude 正在把你的描述拆成筛选条件…"}

    def run():
        ok, out, js = claude_gen.spec(note, idea.get("symbol", ""), b.get("tf", "5m"))
        if not ok:
            JOBS[job] = {"state": "error", "msg": out}
            return
        d = idea_lib.idea_dir(slug)
        os.makedirs(os.path.join(d, "strategy"), exist_ok=True)
        json.dump(js, open(os.path.join(d, "strategy", "spec.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        JOBS[job] = {"state": "done", "msg": "筛选条件已生成", "spec": js}
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "job": job}


@app.post("/api/idea/{slug}/codegen")
async def api_idea_codegen(slug: str, request: Request):
    """② 筛选条件 → Python 代码 + 优化后的文字版 → 落进「策略迭代」的首版。"""
    import threading
    import claude_gen
    import idea_lib
    b = await request.json()
    d = idea_lib.idea_dir(slug)
    if not d:
        return JSONResponse({"error": "灵感不存在"}, status_code=404)
    sp = os.path.join(d, "strategy", "spec.json")
    if not os.path.exists(sp):
        return JSONResponse({"error": "先点「① 转成筛选条件」。"}, status_code=400)
    spec_js = json.load(open(sp, encoding="utf-8"))
    note = idea_lib.load_desc(slug)
    tf = b.get("tf", "5m")
    idea = next((x for x in idea_lib.load_ideas() if x["slug"] == slug), {}) or {}
    job = f"codegen:{slug}"
    if JOBS.get(job, {}).get("state") == "running":
        return {"ok": True, "job": job}
    JOBS[job] = {"state": "running", "msg": "本机 Claude 正在写策略代码…(约1分钟)"}

    def run():
        vnum = idea_lib.next_vnum(slug)
        ok, msg, r = claude_gen.codegen(note, spec_js, slug, vnum, idea.get("symbol", ""), tf)
        if not ok:
            JOBS[job] = {"state": "error", "msg": msg}
            return
        v = idea_lib.save_generated(slug, vnum, r["title"], r["py"], r["md"],
                                    why="据原始描述生成的首版")
        JOBS[job] = {"state": "done", "msg": f"已生成 {v} —— 代码在「策略迭代」里, 看过再跑", "v": v}
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "job": job}


@app.post("/api/idea/{slug}/iterate")
async def api_idea_iterate(slug: str, request: Request):
    """③ 据盲测标注 → 二次生成(下一版)。"""
    import threading
    import claude_gen
    import idea_lib
    b = await request.json()
    bt_id = b.get("bt_id", "")
    # 用【跨所有版本】的全部判断, 不只是这一次回测的 —— 你在 v1 说过的话, 到 v3 依然算数
    ann = list(idea_lib.load_judged(slug).values())
    if len(ann) < 5:
        return JSONResponse({"error": f"总共只判了 {len(ann)} 笔 —— 样本太少, 归纳不出规律。"
                                      f"建议至少 20 笔。"}, status_code=400)
    vs = idea_lib.load_versions(slug)
    cur = next((v for v in vs if bt_id.startswith(v["v"] + "_")), vs[0] if vs else None)
    if not cur or not cur.get("code"):
        return JSONResponse({"error": "找不到这次回测对应版本的代码。"}, status_code=400)
    note = idea_lib.load_desc(slug)
    job = f"iterate:{slug}"
    if JOBS.get(job, {}).get("state") == "running":
        return {"ok": True, "job": job}
    JOBS[job] = {"state": "running", "msg": f"本机 Claude 正在读你那 {len(ann)} 笔判断, 迭代下一版…"}

    def run():
        vnum = idea_lib.next_vnum(slug)
        ok, msg, r = claude_gen.iterate(note, cur["code"], cur["body_html"], ann, vnum)
        if not ok:
            JOBS[job] = {"state": "error", "msg": msg}
            return
        v = idea_lib.save_generated(slug, vnum, r["title"], r["py"], r["md"],
                                    why=r.get("why", ""), based_on=cur["v"])
        JOBS[job] = {"state": "done", "v": v,
                     "msg": f"已生成 {v}（基于 {cur['v']} + 你那 {len(ann)} 笔盲测判断）—— "
                            f"去「回测记录」用它重跑, 看图形通过率涨没涨"}
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "job": job}


@app.post("/api/idea/{slug}/summary")
async def api_idea_summary(slug: str, request: Request):
    """把这次回测里所有「不符合」的理由按频次归纳 → 存成 annotations/<bt_id>_summary.md。

    诚实说明: 看图器是个本地静态应用, 连不上 LLM —— 这个按钮做的是【归纳】, 不是【生成新策略】。
    归纳出来的东西是给 Claude 的输入; 由 Claude 读了它去改规则、写出 v(N+1) 的代码。
    """
    import idea_lib
    b = await request.json()
    bt_id = b.get("bt_id", "")
    d = idea_lib.idea_dir(slug)
    if not d or not bt_id:
        return JSONResponse({"error": "参数不全"}, status_code=400)

    ann = idea_lib.load_annotations(slug, bt_id)
    ok = [a for a in ann.values() if a["verdict"] == "ok"]
    bad = [a for a in ann.values() if a["verdict"] == "bad"]
    if not ann:
        return JSONResponse({"error": "还没有任何盲测标注"}, status_code=400)

    groups = {}
    for a in bad:
        groups.setdefault(a["reason"].strip(), []).append(a["sig"])
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    rate = round(len(ok) / len(ann) * 100, 1)

    md = [f"# 盲测汇总 · {bt_id}", "",
          f"- 已盲测: **{len(ann)}** 笔",
          f"- 符合: **{len(ok)}** 笔 · 不符合: **{len(bad)}** 笔",
          f"- **图形通过率: {rate}%**  ← 第一阶段的唯一进度指标(P004: 先形后利)", ""]
    if ranked:
        md += ["## 不符合的理由(按出现频次)", ""]
        for r, sigs in ranked:
            md.append(f"- **{len(sigs)}次** — {r}  <small>(信号 {', '.join(map(str, sigs[:8]))}"
                      f"{'…' if len(sigs) > 8 else ''})</small>")
        md += ["", "## 下一步", "",
               "把这份汇总交给 Claude: 「据此把规则改成 v(N+1)」。",
               "出现频次最高的理由 = 最该先收紧的那条筛选语句。"]
    else:
        md += ["## 没有「不符合」的样本", "",
               f"通过率 {rate}%。图形阶段基本达标, 可以进入第二阶段(研究怎么在这个形态上赚钱)。"]

    txt = "\n".join(md)
    p = os.path.join(d, "annotations")
    os.makedirs(p, exist_ok=True)
    open(os.path.join(p, f"{bt_id}_summary.md"), "w", encoding="utf-8").write(txt + "\n")
    return {"ok": True, "n": len(ann), "n_ok": len(ok), "n_bad": len(bad), "pass_rate": rate,
            "groups": [{"reason": r, "count": len(s)} for r, s in ranked],
            "html": idea_lib.md2html(txt, f"ideas/{slug}")}


@app.post("/api/idea/{slug}/annotate")
async def api_idea_annotate(slug: str, request: Request):
    """回测标注: 符合(ok) / 不准(bad, 必须写明该改哪条筛选语句)。绑定到该次回测=该策略版本。"""
    import idea_lib
    b = await request.json()
    rec = idea_lib.save_annotation(slug, b.get("bt_id", ""), b.get("sig"),
                                   b.get("verdict", ""), b.get("reason", ""),
                                   b.get("symbol", ""), b.get("t", 0), b.get("dir", ""))
    if not rec:
        return JSONResponse({"error": "标注失败(标'不准'必须写理由 —— 它是下一版改规则的依据)"},
                            status_code=400)
    return {"ok": True, "rec": rec}


def trig_tf(strat):
    """这条信号是几分钟级别触发的。META 里 tf 形如 5m / 15m / 15m+1h(第一个才是触发级别,
    后面的是过滤用的大级别)。"""
    tf = str((R.META.get(strat) or {}).get("tf", "") or "")
    return tf.split("+")[0].strip() or "5m"


def _row(s):
    return {"id": s["id"], "strat": s.get("strat"), "symbol": s["symbol"], "dir": s["direction"],
            "stage": s.get("stage"), "t": s["created_at"], "entry": s["entry"], "sl": s["sl"],
            "tp": s["tp"], "result": s.get("result"), "pnl_r": s.get("pnl_r"),
            "climaxX": s.get("climaxX"), "movePct": s.get("movePct"), "anchor": s.get("anchor"),
            "extra": s.get("extra"), "vol_ratio": s.get("vol_ratio"),
            "tf": trig_tf(s.get("strat"))}


@app.get("/api/signals")
def api_signals(strat: str = "", dir: str = "", result: str = "", tf: str = "", limit: int = 800):
    """服务端过滤+截断: 全量是 200 万+ 条(~500MB), 整包下发会把浏览器(尤其手机)打死。
    只回最新 limit 条 + 命中总数。tf = 按触发级别筛(5m/15m/1h)。"""
    hit = [s for s in SIGNALS
           if (not strat or s.get("strat") == strat)
           and (not dir or s.get("direction") == dir)
           and (not result or s.get("result") == result)
           and (not tf or trig_tf(s.get("strat")) == tf)]
    limit = max(1, min(limit, 3000))
    return JSONResponse({"total": len(hit), "rows": [_row(s) for s in hit[::-1][:limit]]})


@app.get("/api/signal/{sid}")
def api_signal(sid: int):
    s = next((x for x in SIGNALS if x["id"] == sid), None)
    return JSONResponse(_row(s) if s else {}, status_code=200 if s else 404)


@lru_cache(maxsize=64)
def _klines_cached(symbol: str, tf: str, days: int, mtime: float):
    """mtime 进缓存键: bt_refresh 改了文件 → 键变 → 自动读新数据。"""
    p = os.path.join(R.CACHE, f"{symbol}_{tf}_{days}d.json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def _klines_of(symbol: str, tf: str, days: int):
    """只读这一个币的缓存文件。

    不要用 R.cache_loader(): 它会 glob 出全部 2263 个币的 *_5m_30d.json 逐个 json.load
    进内存(2.7G 磁盘 -> 进程 RSS 4G+), 于是"重启后第一次点信号"要干等几十秒且界面无提示,
    看起来就像点了没反应。看图一次只需要一个币。

    缓存按文件 mtime 失效 —— 之前 lru_cache 只按 (symbol,tf,days) 缓存, 看图器
    开着的时候跑 bt_refresh, 界面永远读到旧K线(线上单显示"比本地K线还新")。
    """
    p = os.path.join(R.CACHE, f"{symbol}_{tf}_{days}d.json")
    if not os.path.exists(p):
        return None
    return _klines_cached(symbol, tf, days, os.path.getmtime(p))


TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


@app.get("/api/klines")
def api_klines(symbol: str, center: int, span: int = 120, tf: str = "5m",
               after: int = -1, cut: int = 0):
    """after = 触发点【之后】给几根K线; cut = 信息截止时刻(unix秒)。

    盲测(P004)的地基。两个坑:
    1. after=0 → 不给触发之后的K线。
    2. cut → 【必须】, 否则切到大级别时会偷偷泄露未来: 1m 的触发点落在某根 5m K线中间,
       那根 5m K要到5分钟后才收盘, 它的高/低/收盘价包含了触发之后的信息。
       cut = 触发K收盘那一刻; 只给【在 cut 之前就已经收盘】的K线, 那根还没走完的一律砍掉。

    after<0 且 cut=0(默认) → 老行为, 前后都给。
    """
    # cut 有值 = 盲测请求: 禁止静默回退到 5m。回退会让下面的 dur 与真实bar周期错配,
    # 边界算术随之失效(实测 tf=1m 无缓存时回退到5m, dur仍取60 → 多画一根, 泄露最多240秒)。
    k = _klines_of(symbol, tf, DAYS) or (None if cut else _klines_of(symbol, "5m", DAYS))
    if not k:
        return JSONResponse([])
    times = [int(b["open_time"]) // 1000 for b in k]
    # 周期以【实际数据】为准而非请求的 tf: 取相邻开盘时间差的最小值 —— 数据缺口只会
    # 让差值变大、永不变小, 故只要存在任意一对连续bar, min 就等于真实周期。
    dur = min((b - a for a, b in zip(times, times[1:])), default=TF_SEC.get(tf, 300))
    if cut:
        vis = bisect.bisect_right(times, cut - dur)     # 已收盘K的根数(= 可见区右端)
        # after>0: 要 cut 【之后】的K(盲测缓冲区, 前端藏着一根根揭晓)。
        # after<=0: 老行为, 只给可见区 —— 两者拼起来无重叠也无跳过。
        nafter = 0 if after < 0 else after
        lo, hi = max(0, vis - span), min(len(k), vis + nafter)
    else:
        j = bisect.bisect_left(times, center)
        nafter = span if after < 0 else after
        lo, hi = max(0, j - span), min(len(k), j + nafter + 1)
    return JSONResponse([
        {"t": int(b["open_time"]) // 1000, "o": float(b["open"]), "h": float(b["high"]),
         "l": float(b["low"]), "c": float(b["close"]), "v": float(b["volume"])}
        for b in k[lo:hi]])


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


HTML = """<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>回测看图器</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 body{margin:0;font:13px system-ui;background:#0e1116;color:#d6dae0;display:flex;height:100vh;height:100dvh}
 #left{width:420px;overflow:auto;border-right:1px solid #222;flex:none}
 #right{flex:1;display:flex;flex-direction:column;min-width:0}
 #bar{padding:8px 12px;border-bottom:1px solid #222}
 #chart{flex:1;min-height:0}
 #logic{border-top:1px solid #222;padding:8px 12px;background:#11161d;font-size:12px;max-height:170px;overflow:auto}
 #logic h4{margin:0 0 4px}#logic .li{color:#adbac7;margin:2px 0}
 #mlist,#mclose,#backdrop{display:none}
 table{width:100%;border-collapse:collapse}
 th,td{padding:5px 6px;text-align:left;border-bottom:1px solid #1c2128;white-space:nowrap}
 th{position:sticky;top:0;background:#161b22}
 tr.row{cursor:pointer} tr.row:hover{background:#1c2530} tr.sel{background:#243447!important}
 .tp{color:#3fb950}.sl{color:#f85149}.long{color:#3fb950}.short{color:#f85149}
 .badge{padding:1px 6px;border-radius:4px;background:#30363d;font-size:11px}
 /* 触发级别: 一眼看出这条信号是几分钟级别打出来的 */
 .tfb{padding:1px 5px;border-radius:4px;font-size:10px;border:1px solid #30363d;color:#8b949e}
 .tfb.tf-5m{color:#58a6ff;border-color:#1f4b7a}
 .tfb.tf-15m{color:#d29922;border-color:#5c4813}
 .tfb.tf-1h{color:#bc8cff;border-color:#4c3a75}
 select{background:#161b22;color:#d6dae0;border:1px solid #30363d;border-radius:5px;padding:3px 6px;margin:2px}
 .muted{color:#8b949e}
 /* ---- 手机竖屏: K线图占满全屏; 信号列表=顶部下拉抽屉; 策略思路=可折叠 ---- */
 @media (max-width:820px){
   body{flex-direction:column;height:100dvh}
   #right{flex:1;min-height:0}
   /* 信号列表: 抽屉, 默认收起 */
   #left{position:fixed;left:0;right:0;top:0;width:auto;height:80dvh;z-index:30;background:#0e1116;
         border-right:none;border-bottom:2px solid #30363d;box-shadow:0 10px 30px rgba(0,0,0,.7);
         transform:translateY(-102%);transition:transform .22s ease;overscroll-behavior:contain}
   body.drawer #left{transform:translateY(0)}
   #backdrop{display:block;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:29;opacity:0;pointer-events:none;transition:opacity .22s}
   body.drawer #backdrop{opacity:1;pointer-events:auto}
   #mclose{display:inline-block;float:right;margin-left:10px;padding:2px 10px;border:1px solid #30363d;
           border-radius:6px;background:#161b22;color:#d6dae0;font-size:14px}
   /* 顶栏上的抽屉开关 */
   #mlist{display:inline-block;padding:5px 12px;margin-right:8px;border:1px solid #30363d;
          border-radius:6px;background:#161b22;color:#d6dae0;font-size:14px;vertical-align:middle}
   /* 策略思路: 折叠成一行, 点标题展开 */
   #logic{max-height:34px;overflow:hidden;padding-bottom:calc(8px + env(safe-area-inset-bottom))}
   body.logicopen #logic{max-height:40dvh;overflow:auto}
   #logic h4{cursor:pointer}
   #logic h4::after{content:' ▾';color:#6e7681}
   body.logicopen #logic h4::after{content:' ▴'}
   /* 顶栏必须瘦: 它每长高 1px, K线图就矮 1px */
   #bar{padding:6px 10px}
   #fresh{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:10px;margin-bottom:2px}
   #title{font-size:13px}
   /* 打标栏由 JS 控制显隐(display:none/block), 这里只压成单行横滚 */
   #labelbar{white-space:nowrap;overflow-x:auto;overflow-y:hidden;-webkit-overflow-scrolling:touch}
   #labelbar button{padding:5px 10px!important}
   #lreason2{width:110px!important}
   /* 触摸目标放大 */
   th,td{padding:9px 8px;font-size:13px}
   tr.row{min-height:44px}
   select{font-size:15px;padding:6px 8px}
 }
</style></head><body>
<div id=backdrop onclick=closeDrawer()></div>
<div id=left>
 <div style="padding:8px 12px;position:sticky;top:0;background:#0e1116;z-index:2">
  <button id=mclose onclick=closeDrawer()>✕ 收起</button>
  <b>📊 信号</b> <span class=muted id=cnt></span>
  <a href="/ideas" style="float:right;color:#58a6ff;text-decoration:none;margin-left:10px">💡 灵感库</a>
  <a href="/agents" target="_blank" style="float:right;color:#58a6ff;text-decoration:none">🤖 Agent工作台</a><br>
  <select id=fstrat onchange=render()></select>
  <select id=fdir onchange=render()><option value="">全方向</option><option value=long>多</option><option value=short>空</option></select>
  <select id=ftf onchange=render()><option value="">全级别</option><option value=5m>5m触发</option><option value=15m>15m触发</option><option value=1h>1h触发</option></select>
  <select id=fres onchange=render()><option value="">全结果</option><option value=tp>盈✓</option><option value=sl>损✗</option><option value=open>持仓</option></select>
 </div>
 <table><thead><tr><th>时间</th><th>策略</th><th>币</th><th>向</th><th>结果</th></tr></thead><tbody id=rows></tbody></table>
</div>
<div id=right>
 <div id=bar><div id=fresh class=muted style="font-size:11px;margin-bottom:4px">数据新鲜度加载中…</div><button id=mlist onclick=openDrawer()>☰ 信号 <span id=mcnt></span></button><b id=title>← 点左侧信号查看当时K线</b> <span id=tfsw style="margin-left:10px"></span> <button id=savebtn onclick=saveCase() style="margin-left:8px;padding:2px 8px;border:1px solid #30363d;border-radius:4px;background:#161b22;color:#d6dae0;display:none">⭐保存案例</button> <span class=muted id=info></span>
  <div id=labelbar style="display:none;margin-top:5px;font-size:12px">
   审美打标:
   <button onclick="saveLabel('good')" style="padding:2px 8px;border:1px solid #2ea043;border-radius:4px;background:#161b22;color:#3fb950;cursor:pointer">👍 符合</button>
   <button onclick="saveLabel('bad')" style="padding:2px 8px;border:1px solid #b62324;border-radius:4px;background:#161b22;color:#f85149;cursor:pointer">👎 不符合</button>
   理由:
   <select id=lreason style="background:#161b22;color:#d6dae0;border:1px solid #30363d;border-radius:4px;padding:2px 6px">
     <option value="">(选/或手写)</option><option>巨量不够大</option><option>反弹太弱</option>
     <option>急跌不连续/夹横盘</option><option>进得太晚/离恐慌低太远</option><option>结构乱/有干扰分型</option><option>方向不对</option>
   </select>
   <input id=lreason2 placeholder="手写理由" style="background:#161b22;color:#d6dae0;border:1px solid #30363d;border-radius:4px;padding:2px 6px;width:150px">
   <span id=lstate class=muted style="margin-left:6px"></span>
  </div></div>
 <div id=chart></div>
 <div id=logic><h4 id=lt onclick=toggleLogic()>策略思路</h4><div id=ld class=muted>点一条信号,这里显示它所属策略的逻辑</div></div>
</div>
<script>
let ALL=[], META={}, STATS={}, DETAIL={}, chart, candle, vol, lines=[], fvgBox=null;

/* ---- FVG 色块(lightweight-charts v4 series primitive: 画一个真矩形, 不是两条线) ---- */
class FvgRenderer{
 constructor(p1,p2,fill,edge){this._p1=p1;this._p2=p2;this._fill=fill;this._edge=edge;}
 draw(target){
  if(this._p1.x===null||this._p2.x===null||this._p1.y===null||this._p2.y===null)return;
  target.useBitmapCoordinateSpace(scope=>{
   const ctx=scope.context, hr=scope.horizontalPixelRatio, vr=scope.verticalPixelRatio;
   const x1=Math.round(Math.min(this._p1.x,this._p2.x)*hr), x2=Math.round(Math.max(this._p1.x,this._p2.x)*hr);
   const y1=Math.round(Math.min(this._p1.y,this._p2.y)*vr), y2=Math.round(Math.max(this._p1.y,this._p2.y)*vr);
   ctx.fillStyle=this._fill; ctx.fillRect(x1,y1,x2-x1,Math.max(y2-y1,1*vr));
   ctx.strokeStyle=this._edge; ctx.lineWidth=1*vr;
   ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y1); ctx.moveTo(x1,y2); ctx.lineTo(x2,y2); ctx.stroke();
  });
 }
}
class FvgView{
 constructor(src){this._src=src;this._p1={x:null,y:null};this._p2={x:null,y:null};}
 update(){
  const s=this._src._series, ts=this._src._chart.timeScale();
  this._p1={x:ts.timeToCoordinate(this._src._t1),y:s.priceToCoordinate(this._src._lo)};
  this._p2={x:ts.timeToCoordinate(this._src._t2),y:s.priceToCoordinate(this._src._hi)};
 }
 renderer(){return new FvgRenderer(this._p1,this._p2,this._src._fill,this._src._edge);}
}
class FvgPrimitive{
 constructor(t1,t2,lo,hi,long){
  this._t1=t1;this._t2=t2;this._lo=lo;this._hi=hi;
  this._fill=long?'rgba(63,185,80,0.18)':'rgba(248,81,73,0.18)';
  this._edge=long?'rgba(63,185,80,0.55)':'rgba(248,81,73,0.55)';
  this._view=new FvgView(this);
 }
 attached(p){this._series=p.series;this._chart=p.chart;this._req=p.requestUpdate;}
 detached(){}
 updateAllViews(){this._view.update();}
 paneViews(){return [this._view];}
}
const fmt=t=>new Date(t*1000).toLocaleString('zh-CN',{hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
function ageStr(min){ if(min==null)return '?'; if(min<60)return Math.round(min)+'分钟前'; if(min<1440)return (min/60).toFixed(1)+'小时前'; return (min/1440).toFixed(1)+'天前'; }
async function loadFresh(){
 try{
  const c=await (await fetch('/api/cache_status')).json();
  const kl=['5m','15m','1h'].map(tf=>{const x=c.klines[tf]; return x?`${tf} 最新 ${fmt(x.last_open)}(${ageStr(x.age_min)})`:`${tf} 无`;}).join(' · ');
  const sg=Object.entries(c.signals).filter(([k,v])=>v).map(([k,v])=>`${(META[k]||{}).label||k} ${ageStr(v.age_min)}`).join(' · ');
  document.getElementById('fresh').innerHTML=`📦 数据: ${kl}　|　🧮 信号生成: ${sg||'无'}`;
 }catch(e){ document.getElementById('fresh').textContent='数据新鲜度获取失败'; }
}
async function load(){
 const m=await (await fetch('/api/meta')).json(); META=m.meta; STATS=m.stats; DETAIL=m.detail||{};
 loadFresh(); loadLabels();
 const opts=['<option value="">全部策略</option>'].concat(Object.keys(META).map(k=>{
   const st=STATS[k]||{}; return `<option value=${k}>${META[k].label} (${st.n_sig||0}信号/胜${st.win_rate||0}%)</option>`;}));
 document.getElementById('fstrat').innerHTML=opts.join('');
 await render();
}
const LIMIT=800;   // 服务端也回最新 LIMIT 条(全量 200万+ 条, 整包下发会打死浏览器)
async function render(){
 const fs=document.getElementById('fstrat').value, fd=document.getElementById('fdir').value,
       fr=document.getElementById('fres').value, ft=document.getElementById('ftf').value;
 document.getElementById('cnt').textContent='加载中…';
 const q=new URLSearchParams({strat:fs,dir:fd,result:fr,tf:ft,limit:LIMIT});
 let r; try{ r=await (await fetch('/api/signals?'+q)).json(); }
 catch(e){ document.getElementById('cnt').textContent='加载失败'; return; }
 ALL=r.rows||[];
 document.getElementById('cnt').textContent =
   ALL.length<r.total ? `最新 ${ALL.length} / 共 ${r.total} 条` : `共 ${r.total} 条`;
 document.getElementById('mcnt').textContent =
   r.total>=10000 ? `(${(r.total/10000).toFixed(1)}万)` : `(${r.total})`;
 document.getElementById('rows').innerHTML=ALL.map(s=>`<tr class=row data-id=${s.id} onclick=show(${s.id})>
  <td>${fmt(s.t)}</td>
  <td><span class=badge>${(META[s.strat]||{}).label||s.strat}</span> <span class="tfb tf-${s.tf}">${s.tf}</span>${s.stage?(' '+s.stage):''}</td>
  <td><b>${s.symbol}</b></td><td class=${s.dir}>${s.dir==='long'?'多':'空'}</td>
  <td class="${s.result}">${s.result==='tp'?'✓':s.result==='sl'?'✗':'⏳'}</td></tr>`).join('');
}
/* ---- 手机: 信号抽屉 + 策略思路折叠 ---- */
const isMobile=()=>window.matchMedia('(max-width:820px)').matches;
function openDrawer(){document.body.classList.add('drawer');}
function closeDrawer(){document.body.classList.remove('drawer');}
function toggleLogic(){if(isMobile())document.body.classList.toggle('logicopen');}
function ensureChart(){
 if(chart)return;
 chart=LightweightCharts.createChart(document.getElementById('chart'),{layout:{background:{color:'#0e1116'},textColor:'#d6dae0'},grid:{vertLines:{color:'#1c2128'},horzLines:{color:'#1c2128'}},timeScale:{timeVisible:true,secondsVisible:false},
   // K线只占上 72%, 给成交量腾出下面一整条 —— 原来两者叠在一起, 量柱糊在K线里看不清
   rightPriceScale:{borderColor:'#30363d',scaleMargins:{top:0.06,bottom:0.28}}});
 candle=chart.addCandlestickSeries({upColor:'#3fb950',downColor:'#f85149',wickUpColor:'#3fb950',wickDownColor:'#f85149',borderVisible:false});
 vol=chart.addHistogramSeries({priceFormat:{type:'volume'},priceScaleId:'vol'});
 chart.priceScale('vol').applyOptions({scaleMargins:{top:0.78,bottom:0.02}});   // 成交量独占下 20%
 new ResizeObserver(()=>chart.applyOptions({width:document.getElementById('chart').clientWidth,height:document.getElementById('chart').clientHeight})).observe(document.getElementById('chart'));
}
let curSig=null;
function snap(times,t){ // 把标记时间吸附到 ≤t 的最近一根bar开盘(切级别后标记仍能落上)
 if(!times.length)return t; let lo=0,hi=times.length-1,res=times[0];
 while(lo<=hi){const m=(lo+hi)>>1; if(times[m]<=t){res=times[m];lo=m+1;}else hi=m-1;} return res;
}
function defTf(s){ const t=(META[s.strat]||{}).tf||''; return t.indexOf('15m')===0?'15m':'5m'; }
async function show(id){
 const s=ALL.find(x=>x.id===id); if(!s)return;
 document.querySelectorAll('tr.row').forEach(r=>r.classList.toggle('sel',+r.dataset.id===id));
 curSig=s;
 closeDrawer();          // 手机: 选完信号收起抽屉, 把整屏让给K线图
 renderSig(s, defTf(s));
}
function switchTf(tf){ if(curSig) renderSig(curSig, tf); }
let LABELS={};
async function loadLabels(){ try{ LABELS=await (await fetch('/api/labels')).json(); }catch(e){} }
function labelKey(s){ return `${s.strat}|${s.symbol}|${s.t}|${s.dir}`; }
async function saveLabel(verdict){
 if(!curSig)return;
 const reason=(document.getElementById('lreason2').value||document.getElementById('lreason').value||'').trim();
 if(verdict==='bad'&&!reason){ alert('标👎请填一个理由(理由=以后收紧公式的依据)'); return; }
 try{
  const r=await (await fetch('/api/label',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:curSig.id,verdict,reason})})).json();
  if(r.ok){ LABELS[labelKey(curSig)]={v:verdict,r:reason}; showLabelState(curSig); document.getElementById('lreason2').value=''; document.getElementById('lreason').value=''; }
 }catch(e){ alert('打标失败'); }
}
function showLabelState(s){
 const l=LABELS[labelKey(s)];
 document.getElementById('lstate').innerHTML = l ? (l.v==='good'?'已标 👍符合':('已标 👎不符合'+(l.r?(' · '+l.r):''))) : '未打标';
}
async function saveCase(){
 if(!curSig)return;
 const label=prompt('给这个案例打个标签(如 经典小转大 / 反例-假突破):','');
 if(label===null)return;
 try{
  const r=await (await fetch('/api/save_case',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:curSig.id,label})})).json();
  alert(r.ok?('已保存: '+r.path):('失败: '+(r.error||'?')));
 }catch(e){ alert('保存失败'); }
}
async function renderSig(s, tf){
 ensureChart();
 document.getElementById('title').textContent=`${s.symbol} 加载中…`;   // 别让用户对着空白猜是不是点坏了
 let kl;
 try{ kl=await (await fetch(`/api/klines?symbol=${s.symbol}&center=${s.t}&span=120&tf=${tf}`)).json(); }
 catch(e){ document.getElementById('title').textContent=`${s.symbol} K线加载失败`; return; }
 if(!kl.length){ document.getElementById('title').textContent=`${s.symbol} 无K线数据(缓存里没这个币?)`; return; }
 // 按币价定小数位(低价币否则全显示成 0.00, 没法复核)
 const dig=Math.min(8,Math.max(2,Math.ceil(-Math.log10(s.entry||1))+4));
 candle.applyOptions({priceFormat:{type:'price',precision:dig,minMove:Math.pow(10,-dig)}});
 candle.setData(kl.map(k=>({time:k.t,open:k.o,high:k.h,low:k.l,close:k.c})));
 vol.setData(kl.map(k=>({time:k.t,value:k.v,color:k.c>=k.o?'#2ea043cc':'#f85149aa'})));
 lines.forEach(l=>candle.removePriceLine(l)); lines=[];
 let ex={}; try{ ex=typeof s.extra==='string'?JSON.parse(s.extra):(s.extra||{}); }catch(e){}
 const st=ex.structure;
 const PL=(p,c,t)=>{if(p)lines.push(candle.createPriceLine({price:p,color:c,lineWidth:1,lineStyle:2,axisLabelVisible:true,title:t}));};
 const slTitle=(ex.path==='macro_fvg')?(s.dir==='long'?'止损=一买L1':'止损=一卖H1'):'止损';
 PL(s.entry,'#58a6ff','入场');PL(s.sl,'#f85149',slTitle);PL(s.tp,'#3fb950','止盈 1:3');   // 切级别后价位线保留
 const times=kl.map(k=>k.t);
 const mk=[];
 const res=s.result==='tp'?'✓':s.result==='sl'?'✗':'';
 // FVG: 一买后上涨一笔里的三K缺口, 画成色块; 二买必须回落进色块且不跌穿下沿
 if(fvgBox){ try{ candle.detachPrimitive(fvgBox); }catch(e){} fvgBox=null; }
 if(ex.fvg && ex.fvg.lo!=null){
   const g=ex.fvg, long=s.dir==='long';
   const t1=snap(times,Math.floor(g.time/1000));
   const t2=snap(times, st&&st.entry_time?Math.floor(st.entry_time/1000):s.t);
   fvgBox=new FvgPrimitive(t1,t2,g.lo,g.hi,long);
   candle.attachPrimitive(fvgBox);
   PL(long?g.lo:g.hi, long?'#3fb950':'#f85149', long?'FVG下沿(不可跌穿)':'FVG上沿(不可升穿)');
   mk.push({time:t1,position:long?'aboveBar':'belowBar',color:long?'#3fb950':'#f85149',shape:'square',text:'FVG'});
 }
 if(st && (st.H1!=null || st.L1!=null)){
   const long=s.dir==='long', above='aboveBar', below='belowBar', pos=long?below:above;
   const PLd=(p,c,t)=>{if(p!=null)lines.push(candle.createPriceLine({price:p,color:c,lineWidth:1,lineStyle:3,axisLabelVisible:true,title:t}));};
   if(long){ // 二买: L1底分型 / L2底分型 / 爆量K@L1 / 买入
     if(st.L1_time){ mk.push({time:snap(times,Math.floor(st.L1_time/1000)),position:below,color:'#ff7043',shape:'square',text:`爆量K${s.vol_ratio?(' '+s.vol_ratio+'x'):''}`}); mk.push({time:snap(times,Math.floor(st.L1_time/1000)),position:below,color:'#4f8ef7',shape:'circle',text:'L1底分型'}); PLd(st.L1,'#4f8ef7','L1底'); }
     if(st.L2_time){ mk.push({time:snap(times,Math.floor(st.L2_time/1000)),position:below,color:'#4f8ef7',shape:'circle',text:'L2底分型'}); PLd(st.L2,'#4f8ef7','L2底'); }
   } else { // 二卖: H1顶分型 / H2顶分型 / 爆量K@H1 / 卖出
     if(st.H1_time){ mk.push({time:snap(times,Math.floor(st.H1_time/1000)),position:above,color:'#ff7043',shape:'square',text:`爆量K${s.vol_ratio?(' '+s.vol_ratio+'x'):''}`}); mk.push({time:snap(times,Math.floor(st.H1_time/1000)),position:above,color:'#4f8ef7',shape:'circle',text:'H1顶分型'}); PLd(st.H1,'#4f8ef7','H1顶'); }
     if(st.H2_time){ mk.push({time:snap(times,Math.floor(st.H2_time/1000)),position:above,color:'#4f8ef7',shape:'circle',text:'H2顶分型'}); PLd(st.H2,'#4f8ef7','H2顶'); }
   }
   const et=st.entry_time?Math.floor(st.entry_time/1000):s.t;
   mk.push({time:snap(times,et),position:pos,color:'#ffd700',shape:long?'arrowUp':'arrowDown',text:(long?'二买':'二卖')+res});
 } else {
   if(s.anchor)mk.push({time:snap(times,Math.floor(s.anchor/1000)),position:'belowBar',color:'#d29922',shape:'circle',text:'锚'+(s.climaxX?(' '+s.climaxX+'x'):'')});
   mk.push({time:snap(times,s.t),position:s.dir==='long'?'belowBar':'aboveBar',color:s.dir==='long'?'#3fb950':'#f85149',shape:s.dir==='long'?'arrowUp':'arrowDown',text:(s.dir==='long'?'买':'卖')+res});
 }
 candle.setMarkers(mk.sort((a,b)=>a.time-b.time));
 chart.timeScale().fitContent();
 const m=META[s.strat]||{};
 // 触发级别标出来: 带★的那个才是这条信号真正被打出来的级别, 其余是你换着眼睛看
 document.getElementById('tfsw').innerHTML=['5m','15m','1h'].map(x=>`<button onclick="switchTf('${x}')" title="${x===s.tf?'这条信号的触发级别':'仅切换视图'}" style="padding:2px 8px;margin-right:3px;border:1px solid ${x===s.tf?'#1f4b7a':'#30363d'};border-radius:4px;background:${x===tf?'#243447':'#161b22'};color:#d6dae0">${x}${x===s.tf?' ★':''}</button>`).join('')
   +`<span class=muted style="margin-left:6px;font-size:11px">★=触发级别</span>`;
 document.getElementById('savebtn').style.display='inline-block';
 document.getElementById('labelbar').style.display='block'; showLabelState(s);
 document.getElementById('title').innerHTML=`<b>${s.symbol}</b> · <span class=badge>${m.label||s.strat}</span> <span class="tfb tf-${s.tf}">${s.tf}触发</span> · ${s.dir==='long'?'做多':'做空'} · ${fmt(s.t)}`;
 const px=v=>v==null?'-':(+v).toFixed(dig);   // 别把 37.41857142857143 原样吐出来, 手机上一行能撑成三行
 document.getElementById('info').textContent=`入场${px(s.entry)} 止损${px(s.sl)} 止盈${px(s.tp)} 结果:${s.result==='tp'?'止盈':s.result==='sl'?'止损':'持仓'}${s.pnl_r!=null?(' '+s.pnl_r+'R'):''}`+(s.movePct?` 跌幅${s.movePct}%`:'');
 const d=DETAIL[s.strat]||{};
 document.getElementById('lt').textContent=`策略详情 · ${m.label||s.strat} (${m.tf||''})`;
 let head='';
 if(d.desc) head+=`<div class=li><b>简介</b>: ${d.desc}</div>`;
 if(d.idea) head+=`<div class=li><b>原始想法</b>: ${d.idea}</div>`;
 if(d.updated) head+=`<div class=li><b>更新</b>: ${d.updated} · <b>代码</b>: ${d.code||'?'}${d.doc?` · <b>文档</b>: ${d.doc}`:''}</div>`;
 head+=`<div class=li style="margin-top:4px"><b>当前逻辑</b>:</div>`;
 document.getElementById('ld').innerHTML=head+(m.logic||['(无)']).map(x=>`<div class=li>· ${x}</div>`).join('');
}
load();
if(isMobile()){                       // 手机首屏: 直接把信号抽屉拉开, 省一次点击
  document.getElementById('title').textContent='点 ☰ 信号 选一条查看K线';
  openDrawer();
}
</script></body></html>"""


IDEAS_HTML = """<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>灵感库 · 研究档案</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 :root{--bg:#0e1116;--surface:#161b22;--line:#262d36;--ink:#d6dae0;--muted:#8b949e;--accent:#58a6ff;--warn:#d29922;--ok:#3fb950;--bad:#f85149}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
      display:flex;height:100vh;height:100dvh}
 a{color:var(--accent)}
 /* ---- 左: 灵感列表 ---- */
 #left{width:340px;flex:none;border-right:1px solid var(--line);overflow:auto;padding:14px}
 h1{font:600 17px/1.3 system-ui;margin:0 0 2px}
 .sub{color:var(--muted);font-size:12px;margin-bottom:12px}
 .tabs{display:flex;gap:6px;margin-bottom:12px}
 .tabs button{flex:1;border:1px solid var(--line);background:var(--surface);color:var(--ink);
   padding:6px 10px;border-radius:999px;font-size:13px;cursor:pointer}
 .tabs button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#06121f;font-weight:600}
 .item{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 12px;
       margin-bottom:8px;cursor:pointer}
 .item:hover{border-color:#3d4754}
 .item[aria-selected=true]{border-color:var(--accent);background:#132033}
 .t{font-weight:600;font-size:14px}
 .meta{color:var(--muted);font-size:11.5px;margin-top:3px}
 .pill{font-size:10.5px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
 .pill.s-灵感{color:var(--warn);border-color:#5c4813}
 .pill.s-立项,.pill.s-回测中{color:var(--accent);border-color:#1f4b7a}
 .pill.s-已证伪{color:var(--bad);border-color:#6e2725}
 .pill.s-已验证,.pill.s-上线{color:var(--ok);border-color:#1f5c33}
 /* ---- 右: 研究档案(四模块) ---- */
 #right{flex:1;display:flex;flex-direction:column;min-width:0}
 #nav{display:flex;gap:4px;padding:12px 16px 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
 #nav button{border:none;background:none;color:var(--muted);padding:8px 14px;font-size:14px;cursor:pointer;
   border-bottom:2px solid transparent;margin-bottom:-1px}
 #nav button[aria-pressed=true]{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
 #nav .n{font-size:11px;color:var(--muted);background:var(--surface);border-radius:999px;padding:0 6px;margin-left:4px}
 #pane{flex:1;overflow:auto;padding:16px}
 .empty{color:var(--muted);padding:40px;text-align:center}
 .box{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:14px}
 .box h3{margin:0 0 10px;font-size:14px}
 /* markdown 正文 */
 .body img{max-width:100%;border-radius:8px;border:1px solid var(--line);margin:8px 0}
 .body h2,.body h3{font-size:14.5px;margin:16px 0 6px}
 .body h4{font-size:13.5px;margin:14px 0 4px;color:var(--muted)}
 .body table{width:100%;border-collapse:collapse;font-size:12.5px;display:block;overflow-x:auto}
 .body th,.body td{border:1px solid var(--line);padding:5px 8px;text-align:left}
 .body pre{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:10px;overflow-x:auto;font-size:12px}
 .body code{background:#0d1117;padding:1px 5px;border-radius:4px;font-size:12.5px}
 .body blockquote{margin:8px 0;padding:8px 12px;border-left:3px solid var(--warn);background:#1c1a12;color:#e3d9b8}
 .body ul{padding-left:20px} .body hr{border:none;border-top:1px solid var(--line);margin:14px 0}
 /* 原始图: 左静态图 / 右动态K线 */
 .pair{display:grid;grid-template-columns:1fr 1fr;gap:12px}
 .pair .cap{color:var(--muted);font-size:12px;margin-bottom:6px}
 .pair img{width:100%;border-radius:8px;border:1px solid var(--line);cursor:zoom-in}
 .chartbox{height:340px;border:1px solid var(--line);border-radius:8px;background:#0e1116}
 .thumbs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
 .thumbs img{height:56px;border-radius:6px;border:1px solid var(--line);cursor:pointer;opacity:.6}
 .thumbs img[aria-selected=true]{opacity:1;border-color:var(--accent)}
 /* 表单 */
 input,select,textarea{background:#0d1117;color:var(--ink);border:1px solid var(--line);border-radius:6px;
   padding:6px 8px;font:13px system-ui}
 button.act{background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:6px;
   padding:6px 12px;font-size:13px;cursor:pointer}
 button.act:hover{border-color:#3d4754}
 button.ok{color:var(--ok);border-color:#1f5c33}
 button.bad{color:var(--bad);border-color:#6e2725}
 .drop{border:1.5px dashed var(--line);border-radius:10px;padding:18px;text-align:center;color:var(--muted);
   font-size:13px;margin-bottom:10px}
 .drop.hot{border-color:var(--accent);color:var(--accent)}
 .newbtn{width:100%;padding:10px;margin-bottom:12px;border:1px solid var(--accent);border-radius:10px;
   background:#132033;color:var(--accent);font-size:14px;font-weight:600;cursor:pointer}
 .newbtn:hover{background:#18293f}
 /* 新建灵感弹窗 */
 #modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:50;
   align-items:center;justify-content:center;padding:20px}
 #modal.on{display:flex}
 #modal .card{background:var(--surface);border:1px solid var(--line);border-radius:14px;
   width:min(720px,100%);max-height:90dvh;overflow:auto;padding:20px}
 #modal h2{margin:0 0 4px;font-size:17px}
 #modal textarea{width:100%;background:#0d1117;color:var(--ink);border:1px solid var(--line);
   border-radius:8px;padding:10px;font:14px/1.6 system-ui;resize:vertical}
 #npreview img{max-width:100%;border-radius:8px;border:1px solid var(--line);margin-top:8px}
 .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
 /* 回测记录 */
 .kpi{display:flex;gap:18px;flex-wrap:wrap;margin-bottom:10px}
 .kpi div{font-size:12px;color:var(--muted)}
 .kpi b{display:block;font-size:19px;color:var(--ink);font-weight:600}
 .kpi b.pos{color:var(--ok)} .kpi b.neg{color:var(--bad)}
 .verdict{background:#131a24;border-left:3px solid var(--accent);padding:8px 12px;border-radius:0 8px 8px 0;
   font-size:13px;margin-bottom:12px;color:#c9d4e0}
 .sig{border:1px solid var(--line);border-radius:10px;margin-bottom:10px;overflow:hidden}
 .sig>summary{list-style:none;cursor:pointer;padding:9px 12px;display:flex;gap:8px;align-items:center;font-size:13px}
 .sig>summary::-webkit-details-marker{display:none}
 .sig[open]>summary{border-bottom:1px solid var(--line)}
 .sig .st{margin-left:auto;font-size:11px}
 .st.ok{color:var(--ok)} .st.bad{color:var(--bad)} .st.none{color:var(--muted)}
 .tfb{padding:1px 5px;border-radius:4px;font-size:10px;border:1px solid var(--line);color:var(--muted)}
 .tfb.tf-5m{color:var(--accent);border-color:#1f4b7a}
 .tfb.tf-15m{color:var(--warn);border-color:#5c4813}
 .tfb.tf-1h{color:#bc8cff;border-color:#4c3a75}
 @media (max-width:900px){
   body{flex-direction:column}
   #left{width:100%;height:34dvh;border-right:none;border-bottom:1px solid var(--line)}
   .pair{grid-template-columns:1fr}
 }
</style></head><body>
<div id=left>
 <h1>💡 灵感库 · 研究档案</h1>
 <div class=sub>原始图 → 策略迭代 → 回测记录 → 结论。<a href="/">← 回信号</a></div>
 <button class=newbtn onclick="openNew()">➕ 新建灵感（传图 + 写想法）</button>
 <div class=tabs>
  <button id=t-idea aria-pressed=true onclick="tab('idea')">交易策略</button>
  <button id=t-principle aria-pressed=false onclick="tab('principle')">交易准则</button>
  <button id=t-live aria-pressed=false onclick="tab('live')">🔴 线上</button>
 </div>
 <div id=list></div>
</div>
<div id=right>
 <div id=nav></div>
 <div id=pane><div class=empty>← 左边选一条灵感</div></div>
</div>

<!-- 新建灵感: 一张图 + 你的原话 → Claude 据此写出策略 v1 -->
<div id=modal onclick="if(event.target.id==='modal')closeNew()">
 <div class=card>
  <h2>➕ 新建灵感</h2>
  <div class=meta style="margin-bottom:12px">一张原始图 + 你看到了什么。你的原话会一字不改地存进灵感库,
    之后 Claude 据此写出可回测的策略 v1。</div>

  <div class=drop id=ndrop>把图拖进来 / 直接 <b>Ctrl+V 粘贴</b> /
    <label style="color:var(--accent);cursor:pointer">选文件<input type=file accept="image/*" hidden id=nfile></label></div>
  <div id=npreview></div>

  <div class=row style="margin-top:12px">
   <input id=ntitle placeholder="给这个形态起个名(如: 5m放量假破新低反转)" style="flex:1;min-width:220px">
   <input id=nsym placeholder="币种(如 EDGEUSDT)" style="width:150px">
   <select id=ntf><option>5m</option><option>15m</option><option>1h</option><option>1m</option></select>
  </div>
  <div class=row>
   <input id=ntime type="datetime-local" style="width:210px">
   <span class=meta>时间<b>可不填</b> —— 只影响右侧能不能画出那一刻的动态K线, 不挡上传。</span>
  </div>

  <div style="margin-top:10px">
   <div class=meta style="margin-bottom:4px">你看到了什么?（原话, 想到哪写到哪, 不用整理）</div>
   <textarea id=nnote rows=7 placeholder="例: 5分钟标准的反转。我看到的是一个持续下跌的趋势, 最后放量向下砸的时候价格被拉回; 之后尝试再创新低, 结果创不了新低, 然后价格开始往回走, 把那条向下打穿的K线吞没, 又回到了前面……"></textarea>
  </div>

  <div class=row style="margin-top:12px">
   <button class=act onclick=submitNew() style="background:var(--accent);color:#06121f;border-color:var(--accent);font-weight:600">创建灵感</button>
   <button class=act onclick=closeNew()>取消</button>
   <span class=meta id=nmsg></span>
  </div>
 </div>
</div>
<script>
let D={ideas:[],principles:[]}, cur='idea', SEL=null, DET=null, MOD='origin';
let chart, candle, curChartSym='', curChartTf='5m', curChartCenter=0;
let SCANNERS=[], COVERAGE={months:[]}, ONLY_NEW=true;   // 默认只看没判过的点位

const MODULES=[['origin','原始图'],['iter','策略迭代'],['bt','回测记录'],['concl','结论']];

function tab(k){
 cur=k;['idea','principle','live'].forEach(x=>document.getElementById('t-'+x).setAttribute('aria-pressed',String(x===k)));
 if(k==='live'){ renderList(); showLive(); return; }
 renderList();
}

function renderList(){
 if(cur==='live'){
   document.getElementById('list').innerHTML=
     `<div class=item aria-selected=true><div class=t>🔴 线上策略 · 实盘触发</div>
      <div class=meta>VPS 上 live 系统真实推送过的单子(非本地模拟)</div></div>`;
   return;
 }
 const rows=cur==='idea'?D.ideas:D.principles;
 document.getElementById('list').innerHTML = rows.length ? rows.map(r=>`
  <div class=item aria-selected="${SEL===r.slug}" onclick="pick('${r.slug}','${r.kind}')">
   <div class=t>${r.id?('#'+r.id+' '):''}${r.title}</div>
   <div class=meta><span class="pill s-${r.status}">${r.status}</span>
    ${r.date||''}${r.symbol?(' · '+r.symbol):''}${(r.tags||[]).length?(' · '+r.tags.join(' / ')):''}</div>
  </div>`).join('') : '<div class=empty>还没有内容。</div>';
}

/* ---- FVG 色块(lightweight-charts v4 series primitive: 真矩形, 不是两条线) ----
   与主看图器那份同源; ideas 页是另一段独立 HTML, 拿不到那边的类, 故此处再放一份。 */
class FvgRenderer{
 constructor(p1,p2,fill,edge,dash){this._p1=p1;this._p2=p2;this._fill=fill;this._edge=edge;this._dash=dash;}
 draw(target){
  if(this._p1.x===null||this._p2.x===null||this._p1.y===null||this._p2.y===null)return;
  target.useBitmapCoordinateSpace(scope=>{
   const ctx=scope.context, hr=scope.horizontalPixelRatio, vr=scope.verticalPixelRatio;
   const x1=Math.round(Math.min(this._p1.x,this._p2.x)*hr), x2=Math.round(Math.max(this._p1.x,this._p2.x)*hr);
   const y1=Math.round(Math.min(this._p1.y,this._p2.y)*vr), y2=Math.round(Math.max(this._p1.y,this._p2.y)*vr);
   ctx.fillStyle=this._fill; ctx.fillRect(x1,y1,x2-x1,Math.max(y2-y1,1*vr));
   ctx.strokeStyle=this._edge; ctx.lineWidth=1*vr;
   if(this._dash) ctx.setLineDash([3*hr,3*hr]);
   ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y1); ctx.moveTo(x1,y2); ctx.lineTo(x2,y2); ctx.stroke();
   ctx.setLineDash([]);
  });
 }
}
class FvgView{
 constructor(src){this._src=src;this._p1={x:null,y:null};this._p2={x:null,y:null};}
 update(){
  const s=this._src._series, ts=this._src._chart.timeScale();
  this._p1={x:ts.timeToCoordinate(this._src._t1),y:s.priceToCoordinate(this._src._lo)};
  this._p2={x:ts.timeToCoordinate(this._src._t2),y:s.priceToCoordinate(this._src._hi)};
 }
 renderer(){return new FvgRenderer(this._p1,this._p2,this._src._fill,this._src._edge,this._src._dash);}
}
class FvgPrimitive{
 constructor(t1,t2,lo,hi,long,tier){
  this._t1=t1;this._t2=t2;this._lo=lo;this._hi=hi;
  const col=long?'63,185,80':'248,81,73';
  // tier: hit=回踩真的进了这个缺口(策略关心的那个) / plain=没被回踩 / dead=已被跌穿(失效)
  this._fill = tier==='hit'?`rgba(${col},0.26)` : tier==='dead'?'rgba(139,148,158,0.07)' : `rgba(${col},0.10)`;
  this._edge = tier==='hit'?`rgba(${col},0.85)` : tier==='dead'?'rgba(139,148,158,0.35)' : `rgba(${col},0.35)`;
  this._dash = tier!=='hit';
  this._view=new FvgView(this);
 }
 setEnd(t){ this._t2=t; if(this._req) this._req(); }
 attached(p){this._series=p.series;this._chart=p.chart;this._req=p.requestUpdate;}
 detached(){}
 updateAllViews(){this._view.update();}
 paneViews(){return [this._view];}
}
/* 三根K线失衡区。口径与 scripts/strat_macrofvg.py:47-66 的 find_fvgs 一致:
   看涨 low[i+1] > high[i-1] → [high[i-1], low[i+1]]; 看跌 high[i+1] < low[i-1] → [high[i+1], low[i-1]]
   锚点时间取第一根(i-1)的开盘时间; 最小宽度按中点百分比过滤(min_gap_pct 同名同义)。 */
function findFvgs(kl,long,minGapPct){
 const out=[];
 for(let i=1;i<kl.length-1;i++){
  const lo = long ? kl[i-1].h : kl[i+1].h;
  const hi = long ? kl[i+1].l : kl[i-1].l;
  if(!(hi>lo)) continue;
  const mid=(hi+lo)/2;
  if(mid<=0 || (hi-lo)/mid*100 < minGapPct) continue;
  out.push({t:kl[i-1].t, i, lo, hi});
 }
 return out;
}

/* ---------- 🔴 线上策略: 实盘真实打出的最近50笔 ---------- */
let LIVE=null;
let FVG_ON=true, FVG_MINPCT=0.05;
async function showLive(){
 SEL=null;
 document.getElementById('nav').innerHTML='';
 document.getElementById('pane').innerHTML='<div class=empty>加载中…</div>';
 LIVE=await (await fetch('/api/live')).json();
 renderLive();
}
let LIVE_ONLY_NEW=true;
function renderLive(){
 const d=LIVE||{}, rows=d.rows||[];
 const age=d.synced_at?Math.round((Date.now()/1000-d.synced_at)/60):null;
 const done=rows.filter(r=>r.mine);
 const myWin=done.filter(r=>r.mine.verdict==='win').length;
 const head=`<div class=box>
   <h3>🔴 线上策略 · 逐根盲测回放
     <span class=meta style="font-weight:400">${d.synced_at?`同步于 ${age} 分钟前`:'未同步'}</span>
     <button class=act style="float:right" onclick=refreshLive()>刷新(拉最近一周)</button></h3>
   <div class=kpi>
    <div>本周触发<b>${rows.length}</b></div>
    <div>你已走完<b>${done.length}</b></div>
    <div>你的手动胜率<b class="${done.length&&myWin/done.length>0.5?'pos':''}">${done.length?Math.round(myWin/done.length*100)+'%':'—'}</b></div>
    <div>触发反馈<b>${d.n_fb||0}</b>/${rows.length}</div>
    <div>标了问题<b class="${d.n_fb_bad?'neg':''}">${d.n_fb_bad||0}</b></div>
   </div>
   <div class=row style="margin-top:6px">
     <button class=act onclick=recheckLive()>🔁 本地重测(用改后的策略再猜一遍)</button>
     <span class=meta id=rkmsg>${d.recheck?('上次重测 '+d.recheck.at+' · '+d.recheck.msg):'改完策略后点这里, 看是否只剩你满意的触发'}</span>
   </div>
   ${Object.keys(d.fb_tags||{}).length?`<div class=meta style="margin-top:4px">拒因分布: ${
     Object.entries(d.fb_tags).sort((a,b)=>b[1]-a[1]).map(([k,v])=>k+'×'+v).join(' · ')}</div>`:''}
   <div class=verdict>🔒 <b>盲测规则</b>: 每笔只画到【触发那一刻】, 后面的K线要你自己一根根往后拉。
     你亲自决定在哪根止盈、哪根止损 —— 走完才对比策略实际结果。<b>别偷看后市</b>, 这是练手感。</div>
   <div class=row><label class=meta style="cursor:pointer"><input type=checkbox ${LIVE_ONLY_NEW?'checked':''}
      onchange="LIVE_ONLY_NEW=this.checked;renderLive()"> 只看没走过的</label>
     <label class=meta style="cursor:pointer;margin-left:14px"><input type=checkbox ${FVG_ON?'checked':''}
      onchange="toggleFvg(this.checked)"> FVG 色块</label>
     <label class=meta style="margin-left:8px">最小宽度
       <input type=number step=0.01 min=0 value="${FVG_MINPCT}" onchange="setFvgPct(this.value)"
        style="width:62px;background:#0e1116;border:1px solid #30363d;border-radius:5px;color:#d6dae0;padding:2px 5px">%</label>
     <span class=meta id=lmsg></span></div>
   <details style="margin-top:6px"><summary style="cursor:pointer;color:var(--muted);font-size:12px">📉 策略自己的成绩(走之前别看, 会有先入为主)</summary>
     <div class=kpi style="margin-top:6px">
      <div>线上累计<b>${d.total??'—'}</b></div><div>已结算<b>${d.n_closed??'—'}</b></div>
      <div>胜率<b>${d.win_rate!=null?d.win_rate+'%':'—'}</b></div>
      <div>期望(未扣费)<b class="${(d.exp_r||0)>0?'pos':'neg'}">${d.exp_r!=null?(d.exp_r>0?'+':'')+d.exp_r+'R':'—'}</b></div></div></details>
   </div>`;
 if(!rows.length) return void(document.getElementById('pane').innerHTML=head+
   `<div class=box><div class=meta>${d.msg||'没有信号。点「刷新」从 VPS 拉最近一周。'}</div></div>`);
 const cut=d.kline_until||0;
 const shown=rows.map((r,ix)=>[r,ix]).filter(([r])=>!LIVE_ONLY_NEW||!r.mine);
 const list=shown.map(([r,ix])=>{
   const stale=cut&&r.created_at>cut;
   const mine=r.mine;
   const badge=mine?(mine.verdict==='win'?`<span class="st ok">你走: +${mine.my_r}R</span>`
                    :mine.verdict==='loss'?`<span class="st bad">你走: ${mine.my_r}R</span>`
                    :`<span class="st none">你走: 平</span>`)
                  :`<span class="st none">🔒 待你走</span>`;
   const fbBadge=r.fb?(r.fb.verdict==='ok'?`<span class="st ok">触发✓</span>`
                                          :`<span class="st bad">触发✗</span>`):'';
   const rk=(LIVE.recheck&&LIVE.recheck.per)?LIVE.recheck.per[String(r.id)]:null;
   const rkBase=(LIVE.recheck&&LIVE.recheck.base_per)?LIVE.recheck.base_per[String(r.id)]:null;
   const fbv=r.fb&&r.fb.verdict;
   let rkBadge='';
   if(rk){
     if(rkBase&&rkBase!=='hit') rkBadge=`<span class="st none">本地不复现</span>`;
     else if(rk==='hit') rkBadge=fbv==='bad'?`<span class="st bad">问题仍在</span>`:`<span class="st none">仍触发</span>`;
     else if(rk==='no_data') rkBadge=`<span class="st none">缺数据</span>`;
     else rkBadge=fbv==='ok'?`<span class="st bad">⚠误杀</span>`
                 :fbv==='bad'?`<span class="st ok">已消除✓</span>`:`<span class="st none">已消失</span>`;
   }
   return `<details class=sig data-ix="${ix}">
    <summary onclick="setTimeout(()=>startReplay(${ix},${stale?1:0}),50)">
     <b>${r.symbol}</b> <span class="tfb tf-${r.tf}">${r.tf}</span>
     <span style="color:${r.direction==='long'?'#3fb950':'#f85149'}">${r.direction==='long'?'多':'空'}</span>
     <span class=meta>${new Date(r.created_at*1000).toLocaleString('zh-CN')}</span>
     <span class=meta>· ${r.track||'-'}</span>${badge}${fbBadge}${rkBadge}</summary>
    <div style="padding:10px 12px" id="rep_${ix}">
     <div class=chartbox id="lc_${ix}" style="height:340px"></div>
     <div id="rc_${ix}"></div>
     ${fbHtml(r,ix)}
    </div></details>`;
 }).join('');
 document.getElementById('pane').innerHTML=head+`<div class=box><h3>最近一周 ${rows.length} 笔 · 待走 ${rows.length-done.length}</h3>${list||'<div class=meta>都走完了。去掉「只看没走过的」可回看。</div>'}</div>`;
}
async function refreshLive(){
 const m=document.getElementById('lmsg'); m.textContent='⏳ SSH 拉最近一周…';
 const r=await (await fetch('/api/live/refresh',{method:'POST'})).json();
 const poll=setInterval(async()=>{
   const j=await (await fetch('/api/job?id='+encodeURIComponent(r.job))).json();
   m.textContent=(j.state==='running'?'⏳ ':(j.state==='error'?'❌ ':'✅ '))+(j.msg||'');
   if(j.state!=='running'){ clearInterval(poll); if(j.state==='done') showLive(); }
 },1500);
}

/* ---- 触发精准性反馈: 判定触发点本身对不对(和走单盈亏无关) ---- */
const FB_TAGS=['没成笔(不足5根去包含K)','二买不成立(未创更高低点)','一买/spring不成立',
               '爆量K不对','入场太追','位置不对','纯噪音'];
function fbHtml(r,ix){
 const f=r.fb||{};
 const tags=(f.tags||[]);
 return `<div id="fb_${ix}" style="border-top:1px solid #1c2128;margin-top:8px;padding-top:8px">
  <div class=meta style="margin-bottom:4px"><b>这个触发点对吗?</b>(判定的是策略猜点的精准性, 不是这单赚不赚)</div>
  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">${FB_TAGS.map(t=>
    `<label style="border:1px solid ${tags.includes(t)?'#d29922':'#30363d'};color:${tags.includes(t)?'#d29922':'#8b949e'};border-radius:6px;padding:2px 8px;cursor:pointer;font-size:12px">
      <input type=checkbox value="${t}" ${tags.includes(t)?'checked':''} style="display:none"
        onchange="this.parentNode.style.borderColor=this.checked?'#d29922':'#30363d';this.parentNode.style.color=this.checked?'#d29922':'#8b949e'">${t}</label>`).join('')}</div>
  <div style="display:flex;gap:8px;align-items:center">
   <input class=fbnote placeholder="备注(如: 这里其实没成笔)" value="${(f.note||'').replace(/"/g,'&quot;')}"
     style="flex:1;background:#0e1116;border:1px solid #30363d;border-radius:6px;color:#d6dae0;padding:5px 8px;font-size:12px">
   <button class=act style="border-color:#238636;color:#3fb950" onclick="saveFb(${ix},'ok')">✓ 触发OK</button>
   <button class=act style="border-color:#da3633;color:#f85149" onclick="saveFb(${ix},'bad')">✗ 有问题</button>
   <span class=meta id="fbmsg_${ix}">${f.verdict?('已标: '+(f.verdict==='ok'?'✓':'✗')+' '+(f.at||'')):''}</span>
  </div></div>`;
}
async function saveFb(ix,verdict){
 const r=LIVE.rows[ix], box=document.getElementById('fb_'+ix);
 const tags=[...box.querySelectorAll('input[type=checkbox]:checked')].map(c=>c.value);
 const note=box.querySelector('.fbnote').value;
 await fetch('/api/live/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({id:r.id,symbol:r.symbol,verdict,tags,note})});
 r.fb={verdict,tags,note,at:new Date().toLocaleTimeString('zh-CN')};
 document.getElementById('fbmsg_'+ix).textContent='已保存 '+(verdict==='ok'?'✓':'✗');
 LIVE.n_fb=LIVE.rows.filter(x=>x.fb).length;
 LIVE.n_fb_bad=LIVE.rows.filter(x=>x.fb&&x.fb.verdict==='bad').length;
}
async function recheckLive(){
 const m=document.getElementById('rkmsg'); m.textContent='⏳ 本地重测中…';
 const r=await (await fetch('/api/live/recheck',{method:'POST'})).json();
 const poll=setInterval(async()=>{
   const j=await (await fetch('/api/job?id='+encodeURIComponent(r.job))).json();
   m.textContent=(j.state==='running'?'⏳ ':(j.state==='error'?'❌ ':'✅ '))+(j.msg||'');
   if(j.state!=='running'){ clearInterval(poll); if(j.state==='done') showLive(); }
 },1500);
}

/* ---- 逐根步进回放: 图只画到触发, 用户点「下一根」一根根揭晓, 亲自决定止盈止损 ---- */
const REP={};   // ix -> {chart, series, buf, shown, entry, risk, dir, dig, sig}
async function startReplay(ix, stale){
 const el=document.getElementById('lc_'+ix); if(!el||el._init) return;
 el._init=true;
 const r=LIVE.rows[ix];
 if(stale){ el.innerHTML=`<div class=meta style="padding:16px">这条(${new Date(r.created_at*1000).toLocaleString('zh-CN')})比本地K线还新, 跑 bt_refresh 才画得出。</div>`; return; }
 // 盲测边界: created_at = 入场K收盘那一刻(评估时刻)。必须传 cut, 只给【在它之前
 // 已收盘】的K —— 否则 bisect_left(center) 会把"正在走、要到触发之后才收盘"的那根
 // 也画上去(实测176条全中, created_at没对齐bar的53条漏2根), 盲测就成了偷看。
 const cut=r.created_at;
 const pre=await (await fetch(`/api/klines?symbol=${r.symbol}&center=${r.created_at}&span=90&tf=${r.tf}&after=0&cut=${cut}`)).json();
 const post=await (await fetch(`/api/klines?symbol=${r.symbol}&center=${r.created_at}&span=0&tf=${r.tf}&after=200&cut=${cut}`)).json();
 if(!pre.length){ el.innerHTML='<div class=meta style="padding:16px">本地缓存没有这个币的K线。</div>'; return; }
 const buf=post;   // cut 分支保证 post 紧接 pre 之后, 无重叠(旧的 t>created_at 过滤会吞掉第一根)
 const c=LightweightCharts.createChart(el,{layout:{background:{color:'#0e1116'},textColor:'#d6dae0'},
   grid:{vertLines:{color:'#1c2128'},horzLines:{color:'#1c2128'}},
   timeScale:{timeVisible:true,secondsVisible:false},
   rightPriceScale:{borderColor:'#30363d',scaleMargins:{top:0.08,bottom:0.08}}});
 const dig=Math.min(8,Math.max(2,Math.ceil(-Math.log10(r.entry||1))+4));
 const s=c.addCandlestickSeries({upColor:'#3fb950',downColor:'#f85149',wickUpColor:'#3fb950',wickDownColor:'#f85149',
   borderVisible:false,priceFormat:{type:'price',precision:dig,minMove:Math.pow(10,-dig)},
   priceScaleId:'right'});
 c.priceScale('right').applyOptions({scaleMargins:{top:0.06,bottom:0.26}});
 s.setData(pre.map(k=>({time:k.t,open:k.o,high:k.h,low:k.l,close:k.c})));
 // 成交量(独占下 22%)
 const vol=c.addHistogramSeries({priceFormat:{type:'volume'},priceScaleId:'vol'});
 c.priceScale('vol').applyOptions({scaleMargins:{top:0.78,bottom:0.02}});
 vol.setData(pre.map(k=>({time:k.t,value:k.v,color:k.c>=k.o?'#2ea043cc':'#f85149aa'})));
 s.createPriceLine({price:r.entry,color:'#58a6ff',lineWidth:1,lineStyle:0,axisLabelVisible:true,title:'入场'});
 // 结构标记: 策略判定二买/二卖时看到的 一买/一卖(★爆量倍数)、二买/二卖 各在哪根 —— 都在触发之前, 不泄露未来
 const mk=[{time:pre[pre.length-1].t,position:r.direction==='long'?'belowBar':'aboveBar',
   color:'#d29922',shape:r.direction==='long'?'arrowUp':'arrowDown',text:'触发'}];
 (r.markers||[]).forEach(m=>{
   if(!m.t||m.t>r.created_at) return;                 // 只标触发及之前的
   const isFirst=m.label&&m.label.indexOf('1')<0&&(m.label[0]==='H'||m.label[0]==='L');
   const txt=m.vol_ratio?`${m.label} 爆量${(+m.vol_ratio).toFixed(1)}x`:m.label;
   mk.push({time:m.t, position:m.label&&m.label[0]==='H'?'aboveBar':'belowBar',
     color:m.vol_ratio?'#f0883e':'#8b949e', shape:'circle', text:txt});
 });
 mk.sort((a,b)=>a.time-b.time);
 s.setMarkers(mk);
 c.timeScale().fitContent();
 new ResizeObserver(()=>c.applyOptions({width:el.clientWidth,height:el.clientHeight})).observe(el);
 const risk=Math.abs(r.entry-r.sl)||1e-9;
 REP[ix]={chart:c, series:s, vol, buf, shown:0, entry:r.entry, risk, dir:r.direction, dig, sig:r,
          pre, fvgs:[], fvgInfo:''};
 buildFvgs(ix);
 // 结构说明: 策略凭什么判它是二买/二卖 —— 一买/一卖(爆量) → 更低高点/更高低点 → 入场
 const ms=r.markers||[], first=ms.find(m=>m.vol_ratio), second=ms.find(m=>m.label&&(m.label.indexOf('2')>=0||m.label[1]==='2'));
 const isShort=r.direction==='short';
 let struct='';
 if(first&&second){
   const validLH = isShort ? (second.price<first.price) : (second.price>first.price);
   struct=`<div class=meta style="margin-top:6px;line-height:1.8">
     <b>策略凭什么开这一单</b>(${isShort?'威科夫UTAD一卖→缠论二卖':'威科夫Spring一买→缠论二买'}):<br>
     ① ${first.label} ${first.price} <span style="color:#f0883e">爆量${(+first.vol_ratio).toFixed(1)}x</span> ← 就是你圈的那根<br>
     ② ${second.label} ${second.price} ${validLH?`<span style="color:var(--ok)">✓ ${isShort?'更低的高点':'更高的低点'}(缠论${isShort?'二卖':'二买'}成立)</span>`:`<span style="color:var(--bad)">✗ ${isShort?'没比一卖更低':'没比一买更高'}, 结构存疑</span>`}<br>
     ③ 入场 ${r.entry} · 止损 ${r.sl}(${isShort?'H2上方':'L2下方'}) · 止盈 ${r.tp}<br>
     <span id="fvgline_${ix}">${REP[ix].fvgInfo}</span></div>`;
 }
 document.getElementById('rc_'+ix).insertAdjacentHTML('beforebegin',`<div id="st_${ix}">${struct}</div>`);
 // 已经走过的: 直接显示你当时的结果 + 策略结果, 不再重走
 if(r.mine){ revealCompare(ix); return; }
 renderReplayCtl(ix);
}
/* 在回放图上画 FVG 矩形。级别 = 信号自己的 tf(5m单就是5m的FVG)。
   只用 pre(触发及之前已收盘的K)计算 —— 盲测不能拿未来的K去找缺口。
   范围: 从最早的结构标记(爆量K/一买一卖分型)起, 到触发为止的那一段推动。 */
function buildFvgs(ix){
 const R=REP[ix]; if(!R) return;
 (R.fvgs||[]).forEach(p=>{ try{ R.series.detachPrimitive(p); }catch(e){} });
 R.fvgs=[]; R.fvgInfo='';
 const r=R.sig, long=r.direction==='long', kl=R.pre;
 if(!kl||kl.length<3) return;
 // 右端跟随"已揭晓到哪根": 回放中途改参数重建时不能缩回触发点
 const endT=(R.shown&&R.buf[R.shown-1]) ? R.buf[R.shown-1].t : kl[kl.length-1].t;
 const ms=(r.markers||[]).filter(m=>m.t&&+m.t<=r.created_at);
 const secM=ms.find(m=>m.label&&m.label.indexOf('2')>=0);        // L2/H2 = 二买/二卖那根
 const i1=ms.length?kl.findIndex(k=>k.t===Math.min.apply(null,ms.map(m=>+m.t))):-1;
 const i2=secM?kl.findIndex(k=>k.t===+secM.t):-1;
 if(i1<0||i2<=i1){   // 标记对不上就不下"命中"结论, 只说找到几个, 不误导
   const all=findFvgs(kl,long,FVG_MINPCT);
   R.fvgInfo=`④ 这段有 ${all.length} 个${long?'看涨':'看跌'}FVG（<span style="color:var(--muted)">未能定位二买/二卖, 不判定回踩是否命中</span>）`;
   all.forEach(g=>{ const p=new FvgPrimitive(g.t,endT,g.lo,g.hi,long,'plain'); R.fvgs.push(p);
     if(FVG_ON){ try{ R.series.attachPrimitive(p); }catch(e){} } });
   R.fvgAttached=FVG_ON; repaint(R); return;
 }
 // 腿极值(反弹顶/反抽底): 与 macro_pullback.py:174 的 leg_high_idx 同口径
 let legEnd=i1+1;
 for(let x=i1+1;x<=i2;x++){ if(long ? kl[x].h>kl[legEnd].h : kl[x].l<kl[legEnd].l) legEnd=x; }
 // FVG 只在【一买→腿极值】这段推动里找: 三根K要整体落在区间内。
 // 回调段和 L2→入场段里的缺口不算 —— 策略自己也不看那些(strat_macrofvg.py:206 同界)。
 const gaps=findFvgs(kl,long,FVG_MINPCT).filter(g=>g.i-1>=i1 && g.i+1<=legEnd);
 let hitCount=0, hitZone=null;
 gaps.forEach(g=>{
  // 命中窗口 = 【腿极值→二买】那一段回踩, 不是"成型后到入场的所有K"。
  // 旧写法把腿内小回抽也算成"回踩命中", 实测 176 条里 50 条是假阳性。
  const seg=kl.slice(Math.max(g.i+2,legEnd), i2+1);
  const entered=seg.some(k=> long ? k.l<=g.hi : k.h>=g.lo);
  const pierced=seg.some(k=> long ? k.l< g.lo : k.h> g.hi);
  const tier = pierced ? 'dead' : (entered ? 'hit' : 'plain');
  if(tier==='hit'){ hitCount++; if(!hitZone) hitZone=g; }
  const p=new FvgPrimitive(g.t,endT,g.lo,g.hi,long,tier);
  R.fvgs.push(p);
  if(FVG_ON){ try{ R.series.attachPrimitive(p); }catch(e){} }
 });
 R.fvgAttached=FVG_ON;
 repaint(R);
 const d=R.dig;
 R.fvgInfo = gaps.length
   ? (hitZone ? `④ 回踩落在 FVG [${hitZone.lo.toFixed(d)} ~ ${hitZone.hi.toFixed(d)}] 内 <span style="color:var(--ok)">✓ 与流动性缺口重合</span>（推动段共 ${gaps.length} 个${long?'看涨':'看跌'}FVG, 回踩命中 ${hitCount}）`
              : `④ <span style="color:var(--warn)">回踩没进任何 FVG</span>（推动段有 ${gaps.length} 个${long?'看涨':'看跌'}FVG, 回踩时都没碰到或已被跌穿）`)
   : `④ 这段推动里<span style="color:var(--muted)">没有留下 ${long?'看涨':'看跌'}FVG</span>（最小宽度 ${FVG_MINPCT}%）`;
}
/* attachPrimitive/detachPrimitive 只改集合、不触发重绘(v4.1.3 实测),
   改完必须显式请求一次, 否则开关和阈值"点了没反应"。 */
function repaint(R){
 try{ R.chart.applyOptions({}); }catch(e){}
 (R.fvgs||[]).forEach(p=>{ try{ if(p._req) p._req(); }catch(e){} });
}
/* 切换显示 / 改最小宽度: 对所有已展开的图重建 */
function toggleFvg(on){
 FVG_ON=on;
 Object.keys(REP).forEach(ix=>{ const R=REP[ix]; if(!R||!R.fvgs) return;
  if(on===R.fvgAttached) return;
  R.fvgs.forEach(p=>{ try{ on?R.series.attachPrimitive(p):R.series.detachPrimitive(p); }catch(e){} });
  R.fvgAttached=on;
  repaint(R);
 });
}
function setFvgPct(v){
 const n=parseFloat(v); if(!isFinite(n)||n<0) return;
 FVG_MINPCT=n;
 Object.keys(REP).forEach(ix=>{ if(!REP[ix]) return; buildFvgs(ix); refreshStruct(ix); });
}
function refreshStruct(ix){
 const box=document.getElementById('fvgline_'+ix);
 if(box) box.innerHTML=REP[ix]?REP[ix].fvgInfo:'';
}
function curR(ix){
 const R=REP[ix]; if(!R||!R.shown) return 0;
 const last=R.buf[R.shown-1]; const px=last.c;
 return (R.dir==='long'?(px-R.entry):(R.entry-px))/myRisk(R);
}
/* 风险以【你自己设的止损】为准 —— 盈亏用R衡量, 分母必须是你真实承担的风险 */
function myRisk(R){ return Math.abs(R.entry-(R.mySl!=null?R.mySl:R.sig.sl))||1e-9; }

/* 设自己的止损: 数字输入 或 点图取价。设完画一条线, 可反复改(未开走之前) */
function setMySl(ix, price, src){
 const R=REP[ix]; if(!R) return;
 const p=parseFloat(price); if(!isFinite(p)||p<=0) return;
 const long=R.dir==='long';
 if(long ? p>=R.entry : p<=R.entry){ alert(long?'做多的止损要低于入场价':'做空的止损要高于入场价'); return; }
 R.mySl=p; R.slSrc=src||'manual';
 if(R.slLine){ try{ R.series.removePriceLine(R.slLine); }catch(e){} }
 R.slLine=R.series.createPriceLine({price:p,color:'#f0883e',lineWidth:2,lineStyle:0,
   axisLabelVisible:true,title:'我的止损'});
 renderReplayCtl(ix);
}
/* 点图取价: 点一下K线区域, 把那个纵坐标换算成价格当止损 */
function pickSl(ix){
 const R=REP[ix]; if(!R) return;
 if(R.picking){ return; }
 R.picking=true;
 const h=param=>{
   if(!param.point){ return; }
   const p=R.series.coordinateToPrice(param.point.y);
   if(p!=null) setMySl(ix,p,'click');
   R.chart.unsubscribeClick(h); R.picking=false; renderReplayCtl(ix);
 };
 R.chart.subscribeClick(h);
 renderReplayCtl(ix);
}
function renderReplayCtl(ix){
 const R=REP[ix], box=document.getElementById('rc_'+ix);
 const r=curR(ix), atEnd=R.shown>=R.buf.length;
 const bars=R.shown, hrs=(bars*(R.sig.tf==='15m'?15:R.sig.tf==='1h'?60:5)/60).toFixed(1);
 const d=R.dig, sl=(R.mySl!=null?R.mySl:R.sig.sl);
 const slPct=Math.abs(R.entry-sl)/R.entry*100;
 const slTag = R.mySl==null ? '<span style="color:var(--muted)">(暂用策略的)</span>'
              : `<span style="color:var(--warn)">(你设的${R.slSrc==='click'?'·点图':''})</span>`;
 // 止损必须在开走【之前】定 —— 走过之后再改就是拿后见之明调风险, 那数据就废了
 const locked = bars>0;
 box.innerHTML=`
  <div class=row style="margin-top:8px;align-items:center;flex-wrap:wrap">
   <span class=meta>我的止损 <b style="color:#f0883e">${sl.toFixed(d)}</b>(${slPct.toFixed(2)}%) ${slTag}</span>
   ${locked?'<span class=meta style="color:var(--muted)">已开走, 止损锁定</span>':`
     <input type=number step=any value="${sl}" id="slin_${ix}"
       style="width:120px;background:#0e1116;border:1px solid #30363d;border-radius:5px;color:#d6dae0;padding:2px 6px">
     <button class=act onclick="setMySl(${ix},document.getElementById('slin_${ix}').value,'input')">设为止损</button>
     <button class=act onclick="pickSl(${ix})">${R.picking?'👆 点图上任意高度…':'点图设置'}</button>`}
  </div>
  <div class=row style="margin-top:8px;align-items:center">
   <button class=act onclick="stepReplay(${ix},1)" ${atEnd?'disabled':''}>下一根 →</button>
   <button class=act onclick="stepReplay(${ix},5)" ${atEnd?'disabled':''}>快进 5根</button>
   <span class=meta>已走 <b>${bars}</b> 根(~${hrs}小时)　浮动盈亏 <b class="${r>=0?'pos':'neg'}" style="font-size:15px">${r>=0?'+':''}${r.toFixed(2)}R</b></span>
  </div>
  <div class=row style="margin-top:8px">
   <button class="act ${r>=0?'ok':'bad'}" style="font-size:14px" onclick="exitReplay(${ix},'tp')" ${bars?'':'disabled'}>
     ✋ 获利了结 (${r>=0?'+':''}${r.toFixed(2)}R)</button>
   <span class=meta>止损先设好再开走; 走的过程中被打到止损会自动出局。你只管决定"在哪根收手"</span>
   ${atEnd?'<span class=meta style="color:var(--warn)">缓冲用完了(已到最新K线, 这笔还没走完的话就是数据到头了)</span>':''}
  </div>`;
}
function stepReplay(ix,n){
 const R=REP[ix]; if(!R||R.done) return;
 const long=R.dir==='long', sl=(R.mySl!=null?R.mySl:R.sig.sl);
 let last=null, hitSl=false;
 for(let i=0;i<n&&R.shown<R.buf.length;i++){
   const k=R.buf[R.shown++]; last=k;
   R.series.update({time:k.t,open:k.o,high:k.h,low:k.l,close:k.c});
   if(R.vol) R.vol.update({time:k.t,value:k.v,color:k.c>=k.o?'#2ea043cc':'#f85149aa'});
   // 这根K的振幅穿过止损 → 就地出局, 后面的快进不再继续(真实交易里你已经不在场了)
   if(long ? k.l<=sl : k.h>=sl){ hitSl=true; break; }
 }
 if(last) (R.fvgs||[]).forEach(p=>p.setEnd(last.t));   // FVG框跟着已揭晓的K线延伸
 R.chart.timeScale().scrollToRealTime();
 if(hitSl){ exitReplay(ix,'sl'); return; }
 renderReplayCtl(ix);
}
async function exitReplay(ix, kind){
 const R=REP[ix]; if(!R||!R.shown||R.done) return;
 R.done=true;
 const last=R.buf[R.shown-1];
 const mySl=(R.mySl!=null?R.mySl:R.sig.sl);
 // 止损出局按【止损价】成交(不是收盘价), 否则会低估亏损
 const px = kind==='sl' ? mySl : last.c;
 const my_r=+(((R.dir==='long'?(px-R.entry):(R.entry-px))/myRisk(R)).toFixed(3));
 const verdict = my_r>0.05?'win':my_r<-0.05?'loss':'flat';   // 盈亏由R正负自动定, 不再自相矛盾
 const rec={id:R.sig.id, symbol:R.sig.symbol, exit_bar:R.shown, exit_price:px,
            my_r, verdict, note:'', bar_base:'cut',
            exit_kind:kind||'tp',                    // tp=你主动获利了结 / sl=被你设的止损打掉
            my_sl:mySl, sl_source:(R.mySl==null?'strategy':R.slSrc||'manual'),
            my_sl_pct:+(Math.abs(R.entry-mySl)/R.entry*100).toFixed(4),
            strat_sl:R.sig.sl, strat_tp:R.sig.tp, entry:R.entry, tf:R.sig.tf,
            direction:R.dir};
 await fetch('/api/live/replay',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(rec)});
 R.sig.mine=rec;
 revealCompare(ix);
 // 更新列表徽章(把"🔒 待你走"换成你的结果), 但保持当前展开的这条不动
 const sm=document.querySelector(`details.sig[data-ix="${ix}"] > summary`);
 if(sm){ const b=sm.querySelector('.st'); if(b){
   b.className='st '+(rec.verdict==='win'?'ok':rec.verdict==='loss'?'bad':'none');
   b.textContent=(rec.exit_kind==='sl'?'你止损: ':'你走: ')+
     (rec.verdict==='flat'?'平':`${rec.my_r>0?'+':''}${rec.my_r}R`); } }
}
function revealCompare(ix){
 const R=REP[ix], r=R.sig, box=document.getElementById('rc_'+ix);
 // 揭晓: 把策略计划的止损止盈画出来 + 策略实际结果
 [['止损',r.sl,'#f85149'],['止盈',r.tp,'#3fb950']].forEach(([t,p,col])=>{
   if(p) R.series.createPriceLine({price:p,color:col,lineWidth:1,lineStyle:2,axisLabelVisible:true,title:t+'(策略)'}); });
 const stratRes=r.result==='tp'?'止盈':r.result==='sl'?'止损':r.result==='rev'?'反转平':r.result?'超时':'仍持仓';
 const mine=r.mine;
 const myTxt=`${mine.my_r>0?'+':''}${mine.my_r}R ${mine.verdict==='win'?'(赚)':mine.verdict==='loss'?'(亏)':'(平)'}`;
 box.innerHTML=`<div class=verdict style="margin-top:8px">
   <b>你走的</b>: 第 ${mine.exit_bar} 根离场 · ${myTxt}<br>
   <b>策略实际</b>: ${stratRes}${r.pnl_r!=null?` · ${r.pnl_r>0?'+':''}${r.pnl_r}R`:''}
   ${r.reason?`<br><span class=meta>信号: ${r.reason}</span>`:''}</div>`;
}

async function pick(slug, kind){
 SEL=slug; renderList();
 if(kind==='principle'){
   const p=D.principles.find(x=>x.slug===slug);
   document.getElementById('nav').innerHTML='';
   document.getElementById('pane').innerHTML=`<div class="box body">${p.body_html}</div>`;
   return;
 }
 document.getElementById('pane').innerHTML='<div class=empty>加载中…</div>';
 DET=await (await fetch('/api/idea/'+slug)).json();
 MOD='origin'; renderNav(); renderPane();
}

function renderNav(){
 const n={origin:(DET.charts||[]).length, iter:(DET.versions||[]).length, bt:(DET.backtests||[]).length, concl:0};
 document.getElementById('nav').innerHTML=MODULES.map(([k,label])=>
   `<button aria-pressed="${MOD===k}" onclick="go('${k}')">${label}${n[k]?`<span class=n>${n[k]}</span>`:''}</button>`).join('');
}
function go(k){ MOD=k; renderNav(); renderPane(); }

function renderPane(){
 const p=document.getElementById('pane');
 if(MOD==='origin') p.innerHTML=viewOrigin();
 else if(MOD==='iter') p.innerHTML=viewIter();
 else if(MOD==='bt') p.innerHTML=viewBt();
 else p.innerHTML=viewConcl();
 if(MOD==='origin'){ const c=(DET.charts||[]).find(x=>x.symbol&&x.center); if(c) showChart(c.symbol,c.tf||'5m',c.center); }
}

/* ---------- ① 原始图: 左上传的静态图 / 右可切周期的动态K线 ---------- */
function viewOrigin(){
 const cs=DET.charts||[], i=DET.idea||{};
 const thumbs=cs.map((c,ix)=>`<img src="/research/asset?p=${encodeURIComponent(c.path)}"
    aria-selected="${ix===0}" onclick="selThumb(${ix})" title="${c.note||c.file}">`).join('');
 const first=cs[0];
 const canDraw=first&&first.symbol&&first.center;
 return `
 <div class=box>
  <h3>📤 给这条灵感补一张图</h3>
  <div class=meta style="margin-bottom:8px">新的形态想法请用左上角「➕ 新建灵感」—— 那才是"传图+写想法→生成新策略"的入口。这里只是给 <b>#${i.id||''}</b> 补充图例。</div>
  <div class=drop id=drop>把图拖进来 / 直接 Ctrl+V 粘贴 / <label style="color:var(--accent);cursor:pointer">选文件<input type=file accept="image/*" hidden id=fpick></label></div>
  <div class=row>
   <input id=usym placeholder="币种 如 EDGEUSDT" value="${i.symbol||''}" style="width:150px">
   <select id=utf><option>5m</option><option>15m</option><option>1h</option><option>1m</option></select>
   <input id=utime type="datetime-local" title="这张图对应的时间(右侧据此画K线; 可不填)">
   <input id=unote placeholder="这张图说明什么(可选)" style="flex:1;min-width:120px">
   <button class=act onclick="doUpload()">上传</button>
  </div>
  <div class=meta id=upmsg>时间<b>可不填</b>, 只影响右侧能不能画出那一刻的动态K线。
    从截图反推是哪个币哪一刻属于图像识别 —— 做不到, 不猜, 所以要你给。</div>
 </div>
 <div class=box>
  <h3>🖼 左: 你的原始图　|　右: 同一时刻的动态K线</h3>
  ${cs.length?`<div class=thumbs id=thumbs>${thumbs}</div>`:'<div class=meta>还没有图。</div>'}
  <div class=pair>
   <div><div class=cap id=capL>原始图(静态)</div>
        <div id=staticimg>${first?`<img src="/research/asset?p=${encodeURIComponent(first.path)}" onclick="window.open(this.src)">`:'<div class=meta>—</div>'}</div></div>
   <div><div class=cap>动态K线 ${['5m','15m','1h'].map(t=>`<button class=act style="padding:2px 8px;margin-left:4px" onclick="switchTf('${t}')">${t}</button>`).join('')}</div>
        <div class=chartbox id=chartbox></div>
        <div class=meta id=chartmsg>${canDraw?'':'这张图没记 币种+时间, 画不出对应K线 —— 重新上传时填上即可。'}</div></div>
  </div>
 </div>

 <!-- 点位描述 → 本机 Claude → 筛选条件 → Python 代码 → 首版策略 -->
 <div class=box>
  <h3>✍️ 这个点位, 你看到了什么</h3>
  <div class=meta style="margin-bottom:8px">用大白话写清楚"我要的入场点长什么样"。
   写完点下面的按钮, <b>本机的 Claude</b> 会把它拆成可判定的筛选条件, 再写成能回测的 Python 代码,
   自动放进「策略迭代」的首版。</div>
  <textarea id=edesc rows=6 style="width:100%;background:#0d1117;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:10px;font:14px/1.6 system-ui"
    placeholder="例: 持续下跌的趋势, 最后放量向下砸的时候价格被拉回; 之后尝试再创新低但创不了, 然后价格开始往回走, 把那条向下打穿的K线吞没, 又回到前面。">${(DET.desc||'').replace(/</g,'&lt;')}</textarea>
  <div class=row style="margin-top:10px">
   <select id=etf><option value=5m>5m</option><option value=15m>15m</option><option value=1h>1h</option><option value=1m>1m</option></select>
   <button class=act onclick=genSpec()>① 转成筛选条件</button>
   <button class=act onclick=genCode() ${DET.spec?'':'disabled'}
     title="${DET.spec?'':'先点①'}">② 生成策略代码 → 首版</button>
   <span class=meta id=genmsg>${DET.spec?'':'两步都要点: ① 出条件(约1分钟) → ② 出代码(约4分钟)。'}</span>
  </div>
  <div id=specbox>${DET.spec?specHtml(DET.spec):''}</div>
 </div>`;
}

/* ① 自然语言 → 筛选条件(调本机 claude -p, 约1分钟) */
async function genSpec(){
 const m=document.getElementById('genmsg');
 const text=document.getElementById('edesc').value.trim();
 if(!text){ m.textContent='先写下你看到了什么。'; return; }
 m.textContent='⏳ 本机 Claude 正在拆解…(约1分钟, 别关页面)';
 const r=await (await fetch(`/api/idea/${SEL}/spec`,{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({text, tf:document.getElementById('etf').value})})).json();
 if(!r.ok){ m.textContent='失败: '+(r.error||'?'); return; }
 pollJob(r.job, m, async()=>{
   // 重新拉一次: 让描述和筛选条件都【持久化回显】。原来只塞进一个临时div,
   // 页面一重绘/一刷新就没了, 用户会以为①失败了(其实早就成功了)。
   DET=await (await fetch('/api/idea/'+SEL)).json();
   renderNav(); renderPane();
 });
}
function specHtml(sp){
 const rows=(sp.conditions||[]).map(c=>`<tr>
   <td><b>${c.id}</b></td><td>${c['用户的话']||''}</td><td>${c['量化定义']||''}</td>
   <td>${JSON.stringify(c['参数']||{})}</td>
   <td style="color:${c['把握度']==='高'?'var(--ok)':c['把握度']==='低'?'var(--bad)':'var(--warn)'}">${c['把握度']||''}</td>
   <td style="color:var(--warn)">${c['待用户拍板']||''}</td></tr>`).join('');
 return `<div class=box style="margin-top:12px">
   <h3>🔍 筛选条件 · ${sp.name||''} <span class=meta style="font-weight:400">${sp.direction||''}</span></h3>
   <div class=verdict>条件已出。<b>还没有策略</b> —— 再点上面的「② 生成策略代码 → 首版」才会写出可回测的代码(约4分钟)。</div>
   <table style="width:100%;border-collapse:collapse;font-size:12.5px">
    <tr><th>#</th><th>你的话</th><th>量化定义</th><th>参数</th><th>把握度</th><th>待你拍板</th></tr>${rows}</table>
   <div class=meta style="margin-top:8px">入场: ${sp.entry||'-'}<br>止损: ${sp.sl||'-'}<br>止盈: ${sp.tp||'-'}</div>
   ${(sp['疑问']||[]).length?`<div class=verdict style="margin-top:10px"><b>必须由你拍板的问题</b>(拍板后把答案补进上面的描述框, 重跑①):<br>
     ${sp['疑问'].map((q,i)=>`${i+1}. ${q}`).join('<br>')}</div>`:''}
  </div>`;
}
/* ② 筛选条件 → Python 代码 → 首版策略 */
async function genCode(){
 const m=document.getElementById('genmsg');
 m.textContent='⏳ 本机 Claude 正在写代码…(约1分钟)';
 const r=await (await fetch(`/api/idea/${SEL}/codegen`,{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({tf:document.getElementById('etf').value})})).json();
 if(!r.ok){ m.textContent='失败: '+(r.error||'?'); return; }
 pollJob(r.job, m, async j=>{
   DET=await (await fetch('/api/idea/'+SEL)).json();
   MOD='iter'; renderNav(); renderPane();
 });
}
/* 统一的任务轮询 */
function pollJob(job, msgEl, onDone){
 const t=setInterval(async()=>{
   const j=await (await fetch('/api/job?id='+encodeURIComponent(job))).json();
   msgEl.textContent=(j.state==='running'?'⏳ ':(j.state==='error'?'❌ ':'✅ '))+(j.msg||j.state);
   if(j.state!=='running'){ clearInterval(t); if(j.state==='done'&&onDone) onDone(j); }
 },2000);
}

function selThumb(ix){
 const c=(DET.charts||[])[ix]; if(!c) return;
 document.querySelectorAll('#thumbs img').forEach((im,j)=>im.setAttribute('aria-selected',String(j===ix)));
 document.getElementById('staticimg').innerHTML=`<img src="/research/asset?p=${encodeURIComponent(c.path)}" onclick="window.open(this.src)">`;
 document.getElementById('capL').textContent=`原始图 · ${c.symbol||'?'} ${c.note?('· '+c.note):''}`;
 if(c.symbol&&c.center){ document.getElementById('chartmsg').textContent=''; showChart(c.symbol,c.tf||'5m',c.center); }
 else document.getElementById('chartmsg').textContent='这张图没记 币种+时间, 画不出对应K线。';
}

function ensureChart(el){
 if(chart&&chart._el===el) return;
 el.innerHTML='';
 chart=LightweightCharts.createChart(el,{layout:{background:{color:'#0e1116'},textColor:'#d6dae0'},
   grid:{vertLines:{color:'#1c2128'},horzLines:{color:'#1c2128'}},
   timeScale:{timeVisible:true,secondsVisible:false},rightPriceScale:{borderColor:'#30363d'}});
 chart._el=el;
 candle=chart.addCandlestickSeries({upColor:'#3fb950',downColor:'#f85149',wickUpColor:'#3fb950',wickDownColor:'#f85149',borderVisible:false});
 new ResizeObserver(()=>chart.applyOptions({width:el.clientWidth,height:el.clientHeight})).observe(el);
}
async function showChart(sym,tf,center){
 const el=document.getElementById('chartbox'); if(!el) return;
 curChartSym=sym; curChartTf=tf; curChartCenter=center;
 ensureChart(el);
 const kl=await (await fetch(`/api/klines?symbol=${sym}&center=${center}&span=120&tf=${tf}`)).json();
 const msg=document.getElementById('chartmsg');
 if(!kl.length){ if(msg) msg.textContent=`缓存里没有 ${sym} 的 ${tf} K线(只存了近30天)。`; return; }
 if(msg) msg.textContent=`${sym} · ${tf} · ${new Date(center*1000).toLocaleString('zh-CN')}`;
 const dig=Math.min(8,Math.max(2,Math.ceil(-Math.log10(kl[0].c||1))+4));
 candle.applyOptions({priceFormat:{type:'price',precision:dig,minMove:Math.pow(10,-dig)}});
 candle.setData(kl.map(k=>({time:k.t,open:k.o,high:k.h,low:k.l,close:k.c})));
 chart.timeScale().fitContent();
}
function switchTf(t){ if(curChartSym) showChart(curChartSym,t,curChartCenter); }

/* ---------- 上传: 粘贴 / 拖拽 / 选文件 ----------
   两个入口各有一份待上传的图: PENDING(给已有灵感补图) / NPENDING(新建灵感弹窗)。
   弹窗开着时, 粘贴/拖拽一律进弹窗。 */
let PENDING=null, NPENDING=null;
const modalOn=()=>document.getElementById('modal').classList.contains('on');

function armUpload(){
 document.addEventListener('paste',e=>{
   const it=[...(e.clipboardData||{}).items||[]].find(x=>x.type.startsWith('image/'));
   if(!it) return;
   if(modalOn()) return readNImg(it.getAsFile());
   if(MOD==='origin'&&SEL) readImg(it.getAsFile());
 });
 document.addEventListener('dragover',e=>{
   const d=modalOn()?document.getElementById('ndrop'):document.getElementById('drop');
   if(d){e.preventDefault();d.classList.add('hot');}
 });
 document.addEventListener('dragleave',()=>{
   ['drop','ndrop'].forEach(id=>{const d=document.getElementById(id); if(d)d.classList.remove('hot');});
 });
 document.addEventListener('drop',e=>{
   const d=modalOn()?document.getElementById('ndrop'):document.getElementById('drop');
   if(!d) return;
   e.preventDefault(); d.classList.remove('hot');
   const f=[...e.dataTransfer.files].find(x=>x.type.startsWith('image/'));
   if(!f) return;
   modalOn()?readNImg(f):readImg(f);
 });
 document.addEventListener('change',e=>{
   if(e.target.id==='fpick'&&e.target.files[0]) readImg(e.target.files[0]);
   if(e.target.id==='nfile'&&e.target.files[0]) readNImg(e.target.files[0]);
 });
}
function readImg(file){
 const r=new FileReader();
 r.onload=()=>{ PENDING=r.result;
   const d=document.getElementById('drop');
   if(d){ d.innerHTML=`已选好图 (${Math.round(file.size/1024)}KB) —— 点「上传」`; d.classList.add('hot'); }
 };
 r.readAsDataURL(file);
}
async function doUpload(){
 const msg=document.getElementById('upmsg');
 if(!PENDING){ msg.textContent='还没选图(拖进来/Ctrl+V/选文件)。'; return; }
 const tv=document.getElementById('utime').value;
 const body={data:PENDING, symbol:document.getElementById('usym').value.trim(),
             tf:document.getElementById('utf').value,
             center: tv?Math.floor(new Date(tv).getTime()/1000):0,     // 时间可不填
             note:document.getElementById('unote').value.trim()};
 const r=await (await fetch(`/api/idea/${SEL}/chart`,{method:'POST',headers:{'Content-Type':'application/json'},
                             body:JSON.stringify(body)})).json();
 if(!r.ok){ msg.textContent='上传失败: '+(r.error||'?'); return; }
 PENDING=null;
 DET=await (await fetch('/api/idea/'+SEL)).json();
 renderNav(); renderPane();
}

/* ---------- 新建灵感弹窗 ---------- */
function openNew(){ document.getElementById('modal').classList.add('on'); }
function closeNew(){
 document.getElementById('modal').classList.remove('on');
 NPENDING=null;
 document.getElementById('npreview').innerHTML='';
 document.getElementById('ndrop').innerHTML='把图拖进来 / 直接 <b>Ctrl+V 粘贴</b> / <label style="color:var(--accent);cursor:pointer">选文件<input type=file accept="image/*" hidden id=nfile></label>';
 document.getElementById('ndrop').classList.remove('hot');
 ['ntitle','nsym','ntime','nnote'].forEach(id=>document.getElementById(id).value='');
 document.getElementById('nmsg').textContent='';
}
function readNImg(file){
 const r=new FileReader();
 r.onload=()=>{ NPENDING=r.result;
   const d=document.getElementById('ndrop');
   d.innerHTML=`已选好图 (${Math.round(file.size/1024)}KB)`; d.classList.add('hot');
   document.getElementById('npreview').innerHTML=`<img src="${r.result}">`;
 };
 r.readAsDataURL(file);
}
async function submitNew(){
 const msg=document.getElementById('nmsg');
 const note=document.getElementById('nnote').value.trim();
 if(!note){ msg.textContent='写下你看到了什么 —— 没有想法的图只是张图, 不是灵感。'; return; }
 const tv=document.getElementById('ntime').value;
 msg.textContent='创建中…';
 const body={title:document.getElementById('ntitle').value.trim(),
             note, data:NPENDING||'',
             symbol:document.getElementById('nsym').value.trim(),
             tf:document.getElementById('ntf').value,
             center: tv?Math.floor(new Date(tv).getTime()/1000):0};
 const r=await (await fetch('/api/idea/new',{method:'POST',headers:{'Content-Type':'application/json'},
                             body:JSON.stringify(body)})).json();
 if(!r.ok){ msg.textContent='失败: '+(r.error||'?'); return; }
 const slug=r.slug;
 closeNew();
 D=await (await fetch('/api/ideas')).json();
 cur='idea'; renderList();
 await pick(slug,'idea');
 document.getElementById('pane').insertAdjacentHTML('afterbegin',
  `<div class=verdict style="margin-bottom:14px">✅ 已建灵感 <b>#${r.id}</b>。你的原话已一字不改存进
   <code>research/ideas/${slug}/idea.md</code>。<br>
   <b>下一步</b>: 跟 Claude 说「读 #${r.id}, 写出策略 v1」—— 它会先把必须由你拍板的定义列出来(不替你假设),
   再写成可回测的代码。看图器是本地静态页面, 连不上 LLM, 生成策略这一步必须由 Claude 来做。</div>`);
}

/* ---------- ② 策略迭代 ---------- */
function viewIter(){
 const vs=DET.versions||[];
 const form=`<div class=box>
   <h3>➕ 新建版本</h3>
   <div class=row>
    <input id=vtitle placeholder="这一版叫什么(如: 止损放FVG下沿)" style="flex:1;min-width:180px">
    <select id=vscan><option value="">选可执行策略(scanner)</option>${
      SCANNERS.map(s=>`<option value="${s.name}">${s.label} · ${s.tf} · ${s.name}</option>`).join('')}</select>
   </div>
   <div class=row><input id=vwhy placeholder="为什么要有这一版?(通常来自上一版的标注)" style="flex:1"></div>
   <div class=row><textarea id=vbody rows=4 placeholder="规则说明(markdown)" style="width:100%"></textarea></div>
   <div class=row><button class=act onclick=newVersion()>创建</button>
     <span class=meta id=vmsg>没选 scanner 的版本只是文字, 跑不了回测 —— 要能跑, 规则得先由 Claude 写成代码并注册进 bt_registry.SCANS。</span></div>
  </div>`;
 if(!vs.length) return form+`<div class=box><h3>🧬 策略迭代</h3>
   <div class=meta>还没有版本。流程: 原始图+你的感悟 → Claude 写出 <b>v1</b> → 选月份跑回测 →
   你逐张标注 → Claude 据标注迭代出 <b>v2</b> → 循环。</div></div>`;
 return form+vs.map(v=>`<div class=box>
   <h3>🧬 ${v.v} · ${v.title} <span class=meta style="font-weight:400">${v.date||''}</span>
     ${v.scanner?`<span class=pill>可回测</span>`:'<span class=pill style="color:var(--warn)">无代码·跑不了回测</span>'}</h3>
   ${v.based_on?`<div class=meta>由 ${v.based_on} 演化而来</div>`:''}
   ${v.why?`<div class=verdict>为什么有这一版: ${v.why}</div>`:''}
   <div class=body>${v.body_html}</div>
   ${v.code?`<details style="margin-top:10px">
     <summary style="cursor:pointer;color:var(--accent);font-size:13px">📄 看代码 (${v.v}.py) —— 跑之前请过一眼, 这是要在你机器上执行的</summary>
     <pre style="background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto;font-size:12px;margin-top:8px"><code>${v.code.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</code></pre>
    </details>`:''}
  </div>`).join('');
}
async function newVersion(){
 const m=document.getElementById('vmsg');
 const title=document.getElementById('vtitle').value.trim();
 if(!title){ m.textContent='起个名字。'; return; }
 const body={title, scanner:document.getElementById('vscan').value,
             why:document.getElementById('vwhy').value.trim(),
             body:document.getElementById('vbody').value};
 const r=await (await fetch(`/api/idea/${SEL}/version`,{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
 if(!r.ok){ m.textContent='失败: '+(r.error||'?'); return; }
 DET=await (await fetch('/api/idea/'+SEL)).json(); renderNav(); renderPane();
}

/* ---------- ③ 回测记录 (顶部期望/笔数/结论建议, 下面逐个触发点位) ---------- */
function viewBt(){
 const bs=DET.backtests||[], vs=(DET.versions||[]).filter(v=>v.scanner);
 const run=`<div class=box>
   <h3>▶ 跑回测</h3>
   <div class=row>
    <select id=bver>${vs.length?vs.map(v=>`<option value="${v.v}">${v.v} · ${v.title} (${v.scanner})</option>`).join('')
                              :'<option value="">还没有可执行的版本</option>'}</select>
    <select id=bmon>${(COVERAGE.months||[]).map(m=>
       `<option value="${m.m}">${m.m} · ${m.symbols}币${m.note?(' · '+m.note):''}</option>`).join('')}</select>
    <button class=act onclick=runBt() ${vs.length?'':'disabled'}>跑</button>
    <span class=meta id=bmsg>回测按<b>月</b>跑。缓存只有近30天全量(663币); 更早的月份只有50个币有一年数据 —— 已标在选项里。</span>
   </div></div>`;
 if(!bs.length) return run+`<div class=box><h3>📊 回测记录</h3><div class=meta>还没有回测。</div></div>`;
 return run+bs.map(b=>{
   const ann=b.annotations||{}, sigs=b.signals||[];
   const vals=Object.values(ann), nok=vals.filter(a=>a.verdict==='ok').length;
   const rate=vals.length?Math.round(nok/vals.length*100):0;
   const exp=b.exp_r, pos=exp>0;
   return `<div class=box>
    <h3>📊 ${b.id} <span class=meta style="font-weight:400">策略 ${b.version||'?'} · ${b.range||'?'} ·
      ${b.symbols||'?'}币 · ${b.at||''}</span></h3>

    <!-- 第一阶段(P004): 图形对不对。盈利指标折叠起来 —— 盲测时看到期望值会污染判断 -->
    <div class=kpi>
     <div>触发笔数<b>${b.n||sigs.length}</b></div>
     <div>已盲测<b>${vals.length}</b></div>
     <div>图形通过率<b class="${rate>=70?'pos':(vals.length?'neg':'')}">${vals.length?rate+'%':'—'}</b></div>
    </div>
    <div class=meta id="prog_${b.id}"></div>
    <div class=row style="margin-top:8px">
      <button class=act onclick="summarize('${b.id}')">🧾 汇总不满意理由</button>
      <button class=act onclick="iterate('${b.id}')" style="border-color:var(--accent);color:var(--accent)"
        ${vals.length>=5?'':'disabled'}>🔁 二次生成 → 据我这 ${vals.length} 笔判断写下一版</button>
      <span class=meta id="itmsg_${b.id}">${vals.length<20?`建议判满 20 笔再生成(现在 ${vals.length} 笔, 少于5笔不给点)`:''}</span>
    </div>
    <div id="sum_${b.id}"></div>

    <details style="margin-top:10px">
     <summary style="cursor:pointer;color:var(--muted);font-size:12px">
       💰 盈利指标(第二阶段才看 —— 盲测时别点开, 会污染你的判断)</summary>
     <div class=kpi style="margin-top:8px">
      <div>扣费后期望<b class="${pos?'pos':'neg'}">${exp==null?'—':(exp>0?'+':'')+exp+'R'}</b></div>
      <div>胜率<b>${b.win_rate!=null?b.win_rate+'%':'—'}</b></div>
      <div>t值<b class="${(b.tstat||0)>2?'pos':''}">${b.tstat!=null?b.tstat:'—'}</b></div>
     </div>
     ${b.verdict?`<div class=verdict><b>结论与建议</b>: ${b.verdict}</div>`:''}
    </details>

    ${b.truncated?`<div class=meta style="color:var(--warn);margin-top:8px">⚠ 共 ${b.n} 个信号, 页面只画前 300 张。</div>`:''}
    <div class=row style="margin-top:10px">
     <label class=meta style="cursor:pointer"><input type=checkbox id="only_${b.id}" ${ONLY_NEW?'checked':''}
       onchange="ONLY_NEW=this.checked;renderPane()"> 只看没判过的</label>
     <span class=meta>${b.n_prior?`这一版里有 <b>${b.n_prior}</b> 个点位你以前判过(跨版本继承, 不再问你第二遍)`:''}</span>
    </div>
    <div style="margin-top:10px">
    ${(()=>{
      const rows=sigs.map((s,ix)=>[s,ix]).filter(([s,ix])=>!ONLY_NEW||!(ann[String(s.id!=null?s.id:ix)]||s.prior));
      if(!sigs.length) return '<div class=meta>这次回测没有触发信号。</div>';
      if(!rows.length) return '<div class=meta>这一版里的点位你都判过了 —— 去掉上面的勾可以回看。</div>';
      return rows.map(([s,ix])=>sigRow(b,s,ix,ann)).join('');
    })()}
    </div>
   </div>`;
 }).join('');
}
async function runBt(){
 const m=document.getElementById('bmsg');
 const version=document.getElementById('bver').value, month=document.getElementById('bmon').value;
 if(!version){ m.textContent='先去「策略迭代」建一个带 scanner 的版本。'; return; }
 const r=await (await fetch(`/api/idea/${SEL}/backtest`,{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({version,month})})).json();
 if(!r.ok){ m.textContent='失败: '+(r.error||'?'); return; }
 m.textContent='回测已启动…';
 const poll=setInterval(async()=>{
   const j=await (await fetch('/api/job?id='+encodeURIComponent(r.job))).json();
   m.textContent=(j.state==='running'?'⏳ ':(j.state==='error'?'❌ ':'✅ '))+(j.msg||j.state);
   if(j.state==='done'||j.state==='error'){
     clearInterval(poll);
     if(j.state==='done'){ DET=await (await fetch('/api/idea/'+SEL)).json(); renderNav(); renderPane(); }
   }
 },1500);
}
/* ---- 盲测(P004): 判断前只给触发那一刻为止的K线, 判完才揭晓后市与盈亏 ----
   已判过的样本直接显示结果(没必要再瞒), 没判过的一律封住。 */
function sigRow(b,s,ix,ann){
 const key=String(s.id!=null?s.id:ix);
 // 判断跟着【市场点位】走, 不跟着下标走 —— 你在 v1 判过的点, 换版本重跑不该再问一遍
 const a=ann[key]||s.prior;
 const judged=!!a;
 const cross=!ann[key]&&!!s.prior;      // 是别的版本判的
 const st=judged?(a.verdict==='ok'?`<span class="st ok">✓ 符合${cross?` <span class=meta>(${s.prior.from} 判的)</span>`:''}</span>`
            :`<span class="st bad">✗ 不符合 · ${a.reason}${cross?` <span class=meta>(${s.prior.from} 判的)</span>`:''}</span>`)
          :`<span class="st none">🔒 未判(盲测)</span>`;
 // 未判 = 结果/盈亏一个字都不能露
 const outcome = judged
   ? `<span class=meta>· ${s.result==='tp'?'止盈':s.result==='sl'?'止损':s.result==='timeout'?'超时':'持仓'}${s.pnl_r!=null?(' '+s.pnl_r+'R'):''}</span>`
   : '';
 return `<details class=sig data-key="${key}">
  <summary onclick="setTimeout(()=>drawSig('${b.id}','${key}',${ix},${judged?1:0}),50)">
   <b>${s.symbol}</b> <span class="tfb tf-${s.tf||'5m'}">${s.tf||'5m'}</span>
   <span class=meta>${new Date(s.t*1000).toLocaleString('zh-CN')}</span>
   ${outcome}${st}</summary>
  <div style="padding:10px 12px">
   <div class=row style="margin-bottom:6px">
    <span class=meta>看图级别:</span>
    ${['1m','5m','15m','1h'].map(t=>`<button class=act id="tf_${b.id}_${key}_${t}"
       style="padding:3px 10px" onclick="switchSigTf('${b.id}','${key}',${ix},'${t}')">${t}${t===(s.tf||'5m')?' ★':''}</button>`).join('')}
    <span class=meta>★ = 策略的触发级别(${s.tf||'5m'}); 其余是你换个眼睛看形态/趋势</span>
   </div>
   <div class=chartbox id="c_${b.id}_${key}" style="height:320px"></div>
   <div class=meta id="h_${b.id}_${key}" style="margin-top:6px"></div>
   <div class=row style="margin-top:10px" id="a_${b.id}_${key}">
    ${judged?'':`
     <button class="act ok" onclick="annot('${b.id}','${key}',${ix},'ok')">👍 符合我要的入场点</button>
     <button class="act bad" onclick="annot('${b.id}','${key}',${ix},'bad')">👎 不符合</button>
     <input id="r_${b.id}_${key}" placeholder="不符合的话: 哪里不对?(这条理由=下一版要改的筛选语句)" style="flex:1;min-width:240px">`}
    <span class=meta id="m_${b.id}_${key}"></span>
   </div>
  </div></details>`;
}

const TFSEC={'1m':60,'5m':300,'15m':900,'1h':3600};
const DEFAULT_VIEW_TF='5m';        // 默认用5m看形态(哪怕策略在1m上触发) —— 触发级别≠看图级别

function switchSigTf(bid,key,ix,tf){
 const el=document.getElementById(`c_${bid}_${key}`); if(!el) return;
 const reveal=el._mode===1?1:0;
 el._tf=tf; el._mode=-1;            // 强制重画
 drawSig(bid,key,ix,reveal);
}

/* reveal=0 盲测(不给未来) | reveal=1 揭晓(给后市+结果) */
async function drawSig(bid,key,ix,reveal){
 const el=document.getElementById(`c_${bid}_${key}`);
 const hint=document.getElementById(`h_${bid}_${key}`);
 if(!el) return;
 const b=(DET.backtests||[]).find(x=>x.id===bid); if(!b) return;
 const s=b.signals[ix];
 const tf=el._tf||DEFAULT_VIEW_TF;
 if(el._mode===reveal) return;             // 已经是这个模式了
 el._mode=reveal; el.innerHTML='';
 // 按钮高亮
 ['1m','5m','15m','1h'].forEach(t=>{const btn=document.getElementById(`tf_${bid}_${key}_${t}`);
   if(btn) btn.style.background = t===tf?'#243447':'#161b22';});
 // 盲测: cut = 触发K【收盘】那一刻。切到大级别时, 那根还没走完的K必须砍掉,
 // 否则它的高/低/收盘价里藏着触发之后的信息 —— 那就不是盲测了。
 const cut=s.t+(TFSEC[s.tf]||60);
 const q=reveal
   ? `symbol=${s.symbol}&center=${s.t}&span=120&tf=${tf}`
   : `symbol=${s.symbol}&center=${s.t}&span=120&tf=${tf}&after=0&cut=${cut}`;
 const kl=await (await fetch('/api/klines?'+q)).json();
 if(!kl.length){ el.innerHTML=`<div class=meta style="padding:16px">缓存里没有 ${s.symbol} 的 ${tf} K线。</div>`; return; }
 const c=LightweightCharts.createChart(el,{layout:{background:{color:'#0e1116'},textColor:'#d6dae0'},
   grid:{vertLines:{color:'#1c2128'},horzLines:{color:'#1c2128'}},
   timeScale:{timeVisible:true,secondsVisible:false},
   rightPriceScale:{borderColor:'#30363d',scaleMargins:{top:0.06,bottom:0.28}}});
 const dig=Math.min(8,Math.max(2,Math.ceil(-Math.log10(s.entry||kl[0].c||1))+4));
 const ser=c.addCandlestickSeries({upColor:'#3fb950',downColor:'#f85149',wickUpColor:'#3fb950',wickDownColor:'#f85149',
   borderVisible:false,priceFormat:{type:'price',precision:dig,minMove:Math.pow(10,-dig)}});
 ser.setData(kl.map(k=>({time:k.t,open:k.o,high:k.h,low:k.l,close:k.c})));
 const vol=c.addHistogramSeries({priceFormat:{type:'volume'},priceScaleId:'vol'});
 c.priceScale('vol').applyOptions({scaleMargins:{top:0.78,bottom:0.02}});
 vol.setData(kl.map(k=>({time:k.t,value:k.v,color:k.c>=k.o?'#2ea043cc':'#f85149aa'})));
 // 入场/止损/止盈都是【触发那一刻就已知】的, 不是未来信息, 盲测下照给
 [['入场',s.entry,'#58a6ff'],['止损',s.sl,'#f85149'],['止盈',s.tp,'#3fb950']].forEach(([t,p,col])=>{
   if(p) ser.createPriceLine({price:p,color:col,lineWidth:1,lineStyle:2,axisLabelVisible:true,title:t}); });
 // 触发时刻吸附到当前级别的K线上(5m图上没有1m的时间戳, 不吸附就画不出标记)
 let mt=kl[0].t; for(const k of kl){ if(k.t<=s.t) mt=k.t; }
 ser.setMarkers([{time:mt,position:s.dir==='short'?'aboveBar':'belowBar',color:'#d29922',
   shape:s.dir==='short'?'arrowDown':'arrowUp',text:'触发'}]);
 c.timeScale().fitContent();
 new ResizeObserver(()=>c.applyOptions({width:el.clientWidth,height:el.clientHeight})).observe(el);
 const coarse = (TFSEC[tf]||300) > (TFSEC[s.tf]||60);
 if(hint) hint.innerHTML = reveal
   ? `已揭晓 · ${tf}图 · 结果 <b>${s.result==='tp'?'止盈':s.result==='sl'?'止损':s.result==='timeout'?'超时平':'仍持仓'}</b>${s.pnl_r!=null?` · ${s.pnl_r>0?'+':''}${s.pnl_r}R`:''}${s.net_r!=null?` (扣费 ${s.net_r}R)`:''}`
   : `🔒 <b>盲测中</b> · ${tf}图: 只画到触发那一刻, 后面一根K都没给。`
     + (coarse?` <span style="color:var(--warn)">(触发时那根${tf}K还没收盘, 已砍掉 —— 它的高低收里藏着未来)</span>`:'')
     + ` 先判断这是不是你要的入场点 —— 判完才揭晓。`;
}

async function annot(bid,key,ix,verdict){
 const inp=document.getElementById(`r_${bid}_${key}`);
 const reason=inp?inp.value.trim():'';
 const m=document.getElementById(`m_${bid}_${key}`);
 if(verdict==='bad'&&!reason){ m.textContent='说明哪里不对 —— 这条理由就是下一版要改的筛选语句, 空着等于白判。'; return; }
 const b=(DET.backtests||[]).find(x=>x.id===bid), s=b?b.signals[ix]:null;
 const r=await (await fetch(`/api/idea/${SEL}/annotate`,{method:'POST',headers:{'Content-Type':'application/json'},
   // 带上市场点位身份(币|时刻|方向), 这条判断才能跨版本认得出同一个点
   body:JSON.stringify({bt_id:bid,sig:key,verdict,reason,
                        symbol:s?s.symbol:'', t:s?s.t:0, dir:s?s.dir:''})})).json();
 if(!r.ok){ m.textContent='失败: '+(r.error||'?'); return; }
 // 判完 → 揭晓后市
 document.getElementById(`a_${bid}_${key}`).innerHTML=
   `<span class=meta>已判: ${verdict==='ok'?'👍 符合':'👎 不符合 · '+reason}</span>`;
 const el=document.getElementById(`c_${bid}_${key}`); if(el) el._mode=-1;
 await drawSig(bid,key,ix,1);
 DET=await (await fetch('/api/idea/'+SEL)).json();
 renderProgress(bid);
}
function renderProgress(bid){
 const b=(DET.backtests||[]).find(x=>x.id===bid); if(!b) return;
 const el=document.getElementById('prog_'+bid); if(!el) return;
 const ann=b.annotations||{}, vals=Object.values(ann);
 const ok=vals.filter(a=>a.verdict==='ok').length;
 el.innerHTML=`已盲测 <b>${vals.length}</b>/${(b.signals||[]).length} · 符合 <b>${ok}</b> ·
   <b>图形通过率 ${vals.length?Math.round(ok/vals.length*100):0}%</b>`;
}

/* 🔁 二次生成: 把你的盲测判断喂给本机 Claude, 让它写出下一版 */
async function iterate(bid){
 const m=document.getElementById('itmsg_'+bid);
 m.textContent='⏳ 本机 Claude 正在读你的判断…';
 const r=await (await fetch(`/api/idea/${SEL}/iterate`,{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({bt_id:bid})})).json();
 if(!r.ok){ m.textContent='失败: '+(r.error||'?'); return; }
 pollJob(r.job, m, async j=>{
   DET=await (await fetch('/api/idea/'+SEL)).json();
   MOD='iter'; renderNav(); renderPane();
 });
}

/* 汇总不满意理由 → 下一版改哪条规则 */
async function summarize(bid){
 const box=document.getElementById('sum_'+bid);
 box.innerHTML='<div class=meta>归纳中…</div>';
 const r=await (await fetch(`/api/idea/${SEL}/summary`,{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({bt_id:bid})})).json();
 if(!r.ok){ box.innerHTML=`<div class=meta>${r.error||'失败'}</div>`; return; }
 box.innerHTML=`<div class=body>${r.html}</div>
   <div class=verdict>这个按钮做的是<b>归纳</b>, 不是<b>生成新策略</b> —— 看图器是本地静态应用, 连不上 LLM。
   把上面这份汇总(已存进 <code>annotations/${bid}_summary.md</code>)交给 Claude, 说「据此写出 v(N+1)」,
   由它改规则、写代码、注册成新 scanner, 你再回来重跑一遍盲测, 看通过率有没有涨。</div>`;
}

/* ---------- ④ 结论 ---------- */
function viewConcl(){
 const i=DET.idea||{};
 return `<div class=box>
   <h3>🏁 结论 <span class="pill s-${i.status}">${i.status}</span></h3>
   <div class=meta>状态流转: 灵感 → 立项 → 回测中 → 已验证 / 已证伪 → 上线</div>
  </div>
  <div class="box body"><h3>💡 原始想法与假设</h3>${i.body_html||''}</div>
  ${i.timeline_html?`<div class="box body"><h3>📜 研究链路（${i.steps} 步）</h3>${i.timeline_html}</div>`:''}`;
}

armUpload();
fetch('/api/scanners').then(r=>r.json()).then(d=>{SCANNERS=d.scanners||[];COVERAGE=d.coverage||{months:[]};});
fetch('/api/ideas').then(r=>r.json()).then(d=>{D=d;renderList();});
</script></body></html>"""


AGENTS_HTML = """<!DOCTYPE html><html lang=zh><head><meta charset=utf-8><title>Agent工作台</title>
<style>
 body{margin:0;font:13px system-ui;background:#0e1116;color:#d6dae0;padding:16px}
 h2{margin:0 0 12px} a{color:#58a6ff}
 table{width:100%;border-collapse:collapse;margin-top:8px}
 th,td{padding:7px 9px;text-align:left;border-bottom:1px solid #1c2128;vertical-align:top}
 th{background:#161b22} .muted{color:#8b949e;font-size:11px}
 pre{white-space:pre-wrap;margin:0;font:11px ui-monospace;color:#adbac7;max-height:120px;overflow:auto}
</style></head><body>
<h2>🤖 Agent 工作台 <a href="/" style="font-size:13px;font-weight:400">← 回信号</a></h2>
<div class=muted>已注册子Agent + 各策略研究笔记/交接。纯本地, 无外部服务。</div>
<table><thead><tr><th>Agent</th><th>职责</th><th>交接/笔记</th><th>最新进展(尾段)</th></tr></thead>
<tbody id=rows></tbody></table>
<script>
const fmt=t=>t?new Date(t*1000).toLocaleString('zh-CN',{hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
fetch('/api/agents').then(r=>r.json()).then(rows=>{
 document.getElementById('rows').innerHTML=rows.map(a=>`<tr>
  <td><b>${a.name}</b></td>
  <td>${a.responsibility||''}</td>
  <td>${a.handoff?`<code>${a.handoff}</code><br><span class=muted>${fmt(a.note_mtime)}</span>`:'<span class=muted>无</span>'}</td>
  <td><pre>${(a.latest_note||'').replace(/</g,'&lt;')||'—'}</pre></td></tr>`).join('')
  ||'<tr><td colspan=4 class=muted>暂无</td></tr>';
});
</script></body></html>"""


def _load_precomputed(days, strats):
    """读 bt_scan.py 预生成的 sig_<strat>_<days>d.json,秒级启动(不在此扫描)。"""
    import glob
    names = strats or list(R.SCANS)
    sigs = []
    for n in names:
        p = os.path.join(R.CACHE, f"sig_{n}_{days}d.json")
        if os.path.exists(p):
            try:
                sigs.extend(json.load(open(p)))
            except Exception:
                pass
    sigs.sort(key=lambda s: s.get("created_at") or 0)
    for i, s in enumerate(sigs):
        s["id"] = i
    return sigs


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--port", type=int, default=8530)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--strats", default="")    # 逗号分隔, 空=全部
    a = ap.parse_args()
    DAYS = a.days
    META = R.META
    strats = [x for x in a.strats.split(",") if x] or None
    SIGNALS = _load_precomputed(a.days, strats)
    if not SIGNALS:
        print("[viewer] 无预生成信号; 请先跑: .venv/Scripts/python scripts/bt_scan.py --days %d" % a.days)
    by = {}
    for s in SIGNALS:
        by[s["strat"]] = by.get(s["strat"], 0) + 1
    print(f"[viewer] days={a.days} 信号合计 {len(SIGNALS)}: " +
          ", ".join(f"{R.META.get(k,{}).get('label',k)}={v}" for k, v in by.items()))
    print(f"[viewer] open locally: http://127.0.0.1:{a.port}")
    if a.host in ("0.0.0.0", "::"):
        try:
            print(f"[viewer] same-LAN phone/PC: http://{socket.gethostbyname(socket.gethostname())}:{a.port}")
        except Exception:
            pass
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
