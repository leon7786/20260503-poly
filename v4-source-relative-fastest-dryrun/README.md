# 20260503-poly v4 — Polymarket 15 分钟 Up/Down Dry-Run 交易器

> **状态：** v4 默认只做 dry-run。真实交易默认关闭。
>
> 仓库：<https://github.com/leon7786/20260503-poly>

## 1. v4 做什么

v4 是一个针对 Polymarket 加密货币 15 分钟 **Up/Down** 市场的策略运行器，重点监控每轮最后几秒。

核心思路：

1. 根据 slug 自动找到当前 Polymarket 15m 市场 token，例如 `btc-updown-15m-<round_start>`。
2. 每个报价源在本轮开始时，用自己的第一笔有效价格锁定 `source_open`。
3. 最后 10 秒，同时监控多个交易所的最快报价源。
4. 任意一个 source 穿越自己的 `source_open`，即认为 fastest valid cross 出现。
5. 立即生成 Polymarket 受保护订单意图，当前默认 dry-run：
   - 方向：`UP` 或 `DOWN`
   - 数量：5 shares
   - 最高价格：0.65
   - 订单类型：默认 `FAK`

v4 的目标是：在真实交易前，先安全验证多源行情、时间戳开盘价、final-window cross、下单路径和日志完整性。

## 2. 为什么使用 source-relative open

Polymarket BTC/USD round 的结算来源是 Chainlink BTC/USD，不是直接使用 Binance、Coinbase、OKX 或 Bybit 的现货价格。

直接这样判断是不安全的：

```text
Binance 当前价 > Polymarket / Chainlink open
```

原因是不同交易所之间天然存在 basis / 价差。比如同一时间：

```text
Chainlink BTC/USD open = 101000.0
Binance BTCUSDT        = 100996.5
Coinbase BTC-USD       = 101002.1
Bybit BTCUSDT mid      = 100998.8
OKX BTC-USDT mid       = 100997.9
```

如果拿交易所的 absolute price 去穿越 Chainlink open，很容易被交易所 basis 误导。

v4 改用 timestamp 对齐的 source open：

```text
Binance 当前价  vs Binance 本轮 open
Coinbase 当前价 vs Coinbase 本轮 open
Bybit mid 当前价 vs Bybit 本轮 open
OKX mid 当前价   vs OKX 本轮 open
```

这等价于把每个交易所的开盘价映射到 Polymarket round 的同一个时间戳上。触发信号只看：

```text
source_now 是否穿越 source_open
```

这样更接近“用最快交易所报价预测 Chainlink 最终方向”的真实意图。

## 3. 当前监控的最快报价源

v4 同时监控这些 WebSocket feed：

| Source 名称 | 交易所 feed | 使用价格 | 用途 |
|---|---|---:|---|
| `binance_trade` | Binance Spot `@trade` | 最新成交价 | 原始逐笔成交，反应快 |
| `coinbase_market_trades` | Coinbase Advanced Trade `market_trades` | 最新成交价 | 高频 USD 成交流 |
| `bybit_orderbook1_mid` | Bybit Spot `orderbook.1` | `(best_bid + best_ask) / 2` | 类似手机 App 交易页快速跳动的 level-1 报价 |
| `okx_books5_mid` | OKX Spot `books5` | `(best_bid + best_ask) / 2` | OKX 五档订单簿 mid，通常比 OKX trades 更密 |

策略**不等待多交易所确认**。谁先在 final window 内有效穿越自己的 open，谁触发 dry-run 下单意图。

## 4. 文件结构

```text
src/poly_v1_trader.py              主程序，支持 dry-run / live-capable 执行路径
tests/test_v1_trader.py            单元测试，覆盖 source-relative open 和执行规则
scripts/poly_v1_health_check.py    导入、配置、公共 API 健康检查
deploy/poly-v1-trader.env.example  安全环境变量模板，默认 LIVE_TRADING=0
deploy/poly-v1-trader.service      systemd 服务模板
requirements.txt                   Python 依赖
```

## 5. 安全模型

### 默认关闭真实交易

默认配置里是：

```text
LIVE_TRADING=0
```

当 `LIVE_TRADING=0` 时，服务只写入：

```text
dry_run_buy
dry_run_sell
```

不会提交真实 Polymarket 订单。

### 开启 live 必须显式确认

不要直接把 `LIVE_TRADING` 改成 1。必须先确认：

- dry-run 日志正常；
- source open 锁定时间合理；
- final-window cross 逻辑符合预期；
- 钱包、funder、signature_type 正确；
- Polymarket 余额和 allowance 正常；
- 已明确接受真实交易风险。

live mode 需要：

```text
POLY_PRIVATE_KEY=0x...
POLY_FUNDER_ADDRESS=0x...
POLY_SIGNATURE_TYPE=1
LIVE_TRADING=1
```

### source_open 新鲜度保护

配置：

```text
SOURCE_OPEN_MAX_DELAY_SEC=5
```

如果某个 source 是 round 开始很久之后才锁到 open，entry 会跳过：

```text
source_open_too_late
```

这个保护用于避免服务在一轮中途启动时，把错误价格当成本轮开盘价。

## 6. 配置

复制环境变量模板：

```bash
sudo cp deploy/poly-v1-trader.env.example /etc/poly-v1-trader.env
sudo chmod 600 /etc/poly-v1-trader.env
```

重要参数：

```text
LIVE_TRADING=0
ENTRY_WINDOW_SEC=10
NO_ENTRY_AFTER_SEC=0.25
ENTRY_PRICE_CAP=0.65
ENTRY_SHARES=5
ENTRY_ORDER_TYPE=FAK
POST_FILL_COOLDOWN_SEC=2
FAILED_ATTEMPT_COOLDOWN_SEC=0.2
STOP_SELL_FLOOR=0.01
STOP_ORDER_TYPE=FAK
ENTRY_EPSILON_PCT=0
DRY_RUN_FILL_MODE=no_fill
SOURCE_OPEN_MAX_DELAY_SEC=5
V1_LOG_PATH=/root/projects/20260503-poly/logs/v1_live_trader_events.jsonl
POLY_TICK_SIZE=0.01
POLY_NEG_RISK=0
```

参数说明：

| 参数 | 含义 |
|---|---|
| `ENTRY_WINDOW_SEC=10` | 只在最后 10 秒允许 entry |
| `NO_ENTRY_AFTER_SEC=0.25` | 距离结束小于 0.25 秒不再 entry，避免太晚 |
| `ENTRY_PRICE_CAP=0.65` | 买入最高保护价 |
| `ENTRY_SHARES=5` | 默认买 5 shares |
| `ENTRY_ORDER_TYPE=FAK` | 默认 Fill-And-Kill，允许部分成交并取消剩余 |
| `POST_FILL_COOLDOWN_SEC=2` | 成交后 2 秒冷却，不马上反手或止损 |
| `FAILED_ATTEMPT_COOLDOWN_SEC=0.2` | 失败尝试冷却，避免 tick 级别刷单 |
| `DRY_RUN_FILL_MODE=no_fill` | dry-run 默认不假设成交 |
| `SOURCE_OPEN_MAX_DELAY_SEC=5` | source open 迟到超过 5 秒则不使用该 source entry |

## 7. 安装

在 v4 目录里安装：

```bash
cd /root/projects/20260503-poly/v4-source-relative-fastest-dryrun
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

如果使用仓库根目录已有 `.venv`，也可以直接用根目录 venv 运行。

## 8. 测试和健康检查

运行单元测试：

```bash
.venv/bin/pytest tests/test_v1_trader.py -q
```

语法检查：

```bash
.venv/bin/python -m py_compile src/poly_v1_trader.py scripts/poly_v1_health_check.py
```

公共 API 健康检查：

```bash
.venv/bin/python scripts/poly_v1_health_check.py
```

期望输出包含：

```text
clob_ok: OK
gamma_sample_len: 1
live_trading: false
```

## 9. 手动 dry-run

```bash
cd /root/projects/20260503-poly/v4-source-relative-fastest-dryrun
LIVE_TRADING=0 timeout 30s .venv/bin/python src/poly_v1_trader.py
```

查看日志：

```bash
tail -n 100 logs/v1_live_trader_events.jsonl
```

常见事件：

| Event | 含义 |
|---|---|
| `service_started` | 服务启动并记录配置 |
| `wss_connected` | 某个交易所行情 WebSocket 已连接 |
| `round_reset` | 检测到新的 15m round |
| `open_set` | audit/fallback open，从第一笔 source tick 设置 |
| `source_open_locked` | 某个 source 的 timestamp-aligned round open 已锁定 |
| `source_tick` | 处理了一笔报价更新 |
| `cross` | 某个 source 穿越自己的 source_open |
| `dry_run_buy` | 如果 live 会提交 Polymarket buy，此处只记录意图 |
| `entry_no_fill` | dry-run/live 订单没有成交 |
| `dry_run_sell` | 如果 live 会提交 Polymarket sell，此处只记录意图 |

`source_open_locked` 示例：

```json
{
  "event": "source_open_locked",
  "coin": "btc",
  "source": "bybit_orderbook1_mid",
  "round_start": 1777834800,
  "source_open": 123456.7,
  "open_delay_ms": 42.0
}
```

final-window cross 示例：

```json
{
  "event": "cross",
  "coin": "btc",
  "source": "coinbase_market_trades",
  "direction": "UP",
  "price": 123460.0,
  "source_open": 123456.7,
  "source_delta": 3.3,
  "secs_left": 7.8,
  "reason": "ok"
}
```

## 10. systemd dry-run 部署

安装服务文件：

```bash
sudo cp deploy/poly-v1-trader.service /etc/systemd/system/poly-v1-trader.service
sudo systemctl daemon-reload
sudo systemctl enable --now poly-v1-trader.service
```

查看状态：

```bash
systemctl status poly-v1-trader.service --no-pager
```

查看策略日志：

```bash
tail -f /root/projects/20260503-poly/logs/v1_live_trader_events.jsonl
```

停止服务：

```bash
sudo systemctl stop poly-v1-trader.service
```

> 注意：当前 systemd 模板的路径仍按服务器项目路径写死。如果你把 v4 放在其他路径，需要同步修改 `WorkingDirectory`、`ExecStart` 和日志路径。

## 11. 下单实现细节

live mode 开启后，执行器使用官方 `py-clob-client`：

```python
OrderArgs(token_id=token_id, price=cap, size=shares, side=BUY)
client.create_order(order_args, PartialCreateOrderOptions(...))
client.post_order(signed, OrderType.FAK)
```

v4 故意使用 `OrderArgs`，不用 `MarketOrderArgs`。原因：

- `MarketOrderArgs` 的 BUY `amount` 是美元金额，不是 shares；
- 如果 `price <= 0`，它可能会额外调用 REST orderbook 计算价格，增加延迟；
- v4 的目标是：已经有明确 price cap 和 shares，直接构造受保护的 FAK/FOK order。

## 12. 当前验证状态

写入 v4 README 时，本地验证通过：

```text
pytest: 7 passed
py_compile: OK
health_check: CLOB OK, Gamma OK
short dry-run: Binance, Coinbase, Bybit, OKX connected
```

短时间 dry-run 已确认会产生：

```text
wss_connected
source_open_locked
source_tick
cross
```

## 13. 重要限制

- dry-run 默认 `DRY_RUN_FILL_MODE=no_fill`，只验证信号和下单意图，不代表真实成交质量。
- `open_price` 目前是 audit/fallback 字段；实际触发使用的是每个 source 自己的 `source_open`。
- Chainlink official open/close 后续仍应接入日志，用于复盘哪个 source 更贴近最终结算。
- live trading 需要确认钱包、funder、signature_type、余额、allowance 和地理/合规限制。
- final 10s aggressive 策略可能高频触发，必须依赖 cooldown 和 order cap 防止过度交易。

## 14. v4 相比旧版本新增内容

- timestamp/source-relative open 锁定；
- 多交易所 fastest quote monitoring；
- Coinbase `market_trades`；
- Binance `@trade`；
- Bybit `orderbook.1` mid；
- OKX `books5` mid；
- dry-run 安全默认配置；
- source_open 迟到保护；
- 覆盖 source-relative cross 的单元测试。
