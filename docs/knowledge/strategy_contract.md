# Strategy Contract

Every strategy must expose clear human semantics and machine-readable outputs.

## Strategy Metadata

Each strategy must define:

- `id`: stable id such as `macro_pullback`.
- `name`: human-readable Chinese name.
- `status`: idea, backtest, review, paper, live, paused, rejected.
- `directions`: long, short, or both.
- `timeframes`: supported timeframes.
- `role`: filter, setup, trigger, risk, standalone.
- `owner_agent`: responsible agent name.
- `updated_at`: latest logic update date.
- `version`: strategy semantic version.

## Signal Output

Each signal must include:

- strategy id and version,
- symbol,
- timeframe,
- direction,
- created time,
- entry,
- stop loss,
- take profit,
- RR,
- trigger reason,
- structured markers,
- raw strategy details for debugging,
- settlement result when backtested.

## Marker Schema

Markers should be explicit and stable:

```json
{
  "key": "L2",
  "label": "L2底分型",
  "time": 1781702400000,
  "price": 0.36279,
  "position": "belowBar",
  "source": "chan_merged_fractal"
}
```

Common marker keys:

- `volume_sweep`
- `L1`
- `H1`
- `L2`
- `H2`
- `stall_k`
- `entry_k`
- `tp`
- `sl`
- `liquidity_void`
- `invalidated`

## Documentation Required Per Strategy

Each important strategy should have:

```text
docs/strategies/<strategy_id>/origin.md
docs/strategies/<strategy_id>/logic.md
docs/strategies/<strategy_id>/changelog.md
docs/strategies/<strategy_id>/scorecards/
docs/strategies/<strategy_id>/cases.md
```

If those paths do not exist yet, create them when the strategy becomes active enough to track.

