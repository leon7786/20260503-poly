# v3 — 多因子复合信号 + 动态价格上限 + 分批止盈

## 设计哲学

**不追求高胜率，追求高期望值（EV）。**

```
旧思路: 98% 胜率 × $2.50 盈利 = 每笔 $2.45 EV ← 过拟合，不现实
新思路: 55% 胜率 × $0.60 盈利 - 45% × $0.40 亏损 = 每笔 $0.15 EV ← 可持续
        × 每窗口 3-5 笔 × 96 窗口/天 = 每天 $43-$72
        × 多币种 7 路并行 = 每天 $300-$500
```

核心公式：

```text
EV = win_rate × (1 - fill_price) - (1 - win_rate) × fill_price

盈亏平衡: win_rate = fill_price
  fill_price = $0.50 → 需要 50% 胜率
  fill_price = $0.40 → 需要 40% 胜率
  fill_price = $0.60 → 需要 60% 胜率
```

**关键洞察：信号越强 → 出价越高 → 成交越快 → 利润越大**

---

## 策略一：复合评分信号引擎

不是单一动量阈值，而是多因子加权：

```text
composite_score = w1 × momentum + w2 × burst + w3 × agreement + w4 × timing + w5 × volume_trend

其中:
  momentum      = (price - open) / open × 100    权重 0.35
  burst         = 最近 1s 内同方向成交流浓度       权重 0.25
  agreement     = Binance 与 OKX 方向一致性        权重 0.15
  timing        = 距窗口结束的秒数倒数（越近越强） 权重 0.15
  volume_trend  = 最近 3s 成交量 vs 前 10s 均值    权重 0.10
```

### 动量因子 (momentum)

```text
raw = (current_price - open_price) / open_price * 100
normalized = tanh(raw / 0.05)  # 映射到 [-1, 1]，0.05% 时约 0.76

# tanh 的好处：
#   0.01% → 0.20（弱信号）
#   0.03% → 0.56（中等）
#   0.05% → 0.76（强）
#   0.10% → 0.96（极强，但不会无限放大）
```

### 微爆发因子 (burst)

```text
最近 1 秒内：
  up_volume = sum(trade.size for trade in last_1s if trade.side == 'BUY')
  down_volume = sum(trade.size for trade in last_1s if trade.side == 'SELL')

burst_direction = sign(up_volume - down_volume)
burst_intensity = |up_volume - down_volume| / total_volume  # 0~1

burst = burst_direction × burst_intensity
```

含义：短时间内单方向成交流突然放大 = 真实突破，不是噪音。

### 交易所一致性因子 (agreement)

```text
binance_dir = sign(binance_price - open)
okx_dir = sign(okx_price - open)

agreement = +1 if binance_dir == okx_dir != 0
            0  if either is 0
           -1  if binance_dir != okx_dir
```

两个独立交易所同时看涨/看跌 → 信号更可靠。

### 时间因子 (timing)

```text
seconds_left = window_end - now

# 越接近窗口结束，信息越确定
timing = 1.0 - (seconds_left / 900)  # 0~1，窗口开始=0，结束=1

# 但我们只在最后 10 秒交易
# 所以实际 timing ∈ [0.889, 1.0]
# 用 sigmoid 拉开差距:
timing = 1 / (1 + exp(-(10 - seconds_left) / 2))
```

### 成交量趋势因子 (volume_trend)

```text
recent_vol = sum(trade.size for trade in last_3s)
baseline_vol = sum(trade.size for trade in last_10s) / 3.33  # 3s 均值

volume_trend = tanh((recent_vol - baseline_vol) / baseline_vol)
```

含义：成交量放大 = 市场关注，信号更可靠。

### 复合评分

```text
score = 0.35 * momentum + 0.25 * burst + 0.15 * agreement + 0.15 * timing + 0.10 * volume_trend

direction = UP   if score > 0
           DOWN  if score < 0
           NONE  if |score| < 0.15  # 太弱，不下单
```

---

## 策略二：动态价格上限（核心创新）

**信号越强，出价越高，成交概率越大。**

```text
|score|    price_cap    期望成交价    需要胜率    预期EV/笔
─────────────────────────────────────────────────────────
0.15-0.25  $0.42         ~$0.40      40%        $0.10
0.25-0.35  $0.48         ~$0.45      45%        $0.10
0.35-0.50  $0.55         ~$0.50      50%        $0.10
0.50-0.65  $0.62         ~$0.55      55%        $0.15
0.65-0.80  $0.70         ~$0.60      60%        $0.20
0.80+      $0.78         ~$0.65      65%        $0.25
```

为什么这样设计：

```text
弱信号 (|score|=0.20):
  → 我不太确定方向
  → 出 $0.42 cap
  → 如果 Polymarket 还在 $0.50 → 不成交（保护我）
  → 如果 Polymarket 已跌到 $0.40 → 成交（有优势）

强信号 (|score|=0.70):
  → 我非常确定方向
  → 出 $0.70 cap
  → 即使 Polymarket 已涨到 $0.60 → 也要成交
  → 因为实际概率可能 75%，$0.60 仍然便宜
```

公式：

```text
base_price = 0.40 + |score| * 0.45    # 映射到 $0.40-$0.85
price_cap = min(base_price, 0.78)     # 硬顶 $0.78（安全边际）
price_cap = max(price_cap, 0.40)      # 硬底 $0.40
```

---

## 策略三：时间窗口分段

不是整个最后 10 秒都一样，分三段：

```text
最后 10-5 秒: "侦察期"
  只记录信号，不下单
  建立方向共识

最后 5-2 秒: "主攻期"
  复合评分 ≥ 0.25 → 下单
  这是最佳入场窗口（信息多 + 时间够）

最后 2-0 秒: "收割期"
  如果还没入场且信号极强 (|score| > 0.60) → 最后机会下单
  否则跳过
```

为什么这样分：

```text
太早（10-5s）: 信号可能反转，假突破多
太晚（2-0s）:  来不及下单/成交
中间（5-2s）:  信息充分 + 有时间执行 = 最优
```

---

## 策略四：分批止盈

不是"买入等结算"，而是主动管理：

```text
买入 5 shares @ $0.50

目标 1: 价格涨到 $0.65 → 卖出 2 shares (锁利 $0.30)
目标 2: 价格涨到 $0.80 → 再卖 1 share (锁利 $0.30)
目标 3: 持有剩余 2 shares → 等结算 (赢=$1.00, 亏=$0)

如果价格跌:
  止损: Binance 价格反转穿越 open → 全部卖出
```

数学：

```text
场景 A: 全部持有到结算
  胜: 5 × ($1.00 - $0.50) = +$2.50
  负: 5 × ($0.00 - $0.50) = -$2.50

场景 B: 分批止盈 + 结算
  涨到 $0.65 卖 2: +$0.30 (锁利)
  涨到 $0.80 卖 1: +$0.30 (锁利)
  剩余 2 股结算:
    胜: +$0.30 + $0.30 + 2×$0.50 = +$1.60
    负: +$0.30 + $0.30 + 2×(-$0.50) = -$0.40

  → 胜利时赚 $1.60（少于全仓的 $2.50）
  → 失败时只亏 $0.40（远好于全仓的 -$2.50）
  → 盈亏比从 1:1 提升到 4:1
```

---

## 策略五：多币种对冲

同时监控 7 个币种，但有选择地交易：

```text
每 15 分钟窗口:
  1. 计算所有 7 个币的 composite_score
  2. 按 |score| 排序
  3. 只交易 top 3（最强信号）
  4. 跳过弱信号（避免噪音交易）

好处:
  - 集中火力在最确定的机会
  - 减少交易次数 = 减少手续费损耗
  - 自然过滤掉震荡/横盘市场
```

特殊情况 — 反向对冲：

```text
如果 BTC score = +0.70 (强看涨)
同时 ETH score = -0.65 (强看跌)

可以同时开两个仓位:
  BTC UP + ETH DOWN

逻辑: 如果宏观环境不确定，但两个币走势分化
      → 对冲后只赚信号差
```

---

## 风控系统

### 每笔风险

```text
最大单笔: 5 shares × $0.78 = $3.90
止损后最大亏损: 5 × $0.50 = $2.50（分批止盈后更低）
```

### 每窗口风险

```text
最多 3 笔/窗口（top 3 币种）
最大窗口亏损: 3 × $2.50 = $7.50
```

### 每日风控

```text
日亏损上限: -$50
  → 触发后暂停交易到 UTC 00:00
连续亏损: 5 笔连亏
  → 暂停 30 分钟，重新校准
```

### 异常检测

```text
Binance OKX 价差 > 0.5%:
  → 可能有交易所故障/极端行情
  → 暂停下单，只记录

Polymarket midpoint 异常（$0.10-$0.90 之外）:
  → 市场可能已结算
  → 跳过该币种

信号延迟 > 2s:
  → Binance tick 到下单超过 2 秒
  → 信号可能已失效，跳过
```

---

## 执行流程（完整）

```text
┌─────────────────────────────────────────────────────────┐
│  每 50ms 循环（所有币种并行）                             │
│                                                          │
│  1. 读取 Binance + OKX 最新 tick                        │
│  2. 更新各因子值                                         │
│  3. 计算 composite_score                                 │
│                                                          │
│  if seconds_left > 5:                                    │
│    → 侦察期，只记录                                      │
│                                                          │
│  if 2 < seconds_left ≤ 5:                                │
│    → 主攻期                                              │
│    → |score| ≥ 0.25?                                     │
│      → YES: 计算 price_cap                               │
│             MarketOrderArgs(BUY @ price_cap, FAK, 5)     │
│             发单！                                        │
│      → NO: 继续监控                                      │
│                                                          │
│  if seconds_left ≤ 2:                                    │
│    → 收割期                                              │
│    → |score| ≥ 0.60 且未入场?                            │
│      → YES: 最后机会下单                                 │
│      → NO: 跳过                                          │
│                                                          │
│  ── 已入场后 ──                                          │
│                                                          │
│  if 价格达 +30%: 卖出 40%（2 shares）锁利                │
│  if 价格达 +60%: 再卖 20%（1 share）锁利                 │
│  if Binance 反转穿越 open: 全部止损                      │
│  if 窗口结束: 结算剩余仓位                               │
└─────────────────────────────────────────────────────────┘
```

---

## 预期收益模型

```text
保守估计:
  胜率: 55%
  平均成交价: $0.50
  平均每笔 EV: $0.10
  每窗口有效交易: 2 笔（7 币中选 top 3，实际成交 2 笔）
  每天: 96 窗口 × 2 笔 × $0.10 = $19.20/天
  多币种效应: × 1.5 = $28.80/天

乐观估计:
  胜率: 62%
  平均成交价: $0.48
  平均每笔 EV: $0.26
  每窗口: 3 笔
  每天: 96 × 3 × $0.26 = $74.88/天

分批止盈加成:
  止盈锁利减少亏损幅度
  实际 EV 提升 ~30%
  乐观: $74.88 × 1.3 = $97/天
```

---

## 实现要点

### 必须精确计时

```python
import time

# Binance tick 时间戳（交易所时间）
binance_ts = tick['T']  # 毫秒级

# 本地接收时间
local_ts = time.time()

# 延迟 = local - binance
latency_ms = (local_ts - binance_ts / 1000) * 1000

# 如果延迟 > 2000ms，信号可能已失效
if latency_ms > 2000:
    skip_signal()
```

### 必须并行获取 Polymarket 价格

```python
from concurrent.futures import ThreadPoolExecutor

def fetch_midpoint(coin):
    return requests.get(f'{CLOB_API}/midpoint', params={'token_id': token_id})

with ThreadPoolExecutor(max_workers=7) as pool:
    midpoints = dict(zip(COINS, pool.map(fetch_midpoint, COINS)))
```

### 必须记录每笔交易的完整上下文

```json
{
  "event": "v3_trade",
  "coin": "BTC",
  "window_start": 1777782600,
  "signal_time": "04:29:55.123",
  "composite_score": 0.62,
  "factors": {
    "momentum": 0.71,
    "burst": 0.55,
    "agreement": 1.0,
    "timing": 0.89,
    "volume_trend": 0.33
  },
  "direction": "UP",
  "price_cap": 0.68,
  "order_type": "FAK",
  "fill_price": 0.52,
  "filled_shares": 5,
  "cost": 2.60,
  "exits": [
    {"time": "04:29:57", "price": 0.65, "shares": 2, "pnl": 0.26},
    {"time": "04:30:00", "price": 1.00, "shares": 3, "pnl": 1.44}
  ],
  "total_pnl": 1.70,
  "settlement": "WIN"
}
```

---

## 与 v1/v1.1 的本质区别

| 维度 | v1/v1.1 | v3 |
|------|---------|-----|
| 信号 | 单一动量阈值 | 多因子复合评分 |
| 价格 | 固定 $0.65 cap | 动态 cap（$0.40-$0.78） |
| 入场 | 最后 10 秒随时 | 分段（侦察→主攻→收割） |
| 出场 | 等结算 | 分批止盈 + 动态止损 |
| 选择 | 所有币种都交易 | top 3 最强信号 |
| 目标 | 高胜率 | 高期望值（EV） |
| 风控 | 无 | 日亏损上限 + 连亏暂停 |
