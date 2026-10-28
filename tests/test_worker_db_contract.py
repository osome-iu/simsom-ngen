# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Unit tests for worker-owned database contracts."""

import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from db.core import DatabaseSettings, UserRecord  # noqa: E402
from db.worker import (  # noqa: E402
    ActionViewRecord,
    ActionWriteRecord,
    APPEAL_PROPERTY,
    BatchInNetworkInventory,
    ClaimedUserSessionRecord,
    POST_DISTRIBUTION_FUNCTIONS,
    QUALITY_PROPERTY,
    PostgresWorkerDatabase,
    create_worker_database_client,
    gaussian_around_user_quality,
    post_distribution_function,
    sample_generated_property,
)
from subcomponents.post_labeling import (
    PostLabelingContext,
    post_labeling_function,
)
from subcomponents.user_executor import UserAction  # noqa: E402


def content_generation_behavior(
    *,
    quality=0.5,
):
    """Return user behavior with content property generation settings."""
    return {
        "quality": quality,
    }


def configured_post_distribution_selector():
    """Return the test-owned post-property selector."""
    return {TEST_POST_DISTRIBUTION_SELECTOR: {}}


TEST_POST_DISTRIBUTION_SELECTOR = "test_post_distribution"
TEST_POST_LABELING_SELECTOR = "test_post_labeling"


@post_distribution_function(TEST_POST_DISTRIBUTION_SELECTOR)
def test_post_distribution(behavior, spec):
    """Generate a test property through the shared Gaussian implementation."""
    return gaussian_around_user_quality(behavior, spec, stddev=0.12)


@post_labeling_function(TEST_POST_LABELING_SELECTOR)
def test_post_labeling(context: PostLabelingContext):
    """Return a deterministic test label from the creator's user type."""
    return {"test": context.user_record.user_type}


def content_generation_user_types_config():
    """Return a minimal explicit per-user-type content-generation config."""
    return {
        "normal": {
            "num_nodes": 1,
            "post_quality_distribution_function": configured_post_distribution_selector(),
            "post_appeal_distribution_function": configured_post_distribution_selector(),
        }
    }


def recsys_config():
    """Return the required recsys settings used by worker DB tests."""
    return SimpleNamespace(engagement_action_lookback_population_multiplier=10)


class RecordingWorkerDatabase(PostgresWorkerDatabase):
    """Worker DB test double that records SQL and returns queued outputs."""

    def __init__(
        self,
        outputs=None,
        *,
        action_insert_chunk_size=1000,
        user_types_config=None,
        recsys_config_value=None,
    ):
        """Initialize with optional captured-query outputs."""
        super().__init__(
            DatabaseSettings(schema="unit_test", clock_extension_name="simsom_clock"),
            action_insert_chunk_size=action_insert_chunk_size,
            user_types_config=(
                user_types_config
                if user_types_config is not None
                else content_generation_user_types_config()
            ),
            recsys_config=recsys_config_value or recsys_config(),
        )
        self.calls = []
        self.outputs = list(outputs or [])

    def run_psql(self, sql: str, *, capture_output: bool = False):
        """Record SQL and return the next queued output for captured reads."""
        self.calls.append((sql, capture_output))
        if capture_output:
            if self.outputs:
                return self.outputs.pop(0)
            return '{"should_terminate": false, "reason": null}'
        return None


class WorkerDbContractTests(unittest.TestCase):
    """Validate worker DB reads, writes, lifecycle, and type enforcement."""

    def test_action_write_record_exposes_shared_moderation_action_fields(self):
        """Action writes should expose ERD-aligned moderation_actions."""
        record = ActionWriteRecord(
            session_id="session_1",
            action=UserAction.post(
                creator_user_id=1,
                content_embedding=[0.1],
                moderation_actions=[
                    {
                        "intervention_type": "queued",
                        "violation_type": "test_fixture",
                        "rule": "test_fixture",
                    }
                ],
            ),
        )

        self.assertEqual(
            record.action.moderation_actions,
            [
                {
                    "intervention_type": "queued",
                    "violation_type": "test_fixture",
                    "rule": "test_fixture",
                }
            ],
        )

    def test_worker_lifecycle_uses_shared_core_contract(self):
        """Worker DB should read lifecycle state through the shared core table."""
        database = RecordingWorkerDatabase()

        self.assertFalse(database.should_terminate())

        self.assertTrue(database.calls[0][1])

    def test_claim_user_batch_rejects_invalid_batch_size(self):
        """claim_user_batch should reject non-integer batch sizes."""
        database = RecordingWorkerDatabase()

        with self.assertRaises(TypeError):
            database.claim_user_batch(
                "3", lurker_ratio=0.0, planned_action_count=1
            )

        self.assertEqual(database.calls, [])

    def test_claim_user_batch_rejects_invalid_lurker_ratio(self):
        """claim_user_batch should reject invalid lurker ratios before SQL reads."""
        database = RecordingWorkerDatabase()

        with self.assertRaisesRegex(ValueError, "lurker_ratio"):
            database.claim_user_batch(
                3, lurker_ratio=1.0, planned_action_count=1
            )

        self.assertEqual(database.calls, [])

    def test_claim_user_batch_maps_active_and_zero_activity_rows(self):
        """claim_user_batch should map active users plus added zero-activity users."""
        database = RecordingWorkerDatabase(
            outputs=[
                """
                [
                    {
                        "user_id": 1,
                        "vector_profile": [0.1],
                        "activity_level": 1.0,
                        "user_type": "normal",
                        "user_behavior": {"p_actions": {"post": 1.0}},
                        "moderation_actions": null,
                        "other_properties": {}
                    }
                ]
                """,
                """
                [
                    {
                        "user_id": 2,
                        "vector_profile": [],
                        "activity_level": 0.0,
                        "user_type": "bad",
                        "user_behavior": {"p_actions": {"post": 1.0}},
                        "moderation_actions": null,
                        "other_properties": {}
                    }
                ]
                """
            ]
        )

        claimed = database.claim_user_batch(
            3, lurker_ratio=0.5, planned_action_count=1
        )

        self.assertEqual(len(claimed), 2)
        self.assertEqual(claimed[0].user_id, 1)
        self.assertIsInstance(claimed[0].session_id, str)
        self.assertEqual(claimed[0].planned_action_count, 1)
        self.assertEqual(claimed[0].user_record.activity_level, 1.0)
        self.assertEqual(claimed[0].user_record.user_type, "normal")
        self.assertIsNone(claimed[0].user_record.moderation_actions)
        self.assertEqual(claimed[1].user_id, 2)
        self.assertEqual(claimed[1].planned_action_count, 0)
        self.assertEqual(claimed[1].user_record.activity_level, 0.0)
        self.assertEqual(claimed[1].user_record.user_type, "bad")
        self.assertFalse(hasattr(claimed[0], "lurker"))

    def test_planned_zero_activity_count_uses_target_ratio_formula(self):
        """Runtime zero-activity additions should be derived from NZ * L / (1 - L)."""
        self.assertEqual(PostgresWorkerDatabase._planned_zero_activity_count(4, 0.2), 1)
        self.assertEqual(PostgresWorkerDatabase._planned_zero_activity_count(0, 0.5), 0)
        self.assertEqual(PostgresWorkerDatabase._planned_zero_activity_count(4, 0.0), 0)
        with self.assertRaisesRegex(ValueError, "lurker_ratio"):
            PostgresWorkerDatabase._planned_zero_activity_count(4, 1.0)

    def test_claim_user_batch_rejects_non_object_query_rows(self):
        """claim_user_batch should reject malformed JSON row payloads."""
        database = RecordingWorkerDatabase(outputs=["[1]"])

        with self.assertRaises(ValueError):
            database.claim_user_batch(
                1, lurker_ratio=0.0, planned_action_count=1
            )

    def test_fetch_out_network_feed_inventory_reads_global_inventory(self):
        """Out-network inventory should read a global recent-action pool."""
        database = RecordingWorkerDatabase(
            outputs=[
                """
                [
                    {
                        "action_id": 43,
                        "creator_user_id": 9,
                        "timestamp": 98,
                        "action_type": "post",
                        "parent_action_id": null,
                        "content_embedding": [],
                        "moderation_actions": null,
                        "repeated_like_n_reshare_creator_count": 0,
                        "views_by_others_count": 1,
                        "other_properties": {
                            "source_action_id": 99,
                            "source_action_type": "repost"
                        }
                    }
                ]
                """
            ]
        )

        inventory = database.fetch_out_network_feed_inventory(
            limit=11,
        )

        self.assertEqual(inventory[0].action_id, 43)

    def test_fetch_in_network_feed_inventory_reads_followed_creator_inventory(self):
        """In-network inventory should read recent actions from followed users."""
        database = RecordingWorkerDatabase(
            outputs=[
                """
                [
                    {
                        "action_id": 42,
                        "creator_user_id": 2,
                        "timestamp": 99,
                        "action_type": "post",
                        "parent_action_id": null,
                        "content_embedding": [0.2],
                        "moderation_actions": null,
                        "repeated_like_n_reshare_creator_count": 2,
                        "views_by_others_count": 5,
                        "other_properties": {
                            "source": "test",
                            "source_action_id": 42
                        },
                        "visible_to_user_ids": [1]
                    }
                ]
                """
            ]
        )
        claimed = ClaimedUserSessionRecord(
            user_id=1,
            session_id="session_1",
            user_record=UserRecord(user_id=1, user_type="normal"),
        )

        inventory = database.fetch_in_network_feed_inventory(
            [claimed],
            limit=7,
        )

        self.assertIsInstance(inventory, BatchInNetworkInventory)
        self.assertEqual(inventory.actions[0].action_id, 42)
        self.assertEqual(inventory.for_user(1)[0].action_id, 42)
        self.assertIsNone(inventory.actions[0].moderation_actions)

    def test_fetch_in_network_feed_inventory_rejects_invalid_claim(self):
        """In-network inventory should require one claimed session record."""
        database = RecordingWorkerDatabase()

        with self.assertRaises(TypeError):
            database.fetch_in_network_feed_inventory(
                object(),
                limit=7,
            )

        self.assertEqual(database.calls, [])

    def test_content_actions_receive_generated_quality_and_appeal(self):
        """Content action enrichment should overwrite stale content metrics."""
        database = RecordingWorkerDatabase(
            user_types_config=content_generation_user_types_config(),
        )
        other_properties = {
            "source": "unit",
            "quality": -1,
            "appeal": -1,
        }
        record = ActionWriteRecord(
            session_id="session_1",
            action=UserAction.post(
                creator_user_id=1,
                content_embedding=[0.1, 0.2],
            ),
            user_record=UserRecord(
                user_id=1,
                user_type="normal",
                user_behavior=content_generation_behavior(),
            ),
        )

        with patch("db.worker.random.gauss", side_effect=[0.8, 0.6]):
            database._add_quality_and_appeal(other_properties, record)

        self.assertEqual(other_properties["source"], "unit")
        self.assertEqual(other_properties["quality"], 0.8)
        self.assertEqual(other_properties["appeal"], 0.6)

    def test_content_generation_requires_user_type_generation_config(self):
        """Content actions should not fall back to code-selected generators."""
        database = RecordingWorkerDatabase(
            user_types_config={
                "other": {
                    "num_nodes": 1,
                    "post_quality_distribution_function": configured_post_distribution_selector(),
                    "post_appeal_distribution_function": configured_post_distribution_selector(),
                }
            },
        )
        record = ActionWriteRecord(
            session_id="session_1",
            action=UserAction.post(
                creator_user_id=1,
                content_embedding=[0.1],
            ),
            user_record=UserRecord(
                user_id=1,
                user_type="normal",
                user_behavior=content_generation_behavior(),
            ),
        )

        with self.assertRaisesRegex(ValueError, "user_type: normal"):
            database._add_quality_and_appeal({}, record)

    def test_generated_property_uses_gaussian_wrapper_and_clamps(self):
        """Gaussian property wrappers should clamp generated values to [0, 1]."""
        behavior = content_generation_behavior(quality=0.4)

        with patch("db.worker.random.gauss", return_value=1.4) as gauss:
            value = sample_generated_property(
                behavior,
                spec=QUALITY_PROPERTY,
                generation_function_name=configured_post_distribution_selector(),
            )

        self.assertEqual(value, 1.0)
        gauss.assert_called_once()
        self.assertEqual(gauss.call_args.args[0], 0.4)

    def test_registered_post_distribution_functions_are_callable(self):
        """Every registered post-property selector should resolve to a callable."""
        self.assertTrue(POST_DISTRIBUTION_FUNCTIONS)
        self.assertTrue(
            all(callable(function) for function in POST_DISTRIBUTION_FUNCTIONS.values())
        )

    def test_generated_appeal_uses_user_quality_as_gaussian_center(self):
        """Post appeal should be generated from the same user-quality anchor."""
        behavior = content_generation_behavior(quality=0.7)

        with patch("db.worker.random.gauss", return_value=0.6) as gauss:
            value = sample_generated_property(
                behavior,
                spec=APPEAL_PROPERTY,
                generation_function_name=configured_post_distribution_selector(),
            )

        self.assertEqual(value, 0.6)
        gauss.assert_called_once()
        self.assertEqual(gauss.call_args.args[0], 0.7)

    def test_generated_property_requires_user_value_for_gaussian(self):
        """Gaussian content property generation should require user scalar values."""
        behavior = content_generation_behavior()
        del behavior["quality"]

        with self.assertRaisesRegex(ValueError, "user_behavior.quality"):
            sample_generated_property(
                behavior,
                spec=QUALITY_PROPERTY,
                generation_function_name=configured_post_distribution_selector(),
            )

        behavior = content_generation_behavior(quality="bad")
        with self.assertRaisesRegex(TypeError, "user_behavior.quality"):
            sample_generated_property(
                behavior,
                spec=QUALITY_PROPERTY,
                generation_function_name=configured_post_distribution_selector(),
            )

    def test_write_actions_skips_quality_and_appeal_for_non_content_actions(self):
        """Only actions that generate new content should get fresh metrics."""
        database = RecordingWorkerDatabase()
        other_properties = {}
        record = ActionWriteRecord(
            session_id="session_1",
            action=UserAction.like(
                creator_user_id=1,
                parent_action_id=42,
            ),
            user_record=UserRecord(
                user_id=1,
                user_type="normal",
                user_behavior=content_generation_behavior(),
            ),
        )

        database._add_quality_and_appeal(other_properties, record)

        self.assertEqual(other_properties, {})

    def test_worker_sql_uses_integer_user_ids_and_bigint_action_ids(self):
        """Worker SQL keeps user references narrow and action references wide."""
        database = RecordingWorkerDatabase()
        action = UserAction.like(creator_user_id=1, parent_action_id=42)

        insert_values = database._action_insert_values(
            ActionWriteRecord(session_id="session_1", action=action),
            input_index=0,
        )
        self.assertIn("0, 1,", insert_values)
        self.assertIn(", 42,", insert_values)

        database.write_actions([ActionWriteRecord(session_id="session_1", action=action)])
        action_sql = database.calls[0][0]
        self.assertIn("new_action.creator_user_id::INTEGER", action_sql)
        self.assertIn("new_action.parent_action_id::BIGINT", action_sql)

        database.write_action_views(
            [ActionViewRecord(session_id="session_1", viewer_user_id=1, action_id=42)]
        )
        view_sql = database.calls[-1][0]
        self.assertIn("(42, 1)", view_sql)
        self.assertIn("COUNT(*)::BIGINT", view_sql)

    def test_write_actions_enriches_reply_with_content(self):
        """Replies with embeddings should receive fresh quality and appeal."""
        database = RecordingWorkerDatabase(
            user_types_config=content_generation_user_types_config(),
        )
        user_record = UserRecord(
            user_id=1,
            user_type="normal",
            user_behavior=content_generation_behavior(),
        )

        other_properties = {}
        with patch("db.worker.random.gauss", side_effect=[0.8, 0.6]):
            database._add_quality_and_appeal(
                other_properties,
                ActionWriteRecord(
                    session_id="session_1",
                    action=UserAction.reply(
                        creator_user_id=1,
                        parent_action_id=42,
                        content_embedding=[0.3],
                    ),
                    user_record=user_record,
                ),
            )

        self.assertEqual(other_properties, {"quality": 0.8, "appeal": 0.6})

    def test_content_actions_receive_configured_post_labels(self):
        """Configured labelers should write labels beside generated properties."""
        database = RecordingWorkerDatabase(
            user_types_config={
                "normal": {
                    "num_nodes": 1,
                    "post_quality_distribution_function": configured_post_distribution_selector(),
                    "post_appeal_distribution_function": configured_post_distribution_selector(),
                    "post_labeling_functions": {TEST_POST_LABELING_SELECTOR: {}},
                }
            },
        )
        user_record = UserRecord(
            user_id=1,
            user_type="normal",
            user_behavior=content_generation_behavior(),
        )
        other_properties = {}
        database._add_post_labels(
            other_properties,
            ActionWriteRecord(
                session_id="session_1",
                action=UserAction.post(
                    creator_user_id=1,
                    content_embedding=[0.3],
                ),
                user_record=user_record,
            ),
        )

        self.assertEqual(other_properties, {"post_labels": {"test": "normal"}})

    def test_non_content_actions_skip_post_labeling(self):
        """Like actions should inherit labels instead of running labelers."""
        database = RecordingWorkerDatabase(
            user_types_config={
                "normal": {
                    "num_nodes": 1,
                    "post_quality_distribution_function": configured_post_distribution_selector(),
                    "post_appeal_distribution_function": configured_post_distribution_selector(),
                    "post_labeling_functions": {TEST_POST_LABELING_SELECTOR: {}},
                }
            },
        )
        other_properties = {}
        database._add_post_labels(
            other_properties,
            ActionWriteRecord(
                session_id="session_1",
                action=UserAction.like(
                    creator_user_id=1,
                    parent_action_id=42,
                ),
                user_record=UserRecord(user_id=1, user_type="normal"),
            ),
        )
        self.assertEqual(other_properties, {})

    def test_reply_without_content_is_invalid(self):
        """Replies must carry content instead of becoming contentless actions."""
        with self.assertRaisesRegex(ValueError, "reply"):
            UserAction.reply(
                creator_user_id=1,
                parent_action_id=42,
            )

    def test_worker_database_factory_reads_action_insert_and_generation_config(self):
        """create_worker_database_client should read worker and generation config."""
        quality_function_name = configured_post_distribution_selector()
        appeal_function_name = configured_post_distribution_selector()
        lookback_multiplier = 10
        database = create_worker_database_client(
            SimpleNamespace(
                dsn="postgres",
                schema="unit_test",
                clock_extension=SimpleNamespace(
                    name="simsom_clock",
                ),
            ),
            SimpleNamespace(action_insert_chunk_size=7),
            {
                "normal": {
                    "num_nodes": 1,
                    "post_quality_distribution_function": quality_function_name,
                    "post_appeal_distribution_function": appeal_function_name,
                }
            },
            SimpleNamespace(
                engagement_action_lookback_population_multiplier=lookback_multiplier
            ),
        )

        self.assertEqual(database.action_insert_chunk_size, 7)
        self.assertEqual(
            database.engagement_action_lookback_population_multiplier,
            lookback_multiplier,
        )
        self.assertEqual(
            database._post_distribution_function_for_user_type(
                "normal",
                QUALITY_PROPERTY,
            ),
            quality_function_name,
        )
        self.assertEqual(
            database._post_distribution_function_for_user_type(
                "normal",
                APPEAL_PROPERTY,
            ),
            appeal_function_name,
        )

    def test_worker_database_rejects_invalid_generation_functions(self):
        """Worker DB should validate runtime generation selector names."""
        database_config = SimpleNamespace(
            dsn="postgres",
            schema="unit_test",
            clock_extension=SimpleNamespace(
                name="simsom_clock",
            ),
        )
        worker_config = SimpleNamespace(action_insert_chunk_size=7)

        with self.assertRaisesRegex(ValueError, "post_quality_distribution_function"):
            create_worker_database_client(
                database_config,
                worker_config,
                {
                    "normal": {
                        "num_nodes": 1,
                        "post_quality_distribution_function": {"normal": {}},
                        "post_appeal_distribution_function": configured_post_distribution_selector(),
                    }
                },
                recsys_config(),
            )

        with self.assertRaisesRegex(ValueError, "post_appeal_distribution_function"):
            create_worker_database_client(
                database_config,
                worker_config,
                {
                    "normal": {
                        "num_nodes": 1,
                        "post_quality_distribution_function": configured_post_distribution_selector(),
                        "post_appeal_distribution_function": {"normal": {}},
                    }
                },
                recsys_config(),
            )

    def test_worker_database_factory_requires_recsys_config(self):
        """Worker DB construction should require recsys lookback config."""
        with self.assertRaisesRegex(ValueError, "recsys"):
            create_worker_database_client(
                SimpleNamespace(
                    dsn="postgres",
                    schema="unit_test",
                    clock_extension=SimpleNamespace(
                        name="simsom_clock",
                    ),
                ),
                SimpleNamespace(action_insert_chunk_size=7),
                content_generation_user_types_config(),
            )

    def test_worker_database_factory_requires_user_types_config(self):
        """Factory construction should require setup user type config."""
        with self.assertRaisesRegex(ValueError, "setup_db.user_types"):
            create_worker_database_client(
                SimpleNamespace(
                    dsn="postgres",
                    schema="unit_test",
                    clock_extension=SimpleNamespace(
                        name="simsom_clock",
                    ),
                ),
                SimpleNamespace(action_insert_chunk_size=7),
                None,
                recsys_config(),
            )

        with self.assertRaisesRegex(ValueError, "non-empty object"):
            create_worker_database_client(
                SimpleNamespace(
                    dsn="postgres",
                    schema="unit_test",
                    clock_extension=SimpleNamespace(
                        name="simsom_clock",
                    ),
                ),
                SimpleNamespace(action_insert_chunk_size=7),
                {},
                recsys_config(),
            )

    def test_write_actions_noops_for_empty_batch(self):
        """write_actions should skip empty batches."""
        database = RecordingWorkerDatabase()

        database.write_actions([])

        self.assertEqual(database.calls, [])

    def test_write_action_views_increments_aggregate_view_counts(self):
        """write_action_views should aggregate view increments by action id."""
        database = RecordingWorkerDatabase()

        database.write_action_views(
            [
                ActionViewRecord(
                    session_id="session_1",
                    viewer_user_id=1,
                    action_id=42,
                ),
                ActionViewRecord(
                    session_id="session_2",
                    viewer_user_id=2,
                    action_id=42,
                ),
                ActionViewRecord(
                    session_id="session_3",
                    viewer_user_id=3,
                    action_id=43,
                ),
            ]
        )

        self.assertEqual(len(database.calls), 1)

    def test_write_action_views_noops_for_empty_batch(self):
        """write_action_views should skip empty batches."""
        database = RecordingWorkerDatabase()

        database.write_action_views([])

        self.assertEqual(database.calls, [])

    def test_write_action_views_rejects_invalid_records(self):
        """write_action_views should reject non-ActionViewRecord items."""
        database = RecordingWorkerDatabase()

        with self.assertRaises(TypeError):
            database.write_action_views([object()])

        self.assertEqual(database.calls, [])

    def test_write_actions_rejects_invalid_records(self):
        """write_actions should reject non-ActionWriteRecord items."""
        database = RecordingWorkerDatabase()

        with self.assertRaises(TypeError):
            database.write_actions([object()])

        self.assertEqual(database.calls, [])


if __name__ == "__main__":
    unittest.main()
