# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Regression coverage for numeric, population and action-preference contracts."""

import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import json
import numpy as np

SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from config_helper import CONFIG, _validate_required_config
from subcomponents.setup_db import DefaultSetupDbPlanner
from subcomponents import recsys, user_executor


class InitializationContractTests(unittest.TestCase):
    def test_enabled_draws_are_clipped_but_disabled_types_are_not_sampled(self):
        with patch("subcomponents.setup_db.np.random.normal", side_effect=[
            np.array([-0.1, 0.0, 0.000001]), np.array([0.5, -0.2, 0.0]),
        ]) as draw:
            probabilities = DefaultSetupDbPlanner._sample_user_action_probabilities(
                {"like": [0.5, 0.2], "post": [0.2, 0.3], "quote": [0, 0]}, 3,
            )
        self.assertEqual(draw.call_count, 2)
        np.testing.assert_allclose(probabilities["like"], [0.0, 0.5 / 0.7, 1.0])
        np.testing.assert_allclose(probabilities["post"], [1.0, 0.2 / 0.7, 0.0])
        np.testing.assert_array_equal(probabilities["quote"], [0, 0, 0])
        np.testing.assert_allclose(sum(probabilities.values()), 1.0)

    def test_all_zero_clipped_draws_use_configured_means(self):
        with patch(
            "subcomponents.setup_db.np.random.normal",
            side_effect=[np.array([-0.1, -0.2]), np.array([-0.3, -0.4])],
        ):
            probabilities = DefaultSetupDbPlanner._sample_user_action_probabilities(
                {"like": [0.2, 0.1], "post": [0.8, 0.1]}, 2,
            )
        np.testing.assert_allclose(probabilities["like"], [0.2, 0.2])
        np.testing.assert_allclose(probabilities["post"], [0.8, 0.8])

    def test_all_zero_fallback_with_zero_means_is_rejected(self):
        with patch(
            "subcomponents.setup_db.np.random.normal",
            return_value=np.array([-0.1]),
        ):
            with self.assertRaisesRegex(ValueError, "enabled means are zero"):
                DefaultSetupDbPlanner._sample_user_action_probabilities(
                    {"post": [0, 0.1]}, 1,
                )

    def test_all_disabled_configuration_is_rejected(self):
        config = copy.deepcopy(CONFIG.config)
        config["user_behavior"]["p_actions"] = {
            key: [0, 0] for key in config["user_behavior"]["p_actions"]
        }
        with self.assertRaisesRegex(ValueError, "enable at least one"):
            _validate_required_config(config)

    def test_enabled_action_types_require_a_positive_mean(self):
        config = copy.deepcopy(CONFIG.config)
        config["user_behavior"]["p_actions"] = {
            key: [0, 0.1] for key in config["user_behavior"]["p_actions"]
        }
        with self.assertRaisesRegex(ValueError, "positive mean"):
            _validate_required_config(config)

    def test_normalization_handles_large_finite_weights(self):
        result = DefaultSetupDbPlanner._generate_user_event_dist(
            {"post": [1e308, 0], "like": [1e308, 0], "quote": [0, 0]},
        )
        self.assertEqual(result, {"post": 0.5, "like": 0.5, "quote": 0.0})

    def test_nonfinite_draws_are_rejected(self):
        with patch("subcomponents.setup_db.np.random.normal", return_value=np.array([np.nan])):
            with self.assertRaisesRegex(ValueError, "nonfinite weights"):
                DefaultSetupDbPlanner._generate_user_event_dist({"post": [1, 0.1]})

    def test_an_inactive_user_type_is_warned_and_allowed(self):
        for ratio in (0.9, 1.0):
            with self.subTest(ratio=ratio):
                config = copy.deepcopy(CONFIG.config)
                for value in config["setup_db"]["user_types"].values():
                    value.update(num_nodes=3, lurker_ratio=ratio)
                config["setup_db"]["user_types"]["normal"].update(num_nodes=2, lurker_ratio=0)
                with self.assertNoLogs("config_helper", level="WARNING"):
                    warnings = _validate_required_config(config)
                self.assertEqual(warnings, (
                    "User type bad has no active users after lurker rounding; "
                    "other user types provide active users",
                ))

    def test_all_lurkers_is_a_population_error(self):
        for ratio in (0.9, 1.0):
            with self.subTest(ratio=ratio):
                config = copy.deepcopy(CONFIG.config)
                for value in config["setup_db"]["user_types"].values():
                    value.update(num_nodes=3, lurker_ratio=ratio)
                with self.assertRaisesRegex(ValueError, "no active users after lurker rounding"):
                    _validate_required_config(config)

    def test_launcher_exits_cleanly_before_starting_mpi_for_all_lurkers(self):
        config = copy.deepcopy(CONFIG.config)
        for value in config["setup_db"]["user_types"].values():
            value.update(num_nodes=3, lurker_ratio=0.9)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(config))
            result = subprocess.run(
                [sys.executable, str(SIMSOM_ROOT / "run_simulation.py"), "--simulator_spec", str(path)],
                capture_output=True, text=True, timeout=10,
                env={**os.environ, "MPI4PY_RC_INITIALIZE": "0"},
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("no active users after lurker rounding", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("Starting SimSoM", result.stdout)

    def test_nonfinite_configuration_and_scoring_results_are_rejected(self):
        for number in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(number=number):
                config = copy.deepcopy(CONFIG.config)
                config["recsys"]["feed_weight_components"][0]["importance_in"] = number
                with self.assertRaisesRegex(ValueError, "must be finite"):
                    _validate_required_config(config)
                for module in (recsys, user_executor):
                    with self.assertRaisesRegex(ValueError, "must be finite"):
                        module._require_unit_interval_component_value(number, "custom", "components")

    def test_zero_scoring_values_remain_valid(self):
        for module in (recsys, user_executor):
            self.assertEqual(module._require_unit_interval_component_value(0, "custom", "components"), 0)
