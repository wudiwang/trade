"""本地回测可视化看图器(多策略版,用户 2026-06-16)。

读 .btcache/ 缓存 + 跑「策略注册表」里所有策略 → 信号表(带策略名)。
点一条 → 画当时K线(蜡烛+量)+ 入场/止损/止盈线 + 锚点/入场标记,按盈亏上色;
侧栏展示该信号所属策略的「思路逻辑」。

运行:  .venv/Scripts/python scripts/bt_viewer.py --days 30
浏览器:http://127.0.0.1:8530   纯本地、只读缓存。
"""
import argparse
import bisect
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bt_registry as R

from fastapi import FastAPI, Request
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
}
app = FastAPI()


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


@app.get("/api/signals")
def api_signals():
    return JSONResponse([
        {"id": s["id"], "strat": s.get("strat"), "symbol": s["symbol"], "dir": s["direction"],
         "stage": s.get("stage"), "t": s["created_at"], "entry": s["entry"], "sl": s["sl"],
         "tp": s["tp"], "result": s.get("result"), "pnl_r": s.get("pnl_r"),
         "climaxX": s.get("climaxX"), "movePct": s.get("movePct"), "anchor": s.get("anchor"),
         "extra": s.get("extra"), "vol_ratio": s.get("vol_ratio")}
        for s in SIGNALS])


@app.get("/api/klines")
def api_klines(symbol: str, center: int, span: int = 120, tf: str = "5m"):
    C = R.cache_loader(DAYS)
    k = C(tf).get(symbol) or C("5m").get(symbol)
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
<title>回测看图器</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 body{margin:0;font:13px system-ui;background:#0e1116;color:#d6dae0;display:flex;height:100vh}
 #left{width:420px;overflow:auto;border-right:1px solid #222;flex:none}
 #right{flex:1;display:flex;flex-direction:column;min-width:0}
 #bar{padding:8px 12px;border-bottom:1px solid #222}
 #chart{flex:1}
 #logic{border-top:1px solid #222;padding:8px 12px;background:#11161d;font-size:12px;max-height:170px;overflow:auto}
 #logic h4{margin:0 0 4px}#logic .li{color:#adbac7;margin:2px 0}
 table{width:100%;border-collapse:collapse}
 th,td{padding:5px 6px;text-align:left;border-bottom:1px solid #1c2128;white-space:nowrap}
 th{position:sticky;top:0;background:#161b22}
 tr.row{cursor:pointer} tr.row:hover{background:#1c2530} tr.sel{background:#243447!important}
 .tp{color:#3fb950}.sl{color:#f85149}.long{color:#3fb950}.short{color:#f85149}
 .badge{padding:1px 6px;border-radius:4px;background:#30363d;font-size:11px}
 select{background:#161b22;color:#d6dae0;border:1px solid #30363d;border-radius:5px;padding:3px 6px;margin:2px}
 .muted{color:#8b949e}
</style></head><body>
<div id=left>
 <div style="padding:8px 12px;position:sticky;top:0;background:#0e1116;z-index:2">
  <b>📊 信号</b> <span class=muted id=cnt></span>
  <a href="/agents" target="_blank" style="float:right;color:#58a6ff;text-decoration:none">🤖 Agent工作台</a><br>
  <select id=fstrat onchange=render()></select>
  <select id=fdir onchange=render()><option value="">全方向</option><option value=long>多</option><option value=short>空</option></select>
  <select id=fres onchange=render()><option value="">全结果</option><option value=tp>盈✓</option><option value=sl>损✗</option><option value=open>持仓</option></select>
 </div>
 <table><thead><tr><th>时间</th><th>策略</th><th>币</th><th>向</th><th>结果</th></tr></thead><tbody id=rows></tbody></table>
</div>
<div id=right>
 <div id=bar><div id=fresh class=muted style="font-size:11px;margin-bottom:4px">数据新鲜度加载中…</div><b id=title>← 点左侧信号查看当时K线</b> <span id=tfsw style="margin-left:10px"></span> <button id=savebtn onclick=saveCase() style="margin-left:8px;padding:2px 8px;border:1px solid #30363d;border-radius:4px;background:#161b22;color:#d6dae0;display:none">⭐保存案例</button> <span class=muted id=info></span>
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
 <div id=logic><h4 id=lt>策略思路</h4><div id=ld class=muted>点一条信号,这里显示它所属策略的逻辑</div></div>
</div>
<script>
let ALL=[], META={}, STATS={}, DETAIL={}, chart, candle, vol, lines=[];
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
 ALL=await (await fetch('/api/signals')).json();
 const opts=['<option value="">全部策略</option>'].concat(Object.keys(META).map(k=>{
   const st=STATS[k]||{}; return `<option value=${k}>${META[k].label} (${st.n_sig||0}信号/胜${st.win_rate||0}%)</option>`;}));
 document.getElementById('fstrat').innerHTML=opts.join('');
 render();
}
function render(){
 const fs=document.getElementById('fstrat').value, fd=document.getElementById('fdir').value, fr=document.getElementById('fres').value;
 let rows=ALL.filter(s=>(!fs||s.strat===fs)&&(!fd||s.dir===fd)&&(!fr||s.result===fr));
 document.getElementById('cnt').textContent=`共 ${rows.length} 条`;
 document.getElementById('rows').innerHTML=rows.slice(0,1500).map(s=>`<tr class=row data-id=${s.id} onclick=show(${s.id})>
  <td>${fmt(s.t)}</td><td><span class=badge>${(META[s.strat]||{}).label||s.strat}</span>${s.stage?(' '+s.stage):''}</td>
  <td><b>${s.symbol}</b></td><td class=${s.dir}>${s.dir==='long'?'多':'空'}</td>
  <td class="${s.result}">${s.result==='tp'?'✓':s.result==='sl'?'✗':'⏳'}</td></tr>`).join('');
}
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
 const kl=await (await fetch(`/api/klines?symbol=${s.symbol}&center=${s.t}&span=120&tf=${tf}`)).json();
 candle.setData(kl.map(k=>({time:k.t,open:k.o,high:k.h,low:k.l,close:k.c})));
 vol.setData(kl.map(k=>({time:k.t,value:k.v,color:k.c>=k.o?'#26443055':'#5c252855'})));
 lines.forEach(l=>candle.removePriceLine(l)); lines=[];
 const PL=(p,c,t)=>{if(p)lines.push(candle.createPriceLine({price:p,color:c,lineWidth:1,lineStyle:2,axisLabelVisible:true,title:t}));};
 PL(s.entry,'#58a6ff','入场');PL(s.sl,'#f85149','止损');PL(s.tp,'#3fb950','止盈');   // 切级别后价位线保留
 const times=kl.map(k=>k.t);
 const mk=[];
 // macro二买二卖: 用 extra.structure 画 爆量K/H1H2(或L1L2)分型 + 二买二卖入场(照线上)
 let ex={}; try{ ex=typeof s.extra==='string'?JSON.parse(s.extra):(s.extra||{}); }catch(e){}
 const st=ex.structure;
 const res=s.result==='tp'?'✓':s.result==='sl'?'✗':'';
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
 document.getElementById('info').textContent=`入场${s.entry} 止损${s.sl} 止盈${s.tp} 结果:${s.result==='tp'?'止盈':s.result==='sl'?'止损':'持仓'}${s.pnl_r!=null?(' '+s.pnl_r+'R'):''}`+(s.movePct?` 跌幅${s.movePct}%`:'');
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
