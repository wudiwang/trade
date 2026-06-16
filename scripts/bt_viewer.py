"""本地回测可视化看图器(用户 2026-06-16:点信号→看当时K线,矫正算法+肉眼复核)。

读 .btcache/ 的5m缓存 + 跑策略detect → 信号表;点一条 → 画当时K线(蜡烛+量)
+ 入场/止损/止盈线 + 形态标记(巨量高潮K、入场K),并按 止盈/止损 上色。

运行:  .venv/Scripts/python scripts/bt_viewer.py --strat smallbig --days 30
然后浏览器开  http://127.0.0.1:8530
纯本地、只读缓存,不碰币安、不碰VPS。
"""
import argparse
import glob
import importlib.util
import json
import os
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".btcache")

SIGNALS = []        # 预计算的信号(含结算结果)
CACHE_FILES = {}    # symbol -> 缓存文件路径
PARAMS = {}


def load_strat(name):
    path = os.path.join(ROOT, "app", "engine", f"strat_{name}.py")
    spec = importlib.util.spec_from_file_location(f"strat_{name}", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def build_signals(strat, days):
    sb = load_strat(strat)
    P = dict(decline_bars=10, vol_ma=20, climax_min=3.0, climax_max=12.0, drop_pct=6.0,
             dryup_window=10, dryup_ratio=0.6, sl_buf_pct=0.3, rr_target=2.0)
    tag = f"_5m_{days}d_{time.strftime('%Y%m%d')}.json"
    files = glob.glob(os.path.join(CACHE, f"*{tag}"))
    sigs = []
    for f in files:
        sym = os.path.basename(f)[: -len(tag)]
        CACHE_FILES[sym] = f
        k5 = json.load(open(f))
        if len(k5) < 80:
            continue
        for d in ("long", "short"):
            for s in sb.detect_small_to_big(k5, d, P):
                s["symbol"] = sym
                sb._settle(s, k5)
                sigs.append(s)
    sigs.sort(key=lambda s: s["created_at"])
    for i, s in enumerate(sigs):
        s["id"] = i
    return sigs, P


app = FastAPI()


@app.get("/api/signals")
def api_signals():
    return JSONResponse([
        {"id": s["id"], "symbol": s["symbol"], "dir": s["direction"],
         "t": s["created_at"], "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
         "result": s.get("result"), "pnl_r": s.get("pnl_r"),
         "climaxX": s.get("climax_ratio"), "movePct": s.get("move_pct"),
         "anchor": s.get("anchor"), "bars": s.get("bars_held")}
        for s in SIGNALS])


@app.get("/api/klines")
def api_klines(symbol: str, center: int, span: int = 120):
    f = CACHE_FILES.get(symbol)
    if not f:
        return JSONResponse([])
    k5 = json.load(open(f))
    # center 为 unix秒; 找最近的bar下标, 取前后span根
    times = [int(k["open_time"]) // 1000 for k in k5]
    import bisect
    j = bisect.bisect_left(times, center)
    lo = max(0, j - span)
    hi = min(len(k5), j + span)
    return JSONResponse([
        {"t": int(k["open_time"]) // 1000, "o": float(k["open"]), "h": float(k["high"]),
         "l": float(k["low"]), "c": float(k["close"]), "v": float(k["volume"])}
        for k in k5[lo:hi]])


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


HTML = """<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>
<title>回测看图器 · 小转大</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 body{margin:0;font:13px system-ui;background:#0e1116;color:#d6dae0;display:flex;height:100vh}
 #left{width:380px;overflow:auto;border-right:1px solid #222;flex:none}
 #right{flex:1;display:flex;flex-direction:column}
 #bar{padding:8px 12px;border-bottom:1px solid #222;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 #chart{flex:1}
 table{width:100%;border-collapse:collapse}
 th,td{padding:5px 7px;text-align:left;border-bottom:1px solid #1c2128;white-space:nowrap}
 th{position:sticky;top:0;background:#161b22}
 tr.row{cursor:pointer}
 tr.row:hover{background:#1c2530}
 tr.sel{background:#243447!important}
 .tp{color:#3fb950}.sl{color:#f85149}.long{color:#3fb950}.short{color:#f85149}
 select,button{background:#161b22;color:#d6dae0;border:1px solid #30363d;border-radius:5px;padding:4px 8px}
 .muted{color:#8b949e}
</style></head><body>
<div id=left>
 <div style="padding:8px 12px;position:sticky;top:0;background:#0e1116;z-index:2">
  <b>📊 信号</b> <span class=muted id=cnt></span><br>
  <select id=fdir onchange=render()><option value="">全方向</option><option value=long>做多</option><option value=short>做空</option></select>
  <select id=fres onchange=render()><option value="">全结果</option><option value=tp>止盈✓</option><option value=sl>止损✗</option><option value=open>持仓</option></select>
 </div>
 <table><thead><tr><th>时间</th><th>币</th><th>向</th><th>巨量</th><th>跌幅</th><th>结果</th></tr></thead>
 <tbody id=rows></tbody></table>
</div>
<div id=right>
 <div id=bar><b id=title>← 点左侧任意信号查看当时K线</b><span class=muted id=info></span></div>
 <div id=chart></div>
</div>
<script>
let ALL=[], chart, candle, vol, lines=[];
const fmt=t=>new Date(t*1000).toLocaleString('zh-CN',{hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
async function load(){ ALL=await (await fetch('/api/signals')).json(); render(); }
function render(){
 const fd=document.getElementById('fdir').value, fr=document.getElementById('fres').value;
 let rows=ALL.filter(s=>(!fd||s.dir===fd)&&(!fr||s.result===fr));
 document.getElementById('cnt').textContent=`共 ${rows.length} 条`;
 document.getElementById('rows').innerHTML=rows.map(s=>`<tr class=row data-id=${s.id} onclick=show(${s.id})>
  <td>${fmt(s.t)}</td><td><b>${s.symbol}</b></td>
  <td class=${s.dir}>${s.dir==='long'?'多':'空'}</td>
  <td>${s.climaxX}x</td><td>${s.movePct}%</td>
  <td class="${s.result}">${s.result==='tp'?'✓盈':s.result==='sl'?'✗损':'⏳'}</td></tr>`).join('');
}
function ensureChart(){
 if(chart) return;
 chart=LightweightCharts.createChart(document.getElementById('chart'),{layout:{background:{color:'#0e1116'},textColor:'#d6dae0'},grid:{vertLines:{color:'#1c2128'},horzLines:{color:'#1c2128'}},timeScale:{timeVisible:true,secondsVisible:false},rightPriceScale:{borderColor:'#30363d'}});
 candle=chart.addCandlestickSeries({upColor:'#3fb950',downColor:'#f85149',wickUpColor:'#3fb950',wickDownColor:'#f85149',borderVisible:false});
 vol=chart.addHistogramSeries({priceFormat:{type:'volume'},priceScaleId:'',scaleMargins:{top:0.82,bottom:0}});
 new ResizeObserver(()=>chart.applyOptions({width:document.getElementById('chart').clientWidth,height:document.getElementById('chart').clientHeight})).observe(document.getElementById('chart'));
}
async function show(id){
 const s=ALL.find(x=>x.id===id); if(!s)return;
 document.querySelectorAll('tr.row').forEach(r=>r.classList.toggle('sel',+r.dataset.id===id));
 ensureChart();
 const kl=await (await fetch(`/api/klines?symbol=${s.symbol}&center=${s.t}&span=120`)).json();
 candle.setData(kl.map(k=>({time:k.t,open:k.o,high:k.h,low:k.l,close:k.c})));
 vol.setData(kl.map(k=>({time:k.t,value:k.v,color:k.c>=k.o?'#26443055':'#5c252855'})));
 lines.forEach(l=>candle.removePriceLine(l)); lines=[];
 const PL=(p,c,t)=>lines.push(candle.createPriceLine({price:p,color:c,lineWidth:1,lineStyle:2,axisLabelVisible:true,title:t}));
 PL(s.entry,'#58a6ff','入场'); PL(s.sl,'#f85149','止损'); PL(s.tp,'#3fb950','止盈');
 const mk=[];
 if(s.anchor)mk.push({time:Math.floor(s.anchor/1000),position:'belowBar',color:'#d29922',shape:'circle',text:'巨量'+s.climaxX+'x'});
 mk.push({time:s.t,position:s.dir==='long'?'belowBar':'aboveBar',color:s.dir==='long'?'#3fb950':'#f85149',shape:s.dir==='long'?'arrowUp':'arrowDown',text:(s.dir==='long'?'买':'卖')+' '+(s.result==='tp'?'✓':s.result==='sl'?'✗':'')});
 candle.setMarkers(mk.sort((a,b)=>a.time-b.time));
 chart.timeScale().fitContent();
 document.getElementById('title').innerHTML=`<b>${s.symbol}</b> · ${s.dir==='long'?'做多':'做空'} · ${fmt(s.t)}`;
 document.getElementById('info').textContent=`巨量${s.climaxX}x 跌幅${s.movePct}% 入场${s.entry} 止损${s.sl} 止盈${s.tp} 结果:${s.result==='tp'?'止盈':s.result==='sl'?'止损':'持仓'}${s.pnl_r!=null?(' '+s.pnl_r+'R'):''}`;
}
load();
</script></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--strat", default="smallbig")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--port", type=int, default=8530)
    a = ap.parse_args()
    SIGNALS, PARAMS = build_signals(a.strat, a.days)
    print(f"[viewer] {a.strat}: {len(SIGNALS)} 个信号, {len(CACHE_FILES)} 个币缓存")
    print(f"[viewer] 浏览器打开 http://127.0.0.1:{a.port}")
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
