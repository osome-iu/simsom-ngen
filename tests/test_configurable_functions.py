# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Exercise every shipped configurable algorithm and extension binding boundary."""

from __future__ import annotations

import copy
import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'libs/simsom'))

from config_helper import CONFIG, _validate_required_config
from db.worker import POST_DISTRIBUTION_FUNCTIONS, QUALITY_PROPERTY, APPEAL_PROPERTY, sample_generated_property
from domain.function_config import resolve_one
from domain.component_semantics import COMPONENT_FUNCTIONS_BY_SELECTOR, resolve_component_function
from domain.termination_semantics import TERMINATION_CRITERION_FUNCTIONS, evaluate_termination_criteria
from subcomponents import recsys, user_executor
from subcomponents.analytics_output import ANALYTICS_FUNCTIONS, AnalyticsFunctionRunner
from subcomponents.policy_rules import POLICY_RULE_FUNCTIONS, PolicyRuleContext, ReviewTarget, resolve_policy_rule_functions
from subcomponents.post_labeling import POST_LABELING_FUNCTIONS, SURVIVAL_DURATION_FUNCTIONS, PostLabelingContext, resolve_post_labeling_functions
from subcomponents.setup_db import DefaultSetupDbPlanner


NETWORK_INPUTS = {
    'no_edges': {},
    'density_uniform_sparse': {'density': 1.0},
    'random_walk': {'seed_size': 2, 'avg_n_friend': 2, 'probability_follow': 0.8},
    'power_law_by_followers_sparse': {'density': 1.0, 'power_law_exponent': 2.0},
}
ACTIVITY_INPUTS = {'power_law_activity': {
    'power_law_exponent': 3.0, 'minimum_activity_level': 2.0, 'maximum_activity_level': 3.0,
}}
LABEL_INPUTS = {
    'legal_or_illegal_probability': {'probability': 0.25},
    'illegal_probability_n_survival_hours': {'probability': 0.25, 'expected_survival_hours': 2.0},
}
RULE_INPUTS = {'below_quality_threshold': {'threshold': 0.7}, 'illegal_content': {}}
TERMINATION_INPUTS = {
    'max_avg_actions_per_user': {'threshold': 2.0},
    'exponential_moving_average_action_quality': {'threshold': 0.21, 'rho': 0.5},
}
CONTENT_EMBEDDING_INPUTS = {'social_media_archive_content_embedding': {
    'gen_gamma': {'a': 1.0, 'c': 1.0, 'loc': 0.0, 'scale': 0.5},
}}


def shipped(registry, module):
    return {name: function for name, function in registry.items() if function.__module__ == module}


def configuration():
    config = copy.deepcopy(CONFIG.config)
    for value in config['setup_db']['user_types'].values():
        value.update(num_nodes=6, lurker_ratio=0.0, activity_level_distribution=copy.deepcopy(ACTIVITY_INPUTS))
    return config


class ConfigurableFunctionTests(unittest.TestCase):
    def test_all_registered_input_contracts(self):
        """No shipped callable escapes missing/unknown/nonfinite input checks."""
        planner = DefaultSetupDbPlanner
        edges = {name: getattr(planner, spec.method_name) for name, spec in planner._edge_generation_registry().items()}
        activities = {name: spec.function for name, spec in planner._activity_distribution_registry().items()}
        cases = [
            (edges, NETWORK_INPUTS, 1, ('source_user_type', 'users_by_type', 'state', 'progress')),
            (activities, ACTIVITY_INPUTS, 1, ()),
            (shipped(POST_DISTRIBUTION_FUNCTIONS, 'db.worker'), {'gaussian_around_user_quality': {'stddev': 0.4}}, 2, ()),
            (shipped(POST_LABELING_FUNCTIONS, 'subcomponents.post_labeling'), LABEL_INPUTS, 1, ()),
            (SURVIVAL_DURATION_FUNCTIONS, {'exponential_survival_hours': {'expected_survival_hours': 2.0}}, 1, ()),
            (shipped(POLICY_RULE_FUNCTIONS, 'subcomponents.policy_rules'), RULE_INPUTS, 1, ()),
            (shipped(TERMINATION_CRITERION_FUNCTIONS, 'domain.termination_semantics'), TERMINATION_INPUTS, 2, ()),
            (shipped(user_executor.CONTENT_EMBEDDING_GENERATION_FUNCTIONS, 'subcomponents.user_executor'), {
                'dummy_content_embedding': {}, **CONTENT_EMBEDDING_INPUTS,
            }, 1, ()),
        ]
        for registry, inputs_by_name, count, keywords in cases:
            self.assertEqual(set(registry), set(inputs_by_name))
            for name, inputs in inputs_by_name.items():
                with self.subTest(function=name):
                    kwargs = {'runtime_count': count, 'runtime_keywords': keywords}
                    resolve_one({name: inputs}, registry, 'config', **kwargs)
                    resolve_one(SimpleNamespace(**{name: SimpleNamespace(**inputs)}), registry, 'config', **kwargs)
                    with self.assertRaisesRegex(ValueError, 'unknown inputs'):
                        resolve_one({name: {**inputs, 'misspelled_input': 1}}, registry, 'config', **kwargs)
                    for key, value in inputs.items():
                        with self.subTest(input=key):
                            missing = {k: v for k, v in inputs.items() if k != key}
                            with self.assertRaisesRegex(ValueError, 'missing required inputs'):
                                resolve_one({name: missing}, registry, 'config', **kwargs)
                            for invalid in (float('nan'), float('inf'), -float('inf'), '0.2', True):
                                with self.assertRaises(ValueError):
                                    resolve_one({name: {**inputs, key: invalid}}, registry, 'config', **kwargs)
                    if inputs:
                        with self.assertRaisesRegex(ValueError, 'missing required inputs'):
                            resolve_one({name: {}}, registry, 'config', **kwargs)

    def test_content_and_profile_generators_are_registered(self):
        content_names = user_executor.allowed_content_embedding_generation_function_names()
        self.assertEqual(
            set(content_names),
            {'dummy_content_embedding', 'social_media_archive_content_embedding'},
        )
        profile_names = DefaultSetupDbPlanner.vector_profile_generation_function_names()
        self.assertIn('pca_7param_social_media_archive_profile', profile_names)

    def test_archive_embedding_uses_profile_and_gamma_configuration(self):
        generator = user_executor._content_embedding_generation_function_from_config(
            {'social_media_archive_content_embedding': CONTENT_EMBEDDING_INPUTS['social_media_archive_content_embedding']}
        )
        profile = [1.0] + [0.0] * 9

        embedding = generator(profile)

        self.assertEqual(len(embedding), 10)
        self.assertAlmostEqual(math.hypot(*embedding), math.hypot(*profile))

    def test_every_network_function_executes_with_nondefault_inputs(self):
        for within, within_count in [('no_edges', 0), ('density_uniform_sparse', 60), ('random_walk', 20)]:
            for cross, cross_count in [('no_edges', 0), ('power_law_by_followers_sparse', 72)]:
                with self.subTest(within=within, cross=cross):
                    config = configuration()
                    for value in config['setup_db']['user_types'].values():
                        value['edge_generation'] = {
                            'within_type_networking_function': {within: NETWORK_INPUTS[within]},
                            'cross_type_networking_function': {cross: NETWORK_INPUTS[cross]},
                        }
                    _validate_required_config(config)
                    dataset = DefaultSetupDbPlanner(config['setup_db'], config['user_behavior']).build_dataset()
                    edges = [(edge.user_id, edge.friend_id) for edge in dataset.follower_network]
                    self.assertEqual(len(edges), within_count + cross_count)
                    self.assertEqual(len(set(edges)), len(edges))
                    self.assertTrue(all(a != b for a, b in edges))
                    self.assertTrue(all(2 <= user.activity_level <= 3 for user in dataset.users))

    def test_file_network_source_still_uses_explicit_config_path(self):
        config = configuration()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'network.csv'
            path.write_text('user_id,friend_id\n1,7\n8,2\n')
            config['setup_db'].update(network_source='file', network_file=str(path))
            _validate_required_config(config)
            dataset = DefaultSetupDbPlanner(config['setup_db'], config['user_behavior']).build_dataset()
        self.assertEqual([(edge.user_id, edge.friend_id) for edge in dataset.follower_network], [(1, 7), (8, 2)])

    def test_activity_parameters_reach_inverse_cdf_sampling(self):
        spec = DefaultSetupDbPlanner._activity_distribution_spec(
            {"activity_level_distribution": ACTIVITY_INPUTS}
        )
        with patch(
            "subcomponents.setup_db.np.random.uniform",
            return_value=np.array([0, 0.2, 1]),
        ) as sampler:
            values = spec.function(3)

        sampler.assert_called_once_with(size=3)
        np.testing.assert_allclose(values, [2, 29 ** 0.25, 3])

    def test_user_quality_flat_parameters_remain_effective(self):
        for w, expected_high in [(0, False), (1, True)]:
            with patch('subcomponents.setup_db.np.random.random', return_value=np.array([0.5])):
                values = DefaultSetupDbPlanner._draw_normalized_two_cluster_inverse_many(
                    {'user_quality_distribution': {'w': w, 'lambda_slope': 0.2}}, 1)
            self.assertEqual(values[0] > 0.5, expected_high)
            distance = -math.log(1 - 0.5 * (1 - math.exp(-20))) / 20
            self.assertAlmostEqual(values[0], 1 - distance if expected_high else distance)

    def test_quality_and_appeal_share_explicit_stddev_and_clamping(self):
        for spec in (QUALITY_PROPERTY, APPEAL_PROPERTY):
            for draw, expected in [(-1, 0), (0.7, 0.7), (2, 1)]:
                with self.subTest(property=spec.name, draw=draw), patch('db.worker.random.gauss', return_value=draw) as sampler:
                    result = sample_generated_property({'quality': 0.6}, spec=spec,
                        generation_function_name={'gaussian_around_user_quality': {'stddev': 0.4}})
                sampler.assert_called_once_with(0.6, 0.4)
                self.assertEqual(result, expected)

    def test_both_labelers_use_the_configured_probability(self):
        context = PostLabelingContext(None, None, {})
        for name, inputs in LABEL_INPUTS.items():
            _, function = resolve_post_labeling_functions({name: inputs}, 'labels')[0]
            with patch('subcomponents.post_labeling.random.random', return_value=0.2), patch(
                    'subcomponents.post_labeling.random.expovariate', return_value=42) as duration:
                labels = function(context)
            self.assertEqual(labels['legality'], 'illegal')
            if 'expected_survival_hours' in inputs:
                duration.assert_called_once_with(1 / 7200000)
            with patch('subcomponents.post_labeling.random.random', return_value=0.3):
                labels = function(context)
            self.assertEqual(labels, {'legality': 'legal'} if name == 'legal_or_illegal_probability' else {})

    def test_all_policy_rules_execute_and_threshold_changes_match(self):
        context = PolicyRuleContext(ReviewTarget('1', other_properties={
            'quality': 0.5, 'post_labels': {'legality': 'illegal'}}), current_timestamp=0)
        for name, function in resolve_policy_rule_functions(RULE_INPUTS, 'rules'):
            self.assertIsNotNone(function(context), name)
        _, lower = resolve_policy_rule_functions({'below_quality_threshold': {'threshold': 0.3}}, 'rules')[0]
        self.assertIsNone(lower(context))

    def test_all_termination_functions_execute_with_changed_parameters(self):
        metrics = SimpleNamespace(user_count=10, action_count=25, avg_action_quality=0.6)
        results = evaluate_termination_criteria(SimpleNamespace(), metrics, TERMINATION_INPUTS)
        self.assertEqual([result.function_name for result in results], list(TERMINATION_INPUTS))
        self.assertTrue(all(result.met for result in results))
        changed = copy.deepcopy(TERMINATION_INPUTS)
        changed['max_avg_actions_per_user']['threshold'] = 3
        changed['exponential_moving_average_action_quality']['rho'] = 0.25
        results = evaluate_termination_criteria(SimpleNamespace(), metrics, changed)
        self.assertTrue(all(not result.met for result in results))

    def test_all_scoring_components_work_in_both_consumers(self):
        user = recsys.UserProfile(user_id=1, user_type='normal', vector_profile=[1.0])
        item = recsys.FeedCandidate(action_id=1, creator_user_id=2, action_type='post', content_embedding=[1.0],
            other_properties={'appeal': 0.25, **{'engagement_' + action: 2 for action in ('like', 'repost', 'reply', 'quote')}})
        expected = {'post_appeal': 0.25, 'inverse_rank_recency': 0.5,
                    'profile_vector_n_content_embedding_cosine_relevance': 1.0,
                    **{'global_' + action + '_count': 2 / 3 for action in ('like', 'repost', 'reply', 'quote')}}
        visited = set()
        for selector, registry in COMPONENT_FUNCTIONS_BY_SELECTOR.items():
            for name, spec in registry.items():
                if spec.function.__module__ != 'domain.component_semantics':
                    continue
                visited.add(name)
                for selected in ({name: {}}, SimpleNamespace(**{name: SimpleNamespace()})):
                    entry = {selector: selected, 'importance_in': 0.3, 'importance_out': 0.9}
                    parsers = (recsys._parse_weight_components, user_executor._parse_weight_components)
                    for parser in parsers:
                        components = parser([entry], 'components')
                        self.assertAlmostEqual(components[0]['function'](user, item, 0.5), expected[name])
                    with self.assertRaisesRegex(ValueError, 'unknown inputs'):
                        resolve_component_function(selector, {name: {'hidden_input': 1}})
                for selected in (name, [name]):
                    with self.assertRaisesRegex(ValueError, 'object mapping function names to inputs'):
                        resolve_component_function(selector, selected)
        self.assertEqual(visited, set(expected))

    def test_parameterized_extensions_use_the_same_design_in_every_consumer(self):
        # String annotations deliberately exercise modules using future annotations.
        def component(user, item, recency, *, score: float = 0.4):
            return score
        def embedding(profile, *, scale: float):
            return [scale * value for value in profile]
        def analytics(metrics, *, multiplier: float):
            return metrics.action_count * multiplier
        from domain.component_semantics import ComponentFunctionSpec
        selector = 'appeal_component_function'
        with patch.dict(COMPONENT_FUNCTIONS_BY_SELECTOR[selector], {
            'test_explicit_score': ComponentFunctionSpec(selector, 'test_explicit_score', component),
        }), patch.dict(user_executor.CONTENT_EMBEDDING_GENERATION_FUNCTIONS, {'test_scaled': embedding}), patch.dict(
            ANALYTICS_FUNCTIONS, {'test_scaled': analytics}):
            entry = {selector: {'test_explicit_score': {'score': 0.7}}, 'importance_in': 1, 'importance_out': 1}
            for parser in (recsys._parse_weight_components, user_executor._parse_weight_components):
                self.assertEqual(parser([entry], 'components')[0]['function'](None, None, 0), 0.7)
                for inputs in ({}, {'score': True}, {'score': 0.7, 'typo': 1}):
                    with self.assertRaises(ValueError):
                        parser([{**entry, selector: {'test_explicit_score': inputs}}], 'components')
            bound = user_executor._content_embedding_generation_function_from_config({'test_scaled': {'scale': 2}})
            self.assertEqual(bound([0.5, 1]), [1, 2])
            runner = AnalyticsFunctionRunner({'recurring_functions': {'test_scaled': {'multiplier': 2}},
                                             'final_functions': {'test_scaled': {'multiplier': 3}}})
            metrics = SimpleNamespace(action_count=7)
            self.assertEqual(runner.run_recurring(metrics), {'test_scaled': 14})
            self.assertEqual(runner.run_final(metrics), {'test_scaled': 21})
        self.assertEqual(user_executor._content_embedding_generation_function_from_config({'dummy_content_embedding': {}})([]), [1.0])


if __name__ == '__main__':
    unittest.main()
