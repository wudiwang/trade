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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bt_registry as R

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

SIGNALS = []
META = {}
STATS = {}
DAYS = 30
app = FastAPI()


@app.get("/api/meta")
def api_meta():
    return {"meta": META, "stats": STATS}


@app.get("/api/signals")
def api_signals():
    return JSONResponse([
        {"id": s["id"], "strat": s.get("strat"), "symbol": s["symbol"], "dir": s["direction"],
         "stage": s.get("stage"), "t": s["created_at"], "entry": s["entry"], "sl": s["sl"],
         "tp": s["tp"], "result": s.get("result"), "pnl_r": s.get("pnl_r"),
         "climaxX": s.get("climaxX"), "movePct": s.get("movePct"), "anchor": s.get("anchor")}
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
  <b>📊 信号</b> <span class=muted id=cnt></span><br>
  <select id=fstrat onchange=render()></select>
  <select id=fdir onchange=render()><option value="">全方向</option><option value=long>多</option><option value=short>空</option></select>
  <select id=fres onchange=render()><option value="">全结果</option><option value=tp>盈✓</option><option value=sl>损✗</option><option value=open>持仓</option></select>
 </div>
 <table><thead><tr><th>时间</th><th>策略</th><th>币</th><th>向</th><th>结果</th></tr></thead><tbody id=rows></tbody></table>
</div>
<div id=right>
 <div id=bar><b id=title>← 点左侧信号查看当时K线</b> <span class=muted id=info></span></div>
 <div id=chart></div>
 <div id=logic><h4 id=lt>策略思路</h4><div id=ld class=muted>点一条信号,这里显示它所属策略的逻辑</div></div>
</div>
<script>
let ALL=[], META={}, STATS={}, chart, candle, vol, lines=[];
const fmt=t=>new Date(t*1000).toLocaleString('zh-CN',{hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
async function load(){
 const m=await (await fetch('/api/meta')).json(); META=m.meta; STATS=m.stats;
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
async function show(id){
 const s=ALL.find(x=>x.id===id); if(!s)return;
 document.querySelectorAll('tr.row').forEach(r=>r.classList.toggle('sel',+r.dataset.id===id));
 ensureChart();
 const tf=(META[s.strat]||{}).tf&&(META[s.strat].tf.indexOf('15m')===0)?'15m':'5m';
 const kl=await (await fetch(`/api/klines?symbol=${s.symbol}&center=${s.t}&span=120&tf=${tf}`)).json();
 candle.setData(kl.map(k=>({time:k.t,open:k.o,high:k.h,low:k.l,close:k.c})));
 vol.setData(kl.map(k=>({time:k.t,value:k.v,color:k.c>=k.o?'#26443055':'#5c252855'})));
 lines.forEach(l=>candle.removePriceLine(l)); lines=[];
 const PL=(p,c,t)=>{if(p)lines.push(candle.createPriceLine({price:p,color:c,lineWidth:1,lineStyle:2,axisLabelVisible:true,title:t}));};
 PL(s.entry,'#58a6ff','入场');PL(s.sl,'#f85149','止损');PL(s.tp,'#3fb950','止盈');
 const mk=[];
 if(s.anchor)mk.push({time:Math.floor(s.anchor/1000),position:'belowBar',color:'#d29922',shape:'circle',text:'锚'+(s.climaxX?(' '+s.climaxX+'x'):'')});
 mk.push({time:s.t,position:s.dir==='long'?'belowBar':'aboveBar',color:s.dir==='long'?'#3fb950':'#f85149',shape:s.dir==='long'?'arrowUp':'arrowDown',text:(s.dir==='long'?'买':'卖')+(s.result==='tp'?'✓':s.result==='sl'?'✗':'')});
 candle.setMarkers(mk.sort((a,b)=>a.time-b.time));
 chart.timeScale().fitContent();
 const m=META[s.strat]||{};
 document.getElementById('title').innerHTML=`<b>${s.symbol}</b> · <span class=badge>${m.label||s.strat}</span> · ${s.dir==='long'?'做多':'做空'} · ${fmt(s.t)}`;
 document.getElementById('info').textContent=`入场${s.entry} 止损${s.sl} 止盈${s.tp} 结果:${s.result==='tp'?'止盈':s.result==='sl'?'止损':'持仓'}${s.pnl_r!=null?(' '+s.pnl_r+'R'):''}`+(s.movePct?` 跌幅${s.movePct}%`:'');
 document.getElementById('lt').textContent=`策略思路 · ${m.label||s.strat} (${m.tf||''})`;
 document.getElementById('ld').innerHTML=(m.logic||['(无)']).map(x=>`<div class=li>· ${x}</div>`).join('');
}
load();
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
    print(f"[viewer] 打开 http://127.0.0.1:{a.port}")
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
