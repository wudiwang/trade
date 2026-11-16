"""订单流页本地预览服务：真实的 FastAPI 应用 + 真实的 /api/flow/* 路由与后台任务，拉真实行情。

    PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/flow_dev_server.py            # http://127.0.0.1:8532/flow.html
    PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/flow_dev_server.py --port 8600 --temp

它【不会】导入或启动交易引擎、Telegram Bot、实盘下单器：
  - create_app(cfg, db, engine=None, bot=None)：控制台首页能打开，但没有引擎数据；
  - 主库(users/signals…)用一次性的临时 sqlite，绝不碰 data/trade.db；
  - 只构造一个不带密钥的 BinanceRest 给状态卡取公开行情（K 线 / OI / 资金费）。
深度快照默认写 data/flow_dev.db（重启后历史还在，热力图能越攒越长）；--temp 则用完即删。

本机 DNS 解析不了 Bybit / OKX 时（有些网络如此），自动退到 DoH(1.1.1.1) 解析；线上不走这条路。
--probe 额外起一个事件循环探针，每 30 秒打印一次「单次循环迭代的最长耗时」
（= 事件循环被同线程回调、或被抢 GIL 的线程卡住的最长时间；探针会空转吃满一个核，仅供测量）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402
import uvicorn  # noqa: E402
from aiohttp.abc import AbstractResolver  # noqa: E402
from aiohttp.resolver import ThreadedResolver  # noqa: E402

from app.config import Config  # noqa: E402
from app.db import DB  # noqa: E402
from app.engine.binance_rest import BinanceRest  # noqa: E402  只用公开行情方法
from app.flow import api as flow_api  # noqa: E402
from app.web.server import create_app  # noqa: E402

log = logging.getLogger("flow.dev")
DOH_URL = "https://1.1.1.1/dns-query"


class FallbackResolver(AbstractResolver):
    """先用系统 DNS；失败再用 DoH(1.1.1.1) 查 A 记录。只给本地预览用。"""

    def __init__(self):
        self._sys = ThreadedResolver()
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._session: aiohttp.ClientSession | None = None
        self.used_doh: set[str] = set()

    async def _doh(self, host: str) -> list[str]:
        hit = self._cache.get(host)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()          # 目标是 IP 字面量，不需要再解析
        async with self._session.get(DOH_URL, params={"name": host, "type": "A"},
                                     headers={"accept": "application/dns-json"},
                                     timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json(content_type=None)
        answers = [a for a in data.get("Answer") or [] if a.get("type") == 1 and a.get("data")]
        ips = [str(a["data"]) for a in answers]
        if not ips:
            raise OSError(f"DoH 也解析不到 {host}")
        ttl = max(60, min(600, min(int(a.get("TTL") or 60) for a in answers)))
        self._cache[host] = (time.monotonic() + ttl, ips)
        if host not in self.used_doh:
            self.used_doh.add(host)
            log.info("系统 DNS 解析不了 %s，改用 DoH → %s", host, ips[0])
        return ips

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        try:
            return await self._sys.resolve(host, port, family)
        except OSError:
            ips = await self._doh(host)
        return [{"hostname": host, "host": ip, "port": port, "family": socket.AF_INET,
                 "proto": 0, "flags": socket.AI_NUMERICHOST} for ip in ips]

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


async def loop_probe(report_every: float = 30.0) -> None:
    """事件循环探针：不停 sleep(0)，量每一圈的墙钟时间。最大值 = 这段时间里循环被卡住的最长一次。"""
    pc = time.perf_counter
    worst, n, t_rep = 0.0, 0, pc()
    over = [0, 0, 0]                                  # >5ms / >20ms / >50ms 的圈数
    last = pc()
    while True:
        await asyncio.sleep(0)
        now = pc()
        dt = now - last
        last = now
        n += 1
        if dt > worst:
            worst = dt
        if dt > 0.005:
            over[0] += 1
            if dt > 0.020:
                over[1] += 1
                if dt > 0.050:
                    over[2] += 1
        if now - t_rep >= report_every:
            print(f"[probe] {now - t_rep:.0f}s 内循环 {n} 圈, 最长一圈 {worst * 1000:.1f}ms, "
                  f">5ms {over[0]} 次, >20ms {over[1]} 次, >50ms {over[2]} 次", flush=True)
            worst, n, t_rep, over = 0.0, 0, now, [0, 0, 0]


async def status_printer(rt: flow_api.FlowRuntime, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        h = rt.health(full=True)
        now = h["ts"]
        rec = h["detail"]["recorder"] or {}
        ages = {k: (f"{(now - v) / 1000:.0f}s" if v else "-") for k, v in h["caches"].items()}
        errs = {k: v for k, v in (rec.get("last_error") or {}).items() if v}
        loops = {k: v["last_error"] for k, v in h["detail"]["refreshers"].items() if v["last_error"]}
        print(f"[flow] 快照 {rec.get('snapshots')} 张 | 上次处理 CPU {rec.get('last_cpu_ms')}ms "
              f"(解析 {rec.get('last_compute_ms')}ms + 写库 {rec.get('last_write_ms')}ms) "
              f"| 权重 {rec.get('weight')} | 缓存年龄 {ages} | 深度缓存 {h['detail']['depth_cache']}"
              + (f" | 源错误 {errs}" if errs else "") + (f" | 刷新错误 {loops}" if loops else ""),
              flush=True)


async def amain(args) -> None:
    cfg = Config()
    tmp = Path(tempfile.mkdtemp(prefix="flow_dev_"))
    depth_db = (tmp / "flow_depth.sqlite") if args.temp else Path(args.db)
    if not depth_db.is_absolute():
        depth_db = ROOT / depth_db
    cfg.set_override("flow.enabled", not args.disabled)
    cfg.set_override("flow.db_path", str(depth_db))
    if args.interval:
        cfg.set_override("flow.interval_s", args.interval)

    db = DB(tmp / "console.db")                           # 一次性的主库：不碰 data/trade.db
    app = create_app(cfg, db, engine=None, bot=None)      # 路由里已含 /api/flow/*（register_flow）
    rt = flow_api.get_runtime()
    if rt is None or rt.enabled == args.disabled:
        raise SystemExit("订单流路由没有注册成功（看上面的日志）")

    rest = BinanceRest(cfg.get("binance.rest_base", "https://fapi.binance.com"))   # 不带密钥
    resolvers: list[FallbackResolver] = []

    def session_factory() -> aiohttp.ClientSession:
        r = FallbackResolver()
        resolvers.append(r)
        return aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=300, resolver=r))

    tasks = flow_api.start_flow_tasks(cfg, SimpleNamespace(rest=rest), session_factory=session_factory)
    extra = [] if args.disabled else [asyncio.create_task(status_printer(rt, args.status_every))]
    if args.probe:
        extra.append(asyncio.create_task(loop_probe()))

    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    print(f"订单流预览: http://{args.host}:{args.port}/flow.html   (后台任务 {len(tasks)} 个, 深度库 {depth_db})",
          flush=True)
    try:
        await server.serve()
    finally:
        for t in extra:
            t.cancel()
        await flow_api.stop_flow_tasks()
        await rest.close()
        for r in resolvers:
            await r.close()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="订单流页本地预览（真实行情，不启动交易引擎）")
    ap.add_argument("--port", type=int, default=8532)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default="data/flow_dev.db", help="深度快照库（默认 data/flow_dev.db）")
    ap.add_argument("--temp", action="store_true", help="深度快照库放临时目录，退出即删")
    ap.add_argument("--interval", type=float, default=0, help="盘口快照间隔秒数（默认取 config.yaml）")
    ap.add_argument("--status-every", type=float, default=30.0, help="每隔多少秒打印一行状态")
    ap.add_argument("--probe", action="store_true", help="打印事件循环最长卡顿（探针会吃满一个核）")
    ap.add_argument("--disabled", action="store_true", help="按 flow.enabled=false 启动（看页面在关闭状态下的样子）")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
