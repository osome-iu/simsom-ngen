# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Policy-owned DB client.

Policy reads newly created supported action targets from Postgres and writes
moderation actions back. The persisted cursor is the new-action review
boundary; delayed label targets can be revisited after their due time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Protocol, Sequence

from db.core import (
    ACTION_TABLE,
    build_action_advisory_lock_sql,
    build_record_policy_cursor_sql,
    CLOCK_STATE_TABLE,
    DatabaseSettings,
    LifecycleDatabase,
    LIFECYCLE_TABLE,
    PostgresClientBase,
    PostgresLifecycleMixin,
    qualified_name,
    sql_bigint,
    sql_json,
)
from domain.action_semantics import require_supported_action_type
from domain.action_semantics import supported_action_type_sql
from subcomponents.policy_annotations import (
    moderation_actions_payload,
    feed_exclusion_moderation_filter_sql,
    validate_intervention_entry,
)
from domain.post_label_semantics import POST_LABELS_PROPERTY, SURVIVAL_TIME_LABEL


DEFAULT_POLICY_TARGET_BATCH_SIZE = 500


@dataclass(frozen=True)
class PolicyTargetRecord:
    subject_id: str
    cursor_action_id: int
    action_type: str = "post"
    current_timestamp: int = 0
    moderation_actions: Any = None
    other_properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_positive_int("cursor_action_id", self.cursor_action_id)
        _require_non_negative_int("current_timestamp", self.current_timestamp)
        require_supported_action_type(self.action_type)


class PolicyTargetBatch(list[PolicyTargetRecord]):
    """A bounded target page with new-action and delayed-scan positions."""

    def __init__(
        self,
        targets: Sequence[PolicyTargetRecord] = (),
        *,
        next_cursor: PolicyCursor | None = None,
        next_due_scan_cursor: PolicyCursor | None = None,
    ) -> None:
        super().__init__(targets)
        if next_cursor is not None and not isinstance(next_cursor, PolicyCursor):
            raise TypeError(
                f"next_cursor must be PolicyCursor or None, "
                f"got {type(next_cursor).__name__}"
            )
        if next_due_scan_cursor is not None and not isinstance(next_due_scan_cursor, PolicyCursor):
            raise TypeError("next_due_scan_cursor must be PolicyCursor or None")
        self.next_cursor = next_cursor
        self.next_due_scan_cursor = next_due_scan_cursor


@dataclass(frozen=True)
class PolicyCursor:
    action_id: int

    def __post_init__(self) -> None:
        _require_positive_int("action_id", self.action_id)


@dataclass(frozen=True)
class PolicyModerationAction:
    subject_id: str
    interventions: tuple[dict[str, str], ...]

    def __post_init__(self) -> None:
        _require_non_empty_str("subject_id", self.subject_id)
        if not isinstance(self.interventions, Sequence) or isinstance(
            self.interventions,
            (str, bytes),
        ):
            raise TypeError(
                f"interventions must be a sequence, "
                f"got {type(self.interventions).__name__}"
            )
        interventions = tuple(
            validate_intervention_entry(intervention, f"interventions[{index}]")
            for index, intervention in enumerate(self.interventions)
        )
        if not interventions:
            raise ValueError("interventions must be non-empty")
        object.__setattr__(self, "interventions", interventions)

    @property
    def moderation_actions(self) -> list[dict[str, str]]:
        return moderation_actions_payload(self.interventions)


class PolicyDatabase(LifecycleDatabase, Protocol):
    def fetch_moderation_targets(
        self,
        cursor: PolicyCursor | None = None,
    ) -> list[PolicyTargetRecord]:
        """Return a bounded page of new and due supported actions.

        See docs/algorithms/policy.md#cursor-and-values-read-for-each-pass.
        """

    def write_moderation_actions(
        self,
        actions: Sequence[PolicyModerationAction],
    ) -> None:
        """Persist moderation actions on target actions."""

    def fetch_policy_cursor(self) -> PolicyCursor | None:
        """Return policy's persisted action high-water mark, if one exists."""

    def record_policy_cursor(self, cursor: PolicyCursor) -> None:
        """Persist policy's latest processed action high-water mark.

        See docs/algorithms/policy.md#cursor-and-values-read-for-each-pass.
        """


class PostgresPolicyDatabase(PostgresLifecycleMixin, PostgresClientBase):
    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        target_batch_size: int = DEFAULT_POLICY_TARGET_BATCH_SIZE,
    ):
        if (
            isinstance(target_batch_size, bool)
            or not isinstance(target_batch_size, int)
            or target_batch_size < 1
        ):
            raise ValueError("policy.target_batch_size must be a positive integer")
        super().__init__(settings)
        self.target_batch_size = target_batch_size
        self._due_scan_action_id = 0
        self._due_scan_upper_bound: int | None = None
        self._prefer_due_next = False

    def fetch_moderation_targets(
        self,
        cursor: PolicyCursor | None = None,
    ) -> list[PolicyTargetRecord]:
        """Execute the configured algorithm.

        See docs/algorithms/policy.md#cursor-and-values-read-for-each-pass.
        """
        action_table = qualified_name(self.settings.schema, ACTION_TABLE)
        clock_table = qualified_name(self.settings.schema, CLOCK_STATE_TABLE)
        new_action_clause = _new_action_where_clause(cursor, table_alias="action_row")
        due_upper_bound = self._due_scan_upper_bound
        if due_upper_bound is None and cursor is not None:
            due_upper_bound = cursor.action_id
        due_action_clause = _due_action_where_clause(
            None if due_upper_bound is None else PolicyCursor(due_upper_bound),
            table_alias="action_row",
        )
        due_scan_floor = self._due_scan_action_id
        new_priority = 1 if self._prefer_due_next else 0
        due_priority = 0 if self._prefer_due_next else 1
        sql = f"""
        WITH current_clock AS (
            SELECT
                COALESCE((
                    SELECT current_tick_ms
                    FROM {clock_table}
                    WHERE singleton
                ), 0)::BIGINT AS current_timestamp
        ), new_candidates AS (
            SELECT action_row.*
            FROM {action_table} AS action_row
            WHERE {supported_action_type_sql("action_row")}
                AND {new_action_clause}
            ORDER BY action_row.action_id ASC
            LIMIT {self.target_batch_size}
        ), due_candidates AS (
            SELECT action_row.*
            FROM {action_table} AS action_row
            CROSS JOIN current_clock
            WHERE {supported_action_type_sql("action_row")}
                AND {due_action_clause}
                AND action_row.action_id > {due_scan_floor}
                AND jsonb_typeof(
                    action_row.other_properties
                        #> '{{{POST_LABELS_PROPERTY},{SURVIVAL_TIME_LABEL}}}'
                ) = 'number'
                AND (
                    action_row.other_properties
                        #>> '{{{POST_LABELS_PROPERTY},{SURVIVAL_TIME_LABEL}}}'
                )::BIGINT <= current_clock.current_timestamp
                AND {feed_exclusion_moderation_filter_sql("action_row")}
            ORDER BY action_row.action_id ASC
            LIMIT {self.target_batch_size}
        ), numbered_candidates AS (
            SELECT
                action_row.*,
                ROW_NUMBER() OVER (ORDER BY action_row.action_id) AS review_position,
                {new_priority} AS branch_priority,
                'new'::TEXT AS review_branch
            FROM new_candidates AS action_row
            UNION ALL
            SELECT
                action_row.*,
                ROW_NUMBER() OVER (ORDER BY action_row.action_id) AS review_position,
                {due_priority} AS branch_priority,
                'due'::TEXT AS review_branch
            FROM due_candidates AS action_row
        ), review_window AS (
            SELECT *
            FROM numbered_candidates
            ORDER BY review_position, branch_priority
            LIMIT {self.target_batch_size}
        ), new_window AS (
            SELECT * FROM review_window WHERE review_branch = 'new'
        ), due_window AS (
            SELECT * FROM review_window WHERE review_branch = 'due'
        ), targets AS (
            SELECT
                action_row.action_id AS cursor_action_id,
                action_row.action_id::TEXT AS subject_id,
                action_row.action_type,
                action_row.moderation_actions,
                current_clock.current_timestamp,
                action_row.other_properties
            FROM new_window AS action_row
            CROSS JOIN current_clock
            WHERE {_survival_time_not_pending_sql("action_row", "current_clock")}
            UNION ALL
            SELECT
                action_row.action_id AS cursor_action_id,
                action_row.action_id::TEXT AS subject_id,
                action_row.action_type,
                action_row.moderation_actions,
                current_clock.current_timestamp,
                action_row.other_properties
            FROM due_window AS action_row
            CROSS JOIN current_clock
        )
        SELECT json_build_object(
            'targets',
            COALESCE(
                (
                    SELECT json_agg(row_to_json(target_row))
                    FROM (
                        SELECT *
                        FROM targets
                        ORDER BY cursor_action_id ASC
                    ) AS target_row
                ),
                '[]'::json
            ),
            'next_cursor_action_id', (SELECT MAX(action_id) FROM new_window),
            'next_due_scan_action_id', (SELECT MAX(action_id) FROM due_window)
        )::text
        FROM current_clock;
        """
        batch = _load_policy_target_batch(self.run_psql(sql, capture_output=True))
        if batch.next_due_scan_cursor is not None:
            self._due_scan_action_id = batch.next_due_scan_cursor.action_id
            self._due_scan_upper_bound = due_upper_bound
        elif self.target_batch_size == 1 and not self._prefer_due_next:
            # A due candidate may have lost the only slot to a new action.
            # Preserve its scan position until a due-first pass can check it.
            self._due_scan_upper_bound = due_upper_bound
        else:
            self._due_scan_action_id = 0
            self._due_scan_upper_bound = None
        self._prefer_due_next = not self._prefer_due_next
        return batch

    def write_moderation_actions(self, actions: Sequence[PolicyModerationAction]) -> None:
        _require_sequence("actions", actions, PolicyModerationAction)
        if not actions:
            return

        payload_rows = _moderation_action_payload_rows(actions)
        if not payload_rows:
            return

        if len(payload_rows) > self.target_batch_size:
            raise ValueError(
                "moderation action count exceeds policy.target_batch_size"
            )
        self._write_moderation_action_batch(payload_rows)

    def _write_moderation_action_batch(
        self, payload_rows: list[tuple[int, list[dict[str, str]]]]
    ) -> None:
        table = qualified_name(self.settings.schema, ACTION_TABLE)
        values = ",\n".join(
            f"({sql_bigint(action_id)}::BIGINT, {sql_json(interventions)})"
            for action_id, interventions in payload_rows
        )
        statements = ["BEGIN;"]
        statements.append(
            build_action_advisory_lock_sql(
                [action_id for action_id, _interventions in payload_rows]
            )
        )
        statements.append(
            f"""
            WITH moderation_payload(action_id, interventions) AS (
                VALUES
                    {values}
            )
            UPDATE {table} AS action_row
            SET moderation_actions =
                (
                    CASE
                        WHEN jsonb_typeof(action_row.moderation_actions) = 'array'
                        THEN action_row.moderation_actions
                        ELSE '[]'::jsonb
                    END
                ) || moderation_payload.interventions::jsonb
            FROM moderation_payload
            WHERE action_row.action_id = moderation_payload.action_id;
            """
        )
        statements.append("COMMIT;")
        self.run_psql("\n".join(statements))

    def fetch_policy_cursor(self) -> PolicyCursor | None:
        lifecycle_table = qualified_name(self.settings.schema, LIFECYCLE_TABLE)
        output = self.run_psql(
            f"""
            SELECT policy_cursor_action_id::TEXT
            FROM {lifecycle_table}
            WHERE lifecycle_id = 1;
            """,
            capture_output=True,
        )
        if not output:
            return None
        return PolicyCursor(
            action_id=_require_positive_int("policy_cursor_action_id", int(output)),
        )

    def record_policy_cursor(self, cursor: PolicyCursor) -> None:
        """Execute the configured algorithm.

        See docs/algorithms/policy.md#cursor-and-values-read-for-each-pass.
        """
        if not isinstance(cursor, PolicyCursor):
            raise TypeError(
                f"cursor must be PolicyCursor, got {type(cursor).__name__}"
            )
        self.run_psql(
            build_record_policy_cursor_sql(self.settings.schema, cursor.action_id),
        )


def create_policy_database_client(
    database_config: Any,
    *,
    target_batch_size: int = DEFAULT_POLICY_TARGET_BATCH_SIZE,
) -> PolicyDatabase:
    return PostgresPolicyDatabase(
        DatabaseSettings.from_config(database_config),
        target_batch_size=target_batch_size,
    )


def _require_non_empty_str(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be greater than or equal to 1")
    return value


def _require_non_negative_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")
    return value


def _new_action_where_clause(
    cursor: PolicyCursor | None,
    *,
    table_alias: str | None = None,
) -> str:
    if cursor is None:
        return "TRUE"
    if not isinstance(cursor, PolicyCursor):
        raise TypeError(
            f"cursor must be PolicyCursor or None, got {type(cursor).__name__}"
        )
    prefix = f"{table_alias}." if table_alias else ""
    return f"{prefix}action_id > {sql_bigint(cursor.action_id)}"


def _due_action_where_clause(
    cursor: PolicyCursor | None,
    *,
    table_alias: str | None = None,
) -> str:
    if cursor is None:
        return "FALSE"
    if not isinstance(cursor, PolicyCursor):
        raise TypeError(
            f"cursor must be PolicyCursor or None, got {type(cursor).__name__}"
        )
    prefix = f"{table_alias}." if table_alias else ""
    return f"{prefix}action_id <= {sql_bigint(cursor.action_id)}"


def _survival_time_not_pending_sql(table_alias: str, clock_alias: str) -> str:
    path = (
        f"{table_alias}.other_properties "
        f"#> '{{{POST_LABELS_PROPERTY},{SURVIVAL_TIME_LABEL}}}'"
    )
    value = (
        f"{table_alias}.other_properties "
        f"#>> '{{{POST_LABELS_PROPERTY},{SURVIVAL_TIME_LABEL}}}'"
    )
    return (
        "CASE "
        f"WHEN {path} IS NULL THEN TRUE "
        f"WHEN jsonb_typeof({path}) = 'number' "
        f"THEN ({value})::BIGINT <= {clock_alias}.current_timestamp "
        "ELSE TRUE "
        "END"
    )


def _moderation_action_payload_rows(
    actions: Sequence[PolicyModerationAction],
) -> list[tuple[int, list[dict[str, str]]]]:
    payload_by_action_id: dict[int, list[dict[str, str]]] = {}
    for action in actions:
        action_id = _parse_action_id(action.subject_id)
        payload_by_action_id.setdefault(action_id, []).extend(action.moderation_actions)
    return [
        (action_id, interventions)
        for action_id, interventions in payload_by_action_id.items()
        if interventions
    ]


def _require_sequence(name: str, value: Any, item_type: type) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be a sequence, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, item_type):
            raise TypeError(
                f"{name}[{index}] must be {item_type.__name__}, "
                f"got {type(item).__name__}"
            )


def _load_policy_target_batch(output: str | None) -> PolicyTargetBatch:
    if output is not None and not isinstance(output, str):
        raise TypeError(f"output must be str or None, got {type(output).__name__}")
    if not output:
        return PolicyTargetBatch()
    payload = json.loads(output)
    if payload is None:
        return PolicyTargetBatch()
    if isinstance(payload, list):
        return PolicyTargetBatch(
            [_policy_target_from_row(row) for row in _load_json_rows(payload)]
        )
    if not isinstance(payload, dict):
        raise ValueError(
            f"Expected JSON object from policy query, got: {type(payload).__name__}"
        )
    targets = _load_json_rows(payload.get("targets") or [])
    next_cursor_value = payload.get("next_cursor_action_id")
    next_cursor = (
        None
        if next_cursor_value is None
        else PolicyCursor(
            action_id=_required_int(next_cursor_value, "next_cursor_action_id")
        )
    )
    return PolicyTargetBatch(
        [_policy_target_from_row(row) for row in targets],
        next_cursor=next_cursor,
        next_due_scan_cursor=(
            None if payload.get("next_due_scan_action_id") is None
            else PolicyCursor(action_id=_required_int(
                payload["next_due_scan_action_id"], "next_due_scan_action_id"
            ))
        ),
    )


def _load_json_rows(rows: Any) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError(
            f"Expected JSON array from policy query, got: {type(rows).__name__}"
        )
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(
                f"Expected JSON object at policy query row {index}, "
                f"got: {type(row).__name__}"
            )
    return rows


def _policy_target_from_row(row: dict[str, Any]) -> PolicyTargetRecord:
    return PolicyTargetRecord(
        subject_id=str(row["subject_id"]),
        cursor_action_id=_required_int(
            row.get("cursor_action_id"),
            "cursor_action_id",
        ),
        action_type=str(row.get("action_type", "post")),
        current_timestamp=_required_int(
            row.get("current_timestamp"),
            "current_timestamp",
        ),
        moderation_actions=row.get("moderation_actions"),
        other_properties=row.get("other_properties") or {},
    )


def _required_int(value: Any, name: str) -> int:
    if value is None:
        raise ValueError(f"{name} is required")
    if isinstance(value, bool):
        raise TypeError(f"{name} must be int, got bool")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _parse_action_id(subject_id: str) -> int:
    try:
        parsed = int(subject_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("action subject_id must be an integer") from exc
    if parsed < 1:
        raise ValueError("action subject_id must be greater than or equal to 1")
    return parsed
