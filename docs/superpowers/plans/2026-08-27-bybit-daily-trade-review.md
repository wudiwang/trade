# Bybit 每日交易电影与 AI 纪律复盘 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个只读、可对账、可重复运行的 Bybit 北京时间自然日日报，输出逐笔 1m/15m 交易电影、纪律证据、AI/降级复盘和 Telegram 交付包。

**Architecture:** 私有 API 适配器只负责签名、分页和规范化；账本层以 transaction log 为财务真值并重建 execution 回合；分析层只消费规范化数据，生成纪律证据和严格 JSON 的 AI 复盘；展示层生成自包含 HTML、摘要 PNG/文本和 Telegram 发送包。所有私有数据写入独立本地 SQLite，不接触现有 Binance 实盘执行表。

**Tech Stack:** Python 3.12、aiohttp、SQLite、zoneinfo/tzdata、Pillow、pytest、Bybit V5 REST、现有 Telegram Bot API、可选本机 Claude CLI。

---

## 文件结构

- Create: `app/review/__init__.py` — 复盘包入口。
- Create: `app/review/models.py` — Decimal 数据模型和序列化。
- Create: `app/review/bybit_client.py` — 只读 Bybit V5 签名、分页和公共 K 线。
- Create: `app/review/store.py` — 独立研究 SQLite 和幂等写入。
- Create: `app/review/episodes.py` — execution 回合重建与 closed PnL 对照。
- Create: `app/review/discipline.py` — 高位反转做空硬纪律和探索因子证据。
- Create: `app/review/narrator.py` — 严格 JSON AI 提示、校验和确定性降级。
- Create: `app/review/report.py` — 日统计、自包含 HTML 和 Telegram 摘要。
- Create: `app/review/image.py` — Pillow 总战绩摘要图。
- Create: `scripts/bybit_daily_review.py` — 同步、生成、发送 CLI。
- Create: `tests/fixtures/bybit_review/*.json` — 去标识的合成官方响应。
- Create: `tests/test_bybit_client.py`
- Create: `tests/test_review_episodes.py`
- Create: `tests/test_review_ledger.py`
- Create: `tests/test_review_discipline.py`
- Create: `tests/test_review_report.py`
- Modify: `app/bot/telegram.py` — 增加通用 `send_photo`/`send_document`，不改交易按钮逻辑。
- Modify: `requirements.txt` — 增加 Pillow 和 Windows 所需的 tzdata。
- Modify: `config.yaml` — 只增加无密钥的 review 开关、时区和本地路径。
- Modify: `research/ideas/006-bybit-daily-trade-review/timeline.md`
- Modify: `docs/journal/daily/2026-08-27.md`

### Task 1: 时间窗口、Decimal 模型和本地存储

**Files:**
- Create: `app/review/__init__.py`
- Create: `app/review/models.py`
- Create: `app/review/store.py`
- Create: `tests/test_review_ledger.py`

- [ ] **Step 1: 写失败测试**

```python
def test_beijing_day_is_left_closed_right_open():
    start, end = beijing_day("2026-08-27")
    assert start == 1787760000000
    assert end - start == 86_400_000

def test_transaction_change_is_financial_truth():
    rows = [tx(cash_flow="12", funding="-1", fee="2")]
    result = summarize_ledger(rows)
    assert result.closed_cash_flow == Decimal("12")
    assert result.funding == Decimal("-1")
    assert result.fees == Decimal("2")
    assert result.net_change == Decimal("9")

def test_store_upsert_is_idempotent(tmp_path):
    store = ReviewStore(tmp_path / "review.db")
    store.upsert_transactions([tx(id="same")])
    store.upsert_transactions([tx(id="same")])
    assert store.count("bybit_transactions") == 1
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_review_ledger.py -q`

Expected: FAIL，因为 `app.review` 尚不存在。

- [ ] **Step 3: 实现模型和数据库**

模型必须以 `Decimal` 保存金额，毫秒整数保存时间。数据库至少创建设计规格中的十张表，并以
`account_id + category + Bybit id` 为私有记录唯一键。实现：

```python
def beijing_day(day: str) -> tuple[int, int]: ...
def decimal(value: str | int | None) -> Decimal: ...
def summarize_ledger(rows: Sequence[Transaction]) -> LedgerSummary: ...
class ReviewStore:
    def upsert_executions(self, rows: Sequence[Execution]) -> None: ...
    def upsert_transactions(self, rows: Sequence[Transaction]) -> None: ...
    def replace_review(self, review: DailyReview) -> None: ...
```

`summarize_ledger` 只把 `TRADE/SETTLEMENT` 等交易相关类型计入表现；转入、转出、充值和提现单列。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_review_ledger.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add -- app/review tests/test_review_ledger.py
git commit -m "feat: add Bybit review ledger models"
```

### Task 2: 只读 Bybit V5 客户端

**Files:**
- Create: `app/review/bybit_client.py`
- Create: `tests/test_bybit_client.py`
- Create: `tests/fixtures/bybit_review/api_key.json`
- Create: `tests/fixtures/bybit_review/executions_page1.json`
- Create: `tests/fixtures/bybit_review/executions_page2.json`

- [ ] **Step 1: 写签名、分页和只读拒绝测试**

```python
def test_v5_signature_uses_sorted_query_and_never_logs_secret(): ...

async def test_cursor_pagination_deduplicates_exec_id(fake_server):
    rows = await client.executions("linear", START, END)
    assert [row.exec_id for row in rows] == ["e1", "e2"]

async def test_read_write_key_is_rejected(fake_server):
    fake_server.api_key_info(readOnly=0)
    with pytest.raises(ReadOnlyKeyRequired):
        await client.verify_read_only()
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_bybit_client.py -q`

Expected: FAIL，因为客户端尚不存在。

- [ ] **Step 3: 实现查询白名单客户端**

客户端公开方法严格限制为：

```python
class BybitReadClient:
    async def verify_read_only(self) -> ApiKeyInfo: ...
    async def executions(self, category, start_ms, end_ms) -> list[Execution]: ...
    async def order_history(self, category, start_ms, end_ms) -> list[Order]: ...
    async def closed_pnl(self, category, start_ms, end_ms) -> list[ClosedPnl]: ...
    async def transactions(self, category, start_ms, end_ms) -> list[Transaction]: ...
    async def wallet_balance(self) -> EquitySnapshot: ...
    async def klines(self, category, symbol, interval, start_ms, end_ms) -> list[Kline]: ...
```

签名使用官方 `timestamp + api_key + recv_window + query_string` HMAC-SHA256；日志只输出 endpoint、状态码、
cursor 和记录数。类中不得出现 POST、place、cancel、transfer 或 withdraw 方法。

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_bybit_client.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add -- app/review/bybit_client.py tests/test_bybit_client.py tests/fixtures/bybit_review
git commit -m "feat: add read-only Bybit review client"
```

### Task 3: 加减仓、反手和跨日回合重建

**Files:**
- Create: `app/review/episodes.py`
- Create: `tests/test_review_episodes.py`

- [ ] **Step 1: 写回合状态机测试**

```python
def test_add_reduce_close_builds_one_episode():
    fills = [sell(2), sell(1), buy(1), buy(2)]
    episodes = reconstruct(fills, position_mode="one_way")
    assert len(episodes) == 1
    assert [x.action for x in episodes[0].fills] == ["entry", "add", "reduce", "close"]

def test_flip_closes_then_opens_opposite_episode():
    episodes = reconstruct([sell(1), buy(2)], position_mode="one_way")
    assert [(e.direction, e.status) for e in episodes] == [("short", "closed"), ("long", "open")]

def test_hedge_mode_never_merges_position_idx(): ...
def test_cross_day_episode_keeps_full_context_but_daily_cash_is_windowed(): ...
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_review_episodes.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现确定性状态机和对账**

实现：

```python
def reconstruct(executions, orders, position_mode) -> list[TradeEpisode]: ...
def reconcile(episodes, closed_pnl, ledger, tolerance=Decimal("0.01")) -> Reconciliation: ...
```

同毫秒撮合按 `exec_time, exec_id, order_id, leaves_qty` 稳定排序。无法识别 hedge leg 时返回
`needs_review=True`，禁止猜测。`reconcile.ok=False` 时日报不得显示“已核准净盈亏”。

- [ ] **Step 4: 运行测试并提交**

Run: `python -m pytest tests/test_review_episodes.py -q`

Expected: PASS。

```powershell
git add -- app/review/episodes.py tests/test_review_episodes.py
git commit -m "feat: reconstruct Bybit trade episodes"
```

### Task 4: 高位反转做空纪律证据

**Files:**
- Create: `app/review/discipline.py`
- Create: `tests/test_review_discipline.py`

- [ ] **Step 1: 写结构与未知值测试**

```python
def test_15m_lower_high_then_lower_low_is_present(): ...
def test_1m_large_body_is_reference_not_hard_violation(): ...
def test_missing_historical_rank_is_unknown_not_absent(): ...
def test_profit_does_not_change_compliance_verdict(): ...
```

- [ ] **Step 2: 运行并确认失败**

Run: `python -m pytest tests/test_review_discipline.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现三态证据和四级结论**

```python
class EvidenceState(str, Enum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"

def evaluate_episode(episode, k1m, k15m, market_context, account_context) -> DisciplineReview: ...
```

硬纪律和探索因子分开。大实体先输出相对前 20 根实体中位数倍数，POC 使用进场前已完成 K 线的
Volume Profile；所有计算截止入场时刻，禁止使用入场后的未来 K 线判入场合规。

- [ ] **Step 4: 运行测试并提交**

Run: `python -m pytest tests/test_review_discipline.py -q`

Expected: PASS。

```powershell
git add -- app/review/discipline.py tests/test_review_discipline.py
git commit -m "feat: evaluate short reversal discipline"
```

### Task 5: AI 叙事与确定性降级

**Files:**
- Create: `app/review/narrator.py`
- Modify: `tests/test_review_discipline.py`

- [ ] **Step 1: 写 AI 输出约束测试**

```python
def test_prompt_contains_evidence_but_no_secret_or_raw_account_id(): ...
def test_invalid_ai_json_falls_back_to_deterministic_summary(): ...
def test_ai_cannot_change_pnl_or_verdict_fields(): ...
```

- [ ] **Step 2: 实现提示和严格解析**

AI 只返回：

```json
{"summary":"...","reasons":["..."],"next_action":"...","confidence":"low|medium|high"}
```

输入只含去标识的汇总、K 线证据和纪律结果；金额、分类和状态由程序写死。默认尝试本机
`claude -p`，找不到、超时或 JSON 无效时调用 `deterministic_narrative(review)`。

- [ ] **Step 3: 运行测试并提交**

Run: `python -m pytest tests/test_review_discipline.py -q`

Expected: PASS。

```powershell
git add -- app/review/narrator.py tests/test_review_discipline.py
git commit -m "feat: add evidence-bound review narration"
```

### Task 6: 日报 HTML、摘要图片和 Telegram 发送包

**Files:**
- Create: `app/review/report.py`
- Create: `app/review/image.py`
- Create: `tests/test_review_report.py`
- Modify: `app/bot/telegram.py`
- Modify: `requirements.txt`

- [ ] **Step 1: 写报告和发送降级测试**

```python
def test_report_shows_pnl_and_compliance_in_separate_columns(): ...
def test_report_contains_1m_15m_and_every_execution_marker(): ...
def test_summary_image_is_valid_png(tmp_path): ...
async def test_photo_failure_falls_back_to_text_and_document(fake_telegram): ...
```

- [ ] **Step 2: 实现自包含 HTML 和 PNG**

HTML 不引用外网脚本，复用仓库现有纯 SVG K 线做法；交易卡支持 1m/15m 切换、上一笔/下一笔、
execution 详情、纪律证据和用户反馈区。Pillow 图片尺寸 1080×1350，包含总战绩、费用、四级纪律数量、
最佳纪律交易、最严重变形和“明天只改一件事”。

Telegram 新接口：

```python
async def send_photo(self, path: str, caption: str = "") -> dict: ...
async def send_document(self, path: str, caption: str = "") -> dict: ...
```

使用 aiohttp multipart；不记录 token、chat_id 或文件原始私有内容。
`requirements.txt` 增加 `pillow>=10.0` 和 `tzdata>=2025.1`，确保 Windows 的
`ZoneInfo("Asia/Shanghai")` 可用。

- [ ] **Step 3: 运行测试并提交**

Run: `python -m pytest tests/test_review_report.py -q`

Expected: PASS。

```powershell
git add -- app/review/report.py app/review/image.py app/bot/telegram.py requirements.txt tests/test_review_report.py
git commit -m "feat: render and deliver daily trade review"
```

### Task 7: CLI 编排、配置和模拟端到端验收

**Files:**
- Create: `scripts/bybit_daily_review.py`
- Modify: `config.yaml`
- Create: `tests/test_bybit_daily_review_cli.py`

- [ ] **Step 1: 写 CLI 端到端测试**

```python
def test_fixture_run_generates_idempotent_report(tmp_path):
    first = run_cli("--fixture", "tests/fixtures/bybit_review", "--date", "2026-08-27")
    second = run_cli("--fixture", "tests/fixtures/bybit_review", "--date", "2026-08-27")
    assert first.report_id == second.report_id
    assert first.net_pnl == second.net_pnl

def test_live_mode_without_credentials_is_blocked(): ...
def test_send_requires_reconciled_report(): ...
```

- [ ] **Step 2: 实现 CLI**

支持：

```text
python scripts/bybit_daily_review.py --date 2026-08-27
python scripts/bybit_daily_review.py --today
python scripts/bybit_daily_review.py --last-24h
python scripts/bybit_daily_review.py --fixture tests/fixtures/bybit_review
python scripts/bybit_daily_review.py --date 2026-08-27 --send-telegram
```

运行顺序固定：验证只读 Key → 同步原始数据 → 幂等落库 → 重建回合 → 拉 K 线 → 生成证据 → 对账 →
生成叙事 → HTML/PNG → 可选发送。任一步失败写 sync run 状态并返回非零码。

配置只增加：

```yaml
review:
  enabled: false
  timezone: Asia/Shanghai
  db_path: data/bybit_review.db
  output_dir: artifacts/bybit_reviews
  ai_timeout_seconds: 120
  telegram_enabled: false
```

- [ ] **Step 3: 运行测试和模拟日报**

Run: `python -m pytest tests/test_bybit_daily_review_cli.py -q`

Run: `python scripts/bybit_daily_review.py --fixture tests/fixtures/bybit_review --date 2026-08-27`

Expected: 测试 PASS；生成 HTML、PNG、JSON，第二次运行不增加记录数。

- [ ] **Step 4: 提交**

```powershell
git add -- scripts/bybit_daily_review.py config.yaml tests/test_bybit_daily_review_cli.py
git commit -m "feat: orchestrate Bybit daily review"
```

### Task 8: 真实只读对账、草图发送和运行记录

**Files:**
- Modify: `research/ideas/006-bybit-daily-trade-review/timeline.md`
- Create/Modify: `docs/journal/daily/2026-08-27.md`

- [ ] **Step 1: 检查凭据存在性但不读取值**

仅检查 `BYBIT_API_KEY` 和 `BYBIT_API_SECRET` 是否配置。若缺失，记录为唯一外部阻塞，保留
`review.enabled=false` 和 `review.telegram_enabled=false`；不得打开或修改 `.env`。

- [ ] **Step 2: 有凭据时验证只读并生成首份日报**

必须先得到 `readOnly=1`。选择最近一个完整北京时间自然日，执行真实同步并对照 Bybit 页面：成交数、
已结盈亏、手续费、资金费和净变化。任何差额超过 0.01 U 不发送“已核准”报告。

- [ ] **Step 3: 发送草图/报告**

只有现有大仙机器人已在运行配置中启用，且报告通过对账时，才发送 PNG、摘要和 HTML；否则输出本地
可点击路径并记录“Telegram 发送待运行环境完成”，不得直接读取 bot secret。

- [ ] **Step 4: 最终验证**

Run: `python -m pytest -q`

Run: `git diff --check`

Expected: 全套测试通过；无空白错误；现有 Binance 实盘模块测试不回归。

- [ ] **Step 5: 写研究链路和交接日志并提交**

记录文件、命令、测试、模拟/真实对账结果、凭据阻塞、Telegram 状态和影响范围。

```powershell
git add -- research/ideas/006-bybit-daily-trade-review/timeline.md docs/journal/daily/2026-08-27.md
git commit -m "docs: record Bybit review verification"
```

## 完成边界

- “代码完成”：模拟数据端到端通过、HTML/PNG/JSON 可重复生成、全套测试通过。
- “真实日报完成”：额外要求只读 Key 验证和最近完整自然日与 Bybit 页面对账通过。
- “自动发送完成”：额外要求机器人运行环境可用并完成一次无秘密泄露的发送验证。
- 不创建真实下单能力，不部署到 VPS，不启用自动任务；这些都需要独立的上线规格。
