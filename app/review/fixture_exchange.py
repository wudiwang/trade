"""Replay a recorded/invented set of Bybit V5 GET responses (the ``--fixture`` mode).

No network, no credentials.  ``FixtureExchange.requester`` has the same signature as the
transport of ``BybitReadClient`` and is injected through its ``requester=`` argument, so a
fixture run goes through exactly the same client, allow-list and pipeline as a live run.

Fixture file (JSON):
  "window":    {"day": "YYYY-MM-DD", "timezone": "Asia/Shanghai", ...}
  "now_ms":    the moment the "sync" happens (used instead of the wall clock)
  "responses": {"/v5/...": [envelope, envelope, ...]}   pages chained by result.nextPageCursor
               ("page-2", "page-3", ...), same layout as the order-layer fixtures.  A path that
               is not listed answers with an empty list.
  "market":    {"SYMBOL": {"tick": "0.1", "wiggle": 0.0005, "path": [[time_ms, price], ...]}}
               Candles are synthesised from this price path instead of being stored:
               GET /v5/market/kline is answered in the documented shape
               (https://bybit-exchange.github.io/docs/v5/market/kline): each row is
               [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover] as
               strings, "Sort in reverse by startTime", at most ``limit`` (<= 1000) rows.

All fixture data is invented; nothing here was captured from a real account.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_right
from decimal import Decimal
from pathlib import Path
from typing import Any

MINUTE_MS = 60_000
# [ENUM interval] minutes-based intervals of GET /v5/market/kline.
_INTERVAL_MINUTES = {"1": 1, "3": 3, "5": 5, "15": 15, "30": 30, "60": 60, "120": 120,
                     "240": 240, "360": 360, "720": 720}
_READ_ONLY_KEY = {"retCode": 0, "retMsg": "", "result": {"id": "fixture", "readOnly": 1},
                  "retExtInfo": {}, "time": 0}


class FixtureError(ValueError):
    pass


def _envelope(rows: list, cursor: str = "") -> dict[str, Any]:
    return {"retCode": 0, "retMsg": "OK",
            "result": {"list": rows, "nextPageCursor": cursor, "category": "linear"},
            "retExtInfo": {}, "time": 0}


class PricePath:
    """Deterministic 1-minute candles along straight segments between waypoints."""

    def __init__(self, spec: dict[str, Any]):
        points = sorted((int(stamp), float(price)) for stamp, price in spec.get("path") or [])
        if not points:
            raise FixtureError("market path is empty")
        self._times = [stamp for stamp, _ in points]
        self._prices = [price for _, price in points]
        self._wiggle = float(spec.get("wiggle", 0.0005))
        self._tick = Decimal(str(spec.get("tick", "0.01")))

    def _base(self, stamp: int) -> float:
        index = bisect_right(self._times, stamp)
        if index <= 0:
            return self._prices[0]
        if index >= len(self._times):
            return self._prices[-1]
        t0, t1 = self._times[index - 1], self._times[index]
        p0, p1 = self._prices[index - 1], self._prices[index]
        return p0 + (p1 - p0) * (stamp - t0) / (t1 - t0)

    def _price(self, stamp: int) -> float:
        minute = stamp // MINUTE_MS
        wave = 0.6 * math.sin(minute * 1.7) + 0.4 * math.sin(minute * 0.37)
        return self._base(stamp) * (1 + self._wiggle * wave)

    def _minute(self, start: int) -> tuple[float, float, float, float, float]:
        minute = start // MINUTE_MS
        open_, close = self._price(start), self._price(start + MINUTE_MS)
        wick = self._base(start) * self._wiggle * 0.25 * (1 + math.sin(minute * 2.3))
        volume = 120 + 60 * math.sin(minute * 0.9) + 2000 * abs(close - open_) / open_
        return open_, max(open_, close) + wick, min(open_, close) - wick, close, volume

    def _text(self, value: float) -> str:
        return format(Decimal(repr(value)).quantize(self._tick), "f")

    def candle(self, start: int, minutes: int) -> list[str]:
        rows = [self._minute(start + index * MINUTE_MS) for index in range(minutes)]
        volume = sum(row[4] for row in rows)
        close = rows[-1][3]
        return [str(start), self._text(rows[0][0]), self._text(max(row[1] for row in rows)),
                self._text(min(row[2] for row in rows)), self._text(close),
                format(volume, ".3f"), format(volume * close, ".2f")]


class FixtureExchange:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.responses: dict[str, list] = dict(payload.get("responses") or {})
        self.now_ms = int(payload.get("now_ms") or 0)
        self.window = dict(payload.get("window") or {})
        self._paths = {symbol: PricePath(spec)
                       for symbol, spec in (payload.get("market") or {}).items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @classmethod
    def load(cls, path: str | Path) -> "FixtureExchange":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FixtureError(f"fixture 读不出来：{exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("responses"), dict):
            raise FixtureError("fixture 格式不对：需要 responses（各接口的应答）")
        return cls(payload)

    async def requester(self, path: str, params: dict[str, Any], private: bool) -> dict[str, Any]:
        self.calls.append((path, dict(params)))
        if path == "/v5/user/query-api":
            return self.responses.get(path) or _READ_ONLY_KEY
        if path == "/v5/market/kline":
            return self._kline(params)
        pages = self.responses.get(path)
        if not pages:
            return _envelope([])
        if isinstance(pages, dict):
            return pages
        cursor = str(params.get("cursor") or "")
        if not cursor:
            return pages[0]
        if cursor.startswith("page-") and cursor[5:].isdigit() and 2 <= int(cursor[5:]) <= len(pages):
            return pages[int(cursor[5:]) - 1]
        raise FixtureError(f"{path}: unknown cursor {cursor!r}")

    def _kline(self, params: dict[str, Any]) -> dict[str, Any]:
        symbol = str(params.get("symbol") or "")
        minutes = _INTERVAL_MINUTES.get(str(params.get("interval")))
        result: dict[str, Any] = {"symbol": symbol, "category": params.get("category", "linear"),
                                  "list": []}
        path = self._paths.get(symbol)
        if path is not None and minutes is not None:
            step = minutes * MINUTE_MS
            limit = min(1000, max(1, int(params.get("limit") or 200)))
            newest = self.now_ms or int(params.get("end") or 0)
            end = min(int(params.get("end") or newest), newest)
            start = int(params.get("start") or (end - limit * step))
            first = -(-start // step) * step                 # first candle starting at/after start
            last = end // step * step
            stamps = list(range(first, last + 1, step))[-limit:]
            result["list"] = [path.candle(stamp, minutes) for stamp in reversed(stamps)]
        return {"retCode": 0, "retMsg": "OK", "result": result, "retExtInfo": {}, "time": 0}
