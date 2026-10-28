# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Unit tests for the shared database schema and lifecycle contracts."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from db.core import (  # noqa: E402
    ANALYTICS_SUMMARY_CONTRACT,
    ACTION_CONTRACT,
    FOLLOWER_NETWORK_CONTRACT,
    SHARED_INDEX_CONTRACTS,
    LIFECYCLE_CONTRACT,
    USER_CONTRACT,
    DatabaseSettings,
    FollowerNetworkRecord,
    MAX_INTEGER_USER_ID,
    PostgresLifecycleMixin,
    PostgresClientBase,
    UserRecord,
    build_shared_schema_sql,
)


class SharedSchemaContractTests(unittest.TestCase):
    """Validate the shared Postgres schema contract exposed by db.core."""

    def test_user_record_imports_and_defaults_match_schema(self):
        """User DTO defaults should match the shared user/action columns."""
        user = UserRecord(user_id=1, user_type="normal")

        self.assertEqual(user.user_behavior, {})
        self.assertEqual(user.activity_level, 0.0)
        self.assertEqual(user.user_type, "normal")
        self.assertLessEqual(
            {"user_type", "user_behavior", "moderation_actions", "other_properties"},
            set(USER_CONTRACT.columns),
        )
        self.assertLessEqual(
            {
                "moderation_actions",
                "repeated_like_n_reshare_creator_count",
                "other_properties",
            },
            set(ACTION_CONTRACT.columns),
        )
        self.assertFalse(
            {"lurker", "annotations", "attributes"}.intersection(USER_CONTRACT.columns)
        )
        self.assertFalse(
            {"annotations", "attributes"}.intersection(ACTION_CONTRACT.columns)
        )
        self.assertLessEqual(
            {
                "user_count",
                "action_count",
                "avg_activity_level",
                "avg_action_quality",
                "termination_criteria",
                "other_properties",
            },
            set(ANALYTICS_SUMMARY_CONTRACT.columns),
        )
        self.assertLessEqual(
            {
                "started_at",
                "termination_source",
                "analytics_stop_requested",
                "analytics_stop_reason",
                "analytics_stop_updated_at",
                "policy_cursor_action_id",
                "policy_cursor_updated_at",
            },
            set(LIFECYCLE_CONTRACT.columns),
        )

        with self.assertRaises(TypeError):
            UserRecord(user_id=1)

    def test_user_identifiers_accept_one_billion_and_reject_integer_overflow(self):
        """User DTOs enforce the PostgreSQL INTEGER user-ID range."""
        UserRecord(user_id=1_000_000_000, user_type="normal")
        FollowerNetworkRecord(
            user_id=1_000_000_000,
            friend_id=MAX_INTEGER_USER_ID,
        )

        with self.assertRaises(ValueError):
            UserRecord(user_id=MAX_INTEGER_USER_ID + 1, user_type="normal")
        with self.assertRaises(ValueError):
            FollowerNetworkRecord(user_id=1, friend_id=MAX_INTEGER_USER_ID + 1)

    def test_core_contracts_define_relationships_and_indexes(self):
        """Core contracts should expose ERD relationships and shared indexes."""
        action_fks = {
            (foreign_key.column, foreign_key.target_table, foreign_key.target_column)
            for foreign_key in ACTION_CONTRACT.foreign_keys
        }
        follower_fks = {
            (foreign_key.column, foreign_key.target_table, foreign_key.target_column)
            for foreign_key in FOLLOWER_NETWORK_CONTRACT.foreign_keys
        }
        index_names = {index.name for index in SHARED_INDEX_CONTRACTS}

        self.assertLessEqual(
            {
                ("creator_user_id", "user", "user_id"),
                ("parent_action_id", "action", "action_id"),
            },
            action_fks,
        )
        self.assertLessEqual(
            {
                ("user_id", "user", "user_id"),
                ("friend_id", "user", "user_id"),
            },
            follower_fks,
        )
        self.assertLessEqual(
            {"action_timestamp_idx", "follower_network_friend_idx"},
            index_names,
        )

    def test_schema_assigns_action_timestamps_to_clock_extension(self):
        """Action timestamps should be wired to the clock extension."""
        sql = build_shared_schema_sql(
            DatabaseSettings(
                schema="unit_test",
                clock_extension_name="simsom_clock",
            )
        )

        self.assertTrue(sql.strip())
        self.assertIn("action_post_label_survival_time_idx", sql)
        self.assertIn("user_id INTEGER PRIMARY KEY", sql)
        self.assertIn("creator_user_id INTEGER", sql)
        self.assertIn("friend_id INTEGER", sql)
        self.assertIn("action_id BIGINT GENERATED ALWAYS AS IDENTITY", sql)
        self.assertIn("parent_action_id BIGINT", sql)
        self.assertIn("timestamp BIGINT", sql)
        self.assertIn("user_count BIGINT", sql)
        self.assertIn("action_count BIGINT", sql)
        self.assertIn("DROP TABLE IF EXISTS", sql)
        self.assertNotIn("CREATE TABLE schema_migration", sql)
        self.assertNotIn("INSERT INTO schema_migration", sql)


class RecordingLifecycleClient(PostgresLifecycleMixin):
    """Lifecycle test double that records generated SQL instead of running psql."""

    def __init__(self, output: str):
        """Store one lifecycle query output for captured psql calls."""
        self.settings = DatabaseSettings(schema="unit_test")
        self.output = output
        self.calls = []

    def run_psql(self, sql: str, *, capture_output: bool = False):
        """Record SQL and return configured output for captured reads."""
        self.calls.append((sql, capture_output))
        if capture_output:
            return self.output
        return None


class PostgresLifecycleMixinTests(unittest.TestCase):
    """Validate shared lifecycle read behavior."""

    def test_should_terminate_reads_shared_lifecycle_row(self):
        """should_terminate should read the singleton lifecycle row."""
        client = RecordingLifecycleClient(
            '{"should_terminate": true, "reason": "done", '
            '"analytics_stop_requested": true, '
            '"analytics_stop_reason": "test termination", '
            '"analytics_termination_criteria": [{"function_name": "quality", '
            '"met": true}]}'
        )

        self.assertTrue(client.should_terminate())
        state = client.get_termination_state()
        self.assertTrue(state.analytics_stop_requested)
        self.assertEqual(
            state.analytics_stop_reason,
            "test termination",
        )
        self.assertEqual(
            state.analytics_termination_criteria,
            [{"function_name": "quality", "met": True}],
        )
        self.assertTrue(client.calls[0][1])


class PostgresClientBaseTests(unittest.TestCase):
    """Validate shared psql process execution behavior."""

    def test_run_psql_suppresses_normal_command_output(self):
        """Normal command tags should be captured, not printed to terminal."""
        client = PostgresClientBase(DatabaseSettings(dsn="postgres", schema="unit_test"))

        with patch("db.core.connection.shutil.which", return_value="/usr/bin/psql"):
            with patch("db.core.connection.subprocess.run") as run:
                run.return_value = SimpleNamespace(
                    returncode=0,
                    stdout="INSERT 0 1\nCOMMIT\n",
                    stderr="",
                )

                self.assertIsNone(client.run_psql("INSERT INTO x VALUES (1);"))

        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_run_psql_returns_captured_query_output_when_requested(self):
        """Explicit reads should still return unaligned captured psql output."""
        client = PostgresClientBase(DatabaseSettings(dsn="postgres", schema="unit_test"))

        with patch("db.core.connection.shutil.which", return_value="/usr/bin/psql"):
            with patch("db.core.connection.subprocess.run") as run:
                run.return_value = SimpleNamespace(
                    returncode=0,
                    stdout="42\n",
                    stderr="",
                )

                output = client.run_psql("SELECT 42;", capture_output=True)

        self.assertEqual(output, "42")
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["-t", "-A"])

    def test_run_psql_labels_failed_command_output(self):
        """Failed commands should expose stderr and stdout without normal streaming."""
        client = PostgresClientBase(DatabaseSettings(dsn="postgres", schema="unit_test"))

        with patch("db.core.connection.shutil.which", return_value="/usr/bin/psql"):
            with patch("db.core.connection.subprocess.run") as run:
                run.return_value = SimpleNamespace(
                    returncode=1,
                    stdout="partial output\n",
                    stderr="ERROR: deadlock detected\n",
                )

                with self.assertRaisesRegex(RuntimeError, "stderr:") as raised:
                    client.run_psql("UPDATE x SET y = 1;")

        self.assertEqual(
            str(raised.exception),
            "psql command failed: stderr:\nERROR: deadlock detected\n"
            "stdout:\npartial output",
        )


if __name__ == "__main__":
    unittest.main()
