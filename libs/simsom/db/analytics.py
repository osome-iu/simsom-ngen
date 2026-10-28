# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Analytics-owned DB client.

Analytics reads progress and aggregate DB state, writes summary rows, and reads
lifecycle state to know when the controller has stopped the run.
"""

from dataclasses import dataclass, field
import json
from typing import Any, Protocol

from db.core import (
    ANALYTICS_SUMMARY_TABLE,
    ACTION_TABLE,
    build_record_analytics_stop_sql,
    CLOCK_STATE_TABLE,
    DatabaseSettings,
    FOLLOWER_NETWORK_TABLE,
    LifecycleDatabase,
    LIFECYCLE_TABLE,
    PostgresClientBase,
    PostgresLifecycleMixin,
    USER_TABLE,
    qualified_name,
    sql_bigint,
    sql_bool,
    sql_float,
    sql_json,
    sql_text,
)
from domain.action_semantics import supported_action_type_sql
import subcomponents.policy_rules  # noqa: F401
from subcomponents.policy_annotations import intervention_source_exists_sql
from domain.content_semantics import QUALITY_PROPERTY_KEY


@dataclass(frozen=True)
class AnalyticsSnapshot:
    """Aggregate database state read for a termination check or final report."""

    user_count: int = 0
    action_count: int = 0
    simulator_timestamp_ms: int = 0
    avg_activity_level: float | None = None
    avg_action_quality: float | None = None
    other_properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_negative_int("user_count", self.user_count)
        _require_non_negative_int("action_count", self.action_count)
        _require_non_negative_int(
            "simulator_timestamp_ms",
            self.simulator_timestamp_ms,
        )
        _require_optional_non_negative_number(
            "avg_activity_level",
            self.avg_activity_level,
        )
        _require_optional_unit_interval("avg_action_quality", self.avg_action_quality)
        _require_dict("other_properties", self.other_properties)


@dataclass(frozen=True)
class AnalyticsProgress:
    """Cheap database watermark used to detect a due analytics checkpoint."""

    user_count: int = 0
    action_id_watermark: int = 0
    simulator_timestamp_ms: int = 0

    def __post_init__(self) -> None:
        _require_non_negative_int("user_count", self.user_count)
        _require_non_negative_int("action_id_watermark", self.action_id_watermark)
        _require_non_negative_int(
            "simulator_timestamp_ms",
            self.simulator_timestamp_ms,
        )


@dataclass(frozen=True)
class AnalyticsStopSignal:
    """Analytics-owned lifecycle termination request."""

    stop_requested: bool = False
    stop_reason: str | None = None
    termination_criteria: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_bool("stop_requested", self.stop_requested)
        _require_optional_str("stop_reason", self.stop_reason)
        _require_list_of_dicts("termination_criteria", self.termination_criteria)


@dataclass(frozen=True)
class AnalyticsSummaryRecord:
    """One durable status summary produced by an analytics pass."""

    user_count: int = 0
    action_count: int = 0
    avg_activity_level: float | None = None
    avg_action_quality: float | None = None
    termination_criteria: list[dict[str, Any]] = field(default_factory=list)
    other_properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_negative_int("user_count", self.user_count)
        _require_non_negative_int("action_count", self.action_count)
        _require_optional_non_negative_number(
            "avg_activity_level",
            self.avg_activity_level,
        )
        _require_optional_unit_interval("avg_action_quality", self.avg_action_quality)
        _require_list_of_dicts("termination_criteria", self.termination_criteria)
        _require_dict("other_properties", self.other_properties)


class AnalyticsDatabase(LifecycleDatabase, Protocol):
    def fetch_analytics_progress(self) -> AnalyticsProgress:
        """Return cheap monotonic values used to detect the next checkpoint."""

    def fetch_termination_metrics(self) -> AnalyticsSnapshot:
        """Return only the metrics needed for recurring termination checks."""

    def fetch_analytics_snapshot(self) -> AnalyticsSnapshot:
        """Return current aggregate state for analytics."""

    def record_analytics_summary(self, summary: AnalyticsSummaryRecord) -> None:
        """Persist one analytics status summary for this run."""

    def record_analytics_stop_signal(self, signal: AnalyticsStopSignal) -> None:
        """Persist an analytics stop signal without requesting termination."""


class PostgresAnalyticsDatabase(PostgresLifecycleMixin, PostgresClientBase):
    def fetch_analytics_progress(self) -> AnalyticsProgress:
        """Read clock state and the indexed action-id high watermark."""
        action_table = qualified_name(self.settings.schema, ACTION_TABLE)
        clock_table = qualified_name(self.settings.schema, CLOCK_STATE_TABLE)
        row = _load_json_object(
            self.run_psql(
                f"""
                SELECT row_to_json(progress)::text
                FROM (
                    SELECT
                        num_users::INTEGER AS user_count,
                        (
                            SELECT COALESCE(MAX(action_id), 0)::BIGINT
                            FROM {action_table}
                        ) AS action_id_watermark,
                        current_tick_ms::BIGINT AS simulator_timestamp_ms
                    FROM {clock_table}
                    WHERE singleton
                ) AS progress;
                """,
                capture_output=True,
            )
        )
        return AnalyticsProgress(
            user_count=int(row.get("user_count") or 0),
            action_id_watermark=int(row.get("action_id_watermark") or 0),
            simulator_timestamp_ms=int(row.get("simulator_timestamp_ms") or 0),
        )

    def fetch_termination_metrics(self) -> AnalyticsSnapshot:
        """Read the small aggregate set needed for a recurring criterion check."""
        user_table = qualified_name(self.settings.schema, USER_TABLE)
        action_table = qualified_name(self.settings.schema, ACTION_TABLE)
        clock_table = qualified_name(self.settings.schema, CLOCK_STATE_TABLE)
        sql = f"""
        SELECT
            (SELECT COUNT(*)::BIGINT FROM {user_table}) AS user_count,
            (SELECT COUNT(*)::BIGINT FROM {action_table}) AS action_count,
            (
                SELECT current_tick_ms::BIGINT
                FROM {clock_table}
                WHERE singleton
            ) AS simulator_timestamp_ms,
            (
                SELECT AVG((other_properties->>{sql_text(QUALITY_PROPERTY_KEY)})::FLOAT)
                FROM {action_table}
                WHERE other_properties ? {sql_text(QUALITY_PROPERTY_KEY)}
            ) AS avg_action_quality
        """
        row = _load_json_object(
            self.run_psql(
                f"SELECT row_to_json(metrics)::text FROM ({sql}) AS metrics;",
                capture_output=True,
            )
        )
        return AnalyticsSnapshot(
            user_count=int(row.get("user_count") or 0),
            action_count=int(row.get("action_count") or 0),
            simulator_timestamp_ms=int(row.get("simulator_timestamp_ms") or 0),
            avg_action_quality=_optional_float(row.get("avg_action_quality")),
        )

    def fetch_analytics_snapshot(self) -> AnalyticsSnapshot:
        """
        Read one aggregate snapshot from Postgres.

        The snapshot is the input to analytics. It includes counts, averages,
        action mix, policy coverage, network shape, repost details, and run
        timing so the final summary can describe the final database state
        without a separate report query.
        """
        user_table = qualified_name(self.settings.schema, USER_TABLE)
        action_table = qualified_name(self.settings.schema, ACTION_TABLE)
        follower_table = qualified_name(self.settings.schema, FOLLOWER_NETWORK_TABLE)
        lifecycle_table = qualified_name(self.settings.schema, LIFECYCLE_TABLE)
        policy_intervention_exists = intervention_source_exists_sql(
            "policy_action_source",
            "policy",
        )
        clock_table = qualified_name(self.settings.schema, CLOCK_STATE_TABLE)
        sql = f"""
        WITH action_type_ratios AS (
            SELECT COALESCE(
                jsonb_object_agg(action_type, action_ratio ORDER BY action_type),
                '{{}}'::jsonb
            ) AS value
            FROM (
                SELECT
                    action_type,
                    TO_CHAR(
                        ROUND(
                            (
                                COUNT(*)::FLOAT
                                / NULLIF(SUM(COUNT(*)) OVER (), 0)::FLOAT
                            )::NUMERIC,
                            3
                        ),
                        'FM999999990.000'
                    ) AS action_ratio
                FROM {action_table}
                GROUP BY action_type
            ) AS ratios
        ),
        repost_rows AS (
            SELECT
                repost.action_id,
                repost.creator_user_id,
                repost.parent_action_id,
                parent.creator_user_id AS parent_creator_user_id,
                ROW_NUMBER() OVER (
                    PARTITION BY repost.creator_user_id, repost.parent_action_id
                    ORDER BY repost.timestamp, repost.action_id
                ) AS creator_parent_repost_number
            FROM {action_table} AS repost
            LEFT JOIN {action_table} AS parent
                ON parent.action_id = repost.parent_action_id
            WHERE repost.action_type = 'repost'
        ),
        repost_stats AS (
            SELECT json_build_object(
                'repost_action_ratio',
                    TO_CHAR(
                        ROUND(
                            (
                                CASE
                                    WHEN action_count = 0
                                    THEN NULL
                                    ELSE repost_count::FLOAT / action_count::FLOAT
                                END
                            )::NUMERIC,
                            3
                        ),
                        'FM999999990.000'
                    ),
                'repeated_repost_ratio',
                    TO_CHAR(
                        ROUND(
                            (
                                CASE
                                    WHEN repost_count = 0
                                    THEN NULL
                                    ELSE repeated_repost_count::FLOAT / repost_count::FLOAT
                                END
                            )::NUMERIC,
                            3
                        ),
                        'FM999999990.000'
                    ),
                'self_repost_ratio',
                    TO_CHAR(
                        ROUND(
                            (
                                CASE
                                    WHEN repost_count = 0
                                    THEN NULL
                                    ELSE self_repost_count::FLOAT / repost_count::FLOAT
                                END
                            )::NUMERIC,
                            3
                        ),
                        'FM999999990.000'
                    ),
                'self_repeated_repost_ratio',
                    TO_CHAR(
                        ROUND(
                            (
                                CASE
                                    WHEN repost_count = 0
                                    THEN NULL
                                    ELSE (
                                        self_repeated_repost_count::FLOAT
                                        / repost_count::FLOAT
                                    )
                                END
                            )::NUMERIC,
                            3
                        ),
                        'FM999999990.000'
                    )
            ) AS value
            FROM (
                SELECT
                    (SELECT COUNT(*) FROM {action_table}) AS action_count,
                    COUNT(*) AS repost_count,
                    COUNT(*) FILTER (
                        WHERE creator_parent_repost_number > 1
                    ) AS repeated_repost_count,
                    COUNT(*) FILTER (
                        WHERE parent_creator_user_id = creator_user_id
                    ) AS self_repost_count,
                    COUNT(*) FILTER (
                        WHERE parent_creator_user_id = creator_user_id
                            AND creator_parent_repost_number > 1
                    ) AS self_repeated_repost_count
                FROM repost_rows
            ) AS repost_counts
        ),
        policy_window AS (
            SELECT
                COALESCE(
                    (
                        SELECT policy_cursor_action_id
                        FROM {lifecycle_table}
                        WHERE lifecycle_id = 1
                    ),
                    0
                )::BIGINT AS policy_cursor_action_id
        ),
        policy_action_stats AS (
            SELECT
                COUNT(*) FILTER (
                    WHERE is_policy_eligible
                ) AS policy_eligible_count,
                COUNT(*) FILTER (
                    WHERE action_id <= policy_cursor_action_id
                        AND is_policy_eligible
                ) AS policy_cursor_covered_count,
                COUNT(*) FILTER (
                    WHERE action_id <= policy_cursor_action_id
                        AND is_policy_eligible
                        AND has_policy_intervention
                ) AS policy_intervention_count
            FROM (
                SELECT
                    policy_action_source.action_id,
                    policy_window.policy_cursor_action_id,
                    {policy_intervention_exists} AS has_policy_intervention,
                    ({supported_action_type_sql("policy_action_source")})
                        AS is_policy_eligible
                FROM {action_table} AS policy_action_source
                CROSS JOIN policy_window
            ) AS policy_actions
        ),
        policy_stats AS (
            SELECT json_build_object(
                'policy_coverage',
                    TO_CHAR(
                        ROUND(
                            CASE
                                WHEN policy_eligible_count = 0
                                THEN NULL
                                ELSE policy_cursor_covered_count::FLOAT
                                    / policy_eligible_count::FLOAT
                            END
                            ::NUMERIC,
                            3
                        ),
                        'FM999999990.000'
                    ),
                'policy_intervention_ratio',
                    TO_CHAR(
                        ROUND(
                            CASE
                                WHEN policy_cursor_covered_count = 0
                                THEN NULL
                                ELSE policy_intervention_count::FLOAT
                                    / policy_cursor_covered_count::FLOAT
                            END
                            ::NUMERIC,
                            3
                        ),
                        'FM999999990.000'
                    )
            ) AS value
            FROM policy_action_stats
        ),
        friend_counts AS (
            SELECT
                sim_user.user_id,
                COUNT(friend_edge.friend_id)::BIGINT AS friend_count
            FROM {user_table} AS sim_user
            LEFT JOIN {follower_table} AS friend_edge
                ON friend_edge.user_id = sim_user.user_id
            GROUP BY sim_user.user_id
        ),
        follower_counts AS (
            SELECT
                sim_user.user_id,
                COUNT(follower_edge.user_id)::BIGINT AS follower_count
            FROM {user_table} AS sim_user
            LEFT JOIN {follower_table} AS follower_edge
                ON follower_edge.friend_id = sim_user.user_id
            GROUP BY sim_user.user_id
        ),
        network_stats AS (
            SELECT jsonb_build_object(
                'avg_friends_per_user',
                    ROUND(
                        COALESCE(
                            (SELECT AVG(friend_count)::FLOAT FROM friend_counts),
                            0.0
                        )::NUMERIC,
                        3
                    )::FLOAT,
                'avg_followers_per_user',
                    ROUND(
                        COALESCE(
                            (SELECT AVG(follower_count)::FLOAT FROM follower_counts),
                            0.0
                        )::NUMERIC,
                        3
                    )::FLOAT
            ) AS value
        ),
        action_timestamp_stats AS (
            SELECT
                CASE
                    WHEN COUNT(*) = 0 THEN 0
                    ELSE MAX(timestamp) - MIN(timestamp)
                END AS simulated_elapsed_ms
            FROM {action_table}
        ),
        action_intervals AS (
            SELECT
                (timestamp - previous_timestamp)::BIGINT AS interval_ms
            FROM (
                SELECT
                    timestamp,
                    LAG(timestamp) OVER (ORDER BY timestamp, action_id) AS previous_timestamp
                FROM {action_table}
            ) AS ordered_actions
            WHERE previous_timestamp IS NOT NULL
        ),
        action_interval_stats AS (
            SELECT jsonb_build_object(
                'min', ROUND(MIN(interval_ms)::NUMERIC, 3)::FLOAT,
                'mean', ROUND(AVG(interval_ms)::NUMERIC, 3)::FLOAT,
                'p50', ROUND(
                    (
                        percentile_cont(0.50) WITHIN GROUP (ORDER BY interval_ms)
                    )::NUMERIC,
                    3
                )::FLOAT,
                'p90', ROUND(
                    (
                        percentile_cont(0.90) WITHIN GROUP (ORDER BY interval_ms)
                    )::NUMERIC,
                    3
                )::FLOAT,
                'p95', ROUND(
                    (
                        percentile_cont(0.95) WITHIN GROUP (ORDER BY interval_ms)
                    )::NUMERIC,
                    3
                )::FLOAT,
                'p99', ROUND(
                    (
                        percentile_cont(0.99) WITHIN GROUP (ORDER BY interval_ms)
                    )::NUMERIC,
                    3
                )::FLOAT,
                'max', ROUND(MAX(interval_ms)::NUMERIC, 3)::FLOAT
            ) AS value
            FROM action_intervals
        ),
        timing_stats AS (
            SELECT jsonb_build_object(
                'inter_activity_ms', interval_stats.value
            ) AS value
            FROM action_interval_stats AS interval_stats
        ),
        runtime_stats AS (
            SELECT jsonb_build_object(
                'software_runtime_seconds',
                    ROUND(
                        GREATEST(
                            EXTRACT(EPOCH FROM (NOW() - lifecycle.started_at)),
                            0
                        )::NUMERIC,
                        3
                    )::FLOAT,
                'simulated_runtime_seconds',
                    ROUND(
                        (
                            timestamp_stats.simulated_elapsed_ms::FLOAT / 1000.0
                        )::NUMERIC,
                        3
                    )::FLOAT
            ) AS value
            FROM {lifecycle_table} AS lifecycle
            CROSS JOIN action_timestamp_stats AS timestamp_stats
            WHERE lifecycle.lifecycle_id = 1
        )
        SELECT row_to_json(snapshot_row)::text
        FROM (
            SELECT
                (SELECT COUNT(*)::BIGINT FROM {user_table}) AS user_count,
                (SELECT COUNT(*)::BIGINT FROM {action_table}) AS action_count,
                (
                    SELECT current_tick_ms::BIGINT
                    FROM {clock_table}
                    WHERE singleton
                ) AS simulator_timestamp_ms,
                (
                    SELECT ROUND(AVG(activity_level)::NUMERIC, 3)::FLOAT
                    FROM {user_table}
                ) AS avg_activity_level,
                (
                    SELECT ROUND(
                        AVG((other_properties->>{sql_text(QUALITY_PROPERTY_KEY)})::FLOAT)::NUMERIC,
                        3
                    )::FLOAT
                    FROM {action_table}
                    WHERE other_properties ? {sql_text(QUALITY_PROPERTY_KEY)}
                ) AS avg_action_quality,
                json_build_object(
                    'action_type_ratios', (SELECT value FROM action_type_ratios),
                    'repost_stats', (SELECT value FROM repost_stats),
                    'policy_stats', (SELECT value FROM policy_stats),
                    'network_stats', (SELECT value FROM network_stats),
                    'timing', (SELECT value FROM timing_stats),
                    'runtime', (SELECT value FROM runtime_stats)
                ) AS other_properties
        ) AS snapshot_row;
        """
        return _analytics_snapshot_from_row(
            _load_json_object(self.run_psql(sql, capture_output=True))
        )

    def record_analytics_summary(self, summary: AnalyticsSummaryRecord) -> None:
        """Write one analytics summary row to the database."""
        if not isinstance(summary, AnalyticsSummaryRecord):
            raise TypeError(
                "summary must be AnalyticsSummaryRecord, "
                f"got {type(summary).__name__}"
            )
        table = qualified_name(self.settings.schema, ANALYTICS_SUMMARY_TABLE)
        sql = f"""
        INSERT INTO {table} (
            user_count,
            action_count,
            avg_activity_level,
            avg_action_quality,
            termination_criteria,
            other_properties
        )
        VALUES (
            {sql_bigint(summary.user_count)},
            {sql_bigint(summary.action_count)},
            {sql_float(summary.avg_activity_level)},
            {sql_float(summary.avg_action_quality)},
            {sql_json(summary.termination_criteria)},
            {sql_json(summary.other_properties)}
        );
        """
        self.run_psql(sql)

    def record_analytics_stop_signal(self, signal: AnalyticsStopSignal) -> None:
        """Write analytics' latest stop signal into the lifecycle row."""
        if not isinstance(signal, AnalyticsStopSignal):
            raise TypeError(
                "signal must be AnalyticsStopSignal, "
                f"got {type(signal).__name__}"
            )
        self.run_psql(
            build_record_analytics_stop_sql(
                self.settings.schema,
                stop_requested=signal.stop_requested,
                stop_reason=signal.stop_reason if signal.stop_requested else None,
                termination_criteria=signal.termination_criteria,
            )
        )


def create_analytics_database_client(database_config: Any) -> AnalyticsDatabase:
    """Build the analytics DB client from the shared database config."""
    return PostgresAnalyticsDatabase(DatabaseSettings.from_config(database_config))


def _load_json_object(output: str | None) -> dict[str, Any]:
    """Parse one JSON object returned by psql."""
    if output is not None and not isinstance(output, str):
        raise TypeError(f"output must be str or None, got {type(output).__name__}")
    if not output:
        return {}
    row = json.loads(output)
    if row is None:
        return {}
    if not isinstance(row, dict):
        raise ValueError(
            f"Expected JSON object from analytics query, got: {type(row).__name__}"
        )
    return row


def _analytics_snapshot_from_row(row: dict[str, Any]) -> AnalyticsSnapshot:
    """Convert a raw SQL row into the typed analytics snapshot."""
    return AnalyticsSnapshot(
        user_count=int(row.get("user_count") or 0),
        action_count=int(row.get("action_count") or 0),
        simulator_timestamp_ms=int(row.get("simulator_timestamp_ms") or 0),
        avg_activity_level=_optional_float(row.get("avg_activity_level")),
        avg_action_quality=_optional_float(row.get("avg_action_quality")),
        other_properties=row.get("other_properties") or {},
    )


def _optional_float(value: Any) -> float | None:
    """Return a float when a SQL value is present, otherwise None."""
    if value is None:
        return None
    return float(value)


def _require_non_negative_int(name: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")


def _require_optional_non_negative_number(name: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float, got {type(value).__name__}")
    if value < 0.0:
        raise ValueError(f"{name} must be greater than or equal to 0")


def _require_optional_unit_interval(name: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float, got {type(value).__name__}")
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must satisfy 0 <= value <= 1")


def _require_bool(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool, got {type(value).__name__}")


def _require_optional_str(name: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str or None, got {type(value).__name__}")


def _require_dict(name: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be dict, got {type(value).__name__}")


def _require_list_of_dicts(name: str, value: Any) -> None:
    """Validate JSON-ready termination criterion records."""
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise TypeError(f"{name}[{index}] must be a dict")
