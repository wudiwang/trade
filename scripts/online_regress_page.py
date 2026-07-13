"""线上策略回归 · 手机端形态展示页(用户 2026-07-12)。

读 .btcache/sig_online_regress_30d.json + 15m K线缓存 → 生成单文件 HTML(数据内嵌, 无外链)。
手机上打开就能一张张看那波盈利单的形态, 并看到它们在时间上的扎堆分布。

用法: .venv/Scripts/python scripts/online_regress_page.py --out <path.html>
"""
import argparse
import bisect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import bt_registry as R

PRE, POST = 44, 14          # 入场前/平仓后各留多少根15m
CLUSTER = ("07-07 05:00", "07-07 09:00")     # 山寨瀑布那四小时


def load_k(sym, tf="15m", days=30):
    p = os.path.join(R.CACHE, f"{sym}_{tf}_{days}d.json")
    return json.load(open(p)) if os.path.exists(p) else None


def window(k, t_entry, t_close):
    T = [int(x["open_time"]) // 1000 for x in k]
    i = bisect.bisect_left(T, t_entry)
    j = bisect.bisect_left(T, t_close)
    lo, hi = max(0, i - PRE), min(len(k), j + POST)
    return [{"t": T[x], "o": float(k[x]["open"]), "h": float(k[x]["high"]),
             "l": float(k[x]["low"]), "c": float(k[x]["close"]),
             "v": float(k[x]["volume"])} for x in range(lo, hi)], i - lo, j - lo


def build_rows(days=30):
    sigs = json.load(open(os.path.join(R.CACHE, f"sig_online_regress_{days}d.json")))
    c0 = time.mktime(time.strptime("2026-" + CLUSTER[0], "%Y-%m-%d %H:%M"))
    c1 = time.mktime(time.strptime("2026-" + CLUSTER[1], "%Y-%m-%d %H:%M"))
    out = []
    for s in sigs:
        k = load_k(s["symbol"])
        if not k:
            continue
        bars, ei, ci = window(k, s["created_at"], s["closed_at"])
        if len(bars) < 20:
            continue
        st = (s.get("extra") or {}).get("structure") or {}
        wy = (s.get("extra") or {}).get("wyckoff") or {}
        sweep_t = None
        if wy.get("sweep_idx") is not None and st.get("H1_time" if s["direction"] == "short" else "L1_time"):
            sweep_t = int(st["H1_time" if s["direction"] == "short" else "L1_time"]) // 1000
        out.append({
            "sym": s["symbol"], "dir": s["direction"], "r": s["pnl_r"], "netr": s["net_r"],
            "entry": s["entry"], "sl": s["sl"], "tp": s["tp"],
            "t": s["created_at"], "tc": s["closed_at"],
            "vr": s.get("vol_ratio"),
            "p1": st.get("H1") if s["direction"] == "short" else st.get("L1"),
            "p1t": (int(st["H1_time"]) // 1000) if st.get("H1_time") else None,
            "p2": st.get("H2") if s["direction"] == "short" else st.get("L2"),
            "p2t": (int(st["H2_time"]) // 1000) if st.get("H2_time") else
                   ((int(st["L2_time"]) // 1000) if st.get("L2_time") else None),
            "sweept": sweep_t,
            "cluster": bool(c0 <= s["created_at"] < c1),
            "bars": [[b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars],
            "ei": ei, "ci": ci,
        })
    out.sort(key=lambda r: r["t"])
    return out


HTML = """<title>线上策略回归 · 那波盈利单长什么样</title>
<style>
 :root{
   --bg:#eef0f4; --surface:#fff; --line:#d9dde4; --ink:#151a21; --muted:#616b7a;
   --up:#137a4d; --down:#c0392f; --accent:#3f5bd9; --warn:#b06a06; --warn-bg:#fdf1dc;
   --shadow:0 1px 2px rgba(16,22,34,.06),0 6px 18px rgba(16,22,34,.05);
 }
 @media (prefers-color-scheme:dark){
   :root{--bg:#0e1218; --surface:#171d26; --line:#28313d; --ink:#e4e8ee; --muted:#8b96a5;
         --up:#33b877; --down:#ef5b5b; --accent:#7c92f5; --warn:#e0a33e; --warn-bg:#2e2416;
         --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 22px rgba(0,0,0,.28);}
 }
 :root[data-theme=dark]{--bg:#0e1218; --surface:#171d26; --line:#28313d; --ink:#e4e8ee; --muted:#8b96a5;
   --up:#33b877; --down:#ef5b5b; --accent:#7c92f5; --warn:#e0a33e; --warn-bg:#2e2416;
   --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 22px rgba(0,0,0,.28);}
 :root[data-theme=light]{--bg:#eef0f4; --surface:#fff; --line:#d9dde4; --ink:#151a21; --muted:#616b7a;
   --up:#137a4d; --down:#c0392f; --accent:#3f5bd9; --warn:#b06a06; --warn-bg:#fdf1dc;
   --shadow:0 1px 2px rgba(16,22,34,.06),0 6px 18px rgba(16,22,34,.05);}

 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);
      font:15px/1.55 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
 .wrap{max-width:1180px;margin:0 auto;padding:20px 16px 64px;display:flex;flex-direction:column;gap:22px}
 h1{font:600 26px/1.25 "Iowan Old Style",Georgia,"Songti SC",serif;margin:0;text-wrap:balance;letter-spacing:.2px}
 .sub{color:var(--muted);font-size:14px;margin-top:6px}
 .eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600}
 .num{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}

 .verdict{background:var(--warn-bg);border:1px solid color-mix(in srgb,var(--warn) 35%,transparent);
   border-radius:10px;padding:13px 15px;font-size:14px;line-height:1.6}
 .verdict b{color:var(--warn)}

 .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
 .stat{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px 14px;box-shadow:var(--shadow)}
 .stat .k{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
 .stat .v{font:600 21px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums;margin-top:3px}
 .pos{color:var(--up)} .neg{color:var(--down)}

 .panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:var(--shadow)}
 .panel h2{font:600 16px/1.3 system-ui;margin:0 0 4px}
 .panel .note{color:var(--muted);font-size:13px;margin-bottom:14px}
 .hist{display:flex;align-items:flex-end;gap:3px;height:120px;overflow-x:auto;padding-bottom:4px}
 .hb{flex:1 0 14px;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:4px;min-width:14px}
 .hb .bar{width:100%;background:var(--accent);border-radius:2px 2px 0 0;min-height:2px;opacity:.75}
 .hb.hot .bar{background:var(--warn);opacity:1}
 .hb .lbl{font-size:9px;color:var(--muted);white-space:nowrap;transform:rotate(-60deg);transform-origin:center;height:26px}
 .hb .cnt{font-size:10px;color:var(--muted);font-variant-numeric:tabular-nums}

 .bar-filter{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
 button.f{appearance:none;border:1px solid var(--line);background:var(--surface);color:var(--ink);
   padding:7px 13px;border-radius:999px;font-size:13px;cursor:pointer}
 button.f[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
 button.f:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
 .card{background:var(--surface);border:1px solid var(--line);border-radius:12px;overflow:hidden;
   box-shadow:var(--shadow);cursor:pointer;display:flex;flex-direction:column}
 .card:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
 .card .hd{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line)}
 .card .sym{font-weight:600;font-size:14px}
 .chip{font-size:11px;padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
 .chip.short{color:var(--down);border-color:color-mix(in srgb,var(--down) 40%,transparent)}
 .chip.long{color:var(--up);border-color:color-mix(in srgb,var(--up) 40%,transparent)}
 .chip.hot{background:var(--warn-bg);color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}
 .card .r{margin-left:auto;font:600 13px ui-monospace,monospace;font-variant-numeric:tabular-nums}
 .card svg{display:block;width:100%;height:auto;background:transparent}
 .card .ft{padding:8px 12px;font-size:11px;color:var(--muted);display:flex;gap:10px;flex-wrap:wrap}

 dialog{border:none;border-radius:14px;padding:0;background:var(--surface);color:var(--ink);
   max-width:min(96vw,900px);width:100%;box-shadow:0 20px 60px rgba(0,0,0,.35)}
 dialog::backdrop{background:rgba(8,11,16,.62)}
 dialog .hd{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--line)}
 dialog .body{padding:12px 14px 18px}
 dialog .close{margin-left:auto;appearance:none;border:1px solid var(--line);background:transparent;
   color:var(--ink);border-radius:8px;padding:5px 11px;cursor:pointer;font-size:13px}
 .kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin-top:12px}
 .kv div{border:1px solid var(--line);border-radius:8px;padding:7px 9px}
 .kv .k{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em}
 .kv .v{font:600 14px ui-monospace,monospace;font-variant-numeric:tabular-nums;margin-top:2px}
 footer{color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:14px;line-height:1.7}
 @media (max-width:640px){
   .wrap{padding:16px 12px 48px;gap:18px}
   h1{font-size:22px}
   .grid{grid-template-columns:1fr}
   .stats{grid-template-columns:repeat(2,1fr)}
 }
 @media (prefers-reduced-motion:no-preference){ .card{transition:transform .12s ease} .card:active{transform:scale(.985)} }
</style>

<div class=wrap>
 <header>
  <div class=eyebrow>线上策略回归 · macro_pullback</div>
  <h1>那波把曲线拉回来的盈利单，长什么样</h1>
  <div class=sub id=sub></div>
 </header>

 <div class=verdict id=verdict></div>

 <div class=stats id=stats></div>

 <section class=panel>
  <h2>它们在时间上的分布</h2>
  <div class=note>按开仓时刻分桶（每 2 小时）。橙色那几根是 07-07 清晨那次山寨集体瀑布。</div>
  <div class=hist id=hist></div>
 </section>

 <div class="bar-filter">
  <button class=f id=f-all aria-pressed=true onclick="setF('all')">全部 <span class=num id=n-all></span></button>
  <button class=f id=f-short aria-pressed=false onclick="setF('short')">做空 <span class=num id=n-short></span></button>
  <button class=f id=f-long aria-pressed=false onclick="setF('long')">做多 <span class=num id=n-long></span></button>
  <button class=f id=f-hot aria-pressed=false onclick="setF('hot')">瀑布时段 <span class=num id=n-hot></span></button>
 </div>

 <div class=grid id=grid></div>

 <footer id=foot></footer>
</div>

<dialog id=dlg>
  <div class=hd><span class=sym id=d-sym></span><span id=d-chips></span>
    <button class=close onclick="dlg.close()">关闭</button></div>
  <div class=body><div id=d-chart></div><div class=kv id=d-kv></div></div>
</dialog>

<script>
const DATA = __DATA__;
const fmtT = t => new Date(t*1000).toLocaleString('zh-CN',{hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
const dig = p => Math.min(8, Math.max(2, Math.ceil(-Math.log10(p||1))+4));
const px = (p,d) => p==null ? '—' : p.toFixed(d);

/* 蜡烛图: 纯SVG, 无外链。标出 一卖/一买(H1/L1)、二卖/二买(H2/L2)、入场、止损、止盈。 */
function chart(t, W, H){
  const bars=t.bars, n=bars.length, d=dig(t.entry);
  const sig=p=> p>=100 ? p.toFixed(1) : p>=1 ? p.toFixed(3) : p.toPrecision(3);   // 标签用有效数字, 否则小币价被裁掉
  const padL=6, padR=64, padT=8, padB=16, iw=W-padL-padR, ih=H-padT-padB;
  const lo=Math.min(t.sl,t.tp,...bars.map(b=>b[3])), hi=Math.max(t.sl,t.tp,...bars.map(b=>b[2]));
  const pad=(hi-lo)*0.06 || hi*0.01, LO=lo-pad, HI=hi+pad;
  const X=i=>padL+iw*(i+0.5)/n, Y=p=>padT+ih*(HI-p)/(HI-LO), bw=Math.max(1.4,iw/n*0.66);
  const idxOf=ts=>{let best=0,bd=1e18;bars.forEach((b,i)=>{const dd=Math.abs(b[0]-ts);if(dd<bd){bd=dd;best=i;}});return best;};
  let s=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${t.sym} K线">`;
  // 持仓区间底色
  const x0=X(t.ei), x1=X(Math.min(t.ci,n-1));
  s+=`<rect x="${x0}" y="${padT}" width="${Math.max(x1-x0,2)}" height="${ih}" fill="var(--accent)" opacity=".07"/>`;
  // 止损/止盈/入场 三条线
  const line=(p,c,lbl,dash)=>`<line x1="${padL}" x2="${padL+iw}" y1="${Y(p)}" y2="${Y(p)}" stroke="${c}" stroke-width="1" stroke-dasharray="${dash}" opacity=".85"/>
    <text x="${padL+iw+4}" y="${Y(p)+3.5}" font-size="9" fill="${c}" font-family="ui-monospace,monospace">${lbl} ${px(p,d)}</text>`;
  s+=line(t.tp,'var(--up)','止盈','4 3')+line(t.sl,'var(--down)','止损','4 3')+line(t.entry,'var(--accent)','入场','2 2');
  // 蜡烛
  bars.forEach((b,i)=>{
    const [_,o,h,l,c]=b, up=c>=o, col=up?'var(--up)':'var(--down)';
    s+=`<line x1="${X(i)}" x2="${X(i)}" y1="${Y(h)}" y2="${Y(l)}" stroke="${col}" stroke-width="1"/>`;
    s+=`<rect x="${X(i)-bw/2}" y="${Y(Math.max(o,c))}" width="${bw}" height="${Math.max(Y(Math.min(o,c))-Y(Math.max(o,c)),1)}" fill="${col}"/>`;
  });
  // 结构点: 一卖(H1)/二卖(H2) 或 一买(L1)/二买(L2)
  const short=t.dir==='short';
  const mark=(ts,p,lbl)=>{ if(ts==null||p==null) return '';
    const i=idxOf(ts), y=Y(p), up=short?-1:1;
    return `<circle cx="${X(i)}" cy="${y}" r="2.6" fill="var(--ink)" opacity=".8"/>
      <text x="${X(i)}" y="${y+(short?-7:12)}" font-size="9" fill="var(--muted)" text-anchor="middle">${lbl}</text>`;};
  s+=mark(t.p1t,t.p1,short?'一卖':'一买')+mark(t.p2t,t.p2,short?'二卖':'二买');
  // 入场箭头
  const ye=Y(t.entry), xe=X(t.ei);
  s+= short
    ? `<path d="M${xe} ${ye-11} l4.5 -6 h-9 z" fill="var(--down)"/>`
    : `<path d="M${xe} ${ye+11} l4.5 6 h-9 z" fill="var(--up)"/>`;
  return s+'</svg>';
}

function card(t,i){
  const cls=t.dir==='short'?'short':'long';
  return `<article class=card tabindex=0 onclick="open_(${i})" onkeydown="if(event.key==='Enter')open_(${i})">
    <div class=hd><span class=sym>${t.sym.replace('USDT','')}</span>
      <span class="chip ${cls}">${t.dir==='short'?'二卖 做空':'二买 做多'}</span>
      ${t.cluster?'<span class="chip hot">瀑布时段</span>':''}
      <span class="r pos">+${t.r.toFixed(1)}R</span></div>
    ${chart(t,340,150)}
    <div class=ft><span>${fmtT(t.t)}</span><span>爆量 ${t.vr??'—'}x</span><span>扣费后 +${t.netr.toFixed(2)}R</span></div>
  </article>`;
}

let cur='all';
const shown=()=>DATA.filter(t=> cur==='all'?1 : cur==='hot'?t.cluster : t.dir===cur);
function render(){
  document.getElementById('grid').innerHTML=shown().map((t,i)=>card(t,DATA.indexOf(t))).join('');
}
function setF(f){
  cur=f;
  ['all','short','long','hot'].forEach(k=>document.getElementById('f-'+k).setAttribute('aria-pressed', String(k===f)));
  render();
}
function open_(i){
  const t=DATA[i], d=dig(t.entry);
  document.getElementById('d-sym').textContent=t.sym;
  document.getElementById('d-chips').innerHTML=
    `<span class="chip ${t.dir}">${t.dir==='short'?'二卖 做空':'二买 做多'}</span>`+(t.cluster?'<span class="chip hot">瀑布时段</span>':'');
  document.getElementById('d-chart').innerHTML=chart(t,860,380);
  document.getElementById('d-kv').innerHTML=[
    ['入场',px(t.entry,d)],['止损',px(t.sl,d)],['止盈',px(t.tp,d)],
    ['盈亏(毛)','+'+t.r.toFixed(1)+'R'],['扣手续费后','+'+t.netr.toFixed(2)+'R'],['爆量倍数',(t.vr??'—')+'x'],
    ['开仓',fmtT(t.t)],['平仓',fmtT(t.tc)],
  ].map(([k,v])=>`<div><div class=k>${k}</div><div class=v>${v}</div></div>`).join('');
  document.getElementById('dlg').showModal();
}

/* 顶部数字 + 时间分布 */
(function(){
  const n=DATA.length, ns=DATA.filter(t=>t.dir==='short').length, nh=DATA.filter(t=>t.cluster).length;
  const gross=DATA.reduce((a,t)=>a+t.r,0), net=DATA.reduce((a,t)=>a+t.netr,0);
  document.getElementById('sub').textContent=`线上 macro_pullback 于 07-06 ~ 07-10 打出的 ${n} 笔盈利单 · 15m · 只看赢单`;
  document.getElementById('verdict').innerHTML=
    `<b>先说结论：这不是策略开始赚钱。</b>同期全部 611 笔已结单，毛利 -38R，<b>扣掉手续费后 -154R</b>（单均 -0.252R）。`+
    `控制台之所以显示"翻正"，是因为纸面权益曲线<b>没有扣手续费</b>。<br>`+
    `而这 ${nh} 笔赢单挤在 07-07 清晨四小时内 —— 它们不是 ${n} 个独立的赌注，而是<b>一次山寨集体瀑布</b>同时触发的一堆信号。`;
  const S=[['赢单','+'+gross.toFixed(0)+'R','pos'],['扣费后','+'+net.toFixed(0)+'R','pos'],
           ['做空 / 做多',ns+' / '+(n-ns),''],['同期全期(扣费)','-154R','neg']];
  document.getElementById('stats').innerHTML=S.map(([k,v,c])=>
    `<div class=stat><div class=k>${k}</div><div class="v ${c}">${v}</div></div>`).join('');
  ['all','short','long','hot'].forEach(k=>{
    const c= k==='all'?n : k==='hot'?nh : DATA.filter(t=>t.dir===k).length;
    document.getElementById('n-'+k).textContent=c;
  });
  // 2小时一桶的开仓时刻分布
  const buckets={};
  DATA.forEach(t=>{const d=new Date(t.t*1000); d.setMinutes(0,0,0); d.setHours(d.getHours()-(d.getHours()%2));
    const k=d.getTime(); (buckets[k]=buckets[k]||[]).push(t);});
  const keys=Object.keys(buckets).map(Number).sort((a,b)=>a-b);
  const mx=Math.max(...keys.map(k=>buckets[k].length));
  document.getElementById('hist').innerHTML=keys.map(k=>{
    const b=buckets[k], hot=b.some(t=>t.cluster);
    const dt=new Date(k), lbl=`${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')} ${String(dt.getHours()).padStart(2,'0')}h`;
    return `<div class="hb ${hot?'hot':''}" title="${lbl}: ${b.length}笔">
      <span class=cnt>${b.length}</span>
      <div class=bar style="height:${b.length/mx*70}px"></div>
      <span class=lbl>${lbl}</span></div>`;}).join('');
  document.getElementById('foot').innerHTML=
    `数据来自线上 VPS <span class=num>trade.db</span> 的真实 paper 成交记录（只读导出），K线为币安 15m。`+
    `手续费按 taker 单边 0.045% 往返计。<br>本页只展示盈利单，是为了看形态——不要把它当作策略绩效。`;
})();
render();
</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    rows = build_rows(a.days)
    html = HTML.replace("__DATA__", json.dumps(rows, separators=(",", ":")))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    open(a.out, "w", encoding="utf-8").write(html)
    kb = os.path.getsize(a.out) / 1024
    print(f"{len(rows)} 笔 → {a.out}  ({kb:.0f} KB)")


if __name__ == "__main__":
    main()
