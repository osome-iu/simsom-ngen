# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Shared DB core for SimSoM.

Import shared DB contracts from this package instead of reaching into several
separate files.
"""

from db.core.connection import DatabaseClient, PostgresClientBase
from db.core.data_transfer_objects import (
    ActionRow,
    FollowerNetworkRecord,
    UserRecord,
)
from db.core.lifecycle import LifecycleDatabase, PostgresLifecycleMixin, TerminationState
from db.core.schema import (
    ANALYTICS_SUMMARY_CONTRACT,
    ANALYTICS_SUMMARY_TABLE,
    ACTION_CONTRACT,
    ACTION_TABLE,
    CLOCK_CONFIGURE_FUNCTION,
    CLOCK_NEXTVAL_FUNCTION,
    CLOCK_STATE_TABLE,
    FOLLOWER_NETWORK_CONTRACT,
    FOLLOWER_NETWORK_TABLE,
    ForeignKeyContract,
    IndexContract,
    LIFECYCLE_CONTRACT,
    LIFECYCLE_TABLE,
    MAX_INTEGER_USER_ID,
    SHARED_INDEX_CONTRACTS,
    SHARED_TABLE_CONTRACTS,
    TableContract,
    USER_CONTRACT,
    USER_TABLE,
    build_action_advisory_lock_sql,
    build_clock_configure_sql,
    build_clock_extension_sql,
    build_get_termination_state_sql,
    build_index_sql,
    build_lifecycle_reset_sql,
    build_record_policy_cursor_sql,
    build_record_analytics_stop_sql,
    build_request_termination_sql,
    build_shared_schema_sql,
    qualified_name,
    quote_ident,
    sql_bigint,
    sql_bool,
    sql_float,
    sql_float2,
    sql_integer,
    sql_json,
    sql_text,
)
from db.core.settings import DatabaseSettings

__all__ = [
    "DatabaseClient",
    "DatabaseSettings",
    "ActionRow",
    "FollowerNetworkRecord",
    "LifecycleDatabase",
    "PostgresClientBase",
    "PostgresLifecycleMixin",
    "TerminationState",
    "UserRecord",
    "ANALYTICS_SUMMARY_CONTRACT",
    "ANALYTICS_SUMMARY_TABLE",
    "ACTION_CONTRACT",
    "ACTION_TABLE",
    "CLOCK_CONFIGURE_FUNCTION",
    "CLOCK_NEXTVAL_FUNCTION",
    "CLOCK_STATE_TABLE",
    "FOLLOWER_NETWORK_CONTRACT",
    "FOLLOWER_NETWORK_TABLE",
    "ForeignKeyContract",
    "IndexContract",
    "LIFECYCLE_CONTRACT",
    "LIFECYCLE_TABLE",
    "MAX_INTEGER_USER_ID",
    "SHARED_INDEX_CONTRACTS",
    "SHARED_TABLE_CONTRACTS",
    "TableContract",
    "USER_CONTRACT",
    "USER_TABLE",
    "build_action_advisory_lock_sql",
    "build_clock_configure_sql",
    "build_clock_extension_sql",
    "build_get_termination_state_sql",
    "build_index_sql",
    "build_lifecycle_reset_sql",
    "build_record_policy_cursor_sql",
    "build_record_analytics_stop_sql",
    "build_request_termination_sql",
    "build_shared_schema_sql",
    "qualified_name",
    "quote_ident",
    "sql_bigint",
    "sql_bool",
    "sql_float",
    "sql_float2",
    "sql_integer",
    "sql_json",
    "sql_text",
]
