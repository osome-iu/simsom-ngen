# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Tests for configurable analytics functions and JSONL output."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from components.analytics import record_final_analytics_summary  # noqa: E402
from db.analytics import AnalyticsSnapshot  # noqa: E402
from subcomponents.analytics_output import (  # noqa: E402
    AnalyticsFunctionRunner,
    AnalyticsJsonlWriter,
    analytics_function,
)
from subcomponents.analytics_report import (  # noqa: E402
    AnalyticsMetrics,
    AnalyticsReport,
)


RECURRING_SELECTOR = "test_recurring_analytics_output"
FINAL_SELECTOR = "test_final_analytics_output"


@analytics_function(RECURRING_SELECTOR)
def test_recurring_analytics_output(metrics):
    """Return a small recurring output for registry tests."""
    return {"actions": metrics.action_count}


@analytics_function(FINAL_SELECTOR)
def test_final_analytics_output(metrics):
    """Return a small final output for registry tests."""
    return {"quality": metrics.avg_action_quality}


class AnalyticsOutputTests(unittest.TestCase):
    """Validate registry resolution, execution phases, and JSONL records."""

    def test_function_runner_resolves_groups_and_runs_each_phase(self):
        """Recurring and final selectors should run only in their own phase."""
        runner = AnalyticsFunctionRunner(
            {
                "recurring_functions": {RECURRING_SELECTOR: {}},
                "final_functions": {FINAL_SELECTOR: {}},
            }
        )
        metrics = AnalyticsMetrics(user_count=2, action_count=5, avg_action_quality=0.7)

        self.assertEqual(runner.run_recurring(metrics), {"test_recurring_analytics_output": {"actions": 5}})
        self.assertEqual(runner.run_final(metrics), {"test_final_analytics_output": {"quality": 0.7}})

    def test_unknown_selector_fails_at_runner_initialization(self):
        """Unknown configured analytics selectors should fail before a run."""
        with self.assertRaisesRegex(ValueError, "analytics.recurring_functions"):
            AnalyticsFunctionRunner(
                {
                    "recurring_functions": {"does_not_exist": {}},
                    "final_functions": {},
                }
            )

    def test_empty_function_groups_retain_builtin_metrics_in_both_phases(self):
        runner = AnalyticsFunctionRunner({"recurring_functions": {}, "final_functions": {}})
        metrics = AnalyticsMetrics(user_count=2, action_count=5, avg_action_quality=0.7)
        with tempfile.TemporaryDirectory() as directory:
            writer = AnalyticsJsonlWriter(directory, "termination.jsonl", "analytics.jsonl")
            try:
                writer.write_analytics("recurring", metrics, runner.run_recurring(metrics))
                writer.write_analytics("final", metrics, runner.run_final(metrics))
            finally:
                writer.close()
            records = [json.loads(line) for line in writer.analytics_path.read_text().splitlines()]
        self.assertEqual([record["phase"] for record in records], ["recurring", "final"])
        for record in records:
            self.assertEqual(record["outputs"], {})
            self.assertEqual(record["action_count"], 5)
            self.assertEqual(record["avg_action_quality"], 0.7)
            self.assertEqual(record["avg_actions_per_user"], 2.5)

    def test_non_serializable_output_fails_with_selector_name(self):
        """Analytics outputs must be JSON-compatible for files and DB storage."""
        selector = "test_non_serializable_analytics_output"

        @analytics_function(selector)
        def non_serializable(_metrics):
            return {"value": object()}

        runner = AnalyticsFunctionRunner(
            {"recurring_functions": {selector: {}}, "final_functions": {}}
        )
        with self.assertRaisesRegex(TypeError, selector):
            runner.run_recurring(AnalyticsMetrics(action_count=1))

    def test_jsonl_writer_separates_termination_and_analytics_records(self):
        """Each JSONL file should contain flushed, parseable phase records."""
        report = AnalyticsReport(
            metrics=AnalyticsMetrics(user_count=2, action_count=4, avg_action_quality=0.6),
            termination_requested=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            writer = AnalyticsJsonlWriter(
                directory,
                "nested/termination.jsonl",
                "nested/analytics.jsonl",
            )
            writer.write_termination(report)
            writer.write_analytics("recurring", report.metrics, {"x": 1})
            writer.write_analytics("final", report.metrics, {"y": 2})
            writer.close()

            termination_records = [
                json.loads(line)
                for line in writer.termination_path.read_text().splitlines()
            ]
            analytics_records = [
                json.loads(line)
                for line in writer.analytics_path.read_text().splitlines()
            ]

        self.assertEqual(len(termination_records), 1)
        self.assertEqual(termination_records[0]["record_type"], "termination_check")
        self.assertEqual(len(analytics_records), 2)
        self.assertEqual(
            [record["phase"] for record in analytics_records],
            ["recurring", "final"],
        )

    def test_final_outputs_are_added_to_final_summary(self):
        """Final function results should be persisted in summary JSON properties."""
        snapshot = AnalyticsSnapshot(
            user_count=2,
            action_count=4,
            avg_action_quality=0.6,
        )

        class Database:
            def __init__(self):
                self.summaries = []

            def fetch_analytics_snapshot(self):
                return snapshot

            def record_analytics_summary(self, summary):
                self.summaries.append(summary)

        class Analyzer:
            def analyze(self, metrics):
                return AnalyticsReport(metrics=metrics)

        database = Database()
        record_final_analytics_summary(
            database=database,
            analytics_analyzer=Analyzer(),
            lifecycle_state=SimpleNamespace(analytics_stop_requested=False),
            poll_seconds=0,
            stable_polls=1,
            analytics_function_runner=AnalyticsFunctionRunner(
                {"recurring_functions": {}, "final_functions": {FINAL_SELECTOR: {}}}
            ),
        )

        self.assertEqual(
            database.summaries[0].other_properties["analytics_outputs"],
            {FINAL_SELECTOR: {"quality": 0.6}},
        )


    def test_final_report_preserves_accepted_cause_and_separates_final_measurements(self):
        from db.core import TerminationState
        from domain.termination_semantics import TerminationCriterionResult
        accepted = {"met": True, "function_name": "accepted_criterion",
                    "reason": "accepted", "details": {"action_count": 30000}}
        final = TerminationCriterionResult(met=True, function_name="late_criterion",
            reason="late", details={"action_count": 30500})
        class Database:
            def fetch_analytics_snapshot(self):
                return AnalyticsSnapshot(user_count=10000, action_count=30500)
            def record_analytics_summary(self, summary):
                self.summary = summary
        class Analyzer:
            def analyze(self, metrics):
                return AnalyticsReport(metrics=metrics, termination_requested=True,
                    termination_reason="late", termination_criteria=[final])
        database = Database()
        record_final_analytics_summary(database=database, analytics_analyzer=Analyzer(),
            lifecycle_state=TerminationState(should_terminate=True, reason="accepted",
                termination_source="controller", termination_criteria=[accepted],
                analytics_stop_requested=True, analytics_stop_reason="late"),
            poll_seconds=0, stable_polls=1)
        self.assertEqual(database.summary.action_count, 30500)
        self.assertEqual(database.summary.termination_criteria, [accepted])
        properties = database.summary.other_properties
        self.assertEqual(properties["termination_reason"], "accepted")
        self.assertEqual(properties["final_termination_criteria"][0]["details"]["action_count"], 30500)


if __name__ == "__main__":
    unittest.main()
