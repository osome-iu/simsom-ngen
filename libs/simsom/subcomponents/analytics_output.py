# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Structured analytics function execution and JSONL output helpers."""

from datetime import datetime, timezone
import json
from pathlib import Path
from domain.function_config import resolve_functions
from typing import Any, Callable

from domain.termination_semantics import average_actions_per_user
from subcomponents.analytics_report import AnalyticsMetrics


AnalyticsFunction = Callable[[AnalyticsMetrics], Any]
ANALYTICS_FUNCTIONS: dict[str, AnalyticsFunction] = {}


def analytics_function(name: str):
    """
    Register a config-visible analytics function under one selector name.

    Declare tunable inputs as keyword-only parameters supplied by configuration.
    See docs/algorithms/analytics.md#configurable-outputs.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("analytics function name must be a non-empty string")

    def decorator(function: AnalyticsFunction) -> AnalyticsFunction:
        if name in ANALYTICS_FUNCTIONS:
            raise ValueError(f"analytics function already registered: {name}")
        ANALYTICS_FUNCTIONS[name] = function
        return function

    return decorator


def allowed_analytics_function_names() -> tuple[str, ...]:
    """Return all registered analytics selector names."""
    return tuple(sorted(ANALYTICS_FUNCTIONS))


class AnalyticsFunctionRunner:
    """Resolve and run recurring and final analytics function groups."""

    def __init__(self, analytics_config: Any):
        """Resolve both configured function groups once at process startup."""
        self.recurring_functions = self._resolve_group(
            _config_value(analytics_config, "recurring_functions"),
            "analytics.recurring_functions",
        )
        self.final_functions = self._resolve_group(
            _config_value(analytics_config, "final_functions"),
            "analytics.final_functions",
        )

    def run_recurring(self, metrics: Any) -> dict[str, Any]:
        """Run the configured recurring functions for one lightweight pass.

        See docs/algorithms/analytics.md#configurable-outputs.
        """
        return self._run(self.recurring_functions, metrics)

    def run_final(self, metrics: Any) -> dict[str, Any]:
        """Run the configured final functions for the stable final state.

        See docs/algorithms/analytics.md#configurable-outputs.
        """
        return self._run(self.final_functions, metrics)

    @staticmethod
    def _resolve_group(value: Any, field_name: str) -> list[tuple[str, AnalyticsFunction]]:
        """Bind analytics inputs; see docs/algorithms/analytics.md#configurable-outputs."""
        return list(resolve_functions(value, ANALYTICS_FUNCTIONS, field_name))

    @staticmethod
    def _run(
        functions: list[tuple[str, AnalyticsFunction]],
        metrics: Any,
    ) -> dict[str, Any]:
        """Run functions in config order and require JSON-compatible outputs."""
        outputs: dict[str, Any] = {}
        for name, function in functions:
            output = function(metrics)
            try:
                json.dumps(output, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise TypeError(
                    f"analytics function {name} returned a non-JSON-serializable value"
                ) from exc
            outputs[name] = output
        return outputs


class AnalyticsJsonlWriter:
    """Write recurring termination and analytics records as flushed JSONL."""

    def __init__(self, log_directory: str | Path, termination_file: str, analytics_file: str):
        """Create fresh output files beneath the configured log directory."""
        self.termination_path = resolve_analytics_output_path(
            log_directory,
            termination_file,
        )
        self.analytics_path = resolve_analytics_output_path(
            log_directory,
            analytics_file,
        )
        if self.termination_path == self.analytics_path:
            raise ValueError("termination and analytics JSONL files must be different")
        self.termination_path.parent.mkdir(parents=True, exist_ok=True)
        self.analytics_path.parent.mkdir(parents=True, exist_ok=True)
        self._termination_stream = self.termination_path.open("w", encoding="utf-8")
        self._analytics_stream = self.analytics_path.open("w", encoding="utf-8")

    def write_termination(self, report: Any) -> None:
        """Write one lightweight termination-check record and flush it."""
        metrics = report.metrics
        payload = {
            "record_type": "termination_check",
            "recorded_at": _now_utc(),
            "action_count": metrics.action_count,
            "avg_action_quality": metrics.avg_action_quality,
            "avg_actions_per_user": average_actions_per_user(metrics),
            "termination_requested": report.termination_requested,
            "termination_reason": report.termination_reason,
            "termination_criteria": _criterion_payload(report.termination_criteria),
        }
        self._write_line(self._termination_stream, payload)

    def write_analytics(self, phase: str, metrics: Any, outputs: dict[str, Any]) -> None:
        """Write built-in snapshot metrics plus configured outputs, then flush.

        Empty function groups leave ``outputs`` empty, not the built-in metrics.
        See docs/algorithms/analytics.md#configurable-outputs.
        """
        if phase not in ("recurring", "final"):
            raise ValueError("analytics phase must be recurring or final")
        payload = {
            "record_type": "analytics_output",
            "phase": phase,
            "recorded_at": _now_utc(),
            "action_count": metrics.action_count,
            "avg_action_quality": metrics.avg_action_quality,
            "avg_actions_per_user": average_actions_per_user(metrics),
            "outputs": outputs,
        }
        self._write_line(self._analytics_stream, payload)

    def close(self) -> None:
        """Flush and close both structured output files."""
        self._termination_stream.close()
        self._analytics_stream.close()

    @staticmethod
    def _write_line(stream: Any, payload: dict[str, Any]) -> None:
        """Serialize one record as a single JSONL line and flush it."""
        stream.write(
            json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n"
        )
        stream.flush()


def resolve_analytics_output_path(log_directory: str | Path, configured_file: str) -> Path:
    """Resolve an analytics file relative to the configured log directory."""
    path = Path(configured_file).expanduser()
    return path if path.is_absolute() else Path(log_directory).expanduser() / path


def _config_value(value: Any, key: str) -> Any:
    """Read one field from a dictionary or config namespace."""
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"Missing required config field: analytics.{key}")
        return value[key]
    if not hasattr(value, key):
        raise ValueError(f"Missing required config field: analytics.{key}")
    return getattr(value, key)


def _criterion_payload(criteria: list[Any]) -> list[dict[str, Any]]:
    """Convert typed termination results to JSON-ready dictionaries."""
    return [
        {
            "met": criterion.met,
            "function_name": criterion.function_name,
            "reason": criterion.reason,
            "details": criterion.details,
        }
        for criterion in criteria
    ]


def _now_utc() -> str:
    """Return an ISO-8601 UTC timestamp for one output record."""
    return datetime.now(timezone.utc).isoformat()
