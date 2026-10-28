# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Analytics metrics, reports, and the configurable termination criteria."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from domain.termination_semantics import (
    TerminationCriterionResult,
    average_actions_per_user,
    criteria_function_list,
    evaluate_termination_criteria,
)

@dataclass(frozen=True)
class AnalyticsMetrics:
    """Aggregate database metrics used by analytics and termination checks."""

    user_count: int = 0
    action_count: int = 0
    avg_activity_level: float | None = None
    avg_action_quality: float | None = None
    other_properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_negative_int("user_count", self.user_count)
        _require_non_negative_int("action_count", self.action_count)
        _require_optional_non_negative_number("avg_activity_level", self.avg_activity_level)
        _require_optional_unit_interval("avg_action_quality", self.avg_action_quality)
        _require_dict("other_properties", self.other_properties)


@dataclass(frozen=True)
class AnalyticsReport:
    """The result of one recurring check or final analytics pass."""

    metrics: AnalyticsMetrics
    termination_requested: bool = False
    termination_reason: str | None = None
    termination_criteria: list[TerminationCriterionResult] = field(default_factory=list)
    other_properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_type("metrics", self.metrics, AnalyticsMetrics)
        if not isinstance(self.termination_requested, bool):
            raise TypeError("termination_requested must be bool")
        _require_optional_str("termination_reason", self.termination_reason)
        if not isinstance(self.termination_criteria, list):
            raise TypeError("termination_criteria must be a list")
        for criterion in self.termination_criteria:
            _require_type("termination criterion", criterion, TerminationCriterionResult)
        _require_dict("other_properties", self.other_properties)

class AnalyticsAnalyzer(Protocol):
    def analyze(self, metrics: AnalyticsMetrics) -> AnalyticsReport:
        """Return analytics output for the supplied metrics.

        See docs/algorithms/analytics.md#termination-inputs.
        """


class DefaultAnalyticsAnalyzer:
    """Evaluate every configured termination criterion in order."""

    def __init__(self, termination_config: Any):
        """Validate selectors once and initialize quality EMA state."""
        self.criteria_functions = _config_value(
                termination_config,
                "criteria_functions",
                "termination.criteria_functions",
        )
        criteria_function_list(self.criteria_functions)
        self._avg_action_quality_ema = 1.0
        self._avg_action_quality_action_count = 0

    def analyze(self, metrics: AnalyticsMetrics) -> AnalyticsReport:
        """Return metrics and the results of every configured criterion.

        See docs/algorithms/analytics.md#termination-inputs.
        """
        other_properties = dict(metrics.other_properties)
        other_properties["avg_actions_per_user"] = round(
            average_actions_per_user(metrics), 3
        )
        criterion_results = evaluate_termination_criteria(
            self,
            metrics,
            self.criteria_functions,
        )
        for criterion in criterion_results:
            if criterion.details is not None:
                other_properties[criterion.function_name] = criterion.details
        met = [criterion for criterion in criterion_results if criterion.met]
        return AnalyticsReport(
            metrics=metrics,
            termination_requested=bool(met),
            termination_reason=met[0].reason if met else None,
            termination_criteria=criterion_results,
            other_properties=other_properties,
        )


def _config_value(value: Any, key: str, display_name: str) -> Any:
    """Read one required field from a dict or namespace config object."""
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"Missing required config field: {display_name}")
        return value[key]
    if not hasattr(value, key):
        raise ValueError(f"Missing required config field: {display_name}")
    return getattr(value, key)


def _require_type(name: str, value: Any, expected: type) -> None:
    if not isinstance(value, expected):
        raise TypeError(f"{name} must be {expected.__name__}")


def _require_dict(name: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a dict")


def _require_non_negative_int(name: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_optional_non_negative_number(name: str, value: Any) -> None:
    if value is not None and (
        not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0
    ):
        raise ValueError(f"{name} must be a non-negative number or None")


def _require_optional_unit_interval(name: str, value: Any) -> None:
    if value is not None and (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
        or value > 1
    ):
        raise ValueError(f"{name} must be in [0, 1] or None")


def _require_optional_str(name: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be str or None")
