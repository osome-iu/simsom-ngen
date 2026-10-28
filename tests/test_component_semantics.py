# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Unit tests for component-owned action, activity, and RecSys semantics."""

import sys
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from db.worker import ActionWriteRecord  # noqa: E402
from domain.component_semantics import (  # noqa: E402
    APPEAL_COMPONENT_SELECTOR,
    component_function,
)
from domain.analytics_semantics import (  # noqa: E402
    AnalyticsEvaluationInterval,
    AVERAGE_ACTIONS_PER_USER,
    SIMULATED_HOURS,
    SIMULATED_HOUR_MS,
)
from db.analytics import (  # noqa: E402
    AnalyticsProgress,
    AnalyticsSnapshot,
    AnalyticsStopSignal,
    AnalyticsSummaryRecord,
)
from subcomponents.analytics_report import (  # noqa: E402
    AnalyticsMetrics,
    AnalyticsReport,
    DefaultAnalyticsAnalyzer,
)
from subcomponents.user_executor import (  # noqa: E402
    UserAction,
    UserSessionResult,
    DefaultUserExecutor,
    execute_user_session,
    sample_viewed_count,
    sampled_viewed_feed_items,
)
from subcomponents.recsys import (  # noqa: E402
    DefaultFeedBuilder,
    UserProfile,
    FeedCandidate,
    IN_NETWORK,
    OUT_NETWORK,
    RankedFeed,
)
from subcomponents.setup_db import DefaultSetupDbPlanner  # noqa: E402
from components.analytics import (  # noqa: E402
    analytics_report_log_payload,
    log_analytics_report,
    run_analytics_loop,
    to_analytics_summary_record,
    to_analytics_stop_signal,
)
from domain.termination_semantics import (  # noqa: E402
    exponential_moving_average_action_quality,
    max_avg_actions_per_user,
    termination_criterion_function,
)


def recsys_config(feed_size=5, components=None):
    """Return a minimal RecSys config object for feed-builder tests."""
    return SimpleNamespace(
        feed_size=feed_size,
        feed_weight_components=components
        or [
            SimpleNamespace(
                appeal_component_function={"post_appeal": {}},
                importance_in=1.0,
                importance_out=1.0,
            )
        ],
    )


def user_feed_weight_components():
    """Return explicit feed-item sampling components for user executor tests."""
    return [
        {
            "appeal_component_function": {"post_appeal": {}},
            "importance_in": 1.0,
            "importance_out": 1.0,
        }
    ]


def user_behavior_config():
    """Return minimal user-behavior config for user executor tests."""
    return {
        "content_embedding_generation_function": {"dummy_content_embedding": {}},
        "feed_weight_components": user_feed_weight_components(),
    }


def p_actions(**overrides):
    """Return explicit per-action weights with omitted actions set to zero."""
    values = {
        "like": 0.0,
        "post": 0.0,
        "repost": 0.0,
        "reply": 0.0,
        "quote": 0.0,
    }
    values.update(overrides)
    return values


def termination_config():
    """Return explicit termination settings for analyzer tests."""
    return {"criteria_functions": {}}


def quality_termination_config():
    """Return termination settings that enable the quality criterion."""
    return {
        "criteria_functions": {TEST_QUALITY_CRITERION: {}}
    }


TEST_MAX_AVG_ACTIONS_CRITERION = "test_max_avg_actions"
TEST_QUALITY_CRITERION = "test_quality_stability"


@termination_criterion_function(TEST_MAX_AVG_ACTIONS_CRITERION)
def test_max_avg_actions_criterion(_evaluator, metrics):
    """Use a test-owned threshold so tests do not pin shipped config."""
    return max_avg_actions_per_user(
        metrics,
        max_avg_actions_per_user=4.0,
    )


@termination_criterion_function(TEST_QUALITY_CRITERION)
def test_quality_criterion(evaluator, metrics):
    """Use test-owned EMA parameters instead of the shipped wrapper."""
    return exponential_moving_average_action_quality(
        evaluator,
        metrics,
        threshold=0.1,
        rho=0.5,
    )


@component_function(APPEAL_COMPONENT_SELECTOR, "unit_test_out_of_bounds")
def unit_test_out_of_bounds_component(user, item, recency_score):
    """Return an invalid score so tests can verify scoring-loop guards."""
    del user, item, recency_score
    return 1.5


@component_function(APPEAL_COMPONENT_SELECTOR, "unit_test_counted")
def unit_test_counted_component(user, item, recency_score):
    """Return a valid score while recording whether evaluation occurred."""
    del user, item, recency_score
    unit_test_counted_component.calls += 1
    return 1.0


unit_test_counted_component.calls = 0


@component_function(APPEAL_COMPONENT_SELECTOR, "unit_test_out_network_only")
def unit_test_out_network_only_component(user, item, recency_score):
    """Fail if a source-filtered test component receives an in-network item."""
    del user, recency_score
    if item.network_source != OUT_NETWORK:
        raise AssertionError("out-network component received an in-network item")
    unit_test_out_network_only_component.calls += 1
    return 1.0


unit_test_out_network_only_component.calls = 0


class UserActionBoundaryTests(unittest.TestCase):
    """Validate the action API that user-model code should use directly."""

    def test_named_constructors_create_valid_actions(self):
        """User code should create common actions without importing domain helpers."""
        user = UserProfile(
            user_id=1,
            user_type="normal",
            session_id="session_1",
        )
        post = UserAction.post(
            creator_user_id=user.user_id,
            content_embedding=[0.1, 0.2],
        )
        like = UserAction.like(
            creator_user_id=user.user_id,
            parent_action_id=42,
        )
        repost = UserAction.repost(
            creator_user_id=user.user_id,
            parent_action_id=42,
        )
        reply = UserAction.reply(
            creator_user_id=user.user_id,
            parent_action_id=42,
            content_embedding=[0.3],
        )
        quote = UserAction.quote(
            creator_user_id=user.user_id,
            parent_action_id=42,
            content_embedding=[0.4],
        )

        self.assertEqual(post.creator_user_id, 1)
        self.assertEqual(post.action_type, UserAction.POST)
        self.assertEqual(like.action_type, UserAction.LIKE)
        self.assertEqual(repost.action_type, UserAction.REPOST)
        self.assertEqual(reply.action_type, UserAction.REPLY)
        self.assertEqual(quote.action_type, UserAction.QUOTE)
        self.assertEqual(reply.content_embedding, [0.3])

    def test_boundary_objects_store_action_type(self):
        """Action-facing boundary objects should store the provided action type."""
        action = UserAction(
            creator_user_id=1,
            action_type="post",
            content_embedding=[0.1],
        )
        candidate = FeedCandidate(
            action_id=1,
            creator_user_id=1,
            action_type="like",
        )

        self.assertEqual(action.action_type, UserAction.POST)
        self.assertEqual(candidate.action_type, UserAction.LIKE)

    def test_named_constructors_use_native_required_arguments(self):
        """Named constructors should rely on Python-required arguments."""
        with self.assertRaises(TypeError):
            UserAction.post()

        with self.assertRaises(TypeError):
            UserAction.like()

    def test_write_record_wraps_user_action_with_session_id(self):
        """Worker DB writes should pair an action with session trace data."""
        action = UserAction.post(
            creator_user_id=1,
            content_embedding=[0.4],
            moderation_actions=[
                {
                    "intervention_type": "queued",
                    "violation_type": "test_fixture",
                    "rule": "test_fixture",
                }
            ],
            other_properties={"source": "unit"},
        )

        record = ActionWriteRecord(
            session_id="session_1",
            action=action,
        )

        self.assertEqual(record.session_id, "session_1")
        self.assertEqual(record.action.creator_user_id, 1)
        self.assertEqual(record.action.action_type, UserAction.POST)
        self.assertEqual(record.action.content_embedding, [0.4])
        self.assertEqual(
            record.action.moderation_actions,
            [
                {
                    "intervention_type": "queued",
                    "violation_type": "test_fixture",
                    "rule": "test_fixture",
                }
            ],
        )
        self.assertEqual(record.action.other_properties, {"source": "unit"})

    def test_session_result_stores_views_and_actions(self):
        """User execution should return viewed IDs separately from actions."""
        result = UserSessionResult(
            viewed_action_ids=[42],
            actions=[
                UserAction.post(
                    creator_user_id=1,
                    content_embedding=[0.4],
                )
            ],
        )

        self.assertEqual(result.viewed_action_ids, [42])
        self.assertEqual(result.actions[0].action_type, UserAction.POST)


class AnalyticsBoundaryTests(unittest.TestCase):
    """Validate analytics reports and controller-readable stop signals."""

    def test_default_analyzer_reports_metrics_without_termination(self):
        """Default analytics should analyze without requesting termination."""
        metrics = AnalyticsMetrics(
            user_count=2,
            action_count=3,
            other_properties={"action_type_ratios": {"like": 0.333, "post": 0.667}},
        )
        report = DefaultAnalyticsAnalyzer(termination_config()).analyze(metrics)

        self.assertEqual(report.metrics, metrics)
        self.assertFalse(report.termination_requested)
        self.assertIsNone(report.termination_reason)
        self.assertEqual(report.termination_criteria, [])
        self.assertEqual(
            report.other_properties,
            {
                "action_type_ratios": {"like": 0.333, "post": 0.667},
                "avg_actions_per_user": 1.5,
            },
        )

    def test_default_analyzer_signals_max_avg_actions_per_user(self):
        """Default analytics should stop when average actions per user is high enough."""
        metrics = AnalyticsMetrics(user_count=2, action_count=30)
        report = DefaultAnalyticsAnalyzer(
            {
                "criteria_functions": {TEST_MAX_AVG_ACTIONS_CRITERION: {}},
            },
        ).analyze(metrics)

        self.assertTrue(report.termination_requested)
        self.assertEqual(
            report.termination_reason,
            "average actions per user reached: 15.000 >= 4.000",
        )
        self.assertEqual(report.other_properties["avg_actions_per_user"], 15.0)

    def test_default_analyzer_signals_avg_action_quality_ema_termination(self):
        """Quality termination should use EMA over avg_action_quality snapshots."""
        analyzer = DefaultAnalyticsAnalyzer(
            quality_termination_config(),
        )

        report = analyzer.analyze(
            AnalyticsMetrics(action_count=10, avg_action_quality=1.0)
        )

        self.assertTrue(report.termination_requested)
        self.assertEqual(
            report.termination_reason,
            "Exponential Moving Average action quality met: quality_diff 0.000 <= threshold 0.100",
        )
        self.assertEqual(
            report.other_properties[TEST_QUALITY_CRITERION],
            {
                "threshold": 0.1,
                "rho": 0.5,
                "avg_action_quality": 1.0,
                "ema": 1.0,
                "quality_diff": 0.0,
                "status": "met",
                "previous_ema": 1.0,
            },
        )

    def test_default_analyzer_waits_for_quality_before_quality_termination(self):
        """Quality termination should wait until avg_action_quality exists."""
        analyzer = DefaultAnalyticsAnalyzer(
            quality_termination_config(),
        )

        report = analyzer.analyze(AnalyticsMetrics(action_count=10))

        self.assertFalse(report.termination_requested)
        self.assertEqual(
            report.other_properties[TEST_QUALITY_CRITERION]["status"],
            "waiting_for_quality",
        )

    def test_default_analyzer_waits_for_new_actions_before_quality_update(self):
        """Repeated snapshots with the same action count should not update EMA."""
        analyzer = DefaultAnalyticsAnalyzer(
            quality_termination_config(),
        )

        first_report = analyzer.analyze(
            AnalyticsMetrics(action_count=10, avg_action_quality=0.5)
        )
        second_report = analyzer.analyze(
            AnalyticsMetrics(action_count=10, avg_action_quality=0.6)
        )

        self.assertFalse(first_report.termination_requested)
        self.assertFalse(second_report.termination_requested)
        self.assertEqual(
            second_report.other_properties[TEST_QUALITY_CRITERION]["status"],
            "waiting_for_new_actions",
        )
        self.assertEqual(
            second_report.other_properties[TEST_QUALITY_CRITERION]["ema"],
            0.75,
        )

    def test_default_analyzer_evaluates_and_reports_all_configured_criteria(self):
        """Every configured termination criterion should be evaluated."""
        analyzer = DefaultAnalyticsAnalyzer(
            {
                "criteria_functions": {TEST_MAX_AVG_ACTIONS_CRITERION: {}, TEST_QUALITY_CRITERION: {}}
            },
        )

        report = analyzer.analyze(
            AnalyticsMetrics(
                user_count=1,
                action_count=4,
                avg_action_quality=1.0,
            )
        )

        self.assertEqual(
            [criterion.function_name for criterion in report.termination_criteria],
                [
                TEST_MAX_AVG_ACTIONS_CRITERION,
                TEST_QUALITY_CRITERION,
            ],
        )
        self.assertTrue(all(criterion.met for criterion in report.termination_criteria))
        summary = to_analytics_summary_record(report)
        self.assertEqual(
            [criterion["function_name"] for criterion in summary.termination_criteria],
                [
                TEST_MAX_AVG_ACTIONS_CRITERION,
                TEST_QUALITY_CRITERION,
            ],
        )

    def test_analytics_metrics_validate_aggregate_shape(self):
        """Analytics metrics should reject invalid aggregate snapshot values."""
        with self.assertRaisesRegex(ValueError, "user_count"):
            AnalyticsMetrics(user_count=-1)

        with self.assertRaisesRegex(TypeError, "action_count"):
            AnalyticsMetrics(action_count=True)

        with self.assertRaisesRegex(ValueError, "avg_action_quality"):
            AnalyticsMetrics(avg_action_quality=1.1)

        with self.assertRaisesRegex(TypeError, "other_properties"):
            AnalyticsMetrics(other_properties=[])

    def test_analytics_report_validates_controller_signal_shape(self):
        """Analytics reports should keep termination state well typed."""
        with self.assertRaisesRegex(TypeError, "metrics"):
            AnalyticsReport(metrics={})

        with self.assertRaisesRegex(TypeError, "termination_requested"):
            AnalyticsReport(metrics=AnalyticsMetrics(), termination_requested="yes")

        with self.assertRaisesRegex(TypeError, "termination_reason"):
            AnalyticsReport(metrics=AnalyticsMetrics(), termination_reason=5)

    def test_analytics_db_records_validate_persisted_summary_shape(self):
        """Analytics DB records should reject invalid persisted status values."""
        with self.assertRaisesRegex(ValueError, "avg_action_quality"):
            AnalyticsSummaryRecord(avg_action_quality=1.2)

        with self.assertRaisesRegex(TypeError, "termination_criteria"):
            AnalyticsSummaryRecord(termination_criteria="yes")

        with self.assertRaisesRegex(TypeError, "termination_criteria"):
            AnalyticsStopSignal(termination_criteria="yes")

        with self.assertRaisesRegex(TypeError, "stop_requested"):
            AnalyticsStopSignal(stop_requested="yes")

        with self.assertRaisesRegex(ValueError, "action_count"):
            AnalyticsSnapshot(action_count=-1)

        with self.assertRaisesRegex(ValueError, "action_id_watermark"):
            AnalyticsProgress(action_id_watermark=-1)

    def test_analytics_evaluation_intervals_use_simulation_progress(self):
        """Recurring checkpoints should support action and simulator-time progress."""
        action_interval = AnalyticsEvaluationInterval(
            AVERAGE_ACTIONS_PER_USER,
            0.5,
        )
        time_interval = AnalyticsEvaluationInterval(SIMULATED_HOURS, 2)

        self.assertEqual(
            action_interval.checkpoint(
                user_count=10,
                action_count=14,
                simulator_timestamp_ms=0,
            ),
            2,
        )
        self.assertEqual(
            action_interval.checkpoint(
                user_count=10,
                action_count=15,
                simulator_timestamp_ms=0,
            ),
            3,
        )
        self.assertEqual(
            time_interval.checkpoint(
                user_count=10,
                action_count=0,
                simulator_timestamp_ms=4 * SIMULATED_HOUR_MS,
            ),
            2,
        )

    def test_termination_report_maps_to_analytics_signal(self):
        """A met criterion should be persisted in the generic termination signal."""
        report = AnalyticsReport(
            metrics=AnalyticsMetrics(action_count=10, avg_action_quality=0.7),
            termination_requested=True,
            termination_reason="stable metrics",
        )

        signal = to_analytics_stop_signal(report)
        summary = to_analytics_summary_record(report)

        self.assertTrue(signal.stop_requested)
        self.assertEqual(signal.stop_reason, "stable metrics")
        self.assertEqual(signal.termination_criteria, [])
        self.assertEqual(summary.termination_criteria, [])
        self.assertAlmostEqual(summary.avg_action_quality, 0.7)

    def test_analytics_report_logs_structured_output(self):
        """Analytics report logging should include metrics and final marker."""

        class CapturingLogger:
            def __init__(self):
                self.messages = []

            def info(self, message, *args):
                self.messages.append(message % args)

        report = AnalyticsReport(
            metrics=AnalyticsMetrics(
                user_count=2,
                action_count=10,
                avg_action_quality=0.7,
                other_properties={
                    "runtime": {"software_runtime_seconds": 1.5},
                    "network_stats": {"avg_friends_per_user": 3.0},
                    "repost_stats": {
                        "repost_action_ratio": "0.137",
                        "repeated_repost_ratio": "0.132",
                        "self_repost_ratio": "0.039",
                        "self_repeated_repost_ratio": "0.020",
                    },
                },
            ),
            termination_requested=True,
            termination_reason="stable metrics",
            other_properties={
                "runtime": {"software_runtime_seconds": 1.5},
                "network_stats": {"avg_friends_per_user": 3.0},
                "repost_stats": {
                    "repost_action_ratio": "0.137",
                    "repeated_repost_ratio": "0.132",
                    "self_repost_ratio": "0.039",
                    "self_repeated_repost_ratio": "0.020",
                },
            },
        )
        logger = CapturingLogger()

        log_analytics_report(report, logger, final=True)
        payload = analytics_report_log_payload(report, final=True)
        pass_payload = analytics_report_log_payload(report, final=False)

        self.assertTrue(payload["final"])
        self.assertEqual(set(payload["metrics"]), {"action_count", "avg_activity_level", "avg_action_quality"})
        self.assertEqual(payload["metrics"]["action_count"], 10)
        self.assertTrue(payload["termination_requested"])
        self.assertEqual(payload["termination_reason"], "stable metrics")
        self.assertEqual(payload["other_properties"]["network_stats"], {"avg_friends_per_user": 3.0})
        self.assertEqual(
            set(pass_payload),
            {
                "final",
                "termination_requested",
                "termination_reason",
                "termination_criteria",
            },
        )
        self.assertEqual(
            list(payload["other_properties"]["repost_stats"]),
            [
                "repost_action_ratio",
                "repeated_repost_ratio",
                "self_repost_ratio",
                "self_repeated_repost_ratio",
            ],
        )
        logged = json.loads(logger.messages[0].split("Analytics report: ", 1)[1])
        self.assertEqual(logged["final"], True)
        self.assertEqual(logged["metrics"]["action_count"], 10)
        self.assertEqual(
            list(logged["other_properties"]["repost_stats"]),
            [
                "repost_action_ratio",
                "repeated_repost_ratio",
                "self_repost_ratio",
                "self_repeated_repost_ratio",
            ],
        )

    def test_quality_termination_details_are_logged_with_analytics_report(self):
        """Structured analytics logs should expose quality termination details."""

        class CapturingLogger:
            def __init__(self):
                self.messages = []

            def info(self, message, *args):
                self.messages.append(message % args)

        analyzer = DefaultAnalyticsAnalyzer(
            quality_termination_config(),
        )
        report = analyzer.analyze(
            AnalyticsMetrics(action_count=10, avg_action_quality=1.0)
        )
        logger = CapturingLogger()

        log_analytics_report(report, logger)

        logged = json.loads(logger.messages[0].split("Analytics report: ", 1)[1])
        details = logged["termination_criteria"][0]["details"]
        self.assertEqual(details["quality_diff"], 0.0)
        self.assertEqual(details["status"], "met")
        self.assertEqual(
            set(logged),
            {
                "final",
                "termination_requested",
                "termination_reason",
                "termination_criteria",
            },
        )

    def test_analytics_loop_publishes_termination_reports(self):
        """Analytics should log a check, then write one final summary and signal."""

        class OneSnapshotDatabase:
            def __init__(self):
                self.snapshots = [
                    SimpleNamespace(
                        user_count=2,
                        action_count=10,
                        avg_activity_level=1.5,
                        avg_action_quality=0.7,
                        other_properties={},
                    ),
                    SimpleNamespace(
                        user_count=2,
                        action_count=10,
                        avg_activity_level=1.5,
                        avg_action_quality=0.7,
                        other_properties={},
                    ),
                    SimpleNamespace(
                        user_count=2,
                        action_count=10,
                        avg_activity_level=1.5,
                        avg_action_quality=0.7,
                        other_properties={},
                    ),
                    SimpleNamespace(
                        user_count=2,
                        action_count=10,
                        avg_activity_level=1.5,
                        avg_action_quality=0.7,
                        other_properties={},
                    ),
                ]
                for snapshot in self.snapshots:
                    snapshot.simulator_timestamp_ms = 0
                self.summaries = []
                self.signals = []

            def get_termination_state(self):
                return SimpleNamespace(
                    should_terminate=bool(self.signals),
                    reason=self.signals[-1].stop_reason if self.signals else None,
                    termination_source="controller",
                    termination_criteria=(
                        self.signals[-1].termination_criteria if self.signals else []
                    ),
                    analytics_stop_requested=bool(self.signals),
                    analytics_stop_reason=(
                        self.signals[-1].stop_reason if self.signals else None
                    ),
                    analytics_termination_criteria=(
                        self.signals[-1].termination_criteria if self.signals else []
                    ),
                )

            def fetch_analytics_snapshot(self):
                return self.snapshots.pop(0)

            def fetch_termination_metrics(self):
                return self.fetch_analytics_snapshot()

            def fetch_analytics_progress(self):
                return SimpleNamespace(
                    user_count=2,
                    action_id_watermark=10,
                    simulator_timestamp_ms=0,
                )

            def record_analytics_summary(self, summary):
                self.summaries.append(summary)

            def record_analytics_stop_signal(self, signal):
                self.signals.append(signal)

        class ConvergingAnalyzer:
            def analyze(self, metrics):
                return AnalyticsReport(
                    metrics=metrics,
                    termination_requested=True,
                    termination_reason="stable metrics",
                )

        database = OneSnapshotDatabase()

        run_analytics_loop(
            database=database,
            analytics_analyzer=ConvergingAnalyzer(),
            evaluation_interval=AnalyticsEvaluationInterval(
                AVERAGE_ACTIONS_PER_USER,
                1,
            ),
        )

        self.assertEqual(len(database.summaries), 2)
        self.assertAlmostEqual(database.summaries[-1].avg_action_quality, 0.7)
        self.assertEqual(database.summaries[-1].termination_criteria, [])
        self.assertEqual(database.summaries[-1].action_count, 10)
        self.assertIsNone(database.summaries[0].avg_activity_level)
        self.assertEqual(database.summaries[0].other_properties, {})
        self.assertEqual(len(database.signals), 1)
        self.assertEqual(database.signals[0].termination_criteria, [])

    def test_analytics_loop_does_not_publish_non_terminating_reports(self):
        """A non-terminating check should not publish a termination signal."""

        class OneSnapshotDatabase:
            def __init__(self):
                self.snapshots = [
                    SimpleNamespace(
                        user_count=1,
                        action_count=2,
                        avg_activity_level=None,
                        avg_action_quality=0.4,
                        other_properties={
                            "action_type_ratios": {"post": 0.5, "reply": 0.5},
                        },
                    ),
                    SimpleNamespace(
                        user_count=1,
                        action_count=2,
                        avg_activity_level=None,
                        avg_action_quality=0.4,
                        other_properties={
                            "action_type_ratios": {"post": 0.5, "reply": 0.5},
                        },
                    ),
                    SimpleNamespace(
                        user_count=1,
                        action_count=2,
                        avg_activity_level=None,
                        avg_action_quality=0.4,
                        other_properties={
                            "action_type_ratios": {"post": 0.5, "reply": 0.5},
                        },
                    ),
                    SimpleNamespace(
                        user_count=1,
                        action_count=2,
                        avg_activity_level=None,
                        avg_action_quality=0.4,
                        other_properties={
                            "action_type_ratios": {"post": 0.5, "reply": 0.5},
                        },
                    ),
                ]
                for snapshot in self.snapshots:
                    snapshot.simulator_timestamp_ms = 0
                self.fetches = 0
                self.summaries = []
                self.signals = []

            def get_termination_state(self):
                return SimpleNamespace(
                    should_terminate=self.fetches >= 1,
                    reason="manual stop",
                    termination_source="controller",
                    termination_criteria=[],
                    analytics_stop_requested=False,
                    analytics_stop_reason=None,
                    analytics_termination_criteria=[],
                )

            def fetch_analytics_snapshot(self):
                self.fetches += 1
                return self.snapshots.pop(0)

            def fetch_termination_metrics(self):
                return self.fetch_analytics_snapshot()

            def fetch_analytics_progress(self):
                return SimpleNamespace(
                    user_count=1,
                    action_id_watermark=2,
                    simulator_timestamp_ms=0,
                )

            def record_analytics_summary(self, summary):
                self.summaries.append(summary)

            def record_analytics_stop_signal(self, signal):
                self.signals.append(signal)

        database = OneSnapshotDatabase()

        run_analytics_loop(
            database=database,
            analytics_analyzer=DefaultAnalyticsAnalyzer(termination_config()),
            evaluation_interval=AnalyticsEvaluationInterval(
                AVERAGE_ACTIONS_PER_USER,
                1,
            ),
        )

        self.assertEqual(len(database.summaries), 2)
        self.assertEqual(database.summaries[-1].termination_criteria, [])
        self.assertAlmostEqual(database.summaries[-1].avg_action_quality, 0.4)
        self.assertEqual(
            database.summaries[-1].other_properties,
            {
                "action_type_ratios": {"post": 0.5, "reply": 0.5},
                "avg_actions_per_user": 2.0,
                "analytics_outputs": {},
                "termination_requested": True,
                "termination_reason": "manual stop",
                "termination_source": "controller",
                "final_termination_criteria": [],
            },
        )
        self.assertIsNone(database.summaries[0].avg_activity_level)
        self.assertEqual(database.summaries[0].other_properties, {})
        self.assertEqual(database.signals, [])

    def test_analytics_loop_confirms_watermark_with_exact_action_count(self):
        """An action-id watermark alone must not trigger a recurring pass."""

        class Database:
            def __init__(self):
                self.lifecycle_reads = 0
                self.termination_fetches = 0
                self.summaries = []
                self.snapshot = AnalyticsSnapshot(user_count=10, action_count=9)

            def get_termination_state(self):
                self.lifecycle_reads += 1
                return SimpleNamespace(
                    should_terminate=self.lifecycle_reads > 1,
                    reason="manual stop",
                    termination_source="controller",
                    termination_criteria=[],
                    analytics_stop_requested=False,
                    analytics_stop_reason=None,
                    analytics_termination_criteria=[],
                )

            def fetch_analytics_progress(self):
                return SimpleNamespace(
                    user_count=10,
                    action_id_watermark=10,
                    simulator_timestamp_ms=0,
                )

            def fetch_termination_metrics(self):
                self.termination_fetches += 1
                return self.snapshot

            def fetch_analytics_snapshot(self):
                return self.snapshot

            def record_analytics_summary(self, summary):
                self.summaries.append(summary)

            def record_analytics_stop_signal(self, _signal):
                raise AssertionError("no stop signal expected")

        class Analyzer:
            def __init__(self):
                self.calls = 0

            def analyze(self, metrics):
                self.calls += 1
                return AnalyticsReport(metrics=metrics)

        database = Database()
        analyzer = Analyzer()
        run_analytics_loop(
            database=database,
            analytics_analyzer=analyzer,
            evaluation_interval=AnalyticsEvaluationInterval(
                AVERAGE_ACTIONS_PER_USER,
                1,
            ),
        )

        self.assertEqual(database.termination_fetches, 1)
        self.assertEqual(analyzer.calls, 1)
        self.assertEqual(len(database.summaries), 1)

    def test_analytics_mpi_shutdown_waits_for_final_authorization(self):
        """Analytics should acknowledge readiness before writing its final row."""

        class Request:
            def wait(self):
                return None

        class ShutdownCommunicator:
            def __init__(self):
                self.incoming = [
                    {"message": "stop_request"},
                    {"message": "final_analysis"},
                ]
                self.sent = []

            def iprobe(self, *args, **kwargs):
                return bool(self.incoming)

            def recv(self, *args, **kwargs):
                return self.incoming.pop(0)

            def isend(self, message, dest):
                self.sent.append((dest, message))
                return Request()

        class FinalDatabase:
            def __init__(self):
                self.summaries = []
                self.snapshot = AnalyticsSnapshot(
                    user_count=2,
                    action_count=10,
                    avg_action_quality=0.7,
                )

            def get_termination_state(self):
                return SimpleNamespace(
                    should_terminate=True,
                    reason="quality stable",
                    termination_source="controller",
                    termination_criteria=[
                        {
                            "met": True,
                            "function_name": "criterion_a",
                            "reason": "first",
                        },
                        {
                            "met": True,
                            "function_name": "criterion_b",
                            "reason": "second",
                        },
                    ],
                )

            def fetch_analytics_snapshot(self):
                return self.snapshot

            def record_analytics_summary(self, summary):
                self.summaries.append(summary)

        class FinalAnalyzer:
            def analyze(self, metrics):
                return AnalyticsReport(metrics=metrics)

        communicator = ShutdownCommunicator()
        database = FinalDatabase()

        run_analytics_loop(
            database=database,
            analytics_analyzer=FinalAnalyzer(),
            evaluation_interval=AnalyticsEvaluationInterval(
                AVERAGE_ACTIONS_PER_USER,
                1,
            ),
            comm_world=communicator,
            controller_rank=0,
        )

        self.assertEqual(
            [message[1]["message"] for message in communicator.sent],
            [
                "analytics_ready_for_final",
                "final_analytics_complete",
            ],
        )
        self.assertEqual(
            [criterion["function_name"] for criterion in database.summaries[0].termination_criteria],
            ["criterion_a", "criterion_b"],
        )

    def test_default_executor_returns_view_only_result_for_zero_planned_actions(self):
        """Zero planned actions should keep session output view-only."""
        executor = DefaultUserExecutor(user_behavior_config=user_behavior_config())
        user = UserProfile(
            user_id=1,
            user_type="normal",
            planned_action_count=0,
        )
        feed = RankedFeed(
            items=[
                FeedCandidate(action_id=42, creator_user_id=2, action_type="post"),
                FeedCandidate(action_id=42, creator_user_id=2, action_type="post"),
                FeedCandidate(action_id=43, creator_user_id=2, action_type="post"),
            ]
        )

        with patch("subcomponents.user_executor.random.expovariate", return_value=100.0):
            result = executor.execute_session(user, feed)

        self.assertEqual(result.viewed_action_ids, [42, 43])
        self.assertEqual(result.actions, [])

    def test_default_executor_samples_posts_with_empty_seen(self):
        """Only parentless posts can be emitted when no item has been viewed."""
        executor = DefaultUserExecutor(user_behavior_config=user_behavior_config())
        user = UserProfile(
            user_id=1,
            user_type="normal",
            planned_action_count=2,
            user_behavior={"p_actions": p_actions(post=1.0)},
        )

        result = executor.execute_session(user, RankedFeed())

        self.assertEqual(result.viewed_action_ids, [])
        self.assertEqual([action.action_type for action in result.actions], ["post", "post"])
        self.assertTrue(all(action.parent_action_id is None for action in result.actions))
        self.assertTrue(
            all(action.content_embedding == [1.0] for action in result.actions)
        )

    def test_content_embedding_generator_receives_user_profile_vector(self):
        """Configured content generators should receive only the creator profile vector."""
        with patch.dict(
            "subcomponents.user_executor.CONTENT_EMBEDDING_GENERATION_FUNCTIONS",
            {"profile_echo": lambda profile_vector: list(profile_vector)},
        ):
            executor = DefaultUserExecutor(
                user_behavior_config={
                    "content_embedding_generation_function": {"profile_echo": {}},
                    "feed_weight_components": user_feed_weight_components(),
                }
            )
            user = UserProfile(
                user_id=1,
                user_type="normal",
                planned_action_count=1,
                vector_profile=[0.25, 0.75],
                user_behavior={"p_actions": p_actions(post=1.0)},
            )

            result = executor.execute_session(user, RankedFeed())

        self.assertEqual(result.actions[0].content_embedding, [0.25, 0.75])

    def test_empty_generated_content_embedding_raises(self):
        """Content generators should not be allowed to create empty embeddings."""
        with patch.dict(
            "subcomponents.user_executor.CONTENT_EMBEDDING_GENERATION_FUNCTIONS",
            {"empty_embedding": lambda profile_vector: []},
        ):
            executor = DefaultUserExecutor(
                user_behavior_config={
                    "content_embedding_generation_function": {"empty_embedding": {}},
                    "feed_weight_components": user_feed_weight_components(),
                }
            )
            user = UserProfile(
                user_id=1,
                user_type="normal",
                planned_action_count=1,
                user_behavior={"p_actions": p_actions(post=1.0)},
            )

            with self.assertRaisesRegex(ValueError, "content_embedding"):
                executor.execute_session(user, RankedFeed())

    def test_default_executor_skips_parented_actions_when_seen_is_empty(self):
        """Parented action types should not emit without viewed targets."""
        executor = DefaultUserExecutor(user_behavior_config=user_behavior_config())
        user = UserProfile(
            user_id=1,
            user_type="normal",
            planned_action_count=1,
            user_behavior={"p_actions": p_actions(like=1.0)},
        )

        result = executor.execute_session(user, RankedFeed())

        self.assertEqual(result.actions, [])

    def test_default_executor_allows_different_action_types_on_same_item(self):
        """Different parented action types may target the same viewed item."""
        executor = DefaultUserExecutor(user_behavior_config=user_behavior_config())
        user = UserProfile(
            user_id=1,
            user_type="normal",
            planned_action_count=2,
            user_behavior={"p_actions": p_actions(like=1.0, repost=1.0)},
        )
        feed = RankedFeed(
            items=[
                FeedCandidate(
                    action_id=42,
                    creator_user_id=2,
                    action_type="post",
                    other_properties={"appeal": 1.0},
                )
            ]
        )

        with patch("subcomponents.user_executor.random.expovariate", return_value=0.1):
            with patch(
                "subcomponents.user_executor.random.random",
                side_effect=[0.1, 0.75, 0.0, 0.0],
            ):
                result = executor.execute_session(user, feed)

        self.assertEqual(result.viewed_action_ids, [42])
        self.assertEqual(
            [(action.action_type, action.parent_action_id) for action in result.actions],
            [("like", 42), ("repost", 42)],
        )

    def test_default_executor_samples_same_action_type_without_replacement(self):
        """One action type should not select the same viewed item twice."""
        executor = DefaultUserExecutor(user_behavior_config())
        user = UserProfile(
            user_id=1,
            user_type="normal",
            planned_action_count=3,
            user_behavior={"p_actions": p_actions(like=1.0)},
        )
        feed = RankedFeed(
            items=[
                FeedCandidate(
                    action_id=41,
                    creator_user_id=2,
                    action_type="post",
                    other_properties={"appeal": 1.0},
                ),
                FeedCandidate(
                    action_id=42,
                    creator_user_id=3,
                    action_type="post",
                    other_properties={"appeal": 1.0},
                ),
            ]
        )

        with patch("subcomponents.user_executor.random.expovariate", return_value=100.0):
            with patch("subcomponents.user_executor.random.random", return_value=0.0):
                result = executor.execute_session(user, feed)

        self.assertEqual(
            [action.parent_action_id for action in result.actions],
            [41, 42],
        )

    def test_default_executor_drops_excess_parented_actions(self):
        """Extra parented requests disappear when only one item was viewed."""
        executor = DefaultUserExecutor(user_behavior_config())
        user = UserProfile(
            user_id=1,
            user_type="normal",
            planned_action_count=3,
            user_behavior={"p_actions": p_actions(repost=1.0)},
        )
        feed = RankedFeed(
            items=[
                FeedCandidate(
                    action_id=41,
                    creator_user_id=2,
                    action_type="post",
                    other_properties={"appeal": 1.0},
                )
            ]
        )

        with patch("subcomponents.user_executor.random.expovariate", return_value=0.1):
            with patch("subcomponents.user_executor.random.random", return_value=0.0):
                result = executor.execute_session(user, feed)

        self.assertEqual(len(result.actions), 1)
        self.assertEqual(result.actions[0].parent_action_id, 41)

    def test_default_executor_parent_targets_are_limited_to_viewed_prefix(self):
        """Parented actions should only target sampled viewed feed items."""
        executor = DefaultUserExecutor(user_behavior_config=user_behavior_config())
        user = UserProfile(
            user_id=1,
            user_type="normal",
            planned_action_count=1,
            user_behavior={"p_actions": p_actions(like=1.0)},
        )
        feed = RankedFeed(
            items=[
                FeedCandidate(
                    action_id=41,
                    creator_user_id=2,
                    action_type="post",
                    other_properties={"appeal": 1.0},
                ),
                FeedCandidate(
                    action_id=42,
                    creator_user_id=3,
                    action_type="post",
                    other_properties={"appeal": 1.0},
                ),
            ]
        )

        with patch("subcomponents.user_executor.random.expovariate", return_value=0.1):
            with patch("subcomponents.user_executor.random.random", side_effect=[0.0, 0.0]):
                result = executor.execute_session(user, feed)

        self.assertEqual(result.viewed_action_ids, [41])
        self.assertEqual(result.actions[0].parent_action_id, 41)

    def test_default_executor_samples_parent_targets_by_weight(self):
        """Parented targets should be sampled by configured W components."""
        executor = DefaultUserExecutor(user_behavior_config=user_behavior_config())
        user = UserProfile(
            user_id=1,
            user_type="normal",
            planned_action_count=1,
            user_behavior={"p_actions": p_actions(like=1.0)},
        )
        feed = RankedFeed(
            items=[
                FeedCandidate(
                    action_id=41,
                    creator_user_id=2,
                    action_type="post",
                    other_properties={"appeal": 0.0},
                ),
                FeedCandidate(
                    action_id=42,
                    creator_user_id=3,
                    action_type="post",
                    other_properties={"appeal": 1.0},
                ),
            ]
        )

        with patch("subcomponents.user_executor.random.expovariate", return_value=100.0):
            with patch("subcomponents.user_executor.random.random", side_effect=[0.0, 0.5]):
                result = executor.execute_session(user, feed)

        self.assertEqual(result.viewed_action_ids, [41, 42])
        self.assertEqual(result.actions[0].parent_action_id, 42)

    def test_default_executor_rejects_out_of_bounds_component_value(self):
        """User-side target selection should enforce bounded component values."""
        executor = DefaultUserExecutor(
            user_behavior_config={
                "content_embedding_generation_function": {"dummy_content_embedding": {}},
                "feed_weight_components": [
                    {
                        "appeal_component_function": {"unit_test_out_of_bounds": {}},
                        "importance_in": 1.0,
                        "importance_out": 1.0,
                    }
                ],
            }
        )
        user = UserProfile(
            user_id=1,
            user_type="normal",
            planned_action_count=1,
            user_behavior={"p_actions": p_actions(like=1.0)},
        )
        feed = RankedFeed(
            items=[
                FeedCandidate(
                    action_id=41,
                    creator_user_id=2,
                    action_type="post",
                    other_properties={"appeal": 1.0},
                )
            ]
        )

        with patch("subcomponents.user_executor.random.expovariate", return_value=0.1):
            with self.assertRaisesRegex(ValueError, "unit_test_out_of_bounds"):
                executor.execute_session(user, feed)

    def test_sample_viewed_count_uses_exponential_model_and_caps_feed(self):
        """Viewed count should use p_stop = 2 / feed_size and cap to the feed."""
        with patch(
            "subcomponents.user_executor.random.expovariate",
            return_value=7.2,
        ) as expovariate:
            self.assertEqual(sample_viewed_count(3), 3)

        expovariate.assert_called_once_with(2.0 / 3.0)

    def test_sample_viewed_count_rounds_positive_draw_up(self):
        """Positive raw viewed counts should become at least one viewed item."""
        with patch("subcomponents.user_executor.random.expovariate", return_value=1.01):
            self.assertEqual(sample_viewed_count(5), 2)
        with patch("subcomponents.user_executor.random.expovariate", return_value=0.01):
            self.assertEqual(sample_viewed_count(5), 1)
        self.assertEqual(sample_viewed_count(0), 0)

    def test_sampled_viewed_feed_items_deduplicates_before_prefix_cap(self):
        """The viewed set should be a distinct feed prefix, capped by the draw."""
        feed = RankedFeed(
            items=[
                FeedCandidate(action_id=42, creator_user_id=2, action_type="post"),
                FeedCandidate(action_id=42, creator_user_id=2, action_type="post"),
                FeedCandidate(action_id=43, creator_user_id=2, action_type="post"),
            ]
        )

        with patch("subcomponents.user_executor.random.expovariate", return_value=0.1):
            viewed_items = sampled_viewed_feed_items(feed)

        self.assertEqual([item.action_id for item in viewed_items], [42])

    def test_execute_user_session_strips_zero_count_actions_but_keeps_views(self):
        """Shared session semantics should keep zero-action sessions view-only."""

        class ActionReturningExecutor:
            def execute_session(self, user, feed):
                return UserSessionResult(
                    viewed_action_ids=[42],
                    actions=[
                        UserAction.post(
                            creator_user_id=user.user_id,
                            content_embedding=[0.4],
                        )
                    ],
                )

        result = execute_user_session(
            UserProfile(
                user_id=1,
                user_type="normal",
                planned_action_count=0,
            ),
            RankedFeed(),
            ActionReturningExecutor(),
        )

        self.assertEqual(result.viewed_action_ids, [42])
        self.assertEqual(result.actions, [])

    def test_execute_user_session_preserves_positive_count_actions(self):
        """Positive planned action counts should allow executor-returned actions."""

        class ActionReturningExecutor:
            def execute_session(self, user, feed):
                return UserSessionResult(
                    viewed_action_ids=[42],
                    actions=[
                        UserAction.post(
                            creator_user_id=user.user_id,
                            content_embedding=[0.4],
                        )
                    ],
                )

        result = execute_user_session(
            UserProfile(
                user_id=1,
                user_type="normal",
                planned_action_count=1,
            ),
            RankedFeed(),
            ActionReturningExecutor(),
        )

        self.assertEqual(result.viewed_action_ids, [42])
        self.assertEqual(len(result.actions), 1)


class ActivityAndSetupBoundaryTests(unittest.TestCase):
    """Validate activity and setup behavior without extra runtime validators."""

    def test_user_profile_stores_activity_and_behavior(self):
        """User profiles should store provided activity and behavior values."""
        profile = UserProfile(
            user_id=1,
            user_type="normal",
            session_id="session_1",
            activity_level=0.0,
            planned_action_count=0,
            user_behavior={"p_actions": {"post": 1.0}},
        )

        self.assertEqual(profile.planned_action_count, 0)
        self.assertEqual(profile.session_id, "session_1")
        self.assertEqual(profile.user_behavior["p_actions"], {"post": 1.0})

    def test_user_profile_requires_user_type(self):
        """User type is required for worker-facing profiles."""
        with self.assertRaises(TypeError):
            UserProfile(user_id=1)

    def test_setup_action_settings_are_normalized(self):
        """Setup should store normalized action preferences."""
        action_settings = DefaultSetupDbPlanner._generate_user_event_dist(
            {"POST": [2.0, 0.0], "like": [1.0, 0.0]}
        )

        self.assertEqual(action_settings, {"POST": 2 / 3, "like": 1 / 3})


class RecSysCombinedRankingTests(unittest.TestCase):
    """Validate ranking over combined in/out-network candidates."""

    def test_recency_is_global_and_feed_limit_follows_ranking(self):
        builder = DefaultFeedBuilder(recsys_config(feed_size=2, components=[{
            "recency_component_function": {"inverse_rank_recency": {}},
            "importance_in": 1.0, "importance_out": 1.0,
        }]))
        candidates = [
            feed_candidate(1, IN_NETWORK, timestamp=100, appeal=1.0),
            feed_candidate(3, OUT_NETWORK, timestamp=1, appeal=1.0),
            feed_candidate(2, OUT_NETWORK, timestamp=2, appeal=1.0),
        ]
        feed = builder.build_feed(UserProfile(user_id=1, user_type="normal"), candidates)
        self.assertEqual([item.action_id for item in feed.items], [3, 2])

    def test_duplicate_parent_prefers_in_network_then_latest_surviving_source(self):
        builder = DefaultFeedBuilder(recsys_config(feed_size=2, components=[{
            "recency_component_function": {"inverse_rank_recency": {}},
            "importance_in": 1.0, "importance_out": 1.0,
        }]))
        candidates = [
            FeedCandidate(action_id=1, creator_user_id=2, action_type="post",
                          network_source=OUT_NETWORK, source_action_id=100),
            FeedCandidate(action_id=1, creator_user_id=2, action_type="post",
                          network_source=IN_NETWORK, source_action_id=5),
            FeedCandidate(action_id=1, creator_user_id=2, action_type="post",
                          network_source=IN_NETWORK, source_action_id=7),
            FeedCandidate(action_id=2, creator_user_id=3, action_type="post",
                          network_source=OUT_NETWORK, source_action_id=8),
        ]
        for ordered in (candidates, list(reversed(candidates))):
            with self.subTest(reversed=ordered is not candidates):
                feed = builder.build_feed(UserProfile(user_id=1, user_type="normal"), ordered)
                self.assertEqual([item.action_id for item in feed.items], [2, 1])
                self.assertEqual(feed.items[1].network_source, IN_NETWORK)
                self.assertEqual(feed.items[1].source_action_id, 7)

    def test_combined_ranking_preserves_source_specific_importance(self):
        builder = DefaultFeedBuilder(recsys_config(feed_size=2, components=[{
            "appeal_component_function": {"post_appeal": {}},
            "importance_in": 2.0, "importance_out": 1.0,
        }]))
        feed = builder.build_feed(UserProfile(user_id=1, user_type="normal"), [
            feed_candidate(1, IN_NETWORK, timestamp=1, appeal=0.8),
            feed_candidate(2, OUT_NETWORK, timestamp=2, appeal=0.7),
        ])
        self.assertEqual([item.action_id for item in feed.items], [2, 1])

    def test_empty_and_zero_length_feeds(self):
        user = UserProfile(user_id=1, user_type="normal")
        self.assertEqual(DefaultFeedBuilder(recsys_config()).build_feed(user, []).items, [])
        self.assertEqual(DefaultFeedBuilder(recsys_config(feed_size=0)).build_feed(
            user, [feed_candidate(1, IN_NETWORK, timestamp=1, appeal=1.0)]).items, [])

    def test_feed_builder_ranks_both_inventories_together(self):
        """The highest score leads even when it comes from out-of-network."""
        builder = DefaultFeedBuilder(recsys_config(feed_size=5))
        candidates = [
            feed_candidate(1, IN_NETWORK, timestamp=10, appeal=0.4),
            feed_candidate(2, IN_NETWORK, timestamp=8, appeal=0.3),
            feed_candidate(3, IN_NETWORK, timestamp=6, appeal=0.2),
            feed_candidate(4, IN_NETWORK, timestamp=4, appeal=0.1),
            feed_candidate(10, OUT_NETWORK, timestamp=100, appeal=1.0),
        ]

        feed = builder.build_feed(
            UserProfile(user_id=1, user_type="normal"),
            candidates,
        )

        self.assertEqual(
            [item.network_source for item in feed.items],
            [OUT_NETWORK, IN_NETWORK, IN_NETWORK, IN_NETWORK, IN_NETWORK],
        )
        self.assertEqual([item.action_id for item in feed.items], [10, 1, 2, 3, 4])

    def test_feed_builder_does_not_interleave_sources(self):
        """A global score ordering can put two in-network items first."""
        builder = DefaultFeedBuilder(recsys_config(feed_size=4))
        candidates = [
            feed_candidate(1, IN_NETWORK, timestamp=10, appeal=0.4),
            feed_candidate(2, IN_NETWORK, timestamp=9, appeal=0.3),
            feed_candidate(10, OUT_NETWORK, timestamp=8, appeal=0.2),
            feed_candidate(11, OUT_NETWORK, timestamp=7, appeal=0.1),
        ]

        feed = builder.build_feed(
            UserProfile(user_id=1, user_type="normal"),
            candidates,
        )

        self.assertEqual(
            [item.network_source for item in feed.items],
            [IN_NETWORK, IN_NETWORK, OUT_NETWORK, OUT_NETWORK],
        )
        self.assertEqual([item.action_id for item in feed.items], [1, 2, 10, 11])

    def test_feed_builder_accepts_uneven_inventory_sizes(self):
        """Uneven inventories can fill the feed without source quotas."""
        builder = DefaultFeedBuilder(recsys_config(feed_size=5))
        candidates = [
            feed_candidate(1, IN_NETWORK, timestamp=10, appeal=1.0),
            feed_candidate(10, OUT_NETWORK, timestamp=7, appeal=1.0),
            feed_candidate(11, OUT_NETWORK, timestamp=6, appeal=1.0),
            feed_candidate(12, OUT_NETWORK, timestamp=5, appeal=1.0),
            feed_candidate(13, OUT_NETWORK, timestamp=4, appeal=1.0),
        ]

        feed = builder.build_feed(
            UserProfile(user_id=1, user_type="normal"),
            candidates,
        )

        self.assertEqual(
            [item.network_source for item in feed.items],
            [IN_NETWORK, OUT_NETWORK, OUT_NETWORK, OUT_NETWORK, OUT_NETWORK],
        )
        self.assertEqual([item.action_id for item in feed.items], [1, 10, 11, 12, 13])

    def test_feed_builder_ranks_by_weight_not_timestamp(self):
        """W(i) should control rank before feed merge."""
        builder = DefaultFeedBuilder(recsys_config(feed_size=1))
        candidates = [
            feed_candidate(1, IN_NETWORK, timestamp=100, appeal=0.0),
            feed_candidate(2, IN_NETWORK, timestamp=1, appeal=1.0),
        ]

        feed = builder.build_feed(
            UserProfile(user_id=1, user_type="normal"),
            candidates,
        )

        self.assertEqual([item.action_id for item in feed.items], [2])

    def test_feed_builder_does_not_use_timestamp_as_weight_tie_breaker(self):
        """Equal component weights should not be reordered by timestamp."""
        builder = DefaultFeedBuilder(recsys_config(feed_size=2))
        candidates = [
            feed_candidate(1, IN_NETWORK, timestamp=100, appeal=1.0),
            feed_candidate(2, IN_NETWORK, timestamp=1, appeal=1.0),
        ]

        feed = builder.build_feed(
            UserProfile(user_id=1, user_type="normal"),
            candidates,
        )

        self.assertEqual([item.action_id for item in feed.items], [1, 2])

    def test_feed_builder_rejects_missing_or_invalid_appeal(self):
        """Appeal must be present, numeric, and normalized before scoring."""
        builder = DefaultFeedBuilder(recsys_config(feed_size=1))

        with self.assertRaisesRegex(ValueError, "appeal is required"):
            builder.build_feed(
                UserProfile(user_id=1, user_type="normal"),
                [FeedCandidate(action_id=1, creator_user_id=2, action_type="post")],
            )

        with self.assertRaisesRegex(TypeError, "appeal"):
            builder.build_feed(
                UserProfile(user_id=1, user_type="normal"),
                [feed_candidate(1, IN_NETWORK, timestamp=1, appeal="high")],
            )

        with self.assertRaisesRegex(ValueError, "appeal"):
            builder.build_feed(
                UserProfile(user_id=1, user_type="normal"),
                [feed_candidate(1, IN_NETWORK, timestamp=1, appeal=1.1)],
            )

    def test_feed_builder_skips_duplicate_materialized_candidates(self):
        """Deduplication precedes global ranking and the feed limit."""
        builder = DefaultFeedBuilder(recsys_config(feed_size=3))
        candidates = [
            feed_candidate(1, IN_NETWORK, timestamp=10, appeal=0.3),
            feed_candidate(2, IN_NETWORK, timestamp=9, appeal=0.2),
            feed_candidate(1, OUT_NETWORK, timestamp=8, appeal=0.3),
            feed_candidate(10, OUT_NETWORK, timestamp=7, appeal=0.1),
        ]

        feed = builder.build_feed(
            UserProfile(user_id=1, user_type="normal"),
            candidates,
        )

        self.assertEqual([item.action_id for item in feed.items], [1, 2, 10])

    def test_feed_builder_uses_engagement_components_for_weight(self):
        """Stored engagement components should contribute to the W(i) weight."""
        builder = DefaultFeedBuilder(
            recsys_config(
                feed_size=1,
                components=[
                    {
                        "engagement_component_function": {"global_like_count": {}},
                        "importance_in": 1.0,
                        "importance_out": 1.0,
                    }
                ],
            )
        )
        candidates = [
            feed_candidate(
                1,
                IN_NETWORK,
                timestamp=10,
                appeal=1.0,
                engagement_like=1,
            ),
            feed_candidate(
                2,
                IN_NETWORK,
                timestamp=9,
                appeal=1.0,
                engagement_like=6,
            ),
        ]

        feed = builder.build_feed(
            UserProfile(user_id=1, user_type="normal"),
            candidates,
        )

        self.assertEqual([item.action_id for item in feed.items], [2])

    def test_feed_builder_rejects_missing_or_invalid_engagement(self):
        """Engagement fields must be supplied by the inventory builder."""
        builder = DefaultFeedBuilder(
            recsys_config(
                feed_size=1,
                components=[
                    {
                        "engagement_component_function": {"global_like_count": {}},
                        "importance_in": 1.0,
                        "importance_out": 1.0,
                    }
                ],
            )
        )

        missing = feed_candidate(1, IN_NETWORK, timestamp=1, appeal=1.0)
        del missing.other_properties["engagement_like"]
        with self.assertRaisesRegex(ValueError, "engagement_like is required"):
            builder.build_feed(
                UserProfile(user_id=1, user_type="normal"),
                [missing],
            )

        with self.assertRaisesRegex(TypeError, "engagement_like"):
            builder.build_feed(
                UserProfile(user_id=1, user_type="normal"),
                [feed_candidate(1, IN_NETWORK, timestamp=1, appeal=1.0, engagement_like="many")],
            )

        with self.assertRaisesRegex(ValueError, "engagement_like"):
            builder.build_feed(
                UserProfile(user_id=1, user_type="normal"),
                [feed_candidate(1, IN_NETWORK, timestamp=1, appeal=1.0, engagement_like=-1)],
            )

    def test_feed_builder_uses_profile_content_relevance_component(self):
        """Relevance should rank content by normalized profile/content cosine."""
        builder = DefaultFeedBuilder(
            recsys_config(
                feed_size=2,
                components=[
                    {
                        "relevance_component_function": (
                            {"profile_vector_n_content_embedding_cosine_relevance": {}}
                        ),
                        "importance_in": 1.0,
                        "importance_out": 1.0,
                    }
                ],
            )
        )
        candidates = [
            feed_candidate(1, IN_NETWORK, timestamp=10, appeal=1.0, content_embedding=[-1.0, 0.0]),
            feed_candidate(2, IN_NETWORK, timestamp=9, appeal=1.0, content_embedding=[1.0, 0.0]),
        ]

        feed = builder.build_feed(
            UserProfile(user_id=1, user_type="normal", vector_profile=[1.0, 0.0]),
            candidates,
        )

        self.assertEqual([item.action_id for item in feed.items], [2, 1])

    def test_relevance_component_rejects_missing_vectors(self):
        """Relevance should fail clearly when the user or content vector is absent."""
        builder = DefaultFeedBuilder(
            recsys_config(
                feed_size=1,
                components=[
                    {
                        "relevance_component_function": (
                            {"profile_vector_n_content_embedding_cosine_relevance": {}}
                        ),
                        "importance_in": 1.0,
                        "importance_out": 1.0,
                    }
                ],
            )
        )

        with self.assertRaisesRegex(ValueError, "user.vector_profile"):
            builder.build_feed(
                UserProfile(user_id=1, user_type="normal"),
                [feed_candidate(1, IN_NETWORK, timestamp=10, appeal=1.0, content_embedding=[1.0])],
            )

        with self.assertRaisesRegex(ValueError, "candidate.content_embedding"):
            builder.build_feed(
                UserProfile(user_id=1, user_type="normal", vector_profile=[1.0]),
                [feed_candidate(1, IN_NETWORK, timestamp=10, appeal=1.0, content_embedding=[])],
            )

    def test_feed_builder_uses_source_specific_importance(self):
        """Out-network ranking should use importance_out, not importance_in."""
        builder = DefaultFeedBuilder(
            recsys_config(
                feed_size=2,
                components=[
                    {
                        "appeal_component_function": {"post_appeal": {}},
                        "importance_in": 0.0,
                        "importance_out": 1.0,
                    }
                ],
            )
        )
        candidates = [
            feed_candidate(1, OUT_NETWORK, timestamp=100, appeal=0.0),
            feed_candidate(2, OUT_NETWORK, timestamp=1, appeal=1.0),
        ]

        feed = builder.build_feed(
            UserProfile(user_id=1, user_type="normal"),
            candidates,
        )

        self.assertEqual([item.action_id for item in feed.items], [2, 1])

    def test_feed_builder_does_not_evaluate_zero_importance_components(self):
        """Zero-importance components are validated but never called at runtime."""
        unit_test_counted_component.calls = 0
        builder = DefaultFeedBuilder(
            recsys_config(
                feed_size=2,
                components=[
                    {
                        "appeal_component_function": {"unit_test_counted": {}},
                        "importance_in": 0.0,
                        "importance_out": 0.0,
                    }
                ],
            )
        )

        builder.build_feed(
            UserProfile(user_id=1, user_type="normal"),
            [
                feed_candidate(1, IN_NETWORK, timestamp=2, appeal=1.0),
                feed_candidate(2, OUT_NETWORK, timestamp=1, appeal=1.0),
            ],
        )

        self.assertEqual(unit_test_counted_component.calls, 0)

    def test_user_executor_uses_only_active_source_components(self):
        """User-side target sampling should skip inactive source components."""
        unit_test_out_network_only_component.calls = 0
        executor = DefaultUserExecutor(
            {
                "content_embedding_generation_function": {"dummy_content_embedding": {}},
                "feed_weight_components": [
                    {
                        "appeal_component_function": {"unit_test_out_network_only": {}},
                        "importance_in": 0.0,
                        "importance_out": 1.0,
                    }
                ],
            }
        )
        user = UserProfile(
            user_id=1,
            user_type="normal",
            planned_action_count=2,
            user_behavior={"p_actions": p_actions(like=1.0)},
        )
        feed = RankedFeed(
            items=[
                FeedCandidate(
                    action_id=41,
                    creator_user_id=2,
                    action_type="post",
                    network_source=IN_NETWORK,
                ),
                FeedCandidate(
                    action_id=42,
                    creator_user_id=3,
                    action_type="post",
                    network_source=OUT_NETWORK,
                ),
            ]
        )

        with patch("subcomponents.user_executor.random.expovariate", return_value=100.0):
            with patch("subcomponents.user_executor.random.random", return_value=0.0):
                result = executor.execute_session(user, feed)

        self.assertEqual(len(result.actions), 2)
        self.assertEqual(unit_test_out_network_only_component.calls, 1)

    def test_user_executor_does_not_evaluate_zero_importance_components(self):
        """User-side scoring should skip components inactive in both sources."""
        unit_test_counted_component.calls = 0
        executor = DefaultUserExecutor(
            {
                "content_embedding_generation_function": {"dummy_content_embedding": {}},
                "feed_weight_components": [
                    {
                        "appeal_component_function": {"unit_test_counted": {}},
                        "importance_in": 0.0,
                        "importance_out": 0.0,
                    }
                ],
            }
        )
        user = UserProfile(
            user_id=1,
            user_type="normal",
            planned_action_count=1,
            user_behavior={"p_actions": p_actions(like=1.0)},
        )
        feed = RankedFeed(
            items=[
                FeedCandidate(
                    action_id=41,
                    creator_user_id=2,
                    action_type="post",
                    network_source=IN_NETWORK,
                )
            ]
        )

        with patch("subcomponents.user_executor.random.expovariate", return_value=0.1):
            with patch("subcomponents.user_executor.random.random", return_value=0.0):
                result = executor.execute_session(user, feed)

        self.assertEqual(len(result.actions), 1)
        self.assertEqual(unit_test_counted_component.calls, 0)

    def test_feed_builder_rejects_out_of_bounds_component_value(self):
        """Feed ranking should enforce the unit-interval component contract."""
        builder = DefaultFeedBuilder(
            recsys_config(
                feed_size=1,
                components=[
                    {
                        "appeal_component_function": {"unit_test_out_of_bounds": {}},
                        "importance_in": 1.0,
                        "importance_out": 1.0,
                    }
                ],
            )
        )

        with self.assertRaisesRegex(ValueError, "unit_test_out_of_bounds"):
            builder.build_feed(
                UserProfile(user_id=1, user_type="normal"),
                [feed_candidate(1, IN_NETWORK, timestamp=10, appeal=1.0)],
            )

    def test_feed_builder_rejects_unknown_network_source(self):
        """Feed candidates should be tagged as in-network or out-of-network."""
        builder = DefaultFeedBuilder(recsys_config(feed_size=1))

        with self.assertRaisesRegex(ValueError, "network_source"):
            builder.build_feed(
                UserProfile(user_id=1, user_type="normal"),
                [feed_candidate(1, "unknown", timestamp=10, appeal=1.0)],
            )


def feed_candidate(
    action_id,
    network_source,
    *,
    timestamp,
    appeal,
    engagement_like=1,
    engagement_repost=1,
    engagement_reply=1,
    engagement_quote=1,
    repeated_like_n_reshare_creator_count=0,
    views_by_others_count=0,
    content_embedding=None,
):
    """Return one feed candidate for RecSys feed-mix tests."""
    return FeedCandidate(
        action_id=action_id,
        creator_user_id=action_id + 100,
        action_type="post",
        content_embedding=[1.0] if content_embedding is None else content_embedding,
        timestamp=timestamp,
        network_source=network_source,
        repeated_like_n_reshare_creator_count=repeated_like_n_reshare_creator_count,
        views_by_others_count=views_by_others_count,
        other_properties={
            "appeal": appeal,
            "engagement_like": engagement_like,
            "engagement_repost": engagement_repost,
            "engagement_reply": engagement_reply,
            "engagement_quote": engagement_quote,
        },
    )


if __name__ == "__main__":
    unittest.main()
