# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Worker-owned DB client.

Workers claim user sessions, read candidate actions for feed construction, and
write resulting actions back to Postgres.
"""

from dataclasses import dataclass, field
import json
import math
import random
from typing import Any, Callable, Collection, Protocol, Sequence
import uuid

from db.core import (
    ACTION_TABLE,
    ActionRow,
    build_action_advisory_lock_sql,
    DatabaseSettings,
    FOLLOWER_NETWORK_TABLE,
    LifecycleDatabase,
    PostgresClientBase,
    PostgresLifecycleMixin,
    USER_TABLE,
    UserRecord,
    qualified_name,
    sql_bigint,
    sql_float,
    sql_integer,
    sql_json,
    sql_text,
)
from domain.action_semantics import action_source_id, is_content_action_type
from db.core.schema import build_lock_action_clock_sql
from domain.function_config import resolve_one
from domain.post_label_semantics import (
    INTERNAL_SURVIVAL_DURATION_MS_LABEL,
    POST_LABELS_PROPERTY,
    SURVIVAL_TIME_LABEL,
    merge_post_labels,
)
from subcomponents.policy_annotations import (
    feed_exclusion_moderation_filter_sql,
    moderation_actions_payload,
    user_duplicate_reaction_cleanup_intervention,
    validate_moderation_actions,
)
from subcomponents.post_labeling import (
    PostLabelingContext,
    PostLabelingFunction,
    resolve_post_labeling_functions,
)
from domain.content_semantics import APPEAL_PROPERTY_KEY, QUALITY_PROPERTY_KEY
from subcomponents.user_executor import UserAction


@dataclass(frozen=True)
class _ContentPropertySpec:
    """
    Bridge from a persisted user scalar to one generated content property.

    The configured selector for each user type must name a registered post
    distribution wrapper.
    """

    name: str
    base_user_property: str


POST_DISTRIBUTION_FUNCTIONS: dict[
    str,
    Callable[[Any, _ContentPropertySpec], float],
] = {}


def post_distribution_function(name: str):
    """
    Attach a config selector label to a post property generator.

    Declare tunable inputs as keyword-only parameters supplied by configuration.
    See docs/algorithms/configuration.md#content-functions.
    """

    def decorator(
        function: Callable[[Any, _ContentPropertySpec], float],
    ) -> Callable[[Any, _ContentPropertySpec], float]:
        POST_DISTRIBUTION_FUNCTIONS[name] = function
        return function

    return decorator


@post_distribution_function("gaussian_around_user_quality")
def gaussian_around_user_quality(
    behavior: Any,
    spec: _ContentPropertySpec,
    *,
    stddev: float,
) -> float:
    """Generate a post-level property from a Gaussian around creator quality.

    Configuration supplies the standard deviation explicitly. The function
    validates inputs, samples and clamps the result.

    See docs/algorithms/simulation.md#quality-and-appeal-generation.
    """
    stddev = _require_number(stddev, "stddev")
    if not math.isfinite(stddev) or stddev < 0:
        raise ValueError("stddev must be finite and nonnegative")
    behavior_config = _config_mapping(behavior)
    user_value = _require_unit_interval(
        _required_config_value(
            behavior_config,
            spec.base_user_property,
            f"user_behavior.{spec.base_user_property}",
        ),
        f"user_behavior.{spec.base_user_property}",
    )
    return _clamp_unit_interval(random.gauss(user_value, stddev))

QUALITY_PROPERTY = _ContentPropertySpec(
    name=QUALITY_PROPERTY_KEY,
    base_user_property=QUALITY_PROPERTY_KEY,
)
APPEAL_PROPERTY = _ContentPropertySpec(
    name=APPEAL_PROPERTY_KEY,
    base_user_property=QUALITY_PROPERTY_KEY,
)
INHERITED_OTHER_PROPERTIES_EXCLUDED_KEYS = ("session_id", "via")
INHERITED_OTHER_PROPERTIES_EXCLUDED_PREFIXES = (
    "source_",
    "materialized_",
    "engagement_",
)


def allowed_post_distribution_function_names() -> tuple[str, ...]:
    """Return config-visible post property generator names."""
    return tuple(POST_DISTRIBUTION_FUNCTIONS)


def _inheritable_other_property_filter_sql(key_expression: str) -> str:
    """
    Return a SQL predicate for parent metadata that can be copied to a child.

    Non-content child actions inherit descriptive and policy metadata from
    their parent, while row-local trace and materialization fields stay on the
    row where they were produced.
    """
    conditions = [
        f"{key_expression} <> {sql_text(key)}"
        for key in INHERITED_OTHER_PROPERTIES_EXCLUDED_KEYS
    ]
    conditions.extend(
        f"LEFT({key_expression}, {len(prefix)}) <> {sql_text(prefix)}"
        for prefix in INHERITED_OTHER_PROPERTIES_EXCLUDED_PREFIXES
    )
    return "\n                                AND ".join(conditions)


@dataclass(frozen=True)
class ClaimedUserSessionRecord:
    """
    Worker-facing claimed unit of work.

    This boundary is about worker execution, not raw table shape. `user_id`
    identifies the persisted user being processed, while `session_id`
    identifies the claimed work item for tracing and debugging.

    `planned_action_count = 0` means the session can view feed items but
    should not generate actions.

    Multiple sessions for the same `user_id` may coexist. The worker layer
    does not enforce per-user exclusivity; DB write/aggregation paths are
    responsible for consolidating generated state.
    """

    user_id: int
    # Unique id for one claimed execution slot. Use this to trace one user
    # session across claim, feed generation, action execution, and DB writes.
    session_id: str | int
    user_record: UserRecord
    planned_action_count: int = 0

    def __post_init__(self) -> None:
        _require_positive_int("user_id", self.user_id)
        _require_str_or_int("session_id", self.session_id)
        _require_type("user_record", self.user_record, UserRecord)
        _require_non_negative_int("planned_action_count", self.planned_action_count)


@dataclass(frozen=True)
class ActionViewRecord:
    """One aggregate view exposure from a worker session to an action."""

    session_id: str | int
    viewer_user_id: int
    action_id: int

    def __post_init__(self) -> None:
        _require_str_or_int("session_id", self.session_id)
        _require_positive_int("viewer_user_id", self.viewer_user_id)
        _require_positive_int("action_id", self.action_id)


@dataclass(frozen=True)
class ActionWriteRecord:
    """One generated action plus the session/user data needed to write it."""

    # Carries the originating claimed session so DB rows can be traced back
    # to the exact worker work item that produced them.
    session_id: str | int
    action: UserAction
    user_record: UserRecord | None = None

    def __post_init__(self) -> None:
        _require_str_or_int("session_id", self.session_id)
        _require_type("action", self.action, UserAction)
        if self.user_record is not None:
            _require_type("user_record", self.user_record, UserRecord)


@dataclass(frozen=True)
class FeedInventoryContext:
    """
    Feed inventories for one user execution.

    Both inventories are fetched once per worker batch. The in-network list is
    filtered from the shared batch result before this context is built.
    """

    in_network_actions: list[ActionRow] = field(default_factory=list)
    out_network_actions: list[ActionRow] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_list("in_network_actions", self.in_network_actions, ActionRow)
        _require_list("out_network_actions", self.out_network_actions, ActionRow)


@dataclass(frozen=True)
class BatchInNetworkInventory:
    """Shared in-network actions; viewer eligibility is keyed by source event ID."""

    actions: list[ActionRow] = field(default_factory=list)
    visible_to_user_ids: dict[int, frozenset[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_list("in_network_actions", self.actions, ActionRow)
        if not isinstance(self.visible_to_user_ids, dict):
            raise TypeError("visible_to_user_ids must be a dict")
        action_ids = {action_source_id(action) for action in self.actions}
        for action_id, user_ids in self.visible_to_user_ids.items():
            if action_id not in action_ids:
                raise ValueError(
                    f"visible_to_user_ids contains unknown action_id {action_id}"
                )
            if not isinstance(user_ids, frozenset):
                raise TypeError("visible_to_user_ids values must be frozenset")
            for user_id in user_ids:
                _require_positive_int("visible user id", user_id)

    def for_user(self, user_id: int) -> list[ActionRow]:
        """Return shared actions visible to one batch user, preserving query order."""
        _require_positive_int("user_id", user_id)
        return [
            action
            for action in self.actions
            if user_id in self.visible_to_user_ids.get(action_source_id(action), frozenset())
        ]


class WorkerDatabase(LifecycleDatabase, Protocol):
    def claim_user_batch(
        self,
        batch_size: int,
        *,
        lurker_ratio: float,
        planned_action_count: int,
    ) -> list[ClaimedUserSessionRecord]:
        """Claim and return the next worker batch of user sessions.

        The batch may contain more than one session for the same `user_id`.

        See docs/algorithms/simulation.md#user-batch-algorithm.
        """

    def fetch_out_network_feed_inventory(
        self,
        *,
        limit: int,
    ) -> list[ActionRow]:
        """Return the batch-level global recent-action inventory.

        See docs/algorithms/worker.md#inventory-rules.
        """

    def fetch_in_network_feed_inventory(
        self,
        claimed_sessions: Sequence[ClaimedUserSessionRecord],
        *,
        limit: int,
    ) -> BatchInNetworkInventory:
        """Return one shared followed-user inventory for a worker batch.

        See docs/algorithms/worker.md#inventory-rules.
        """

    def write_actions(self, actions: Sequence[ActionWriteRecord]) -> None:
        """Persist generated action rows and any related table updates.

        See docs/algorithms/worker.md#action-write-rules.
        """

    def write_action_views(self, views: Sequence[ActionViewRecord]) -> None:
        """Increment aggregate view counters for viewed action rows."""


class PostgresWorkerDatabase(PostgresLifecycleMixin, PostgresClientBase):
    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        action_insert_chunk_size: int,
        user_types_config: Any,
        recsys_config: Any,
    ):
        """Store DB settings and validate per-user-type post generators."""
        super().__init__(settings)
        self.action_insert_chunk_size = _require_positive_int(
            "worker.action_insert_chunk_size",
            action_insert_chunk_size,
        )
        self.user_types_config = _required_non_empty_mapping(
            user_types_config,
            "setup_db.user_types",
        )
        self.engagement_action_lookback_population_multiplier = (
            _engagement_action_lookback_population_multiplier(recsys_config)
        )
        _validate_user_type_post_distribution_functions(self.user_types_config)
        self.post_labeling_functions_by_user_type = (
            _resolve_user_type_post_labeling_functions(self.user_types_config)
        )

    def claim_user_batch(
        self,
        batch_size: int,
        *,
        lurker_ratio: float,
        planned_action_count: int,
    ) -> list[ClaimedUserSessionRecord]:
        """Return active sessions plus zero-activity users required by `lurker_ratio`.

        See docs/algorithms/simulation.md#user-batch-algorithm.
        """
        limit = _require_non_negative_int("batch_size", batch_size)
        lurker_ratio = _require_lurker_ratio(lurker_ratio)
        planned_action_count = _require_positive_int(
            "planned_action_count",
            planned_action_count,
        )
        if limit == 0:
            return []

        user_table = self._qualified_table(USER_TABLE)
        active_sql = f"""
        SELECT COALESCE(json_agg(row_to_json(user_row)), '[]'::json)::text
        FROM (
            SELECT
                user_id,
                vector_profile,
                activity_level,
                user_type,
                user_behavior,
                moderation_actions,
                other_properties
            FROM {user_table}
            WHERE activity_level > 0
            ORDER BY
                -LN(GREATEST(random(), 1e-12)) / activity_level
            LIMIT {limit}
        ) AS user_row;
        """
        active_rows = self._load_json_rows(
            self.run_psql(active_sql, capture_output=True)
        )
        active_sessions = [
            self._claimed_session_from_user_row(row, planned_action_count)
            for row in active_rows
        ]

        nz_count = sum(
            1 for session in active_sessions if session.planned_action_count >= 1
        )
        zero_activity_count = self._planned_zero_activity_count(nz_count, lurker_ratio)
        zero_activity_sessions: list[ClaimedUserSessionRecord] = []
        if zero_activity_count > 0:
            zero_activity_sql = self._build_zero_activity_claim_sql(zero_activity_count)
            zero_activity_rows = self._load_json_rows(
                self.run_psql(zero_activity_sql, capture_output=True)
            )
            zero_activity_sessions = [
                self._claimed_session_from_user_row(row, 0)
                for row in zero_activity_rows
            ]

        sessions = active_sessions + zero_activity_sessions
        _require_list("claimed sessions", sessions, ClaimedUserSessionRecord)
        return sessions

    def _build_zero_activity_claim_sql(self, zero_activity_count: int) -> str:
        """Build SQL that samples lurker sessions for the current batch."""
        user_table = self._qualified_table(USER_TABLE)
        return f"""
        SELECT COALESCE(json_agg(row_to_json(user_row)), '[]'::json)::text
        FROM (
            SELECT
                user_id,
                vector_profile,
                activity_level,
                user_type,
                user_behavior,
                moderation_actions,
                other_properties
            FROM {user_table}
            WHERE activity_level = 0
            ORDER BY random()
            LIMIT {zero_activity_count}
        ) AS user_row;
        """

    def _claimed_session_from_user_row(
        self,
        row: dict[str, Any],
        planned_action_count: int,
    ) -> ClaimedUserSessionRecord:
        """Create one worker session from a persisted user row."""
        user_record = self._user_record_from_row(row)
        return ClaimedUserSessionRecord(
            user_id=int(row["user_id"]),
            session_id=str(uuid.uuid4()),
            user_record=user_record,
            planned_action_count=planned_action_count,
        )

    @classmethod
    def _planned_zero_activity_count(cls, nz_count: int, lurker_ratio: float) -> int:
        """Return extra zero-activity users needed for ratio `L` of final batch."""
        lurker_ratio = _require_lurker_ratio(lurker_ratio)
        if nz_count == 0 or lurker_ratio == 0:
            return 0

        desired_zero_activity_users = nz_count * lurker_ratio / (1 - lurker_ratio)
        return cls._probabilistically_round_activity_level(desired_zero_activity_users)

    @staticmethod
    def _probabilistically_round_activity_level(value: float) -> int:
        """Round a fractional activity level by using its fraction as chance."""
        base = math.floor(value)
        fraction = value - base
        return base + 1 if random.random() < fraction else base

    def fetch_out_network_feed_inventory(
        self,
        *,
        limit: int,
    ) -> list[ActionRow]:
        """Return the batch-level global recent-action inventory.

        See docs/algorithms/worker.md#inventory-rules.
        """
        _require_non_negative_int("out_network_inventory_limit", limit)
        return self._fetch_out_network_inventory_actions(limit)

    def fetch_in_network_feed_inventory(
        self,
        claimed_sessions: Sequence[ClaimedUserSessionRecord],
        *,
        limit: int,
    ) -> BatchInNetworkInventory:
        """Return one shared recent inventory for all batch viewers.

        See docs/algorithms/worker.md#inventory-rules.
        """
        _require_sequence(
            "claimed_sessions",
            claimed_sessions,
            ClaimedUserSessionRecord,
        )
        _require_non_negative_int("in_network_inventory_limit", limit)
        if not claimed_sessions:
            return BatchInNetworkInventory()
        return self._fetch_in_network_inventory_actions(
            [session.user_id for session in claimed_sessions],
            limit,
        )

    def _fetch_in_network_inventory_actions(
        self,
        user_ids: Sequence[int],
        limit: int,
    ) -> BatchInNetworkInventory:
        """Fetch one limited union of followed-creator actions for a batch."""
        action_table = self._qualified_table(ACTION_TABLE)
        follower_table = self._qualified_table(FOLLOWER_NETWORK_TABLE)
        _require_sequence("user_ids", user_ids, int)
        user_values = ", ".join(f"({sql_integer(user_id)})" for user_id in user_ids)
        sql = f"""
        WITH batch_users(user_id) AS (
            VALUES {user_values}
        ),
        source_viewers AS (
            SELECT candidate_action.action_id AS source_action_id,
                ARRAY_AGG(DISTINCT batch_users.user_id ORDER BY batch_users.user_id)
                    AS visible_to_user_ids
            FROM batch_users
            JOIN {follower_table} AS follower
                ON follower.user_id = batch_users.user_id
            JOIN {action_table} AS candidate_action
                ON candidate_action.creator_user_id = follower.friend_id
            WHERE candidate_action.action_type IN ('post', 'repost', 'reply', 'quote')
            GROUP BY candidate_action.action_id
            ORDER BY candidate_action.action_id DESC
            LIMIT {limit}
        ),
        candidate_source AS (
            SELECT candidate_action.*, source_viewers.visible_to_user_ids
            FROM source_viewers
            JOIN {action_table} AS candidate_action
                ON candidate_action.action_id = source_viewers.source_action_id
        ),
        {self._materialized_inventory_ctes()}
        SELECT COALESCE(json_agg(row_to_json(action_row)), '[]'::json)::text
        FROM (
            SELECT
                action_id,
                creator_user_id,
                timestamp,
                action_type,
                parent_action_id,
                content_embedding,
                moderation_actions,
                repeated_like_n_reshare_creator_count,
                views_by_others_count,
                other_properties,
                visible_to_user_ids
            FROM materialized_inventory
            ORDER BY source_action_id DESC
        ) AS action_row;
        """
        rows = self._load_json_rows(self.run_psql(sql, capture_output=True))
        actions = self._action_rows_from_rows(rows)
        visibility = {
            action_source_id(action): frozenset(
                int(user_id) for user_id in (row.get("visible_to_user_ids") or [])
            )
            for action, row in zip(actions, rows)
        }
        return BatchInNetworkInventory(actions=actions, visible_to_user_ids=visibility)

    def _fetch_out_network_inventory_actions(
        self,
        limit: int,
    ) -> list[ActionRow]:
        """Return the global recent-action inventory for a worker batch."""
        action_table = self._qualified_table(ACTION_TABLE)
        sql = f"""
        WITH candidate_source AS (
            SELECT candidate_action.*, NULL::INTEGER[] AS visible_to_user_ids
            FROM {action_table} AS candidate_action
            WHERE candidate_action.action_type IN ('post', 'repost', 'reply', 'quote')
            ORDER BY candidate_action.action_id DESC
            LIMIT {limit}
        ),
        {self._materialized_inventory_ctes()}
        SELECT COALESCE(json_agg(row_to_json(friend_row)), '[]'::json)::text
        FROM (
            SELECT
                action_id,
                creator_user_id,
                timestamp,
                action_type,
                parent_action_id,
                content_embedding,
                moderation_actions,
                repeated_like_n_reshare_creator_count,
                views_by_others_count,
                other_properties
            FROM materialized_inventory
            ORDER BY source_action_id DESC
        ) AS friend_row;
        """
        return self._action_rows_from_rows(
            self._load_json_rows(self.run_psql(sql, capture_output=True)),
        )

    def _materialized_inventory_ctes(self) -> str:
        """
        Build shared inventory SQL that resolves reposts to their source content.

        The source-event limit has already been applied. Filter both source
        events and resolved content here without refilling rejected slots.
        """
        action_table = self._qualified_table(ACTION_TABLE)
        user_table = self._qualified_table(USER_TABLE)
        lookback_limit = self._engagement_action_lookback_limit_sql(user_table)
        return f"""
        recent_actions AS (
            SELECT action_id, action_type, parent_action_id, moderation_actions
            FROM {action_table}
            ORDER BY action_id DESC
            LIMIT {lookback_limit}
        ),
        recent_engagement_actions AS (
            SELECT
                engagement_action.action_id,
                engagement_action.action_type,
                engagement_action.parent_action_id
            FROM recent_actions AS engagement_action
            WHERE engagement_action.parent_action_id IS NOT NULL
                AND engagement_action.action_type IN (
                    'like',
                    'repost',
                    'reply',
                    'quote'
                )
                AND {feed_exclusion_moderation_filter_sql("engagement_action")}
        ),
        engagement_counts AS (
            SELECT
                parent_action_id AS action_id,
                COUNT(*) FILTER (WHERE action_type = 'like')::BIGINT
                    AS engagement_like,
                COUNT(*) FILTER (WHERE action_type = 'repost')::BIGINT
                    AS engagement_repost,
                COUNT(*) FILTER (WHERE action_type = 'reply')::BIGINT
                    AS engagement_reply,
                COUNT(*) FILTER (WHERE action_type = 'quote')::BIGINT
                    AS engagement_quote
            FROM recent_engagement_actions
            GROUP BY parent_action_id
        ),
        materialized_inventory AS (
            SELECT
                materialized_action.action_id,
                materialized_action.creator_user_id,
                source_action.timestamp,
                materialized_action.action_type,
                materialized_action.parent_action_id,
                materialized_action.content_embedding,
                materialized_action.moderation_actions,
                materialized_action.repeated_like_n_reshare_creator_count,
                materialized_action.views_by_others_count,
                materialized_action.other_properties || jsonb_build_object(
                    'source_action_id', source_action.action_id,
                    'source_creator_user_id', source_action.creator_user_id,
                    'source_action_type', source_action.action_type,
                    'source_timestamp', source_action.timestamp,
                    'materialized_timestamp', materialized_action.timestamp,
                    'engagement_like', COALESCE(engagement_counts.engagement_like, 0) + 1,
                    'engagement_repost', COALESCE(engagement_counts.engagement_repost, 0) + 1,
                    'engagement_reply', COALESCE(engagement_counts.engagement_reply, 0) + 1,
                    'engagement_quote', COALESCE(engagement_counts.engagement_quote, 0) + 1
                ) AS other_properties,
                source_action.action_id AS source_action_id,
                source_action.timestamp AS source_timestamp,
                source_action.visible_to_user_ids
            FROM candidate_source AS source_action
            JOIN {action_table} AS materialized_action
                ON materialized_action.action_id = CASE
                    WHEN source_action.action_type = 'repost'
                        THEN source_action.parent_action_id
                    ELSE source_action.action_id
                END
            LEFT JOIN engagement_counts
                ON engagement_counts.action_id = materialized_action.action_id
            WHERE materialized_action.action_type <> 'like'
                AND {feed_exclusion_moderation_filter_sql("source_action")}
                AND {feed_exclusion_moderation_filter_sql("materialized_action")}
        )
        """

    def write_actions(self, actions: Sequence[ActionWriteRecord]) -> None:
        """Persist one completed worker operation.

        Postgres assigns simulator timestamps. Under MPI, the worker receives
        a stop request between operations, so the current transaction can
        finish before the process exits.

        See docs/algorithms/worker.md#action-write-rules.
        """
        _require_sequence("actions", actions, ActionWriteRecord)
        if not actions:
            return

        table = self._qualified_table(ACTION_TABLE)
        statements: list[str] = ["BEGIN;"]
        statements.append(
            build_action_advisory_lock_sql(
                [
                    record.action.parent_action_id
                    for record in actions
                    if record.action.parent_action_id is not None
                ]
            )
        )
        statements.append(build_lock_action_clock_sql(self.settings))
        for start in range(0, len(actions), self.action_insert_chunk_size):
            chunk = actions[start : start + self.action_insert_chunk_size]
            values = ",\n".join(
                self._action_insert_values(action, input_index)
                for input_index, action in enumerate(chunk)
            )
            statements.append(f"""
                WITH new_action_rows AS (
                    SELECT *
                    FROM (VALUES
                        {values}
                    ) AS raw_new_actions (
                        input_index,
                        creator_user_id,
                        action_type,
                        parent_action_id,
                        content_embedding,
                        moderation_actions,
                        other_properties
                    )
                ),
                prepared_actions AS (
                    SELECT
                        new_action.creator_user_id::INTEGER AS creator_user_id,
                        new_action.action_type::TEXT AS action_type,
                        new_action.parent_action_id::BIGINT AS parent_action_id,
                        new_action.content_embedding::JSONB AS content_embedding,
                        CASE
                            WHEN parent_action.action_id IS NOT NULL
                            THEN NULLIF(
                                (
                                    CASE
                                        WHEN jsonb_typeof(parent_action.moderation_actions) = 'array'
                                        THEN parent_action.moderation_actions
                                        ELSE '[]'::jsonb
                                    END
                                ) || (
                                    CASE
                                        WHEN jsonb_typeof(new_action.moderation_actions::JSONB) = 'array'
                                        THEN new_action.moderation_actions::JSONB
                                        ELSE '[]'::jsonb
                                    END
                                ),
                                '[]'::jsonb
                            )
                            ELSE new_action.moderation_actions::JSONB
                        END AS moderation_actions,
                        CASE
                            WHEN parent_action.action_id IS NOT NULL
                            THEN (
                                new_action.other_properties::JSONB
                                || inherited_properties.other_properties
                                || jsonb_strip_nulls(
                                    jsonb_build_object(
                                        'session_id',
                                        new_action.other_properties::JSONB
                                            -> 'session_id'
                                    )
                                )
                            )
                            ELSE new_action.other_properties::JSONB
                        END AS other_properties,
                        CASE
                            WHEN new_action.action_type::TEXT IN ('like', 'repost')
                                AND new_action.parent_action_id::BIGINT IS NOT NULL
                            THEN (
                                prior_counts.max_repeated_count
                                + ROW_NUMBER() OVER (
                                    PARTITION BY
                                        new_action.creator_user_id::INTEGER,
                                        new_action.action_type::TEXT,
                                        new_action.parent_action_id::BIGINT
                                    ORDER BY new_action.input_index::INTEGER
                                )
                            )::BIGINT
                            ELSE 0
                        END AS repeated_like_n_reshare_creator_count
                    FROM new_action_rows AS new_action
                    LEFT JOIN LATERAL (
                        SELECT COALESCE(
                            MAX(prior.repeated_like_n_reshare_creator_count),
                            0
                        )::BIGINT AS max_repeated_count
                        FROM {table} AS prior
                        WHERE prior.creator_user_id = new_action.creator_user_id::INTEGER
                            AND prior.action_type = new_action.action_type::TEXT
                            AND prior.parent_action_id = new_action.parent_action_id::BIGINT
                            AND new_action.action_type::TEXT IN ('like', 'repost')
                            AND new_action.parent_action_id::BIGINT IS NOT NULL
                    ) AS prior_counts ON TRUE
                    LEFT JOIN {table} AS parent_action
                        ON parent_action.action_id = new_action.parent_action_id::BIGINT
                        AND new_action.action_type::TEXT IN ('like', 'repost')
                        AND new_action.parent_action_id::BIGINT IS NOT NULL
                    LEFT JOIN LATERAL (
                        SELECT COALESCE(
                            jsonb_object_agg(
                                parent_property.key,
                                parent_property.value
                            ),
                            '{{}}'::jsonb
                        ) AS other_properties
                        FROM jsonb_each(
                            COALESCE(parent_action.other_properties, '{{}}'::jsonb)
                        ) AS parent_property(key, value)
                        WHERE parent_action.action_id IS NOT NULL
                            AND {_inheritable_other_property_filter_sql("parent_property.key")}
                    ) AS inherited_properties ON TRUE
                )
                INSERT INTO {table} (
                    creator_user_id,
                    action_type,
                    parent_action_id,
                    content_embedding,
                    moderation_actions,
                    repeated_like_n_reshare_creator_count,
                    other_properties
                )
                SELECT
                    creator_user_id::INTEGER,
                    action_type::TEXT,
                    parent_action_id::BIGINT,
                    content_embedding::JSONB,
                    moderation_actions::JSONB,
                    repeated_like_n_reshare_creator_count::BIGINT,
                    other_properties::JSONB
                FROM prepared_actions
                ;
                """)
            statements.append(self._materialize_survival_time_labels_sql(table))
            statements.append(
                self._duplicate_reaction_cleanup_sql(
                    table=table,
                    values=values,
                )
            )
        statements.append("COMMIT;")
        self.run_psql("\n".join(statements))

    def _materialize_survival_time_labels_sql(self, table: str) -> str:
        """Convert internal survival-duration labels to absolute timestamps.

        See docs/algorithms/worker.md#action-write-rules.
        """
        return f"""
        UPDATE {table} AS action_row
        SET other_properties =
            jsonb_set(
                action_row.other_properties
                    #- '{{{POST_LABELS_PROPERTY},{INTERNAL_SURVIVAL_DURATION_MS_LABEL}}}',
                '{{{POST_LABELS_PROPERTY},{SURVIVAL_TIME_LABEL}}}',
                to_jsonb(
                    action_row.timestamp
                    + (
                        action_row.other_properties
                            #>> '{{{POST_LABELS_PROPERTY},{INTERNAL_SURVIVAL_DURATION_MS_LABEL}}}'
                    )::BIGINT
                ),
                true
            )
        WHERE jsonb_typeof(
            action_row.other_properties
                #> '{{{POST_LABELS_PROPERTY},{INTERNAL_SURVIVAL_DURATION_MS_LABEL}}}'
        ) = 'number';
        """

    def write_action_views(self, views: Sequence[ActionViewRecord]) -> None:
        """
        Increment view counters for the completed worker operation.

        Views are aggregate counters, so the write groups repeated exposures to
        the same action before updating the action table.
        """
        _require_sequence("views", views, ActionViewRecord)
        if not views:
            return

        table = self._qualified_table(ACTION_TABLE)
        values = ",\n".join(
            f"({sql_bigint(view.action_id)}, {sql_integer(view.viewer_user_id)})"
            for view in views
        )
        sql = f"""
        BEGIN;
        {build_action_advisory_lock_sql([view.action_id for view in views])}

        WITH view_rows(action_id, viewer_user_id) AS (
            VALUES
                {values}
        ),
        view_counts AS (
            SELECT counted_action.action_id, COUNT(*)::BIGINT AS view_count
            FROM {table} AS counted_action
            JOIN view_rows
                ON view_rows.action_id = counted_action.action_id
            WHERE counted_action.creator_user_id <> view_rows.viewer_user_id
            GROUP BY counted_action.action_id
        )
        UPDATE {table} AS target
        SET views_by_others_count = target.views_by_others_count + view_counts.view_count
        FROM view_counts
            WHERE target.action_id = view_counts.action_id;
        COMMIT;
        """
        self.run_psql(sql)

    def _action_insert_values(self, record: ActionWriteRecord, input_index: int) -> str:
        """Build one VALUES tuple for an action INSERT."""
        user_action = record.action
        moderation_actions = validate_moderation_actions(
            user_action.moderation_actions,
            "action.moderation_actions",
        )
        stored_moderation_actions = moderation_actions or None
        other_properties = dict(user_action.other_properties or {})
        other_properties.setdefault("session_id", record.session_id)
        self._add_quality_and_appeal(other_properties, record)
        self._add_post_labels(other_properties, record)
        return (
            "("
            f"{sql_integer(input_index)}, "
            f"{sql_integer(user_action.creator_user_id)}, "
            f"{sql_text(user_action.action_type)}, "
            f"{sql_bigint(user_action.parent_action_id)}, "
            f"{sql_json(user_action.content_embedding)}, "
            f"{sql_json(stored_moderation_actions)}, "
            f"{sql_json(other_properties)}"
            ")"
        )

    def _duplicate_reaction_cleanup_sql(
        self,
        *,
        table: str,
        values: str,
    ) -> str:
        """
        Mark older duplicate likes/reposts after a chunk insert.

        A duplicate reaction is the same creator performing the same like or
        repost action against the same parent action. The newest row remains in
        feed inventories; earlier matching rows receive a `delete` intervention.
        """
        delete_intervention = sql_json(
            moderation_actions_payload(
                [user_duplicate_reaction_cleanup_intervention()]
            ),
        )
        return f"""
        WITH new_action_rows AS (
            SELECT *
            FROM (VALUES
                {values}
            ) AS raw_new_actions (
                input_index,
                creator_user_id,
                action_type,
                parent_action_id,
                content_embedding,
                moderation_actions,
                other_properties
            )
        ),
        duplicate_keys AS (
            SELECT DISTINCT
                creator_user_id::INTEGER AS creator_user_id,
                action_type::TEXT AS action_type,
                parent_action_id::BIGINT AS parent_action_id
            FROM new_action_rows
            WHERE action_type::TEXT IN ('like', 'repost')
                AND parent_action_id::BIGINT IS NOT NULL
        ),
        ranked_reactions AS (
            SELECT
                existing.action_id,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        existing.creator_user_id,
                        existing.action_type,
                        existing.parent_action_id
                    ORDER BY existing.action_id DESC
                ) AS duplicate_rank
            FROM {table} AS existing
            JOIN duplicate_keys
                ON duplicate_keys.creator_user_id = existing.creator_user_id
                AND duplicate_keys.action_type = existing.action_type
                AND duplicate_keys.parent_action_id = existing.parent_action_id
        )
        UPDATE {table} AS existing
        SET moderation_actions =
            (
                CASE
                    WHEN jsonb_typeof(existing.moderation_actions) = 'array'
                    THEN existing.moderation_actions
                    ELSE '[]'::jsonb
                END
            ) || {delete_intervention}::jsonb
        FROM ranked_reactions
        WHERE existing.action_id = ranked_reactions.action_id
            AND ranked_reactions.duplicate_rank > 1
            AND {feed_exclusion_moderation_filter_sql("existing")};
        """

    def _add_quality_and_appeal(
        self,
        other_properties: dict[str, Any],
        record: ActionWriteRecord,
    ) -> None:
        """
        Add generated quality and appeal to content-bearing actions.

        Quality is an intrinsic content property. The selected user-type
        wrappers draw each value around the creator's stored user quality.
        """
        if (
            not is_content_action_type(record.action.action_type)
            or record.user_record is None
        ):
            return

        # Post quality and appeal are generated from the creator's persisted
        # user quality using per-user-type wrapper selectors.
        behavior = record.user_record.user_behavior
        other_properties[QUALITY_PROPERTY.name] = sample_generated_property(
            behavior,
            spec=QUALITY_PROPERTY,
            generation_function_name=self._post_distribution_function_for_user_type(
                record.user_record.user_type,
                QUALITY_PROPERTY,
            ),
        )
        other_properties[APPEAL_PROPERTY.name] = sample_generated_property(
            behavior,
            spec=APPEAL_PROPERTY,
            generation_function_name=self._post_distribution_function_for_user_type(
                record.user_record.user_type,
                APPEAL_PROPERTY,
            ),
        )

    def _add_post_labels(
        self,
        other_properties: dict[str, Any],
        record: ActionWriteRecord,
    ) -> None:
        """Run the optional per-user-type labelers for a content action."""
        if (
            not is_content_action_type(record.action.action_type)
            or record.user_record is None
        ):
            return

        labelers = self.post_labeling_functions_by_user_type.get(
            record.user_record.user_type,
            (),
        )
        if not labelers:
            return

        context = PostLabelingContext(
            action=record.action,
            user_record=record.user_record,
            other_properties=other_properties,
        )
        for function_name, function in labelers:
            labels = function(context)
            merge_post_labels(
                other_properties,
                labels,
                source_name=(
                    "post_labeling_functions."
                    f"{function_name} result"
                ),
            )

    def _qualified_table(self, table_name: str) -> str:
        return qualified_name(self.settings.schema, table_name)

    def _engagement_action_lookback_limit_sql(self, user_table: str) -> str:
        """Return a SQL subquery for the configured engagement action lookback."""
        multiplier = sql_float(self.engagement_action_lookback_population_multiplier)
        return (
            "(SELECT GREATEST(1, CEIL("
            f"{multiplier} * COUNT(*)"
            f"))::BIGINT FROM {user_table})"
        )

    def _post_distribution_function_for_user_type(
        self,
        user_type: str,
        spec: _ContentPropertySpec,
    ) -> str:
        """Return the configured post property generator for a user type."""
        if not self.user_types_config:
            raise ValueError("Missing content generation config: setup_db.user_types")
        if user_type not in self.user_types_config:
            raise ValueError(
                f"Missing content generation config for user_type: {user_type}"
            )
        type_config = _config_mapping(self.user_types_config[user_type])
        field_name = f"post_{spec.name}_distribution_function"
        return _require_generation_function(
            f"setup_db.user_types.{user_type}.{field_name}",
            _required_config_value(
                type_config,
                field_name,
                f"setup_db.user_types.{user_type}.{field_name}",
            ),
            POST_DISTRIBUTION_FUNCTIONS,
        )

    def _action_rows_from_rows(self, rows: list[dict[str, Any]]) -> list[ActionRow]:
        """Convert raw JSON rows into typed action records."""
        return [self._action_row_from_row(row) for row in rows]

    @staticmethod
    def _load_json_rows(output: str | None) -> list[dict[str, Any]]:
        """Parse a psql JSON array result into row dictionaries."""
        if output is not None and not isinstance(output, str):
            raise TypeError(f"output must be str or None, got {type(output).__name__}")
        if not output:
            return []
        rows = json.loads(output)
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise ValueError(
                f"Expected JSON array from worker query, got: {type(rows).__name__}"
            )
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(
                    f"Expected JSON object at worker query row {index}, "
                    f"got: {type(row).__name__}"
                )
        return rows

    @staticmethod
    def _user_record_from_row(row: dict[str, Any]) -> UserRecord:
        """Convert a raw user row from SQL into a typed user record."""
        _require_dict("user row", row)
        _require_key("user row", row, "user_id")
        _require_key("user row", row, "vector_profile")
        _require_key("user row", row, "activity_level")
        _require_key("user row", row, "user_type")
        _require_key("user row", row, "user_behavior")
        _require_key("user row", row, "other_properties")
        return UserRecord(
            user_id=int(row["user_id"]),
            vector_profile=row["vector_profile"],
            activity_level=float(row["activity_level"]),
            user_type=row["user_type"],
            other_properties=row["other_properties"],
            user_behavior=row["user_behavior"],
            moderation_actions=row.get("moderation_actions"),
        )

    @staticmethod
    def _action_row_from_row(row: dict[str, Any]) -> ActionRow:
        """Convert a raw action row from SQL into a typed action record."""
        _require_dict("action row", row)
        _require_key("action row", row, "action_id")
        _require_key("action row", row, "creator_user_id")
        _require_key("action row", row, "action_type")
        parent_action_id = row.get("parent_action_id")
        return ActionRow(
            action_id=int(row["action_id"]),
            creator_user_id=int(row["creator_user_id"]),
            timestamp=row.get("timestamp"),
            action_type=row["action_type"],
            parent_action_id=(
                int(parent_action_id) if parent_action_id is not None else None
            ),
            content_embedding=row.get("content_embedding") or [],
            moderation_actions=row.get("moderation_actions"),
            repeated_like_n_reshare_creator_count=int(
                row.get("repeated_like_n_reshare_creator_count") or 0
            ),
            views_by_others_count=int(row.get("views_by_others_count") or 0),
            other_properties=row.get("other_properties") or {},
        )


def create_worker_database_client(
    database_config: Any,
    worker_config: Any,
    user_types_config: Any = None,
    recsys_config: Any = None,
) -> WorkerDatabase:
    """Create the worker DB client from shared database and worker config."""
    if user_types_config is None:
        raise ValueError("Missing required config field: setup_db.user_types")
    user_types = _required_non_empty_mapping(
        user_types_config,
        "setup_db.user_types",
    )
    return PostgresWorkerDatabase(
        DatabaseSettings.from_config(database_config),
        action_insert_chunk_size=worker_config.action_insert_chunk_size,
        user_types_config=user_types,
        recsys_config=recsys_config,
    )


def _require_type(name: str, value: Any, expected_type: type) -> None:
    if not isinstance(value, expected_type):
        raise TypeError(
            f"{name} must be {expected_type.__name__}, got {type(value).__name__}"
        )


def _require_positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be greater than or equal to 1")
    return value


def _require_str_or_int(name: str, value: Any) -> None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise TypeError(f"{name} must be str or int, got {type(value).__name__}")


def _require_non_negative_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")
    return value


def _require_lurker_ratio(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(
            f"lurker_ratio must be int or float, got {type(value).__name__}"
        )

    ratio = float(value)
    if ratio < 0.0 or ratio >= 1.0:
        raise ValueError("lurker_ratio must satisfy 0 <= value < 1")

    return ratio


def _require_dict(name: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be dict, got {type(value).__name__}")


def _require_key(name: str, value: dict[str, Any], key: str) -> None:
    if key not in value:
        raise ValueError(f"{name} missing required key: {key}")


def sample_generated_property(
    behavior: Any,
    *,
    spec: _ContentPropertySpec,
    generation_function_name: Any,
) -> float:
    """Generate one normalized post-level content property.

    The configured object names a registered function and all of its inputs.
    The shipped Gaussian samples around user quality and clamps to [0, 1].

    See docs/algorithms/worker.md#action-write-rules.
    """
    generation_function_name = _require_generation_function(
        "generation_function_name",
        generation_function_name,
        POST_DISTRIBUTION_FUNCTIONS,
    )
    _, function = resolve_one(generation_function_name, POST_DISTRIBUTION_FUNCTIONS, "generation_function_name", runtime_count=2)
    return function(behavior, spec)


def _require_generation_function(name: str, value: Any, registry: Any) -> Any:
    """Validate explicit generator inputs; see docs/algorithms/configuration.md#function-inputs."""
    resolve_one(value, registry, name, runtime_count=2)
    return value


def _validate_user_type_post_distribution_functions(user_types_config: Any) -> None:
    """Validate post quality/appeal generator names for each active user type."""
    for user_type, raw_type_config in _config_mapping(user_types_config).items():
        type_config = _config_mapping(raw_type_config)
        field_prefix = f"setup_db.user_types.{user_type}"
        num_nodes = _require_non_negative_int(
            f"{field_prefix}.num_nodes",
            _required_config_value(
                type_config,
                "num_nodes",
                f"{field_prefix}.num_nodes",
            ),
        )
        requires_fields = num_nodes != 0
        for spec in (QUALITY_PROPERTY, APPEAL_PROPERTY):
            field_name = f"post_{spec.name}_distribution_function"
            if not requires_fields and field_name not in type_config:
                continue
            _require_generation_function(
                f"setup_db.user_types.{user_type}.{field_name}",
                _required_config_value(
                    type_config,
                    field_name,
                    f"setup_db.user_types.{user_type}.{field_name}",
                ),
                POST_DISTRIBUTION_FUNCTIONS,
            )


def _resolve_user_type_post_labeling_functions(
    user_types_config: Any,
) -> dict[str, tuple[tuple[str, PostLabelingFunction], ...]]:
    """Resolve optional labeler selectors once for every configured user type."""
    resolved: dict[str, tuple[tuple[str, PostLabelingFunction], ...]] = {}
    for user_type, raw_type_config in _config_mapping(user_types_config).items():
        type_config = _config_mapping(raw_type_config)
        field_name = f"setup_db.user_types.{user_type}.post_labeling_functions"
        if "post_labeling_functions" not in type_config:
            resolved[user_type] = ()
            continue
        resolved[user_type] = resolve_post_labeling_functions(
            type_config["post_labeling_functions"],
            field_name,
        )
    return resolved


def _engagement_action_lookback_population_multiplier(recsys_config: Any) -> float:
    """Read the configured multiplier for recent engagement-action scans."""
    if recsys_config is None:
        raise ValueError("Missing required config field: recsys")
    config = _config_mapping(recsys_config)
    return _require_positive_number(
        _required_config_value(
            config,
            "engagement_action_lookback_population_multiplier",
            "recsys.engagement_action_lookback_population_multiplier",
        ),
        "recsys.engagement_action_lookback_population_multiplier",
    )


def _required_config_value(value: Any, key: str, field_name: str) -> Any:
    """Read a required config value from a dict-like or object-like config."""
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"Missing required config field: {field_name}")
        return value[key]
    if not hasattr(value, key):
        raise ValueError(f"Missing required config field: {field_name}")
    return getattr(value, key)


def _require_unit_interval(value: Any, name: str) -> float:
    number = _require_number(value, name)
    if number < 0.0 or number > 1.0:
        raise ValueError(f"{name} must satisfy 0 <= value <= 1")
    return number


def _clamp_unit_interval(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


def _config_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    raise TypeError(f"Expected config object or dict, got {type(value).__name__}")


def _required_non_empty_mapping(value: Any, field_name: str) -> dict[str, Any]:
    config = _config_mapping(value)
    if not config:
        raise ValueError(f"{field_name} must be a non-empty object")
    return config


def _config_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"distribution missing required key: {key}")
        return value[key]
    if not hasattr(value, key):
        raise ValueError(f"distribution missing required key: {key}")
    return getattr(value, key)


def _require_probability(value: Any, name: str) -> float:
    number = _require_number(value, name)
    if number < 0.0 or number > 1.0:
        raise ValueError(f"{name} must satisfy 0 <= value <= 1")
    return number


def _require_positive_number(value: Any, name: str) -> float:
    number = _require_number(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return number


def _require_number(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float, got {type(value).__name__}")
    return float(value)


def _require_list(name: str, value: Any, item_type: type | tuple[type, ...]) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be list, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, item_type):
            expected_name = _type_name(item_type)
            raise TypeError(
                f"{name}[{index}] must be {expected_name}, got {type(item).__name__}"
            )


def _require_sequence(name: str, value: Any, item_type: type) -> None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, item_type):
            raise TypeError(
                f"{name}[{index}] must be {item_type.__name__}, "
                f"got {type(item).__name__}"
            )


def _type_name(item_type: type | tuple[type, ...]) -> str:
    if isinstance(item_type, tuple):
        return " or ".join(type_item.__name__ for type_item in item_type)
    return item_type.__name__
