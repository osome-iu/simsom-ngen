# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Behavioral checks for explicit configured model inputs."""

import copy
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from config_helper import CONFIG, _validate_required_config
from subcomponents.post_labeling import PostLabelingContext, resolve_post_labeling_functions
from domain.post_label_semantics import INTERNAL_SURVIVAL_DURATION_MS_LABEL, LEGALITY_LABEL
from domain.termination_semantics import evaluate_termination_criteria
from domain.function_config import resolve_functions, resolve_one


class FunctionInputTests(unittest.TestCase):
    def test_parameterless_functions_require_named_input_objects(self):
        calls = []

        def record(context):
            calls.append(context)
            return context

        registry = {"record": record}
        for selection in ("record", ["record"], [], None, {"record": None}, {"record": []}):
            with self.subTest(selection=selection), self.assertRaisesRegex(ValueError, "config"):
                resolve_functions(selection, registry, "config")
        self.assertEqual(calls, [])
        self.assertEqual(resolve_functions({}, registry, "config"), ())
        with self.assertRaisesRegex(ValueError, "exactly one"):
            resolve_one({}, registry, "config")
        for selection in ({"record": {}}, SimpleNamespace(record=SimpleNamespace())):
            _, function = resolve_one(selection, registry, "config")
            self.assertEqual(function("runtime context"), "runtime context")
        self.assertEqual(calls, ["runtime context", "runtime context"])

    def test_configuration_rejects_shorthand_across_function_fields(self):
        paths = [
            ("setup_db", "user_types", "bad", "edge_generation", "cross_type_networking_function"),
            ("setup_db", "user_types", "normal", "post_labeling_functions"),
            ("user_behavior", "content_embedding_generation_function"),
            ("recsys", "feed_weight_components", 0, "appeal_component_function"),
            ("user_behavior", "feed_weight_components", 0, "appeal_component_function"),
            ("policy", "rule_functions"),
            ("termination", "criteria_functions"),
            ("analytics", "recurring_functions"),
            ("analytics", "final_functions"),
        ]
        for path in paths:
            for selection in ("no_edges", ["no_edges"], []):
                with self.subTest(path=path, selection=selection):
                    config = copy.deepcopy(CONFIG.config)
                    parent = config
                    for key in path[:-1]:
                        parent = parent[key]
                    parent[path[-1]] = selection
                    with self.assertRaisesRegex(ValueError, "object mapping function names to inputs"):
                        _validate_required_config(config)

    def test_custom_probability_and_hours_reach_the_sampler(self):
        context = PostLabelingContext(None, None, {})
        _, function = resolve_post_labeling_functions(
            {"illegal_probability_n_survival_hours": {"probability": 0.2, "expected_survival_hours": 2}},
            "labels",
        )[0]
        with patch("subcomponents.post_labeling.random.random", return_value=0.15), patch(
            "subcomponents.post_labeling.random.expovariate", return_value=1000,
        ) as sampler:
            labels = function(context)
        sampler.assert_called_once_with(1 / 7200000)
        self.assertEqual(labels[INTERNAL_SURVIVAL_DURATION_MS_LABEL], 1000)
        self.assertEqual(labels[LEGALITY_LABEL], "illegal")
        with patch("subcomponents.post_labeling.random.random", return_value=0.25):
            self.assertEqual(function(context), {})

    def test_missing_and_unknown_inputs_fail_during_configuration(self):
        for inputs, message in [
            ({"probability": 0.01}, "missing required inputs: expected_survival_hours"),
            ({"probability": 0.01, "expected_survival_hours": 144, "typo": 1}, "unknown inputs: typo"),
            ({"probability": float("nan"), "expected_survival_hours": 144}, "must be finite"),
        ]:
            with self.subTest(inputs=inputs):
                config = copy.deepcopy(CONFIG.config)
                config["setup_db"]["user_types"]["normal"]["post_labeling_functions"] = {
                    "illegal_probability_n_survival_hours": inputs,
                }
                with self.assertRaisesRegex(ValueError, message):
                    _validate_required_config(config)

    def test_network_inputs_are_required(self):
        config = copy.deepcopy(CONFIG.config)
        del config["setup_db"]["user_types"]["normal"]["edge_generation"]["within_type_networking_function"]["random_walk"]["probability_follow"]
        with self.assertRaisesRegex(ValueError, "missing required inputs: probability_follow"):
            _validate_required_config(config)

    def test_custom_termination_threshold_changes_the_decision(self):
        metrics = SimpleNamespace(user_count=10, action_count=25)
        self.assertTrue(evaluate_termination_criteria(
            None, metrics, {"max_avg_actions_per_user": {"threshold": 2}},
        )[0].met)
        self.assertFalse(evaluate_termination_criteria(
            None, metrics, {"max_avg_actions_per_user": {"threshold": 3}},
        )[0].met)
