# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Data transfer objects shared across multiple DB clients.

Keep this file limited to records that mirror the simulator's shared tables:
user, action, and follower_network.
"""

from dataclasses import dataclass, field
from typing import Any

from db.core.schema import MAX_INTEGER_USER_ID


def _require_user_id(name: str, value: int) -> None:
    """Validate a persisted user identifier for PostgreSQL INTEGER storage."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if not 1 <= value <= MAX_INTEGER_USER_ID:
        raise ValueError(
            f"{name} must be between 1 and {MAX_INTEGER_USER_ID}"
        )


@dataclass(frozen=True, slots=True)
class UserRecord:
    """
    Shared `user` row shape used across setup, controller, and workers.

    `user_behavior` carries sampled behavior values such as `p_actions` and
    per-user quality. `moderation_actions` remains policy-owned.
    """

    user_id: int
    user_type: str
    vector_profile: list[float] = field(default_factory=list)
    activity_level: float = 0.0
    other_properties: dict[str, Any] = field(default_factory=dict)
    user_behavior: dict[str, Any] = field(default_factory=dict)
    moderation_actions: Any = None

    def __post_init__(self) -> None:
        _require_user_id("user_id", self.user_id)
        if not isinstance(self.activity_level, (int, float)) or isinstance(
            self.activity_level,
            bool,
        ):
            raise TypeError(
                f"activity_level must be int or float, got {type(self.activity_level).__name__}"
            )
        if self.activity_level < 0:
            raise ValueError("activity_level must be greater than or equal to 0")


@dataclass(frozen=True, slots=True)
class ActionRow:
    """
    Shared `action` row shape read back from Postgres.

    Content actions generate post-level `quality`, `appeal`, and labels inside
    `other_properties`. Non-content actions can carry the same descriptive and
    policy metadata by inheriting it from their parent action.
    """

    action_id: int
    creator_user_id: int
    action_type: str
    timestamp: int | None = None
    parent_action_id: int | None = None
    content_embedding: list[float] = field(default_factory=list)
    moderation_actions: Any = None
    repeated_like_n_reshare_creator_count: int = 0
    views_by_others_count: int = 0
    other_properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FollowerNetworkRecord:
    """Directed friend edge: `user_id` follows or has added `friend_id`."""

    user_id: int
    friend_id: int

    def __post_init__(self) -> None:
        _require_user_id("user_id", self.user_id)
        _require_user_id("friend_id", self.friend_id)
