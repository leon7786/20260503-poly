# v2 — Trade Lead 利润最大化策略

## 一句话核心

使用 **CEX 原始成交流**（Binance `@trade`，不是慢速 ticker 摘要）作为最早公开信号，在 Polymarket CLOB 完全重定价之前，发送 **带价格保护的即时订单** 捕捉价差。

```text
CEX @trade 成交流最早暴露真实穿越
→ 在 Polymarket 完全重定价前判断近端方向
→ 立即发送受保护 FOK/FAK 订单
→ 用动态价格 cap、冷却、止损和止盈最大化利润
```

v2 从一个核心出发：

> 这里的 edge 不是分钟级预测，而是微延迟：CEX 成交流可能领先 Polymarket 重新定价。

## v2 相比 v1 的变化

v1 是清晰保守版：

```text
Binance aggTrade 穿越 open
→ FOK/FAK BUY 5 shares @ 0.65
→ 2s cooldown
→ 再检查止损
```

v2 升级为：

1. 使用 **Binance `@trade`** 作为主触发源，而不是 `@aggTrade`。
2. 加入 **微爆发确认**，不依赖单一穿越。
3. 使用 **动态价格 cap 梯度**，不只固定 0.65。
4. 把 **捕捉入场** 和 **利润最大化** 分开设计。
5. 成交后根据 edge 评分决定持有、止损、止盈或减仓。
6. 记录完整延迟漏斗，而不是只记录交易结果。

## 数据源

### 主快速数据源

```text
Binance symbol@trade
```

原始成交流字段示例：

```json
{
  "e": "trade",
  "E": 0,
  "s": "BTCUSDT",
  "t": 0,
  "p": "100000.00",
  "q": "0.001",
  "T": 0,
  "m": true
}
```

使用字段：

- `p`：成交价格；
- `q`：成交数量；
- `T`：交易所成交时间戳；
- 本地接收时间戳：用于测量网络延迟。

### 辅助确认源

辅助源只用内存里的最新值，热路径中不等待它们，除非信号较弱。

```text
OKX trades/ticker latest
Bybit trades/ticker latest
Polymarket WSS book cache 仅用于诊断和可选 gate
```

## 核心流程

```text
1. 维护当前 15m round open 价格。
2. 订阅 Binance @trade。
3. final window 内处理每一笔成交。
4. 检测 open-cross 微爆发。
5. 在本地 CPU <1ms 内计算 signal score。
6. 分数达标后，立即发送 Polymarket 受保护即时订单。
7. 如果成交，进入 cooldown + position management。
8. 冷却后决定 hold / stop / take profit / hedge。
```

## 信号：trade-cross 微爆发

单一成交穿越 open 噪音太大。v2 使用极短滚动窗口，例如 100ms 到 300ms。

### 每个币维护的滚动字段

```text
last_trade_price
last_trade_ts
side_vs_open: UP / DOWN / FLAT
trades_100ms
trades_300ms
volume_100ms
volume_300ms
signed_volume_100ms
signed_volume_300ms
cross_count_300ms
last_cross_ts
```

### 方向定义

```text
price > open + epsilon => UP
price < open - epsilon => DOWN
否则 FLAT
```

`epsilon` 用于过滤极小噪音穿越。

第一版建议：

```text
BTC epsilon: 0.5 到 1.0 USD
ETH epsilon: 0.03 到 0.10 USD
SOL epsilon: 0.005 到 0.02 USD
DOGE/XRP: 使用百分比 epsilon
```

## 入场评分

v2 不只问：是否穿越？

v2 问：

> 这次穿越是否足够强，值得立即发送 FOK/FAK？

建议评分：

```text
score = 方向强度
      + 微爆发成交量分数
      + 剩余时间分数
      + 辅助源同向分数
      - 震荡惩罚
      - stale market 惩罚
```

### 评分组成

#### 1. 方向强度

```text
abs(last_trade_price - open) / open
```

穿越越深，信号越强。

#### 2. 微爆发成交量分数

```text
最近 100ms 同方向成交量 / 近期基准成交量
```

真实突破通常带成交量；假穿越可能只是一笔很小的成交。

#### 3. 剩余时间分数

```text
final 10s 到 3s：较好
final 3s 到 0.5s：潜在收益高，但订单延迟风险高
<0.5s：除非延迟已被证明足够低，否则避免新开仓
```

#### 4. 辅助源同向分数

不等待辅助源，但如果它们已经同向，给加分：

```text
OKX latest 同向：小幅加分
Bybit latest 同向：小幅加分
两者都反向：扣分
```

#### 5. 震荡惩罚

如果短时间来回穿越太多，降低分数或禁止入场。

```text
如果 cross_count_300ms >= 3：
    禁止入场，或要求更高成交量
```

## 入场订单设计

v2 使用受价格保护的即时订单。热路径中不 REST 获取 orderbook。

### 基础订单

```text
方向：BUY 目标 outcome
数量：至少 5 shares
订单类型：优先 FOK，方便干净评估
限价：动态 cap
```

### 动态 cap 梯度

不再只使用 0.65。v2 用信号强度决定可接受价格，以最大化利润和成交率。

建议：

```text
弱有效信号：       cap 0.60
普通信号：         cap 0.65
强微爆发：         cap 0.70
非常强的末端走势： cap 0.75
```

注意：

- 这不是“无脑高价买”；
- 每个 cap 都必须明确记录；
- 每个 cap bucket 单独评估 EV。

### 为什么需要动态 cap？

如果 Polymarket 重新定价很快，0.65 可能很少成交。强信号下 0.70 仍可能有正期望。

粗略 EV：

```text
每 share EV = P(win) * 1.00 - entry_price
```

例如：

```text
entry 0.65 要求 P(win) > 65%
entry 0.70 要求 P(win) > 70%
entry 0.75 要求 P(win) > 75%
```

v2 要通过数据学习哪个 cap bucket 真正赚钱。

## 持仓管理

### 成交后硬冷却

```text
post_fill_cooldown = 2s
```

冷却期间：

- 不新开仓；
- 不反手；
- 不立即止损；
- 继续记录所有 CEX trade 和 Polymarket book 变化。

目的：避免在 open 附近被噪音来回扫。

### 冷却后做三选一

2 秒后不要只问“止损还是持有”，而是问：

```text
1. 信号仍同向？       => hold
2. 信号已失效？       => stop / reduce
3. 已大幅盈利？       => take profit
```

## 止损逻辑

### v2 基础止损

```text
UP 仓：
    Binance @trade 最新价 < open - epsilon_stop

DOWN 仓：
    Binance @trade 最新价 > open + epsilon_stop
```

建议：

```text
epsilon_stop >= epsilon_entry
```

这样避免被很小的噪音止损。

### 止损确认实验

每个实验可以选择一种：

```text
A. Binance-only 最快止损
B. Binance + OKX 同向确认止损
C. 连续两笔反向 @trade
D. 反向微爆发成交量达到阈值
```

v2 默认建议：

```text
实盘小额优先 B 或 D；
纯延迟研究可以用 A。
```

## 止盈逻辑

这是 v2 的利润最大化升级。

如果入场价很便宜，且目标 outcome 很快涨到高 bid，可以提前锁定利润，而不是硬拿到结算。

冷却后，如果仓位仍同向：

```text
如果 Polymarket 持仓 token bid >= take_profit_bid：
    卖出已持有 shares
```

建议阈值：

```text
entry <= 0.65 → take profit bid >= 0.85
entry <= 0.70 → take profit bid >= 0.88
entry <= 0.75 → take profit bid >= 0.92
```

原因：

- 最后几秒可能剧烈反转；
- 每 share 赚 0.20 可能优于冒结算反转风险；
- 数据会告诉我们 hold-to-expiry 和 take-profit 哪个 EV 更高。

## 订单类型

### 入场

优先：

```text
FOK BUY 5 shares @ cap
```

原因：

- all-or-none；
- 记账干净；
- 避免奇怪的 partial minimum-size 仓位。

可选实验：

```text
FAK BUY 5 shares @ cap
```

前提是必须实现 partial fill 记账。

### 出场

止盈可优先：

```text
FAK SELL held shares
```

因为能卖多少就卖多少可以降低风险。

第一版为了清晰也可以：

```text
FOK SELL full held shares
```

## 防过度交易规则

### 每个 coin-round 最多一个活跃仓位

```text
max_one_active_position_per_coin_round = true
```

### 失败尝试节流

如果入场 FOK 失败：

```text
同 coin + 同 direction 冷却 150ms 到 300ms
```

但不要永久禁止反方向，因为 final window 的反转可能是真实机会。

### 震荡锁定

如果穿越太频繁：

```text
如果 cross_count_1s >= 5：
    锁定该 coin-round 1 秒，或要求极高 score
```

### 回合结束保护

如果离结算太近，除非已证明延迟足够低，否则不新开仓：

```text
secs_left < 0.35s 时不新开仓
```

## 热路径工程要求

热路径必须非常轻。

### 热路径应该做

```text
解析 Binance trade JSON
更新 rolling window
计算 side / score
如果达标：提交预构建 order intent
```

### 热路径不应该做

```text
REST fetch market
REST fetch book
同步写大 JSON 日志
复杂策略循环
dashboard 渲染
大量 Python 对象 deepcopy
```

### 推荐架构

```text
CEX WSS task
    → 极少锁或无锁状态更新
    → signal queue

Order executor task
    → 签名并提交即时订单
    → 记录响应

Logger task
    → 异步 / 批量 JSONL 写入

Polymarket WSS task
    → book cache + 诊断，不阻塞下单
```

## 延迟指标

每次 attempt 必须记录：

```json
{
  "event": "v2_order_attempt",
  "coin": "btc",
  "direction": "UP",
  "source": "binance_trade",
  "trade_id": 0,
  "trade_price": 100000.0,
  "trade_size": 0.01,
  "exchange_trade_ts": 0.0,
  "local_receive_ts": 0.0,
  "signal_decision_ts": 0.0,
  "order_submit_ts": 0.0,
  "order_response_ts": 0.0,
  "exchange_to_receive_ms": 0,
  "receive_to_decision_ms": 0,
  "decision_to_submit_ms": 0,
  "submit_to_response_ms": 0,
  "total_trade_to_response_ms": 0,
  "score": 0.0,
  "cap": 0.65,
  "order_type": "FOK",
  "requested_shares": 5,
  "result": "filled|cancelled|rejected|error",
  "filled_shares": 0,
  "avg_fill_price": null
}
```

没有这些指标，就无法知道是市场太快，还是机器人太慢。

## 评估框架

v2 初期不要只看总 PnL，而要拆开漏斗：

```text
CEX trade crosses
→ valid microburst signals
→ order attempts
→ API accepted
→ filled
→ after exit/settlement profitable
```

关键指标：

```text
signals/hour
attempts/hour
fill rate by cap bucket
win rate by cap bucket
EV per filled share
median trade-to-submit latency
median submit-to-response latency
fill rate vs latency bucket
PnL: hold-to-expiry vs take-profit vs stop
```

## 初始 v2 参数

```text
primary_source: Binance @trade
secondary_sources: OKX latest, Bybit latest
entry_window: final 10s
no_entry_after: 0.35s left
entry_epsilon: 按资产设定
score_threshold: 1.0 baseline
entry_caps: [0.60, 0.65, 0.70, 0.75]
entry_size: 5 shares
entry_order_type: FOK
post_fill_cooldown: 2s
failed_attempt_cooldown: 200ms
chop_window: 1s
chop_cross_limit: 5
take_profit_enabled: true
stop_enabled: true
max_active_position_per_coin_round: 1
```

## v2 预期优势

v1 问的是：

```text
aggTrade 穿越后，0.65 是否能成交？
```

v2 问的是更赚钱的问题：

```text
在什么信号强度和什么价格 cap 下，直接即时执行有正期望？
```

这样策略可以学习真正最优动作：

```text
不交易
只做 0.60
普通做 0.65
强信号做 0.70
极强信号做 0.75
持有到结算
快速止盈
确认后止损
```

## 安全备注

- 没有订单响应，不能记为成交；
- 本地 orderbook 模拟不能记为真实成交；
- 每个 cap bucket 必须单独评估；
- 如果使用 FAK，必须支持部分成交记账；
- 实盘必须从极小 size 和硬性 daily loss limit 开始；
- 没有真实订单或真实成交时，dashboard 必须显示空状态或取消状态，不显示推断进展。
