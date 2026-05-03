# v1.1 — CLOB SDK 直接下单 + 中途平仓

## 核心改进

基于 v1/v2 的研究，发现三个关键问题并解决：

```
❌ 旧方案: 读 orderbook → 判断 ask → 下单
   问题1: CLOB WSS 只给初始 snapshot，不推实时更新
   问题2: CLOB REST /book 返回 stale 数据（ask=$0.99）
   问题3: 整个下单流程建立在错误数据上

✅ 新方案: 跳过 orderbook，直接下单让 CLOB 撮合
   信号源: Binance @aggTrade（领先 ~2s）
   下单: py-clob-client SDK，FAK 市价单
   平仓: 随时可以中途卖出
```

## 策略流程

```text
Binance aggTrade 穿越 open（领先 ~2s）
  ↓
py_clob_client: MarketOrderArgs(BUY @ $0.65 cap, FAK, 5 shares)
  ↓
CLOB 自动撮合 → 实际成交价 ~$0.50（由市场决定）
  ↓
2s cooldown（防 whipsaw）
  ↓
cooldown 结束 → 检查 Binance 价格
  → 仍在同侧 → 持有等 15min 结算
  → 反转 → MarketOrderArgs(SELL @ $0.01 floor, FAK) → 中途止损
```

## 关键技术决策

### 1. 为什么跳过 orderbook？

经过实测验证（2026-05-03）：

| 数据源 | 状态 | 说明 |
|--------|------|------|
| CLOB WSS `market` 订阅 | ❌ 无用 | 只给初始 snapshot + new_market 事件 |
| CLOB REST `/book` | ❌ stale | 返回 $0.99/$0.01，即使市场还在交易 |
| CLOB REST `/midpoint` | ✅ 实时 | 返回 $0.50（正确的 50/50 价格） |
| Data API `/trades` | ✅ 实时 | 有真实成交记录 |

结论：orderbook 数据不可靠，但 CLOB 撮合引擎本身是正常的。
所以直接下单让引擎撮合，不看 orderbook。

### 2. 为什么用 FAK 不用 FOK？

```text
FOK (Fill-Or-Kill):
  要求 5 shares 全部在 ≤$0.65 成交，否则全部取消
  → 盘口只有 3 shares → 全部取消，错过机会

FAK (Fill-And-Kill):
  能成交多少算多少，剩余取消
  → 盘口只有 3 shares → 先拿 3 shares，剩余 2 取消
  → 不会因为差 1-2 shares 就完全错过
```

### 3. 为什么 $0.65 是 price cap 不是成本？

```text
MarketOrderArgs(price=0.65) 的含义：
  "我最高出 $0.65 买，但尽量用更低的价格"

实际成交价由 CLOB 撮合引擎决定：
  如果卖单在 $0.50 → 你 $0.50 成交
  如果卖单在 $0.60 → 你 $0.60 成交
  如果卖单在 $0.70 → 超过 cap，不成交

差价自动退回：
  出价 $0.65，成交 $0.50 → 实际成本 $0.50 × 5 = $2.50
```

### 4. 中途平仓机制

Polymarket 允许在 15 分钟窗口内随时平仓（卖出持仓）：

```python
# 步骤 1: 同步 token 余额（必须，有传播延迟）
client.prepare_sell(token_id)

# 步骤 2: 卖出
order = MarketOrderArgs(
    token_id="xxx",
    amount=5,           # 卖出全部持仓
    side=SELL,
    price=0.01,         # floor price，尽量以最高价卖
    order_type=FAK,
)
signed = client.create_market_order(order)
result = client.post_order(signed, OrderType.FAK)
```

### 5. 信号源：Binance @aggTrade

```text
Binance WSS: wss://stream.binance.com:9443/stream?streams=btcusdt@aggTrade
OKX WSS:     wss://ws.okx.com:8443/ws/v5/public (channel: trades)

推送频率（实测）:
  周六凌晨: BTC ~3 ticks/s, ETH ~10, DOGE ~29
  工作日:   BTC 20-50+ ticks/s

每笔 tick = 一笔真实成交（不是行情快照）
```

## 依赖

```bash
pip install py-clob-client websockets
```

## 环境变量

```env
PRIVATE_KEY=你的以太坊钱包私钥
FUNDER_ADDRESS=你的 Polygon 地址
POLYGON_RPC_URL=https://polygon-rpc.com
```

## 代码参考

| 项目 | 说明 |
|------|------|
| [nothing-ever-happens](https://github.com/sterlingcrispin/nothing-ever-happens) | 最佳 CLOB 客户端参考代码（932⭐） |
| [poly-maker](https://github.com/warproxxx/poly-maker) | 做市商机器人（1123⭐） |
| [weather-bot](https://github.com/suislanchez/polymarket-kalshi-weather-bot) | BTC 5m 微观结构信号 + 天气（287⭐） |

## 状态机

```text
FLAT
  ↓ Binance aggTrade 穿越 open
ORDER_SENT (MarketOrderArgs BUY @ $0.65 cap, FAK)
  ↓ 成交
POSITION_OPEN + cooldown_until = now + 2s
  ↓ cooldown 结束
WATCH_STOP
  ↓ Binance 价格反转 → SELL FAK 中途止损
EXIT_SENT → CLOSED
  ↓ Binance 价格未反转
HOLD_TO_SETTLEMENT → 结算
```

## 与 v1 的区别

| 维度 | v1 | v1.1 |
|------|----|----|
| 下单方式 | 手动构造 API 请求 | py-clob-client SDK |
| 订单类型 | FOK | FAK（更好的部分成交） |
| 是否看 orderbook | 看（但数据是 stale 的） | 不看，直接下单 |
| 中途平仓 | 不支持 | 支持（SELL FAK） |
| 签名方式 | 手动 EIP-712 | SDK 自动处理 |
| 数据源 | CLOB WSS（无用） | Binance aggTrade |

## 研究问题

v1.1 应该回答：

1. FAK 直接下单的实际成交率是多少？
2. 实际成交价 vs midpoint 的偏差有多大？
3. 2s cooldown 后止损触发率是多少？
4. 中途止损 vs 持有结算，哪个 PnL 更好？
5. 不同时段（亚洲/美国/周末）的成交质量差异？
