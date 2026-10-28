# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Recommendation/feed-generation subcomponent.

Owned by the RecSys team. It converts a user profile plus candidate actions into
an ordered feed consumed by the user-execution subcomponent.
"""

from dataclasses import dataclass, field
import math
from typing import Any
from typing import Protocol

from domain.action_semantics import action_source_id
from domain.component_semantics import (
    component_function_from_config,
    resolve_component_function,
)

IN_NETWORK = "in_network"
OUT_NETWORK = "out_network"


@dataclass(frozen=True)
class UserProfile:
    """
    Worker-facing user profile for one session.

    `planned_action_count` is the fixed active-session action budget and zero
    for a zero-activity view-only session.
    """

    user_id: int
    user_type: str
    # Debug/tracing id for this claimed session. It is not the user identity;
    # it identifies one execution of that user in the worker pipeline.
    session_id: str | int | None = None
    activity_level: float = 0.0
    planned_action_count: int = 0
    vector_profile: list[float] = field(default_factory=list)
    user_behavior: dict[str, Any] = field(default_factory=dict)
    other_properties: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FeedCandidate:
    action_id: int
    creator_user_id: int
    action_type: str
    parent_action_id: int | None = None
    content_embedding: list[float] = field(default_factory=list)
    moderation_actions: Any = None
    repeated_like_n_reshare_creator_count: int = 0
    views_by_others_count: int = 0
    other_properties: dict = field(default_factory=dict)
    timestamp: int | None = None
    network_source: str = OUT_NETWORK
    source_action_id: int | None = None
    source_creator_user_id: int | None = None
    source_action_type: str | None = None
    source_timestamp: int | None = None


@dataclass(frozen=True)
class RankedFeed:
    items: list[FeedCandidate] = field(default_factory=list)


class FeedBuilder(Protocol):
    def build_feed(self, user: UserProfile, candidates: list[FeedCandidate]) -> RankedFeed:
        """Return an ordered feed for one user session.

        See docs/algorithms/worker.md#selection-and-feed-math.
        """


class DefaultFeedBuilder:
    def __init__(self, recsys_config):
        """Resolve and validate feed components once for this worker."""
        self.feed_size = _require_non_negative_int(
            "recsys.feed_size",
            _config_value(recsys_config, "feed_size"),
        )
        self.feed_weight_components = _parse_weight_components(
            _config_value(recsys_config, "feed_weight_components"),
            "recsys.feed_weight_components",
        )
        self.feed_weight_components_by_source = {
            IN_NETWORK: [
                component
                for component in self.feed_weight_components
                if component["importance_in"] > 0.0
            ],
            OUT_NETWORK: [
                component
                for component in self.feed_weight_components
                if component["importance_out"] > 0.0
            ],
        }
    def build_feed(self, user: UserProfile, candidates: list[FeedCandidate]) -> RankedFeed:
        """Deduplicate combined inventories, rank once, then cap the feed.

        See docs/algorithms/worker.md#selection-and-feed-math.
        """
        _require_type("user", user, UserProfile)
        _require_list("candidates", candidates, FeedCandidate)
        unique: dict[int, FeedCandidate] = {}
        for candidate in candidates:
            source = self._candidate_network_source(candidate)
            previous = unique.get(candidate.action_id)
            if previous is None or (
                source == IN_NETWORK and previous.network_source == OUT_NETWORK
            ) or (
                source == previous.network_source
                and action_source_id(candidate) > action_source_id(previous)
            ):
                unique[candidate.action_id] = candidate
        return RankedFeed(
            items=self._ranked_candidates(user, list(unique.values()))[:self.feed_size]
        )

    @staticmethod
    def _newest_first(candidates: list[FeedCandidate]) -> list[FeedCandidate]:
        """Order by the surviving source event, including a repost's own ID.

        See docs/algorithms/worker.md#selection-and-feed-math.
        """
        return sorted(candidates, key=action_source_id, reverse=True)

    @staticmethod
    def _candidate_network_source(candidate: FeedCandidate) -> str:
        """Validate and return the candidate's in/out network source."""
        if candidate.network_source not in (IN_NETWORK, OUT_NETWORK):
            raise ValueError(
                "FeedCandidate.network_source must be 'in_network' or 'out_network'"
            )
        return candidate.network_source

    def _ranked_candidates(
        self,
        user: UserProfile,
        candidates: list[FeedCandidate],
    ) -> list[FeedCandidate]:
        """Return candidates ranked only by their component weight W(i).

        See docs/algorithms/worker.md#selection-and-feed-math.
        """
        if not candidates:
            return []
        recency_scores = self._recency_scores(candidates)
        return sorted(
            candidates,
            key=lambda candidate: self._candidate_weight(
                user,
                candidate,
                recency_scores,
            ),
            reverse=True,
        )

    def _candidate_weight(
        self,
        user: UserProfile,
        candidate: FeedCandidate,
        recency_scores: dict[int, float],
    ) -> float:
        """Multiply the active bounded component scores for one candidate."""
        weight = 1.0
        source = self._candidate_network_source(candidate)
        recency_score = recency_scores[id(candidate)]
        for component in self.feed_weight_components_by_source[source]:
            value = component["function"](
                user,
                candidate,
                recency_score, # only used by inverse_rank_recency, here for consistency
            )
            bounded_value = _require_unit_interval_component_value(
                value,
                component["function_name"],
                "recsys.feed_weight_components",
            )
            importance = component[_importance_key_for_source(source)]
            weight *= bounded_value ** importance
        return max(weight, 0.0)

    @classmethod
    def _recency_scores(cls, candidates: list[FeedCandidate]) -> dict[int, float]:
        """Return inverse-rank scores keyed by candidate identity."""
        newest = cls._newest_first(candidates)
        return {
            id(candidate): 1.0 / rank
            for rank, candidate in enumerate(newest, start=1)
        }


def _config_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"Missing required config field: {key}")
        return value[key]
    if not hasattr(value, key):
        raise ValueError(f"Missing required config field: {key}")
    return getattr(value, key)


def _require_non_negative_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")
    return value


def _require_number(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool): # float(True) = 1.0
        raise TypeError(f"{name} must be int or float, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be finite") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _require_non_negative_number(value: Any, name: str) -> float:
    number = _require_number(value, name)
    if number < 0.0:
        raise ValueError(f"{name} must be greater than or equal to 0")
    return number


def _parse_weight_components(value: Any, name: str) -> list[dict[str, float | str]]:
    """Validate feed ranking component configs and return simple dicts."""
    if not isinstance(value, list) or not value:
        raise TypeError(f"{name} must be a non-empty list")
    components = []
    for index, item in enumerate(value):
        item_name = f"{name}[{index}]"
        try:
            selector, component_function = component_function_from_config(item)
            function_spec = resolve_component_function(selector, component_function)
        except ValueError as exc:
            raise ValueError(f"{item_name} has invalid component function") from exc
        components.append(
            {
                "component_function": function_spec.name,
                "function_name": function_spec.name,
                "function": function_spec.function,
                "importance_in": _require_non_negative_number(
                    _config_value(item, "importance_in"),
                    f"{item_name}.importance_in",
                ),
                "importance_out": _require_non_negative_number(
                    _config_value(item, "importance_out"),
                    f"{item_name}.importance_out",
                ),
            }
        )
    return components


def _importance_key_for_source(network_source: str) -> str:
    """Return the importance field used for one inventory source."""
    if network_source == IN_NETWORK:
        return "importance_in"
    if network_source == OUT_NETWORK:
        return "importance_out"
    raise ValueError("network_source must be 'in_network' or 'out_network'")


def _require_unit_interval_component_value(
    value: Any,
    function_name: str,
    component_path: str,
) -> float:
    """Validate one scoring result before it enters the weight product."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(
            f"{component_path}.{function_name} returned non-numeric value"
        )
    number = _require_number(value, f"{component_path}.{function_name}")
    if number < 0.0 or number > 1.0:
        raise ValueError(
            f"{component_path}.{function_name} returned {number}, expected 0 <= value <= 1"
        )
    return number


def _require_type(name: str, value: Any, expected_type: type) -> None:
    if not isinstance(value, expected_type):
        raise TypeError(
            f"{name} must be {expected_type.__name__}, got {type(value).__name__}"
        )


def _require_list(name: str, value: Any, item_type: type) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be list, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, item_type):
            raise TypeError(
                f"{name}[{index}] must be {item_type.__name__}, "
                f"got {type(item).__name__}"
            )
