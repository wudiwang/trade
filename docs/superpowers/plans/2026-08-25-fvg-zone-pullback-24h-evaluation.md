# FVG 区域回踩过滤器 24 小时评估 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 只读扫描全部 Binance USDT 永续合约，准确比较现行线上策略与新增 FVG 影线过滤器在滚动 24 小时内的触发笔数。

**Architecture:** 新建独立评估器，不修改 `app/engine/macro_pullback.py`。评估器从一个指定的源码根目录导入线上版本策略，逐根重放闭合 K 线生成基准信号，再依据基准信号中的 L1/H1/L2/H2 索引执行纯函数式 FVG 分类。市场数据直接从 Binance 公共 REST 接口读取，运行参数由只读 VPS 审计生成的无密钥 JSON 提供。

**Tech Stack:** Python 3.12、标准库 `asyncio/json/urllib`、项目现有策略模块、pytest、Binance Futures 公共 REST API、只读 SSH 审计。

---

## 文件结构

- Create: `scripts/eval_fvg_pullback_24h.py` — 下载公共 K 线、重放线上策略、应用 FVG 分类并生成 JSON/Markdown 报告。
- Create: `tests/test_eval_fvg_pullback_24h.py` — 验证 FVG 边界、影线打穿、做空对称、多 FVG 去重及窗口计数。
- Create at runtime: `artifacts/fvg_pullback_24h_<UTC timestamp>.json` — 可复现的原始统计和信号明细。
- Create at runtime: `artifacts/fvg_pullback_24h_<UTC timestamp>.md` — 面向人工复核的汇总报告。
- Modify: `research/ideas/005-macro-pullback-fvg-filter/timeline.md` — 在顶部记录本次统计、覆盖率与结论。
- Modify: `docs/journal/daily/2026-08-25.md` — 记录命令、验证结果、风险和影响范围。

### Task 1: 实现并测试 FVG 纯函数

**Files:**
- Create: `tests/test_eval_fvg_pullback_24h.py`
- Create: `scripts/eval_fvg_pullback_24h.py`

- [ ] **Step 1: 写失败测试**

测试须构造字典 K 线并明确覆盖以下断言：

```python
def test_long_enters_zone_without_midpoint_and_passes():
    gaps = find_fvgs(long_bars(), 0, 4, "long")
    result = classify_fvg(long_bars(), gaps, second_idx=6, direction="long")
    assert result.status == "passed"

def test_long_wick_below_far_edge_is_pierced_even_if_close_recovers():
    bars = long_bars(pullback_low=99.99, pullback_close=100.50)
    result = classify_fvg(bars, find_fvgs(bars, 0, 4, "long"), 6, "long")
    assert result.status == "pierced"

def test_equal_far_edge_is_not_pierced():
    bars = long_bars(pullback_low=100.00)
    result = classify_fvg(bars, find_fvgs(bars, 0, 4, "long"), 6, "long")
    assert result.status == "passed"

def test_short_rule_is_symmetric():
    gaps = find_fvgs(short_bars(), 0, 4, "short")
    assert classify_fvg(short_bars(), gaps, 6, "short").status == "passed"

def test_multiple_matching_gaps_still_produce_one_candidate():
    result = apply_fvg_filter([baseline_signal_with_two_gaps()])
    assert len(result) == 1
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `C:\Users\ADMIN\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_eval_fvg_pullback_24h.py -q`

Expected: FAIL，因为 `scripts.eval_fvg_pullback_24h` 尚不存在。

- [ ] **Step 3: 实现最小 FVG 逻辑**

实现以下稳定接口：

```python
@dataclass(frozen=True)
class Fvg:
    index: int
    lower: float
    upper: float

@dataclass(frozen=True)
class FvgDecision:
    status: str          # passed | no_fvg | not_entered | pierced
    gap: Fvg | None

def find_fvgs(bars, start_idx, end_idx, direction):
    gaps = []
    for i in range(max(start_idx + 1, 1), min(end_idx, len(bars) - 1)):
        if direction == "long":
            lower, upper = float(bars[i - 1]["high"]), float(bars[i + 1]["low"])
        else:
            lower, upper = float(bars[i + 1]["high"]), float(bars[i - 1]["low"])
        if upper > lower:
            gaps.append(Fvg(i, lower, upper))
    return gaps

def classify_fvg(bars, gaps, second_idx, direction):
    if not gaps:
        return FvgDecision("no_fvg", None)
    entered = []
    for gap in gaps:
        path = bars[gap.index + 1:second_idx + 1]
        if direction == "long" and float(bars[second_idx]["low"]) <= gap.upper:
            entered.append(gap)
            if not any(float(bar["low"]) < gap.lower for bar in path):
                return FvgDecision("passed", gap)
        elif direction == "short" and float(bars[second_idx]["high"]) >= gap.lower:
            entered.append(gap)
            if not any(float(bar["high"]) > gap.upper for bar in path):
                return FvgDecision("passed", gap)
    return FvgDecision("pierced" if entered else "not_entered", None)
```

- [ ] **Step 4: 运行单元测试**

Run: `C:\Users\ADMIN\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_eval_fvg_pullback_24h.py -q`

Expected: PASS，至少 5 项测试通过。

- [ ] **Step 5: 提交纯函数和测试**

```powershell
git add -- scripts/eval_fvg_pullback_24h.py tests/test_eval_fvg_pullback_24h.py
git commit -m "test: define FVG pullback evaluation rules"
```

### Task 2: 实现只读全市场逐 K 重放

**Files:**
- Modify: `scripts/eval_fvg_pullback_24h.py`
- Modify: `tests/test_eval_fvg_pullback_24h.py`

- [ ] **Step 1: 写窗口、去重和分类计数测试**

```python
def test_summarize_uses_entry_time_and_deduplicates():
    rows = [
        row("BTCUSDT", "5m", "long", 1000, "passed"),
        row("BTCUSDT", "5m", "long", 1000, "passed"),
        row("ETHUSDT", "15m", "short", 999, "pierced"),
    ]
    summary = summarize(rows, window_start_ms=1000, window_end_ms=2000)
    assert summary["baseline_total"] == 1
    assert summary["candidate_total"] == 1
    assert summary["by_tf"]["5m"]["candidate"] == 1
```

- [ ] **Step 2: 运行目标测试并确认失败**

Run: `C:\Users\ADMIN\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_eval_fvg_pullback_24h.py::test_summarize_uses_entry_time_and_deduplicates -q`

Expected: FAIL，因为 `summarize` 尚不存在。

- [ ] **Step 3: 实现市场发现、数据下载、重放和报告**

CLI 必须为：

```text
python scripts/eval_fvg_pullback_24h.py \
  --source-root <线上版本独立工作树> \
  --params-json <只读参数快照> \
  --hours 24 --timeframes 5m,15m \
  --output-dir artifacts
```

实现约束：

```python
ACTIVE_CONTRACT = (
    item.get("quoteAsset") == "USDT"
    and item.get("contractType") == "PERPETUAL"
    and item.get("status") == "TRADING"
)
SIGNAL_KEY = (symbol, timeframe, direction, entry_time_ms)
```

- 从 `/fapi/v1/exchangeInfo` 选出所有 `ACTIVE_CONTRACT`。
- 每个币种、每个周期从 `/fapi/v1/klines` 获取至多 1000 根，过滤尚未闭合的最后一根。
- 将 `source_root` 插入 `sys.path[0]`，导入该工作树中的
  `app.engine.macro_pullback.detect_macro_pullback`，不得导入当前脏工作区版本。
- 对过去 24 小时内每个闭合 K 线前缀分别调用线上检测函数的 long/short 两个方向；
  用 `signal.extra["structure"]["entry_time"]` 作为真实触发时间。
- FVG 搜索区间：做多 `L1_idx..H1_idx`，做空 `H1_idx..L1_idx`；回踩终点分别是
  `L2_idx` 和 `H2_idx`。
- 用 `SIGNAL_KEY` 去重。下载或解析失败须记入 `failures`，不允许降低合约总数后静默成功。
- JSON 保存版本、参数、UTC 窗口、覆盖率、全部明细与失败项；Markdown 保存汇总和分组表。

- [ ] **Step 4: 运行完整单元测试**

Run: `C:\Users\ADMIN\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_eval_fvg_pullback_24h.py -q`

Expected: PASS，窗口外信号不计数、重复信号只计一次、四个方向/周期桶正确。

- [ ] **Step 5: 提交评估器**

```powershell
git add -- scripts/eval_fvg_pullback_24h.py tests/test_eval_fvg_pullback_24h.py
git commit -m "feat: add read-only FVG 24h evaluator"
```

### Task 3: 审计线上参数并执行统计

**Files:**
- Create at runtime: `artifacts/fvg_pullback_runtime_<UTC timestamp>.json`
- Create at runtime: `artifacts/fvg_pullback_24h_<UTC timestamp>.json`
- Create at runtime: `artifacts/fvg_pullback_24h_<UTC timestamp>.md`

- [ ] **Step 1: 建立线上版本独立工作树**

先只读确认 VPS 的 `/opt/trade/REVISION`。使用该精确提交创建临时 Git 工作树，路径必须位于
`C:\Users\ADMIN\trade-eval-worktrees\<revision>`；不得使用当前修改过的
`app/engine/macro_pullback.py`。

Expected: 工作树 `git rev-parse HEAD` 与 VPS revision 完全一致。

- [ ] **Step 2: 只读导出有效策略参数**

通过 SSH 在 VPS 上读取应用实际合并后的 `macro_pullback.*`、`risk.account_equity`、
`risk.risk_pct` 和启用周期。只输出白名单字段到本地 JSON，不读取或打印 `.env`。

Expected: JSON 包含线上 revision、`timeframes=["5m","15m"]`、directions 及检测函数使用的全部参数。

- [ ] **Step 3: 执行全市场统计**

Run: `C:\Users\ADMIN\AppData\Local\Programs\Python\Python312\python.exe scripts/eval_fvg_pullback_24h.py --source-root <工作树> --params-json <参数快照> --hours 24 --timeframes 5m,15m --output-dir artifacts`

Expected: 返回码 0；报告同时给出合约总数、成功覆盖、失败数、基准笔数、候选笔数和三类过滤数。

- [ ] **Step 4: 验证结果完整性**

- 要求 `covered + failed == discovered`。
- 要求 `candidate_total <= baseline_total`。
- 要求 `passed + no_fvg + not_entered + pierced == baseline_total`。
- 随机抽查候选信号和每类拒绝信号的 K 线索引、FVG 边界及影线关系。
- 若覆盖率低于 98%，修复下载失败并重跑，不以不完整统计作为最终答案。

### Task 4: 记录证据并提交

**Files:**
- Modify: `research/ideas/005-macro-pullback-fvg-filter/timeline.md`
- Modify: `docs/journal/daily/2026-08-25.md`

- [ ] **Step 1: 在研究时间线顶部记录结果**

记录 UTC/本地窗口、线上 revision、参数快照文件、覆盖率、现行规则笔数、候选规则笔数、
各拒绝原因和方向/周期分布。只陈述证据，不自动作上线结论。

- [ ] **Step 2: 写交接日志**

记录更改文件、执行命令、测试输出、失败合约、剩余风险，以及影响范围为“本地只读研究；
未影响 VPS paper/live”。

- [ ] **Step 3: 最终验证并提交**

Run: `git diff --check`

Run: `C:\Users\ADMIN\AppData\Local\Programs\Python\Python312\python.exe -m pytest tests/test_eval_fvg_pullback_24h.py -q`

Expected: `git diff --check` 无输出；测试全部通过。

```powershell
git add -- research/ideas/005-macro-pullback-fvg-filter/timeline.md docs/journal/daily/2026-08-25.md
git commit -m "docs: record FVG 24h evaluation"
```
