# Watch Agent Design

## Purpose

The Watch Agent replaces manual staring at 1m charts for selected symbols. It follows a predefined playbook, summarizes market state, alerts when a symbolic K line or entry condition appears, and abandons the watch when the setup is invalidated.

## Candidate Sources

- extreme funding rate,
- OI expansion,
- strong gainers or losers,
- user manually added symbol,
- strategy near-trigger state,
- macro theme watchlist.

## State Machine

```text
candidate
→ watching
→ setup_forming
→ trigger_near
→ actionable
→ entered
→ invalidated
→ expired
```

## Watch Playbook Fields

- symbol,
- direction,
- source reason,
- market regime,
- key support/resistance,
- liquidity void zones,
- expected entry zone,
- symbolic K definition,
- invalidation level,
- update frequency,
- latest summary,
- next expected event,
- allowed actions.

## Safety Boundary

The Watch Agent may alert and summarize. It must not directly place live orders. Execution requires strategy trigger, risk validation, and user-approved automation mode.

## First Version

Start with manual or system-added watch symbols and 1m/5m summaries every 1-3 minutes. Keep summaries short and persistent in local storage.

