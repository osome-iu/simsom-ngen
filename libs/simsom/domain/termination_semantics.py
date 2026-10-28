# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Shared termination-criterion registry and result contract."""

from dataclasses import dataclass
import math
from domain.function_config import resolve_functions
from typing import Any, Callable


@dataclass(frozen=True)
class TerminationCriterionResult:
    """The result of evaluating one configured termination criterion."""

    met: bool
    function_name: str
    reason: str | None = None
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.met, bool):
            raise TypeError("met must be bool")
        if not isinstance(self.function_name, str) or not self.function_name:
            raise ValueError("function_name must be a non-empty string")
        if self.reason is not None and not isinstance(self.reason, str):
            raise TypeError("reason must be str or None")
        if self.details is not None and not isinstance(self.details, dict):
            raise TypeError("details must be a dict or None")


TerminationCriterionFunction = Callable[[Any, Any], TerminationCriterionResult]
TERMINATION_CRITERION_FUNCTIONS: dict[str, TerminationCriterionFunction] = {}


def termination_criterion_function(name: str):
    """
    Register a config-visible termination criterion function.

    Configuration supplies each criterion's tunable inputs explicitly. Evaluation
    stamps the registered name onto the result.
    See docs/algorithms/analytics.md#termination-inputs.
    """

    def decorator(function: TerminationCriterionFunction) -> TerminationCriterionFunction:
        TERMINATION_CRITERION_FUNCTIONS[name] = function
        return function

    return decorator


def allowed_termination_criterion_function_names() -> tuple[str, ...]:
    """Return all registered termination criterion selectors."""
    return tuple(sorted(TERMINATION_CRITERION_FUNCTIONS))


def criteria_function_list(functions: Any, *, allowed_functions: tuple[str, ...] | None = None,
                           field_name: str = "termination.criteria_functions") -> list[str]:
    """Validate criteria; see docs/algorithms/analytics.md#termination-inputs."""
    registry = TERMINATION_CRITERION_FUNCTIONS
    if allowed_functions is not None:
        registry = {name: registry[name] for name in allowed_functions if name in registry}
    return [name for name, _ in resolve_functions(functions, registry, field_name, runtime_count=2)]


def criteria_function_set(
    functions: Any,
    *,
    allowed_functions: tuple[str, ...] | None = None,
    field_name: str = "termination.criteria_functions",
) -> set[str]:
    """Validate configured criteria and return their distinct names."""
    return set(
        criteria_function_list(
            functions,
            allowed_functions=allowed_functions,
            field_name=field_name,
        )
    )


def evaluate_termination_criteria(evaluator: Any, metrics: Any, functions: Any) -> list[TerminationCriterionResult]:
    """Evaluate explicit criteria; see docs/algorithms/analytics.md#termination-inputs."""
    return [_with_function_name(function(evaluator, metrics), name)
            for name, function in resolve_functions(functions, TERMINATION_CRITERION_FUNCTIONS,
                                                    "termination.criteria_functions", runtime_count=2)]


@termination_criterion_function("max_avg_actions_per_user")
def configured_max_avg_actions_per_user(_evaluator: Any, metrics: Any, *, threshold: float) -> TerminationCriterionResult:
    """Limit average actions; see docs/algorithms/analytics.md#termination-inputs."""
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("threshold must be finite and positive")
    return max_avg_actions_per_user(metrics, max_avg_actions_per_user=threshold)


def max_avg_actions_per_user(
    metrics: Any,
    *,
    max_avg_actions_per_user: float,
) -> TerminationCriterionResult:
    """Return whether average actions per initialized user reached a limit.

    See docs/algorithms/analytics.md#termination-inputs.
    """
    average = average_actions_per_user(metrics)
    met = average >= max_avg_actions_per_user
    reason = (
        f"average actions per user reached: {_round_metric(average):.3f} >= "
        f"{_round_metric(max_avg_actions_per_user):.3f}"
        if met
        else None
    )
    return TerminationCriterionResult(
        met=met,
        function_name="max_avg_actions_per_user",
        reason=reason,
        details={
            "average_actions_per_user": _round_metric(average),
            "threshold": _round_metric(max_avg_actions_per_user),
        },
    )


def average_actions_per_user(metrics: Any) -> float:
    """Return persisted action count divided by all initialized users.

    See docs/algorithms/analytics.md#termination-inputs.
    """
    user_count = getattr(metrics, "user_count")
    if user_count <= 0:
        return 0.0
    return getattr(metrics, "action_count") / user_count


@termination_criterion_function("exponential_moving_average_action_quality")
def exponential_moving_average_action_quality(
    evaluator: Any,
    metrics: Any,
    *,
    threshold: float,
    rho: float,
) -> TerminationCriterionResult:
    """Return whether average quality is stable under an EMA configuration.

    See docs/algorithms/analytics.md#termination-inputs.
    """
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise TypeError("threshold must be a number")
    if not math.isfinite(threshold) or not 0 < threshold:
        raise ValueError("threshold must be greater than 0")
    if not isinstance(rho, (int, float)) or isinstance(rho, bool):
        raise TypeError("rho must be a number")
    if not 0 < rho <= 1:
        raise ValueError("rho must satisfy 0 < rho <= 1")

    details: dict[str, Any] = {
        "threshold": _round_metric(threshold),
        "rho": _round_metric(rho),
        "avg_action_quality": getattr(metrics, "avg_action_quality"),
        "ema": _round_metric(getattr(evaluator, "_avg_action_quality_ema", 1.0)),
        "quality_diff": None,
    }
    avg_quality = details["avg_action_quality"]
    previous_count = getattr(evaluator, "_avg_action_quality_action_count", 0)
    if avg_quality is None:
        details["status"] = "waiting_for_quality"
        return TerminationCriterionResult(
            False,
            "exponential_moving_average_action_quality",
            details=details,
        )
    if getattr(metrics, "action_count") <= previous_count:
        details["status"] = "waiting_for_new_actions"
        return TerminationCriterionResult(
            False,
            "exponential_moving_average_action_quality",
            details=details,
        )

    previous_ema = getattr(evaluator, "_avg_action_quality_ema", 1.0)
    new_ema = rho * previous_ema + (1.0 - rho) * avg_quality
    quality_diff = abs(new_ema - previous_ema) / previous_ema if previous_ema else 0.0
    evaluator._avg_action_quality_ema = new_ema
    evaluator._avg_action_quality_action_count = getattr(metrics, "action_count")
    details.update(
        {
            "status": "updated",
            "previous_ema": _round_metric(previous_ema),
            "ema": _round_metric(new_ema),
            "quality_diff": _round_metric(quality_diff),
        }
    )
    if quality_diff <= threshold:
        details["status"] = "met"
        return TerminationCriterionResult(
            True,
            "exponential_moving_average_action_quality",
            reason=(
                "Exponential Moving Average action quality met: "
                f"quality_diff {quality_diff:.3f} <= threshold {threshold:.3f}"
            ),
            details=details,
        )
    return TerminationCriterionResult(
        False,
        "exponential_moving_average_action_quality",
        details=details,
    )


def _with_function_name(
    result: TerminationCriterionResult,
    function_name: str,
) -> TerminationCriterionResult:
    """Return a criterion result named by the configured selector."""
    if not isinstance(result, TerminationCriterionResult):
        raise TypeError(
            "termination criterion functions must return TerminationCriterionResult"
        )
    return TerminationCriterionResult(
        met=result.met,
        function_name=function_name,
        reason=result.reason,
        details=result.details,
    )


def _round_metric(value: float) -> float:
    """Round a criterion metric to three decimal places."""
    return round(float(value), 3)
