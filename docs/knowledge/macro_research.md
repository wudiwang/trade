# Macro Research and BTC Regime

## Purpose

Provide the overall market backdrop for strategy decisions.

## BTC Regime

Classify BTC as:

- uptrend,
- downtrend,
- range / center consolidation,
- high-risk transition.

Track:

- key levels,
- liquidity void zones,
- support and resistance,
- invalidation conditions,
- impact on altcoin risk.

## YouTube Analyst Ingestion

Target analyst: 提阿菲罗 / 提阿非罗.

Pipeline:

```text
find latest video
→ fetch metadata and transcript/captions if available
→ summarize BTC view
→ extract key levels and risks
→ store daily macro note
```

If YouTube access, captions, or API keys are unavailable, create the scaffold and mark ingestion blocked. Do not block other work.

## Macro Factors

Track later:

- broad market liquidity,
- rates and yields,
- central bank liquidity / easing,
- dollar strength,
- risk asset sentiment,
- crypto-specific funding and leverage.

## Output

Daily macro notes should go under:

```text
docs/journal/daily/YYYY-MM-DD.md
```

Structured generated summaries may go under:

```text
artifacts/macro/YYYY-MM-DD.json
```

