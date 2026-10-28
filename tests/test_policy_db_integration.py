# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Integration tests for policy DB behavior against Postgres."""

import os
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from db.controller import PostgresControllerDatabase, SetupDbDataset  # noqa: E402
from db.core import DatabaseSettings, UserRecord  # noqa: E402
from db.core import CLOCK_STATE_TABLE  # noqa: E402
from db.policy import (  # noqa: E402
    PolicyCursor,
    PolicyModerationAction,
    PostgresPolicyDatabase,
)
from subcomponents.policy_annotations import (  # noqa: E402
    REMOVED_INTERVENTION,
    intervention_entry,
)


class PolicyPostgresIntegrationTests(unittest.TestCase):
    """Exercise policy target reads and moderation writes with real JSONB rows."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.psql_bin = cls._resolve_binary(os.environ.get("SIMSOM_PSQL_BIN"), "psql")
        cls.createdb_bin = cls._resolve_binary(
            os.environ.get("SIMSOM_CREATEDB_BIN"),
            "createdb",
        )
        cls.dropdb_bin = cls._resolve_binary(
            os.environ.get("SIMSOM_DROPDB_BIN"),
            "dropdb",
        )
        cls.maintenance_db = os.environ.get("SIMSOM_MAINTENANCE_DB", "postgres")
        cls._check_server()

        cls.test_db = f"simsom_policy_test_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        subprocess.run([cls.createdb_bin, cls.test_db], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "test_db"):
            subprocess.run([cls.dropdb_bin, "--if-exists", cls.test_db], check=False)

    def setUp(self) -> None:
        self.schema = f"policy_test_{uuid.uuid4().hex[:8]}"
        self.settings = DatabaseSettings(
            dsn=self.test_db,
            schema=self.schema,
            clock_extension_name="simsom_clock",
        )
        self.controller = PostgresControllerDatabase(self.settings)
        self.policy_db = PostgresPolicyDatabase(self.settings)
        self.controller.setup_schema()
        self.controller.populate_initial_state(
            SetupDbDataset(
                users=[
                    UserRecord(
                        user_id=1,
                        user_type="normal",
                        user_behavior={"quality": 0.7},
                    )
                ],
                follower_network=[],
            )
        )

    def _set_clock_tick(self, tick_ms: int) -> None:
        clock_table = self.policy_db.schema_name(CLOCK_STATE_TABLE)
        self.policy_db.execute_sql(
            f"UPDATE {clock_table} SET current_tick_ms = {tick_ms} WHERE singleton;"
        )

    def test_fetch_moderation_targets_returns_new_supported_actions(self):
        """Policy target query should return every supported action type."""
        action_table = self.policy_db.schema_name("action")
        self.policy_db.run_psql(
            f"""
            INSERT INTO {action_table} (
                creator_user_id,
                timestamp,
                action_type,
                content_embedding,
                moderation_actions,
                other_properties
            )
            VALUES
                (1, 100, 'post', '[]'::jsonb, NULL, '{{"quality": 0.1}}'::jsonb),
                (1, 200, 'post', '[]'::jsonb, NULL, '{{"quality": 0.2}}'::jsonb),
                (1, 300, 'post', '[]'::jsonb, '[{{"intervention_type": "reviewed", "violation_type": "test_fixture", "rule": "test_fixture"}}]'::jsonb, '{{"quality": 0.3}}'::jsonb),
                (1, 350, 'post', '[]'::jsonb, '[{{"intervention_type": "removed", "violation_type": "below_quality_threshold", "rule": "below_quality_threshold"}}]'::jsonb, '{{"quality": 0.35}}'::jsonb),
                (1, 400, 'post', '[]'::jsonb, NULL, '{{"source": "content_without_quality"}}'::jsonb),
                (1, 500, 'like', '[]'::jsonb, NULL, '{{"source": "not_content"}}'::jsonb),
                (1, 600, 'repost', '[]'::jsonb, NULL, '{{"source": "non_content_repost"}}'::jsonb),
                (1, 700, 'quote', '[0.7]'::jsonb, NULL, '{{"quality": 0.7}}'::jsonb),
                (1, 800, 'post', '[]'::jsonb, NULL, '{{"quality": 0.8}}'::jsonb);

            UPDATE {action_table}
            SET parent_action_id = CASE
                WHEN timestamp IN (500, 700) THEN (
                    SELECT action_id FROM {action_table} WHERE timestamp = 100
                )
                WHEN timestamp = 600 THEN (
                    SELECT action_id FROM {action_table} WHERE timestamp = 200
                )
                ELSE parent_action_id
            END;
            """
        )
        action_ids_by_timestamp = self.policy_db.fetch_json(
            f"""
            SELECT json_object_agg(timestamp, action_id)
            FROM {action_table};
            """
        )
        self._set_clock_tick(825)

        targets = self.policy_db.fetch_moderation_targets()

        self.assertEqual(
            [target.subject_id for target in targets],
            [
                str(action_ids_by_timestamp["100"]),
                str(action_ids_by_timestamp["200"]),
                str(action_ids_by_timestamp["300"]),
                str(action_ids_by_timestamp["350"]),
                str(action_ids_by_timestamp["400"]),
                str(action_ids_by_timestamp["500"]),
                str(action_ids_by_timestamp["600"]),
                str(action_ids_by_timestamp["700"]),
                str(action_ids_by_timestamp["800"]),
            ],
        )
        self.assertEqual(
            [target.action_type for target in targets],
            [
                "post",
                "post",
                "post",
                "post",
                "post",
                "like",
                "repost",
                "quote",
                "post",
            ],
        )
        self.assertTrue(
            all(target.current_timestamp == 825 for target in targets)
        )

        self.assertEqual(
            [target.cursor_action_id for target in targets],
            [
                action_ids_by_timestamp["100"],
                action_ids_by_timestamp["200"],
                action_ids_by_timestamp["300"],
                action_ids_by_timestamp["350"],
                action_ids_by_timestamp["400"],
                action_ids_by_timestamp["500"],
                action_ids_by_timestamp["600"],
                action_ids_by_timestamp["700"],
                action_ids_by_timestamp["800"],
            ],
        )

        targets_after_cursor = self.policy_db.fetch_moderation_targets(
            cursor=PolicyCursor(action_id=action_ids_by_timestamp["400"]),
        )

        self.assertEqual(
            [target.subject_id for target in targets_after_cursor],
            [
                str(action_ids_by_timestamp["500"]),
                str(action_ids_by_timestamp["600"]),
                str(action_ids_by_timestamp["700"]),
                str(action_ids_by_timestamp["800"]),
            ],
        )

    def test_fetch_moderation_targets_returns_expired_rows_after_cursor(self):
        """Expired survival labels should be revisited after action cursor progress."""
        action_table = self.policy_db.schema_name("action")
        self.policy_db.run_psql(
            f"""
            INSERT INTO {action_table} (
                creator_user_id,
                timestamp,
                action_type,
                content_embedding,
                other_properties
            )
            VALUES
                (
                    1,
                    100,
                    'post',
                    '[]'::jsonb,
                    '{{"quality": 0.8, "post_labels": {{"survival_time": 150}}}}'::jsonb
                ),
                (
                    1,
                    125,
                    'post',
                    '[]'::jsonb,
                    '{{"quality": 0.8, "post_labels": {{"survival_time": 250}}}}'::jsonb
                ),
                (
                    1,
                    150,
                    'post',
                    '[]'::jsonb,
                    '{{"quality": 0.8, "post_labels": {{"survival_time": 150}}}}'::jsonb
                ),
                (
                    1,
                    200,
                    'post',
                    '[]'::jsonb,
                    '{{"quality": 0.8, "post_labels": {{"legality": "legal"}}}}'::jsonb
                );

            UPDATE {action_table}
            SET moderation_actions = '[{{
                "intervention_type": "removed",
                "violation_type": "test_fixture",
                "rule": "test_fixture"
            }}]'::jsonb
            WHERE timestamp = 150;
            """
        )
        action_ids_by_timestamp = self.policy_db.fetch_json(
            f"""
            SELECT json_object_agg(timestamp, action_id)
            FROM {action_table};
            """
        )
        self._set_clock_tick(220)

        targets = self.policy_db.fetch_moderation_targets(
            cursor=PolicyCursor(action_id=action_ids_by_timestamp["200"]),
        )

        self.assertEqual(
            [target.subject_id for target in targets],
            [str(action_ids_by_timestamp["100"])],
        )
        self.assertEqual(targets[0].current_timestamp, 220)

    def test_fetch_moderation_targets_advances_cursor_for_deferred_new_rows(self):
        """Future-survival rows should move the cursor without becoming targets."""
        action_table = self.policy_db.schema_name("action")
        action_id = int(
            self.policy_db.execute_sql(
                f"""
                WITH inserted AS (
                    INSERT INTO {action_table} (
                        creator_user_id,
                        timestamp,
                        action_type,
                        content_embedding,
                        other_properties
                    )
                    VALUES (
                        1,
                        100,
                        'post',
                        '[]'::jsonb,
                        '{{"quality": 0.8, "post_labels": {{"survival_time": 500}}}}'::jsonb
                    )
                    RETURNING action_id
                )
                SELECT action_id FROM inserted;
                """
            )
        )

        targets = self.policy_db.fetch_moderation_targets()

        self.assertEqual(targets, [])
        self.assertEqual(targets.next_cursor, PolicyCursor(action_id=action_id))

        self._set_clock_tick(500)
        due_targets = self.policy_db.fetch_moderation_targets(
            cursor=PolicyCursor(action_id=action_id),
        )
        self.assertEqual([target.subject_id for target in due_targets], [str(action_id)])
        self.assertEqual(due_targets[0].current_timestamp, 500)

    def test_new_action_pages_advance_past_deferred_rows(self):
        action_table = self.policy_db.schema_name("action")
        self.policy_db = PostgresPolicyDatabase(self.settings, target_batch_size=2)
        ids = [int(value) for value in self.policy_db.execute_sql(f"""
            INSERT INTO {action_table} (creator_user_id, timestamp, action_type, other_properties)
            VALUES
                (1, 1, 'post', '{{"quality": 0.1}}'::jsonb),
                (1, 2, 'post', '{{"quality": 0.1}}'::jsonb),
                (1, 3, 'post', '{{"quality": 0.1, "post_labels": {{"survival_time": 100}}}}'::jsonb),
                (1, 4, 'post', '{{"quality": 0.1}}'::jsonb),
                (1, 5, 'post', '{{"quality": 0.1}}'::jsonb)
            RETURNING action_id;
        """).splitlines() if value.isdigit()]

        first = self.policy_db.fetch_moderation_targets()
        second = self.policy_db.fetch_moderation_targets(cursor=first.next_cursor)
        third = self.policy_db.fetch_moderation_targets(cursor=second.next_cursor)

        self.assertEqual([[int(target.subject_id) for target in page]
                          for page in (first, second, third)],
                         [ids[:2], [ids[3]], ids[4:]])
        self.assertEqual([page.next_cursor.action_id for page in (first, second, third)],
                         [ids[1], ids[3], ids[4]])

    def test_due_action_pages_cycle_without_starving_later_ids(self):
        action_table = self.policy_db.schema_name("action")
        self.policy_db = PostgresPolicyDatabase(self.settings, target_batch_size=2)
        ids = [int(value) for value in self.policy_db.execute_sql(f"""
            INSERT INTO {action_table} (creator_user_id, timestamp, action_type, other_properties)
            SELECT 1, 1, 'post',
                   '{{"quality": 0.1, "post_labels": {{"survival_time": 10}}}}'::jsonb
            FROM generate_series(1, 3)
            RETURNING action_id;
        """).splitlines() if value.isdigit()]
        self._set_clock_tick(10)
        cursor = PolicyCursor(action_id=ids[-1])

        first = self.policy_db.fetch_moderation_targets(cursor=cursor)
        later_id = int(next(value for value in self.policy_db.execute_sql(f"""
            INSERT INTO {action_table} (creator_user_id, timestamp, action_type, other_properties)
            VALUES (1, 1, 'post',
                    '{{"quality": 0.1, "post_labels": {{"survival_time": 10}}}}'::jsonb)
            RETURNING action_id;
        """).splitlines() if value.isdigit()))
        later_cursor = PolicyCursor(action_id=later_id)
        pages = [first] + [self.policy_db.fetch_moderation_targets(cursor=later_cursor)
                           for _ in range(3)]

        self.assertEqual([[int(target.subject_id) for target in page] for page in pages],
                         [ids[:2], ids[2:], [], ids[:2]])

    def test_policy_cursor_is_persisted_on_lifecycle_row(self):
        """Policy cursor should be a durable action-id high-water mark."""
        self.assertIsNone(self.policy_db.fetch_policy_cursor())

        self.policy_db.record_policy_cursor(PolicyCursor(action_id=7))
        self.policy_db.record_policy_cursor(PolicyCursor(action_id=5))

        self.assertEqual(self.policy_db.fetch_policy_cursor(), PolicyCursor(action_id=7))

    def test_write_moderation_actions_merges_jsonb_without_timestamp(self):
        """Policy write should merge structured annotations into existing JSONB."""
        action_table = self.policy_db.schema_name("action")
        action_id = int(
            self.policy_db.execute_sql(
                f"""
                WITH inserted AS (
                    INSERT INTO {action_table} (
                        creator_user_id,
                        timestamp,
                        action_type,
                        moderation_actions,
                        other_properties
                    )
                    VALUES (
                        1,
                        100,
                        'post',
                        '[{{"intervention_type": "reviewed", "violation_type": "test_fixture", "rule": "test_fixture"}}]'::jsonb,
                        '{{"quality": 0.1}}'::jsonb
                    )
                    RETURNING action_id
                )
                SELECT action_id FROM inserted;
                """
            )
        )

        self.policy_db.write_moderation_actions(
            [
                PolicyModerationAction(
                    subject_id=str(action_id),
                    interventions=(
                        intervention_entry(
                            intervention_type=REMOVED_INTERVENTION,
                            violation_type="below_quality_threshold",
                            rule="below_quality_threshold",
                        ),
                    ),
                )
            ]
        )

        row = self.policy_db.fetch_json(
            f"""
            SELECT json_build_object('moderation_actions', moderation_actions)
            FROM {action_table}
            WHERE action_id = {action_id};
            """
        )

        self.assertEqual(
            row["moderation_actions"],
            [
                {
                    "intervention_type": "reviewed",
                    "violation_type": "test_fixture",
                    "rule": "test_fixture",
                },
                {
                    "intervention_type": REMOVED_INTERVENTION,
                    "violation_type": "below_quality_threshold",
                    "rule": "below_quality_threshold",
                },
            ],
        )

    def test_empty_moderation_actions_noop(self):
        """Policy should skip DB writes when there are no interventions."""
        action_table = self.policy_db.schema_name("action")
        action_id = int(
            self.policy_db.execute_sql(
                f"""
                WITH inserted AS (
                    INSERT INTO {action_table} (
                        creator_user_id,
                        timestamp,
                        action_type,
                        other_properties
                    )
                    VALUES (
                        1,
                        100,
                        'post',
                        '{{"quality": 0.9}}'::jsonb
                    )
                    RETURNING action_id
                )
                SELECT action_id FROM inserted;
                """
            )
        )

        self.policy_db.write_moderation_actions([])

        row = self.policy_db.fetch_json(
            f"""
            SELECT json_build_object('moderation_actions', moderation_actions)
            FROM {action_table}
            WHERE action_id = {action_id};
            """
        )

        self.assertIsNone(row["moderation_actions"])

    @classmethod
    def _check_server(cls) -> None:
        try:
            completed = subprocess.run(
                [cls.psql_bin, "-d", cls.maintenance_db, "-tAc", "SELECT 1"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise unittest.SkipTest(f"local PostgreSQL server is not available: {exc}") from exc

        if completed.stdout.strip() != "1":
            raise unittest.SkipTest("local PostgreSQL server did not return a healthy probe result")

    @staticmethod
    def _resolve_binary(*candidates: str) -> str:
        for candidate in candidates:
            if not candidate:
                continue
            if os.path.sep in candidate:
                if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                    return candidate
                continue
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        raise unittest.SkipTest(f"required Postgres client not found in candidates: {candidates}")


if __name__ == "__main__":
    unittest.main()
