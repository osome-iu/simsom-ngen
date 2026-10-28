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
import time
import unittest
import uuid


class BenchmarkClockExtensionTest(unittest.TestCase):
    """
    Integration tests for the benchmark-local PostgreSQL clock extension.

    This intentionally exercises the benchmark-only extension under
    analyses/research/psql_extension/benchmark/extension so experiments stay
    separate from libs/simsom/db/clock.
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
        cls.repo_root = Path(__file__).resolve().parents[4]
        cls.extension_dir = cls.repo_root / "analyses" / "research" / "psql_extension" / "benchmark" / "extension"

        cls._install_extension_under_test()
        cls._check_server()

        cls.test_db = f"benchmark_clock_test_{int(time.time())}_{uuid.uuid4().hex[:8]}"
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
            raise unittest.SkipTest(f"benchmark clock extension directory not found: {cls.extension_dir}")

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
            raise unittest.SkipTest(f"failed to build/install benchmark_clock under test: {message}") from exc

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
        self._run_sql("DROP EXTENSION IF EXISTS benchmark_clock CASCADE;")
        self._run_sql("CREATE EXTENSION benchmark_clock;")

    def test_configure_fixed_step_resets_state(self) -> None:
        row = self._run_sql(
            """
            SELECT current_action_id, current_tick_ms, distribution, fixed_step
            FROM benchmark_clock_configure_fixed_step(
                p_fixed_step => 1,
                p_start_action_id => 0,
                p_start_tick_ms => 0
            );
            """
        )

        action_id_text, tick_ms_text, distribution_text, fixed_step_text = row.split("|")
        self.assertEqual(int(action_id_text), 0)
        self.assertEqual(int(tick_ms_text), 0)
        self.assertEqual(distribution_text, "fixed_step")
        self.assertEqual(int(fixed_step_text), 1)

    def test_next_allocation_is_gapless_in_fixed_step_mode(self) -> None:
        results = self._run_sql(
            """
            DO $$
            BEGIN
                PERFORM benchmark_clock_configure_fixed_step(
                    p_fixed_step => 1,
                    p_start_action_id => 0,
                    p_start_tick_ms => 0
                );
            END
            $$;

            WITH generated AS (
                SELECT
                    (clock_allocation).action_id AS action_id,
                    (clock_allocation).timestamp AS timestamp
                FROM (
                    SELECT benchmark_clock_next_allocation() AS clock_allocation
                    FROM generate_series(1, 25)
                ) AS allocated_rows
            )
            SELECT
                bool_and(action_id = timestamp),
                bool_and(prev_action_id IS NULL OR action_id = prev_action_id + 1)
            FROM (
                SELECT
                    action_id,
                    timestamp,
                    lag(action_id) OVER (ORDER BY action_id) AS prev_action_id
                FROM generated
            ) AS checked;
            """
        )

        equal_text, gapless_text = results.split("|")
        self.assertEqual(equal_text, "t")
        self.assertEqual(gapless_text, "t")

    def test_trigger_path_assigns_both_fields_in_fixed_step_mode(self) -> None:
        results = self._run_sql(
            """
            DO $$
            BEGIN
                PERFORM benchmark_clock_configure_fixed_step(
                    p_fixed_step => 1,
                    p_start_action_id => 0,
                    p_start_tick_ms => 0
                );
            END
            $$;

            CREATE TABLE test_fixed_trigger_rows (
                action_id bigint NOT NULL,
                timestamp bigint NOT NULL,
                payload bigint NOT NULL
            );

            CREATE TRIGGER benchmark_clock_fill_action_fields
            BEFORE INSERT ON test_fixed_trigger_rows
            FOR EACH ROW
            EXECUTE FUNCTION benchmark_clock_assign_action_fields();

            INSERT INTO test_fixed_trigger_rows (payload)
            SELECT 0::bigint
            FROM generate_series(1, 10);

            SELECT
                count(*),
                bool_and(action_id = timestamp)
            FROM test_fixed_trigger_rows;
            """
        )

        row_count_text, equal_text = results.split("|")
        self.assertEqual(int(row_count_text), 10)
        self.assertEqual(equal_text, "t")

    def test_trigger_path_assigns_gapless_ids_and_increasing_exponential_timestamps(self) -> None:
        results = self._run_sql(
            """
            DO $$
            BEGIN
                PERFORM benchmark_clock_configure_exponential(
                    p_num_users => 10000,
                    p_start_action_id => 0,
                    p_start_tick_ms => 0
                );
            END
            $$;

            CREATE TABLE test_exponential_trigger_rows (
                action_id bigint NOT NULL,
                timestamp bigint NOT NULL,
                payload bigint NOT NULL
            );

            CREATE TRIGGER benchmark_clock_fill_action_fields
            BEFORE INSERT ON test_exponential_trigger_rows
            FOR EACH ROW
            EXECUTE FUNCTION benchmark_clock_assign_action_fields();

            INSERT INTO test_exponential_trigger_rows (payload)
            SELECT 0::bigint
            FROM generate_series(1, 25);

            WITH checked AS (
                SELECT
                    action_id,
                    lag(action_id) OVER (ORDER BY action_id) AS prev_action_id,
                    timestamp,
                    lag(timestamp) OVER (ORDER BY action_id) AS prev_timestamp
                FROM test_exponential_trigger_rows
            ),
            deltas AS (
                SELECT
                    action_id,
                    timestamp - prev_timestamp AS delta_ms
                FROM checked
            )
            SELECT
                bool_and(prev_action_id IS NULL OR action_id = prev_action_id + 1),
                bool_and(prev_timestamp IS NULL OR timestamp > prev_timestamp),
                min(delta_ms)
            FROM checked
            LEFT JOIN deltas USING (action_id)
            WHERE delta_ms IS NOT NULL;
            """
        )

        gapless_text, increasing_text, min_delta_text = results.split("|")
        self.assertEqual(gapless_text, "t")
        self.assertEqual(increasing_text, "t")
        self.assertGreaterEqual(int(min_delta_text), 1)

    def test_generated_gaps_loosely_fit_exponential_distribution(self) -> None:
        sample_size = int(os.environ.get("BENCHMARK_CLOCK_DISTRIBUTION_SAMPLE_SIZE", "10000"))
        lambda_value = 0.0009977140112913797

        results = self._run_sql(
            f"""
            DO $$
            BEGIN
                PERFORM benchmark_clock_configure_exponential(
                    p_num_users => 10000,
                    p_start_action_id => 0,
                    p_start_tick_ms => 0
                );
            END
            $$;

            WITH generated AS (
                SELECT
                    row_number() OVER () AS rn,
                    (clock_allocation).timestamp AS timestamp
                FROM (
                    SELECT benchmark_clock_next_allocation() AS clock_allocation
                    FROM generate_series(1, {sample_size})
                ) AS allocated_rows
            ),
            gaps AS (
                SELECT
                    timestamp - lag(timestamp) OVER (ORDER BY rn) AS gap_ms
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
