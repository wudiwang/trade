# 把本地回测系统迁到 Mac

这套东西是纯 Python + FastAPI，跨平台。已确认**没有硬编码 Windows 路径、没有 Windows 专用库**。
迁移 = git 拉代码 + 装依赖 + 准备 K 线数据。

## 一、拉代码

```bash
git clone git@github.com:wudiwang/trade.git
cd trade
git checkout codex/macro-chan-pullback     # 回测系统都在这个分支
```

git 里**有**：所有脚本、`app/engine`(缠论实现, strat_kit 依赖它)、`research/`(灵感库+准则+你的研究数据)、`docs/`、`config.yaml`。

git 里**没有**(被 .gitignore 排除，需另外准备)：
- `.btcache/` —— K线数据 + 信号缓存，**2.7G**，见第三步
- `.env` / `data/*.db` —— 密钥和实盘数据库，本地回测用不到

## 二、装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # fastapi/uvicorn/aiohttp/pyyaml/python-dotenv
```

Python 3.12（和 Windows 端一致；3.10+ 应该都行）。

## 三、准备 K 线数据（.btcache）

两条路，选一条：

**A. 从 Windows 拷过来**（数据现成，最快）
```bash
# 在 Mac 上，从 Windows 机器同步(需两台在同一网/开 SSH)。2.7G。
rsync -av <windows>:/c/Users/ADMIN/trade/.btcache/ ./.btcache/
```

**B. 在 Mac 上重新刷**（干净，但要等，且只有近30天）
```bash
python3 scripts/bt_refresh.py --tfs 5m,15m,1h --days 30 --top 0    # 660币, 十几分钟
python3 scripts/bt_scan.py --days 30                                # 预生成信号(看图器"信号"页要)
```

> `.btcache` 里的 `sig_*.json`(策略信号)和 `live_signals.json`(线上同步)会随用随生成，不必手动搬。

## 四、启动

```bash
chmod +x scripts/run.sh
./scripts/run.sh                 # 本机: http://127.0.0.1:8530
./scripts/run.sh --lan           # 手机同 WiFi 也能开(要先设 BT_USER/BT_PASS)
```

看图器在 `/`，灵感库在 `/ideas`。

## 五、两个可选功能的额外配置

这两个不配也能回测，只是对应按钮用不了：

| 功能 | 依赖 | Mac 上怎么弄 |
|---|---|---|
| **AI 生成策略**(灵感库①②、二次生成) | 本机 `claude` CLI | 装 Claude Code，确保 `claude -p` 能跑 |
| **线上信号同步**(🔴线上 标签) | SSH 到 VPS | 把 Mac 的公钥加到 VPS `~/.ssh/authorized_keys` |

`claude_gen.py` 调 `claude` CLI、`live_sync.py` 用 `ssh` —— 这俩在 Mac 上是原生的，配好凭证即可。

## 迁移后跑一遍自检

```bash
python3 -c "import sys; sys.path.insert(0,'scripts'); import strat_kit as K; print('缠论:', len(K.bi_seq(__import__('json').load(open('.btcache/BTCUSDT_5m_30d.json')), upto=500)), '个分型')"
# 能打印分型数 = app/engine 缠论 + 数据都通了
```

## 关于"哪台机器是主"

- 代码：git 是唯一真相。哪台改了都**先推 GitHub**（部署铁律），另一台 `git pull`。
- 研究数据(research/)：也走 git，两台会冲突，所以**一次只在一台上做标注/迭代**，做完就推。
- K线缓存(.btcache)：各机器独立，各自 `bt_refresh` 即可，不必同步。
