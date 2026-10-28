# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Unit tests for planning the controller's initial database dataset."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from db.core import FollowerNetworkRecord, UserRecord  # noqa: E402
from db.worker import allowed_post_distribution_function_names  # noqa: E402
import subcomponents.setup_db as setup_db_module  # noqa: E402
from subcomponents.setup_db import (  # noqa: E402
    DefaultSetupDbPlanner,
    WITHIN_TYPE_NETWORKING_PHASE,
    CROSS_TYPE_NETWORKING_PHASE,
)


def activity_distribution_config():
    """Supply every input of the uncalibrated activity distribution."""
    return {"power_law_activity": {
        "power_law_exponent": 2.0, "minimum_activity_level": 0.1, "maximum_activity_level": 10.0,
    }}


def post_distribution_selector():
    """Use an explicit post-property standard deviation."""
    return {"gaussian_around_user_quality": {"stddev": 0.12}}


def edge_function_config(name):
    inputs = {"density_uniform_sparse": {"density": 0.0003},
              "power_law_by_followers_sparse": {"density": 0.0003, "power_law_exponent": 1.0},
              "random_walk": {"seed_size": 3, "avg_n_friend": 3, "probability_follow": 0.5}}
    return {name: inputs.get(name, {})}


def user_types_config(
    *,
    normal_num_nodes=2,
    bad_num_nodes=2,
    normal_within_type_networking_function="density_uniform_sparse",
    normal_cross_type_networking_function="no_edges",
    bad_within_type_networking_function="density_uniform_sparse",
    bad_cross_type_networking_function="no_edges",
    normal_lurker_ratio=0.0,
    bad_lurker_ratio=0.0,
):
    """Return a minimal two-type setup config for planner tests."""
    return {
        "normal": {
            "num_nodes": normal_num_nodes,
            "lurker_ratio": normal_lurker_ratio,
            "edge_generation": {
                "within_type_networking_function": edge_function_config(normal_within_type_networking_function),
                "cross_type_networking_function": edge_function_config(normal_cross_type_networking_function),
            },
            "activity_level_distribution": activity_distribution_config(),
            "user_quality_distribution": {"w": 0.85, "lambda_slope": 0.1190},
            "post_quality_distribution_function": post_distribution_selector(),
            "post_appeal_distribution_function": post_distribution_selector(),
        },
        "bad": {
            "num_nodes": bad_num_nodes,
            "lurker_ratio": bad_lurker_ratio,
            "edge_generation": {
                "within_type_networking_function": edge_function_config(bad_within_type_networking_function),
                "cross_type_networking_function": edge_function_config(bad_cross_type_networking_function),
            },
            "activity_level_distribution": activity_distribution_config(),
            "user_quality_distribution": {"w": 0.15, "lambda_slope": 0.1190},
            "post_quality_distribution_function": post_distribution_selector(),
            "post_appeal_distribution_function": post_distribution_selector(),
        },
    }


def user_behavior_config(p_actions=None):
    """Return a minimal user behavior config for setup planner tests."""
    return SimpleNamespace(
        p_actions={"like": [1.0, 0.0]} if p_actions is None else p_actions,
        vector_profile_generation_function={
            "constant_vector_profile": {"dimension": 1, "value": 1.0}
        },
    )


class SetupDbPlannerTests(unittest.TestCase):
    """Validate setup_db dataset planning and input validation."""

    def test_vector_profile_generators_produce_one_vector_per_user(self):
        constant = DefaultSetupDbPlanner._vector_profile_generation_spec(
            {"constant_vector_profile": {"dimension": 2, "value": 0.5}}
        ).function(3)
        self.assertEqual(constant.tolist(), [[0.5, 0.5]] * 3)
        with patch("subcomponents.setup_db.np.random.uniform", return_value=setup_db_module.np.ones((2, 3))):
            random_vectors = DefaultSetupDbPlanner._vector_profile_generation_spec(
                {"uniform_random_vector_profile": {
                    "dimension": 3, "minimum": 0.0, "maximum": 1.0,
                }}
            ).function(2)
        self.assertEqual(random_vectors.shape, (2, 3))

    def test_build_dataset_creates_users_from_num_nodes_and_synthetic_network(self):
        """Synthetic setup should derive users from configured type node counts."""
        config = SimpleNamespace(
            network_source="synthetic",
            user_types=user_types_config(
                normal_num_nodes=3,
                bad_num_nodes=1,
                normal_lurker_ratio=1 / 3,
                bad_lurker_ratio=0.0,
            ),
        )
        behavior_config = user_behavior_config(
            {
                "like": [1.0, 0.0],
                "post": [1.0, 0.0],
            },
        )
        planner = DefaultSetupDbPlanner(config, behavior_config)

        dataset = planner.build_dataset()

        self.assertEqual(len(dataset.users), 4)
        self.assertEqual(
            {
                user_type: sum(user.user_type == user_type for user in dataset.users)
                for user_type in ("normal", "bad")
            },
            {"normal": 3, "bad": 1},
        )
        self.assertEqual([user.user_id for user in dataset.users], [1, 2, 3, 4])
        self.assertEqual(set(dataset.users[0].user_behavior["p_actions"]), {"like", "post"})
        self.assertEqual(
            dataset.users[0].user_behavior["p_actions"],
            {"like": 0.5, "post": 0.5},
        )
        zero_activity_users = [
            user for user in dataset.users if user.activity_level == 0.0
        ]
        active_users = [
            user for user in dataset.users if user.activity_level > 0.0
        ]
        self.assertEqual(len(zero_activity_users), 1)
        self.assertEqual(len(active_users), 3)
        self.assertTrue(all(user.activity_level > 0.0 for user in active_users))
        self.assertGreaterEqual(dataset.users[0].user_behavior["quality"], 0.0)
        self.assertLessEqual(dataset.users[0].user_behavior["quality"], 1.0)
        self.assertTrue(all(user.vector_profile == [1.0] for user in dataset.users))
        self.assertFalse(
            {
                "user_quality_distribution",
                "appeal",
                "user_appeal_distribution",
                "post_quality_distribution_function",
                "post_appeal_distribution_function",
            }.intersection(dataset.users[0].user_behavior)
        )
        self.assertTrue(
            all(edge.user_id != edge.friend_id for edge in dataset.follower_network)
        )
        self.assertEqual(
            len({(edge.user_id, edge.friend_id) for edge in dataset.follower_network}),
            len(dataset.follower_network),
        )

    def test_stream_keeps_user_rows_lazy_and_lurker_totals_exact(self):
        """Streaming setup should preserve exact counts without retaining user rows."""
        config = SimpleNamespace(
            network_source="synthetic",
            user_types=user_types_config(
                normal_num_nodes=9,
                bad_num_nodes=3,
                normal_lurker_ratio=1 / 3,
                bad_lurker_ratio=0.0,
            ),
        )
        planner = DefaultSetupDbPlanner(config, user_behavior_config())

        with patch.object(setup_db_module, "USER_PROFILE_CHUNK_SIZE", 5):
            stream = planner.build_stream()
            users = list(stream.users)

        self.assertNotIsInstance(stream.users, list)
        self.assertEqual(len(users), stream.user_count)
        self.assertEqual(sum(user.activity_level == 0.0 for user in users), 3)
        self.assertEqual(
            sum(user.user_type == "normal" for user in users),
            9,
        )

    def test_single_populated_type_needs_no_assignment_array(self):
        """One populated type should use an implicit contiguous ID range."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(
                network_source="synthetic",
                user_types=user_types_config(normal_num_nodes=4, bad_num_nodes=0),
            ),
            user_behavior_config(),
        )

        population = planner._build_compact_user_population()

        self.assertIsNone(population.assignment_codes)
        self.assertIsInstance(population.user_ids_by_type()["normal"], range)

    def test_multiple_types_use_one_byte_codes_when_possible(self):
        """Small type registries should avoid Python labels for every user."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(
                network_source="synthetic",
                user_types=user_types_config(normal_num_nodes=4, bad_num_nodes=4),
            ),
            user_behavior_config(),
        )

        population = planner._build_compact_user_population()

        self.assertEqual(population.assignment_codes.dtype.itemsize, 1)
        self.assertEqual(population.assignment_codes.nbytes, population.user_count)
        self.assertTrue(
            all(
                storage is None or storage.array.dtype == population.user_id_dtype
                for storage in population.type_id_storage
            )
        )
        self.assertEqual(population.user_id_dtype, setup_db_module.np.uint8)
        population.close()

    def test_single_type_metadata_supports_integer_maximum_ids(self):
        """The planner should represent the INTEGER ID boundary without allocation."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(
                network_source="synthetic",
                user_types=user_types_config(
                    normal_num_nodes=2_147_483_647,
                    bad_num_nodes=0,
                ),
            ),
            user_behavior_config(),
        )

        population = planner._build_compact_user_population()

        user_ids = population.user_ids_by_type()["normal"]

        self.assertEqual(user_ids[-1], 2_147_483_647)
        self.assertEqual(population.user_id_dtype, setup_db_module.np.uint32)

    def test_population_above_integer_maximum_is_rejected(self):
        """The planner should reject IDs that cannot fit PostgreSQL INTEGER."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(
                network_source="synthetic",
                user_types=user_types_config(
                    normal_num_nodes=2_147_483_648,
                    bad_num_nodes=0,
                ),
            ),
            user_behavior_config(),
        )

        with self.assertRaisesRegex(ValueError, "supported limit"):
            planner._build_compact_user_population()

    def test_one_billion_ids_use_compact_32_bit_local_storage(self):
        """Local edge state should not pay for 64 bits until IDs require it."""
        self.assertEqual(
            setup_db_module._smallest_unsigned_dtype(1_000_000_000),
            setup_db_module.np.uint32,
        )

    def test_one_billion_single_type_population_uses_lazy_id_range(self):
        """The planner can represent one billion users without a full ID array."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(
                network_source="synthetic",
                user_types=user_types_config(
                    normal_num_nodes=1_000_000_000,
                    bad_num_nodes=0,
                ),
            ),
            user_behavior_config(),
        )

        population = planner._build_compact_user_population()
        try:
            user_ids = population.user_ids_by_type()["normal"]
            self.assertIsInstance(user_ids, range)
            self.assertEqual(user_ids.start, 1)
            self.assertEqual(user_ids.stop, 1_000_000_001)
        finally:
            population.close()

    def test_compact_array_uses_temporary_storage_above_memory_limit(self):
        """Oversized setup state should be memory-mapped instead of resident."""
        with patch.object(setup_db_module, "COMPACT_ARRAY_MEMORY_LIMIT_BYTES", 0):
            storage = setup_db_module._CompactArray(
                4,
                setup_db_module.np.uint64,
                zero=True,
            )
        try:
            self.assertEqual(storage.storage_kind, "temporary_file")
            self.assertTrue(setup_db_module.np.all(storage.array == 0))
        finally:
            storage.close()

    def test_power_law_activity_wrapper_outputs_positive_values(self):
        """Active activity sampling should never collide with the zero sentinel."""
        config = {"activity_level_distribution": activity_distribution_config()}

        samples = [
            DefaultSetupDbPlanner._draw_activity_level(config)
            for _ in range(50)
        ]

        self.assertTrue(all(sample > 0.0 for sample in samples))

    def test_build_dataset_imports_csv_network_file(self):
        """File setup should import directed links from setup_db.network_file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            network_file = Path(temp_dir) / "network.csv"
            network_file.write_text(
                "user_id,friend_id\n1,2\n3,1\n",
                encoding="utf-8",
            )
            config = SimpleNamespace(
                network_source="file",
                network_file=str(network_file),
                user_types=user_types_config(normal_num_nodes=2, bad_num_nodes=1),
            )
            planner = DefaultSetupDbPlanner(config, user_behavior_config())

            dataset = planner.build_dataset()

        self.assertEqual(len(dataset.users), 3)
        self.assertEqual(
            [(edge.user_id, edge.friend_id) for edge in dataset.follower_network],
            [(1, 2), (3, 1)],
        )

    def test_build_dataset_imports_gml_network_file(self):
        """File setup should support GML edge blocks using simulator user IDs."""
        with tempfile.TemporaryDirectory() as temp_dir:
            network_file = Path(temp_dir) / "network.gml"
            network_file.write_text(
                """
graph [
  directed 1
  node [ id 1 ]
  node [ id 2 ]
  edge [ source 1 target 2 ]
]
""",
                encoding="utf-8",
            )
            config = SimpleNamespace(
                network_source="file",
                network_file=str(network_file),
                user_types=user_types_config(normal_num_nodes=2, bad_num_nodes=0),
            )
            planner = DefaultSetupDbPlanner(config, user_behavior_config())

            dataset = planner.build_dataset()

        self.assertEqual(
            [(edge.user_id, edge.friend_id) for edge in dataset.follower_network],
            [(1, 2)],
        )

    def test_build_dataset_rejects_invalid_network_file_edges(self):
        """Imported files should fail on invalid graph links."""
        with tempfile.TemporaryDirectory() as temp_dir:
            network_file = Path(temp_dir) / "network.csv"
            network_file.write_text(
                "user_id,friend_id\n1,1\n",
                encoding="utf-8",
            )
            config = SimpleNamespace(
                network_source="file",
                network_file=str(network_file),
                user_types=user_types_config(normal_num_nodes=2, bad_num_nodes=0),
            )
            planner = DefaultSetupDbPlanner(config, user_behavior_config())

            with self.assertRaisesRegex(ValueError, "self-link"):
                planner.build_dataset()

    def test_build_dataset_rejects_unsupported_network_source(self):
        """Planner should reject unknown network sources."""
        config = SimpleNamespace(
            network_source="archive",
            user_types=user_types_config(normal_num_nodes=1, bad_num_nodes=0),
        )
        planner = DefaultSetupDbPlanner(config, user_behavior_config())

        with self.assertRaises(ValueError):
            planner.build_dataset()

    def test_build_dataset_requires_user_types(self):
        """Planner should require setup_db.user_types."""
        config = SimpleNamespace(
            network_source="synthetic",
        )
        planner = DefaultSetupDbPlanner(config, user_behavior_config())

        with self.assertRaisesRegex(ValueError, "user_types"):
            planner.build_dataset()

    def test_build_dataset_requires_user_p_actions(self):
        """Planner should require user_behavior.p_actions."""
        config = SimpleNamespace(
            network_source="synthetic",
            user_types=user_types_config(),
        )
        planner = DefaultSetupDbPlanner(config, SimpleNamespace())

        with self.assertRaisesRegex(ValueError, "p_actions"):
            planner.build_dataset()

    def test_build_dataset_rejects_invalid_lurker_ratio(self):
        """Planner should not silently coerce an invalid per-type lurker_ratio."""
        config = SimpleNamespace(
            network_source="synthetic",
            user_types=user_types_config(
                normal_num_nodes=1,
                bad_num_nodes=0,
                normal_lurker_ratio=1.01,
            ),
        )
        planner = DefaultSetupDbPlanner(config, user_behavior_config())

        with self.assertRaisesRegex(ValueError, "lurker_ratio"):
            planner.build_dataset()

    def test_an_entire_user_type_can_be_lurkers_across_profile_chunks(self):
        """An all-lurker type retains its exact count alongside active types."""
        config = SimpleNamespace(
            network_source="synthetic",
            user_types=user_types_config(
                normal_num_nodes=3, bad_num_nodes=7,
                normal_lurker_ratio=0, bad_lurker_ratio=1,
            ),
        )
        with patch("subcomponents.setup_db.USER_PROFILE_CHUNK_SIZE", 2):
            dataset = DefaultSetupDbPlanner(config, user_behavior_config()).build_dataset()
        bad = [u for u in dataset.users if u.user_type == "bad"]
        normal = [u for u in dataset.users if u.user_type == "normal"]
        self.assertEqual(len(bad), 7)
        self.assertTrue(all(u.activity_level == 0 for u in bad))
        self.assertEqual(len(normal), 3)
        self.assertTrue(all(u.activity_level > 0 for u in normal))

    def test_planner_rejects_only_population_wide_inactivity(self):
        for ratio in (0.9, 1.0):
            with self.subTest(ratio=ratio):
                config = SimpleNamespace(
                    network_source="synthetic",
                    user_types=user_types_config(
                        normal_num_nodes=3, bad_num_nodes=3,
                        normal_lurker_ratio=ratio, bad_lurker_ratio=ratio,
                    ),
                )
                with self.assertRaisesRegex(ValueError, "no active users after lurker rounding"):
                    DefaultSetupDbPlanner(config, user_behavior_config()).build_dataset()

    def test_missing_cross_type_networking_function_is_rejected(self):
        """Each synthetic user type should configure one cross-type networking function."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(
                network_source="synthetic",
                user_types=user_types_config(),
            ),
            user_behavior_config(),
        )
        normal_config = planner._user_types()["normal"]
        del normal_config["edge_generation"]["cross_type_networking_function"]

        with self.assertRaisesRegex(ValueError, "cross_type_networking_function"):
            planner._edge_generation_function_name(
                normal_config,
                CROSS_TYPE_NETWORKING_PHASE,
            )

    def test_synthetic_network_runs_within_type_before_cross_type(self):
        """The planner should execute all within-type functions first."""

        class RecordingPlanner(DefaultSetupDbPlanner):
            def __init__(self, setup_db_config, user_behavior_config):
                super().__init__(setup_db_config, user_behavior_config)
                self.calls = []

            def _run_edge_generation_function(self, **kwargs):
                self.calls.append((kwargs["phase"], kwargs["source_user_type"]))
                return iter(())

        config = SimpleNamespace(
            network_source="synthetic",
            user_types=user_types_config(),
        )
        planner = RecordingPlanner(config, user_behavior_config({}))
        users = [
            UserRecord(user_id=1, user_type="normal"),
            UserRecord(user_id=2, user_type="normal"),
            UserRecord(user_id=3, user_type="bad"),
            UserRecord(user_id=4, user_type="bad"),
        ]

        planner._build_synthetic_follower_network(users)

        self.assertEqual(
            planner.calls,
            [
                (WITHIN_TYPE_NETWORKING_PHASE, "normal"),
                (WITHIN_TYPE_NETWORKING_PHASE, "bad"),
                (CROSS_TYPE_NETWORKING_PHASE, "normal"),
                (CROSS_TYPE_NETWORKING_PHASE, "bad"),
            ],
        )

    def test_synthetic_network_reports_each_phase_with_current_edge_counts(self):
        """Every synthetic phase should report source users and edge counts."""

        class RecordingProgress:
            def __init__(self):
                self.current = None
                self.records = []

            def begin(self, stage, **fields):
                if "progress_total" in fields:
                    fields["progress_current"] = 0
                self.current = {"stage": stage, "status": "running", **fields}
                self.records.append(dict(self.current))

            def update(self, **fields):
                self.current.update(fields)
                self.records.append(dict(self.current))

            def complete(self, **fields):
                self.current.update(fields, status="complete")
                self.records.append(dict(self.current))

        progress = RecordingProgress()
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(
                network_source="synthetic",
                user_types=user_types_config(),
            ),
            user_behavior_config(),
            setup_progress=progress,
        )

        planner.build_dataset()

        phase_starts = [
            record
            for record in progress.records
            if record["stage"] == "network_generation"
            and record["status"] == "running"
            and record["progress_current"] == 0
        ]
        self.assertEqual(len(phase_starts), 4)
        self.assertTrue(
            all(
                {
                    "phase",
                    "user_type",
                    "function",
                    "progress_total",
                    "phase_edges",
                    "total_edges",
                }.issubset(record)
                for record in phase_starts
            )
        )
        self.assertTrue(
            all(
                record["total_edges"] >= record["phase_edges"] >= 0
                for record in progress.records
                if record["stage"] == "network_generation"
            )
        )

    def test_edge_dispatch_uses_phase_specific_function_selectors(self):
        """Each phase should choose functions through its own selector."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(user_types={}),
            SimpleNamespace(),
        )

        with self.assertRaisesRegex(ValueError, "Unsupported within-type networking"):
            list(
                planner._run_within_type_edge_generation_function(
                    function_name="unknown",
                    source_user_type="normal",
                    users_by_type={},
                    state=setup_db_module._NetworkGenerationState(
                        0,
                        track_follower_counts=False,
                    ),
                )
            )

        with self.assertRaisesRegex(ValueError, "Unsupported cross-type networking"):
            list(
                planner._run_cross_type_edge_generation_function(
                    function_name="unknown",
                    source_user_type="normal",
                    users_by_type={},
                    state=setup_db_module._NetworkGenerationState(
                        0,
                        track_follower_counts=False,
                    ),
                )
            )

    def test_power_law_cross_type_uses_current_follower_counts(self):
        """Power-law generation should weight candidates by existing followers."""

        class CapturingSampler:
            weights = None

            @classmethod
            def from_follower_counts(
                cls,
                candidate_ids,
                follower_counts,
                power_law_exponent,
            ):
                cls.weights = [
                    (float(follower_counts[int(user_id) - 1]) + 1.0)
                    ** power_law_exponent
                    for user_id in candidate_ids
                ]
                return cls()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def sample_without_replacement(self, _count, _blocked_indexes=None):
                return []

        planner = DefaultSetupDbPlanner(
            SimpleNamespace(user_types={}),
            SimpleNamespace(),
        )
        state = setup_db_module._NetworkGenerationState(
            3,
            track_follower_counts=True,
        )
        state.record_edge(FollowerNetworkRecord(user_id=1, friend_id=2))
        state.record_edge(FollowerNetworkRecord(user_id=3, friend_id=2))
        state.record_edge(FollowerNetworkRecord(user_id=1, friend_id=3))
        users_by_type = {
            "normal": [UserRecord(user_id=1, user_type="normal")],
            "bad": [
                UserRecord(user_id=2, user_type="bad"),
                UserRecord(user_id=3, user_type="bad"),
            ],
        }

        with patch.object(setup_db_module, "_WeightedIndexSampler", CapturingSampler):
            list(
                planner._generate_power_law_by_followers_edges(
                    density=0.0003, power_law_exponent=1.0,
                    source_user_type="normal",
                    users_by_type=users_by_type,
                    state=state,
                )
            )
        state.close()

        self.assertEqual(CapturingSampler.weights, [3.0, 2.0])

    def test_sparse_generators_stream_unique_non_self_edges(self):
        """Sparse wrappers should stream valid edges without a global edge set."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(user_types={}),
            SimpleNamespace(),
        )
        population_size = 1000
        users_by_type = {
            "normal": [
                UserRecord(user_id=index + 1, user_type="normal")
                for index in range(population_size)
            ],
            "bad": [
                UserRecord(
                    user_id=population_size + index + 1,
                    user_type="bad",
                )
                for index in range(population_size)
            ],
        }

        for generation_function in (
            planner._generate_density_uniform_edges,
            planner._generate_power_law_by_followers_edges,
        ):
            state = setup_db_module._NetworkGenerationState(
                population_size * 2,
                track_follower_counts=True,
            )
            edges = list(
                generation_function(
                    density=0.0003,
                    **({"power_law_exponent": 1.0} if generation_function == planner._generate_power_law_by_followers_edges else {}),
                    source_user_type="normal",
                    users_by_type=users_by_type,
                    state=state,
                )
            )
            state.close()
            pairs = [(edge.user_id, edge.friend_id) for edge in edges]
            self.assertEqual(len(pairs), len(set(pairs)))
            self.assertTrue(all(user_id != friend_id for user_id, friend_id in pairs))

    def test_random_walk_does_not_materialize_growing_user_prefixes(self):
        """Random walk should index earlier users instead of slicing each prefix."""

        class PrefixGuardList(list):
            def __getitem__(self, index):
                if (
                    isinstance(index, slice)
                    and index.stop is not None
                    and index.stop > 3
                ):
                    raise AssertionError("growing user prefix was materialized")
                return super().__getitem__(index)

        planner = DefaultSetupDbPlanner(
            SimpleNamespace(user_types={}),
            SimpleNamespace(),
        )
        source_users = PrefixGuardList(
            UserRecord(user_id=user_id, user_type="normal")
            for user_id in range(1, 101)
        )
        edges = list(
            planner._generate_random_walk_edges(
                source_user_type="normal",
                users_by_type={"normal": source_users},
                parameters={
                    "seed_size": 3,
                    "avg_n_friend": 3,
                    "probability_follow": 0.5,
                },
                state=setup_db_module._NetworkGenerationState(
                    len(source_users),
                    track_follower_counts=False,
                ),
            )
        )

        self.assertEqual(len(edges), 297)

    def test_large_loop_refreshes_counter_before_first_whole_percent(self):
        """Large runs should expose current counts before reaching one percent."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(user_types={}),
            SimpleNamespace(),
        )
        reported = []

        with patch.object(
            setup_db_module.time,
            "monotonic",
            side_effect=(0.0, 0.5, 1.1),
        ):
            planner._reset_loop_progress()
            planner._update_loop_progress(1_000, 1_000_000_000, reported.append)
            planner._update_loop_progress(2_000, 1_000_000_000, reported.append)

        self.assertEqual(reported, [2_000])

    def test_weighted_index_sampler_preserves_blocks_and_uniqueness(self):
        """Weighted sparse selection should exclude blocks and avoid replacement."""
        sampler = setup_db_module._WeightedIndexSampler([1.0, 2.0, 3.0, 4.0])

        selected = sampler.sample_without_replacement(3, {2})

        self.assertEqual(len(selected), 3)
        self.assertEqual(len(set(selected)), 3)
        self.assertNotIn(2, selected)
        self.assertEqual(float(sampler.weights[2]), 3.0)

    def test_disk_backed_weight_tree_matches_follower_counts_across_chunks(self):
        """Chunked Fenwick construction should preserve exact cumulative mass."""
        follower_counts = setup_db_module.np.arange(10, dtype=setup_db_module.np.uint64)
        candidate_ids = setup_db_module.np.arange(
            1,
            11,
            dtype=setup_db_module.np.uint8,
        )
        with patch.object(setup_db_module, "COMPACT_ARRAY_MEMORY_LIMIT_BYTES", 0):
            with patch.object(setup_db_module, "COMPACT_ARRAY_BUILD_CHUNK_SIZE", 3):
                sampler = setup_db_module._WeightedIndexSampler.from_follower_counts(
                    candidate_ids,
                    follower_counts,
                    1.0,
                )
        try:
            self.assertEqual(sampler._weights_storage.storage_kind, "temporary_file")
            self.assertAlmostEqual(sampler._total_weight(), 55.0)
            sampler.set_weight(9, 20.0)
            self.assertAlmostEqual(sampler._total_weight(), 65.0)
        finally:
            sampler.close()

    def test_random_walk_creates_clustered_growth_edges(self):
        """Random-walk growth should follow the selected target's successors."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(user_types={}),
            SimpleNamespace(),
        )
        users_by_type = {
            "normal": [
                UserRecord(user_id=user_id, user_type="normal")
                for user_id in range(1, 7)
            ]
        }
        edges = list(
            planner._generate_random_walk_edges(
                source_user_type="normal",
                users_by_type=users_by_type,
                parameters={
                    "seed_size": 3,
                    "avg_n_friend": 3,
                    "probability_follow": 1.0,
                },
                state=setup_db_module._NetworkGenerationState(
                    6,
                    track_follower_counts=False,
                ),
            )
        )

        generated_pairs = [(edge.user_id, edge.friend_id) for edge in edges]
        normal_user_ids = {user.user_id for user in users_by_type["normal"]}
        self.assertGreater(len(generated_pairs), 0)
        self.assertEqual(len(generated_pairs), len(set(generated_pairs)))
        self.assertTrue(
            all(
                user_id in normal_user_ids
                and friend_id in normal_user_ids
                and user_id != friend_id
                for user_id, friend_id in generated_pairs
            )
        )
        self.assertEqual(
            sum(user_id == 5 for user_id, _ in generated_pairs),
            3,
        )

    def test_random_walk_rejects_seed_smaller_than_link_count(self):
        """Random-walk setup should reject an undersized initial seed."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(user_types={}),
            SimpleNamespace(),
        )

        with self.assertRaisesRegex(ValueError, "seed_size must not be smaller"):
            list(
                planner._generate_random_walk_edges(
                    source_user_type="normal",
                    users_by_type={
                        "normal": [
                            UserRecord(user_id=user_id, user_type="normal")
                            for user_id in range(1, 6)
                        ]
                    },
                    parameters={
                        "seed_size": 2,
                        "avg_n_friend": 3,
                        "probability_follow": 1.0,
                    },
                    state=setup_db_module._NetworkGenerationState(
                        5,
                        track_follower_counts=False,
                    ),
                )
            )

    def test_random_walk_allows_seed_equal_to_link_count(self):
        """Random-walk setup should allow a seed equal to the link budget."""
        planner = DefaultSetupDbPlanner(
            SimpleNamespace(user_types={}),
            SimpleNamespace(),
        )
        edges = list(
            planner._generate_random_walk_edges(
                source_user_type="normal",
                users_by_type={
                    "normal": [
                        UserRecord(user_id=user_id, user_type="normal")
                        for user_id in range(1, 5)
                    ]
                },
                parameters={
                    "seed_size": 3,
                    "avg_n_friend": 3,
                    "probability_follow": 1.0,
                },
                state=setup_db_module._NetworkGenerationState(
                    4,
                    track_follower_counts=False,
                ),
            )
        )

        self.assertEqual(
            sum(edge.user_id == 4 for edge in edges),
            3,
        )


if __name__ == "__main__":
    unittest.main()
