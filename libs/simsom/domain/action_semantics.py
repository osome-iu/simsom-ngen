# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Shared action-type semantics used across worker, policy, and analytics."""

from typing import Any

CONTENT_ACTION_TYPES = ("post", "reply", "quote")
NON_CONTENT_ACTION_TYPES = ("like", "repost")
ALL_ACTION_TYPES = CONTENT_ACTION_TYPES + NON_CONTENT_ACTION_TYPES


def action_source_id(action: Any) -> int:
    """Identify the inventory event, which may be a repost of older content."""
    source_id = getattr(action, "source_action_id", None)
    if source_id is None:
        source_id = (getattr(action, "other_properties", None) or {}).get(
            "source_action_id"
        )
    return action.action_id if source_id is None else int(source_id)


def is_content_action_type(action_type: str) -> bool:
    """Return whether an action type creates user-visible content."""
    return action_type in CONTENT_ACTION_TYPES


def is_non_content_action_type(action_type: str) -> bool:
    """Return whether an action type is an engagement action without content."""
    return action_type in NON_CONTENT_ACTION_TYPES


def require_supported_action_type(action_type: str) -> str:
    """Validate a known simulator action type and return it unchanged."""
    if action_type not in ALL_ACTION_TYPES:
        allowed = ", ".join(ALL_ACTION_TYPES)
        raise ValueError(f"action_type must be one of: {allowed}")
    return action_type


def content_action_type_sql(table_alias: str) -> str:
    """Return SQL that matches rows whose action type creates content."""
    return f"{table_alias}.action_type IN ('post', 'reply', 'quote')"


def supported_action_type_sql(table_alias: str) -> str:
    """Return SQL that matches rows with any supported simulator action type."""
    return (
        f"{table_alias}.action_type "
        "IN ('post', 'reply', 'quote', 'like', 'repost')"
    )


def require_generated_action_content(action: Any) -> None:
    """
    Reject generated content actions that have no content embedding.

    The simulator treats posts, replies, and quotes as content-bearing. A
    content-generation failure is therefore a data error, not a contentless
    action variant.
    """
    action_type = getattr(action, "action_type", None)
    if not is_content_action_type(action_type):
        return
    if getattr(action, "content_embedding", None):
        return
    raise ValueError(f"{action_type} actions must carry content_embedding")
