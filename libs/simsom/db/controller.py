# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Controller-owned DB client.

The controller owns schema setup, initial-state population, final dump/export,
and may also request lifecycle termination. Schema setup is responsible for
installing or enabling the Postgres clock extension plus the column defaults
that allocate simulator timestamps server-side.
"""

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol

from db.core import (
    ACTION_TABLE,
    FOLLOWER_NETWORK_TABLE,
    USER_TABLE,
    build_clock_configure_sql,
    build_index_sql,
    build_lifecycle_reset_sql,
    build_request_termination_sql,
    build_shared_schema_sql,
    DatabaseSettings,
    FollowerNetworkRecord,
    LifecycleDatabase,
    MAX_INTEGER_USER_ID,
    PostgresClientBase,
    PostgresLifecycleMixin,
    TerminationState,
    UserRecord,
    qualified_name,
    quote_ident,
    sql_float2,
    sql_json,
    sql_text,
)


@dataclass(frozen=True)
class SetupDbDataset:
    """Initial users and friend links written to Postgres."""

    users: list[UserRecord] = field(default_factory=list)
    follower_network: list[FollowerNetworkRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_list("users", self.users, UserRecord)
        _require_list("follower_network", self.follower_network, FollowerNetworkRecord)


@dataclass(frozen=True)
class SetupDbStream:
    """One-pass initial rows with a known user count and bounded producer memory."""

    user_count: int
    users: Iterable[UserRecord]
    follower_network: Iterable[FollowerNetworkRecord]
    close_callback: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.user_count, int) or isinstance(self.user_count, bool):
            raise TypeError("user_count must be an int")
        if self.user_count < 1:
            raise ValueError("user_count must be greater than zero")

    def close(self) -> None:
        """Release planner-owned temporary resources, when present."""
        if self.close_callback is not None:
            self.close_callback()


@dataclass(frozen=True)
class InitialStateCounts:
    """Rows successfully sent through the initial-state transaction."""

    users: int
    follower_edges: int


class ControllerDatabase(LifecycleDatabase, Protocol):
    def setup_schema(self) -> None:
        """Create tables and extension hooks and prepare for initial loading.

        See docs/algorithms/database.md#setup-steps.
        """

    def populate_initial_state(
        self,
        dataset: SetupDbDataset | SetupDbStream,
        *,
        started_at: datetime | None = None,
        progress: Callable[[str, int, int | None], None] | None = None,
    ) -> InitialStateCounts:
        """Persist the planned initial users and follower edges.

        See docs/algorithms/database.md#initial-load-steps.
        """

    def get_termination_state(self) -> TerminationState:
        """Return the current shared lifecycle state."""

    def request_termination(
        self, reason: str, *, termination_criteria: list[dict] | None = None
    ) -> None:
        """Record the controller's decision to terminate the run.

        See docs/algorithms/simulation.md#authoritative-termination-state.
        """

    def dump(self) -> None:
        """Persist or export final DB state for analysis."""


class PostgresControllerDatabase(PostgresLifecycleMixin, PostgresClientBase):
    USER_COPY_CHUNK_SIZE = 10_000
    FOLLOWER_COPY_CHUNK_SIZE = 50_000

    def setup_schema(self) -> None:
        """Prepare shared tables without secondary indexes for bulk loading.

        See docs/algorithms/database.md#setup-steps.
        """
        self.run_psql(self._build_schema_sql())

    def populate_initial_state(
        self,
        dataset: SetupDbDataset | SetupDbStream,
        *,
        started_at: datetime | None = None,
        progress: Callable[[str, int, int | None], None] | None = None,
    ) -> InitialStateCounts:
        """Write materialized or one-pass setup rows in one transaction.

        See docs/algorithms/database.md#initial-load-steps.
        """
        if not isinstance(dataset, (SetupDbDataset, SetupDbStream)):
            raise TypeError(
                "populate_initial_state expected SetupDbDataset or SetupDbStream, "
                f"got {type(dataset).__name__}"
            )
        counts = [0, 0]
        try:
            self.run_psql_stream(
                self._iter_initial_state_sql(
                    dataset,
                    started_at=started_at,
                    progress=progress,
                    counts=counts,
                )
            )
        finally:
            if isinstance(dataset, SetupDbStream):
                dataset.close()
        return InitialStateCounts(users=counts[0], follower_edges=counts[1])

    def request_termination(
        self, reason: str, *, termination_criteria: list[dict] | None = None
    ) -> None:
        """Write the controller-owned lifecycle termination request.

        See docs/algorithms/simulation.md#authoritative-termination-state.
        """
        self.run_psql(
            build_request_termination_sql(
                self.settings.schema,
                reason,
                source="controller",
                termination_criteria=termination_criteria,
            )
        )

    def dump(self) -> None:
        """Export the configured schema to `output/` with pg_dump."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path.cwd() / "output" / f"{self.settings.schema}_dump_{timestamp}.sql"
        self.run_pg_dump(output_path, schema=self.settings.schema)

    def _build_schema_sql(self) -> str:
        """Recreate current tables without load-sensitive secondary indexes."""
        return build_shared_schema_sql(
            self.settings,
            include_indexes=False,
        )

    def _iter_user_copy_sql(
        self,
        users: Iterable[UserRecord],
    ) -> Iterator[tuple[str, int]]:
        """Yield a PostgreSQL COPY stream with bounded user-row chunks."""
        table = self._qualified_table(USER_TABLE)
        yield (
            f"COPY {table} (user_id, vector_profile, activity_level, user_type, "
            "user_behavior, moderation_actions, other_properties) FROM STDIN;",
            0,
        )
        for chunk in _iter_chunks(users, self.USER_COPY_CHUNK_SIZE, UserRecord):
            rows = []
            for user in chunk:
                rows.append(
                    "\t".join(
                        (
                            _copy_integer(user.user_id),
                            _copy_json(user.vector_profile),
                            repr(float(user.activity_level)),
                            _copy_text(user.user_type),
                            _copy_json(user.user_behavior),
                            _copy_json(user.moderation_actions),
                            _copy_json(user.other_properties),
                        )
                    )
                )
            yield "\n".join(rows), len(chunk)
        yield (r"\.", 0)

    def _iter_follower_copy_sql(
        self,
        follower_network: Iterable[FollowerNetworkRecord],
    ) -> Iterator[tuple[str, int]]:
        """Yield a PostgreSQL COPY stream with bounded friend-link chunks."""
        table = self._qualified_table(FOLLOWER_NETWORK_TABLE)
        yield (f"COPY {table} (user_id, friend_id) FROM STDIN;", 0)
        for chunk in _iter_chunks(
            follower_network,
            self.FOLLOWER_COPY_CHUNK_SIZE,
            FollowerNetworkRecord,
        ):
            yield (
                "\n".join(
                    f"{_copy_integer(edge.user_id)}\t{_copy_integer(edge.friend_id)}"
                    for edge in chunk
                ),
                len(chunk),
            )
        yield (r"\.", 0)

    def _iter_initial_state_sql(
        self,
        dataset: SetupDbDataset | SetupDbStream,
        *,
        started_at: datetime | None,
        progress: Callable[[str, int, int | None], None] | None,
        counts: list[int],
    ) -> Iterator[str]:
        """Yield one transaction while reporting rows sent to its psql process."""
        total = (
            len(dataset.users) + len(dataset.follower_network)
            if isinstance(dataset, SetupDbDataset)
            else None
        )
        current = 0
        yield "BEGIN;"
        for sql, row_count in self._iter_user_copy_sql(dataset.users):
            yield sql
            current += row_count
            counts[0] += row_count
            if progress is not None and row_count:
                progress("user", current, total)
        if progress is not None and isinstance(dataset, SetupDbStream):
            progress("user_complete", current, total)
        for sql, row_count in self._iter_follower_copy_sql(dataset.follower_network):
            yield sql
            current += row_count
            counts[1] += row_count
            if progress is not None and row_count:
                progress("follower_network", current, total)
        if progress is not None:
            progress("indexes", current, total)
        yield build_index_sql(self.settings.schema)
        yield self._build_initial_state_analyze_sql()
        yield self._build_lifecycle_reset_sql(started_at=started_at)
        yield self._build_clock_configure_sql()
        yield "COMMIT;"

    def _build_initial_state_analyze_sql(self) -> str:
        """Build planner statistics before runtime processes query fresh tables."""
        return "\n".join(
            f"ANALYZE {self._qualified_table(table_name)};"
            for table_name in (USER_TABLE, FOLLOWER_NETWORK_TABLE, ACTION_TABLE)
        )

    def _build_lifecycle_reset_sql(self, *, started_at: datetime | None = None) -> str:
        """Build SQL that marks the beginning of this run."""
        return build_lifecycle_reset_sql(self.settings.schema, started_at=started_at)

    def _build_clock_configure_sql(self) -> str:
        """Build SQL that configures the Postgres clock extension for this run."""
        return build_clock_configure_sql(self.settings)

    def _schema_ident(self) -> str:
        return quote_ident(self.settings.schema)

    def _qualified_table(self, table_name: str) -> str:
        return qualified_name(self.settings.schema, table_name)

    @staticmethod
    def _quote_ident(identifier: str) -> str:
        return quote_ident(identifier)

    @staticmethod
    def _sql_text(value: str | None) -> str:
        return sql_text(value)

    @staticmethod
    def _sql_json(value: Any) -> str:
        return sql_json(value)

    @staticmethod
    def _sql_float2(value: float | None) -> str:
        return sql_float2(value)

    @staticmethod
    def _sql_bool(value: bool) -> str:
        return "TRUE" if value else "FALSE"


def create_controller_database_client(database_config: Any) -> ControllerDatabase:
    """Create the controller DB client from the shared database config object."""
    return PostgresControllerDatabase(DatabaseSettings.from_config(database_config))


def _require_list(name: str, value: Any, item_type: type) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a list, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, item_type):
            raise TypeError(
                f"{name}[{index}] must be {item_type.__name__}, "
                f"got {type(item).__name__}"
            )


def _iter_chunks(items: Iterable[Any], size: int, item_type: type) -> Iterator[list[Any]]:
    """Validate and yield bounded chunks from a one-pass or materialized input."""
    chunk = []
    for index, item in enumerate(items):
        if not isinstance(item, item_type):
            raise TypeError(
                f"item {index} must be {item_type.__name__}, "
                f"got {type(item).__name__}"
            )
        chunk.append(item)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _copy_text(value: str) -> str:
    """Escape one non-null value for PostgreSQL COPY text format."""
    if not isinstance(value, str):
        raise TypeError(f"COPY text value must be str, got {type(value).__name__}")
    return (
        value.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


def _copy_json(value: Any) -> str:
    """Serialize one JSON-compatible value for PostgreSQL COPY text format."""
    if value is None:
        return r"\N"
    return _copy_text(json.dumps(value, separators=(",", ":")))


def _copy_integer(value: int) -> str:
    """Validate and serialize one user identifier for INTEGER COPY input."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"COPY integer value must be int, got {type(value).__name__}")
    if not 1 <= value <= MAX_INTEGER_USER_ID:
        raise ValueError("COPY integer value is outside the signed INTEGER range")
    return str(value)
