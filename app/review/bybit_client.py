from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import aiohttp

from .models import ClosedPnl, Execution, Kline, Transaction, decimal


Requester = Callable[[str, dict[str, Any], bool], Awaitable[dict[str, Any]]]


class BybitError(RuntimeError):
    pass


class ReadOnlyKeyRequired(BybitError):
    pass


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def sign_v5(secret: str, timestamp: str, api_key: str, recv_window: str, query: str) -> str:
    payload = f"{timestamp}{api_key}{recv_window}{query}".encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def encode_query(params: dict[str, Any]) -> str:
    clean = {key: value for key, value in params.items() if value not in (None, "")}
    return urlencode(sorted(clean.items()))


class BybitReadClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = "https://api.bybit.com",
        recv_window: int = 5000,
        requester: Requester | None = None,
    ):
        self.api_key = api_key
        self._api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.recv_window = str(recv_window)
        self._requester = requester or self._request

    async def _request(self, path: str, params: dict[str, Any], private: bool) -> dict[str, Any]:
        query = encode_query(params)
        headers = {}
        if private:
            timestamp = str(int(time.time() * 1000))
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": self.recv_window,
                "X-BAPI-SIGN": sign_v5(
                    self._api_secret, timestamp, self.api_key, self.recv_window, query
                ),
            }
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                payload = await response.json()
        if int(payload.get("retCode", 0)) != 0:
            raise BybitError(f"{path}: {payload.get('retCode')} {payload.get('retMsg', '')}")
        return payload

    async def verify_read_only(self) -> dict[str, Any]:
        payload = await self._requester("/v5/user/query-api", {}, True)
        result = payload.get("result") or {}
        if int(result.get("readOnly", 0)) != 1:
            raise ReadOnlyKeyRequired("Bybit API key must be read-only")
        return {"id": str(result.get("id", "")), "read_only": True}

    async def _pages(self, path: str, params: dict[str, Any], id_key: str) -> list[dict]:
        cursor = ""
        rows: dict[str, dict] = {}
        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = await self._requester(path, page_params, True)
            result = payload.get("result") or {}
            for row in result.get("list") or []:
                rows.setdefault(str(row.get(id_key, "")), row)
            cursor = str(result.get("nextPageCursor") or "")
            if not cursor:
                break
        return list(rows.values())

    async def executions(self, category: str, start_ms: int, end_ms: int) -> list[Execution]:
        raw_by_id: dict[str, dict] = {}
        cursor_ms = start_ms
        max_window = 7 * 86_400_000
        while cursor_ms < end_ms:
            chunk_end = min(end_ms, cursor_ms + max_window)
            chunk = await self._pages(
                "/v5/execution/list",
                {"category": category, "startTime": cursor_ms,
                 "endTime": chunk_end - 1, "limit": 100},
                "execId",
            )
            for item in chunk:
                raw_by_id[str(item.get("execId", ""))] = item
            cursor_ms = chunk_end
        raw = list(raw_by_id.values())
        rows = [
            Execution(
                exec_id=str(item["execId"]),
                order_id=str(item.get("orderId", "")),
                category=category,
                symbol=str(item.get("symbol", "")),
                side=str(item.get("side", "")),
                qty=decimal(item.get("execQty")),
                price=decimal(item.get("execPrice")),
                value=decimal(item.get("execValue")),
                fee=decimal(item.get("execFee")),
                timestamp_ms=int(item.get("execTime", 0)),
                leaves_qty=decimal(item.get("leavesQty")),
                is_maker=_boolean(item.get("isMaker", False)),
                position_idx=int(item.get("positionIdx", 0) or 0),
            )
            for item in raw
        ]
        rows = [row for row in rows if start_ms <= row.timestamp_ms < end_ms]
        return sorted(rows, key=lambda row: (row.timestamp_ms, row.exec_id, row.order_id, row.leaves_qty))

    async def transactions(self, category: str, start_ms: int, end_ms: int) -> list[Transaction]:
        raw = await self._pages(
            "/v5/account/transaction-log",
            {"accountType": "UNIFIED", "category": category, "startTime": start_ms,
             "endTime": end_ms, "limit": 50},
            "id",
        )
        rows = [
            Transaction(
                tx_id=str(item["id"]), category=str(item.get("category") or category),
                symbol=str(item.get("symbol", "")), timestamp_ms=int(item.get("transactionTime", 0)),
                tx_type=str(item.get("type", "")), cash_flow=decimal(item.get("cashFlow")),
                funding=decimal(item.get("funding")), fee=decimal(item.get("fee")),
                change=decimal(item.get("change")), currency=str(item.get("currency", "")),
            ) for item in raw
        ]
        return sorted([row for row in rows if start_ms <= row.timestamp_ms < end_ms],
                      key=lambda row: (row.timestamp_ms, row.tx_id))

    async def closed_pnl(self, category: str, start_ms: int, end_ms: int) -> list[ClosedPnl]:
        raw = await self._pages(
            "/v5/position/closed-pnl",
            {"category": category, "startTime": start_ms, "endTime": end_ms, "limit": 100},
            "orderId",
        )
        rows = [
            ClosedPnl(
                order_id=str(item["orderId"]), category=category,
                symbol=str(item.get("symbol", "")), side=str(item.get("side", "")),
                qty=decimal(item.get("qty")), closed_size=decimal(item.get("closedSize")),
                avg_entry_price=decimal(item.get("avgEntryPrice")),
                avg_exit_price=decimal(item.get("avgExitPrice")),
                closed_pnl=decimal(item.get("closedPnl")), leverage=decimal(item.get("leverage")),
                open_fee=decimal(item.get("openFee")), close_fee=decimal(item.get("closeFee")),
                updated_ms=int(item.get("updatedTime", 0)),
            ) for item in raw
        ]
        return sorted([row for row in rows if start_ms <= row.updated_ms < end_ms],
                      key=lambda row: (row.updated_ms, row.order_id))

    async def positions(self, category: str) -> dict[tuple[str, str, int], Any]:
        result: dict[tuple[str, str, int], Any] = {}
        cursor = ""
        seen_cursors: set[str] = set()
        while True:
            params = {"category": category, "settleCoin": "USDT", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            payload = await self._requester("/v5/position/list", params, True)
            page = payload.get("result") or {}
            for item in (page.get("list") or []):
                size = decimal(item.get("size"))
                side = str(item.get("side", "")).lower()
                signed = -size if side == "sell" else size
                key = (category, str(item.get("symbol", "")),
                       int(item.get("positionIdx", 0) or 0))
                result[key] = signed
            cursor = str(page.get("nextPageCursor") or "")
            if not cursor:
                break
            if cursor in seen_cursors:
                raise BybitError("position pagination cursor repeated")
            seen_cursors.add(cursor)
        return result

    async def wallet_balance(self) -> dict[str, Any]:
        payload = await self._requester(
            "/v5/account/wallet-balance", {"accountType": "UNIFIED"}, True
        )
        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            raise BybitError("wallet balance returned no account")
        row = rows[0]
        return {
            "total_equity": decimal(row.get("totalEquity")),
            "wallet_balance": decimal(row.get("totalWalletBalance")),
            "unrealized_pnl": decimal(row.get("totalPerpUPL")),
        }

    async def klines(
        self, category: str, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> list[Kline]:
        interval_ms = {"1": 60_000, "3": 180_000, "5": 300_000, "15": 900_000,
                       "30": 1_800_000, "60": 3_600_000}.get(str(interval))
        if interval_ms is None:
            raise ValueError(f"unsupported paged kline interval: {interval}")
        cursor = start_ms // interval_ms * interval_ms
        aligned_end = ((end_ms + interval_ms - 1) // interval_ms) * interval_ms
        collected: dict[int, Kline] = {}
        while cursor < aligned_end:
            chunk_end = min(aligned_end, cursor + interval_ms * 1000)
            payload = await self._requester(
                "/v5/market/kline",
                {"category": category, "symbol": symbol, "interval": interval,
                 "start": cursor, "end": chunk_end - 1, "limit": 1000},
                False,
            )
            for item in ((payload.get("result") or {}).get("list") or []):
                row = Kline(int(item[0]), decimal(item[1]), decimal(item[2]), decimal(item[3]),
                            decimal(item[4]), decimal(item[5]), decimal(item[6]))
                collected[row.start_ms] = row
            cursor = chunk_end
        return sorted(
            (row for row in collected.values() if row.start_ms < end_ms),
            key=lambda row: row.start_ms,
        )
