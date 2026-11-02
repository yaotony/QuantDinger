# Agent Gateway 快速开始

QuantDinger 在 `/api/agent/v1` 提供租户隔离的 Agent Gateway。Agent Token 与人类用户 JWT 相互独立，并受 Scope、市场与标的白名单、速率、有效期、模拟盘限制和名义金额上限约束。

## 1. 创建并验证令牌

在管理员界面创建 Agent Token，首次显示时立即保存完整值。先用最小权限验证身份：

```bash
curl -H "Authorization: Bearer $QUANTDINGER_AGENT_TOKEN" \
  http://localhost:8888/api/agent/v1/whoami
```

开始工作前，可调用 `GET /api/agent/v1/environment`（`R` Scope；MCP 为 `check_environment`）一次查看后端版本、Token 权限、是否仅限模拟盘、回测并发额度、各市场与周期的回测区间上限以及 Token 到期时间。每项检查为 `pass`、`warn` 或 `fail`，并附 `remediation` 修正建议；MCP 工具还会检查连接与认证。返回内容不含 Token 本身。

任何会产生副作用的 `W/B/N/T` 请求都必须带唯一的 `Idempotency-Key`。只有重试完全相同的方法、路径、查询和请求体时，才复用原键。

## 2. 策略源码与部署

可执行策略使用 Strategy API V2。推荐流程：

1. `GET /strategy-sources/templates` 获取模板；
2. `POST /strategy-sources/compile` 编译检查；
3. `POST /strategy-sources` 保存私有源码；
4. 通过 `/strategy-sources/{source_id}/versions` 查看不可变版本；
5. 用已保存的 `sourceId` 创建停止状态的部署。

```bash
curl -X POST http://localhost:8888/api/agent/v1/strategies \
  -H "Authorization: Bearer $QUANTDINGER_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: deploy-spy-trend-v1" \
  -d '{
    "name": "spy-trend",
    "sourceId": 12,
    "initialCapital": 10000,
    "executionMode": "signal",
    "leverageEnabled": false,
    "params": {"lookback": 50}
  }'
```

部署配置使用 `W` Scope；启动、停止或下单属于运行时行为，需要 `T` Scope 和额外服务端保护。

## 3. 提交回测

回测异步执行。提交后轮询 `/jobs/{job_id}`，或订阅 `/jobs/{job_id}/stream`。提交时会先按市场与周期上限检查回测区间（含指标 warmup，例如加密货币 `15m` 为 365 天），超限时不建立也不计费任务，直接返回 HTTP 400，`message` 为 `strategyV2.backtestRangeLimit`，`details` 包含 `max_days`、`warmup_days`、`recommended_start`、`recommended_end`；若 warmup 本身已超限，`recommendation_available` 为 `false`、建议日期为 `null`，需减少 warmup 或改用更高周期。任务运行时因同类原因失败（如动态股票池）时，`error` 为 `{code, message, details}` 对象，`details` 结构相同。取消任务使用 `POST /jobs/{job_id}/cancel`，需要 `B` Scope、新幂等键以及 MCP 层确认。

回测可传内嵌 `code`，也可传已保存的 `sourceId`（可选 `sourceVersion`，不传则用最新版本），两者必须且只能传一个；只能回测本租户的源码，其他租户的源码返回 404。使用 `sourceId` 时，回执与任务结果会带 `strategySource`，记录实际运行的 source id 与版本。

提交前可先调用 `GET /api/agent/v1/data-coverage`（`R` Scope；MCP 为 `get_data_coverage`）检查区间：传入 `market`、`symbol`、`timeframe`，可选 `start_date`、`end_date`、`warmup_bars`。返回数据源最早可用的 K 线时间（数据源无法提供时为 `unknown`，不做猜测）、含 warmup 的区间上限，以及区间是否可行和建议日期；判断规则与提交时的检查相同。

期交所选择权只提供行情。用 `GET /api/agent/v1/option-chain?product=TXO&expiry_month=2026-08`（`R` Scope；MCP 为 `get_option_chain`）读取选择权链：返回各履约价的买权与卖权报价，默认以加权指数价格为中心（`strikes` 1-25，默认 10；可用 `center` 指定其他履约价）。周选商品为 `TX1`／`TX2`／`TX4`／`TX5`。`TXO23000H6` 这类合约代码（买权月份字母 A-L、卖权 M-X）也可在 `TWFutures` 市场下用于 `/klines` 与 `/price`。回测引擎不模拟权利金、到期、履约与保证金，因此策略中只要出现选择权合约，回测或部署都会返回 HTTP 400，`message` 为 `strategyV2.optionsBacktestUnsupported`。

完整的设计与迭代流程、回测区间上限和错误处理见 [Agent 策略设计与回测迭代流程](AGENT_STRATEGY_ITERATION_CN.md)。

成功的回测会写入 Backtest Center 历史，任务结果中的 `result.runId` 即该记录。使用 `R` Scope 读取回测历史：

- `GET /backtest/runs`：按时间倒序列出本租户的回测，可按 `sourceId`、`strategyId`、`symbol`、`market`、`timeframe`、`createdFrom`/`createdTo`（`YYYY-MM-DD` 或 ISO-8601）筛选，用 `limit`/`offset` 分页，`pagination.hasMore` 与 `nextOffset` 指示是否还有下一页。每笔附 `metrics`：`totalReturn`、`maxDrawdown`、`totalTrades`、`winRate`、`totalFees`。
- `GET /backtest/runs/{run_id}`：读取单笔回测的完整结果（含 `manifest` 与 `result`）。
- `GET /backtest/runs/{run_id}/report?language=zh-CN`：读取 Markdown 回测报告（`data.markdown`），`language` 可用 `en-US`（默认）、`zh-CN`、`zh-TW`。内容与 Backtest Center 下载的报告相同，各区块说明见 [向使用者報告回測結果](AGENT_STRATEGY_ITERATION_CN.md#向使用者報告回測結果)。

旧记录未保存手续费合计时，列表中的 `totalFees` 为 `null`，单笔读取会改用 `result.totalCommission`。其他租户的 `runId` 返回 404。

优化已保存源码的参数时，请使用优化任务，而不是循环提交单次回测。`GET /optimization/parameter-space?sourceId=12`（R）返回源码声明的参数范围与上限。`POST /optimization/sweep` 与 `POST /optimization/walk-forward`（B，必须带 `Idempotency-Key`）会提交 `parameter_optimization` 任务；结果分别列出样本内（选参）与样本外（仅验证）指标，给出 `bestParamsOutOfSample`，并把最佳参数的回测写入历史（`result.history`）。组合数、并发数与每日次数超限时返回明确的 400/429 错误。详见[迭代流程步骤 7](AGENT_STRATEGY_ITERATION_CN.md#步驟-7參數優化但不過度擬合)。

## 4. 研究、指标与交易

- `/research/*`：点时点股票池、因子元数据和租户自选列表。
- `/indicators/*`：仅用于图表指标；指标代码不能直接提交到回测端点。
- `/trading/*`：返回脱敏的凭据元数据、账户快照、持仓、挂单和成交记录。
- `/notifications/signal-alerts`：管理信号提醒；立即评估可能触发通知，需明确确认。
- `/quick-trade/*`：快速下单和紧急停止，必须使用 `T` Scope。

实盘执行还要求服务端启用实盘、令牌允许实盘、提供凭据引用、客户端明确确认，并满足单笔和单日名义金额上限。

## 5. 上线前检查

- 先用模拟盘和最小 Scope 验证完整流程。
- 不记录 Token、API Key、Secret 或 Passphrase；脱敏值不可尝试还原。
- 对订单、通知、取消、恢复版本和紧急停止保留人工确认。
- 按 `429` 响应的 `Retry-After` 退避，不绕过共享速率限制。

完整字段和响应以 [Agent OpenAPI](agent-openapi.json) 为准；MCP 客户端配置见 [MCP 接入指南](MCP_SETUP_CN.md)。
