# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Fixed action-budget contract for active sessions (lurkers remain view-only)."""


def fixed_actions_per_active_session(feed_size: int, actions_per_session_ratio: float) -> int:
    """Return the rounded feed-size budget for an active session.

    See docs/algorithms/domain_activity.md#session-meaning.
    """
    if not isinstance(feed_size, int) or isinstance(feed_size, bool):
        raise TypeError("recsys.feed_size must be an int")
    if feed_size < 0:
        raise ValueError("recsys.feed_size must be non-negative")
    if not isinstance(actions_per_session_ratio, (int, float)) or isinstance(
        actions_per_session_ratio, bool
    ):
        raise TypeError("user_behavior.actions_per_session_ratio must be a number")
    if not 0.0 <= actions_per_session_ratio <= 1.0:
        raise ValueError("user_behavior.actions_per_session_ratio must be between 0 and 1")
    budget = round(feed_size * actions_per_session_ratio)
    if budget < 1:
        raise ValueError(
            "recsys.feed_size and user_behavior.actions_per_session_ratio must "
            "produce at least one action per active session"
        )
    return budget
