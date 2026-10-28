# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Unit tests for simulator configuration contract checks."""

import copy
import sys
import unittest
from pathlib import Path


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from config_helper import (  # noqa: E402
    CONFIG,
    _validate_required_config,
    _validate_user_types_config,
)
from db.core import MAX_INTEGER_USER_ID  # noqa: E402


class ConfigContractTests(unittest.TestCase):
    """Validate required config fields and current setup/recsys contracts."""

    def test_default_config_is_valid(self):
        """The shipped config should satisfy the schema without pinning defaults."""
        _validate_required_config(CONFIG.config)

    def test_vector_profile_generator_is_required(self):
        """User creation must name a vector-profile generator explicitly."""
        config = copy.deepcopy(CONFIG.config)
        del config["user_behavior"]["vector_profile_generation_function"]

        with self.assertRaisesRegex(
            ValueError, "vector_profile_generation_function"
        ):
            _validate_required_config(config)

    def test_unknown_vector_profile_generator_is_rejected(self):
        """Vector-profile selectors must resolve through the setup registry."""
        config = copy.deepcopy(CONFIG.config)
        config["user_behavior"]["vector_profile_generation_function"] = {
            "unknown_vector_profile": {}
        }

        with self.assertRaisesRegex(
            ValueError, "vector_profile_generation_function"
        ):
            _validate_required_config(config)

    def test_default_recency_applies_only_to_recsys(self):
        """RecSys ranks by recency while user behavior omits it by default."""
        recsys_components = CONFIG.config["recsys"]["feed_weight_components"]
        behavior_components = CONFIG.config["user_behavior"]["feed_weight_components"]
        self.assertTrue(
            any("recency_component_function" in component for component in recsys_components)
        )
        self.assertFalse(
            any("recency_component_function" in component for component in behavior_components)
        )

    def test_missing_nested_config_field_raises(self):
        """Missing config fields should fail before runtime use."""
        config = copy.deepcopy(CONFIG.config)
        del config["setup_db"]["user_types"]

        with self.assertRaisesRegex(
            ValueError,
            "Missing required config field: setup_db.user_types",
        ):
            _validate_required_config(config)

    def test_invalid_database_dsn_raises(self):
        """Default-run database connection config should fail early if empty."""
        config = copy.deepcopy(CONFIG.config)
        config["database"]["dsn"] = None

        with self.assertRaisesRegex(ValueError, "database.dsn"):
            _validate_required_config(config)

    def test_invalid_user_type_num_nodes_raises(self):
        """User type node counts should be non-negative integers."""
        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["normal"]["num_nodes"] = -1

        with self.assertRaisesRegex(ValueError, "num_nodes"):
            _validate_required_config(config)

    def test_one_billion_users_is_within_integer_user_id_limit(self):
        """A one-billion-user population remains valid for INTEGER IDs."""
        user_types = copy.deepcopy(CONFIG.config["setup_db"]["user_types"])
        for type_config in user_types.values():
            type_config["num_nodes"] = 0
        user_types["normal"]["num_nodes"] = 1_000_000_000

        _validate_user_types_config(user_types, "synthetic")

    def test_user_population_above_integer_limit_is_rejected(self):
        """Configured users may not exceed the PostgreSQL INTEGER maximum."""
        user_types = copy.deepcopy(CONFIG.config["setup_db"]["user_types"])
        for type_config in user_types.values():
            type_config["num_nodes"] = 0
        user_types["normal"]["num_nodes"] = MAX_INTEGER_USER_ID + 1

        with self.assertRaisesRegex(ValueError, "INTEGER user ID limit"):
            _validate_user_types_config(user_types, "synthetic")

    def test_invalid_network_source_raises(self):
        """Network source should be one of the supported setup paths."""
        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["network_source"] = "archive"

        with self.assertRaisesRegex(ValueError, "network_source"):
            _validate_required_config(config)

    def test_file_network_source_can_omit_edge_generation(self):
        """Imported networks should not require unused edge-generation configs."""
        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["network_source"] = "file"
        for type_config in config["setup_db"]["user_types"].values():
            del type_config["edge_generation"]

        _validate_required_config(config)

    def test_missing_cross_type_networking_function_raises(self):
        """Synthetic edge generation should name one cross-type wrapper."""
        config = copy.deepcopy(CONFIG.config)
        del config["setup_db"]["user_types"]["normal"]["edge_generation"][
            "cross_type_networking_function"
        ]

        with self.assertRaisesRegex(ValueError, "cross_type_networking_function"):
            _validate_required_config(config)

    def test_invalid_edge_function_raises(self):
        """Edge generation wrappers should be validated by registry name."""
        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["normal"]["edge_generation"][
            "within_type_networking_function"
        ] = {"unknown": {}}

        with self.assertRaisesRegex(ValueError, "within_type_networking_function"):
            _validate_required_config(config)

    def test_invalid_activity_function_raises(self):
        """Activity level distribution should use the configured wrapper registry."""
        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["normal"]["activity_level_distribution"][
            "activity_level_distribution_function"
        ] = "normal"

        with self.assertRaisesRegex(ValueError, "activity_level_distribution_function"):
            _validate_required_config(config)

    def test_invalid_distribution_parameters_raise(self):
        """Active user quality parameters should be validated before setup."""
        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["normal"]["user_quality_distribution"]["w"] = 1.1

        with self.assertRaisesRegex(ValueError, "user_quality_distribution.w"):
            _validate_required_config(config)

    def test_user_type_lurker_ratio_is_required_and_validated(self):
        """Lurker ratios should be configured per user type."""
        config = copy.deepcopy(CONFIG.config)
        del config["setup_db"]["user_types"]["normal"]["lurker_ratio"]

        with self.assertRaisesRegex(ValueError, "user_types.normal.lurker_ratio"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["normal"]["lurker_ratio"] = 1.01

        with self.assertRaisesRegex(ValueError, "user_types.normal.lurker_ratio"):
            _validate_required_config(config)

    def test_zero_node_user_types_are_inactive_templates(self):
        """Inactive user type templates may omit runnable setup fields."""
        config = copy.deepcopy(CONFIG.config)
        bad_config = config["setup_db"]["user_types"]["bad"]
        bad_config["num_nodes"] = 0
        del bad_config["edge_generation"]
        del bad_config["activity_level_distribution"]
        del bad_config["user_quality_distribution"]
        del bad_config["post_quality_distribution_function"]
        del bad_config["post_appeal_distribution_function"]

        _validate_required_config(config)

    def test_post_labeling_functions_are_optional(self):
        """A user type may omit post labelers when it generates no labels."""
        config = copy.deepcopy(CONFIG.config)
        for type_config in config["setup_db"]["user_types"].values():
            type_config.pop("post_labeling_functions", None)

        _validate_required_config(config)

    def test_post_labeling_functions_require_registered_selectors(self):
        """Configured post labeler mappings must resolve through the registry."""
        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["normal"]["post_labeling_functions"] = {"unknown_post_labeler": {}}

        with self.assertRaisesRegex(ValueError, "post_labeling_functions"):
            _validate_required_config(config)

    def test_post_labeling_functions_reject_unknown_names(self):
        """Post labeler configuration should reject scalar selector values."""
        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["normal"]["post_labeling_functions"] = (
            "unknown_post_labeler"
        )

        with self.assertRaisesRegex(ValueError, "post_labeling_functions"):
            _validate_required_config(config)

    def test_post_labeling_functions_do_not_accept_null(self):
        """Explicit null is different from omitting an optional selector list."""
        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["normal"]["post_labeling_functions"] = None

        with self.assertRaisesRegex(ValueError, "post_labeling_functions"):
            _validate_required_config(config)

    def test_zero_node_user_type_distributions_must_be_valid_when_present(self):
        """Inactive templates should still reject malformed distributions."""
        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["bad"]["user_quality_distribution"]["w"] = 1.1

        with self.assertRaisesRegex(ValueError, "user_quality_distribution.w"):
            _validate_required_config(config)

    def test_inventory_composition_requires_ratio_and_multiplier(self):
        config = copy.deepcopy(CONFIG.config)
        config["recsys"]["inventory_composition"]["out_of_network_ratio"] = -0.1

        with self.assertRaisesRegex(ValueError, "out_of_network_ratio"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["recsys"]["inventory_composition"]["out_of_network_ratio"] = 1.1

        with self.assertRaisesRegex(ValueError, "out_of_network_ratio"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["recsys"]["inventory_composition"]["size_multiplier"] = 0

        with self.assertRaisesRegex(ValueError, "size_multiplier"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        del config["recsys"]["inventory_composition"]["size_multiplier"]

        with self.assertRaisesRegex(
            ValueError,
            "Missing required config field: recsys.inventory_composition.size_multiplier",
        ):
            _validate_required_config(config)

    def test_weight_components_require_known_function_names_and_importance(self):
        """Feed and user-side component lists should be explicit."""
        config = copy.deepcopy(CONFIG.config)
        self._component(config, "recsys", "appeal_component_function")[
            "appeal_component_function"
        ] = {"quality": {}}

        with self.assertRaisesRegex(ValueError, "feed_weight_components"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        del self._component(
            config,
            "user_behavior",
            "appeal_component_function",
        )["importance_in"]

        with self.assertRaisesRegex(ValueError, "user_behavior.feed_weight_components"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["user_behavior"]["feed_weight_components"] = []

        with self.assertRaisesRegex(ValueError, "user_behavior.feed_weight_components"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["recsys"]["feed_weight_components"][0] = {
            "appeal_component_function": {"post_appeal": {}},
            "k": 1.0,
        }

        with self.assertRaisesRegex(ValueError, "importance_in"):
            _validate_required_config(config)

    @staticmethod
    def _component(config, section, function_key):
        """Find a component by selector field, independent of list order."""
        return next(
            item
            for item in config[section]["feed_weight_components"]
            if function_key in item
        )

    def test_engagement_components_require_valid_function_names(self):
        """Engagement components should name supported global functions."""
        config = copy.deepcopy(CONFIG.config)
        self._component(config, "recsys", "engagement_component_function")[
            "engagement_component_function"
        ] = {"friends_like_count": {}}

        with self.assertRaisesRegex(ValueError, "engagement_component_function"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["recsys"][
            "engagement_action_lookback_population_multiplier"
        ] = 0

        with self.assertRaisesRegex(ValueError, "lookback_population_multiplier"):
            _validate_required_config(config)

    def test_content_embedding_generation_function_must_be_registered(self):
        """Content embedding generation should use a configured function label."""
        config = copy.deepcopy(CONFIG.config)
        config["user_behavior"]["content_embedding_generation_function"] = {"random_text": {}}

        with self.assertRaisesRegex(
            ValueError,
            "user_behavior.content_embedding_generation_function",
        ):
            _validate_required_config(config)

    def test_unregistered_generator_function_names_are_not_accepted(self):
        """Generator selectors must match the current function registry."""
        config = copy.deepcopy(CONFIG.config)
        config["user_behavior"]["content_embedding_generation_function"][
            "unregistered_content_embedding"
        ] = config["user_behavior"][
            "content_embedding_generation_function"
        ].pop("social_media_archive_content_embedding")
        with self.assertRaisesRegex(
            ValueError, "user_behavior.content_embedding_generation_function"
        ):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["user_behavior"]["vector_profile_generation_function"][
            "unregistered_profile_generator"
        ] = config["user_behavior"][
            "vector_profile_generation_function"
        ].pop("pca_7param_social_media_archive_profile")
        with self.assertRaisesRegex(
            ValueError, "vector_profile_generation_function"
        ):
            _validate_required_config(config)

    def test_termination_criteria_are_validated(self):
        """Termination config should validate one function-input mapping."""
        config = copy.deepcopy(CONFIG.config)
        config["termination"]["criteria_functions"] = {"wall_clock": {}}

        with self.assertRaisesRegex(ValueError, "termination.criteria_functions"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["termination"]["criteria_functions"] = {"stable_users": {}}

        with self.assertRaisesRegex(ValueError, "termination.criteria_functions"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["termination"]["criteria_functions"] = {}

        with self.assertRaisesRegex(ValueError, "at least one"):
            _validate_required_config(config)

    def test_process_control_flags_must_be_booleans(self):
        """Boolean process/config switches should reject string or numeric stand-ins."""
        config = copy.deepcopy(CONFIG.config)
        config["policy"]["enabled"] = "true"

        with self.assertRaisesRegex(ValueError, "policy.enabled"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["controller"]["dump_on_termination"] = "yes"

        with self.assertRaisesRegex(ValueError, "controller.dump_on_termination"):
            _validate_required_config(config)

    def test_final_analytics_timeout_is_optional_and_positive(self):
        """Custom shutdown deadlines must be finite and greater than zero."""
        config = copy.deepcopy(CONFIG.config)
        del config["controller"]["final_analytics_timeout_seconds"]
        _validate_required_config(config)

        for value in (0, -1, True, float("inf"), float("nan"), "60"):
            with self.subTest(value=value):
                config["controller"]["final_analytics_timeout_seconds"] = value
                with self.assertRaisesRegex(
                    ValueError, "controller.final_analytics_timeout_seconds"
                ):
                    _validate_required_config(config)

        config["controller"]["final_analytics_timeout_seconds"] = 1.5
        _validate_required_config(config)

    def test_mpi_num_procs_must_be_positive(self):
        """MPI process count should be an explicit positive integer."""
        config = copy.deepcopy(CONFIG.config)
        config["mpi"]["num_procs"] = None

        with self.assertRaisesRegex(ValueError, "mpi.num_procs"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["mpi"]["num_procs"] = 0

        with self.assertRaisesRegex(ValueError, "mpi.num_procs"):
            _validate_required_config(config)

    def test_mpi_ranks_must_be_non_negative_integers(self):
        """MPI role ranks should not be coerced from invalid config values."""
        config = copy.deepcopy(CONFIG.config)
        config["mpi"]["processes"]["controller"]["rank"] = "0"

        with self.assertRaisesRegex(ValueError, "mpi.processes.controller.rank"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["mpi"]["processes"]["worker"]["start_rank"] = -1

        with self.assertRaisesRegex(ValueError, "mpi.processes.worker.start_rank"):
            _validate_required_config(config)

    def test_process_backoff_requires_finite_nonnegative_milliseconds(self):
        """Both controls allow zero/fractional milliseconds and reject invalid inputs."""
        for section, key in [
            ("worker", "idle_sleep_milliseconds"),
            ("policy", "poll_interval_milliseconds"),
        ]:
            for value in [0, 7.5, 250]:
                with self.subTest(section=section, value=value):
                    config = copy.deepcopy(CONFIG.config)
                    config[section][key] = value
                    _validate_required_config(config)
            for value in [-1, True, "7", None, float("inf"), float("nan")]:
                with self.subTest(section=section, value=value):
                    config = copy.deepcopy(CONFIG.config)
                    config[section][key] = value
                    with self.assertRaisesRegex(ValueError, f"{section}.{key}"):
                        _validate_required_config(config)

    def test_worker_legacy_seconds_key_does_not_satisfy_required_input(self):
        """Old configs must choose milliseconds explicitly, avoiding a unit mix-up."""
        config = copy.deepcopy(CONFIG.config)
        del config["worker"]["idle_sleep_milliseconds"]
        config["worker"]["idle_sleep_seconds"] = 0.25
        with self.assertRaisesRegex(ValueError, "worker.idle_sleep_milliseconds"):
            _validate_required_config(config)

    def test_policy_and_analytics_runtime_numbers_are_validated(self):
        """Process polling and policy settings should fail during config load."""
        config = copy.deepcopy(CONFIG.config)
        config["policy"]["poll_interval_milliseconds"] = -1

        with self.assertRaisesRegex(ValueError, "policy.poll_interval_milliseconds"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["policy"]["rule_functions"] = {"unknown_policy_rule": {}}

        with self.assertRaisesRegex(ValueError, "policy.rule_functions"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["policy"]["rule_functions"] = {"below_quality_threshold": {}}

        with self.assertRaisesRegex(ValueError, "policy.rule_functions"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["analytics"]["evaluation_interval"]["value"] = 0

        with self.assertRaisesRegex(ValueError, "analytics.evaluation_interval.value"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["analytics"]["evaluation_interval"]["unit"] = "wall_clock_seconds"

        with self.assertRaisesRegex(ValueError, "analytics.evaluation_interval.unit"):
            _validate_required_config(config)

    def test_analytics_output_groups_and_paths_are_required(self):
        """Analytics output groups and file paths are required config fields."""
        config = copy.deepcopy(CONFIG.config)
        del config["analytics"]["recurring_functions"]

        with self.assertRaisesRegex(
            ValueError,
            "Missing required config field: analytics.recurring_functions",
        ):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["analytics"]["final_functions"] = {"does_not_exist": {}}
        with self.assertRaisesRegex(ValueError, "analytics.final_functions"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["analytics"]["analytics_output_file"] = ""
        with self.assertRaisesRegex(ValueError, "analytics.analytics_output_file"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        del config["termination"]["termination_metrics_file"]
        with self.assertRaisesRegex(
            ValueError,
            "Missing required config field: termination.termination_metrics_file",
        ):
            _validate_required_config(config)

    def test_missing_user_action_settings_raise(self):
        """User action settings should be required under user_behavior."""
        config = copy.deepcopy(CONFIG.config)
        del config["user_behavior"]["p_actions"]

        with self.assertRaisesRegex(
            ValueError,
            "Missing required config field: user_behavior.p_actions",
        ):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        del config["user_behavior"]["p_actions"]["quote"]

        with self.assertRaisesRegex(ValueError, "user_behavior.p_actions.quote"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["user_behavior"]["p_actions"]["post"] = [1.0]

        with self.assertRaisesRegex(ValueError, "user_behavior.p_actions.post"):
            _validate_required_config(config)

    def test_user_type_generation_functions_are_required_for_active_types(self):
        """Active user types should configure content-generation wrappers."""
        config = copy.deepcopy(CONFIG.config)
        del config["setup_db"]["user_types"]["normal"]["post_quality_distribution_function"]

        with self.assertRaisesRegex(ValueError, "post_quality_distribution_function"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        del config["setup_db"]["user_types"]["normal"]["post_appeal_distribution_function"]

        with self.assertRaisesRegex(ValueError, "post_appeal_distribution_function"):
            _validate_required_config(config)

    def test_user_type_generation_functions_are_validated(self):
        """Quality and appeal generation should use named per-type wrappers."""
        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["normal"][
            "post_quality_distribution_function"
        ] = {"normal": {}}

        with self.assertRaisesRegex(ValueError, "post_quality_distribution_function"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["normal"][
            "post_appeal_distribution_function"
        ] = {"normal": {}}

        with self.assertRaisesRegex(ValueError, "post_appeal_distribution_function"):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["setup_db"]["user_types"]["normal"][
            "post_quality_distribution_function"
        ] = {"two_cluster_inverse": {}}

        with self.assertRaisesRegex(ValueError, "post_quality_distribution_function"):
            _validate_required_config(config)

    def test_actions_per_session_ratio_is_required_and_bounded(self):
        """The fixed active-session action ratio must be explicit and bounded."""
        config = copy.deepcopy(CONFIG.config)
        del config["user_behavior"]["actions_per_session_ratio"]

        with self.assertRaisesRegex(
            ValueError,
            "Missing required config field: user_behavior.actions_per_session_ratio",
        ):
            _validate_required_config(config)

        config = copy.deepcopy(CONFIG.config)
        config["user_behavior"]["actions_per_session_ratio"] = 1.1

        with self.assertRaisesRegex(ValueError, "actions_per_session_ratio"):
            _validate_required_config(config)


    def test_active_budget_cannot_round_to_zero(self):
        for feed_size, ratio in ((15, 0), (20, 0.01), (1, 0.5), (0, 1)):
            with self.subTest(feed_size=feed_size, ratio=ratio):
                config = copy.deepcopy(CONFIG.config)
                config["recsys"]["feed_size"] = feed_size
                config["user_behavior"]["actions_per_session_ratio"] = ratio
                with self.assertRaisesRegex(ValueError, "at least one action"):
                    _validate_required_config(config)


if __name__ == "__main__":
    unittest.main()
