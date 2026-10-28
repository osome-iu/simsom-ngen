# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Shared simulation lifecycle boundary.

The lifecycle row is read by all processes. The controller owns writes that
request termination.
"""

from dataclasses import dataclass
import json
from typing import Protocol

from db.core.connection import DatabaseClient
from db.core.schema import build_get_termination_state_sql


@dataclass(frozen=True)
class TerminationState:
    should_terminate: bool = False
    reason: str | None = None
    termination_source: str | None = None
    analytics_stop_requested: bool = False
    analytics_stop_reason: str | None = None
    analytics_termination_criteria: list[dict] | None = None
    termination_criteria: list[dict] | None = None


class LifecycleDatabase(DatabaseClient, Protocol):
    def should_terminate(self) -> bool:
        """Return whether this process should exit."""


class PostgresLifecycleMixin:
    """
    Shared implementation for the lifecycle singleton row.

    Process-specific Postgres clients inherit this mixin so all roles observe
    termination through the same table and SQL contract.
    """

    def should_terminate(self) -> bool:
        """Return True when shared lifecycle state says this process should stop."""
        return self.get_termination_state().should_terminate

    def get_termination_state(self) -> TerminationState:
        """Read and parse the lifecycle row into a small Python object."""
        output = self.run_psql(
            build_get_termination_state_sql(self.settings.schema),
            capture_output=True,
        )
        if not output:
            return TerminationState()

        state_data = json.loads(output)
        return TerminationState(
            should_terminate=bool(state_data.get("should_terminate", False)),
            reason=state_data.get("reason"),
            termination_source=state_data.get("termination_source"),
            termination_criteria=state_data.get("termination_criteria") or [],
            analytics_stop_requested=bool(
                state_data.get("analytics_stop_requested", False)
            ),
            analytics_stop_reason=state_data.get("analytics_stop_reason"),
            analytics_termination_criteria=(
                state_data.get("analytics_termination_criteria") or []
            ),
        )
