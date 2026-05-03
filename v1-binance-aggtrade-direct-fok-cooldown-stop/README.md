# v1 — Binance aggTrade 直接 FOK + 2秒冷却止损

## 策略摘要

```text
Binance aggTrade 穿越 open
→ 立即发 Polymarket FOK/FAK BUY 5 shares @ 0.65
→ 成交后进入 2s cooldown
→ cooldown 期间不因为来回穿越反复开仓/反手
→ 2s 后重新评估是否触发止损/退出
```

这个版本用于测试：Binance 的成交级别价格变化，是否会比 Polymarket CLOB 重新定价更早几毫秒到几秒。

v1 的执行思想更接近真实抢单，而不是保守的 paper-sweep：

- 入场前不等待 REST 获取 orderbook；
- 使用 Binance `@aggTrade` 作为快速触发源；
- 发送带价格保护的 Polymarket 即时订单；
- 让 Polymarket 撮合系统自己决定成交或取消；
- 同时保留完整日志用于复盘。

## 入场触发

每个币、每个 15 分钟 round：

1. 记录本轮 `open` 价格。
2. 订阅 Binance `symbol@aggTrade`。
3. 检测价格是否有效穿越 open：

```text
之前在 open 下方
当前 aggTrade 价格到 open 上方
=> UP 信号

之前在 open 上方
当前 aggTrade 价格到 open 下方
=> DOWN 信号
```

第一版建议：

```text
entry_source = Binance aggTrade
entry_window = final 10s，之后再对比 final 5s / 3s / 2s
max_one_position_per_coin_round = true
```

## 入场订单

信号出现后：

```text
BUY 目标 outcome
订单类型：优先 FOK，FAK 作为可选实验
数量：5 shares
限价 / 最大平均价格：0.65
最大理论成本：5 * 0.65 = 3.25 USDC
```

预期行为：

- 如果 `<= 0.65` 有足够 ask，订单成交；
- 如果流动性不足，FOK 立即取消或失败；
- 如果使用 FAK，可能部分成交，剩余取消；
- 订单不能在价格 cap 以上成交。

## 为什么跳过下单前查询订单薄

这个策略假设机会非常短：

```text
Binance aggTrade 穿越 open
→ Polymarket 订单薄可能还没完全重定价
→ 直接发受保护订单可能捕获这段价差
```

如果在热路径里先 REST 请求 `/book`，或者先做本地复杂模拟，会增加延迟。因此 v1 应该测试直接发受保护订单。

但诊断用的 Polymarket WSS orderbook 仍然要并行记录，用于解释为什么成交或失败。

## 冷却规则

确认成交后：

```text
cooldown_until = fill_response_time + 2 seconds
```

冷却期间：

- 不新开仓；
- 不反手；
- 不止损；
- 只记录价格和订单薄变化。

原因：Binance aggTrade 可能在 open 附近快速来回穿越。冷却可以防止反复打单和被噪音洗出去。

## 止损 / 退出检查

冷却结束后：

### 简单第一版

```text
如果持仓 = UP 且 Binance 最新 aggTrade 价格 < open：
    尝试止损卖出

如果持仓 = DOWN 且 Binance 最新 aggTrade 价格 > open：
    尝试止损卖出
```

### 更保守版本

要求 Binance 和 OKX 最新价格同向确认：

```text
UP 仓止损：
    Binance latest < open 且 OKX latest < open

DOWN 仓止损：
    Binance latest > open 且 OKX latest > open
```

## 退出订单

初始清晰版本：

```text
SELL 已持有 shares
订单类型：FOK 或 FAK
数量：真实成交 shares
```

备注：

- FOK 记账简单：要么全部平仓，要么继续持有；
- FAK 可以降低风险，但必须实现部分仓位记账。

## 状态机

```text
FLAT
  ↓ Binance aggTrade 穿越 open
ORDER_SENT
  ↓ 成交
POSITION_OPEN + cooldown_until = now + 2s
  ↓ 冷却结束
WATCH_STOP
  ↓ 反向条件触发
EXIT_SENT
  ↓ 平仓成功或失败
CLOSED / HOLD_TO_SETTLEMENT
```

如果入场订单没有成交：

```text
ORDER_SENT
  ↓ cancelled / rejected / no fill
FLAT
```

建议失败尝试冷却：

```text
每个 coin / round / direction 冷却 100ms - 300ms
```

用于避免每个 aggTrade tick 都疯狂发送 FOK。

## 必须记录的日志

每次信号和下单尝试都应该记录：

```json
{
  "event": "direct_order_attempt",
  "coin": "btc",
  "round_start": 0,
  "source": "binance_aggTrade",
  "direction": "UP",
  "order_type": "FOK",
  "shares": 5,
  "limit_price": 0.65,
  "signal_exchange_ts": 0.0,
  "signal_receive_ts": 0.0,
  "submit_ts": 0.0,
  "response_ts": 0.0,
  "exchange_latency_ms": 0,
  "submit_latency_ms": 0,
  "roundtrip_ms": 0,
  "result": "filled|cancelled|rejected|error",
  "avg_fill_price": null,
  "filled_shares": 0,
  "cooldown_until": null
}
```

并行诊断日志还要记录：

- Binance aggTrade 价格和时间戳；
- OKX 最新价格和时间戳；
- Polymarket UP/DOWN best bid/ask；
- Polymarket book 更新时间和 age；
- fill / reject 原因；
- 止损检查结果；
- 最终结算结果。

## v1 要回答的问题

1. Binance `aggTrade` 穿越 open 后，是否足够早，能抢在 Polymarket 重定价前？
2. `BUY 5 @ 0.65` 的真实成交率是多少？
3. 失败原因是价格已移动、深度不足、API 延迟、签名延迟，还是 rate limit？
4. 2 秒冷却是否减少过度交易，同时不明显错过必要退出？
5. Binance-only 是否比 Binance+OKX 确认更快但噪音更大？

## 初始参数

```text
entry_source: Binance aggTrade
entry_order_type: FOK
entry_size: 5 shares
entry_limit_price: 0.65
entry_window: final 10s
post_fill_cooldown: 2s
failed_attempt_cooldown: 200ms
stop_check_after: 2s
stop_trigger_v1: Binance 最新价格反穿 open
max_positions: 每个币每个 round 最多一个仓位
```

## 安全 / 正确性备注

- 不能把没有响应的订单当成成交；
- 不能只凭本地 orderbook 模拟推断真实成交；
- 不使用 Gamma `outcomePrices` 作为执行价格；
- 实盘成交、paper 模拟、理论可成交必须分开记录；
- 如果没有真实订单或真实成交，dashboard 必须显示空状态或取消状态。
