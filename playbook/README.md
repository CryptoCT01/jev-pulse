# Jev Pulse / 策略说明

## 策略 / Strategy

Jev Pulse is a **paper-only** BTCUSDT perpetual Playbook. GetAgent Studio owns
the heartbeat (15m), Bitget **5m** klines, AgentHub-equivalent tape via
`getagent.data` (order book, long/short, liquidations, taker volume, large
trades), paper fills, and the publish link.

`bitget-agent-mcp` cannot run inside the sandbox (no npx/stdio). The Playbook
uses the same market fields through `getagent.data` with `exchange="bitget"`.
It does **not** call OpenRouter or TypeSafe Jev. Mac companion still runs Jev
every 5s offline.

## 开仓 / Entry

BUY when 5m/1h return, range, book imbalance, taker buy ratio, long/short,
liquidations, or a large print lean long (net lean ≥ 1). SELL on the opposite.
Size from `margin_budget` and `leverage`. Same-side adds are skipped.

## 平仓 / Exit

REDUCE when open uPnL ≤ −1.2%, or when the next lean is opposite. Paper only.

## 风险 / Risk

- Paper / Demo only
- 15m Studio cron is not the Mac 5s Jev loop
- Never put Playbook API keys or OpenRouter keys in uploaded package files

### Parameters

- `trading_symbols`: default BTCUSDT
- `leverage`: paper leverage cap (default 10)
- `margin_budget`: USDT budget string for sizing helpers
