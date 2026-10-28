# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Tests for worker and policy shutdown acknowledgments."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from components.policy import run_policy  # noqa: E402
from components.worker import run_worker  # noqa: E402
from mpi_layout import Role  # noqa: E402
from mpi_protocol import MpiMessage  # noqa: E402


class _Request:
    """Minimal nonblocking-send handle for process protocol tests."""

    def wait(self):
        """Complete the fake send immediately."""


class _Database:
    """Database double that records when the connection closes."""

    def __init__(self, events):
        self.events = events

    def close(self):
        """Record connection closure."""
        self.events.append("close")


class ProcessShutdownTests(unittest.TestCase):
    """Verify that completed processes close DB state before acknowledging."""

    def test_worker_closes_database_before_process_acknowledgment(self):
        """Worker completion should acknowledge only after its DB closes."""
        events = []
        context = SimpleNamespace(
            config=SimpleNamespace(
                database=SimpleNamespace(),
                worker=SimpleNamespace(
                    batch_size=1,
                    idle_sleep_milliseconds=0.0,
                ),
                setup_db=SimpleNamespace(
                    user_types={"normal": {"num_nodes": 1, "lurker_ratio": 0.0}},
                ),
                recsys=SimpleNamespace(
                    feed_size=5,
                    inventory_composition=SimpleNamespace(
                        out_of_network_ratio=0.5,
                        size_multiplier=1,
                    ),
                ),
                user_behavior=SimpleNamespace(actions_per_session_ratio=0.2),
            ),
            comm_world=object(),
            mpi_layout=SimpleNamespace(rank=lambda _role: 0),
            rank=3,
            role=Role.WORKER,
        )

        def send(_comm, _destination, message, **_payload):
            events.append(message)
            return _Request()

        with patch(
            "components.worker.create_worker_database_client",
            return_value=_Database(events),
        ), patch("components.worker.DefaultFeedBuilder"), patch(
            "components.worker.DefaultUserExecutor"
        ), patch("components.worker.run_worker_loop"), patch(
            "components.worker.send_control_message",
            side_effect=send,
        ):
            run_worker(context)

        self.assertEqual(events, ["close", MpiMessage.PROCESS_TERMINATED])

    def test_policy_closes_database_before_process_acknowledgment(self):
        """Policy completion should acknowledge only after its DB closes."""
        events = []
        context = SimpleNamespace(
            config=SimpleNamespace(
                policy=SimpleNamespace(
                    enabled=True,
                    poll_interval_milliseconds=0.0,
                ),
                database=SimpleNamespace(),
            ),
            comm_world=object(),
            mpi_layout=SimpleNamespace(rank=lambda _role: 0),
            rank=2,
            role=Role.POLICY,
        )

        def send(_comm, _destination, message, **_payload):
            events.append(message)
            return _Request()

        with patch(
            "components.policy.create_policy_database_client",
            return_value=_Database(events),
        ), patch("components.policy.DefaultPolicyEvaluator"), patch(
            "components.policy.run_policy_loop"
        ), patch(
            "components.policy.send_control_message",
            side_effect=send,
        ):
            run_policy(context)

        self.assertEqual(events, ["close", MpiMessage.PROCESS_TERMINATED])


if __name__ == "__main__":
    unittest.main()
