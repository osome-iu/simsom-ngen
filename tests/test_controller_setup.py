# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Unit tests for controller-owned schema setup and initial-state population."""

from datetime import datetime, timezone
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from mpi4py import MPI
from unittest.mock import patch


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from components.controller import (  # noqa: E402
    coordinate_process_shutdown,
    controller_termination_reason,
    release_processes_after_setup,
    setup_database,
    wait_for_termination,
)
from db.controller import (  # noqa: E402
    InitialStateCounts,
    PostgresControllerDatabase,
    SetupDbDataset,
    SetupDbStream,
)
from db.core import (  # noqa: E402
    DatabaseSettings,
    FollowerNetworkRecord,
    TerminationState,
    UserRecord,
)
from mpi_layout import MpiLayout, Role  # noqa: E402
from main import _wait_for_controller_setup  # noqa: E402
from mpi_protocol import MpiMessage  # noqa: E402


class RecordingControllerDatabase(PostgresControllerDatabase):
    """Controller DB test double that records psql and pg_dump calls."""

    def __init__(self, psql_output=None):
        """Initialize a controller DB client with unit-test settings."""
        super().__init__(
            DatabaseSettings(
                schema="unit_test",
                clock_extension_name="simsom_clock",
            )
        )
        self.psql_calls = []
        self.dump_calls = []
        self.psql_output = psql_output
        self.psql_streams = []

    def run_psql(self, sql: str, *, capture_output: bool = False):
        """Record SQL instead of executing psql."""
        self.psql_calls.append((sql, capture_output))
        if capture_output:
            return self.psql_output
        return None

    def run_psql_stream(self, sql_parts):
        """Consume and record streamed SQL without launching psql."""
        self.psql_streams.append(list(sql_parts))

    def run_pg_dump(self, output_path, *, schema=None):
        """Record dump requests instead of running pg_dump."""
        self.dump_calls.append((output_path, schema))
        return output_path


class ControllerSetupTests(unittest.TestCase):
    """Validate controller DB setup and population behavior."""

    def test_setup_dataset_rejects_invalid_user_rows(self):
        """SetupDbDataset should reject non-UserRecord users."""
        with self.assertRaises(TypeError):
            SetupDbDataset(users=[object()])

    def test_setup_release_waits_on_nonblocking_barrier(self):
        """Controller setup release should retain barrier synchronization."""

        class BarrierRequest:
            waited = False

            def Wait(self):
                self.waited = True

        request = BarrierRequest()
        comm_world = SimpleNamespace(Ibarrier=lambda: request)

        release_processes_after_setup(SimpleNamespace(comm_world=comm_world))

        self.assertTrue(request.waited)

    def test_non_controller_setup_wait_sleeps_between_barrier_checks(self):
        """Waiting ranks should not continuously spin before setup release."""

        class BarrierRequest:
            def __init__(self):
                self.results = iter((False, False, True))

            def Test(self):
                return next(self.results)

        comm_world = SimpleNamespace(Ibarrier=lambda: BarrierRequest())

        with patch("main.time.sleep") as sleep:
            _wait_for_controller_setup(comm_world)

        self.assertEqual(sleep.call_count, 2)

    def test_populate_initial_state_rejects_invalid_dataset(self):
        """populate_initial_state should reject non-SetupDbDataset input."""
        database = RecordingControllerDatabase()

        with self.assertRaises(TypeError):
            database.populate_initial_state(object())

        self.assertEqual(database.psql_calls, [])

    def test_schema_setup_is_one_fresh_bootstrap_without_secondary_indexes(self):
        """Setup should recreate tables once and defer secondary indexes."""
        database = RecordingControllerDatabase()

        database.setup_schema()

        self.assertEqual(len(database.psql_calls), 1)
        schema_sql = database.psql_calls[0][0]
        self.assertIn("DROP TABLE IF EXISTS", schema_sql)
        self.assertNotIn("CREATE INDEX", schema_sql)

    def test_setup_database_logs_clock_extension_details(self):
        """Controller setup should log clock install and run configuration steps."""

        class CapturingDatabase:
            def __init__(self):
                self.calls = []

            def setup_schema(self):
                self.calls.append("setup_schema")

            def populate_initial_state(
                self,
                dataset,
                *,
                started_at=None,
                progress=None,
            ):
                self.calls.append(("populate_initial_state", dataset, started_at))
                return InitialStateCounts(
                    users=len(dataset.users),
                    follower_edges=len(dataset.follower_network),
                )

        class CapturingLogger:
            def __init__(self):
                self.messages = []

            def info(self, message, *args):
                self.messages.append(message % args if args else message)

        database = CapturingDatabase()
        logger = CapturingLogger()
        started_at = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        dataset = SetupDbDataset(users=[UserRecord(user_id=1, user_type="normal")])
        database_config = SimpleNamespace(
            schema="simsom",
            clock_extension=SimpleNamespace(
                name="simsom_clock",
            ),
        )

        setup_database(
            database,
            dataset,
            started_at=started_at,
            logger=logger,
            database_config=database_config,
        )

        self.assertEqual(database.calls[0], "setup_schema")
        self.assertEqual(database.calls[1], ("populate_initial_state", dataset, started_at))
        self.assertEqual(
            logger.messages,
            [
                "Clock extension bootstrap starting: extension=simsom_clock "
                "schema=simsom default_function=simsom.simsom_clock_nextval",
                "Clock extension bootstrap complete: extension=simsom_clock "
                "recreated; action.timestamp default wired to "
                "simsom.simsom_clock_nextval",
                "Clock extension run configuration starting: "
                "function=simsom_clock_configure_exponential user_count=1 "
                "tick_reset_ms=0 min_step_ms=1",
                "Clock extension run configuration complete: "
                "function=simsom_clock_configure_exponential user_count=1 "
                "current_tick_ms=0",
            ],
        )

    def test_populate_initial_state_streams_one_transaction_with_row_progress(self):
        """Initial-state loading should stream chunks and report rows sent."""
        database = RecordingControllerDatabase()
        dataset = SetupDbDataset(
            users=[UserRecord(user_id=1, user_type="normal")],
        )
        progress = []

        database.populate_initial_state(
            dataset,
            progress=lambda table, current, total: progress.append(
                (table, current, total)
            ),
        )

        statements = database.psql_streams[0]
        self.assertEqual(statements[0], "BEGIN;")
        self.assertEqual(statements[-1], "COMMIT;")
        self.assertTrue(any("COPY" in statement for statement in statements))
        self.assertFalse(any("COPY" in statement and '"action"' in statement
                             for statement in statements))
        index_position = next(
            index
            for index, statement in enumerate(statements)
            if "CREATE INDEX" in statement
        )
        analyze_position = next(
            index
            for index, statement in enumerate(statements)
            if 'ANALYZE "unit_test"."user"' in statement
        )
        copy_end_positions = [
            index for index, statement in enumerate(statements) if statement == r"\."
        ]
        self.assertEqual(len(copy_end_positions), 2)
        self.assertGreater(index_position, max(copy_end_positions))
        self.assertGreater(analyze_position, index_position)
        self.assertIn(
            'ANALYZE "unit_test"."follower_network"',
            statements[analyze_position],
        )
        self.assertIn(
            'ANALYZE "unit_test"."action"',
            statements[analyze_position],
        )
        self.assertFalse(any("TRUNCATE TABLE" in statement for statement in statements))
        self.assertEqual(progress, [("user", 1, 1), ("indexes", 1, 1)])

    def test_populate_initial_state_consumes_one_pass_user_stream(self):
        """The production input shape should not require a materialized user list."""
        database = RecordingControllerDatabase()
        stream = SetupDbStream(
            user_count=2,
            users=(
                UserRecord(user_id=user_id, user_type="normal")
                for user_id in (1, 2)
            ),
            follower_network=(),
        )

        counts = database.populate_initial_state(stream)

        self.assertEqual(counts.users, 2)
        self.assertEqual(counts.follower_edges, 0)
        self.assertTrue(
            any("1\t[]\t0.0\tnormal" in part for part in database.psql_streams[0])
        )

    def test_populate_initial_state_copies_maximum_integer_friend_ids(self):
        """Friend rows should use COPY with the maximum valid user ID."""
        database = RecordingControllerDatabase()
        large_user_id = 2_147_483_647
        stream = SetupDbStream(
            user_count=2,
            users=(
                UserRecord(user_id=user_id, user_type="normal")
                for user_id in (1, large_user_id)
            ),
            follower_network=(
                FollowerNetworkRecord(
                    user_id=large_user_id,
                    friend_id=1,
                ),
            ),
        )

        counts = database.populate_initial_state(stream)
        statements = database.psql_streams[0]

        self.assertEqual(counts.follower_edges, 1)
        self.assertTrue(
            any(
                statement.startswith('COPY "unit_test"."follower_network"')
                for statement in statements
            )
        )
        self.assertIn(f"{large_user_id}\t1", statements)

    def test_request_termination_is_controller_owned(self):
        """Controller DB client should write the lifecycle stop request."""
        database = RecordingControllerDatabase()

        database.request_termination("done")

        self.assertFalse(database.psql_calls[0][1])

    def test_controller_termination_reason_does_not_poll_avg_actions(self):
        """Controller should rely on analytics-published stop signals."""

        reason = controller_termination_reason()

        self.assertIsNone(reason)

    def test_controller_termination_reason_uses_analytics_stop_signal(self):
        """Controller should treat analytics lifecycle signals as stop requests."""

        reason = controller_termination_reason(
            lifecycle_state=TerminationState(
                analytics_stop_requested=True,
                analytics_stop_reason="stable metrics",
                analytics_termination_criteria=[
                    {"function_name": "quality", "met": True}
                ],
            ),
        )

        self.assertEqual(reason, "stable metrics")

    def test_wait_for_termination_records_controller_stop(self):
        """Controller wait loop should write the stop request for a signal."""

        class StoppingDatabase:
            def __init__(self):
                self.reasons = []

            def get_termination_state(self):
                return TerminationState(
                    should_terminate=bool(self.reasons),
                    reason=self.reasons[-1] if self.reasons else None,
                    analytics_stop_requested=True,
                    analytics_stop_reason="test termination",
                    analytics_termination_criteria=[
                        {
                            "met": True,
                            "function_name": "test_criterion",
                            "reason": "test termination",
                        }
                    ],
                )

            def request_termination(self, reason, *, termination_criteria=None):
                self.reasons.append(reason)
                self.criteria = termination_criteria

        database = StoppingDatabase()

        reason = wait_for_termination(
            database,
            poll_seconds=0,
        )

        self.assertEqual(reason, "test termination")
        self.assertEqual(database.reasons, [reason])
        self.assertEqual(database.criteria[0]["function_name"], "test_criterion")

    def test_wait_for_termination_honors_existing_stop_state(self):
        """Controller should return an existing lifecycle stop without rewriting."""

        class AlreadyStoppedDatabase:
            def __init__(self):
                self.requested = False

            def get_termination_state(self):
                return TerminationState(should_terminate=True, reason="manual")

            def request_termination(self, reason):
                self.requested = True

        database = AlreadyStoppedDatabase()

        self.assertEqual(
            wait_for_termination(
                database,
                poll_seconds=0,
            ),
            "manual",
        )
        self.assertFalse(database.requested)

    def test_shutdown_ignores_completion_from_disabled_policy(self):
        """An early disabled-policy completion must not block shutdown."""

        class Request:
            def wait(self):
                return None

        class FakeCommunicator:
            def __init__(self):
                self.sent = []
                self.incoming = [
                    (2, {"message": MpiMessage.PROCESS_TERMINATED.value}),
                    (
                        3,
                        {
                            "message": MpiMessage.PROCESS_TERMINATED.value,
                            "rank": 3,
                            "role": Role.WORKER.value,
                        },
                    ),
                    (
                        1,
                        {
                            "message": MpiMessage.ANALYTICS_READY_FOR_FINAL.value,
                            "rank": 1,
                            "role": Role.ANALYTICS.value,
                        },
                    ),
                    (
                        1,
                        {
                            "message": MpiMessage.FINAL_ANALYTICS_COMPLETE.value,
                            "rank": 1,
                            "role": Role.ANALYTICS.value,
                        },
                    ),
                ]

            def isend(self, message, dest):
                self.sent.append((dest, message))
                return Request()

            def iprobe(self, source, status):
                if not self.incoming:
                    return False
                next_source, _ = self.incoming[0]
                if source not in (next_source, MPI.ANY_SOURCE):
                    return False
                status.Set_source(next_source)
                return True

            def recv(self, source):
                next_source, message = self.incoming.pop(0)
                self.assert_source(next_source, source)
                return message

            @staticmethod
            def assert_source(expected, actual):
                if expected != actual:
                    raise AssertionError(
                        f"expected source {expected}, received {actual}"
                    )

        context = SimpleNamespace(
            comm_world=FakeCommunicator(),
            rank=0,
            size=4,
            mpi_layout=MpiLayout(
                role_ranks={
                    Role.CONTROLLER: 0,
                    Role.ANALYTICS: 1,
                    Role.POLICY: 2,
                },
                worker_start_rank=3,
            ),
        )

        coordinate_process_shutdown(
            context,
            reason="action limit",
            policy_enabled=False,
            timeout_seconds=1.0,
        )

        sent_destinations = [destination for destination, _ in context.comm_world.sent]
        self.assertEqual(sent_destinations, [1, 3, 1])
        self.assertEqual(
            [message for _, message in context.comm_world.sent],
            [
                {"message": MpiMessage.STOP_REQUEST.value, "reason": "action limit"},
                {"message": MpiMessage.STOP_REQUEST.value, "reason": "action limit"},
                {"message": MpiMessage.FINAL_ANALYSIS.value},
            ],
        )

    def test_shutdown_times_out_when_a_process_does_not_acknowledge(self):
        """The controller must fail instead of waiting forever for an ACK."""

        class Request:
            def wait(self):
                return None

        class SilentCommunicator:
            def __init__(self):
                self.sent = []

            def isend(self, message, dest):
                self.sent.append((dest, message))
                return Request()

            def iprobe(self, source, status):
                return False

        context = SimpleNamespace(
            comm_world=SilentCommunicator(),
            rank=0,
            size=3,
            mpi_layout=MpiLayout(
                role_ranks={
                    Role.CONTROLLER: 0,
                    Role.ANALYTICS: 1,
                    Role.POLICY: 2,
                },
                worker_start_rank=3,
            ),
        )

        with self.assertRaises(TimeoutError):
            coordinate_process_shutdown(
                context,
                reason="action limit",
                policy_enabled=True,
                timeout_seconds=0.0,
            )

    def test_shutdown_rejects_acknowledgment_with_wrong_role(self):
        """A process acknowledgment must identify the role at its MPI rank."""

        class Request:
            def wait(self):
                return None

        class WrongRoleCommunicator:
            def isend(self, _message, dest):
                del dest
                return Request()

            def iprobe(self, source, status):
                if source in (MPI.ANY_SOURCE, 3):
                    status.Set_source(3)
                    return True
                return False

            def recv(self, source):
                if source != 3:
                    raise AssertionError("controller received from wrong source")
                return {
                    "message": MpiMessage.PROCESS_TERMINATED.value,
                    "rank": 3,
                    "role": Role.ANALYTICS.value,
                }

        context = SimpleNamespace(
            comm_world=WrongRoleCommunicator(),
            rank=0,
            size=4,
            mpi_layout=MpiLayout(
                role_ranks={
                    Role.CONTROLLER: 0,
                    Role.ANALYTICS: 1,
                    Role.POLICY: 2,
                },
                worker_start_rank=3,
            ),
        )

        with self.assertRaisesRegex(ValueError, "must identify role"):
            coordinate_process_shutdown(
                context,
                reason="action limit",
                policy_enabled=False,
                timeout_seconds=1.0,
            )

    def test_dump_writes_to_output_directory(self):
        """Default SQL exports should share the run's output root."""
        database = RecordingControllerDatabase()
        with patch("db.controller.Path.cwd", return_value=Path("/simulation")):
            database.dump()

        self.assertEqual(len(database.dump_calls), 1)
        output_path, schema = database.dump_calls[0]
        self.assertEqual(output_path.parent, Path("/simulation/output"))
        self.assertRegex(output_path.name, r"^unit_test_dump_\d{8}_\d{6}\.sql$")
        self.assertEqual(schema, "unit_test")


if __name__ == "__main__":
    unittest.main()
