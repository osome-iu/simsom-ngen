# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Integration tests for analytics DB behavior against Postgres."""

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

from db.analytics import (  # noqa: E402
    AnalyticsStopSignal,
    AnalyticsSummaryRecord,
    PostgresAnalyticsDatabase,
)
from db.controller import PostgresControllerDatabase, SetupDbDataset  # noqa: E402
from db.core import DatabaseSettings, FollowerNetworkRecord, UserRecord  # noqa: E402
from subcomponents.policy_annotations import intervention_rule  # noqa: E402


class AnalyticsPostgresIntegrationTests(unittest.TestCase):
    """Exercise final analytics reports and termination signals with real rows."""

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

        cls.test_db = f"simsom_analytics_test_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        subprocess.run([cls.createdb_bin, cls.test_db], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "test_db"):
            subprocess.run([cls.dropdb_bin, "--if-exists", cls.test_db], check=False)

    def setUp(self) -> None:
        self.schema = f"analytics_test_{uuid.uuid4().hex[:8]}"
        self.settings = DatabaseSettings(
            dsn=self.test_db,
            schema=self.schema,
            clock_extension_name="simsom_clock",
        )
        self.controller = PostgresControllerDatabase(self.settings)
        self.analytics_db = PostgresAnalyticsDatabase(self.settings)
        self.controller.setup_schema()
        self.controller.populate_initial_state(
            SetupDbDataset(
                users=[
                    UserRecord(user_id=1, user_type="normal", activity_level=1.0),
                    UserRecord(user_id=2, user_type="normal", activity_level=3.0),
                ],
                follower_network=[
                    FollowerNetworkRecord(user_id=1, friend_id=2),
                    FollowerNetworkRecord(user_id=2, friend_id=1),
                ],
            )
        )

    def test_fetch_analytics_snapshot_reads_current_aggregates(self):
        """Analytics snapshot should read aggregate and descriptive DB state."""
        action_table = self.analytics_db.schema_name("action")
        lifecycle_table = self.analytics_db.schema_name("simulation_lifecycle")
        self.analytics_db.run_psql(
            f"""
            INSERT INTO {action_table} (
                creator_user_id,
                timestamp,
                action_type,
                moderation_actions,
                other_properties
            )
            VALUES
                (
                    1,
                    100,
                    'post',
                    '[{{"intervention_type": "removed", "violation_type": "below_quality_threshold", "rule": "below_quality_threshold"}}]'::jsonb,
                    '{{"quality": 0.2}}'::jsonb
                );

            INSERT INTO {action_table} (
                creator_user_id,
                timestamp,
                action_type,
                parent_action_id,
                repeated_like_n_reshare_creator_count,
                other_properties
            )
            VALUES
                (2, 120, 'repost', 1, 99, '{{}}'::jsonb),
                (2, 150, 'repost', 1, 1, '{{}}'::jsonb),
                (1, 170, 'repost', 1, 1, '{{}}'::jsonb),
                (1, 180, 'repost', 1, 1, '{{}}'::jsonb),
                (2, 190, 'like', 1, 9, '{{"source": "no_quality"}}'::jsonb);

            UPDATE {lifecycle_table}
            SET policy_cursor_action_id = 1,
                policy_cursor_updated_at = NOW()
            WHERE lifecycle_id = 1;
            """
        )

        snapshot = self.analytics_db.fetch_analytics_snapshot()

        self.assertEqual(snapshot.user_count, 2)
        self.assertEqual(snapshot.action_count, 6)
        self.assertAlmostEqual(snapshot.avg_activity_level, 2.0)
        self.assertAlmostEqual(snapshot.avg_action_quality, 0.2)

        self.assertIsNone(snapshot.other_properties.get("action_type_counts"))

        action_type_ratios = snapshot.other_properties["action_type_ratios"]
        self.assertEqual(action_type_ratios["like"], "0.167")
        self.assertEqual(action_type_ratios["post"], "0.167")
        self.assertEqual(action_type_ratios["repost"], "0.667")

        repost_stats = snapshot.other_properties["repost_stats"]
        self.assertEqual(
            list(repost_stats),
            [
                "repost_action_ratio",
                "repeated_repost_ratio",
                "self_repost_ratio",
                "self_repeated_repost_ratio",
            ],
        )
        self.assertEqual(repost_stats["repost_action_ratio"], "0.667")
        self.assertEqual(repost_stats["repeated_repost_ratio"], "0.500")
        self.assertEqual(repost_stats["self_repost_ratio"], "0.500")
        self.assertEqual(repost_stats["self_repeated_repost_ratio"], "0.250")
        self.assertFalse(
            {
                "repeated_repost_action_ratio",
                "self_repost_action_ratio",
                "self_repeated_repost_action_ratio",
                "repost_count",
                "repeated_repost_count",
                "self_repost_count",
                "self_repeated_repost_count",
                "avg_repeated_repost_count",
                "max_repeated_repost_count",
            }.intersection(repost_stats)
        )

        policy_stats = snapshot.other_properties["policy_stats"]
        self.assertEqual(
            policy_stats,
            {
                "policy_coverage": "0.167",
                "policy_intervention_ratio": "1.000",
            },
        )

        network_stats = snapshot.other_properties["network_stats"]
        self.assertAlmostEqual(network_stats["avg_friends_per_user"], 1.0)
        self.assertAlmostEqual(network_stats["avg_followers_per_user"], 1.0)

        timing = snapshot.other_properties["timing"]
        self.assertEqual(set(timing), {"inter_activity_ms"})
        inter_activity = timing["inter_activity_ms"]
        self.assertIsNone(inter_activity.get("count"))
        self.assertEqual(inter_activity["min"], 10)
        self.assertAlmostEqual(inter_activity["mean"], 18.0)
        self.assertAlmostEqual(inter_activity["p50"], 20.0)
        self.assertAlmostEqual(inter_activity["p90"], 26.0)
        self.assertEqual(inter_activity["max"], 30)

        runtime = snapshot.other_properties["runtime"]
        self.assertGreaterEqual(runtime["software_runtime_seconds"], 0.0)
        self.assertAlmostEqual(runtime["simulated_runtime_seconds"], 0.09)
        self.assertIsNone(runtime.get("simulated_runtime_ms"))

    def test_fetch_analytics_progress_reads_clock_and_action_watermark(self):
        """The progress probe should avoid the full analytics aggregate query."""
        action_table = self.analytics_db.schema_name("action")

        initial = self.analytics_db.fetch_analytics_progress()
        self.analytics_db.run_psql(
            f"""
            INSERT INTO {action_table} (creator_user_id, action_type)
            VALUES (1, 'post');
            """
        )
        progress = self.analytics_db.fetch_analytics_progress()
        snapshot = self.analytics_db.fetch_termination_metrics()

        self.assertEqual(initial.user_count, 2)
        self.assertEqual(initial.action_id_watermark, 0)
        self.assertEqual(progress.action_id_watermark, 1)
        self.assertGreater(progress.simulator_timestamp_ms, 0)
        self.assertEqual(
            snapshot.simulator_timestamp_ms,
            progress.simulator_timestamp_ms,
        )

    def test_policy_stats_use_policy_cursor_coverage(self):
        """Policy coverage should measure cursor-covered supported actions."""
        action_table = self.analytics_db.schema_name("action")
        lifecycle_table = self.analytics_db.schema_name("simulation_lifecycle")
        test_user_rule = "test_user_source_content_intervention"

        @intervention_rule(test_user_rule, source="user")
        def test_user_source_content_intervention():
            return None

        self.analytics_db.run_psql(
            f"""
            INSERT INTO {action_table} (
                creator_user_id,
                timestamp,
                action_type,
                moderation_actions,
                other_properties
            )
            VALUES
                (
                    1,
                    100,
                    'post',
                    '[{{"intervention_type": "removed", "violation_type": "below_quality_threshold", "rule": "below_quality_threshold"}}]'::jsonb,
                    '{{"quality": 0.1}}'::jsonb
                ),
                (
                    1,
                    200,
                    'post',
                    '[{{"intervention_type": "removed", "violation_type": "test_user_fixture", "rule": "{test_user_rule}"}}]'::jsonb,
                    '{{"quality": 0.9}}'::jsonb
                ),
                (
                    1,
                    300,
                    'post',
                    '{{}}'::jsonb,
                    '{{"quality": 0.8}}'::jsonb
                );

            UPDATE {lifecycle_table}
            SET policy_cursor_action_id = 2,
                policy_cursor_updated_at = NOW()
            WHERE lifecycle_id = 1;
            """
        )

        snapshot = self.analytics_db.fetch_analytics_snapshot()

        self.assertEqual(
            snapshot.other_properties["policy_stats"],
            {
                "policy_coverage": "0.667",
                "policy_intervention_ratio": "0.500",
            },
        )

    def test_record_analytics_stop_signal_does_not_request_termination(self):
        """Analytics stop signals should remain controller-readable signal state."""
        self.analytics_db.record_analytics_stop_signal(
            AnalyticsStopSignal(
                stop_requested=True,
                stop_reason="test termination",
                termination_criteria=[
                    {
                        "met": True,
                        "function_name": "test_criterion",
                        "reason": "test termination",
                    }
                ],
            )
        )

        state = self.controller.get_termination_state()

        self.assertTrue(state.analytics_stop_requested)
        self.assertEqual(
            state.analytics_stop_reason,
            "test termination",
        )
        self.assertFalse(state.should_terminate)

    def test_first_accepted_stop_survives_late_analytics_and_repeated_requests(self):
        criteria_a = [{"met": True, "function_name": "criterion_a",
                       "reason": "accepted", "details": {"action_count": 30000}}]
        criteria_b = [{"met": True, "function_name": "criterion_b",
                       "reason": "late", "details": {"action_count": 30500}}]
        self.analytics_db.record_analytics_stop_signal(AnalyticsStopSignal(
            stop_requested=True, stop_reason="accepted", termination_criteria=criteria_a))
        observed = self.controller.get_termination_state()
        # Analytics may change the signal even before the controller's write.
        self.analytics_db.record_analytics_stop_signal(AnalyticsStopSignal(
            stop_requested=True, stop_reason="late", termination_criteria=criteria_b))
        self.controller.request_termination(observed.analytics_stop_reason,
            termination_criteria=observed.analytics_termination_criteria)
        self.controller.request_termination("replacement", termination_criteria=criteria_b)
        self.analytics_db.record_analytics_stop_signal(AnalyticsStopSignal(
            stop_requested=True, stop_reason="late", termination_criteria=criteria_b))
        state = self.controller.get_termination_state()
        self.assertEqual(state.reason, "accepted")
        self.assertEqual(state.termination_criteria, criteria_a)
        self.assertEqual(state.analytics_stop_reason, "late")
        self.assertEqual(state.analytics_termination_criteria, criteria_b)

    def test_record_analytics_summary_persists_average_quality(self):
        """Analytics summaries should log status metrics for the current run."""
        self.analytics_db.record_analytics_summary(
            AnalyticsSummaryRecord(
                user_count=2,
                action_count=4,
                avg_activity_level=2.0,
                avg_action_quality=0.65,
                termination_criteria=[
                    {
                        "met": True,
                        "function_name": "quality_stability",
                        "reason": "stable metrics",
                    }
                ],
                other_properties={"window": "all"},
            )
        )

        row = self.analytics_db.fetch_json(
            f"""
            SELECT json_build_object(
                'user_count', user_count,
                'action_count', action_count,
                'avg_activity_level', avg_activity_level,
                'avg_action_quality', avg_action_quality,
                'termination_criteria', termination_criteria,
                'other_properties', other_properties
            )
            FROM {self.analytics_db.schema_name("analytics_summary")}
            ORDER BY analytics_summary_id DESC
            LIMIT 1;
            """
        )

        self.assertEqual(row["user_count"], 2)
        self.assertEqual(row["action_count"], 4)
        self.assertAlmostEqual(row["avg_activity_level"], 2.0)
        self.assertAlmostEqual(row["avg_action_quality"], 0.65)
        self.assertEqual(
            row["termination_criteria"],
            [
                {
                    "met": True,
                    "function_name": "quality_stability",
                    "reason": "stable metrics",
                }
            ],
        )
        self.assertEqual(row["other_properties"], {"window": "all"})

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
