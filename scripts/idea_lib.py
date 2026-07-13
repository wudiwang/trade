"""灵感库/研究档案读取层(用户 2026-07-12)。

research/
  ideas/<序号>-<slug>/ idea.md + timeline.md + charts/*.png   → 交易策略(可回测的灵感)
  principles/P<n>-<slug>.md                                    → 交易准则(沉淀下来的原则)

供 bt_viewer 的 /ideas 页面读取。纯本地文件, 无数据库 —— 归档就是往这些目录里加文件。
"""
import base64
import glob
import html
import json
import os
import re
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH = os.path.join(ROOT, "research")

STATUS_ORDER = ["灵感", "立项", "回测中", "已验证", "已证伪", "上线"]


def parse_front(text):
    """极简 frontmatter 解析(k: v, 支持 [a, b] 列表)。"""
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            body = parts[2].lstrip("\n")
            for ln in parts[1].splitlines():
                if ":" not in ln:
                    continue
                k, v = ln.split(":", 1)
                k, v = k.strip(), v.split("#")[0].strip()
                if v.startswith("[") and v.endswith("]"):
                    v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
                meta[k] = v
    return meta, body


def md2html(md, base=""):
    """够用就好的 markdown → html(标题/列表/表格/粗体/代码/引用/链接/图片)。"""
    out, in_code, in_tbl = [], False, False
    for ln in md.splitlines():
        if ln.startswith("```"):
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            out.append(html.escape(ln))
            continue
        if ln.startswith("|") and ln.endswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            tag = "th" if not in_tbl else "td"
            if not in_tbl:
                out.append("<table>")
                in_tbl = True
            out.append("<tr>" + "".join(f"<{tag}>{inline(c, base)}</{tag}>" for c in cells) + "</tr>")
            continue
        if in_tbl:
            out.append("</table>")
            in_tbl = False
        if not ln.strip():
            out.append("")
        elif ln.startswith("#"):
            n = len(ln) - len(ln.lstrip("#"))
            out.append(f"<h{min(n+1,5)}>{inline(ln.lstrip('# '), base)}</h{min(n+1,5)}>")
        elif ln.startswith(">"):
            out.append(f"<blockquote>{inline(ln.lstrip('> '), base)}</blockquote>")
        elif re.match(r"^\s*[-*] ", ln):
            out.append(f"<li>{inline(re.sub(r'^\s*[-*] ', '', ln), base)}</li>")
        elif re.match(r"^\s*\d+\. ", ln):
            out.append(f"<li>{inline(re.sub(r'^\s*\d+\. ', '', ln), base)}</li>")
        elif ln.startswith("---"):
            out.append("<hr>")
        else:
            out.append(f"<p>{inline(ln, base)}</p>")
    if in_tbl:
        out.append("</table>")
    h = "\n".join(out)
    h = re.sub(r"(<li>.*?</li>\n?)+", lambda m: "<ul>" + m.group(0) + "</ul>", h, flags=re.S)
    return h


def inline(s, base=""):
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    # 图片 ![alt](path) → 走 /research/asset
    s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)",
               lambda m: f'<img src="/research/asset?p={base}/{m.group(2)}" alt="{m.group(1)}">', s)
    # 链接 [txt](path): 站内 md 链接保留文字即可(避免死链)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               lambda m: (f'<a href="{m.group(2)}" target="_blank">{m.group(1)}</a>'
                          if m.group(2).startswith("http") else f"<u>{m.group(1)}</u>"), s)
    return s


def load_ideas():
    rows = []
    for d in sorted(glob.glob(os.path.join(RESEARCH, "ideas", "*"))):
        f = os.path.join(d, "idea.md")
        if not os.path.isdir(d) or not os.path.exists(f):
            continue
        meta, body = parse_front(open(f, encoding="utf-8").read())
        slug = os.path.basename(d)
        base = f"ideas/{slug}"
        tl = os.path.join(d, "timeline.md")
        tl_md = open(tl, encoding="utf-8").read() if os.path.exists(tl) else ""
        charts = [f"{base}/charts/{os.path.basename(c)}"
                  for c in sorted(glob.glob(os.path.join(d, "charts", "*")))]
        rows.append({
            "slug": slug, "kind": "idea",
            "id": meta.get("id", ""), "title": meta.get("title", slug),
            "status": meta.get("status", "灵感"), "type": meta.get("type", "strategy"),
            "date": meta.get("origin_date", ""), "tags": meta.get("tags", []),
            "symbol": meta.get("origin_symbol", ""),
            "charts": charts,
            "body_html": md2html(body, base),
            "timeline_html": md2html(tl_md, base),
            "steps": len(re.findall(r"^## ", tl_md, flags=re.M)),
            "mtime": int(max(os.path.getmtime(x) for x in (f, tl) if os.path.exists(x))),
        })
    return rows


def load_principles():
    rows = []
    for f in sorted(glob.glob(os.path.join(RESEARCH, "principles", "*.md"))):
        meta, body = parse_front(open(f, encoding="utf-8").read())
        rows.append({
            "slug": os.path.basename(f)[:-3], "kind": "principle",
            "id": meta.get("id", ""), "title": meta.get("title", ""),
            "status": meta.get("status", ""), "date": meta.get("origin_date", ""),
            "tags": meta.get("tags", []), "charts": [],
            "body_html": md2html(body, "principles"),
            "timeline_html": "", "steps": 0,
            "mtime": int(os.path.getmtime(f)),
        })
    return rows


def asset_path(rel):
    """把 /research/asset?p=... 的相对路径安全地解析到 research/ 内。"""
    p = os.path.normpath(os.path.join(RESEARCH, rel.replace("\\", "/")))
    if not p.startswith(os.path.normpath(RESEARCH)) or not os.path.exists(p):
        return None
    return p


# ---------------------------------------------------------------------------
# 研究闭环: 策略迭代 → 回测记录 → 标注 → 据标注迭代下一版
# 设计见 docs/knowledge/idea_lab.md
# ---------------------------------------------------------------------------

def idea_dir(slug):
    """按 slug 定位灵感目录(拒绝路径穿越)。"""
    d = os.path.normpath(os.path.join(RESEARCH, "ideas", slug))
    if not d.startswith(os.path.normpath(os.path.join(RESEARCH, "ideas"))) or not os.path.isdir(d):
        return None
    return d


def _vnum(name):
    m = re.match(r"v(\d+)", name)
    return int(m.group(1)) if m else 0


def load_versions(slug):
    """策略迭代版本: strategy/v1.md, v2.md … 新的在前。"""
    d = idea_dir(slug)
    if not d:
        return []
    out = []
    for f in glob.glob(os.path.join(d, "strategy", "v*.md")):
        name = os.path.basename(f)[:-3]
        meta, body = parse_front(open(f, encoding="utf-8").read())
        out.append({
            "v": name, "n": _vnum(name),
            "title": meta.get("title", name),
            "scanner": meta.get("scanner", ""),        # 可执行策略名; 空 = 跑不了回测
            "date": meta.get("date", ""),
            "based_on": meta.get("based_on", ""),      # 从哪一版演化而来
            "why": meta.get("why", ""),                # 为什么有这一版(通常来自上一版的标注)
            "body_html": md2html(body, f"ideas/{slug}"),
        })
    return sorted(out, key=lambda x: -x["n"])


def new_idea(title, note, data_b64="", symbol="", tf="5m", center=0):
    """新建灵感: 一张原始图 + 用户的原话 → research/ideas/<NNN>-<slug>/

    note 一字不改地存进 idea.md —— 不许用总结替换掉用户当时真实的想法(AGENTS.md)。
    Claude 之后据此写出策略 v1。
    """
    d0 = os.path.join(RESEARCH, "ideas")
    os.makedirs(d0, exist_ok=True)
    nums = []
    for p in glob.glob(os.path.join(d0, "*")):
        m = re.match(r"(\d+)-", os.path.basename(p))
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) if nums else 0) + 1
    sym = re.sub(r"[^A-Za-z0-9]", "", (symbol or "").upper()) or "idea"
    slug = f"{n:03d}-{sym.lower()}-{time.strftime('%Y%m%d')}"
    d = os.path.join(d0, slug)
    os.makedirs(d, exist_ok=True)

    chart_rel = ""
    if data_b64:
        r = save_chart(slug, data_b64, symbol, tf, center, "原始图")
        if r:
            chart_rel = "charts/" + os.path.basename(r)

    body = [
        "---",
        f"id: {n:03d}",
        f"title: {title or (sym + ' 形态')}",
        "type: strategy",
        "status: 灵感",
        f"origin_date: {time.strftime('%Y-%m-%d')}",
        f"origin_chart: {chart_rel}",
        f"origin_symbol: {sym}",
        f"origin_tf: {tf}",
        "tags: []",
        "---",
        "",
        f"## 出发点（用户原话，{time.strftime('%Y-%m-%d')}）",
        "",
    ]
    body += ["> " + ln if ln.strip() else ">" for ln in (note or "").splitlines()]
    body += [
        "",
        "## 仍待明确（由用户拍板，Claude 不许替他假设）",
        "",
        "1. 待定 —— Claude 读完上面的原话后，把必须定义清楚的地方列在这里。",
        "",
        "## 链路",
        "",
        "研究过程见 [timeline.md](timeline.md)。",
    ]
    open(os.path.join(d, "idea.md"), "w", encoding="utf-8").write("\n".join(body) + "\n")
    open(os.path.join(d, "timeline.md"), "w", encoding="utf-8").write(
        f"# 研究链路 · {n:03d} {title}\n\n"
        "> 追加式日志：每做一步（提出/回测/证伪/调整）就在最上面加一条。\n\n---\n\n"
        f"## {time.strftime('%Y-%m-%d')} · 立项\n\n从一张 {sym} 的图 + 一段想法开始。等待 Claude 写出策略 v1。\n")
    return {"slug": slug, "id": f"{n:03d}"}


def save_version(slug, title, scanner, why="", body="", based_on=""):
    """新建策略迭代版本 → strategy/vN.md。

    scanner = bt_registry.SCANS 里的可执行策略名。没有 scanner 的版本只是文字, 跑不了回测 ——
    这是"策略迭代"和"随手记想法"的分界线。
    """
    d = idea_dir(slug)
    if not d:
        return None
    p = os.path.join(d, "strategy")
    os.makedirs(p, exist_ok=True)
    n = max([_vnum(os.path.basename(f)[:-3]) for f in glob.glob(os.path.join(p, "v*.md"))] or [0]) + 1
    v = f"v{n}"
    txt = (f"---\ntitle: {title}\nscanner: {scanner}\ndate: {time.strftime('%Y-%m-%d')}\n"
           f"based_on: {based_on or (f'v{n-1}' if n > 1 else '')}\nwhy: {why}\n---\n\n{body}\n")
    open(os.path.join(p, f"{v}.md"), "w", encoding="utf-8").write(txt)
    return v


def write_backtest(slug, payload):
    """一次回测的结果 → backtests/<version>_<month>.json (同版本同月份覆盖重跑)。"""
    d = idea_dir(slug)
    if not d:
        return None
    p = os.path.join(d, "backtests")
    os.makedirs(p, exist_ok=True)
    bid = f"{payload['version']}_{payload['range']}"
    payload["id"] = bid
    json.dump(payload, open(os.path.join(p, f"{bid}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return bid


def load_backtests(slug):
    """回测记录: backtests/<v>_<range>.json。顶部是期望/笔数/结论建议, 下面是触发信号。"""
    d = idea_dir(slug)
    if not d:
        return []
    out = []
    for f in sorted(glob.glob(os.path.join(d, "backtests", "*.json")), reverse=True):
        try:
            j = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        bid = os.path.basename(f)[:-5]
        ann = load_annotations(slug, bid)
        j["id"] = bid
        j["n_annotated"] = len(ann)
        j["annotations"] = ann
        out.append(j)
    return out


def load_annotations(slug, bt_id):
    """标注记录(追加式, 同一信号以最后一条为准)。绑定回测=绑定策略版本, 一经生成不随新版本改变。"""
    d = idea_dir(slug)
    if not d:
        return {}
    f = os.path.join(d, "annotations", f"{bt_id}.jsonl")
    if not os.path.exists(f):
        return {}
    out = {}
    for ln in open(f, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
            out[str(r["sig"])] = r
        except Exception:
            pass
    return out


def save_annotation(slug, bt_id, sig, verdict, reason=""):
    """verdict: ok(符合要求) | bad(不准, 需优化筛选语句)。reason 是以后改规则的依据。"""
    d = idea_dir(slug)
    if not d:
        return None
    if verdict not in ("ok", "bad"):
        return None
    if verdict == "bad" and not reason.strip():
        return None          # 标"不准"必须说明该改哪条 —— 否则这条标注对下一版没用
    p = os.path.join(d, "annotations")
    os.makedirs(p, exist_ok=True)
    rec = {"sig": sig, "verdict": verdict, "reason": reason.strip(),
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(os.path.join(p, f"{bt_id}.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load_charts(slug):
    """原始图清单。每张图记 symbol + center(那一刻的unix秒) → 右侧才画得出对应的动态K线。

    没进清单的老图(手工放进 charts/ 的)也列出来, 只是没有 symbol/center, 右侧画不了图。
    """
    d = idea_dir(slug)
    if not d:
        return []
    man = {}
    f = os.path.join(d, "charts", "manifest.json")
    if os.path.exists(f):
        try:
            man = {r["file"]: r for r in json.load(open(f, encoding="utf-8"))}
        except Exception:
            man = {}
    out = []
    for c in sorted(glob.glob(os.path.join(d, "charts", "*"))):
        name = os.path.basename(c)
        if name == "manifest.json":
            continue
        r = dict(man.get(name, {}))
        r["file"] = name
        r["path"] = f"ideas/{slug}/charts/{name}"
        out.append(r)
    return out


def save_chart(slug, data_b64, symbol="", tf="5m", center=0, note=""):
    """原始图上传(粘贴/选文件) + 记进 manifest。

    注意: 不做图像识别 —— symbol/时间由用户给出, 不猜。见 docs/knowledge/idea_lab.md。
    """
    d = idea_dir(slug)
    if not d:
        return None
    m = re.match(r"data:image/(png|jpeg|jpg|webp);base64,(.+)$", data_b64 or "", re.S)
    if not m:
        return None
    ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
    try:
        raw = base64.b64decode(m.group(2))
    except Exception:
        return None
    if len(raw) > 12 * 1024 * 1024:
        return None
    p = os.path.join(d, "charts")
    os.makedirs(p, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", (symbol or "").upper())
    fname = "_".join(x for x in [stamp, safe, tf] if x) + f".{ext}"
    open(os.path.join(p, fname), "wb").write(raw)

    f = os.path.join(p, "manifest.json")
    rows = []
    if os.path.exists(f):
        try:
            rows = json.load(open(f, encoding="utf-8"))
        except Exception:
            rows = []
    rows.append({"file": fname, "symbol": safe, "tf": tf or "5m",
                 "center": int(center or 0), "note": (note or "").strip(),
                 "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    json.dump(rows, open(f, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return f"ideas/{slug}/charts/{fname}"
