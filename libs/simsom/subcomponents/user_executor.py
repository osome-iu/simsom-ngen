# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""User execution subcomponent."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from domain.function_config import resolve_one
import random
from typing import Any, ClassVar, Protocol

from domain.action_semantics import action_source_id, require_generated_action_content
from domain.component_semantics import (
    component_function_from_config,
    resolve_component_function,
)
from subcomponents.recsys import IN_NETWORK, OUT_NETWORK


CONTENT_EMBEDDING_GENERATION_FUNCTIONS = {}


def content_embedding_generation_function(name: str):
    """
    Register one config-visible content embedding generator.

    Declare tunable inputs as keyword-only parameters supplied by configuration.
    See docs/algorithms/configuration.md#content-functions.
    """
    def decorator(function):
        CONTENT_EMBEDDING_GENERATION_FUNCTIONS[name] = function
        return function

    return decorator


def allowed_content_embedding_generation_function_names() -> tuple[str, ...]:
    """Return accepted config labels for generated content embeddings."""
    return tuple(sorted(CONTENT_EMBEDDING_GENERATION_FUNCTIONS))


@content_embedding_generation_function("dummy_content_embedding")
def dummy_content_embedding(profile_vector: Any) -> list[float]:
    """Return a stable placeholder content vector.

    The profile vector is accepted so future generators can derive content
    embeddings from the creator profile without changing the call boundary.

    See docs/algorithms/simulation.md#user-activity-model.
    """
    del profile_vector
    return [1.0]


@content_embedding_generation_function("social_media_archive_content_embedding")
def social_media_archive_content_embedding(
    profile_vector: Any, *, gen_gamma: dict,
) -> list[float]:
    """Generate a post at a generalized-gamma cosine distance from its author.

    The transformation Gamma(a, 1)**(1/c) implements the
    generalized gamma convention used by the source notebook.
    """
    gen_gamma = _config_mapping(gen_gamma)
    a, c, loc, scale = (float(gen_gamma[key]) for key in ("a", "c", "loc", "scale"))
    if (
        not all(math.isfinite(value) for value in (a, c, loc, scale))
        or a <= 0 or c <= 0 or scale <= 0 or loc >= 2
    ):
        raise ValueError("social_media_archive_content_embedding.gen_gamma is invalid")
    profile = _require_numeric_vector("author profile", profile_vector)
    if len(profile) != 10 or not all(math.isfinite(value) for value in profile):
        raise ValueError("author profile must contain 10 finite dimensions")
    for _ in range(10000):
        distance = loc + scale * random.gammavariate(a, 1.0) ** (1.0 / c)
        if 0 <= distance <= 2:
            break
    else:
        raise ValueError("generalized gamma failed to sample a valid cosine distance")
    return generate_post_at_angle(profile, math.acos(1.0 - distance))


def generate_post_at_angle(profile_vector: Any, alpha: float, rng=None) -> list[float]:
    """Generate a 10-D post at angle alpha, preserving the author's norm.

    Project a random Gaussian direction onto the orthogonal plane,
    normalize, then rotate.
    """
    profile = _require_numeric_vector("author profile", profile_vector)
    if len(profile) != 10 or not all(math.isfinite(value) for value in profile):
        raise ValueError("author profile must contain 10 finite dimensions")
    if not math.isfinite(alpha):
        raise ValueError("post angle must be finite")
    norm = math.hypot(*profile)
    if not math.isfinite(norm):
        raise ValueError("author profile norm must be finite")
    if norm == 0:
        return profile.copy()
    alpha = min(math.pi, max(0.0, alpha))
    unit = [value / norm for value in profile]
    rng = random if rng is None else rng
    for _ in range(100):
        direction = [rng.gauss(0, 1) for _ in profile]
        projection = sum(value * axis for value, axis in zip(direction, unit))
        orthogonal = [value - projection * axis for value, axis in zip(direction, unit)]
        orthogonal_norm = math.hypot(*orthogonal)
        if orthogonal_norm > 1e-12:
            break
    else:
        raise ValueError("failed to sample a direction orthogonal to author profile")
    cosine = math.cos(alpha)
    sine = math.sin(alpha)
    return [norm * (cosine * axis + sine * value / orthogonal_norm)
            for axis, value in zip(unit, orthogonal)]


@dataclass(frozen=True)
class UserAction:
    """Action emitted by a user before the worker writes it to Postgres.

    Provenance: docs/algorithms/domain_activity.md#repost-exposure-provenance-via.
    """

    POST: ClassVar[str] = "post"
    LIKE: ClassVar[str] = "like"
    REPOST: ClassVar[str] = "repost"
    REPLY: ClassVar[str] = "reply"
    QUOTE: ClassVar[str] = "quote"

    creator_user_id: int
    action_type: str
    parent_action_id: int | None = None
    content_embedding: list[float] = field(default_factory=list)
    moderation_actions: list[dict[str, str]] = field(default_factory=list)
    other_properties: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_generated_action_content(self)
        if "via" in self.other_properties:
            via = self.other_properties["via"]
            if self.action_type != self.REPOST:
                raise ValueError("other_properties.via is only valid for repost actions")
            if isinstance(via, bool) or not isinstance(via, int) or not 0 < via < 2**63:
                raise ValueError("other_properties.via must be a positive BIGINT action ID")
            if self.parent_action_id is None or via == self.parent_action_id:
                raise ValueError("other_properties.via must identify the exposing repost, not the parent")

    @classmethod
    def post(
        cls,
        *,
        creator_user_id: int,
        content_embedding: list[float],
        moderation_actions: list[dict[str, str]] | None = None,
        other_properties: dict | None = None,
    ) -> "UserAction":
        """Create an original post action with required content."""
        return cls(
            creator_user_id=creator_user_id,
            action_type=cls.POST,
            content_embedding=content_embedding,
            moderation_actions=moderation_actions or [],
            other_properties=other_properties or {},
        )

    @classmethod
    def like(
        cls,
        *,
        parent_action_id: int,
        creator_user_id: int,
        moderation_actions: list[dict[str, str]] | None = None,
        other_properties: dict | None = None,
    ) -> "UserAction":
        """Create a like action for an existing parent action."""
        return cls(
            creator_user_id=creator_user_id,
            action_type=cls.LIKE,
            parent_action_id=parent_action_id,
            moderation_actions=moderation_actions or [],
            other_properties=other_properties or {},
        )

    @classmethod
    def repost(
        cls,
        *,
        parent_action_id: int,
        creator_user_id: int,
        moderation_actions: list[dict[str, str]] | None = None,
        other_properties: dict | None = None,
    ) -> "UserAction":
        """Create a repost action for an existing parent action."""
        return cls(
            creator_user_id=creator_user_id,
            action_type=cls.REPOST,
            parent_action_id=parent_action_id,
            moderation_actions=moderation_actions or [],
            other_properties=other_properties or {},
        )

    @classmethod
    def reply(
        cls,
        *,
        parent_action_id: int,
        creator_user_id: int,
        content_embedding: list[float] | None = None,
        moderation_actions: list[dict[str, str]] | None = None,
        other_properties: dict | None = None,
    ) -> "UserAction":
        """Create a reply action with required new content."""
        return cls(
            creator_user_id=creator_user_id,
            action_type=cls.REPLY,
            parent_action_id=parent_action_id,
            content_embedding=content_embedding or [],
            moderation_actions=moderation_actions or [],
            other_properties=other_properties or {},
        )

    @classmethod
    def quote(
        cls,
        *,
        parent_action_id: int,
        creator_user_id: int,
        content_embedding: list[float] | None = None,
        moderation_actions: list[dict[str, str]] | None = None,
        other_properties: dict | None = None,
    ) -> "UserAction":
        """Create a quote action with required new content."""
        return cls(
            creator_user_id=creator_user_id,
            action_type=cls.QUOTE,
            parent_action_id=parent_action_id,
            content_embedding=content_embedding or [],
            moderation_actions=moderation_actions or [],
            other_properties=other_properties or {},
        )


@dataclass(frozen=True)
class UserSessionResult:
    """Views and actions produced by one user session."""

    viewed_action_ids: list[int] = field(default_factory=list)
    actions: list[UserAction] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_list("viewed_action_ids", self.viewed_action_ids, int)
        for index, action_id in enumerate(self.viewed_action_ids):
            if action_id < 1:
                raise ValueError(
                    f"viewed_action_ids[{index}] must be greater than or equal to 1"
                )
        _require_list("actions", self.actions, UserAction)


class UserExecutor(Protocol):
    def execute_session(self, user, feed) -> UserSessionResult:
        """Return viewed action IDs and generated actions for one session."""


class DefaultUserExecutor:
    def __init__(self, user_behavior_config):
        """Resolve all user-side component and content generators once."""
        user_behavior = _config_mapping(user_behavior_config)
        self.feed_weight_components = _parse_weight_components(
            _required_mapping_value(
                user_behavior,
                "feed_weight_components",
                "user_behavior.feed_weight_components",
            ),
            "user_behavior.feed_weight_components",
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
        self.content_embedding_generation_function_name = _required_mapping_value(
            user_behavior,
            "content_embedding_generation_function",
            "user_behavior.content_embedding_generation_function",
        )
        self.content_embedding_generation_function = (
            _content_embedding_generation_function_from_config(
                self.content_embedding_generation_function_name
            )
        )

    def execute_session(self, user, feed) -> UserSessionResult:
        """
        Execute the current default user model.

        The viewed set is the feed prefix selected by the exponential
        view-count model. Action emission is independent of views except that
        parented actions sample targets from viewed items.
        """
        seen_items = sampled_viewed_feed_items(feed)
        viewed_action_ids = [item.action_id for item in seen_items]
        planned_action_count = getattr(user, "planned_action_count", 0)
        if planned_action_count <= 0:
            return UserSessionResult(viewed_action_ids=viewed_action_ids)

        action_types = self._sample_action_types(user, planned_action_count)
        actions: list[UserAction] = []
        for action_type in CANONICAL_ACTION_TYPES:
            action_count = action_types.count(action_type)
            if action_count <= 0:
                continue
            if action_type == UserAction.POST:
                actions.extend(
                    UserAction.post(
                        creator_user_id=user.user_id,
                        content_embedding=self._generate_content_embedding(user),
                    )
                    for _ in range(action_count)
                )
                continue
            if not seen_items:
                continue
            for target in self._sample_targets(user, seen_items, action_count):
                actions.append(
                    _parented_action(
                        action_type=action_type,
                        creator_user_id=user.user_id,
                        parent_action_id=target.action_id,
                        via_action_id=(
                            _repost_exposure_id(target)
                            if action_type == UserAction.REPOST else None
                        ),
                        content_embedding=(
                            self._generate_content_embedding(user)
                            if action_type in (UserAction.REPLY, UserAction.QUOTE)
                            else None
                        ),
                    )
                )

        return UserSessionResult(
            viewed_action_ids=viewed_action_ids,
            actions=actions,
        )

    def _sample_action_types(self, user: Any, planned_action_count: int) -> list[str]:
        """Draw the configured action types for one session.

        See docs/algorithms/simulation.md#user-activity-model.
        """
        user_behavior = _config_mapping(_required_attr(user, "user_behavior"))
        p_actions = _config_mapping(
            _required_mapping_value(user_behavior, "p_actions", "user_behavior.p_actions")
        )
        weights = [
            _require_non_negative_number(
                f"user_behavior.p_actions.{action_type}",
                _required_mapping_value(
                    p_actions,
                    action_type,
                    f"user_behavior.p_actions.{action_type}",
                )
            )
            for action_type in CANONICAL_ACTION_TYPES
        ]
        if sum(weights) <= 0.0:
            raise ValueError("user_behavior.p_actions must contain a positive weight")
        return [
            CANONICAL_ACTION_TYPES[_weighted_index(weights)]
            for _ in range(planned_action_count)
        ]

    def _sample_targets(self, user: Any, feed_items: list[Any], count: int) -> list[Any]:
        """Sample parent feed items using configured user-side feed weights."""
        if count <= 0 or not feed_items:
            return []
        pool = list(feed_items)
        selected = []
        recency_scores = _recency_scores(pool)
        weights = [
            _target_weight(
                user_item=item,
                user=user,
                components=self.feed_weight_components_by_source[
                    _network_source_for_item(item)
                ],
                recency_scores=recency_scores,
            )
            for item in pool
        ]
        while pool and len(selected) < count:
            index = _weighted_index(weights)
            selected.append(pool.pop(index))
            weights.pop(index)
        return selected

    def _generate_content_embedding(self, user: Any) -> list[float]:
        """Generate and validate a non-empty content embedding for this user."""
        embedding = self.content_embedding_generation_function(
            getattr(user, "vector_profile", None)
        )
        return _require_numeric_vector(
            "content_embedding_generation_function result",
            embedding,
        )


def execute_user_session(
    user: Any,
    feed: Any,
    user_executor: UserExecutor,
) -> UserSessionResult:
    """Execute a user and enforce shared view/action session semantics."""
    result = user_executor.execute_session(user, feed)
    _require_type("session result", result, UserSessionResult)
    if getattr(user, "planned_action_count", 0) <= 0:
        return UserSessionResult(viewed_action_ids=result.viewed_action_ids)
    return result


def sampled_viewed_feed_items(feed: Any) -> list[Any]:
    """Return the distinct feed prefix viewed by the default user model."""
    feed_items = _distinct_feed_items(feed)
    viewed_count = sample_viewed_count(len(feed_items))
    return feed_items[:viewed_count]


def sample_viewed_count(feed_size: int) -> int:
    """
    Sample the integer viewed count for a feed of size `feed_size`.

    Input is the number of available feed items. Output is `0` when the feed is
    empty, otherwise an integer in `[1, feed_size]`. The raw positive draw uses
    `p_stop = 2 / alpha` and `alpha = feed_size`, so `E[V_raw] = alpha / 2`.
    """
    if not isinstance(feed_size, int) or isinstance(feed_size, bool):
        raise TypeError(f"feed_size must be int, got {type(feed_size).__name__}")
    if feed_size < 0:
        raise ValueError("feed_size must be greater than or equal to 0")
    if feed_size == 0:
        return 0
    alpha = float(feed_size)
    p_stop = 2.0 / alpha
    raw_viewed_count = random.expovariate(p_stop)
    return min(max(1, math.ceil(raw_viewed_count)), feed_size)


def _distinct_feed_items(feed: Any) -> list[Any]:
    """Remove duplicate action IDs while preserving feed order."""
    seen_action_ids: set[int] = set()
    items = []
    for item in feed.items:
        if item.action_id in seen_action_ids:
            continue
        seen_action_ids.add(item.action_id)
        items.append(item)
    return items


CANONICAL_ACTION_TYPES = (
    UserAction.LIKE,
    UserAction.POST,
    UserAction.REPOST,
    UserAction.REPLY,
    UserAction.QUOTE,
)


def _repost_exposure_id(target: Any) -> int | None:
    """Identify the surviving repost that exposed this displayed item.

    See docs/algorithms/domain_activity.md#repost-exposure-provenance-via.
    A quoted post remains its own content target; never follow its parent.
    """
    source_type = getattr(target, "source_action_type", None)
    if source_type is None:
        source_type = (getattr(target, "other_properties", None) or {}).get("source_action_type")
    if source_type != UserAction.REPOST:
        return None
    return action_source_id(target)


def _parented_action(
    *,
    action_type: str,
    creator_user_id: int,
    parent_action_id: int,
    content_embedding: list[float] | None = None,
    via_action_id: int | None = None,
) -> UserAction:
    """Build one supported action that targets a viewed feed item."""
    if action_type == UserAction.LIKE:
        return UserAction.like(
            creator_user_id=creator_user_id,
            parent_action_id=parent_action_id,
        )
    if action_type == UserAction.REPOST:
        return UserAction.repost(
            creator_user_id=creator_user_id,
            parent_action_id=parent_action_id,
            other_properties={"via": via_action_id} if via_action_id is not None else {},
        )
    if action_type == UserAction.REPLY:
        return UserAction.reply(
            creator_user_id=creator_user_id,
            parent_action_id=parent_action_id,
            content_embedding=content_embedding,
        )
    if action_type == UserAction.QUOTE:
        return UserAction.quote(
            creator_user_id=creator_user_id,
            parent_action_id=parent_action_id,
            content_embedding=content_embedding,
        )
    raise ValueError(f"Unsupported parented action type: {action_type}")


def _target_weight(
    user_item: Any,
    user: Any,
    components: list[dict[str, Any]],
    recency_scores: dict[int, float],
) -> float:
    """Multiply the active bounded component scores for one parent target."""
    if not components:
        return 1.0
    weight = 1.0
    recency_score = recency_scores[user_item.action_id]
    for component in components:
        value = component["function"](
            user,
            user_item,
            recency_score,
        )
        bounded_value = _require_unit_interval_component_value(
            value,
            component["function_name"],
            "user_behavior.feed_weight_components",
        )
        importance = component[_importance_key_for_item(user_item)]
        weight *= bounded_value ** importance
    return max(weight, 0.0)


def _recency_scores(items: list[Any]) -> dict[int, float]:
    """Return inverse-rank scores using the surviving source event IDs."""
    newest = sorted(items, key=action_source_id, reverse=True)
    return {
        item.action_id: 1.0 / rank
        for rank, item in enumerate(newest, start=1)
    }


def _weighted_index(weights: list[float]) -> int:
    """Draw one index using non-negative weights, with a uniform fallback."""
    total = sum(weight for weight in weights if weight > 0.0)
    if total <= 0.0:
        return random.randrange(len(weights))
    threshold = random.random() * total
    running = 0.0
    for index, weight in enumerate(weights):
        if weight <= 0.0:
            continue
        running += weight
        if running >= threshold:
            return index
    return len(weights) - 1


def _parse_weight_components(value: Any, field_name: str) -> list[dict[str, Any]]:
    """Validate target-sampling component configs and return simple dicts."""
    if value is None:
        raise ValueError(f"{field_name} is required")
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} must be a non-empty list")
    components = []
    for index, item in enumerate(value):
        item_name = f"{field_name}[{index}]"
        try:
            selector, component_function = component_function_from_config(item)
            function_spec = resolve_component_function(selector, component_function)
        except ValueError as exc:
            raise ValueError(
                f"{item_name} has invalid component function"
            ) from exc
        components.append(
            {
                "component_function": function_spec.name,
                "function_name": function_spec.name,
                "function": function_spec.function,
                "importance_in": _require_non_negative_number(
                    f"{item_name}.importance_in",
                    _config_value(item, "importance_in", f"{item_name}.importance_in"),
                ),
                "importance_out": _require_non_negative_number(
                    f"{item_name}.importance_out",
                    _config_value(item, "importance_out", f"{item_name}.importance_out"),
                ),
            }
        )
    return components


def _content_embedding_generation_function_from_config(function_name: Any):
    """Bind embedding inputs; see docs/algorithms/configuration.md#content-functions."""
    return resolve_one(function_name, CONTENT_EMBEDDING_GENERATION_FUNCTIONS,
                       "user_behavior.content_embedding_generation_function")[1]


def _config_value(value: Any, key: str, field_name: str) -> Any:
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"Missing required config field: {field_name}")
        return value[key]
    if not hasattr(value, key):
        raise ValueError(f"Missing required config field: {field_name}")
    return getattr(value, key)


def _importance_key_for_item(item: Any) -> str:
    network_source = getattr(item, "network_source", OUT_NETWORK)
    if network_source == IN_NETWORK:
        return "importance_in"
    if network_source == OUT_NETWORK:
        return "importance_out"
    raise ValueError("feed item network_source must be 'in_network' or 'out_network'")


def _network_source_for_item(item: Any) -> str:
    """Return the source key used by user-side scoring."""
    return IN_NETWORK if _importance_key_for_item(item) == "importance_in" else OUT_NETWORK


def _require_unit_interval_component_value(
    value: Any,
    function_name: str,
    component_path: str,
) -> float:
    """Validate one user-side component result before weighting a target."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(
            f"{component_path}.{function_name} returned non-numeric value"
        )
    number = _require_number(f"{component_path}.{function_name}", value)
    if number < 0.0 or number > 1.0:
        raise ValueError(
            f"{component_path}.{function_name} returned {number}, expected 0 <= value <= 1"
        )
    return number


def _require_numeric_vector(name: str, value: Any) -> list[float]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    vector = []
    for index, item in enumerate(value):
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            raise TypeError(f"{name}[{index}] must be int or float")
        vector.append(float(item))
    return vector


def _config_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    raise TypeError(f"config value must be object or dict, got {type(value).__name__}")


def _required_attr(value: Any, key: str) -> Any:
    if not hasattr(value, key):
        raise ValueError(f"Missing required field: {key}")
    return getattr(value, key)


def _required_mapping_value(value: dict[str, Any], key: str, field_name: str) -> Any:
    if key not in value:
        raise ValueError(f"Missing required config field: {field_name}")
    return value[key]


def _require_non_negative_number(name: str, value: Any) -> float:
    number = _require_number(name, value)
    if number < 0.0:
        raise ValueError(f"{name} must be greater than or equal to 0")
    return number


def _require_list(name: str, value: Any, item_type: type) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be list, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, item_type):
            raise TypeError(
                f"{name}[{index}] must be {item_type.__name__}, "
                f"got {type(item).__name__}"
            )


def _require_type(name: str, value: Any, expected_type: type) -> None:
    if not isinstance(value, expected_type):
        raise TypeError(
            f"{name} must be {expected_type.__name__}, got {type(value).__name__}"
        )


def _require_number(name: str, value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be finite") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number
