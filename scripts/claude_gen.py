"""调用【本机的 Claude Code CLI】(headless: claude -p) 把自然语言形态描述变成可回测的策略。

三步:
    1. spec()     自然语言 → 结构化筛选条件(每条都必须可量化, 模糊处必须标"待定"而不是替用户假设)
    2. codegen()  筛选条件 → strategy/vN.py (scan 函数) + vN.md (优化后的文字版策略)
    3. iterate()  盲测标注(符合/不符合+理由) → 下一版 v(N+1)

安全边界(重要):
    生成的是【会被执行的 Python 代码】。本模块只负责【写文件】, 绝不自动运行。
    要跑, 用户必须在页面上另外点「跑回测」—— 那一步才是审查关口。
    代码写进 research/ideas/<slug>/strategy/, 页面上全文展示。
"""
import json
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KIT = os.path.join(ROOT, "scripts", "strat_kit.py")


def _claude(prompt, model="sonnet", timeout=300):
    """跑一次 headless claude。返回 (ok, text)。"""
    exe = "claude"
    try:
        p = subprocess.run([exe, "-p", "--model", model],
                           input=prompt, capture_output=True, text=True,
                           encoding="utf-8", timeout=timeout, cwd=ROOT)
    except FileNotFoundError:
        return False, "本机找不到 claude CLI —— 这个功能依赖 Claude Code 已安装且在 PATH 里。"
    except subprocess.TimeoutExpired:
        return False, f"Claude 超时({timeout}s)。"
    if p.returncode != 0:
        return False, f"Claude 返回错误: {(p.stderr or '')[:300]}"
    return True, (p.stdout or "").strip()


def _block(text, lang):
    """抠出 ```lang ... ``` 代码块(取最长的一段, 防止它先给个小示例)。"""
    blocks = re.findall(rf"```{lang}\s*\n(.*?)```", text, re.S)
    if not blocks:
        blocks = re.findall(r"```\s*\n(.*?)```", text, re.S)
    return max(blocks, key=len).strip() if blocks else ""


RULES = """
## 铁律(违反了回测就是假的, 必须逐条遵守)

1. **不许有未来函数**: 判定条件只能引用 k[i] 及之前的K线; 碰 k[i+1] 就是作弊。
   入场价【只能】用触发K的收盘价 k[i]["close"] —— 用当根 high/low 或下一根的价格都是作弊。
2. **用户已经拍板的条件, 必须一条不落地落实**, 而且要用他的口径, 不许换成你自己觉得差不多的
   近似物。他明确说了的东西(倍数、阈值、"关键是XXX")比你的任何直觉都重要。
3. **模糊的地方标「待定」, 不许替用户假设**: 他没说清的阈值, 给一个【有依据的默认值】并在文字版
   里明确标出"这是我定的, 需要你拍板"。
4. **优先用工具箱里现成的函数**, 不要自己重新发明:
   - `hvn(k, lo, hi)` = 密集成交区(筹码堆积处)。用户说"阻力位/密集成交区"时【必须】用它,
     不许用"前高"或"振幅小的区间"来近似 —— 那是完全不同的东西。
   - `reclaim(k, i, ia)` = k[i] 收盘是否把 k[ia] 那根下跌K线【收回去】了。
   - `vol_x(k, i, n)` = 第i根的量是前n根均量的几倍。`body(b)` / `rng(b)` / `is_bull` / `is_bear`。
   - `ema/sma/atr`。
5. **参数集中在 BASE dict**, 不要把魔法数字散落在代码里 —— 后面要靠调它们迭代。
6. 结算不用你写: 最后 `return settle_all(rows, k5, max_hold=288)` 即可。
"""


def spec(note, symbol="", tf="5m"):
    """自然语言 → 结构化筛选条件。"""
    prompt = f"""你是量化策略工程师。用户用大白话描述了一个他想抓的K线形态, 你要把它拆成
【可以被代码逐条判定】的筛选条件。

## 用户的原话（不许曲解, 不许自行发挥）
{note}

标的: {symbol or '(未指定)'} · 级别: {tf}

{RULES}

## 输出格式(只输出这一个 json 代码块, 不要别的废话)

```json
{{
  "name": "给这个形态起个精准的名字",
  "direction": "long 或 short",
  "conditions": [
    {{"id": "c1",
      "用户的话": "从原话里摘出对应的那句",
      "量化定义": "代码能判定的精确描述",
      "参数": {{"参数名": 默认值}},
      "把握度": "高/中/低 —— 低=用户没说清, 我猜的",
      "待用户拍板": "如果把握度不是高, 写明需要他确认什么; 否则空字符串"}}
  ],
  "entry": "入场时机(必须是某根K的收盘)",
  "sl": "止损放哪儿, 为什么",
  "tp": "止盈放哪儿, 为什么",
  "疑问": ["必须由用户拍板才能定下来的问题, 按重要性排序"]
}}
```"""
    ok, out = _claude(prompt, model="sonnet")
    if not ok:
        return False, out, None
    raw = _block(out, "json")
    try:
        return True, out, json.loads(raw)
    except Exception as e:
        return False, f"Claude 返回的不是合法 JSON: {e}\n---\n{out[:600]}", None


def codegen(note, spec_json, slug, vnum, symbol="", tf="5m"):
    """筛选条件 → Python scan() + 优化后的文字版策略。返回 (ok, msg, {py, md, title}）。"""
    kit = open(KIT, encoding="utf-8").read()
    prompt = f"""你是量化策略工程师。把下面的筛选条件写成【可回测的 Python 策略】。

## 用户的原话
{note}

## 已经拆好的筛选条件
```json
{json.dumps(spec_json, ensure_ascii=False, indent=1)}
```

## 你必须依赖的工具箱(scripts/strat_kit.py, 已经存在, 直接 import)
```python
{kit}
```

{RULES}

## 输出格式(两个代码块, 顺序固定, 别的话一句都不要)

第一块 —— 策略代码(必须能被 `exec` 后拿到 `scan` 和 `BASE`):
```python
from strat_kit import settle_all, hvn, reclaim, ema, sma, atr, vol_x, body, rng, is_bull, is_bear, engulf_bull

TITLE = "策略名"

BASE = dict(
    # 所有可调参数放这, 每个都写注释说明它是什么、为什么是这个默认值
)

def scan(C, P=None):
    P = dict(BASE, **(P or {{}}))
    k5 = C("{tf}")
    rows = []
    for sym, k in k5.items():
        if len(k) < 60:
            continue
        for i in range(50, len(k) - 1):
            # 逐条判定筛选条件, 只能看 k[i] 及之前
            ...
            rows.append({{"symbol": sym, "tf": "{tf}", "direction": "long",
                          "created_at": int(k[i]["open_time"]) // 1000, "i": i,
                          "entry": float(k[i]["close"]), "sl": ..., "tp": ...}})
    return settle_all(rows, k5, max_hold=288)
```

第二块 —— 文字版策略(markdown)。这一版要比用户口述的【更周全】: 把他没想到但必须定义的地方
补上, 并明确标出哪些是你替他定的默认值、需要他拍板:
```markdown
## 形态

## 判定条件(逐条)

## 入场 / 止损 / 止盈

## 我替你定的默认值（需要你拍板）

## 这一版还没解决的问题
```"""
    ok, out = _claude(prompt, model="sonnet", timeout=420)
    if not ok:
        return False, out, None
    py, md = _block(out, "python"), _block(out, "markdown")
    if not py or "def scan" not in py:
        return False, f"Claude 没给出合法的 scan() 代码。\n---\n{out[:600]}", None
    m = re.search(r'TITLE\s*=\s*["\'](.+?)["\']', py)
    return True, "ok", {"py": py, "md": md or "(Claude 没给出文字版)",
                        "title": m.group(1) if m else f"v{vnum}"}


def iterate(note, prev_py, prev_md, annotations, vnum, tf="5m"):
    """据盲测标注迭代出下一版。annotations: [{verdict, reason}, ...]"""
    ok_n = sum(1 for a in annotations if a["verdict"] == "ok")
    bad = [a["reason"] for a in annotations if a["verdict"] == "bad" and a.get("reason")]
    groups = {}
    for r in bad:
        groups[r] = groups.get(r, 0) + 1
    ranked = sorted(groups.items(), key=lambda kv: -kv[1])
    rate = round(ok_n / len(annotations) * 100, 1) if annotations else 0

    kit = open(KIT, encoding="utf-8").read()
    prompt = f"""你是量化策略工程师。用户对上一版策略选出来的入场点做了【盲测】(只看触发那一刻的K线,
不给后市, 判完才揭晓 —— 所以他的判断没有被结果污染), 现在要据此迭代出更准的下一版。

## 用户最初的原话(北极星, 不许偏离)
{note}

## 上一版的文字策略
{prev_md[:3000]}

## 上一版的代码
```python
{prev_py[:6000]}
```

## 盲测结果
- 共判 {len(annotations)} 笔, 符合 {ok_n} 笔 → **图形通过率 {rate}%**
- 「不符合」的理由(按出现次数):
{chr(10).join(f'  - {n}次: {r}' for r, n in ranked) or '  (无)'}

## 你的任务
出现次数最多的理由 = 最该先收紧的那条筛选语句。据此改规则, 目标是【提高图形通过率】——
现在只管形态对不对, **不要为了让回测赚钱去调参数**(那是下一阶段的事, 现在调就是过拟合)。

## 你必须依赖的工具箱(已存在)
```python
{kit[:2500]}
```

{RULES}

## 输出格式(两块, 顺序固定)

```python
# 新版策略代码, 结构同上一版
```

```markdown
## 这一版改了什么（对应用户哪条不满意）
## 判定条件(逐条)
## 我替你定的默认值（需要你拍板）
## 还没解决的问题
```"""
    ok, out = _claude(prompt, model="sonnet", timeout=420)
    if not ok:
        return False, out, None
    py, md = _block(out, "python"), _block(out, "markdown")
    if not py or "def scan" not in py:
        return False, f"Claude 没给出合法的 scan() 代码。\n---\n{out[:600]}", None
    m = re.search(r'TITLE\s*=\s*["\'](.+?)["\']', py)
    return True, "ok", {"py": py, "md": md or "", "title": m.group(1) if m else f"v{vnum}",
                        "pass_rate": rate,
                        "why": (f"上一版通过率 {rate}%; 最主要的不满意: "
                                + (ranked[0][0] if ranked else "无"))}
