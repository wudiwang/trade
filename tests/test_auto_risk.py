import asyncio

from app.engine.binance_rest import BinanceRest
from app.engine.core import Engine


class FakeConfig:
    mode = "live"

    def __init__(self, **values):
        self.values = {
            "live.auto_trade": True,
            "risk.account_equity": 1479.41,
            "live.max_loss_pct": 2.0,
            "live.max_positions": 10,
            **values,
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeDB:
    def __init__(self, baseline_at=1787596582, baseline_present=True):
        self.baseline_at = baseline_at
        self.baseline_present = baseline_present
        self.events = []
        self.signal_updates = []

    def one(self, sql, params=()):
        if "FROM settings" in sql and params == ("risk.account_equity",):
            if not self.baseline_present:
                return None
            return {"updated_at": self.baseline_at}
        return None

    def log(self, level, source, message):
        self.events.append((level, source, message))

    def update_signal(self, sid, **fields):
        self.signal_updates.append((sid, fields))


class FakeRest:
    def __init__(self, wallet, transfers, income_error=None, position_error=None,
                 wallet_error=None):
        self.wallet = wallet
        self.transfers = transfers
        self.income_error = income_error
        self.position_error = position_error
        self.wallet_error = wallet_error
        self.open_count = 0
        self.income_calls = []

    async def account_info(self):
        if self.wallet_error:
            raise self.wallet_error
        return {"totalWalletBalance": str(self.wallet)}

    async def income(self, start_ms=None, limit=1000, end_ms=None, page=None, income_type=None):
        self.income_calls.append({
            "start_ms": start_ms, "limit": limit, "end_ms": end_ms,
            "page": page, "income_type": income_type,
        })
        if self.income_error:
            raise self.income_error
        return self.transfers if page in (None, 1) else []

    async def position_risk(self):
        if self.position_error:
            raise self.position_error
        await asyncio.sleep(0)
        return ([{"positionAmt": "1"}] * self.open_count)


class FakeTrader:
    def __init__(self):
        self.calls = []

    async def execute_signal(self, sid, row):
        self.calls.append((sid, row))
        return {"ok": True, "message": "ok"}


class FakeSignal:
    symbol = "BTCUSDT"
    direction = "short"

    def to_db(self):
        return {"symbol": self.symbol, "direction": self.direction}


def make_engine(wallet, transfers):
    engine = object.__new__(Engine)
    engine.cfg = FakeConfig()
    engine.db = FakeDB()
    engine.rest = FakeRest(wallet, transfers)
    engine.trader = FakeTrader()
    engine.notice_subscribers = []
    engine._auto_halt = False
    engine._bal_cache = (0.0, 0.0)
    engine._risk_equity_cache = (None, 0.0)
    engine._auto_order_lock = asyncio.Lock()
    engine._position_reservations = {}
    return engine


def test_transfer_out_does_not_trigger_drawdown_breaker():
    engine = make_engine(1108.81, [{
        "asset": "USDT", "incomeType": "TRANSFER", "income": "-400.0",
        "time": 1787600000000, "tranId": 1,
    }])

    placed = asyncio.run(engine._auto_execute(99, FakeSignal()))

    assert placed is True
    assert len(engine.trader.calls) == 1
    assert engine._auto_halt is False
    assert engine.rest.income_calls == [{
        "start_ms": 1787596582000, "limit": 1000,
        "end_ms": engine.rest.income_calls[0]["end_ms"],
        "page": 1, "income_type": "TRANSFER",
    }]
    assert isinstance(engine.rest.income_calls[0]["end_ms"], int)


def test_real_trading_loss_still_triggers_drawdown_breaker():
    engine = make_engine(1400.0, [])

    placed = asyncio.run(engine._auto_execute(100, FakeSignal()))

    assert placed is False
    assert engine.trader.calls == []
    assert engine._auto_halt is True
    assert any("熔断" in message for _, _, message in engine.db.events)


def test_missing_equity_baseline_timestamp_fails_closed():
    engine = make_engine(1600.0, [])
    engine.db = FakeDB(baseline_present=False)

    placed = asyncio.run(engine._auto_execute(101, FakeSignal()))

    assert placed is False
    assert engine.trader.calls == []
    assert engine._auto_halt is True


def test_equity_baseline_older_than_income_retention_fails_closed():
    engine = make_engine(1600.0, [])
    engine.db = FakeDB(baseline_at=1)

    placed = asyncio.run(engine._auto_execute(102, FakeSignal()))

    assert placed is False
    assert engine.trader.calls == []
    assert engine.rest.income_calls == []


def test_equity_baseline_in_the_future_fails_closed():
    engine = make_engine(1600.0, [])
    engine.db = FakeDB(baseline_at=9999999999)

    placed = asyncio.run(engine._auto_execute(106, FakeSignal()))

    assert placed is False
    assert engine.trader.calls == []
    assert engine.rest.income_calls == []


def test_position_query_failure_fails_closed():
    engine = make_engine(1600.0, [])
    engine.rest.position_error = RuntimeError("position unavailable")

    placed = asyncio.run(engine._auto_execute(103, FakeSignal()))

    assert placed is False
    assert engine.trader.calls == []
    assert any("持仓" in message for _, _, message in engine.db.events)


def test_wallet_query_failure_fails_closed():
    engine = make_engine(1600.0, [])
    engine.rest.wallet_error = RuntimeError("wallet unavailable")

    placed = asyncio.run(engine._auto_execute(107, FakeSignal()))

    assert placed is False
    assert engine.trader.calls == []
    assert engine._auto_halt is True


def test_transfer_query_failure_fails_closed():
    engine = make_engine(1600.0, [])
    engine.rest.income_error = RuntimeError("income unavailable")

    placed = asyncio.run(engine._auto_execute(108, FakeSignal()))

    assert placed is False
    assert engine.trader.calls == []
    assert engine._auto_halt is True


def test_exactly_one_full_transfer_page_fetches_next_page():
    transfers = [{
        "asset": "USDT", "incomeType": "TRANSFER", "income": "-0.001",
        "time": 1787600000000, "tranId": i,
    } for i in range(1000)]
    engine = make_engine(1600.0, transfers)

    placed = asyncio.run(engine._auto_execute(109, FakeSignal()))

    assert placed is True
    assert [call["page"] for call in engine.rest.income_calls] == [1, 2]


class DelayedVisibilityTrader(FakeTrader):
    async def execute_signal(self, sid, row):
        await asyncio.sleep(0)
        return await super().execute_signal(sid, row)


def test_concurrent_signals_cannot_exceed_position_limit():
    engine = make_engine(1600.0, [])
    engine.cfg.values["live.max_positions"] = 1
    engine.trader = DelayedVisibilityTrader()

    async def execute_both():
        return await asyncio.gather(
            engine._auto_execute(104, FakeSignal()),
            engine._auto_execute(105, FakeSignal()),
        )

    results = asyncio.run(execute_both())

    assert results.count(True) == 1
    assert len(engine.trader.calls) == 1


def test_disabling_auto_trade_while_waiting_for_lock_prevents_order():
    engine = make_engine(1600.0, [])

    async def queue_then_disable():
        await engine._auto_order_lock.acquire()
        task = asyncio.create_task(engine._auto_execute(110, FakeSignal()))
        await asyncio.sleep(0)
        engine.cfg.values["live.auto_trade"] = False
        engine._auto_order_lock.release()
        return await task

    placed = asyncio.run(queue_then_disable())

    assert placed is False
    assert engine.trader.calls == []


def test_short_only_direction_is_rechecked_inside_execution_lock():
    engine = make_engine(1600.0, [])
    engine.cfg.values["trade_direction"] = "short"
    signal = FakeSignal()
    signal.direction = "long"

    placed = asyncio.run(engine._auto_execute(111, signal))

    assert placed is False
    assert engine.trader.calls == []


class RecordingBinanceRest(BinanceRest):
    def __init__(self):
        super().__init__("https://example.invalid", "key", "secret")
        self.signed_call = None

    async def _signed(self, method, path, params):
        self.signed_call = (method, path, params)
        return []


def test_income_forwards_transfer_filter_and_pagination():
    rest = RecordingBinanceRest()

    result = asyncio.run(rest.income(
        start_ms=1000, end_ms=2000, page=3, limit=250, income_type="TRANSFER",
    ))

    assert result == []
    assert rest.signed_call == ("GET", "/fapi/v1/income", {
        "limit": 250, "startTime": 1000, "endTime": 2000,
        "page": 3, "incomeType": "TRANSFER",
    })
