"""Contract tests for the dashboard's editable simulator configuration."""

from copy import deepcopy
import gzip
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from config_helper import _validate_required_config  # noqa: E402
from web.api_server import (
    AnalyticsRepository,
    DatabaseNotReadyError,
    build_run_config,
    create_app,
    editable_config_defaults,
)


class WebConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.defaults = editable_config_defaults()

    def test_editable_defaults_use_current_simulator_fields(self) -> None:
        self.assertIn("idle_sleep_milliseconds", self.defaults["worker"])
        self.assertNotIn("idle_sleep_seconds", self.defaults["worker"])
        self.assertEqual(
            self.defaults["analytics"]["evaluation_interval"],
            {"unit": "simulated_hours", "value": 1},
        )
        self.assertEqual(
            self.defaults["recsys"]["inventory_composition"],
            {"out_of_network_ratio": 0.5, "size_multiplier": 3},
        )
        self.assertNotIn("inventory_size_factor", self.defaults["recsys"])
        self.assertEqual(
            set(self.defaults["termination"]["criteria_functions"]),
            {
                "max_avg_actions_per_user",
                "exponential_moving_average_action_quality",
            },
        )

    def test_build_run_config_accepts_current_editable_defaults(self) -> None:
        config = build_run_config(self.defaults)

        _validate_required_config(config)
        self.assertEqual(config["worker"]["idle_sleep_milliseconds"], 0)
        self.assertEqual(
            config["policy"]["rule_functions"]["below_quality_threshold"]["threshold"],
            0.3,
        )

    def test_criteria_override_replaces_default_mapping(self) -> None:
        overrides = deepcopy(self.defaults)
        overrides["termination"]["criteria_functions"] = {
            "max_avg_actions_per_user": {"threshold": 5.0},
        }

        config = build_run_config(overrides)

        self.assertEqual(
            config["termination"]["criteria_functions"],
            {"max_avg_actions_per_user": {"threshold": 5.0}},
        )

    def test_at_least_one_termination_criterion_is_required(self) -> None:
        overrides = deepcopy(self.defaults)
        overrides["termination"]["criteria_functions"] = {}

        with self.assertRaisesRegex(ValueError, "At least one termination criterion"):
            build_run_config(overrides)

    def test_invalid_analytics_unit_is_rejected(self) -> None:
        overrides = deepcopy(self.defaults)
        overrides["analytics"]["evaluation_interval"]["unit"] = "seconds"

        with self.assertRaisesRegex(ValueError, "Analytics evaluation unit"):
            build_run_config(overrides)

    def test_results_query_uses_current_convergence_function_name(self) -> None:
        sql = AnalyticsRepository()._results_sql()

        self.assertIn("exponential_moving_average_action_quality", sql)
        self.assertIn("currentSimulatorTimestampMs", sql)
        self.assertIn('"simsom_clock_state"', sql)
        self.assertNotIn(
            "exponential_moving_average_action_quality_threshold0_01_rho0_8",
            sql,
        )

    def test_network_query_groups_interactions_and_calculates_weighted_degree(self) -> None:
        sql = AnalyticsRepository()._network_rows_sql(100, None)

        self.assertIn('PARTITION BY source, target, "actionType"', sql)
        self.assertIn('COUNT(*) OVER interaction_group AS "interactionCount"', sql)

        self.assertIn('interaction_details AS MATERIALIZED', sql)
        self.assertIn("'contentModerationActions', \"contentModerationActions\"", sql)
        self.assertIn('details.interactions', sql)
        self.assertIn("WHERE action.parent_action_id IS NOT NULL", sql)
        self.assertIn('FROM grouped_interactions', sql)
        self.assertIn('SUM(interaction_count)::BIGINT AS weighted_degree', sql)
        self.assertIn(
            'COALESCE(node_strengths.weighted_degree, 0) AS "weightedDegree"',
            sql,
        )

    def test_network_metadata_distinguishes_weighted_edges_from_interactions(self) -> None:
        repository = AnalyticsRepository()
        # Actions can arrive between the filtering preflight and the rows query.
        stats = {
            "available": True,
            "meta": {"userCount": 2, "interactionCount": 3},
            "userIds": [1, 2],
        }
        rows = {
            "available": True,
            "meta": {"interactionCount": 4},
            "nodes": [{"id": 1}, {"id": 2}],
            "edges": [
                {"id": 10, "interactionCount": 3},
                {"id": 12, "interactionCount": 1},
            ],
        }

        with patch.object(repository, "_fetch_json", side_effect=[stats, rows]):
            payload = repository.fetch_network(edge_limit=100, node_limit=100)

        self.assertEqual(payload["meta"]["returnedEdgeCount"], 2)
        self.assertEqual(payload["meta"]["returnedInteractionCount"], 4)
        self.assertEqual(payload["meta"]["interactionCount"], 4)


    def test_live_network_uses_current_active_users_when_interactions_fit(self) -> None:
        repository = AnalyticsRepository()
        stats = {
            "available": True,
            "meta": {"userCount": 100, "interactionCount": 3},
            "userIds": list(range(1, 101)),
        }
        rows = {
            "available": True,
            "meta": {"interactionCount": 4},
            "nodes": [{"id": 1}, {"id": 2}],
            "edges": [{"id": 10, "interactionCount": 4}],
        }

        with patch.object(repository, "_fetch_json", side_effect=[stats, rows]) as fetch, patch.object(
            repository, "_python_k_core"
        ) as core:
            payload = repository.fetch_network(edge_limit=10, node_limit=10)

        core.assert_not_called()
        self.assertIn("node_strengths.user_id IS NOT NULL", fetch.call_args_list[1].args[0])
        self.assertEqual(payload["meta"]["kCoreLevel"], 1)
        self.assertEqual(payload["meta"]["interactionCount"], 4)
        self.assertEqual(payload["meta"]["returnedInteractionCount"], 4)



class DatabaseReadinessTests(unittest.TestCase):
    def test_results_send_sql_to_psql_through_standard_input(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        repository = AnalyticsRepository()

        with patch.object(repository, "_resolve_psql", return_value="psql"), patch(
            "web.api_server.subprocess.run", return_value=completed
        ) as run:
            repository.fetch()

        command = run.call_args.args[0]
        self.assertNotIn("--command", command)
        self.assertEqual(run.call_args.kwargs["input"], repository._results_sql())

    def test_results_treat_uninitialized_schema_as_waiting(self) -> None:
        completed = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                'ERROR: relation "simsom.simsom_clock_state" does not exist\n'
                'LINE 6: FROM "simsom"."simsom_clock_state"'
            ),
        )
        repository = AnalyticsRepository()

        with patch.object(repository, "_resolve_psql", return_value="psql"), patch(
            "web.api_server.subprocess.run", return_value=completed
        ):
            results = repository.fetch()

        self.assertFalse(results["available"])
        self.assertEqual(results["series"], [])
        self.assertEqual(
            results["message"],
            "Analytics will appear after the simulation initializes the database.",
        )
        self.assertNotIn("ERROR", results["message"])

    def test_readiness_reports_installed_clock_extension(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=(
                '{"database":"postgres","serverVersion":"18.0",'
                '"currentUser":"tester","isSuperuser":true,'
                '"clockExtension":"simsom_clock",'
                '"clockExtensionAvailable":true}\n'
            ),
            stderr="",
        )
        repository = AnalyticsRepository()

        with patch.object(repository, "_resolve_psql", return_value="psql"), patch(
            "web.api_server.subprocess.run", return_value=completed
        ):
            readiness = repository.readiness()

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(readiness["clockExtension"], "simsom_clock")

    def test_readiness_explains_when_postgres_is_unreachable(self) -> None:
        completed = SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="connection to server failed",
        )
        repository = AnalyticsRepository()

        with patch.object(repository, "_resolve_psql", return_value="psql"), patch(
            "web.api_server.subprocess.run", return_value=completed
        ):
            readiness = repository.readiness()

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "database_unreachable")
        self.assertIn("brew services start postgresql@18", readiness["message"])

    def test_readiness_explains_when_clock_extension_is_missing(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=(
                '{"database":"postgres","serverVersion":"18.0",'
                '"currentUser":"tester","isSuperuser":true,'
                '"clockExtension":"simsom_clock",'
                '"clockExtensionAvailable":false}\n'
            ),
            stderr="",
        )
        repository = AnalyticsRepository()

        with patch.object(repository, "_resolve_psql", return_value="psql"), patch(
            "web.api_server.subprocess.run", return_value=completed
        ):
            readiness = repository.readiness()

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "clock_extension_unavailable")
        self.assertIn("matching pg_config", readiness["message"])

    def test_readiness_requires_role_that_can_recreate_extension(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=(
                '{"database":"postgres","serverVersion":"18.0",'
                '"currentUser":"simulator","isSuperuser":false,'
                '"clockExtension":"simsom_clock",'
                '"clockExtensionAvailable":true}\n'
            ),
            stderr="",
        )
        repository = AnalyticsRepository()

        with patch.object(repository, "_resolve_psql", return_value="psql"), patch(
            "web.api_server.subprocess.run", return_value=completed
        ):
            readiness = repository.readiness()

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "insufficient_database_privileges")
        self.assertIn("simulator", readiness["message"])

    def test_database_export_gzips_sql_with_clock_state(self) -> None:
        dumped = SimpleNamespace(returncode=0, stdout="", stderr="")
        clock_state = SimpleNamespace(
            returncode=0,
            stdout='INSERT INTO "simsom"."simsom_clock_state" SELECT 1;\n',
            stderr="",
        )
        repository = AnalyticsRepository()

        with patch.object(
            repository, "_resolve_pg_dump", return_value="pg_dump"
        ), patch.object(
            repository, "_resolve_psql", return_value="psql"
        ), patch(
            "web.api_server.subprocess.run", side_effect=[dumped, clock_state]
        ) as run:
            output_path = repository.create_database_sql_export()

        try:
            command = run.call_args_list[0].args[0]
            self.assertIn("--format=plain", command)
            self.assertIn("--no-owner", command)
            self.assertIn("--no-privileges", command)
            self.assertEqual(command[command.index("--schema") + 1], repository.schema)
            self.assertEqual(
                command[command.index("--extension") + 1],
                repository.clock_extension,
            )
            self.assertIn(
                "jsonb_populate_record", run.call_args_list[1].args[0][-1]
            )
            self.assertTrue(output_path.name.endswith(".sql.gz"))
            self.assertFalse(Path(command[command.index("--file") + 1]).exists())
            with gzip.open(output_path, "rt", encoding="utf-8") as exported:
                self.assertIn(clock_state.stdout, exported.read())
        finally:
            output_path.unlink(missing_ok=True)

    def test_follower_network_export_streams_complete_csv_from_postgres(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout=None, stderr="")
        repository = AnalyticsRepository()

        with patch.object(
            repository, "_resolve_psql", return_value="psql"
        ), patch("web.api_server.subprocess.run", return_value=completed) as run:
            output_path = repository.create_follower_network_export()

        try:
            self.assertIn("SELECT user_id, friend_id", run.call_args.kwargs["input"])
            self.assertIn("FORMAT CSV, HEADER TRUE", run.call_args.kwargs["input"])
            self.assertIn('"simsom"."follower_network"', run.call_args.kwargs["input"])
            self.assertEqual(output_path.suffix, ".csv")
            self.assertNotIn("capture_output", run.call_args.kwargs)
        finally:
            output_path.unlink(missing_ok=True)

    def test_export_routes_stream_server_generated_files(self) -> None:
        class StubRuns:
            def status(self):
                return {"status": "completed"}

        class StubAnalytics:
            def create_database_sql_export(self):
                with tempfile.NamedTemporaryFile(delete=False, suffix=".sql.gz") as output:
                    output.write(gzip.compress(b"CREATE EXTENSION simsom_clock;\n"))
                    return Path(output.name)

            def create_follower_network_export(self):
                with tempfile.NamedTemporaryFile(
                    delete=False,
                    suffix=".csv",
                    mode="w",
                    encoding="utf-8",
                ) as output:
                    output.write("user_id,friend_id\n1,2\n")
                    return Path(output.name)

        client = create_app(
            run_manager=StubRuns(),
            analytics_repository=StubAnalytics(),
        ).test_client()

        sql_response = client.get("/api/exports/database.sql.gz")
        follower_response = client.get("/api/exports/follower-network.csv")
        try:
            self.assertEqual(sql_response.status_code, 200)
            self.assertEqual(sql_response.mimetype, "application/gzip")
            self.assertEqual(
                gzip.decompress(sql_response.data),
                b"CREATE EXTENSION simsom_clock;\n",
            )
            self.assertIn(
                "database.sql.gz",
                sql_response.headers["Content-Disposition"],
            )
            self.assertEqual(follower_response.status_code, 200)
            self.assertEqual(follower_response.mimetype, "text/csv")
            self.assertEqual(
                follower_response.data,
                b"user_id,friend_id\n1,2\n",
            )
        finally:
            sql_response.close()
            follower_response.close()

    def test_start_route_rejects_run_when_database_is_not_ready(self) -> None:
        class StubRuns:
            started = False

            def start(self, **_kwargs):
                self.started = True
                return {"status": "running"}

            def status(self):
                return {"status": "idle"}

        class StubAnalytics:
            def readiness(self):
                return {
                    "ready": False,
                    "status": "database_unreachable",
                    "message": "PostgreSQL is unavailable",
                }

            def require_ready(self):
                raise DatabaseNotReadyError("PostgreSQL is unavailable")

        runs = StubRuns()
        client = create_app(
            run_manager=runs,
            analytics_repository=StubAnalytics(),
        ).test_client()

        response = client.post("/api/runs", json={})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "PostgreSQL is unavailable")
        self.assertFalse(runs.started)

    def test_health_route_includes_database_readiness(self) -> None:
        class StubRuns:
            def status(self):
                return {"status": "idle"}

        class StubAnalytics:
            def readiness(self):
                return {"ready": True, "status": "ready"}

        client = create_app(
            run_manager=StubRuns(),
            analytics_repository=StubAnalytics(),
        ).test_client()

        response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "ok": True,
                "ready": True,
                "database": {"ready": True, "status": "ready"},
            },
        )


if __name__ == "__main__":
    unittest.main()
