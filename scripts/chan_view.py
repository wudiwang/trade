"""缠论底座验证器(用户 2026-07-27)。独立于主看图器(端口8531), 只为肉眼复核底座画得对不对。

在一张5m图上叠加: 蜡烛 + 笔(zigzag) + 标准中枢(上下沿框) + 一买/二买/一卖/二卖(停顿K处)。
用户可逐个核对"这是不是我要的买卖点"。引擎来自 app/engine/chan_bi.py 的 v1 锁定定义。

运行:  .venv/Scripts/python scripts/chan_view.py --port 8531
浏览器:http://127.0.0.1:8531
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from app.engine import chan_bi as CB

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".btcache")
DAYS = 30
app = FastAPI()


def _symbols():
    tag = f"_5m_{DAYS}d.json"
    out = []
    for f in glob.glob(os.path.join(CACHE, f"*{tag}")):
        s = os.path.basename(f)[:-len(tag)]
        if not s.startswith("sig_"):
            out.append(s)
    out.sort()
    return out


@app.get("/api/symbols")
def api_symbols():
    return JSONResponse(_symbols())


@app.get("/api/chart")
def api_chart(sym: str, quality: int = 0):
    path = os.path.join(CACHE, f"{sym}_5m_{DAYS}d.json")
    if not os.path.exists(path):
        return JSONResponse({"error": "no data"}, status_code=404)
    kl = json.load(open(path))
    candles = [{"t": int(k["open_time"]) // 1000, "o": float(k["open"]), "h": float(k["high"]),
                "l": float(k["low"]), "c": float(k["close"]), "v": float(k["volume"])} for k in kl]
    pts, merged, seq = CB.find_buy_points(kl, apply_quality=bool(quality))
    zs = CB.build_zhongshu(seq)
    # 笔 zigzag: 分型极值点(按原始K时间)
    bi = [{"t": int(kl[f.extreme_src_idx]["open_time"]) // 1000, "price": f.extreme_price,
           "kind": f.kind} for f in seq]
    # 中枢框(时间用起止分型的极值K时间, 价用 ZD/ZG)
    zone = [{"t0": int(kl[seq[z["start_fx"]].extreme_src_idx]["open_time"]) // 1000,
             "t1": int(kl[seq[z["end_fx"]].extreme_src_idx]["open_time"]) // 1000,
             "ZD": z["ZD"], "ZG": z["ZG"], "GG": z["GG"], "DD": z["DD"], "bis": z["bis"]} for z in zs]
    points = [{"t": int(kl[p["stall_idx"]]["open_time"]) // 1000, "type": p["type"],
               "dir": p["direction"], "price": float(kl[p["stall_idx"]]["close"]),
               "fx_price": p["fx_price"], "ref": p["ref_price"], "grade": p["grade"],
               "vol": p["vol_ratio"]} for p in pts]
    return JSONResponse({"candles": candles, "bi": bi, "zhongshu": zone, "points": points})


HTML = """<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>缠论底座验证器</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 body{margin:0;font:13px system-ui;background:#0e1116;color:#d6dae0}
 #bar{padding:8px 12px;border-bottom:1px solid #222;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
 #chart{height:74vh}
 select{background:#161b22;color:#d6dae0;border:1px solid #30363d;border-radius:5px;padding:4px 8px;font-size:14px}
 label{cursor:pointer;user-select:none} .muted{color:#8b949e}
 #info{padding:6px 12px;border-top:1px solid #222;background:#11161d;font-size:12px;min-height:20px}
 .k{color:#8b949e} b.b1{color:#3fb950}b.b2{color:#2ea043}b.s1{color:#f85149}b.s2{color:#da3633}
</style></head><body>
<div id=bar>
 <select id=sym></select>
 <label><input type=checkbox id=cB1 checked> 一买</label>
 <label><input type=checkbox id=cB2 checked> 二买</label>
 <label><input type=checkbox id=cS1 checked> 一卖</label>
 <label><input type=checkbox id=cS2 checked> 二卖</label>
 <label><input type=checkbox id=cZ checked> 中枢</label>
 <label><input type=checkbox id=cBi checked> 笔</label>
 <label><input type=checkbox id=cQ> 只看合格(最强/标准+放量2x)</label>
 <span id=cnt class=muted></span>
</div>
<div id=chart></div>
<div id=info class=muted>选币 → 图上叠加 笔/中枢/一买二买。点标记看详情。</div>
<script>
const el=document.getElementById('chart');
const chart=LightweightCharts.createChart(el,{layout:{background:{color:'#0e1116'},textColor:'#d6dae0'},
 grid:{vertLines:{color:'#161b22'},horzLines:{color:'#161b22'}},timeScale:{timeVisible:true,secondsVisible:false},
 rightPriceScale:{borderColor:'#30363d'}});
const candle=chart.addCandlestickSeries({upColor:'#3fb950',downColor:'#f85149',borderVisible:false,
 wickUpColor:'#3fb950',wickDownColor:'#f85149'});
let extra=[]; // 附加series(笔/中枢), 便于清除
function clearExtra(){extra.forEach(s=>chart.removeSeries(s));extra=[];}
function biColor(k){return '#8b949e';}
async function load(){
 const sym=document.getElementById('sym').value; if(!sym)return;
 const q=document.getElementById('cQ').checked?1:0;
 const d=await (await fetch(`/api/chart?sym=${sym}&quality=${q}`)).json();
 if(d.error){document.getElementById('info').textContent='无数据';return;}
 candle.setData(d.candles.map(k=>({time:k.t,open:k.o,high:k.h,low:k.l,close:k.c})));
 clearExtra();
 // 笔 zigzag
 if(document.getElementById('cBi').checked && d.bi.length){
   const ls=chart.addLineSeries({color:'#c9a227',lineWidth:1,priceLineVisible:false,lastValueVisible:false});
   ls.setData(d.bi.map(b=>({time:b.t,value:b.price}))); extra.push(ls);
 }
 // 中枢: 每个框上沿ZG+下沿ZD, 用whitespace断开合并成2条series
 if(document.getElementById('cZ').checked && d.zhongshu.length){
   const top=[],bot=[];
   d.zhongshu.forEach((z,i)=>{
     top.push({time:z.t0,value:z.ZG},{time:z.t1,value:z.ZG},{time:z.t1+1});
     bot.push({time:z.t0,value:z.ZD},{time:z.t1,value:z.ZD},{time:z.t1+1});
   });
   const tS=chart.addLineSeries({color:'rgba(88,166,255,.9)',lineWidth:2,priceLineVisible:false,lastValueVisible:false});
   const bS=chart.addLineSeries({color:'rgba(88,166,255,.9)',lineWidth:2,priceLineVisible:false,lastValueVisible:false});
   tS.setData(top.sort((a,b)=>a.time-b.time)); bS.setData(bot.sort((a,b)=>a.time-b.time));
   extra.push(tS,bS);
 }
 // 买卖点标记
 const show={buy1:document.getElementById('cB1').checked,buy2:document.getElementById('cB2').checked,
   sell1:document.getElementById('cS1').checked,sell2:document.getElementById('cS2').checked};
 const cfg={buy1:['一买','#3fb950','belowBar','arrowUp'],buy2:['二买','#2ea043','belowBar','arrowUp'],
   sell1:['一卖','#f85149','aboveBar','arrowDown'],sell2:['二卖','#da3633','aboveBar','arrowDown']};
 const mk=[]; window._pts={};
 d.points.filter(p=>show[p.type]).forEach(p=>{
   const c=cfg[p.type]; mk.push({time:p.t,position:c[2],color:c[1],shape:c[3],text:c[0]});
   window._pts[p.t]=p;
 });
 candle.setMarkers(mk.sort((a,b)=>a.time-b.time));
 const n=d.points.filter(p=>show[p.type]).length;
 document.getElementById('cnt').textContent=`中枢${d.zhongshu.length} · 显示买卖点${n}`;
 document.getElementById('info').innerHTML=`<span class=k>${sym}</span> 中枢 ${d.zhongshu.length} 个 · 笔 ${d.bi.length} · 点击图上标记看详情`;
}
chart.subscribeClick(param=>{
 if(!param.time||!window._pts)return; const p=window._pts[param.time]; if(!p)return;
 const nm={buy1:'一买',buy2:'二买',sell1:'一卖',sell2:'二卖'}[p.type];
 let s=`<b class=${p.type[0]+p.type.slice(-1)}>${nm}</b> @ ${new Date(p.t*1000).toLocaleString('zh-CN')} · 分型价 ${p.fx_price} · 强度 ${p.grade} · 放量 ${p.vol}x`;
 if(p.ref!=null)s+=` · ${p.type==='buy2'?'一买低点':'一卖高点'} ${p.ref} (${p.type==='buy2'?p.fx_price>p.ref:'':''}${p.type==='buy2'?' 不破✓':''})`;
 document.getElementById('info').innerHTML=s;
});
['cB1','cB2','cS1','cS2','cZ','cBi','cQ'].forEach(id=>document.getElementById(id).onchange=load);
document.getElementById('sym').onchange=load;
(async()=>{
 const syms=await (await fetch('/api/symbols')).json();
 const sel=document.getElementById('sym');
 syms.forEach(s=>{const o=document.createElement('option');o.value=o.textContent=s;sel.appendChild(o);});
 const pref=syms.indexOf('BTCUSDT'); if(pref>=0)sel.selectedIndex=pref;
 load();
})();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8531)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    global DAYS
    DAYS = a.days
    print(f"[chan_view] http://127.0.0.1:{a.port}  (缠论底座验证, {len(_symbols())}币可选)")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
