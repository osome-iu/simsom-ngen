# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Simulation-progress cadence semantics shared by analytics and configuration."""

from dataclasses import dataclass
import math
from typing import Any


AVERAGE_ACTIONS_PER_USER = "average_actions_per_user"
SIMULATED_HOURS = "simulated_hours"
ANALYTICS_EVALUATION_UNITS = (
    AVERAGE_ACTIONS_PER_USER,
    SIMULATED_HOURS,
)
SIMULATED_HOUR_MS = 60 * 60 * 1000


@dataclass(frozen=True)
class AnalyticsEvaluationInterval:
    """One simulation-progress interval for recurring analytics passes."""

    unit: str
    value: float

    def __post_init__(self) -> None:
        if self.unit not in ANALYTICS_EVALUATION_UNITS:
            allowed = ", ".join(ANALYTICS_EVALUATION_UNITS)
            raise ValueError(
                "analytics.evaluation_interval.unit must be one of: "
                f"{allowed}"
            )
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
            raise ValueError("analytics.evaluation_interval.value must be a number")
        if not math.isfinite(self.value) or self.value <= 0:
            raise ValueError(
                "analytics.evaluation_interval.value must be greater than 0"
            )

    @classmethod
    def from_config(cls, value: Any) -> "AnalyticsEvaluationInterval":
        """Build an interval from a dict or config namespace.

        See docs/algorithms/analytics.md#recurring-checks.
        """
        return cls(
            unit=_config_value(value, "unit"),
            value=_config_value(value, "value"),
        )

    def checkpoint(
        self,
        *,
        user_count: int,
        action_count: int,
        simulator_timestamp_ms: int,
    ) -> int:
        """Return the number of complete intervals represented by the values.

        See docs/algorithms/analytics.md#recurring-checks.
        """
        if self.unit == AVERAGE_ACTIONS_PER_USER:
            progress = action_count / user_count if user_count > 0 else 0.0
        else:
            progress = simulator_timestamp_ms / SIMULATED_HOUR_MS
        ratio = math.nextafter(progress / self.value, math.inf)
        return math.floor(ratio)


def _config_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(
                f"Missing required config field: analytics.evaluation_interval.{key}"
            )
        return value[key]
    if not hasattr(value, key):
        raise ValueError(
            f"Missing required config field: analytics.evaluation_interval.{key}"
        )
    return getattr(value, key)
