# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Unit tests for `homophilic_random_walk` and the profile state it needs."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from db.core import UserRecord  # noqa: E402
import subcomponents.setup_db as setup_db_module  # noqa: E402
from subcomponents.setup_db import (  # noqa: E402
    CompactUserPopulation,
    DefaultSetupDbPlanner,
    WITHIN_TYPE_NETWORKING_PHASE,
    _NetworkGenerationState,
    _uniform_subset,
    _unit_rows,
    edge_generation_function,
)

N_USERS = 400
LINKS_PER_USER = 8


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


def run_walk(profiles=None, alpha=None, seed=7, probability_follow=0.4):
    """Return the edges of one random-walk run over users 1..N_USERS."""
    planner = DefaultSetupDbPlanner({}, {})
    parameters = {
        "seed_size": LINKS_PER_USER,
        "avg_n_friend": LINKS_PER_USER,
        "probability_follow": probability_follow,
    }
    if alpha is not None:
        parameters["alpha"] = alpha
    np.random.seed(seed)
    with _NetworkGenerationState(
        N_USERS,
        track_follower_counts=False,
        profiles=profiles,
    ) as state:
        return [
            (edge.user_id, edge.friend_id)
            for edge in planner._generate_random_walk_edges(
                source_user_type="normal",
                users_by_type={"normal": range(1, N_USERS + 1)},
                parameters=parameters,
                state=state,
            )
        ]


def clustered_profiles(groups=4, dimension=6, seed=0):
    """Return unit profiles in well separated clusters and each user's cluster."""
    generator = np.random.default_rng(seed)
    centres = generator.normal(size=(groups, dimension)) * 4
    group = generator.integers(0, groups, N_USERS)
    noise = generator.normal(size=(N_USERS, dimension))
    return _unit_rows(centres[group] + noise), group


def profiles_with_cosines(cosines):
    """Return 2-D unit profiles whose cosine with the last row is `cosines`."""
    cosines = np.asarray(cosines, dtype=np.float64)
    rows = np.stack([cosines, np.sqrt(1.0 - cosines ** 2)], axis=1)
    return np.vstack([rows, [[1.0, 0.0]]]).astype(np.float32)


class HomophilicRandomWalkTests(unittest.TestCase):
    """Validate the profile-weighted links of `homophilic_random_walk`."""

    def test_alpha_zero_reproduces_random_walk(self):
        profiles, _ = clustered_profiles()
        self.assertEqual(run_walk(profiles=profiles, alpha=0.0), run_walk())

    def test_edges_are_valid_and_count_is_exact(self):
        profiles, _ = clustered_profiles()
        edges = run_walk(profiles=profiles, alpha=8.0)
        self.assertEqual(
            len(edges),
            LINKS_PER_USER * (LINKS_PER_USER - 1)
            + (N_USERS - LINKS_PER_USER) * LINKS_PER_USER,
        )
        self.assertEqual(len(set(edges)), len(edges))
        self.assertTrue(all(user != friend for user, friend in edges))
        self.assertTrue(
            all(friend < user for user, friend in edges if user > LINKS_PER_USER)
        )

    def test_alpha_raises_links_within_profile_clusters(self):
        profiles, group = clustered_profiles()

        def share_within(edges):
            late = [(u, f) for u, f in edges if u > LINKS_PER_USER]
            return np.mean([group[u - 1] == group[f - 1] for u, f in late])

        self.assertGreater(
            share_within(run_walk(profiles=profiles, alpha=8.0)),
            share_within(run_walk()) + 0.3,
        )

    def test_positive_alpha_without_profiles_raises(self):
        with self.assertRaisesRegex(RuntimeError, "requires user profiles"):
            run_walk(profiles=None, alpha=1.0)

    def test_negative_alpha_is_rejected(self):
        profiles, _ = clustered_profiles()
        with self.assertRaisesRegex(ValueError, "alpha"):
            run_walk(profiles=profiles, alpha=-1.0)

    def test_similar_indexes_are_distinct_unblocked_and_earlier(self):
        profiles, _ = clustered_profiles()
        blocked = {0, 3, 17}
        # 10,000 scores every candidate; 50 scores a subset.
        for sample_size in (10_000, 50):
            with self.subTest(sample_size=sample_size):
                with patch.object(
                    setup_db_module,
                    "HOMOPHILY_CANDIDATE_SAMPLE_SIZE",
                    sample_size,
                ):
                    np.random.seed(1)
                    chosen = DefaultSetupDbPlanner._sample_similar_indexes(
                        range(1, N_USERS + 1), 300, 20, blocked, profiles, 8.0,
                    )
                self.assertEqual(len(chosen), 20)
                self.assertEqual(len(set(chosen)), 20)
                self.assertFalse(set(chosen) & blocked)
                self.assertTrue(all(0 <= index < 300 for index in chosen))

    def test_similar_indexes_follow_the_weights(self):
        cosines = np.array([-1.0, -0.5, 0.0, 0.5, 1.0])
        profiles = profiles_with_cosines(cosines)
        alpha = 2.0
        expected = (1 + cosines) ** alpha / ((1 + cosines) ** alpha).sum()
        draws = 20_000
        counts = np.zeros(len(cosines))
        np.random.seed(3)
        for _ in range(draws):
            counts[
                DefaultSetupDbPlanner._sample_similar_indexes(
                    range(1, 7), 5, 1, set(), profiles, alpha,
                )
            ] += 1
        self.assertEqual(counts[0], 0)  # cosine -1 has weight 0
        np.testing.assert_allclose(counts / draws, expected, atol=0.015)

    def test_large_alpha_keeps_the_most_similar(self):
        profiles = profiles_with_cosines([-0.9, -0.5, 0.2, 0.9])
        np.random.seed(5)
        picks = {
            DefaultSetupDbPlanner._sample_similar_indexes(
                range(1, 6), 4, 1, set(), profiles, 2000.0,
            )[0]
            for _ in range(200)
        }
        self.assertEqual(picks, {3})

    def test_more_draws_than_the_candidate_subset(self):
        profiles, _ = clustered_profiles()
        np.random.seed(4)
        with patch.object(setup_db_module, "HOMOPHILY_CANDIDATE_SAMPLE_SIZE", 50):
            chosen = DefaultSetupDbPlanner._sample_similar_indexes(
                range(1, N_USERS + 1), 300, 120, {0}, profiles, 1.0,
            )
        self.assertEqual(len(chosen), 120)
        self.assertEqual(len(set(chosen)), 120)
        self.assertNotIn(0, chosen)

    def test_uniform_subset_is_distinct_unblocked_and_uniform(self):
        blocked = np.array([0, 5, 9], dtype=np.int64)
        draws = 20_000
        counts = np.zeros(20)
        np.random.seed(2)
        for _ in range(draws):
            subset = _uniform_subset(20, 6, blocked)
            self.assertEqual(len(set(subset.tolist())), 6)
            counts[subset] += 1
        self.assertEqual(counts[blocked].sum(), 0)
        free = np.setdiff1d(np.arange(20), blocked)
        np.testing.assert_allclose(counts[free] / draws, 6 / 17, atol=0.015)

    def test_uniform_subset_of_a_large_population(self):
        blocked = np.array([1, 2, 3], dtype=np.int64)
        np.random.seed(6)
        subset = _uniform_subset(1_000_000, 1_000, blocked)
        self.assertEqual(len(set(subset.tolist())), 1_000)
        self.assertFalse(np.isin(subset, blocked).any())
        self.assertTrue(((subset >= 0) & (subset < 1_000_000)).all())

    def test_configured_function_builds_a_homophilic_network(self):
        planner = DefaultSetupDbPlanner(
            setup_config(
                {"homophilic_random_walk": {
                    "seed_size": 3,
                    "avg_n_friend": 3,
                    "probability_follow": 0.5,
                    "alpha": 8.0,
                }},
                num_nodes=50,
            ),
            behavior_config(),
        )

        dataset = planner.build_dataset()

        edges = [(edge.user_id, edge.friend_id) for edge in dataset.follower_network]
        self.assertEqual(len(edges), 3 * 50 - 3)
        self.assertEqual(len(set(edges)), len(edges))
        self.assertTrue(all(user != friend for user, friend in edges))


if __name__ == "__main__":
    unittest.main()
