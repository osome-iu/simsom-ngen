# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Unit tests for the profile state used by profile-aware network generation."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from db.core import UserRecord  # noqa: E402
from subcomponents.setup_db import (  # noqa: E402
    CompactUserPopulation,
    DefaultSetupDbPlanner,
    WITHIN_TYPE_NETWORKING_PHASE,
    _unit_rows,
    edge_generation_function,
)


class ProfileProbePlanner(DefaultSetupDbPlanner):
    """Planner with one test-only edge function that records `state.profiles`."""

    seen_profiles = None

    @edge_generation_function(
        "profile_probe",
        phases=(WITHIN_TYPE_NETWORKING_PHASE,),
        requires_profiles=True,
    )
    def _generate_profile_probe(
        self,
        *,
        source_user_type,
        users_by_type,
        state,
        progress=None,
    ):
        """Copy the profile rows seen by network generation and emit no edges."""
        type(self).seen_profiles = (
            None if state.profiles is None else np.array(state.profiles)
        )
        return iter(())


def setup_config(within_type_function, num_nodes=5):
    """Return a one-type synthetic setup config using `within_type_function`."""
    return SimpleNamespace(
        network_source="synthetic",
        user_types={
            "normal": {
                "num_nodes": num_nodes,
                "lurker_ratio": 0.0,
                "edge_generation": {
                    "within_type_networking_function": within_type_function,
                    "cross_type_networking_function": {"no_edges": {}},
                },
                "activity_level_distribution": {"power_law_activity": {
                    "power_law_exponent": 2.0,
                    "minimum_activity_level": 0.1,
                    "maximum_activity_level": 10.0,
                }},
                "user_quality_distribution": {"w": 0.85, "lambda_slope": 0.1190},
            },
        },
    )


def behavior_config():
    """Return user behavior config with random three-dimensional profiles."""
    return SimpleNamespace(
        p_actions={"like": [1.0, 0.0]},
        vector_profile_generation_function={
            "uniform_random_vector_profile": {
                "dimension": 3, "minimum": -1.0, "maximum": 1.0,
            }
        },
    )


class ProfileStateTests(unittest.TestCase):
    """Validate how setup stores profiles for edge functions that read them."""

    def setUp(self):
        ProfileProbePlanner.seen_profiles = None

    def test_population_stores_unit_profiles_and_checks_size(self):
        population = CompactUserPopulation(
            type_names=("t",), type_counts=(4,), assignment_codes=None,
            single_type_code=0, user_id_dtype=np.uint8, track_profiles=True,
        )
        population.store_profiles(0, np.array([[3.0, 4.0], [0.0, 0.0]]))
        population.store_profiles(2, np.array([[0.0, 2.0], [1.0, 1.0]]))

        self.assertEqual(population.profiles_filled, 4)
        np.testing.assert_allclose(population.profiles[0], [0.6, 0.8], rtol=1e-6)
        np.testing.assert_allclose(population.profiles[1], [0.0, 0.0])
        np.testing.assert_allclose(population.profiles[3], [2 ** -0.5] * 2, rtol=1e-6)
        with self.assertRaises(ValueError):
            population.store_profiles(0, np.ones((2, 3)))
        population.close()
        self.assertIsNone(population.profiles)

    def test_population_ignores_profiles_when_not_tracked(self):
        population = CompactUserPopulation(
            type_names=("t",), type_counts=(2,), assignment_codes=None,
            single_type_code=0, user_id_dtype=np.uint8,
        )
        population.store_profiles(0, np.ones((2, 1)))
        self.assertIsNone(population.profiles)

    def test_unit_rows_rejects_empty_or_nonfinite_vectors(self):
        for vectors in ([[1.0, float("nan")]], [[float("inf")]], [[]], [1.0, 2.0]):
            with self.subTest(vectors=vectors):
                with self.assertRaises(ValueError):
                    _unit_rows(vectors)

    def test_shipped_functions_do_not_store_profiles(self):
        planner = DefaultSetupDbPlanner(
            setup_config({"random_walk": {
                "seed_size": 2, "avg_n_friend": 2, "probability_follow": 0.5,
            }}),
            behavior_config(),
        )
        self.assertFalse(
            planner._network_requires_profiles(
                planner._user_types(),
                {"normal": range(1, 6)},
            )
        )

    def test_stream_passes_stored_profiles_to_network_generation(self):
        planner = ProfileProbePlanner(
            setup_config({"profile_probe": {}}),
            behavior_config(),
        )

        dataset = planner.build_dataset()

        expected = _unit_rows([user.vector_profile for user in dataset.users])
        self.assertEqual(ProfileProbePlanner.seen_profiles.shape, (5, 3))
        np.testing.assert_allclose(ProfileProbePlanner.seen_profiles, expected)

    def test_network_stream_requires_the_user_stream_first(self):
        planner = ProfileProbePlanner(
            setup_config({"profile_probe": {}}),
            behavior_config(),
        )
        stream = planner.build_stream()
        try:
            with self.assertRaisesRegex(RuntimeError, "user stream"):
                list(stream.follower_network)
        finally:
            stream.close()

    def test_user_list_builder_passes_profiles_by_user_id(self):
        planner = ProfileProbePlanner(
            setup_config({"profile_probe": {}}, num_nodes=2),
            behavior_config(),
        )
        users = [
            UserRecord(user_id=2, user_type="normal", vector_profile=[0.0, 5.0]),
            UserRecord(user_id=1, user_type="normal", vector_profile=[3.0, 4.0]),
        ]

        planner._build_synthetic_follower_network(users)

        np.testing.assert_allclose(
            ProfileProbePlanner.seen_profiles,
            [[0.6, 0.8], [0.0, 1.0]],
            rtol=1e-6,
        )

    def test_user_list_builder_rejects_profiles_of_different_sizes(self):
        planner = ProfileProbePlanner(
            setup_config({"profile_probe": {}}, num_nodes=2),
            behavior_config(),
        )
        users = [
            UserRecord(user_id=1, user_type="normal", vector_profile=[1.0]),
            UserRecord(user_id=2, user_type="normal", vector_profile=[1.0, 0.0]),
        ]
        with self.assertRaisesRegex(ValueError, "same size"):
            planner._build_synthetic_follower_network(users)


if __name__ == "__main__":
    unittest.main()
