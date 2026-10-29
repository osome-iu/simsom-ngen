# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Setup DB subcomponent.

Owned by the setup/controller team. It plans the initial dataset for a run.
"""

import csv
import json
import math
import os
import re
import tempfile
import time
import numpy as np
from domain.function_config import function_entries, resolve_one, bind_function
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Protocol, Sequence

from domain.content_semantics import QUALITY_PROPERTY_KEY

from db.core import FollowerNetworkRecord, MAX_INTEGER_USER_ID, UserRecord
from db.controller import SetupDbDataset, SetupDbStream
from setup_progress import SetupProgressReporter


P_ACTIONS_KEY = "p_actions"
USER_QUALITY_DISTRIBUTION_KEY = "user_quality_distribution"
VECTOR_PROFILE_GENERATION_KEY = "vector_profile_generation_function"

WITHIN_TYPE_NETWORKING_PHASE = "within_type_networking"
CROSS_TYPE_NETWORKING_PHASE = "cross_type_networking"
PROGRESS_COUNTER_REFRESH_SECONDS = 1.0
PROGRESS_COUNTER_CHECK_STRIDE = 1_000
USER_PROFILE_CHUNK_SIZE = 100_000
COMPACT_ARRAY_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
COMPACT_ARRAY_BUILD_CHUNK_SIZE = 1_000_000
MAX_SUPPORTED_USERS = MAX_INTEGER_USER_ID


@dataclass(frozen=True)
class EdgeGenerationFunctionSpec:
    """Config-visible edge-generation function metadata."""

    name: str
    phases: tuple[str, ...]
    method_name: str
    requires_follower_counts: bool


@dataclass(frozen=True)
class ActivityDistributionFunctionSpec:
    """Config-visible activity distribution function metadata."""

    name: str
    function: Callable[[int], np.ndarray]


@dataclass(frozen=True)
class VectorProfileGenerationFunctionSpec:
    """Config-visible user vector-profile generator metadata."""

    name: str
    function: Callable[[int], np.ndarray]


@dataclass(frozen=True)
class NetworkSourceSpec:
    """Config-visible initial-network source metadata."""

    name: str
    method_name: str


class _CompactArray:
    """Use RAM for small arrays and a temporary memory map for large arrays."""

    def __init__(
        self,
        shape: int | tuple[int, ...],
        dtype: Any,
        *,
        zero: bool,
    ) -> None:
        normalized_shape = (shape,) if isinstance(shape, int) else shape
        item_count = math.prod(normalized_shape)
        self.nbytes = item_count * np.dtype(dtype).itemsize
        self.file = None
        self.storage_kind = "memory"
        if self.nbytes <= COMPACT_ARRAY_MEMORY_LIMIT_BYTES:
            constructor = np.zeros if zero else np.empty
            self.array = constructor(normalized_shape, dtype=dtype)
            return

        self.storage_kind = "temporary_file"
        self.file = tempfile.TemporaryFile(prefix="simsom_compact_")
        try:
            self.file.truncate(self.nbytes)
            self.array = np.memmap(
                self.file,
                dtype=dtype,
                mode="r+",
                shape=normalized_shape,
            )
        except BaseException:
            self.file.close()
            self.file = None
            raise

    def close(self) -> None:
        """Release memory-map and temporary-file resources."""
        array = getattr(self, "array", None)
        if isinstance(array, np.memmap):
            array._mmap.close()
        self.array = None
        if self.file is not None:
            self.file.close()
            self.file = None

    def __enter__(self) -> "_CompactArray":
        return self

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.close()


@dataclass
class CompactUserPopulation:
    """Exact user-type assignments stored as compact integer codes."""

    type_names: tuple[str, ...]
    type_counts: tuple[int, ...]
    assignment_codes: np.ndarray | None
    single_type_code: int | None
    user_id_dtype: Any
    assignment_storage: _CompactArray | None = None
    type_id_storage: tuple[_CompactArray | None, ...] = ()

    @property
    def user_count(self) -> int:
        """Return the exact configured population size."""
        return sum(self.type_counts)

    def codes_for_slice(self, start: int, stop: int) -> np.ndarray:
        """Return type codes for one bounded user-ID interval."""
        if self.assignment_codes is not None:
            return self.assignment_codes[start:stop]
        return np.full(
            stop - start,
            self.single_type_code,
            dtype=_smallest_unsigned_dtype(len(self.type_names) - 1),
        )

    def user_ids_by_type(self) -> dict[str, Sequence[int]]:
        """Build compact, ordered user-ID indexes needed by network generation."""
        if self.assignment_codes is None:
            return {
                user_type: (
                    range(1, self.user_count + 1)
                    if code == self.single_type_code
                    else ()
                )
                for code, user_type in enumerate(self.type_names)
            }
        if not self.type_id_storage:
            raise RuntimeError("compact type ID indexes were not planned")
        return {
            user_type: (
                ()
                if self.type_id_storage[code] is None
                else self.type_id_storage[code].array
            )
            for code, user_type in enumerate(self.type_names)
        }

    def close(self) -> None:
        """Release temporary compact assignment and type-index storage."""
        for storage in self.type_id_storage:
            if storage is not None:
                storage.close()
        self.type_id_storage = ()
        if self.assignment_storage is not None:
            self.assignment_storage.close()
            self.assignment_storage = None
        self.assignment_codes = None


class _NetworkGenerationState:
    """Retain only compact cross-phase state required by configured wrappers."""

    def __init__(self, user_count: int, *, track_follower_counts: bool) -> None:
        self.total_edges = 0
        self._follower_storage = (
            _CompactArray(user_count, np.uint64, zero=True)
            if track_follower_counts
            else None
        )
        self.follower_counts = (
            None
            if self._follower_storage is None
            else self._follower_storage.array
        )

    def record_edge(self, edge: FollowerNetworkRecord) -> None:
        """Update total edges and optional incoming-degree state."""
        self.total_edges += 1
        if self.follower_counts is not None:
            index = edge.friend_id - 1
            self.follower_counts[index] += 1

    def close(self) -> None:
        """Release optional follower-count storage."""
        self.follower_counts = None
        if self._follower_storage is not None:
            self._follower_storage.close()
            self._follower_storage = None

    def __enter__(self) -> "_NetworkGenerationState":
        return self

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.close()


class _WeightedIndexSampler:
    """Sample mutable positive weights in logarithmic time per selected index."""

    def __init__(self, weights: Any) -> None:
        self._weights_storage = None
        self._tree_storage = None
        self.weights = np.asarray(weights, dtype=np.float64)
        self.tree = np.empty_like(self.weights)
        self._build_tree()

    @classmethod
    def from_follower_counts(
        cls,
        candidate_ids: Sequence[int],
        follower_counts: np.ndarray,
        power_law_exponent: float,
    ) -> "_WeightedIndexSampler":
        """Build disk-capable weights for a compact cross-type candidate index."""
        sampler = cls.__new__(cls)
        sampler._weights_storage = None
        sampler._tree_storage = None
        sampler.weights = None
        sampler.tree = None
        try:
            sampler._weights_storage = _CompactArray(
                len(candidate_ids),
                np.float64,
                zero=False,
            )
            sampler._tree_storage = _CompactArray(
                len(candidate_ids),
                np.float64,
                zero=False,
            )
            sampler.weights = sampler._weights_storage.array
            sampler.tree = sampler._tree_storage.array
            for start in range(
                0,
                len(candidate_ids),
                COMPACT_ARRAY_BUILD_CHUNK_SIZE,
            ):
                stop = min(
                    start + COMPACT_ARRAY_BUILD_CHUNK_SIZE,
                    len(candidate_ids),
                )
                ids = _user_id_array_slice(candidate_ids, start, stop)
                sampler.weights[start:stop] = np.power(
                    follower_counts[ids - 1].astype(np.float64) + 1.0,
                    power_law_exponent,
                )
            sampler._build_tree()
        except BaseException:
            sampler.close()
            raise
        return sampler

    def close(self) -> None:
        """Release optional disk-backed weight and Fenwick arrays."""
        self.weights = None
        self.tree = None
        if self._weights_storage is not None:
            self._weights_storage.close()
            self._weights_storage = None
        if self._tree_storage is not None:
            self._tree_storage.close()
            self._tree_storage = None

    def __enter__(self) -> "_WeightedIndexSampler":
        return self

    def __exit__(self, _error_type, _error, _traceback) -> None:
        self.close()

    def _build_tree(self) -> None:
        """Build Fenwick values with compiled cumulative sums and bounded chunks."""
        np.cumsum(self.weights, out=self.tree)
        stop = len(self.tree)
        while stop:
            start = max(0, stop - COMPACT_ARRAY_BUILD_CHUNK_SIZE)
            indexes = np.arange(start + 1, stop + 1, dtype=np.int64)
            lower_indexes = indexes - (indexes & -indexes)
            values = np.asarray(self.tree[start:stop]).copy()
            has_lower = lower_indexes > 0
            values[has_lower] -= self.tree[lower_indexes[has_lower] - 1]
            self.tree[start:stop] = values
            stop = start

    def sample_without_replacement(
        self,
        count: int,
        blocked_indexes: set[int] | None = None,
    ) -> list[int]:
        """Return weighted distinct indexes while temporarily excluding blocks."""
        blocked_weights = []
        for index in blocked_indexes or ():
            weight = float(self.weights[index])
            if weight > 0.0:
                blocked_weights.append((index, weight))
                self.set_weight(index, 0.0)

        selected = []
        for _ in range(count):
            index = self._sample_index()
            if index is None:
                break
            selected.append(index)
            self.set_weight(index, 0.0)

        for index, weight in blocked_weights:
            self.set_weight(index, weight)
        return selected

    def set_weight(self, index: int, weight: float) -> None:
        """Replace one weight and update the Fenwick tree."""
        difference = float(weight) - float(self.weights[index])
        if difference == 0.0:
            return
        self.weights[index] = weight
        tree_index = index + 1
        while tree_index <= len(self.tree):
            self.tree[tree_index - 1] += difference
            tree_index += tree_index & -tree_index

    def _sample_index(self) -> int | None:
        total = self._total_weight()
        if total <= 0.0:
            return None
        value = float(np.random.random()) * total
        index = 0
        bit = 1 << (len(self.tree).bit_length() - 1)
        while bit:
            candidate = index + bit
            if candidate <= len(self.tree) and self.tree[candidate - 1] <= value:
                value -= float(self.tree[candidate - 1])
                index = candidate
            bit >>= 1
        return index

    def _total_weight(self) -> float:
        total = 0.0
        index = len(self.tree)
        while index:
            total += float(self.tree[index - 1])
            index -= index & -index
        return total


def network_source(name: str):
    """
    Attach a config selector label to an initial-network builder method.

    Declare source-specific inputs in the decorated method body unless they are
    shared config keys.
    """
    def decorator(function):
        setattr(
            function,
            "_simsom_network_source_spec",
            NetworkSourceSpec(
                name=name,
                method_name=function.__name__,
            ),
        )
        return function

    return decorator


def edge_generation_function(
    name: str,
    *,
    phases: tuple[str, ...],
    requires_follower_counts: bool = False,
):
    """
    Attach config-visible behavior and compact-state needs to an edge method.

    A rule must declare `requires_follower_counts=True` only when its sampling
    depends on incoming degree. The planner uses this metadata to avoid an
    otherwise unnecessary eight-byte-per-user state array.
    """
    def decorator(function):
        setattr(
            function,
            "_simsom_edge_generation_function_spec",
            EdgeGenerationFunctionSpec(
                name=name,
                phases=phases,
                method_name=function.__name__,
                requires_follower_counts=requires_follower_counts,
            ),
        )
        return function

    return decorator


def activity_distribution_function(name: str):
    """Attach a config selector label to an activity sampler."""
    def decorator(
        function: Callable[[int], np.ndarray],
    ) -> Callable[[int], np.ndarray]:
        setattr(
            function,
            "_simsom_activity_distribution_function_spec",
            ActivityDistributionFunctionSpec(
                name=name,
                function=function,
            ),
        )
        return function

    return decorator


def vector_profile_generation_function(name: str):
    """Register a config-visible user vector generator."""
    def decorator(function: Callable[[int], np.ndarray]):
        setattr(
            function,
            "_simsom_vector_profile_generation_function_spec",
            VectorProfileGenerationFunctionSpec(name=name, function=function),
        )
        return function
    return decorator


class SetupDbPlanner(Protocol):
    def build_stream(self) -> SetupDbStream:
        """Return one-pass initial rows for bounded-memory database loading.

        See docs/algorithms/controller.md#user-generation-math.
        """

    def build_dataset(self) -> SetupDbDataset:
        """Return the initial users and follower edges for a run.

        See docs/algorithms/controller.md#user-generation-math.
        """


class DefaultSetupDbPlanner:
    def __init__(
        self,
        setup_db_config: Any,
        user_behavior_config: Any,
        *,
        setup_progress: SetupProgressReporter | None = None,
    ) -> None:
        """Store setup and user behavior config used to build initial rows."""
        self.setup_db_config = setup_db_config
        self.user_behavior_config = user_behavior_config
        self.setup_progress = setup_progress
        self._progress_last_report_at = 0.0
        self._progress_last_percent = -1

    def build_dataset(self) -> SetupDbDataset:
        """Materialize the streaming plan for tests and small direct callers.

        See docs/algorithms/controller.md#user-generation-math.
        """
        stream = self.build_stream()
        try:
            return SetupDbDataset(
                users=list(stream.users),
                follower_network=list(stream.follower_network),
            )
        finally:
            stream.close()

    def build_stream(self) -> SetupDbStream:
        """Plan compact user metadata and return one-pass initial rows.

        See docs/algorithms/controller.md#user-generation-math.
        """
        source_name = _config_value(self.setup_db_config, "network_source")
        source_spec = self._network_source_spec(source_name)
        population = self._build_compact_user_population(
            build_type_index=source_spec.name == "synthetic"
        )
        return SetupDbStream(
            user_count=population.user_count,
            users=self._iter_users(population),
            follower_network=self._iter_planned_follower_network(
                population,
                source_spec,
            ),
            close_callback=population.close,
        )

    def _build_compact_user_population(
        self,
        *,
        build_type_index: bool = True,
    ) -> CompactUserPopulation:
        """Assign exact types in bounded chunks using compact disk-capable arrays."""
        user_types = self._user_types()
        global_p_actions = _config_value(self.user_behavior_config, P_ACTIONS_KEY)
        list(_config_items(global_p_actions))
        if self.setup_progress is not None:
            self.setup_progress.begin(
                "user_planning",
                progress_unit="users",
                step="user_type_assignment",
            )
        type_names = []
        type_counts = []
        total_active = 0
        for user_type, type_config in _config_items(user_types):
            type_names.append(str(user_type))
            type_counts.append(
                _require_non_negative_int(
                    _config_value(type_config, "num_nodes"),
                    f"setup_db.user_types.{user_type}.num_nodes",
                )
            )
            lurker_ratio = _require_lurker_ratio(
                _config_value(type_config, "lurker_ratio"),
                f"setup_db.user_types.{user_type}.lurker_ratio",
            )
            total_active += type_counts[-1] - round(type_counts[-1] * lurker_ratio)
            self._activity_distribution_spec(type_config)
            self._quality_distribution_parameters(type_config)
        total_users = sum(type_counts)
        if total_users < 1:
            raise ValueError("setup_db.user_types must configure at least one node")
        if total_active == 0:
            raise ValueError("setup_db.user_types leaves no active users after lurker rounding")
        if total_users > MAX_SUPPORTED_USERS:
            raise ValueError(
                f"configured users exceed the supported limit of {MAX_SUPPORTED_USERS}"
            )

        active_codes = [code for code, count in enumerate(type_counts) if count]
        assignments = None
        assignment_storage = None
        type_id_storage: tuple[_CompactArray | None, ...] = ()
        single_type_code = active_codes[0] if len(active_codes) == 1 else None
        user_id_dtype = _smallest_unsigned_dtype(total_users)
        allocated_type_storages: list[_CompactArray | None] = []
        try:
            if single_type_code is None:
                code_dtype = _smallest_unsigned_dtype(len(type_names) - 1)
                assignment_storage = _CompactArray(
                    total_users,
                    code_dtype,
                    zero=False,
                )
                assignments = assignment_storage.array
                if build_type_index:
                    for count in type_counts:
                        allocated_type_storages.append(
                            _CompactArray(count, user_id_dtype, zero=False)
                            if count
                            else None
                        )
                    type_id_storage = tuple(allocated_type_storages)
            if self.setup_progress is not None:
                self.setup_progress.update(
                    progress_current=0,
                    progress_total=total_users,
                    step="user_type_assignment_and_shuffle",
                    assignment_storage_bytes=(
                        0 if assignments is None else assignments.nbytes
                    ),
                    type_index_storage_bytes=sum(
                        0 if storage is None else storage.nbytes
                        for storage in type_id_storage
                    ),
                    user_id_width_bits=np.dtype(user_id_dtype).itemsize * 8,
                )
            if assignments is not None:
                self._reset_loop_progress()
                remaining_counts = list(type_counts)
                type_offsets = [0] * len(type_counts)
                for start in range(0, total_users, USER_PROFILE_CHUNK_SIZE):
                    stop = min(start + USER_PROFILE_CHUNK_SIZE, total_users)
                    chunk_counts = _sample_multivariate_hypergeometric(
                        remaining_counts,
                        stop - start,
                    )
                    chunk_codes = np.repeat(
                        np.arange(len(type_names), dtype=assignments.dtype),
                        chunk_counts,
                    )
                    np.random.shuffle(chunk_codes)
                    assignments[start:stop] = chunk_codes
                    if type_id_storage:
                        for code, storage in enumerate(type_id_storage):
                            if storage is None:
                                continue
                            positions = np.flatnonzero(chunk_codes == code)
                            offset = type_offsets[code]
                            storage.array[offset : offset + len(positions)] = (
                                positions.astype(user_id_dtype) + start + 1
                            )
                            type_offsets[code] += len(positions)
                    remaining_counts = [
                        remaining - selected
                        for remaining, selected in zip(remaining_counts, chunk_counts)
                    ]
                    self._update_loop_progress(stop, total_users)
        except BaseException:
            for storage in allocated_type_storages:
                if storage is not None:
                    storage.close()
            if assignment_storage is not None:
                assignment_storage.close()
            raise
        if self.setup_progress is not None:
            self.setup_progress.complete(
                progress_current=total_users,
                progress_total=total_users,
                assignment_storage_bytes=(
                    0 if assignments is None else assignments.nbytes
                ),
                type_index_storage_bytes=sum(
                    0 if storage is None else storage.nbytes
                    for storage in type_id_storage
                ),
                user_id_width_bits=np.dtype(user_id_dtype).itemsize * 8,
            )
        return CompactUserPopulation(
            type_names=tuple(type_names),
            type_counts=tuple(type_counts),
            assignment_codes=assignments,
            single_type_code=single_type_code,
            user_id_dtype=user_id_dtype,
            assignment_storage=assignment_storage,
            type_id_storage=type_id_storage,
        )

    def _iter_users(
        self,
        population: CompactUserPopulation,
    ) -> Iterator[UserRecord]:
        """Yield vectorized profile chunks while retaining only compact metadata.

        See docs/algorithms/controller.md#user-generation-math.
        """
        global_p_actions = _config_mapping(
            _config_value(self.user_behavior_config, P_ACTIONS_KEY)
        )
        user_types = self._user_types()
        remaining_users = list(population.type_counts)
        remaining_lurkers = [
            round(
                count
                * _require_lurker_ratio(
                    _config_value(user_types[user_type], "lurker_ratio"),
                    f"setup_db.user_types.{user_type}.lurker_ratio",
                )
            )
            for user_type, count in zip(population.type_names, population.type_counts)
        ]
        total_users = population.user_count
        if self.setup_progress is not None:
            self.setup_progress.begin(
                "user_generation",
                progress_unit="users",
                progress_total=total_users,
                step="profile_generation_and_database_load",
            )
            self._reset_loop_progress()
        for start in range(0, total_users, USER_PROFILE_CHUNK_SIZE):
            stop = min(start + USER_PROFILE_CHUNK_SIZE, total_users)
            chunk_size = stop - start
            vector_profiles = self._draw_vector_profiles(chunk_size)
            type_codes = population.codes_for_slice(start, stop)
            activity_levels = np.zeros(chunk_size, dtype=np.float64)
            qualities = np.empty(chunk_size, dtype=np.float64)
            action_samples = self._sample_user_action_probabilities(
                global_p_actions, chunk_size,
            )

            for code, user_type in enumerate(population.type_names):
                positions = np.flatnonzero(type_codes == code)
                if not len(positions):
                    continue
                type_config = user_types[user_type]
                lurker_count = self._sample_chunk_lurker_count(
                    remaining_users[code],
                    remaining_lurkers[code],
                    len(positions),
                )
                local_lurkers = np.zeros(len(positions), dtype=np.bool_)
                if lurker_count:
                    selected = np.random.choice(
                        len(positions),
                        size=lurker_count,
                        replace=False,
                    )
                    local_lurkers[np.atleast_1d(selected)] = True
                active_positions = positions[~local_lurkers]
                if len(active_positions):
                    activity_levels[active_positions] = self._draw_activity_levels(
                        type_config,
                        len(active_positions),
                    )
                qualities[positions] = self._draw_normalized_two_cluster_inverse_many(
                    type_config,
                    len(positions),
                )
                remaining_users[code] -= len(positions)
                remaining_lurkers[code] -= lurker_count

            for offset, code_value in enumerate(type_codes):
                yield UserRecord(
                    user_id=start + offset + 1,
                    vector_profile=vector_profiles[offset].tolist(),
                    activity_level=float(activity_levels[offset]),
                    user_type=population.type_names[int(code_value)],
                    user_behavior={
                        P_ACTIONS_KEY: {
                            action_type: float(values[offset])
                            for action_type, values in action_samples.items()
                        },
                        QUALITY_PROPERTY_KEY: float(qualities[offset]),
                    },
                )
            self._update_loop_progress(stop, total_users)

    @staticmethod
    def _sample_chunk_lurker_count(
        remaining_users: int,
        remaining_lurkers: int,
        chunk_users: int,
    ) -> int:
        """Draw an exact uniform lurker subset one bounded chunk at a time."""
        if chunk_users == remaining_users:
            return remaining_lurkers
        if remaining_lurkers == 0:
            return 0
        if remaining_lurkers == remaining_users:
            return chunk_users
        return int(
            np.random.hypergeometric(
                remaining_lurkers,
                remaining_users - remaining_lurkers,
                chunk_users,
            )
        )

    def _iter_planned_follower_network(
        self,
        population: CompactUserPopulation,
        source_spec: NetworkSourceSpec,
    ) -> Iterator[FollowerNetworkRecord]:
        """Generate network rows lazily after the user stream is consumed."""
        if source_spec.name == "synthetic":
            yield from self._iter_synthetic_follower_network_from_ids(
                population.user_ids_by_type()
            )
            return
        network_file = self._network_file_path()
        if self.setup_progress is not None:
            self.setup_progress.begin(
                "network_import",
                source="file",
                network_file=os.path.relpath(network_file, Path.cwd()),
            )
        edge_pairs = self._load_network_edge_pairs(network_file)
        edge_count = 0
        for index, (user_id, friend_id) in enumerate(edge_pairs, start=1):
            if not 1 <= user_id <= population.user_count:
                raise ValueError(
                    f"{network_file} edge {index} references unknown user_id "
                    f"{user_id}"
                )
            if not 1 <= friend_id <= population.user_count:
                raise ValueError(
                    f"{network_file} edge {index} references unknown friend_id "
                    f"{friend_id}"
                )
            if user_id == friend_id:
                raise ValueError(f"{network_file} edge {index} is a self-link")
            edge_count += 1
            yield FollowerNetworkRecord(user_id=user_id, friend_id=friend_id)
        if self.setup_progress is not None:
            self.setup_progress.complete(total_edges=edge_count)

    @network_source("file")
    def _build_file_follower_network(
        self,
        users: list[UserRecord],
    ) -> list[FollowerNetworkRecord]:
        """Load directed friend links from `setup_db.network_file`.

        Input users provide the allowed simulator user IDs. Output is a list of
        validated `FollowerNetworkRecord` rows whose `user_id` has added
        `friend_id` as a friend. Imported files must not contain unknown users,
        self-links, or duplicate directed edges.

        See docs/algorithms/controller.md#user-generation-math.
        """
        _require_list("users", users, UserRecord)
        network_file = self._network_file_path()
        if self.setup_progress is not None:
            self.setup_progress.begin(
                "network_import",
                source="file",
                network_file=os.path.relpath(network_file, Path.cwd()),
            )
        edge_pairs = self._load_network_edge_pairs(network_file)
        edges = self._validated_imported_edges(
            edge_pairs=edge_pairs,
            users=users,
            source_name=str(network_file),
        )
        if self.setup_progress is not None:
            self.setup_progress.complete(total_edges=len(edges))
        return edges

    @network_source("synthetic")
    def _build_synthetic_follower_network(
        self,
        users: list[UserRecord],
    ) -> list[FollowerNetworkRecord]:
        """Build typed directed friend links for the planned users.

        See docs/algorithms/controller.md#user-generation-math.
        """
        _require_list("users", users, UserRecord)
        return list(
            self._iter_synthetic_follower_network_from_ids(
                self._users_by_type(users)
            )
        )

    def _build_synthetic_follower_network_from_ids(
        self,
        users_by_type: dict[str, Sequence[int]],
    ) -> list[FollowerNetworkRecord]:
        """Materialize compact-ID generation for tests and small direct callers."""
        return list(self._iter_synthetic_follower_network_from_ids(users_by_type))

    def _iter_synthetic_follower_network_from_ids(
        self,
        users_by_type: dict[str, Sequence[int]],
    ) -> Iterator[FollowerNetworkRecord]:
        """Yield synthetic edges while retaining only wrapper-required state."""
        user_count = sum(len(user_ids) for user_ids in users_by_type.values())
        if user_count < 2:
            return

        user_types = self._user_types()
        track_follower_counts = self._network_requires_follower_counts(
            user_types,
            users_by_type,
        )
        with _NetworkGenerationState(
            user_count,
            track_follower_counts=track_follower_counts,
        ) as state:
            for phase in (
                WITHIN_TYPE_NETWORKING_PHASE,
                CROSS_TYPE_NETWORKING_PHASE,
            ):
                for user_type, type_config in _config_items(user_types):
                    function_name = self._edge_generation_function_name(
                        type_config,
                        phase,
                    )
                    yield from self._run_edge_generation_function(
                        phase=phase,
                        function_name=function_name,
                        source_user_type=str(user_type),
                        users_by_type=users_by_type,
                        state=state,
                    )

    def _network_requires_follower_counts(
        self,
        user_types: dict[str, Any],
        users_by_type: dict[str, Sequence[int]],
    ) -> bool:
        """Return whether any runnable wrapper needs incoming-degree state."""
        populated_types = {
            user_type
            for user_type, user_ids in users_by_type.items()
            if len(user_ids)
        }
        for user_type, type_config in _config_items(user_types):
            if str(user_type) not in populated_types:
                continue
            for phase in (
                WITHIN_TYPE_NETWORKING_PHASE,
                CROSS_TYPE_NETWORKING_PHASE,
            ):
                function_name = self._edge_generation_function_name(
                    type_config,
                    phase,
                )
                spec = self._edge_generation_spec(function_name, phase)
                has_targets = (
                    phase != CROSS_TYPE_NETWORKING_PHASE
                    or bool(populated_types - {str(user_type)})
                )
                if spec.requires_follower_counts and has_targets:
                    return True
        return False

    def _run_edge_generation_function(
        self,
        *,
        phase: str,
        function_name: str,
        source_user_type: str,
        users_by_type: dict[str, Sequence[int]],
        state: _NetworkGenerationState,
    ) -> Iterator[FollowerNetworkRecord]:
        """
        Dispatch one configured edge function for a network phase.

        Inputs are the phase name, function name, source user type, typed user
        index, and compact cross-phase state. Output is a one-pass edge stream.
        """
        spec = self._edge_generation_spec(function_name, phase)
        generation_function = getattr(self, spec.method_name)
        edge_config = _config_mapping(_config_value(self._user_types()[source_user_type], "edge_generation"))
        entries = function_entries(_config_value(edge_config, phase + "_function"), phase + "_function")
        generation_function = bind_function(
            function_name, entries[function_name], generation_function, phase + "_function",
            runtime_count=0, runtime_keywords=("source_user_type", "users_by_type", "state", "progress"),
        )
        source_user_count = len(users_by_type.get(source_user_type, []))
        total_network_users = sum(len(user_ids) for user_ids in users_by_type.values())
        phase_start_edge_count = state.total_edges
        if self.setup_progress is not None:
            self.setup_progress.begin(
                "network_generation",
                progress_total=source_user_count,
                progress_unit="source_users",
                phase=phase,
                user_type=source_user_type,
                network_total_users=total_network_users,
                function=function_name,
                phase_edges=0,
                total_edges=state.total_edges,
            )
            self._reset_loop_progress()

        def report_progress(completed: int) -> None:
            if self.setup_progress is not None:
                self.setup_progress.update(
                    progress_current=completed,
                    phase_edges=state.total_edges - phase_start_edge_count,
                    total_edges=state.total_edges,
                )

        for edge in generation_function(
            source_user_type=source_user_type,
            users_by_type=users_by_type,
            state=state,
            progress=(report_progress if self.setup_progress is not None else None),
        ):
            state.record_edge(edge)
            yield edge
        if self.setup_progress is not None:
            self.setup_progress.complete(
                phase_edges=state.total_edges - phase_start_edge_count,
                total_edges=state.total_edges,
            )

    def _run_within_type_edge_generation_function(
        self,
        *,
        function_name: str,
        source_user_type: str,
        users_by_type: dict[str, Sequence[int]],
        state: _NetworkGenerationState,
    ) -> Iterator[FollowerNetworkRecord]:
        """
        Run the single configured within-type function for one user type.

        This is the within-type selector dispatch point. Within-type functions
        should only choose targets from the same user type unless their own
        contract states otherwise.
        """
        return self._run_edge_generation_function(
            phase=WITHIN_TYPE_NETWORKING_PHASE,
            function_name=function_name,
            source_user_type=source_user_type,
            users_by_type=users_by_type,
            state=state,
        )

    def _run_cross_type_edge_generation_function(
        self,
        *,
        function_name: str,
        source_user_type: str,
        users_by_type: dict[str, Sequence[int]],
        state: _NetworkGenerationState,
    ) -> Iterator[FollowerNetworkRecord]:
        """
        Run the single configured cross-type function for one user type.

        This is the cross-type selector dispatch point. Cross-type functions
        should choose targets outside the source user type unless their own
        contract states otherwise.
        """
        return self._run_edge_generation_function(
            phase=CROSS_TYPE_NETWORKING_PHASE,
            function_name=function_name,
            source_user_type=source_user_type,
            users_by_type=users_by_type,
            state=state,
        )

    @edge_generation_function(
        "no_edges",
        phases=(WITHIN_TYPE_NETWORKING_PHASE, CROSS_TYPE_NETWORKING_PHASE),
    )
    def _generate_no_edges(
        self,
        *,
        source_user_type: str,
        users_by_type: dict[str, Sequence[int]],
        state: _NetworkGenerationState,
        progress: Callable[[int], None] | None = None,
    ) -> Iterator[FollowerNetworkRecord]:
        """Generate no edges for one source user type.

        Inputs match other edge-generation functions so `no_edges` can be used
        as a phase wrapper. Output is an empty edge stream.

        See docs/algorithms/controller.md#no_edges.
        """
        return iter(())

    @edge_generation_function(
        "density_uniform_sparse",
        phases=(WITHIN_TYPE_NETWORKING_PHASE,),
    )
    def _generate_density_uniform_edges(
        self,
        *,
        source_user_type: str,
        users_by_type: dict[str, Sequence[int]],
        state: _NetworkGenerationState,
        density: float,
        progress: Callable[[int], None] | None = None,
    ) -> Iterator[FollowerNetworkRecord]:
        """Append within-type edges sampled uniformly for one source user type.

        Configuration supplies the density explicitly.
        Output is a one-pass edge stream. Per-source target sampling prevents
        duplicates and self-links without global edge objects.

        See docs/algorithms/controller.md#density_uniform_sparse.
        """
        parameters = {"density": density}
        source_users = _user_id_sequence(users_by_type.get(source_user_type, ()))
        for index, user_id in enumerate(source_users, start=1):
            user_id = int(user_id)
            blocked_indexes = {index - 1}
            candidate_count = len(source_users) - 1
            target_count = self._target_edge_count(candidate_count, parameters)
            target_indexes = self._sample_available_indexes(
                len(source_users),
                target_count,
                blocked_indexes,
            )
            for target_index in target_indexes:
                target_id = int(source_users[target_index])
                yield FollowerNetworkRecord(user_id=user_id, friend_id=target_id)
            self._update_loop_progress(index, len(source_users), progress)

    def _generate_random_walk_edges(
        self,
        *,
        source_user_type: str,
        users_by_type: dict[str, Sequence[int]],
        parameters: dict[str, Any],
        state: _NetworkGenerationState,
        progress: Callable[[int], None] | None = None,
    ) -> Iterator[FollowerNetworkRecord]:
        """
        Add within-type links using a random-walk growth model.

        The algorithm is based on *Random Walk Model*, Section 5.5.3,
        https://doi.org/10.1017/9781108653947, and follows the corresponding
        growth model:

        1. Start with a fully connected directed group of `seed_size` users.
        2. For every later user, choose one earlier user uniformly.
        3. For each remaining link, independently choose either a neighbor of
           the first selected user or another earlier user uniformly.
        4. Sample without replacement when multiple choices are available.

        Parameters:
            seed_size: Number of users in the initial fully connected seed. It
                must not be smaller than `avg_n_friend`.
            avg_n_friend: Number of outgoing links created for each new user.
            probability_follow: Chance that each remaining link follows one of
                the selected user's outgoing links.
        """
        source_users = _user_id_sequence(users_by_type.get(source_user_type, ()))
        seed_size = _require_positive_int(
            _config_value(parameters, "seed_size"),
            "seed_size",
        )
        avg_n_friend = _require_positive_int(
            _config_value(parameters, "avg_n_friend"),
            "avg_n_friend",
        )
        if seed_size < avg_n_friend:
            raise ValueError("seed_size must not be smaller than avg_n_friend")
        probability_follow = _require_probability(
            _config_value(parameters, "probability_follow"),
            "probability_follow",
        )
        if len(source_users) < 2: # can't self-link
            return

        seed_size = min(len(source_users), seed_size) # edge case: smaller network than seed_size
        adjacency_width = max(avg_n_friend, seed_size - 1)
        adjacency_dtype = _smallest_unsigned_dtype(len(source_users))
        with _CompactArray(
            (len(source_users), adjacency_width),
            adjacency_dtype,
            zero=True,
        ) as adjacency_storage:
            adjacency = adjacency_storage.array
            if self.setup_progress is not None:
                self.setup_progress.update(
                    adjacency_storage=adjacency_storage.storage_kind,
                    adjacency_storage_bytes=adjacency_storage.nbytes,
                    adjacency_index_width_bits=np.dtype(adjacency_dtype).itemsize * 8,
                )

            for source_index in range(seed_size):
                source_id = int(source_users[source_index])
                write_index = 0
                for target_index in range(seed_size):
                    if source_index == target_index:
                        continue
                    target_id = int(source_users[target_index])
                    adjacency[source_index, write_index] = target_index + 1
                    write_index += 1
                    yield FollowerNetworkRecord(
                        user_id=source_id,
                        friend_id=target_id,
                    )
            self._update_loop_progress(seed_size, len(source_users), progress)

            for source_index in range(seed_size, len(source_users)):
                source_id = int(source_users[source_index])
                first_target_index = int(np.random.randint(0, source_index))
                first_target_id = int(source_users[first_target_index])
                selected_target_indexes = {first_target_index}
                adjacency[source_index, 0] = first_target_index + 1
                yield FollowerNetworkRecord(
                    user_id=source_id,
                    friend_id=first_target_id,
                )

                remaining_count = avg_n_friend - 1
                n_followed_friends = int(
                    np.random.binomial(remaining_count, probability_follow)
                )
                closure_candidates = []
                for encoded_target_index in adjacency[first_target_index]:
                    if encoded_target_index == 0:
                        continue
                    target_index = int(encoded_target_index) - 1
                    if (
                        target_index < source_index
                        and target_index not in selected_target_indexes
                    ):
                        target_id = int(source_users[target_index])
                        closure_candidates.append((target_index, target_id))

                closure_count = min(n_followed_friends, len(closure_candidates))
                selected_targets = []
                if closure_count:
                    closure_indexes = np.random.choice(
                        len(closure_candidates),
                        size=closure_count,
                        replace=False,
                    )
                    for candidate_index in np.atleast_1d(closure_indexes):
                        target_index, target_id = closure_candidates[
                            int(candidate_index)
                        ]
                        selected_target_indexes.add(target_index)
                        selected_targets.append((target_index, target_id))

                random_count = remaining_count - len(selected_targets)
                if random_count:
                    random_indexes = self._sample_available_indexes(
                        source_index,
                        random_count,
                        selected_target_indexes,
                    )
                    selected_targets.extend(
                        (target_index, int(source_users[target_index]))
                        for target_index in random_indexes
                    )

                for target_offset, (target_index, target_id) in enumerate(
                    selected_targets,
                    start=1,
                ):
                    adjacency[source_index, target_offset] = target_index + 1
                    yield FollowerNetworkRecord(
                        user_id=source_id,
                        friend_id=target_id,
                    )
                self._update_loop_progress(
                    source_index + 1,
                    len(source_users),
                    progress,
                )

    @edge_generation_function(
        "random_walk",
        phases=(WITHIN_TYPE_NETWORKING_PHASE,)
    )
    def _generate_random_walk(
        self,
        *,
        source_user_type: str,
        users_by_type: dict[str, Sequence[int]],
        state: _NetworkGenerationState,
        seed_size: int,
        avg_n_friend: int,
        probability_follow: float,
        progress: Callable[[int], None] | None = None,
    ) -> Iterator[FollowerNetworkRecord]:
        """Run random-walk growth with the configured seed, link count and closure probability.

        See docs/algorithms/controller.md#random_walk.
        """
        return self._generate_random_walk_edges(
            source_user_type=source_user_type,
            users_by_type=users_by_type,
            parameters={
                "seed_size": seed_size,
                "avg_n_friend": avg_n_friend,
                "probability_follow": probability_follow,
            },
            state=state,
            progress=progress,
        )

    @edge_generation_function(
        "power_law_by_followers_sparse",
        phases=(CROSS_TYPE_NETWORKING_PHASE,),
        requires_follower_counts=True,
    )
    def _generate_power_law_by_followers_edges(
        self,
        *,
        source_user_type: str,
        users_by_type: dict[str, Sequence[int]],
        state: _NetworkGenerationState,
        density: float,
        power_law_exponent: float,
        progress: Callable[[int], None] | None = None,
    ) -> Iterator[FollowerNetworkRecord]:
        """Append cross-type edges weighted by target follower counts.

        Configuration supplies density and the power-law exponent. Compact follower
        counts include edges produced by earlier phases.

        See docs/algorithms/controller.md#power_law_by_followers_sparse.
        """
        parameters = {
            "density": density,
            "power_law_exponent": power_law_exponent,
        }
        source_users = _user_id_sequence(users_by_type.get(source_user_type, ()))
        if not len(source_users):
            return
        candidate_sequences = [
            _user_id_sequence(candidates)
            for candidate_type, candidates in users_by_type.items()
            if candidate_type != source_user_type and len(candidates)
        ]
        if not candidate_sequences:
            return
        cross_type_candidates = _concatenate_user_id_sequences(candidate_sequences)
        follower_counts = state.follower_counts
        if follower_counts is None:
            raise RuntimeError("power-law generation requires follower counts")
        power_law_exponent = _require_positive_number(
            _config_value(parameters, "power_law_exponent"),
            "power_law_exponent",
        )
        with _WeightedIndexSampler.from_follower_counts(
            cross_type_candidates,
            follower_counts,
            power_law_exponent,
        ) as sampler:
            for index, user_id_value in enumerate(source_users, start=1):
                user_id = int(user_id_value)
                target_count = self._target_edge_count(
                    len(cross_type_candidates),
                    parameters,
                )
                selected_indexes = sampler.sample_without_replacement(target_count)
                for target_index in selected_indexes:
                    target_id = int(cross_type_candidates[target_index])
                    yield FollowerNetworkRecord(
                        user_id=user_id,
                        friend_id=target_id,
                    )
                    sampler.set_weight(
                        target_index,
                        (float(follower_counts[target_id - 1]) + 1.0)
                        ** power_law_exponent,
                    )
                self._update_loop_progress(index, len(source_users), progress)

    def _update_loop_progress(
        self,
        completed: int,
        total: int,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """Refresh large-loop counters by elapsed time and whole-percent changes."""
        if progress is None and self.setup_progress is None:
            return
        percent = 100 if total == 0 else completed * 100 // total
        stride = min(
            PROGRESS_COUNTER_CHECK_STRIDE,
            max(1, total // 1_000),
        )
        if (
            completed != total
            and percent == self._progress_last_percent
            and completed % stride != 0
        ):
            return
        now = time.monotonic()
        if (
            completed != total
            and percent == self._progress_last_percent
            and now - self._progress_last_report_at < PROGRESS_COUNTER_REFRESH_SECONDS
        ):
            return
        if progress is not None:
            progress(completed)
        else:
            self.setup_progress.update(progress_current=completed)
        self._progress_last_report_at = now
        self._progress_last_percent = percent

    def _reset_loop_progress(self) -> None:
        """Start progress-counter timing for one user or network stage."""
        self._progress_last_report_at = time.monotonic()
        self._progress_last_percent = 0

    @classmethod
    def _activity_distribution_spec(cls, type_config: Any) -> ActivityDistributionFunctionSpec:
        """Bind activity inputs; see docs/algorithms/configuration.md#setup-functions."""
        config = _config_value(type_config, "activity_level_distribution")
        registry = {name: spec.function for name, spec in cls._activity_distribution_registry().items()}
        name, function = resolve_one(config, registry, "activity_level_distribution")
        return ActivityDistributionFunctionSpec(name=name, function=function)

    @classmethod
    def _vector_profile_generation_spec(
        cls,
        config: Any,
    ) -> VectorProfileGenerationFunctionSpec:
        """Bind a configured vector-profile generator."""
        registry = {
            name: spec.function
            for name, spec in cls._vector_profile_generation_registry().items()
        }
        name, function = resolve_one(
            config,
            registry,
            "user_behavior.vector_profile_generation_function",
        )
        return VectorProfileGenerationFunctionSpec(name=name, function=function)

    @classmethod
    def _draw_activity_levels(cls, type_config: Any, count: int) -> np.ndarray:
        """Draw a bounded vector of positive active-user activity levels."""
        return cls._activity_distribution_spec(type_config).function(count)

    def _draw_vector_profiles(self, count: int) -> np.ndarray:
        """Generate one vector representation per user in a chunk."""
        config = _config_value(
            self.user_behavior_config,
            VECTOR_PROFILE_GENERATION_KEY,
        )
        function = self._vector_profile_generation_spec(config).function
        values = np.asarray(function(count), dtype=np.float64)
        if values.ndim != 2 or values.shape[0] != count or values.shape[1] == 0:
            raise ValueError(
                "vector_profile_generation_function must return a non-empty "
                "[count, dimension] array"
            )
        if not np.all(np.isfinite(values)):
            raise ValueError("vector profile values must be finite")
        return values

    @classmethod
    def _draw_activity_level(cls, type_config: Any) -> float:
        """
        Draw a strictly positive activity level for one active initialized user.

        Input is one user-type config containing an
        `activity_level_distribution` function-input object. Output is a float
        greater than zero; exact `0.0` is reserved for setup-designated
        zero-activity users.
        """
        return float(cls._draw_activity_levels(type_config, 1)[0])

    @staticmethod
    @activity_distribution_function("power_law_activity")
    def _draw_power_law_activity(
        count: int, *, power_law_exponent: float, minimum_activity_level: float,
        maximum_activity_level: float,
    ) -> np.ndarray:
        """Draw from the default positive-support power-law activity wrapper.

        Configuration supplies the exponent and positive output bounds explicitly.
        Because `minimum_activity_level` is greater than zero, active users never
        collide with the exact `0.0` zero-activity sentinel.

        See docs/algorithms/domain_activity.md#activity-level-meaning.
        """
        minimum_activity_level = _require_positive_number(
            minimum_activity_level,
            "power_law_activity.minimum_activity_level",
        )
        maximum_activity_level = _require_positive_number(
            maximum_activity_level,
            "power_law_activity.maximum_activity_level",
        )
        if maximum_activity_level < minimum_activity_level:
            raise ValueError(
                "power_law_activity.maximum_activity_level must be "
                "greater than or equal to minimum_activity_level"
            )
        if power_law_exponent == -1:
            raise ValueError(
                "power_law_activity.power_law_exponent cannot be "
                "equal to -1"
            )
        values = ((maximum_activity_level ** (power_law_exponent + 1) - minimum_activity_level ** (power_law_exponent + 1)) 
                  * np.random.uniform(size=count) + 
                  minimum_activity_level ** (power_law_exponent + 1)) ** (1 / (power_law_exponent + 1))
        return values

    @staticmethod
    @vector_profile_generation_function("pca_7param_social_media_archive_profile")
    def _pca_7param_social_media_archive_profile(
        count: int, *, dimension: int, pc1: dict, pc2_to_pc10: dict,
    ) -> np.ndarray:
        """Sample archive-calibrated profiles with a PC1 mixture and shared
        PC2-PC10 Gaussian.
        """
        if dimension != 10 or isinstance(dimension, bool):
            raise ValueError("pca_7param_social_media_archive_profile.dimension must be 10")
        pc1 = _plain_config(pc1)
        pc2_to_pc10 = _plain_config(pc2_to_pc10)
        components = [pc1["component1"], pc1["component2"]]
        means = np.asarray([component["mu"] for component in components], dtype=float)
        sigmas = np.asarray([component["sigma"] for component in components], dtype=float)
        weights = np.asarray([component["weight"] for component in components], dtype=float)
        shared_mu = float(pc2_to_pc10["mu"])
        shared_sigma = float(pc2_to_pc10["sigma"])
        if (
            not np.all(np.isfinite(means))
            or not np.all(np.isfinite(sigmas)) or np.any(sigmas <= 0)
            or not np.all(np.isfinite(weights)) or np.any(weights < 0)
            or not math.isfinite(float(weights.sum())) or weights.sum() <= 0
            or not math.isfinite(shared_mu)
            or not math.isfinite(shared_sigma) or shared_sigma <= 0
        ):
            raise ValueError("pca_7param_social_media_archive_profile parameters are invalid")
        selected = np.random.choice(2, size=count, p=weights / weights.sum())
        profiles = np.empty((count, dimension), dtype=np.float64)
        profiles[:, 0] = np.random.normal(means[selected], sigmas[selected])
        profiles[:, 1:] = np.random.normal(shared_mu, shared_sigma, size=(count, dimension - 1))
        return profiles

    @staticmethod
    @vector_profile_generation_function("constant_vector_profile")
    def _constant_vector_profile(
        count: int, *, dimension: int, value: float,
    ) -> np.ndarray:
        """Return the same vector for every generated user.

        See ``docs/algorithms/controller.md#user-generation-math``. This is a
        minimal wiring example; it is not calibrated to empirical data.
        """
        if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
            raise ValueError("constant_vector_profile.dimension must be a positive integer")
        if not math.isfinite(value):
            raise ValueError("constant_vector_profile.value must be finite")
        return np.full((count, dimension), value, dtype=np.float64)

    @staticmethod
    @vector_profile_generation_function("uniform_random_vector_profile")
    def _uniform_random_vector_profile(
        count: int, *, dimension: int, minimum: float, maximum: float,
    ) -> np.ndarray:
        """Draw bounded vectors independently for each generated user.

        See ``docs/algorithms/controller.md#user-generation-math``. This is a
        minimal wiring example; it is not calibrated to empirical data.
        """
        if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
            raise ValueError("uniform_random_vector_profile.dimension must be a positive integer")
        if not math.isfinite(minimum) or not math.isfinite(maximum) or maximum < minimum:
            raise ValueError("uniform_random_vector_profile bounds are invalid")
        return np.random.uniform(minimum, maximum, size=(count, dimension))

    @staticmethod
    def _probabilistically_round(value: float) -> int:
        """Round a non-negative value up with probability equal to its fraction."""
        base = math.floor(value)
        fraction = value - base
        return base + 1 if np.random.random() < fraction else base

    @staticmethod
    def _sample_available_indexes(
        population_size: int,
        count: int,
        blocked_indexes: set[int] | None = None,
    ) -> list[int]:
        """Uniformly sample distinct indexes without building a sparse population."""
        blocked = {
            index
            for index in blocked_indexes or ()
            if 0 <= index < population_size
        }
        available_count = population_size - len(blocked)
        sample_count = min(count, available_count)
        if sample_count <= 0:
            return []
        if sample_count == 1:
            while True:
                index = int(np.random.randint(0, population_size))
                if index not in blocked:
                    return [index]
        if sample_count * 4 > available_count or available_count * 2 < population_size:
            available = [
                index for index in range(population_size) if index not in blocked
            ]
            if sample_count == available_count:
                return available
            sampled = np.random.choice(
                len(available),
                size=sample_count,
                replace=False,
            )
            return [available[int(index)] for index in np.atleast_1d(sampled)]

        selected = []
        selected_set = set()
        while len(selected) < sample_count:
            remaining = sample_count - len(selected)
            draws = np.random.randint(
                0,
                population_size,
                size=max(2, remaining * 2),
            )
            for raw_index in draws:
                index = int(raw_index)
                if index in blocked or index in selected_set:
                    continue
                selected.append(index)
                selected_set.add(index)
                if len(selected) == sample_count:
                    break
        return selected

    @staticmethod
    def _target_edge_count(candidate_count: int, parameters: dict[str, Any]) -> int:
        """Return the configured target count, capped by valid candidates."""
        if candidate_count <= 0:
            return 0
        if "target_edge_count" in parameters:
            return min(
                candidate_count,
                _require_non_negative_int(
                    parameters["target_edge_count"],
                    "target_edge_count",
                ),
            )
        density = _require_non_negative_number(
            _config_value(parameters, "density"),
            "density",
        )
        return min(
            candidate_count,
            DefaultSetupDbPlanner._probabilistically_round(candidate_count * density),
        )

    def _network_file_path(self) -> Path:
        """
        Return the configured network import path.

        Relative paths are resolved from the process working directory, matching
        how simulator config paths are currently interpreted by the runner.
        """
        network_file = _require_non_empty_string(
            _config_value(self.setup_db_config, "network_file"),
            "setup_db.network_file",
        )
        path = Path(network_file).expanduser()
        if path.is_absolute():
            return path
        return Path.cwd() / path

    def _load_network_edge_pairs(self, path: Path) -> Iterable[tuple[int, int]]:
        """
        Read one directed edge list from a supported network file.

        Supported formats are CSV, TSV, JSON, JSONL, and GML. Each edge must
        identify the source user as `user_id` and target friend as `friend_id`;
        CSV/TSV/JSON also accept common `source`/`target` aliases.
        """
        if not path.exists():
            raise FileNotFoundError(f"setup_db.network_file not found: {path}")
        suffix = path.suffix.lower()
        if suffix == ".csv":
            return self._load_delimited_network_edges(path, delimiter=",")
        if suffix == ".tsv":
            return self._load_delimited_network_edges(path, delimiter="\t")
        if suffix == ".json":
            return self._load_json_network_edges(path)
        if suffix == ".jsonl":
            return self._load_jsonl_network_edges(path)
        if suffix == ".gml":
            return self._load_gml_network_edges(path)
        raise ValueError(
            "Unsupported setup_db.network_file format. "
            "Supported extensions: .csv, .tsv, .json, .jsonl, .gml"
        )

    def _load_delimited_network_edges(
        self,
        path: Path,
        *,
        delimiter: str,
    ) -> Iterator[tuple[int, int]]:
        """
        Read CSV/TSV rows into `(user_id, friend_id)` pairs.

        Input files must have a header row. Preferred columns are `user_id` and
        `friend_id`; `source`/`target`, `src`/`dst`, and `from`/`to` are accepted
        as import aliases. Output pairs are parsed as integer simulator user IDs.
        """
        with path.open("r", encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file, delimiter=delimiter)
            if reader.fieldnames is None:
                return []
            field_names = {field.strip(): field for field in reader.fieldnames}
            source_field = self._edge_column(
                field_names,
                ("user_id", "source", "src", "from"),
                str(path),
            )
            target_field = self._edge_column(
                field_names,
                ("friend_id", "target", "dst", "to"),
                str(path),
            )
            for row_number, row in enumerate(reader, start=2):
                yield self._parse_edge_record(
                    {
                        "user_id": row[source_field],
                        "friend_id": row[target_field],
                    },
                    f"{path}:{row_number}",
                )

    def _load_json_network_edges(self, path: Path) -> list[tuple[int, int]]:
        """
        Read a JSON edge list into `(user_id, friend_id)` pairs.

        Input may be a list of edge objects or a mapping with an `edges` list.
        Each edge object should contain `user_id` and `friend_id`, with the same
        aliases accepted by CSV/TSV import.
        """
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        if isinstance(payload, dict):
            if "edges" not in payload:
                raise ValueError(f"{path} must contain an edges list")
            payload = payload["edges"]
        if not isinstance(payload, list):
            raise ValueError(f"{path} must contain a list of edges")
        return [
            self._parse_edge_record(record, f"{path}:edges[{index}]")
            for index, record in enumerate(payload)
        ]

    def _load_jsonl_network_edges(self, path: Path) -> Iterator[tuple[int, int]]:
        """
        Read JSON Lines edges into `(user_id, friend_id)` pairs.

        Each non-empty line must be one edge object or a two-item edge array.
        """
        with path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                yield self._parse_edge_record(
                    json.loads(stripped),
                    f"{path}:{line_number}",
                )

    def _load_gml_network_edges(self, path: Path) -> list[tuple[int, int]]:
        """
        Read GML edge blocks into `(user_id, friend_id)` pairs.

        The parser uses numeric `source` and `target` values from each
        `edge [...]` block directly as simulator user IDs. It does not remap GML
        labels or zero-based node IDs.
        """
        text = path.read_text(encoding="utf-8")
        edge_blocks = re.findall(r"\bedge\s*\[(.*?)\]", text, flags=re.DOTALL)
        edge_pairs = []
        for index, block in enumerate(edge_blocks):
            source_match = re.search(r"\bsource\s+(-?\d+)\b", block)
            target_match = re.search(r"\btarget\s+(-?\d+)\b", block)
            if source_match is None or target_match is None:
                raise ValueError(f"{path}:edge[{index}] must contain source and target")
            edge_pairs.append(
                (
                    self._parse_edge_user_id(
                        source_match.group(1),
                        f"{path}:edge[{index}].source",
                    ),
                    self._parse_edge_user_id(
                        target_match.group(1),
                        f"{path}:edge[{index}].target",
                    ),
                )
            )
        return edge_pairs

    @staticmethod
    def _edge_column(
        field_names: dict[str, str],
        aliases: tuple[str, ...],
        source_name: str,
    ) -> str:
        """Return the original column name for the first matching edge alias."""
        for alias in aliases:
            if alias in field_names:
                return field_names[alias]
        raise ValueError(
            f"{source_name} must include one of these columns: {', '.join(aliases)}"
        )

    def _parse_edge_record(self, record: Any, source_name: str) -> tuple[int, int]:
        """
        Convert one imported edge record into integer source and target IDs.

        Input may be an object with `user_id`/`friend_id` fields or aliases, or a
        two-item array. Output is `(user_id, friend_id)`.
        """
        if isinstance(record, dict):
            source_value = self._edge_value(
                record,
                ("user_id", "source", "src", "from"),
                source_name,
            )
            target_value = self._edge_value(
                record,
                ("friend_id", "target", "dst", "to"),
                source_name,
            )
            return (
                self._parse_edge_user_id(source_value, f"{source_name}.user_id"),
                self._parse_edge_user_id(target_value, f"{source_name}.friend_id"),
            )
        if isinstance(record, (list, tuple)) and len(record) == 2:
            return (
                self._parse_edge_user_id(record[0], f"{source_name}[0]"),
                self._parse_edge_user_id(record[1], f"{source_name}[1]"),
            )
        raise ValueError(
            f"{source_name} must be an edge object or two-item edge array"
        )

    @staticmethod
    def _edge_value(record: dict[str, Any], aliases: tuple[str, ...], source_name: str) -> Any:
        """Return one source/target value from an imported edge object."""
        for alias in aliases:
            if alias in record:
                return record[alias]
        raise ValueError(
            f"{source_name} must include one of these fields: {', '.join(aliases)}"
        )

    @staticmethod
    def _parse_edge_user_id(value: Any, field_name: str) -> int:
        """Parse an imported edge endpoint as an integer simulator user ID."""
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be an integer user ID")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("+"):
                stripped = stripped[1:]
            if stripped.isdigit() or (
                stripped.startswith("-") and stripped[1:].isdigit()
            ):
                return int(stripped)
        raise ValueError(f"{field_name} must be an integer user ID")

    @staticmethod
    def _validated_imported_edges(
        *,
        edge_pairs: list[tuple[int, int]],
        users: list[UserRecord],
        source_name: str,
    ) -> list[FollowerNetworkRecord]:
        """
        Validate imported edge pairs and return follower-network rows.

        Input pairs are interpreted as directed `user_id -> friend_id` links.
        Output preserves file order while rejecting references outside the
        initialized user set, self-links, and duplicate directed links.
        """
        valid_user_ids = {user.user_id for user in users}
        edges: list[FollowerNetworkRecord] = []
        edge_keys: set[tuple[int, int]] = set()
        for index, (user_id, friend_id) in enumerate(edge_pairs, start=1):
            if user_id not in valid_user_ids:
                raise ValueError(
                    f"{source_name} edge {index} references unknown user_id {user_id}"
                )
            if friend_id not in valid_user_ids:
                raise ValueError(
                    f"{source_name} edge {index} references unknown friend_id {friend_id}"
                )
            if user_id == friend_id:
                raise ValueError(f"{source_name} edge {index} is a self-link")
            edge_key = (user_id, friend_id)
            if edge_key in edge_keys:
                raise ValueError(
                    f"{source_name} edge {index} duplicates user_id={user_id}, "
                    f"friend_id={friend_id}"
                )
            edge_keys.add(edge_key)
            edges.append(FollowerNetworkRecord(user_id=user_id, friend_id=friend_id))
        return edges

    @staticmethod
    def _users_by_type(users: list[UserRecord]) -> dict[str, list[int]]:
        """Group initialized user IDs by their fixed user type."""
        users_by_type: dict[str, list[int]] = {}
        for user in users:
            users_by_type.setdefault(user.user_type, []).append(user.user_id)
        return users_by_type

    @classmethod
    def network_source_names(cls) -> tuple[str, ...]:
        """Return config-visible initial-network source names."""
        return tuple(cls._network_source_registry())

    @classmethod
    def _network_source_registry(cls) -> dict[str, NetworkSourceSpec]:
        """Build network-source registry from labeled class methods."""
        registry: dict[str, NetworkSourceSpec] = {}
        for owner in reversed(cls.__mro__):
            for value in vars(owner).values():
                function = _unwrap_descriptor(value)
                spec = getattr(function, "_simsom_network_source_spec", None)
                if spec is not None:
                    registry[spec.name] = spec
        return registry

    @classmethod
    def _network_source_spec(cls, source_name: str) -> NetworkSourceSpec:
        """Return a labeled network-source spec or raise a clear config error."""
        registry = cls._network_source_registry()
        if source_name not in registry:
            allowed = ", ".join(sorted(registry))
            raise ValueError(
                f"setup_db.network_source must be one of: {allowed}"
            )
        return registry[source_name]

    @staticmethod
    def _edge_generation_function_name(type_config: Any, phase: str) -> str:
        """Read the phase choice; see docs/algorithms/configuration.md#setup-functions."""
        if phase not in (WITHIN_TYPE_NETWORKING_PHASE, CROSS_TYPE_NETWORKING_PHASE):
            raise ValueError(f"Unsupported edge generation phase: {phase}")
        config = _config_mapping(_config_value(type_config, "edge_generation"))
        entries = function_entries(_config_value(config, phase + "_function"), phase + "_function")
        if len(entries) != 1:
            raise ValueError(f"{phase}_function must configure exactly one function")
        return next(iter(entries))

    @classmethod
    def edge_generation_function_names(
        cls,
        phase: str | None = None,
    ) -> tuple[str, ...]:
        """Return config-visible edge-generation function names."""
        return tuple(
            name
            for name, spec in cls._edge_generation_registry().items()
            if phase is None or phase in spec.phases
        )

    @classmethod
    def activity_distribution_function_names(cls) -> tuple[str, ...]:
        """Return config-visible activity distribution function names."""
        return tuple(cls._activity_distribution_registry())

    @classmethod
    def vector_profile_generation_function_names(cls) -> tuple[str, ...]:
        """Return config-visible vector-profile generator names."""
        return tuple(cls._vector_profile_generation_registry())

    @classmethod
    def _edge_generation_registry(
        cls,
    ) -> dict[str, EdgeGenerationFunctionSpec]:
        """Build edge-generation registry from labeled class methods."""
        registry: dict[str, EdgeGenerationFunctionSpec] = {}
        for owner in reversed(cls.__mro__):
            for value in vars(owner).values():
                function = _unwrap_descriptor(value)
                spec = getattr(
                    function,
                    "_simsom_edge_generation_function_spec",
                    None,
                )
                if spec is not None:
                    registry[spec.name] = spec
        return registry

    @classmethod
    def _activity_distribution_registry(
        cls,
    ) -> dict[str, ActivityDistributionFunctionSpec]:
        """Build activity distribution registry from labeled class methods."""
        registry: dict[str, ActivityDistributionFunctionSpec] = {}
        for owner in reversed(cls.__mro__):
            for value in vars(owner).values():
                function = _unwrap_descriptor(value)
                spec = getattr(
                    function,
                    "_simsom_activity_distribution_function_spec",
                    None,
                )
                if spec is not None:
                    registry[spec.name] = spec
        return registry

    @classmethod
    def _vector_profile_generation_registry(
        cls,
    ) -> dict[str, VectorProfileGenerationFunctionSpec]:
        """Build the vector-profile generator registry from labeled methods."""
        registry: dict[str, VectorProfileGenerationFunctionSpec] = {}
        for owner in reversed(cls.__mro__):
            for value in vars(owner).values():
                function = _unwrap_descriptor(value)
                spec = getattr(
                    function,
                    "_simsom_vector_profile_generation_function_spec",
                    None,
                )
                if spec is not None:
                    registry[spec.name] = spec
        return registry

    @classmethod
    def _edge_generation_spec(
        cls,
        function_name: str,
        phase: str | None = None,
    ) -> EdgeGenerationFunctionSpec:
        """Return a labeled edge-generation spec or raise a clear config error."""
        registry = cls._edge_generation_registry()
        if function_name not in registry:
            if phase is not None:
                raise ValueError(
                    "Unsupported "
                    f"{_edge_generation_phase_label(phase)} "
                    f"edge generation function: {function_name}"
                )
            raise ValueError(f"Unsupported edge generation function: {function_name}")
        spec = registry[function_name]
        if phase is not None and phase not in spec.phases:
            raise ValueError(
                "Unsupported "
                f"{_edge_generation_phase_label(phase)} "
                f"edge generation function: {function_name}"
            )
        return spec

    def _user_types(self) -> dict[str, Any]:
        user_types = _config_mapping(_config_value(self.setup_db_config, "user_types"))
        if not user_types:
            raise ValueError("setup_db.user_types must be non-empty")
        return user_types

    @staticmethod
    def _generate_user_event_dist(global_event_dist: Any) -> dict[str, float]:
        """Sample one preference vector; see docs/algorithms/domain_activity.md#action-settings-meaning."""
        return {
            action_type: float(values[0])
            for action_type, values in
            DefaultSetupDbPlanner._sample_user_action_probabilities(global_event_dist, 1).items()
        }

    @staticmethod
    def _sample_user_action_probabilities(
        global_event_dist: Any, count: int,
    ) -> dict[str, np.ndarray]:
        """Initialize fixed preferences for a chunk of users.

        See docs/algorithms/domain_activity.md#action-settings-meaning and
        docs/algorithms/domain_activity.md#action-probability-calibration.
        Disabled [0,0] types receive no draw or floor. Enabled weights use
        clip enabled draws at zero before normalization. If every enabled draw
        clips to zero, replace the vector with the configured means first.
        """
        samples = {}
        enabled = []
        for action_type, (mean, std) in _config_items(global_event_dist):
            mean = _require_non_negative_number(mean, f"p_actions.{action_type}.mean")
            std = _require_non_negative_number(std, f"p_actions.{action_type}.std")
            if mean == 0 and std == 0:
                samples[action_type] = np.zeros(count, dtype=np.float64)
                continue
            values = np.random.normal(mean, std, size=count)
            if not np.all(np.isfinite(values)):
                raise ValueError(f"p_actions.{action_type} produced nonfinite weights")
            samples[action_type] = np.maximum(values, 0.0)
            enabled.append(action_type)
        if not enabled:
            raise ValueError("p_actions must enable at least one action type")
        enabled_weights = np.stack([samples[key] for key in enabled])
        all_zero_users = np.all(enabled_weights == 0, axis=0)
        if np.any(all_zero_users):
            means = np.array(
                [global_event_dist[key][0] for key in enabled],
                dtype=np.float64,
            )
            if not np.any(means > 0):
                raise ValueError(
                    "p_actions all enabled means are zero after an all-zero draw"
                )
            for key, mean in zip(enabled, means):
                samples[key][all_zero_users] = mean
        # Scale first so finite but very large weights cannot overflow their sum.
        scale = np.maximum.reduce([samples[key] for key in enabled])
        for key in enabled:
            samples[key] /= scale
        total = sum(samples[key] for key in enabled)
        for key in enabled:
            samples[key] /= total
        return samples

    @staticmethod
    def _draw_normalized_two_cluster_inverse(distribution: Any) -> float:
        """Draw one two-cluster inverse sample and normalize from [0, 100] to [0, 1].

        See docs/algorithms/controller.md#user-generation-math.
        """
        parameters = _config_mapping(distribution)
        w = _require_probability(_config_value(parameters, "w"), "distribution.w")
        lambda_slope = _require_positive_number(
            _config_value(parameters, "lambda_slope"),
            "distribution.lambda_slope",
        )
        if np.random.random() < w:
            u = np.random.random()
            value = 100 + (1 / lambda_slope) * math.log(
                1 - u * (1 - math.exp(-100 * lambda_slope))
            )
        else:
            u = np.random.random()
            value = -(1 / lambda_slope) * math.log(
                1 - u * (1 - math.exp(-100 * lambda_slope))
            )
        return min(max(value / 100.0, 0.0), 1.0)

    @staticmethod
    def _quality_distribution_parameters(type_config: Any) -> tuple[float, float]:
        """Validate and return one user type's quality-distribution parameters."""
        parameters = _config_mapping(
            _config_value(type_config, USER_QUALITY_DISTRIBUTION_KEY)
        )
        return (
            _require_probability(_config_value(parameters, "w"), "distribution.w"),
            _require_positive_number(
                _config_value(parameters, "lambda_slope"),
                "distribution.lambda_slope",
            ),
        )

    @classmethod
    def _draw_normalized_two_cluster_inverse_many(
        cls,
        type_config: Any,
        count: int,
    ) -> np.ndarray:
        """Draw one bounded NumPy vector from the configured quality mixture.

        See docs/algorithms/controller.md#user-generation-math.
        """
        w, lambda_slope = cls._quality_distribution_parameters(type_config)
        high_cluster = np.random.random(size=count) < w
        uniform = np.random.random(size=count)
        log_term = np.log(
            1.0 - uniform * (1.0 - math.exp(-100.0 * lambda_slope))
        ) / lambda_slope
        values = np.where(high_cluster, 100.0 + log_term, -log_term)
        return np.clip(values / 100.0, 0.0, 1.0)


def _config_items(value: Any):
    if isinstance(value, dict):
        return value.items()
    return vars(value).items()


def _smallest_unsigned_dtype(maximum_code: int) -> Any:
    """Return the narrowest NumPy unsigned type that can hold a value."""
    if maximum_code <= np.iinfo(np.uint8).max:
        return np.uint8
    if maximum_code <= np.iinfo(np.uint16).max:
        return np.uint16
    if maximum_code <= np.iinfo(np.uint32).max:
        return np.uint32
    if maximum_code <= np.iinfo(np.uint64).max:
        return np.uint64
    raise ValueError("value exceeds the supported unsigned 64-bit range")


def _sample_multivariate_hypergeometric(
    remaining_counts: Sequence[int],
    sample_size: int,
) -> list[int]:
    """Draw exact category counts for one chunk without global permutation state."""
    remaining_total = sum(remaining_counts)
    sample_remaining = sample_size
    selected_counts = []
    for category_count in remaining_counts[:-1]:
        if sample_remaining == 0 or category_count == 0:
            selected = 0
        elif sample_remaining == remaining_total:
            selected = category_count
        else:
            selected = int(
                np.random.hypergeometric(
                    category_count,
                    remaining_total - category_count,
                    sample_remaining,
                )
            )
        selected_counts.append(selected)
        sample_remaining -= selected
        remaining_total -= category_count
    selected_counts.append(sample_remaining)
    return selected_counts


def _user_id_sequence(users: Sequence[Any]) -> Sequence[int]:
    """Normalize legacy UserRecord lists while leaving compact ID views intact."""
    if not len(users) or not isinstance(users[0], UserRecord):
        return users
    return [user.user_id for user in users]


def _user_id_array_slice(
    user_ids: Sequence[int],
    start: int,
    stop: int,
) -> np.ndarray:
    """Return one bounded ID slice without widening compact source arrays."""
    if isinstance(user_ids, range):
        first = user_ids.start + start * user_ids.step
        last = user_ids.start + stop * user_ids.step
        maximum_id = max(first, last - user_ids.step)
        return np.arange(
            first,
            last,
            user_ids.step,
            dtype=_smallest_unsigned_dtype(maximum_id),
        )
    return np.asarray(user_ids[start:stop])


def _concatenate_user_id_sequences(
    sequences: Sequence[Sequence[int]],
) -> np.ndarray:
    """Concatenate target IDs into one fixed-width array in configured order."""
    arrays = []
    for user_ids in sequences:
        if isinstance(user_ids, range):
            arrays.append(
                np.arange(
                    user_ids.start,
                    user_ids.stop,
                    user_ids.step,
                    dtype=_smallest_unsigned_dtype(user_ids.stop - 1),
                )
            )
        else:
            arrays.append(np.asarray(user_ids))
    return arrays[0] if len(arrays) == 1 else np.concatenate(arrays)


def _unwrap_descriptor(value: Any) -> Any:
    if isinstance(value, (staticmethod, classmethod)):
        return value.__func__
    return value


def _edge_generation_phase_label(phase: str) -> str:
    if phase == WITHIN_TYPE_NETWORKING_PHASE:
        return "within-type networking"
    if phase == CROSS_TYPE_NETWORKING_PHASE:
        return "cross-type networking"
    return phase


def _config_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    raise TypeError(f"Expected config object or dict, got {type(value).__name__}")


def _config_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"Missing required config field: {key}")
        return value[key]
    if not hasattr(value, key):
        raise ValueError(f"Missing required config field: {key}")
    return getattr(value, key)


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{field_name} must be greater than or equal to 0")
    return value


def _require_positive_int(value: Any, field_name: str) -> int:
    """Require a whole-number setting greater than zero."""
    number = _require_non_negative_int(value, field_name)
    if number == 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return number


def _require_number(value: Any, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be int or float, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{field_name} must be finite") from None
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _require_non_negative_number(value: Any, field_name: str) -> float:
    number = _require_number(value, field_name)
    if number < 0.0:
        raise ValueError(f"{field_name} must be greater than or equal to 0")
    return number


def _require_positive_number(value: Any, field_name: str) -> float:
    number = _require_number(value, field_name)
    if number <= 0.0:
        raise ValueError(f"{field_name} must be greater than 0")
    return number


def _require_probability(value: Any, field_name: str) -> float:
    number = _require_number(value, field_name)
    if number < 0.0 or number > 1.0:
        raise ValueError(f"{field_name} must satisfy 0 <= value <= 1")
    return number


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{field_name} must be a non-empty string")
    return value


def _require_lurker_ratio(value: Any, field_name: str) -> float:
    """Allow all members of one type to be lurkers.

    See docs/algorithms/controller.md#user-generation-math.
    """
    return _require_probability(value, field_name)


def _plain_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain_config(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain_config(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: _plain_config(item) for key, item in vars(value).items()}
    return value


def _require_list(name: str, value: Any, item_type: type) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a list, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, item_type):
            raise TypeError(
                f"{name}[{index}] must be {item_type.__name__}, "
                f"got {type(item).__name__}"
            )
