# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Database settings shared by all SimSoM Postgres clients.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DatabaseSettings:
    dsn: str | None = None
    schema: str = "simsom"
    clock_extension_name: str | None = None

    @classmethod
    def from_config(cls, database_config: Any) -> "DatabaseSettings":
        clock_extension_cfg = _config_value(database_config, "clock_extension")
        return cls(
            dsn=_require_non_empty_string(
                _config_value(database_config, "dsn"),
                "database.dsn",
            ),
            schema=_require_non_empty_string(
                _config_value(database_config, "schema"),
                "database.schema",
            ),
            clock_extension_name=_require_non_empty_string(
                _config_value(clock_extension_cfg, "name"),
                "database.clock_extension.name",
            ),
        )


def _config_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"Missing required config field: {key}")
        return value[key]
    if not hasattr(value, key):
        raise ValueError(f"Missing required config field: {key}")
    return getattr(value, key)


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value
