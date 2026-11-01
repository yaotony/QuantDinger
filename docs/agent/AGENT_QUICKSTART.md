# Agent Gateway Quickstart

QuantDinger exposes a tenant-scoped Agent Gateway at `/api/agent/v1`. Agent tokens are separate from human JWT sessions and enforce capability scopes, market/instrument allowlists, rate limits, expiry, and paper-only restrictions.

The machine-readable contract is [agent-openapi.json](agent-openapi.json). MCP setup is documented in [MCP_SETUP.md](MCP_SETUP.md).

## Authenticate

Create an Agent Token from the human admin UI, store the full token when it is shown once, and send it as a bearer token:

```bash
curl -H "Authorization: Bearer $QUANTDINGER_AGENT_TOKEN" \
  http://localhost:8888/api/agent/v1/whoami
```

Before starting work, `GET /api/agent/v1/environment` (R scope; MCP `check_environment`) reports in one call the backend version, token scopes, paper-only mode, backtest concurrency quota, range limits by market and timeframe, and token expiry. Each check is `pass`, `warn`, or `fail` with a `remediation` hint; the MCP tool adds connectivity and authentication checks. The token itself is never returned.

Scopes are `R` for reads, `W` for saved artifacts and deployment configuration, `B` for backtests, `N` for notification side effects, and `T` for runtime or order mutations. `C` is admin-only. Token permissions never bypass server-side live-trading controls.

Every mutating W/B/N/T request requires a unique `Idempotency-Key` header. Reuse the same key only when retrying the exact same method, route, query, and body. The gateway atomically reserves the key, returns a stored completed response on replay, and rejects concurrent or mismatched reuse.

## Strategy API V2

Executable strategies use Strategy API V2. Code defines `initialize(context)`, declares its universe and subscriptions, and provides `handle_data`, `on_rebalance`, or a scheduled callback. Markets, instruments, frequencies, dependencies, warmup, and leverage policy come from the compiled manifest.

The Agent Gateway exposes the complete source lifecycle:

1. List starter code with `GET /strategy-sources/templates`.
2. Compile code with `POST /strategy-sources/compile`.
3. Save a private source with `POST /strategy-sources`.
4. Inspect or update it through `/strategy-sources/{source_id}`.
5. Review immutable snapshots through `/strategy-sources/{source_id}/versions`.
6. Create a stopped deployment from the saved source id.

Source restoration requires an explicit `confirm=true` request and creates another immutable snapshot.

Create a stopped deployment from a saved source:

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

Update the same canonical fields with `PATCH /api/agent/v1/strategies/{id}`. Starting a deployment is intentionally not part of the W-scope configuration endpoint. A T-scope token can stop a running deployment through `/strategies/{id}/stop`.

## Backtests

Backtests accept Strategy API V2 code and run asynchronously:

```bash
curl -X POST http://localhost:8888/api/agent/v1/backtest/run \
  -H "Authorization: Bearer $QUANTDINGER_AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: spy-trend-2025" \
  -d '{
    "code": "def initialize(context):\n    g.symbol = \"USStock:SPY\"\n    context.set_universe([g.symbol])\n    context.subscribe(frequency=\"1d\")\n\ndef handle_data(context, data):\n    pass",
    "startDate": "2025-01-01",
    "endDate": "2025-12-31",
    "initialCapital": 10000,
    "leverageEnabled": false,
    "params": {}
  }'
```

To backtest a saved source instead of resending code, send `"sourceId": 12` (optionally `"sourceVersion": 3`; the latest version when omitted) in place of `code`. Exactly one of `code` or `sourceId` is required, only your tenant's sources are accepted, and the receipt and job result report the version that ran as `strategySource`.

Poll `/api/agent/v1/jobs/{job_id}` or consume `/api/agent/v1/jobs/{job_id}/stream`. Reuse an idempotency key when retrying the same submission.

The submission checks the date range plus indicator warmup against the market and timeframe limit (for example 365 days for crypto `15m`) before it creates or bills a job. An oversized request returns HTTP 400 with `message: "strategyV2.backtestRangeLimit"` and `details` containing `max_days`, `warmup_days`, `recommended_start`, and `recommended_end`. When the warmup alone exceeds the limit, `recommendation_available` is `false` and the recommended dates are `null`; reduce the warmup or use a higher timeframe. If a job still fails for the same reason at run time (for example a dynamic universe), its `error` is the object `{code, message, details}` with the same `details`.

To check a range before submitting, call `GET /api/agent/v1/data-coverage` (R scope; MCP `get_data_coverage`) with `market`, `symbol`, `timeframe`, and optionally `start_date`, `end_date`, and `warmup_bars`. It returns the earliest available bar (`unknown` when the data source cannot report it), the range limit including warmup, and whether the range is feasible with suggested dates, using the same rule as the submission check.

For the full design-and-iterate loop, backtest range limits, and error handling, see [Agent strategy design and backtest iteration](AGENT_STRATEGY_ITERATION.md).

Successful backtests also save a standard Backtest Center history record for the
agent token's user. The completed job's `result.runId` identifies that record;
the initial submission still returns `job_id` while the run is queued. The result
keeps its existing metrics alongside `runId`. A persistence error fails the job
instead of reporting a successful result without saved history. Jobs completed
before this behavior was enabled are not automatically added to history.

Cancel a queued or running job with `POST /jobs/{job_id}/cancel` using B scope, confirmation at the MCP layer, and a new idempotency key. A running worker may finish its local computation, but it cannot overwrite the durable cancelled state.

## Indicators

Indicators are chart-only. Fetch `/indicators/authoring-contract`, validate with `/indicators/validate`, and save through `/indicators`. Indicator code cannot be passed to the backtest endpoint; convert the trading idea to Strategy API V2 first.

## Research, broker observations, and notifications

Research tools expose point-in-time universes, factor metadata, and the tenant watchlist under `/research/*`. Broker observation endpoints under `/trading/*` return safe credential metadata, account snapshots, account/strategy positions, pending orders, and cursor-paginated trade ledgers. They never return decrypted API keys, secrets, passphrases, or encrypted credential blobs.

N-scope signal-alert endpoints under `/notifications/signal-alerts` reuse the existing indicator notification service. Immediate evaluation requires explicit MCP confirmation because it may deliver a notification.

## Runtime and orders

`GET /runtime/overview` returns compact tenant runtime state. Quick orders require T scope and an `Idempotency-Key`. Live execution additionally requires a live-capable token, server live-trading enablement, a credential reference, client-side explicit confirmation, and compliance with the token's `max_order_notional` and `max_daily_notional` caps.

The emergency stop at `/quick-trade/kill-switch` requires `confirm=true`. It attempts to cancel open agent-originated live orders, cancels open paper orders, revokes all active T-scope tokens for the tenant, and returns any exchange cancellation failures for mandatory human review.

Rate limiting is shared through Redis across API workers and enforces both token and tenant quotas. Responses include `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`; `429` responses also include `Retry-After`.

Never log tokens or credential material. Treat redacted values as terminal and do not attempt to reconstruct them.
