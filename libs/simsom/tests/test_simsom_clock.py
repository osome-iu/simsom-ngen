# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
import uuid

SIMSOM_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(SIMSOM_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(SIMSOM_PACKAGE_ROOT))

from db.controller import PostgresControllerDatabase, SetupDbDataset
from db.core import DatabaseSettings, FollowerNetworkRecord, UserRecord
from db.worker import (
    ActionWriteRecord,
    PostgresWorkerDatabase,
    gaussian_around_user_quality,
    post_distribution_function,
)
from subcomponents.user_executor import UserAction


TEST_POST_DISTRIBUTION_SELECTOR = "test_clock_post_distribution"


@post_distribution_function(TEST_POST_DISTRIBUTION_SELECTOR)
def test_clock_post_distribution(behavior, spec):
    """Generate a valid test property without depending on a shipped label."""
    return gaussian_around_user_quality(behavior, spec, stddev=0.12)


class SimsomClockExtensionTest(unittest.TestCase):
    """
    Integration tests for the main PostgreSQL clock extension.

    libs/simsom/db/clock is implemented in C + SQL, so the tests install the
    current extension build and exercise it through psql against a scratch
    database.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.make_bin = cls._resolve_binary(
            os.environ.get("SIMSOM_MAKE_BIN"),
            "make",
        )
        cls.psql_bin = cls._resolve_binary(
            os.environ.get("SIMSOM_PSQL_BIN"),
            "psql",
        )
        cls.createdb_bin = cls._resolve_binary(
            os.environ.get("SIMSOM_CREATEDB_BIN"),
            "createdb",
        )
        cls.dropdb_bin = cls._resolve_binary(
            os.environ.get("SIMSOM_DROPDB_BIN"),
            "dropdb",
        )
        cls.maintenance_db = os.environ.get("SIMSOM_MAINTENANCE_DB", "postgres")
        cls.repo_root = Path(__file__).resolve().parents[3]
        cls.extension_dir = cls.repo_root / "libs" / "simsom" / "db" / "clock"

        cls._install_extension_under_test()
        cls._check_server()

        cls.test_db = f"simsom_clock_test_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        subprocess.run([cls.createdb_bin, cls.test_db], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "test_db"):
            subprocess.run([cls.dropdb_bin, "--if-exists", cls.test_db], check=False)

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

    @classmethod
    def _install_extension_under_test(cls) -> None:
        if not cls.extension_dir.is_dir():
            raise unittest.SkipTest(f"clock extension directory not found: {cls.extension_dir}")

        build_env = os.environ.copy()
        real_pg_config = os.environ.get("SIMSOM_REAL_PG_CONFIG")
        if real_pg_config:
            build_env["REAL_PG_CONFIG"] = real_pg_config

        try:
            subprocess.run(
                [cls.make_bin, "-C", str(cls.extension_dir), "install"],
                check=True,
                env=build_env,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
            raise unittest.SkipTest(f"failed to build/install simsom_clock under test: {message}") from exc

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

    @classmethod
    def _run_sql(cls, sql: str) -> str:
        completed = subprocess.run(
            [cls.psql_bin, "-d", cls.test_db, "-v", "ON_ERROR_STOP=1", "-tAqc", sql],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def setUp(self) -> None:
        self._run_sql("DROP EXTENSION IF EXISTS simsom_clock CASCADE;")
        self._run_sql("CREATE EXTENSION simsom_clock;")

    def test_configure_exponential_resets_state_and_matches_expected_lambda(self) -> None:
        row = self._run_sql(
            """
            SELECT current_tick_ms, lambda
            FROM simsom_clock_configure_exponential(
                p_num_users => 10000,
                p_origin_timestamp => timestamp '2020-01-01 00:00:00'
            );
            """
        )

        tick_ms_text, lambda_text = row.split("|")
        self.assertEqual(int(tick_ms_text), 0)
        self.assertAlmostEqual(float(lambda_text), 0.0009977140112913797, places=15)

    def test_nextval_is_strictly_increasing_and_updates_state(self) -> None:
        results = self._run_sql(
            """
            DO $$
            BEGIN
                PERFORM simsom_clock_configure_exponential(
                    p_num_users => 10000,
                    p_origin_timestamp => timestamp '2020-01-01 00:00:00'
                );
            END
            $$;

            WITH generated AS (
                SELECT
                    row_number() OVER (ORDER BY tick_ms) AS call_index,
                    tick_ms
                FROM (
                    SELECT simsom_clock_nextval() AS tick_ms
                    FROM generate_series(1, 25)
                ) AS allocated_rows
            ),
            checked AS (
                SELECT
                    call_index,
                    tick_ms,
                    lag(tick_ms) OVER (ORDER BY call_index) AS prev_tick_ms
                FROM generated
            ),
            deltas AS (
                SELECT
                    call_index,
                    tick_ms - prev_tick_ms AS delta_ms
                FROM checked
            )
            SELECT
                bool_and(prev_tick_ms IS NULL OR tick_ms > prev_tick_ms),
                min(delta_ms),
                max(tick_ms)
            FROM checked
            LEFT JOIN deltas USING (call_index)
            WHERE delta_ms IS NOT NULL;

            SELECT simsom_clock_current_tick_ms();
            """
        )

        result_lines = results.splitlines()
        increasing_text, min_delta_text, max_tick_text = result_lines[0].split("|")
        current_tick_text = result_lines[1]
        self.assertEqual(increasing_text, "t")
        self.assertGreaterEqual(int(min_delta_text), 1)
        self.assertEqual(int(current_tick_text), int(max_tick_text))
        self.assertGreater(int(current_tick_text), 0)

    def test_v1_default_table_path_uses_clock_allocator(self) -> None:
        results = self._run_sql(
            """
            DO $$
            BEGIN
                PERFORM simsom_clock_configure_exponential(
                    p_num_users => 10000,
                    p_origin_timestamp => timestamp '2020-01-01 00:00:00'
                );
            END
            $$;

            CREATE TABLE test_v1_default (
                timestamp bigint primary key
                    default simsom_clock_nextval(),
                payload bigint NOT NULL
            );

            INSERT INTO test_v1_default (payload)
            SELECT 0::bigint
            FROM generate_series(1, 10);

            WITH checked AS (
                SELECT
                    timestamp,
                    lag(timestamp) OVER (ORDER BY timestamp) AS prev_timestamp
                FROM test_v1_default
            ),
            deltas AS (
                SELECT
                    timestamp - prev_timestamp AS delta_ms
                FROM checked
                WHERE prev_timestamp IS NOT NULL
            )
            SELECT
                (SELECT count(*) FROM test_v1_default),
                (SELECT bool_and(prev_timestamp IS NULL OR timestamp > prev_timestamp)
                 FROM checked),
                (SELECT min(delta_ms) FROM deltas);
            """
        )

        row_count_text, increasing_text, min_delta_text = results.split("|")
        self.assertEqual(int(row_count_text), 10)
        self.assertEqual(increasing_text, "t")
        self.assertGreaterEqual(int(min_delta_text), 1)

    def test_v2_default_table_path_uses_identity_and_clock_allocator(self) -> None:
        results = self._run_sql(
            """
            DO $$
            BEGIN
                PERFORM simsom_clock_configure_exponential(
                    p_num_users => 10000,
                    p_origin_timestamp => timestamp '2020-01-01 00:00:00'
                );
            END
            $$;

            CREATE TABLE test_v2_default (
                action_id bigint generated always as identity primary key,
                timestamp bigint NOT NULL
                    default simsom_clock_nextval(),
                payload bigint NOT NULL
            );

            INSERT INTO test_v2_default (payload)
            SELECT 0::bigint
            FROM generate_series(1, 10);

            WITH checked AS (
                SELECT
                    action_id,
                    lag(action_id) OVER (ORDER BY action_id) AS prev_action_id,
                    timestamp,
                    lag(timestamp) OVER (ORDER BY action_id) AS prev_timestamp
                FROM test_v2_default
            ),
            deltas AS (
                SELECT
                    action_id,
                    timestamp - prev_timestamp AS delta_ms
                FROM checked
            )
            SELECT
                (SELECT count(*) FROM test_v2_default),
                bool_and(prev_action_id IS NULL OR action_id = prev_action_id + 1),
                bool_and(prev_timestamp IS NULL OR timestamp > prev_timestamp),
                min(delta_ms)
            FROM checked
            LEFT JOIN deltas USING (action_id)
            WHERE delta_ms IS NOT NULL;
            """
        )

        row_count_text, gapless_ids_text, increasing_text, min_delta_text = results.split("|")
        self.assertEqual(int(row_count_text), 10)
        self.assertEqual(gapless_ids_text, "t")
        self.assertEqual(increasing_text, "t")
        self.assertGreaterEqual(int(min_delta_text), 1)

    def test_trigger_path_stamps_timestamp_server_side(self) -> None:
        results = self._run_sql(
            """
            DO $$
            BEGIN
                PERFORM simsom_clock_configure_exponential(
                    p_num_users => 10000,
                    p_origin_timestamp => timestamp '2020-01-01 00:00:00'
                );
            END
            $$;

            CREATE TABLE test_trigger_stamped (
                action_id bigint generated always as identity primary key,
                timestamp bigint NOT NULL,
                payload bigint NOT NULL
            );

            CREATE TRIGGER test_trigger_stamped_timestamp
            BEFORE INSERT ON test_trigger_stamped
            FOR EACH ROW
            EXECUTE FUNCTION simsom_clock_stamp_timestamp();

            INSERT INTO test_trigger_stamped (timestamp, payload)
            SELECT -1, 0::bigint
            FROM generate_series(1, 10);

            WITH checked AS (
                SELECT
                    action_id,
                    lag(action_id) OVER (ORDER BY action_id) AS prev_action_id,
                    timestamp,
                    lag(timestamp) OVER (ORDER BY action_id) AS prev_timestamp
                FROM test_trigger_stamped
            ),
            deltas AS (
                SELECT
                    action_id,
                    timestamp - prev_timestamp AS delta_ms
                FROM checked
            )
            SELECT
                (SELECT count(*) FROM test_trigger_stamped),
                bool_and(prev_action_id IS NULL OR action_id = prev_action_id + 1),
                bool_and(timestamp > 0),
                bool_and(prev_timestamp IS NULL OR timestamp > prev_timestamp),
                min(delta_ms)
            FROM checked
            LEFT JOIN deltas USING (action_id)
            WHERE delta_ms IS NOT NULL;
            """
        )

        row_count_text, gapless_ids_text, positive_ticks_text, increasing_text, min_delta_text = results.split("|")
        self.assertEqual(int(row_count_text), 10)
        self.assertEqual(gapless_ids_text, "t")
        self.assertEqual(positive_ticks_text, "t")
        self.assertEqual(increasing_text, "t")
        self.assertGreaterEqual(int(min_delta_text), 1)

    def test_controller_schema_and_worker_writes_use_clock_default(self) -> None:
        schema = f"simsom_runtime_{uuid.uuid4().hex[:8]}"
        settings = DatabaseSettings(
            dsn=self.test_db,
            schema=schema,
            clock_extension_name="simsom_clock",
        )
        controller = PostgresControllerDatabase(settings)
        worker = PostgresWorkerDatabase(
            settings,
            action_insert_chunk_size=1000,
            user_types_config={
                "normal": {
                    "num_nodes": 1,
                    "post_quality_distribution_function": {TEST_POST_DISTRIBUTION_SELECTOR: {}},
                    "post_appeal_distribution_function": {TEST_POST_DISTRIBUTION_SELECTOR: {}},
                }
            },
            recsys_config=SimpleNamespace(
                engagement_action_lookback_population_multiplier=10,
            ),
        )

        try:
            controller.setup_schema()
            controller.populate_initial_state(
                SetupDbDataset(
                    users=[
                        UserRecord(
                            user_id=1,
                            user_type="normal",
                            vector_profile=[0.1, 0.2],
                            activity_level=0.5,
                            user_behavior={
                                "p_actions": {"post": 1.0},
                                "quality": 0.85,
                            },
                        ),
                        UserRecord(
                            user_id=2,
                            user_type="normal",
                            vector_profile=[0.2, 0.3],
                            activity_level=0.7,
                            user_behavior={
                                "p_actions": {"post": 1.0},
                                "quality": 0.85,
                            },
                        ),
                    ],
                    follower_network=[
                        FollowerNetworkRecord(user_id=1, friend_id=2),
                    ],
                )
            )

            claimed = worker.claim_user_batch(
                1, lurker_ratio=0.0, planned_action_count=1
            )
            self.assertEqual(len(claimed), 1)

            worker.write_actions(
                [
                    ActionWriteRecord(
                        session_id=claimed[0].session_id,
                        action=UserAction.post(
                            creator_user_id=claimed[0].user_id,
                            content_embedding=[0.3, 0.4],
                        ),
                        user_record=claimed[0].user_record,
                    )
                ]
            )

            action_stats = controller.fetch_json(
                f"""
                SELECT json_build_object(
                    'action_count', count(*),
                    'positive_timestamps', bool_and(timestamp > 0),
                    'min_timestamp', min(timestamp),
                    'max_timestamp', max(timestamp)
                )
                FROM {controller.schema_name("action")};
                """
            )

            self.assertEqual(action_stats["action_count"], 1)
            self.assertTrue(action_stats["positive_timestamps"])
            self.assertEqual(action_stats["min_timestamp"], action_stats["max_timestamp"])
        finally:
            controller.execute_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE;')

    def test_controller_setup_recreates_stale_clock_extension(self) -> None:
        self._run_sql(
            """
            ALTER EXTENSION simsom_clock DROP FUNCTION simsom_clock_nextval();
            DROP FUNCTION simsom_clock_nextval();
            """
        )
        schema = f"simsom_stale_clock_{uuid.uuid4().hex[:8]}"
        settings = DatabaseSettings(
            dsn=self.test_db,
            schema=schema,
            clock_extension_name="simsom_clock",
        )
        controller = PostgresControllerDatabase(settings)

        try:
            controller.setup_schema()

            clock_state = controller.fetch_json(
                f"""
                SELECT json_build_object(
                    'nextval_exists',
                        EXISTS (
                            SELECT 1
                            FROM pg_proc p
                            JOIN pg_namespace n ON n.oid = p.pronamespace
                            WHERE n.nspname = {controller._sql_text(schema)}
                                AND p.proname = 'simsom_clock_nextval'
                        ),
                    'timestamp_default',
                        (
                            SELECT pg_get_expr(defaults.adbin, defaults.adrelid)
                            FROM pg_attrdef defaults
                            JOIN pg_class table_info
                                ON table_info.oid = defaults.adrelid
                            JOIN pg_namespace table_schema
                                ON table_schema.oid = table_info.relnamespace
                            JOIN pg_attribute column_info
                                ON column_info.attrelid = table_info.oid
                                AND column_info.attnum = defaults.adnum
                            WHERE table_schema.nspname = {controller._sql_text(schema)}
                                AND table_info.relname = 'action'
                                AND column_info.attname = 'timestamp'
                        )
                );
                """
            )

            self.assertTrue(clock_state["nextval_exists"])
            self.assertEqual(
                clock_state["timestamp_default"],
                f"{schema}.simsom_clock_nextval()",
            )
        finally:
            controller.execute_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE;')

    def test_generated_gaps_loosely_fit_exponential_distribution(self) -> None:
        """
        Loose statistical smoke test for the exponential clock.

        This is not a proof. It is meant to catch obvious scaling or sampling
        bugs while staying tolerant of randomness and integer-millisecond
        rounding.
        """
        sample_size = int(os.environ.get("SIMSOM_CLOCK_DISTRIBUTION_SAMPLE_SIZE", "10000"))
        lambda_value = 0.0009977140112913797

        results = self._run_sql(
            f"""
            DO $$
            BEGIN
                PERFORM simsom_clock_configure_exponential(
                    p_num_users => 10000,
                    p_origin_timestamp => timestamp '2020-01-01 00:00:00'
                );
            END
            $$;

            WITH generated AS (
                SELECT
                    row_number() OVER () AS rn,
                    simsom_clock_nextval() AS tick_ms
                FROM generate_series(1, {sample_size})
            ),
            gaps AS (
                SELECT
                    tick_ms - lag(tick_ms) OVER (ORDER BY rn) AS gap_ms
                FROM generated
            ),
            clean_gaps AS (
                SELECT gap_ms::double precision AS gap_ms
                FROM gaps
                WHERE gap_ms IS NOT NULL
            ),
            stats AS (
                SELECT
                    count(*) AS n,
                    avg(gap_ms) AS mean_gap_ms,
                    stddev_samp(gap_ms) AS stddev_gap_ms,
                    percentile_cont(0.50) WITHIN GROUP (ORDER BY gap_ms) AS median_gap_ms,
                    percentile_cont(0.90) WITHIN GROUP (ORDER BY gap_ms) AS p90_gap_ms,
                    percentile_cont(0.99) WITHIN GROUP (ORDER BY gap_ms) AS p99_gap_ms,
                    avg((gap_ms <= 250)::int)::double precision AS cdf_250,
                    avg((gap_ms <= 500)::int)::double precision AS cdf_500,
                    avg((gap_ms <= 1000)::int)::double precision AS cdf_1000,
                    avg((gap_ms <= 2000)::int)::double precision AS cdf_2000
                FROM clean_gaps
            )
            SELECT
                n,
                mean_gap_ms,
                stddev_gap_ms,
                stddev_gap_ms / mean_gap_ms AS coefficient_of_variation,
                median_gap_ms,
                p90_gap_ms,
                p99_gap_ms,
                cdf_250,
                cdf_500,
                cdf_1000,
                cdf_2000
            FROM stats;
            """
        )

        (
            n_text,
            mean_gap_text,
            stddev_gap_text,
            cv_text,
            median_gap_text,
            p90_gap_text,
            p99_gap_text,
            cdf_250_text,
            cdf_500_text,
            cdf_1000_text,
            cdf_2000_text,
        ) = results.split("|")

        n = int(n_text)
        mean_gap = float(mean_gap_text)
        stddev_gap = float(stddev_gap_text)
        coefficient_of_variation = float(cv_text)
        median_gap = float(median_gap_text)
        p90_gap = float(p90_gap_text)
        p99_gap = float(p99_gap_text)
        cdf_250 = float(cdf_250_text)
        cdf_500 = float(cdf_500_text)
        cdf_1000 = float(cdf_1000_text)
        cdf_2000 = float(cdf_2000_text)

        expected_mean = 1.0 / lambda_value
        expected_median = math.log(2) / lambda_value
        expected_p90 = -math.log(0.10) / lambda_value
        expected_p99 = -math.log(0.01) / lambda_value

        def expected_cdf(x: float) -> float:
            return 1.0 - math.exp(-lambda_value * x)

        self.assertEqual(n, sample_size - 1)
        self.assertAlmostEqual(mean_gap, expected_mean, delta=expected_mean * 0.12)
        self.assertAlmostEqual(stddev_gap, expected_mean, delta=expected_mean * 0.18)
        self.assertAlmostEqual(coefficient_of_variation, 1.0, delta=0.18)
        self.assertAlmostEqual(median_gap, expected_median, delta=expected_median * 0.18)
        self.assertAlmostEqual(p90_gap, expected_p90, delta=expected_p90 * 0.20)
        self.assertAlmostEqual(p99_gap, expected_p99, delta=expected_p99 * 0.28)
        self.assertAlmostEqual(cdf_250, expected_cdf(250.0), delta=0.04)
        self.assertAlmostEqual(cdf_500, expected_cdf(500.0), delta=0.05)
        self.assertAlmostEqual(cdf_1000, expected_cdf(1000.0), delta=0.05)
        self.assertAlmostEqual(cdf_2000, expected_cdf(2000.0), delta=0.05)


if __name__ == "__main__":
    unittest.main()
