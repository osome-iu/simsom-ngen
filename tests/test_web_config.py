"""Contract tests for the dashboard's editable simulator configuration."""

from copy import deepcopy
from pathlib import Path
import sys
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
        self.assertNotIn(
            "exponential_moving_average_action_quality_threshold0_01_rho0_8",
            sql,
        )


class DatabaseReadinessTests(unittest.TestCase):
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
