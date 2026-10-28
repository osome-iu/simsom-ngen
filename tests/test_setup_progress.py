# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Tests for the controller-owned setup JSONL progress stream."""

import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from setup_progress import SetupProgressReporter  # noqa: E402


class CapturingLogger:
    """Capture formatted stage-start messages from the parent process."""

    def __init__(self):
        self.messages = []

    def info(self, message, *args):
        self.messages.append(message % args if args else message)


class SetupProgressReporterTests(unittest.TestCase):
    """Validate setup history, heartbeats, and complete JSONL writes."""

    def test_records_transitions_percent_changes_and_heartbeats(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "setup_status.jsonl"
            logger = CapturingLogger()
            reporter = SetupProgressReporter(
                path,
                logger=logger,
                heartbeat_seconds=0.02,
            )
            try:
                reporter.begin(
                    "network_generation",
                    progress_total=10,
                    progress_unit="source_users",
                    phase="within_type_networking",
                    phase_edges=0,
                    total_edges=0,
                )
                reporter.update(
                    progress_current=5,
                    phase_edges=12,
                    total_edges=12,
                )
                deadline = time.monotonic() + 0.5
                while (
                    not path.exists()
                    or len(path.read_text(encoding="utf-8").splitlines()) < 3
                ):
                    if time.monotonic() >= deadline:
                        self.fail("setup heartbeat was not written")
                    time.sleep(0.005)
                reporter.complete(phase_edges=20, total_edges=20)
            finally:
                reporter.close()

            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            stage_events = [json.loads(message) for message in logger.messages]

        self.assertEqual(records[0]["status"], "running")
        self.assertEqual(records[0]["update_type"], "stage_started")
        self.assertEqual(records[1]["progress_percent"], 50)
        self.assertEqual(records[1]["update_type"], "progress")
        self.assertEqual(records[1]["phase_edges"], 12)
        self.assertIn("heartbeat", {record["update_type"] for record in records})
        self.assertEqual(records[-1]["status"], "complete")
        self.assertEqual(records[-1]["update_type"], "stage_completed")
        self.assertEqual(records[-1]["progress_percent"], 100)
        self.assertEqual(
            [record["sequence"] for record in records],
            list(range(1, len(records) + 1)),
        )
        self.assertEqual(len(stage_events), 1)
        self.assertEqual(stage_events[0]["event"], "setup_stage_started")
        self.assertEqual(stage_events[0]["stage"], "network_generation")
        self.assertEqual(stage_events[0]["phase"], "within_type_networking")
        self.assertFalse(Path(stage_events[0]["setup_status_file"]).is_absolute())

    def test_failure_preserves_the_current_stage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "setup_status.jsonl"
            reporter = SetupProgressReporter(path)
            reporter.begin("database_schema")
            reporter.fail(RuntimeError("schema failed"))
            reporter.close()
            final_record = json.loads(
                path.read_text(encoding="utf-8").splitlines()[-1]
            )

        self.assertEqual(final_record["stage"], "database_schema")
        self.assertEqual(final_record["status"], "failed")
        self.assertEqual(final_record["error_type"], "RuntimeError")

    def test_sub_percent_progress_remains_visible_for_billion_item_stage(self):
        """Large totals should not round an advancing counter back to zero percent."""
        with tempfile.TemporaryDirectory() as temp_dir:
            reporter = SetupProgressReporter(
                Path(temp_dir) / "setup_status.jsonl",
                heartbeat_seconds=0.02,
            )
            try:
                reporter.begin("user_generation", progress_total=1_000_000_000)
                reporter.update(progress_current=1_000)
                percent = reporter._state["progress_percent"]
            finally:
                reporter.close()

        self.assertEqual(percent, 0.0001)

    def test_heartbeat_continues_while_parent_python_cannot_schedule_threads(self):
        """Heartbeat ownership must be independent of the controller interpreter."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "setup_status.jsonl"
            reporter = SetupProgressReporter(path, heartbeat_seconds=0.02)
            try:
                reporter.begin("user_generation", step="user_type_assignment")
                deadline = time.monotonic() + 0.5
                while not path.exists() or not path.read_text(encoding="utf-8"):
                    if time.monotonic() >= deadline:
                        self.fail("setup writer did not publish its initial state")
                    time.sleep(0.005)

                previous_switch_interval = sys.getswitchinterval()
                sys.setswitchinterval(0.25)
                try:
                    blocked_until = time.monotonic() + 0.12
                    while time.monotonic() < blocked_until:
                        pass
                finally:
                    sys.setswitchinterval(previous_switch_interval)
            finally:
                reporter.close()

            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertGreaterEqual(
            sum(record["update_type"] == "heartbeat" for record in records),
            3,
        )

    def test_manual_interrupt_is_recorded_before_writer_closes(self):
        """The writer should outlive SIGINT long enough to record setup failure."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "setup_status.jsonl"
            reporter = SetupProgressReporter(path, heartbeat_seconds=0.02)
            try:
                reporter.begin("user_generation", step="profile_generation")
                deadline = time.monotonic() + 0.5
                while not path.exists() or not path.read_text(encoding="utf-8"):
                    if time.monotonic() >= deadline:
                        self.fail("setup writer did not publish its initial state")
                    time.sleep(0.005)
                reporter._process.send_signal(signal.SIGINT)
                reporter.fail(KeyboardInterrupt())
            finally:
                reporter.close()

            final_record = json.loads(
                path.read_text(encoding="utf-8").splitlines()[-1]
            )

        self.assertEqual(final_record["status"], "failed")
        self.assertEqual(final_record["error_type"], "KeyboardInterrupt")


if __name__ == "__main__":
    unittest.main()
