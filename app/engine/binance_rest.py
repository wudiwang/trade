"""币安 USDT-M 合约 REST 客户端（行情部分无需密钥；交易部分用 HMAC 签名）。"""
import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

log = logging.getLogger("binance.rest")


class BinanceRest:
    def __init__(self, base: str, api_key: str = "", api_secret: str = ""):
        self.base = base.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"X-MBX-APIKEY": self.api_key} if self.api_key else {}
            self._session = aiohttp.ClientSession(
                headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict | None = None, retries: int = 3) -> Any:
        s = await self.session()
        url = f"{self.base}{path}"
        for attempt in range(retries):
            try:
                async with s.get(url, params=params or {}) as resp:
                    if resp.status == 429 or resp.status == 418:
                        wait = int(resp.headers.get("Retry-After", "10"))
                        log.warning("rate limited (%s), sleeping %ss", resp.status, wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"GET {path} failed after {retries} retries")

    # ---------- 公开行情 ----------
    async def usdt_perp_symbols(self) -> list[dict]:
        """全部 USDT 本位永续合约（TRADING 状态），含精度信息。"""
        info = await self._get("/fapi/v1/exchangeInfo")
        out = []
        for s in info["symbols"]:
            if (
                s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
            ):
                tick = step = None
                for f in s.get("filters", []):
                    if f["filterType"] == "PRICE_FILTER":
                        tick = float(f["tickSize"])
                    elif f["filterType"] == "LOT_SIZE":
                        step = float(f["stepSize"])
                out.append({
                    "symbol": s["symbol"],
                    "status": s["status"],
                    "price_precision": s["pricePrecision"],
                    "qty_precision": s["quantityPrecision"],
                    "tick_size": tick,
                    "step_size": step,
                })
        return out

    async def ticker_24h(self) -> dict[str, float]:
        """symbol -> 24h 成交额(USDT)。"""
        data = await self._get("/fapi/v1/ticker/24hr")
        return {d["symbol"]: float(d["quoteVolume"]) for d in data}

    async def klines(self, symbol: str, interval: str, limit: int = 500,
                     start_time: int | None = None) -> list[tuple]:
        """返回 (open_time, open, high, low, close, volume, quote_volume, taker_buy, closed) 列表。
        最后一根若未收盘, closed=0。"""
        params: dict = {"symbol": symbol, "interval": interval, "limit": min(limit, 1500)}
        if start_time:
            params["startTime"] = start_time
        raw = await self._get("/fapi/v1/klines", params)
        now_ms = int(time.time() * 1000)
        return [
            (int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]),
             float(k[5]), float(k[7]), float(k[9]), 1 if int(k[6]) < now_ms else 0)
            for k in raw
        ]

    async def open_interest_hist(self, symbol: str, period: str = "15m",
                                 limit: int = 12) -> list[float]:
        """持仓量历史(免费)。返回 sumOpenInterest 序列(升序)。"""
        try:
            data = await self._get("/futures/data/openInterestHist",
                                    {"symbol": symbol, "period": period, "limit": min(limit, 500)})
            return [float(d["sumOpenInterest"]) for d in data]
        except Exception:
            return []

    async def funding_rates(self) -> dict[str, float]:
        """symbol -> 最新资金费率（小数，如 -0.0003 = -0.03%）。"""
        data = await self._get("/fapi/v1/premiumIndex")
        out = {}
        for d in data:
            try:
                out[d["symbol"]] = float(d.get("lastFundingRate") or 0)
            except (ValueError, TypeError):
                pass
        return out

    # ---------- 签名交易（live 模式用） ----------
    def _sign(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        qs = urlencode(params)
        params["signature"] = hmac.new(
            self.api_secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()
        return params

    async def _signed(self, method: str, path: str, params: dict) -> Any:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("未配置币安 API 密钥，无法执行交易操作（当前应使用 paper 模式）")
        s = await self.session()
        url = f"{self.base}{path}"
        async with s.request(method, url, params=self._sign(dict(params))) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(f"binance {path} {resp.status}: {data}")
            return data

    async def place_order(self, **params: Any) -> Any:
        return await self._signed("POST", "/fapi/v1/order", params)

    async def place_algo_order(self, **params: Any) -> Any:
        return await self._signed("POST", "/fapi/v1/algoOrder", params)

    async def cancel_order(self, **params: Any) -> Any:
        return await self._signed("DELETE", "/fapi/v1/order", params)

    async def account_info(self) -> Any:
        return await self._signed("GET", "/fapi/v2/account", {})

    async def position_risk(self) -> Any:
        return await self._signed("GET", "/fapi/v2/positionRisk", {})

    async def income(self, start_ms: int | None = None, limit: int = 1000) -> Any:
        p: dict = {"limit": limit}
        if start_ms:
            p["startTime"] = start_ms
        return await self._signed("GET", "/fapi/v1/income", p)

    async def set_leverage(self, symbol: str, leverage: int) -> Any:
        return await self._signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
