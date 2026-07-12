"""灵感库/研究档案读取层(用户 2026-07-12)。

research/
  ideas/<序号>-<slug>/ idea.md + timeline.md + charts/*.png   → 交易策略(可回测的灵感)
  principles/P<n>-<slug>.md                                    → 交易准则(沉淀下来的原则)

供 bt_viewer 的 /ideas 页面读取。纯本地文件, 无数据库 —— 归档就是往这些目录里加文件。
"""
import glob
import html
import os
import re

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
