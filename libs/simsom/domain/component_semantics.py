# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Shared labeled scoring component functions."""

from dataclasses import dataclass
import math
from typing import Any, Callable

from domain.content_semantics import APPEAL_PROPERTY_KEY
from domain.function_config import resolve_one

APPEAL_COMPONENT_SELECTOR = "appeal_component_function"
RECENCY_COMPONENT_SELECTOR = "recency_component_function"
ENGAGEMENT_COMPONENT_SELECTOR = "engagement_component_function"
RELEVANCE_COMPONENT_SELECTOR = "relevance_component_function"

ComponentValueFunction = Callable[[Any, Any, float], float]


@dataclass(frozen=True)
class ComponentFunctionSpec:
    """Config-visible scoring component function metadata."""

    selector: str
    name: str
    function: ComponentValueFunction


COMPONENT_FUNCTIONS_BY_SELECTOR: dict[str, dict[str, ComponentFunctionSpec]] = {}


def component_function(
    selector: str,
    name: str,
):
    """
    Attach a config selector label to a scoring component implementation.

    Declare tunable inputs as keyword-only parameters supplied by configuration.
    See docs/algorithms/configuration.md#function-inputs.
    """
    def decorator(function: ComponentValueFunction) -> ComponentValueFunction:
        spec = ComponentFunctionSpec(
            selector=selector,
            name=name,
            function=function,
        )
        COMPONENT_FUNCTIONS_BY_SELECTOR.setdefault(selector, {})[name] = spec
        return function

    return decorator


@component_function(APPEAL_COMPONENT_SELECTOR, "post_appeal")
def post_appeal_component(
    user: Any,
    item: Any,
    recency_score: float,
) -> float:
    """Return a bounded score from an item's post appeal property.

    See docs/algorithms/domain_activity.md#weight-components.
    """
    del user, recency_score
    properties = _other_properties(item)
    if APPEAL_PROPERTY_KEY not in properties:
        raise ValueError(
            f"candidate.other_properties.{APPEAL_PROPERTY_KEY} is required"
        )
    return _unit_interval_float(
        properties[APPEAL_PROPERTY_KEY],
        f"candidate.other_properties.{APPEAL_PROPERTY_KEY}",
    )


@component_function(RECENCY_COMPONENT_SELECTOR, "inverse_rank_recency")
def inverse_rank_recency_component(
    user: Any,
    item: Any,
    recency_score: float,
) -> float:
    """Return the caller-provided inverse-rank recency score.

    See docs/algorithms/domain_activity.md#weight-components.
    """
    del user, item
    return recency_score


@component_function(
    ENGAGEMENT_COMPONENT_SELECTOR,
    "global_like_count",
)
def global_like_count_component(
    user: Any,
    item: Any,
    recency_score: float,
) -> float:
    """Return a bounded global like engagement score for an item.

    See docs/algorithms/domain_activity.md#weight-components.
    """
    del user, recency_score
    return _engagement_property_score(item, "engagement_like")


@component_function(
    ENGAGEMENT_COMPONENT_SELECTOR,
    "global_repost_count",
)
def global_repost_count_component(
    user: Any,
    item: Any,
    recency_score: float,
) -> float:
    """Return a bounded global repost engagement score for an item.

    See docs/algorithms/domain_activity.md#weight-components.
    """
    del user, recency_score
    return _engagement_property_score(item, "engagement_repost")


@component_function(
    ENGAGEMENT_COMPONENT_SELECTOR,
    "global_reply_count",
)
def global_reply_count_component(
    user: Any,
    item: Any,
    recency_score: float,
) -> float:
    """Return a bounded global reply engagement score for an item.

    See docs/algorithms/domain_activity.md#weight-components.
    """
    del user, recency_score
    return _engagement_property_score(item, "engagement_reply")


@component_function(
    ENGAGEMENT_COMPONENT_SELECTOR,
    "global_quote_count",
)
def global_quote_count_component(
    user: Any,
    item: Any,
    recency_score: float,
) -> float:
    """Return a bounded global quote engagement score for an item.

    See docs/algorithms/domain_activity.md#weight-components.
    """
    del user, recency_score
    return _engagement_property_score(item, "engagement_quote")


@component_function(
    RELEVANCE_COMPONENT_SELECTOR,
    "profile_vector_n_content_embedding_cosine_relevance",
)
def profile_vector_n_content_embedding_cosine_relevance_component(
    user: Any,
    item: Any,
    recency_score: float,
) -> float:
    """Return normalized cosine relevance between user profile and content.

    See docs/algorithms/domain_activity.md#weight-components.
    """
    del recency_score
    profile_vector = _required_numeric_vector(user, "vector_profile", "user.vector_profile")
    content_embedding = _required_numeric_vector(
        item,
        "content_embedding",
        "candidate.content_embedding",
    )
    return _normalized_cosine_relevance(profile_vector, content_embedding)


def component_function_from_config(item: object) -> tuple[str, Any]:
    """
    Return the single component selector key and function name from a config item.

    A weight component entry identifies itself by exactly one selector field,
    such as `engagement_component_function`.
    """
    present = [
        selector
        for selector in COMPONENT_FUNCTIONS_BY_SELECTOR
        if _config_has_key(item, selector)
    ]
    if len(present) != 1:
        allowed = ", ".join(COMPONENT_FUNCTIONS_BY_SELECTOR)
        raise ValueError(f"component entry must contain exactly one of: {allowed}")
    selector = present[0]
    function_name = _config_value(item, selector)
    return selector, function_name


def validate_component_function(
    selector: str,
    function_name: Any,
) -> None:
    """Validate that a selector points to a supported scoring function."""
    resolve_component_function(selector, function_name)


def resolve_component_function(selector: str, function_name: Any) -> ComponentFunctionSpec:
    """Validate a selector and return its registered callable metadata.

    See docs/algorithms/domain_activity.md#weight-components.
    """
    if selector not in COMPONENT_FUNCTIONS_BY_SELECTOR:
        raise ValueError(f"Unsupported component selector: {selector}")
    registry = {name: spec.function for name, spec in COMPONENT_FUNCTIONS_BY_SELECTOR[selector].items()}
    name, function = resolve_one(function_name, registry, selector, runtime_count=3)
    return ComponentFunctionSpec(selector=selector, name=name, function=function)


def _config_has_key(item: object, key: str) -> bool:
    if isinstance(item, dict):
        return key in item
    return hasattr(item, key)


def _config_value(item: object, key: str) -> object:
    if isinstance(item, dict):
        return item[key]
    return getattr(item, key)


def _other_properties(item: Any) -> dict[str, Any]:
    properties = getattr(item, "other_properties", {})
    return properties if isinstance(properties, dict) else {}


def _engagement_property_score(
    item: Any,
    property_name: str,
) -> float:
    properties = _other_properties(item)
    if property_name not in properties:
        raise ValueError(f"candidate.other_properties.{property_name} is required")
    count = _non_negative_float(
        properties[property_name],
        f"candidate.other_properties.{property_name}",
    )
    return count / (count + 1.0)


def _non_negative_float(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{name} must be finite and greater than or equal to 0")
    return number


def _unit_interval_float(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float")
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise ValueError(f"{name} must satisfy 0 <= value <= 1")
    return number


def _required_numeric_vector(owner: Any, attribute: str, display_name: str) -> list[float]:
    vector = getattr(owner, attribute, None)
    if not isinstance(vector, list) or not vector:
        raise ValueError(f"{display_name} must be a non-empty numeric list")
    numbers: list[float] = []
    for index, value in enumerate(vector):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError(f"{display_name}[{index}] must be int or float")
        numbers.append(float(value))
    return numbers


def _normalized_cosine_relevance(
    profile_vector: list[float],
    content_embedding: list[float],
) -> float:
    if len(profile_vector) != len(content_embedding):
        raise ValueError(
            "user.vector_profile and candidate.content_embedding must have the same length"
        )
    profile_norm = math.sqrt(sum(value * value for value in profile_vector))
    content_norm = math.sqrt(sum(value * value for value in content_embedding))
    if profile_norm == 0.0 or content_norm == 0.0:
        raise ValueError(
            "user.vector_profile and candidate.content_embedding must have non-zero norm"
        )
    dot_product = sum(
        profile_value * content_value
        for profile_value, content_value in zip(profile_vector, content_embedding)
    )
    cosine = dot_product / (profile_norm * content_norm)
    return _clamp((cosine + 1.0) / 2.0, 0.0, 1.0)


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)
