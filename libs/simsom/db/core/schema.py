# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Shared Postgres schema contract for SimSoM.

This module is the single code-owned source for shared table names, columns,
relationships, indexes, and idempotent schema bootstrap SQL. Process-specific
DB modules may build queries against this contract, but they should not define
their own shared table layouts.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any

from db.core.settings import DatabaseSettings


USER_TABLE = "user"
ACTION_TABLE = "action"
FOLLOWER_NETWORK_TABLE = "follower_network"
LIFECYCLE_TABLE = "simulation_lifecycle"
ANALYTICS_SUMMARY_TABLE = "analytics_summary"
MAX_INTEGER_USER_ID = 2_147_483_647
CLOCK_STATE_TABLE = "simsom_clock_state"
CLOCK_CONFIGURE_FUNCTION = "simsom_clock_configure_exponential"
CLOCK_NEXTVAL_FUNCTION = "simsom_clock_nextval"

@dataclass(frozen=True)
class ForeignKeyContract:
    column: str
    target_table: str
    target_column: str
    on_delete: str | None = None


@dataclass(frozen=True)
class IndexContract:
    name: str
    table: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class TableContract:
    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKeyContract, ...] = field(default_factory=tuple)
    description: str = ""


USER_CONTRACT = TableContract(
    name=USER_TABLE,
    columns=(
        "user_id",
        "vector_profile",
        "activity_level",
        "user_type",
        "user_behavior",
        "moderation_actions",
        "other_properties",
    ),
    primary_key=("user_id",),
    description="Persisted simulator user/user profile.",
)

ACTION_CONTRACT = TableContract(
    name=ACTION_TABLE,
    columns=(
        "action_id",
        "creator_user_id",
        "timestamp",
        "action_type",
        "parent_action_id",
        "content_embedding",
        "moderation_actions",
        "repeated_like_n_reshare_creator_count",
        "views_by_others_count",
        "other_properties",
    ),
    primary_key=("action_id",),
    foreign_keys=(
        ForeignKeyContract("creator_user_id", USER_TABLE, "user_id", "CASCADE"),
        ForeignKeyContract("parent_action_id", ACTION_TABLE, "action_id", "SET NULL"),
    ),
    description="Persisted post/action rows, including repost/reply parent links.",
)

FOLLOWER_NETWORK_CONTRACT = TableContract(
    name=FOLLOWER_NETWORK_TABLE,
    columns=("user_id", "friend_id"),
    primary_key=("user_id", "friend_id"),
    foreign_keys=(
        ForeignKeyContract("user_id", USER_TABLE, "user_id", "CASCADE"),
        ForeignKeyContract("friend_id", USER_TABLE, "user_id", "CASCADE"),
    ),
    description="Directed friend edge: user_id follows/adds friend_id.",
)

LIFECYCLE_CONTRACT = TableContract(
    name=LIFECYCLE_TABLE,
    columns=(
        "lifecycle_id",
        "started_at",
        "should_terminate",
        "reason",
        "termination_source",
        "requested_at",
        "termination_criteria",
        "analytics_stop_requested",
        "analytics_stop_reason",
        "analytics_stop_updated_at",
        "analytics_termination_criteria",
        "policy_cursor_action_id",
        "policy_cursor_updated_at",
        "updated_at",
    ),
    primary_key=("lifecycle_id",),
    description="Singleton lifecycle row shared by all processes.",
)

ANALYTICS_SUMMARY_CONTRACT = TableContract(
    name=ANALYTICS_SUMMARY_TABLE,
    columns=(
        "analytics_summary_id",
        "recorded_at",
        "user_count",
        "action_count",
        "avg_activity_level",
        "avg_action_quality",
        "termination_criteria",
        "other_properties",
    ),
    primary_key=("analytics_summary_id",),
    description="Recurring lightweight analytics checks and final report.",
)

SHARED_TABLE_CONTRACTS = (
    USER_CONTRACT,
    ACTION_CONTRACT,
    FOLLOWER_NETWORK_CONTRACT,
    LIFECYCLE_CONTRACT,
    ANALYTICS_SUMMARY_CONTRACT,
)

SHARED_INDEX_CONTRACTS = (
    IndexContract("user_activity_level_idx", USER_TABLE, ("activity_level",)),
    IndexContract("action_creator_idx", ACTION_TABLE, ("creator_user_id",)),
    IndexContract("action_parent_idx", ACTION_TABLE, ("parent_action_id",)),
    IndexContract("action_timestamp_idx", ACTION_TABLE, ("timestamp",)),
    IndexContract("action_type_idx", ACTION_TABLE, ("action_type",)),
    IndexContract(
        "analytics_summary_recorded_at_idx",
        ANALYTICS_SUMMARY_TABLE,
        ("recorded_at",),
    ),
    IndexContract("follower_network_friend_idx", FOLLOWER_NETWORK_TABLE, ("friend_id",)),
)


def build_shared_schema_sql(
    settings: DatabaseSettings,
    *,
    include_indexes: bool = True,
) -> str:
    """
    Return SQL that recreates the current shared schema tables for a fresh run.

    SimSoM does not migrate or preserve run tables between simulations. Setup
    therefore drops only the code-owned tables and creates their current shape
    directly. This also guarantees that the current mixed-width identifier
    types are applied before initial-state loading.
    """

    schema = quote_ident(settings.schema)
    user_table = qualified_name(settings.schema, USER_TABLE)
    action_table = qualified_name(settings.schema, ACTION_TABLE)
    follower_table = qualified_name(settings.schema, FOLLOWER_NETWORK_TABLE)
    lifecycle_table = qualified_name(settings.schema, LIFECYCLE_TABLE)
    analytics_summary_table = qualified_name(settings.schema, ANALYTICS_SUMMARY_TABLE)
    obsolete_schema_migration_table = qualified_name(
        settings.schema,
        "schema_migration",
    )
    index_sql = build_index_sql(settings.schema) if include_indexes else ""

    return f"""
    CREATE SCHEMA IF NOT EXISTS {schema};
    SET search_path TO {schema}, public;

    DROP TABLE IF EXISTS
        {analytics_summary_table},
        {follower_table},
        {action_table},
        {user_table},
        {lifecycle_table},
        {obsolete_schema_migration_table}
    CASCADE;

    CREATE TABLE {user_table} (
        user_id INTEGER PRIMARY KEY
            CONSTRAINT user_user_id_positive CHECK (user_id >= 1),
        vector_profile JSONB NOT NULL DEFAULT '[]'::jsonb,
        activity_level FLOAT(2) NOT NULL DEFAULT 0,
        user_type TEXT NOT NULL,
        user_behavior JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        moderation_actions JSONB,
        other_properties JSONB NOT NULL DEFAULT '{{}}'::jsonb
    );

    CREATE TABLE {action_table} (
        action_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        creator_user_id INTEGER NOT NULL REFERENCES {user_table}(user_id) ON DELETE CASCADE,
        timestamp BIGINT NOT NULL,
        action_type TEXT NOT NULL,
        parent_action_id BIGINT REFERENCES {action_table}(action_id) ON DELETE SET NULL,
        content_embedding JSONB NOT NULL DEFAULT '[]'::jsonb,
        moderation_actions JSONB,
        repeated_like_n_reshare_creator_count BIGINT NOT NULL DEFAULT 0,
        views_by_others_count BIGINT NOT NULL DEFAULT 0,
        other_properties JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        CONSTRAINT action_repeated_like_n_reshare_creator_count_non_negative
            CHECK (repeated_like_n_reshare_creator_count >= 0),
        CONSTRAINT action_views_by_others_count_non_negative
            CHECK (views_by_others_count >= 0)
    );

    CREATE TABLE {follower_table} (
        user_id INTEGER NOT NULL REFERENCES {user_table}(user_id) ON DELETE CASCADE,
        friend_id INTEGER NOT NULL REFERENCES {user_table}(user_id) ON DELETE CASCADE,
        PRIMARY KEY (user_id, friend_id)
    );

    CREATE TABLE {lifecycle_table} (
        lifecycle_id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (lifecycle_id = 1),
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        should_terminate BOOLEAN NOT NULL DEFAULT FALSE,
        reason TEXT,
        termination_source TEXT,
        requested_at TIMESTAMPTZ,
        termination_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
        analytics_stop_requested BOOLEAN NOT NULL DEFAULT FALSE,
        analytics_stop_reason TEXT,
        analytics_stop_updated_at TIMESTAMPTZ,
        analytics_termination_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
        policy_cursor_action_id BIGINT,
        policy_cursor_updated_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE {analytics_summary_table} (
        analytics_summary_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        user_count BIGINT NOT NULL
            CONSTRAINT analytics_summary_user_count_non_negative CHECK (user_count >= 0),
        action_count BIGINT NOT NULL
            CONSTRAINT analytics_summary_action_count_non_negative CHECK (action_count >= 0),
        avg_activity_level FLOAT,
        avg_action_quality FLOAT,
        termination_criteria JSONB NOT NULL DEFAULT '[]'::jsonb,
        other_properties JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        CONSTRAINT analytics_summary_avg_activity_level_non_negative
            CHECK (avg_activity_level IS NULL OR avg_activity_level >= 0),
        CONSTRAINT analytics_summary_avg_action_quality_unit_interval
            CHECK (
                avg_action_quality IS NULL
                OR (avg_action_quality >= 0 AND avg_action_quality <= 1)
            )
    );

    {index_sql}

    ALTER TABLE {action_table}
        ALTER COLUMN timestamp DROP DEFAULT;

    {build_clock_extension_sql(settings)}
    """


def build_index_sql(schema: str) -> str:
    """Return CREATE INDEX statements for the shared schema indexes."""
    statements = []
    for index in SHARED_INDEX_CONTRACTS:
        index_name = quote_ident(index.name)
        table_name = qualified_name(schema, index.table)
        columns = ", ".join(quote_ident(column) for column in index.columns)
        statements.append(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name}({columns});")
    action_table = qualified_name(schema, ACTION_TABLE)
    statements.append(f"""
    CREATE INDEX IF NOT EXISTS action_post_label_survival_time_idx
    ON {action_table} (
        ((other_properties #>> '{{post_labels,survival_time}}')::BIGINT)
    )
    WHERE jsonb_typeof(
        other_properties #> '{{post_labels,survival_time}}'
    ) = 'number';
    """)
    return "\n".join(statements)


def build_lifecycle_reset_sql(
    schema: str,
    *,
    started_at: datetime | None = None,
) -> str:
    """Return SQL that starts a fresh lifecycle row for a new run."""
    table = qualified_name(schema, LIFECYCLE_TABLE)
    started_at_value = sql_timestamptz(started_at)
    return f"""
    INSERT INTO {table} (
        lifecycle_id,
        started_at,
        should_terminate,
        reason,
        termination_source,
        requested_at,
        termination_criteria,
        analytics_stop_requested,
        analytics_stop_reason,
        analytics_stop_updated_at,
        analytics_termination_criteria,
        policy_cursor_action_id,
        policy_cursor_updated_at,
        updated_at
    )
    VALUES (
        1,
        {started_at_value},
        FALSE,
        NULL,
        NULL,
        NULL,
        '[]'::jsonb,
        FALSE,
        NULL,
        NULL,
        '[]'::jsonb,
        NULL,
        NULL,
        NOW()
    )
    ON CONFLICT (lifecycle_id)
    DO UPDATE SET
        started_at = EXCLUDED.started_at,
        should_terminate = EXCLUDED.should_terminate,
        reason = EXCLUDED.reason,
        termination_source = EXCLUDED.termination_source,
        requested_at = EXCLUDED.requested_at,
        termination_criteria = EXCLUDED.termination_criteria,
        analytics_stop_requested = EXCLUDED.analytics_stop_requested,
        analytics_stop_reason = EXCLUDED.analytics_stop_reason,
        analytics_stop_updated_at = EXCLUDED.analytics_stop_updated_at,
        analytics_termination_criteria = EXCLUDED.analytics_termination_criteria,
        policy_cursor_action_id = EXCLUDED.policy_cursor_action_id,
        policy_cursor_updated_at = EXCLUDED.policy_cursor_updated_at,
        updated_at = NOW();
    """


def build_clock_configure_sql(settings: DatabaseSettings) -> str:
    """
    Return SQL that resets/configures the installed simsom clock extension.

    The extension may already exist in another schema, so this resolves the
    actual extension namespace before invoking its configuration function.
    """

    extension_name = sql_text(clock_extension_name(settings))
    user_table = qualified_name(settings.schema, USER_TABLE)
    return f"""
    DO $$
    DECLARE
        clock_schema text;
    BEGIN
        SELECT n.nspname
        INTO clock_schema
        FROM pg_extension e
        JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = {extension_name};

        IF clock_schema IS NULL THEN
            RAISE EXCEPTION 'Postgres extension % is not installed', {extension_name};
        END IF;

        EXECUTE format('SET search_path TO %I, public', clock_schema);
        EXECUTE format(
            'SELECT %I.{CLOCK_CONFIGURE_FUNCTION}(p_num_users => $1)',
            clock_schema
        )
        USING GREATEST((SELECT COUNT(*)::BIGINT FROM {user_table}), 1);
    END
    $$;
    """


def build_clock_extension_sql(settings: DatabaseSettings) -> str:
    """
    Return SQL that installs simsom_clock and assigns action.timestamp.

    CREATE EXTENSION is database-wide. If the extension already exists in a
    different schema, the ALTER DEFAULT block resolves that schema and uses the
    function there instead of assuming it lives in the simulator schema.
    """

    extension_name = clock_extension_name(settings)
    action_table_name = sql_text(ACTION_TABLE)
    schema_name = sql_text(settings.schema)
    return f"""
    DROP EXTENSION IF EXISTS {quote_ident(extension_name)} CASCADE;

    CREATE EXTENSION IF NOT EXISTS {quote_ident(extension_name)}
    WITH SCHEMA {quote_ident(settings.schema)};

    DO $$
    DECLARE
        clock_schema text;
    BEGIN
        SELECT n.nspname
        INTO clock_schema
        FROM pg_extension e
        JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = {sql_text(extension_name)};

        IF clock_schema IS NULL THEN
            RAISE EXCEPTION 'Postgres extension % is not installed', {sql_text(extension_name)};
        END IF;

        EXECUTE format(
            'ALTER TABLE %I.%I ALTER COLUMN timestamp SET DEFAULT %I.{CLOCK_NEXTVAL_FUNCTION}()',
            {schema_name},
            {action_table_name},
            clock_schema
        );
    END
    $$;
    """


def build_lock_action_clock_sql(settings: DatabaseSettings) -> str:
    """Lock the clock before identities; see docs/algorithms/database.md#timestamp-rule.

    The lock survives all insert chunks until transaction commit or rollback.
    Resolve the installed extension schema just as the timestamp default does.
    """
    return f"""
    DO $$
    DECLARE
        clock_schema text;
        locked_rows integer;
    BEGIN
        SELECT n.nspname INTO clock_schema
        FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
        WHERE e.extname = {sql_text(clock_extension_name(settings))};
        IF clock_schema IS NULL THEN
            RAISE EXCEPTION 'Simulator clock extension is not installed';
        END IF;
        EXECUTE format(
            'SELECT 1 FROM %I.{CLOCK_STATE_TABLE} WHERE singleton FOR UPDATE',
            clock_schema
        );
        GET DIAGNOSTICS locked_rows = ROW_COUNT;
        IF locked_rows <> 1 THEN
            RAISE EXCEPTION 'Expected exactly one simulator clock row';
        END IF;
    END
    $$;
    """


def build_request_termination_sql(
    schema: str,
    reason: str,
    *,
    source: str = "controller",
    termination_criteria: list[dict[str, object]] | None = None,
) -> str:
    """Freeze the first accepted reason and its observed criteria atomically."""
    table = qualified_name(schema, LIFECYCLE_TABLE)
    return f"""
    UPDATE {table}
    SET should_terminate = TRUE,
        reason = {sql_text(reason)},
        termination_source = {sql_text(source)},
        termination_criteria = {sql_json(termination_criteria or [])},
        requested_at = NOW(),
        updated_at = NOW()
    WHERE lifecycle_id = 1 AND NOT should_terminate;
    """


def build_record_analytics_stop_sql(
    schema: str,
    *,
    stop_requested: bool,
    stop_reason: str | None,
    termination_criteria: list[dict[str, object]],
) -> str:
    """Return SQL that stores analytics' latest stop signal."""
    table = qualified_name(schema, LIFECYCLE_TABLE)
    return f"""
    UPDATE {table}
    SET analytics_stop_requested = {sql_bool(stop_requested)},
        analytics_stop_reason = {sql_text(stop_reason)},
        analytics_stop_updated_at = NOW(),
        analytics_termination_criteria = {sql_json(termination_criteria)},
        updated_at = NOW()
    WHERE lifecycle_id = 1;
    """


def build_record_policy_cursor_sql(schema: str, action_id: int) -> str:
    """Return SQL that stores policy's latest processed action high-water mark."""
    table = qualified_name(schema, LIFECYCLE_TABLE)
    action_id_sql = sql_bigint(action_id)
    return f"""
    UPDATE {table}
    SET policy_cursor_action_id = GREATEST(
            COALESCE(policy_cursor_action_id, 0),
            {action_id_sql}
        ),
        policy_cursor_updated_at = NOW(),
        updated_at = NOW()
    WHERE lifecycle_id = 1;
    """


def build_get_termination_state_sql(schema: str) -> str:
    """Return SQL that reads the shared lifecycle stop state."""
    table = qualified_name(schema, LIFECYCLE_TABLE)
    return f"""
    SELECT row_to_json(state_row)::text
    FROM (
        SELECT
            should_terminate,
            reason,
            termination_source,
            termination_criteria,
            analytics_stop_requested,
            analytics_stop_reason,
            analytics_termination_criteria
        FROM {table}
        WHERE lifecycle_id = 1
    ) AS state_row;
    """


def clock_extension_name(settings: DatabaseSettings) -> str:
    if (
        not isinstance(settings.clock_extension_name, str)
        or not settings.clock_extension_name
    ):
        raise ValueError("database.clock_extension.name must be a non-empty string")
    return settings.clock_extension_name


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def qualified_name(schema: str, name: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(name)}"


def sql_text(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def sql_json(value: Any) -> str:
    if value is None:
        return "NULL"
    return f"{sql_text(json.dumps(value))}::jsonb"


def sql_timestamptz(value: datetime | None) -> str:
    """Return a SQL TIMESTAMPTZ literal, using NOW() when no value is supplied."""
    if value is None:
        return "NOW()"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return f"{sql_text(value.astimezone(timezone.utc).isoformat())}::timestamptz"


def sql_bool(value: bool) -> str:
    if not isinstance(value, bool):
        raise TypeError(f"Expected bool for BOOLEAN SQL literal, got {type(value).__name__}")
    return "TRUE" if value else "FALSE"


def sql_float(value: float | None) -> str:
    if value is None:
        return "NULL"
    return repr(float(value))


def sql_float2(value: float | None) -> str:
    if value is None:
        return "NULL"
    return f"{repr(float(value))}::FLOAT(2)"


def sql_bigint(value: int | None) -> str:
    if value is None:
        return "NULL"
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Expected int or None for BIGINT SQL literal, got {type(value).__name__}")
    return str(value)


def sql_integer(value: int | None) -> str:
    if value is None:
        return "NULL"
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Expected int or None for INTEGER SQL literal, got {type(value).__name__}")
    return str(value)


def build_action_advisory_lock_sql(action_ids: list[int | None]) -> str:
    """
    Return SQL that locks existing action IDs in a stable order for one transaction.

    Workers can update view counters while other workers insert child actions
    that reference the same parent actions. These advisory locks make those
    transactions agree on action-ID order before Postgres takes row locks for
    updates or foreign-key checks.
    """
    lock_ids = sorted({
        _require_positive_action_lock_id(action_id)
        for action_id in action_ids
        if action_id is not None
    })
    if not lock_ids:
        return ""
    values = ",\n".join(f"({sql_bigint(action_id)})" for action_id in lock_ids)
    return f"""
    SELECT pg_advisory_xact_lock(lock_targets.action_id)
    FROM (
        SELECT action_id
        FROM (VALUES
            {values}
        ) AS raw_lock_targets(action_id)
        GROUP BY action_id
        ORDER BY action_id
    ) AS lock_targets;
    """


def _require_positive_action_lock_id(value: int) -> int:
    """Return a positive action ID for advisory locking."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(
            f"Expected int for action advisory lock ID, got {type(value).__name__}"
        )
    if value < 1:
        raise ValueError("action advisory lock ID must be greater than or equal to 1")
    return value
