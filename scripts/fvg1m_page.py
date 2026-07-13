"""灵感001 · 证据页(用户 2026-07-12: "你给我证据就行, 我会自己判断")。

把回测抓到的信号原样画出来: 每笔给 5m(上涨段+FVG缺口+回踩) 和 1m(止跌分型+入场) 两张图,
让用户亲自核对"代码抓的形态 = 我说的形态吗", 以及漏了什么条件。

用法: .venv/Scripts/python scripts/fvg1m_page.py --out x.html --n 120
"""
import argparse
import bisect
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import bt_registry as R
import strat_fvg1m as S
from fvg1m_gainers import gain_at, load_market

PRE5, POST5 = 6, 30          # 5m 图: 上涨段起点前/入场后各留多少根
PRE1, POST1 = 90, 90         # 1m 图: 入场前/后各留多少根


def _job(a):
    sym, p5, p1, P = a
    rows = S.walk(sym, json.load(open(p5)), json.load(open(p1)), P)
    return [r for r in rows if r["result"] in ("tp", "sl", "timeout")]


def _win(k, i0, i1):
    return [[int(x["open_time"]) // 1000, float(x["open"]), float(x["high"]),
             float(x["low"]), float(x["close"])] for x in k[max(0, i0):i1]]


def build(rows, P):
    """给每个信号取 5m / 1m 两个窗口的K线。"""
    out = []
    by = {}
    for r in rows:
        by.setdefault(r["symbol"], []).append(r)
    for sym, rs in by.items():
        k5 = json.load(open(os.path.join(R.CACHE, f"{sym}_5m_30d.json")))
        k1 = json.load(open(os.path.join(R.CACHE, f"{sym}_1m_30d.json")))
        T5 = [int(x["open_time"]) // 1000 for x in k5]
        T1 = [int(x["open_time"]) // 1000 for x in k1]
        for r in rs:
            st = r["extra"]["structure"]
            g = r["extra"]["fvg"]
            i_l1 = bisect.bisect_left(T5, int(st["L1_time"]) // 1000)
            i_e5 = bisect.bisect_left(T5, r["created_at"])
            i_e1 = bisect.bisect_left(T1, r["created_at"])
            out.append({
                "sym": sym, "res": r["result"], "r": r["pnl_r"], "netr": r["net_r"],
                "entry": r["entry"], "sl": r["sl"], "tp": r["tp"],
                "t": r["created_at"], "stop_pct": r["stop_pct"], "leg_pct": r["leg_pct"],
                "gap_pct": r["gap_pct"], "wait": r["wait_1m"],
                "gain": round(r.get("gain") or 0, 1), "rank": r.get("rank") or 999,
                "gap": [g["lo"], g["hi"], g["t"] // 1000],
                "legL": st["L1"], "legH": st["H1"],
                "legLt": int(st["L1_time"]) // 1000, "legHt": int(st["H1_time"]) // 1000,
                "k5": _win(k5, i_l1 - PRE5, min(len(k5), i_e5 + POST5)),
                "k1": _win(k1, i_e1 - PRE1, min(len(k1), i_e1 + POST1)),
            })
    out.sort(key=lambda x: x["t"])
    return out


HTML = """<title>证据页 · 5m FVG回踩 + 1m止跌入场</title>
<style>
 :root{--bg:#eef0f4;--surface:#fff;--line:#d9dde4;--ink:#151a21;--muted:#616b7a;
       --up:#137a4d;--down:#c0392f;--accent:#3f5bd9;--warn:#b06a06;--warn-bg:#fdf1dc;
       --shadow:0 1px 2px rgba(16,22,34,.06),0 6px 18px rgba(16,22,34,.05)}
 @media (prefers-color-scheme:dark){:root{--bg:#0e1218;--surface:#171d26;--line:#28313d;--ink:#e4e8ee;
   --muted:#8b96a5;--up:#33b877;--down:#ef5b5b;--accent:#7c92f5;--warn:#e0a33e;--warn-bg:#2e2416;
   --shadow:0 1px 2px rgba(0,0,0,.4),0 8px 22px rgba(0,0,0,.28)}}
 :root[data-theme=dark]{--bg:#0e1218;--surface:#171d26;--line:#28313d;--ink:#e4e8ee;--muted:#8b96a5;
   --up:#33b877;--down:#ef5b5b;--accent:#7c92f5;--warn:#e0a33e;--warn-bg:#2e2416}
 :root[data-theme=light]{--bg:#eef0f4;--surface:#fff;--line:#d9dde4;--ink:#151a21;--muted:#616b7a;
   --up:#137a4d;--down:#c0392f;--accent:#3f5bd9;--warn:#b06a06;--warn-bg:#fdf1dc}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
 .wrap{max-width:1200px;margin:0 auto;padding:18px 14px 60px;display:flex;flex-direction:column;gap:18px}
 h1{font:600 24px/1.25 "Iowan Old Style",Georgia,"Songti SC",serif;margin:0}
 .sub{color:var(--muted);font-size:13.5px}
 .num{font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
 .rules{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}
 .rules h2{font:600 15px system-ui;margin:0 0 8px}
 .rules ol{margin:0;padding-left:20px;font-size:14px} .rules li{margin:3px 0}
 .rules .hint{margin-top:10px;padding:10px 12px;background:var(--warn-bg);border-radius:8px;font-size:13.5px;
   border:1px solid color-mix(in srgb,var(--warn) 30%,transparent)}
 .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
 .stat{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:11px 13px;box-shadow:var(--shadow)}
 .stat .k{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
 .stat .v{font:600 20px ui-monospace,monospace;font-variant-numeric:tabular-nums;margin-top:2px}
 .pos{color:var(--up)}.neg{color:var(--down)}
 .filters{display:flex;gap:8px;flex-wrap:wrap}
 button.f{appearance:none;border:1px solid var(--line);background:var(--surface);color:var(--ink);
   padding:7px 13px;border-radius:999px;font-size:13px;cursor:pointer}
 button.f[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
 button.f:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
 .card{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
 .card .hd{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 13px;border-bottom:1px solid var(--line)}
 .card .sym{font-weight:600}
 .chip{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
 .chip.tp{color:var(--up);border-color:color-mix(in srgb,var(--up) 40%,transparent)}
 .chip.sl{color:var(--down);border-color:color-mix(in srgb,var(--down) 40%,transparent)}
 .chip.hot{background:var(--warn-bg);color:var(--warn);border-color:color-mix(in srgb,var(--warn) 40%,transparent)}
 .card .r{margin-left:auto;font:600 13px ui-monospace,monospace}
 .panes{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line)}
 .pane{background:var(--surface);padding:6px}
 .pane .lbl{font-size:11px;color:var(--muted);padding:2px 4px 4px;letter-spacing:.04em}
 .pane svg{display:block;width:100%;height:auto}
 .card .ft{padding:8px 13px;font-size:11.5px;color:var(--muted);display:flex;gap:12px;flex-wrap:wrap}
 @media (max-width:820px){ .panes{grid-template-columns:1fr} h1{font-size:20px} }
 footer{color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:12px;line-height:1.7}
</style>
<div class=wrap>
 <header>
  <h1>证据页：代码抓到的形态，是你说的那个吗？</h1>
  <div class=sub id=sub></div>
 </header>

 <section class=rules>
  <h2>代码目前判定的规则（如果这里跟你脑子里的不一样，回测就白跑了）</h2>
  <ol>
   <li><b>上涨段</b>：5m 底分型 → 顶分型，涨幅 <span class=num>≥8%</span>，且 ≤36 根K（3小时）内完成</li>
   <li><b>FVG（缺口）</b>：上涨段内，第1根K的最高价 &lt; 第3根K的最低价 → 区间 = [前K高, 后K低]，宽度 <span class=num>≥0.3%</span></li>
   <li><b>回踩</b>：顶分型之后 24 根5m内，价格<b>低点碰到缺口上沿即算回踩</b>（不要求吃进多深）</li>
   <li><b>作废</b>：入场前若 1m <b>收盘</b>跌穿缺口下沿 → 这次机会取消</li>
   <li><b>止跌信号</b>：进入缺口后，1m 出现<b>缠论底分型</b>，且分型低点落在缺口区间内</li>
   <li><b>入场</b>：底分型的<b>确认K收盘</b>那一刻买入（右侧K收完才知道有分型，无未来函数）</li>
   <li><b>止损</b>：1m 止跌分型的低点　<b>止盈</b>：1:2</li>
  </ol>
  <div class=hint><b>你最该核对的三处</b>：③ 只碰到上沿就算回踩，是不是太松了？
   ⑤ 缠论底分型是不是你说的"止跌"？ ① 8% 的上涨门槛和 3 小时的时限，跟你眼里的"拉升"对得上吗？</div>
 </section>

 <div class=stats id=stats></div>
 <div class=filters id=filters></div>
 <div id=list style="display:flex;flex-direction:column;gap:12px"></div>
 <footer id=foot></footer>
</div>

<script>
const D=__DATA__, META=__META__;
const fmtT=t=>new Date(t*1000).toLocaleString('zh-CN',{hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
const sig=p=> p>=100?p.toFixed(1) : p>=1?p.toFixed(3) : p.toPrecision(3);

function chart(bars, marks, lines, W, H, entryT){
  const n=bars.length; if(!n) return '';
  const padL=4,padR=58,padT=6,padB=4,iw=W-padL-padR,ih=H-padT-padB;
  let lo=Math.min(...bars.map(b=>b[3])), hi=Math.max(...bars.map(b=>b[2]));
  lines.forEach(l=>{ if(l.p!=null){lo=Math.min(lo,l.p);hi=Math.max(hi,l.p);} });
  marks.filter(m=>m.box).forEach(m=>{lo=Math.min(lo,m.box[0]);hi=Math.max(hi,m.box[1]);});
  const pad=(hi-lo)*.06||hi*.01; lo-=pad; hi+=pad;
  const X=i=>padL+iw*(i+.5)/n, Y=p=>padT+ih*(hi-p)/(hi-lo), bw=Math.max(1.2,iw/n*.62);
  const at=t=>{let b=0,d=1e18;bars.forEach((x,i)=>{const dd=Math.abs(x[0]-t);if(dd<d){d=dd;b=i;}});return b;};
  let s=`<svg viewBox="0 0 ${W} ${H}">`;
  // FVG 色块
  marks.filter(m=>m.box).forEach(m=>{
    const x1=X(at(m.t0)), x2=X(n-1);
    s+=`<rect x="${x1}" y="${Y(m.box[1])}" width="${Math.max(x2-x1,2)}" height="${Math.max(Y(m.box[0])-Y(m.box[1]),1.5)}"
        fill="var(--accent)" opacity=".16"/><rect x="${x1}" y="${Y(m.box[1])}" width="${Math.max(x2-x1,2)}"
        height="${Math.max(Y(m.box[0])-Y(m.box[1]),1.5)}" fill="none" stroke="var(--accent)" stroke-opacity=".5" stroke-width=".8"/>`;
  });
  if(entryT!=null){const xe=X(at(entryT)); s+=`<line x1="${xe}" x2="${xe}" y1="${padT}" y2="${padT+ih}" stroke="var(--accent)" stroke-width=".8" stroke-dasharray="3 3" opacity=".8"/>`;}
  lines.forEach(l=>{ if(l.p==null)return;
    s+=`<line x1="${padL}" x2="${padL+iw}" y1="${Y(l.p)}" y2="${Y(l.p)}" stroke="${l.c}" stroke-width="1" stroke-dasharray="4 3" opacity=".85"/>
        <text x="${padL+iw+3}" y="${Y(l.p)+3.4}" font-size="8.5" fill="${l.c}" font-family="ui-monospace,monospace">${l.l} ${sig(l.p)}</text>`;});
  bars.forEach((b,i)=>{const[,o,h,l,c]=b,up=c>=o,col=up?'var(--up)':'var(--down)';
    s+=`<line x1="${X(i)}" x2="${X(i)}" y1="${Y(h)}" y2="${Y(l)}" stroke="${col}" stroke-width=".9"/>
        <rect x="${X(i)-bw/2}" y="${Y(Math.max(o,c))}" width="${bw}" height="${Math.max(Y(Math.min(o,c))-Y(Math.max(o,c)),.9)}" fill="${col}"/>`;});
  marks.filter(m=>m.pt).forEach(m=>{const i=at(m.t0),y=Y(m.pt);
    s+=`<circle cx="${X(i)}" cy="${y}" r="2.4" fill="var(--ink)" opacity=".75"/>
        <text x="${X(i)}" y="${y+(m.up?-6:11)}" font-size="8.5" fill="var(--muted)" text-anchor="middle">${m.l}</text>`;});
  return s+'</svg>';
}

function card(t){
  const res=t.res==='tp'?'tp':t.res==='sl'?'sl':'';
  const c5=chart(t.k5,[{box:[t.gap[0],t.gap[1]],t0:t.gap[2]},
                       {pt:t.legL,t0:t.legLt,l:'起涨',up:false},{pt:t.legH,t0:t.legHt,l:'顶',up:true}],
                 [{p:t.entry,c:'var(--accent)',l:'入场'}], 420,170, t.t);
  const c1=chart(t.k1,[{box:[t.gap[0],t.gap[1]],t0:t.k1[0][0]}],
                 [{p:t.entry,c:'var(--accent)',l:'入场'},{p:t.sl,c:'var(--down)',l:'止损'},{p:t.tp,c:'var(--up)',l:'止盈'}],
                 420,170, t.t);
  return `<article class=card>
   <div class=hd><span class=sym>${t.sym.replace('USDT','')}</span>
    <span class="chip ${res}">${t.res==='tp'?'止盈 ✓':t.res==='sl'?'止损 ✗':'超时'}</span>
    ${t.rank<=10?`<span class="chip hot">当日涨幅榜 第${t.rank}</span>`:`<span class=chip>涨幅榜第${t.rank}</span>`}
    <span class=chip>当日 ${t.gain>0?'+':''}${t.gain}%</span>
    <span class="r ${t.netr>0?'pos':'neg'}">${t.netr>0?'+':''}${t.netr.toFixed(2)}R</span></div>
   <div class=panes>
    <div class=pane><div class=lbl>5m · 上涨段 ${t.leg_pct}% + FVG缺口 ${t.gap_pct}% + 回踩</div>${c5}</div>
    <div class=pane><div class=lbl>1m · 止跌分型入场（虚线=入场时刻）</div>${c1}</div>
   </div>
   <div class=ft><span>${fmtT(t.t)}</span><span>止损距离 ${t.stop_pct}%</span>
    <span>进缺口后等了 ${t.wait} 根1m</span><span>缺口宽 ${t.gap_pct}%</span></div>
  </article>`;
}

let cur='all';
const FILTERS=[['all','全部'],['tp','只看止盈'],['sl','只看止损'],['top10','涨幅榜前10'],['edge','EDGE(你那笔)']];
const pick=()=>D.filter(t=> cur==='all'?1 : cur==='tp'?t.res==='tp' : cur==='sl'?t.res==='sl'
  : cur==='top10'?t.rank<=10 : t.sym==='EDGEUSDT');
function setF(k){cur=k;document.querySelectorAll('button.f').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.k===k)));render();}
function render(){const r=pick();document.getElementById('list').innerHTML=r.length?r.map(card).join(''):'<div style="color:var(--muted);padding:20px">没有符合的信号</div>';}
document.getElementById('filters').innerHTML=FILTERS.map(([k,l])=>
  `<button class=f data-k=${k} aria-pressed=${k==='all'} onclick="setF('${k}')">${l} <span class=num>${
    k==='all'?D.length:k==='tp'?D.filter(t=>t.res==='tp').length:k==='sl'?D.filter(t=>t.res==='sl').length
    :k==='top10'?D.filter(t=>t.rank<=10).length:D.filter(t=>t.sym==='EDGEUSDT').length}</span></button>`).join('');
document.getElementById('sub').textContent=
  `抽样展示 ${D.length} 笔（共 ${META.total} 笔信号，198个币 × 30天）。全部规则见下方，图里能看到代码到底抓了什么。`;
document.getElementById('stats').innerHTML=[
  ['全样本 信号数',META.total,''],['胜率',META.win.toFixed(1)+'%',''],
  ['毛期望',(META.gross>0?'+':'')+META.gross.toFixed(3)+'R','pos'],
  ['手续费',(-META.fee).toFixed(3)+'R','neg'],
  ['扣费后',(META.net>0?'+':'')+META.net.toFixed(3)+'R',META.net>0?'pos':'neg'],
].map(([k,v,c])=>`<div class=stat><div class=k>${k}</div><div class="v ${c}">${v}</div></div>`).join('');
document.getElementById('foot').innerHTML=
  `数据：币安永续 5m/1m，30天。手续费按 taker 单边 0.045% 往返计。结算逐根1m走，同一根内先判止损（保守）。<br>
   <b>这一页不是结论，是证据。</b>如果你发现代码抓的形态跟你说的不一样，告诉我漏了哪个条件，我改了重跑。`;
render();
</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--leg", type=float, default=8.0)
    ap.add_argument("--gap", type=float, default=0.3)
    a = ap.parse_args()
    P = dict(S.BASE, min_leg_pct=a.leg, min_gap_pct=a.gap, stop="A", tp="rr2")

    files = []
    for f in sorted(os.listdir(R.CACHE)):
        if f.endswith("_1m_30d.json") and not f.startswith("sig_"):
            sym = f[: -len("_1m_30d.json")]
            p5 = os.path.join(R.CACHE, sym + "_5m_30d.json")
            if os.path.exists(p5):
                files.append((sym, p5, os.path.join(R.CACHE, f), P))
    rows = []
    with ProcessPoolExecutor() as ex:
        for v in ex.map(_job, files, chunksize=2):
            rows.extend(v)
    print(f"[page] {len(rows)} 个信号", flush=True)

    mk = load_market()
    for r in rows:
        r["gain"] = gain_at(mk, r["symbol"], r["created_at"])
    cache = {}
    for r in rows:
        if r["gain"] is None:
            r["rank"] = 999
            continue
        key = r["created_at"] // 900
        if key not in cache:
            cache[key] = sorted((gain_at(mk, s, r["created_at"]) or -999) for s in mk)
        arr = cache[key]
        r["rank"] = len(arr) - bisect.bisect_left(arr, r["gain"])

    n = len(rows)
    win = sum(1 for r in rows if r["pnl_r"] > 0)
    meta = {"total": n, "win": win / n * 100,
            "gross": sum(r["pnl_r"] for r in rows) / n,
            "net": sum(r["net_r"] for r in rows) / n,
            "fee": sum(r["pnl_r"] - r["net_r"] for r in rows) / n}

    # 抽样: EDGE 全要 + 涨幅榜前10 一批 + 盈亏各半, 均匀铺开
    edge = [r for r in rows if r["symbol"] == "EDGEUSDT"]
    rest = [r for r in rows if r["symbol"] != "EDGEUSDT"]
    tps = [r for r in rest if r["result"] == "tp"]
    sls = [r for r in rest if r["result"] != "tp"]
    k = max(1, (a.n - len(edge)) // 2)
    pick = edge + tps[:: max(1, len(tps) // k)][:k] + sls[:: max(1, len(sls) // k)][:k]
    print(f"[page] 抽样 {len(pick)} 笔 (EDGE {len(edge)} + 盈 {k} + 亏 {k})", flush=True)

    data = build(pick, P)
    html = HTML.replace("__DATA__", json.dumps(data, separators=(",", ":"))) \
               .replace("__META__", json.dumps(meta))
    open(a.out, "w", encoding="utf-8").write(html)
    print(f"→ {a.out} ({os.path.getsize(a.out)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
