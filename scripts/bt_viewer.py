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
import secrets
import socket
import sys
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


def _row(s):
    return {"id": s["id"], "strat": s.get("strat"), "symbol": s["symbol"], "dir": s["direction"],
            "stage": s.get("stage"), "t": s["created_at"], "entry": s["entry"], "sl": s["sl"],
            "tp": s["tp"], "result": s.get("result"), "pnl_r": s.get("pnl_r"),
            "climaxX": s.get("climaxX"), "movePct": s.get("movePct"), "anchor": s.get("anchor"),
            "extra": s.get("extra"), "vol_ratio": s.get("vol_ratio")}


@app.get("/api/signals")
def api_signals(strat: str = "", dir: str = "", result: str = "", limit: int = 800):
    """服务端过滤+截断: 全量是 200 万+ 条(~500MB), 整包下发会把浏览器(尤其手机)打死。
    只回最新 limit 条 + 命中总数。"""
    hit = [s for s in SIGNALS
           if (not strat or s.get("strat") == strat)
           and (not dir or s.get("direction") == dir)
           and (not result or s.get("result") == result)]
    limit = max(1, min(limit, 3000))
    return JSONResponse({"total": len(hit), "rows": [_row(s) for s in hit[::-1][:limit]]})


@app.get("/api/signal/{sid}")
def api_signal(sid: int):
    s = next((x for x in SIGNALS if x["id"] == sid), None)
    return JSONResponse(_row(s) if s else {}, status_code=200 if s else 404)


@lru_cache(maxsize=64)
def _klines_of(symbol: str, tf: str, days: int):
    """只读这一个币的缓存文件。

    不要用 R.cache_loader(): 它会 glob 出全部 2263 个币的 *_5m_30d.json 逐个 json.load
    进内存(2.7G 磁盘 -> 进程 RSS 4G+), 于是"重启后第一次点信号"要干等几十秒且界面无提示,
    看起来就像点了没反应。看图一次只需要一个币。
    """
    p = os.path.join(R.CACHE, f"{symbol}_{tf}_{days}d.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


@app.get("/api/klines")
def api_klines(symbol: str, center: int, span: int = 120, tf: str = "5m"):
    k = _klines_of(symbol, tf, DAYS) or _klines_of(symbol, "5m", DAYS)
    if not k:
        return JSONResponse([])
    times = [int(b["open_time"]) // 1000 for b in k]
    j = bisect.bisect_left(times, center)
    lo, hi = max(0, j - span), min(len(k), j + span)
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
 const fs=document.getElementById('fstrat').value, fd=document.getElementById('fdir').value, fr=document.getElementById('fres').value;
 document.getElementById('cnt').textContent='加载中…';
 const q=new URLSearchParams({strat:fs,dir:fd,result:fr,limit:LIMIT});
 let r; try{ r=await (await fetch('/api/signals?'+q)).json(); }
 catch(e){ document.getElementById('cnt').textContent='加载失败'; return; }
 ALL=r.rows||[];
 document.getElementById('cnt').textContent =
   ALL.length<r.total ? `最新 ${ALL.length} / 共 ${r.total} 条` : `共 ${r.total} 条`;
 document.getElementById('mcnt').textContent =
   r.total>=10000 ? `(${(r.total/10000).toFixed(1)}万)` : `(${r.total})`;
 document.getElementById('rows').innerHTML=ALL.map(s=>`<tr class=row data-id=${s.id} onclick=show(${s.id})>
  <td>${fmt(s.t)}</td><td><span class=badge>${(META[s.strat]||{}).label||s.strat}</span>${s.stage?(' '+s.stage):''}</td>
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
 chart=LightweightCharts.createChart(document.getElementById('chart'),{layout:{background:{color:'#0e1116'},textColor:'#d6dae0'},grid:{vertLines:{color:'#1c2128'},horzLines:{color:'#1c2128'}},timeScale:{timeVisible:true,secondsVisible:false},rightPriceScale:{borderColor:'#30363d'}});
 candle=chart.addCandlestickSeries({upColor:'#3fb950',downColor:'#f85149',wickUpColor:'#3fb950',wickDownColor:'#f85149',borderVisible:false});
 vol=chart.addHistogramSeries({priceFormat:{type:'volume'},priceScaleId:'',scaleMargins:{top:0.82,bottom:0}});
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
 vol.setData(kl.map(k=>({time:k.t,value:k.v,color:k.c>=k.o?'#26443055':'#5c252855'})));
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
 document.getElementById('tfsw').innerHTML=['5m','15m','1h'].map(x=>`<button onclick="switchTf('${x}')" style="padding:2px 8px;margin-right:3px;border:1px solid #30363d;border-radius:4px;background:${x===tf?'#243447':'#161b22'};color:#d6dae0">${x}</button>`).join('');
 document.getElementById('savebtn').style.display='inline-block';
 document.getElementById('labelbar').style.display='block'; showLabelState(s);
 document.getElementById('title').innerHTML=`<b>${s.symbol}</b> · <span class=badge>${m.label||s.strat}</span> · ${s.dir==='long'?'做多':'做空'} · ${fmt(s.t)}`;
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
<style>
 :root{--bg:#0e1116;--surface:#161b22;--line:#262d36;--ink:#d6dae0;--muted:#8b949e;--accent:#58a6ff;--warn:#d29922}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,"PingFang SC","Microsoft YaHei",sans-serif}
 .wrap{max-width:900px;margin:0 auto;padding:16px 14px 60px}
 h1{font:600 20px/1.3 system-ui;margin:0 0 2px} a{color:var(--accent)}
 .sub{color:var(--muted);font-size:13px;margin-bottom:14px}
 .tabs{display:flex;gap:8px;margin:12px 0 16px;flex-wrap:wrap}
 .tabs button{appearance:none;border:1px solid var(--line);background:var(--surface);color:var(--ink);
   padding:7px 14px;border-radius:999px;font-size:14px;cursor:pointer}
 .tabs button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#06121f;font-weight:600}
 .card{background:var(--surface);border:1px solid var(--line);border-radius:12px;margin-bottom:12px;overflow:hidden}
 .card>summary{list-style:none;cursor:pointer;padding:13px 14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 .card>summary::-webkit-details-marker{display:none}
 .card[open]>summary{border-bottom:1px solid var(--line)}
 .t{font-weight:600;flex:1;min-width:60%}
 .pill{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
 .pill.s-灵感{color:var(--warn);border-color:#5c4813}
 .pill.s-回测中{color:var(--accent);border-color:#1f4b7a}
 .pill.s-已证伪{color:#f85149;border-color:#6e2725}
 .pill.s-已验证,.pill.s-已被数据坐实{color:#3fb950;border-color:#1f5c33}
 .meta{width:100%;color:var(--muted);font-size:12px}
 .body{padding:4px 14px 16px}
 .body img{max-width:100%;border-radius:8px;border:1px solid var(--line);margin:8px 0}
 .body h2,.body h3{font-size:15px;margin:16px 0 6px}
 .body h4{font-size:14px;margin:14px 0 4px;color:var(--muted)}
 .body table{width:100%;border-collapse:collapse;font-size:13px;display:block;overflow-x:auto}
 .body th,.body td{border:1px solid var(--line);padding:5px 8px;text-align:left}
 .body pre{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:10px;overflow-x:auto;font-size:12px}
 .body code{background:#0d1117;padding:1px 5px;border-radius:4px;font-size:12.5px}
 .body blockquote{margin:8px 0;padding:8px 12px;border-left:3px solid var(--warn);background:#1c1a12;color:#e3d9b8}
 .body ul{padding-left:20px} .body hr{border:none;border-top:1px solid var(--line);margin:14px 0}
 .tl{margin-top:14px;border-top:1px dashed var(--line);padding-top:8px}
 .tl>summary{cursor:pointer;color:var(--accent);font-size:13px}
 .empty{color:var(--muted);padding:20px;text-align:center}
</style></head><body><div class=wrap>
 <h1>💡 灵感库 · 研究档案</h1>
 <div class=sub>每个灵感 = 一张原始图 + 当时的想法 + 之后完整的验证链路。<a href="/">← 回信号</a></div>
 <div class=tabs>
  <button id=t-idea aria-pressed=true onclick="tab('idea')">交易策略</button>
  <button id=t-principle aria-pressed=false onclick="tab('principle')">交易准则</button>
 </div>
 <div id=list></div>
</div>
<script>
let D={ideas:[],principles:[]}, cur='idea';
function tab(k){cur=k;['idea','principle'].forEach(x=>document.getElementById('t-'+x).setAttribute('aria-pressed',String(x===k)));render();}
function card(r){
 const charts=(r.charts||[]).map(c=>`<img src="/research/asset?p=${encodeURIComponent(c)}" alt="原图">`).join('');
 const tl=r.timeline_html?`<details class=tl><summary>📜 研究链路（${r.steps} 步）</summary><div class=body>${r.timeline_html}</div></details>`:'';
 return `<details class=card>
  <summary><span class=t>${r.id?('#'+r.id+' '):''}${r.title}</span>
   <span class="pill s-${r.status}">${r.status}</span>
   <span class=meta>${r.date||''}${r.symbol?(' · '+r.symbol):''}${(r.tags||[]).length?(' · '+r.tags.join(' / ')):''}</span></summary>
  <div class=body>${charts}${r.body_html}${tl}</div></details>`;
}
function render(){
 const rows=cur==='idea'?D.ideas:D.principles;
 document.getElementById('list').innerHTML=rows.length?rows.map(card).join('')
   :'<div class=empty>还没有内容。跟 Claude 说「把这个存进灵感库」即可归档。</div>';
}
fetch('/api/ideas').then(r=>r.json()).then(d=>{D=d;render();});
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
