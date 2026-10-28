# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
MPI role and rank layout helpers.

Role identifiers are part of the simulator protocol and are therefore code-owned.
Configuration controls only where those roles are placed in MPI ranks.
"""

from enum import StrEnum
from typing import Any


class Role(StrEnum):
    CONTROLLER = "controller"
    ANALYTICS = "analytics"
    POLICY = "policy"
    WORKER = "worker"


class MpiLayout:
    def __init__(self, role_ranks: dict[Role, int], worker_start_rank: int):
        self._role_ranks = role_ranks
        self.worker_start_rank = worker_start_rank

    @classmethod
    def from_config(cls, mpi_config: Any) -> "MpiLayout":
        processes = _config_value(mpi_config, "processes", "mpi.processes")

        _validate_process_names(processes)
        role_ranks = {
            Role.CONTROLLER: _required_rank(processes, Role.CONTROLLER),
            Role.ANALYTICS: _required_rank(processes, Role.ANALYTICS),
            Role.POLICY: _required_rank(processes, Role.POLICY),
        }
        worker_start_rank = _required_worker_start_rank(processes)
        _validate_layout(role_ranks, worker_start_rank)
        return cls(role_ranks=role_ranks, worker_start_rank=worker_start_rank)

    def rank(self, role: Role) -> int:
        if role == Role.WORKER:
            return self.worker_start_rank
        return self._role_ranks[role]

    def worker_ranks(self, size: int) -> list[int]:
        """Return all worker ranks in the configured communicator size."""
        if not isinstance(size, int) or size < self.worker_start_rank:
            raise ValueError("MPI size must include the worker start rank")
        return list(range(self.worker_start_rank, size))

    def is_worker(self, rank: int) -> bool:
        return rank >= self.worker_start_rank

    @property
    def minimum_size(self) -> int:
        return self.worker_start_rank + 1


def _validate_process_names(processes: Any) -> None:
    known = {role.value for role in Role}
    configured = set(_config_mapping(processes))
    unknown = configured - known
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown MPI process config: {names}")


def _required_rank(processes: Any, role: Role) -> int:
    role_config = _config_value(processes, role.value, f"mpi.processes.{role.value}")
    value = _config_value(role_config, "rank", f"mpi.processes.{role.value}.rank")
    return _require_non_negative_int(value, f"mpi.processes.{role.value}.rank")


def _required_worker_start_rank(processes: Any) -> int:
    worker_config = _config_value(
        processes,
        Role.WORKER.value,
        "mpi.processes.worker",
    )
    value = _config_value(
        worker_config,
        "start_rank",
        "mpi.processes.worker.start_rank",
    )
    return _require_non_negative_int(value, "mpi.processes.worker.start_rank")


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _validate_layout(role_ranks: dict[Role, int], worker_start_rank: int) -> None:
    used_ranks = list(role_ranks.values())
    if len(used_ranks) != len(set(used_ranks)):
        raise ValueError("MPI process ranks must be unique")

    conflicting_roles = [
        role.value for role, rank in role_ranks.items() if rank >= worker_start_rank
    ]
    if conflicting_roles:
        roles = ", ".join(conflicting_roles)
        raise ValueError(
            f"Non-worker MPI process ranks must be below worker.start_rank; conflicting roles: {roles}"
        )


def _config_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    raise ValueError(f"MPI config section must be an object, got {type(value).__name__}")


def _config_value(value: Any, key: str, field_name: str) -> Any:
    config = _config_mapping(value)
    if key not in config:
        raise ValueError(f"Missing required config field: {field_name}")
    return config[key]
