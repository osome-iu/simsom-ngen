# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Registry and execution helpers for optional generated post labels."""

from dataclasses import dataclass
import random
import math
from domain.function_config import resolve_functions
from collections.abc import Mapping
from typing import Any, Callable

from domain.post_label_semantics import (
    ILLEGAL_LABEL,
    INTERNAL_SURVIVAL_DURATION_MS_LABEL,
    LEGAL_LABEL,
    LEGALITY_LABEL,
    PostLabelValue,
)


@dataclass(frozen=True)
class PostLabelingContext:
    """Inputs available to one configured post-labeling function."""

    action: Any
    user_record: Any
    other_properties: Mapping[str, Any]


PostLabelingFunction = Callable[[PostLabelingContext], Mapping[str, PostLabelValue]]
POST_LABELING_FUNCTIONS: dict[str, PostLabelingFunction] = {}
SurvivalDurationFunction = Callable[[PostLabelingContext], int]
SURVIVAL_DURATION_FUNCTIONS: dict[str, SurvivalDurationFunction] = {}


def post_labeling_function(name: str):
    """
    Register a post-labeling function under a config-visible name.

    Declare tunable inputs as keyword-only parameters supplied by configuration.
    See docs/algorithms/configuration.md#function-inputs.
    """

    def decorator(function: PostLabelingFunction) -> PostLabelingFunction:
        POST_LABELING_FUNCTIONS[name] = function
        return function

    return decorator


def allowed_post_labeling_function_names() -> tuple[str, ...]:
    """Return all accepted post-labeling selector names."""
    return tuple(sorted(POST_LABELING_FUNCTIONS))


def survival_duration_function(name: str):
    """
    Register a survival-duration sampler under a config-visible name.

    Declare tunable inputs as keyword-only parameters supplied by configuration.
    See docs/algorithms/configuration.md#function-inputs.
    """

    def decorator(function: SurvivalDurationFunction) -> SurvivalDurationFunction:
        SURVIVAL_DURATION_FUNCTIONS[name] = function
        return function

    return decorator


def allowed_survival_duration_function_names() -> tuple[str, ...]:
    """Return all accepted survival-duration selector names."""
    return tuple(sorted(SURVIVAL_DURATION_FUNCTIONS))


@post_labeling_function("legal_or_illegal_probability")
def legal_or_illegal_probability(context: PostLabelingContext, *, probability: float) -> Mapping[str, PostLabelValue]:
    """Assign legality; see docs/algorithms/policy.md#post-labeling-inputs."""
    del context
    label = ILLEGAL_LABEL if random.random() < _require_probability("probability", probability) else LEGAL_LABEL
    return {LEGALITY_LABEL: label}


def exponential_survival_duration_ms(
    *,
    mean_ms: int,
) -> int:
    """Sample a positive integer duration from an exponential distribution.

    See docs/algorithms/policy.md#post-labeling-inputs.
    """
    mean_ms = _require_positive_int("mean_ms", mean_ms)
    sampled = random.expovariate(1.0 / mean_ms)
    return max(1, round(sampled))


@survival_duration_function("exponential_survival_hours")
def exponential_survival_hours(context: PostLabelingContext, *, expected_survival_hours: float) -> int:
    """Sample duration; see docs/algorithms/policy.md#survival-time-calibration."""
    del context
    if isinstance(expected_survival_hours, bool) or not isinstance(expected_survival_hours, (int, float)):
        raise ValueError("expected_survival_hours must be numeric")
    if not math.isfinite(expected_survival_hours) or expected_survival_hours <= 0:
        raise ValueError("expected_survival_hours must be finite and positive")
    mean_ms = expected_survival_hours * 3600000
    if not math.isfinite(mean_ms):
        raise ValueError("expected_survival_hours is too large")
    return max(1, round(random.expovariate(1.0 / mean_ms)))


@post_labeling_function("illegal_probability_n_survival_hours")
def illegal_probability_n_survival_hours(
    context: PostLabelingContext, *, probability: float, expected_survival_hours: float,
) -> Mapping[str, PostLabelValue]:
    """Label illegal content; see docs/algorithms/policy.md#post-labeling-inputs.

    Calibration sources and units: policy.md#survival-time-calibration.
    Survival duration becomes an absolute simulator timestamp when persisted.
    """
    probability = _require_probability("probability", probability)
    if not math.isfinite(expected_survival_hours) or expected_survival_hours <= 0:
        raise ValueError("expected_survival_hours must be finite and positive")
    if random.random() >= probability:
        return {}
    return {LEGALITY_LABEL: ILLEGAL_LABEL, INTERNAL_SURVIVAL_DURATION_MS_LABEL:
            exponential_survival_hours(context, expected_survival_hours=expected_survival_hours)}


def resolve_post_labeling_functions(function_names: Any, field_name: str) -> tuple[tuple[str, PostLabelingFunction], ...]:
    """Bind labelers; see docs/algorithms/configuration.md#function-inputs."""
    return resolve_functions(function_names, POST_LABELING_FUNCTIONS, field_name)


def _require_probability(name: str, value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise ValueError(f"{name} must satisfy 0 <= value <= 1")
    return number


def _require_positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be greater than or equal to 1")
    return value
