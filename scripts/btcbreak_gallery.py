"""把 btcbreak_chart.py 出的审核图打包成【单文件】HTML 画廊(图片 base64 内嵌, 无任何外部资源), 手机上逐张翻看。

入场图按时间顺序编号 #1.. (与图上的编号一致, 可以直接说「#7 不对」), 之后是到期未入场的 E1..。
每张图下面用文字列出该实例的实测值。只列形态与实测值, 不含任何结算或盈亏统计(铁律七)。

体积: 单个文件控制在 --max-mb 以内(默认 13.5)。图多时(几百张)宽 1150 / JPEG 72 放不进一个文件, 所以:
  1. <out>                —— 全部图在一个文件里; 画质自动往下调到刚好放得下(默认 WebP, 同体积下比 JPEG 清楚;
                             --format jpeg 可改回 JPEG)。图少、放得下时就是宽 1150 / 质量 72 原样。
  2. <out去掉.html>_hq_N.html —— 高清分卷(宽 1150 / JPEG 质量 72), 每卷不超过 --max-mb; 只有第 1 步降了画质才生成。
     --no-hq 可关掉。

用法:
  PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/btcbreak_gallery.py \
      --pngdir artifacts/btcbreak_audit/png --out artifacts/btcbreak_audit/gallery.html
"""
import argparse
import base64
import html
import io
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

from PIL import Image

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

MIME = {"jpeg": "image/jpeg", "webp": "image/webp"}
Q_FLOOR = {"jpeg": 20, "webp": 8}          # 再低就看不清了, 改为缩小宽度
WIDTH_STEPS = (1150, 1040, 940, 860, 780, 700)


# ---------------------------------------------------------------- 图片编码
def encode(job):
    """(path, fmt, width, quality) → (bytes, 宽, 高)。"""
    path, fmt, width, q = job
    im = Image.open(path).convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    if fmt == "webp":
        im.save(buf, "WEBP", quality=q, method=6)
    else:
        im.save(buf, "JPEG", quality=q, optimize=True, progressive=True)
    return buf.getvalue(), im.width, im.height


def encode_all(paths, fmt, width, q, pool):
    return list(pool.map(encode, [(p, fmt, width, q) for p in paths], chunksize=8))


def b64len(n: int) -> int:
    return (n + 2) // 3 * 4


def fit_single(paths, fmt, width, q_max, budget, pool):
    """找出能让全部图片(base64 后)总量 ≤ budget 的最高画质。返回 (编码结果, 宽, 质量)。"""
    sample = paths[::max(1, len(paths) // 40)]

    def est(w, q):
        tot = sum(b64len(len(b)) for b, _, _ in encode_all(sample, fmt, w, q, pool))
        return tot * len(paths) / len(sample)

    for w in [x for x in WIDTH_STEPS if x <= width] or [width]:
        lo, hi = Q_FLOOR[fmt], q_max
        if est(w, lo) > budget:
            continue                                    # 这个宽度最低画质也放不下 → 缩小宽度
        while lo < hi:                                  # 二分: 满足预算的最高质量
            mid = (lo + hi + 1) // 2
            if est(w, mid) <= budget * 0.985:
                lo = mid
            else:
                hi = mid - 1
        q = lo
        while True:
            enc = encode_all(paths, fmt, w, q, pool)
            if sum(b64len(len(b)) for b, _, _ in enc) <= budget or q <= Q_FLOOR[fmt]:
                break
            q -= 1
        if sum(b64len(len(b)) for b, _, _ in enc) <= budget:
            return enc, w, q
    raise SystemExit("图片太多: 最低画质也放不进一个文件, 请调大 --max-mb")


# ---------------------------------------------------------------- HTML
CSS = """
:root{--ink:#e8ecf1;--dim:#9aa4b1;--faint:#6e7681;--bg:#0b0e13;--panel:#141922;--line:#232b36;
--gold:#ffd33d;--warn:#ff7b72;--blue:#58a6ff;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif}
*{box-sizing:border-box}
html{scroll-padding-top:118px}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 var(--sans);-webkit-text-size-adjust:100%}
a{color:var(--blue)}
.wrap{max-width:1180px;margin:0 auto;padding:0 12px 96px}
header.top{padding:22px 0 6px}
h1{margin:0 0 6px;font:600 21px/1.3 var(--sans)}
h2{margin:34px 0 4px;font:600 19px/1.3 var(--sans)}
.sub{color:var(--dim);font-size:14px;line-height:1.7}
.status{display:inline-block;margin:10px 0 0;padding:5px 10px;border:1px solid #5a4a12;border-radius:6px;
  background:#231d08;color:var(--gold);font-size:14px}
details{margin:12px 0 0;background:var(--panel);border:1px solid var(--line);border-radius:10px}
summary{padding:13px 14px;font-size:16px;cursor:pointer;min-height:48px}
details ol{margin:0;padding:0 16px 14px 36px;color:var(--ink);font-size:15px;line-height:1.75}
details p{margin:0;padding:0 16px 14px;color:var(--dim);font-size:14px}
nav{position:sticky;top:0;z-index:9;background:var(--bg);border-bottom:1px solid var(--line);
  margin:14px -12px 0;padding:9px 12px}
.row{display:flex;gap:8px;align-items:center}
.row+.row{margin-top:8px;overflow-x:auto;scrollbar-width:none}
.row::-webkit-scrollbar{display:none}
input{flex:1;min-width:0;height:48px;padding:0 14px;border-radius:10px;border:1px solid var(--line);
  background:var(--panel);color:var(--ink);font:17px var(--mono)}
button,.btn{flex:none;min-height:48px;padding:0 16px;border-radius:10px;border:1px solid var(--line);
  background:var(--panel);color:var(--ink);font:15px var(--sans);cursor:pointer;display:inline-flex;
  align-items:center;text-decoration:none;white-space:nowrap}
button[aria-pressed=true]{background:var(--gold);color:#1a1408;border-color:var(--gold);font-weight:600}
button:focus-visible,input:focus-visible,.btn:focus-visible,summary:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
.count{color:var(--dim);font:13px var(--mono);white-space:nowrap}
.list{display:flex;flex-direction:column;gap:22px;margin-top:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.card>header{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;padding:11px 13px}
.no{font:700 23px/1 var(--mono);color:var(--gold)}
.when{font:15px/1.2 var(--mono);color:var(--ink)}
.tags{display:flex;flex-wrap:wrap;gap:6px;width:100%}
.tag{font-size:13px;line-height:1;padding:5px 8px;border-radius:6px;background:#1c2430;color:var(--dim)}
.tag.w{background:#3a1d1b;color:var(--warn)}
.fig{overflow-x:auto;background:#0e1116;border-top:1px solid var(--line);border-bottom:1px solid var(--line);
  -webkit-overflow-scrolling:touch}
.fig img{display:block;width:100%;height:auto;cursor:zoom-in}
.fig.z img{width:250%;max-width:none;cursor:zoom-out}
.hint{padding:6px 13px 0;color:var(--faint);font-size:13px}
dl{margin:0;padding:10px 13px 14px;display:grid;grid-template-columns:auto 1fr;gap:5px 12px;font-size:15px;line-height:1.5}
dt{color:var(--dim);white-space:nowrap}
dd{margin:0;font-family:var(--mono);font-size:14.5px;overflow-wrap:anywhere}
.float{position:fixed;right:12px;bottom:14px;z-index:10;display:flex;flex-direction:column;gap:8px}
.float button{width:50px;height:50px;padding:0;justify-content:center;font-size:21px;border-radius:50%;
  background:rgba(20,25,34,.82)}
dl{padding-right:58px}
.empty{color:var(--dim);text-align:center;padding:40px 0}
.parts{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 0}
@media(min-width:720px){dl{grid-template-columns:170px 1fr}.fig.z img{width:160%}}
"""

JS = """
const cards=[...document.querySelectorAll('.card')];
const chips=[...document.querySelectorAll('[data-f]')];
const cnt=document.getElementById('cnt'), empty=document.getElementById('empty');
function apply(f){let n=0;cards.forEach(c=>{const ok=f==='all'||(' '+c.dataset.k+' ').includes(' '+f+' ');
  c.hidden=!ok;if(ok)n++;});cnt.textContent=n+' 张';empty.hidden=n>0;
  document.querySelectorAll('section').forEach(s=>{s.hidden=!s.querySelector('.card:not([hidden])');});}
chips.forEach(b=>b.onclick=()=>{chips.forEach(x=>x.setAttribute('aria-pressed',x===b));apply(b.dataset.f);
  window.scrollTo({top:document.querySelector('nav').offsetTop});});
function go(){let v=document.getElementById('jump').value.trim().replace(/^#/,'').toUpperCase();if(!v)return;
  const id='c'+v;let el=document.getElementById(id);
  if(el){if(el.hidden){chips[0].click();}el.scrollIntoView();return;}
  const m=(window.PARTS||[]).find(p=>p.ids.includes(v));
  if(m){location.href=m.file+'#'+id;}else{alert('没有 #'+v);}}
document.getElementById('go').onclick=go;
document.getElementById('jump').addEventListener('keydown',e=>{if(e.key==='Enter')go();});
document.querySelectorAll('.fig').forEach(f=>f.onclick=()=>{f.classList.toggle('z');
  if(f.classList.contains('z'))f.scrollLeft=(f.scrollWidth-f.clientWidth)*0.5;});
function step(d){const vis=cards.filter(c=>!c.hidden);if(!vis.length)return;
  const y=window.scrollY+130;let i=vis.findIndex(c=>c.offsetTop+c.offsetHeight>y);if(i<0)i=vis.length-1;
  const cur=vis[i];let t;if(d>0){t=vis[Math.min(i+1,vis.length-1)];}
  else{t=(cur.offsetTop<y-20)?cur:vis[Math.max(i-1,0)];}t.scrollIntoView();}
document.getElementById('prev').onclick=()=>step(-1);
document.getElementById('next').onclick=()=>step(1);
"""

FILTERS = [            # (key, 文字, 判定)
    ("retrig", "有再破位", lambda r: any(f.startswith("再破位") for f in r["flags"])),
    ("chain", "上一轮入场后4h内", lambda r: "上一轮入场后4小时内" in r["flags"]),
    ("lowbar", "低点在破位K当根", lambda r: "低点在破位K当根" in r["flags"]),
    ("drop", "放弃过无停顿分型", lambda r: any(f.startswith("放弃") for f in r["flags"])),
    ("early", "有低点早于破位的分型", lambda r: any(k == "本轮被放弃的分型" and "低点早于破位" in v
                                             for k, v in r["facts"])),
    ("broken", "停顿前低点被破", lambda r: "停顿前分型低点已被跌破" in r["flags"]),
    ("sl", "信号前触及止损价", lambda r: "信号前已触及止损价" in r["flags"]),
    ("risk", "风险>5%", lambda r: "风险>5%" in r["flags"]),
]
WARN_FLAGS = ("停顿前分型低点已被跌破", "信号前已触及止损价", "风险>5%", "自检不一致", "同K再破位+入场",
              "并列低点判为早于破位")


def card_html(rec, src, w, h, hq_href=None) -> str:
    cid = "c" + rec["label"].lstrip("#")
    keys = [k for k, _, pred in FILTERS if pred(rec)] + [rec["kind"]]
    tags = "".join(f'<span class="tag{" w" if f in WARN_FLAGS else ""}">{html.escape(f)}</span>'
                   for f in rec["flags"])
    facts = "".join(f"<dt>{html.escape(str(k))}</dt><dd>{html.escape(str(v))}</dd>" for k, v in rec["facts"])
    if rec.get("checks"):
        facts += "<dt>自检不一致</dt><dd>" + html.escape("; ".join(rec["checks"])) + "</dd>"
    hq = (f' · <a href="{html.escape(hq_href)}">高清版里的这张</a>' if hq_href else "")
    return (f'<article class="card" id="{cid}" data-k="{" ".join(keys)}">'
            f'<header><span class="no">{html.escape(rec["label"])}</span>'
            f'<span class="when">{html.escape(rec["trigger_local"])} UTC+7</span>'
            + (f'<div class="tags">{tags}</div>' if tags else "") + "</header>"
            f'<div class="fig"><img loading="lazy" decoding="async" width="{w}" height="{h}" src="{src}" '
            f'alt="{html.escape(rec["label"])} {html.escape(rec["trigger_local"])} 审核图"></div>'
            f'<div class="hint">点图放大, 左右拖动; 再点还原{hq}</div>'
            f"<dl>{facts}</dl></article>")


def page_html(man, cards_entry, cards_exp, title, sub_extra, parts_nav, parts_js, n_total) -> str:
    p = man.get("params", {})
    rng = man.get("range") or {}
    chips = ['<button aria-pressed="true" data-f="all">全部</button>']
    for key, text, _ in FILTERS:
        chips.append(f'<button aria-pressed="false" data-f="{key}">{html.escape(text)}</button>')
    chips.append('<button aria-pressed="false" data-f="expired">到期未入场</button>')
    rule = f"""<details><summary>规则原文(图上标的就是这几条)</summary><ol>
<li>触发(BTCUSDT 5m, 收盘判定): 收盘价 &lt; 前 {p.get('lookback_hours', 48):g} 小时(不含本K)所有K的最低价, 且成交量 ≥ {p.get('vol_mult', 2):g} × 前 {p.get('vol_ma', 20)} 根均量(不含本K)。</li>
<li>触发后「武装」{p.get('ttl_hours', 4):g} 小时; 期间再次触发 = 再破位, 有效期从后一次重新起算。</li>
<li>入场(PUMPUSDT 5m): 破位之后形成的第一个底分型(包含合并后 左/中/右 三根, 中间最低), 分型低点那根K必须是破位K当根或之后。</li>
<li>停顿K = 右侧合并K走完后 {p.get('stall_max_gap', 3)} 根内, 第一根收盘 &gt; 右侧合并K最高价的K, 它就是信号K; {p.get('stall_max_gap', 3)} 根内没有 → 该分型作废, 看下一个。</li>
<li>入场价 = 信号K收盘; 止损 = 分型低点 × (1 − {p.get('sl_buffer_pct', 0.3):g}%); 止盈 = 入场 + {p.get('rr', 2):g} × (入场 − 止损)。一轮只出一次入场。</li>
</ol><p>图例: 黄色竖条 = BTC 破位K/再破位K; 蓝色底 = 武装期; 橙虚线 = 48h 参考低点; 紫框 = 左/中/右 合并K;
紫▲ = 分型低点; ★ = 停顿K(信号K)收盘 = 入场价; 灰△ = 本轮被放弃的更早分型。右列两个小图是放大。</p></details>"""
    sec_entry = (f'<section><h2>一、入场实例 {len(cards_entry)} 张</h2><div class="sub">按破位时间先后编号; '
                 f'哪张不是你心里的形态, 报编号即可(例: #7 不对)。</div><div class="list">'
                 + "\n".join(cards_entry) + "</div></section>") if cards_entry else ""
    sec_exp = (f'<section><h2>二、到期未入场 {len(cards_exp)} 张</h2><div class="sub">BTC 触发了, 但武装期内 PUMP '
               f'没有出现合格的「底分型+停顿」。</div><div class="list">' + "\n".join(cards_exp)
               + "</div></section>") if cards_exp else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>{CSS}</style></head><body><div class="wrap">
<header class="top">
<h1>{html.escape(title)}</h1>
<div class="sub">BTC 放量跌破 48h 低 → PUMP 第一个底分型+停顿 · 5m · 时间均为曼谷 UTC+7<br>
数据 {html.escape(str(rng.get('start_local', '')))} → {html.escape(str(rng.get('end_local', '')))} ·
入场 {man.get('n_entry', 0)} 轮 · 到期未入场 {man.get('n_expired', 0)} 轮{sub_extra}</div>
<div class="status">{html.escape(str(man.get('status') or ''))} · 只列形态与实测值, 未做任何结算或盈亏统计</div>
{rule}{parts_nav}
</header>
<nav><div class="row"><input id="jump" inputmode="text" autocomplete="off" placeholder="编号, 如 7 或 E1" aria-label="跳到编号">
<button id="go">跳转</button><span class="count" id="cnt">{n_total} 张</span></div>
<div class="row">{''.join(chips)}</div></nav>
{sec_entry}
{sec_exp}
<p class="empty" id="empty" hidden>本页没有符合的图</p>
</div>
<div class="float"><button id="prev" aria-label="上一张">↑</button><button id="next" aria-label="下一张">↓</button></div>
<script>window.PARTS={json.dumps(parts_js, ensure_ascii=False)};{JS}</script>
</body></html>"""


def data_uri(fmt, raw) -> str:
    return f"data:{MIME[fmt]};base64,{base64.b64encode(raw).decode()}"


def write(path, text) -> float:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return os.path.getsize(path) / 1e6


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="btcbreak_pump 审核图 → 单文件 HTML 画廊")
    ap.add_argument("--pngdir", required=True, help="btcbreak_chart.py 的 --outdir(含 manifest.json)")
    ap.add_argument("--out", required=True, help="画廊 HTML 路径")
    ap.add_argument("--max-mb", type=float, default=13.5, help="单个文件上限(10^6 字节)")
    ap.add_argument("--width", type=int, default=1150)
    ap.add_argument("--quality", type=int, default=72, help="画质上限(放不下会自动调低)")
    ap.add_argument("--format", choices=("webp", "jpeg"), default="webp",
                    help="需要降画质时用的格式; 不需要降时一律 JPEG")
    ap.add_argument("--hq", action=argparse.BooleanOptionalAction, default=True,
                    help="单文件降了画质时, 另出高清分卷")
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args(argv)

    with open(os.path.join(a.pngdir, "manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    recs = [r for r in man["charts"] if os.path.exists(os.path.join(a.pngdir, r["file"]))]
    missing = len(man["charts"]) - len(recs)
    entries = [r for r in recs if r["kind"] == "entry"]
    expired = [r for r in recs if r["kind"] != "entry"]
    order = entries + expired
    paths = [os.path.join(a.pngdir, r["file"]) for r in order]
    if not order:
        raise SystemExit("没有图: 先运行 scripts/btcbreak_chart.py")
    out = os.path.abspath(a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    stem, base = os.path.splitext(out)[0], os.path.basename(os.path.splitext(out)[0])
    limit = int(a.max_mb * 1e6)
    title = "btcbreak_pump 形态审核"

    with ProcessPoolExecutor(a.jobs) as pool:
        # ---- 先按规格(宽 1150 / JPEG 72)编码; 放得下就直接出一个文件
        hq = encode_all(paths, "jpeg", a.width, a.quality, pool)
        skeleton = len(page_html(man, [card_html(r, "", 1, 1, "x" * 40) for r in entries],
                                 [card_html(r, "", 1, 1, "x" * 40) for r in expired], title, "x" * 400,
                                 "x" * 2000, [], len(order)).encode("utf-8")) + 64 * len(order)
        budget = limit - skeleton
        hq_total = sum(b64len(len(b)) + 30 for b, _, _ in hq)
        if hq_total <= budget:
            cards = [card_html(r, data_uri("jpeg", b), w, h) for r, (b, w, h) in zip(order, hq)]
            mb = write(out, page_html(man, cards[:len(entries)], cards[len(entries):], title,
                                      f" · 图 {a.width}px JPEG q{a.quality}", "", [], len(order)))
            print(f"画廊 {out.replace(os.sep, '/')}  {len(entries)} 张入场 + {len(expired)} 张到期  {mb:.2f} MB")
            return 0

        # ---- 放不下: (a) 高清分卷
        part_of, parts = {}, []
        if a.hq:
            cur, size = [], 0
            for i, (r, (b, w, h)) in enumerate(zip(order, hq)):
                need = b64len(len(b)) + 3500
                if cur and size + need > limit - 60_000:
                    parts.append(cur)
                    cur, size = [], 0
                cur.append(i)
                size += need
            parts.append(cur)
            names = [f"{base}_hq_{k + 1}.html" for k in range(len(parts))]
            for k, idxs in enumerate(parts):
                for i in idxs:
                    part_of[order[i]["label"]] = names[k]
            parts_js = [{"file": names[k], "ids": [order[i]["label"].lstrip("#") for i in idxs]}
                        for k, idxs in enumerate(parts)]

            def rng_text(idxs):
                labs = [order[i]["label"] for i in idxs]
                return labs[0] if len(labs) == 1 else f"{labs[0]}–{labs[-1]}"

            for k, idxs in enumerate(parts):
                nav = ('<div class="parts">' + f'<a class="btn" href="{base}.html">单文件版(全部)</a>' + "".join(
                    f'<a class="btn" href="{names[j]}"{" aria-current=page" if j == k else ""}>'
                    f'高清 {j + 1}: {html.escape(rng_text(parts[j]))}</a>' for j in range(len(parts))) + "</div>")
                cards = [card_html(order[i], data_uri("jpeg", hq[i][0]), hq[i][1], hq[i][2]) for i in idxs]
                ce = [c for c, i in zip(cards, idxs) if order[i]["kind"] == "entry"]
                cx = [c for c, i in zip(cards, idxs) if order[i]["kind"] != "entry"]
                mb = write(f"{stem}_hq_{k + 1}.html", page_html(
                    man, ce, cx, f"{title} · 高清 {k + 1}/{len(parts)}",
                    f" · 本卷 {rng_text(idxs)} · 图 {a.width}px JPEG q{a.quality}", nav, parts_js, len(idxs)))
                print(f"高清分卷 {names[k]}  {rng_text(idxs)}  {len(idxs)} 张  {mb:.2f} MB")

        # ---- (b) 单文件: 自动降画质
        enc, w_used, q_used = fit_single(paths, a.format, a.width, a.quality, budget - 30 * len(order), pool)

    nav = ""
    if parts:
        nav = ('<div class="parts">' + "".join(
            f'<a class="btn" href="{base}_hq_{j + 1}.html">高清 {j + 1}: '
            f'{html.escape(order[parts[j][0]]["label"])}–{html.escape(order[parts[j][-1]]["label"])}</a>'
            for j in range(len(parts))) + "</div>")
    cards = [card_html(r, data_uri(a.format, b), w, h,
                       f'{part_of[r["label"]]}#c{r["label"].lstrip("#")}' if r["label"] in part_of else None)
             for r, (b, w, h) in zip(order, enc)]
    note = (f" · 本文件为了放进一个文件, 图片压到 {w_used}px {a.format.upper()} q{q_used}"
            + ("; 细看请点每张图下面的「高清版」(需与本文件放在同一目录)" if parts else ""))
    mb = write(out, page_html(man, cards[:len(entries)], cards[len(entries):], title, note, nav, [], len(order)))
    print(f"画廊 {out.replace(os.sep, '/')}  {len(entries)} 张入场 + {len(expired)} 张到期  "
          f"{w_used}px {a.format} q{q_used}  {mb:.2f} MB")
    if missing:
        print(f"注意: manifest 里有 {missing} 张图不在 {a.pngdir}, 未收入画廊")
    if mb * 1e6 > limit:
        print(f"警告: 超过上限 {a.max_mb} MB")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
