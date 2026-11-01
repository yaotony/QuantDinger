"""Markdown backtest report rendered from one persisted backtest run.

The Backtest Center download and the Agent Gateway / MCP report read the same
tenant-owned run through ``StrategyBacktestRepository.get_run`` and render it
with :func:`render_backtest_report`, so both surfaces show identical content.

Every figure is copied from the stored result JSON (or the run's summary
columns when the JSON lacks it); the renderer never recomputes a metric the
engine already reported. The only derived values are the holding-time and
MAE/MFE distributions, computed from the stored per-trade fields. Missing
fields render as the localized "no data" text instead of failing, so runs
persisted before trade diagnostics still produce a report.

Labels come from the JSON catalogs in ``report_locales/``.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any, Mapping

REPORT_LANGUAGES: tuple[str, ...] = ("en-US", "zh-CN", "zh-TW")
DEFAULT_REPORT_LANGUAGE = "en-US"

_LOCALE_DIR = Path(__file__).resolve().parent / "report_locales"
_HOUR = 3600.0
_DAY = 86400.0
_FUNDING_MARKET_TYPES = {"swap", "perp", "perpetual", "futures", "future"}


def resolve_report_language(raw: Any) -> str | None:
    """Return the supported language tag matching ``raw`` or None."""
    text = str(raw or "").strip().lower().replace("_", "-")
    if not text:
        return None
    aliases = {"en": "en-US", "zh": "zh-CN", "zh-hans": "zh-CN", "zh-hant": "zh-TW"}
    if text in aliases:
        return aliases[text]
    for language in REPORT_LANGUAGES:
        if language.lower() == text:
            return language
    return None


@lru_cache(maxsize=None)
def _catalog(language: str) -> dict[str, Any]:
    with open(_LOCALE_DIR / f"{language}.json", encoding="utf-8") as handle:
        return json.load(handle)


def report_filename(run_id: Any) -> str:
    return f"backtest-run-{int(run_id)}.md"


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: Any) -> str:
    """Escape free text (user-controlled names) for a Markdown table cell."""
    raw = " ".join(str(value or "").split())
    for char in ("\\", "|", "`", "*", "_", "[", "]"):
        raw = raw.replace(char, "\\" + char)
    return raw.replace("<", "&lt;").replace(">", "&gt;")


class _Renderer:
    def __init__(self, run: Mapping[str, Any], language: str) -> None:
        self.run = run
        result = run.get("result")
        self.result: Mapping[str, Any] = result if isinstance(result, Mapping) else {}
        assumptions = self.result.get("executionAssumptions")
        self.assumptions: Mapping[str, Any] = assumptions if isinstance(assumptions, Mapping) else {}
        self.t = _catalog(language)
        self.no_data = self.t["no_data"]
        trades = self.result.get("closedTrades") or self.result.get("trades") or []
        self.trades = [item for item in trades if isinstance(item, Mapping)] if isinstance(trades, list) else []
        self.total_trades = self._int(self.pick("totalTrades", "total_trades"))
        self.has_diagnostics = self.result.get("tradeDiagnosticsBasis") is not None

    # -- value access -------------------------------------------------
    def pick(self, field: str, column: str | None = None) -> Any:
        value = self.result.get(field)
        if value is None and column:
            value = self.run.get(column)
        return value

    @staticmethod
    def _int(value: Any) -> int | None:
        number = _number(value)
        return int(number) if number is not None else None

    def fmt(self, key: str, **values: Any) -> str:
        return str(self.t[key]).format(**values)

    # -- formatting ---------------------------------------------------
    def money(self, value: Any) -> str:
        number = _number(value)
        return self.no_data if number is None else f"{number:,.2f}"

    def pct(self, value: Any) -> str:
        number = _number(value)
        return self.no_data if number is None else f"{number:.2f}%"

    def ratio(self, value: Any) -> str:
        number = _number(value)
        return self.no_data if number is None else f"{number:.2f}"

    def rate(self, value: Any) -> str:
        """Format a fractional rate (0.001) as a percentage (0.1%)."""
        number = _number(value)
        if number is None:
            return self.no_data
        text = f"{number * 100:.4f}".rstrip("0").rstrip(".")
        return f"{text}%"

    def plain(self, value: Any) -> str:
        text = _text(value)
        return text if text else self.no_data

    def code(self, value: Any) -> str:
        text = str(value or "").replace("`", "").strip()
        return f"`{text}`" if text else self.no_data

    def duration(self, seconds: Any) -> str:
        number = _number(seconds)
        if number is None:
            return self.no_data
        total = int(round(max(0.0, number)))
        days, rest = divmod(total, int(_DAY))
        hours, rest = divmod(rest, int(_HOUR))
        minutes, secs = divmod(rest, 60)
        parts: list[str] = []
        for amount, key in (
            (days, "duration_days"),
            (hours, "duration_hours"),
            (minutes, "duration_minutes"),
        ):
            if amount:
                parts.append(self.fmt(key, value=amount))
        if not parts:
            parts.append(self.fmt("duration_seconds", value=secs))
        return " ".join(parts[:2])

    def table(self, rows: list[tuple[str, str]]) -> list[str]:
        lines = [f"| {self.t['metric']} | {self.t['value']} |", "| --- | --- |"]
        lines.extend(f"| {label} | {value} |" for label, value in rows)
        return lines

    # -- sections -----------------------------------------------------
    def header(self) -> list[str]:
        name = self.run.get("strategy_name") or self.result.get("strategyName") or ""
        created = self.run.get("created_at")
        created_text = created.isoformat() if hasattr(created, "isoformat") else str(created or "")
        engine = (self.result.get("engine") or {}).get("version") if isinstance(
            self.result.get("engine"), Mapping
        ) else None
        return [
            "# " + self.fmt("title", name=_text(name) or f"#{self.run.get('id')}"),
            "",
            self.fmt(
                "run_line",
                run_id=self.run.get("id"),
                created_at=created_text or self.no_data,
                engine=_text(engine or self.run.get("engine_version")) or self.no_data,
            ),
        ]

    def summary(self) -> list[str]:
        start = self.run.get("start_date") or self.assumptions.get("startDate") or ""
        end = self.run.get("end_date") or self.assumptions.get("endDate") or ""
        period = f"{_text(start)} – {_text(end)}" if start and end else self.no_data
        initial = self.pick("initialCapital", "initial_capital")
        if _number(initial) is None:
            initial = self.assumptions.get("initialCapital")
        benchmark_available = str(self.result.get("benchmarkStatus") or "") in ("available", "partial")
        benchmark = self.result.get("benchmark")
        benchmark_symbol = benchmark.get("symbol") if isinstance(benchmark, Mapping) else ""
        benchmark_label = (
            self.fmt("benchmark_return", benchmark=_text(benchmark_symbol))
            if benchmark_symbol
            else self.t["benchmark_return_plain"]
        )
        annualized = (
            self.result.get("annualizedReturn")
            if self.result.get("annualizedReturnAvailable") is not False
            else None
        )
        rows = [
            (self.t["period"], period),
            (self.t["market"], self.plain(self.run.get("market"))),
            (self.t["symbol"], self.plain(self.run.get("symbol"))),
            (self.t["timeframe"], self.plain(self.run.get("timeframe") or self.result.get("timeframe"))),
            (self.t["initial_capital"], self.money(initial)),
            (self.t["final_equity"], self.money(self.result.get("finalEquity"))),
            (self.t["total_return"], self.pct(self.pick("totalReturn", "total_return"))),
            (self.t["annualized_return"], self.pct(annualized)),
            (
                benchmark_label,
                self.pct(self.pick("benchmarkTotalReturn", "benchmark_total_return"))
                if benchmark_available
                else self.no_data,
            ),
            (
                self.t["excess_return"],
                self.pct(self.result.get("excessReturn")) if benchmark_available else self.no_data,
            ),
            (self.t["max_drawdown"], self.pct(self.pick("maxDrawdown", "max_drawdown"))),
            (self.t["sharpe_ratio"], self.ratio(self.pick("sharpeRatio", "sharpe_ratio"))),
            (self.t["sortino_ratio"], self.ratio(self.result.get("sortinoRatio"))),
            (self.t["calmar_ratio"], self.ratio(self.result.get("calmarRatio"))),
        ]
        return [f"## {self.t['section_summary']}", "", *self.table(rows)]

    def _funding_cell(self) -> str:
        funding = _number(self.result.get("totalFundingCost"))
        if funding is not None:
            return self.money(funding)
        mode = str(self.assumptions.get("fundingMode") or "")
        if mode == "not_modeled":
            return self.t["funding_not_modeled"]
        return self.no_data

    def costs(self) -> list[str]:
        lines = [f"## {self.t['section_costs']}", ""]
        total_commission = self.pick("totalCommission", "total_commission")
        if not self.has_diagnostics:
            rows = [
                (self.t["commission"], self.money(total_commission)),
                (self.t["slippage"], self.no_data),
                (self.t["funding"], self._funding_cell()),
            ]
            return [*lines, *self.table(rows)]
        rows = [
            (self.t["commission"], self.money(self.result.get("totalTradeCommission"))),
            (self.t["slippage"], self.money(self.result.get("totalSlippageCost"))),
            (self.t["funding"], self._funding_cell()),
            (self.t["total_trading_cost"], self.money(self.result.get("totalTradingCost"))),
            (self.t["gross_profit_before_costs"], self.money(self.result.get("grossProfitBeforeCosts"))),
            (self.t["gross_loss_before_costs"], self.money(self.result.get("grossLossBeforeCosts"))),
            (self.t["cost_to_gross_profit"], self.pct(self.result.get("costToGrossProfitPct"))),
            (self.t["cost_to_gross_loss"], self.pct(self.result.get("costToGrossLossPct"))),
        ]
        lines.extend(self.table(rows))
        lines.extend(["", self.fmt("costs_basis_note", total=self.money(total_commission))])
        return lines

    def _sample_note(self) -> list[str]:
        if self.total_trades is not None and 0 < len(self.trades) < self.total_trades:
            return ["", self.fmt("sample_note", stored=len(self.trades), total=self.total_trades)]
        return []

    def trade_diagnostics(self) -> list[str]:
        average_r = _number(self.result.get("averageR"))
        r_count = self._int(self.result.get("rMultipleTradeCount"))
        average_r_cell = (
            self.fmt("average_r_value", value=f"{average_r:.2f}", count=r_count or 0)
            if average_r is not None
            else self.no_data
        )
        rows = [
            (self.t["closed_trades"], str(self.total_trades) if self.total_trades is not None else self.no_data),
            (self.t["win_rate"], self.pct(self.pick("winRate", "win_rate"))),
            (self.t["profit_factor"], self.ratio(self.result.get("profitFactor"))),
            (self.t["average_r"], average_r_cell),
            (self.t["expectancy_before_costs"], self.money(self.result.get("expectancyBeforeCosts"))),
            (self.t["expectancy_after_costs"], self.money(self.result.get("expectancyAfterCosts"))),
        ]
        lines = [f"## {self.t['section_trades']}", "", *self.table(rows), ""]
        lines.extend(self.holding_distribution())
        lines.append("")
        lines.extend(self.excursions())
        return lines

    def holding_distribution(self) -> list[str]:
        lines = [f"### {self.t['section_holding']}", ""]
        values = [
            number
            for number in (_number(item.get("holding_seconds")) for item in self.trades)
            if number is not None
        ]
        if not values:
            return [*lines, self.no_data]
        buckets = [
            ("holding_le_1h", lambda v: v <= _HOUR),
            ("holding_1h_1d", lambda v: _HOUR < v <= _DAY),
            ("holding_1d_7d", lambda v: _DAY < v <= 7 * _DAY),
            ("holding_gt_7d", lambda v: v > 7 * _DAY),
        ]
        lines.append(f"| {self.t['holding_bucket']} | {self.t['trades']} | {self.t['share']} |")
        lines.append("| --- | --- | --- |")
        for key, matches in buckets:
            count = sum(1 for value in values if matches(value))
            lines.append(f"| {self.t[key]} | {count} | {count / len(values) * 100:.1f}% |")
        lines.append("")
        lines.extend(self.table([
            (self.t["holding_min"], self.duration(min(values))),
            (self.t["holding_median"], self.duration(median(values))),
            (self.t["holding_mean"], self.duration(self.result.get("averageHoldingSeconds"))),
            (self.t["holding_max"], self.duration(max(values))),
        ]))
        lines.extend(self._sample_note())
        return lines

    def excursions(self) -> list[str]:
        lines = [f"### {self.t['section_excursion']}", ""]

        def collect(field: str) -> list[float]:
            return [
                number
                for number in (_number(item.get(field)) for item in self.trades)
                if number is not None
            ]

        mae_pct, mfe_pct = collect("mae_pct"), collect("mfe_pct")
        mae_r, mfe_r = collect("mae_r"), collect("mfe_r")
        if not (mae_pct or mfe_pct or _number(self.result.get("averageMaePct")) is not None):
            return [*lines, self.no_data]

        def mean_r(values: list[float]) -> str:
            return f"{sum(values) / len(values):.2f}R" if values else self.no_data

        lines.extend(self.table([
            (self.t["average_mae_pct"], self.pct(self.result.get("averageMaePct"))),
            (self.t["average_mfe_pct"], self.pct(self.result.get("averageMfePct"))),
            (self.t["worst_mae_pct"], self.pct(max(mae_pct)) if mae_pct else self.no_data),
            (self.t["best_mfe_pct"], self.pct(max(mfe_pct)) if mfe_pct else self.no_data),
            (self.t["average_mae_r"], mean_r(mae_r)),
            (self.t["average_mfe_r"], mean_r(mfe_r)),
        ]))
        lines.extend(self._sample_note())
        return lines

    def execution_assumptions(self) -> list[str]:
        leverage = self.assumptions.get("leverage")
        if _number(leverage) is None:
            leverage = self.run.get("leverage")
        funding_mode = str(self.assumptions.get("fundingMode") or "")
        funding_cell = (
            self.t["funding_not_modeled"]
            if funding_mode == "not_modeled"
            else self.t["funding_mode_included"]
            if funding_mode
            else self.no_data
        )
        provenance = self.result.get("dataProvenance")
        data_kind = provenance.get("kind") if isinstance(provenance, Mapping) else None
        rows = [
            (self.t["commission_rate"], self.rate(self.assumptions.get("commission", self.run.get("commission")))),
            (self.t["slippage_rate"], self.rate(self.assumptions.get("slippage", self.run.get("slippage")))),
            (self.t["leverage"], self.ratio(leverage) + "x" if _number(leverage) is not None else self.no_data),
            (self.t["fill_rule"], self.code(self.assumptions.get("fillRule"))),
            (self.t["intrabar_mode"], self.code(self.assumptions.get("intrabarMode"))),
            (self.t["bar_close_policy"], self.code(self.assumptions.get("barClosePolicy"))),
            (self.t["protection_rule"], self.code(self.assumptions.get("protectionRule"))),
            (self.t["funding_mode"], funding_cell),
            (self.t["data_kind"], self.code(data_kind or self.run.get("data_kind"))),
        ]
        return [
            f"## {self.t['section_assumptions']}",
            "",
            *self.table(rows),
            "",
            f"### {self.t['section_limitations']}",
            "",
            *(f"- {item}" for item in self.t["limitations"]),
        ]

    def warnings(self) -> list[str]:
        items: list[str] = []
        provenance = self.result.get("dataProvenance")
        kind = str(
            (provenance.get("kind") if isinstance(provenance, Mapping) else "")
            or self.run.get("data_kind")
            or ""
        )
        if kind and kind not in ("market", "unknown"):
            items.append(self.fmt("warning_data_kind", kind=kind.replace("`", "")))
        status = str(self.result.get("benchmarkStatus") or "")
        if status == "unavailable":
            reason = str(self.result.get("benchmarkError") or "strategyV2.benchmarkDataUnavailable")
            items.append(self.fmt("warning_benchmark_unavailable", reason=reason.replace("`", "")))
        elif status == "partial":
            ratio = _number(self.result.get("benchmarkCoverageRatio"))
            items.append(self.fmt(
                "warning_benchmark_partial",
                ratio=f"{ratio * 100:.1f}%" if ratio is not None else self.no_data,
            ))
        diagnostics = self.result.get("diagnostics")
        skipped = diagnostics.get("symbolsSkipped") if isinstance(diagnostics, Mapping) else None
        for item in skipped if isinstance(skipped, list) else []:
            if isinstance(item, Mapping):
                items.append(self.fmt(
                    "warning_symbol_skipped",
                    symbol=str(item.get("symbol") or "").replace("`", ""),
                    reason=str(item.get("reason") or "").replace("`", ""),
                ))
        result_status = str(self.result.get("resultStatus") or self.run.get("result_status") or "")
        if result_status == "no_signals":
            items.append(self.t["warning_no_signals"])
        elif result_status == "open_position_only":
            items.append(self.t["warning_open_position_only"])
        if self.result.get("liquidated") is True:
            items.append(self.t["warning_liquidated"])
        if self.result.get("annualizedReturnCapped") is True:
            items.append(self.t["warning_annualized_capped"])
        if not self.has_diagnostics:
            items.append(self.t["warning_legacy_diagnostics"])
        elif self.total_trades is not None and 0 < len(self.trades) < self.total_trades:
            items.append(self.fmt("warning_sampled_history", stored=len(self.trades), total=self.total_trades))
        market_type = str(self.run.get("market_type") or "").strip().lower()
        if self.assumptions.get("fundingMode") == "not_modeled" and market_type in _FUNDING_MARKET_TYPES:
            items.append(self.t["warning_funding_not_modeled"])
        body = [f"- {item}" for item in items] if items else [self.t["no_warnings"]]
        return [f"## {self.t['section_warnings']}", "", *body]

    def strategy_version(self) -> list[str]:
        source = self.result.get("strategySource")
        version = source.get("versionNo") if isinstance(source, Mapping) else None
        params = self.run.get("params")
        params_cell = (
            self.code(json.dumps(params, ensure_ascii=False, sort_keys=True, default=str))
            if isinstance(params, Mapping) and params
            else self.no_data
        )
        rows = [
            (self.t["strategy_name"], self.plain(self.run.get("strategy_name"))),
            (self.t["source_id"], self.plain(self.run.get("source_id"))),
            (self.t["source_version"], self.plain(version)),
            (self.t["strategy_id"], self.plain(self.run.get("strategy_id"))),
            (self.t["code_hash"], self.code(self.run.get("code_hash"))),
            (self.t["engine_version"], self.code(self.run.get("engine_version"))),
            (self.t["params"], params_cell),
        ]
        return [f"## {self.t['section_version']}", "", *self.table(rows)]

    def render(self) -> str:
        sections = [
            self.header(),
            self.summary(),
            self.costs(),
            self.trade_diagnostics(),
            self.execution_assumptions(),
            self.warnings(),
            self.strategy_version(),
        ]
        return "\n\n".join("\n".join(section) for section in sections).rstrip() + "\n"


def render_backtest_report(run: Mapping[str, Any], *, language: str = DEFAULT_REPORT_LANGUAGE) -> str:
    """Render the Markdown report for one run returned by ``get_run``.

    ``run`` must be the tenant-filtered row (summary columns plus ``result``);
    the caller is responsible for ownership checks.
    """
    return _Renderer(run, resolve_report_language(language) or DEFAULT_REPORT_LANGUAGE).render()
