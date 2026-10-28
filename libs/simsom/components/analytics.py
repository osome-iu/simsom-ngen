# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Analytics process

The analytics process evaluates recurring work at simulation-progress
checkpoints, then produces one full report after controller shutdown.
"""

import json
import time
from typing import Any

from db.analytics import (
    AnalyticsStopSignal,
    AnalyticsDatabase,
    AnalyticsSummaryRecord,
    AnalyticsSnapshot,
    create_analytics_database_client,
)
from logger_helper import get_logger
from mpi_layout import Role
from mpi_protocol import (
    MpiMessage,
    receive_control_message,
    send_control_message,
    wait_for_control_message,
)
from subcomponents.analytics_report import (
    AnalyticsAnalyzer,
    AnalyticsMetrics,
    AnalyticsReport,
    DefaultAnalyticsAnalyzer,
)
from subcomponents.analytics_output import (
    AnalyticsFunctionRunner,
    AnalyticsJsonlWriter,
)
from domain.termination_semantics import (
    TerminationCriterionResult,
)
from domain.analytics_semantics import AnalyticsEvaluationInterval

from components.runtime import ProcessContext

FINAL_SUMMARY_POLL_SECONDS = 0.25
FINAL_SUMMARY_STABLE_POLLS = 2


def run_analytics(context: ProcessContext) -> None:
    """Create analytics dependencies and run until the simulator is stopping.

    See docs/algorithms/analytics.md#recurring-checks.
    """
    logger = get_logger(__name__ + ".Analytics")
    database = create_analytics_database_client(context.config.database)
    analytics_analyzer = DefaultAnalyticsAnalyzer(
        context.config.termination,
    )
    analytics_function_runner = AnalyticsFunctionRunner(context.config.analytics)
    output_writer: AnalyticsJsonlWriter | None = None

    try:
        output_writer = AnalyticsJsonlWriter(
            context.config.system.log.path,
            context.config.termination.termination_metrics_file,
            context.config.analytics.analytics_output_file,
        )
        logger.info("Analytics process starting")
        evaluation_interval = AnalyticsEvaluationInterval.from_config(
            context.config.analytics.evaluation_interval
        )
        logger.info(
            "Recurring analytics interval: unit=%s value=%s",
            evaluation_interval.unit,
            evaluation_interval.value,
        )
        logger.info("Termination metrics JSONL: %s", output_writer.termination_path)
        logger.info("Analytics output JSONL: %s", output_writer.analytics_path)
        run_analytics_loop(
            database=database,
            analytics_analyzer=analytics_analyzer,
            evaluation_interval=evaluation_interval,
            logger=logger,
            comm_world=context.comm_world,
            controller_rank=context.mpi_layout.rank(Role.CONTROLLER),
            process_rank=context.rank,
            process_role=context.role.value,
            analytics_function_runner=analytics_function_runner,
            output_writer=output_writer,
        )
    finally:
        if output_writer is not None:
            output_writer.close()
        database.close()


def run_analytics_loop(
    *,
    database: AnalyticsDatabase,
    analytics_analyzer: AnalyticsAnalyzer,
    evaluation_interval: AnalyticsEvaluationInterval,
    idle_sleep_seconds: float = 0,
    logger: Any = None,
    comm_world=None,
    controller_rank: int | None = None,
    process_rank: int | None = None,
    process_role: str = Role.ANALYTICS.value,
    analytics_function_runner: AnalyticsFunctionRunner | None = None,
    output_writer: AnalyticsJsonlWriter | None = None,
) -> None:
    """Run recurring work at progress checkpoints until controller shutdown.

    Recurring analytics functions and all termination criteria share the same
    checkpoint. The final pass remains separate and reads the stable end state.

    See docs/algorithms/analytics.md#recurring-checks.
    """
    if not isinstance(evaluation_interval, AnalyticsEvaluationInterval):
        raise TypeError("evaluation_interval must be AnalyticsEvaluationInterval")
    _require_non_negative_number("idle_sleep_seconds", idle_sleep_seconds)
    last_evaluated_checkpoint = 0

    while True:
        if comm_world is not None:
            message = receive_control_message(comm_world)
            if message is not None and message[0] == MpiMessage.STOP_REQUEST:
                send_control_message(
                    comm_world,
                    controller_rank,
                    MpiMessage.ANALYTICS_READY_FOR_FINAL,
                    rank=process_rank,
                    role=process_role,
                ).wait()
                wait_for_control_message(comm_world, MpiMessage.FINAL_ANALYSIS)
                record_final_analytics_summary(
                    database=database,
                    analytics_analyzer=analytics_analyzer,
                    lifecycle_state=database.get_termination_state(),
                    logger=logger,
                    analytics_function_runner=analytics_function_runner,
                    output_writer=output_writer,
                )
                send_control_message(
                    comm_world,
                    controller_rank,
                    MpiMessage.FINAL_ANALYTICS_COMPLETE,
                    rank=process_rank,
                    role=process_role,
                ).wait()
                return
        lifecycle_state = None
        if comm_world is None:
            lifecycle_state = database.get_termination_state()
        if lifecycle_state is not None and lifecycle_state.should_terminate:
            if comm_world is not None:
                send_control_message(
                    comm_world,
                    controller_rank,
                    MpiMessage.ANALYTICS_READY_FOR_FINAL,
                    rank=process_rank,
                    role=process_role,
                ).wait()
                wait_for_control_message(comm_world, MpiMessage.FINAL_ANALYSIS)
            record_final_analytics_summary(
                database=database,
                analytics_analyzer=analytics_analyzer,
                lifecycle_state=lifecycle_state,
                logger=logger,
                analytics_function_runner=analytics_function_runner,
                output_writer=output_writer,
            )
            if comm_world is not None:
                send_control_message(
                    comm_world,
                    controller_rank,
                    MpiMessage.FINAL_ANALYTICS_COMPLETE,
                    rank=process_rank,
                    role=process_role,
                ).wait()
            return
        progress = database.fetch_analytics_progress()
        progress_checkpoint = evaluation_interval.checkpoint(
            user_count=progress.user_count,
            action_count=progress.action_id_watermark,
            simulator_timestamp_ms=progress.simulator_timestamp_ms,
        )
        if progress_checkpoint <= last_evaluated_checkpoint:
            _idle_sleep(idle_sleep_seconds)
            continue

        snapshot = database.fetch_termination_metrics()
        snapshot_checkpoint = evaluation_interval.checkpoint(
            user_count=snapshot.user_count,
            action_count=snapshot.action_count,
            simulator_timestamp_ms=snapshot.simulator_timestamp_ms,
        )
        if snapshot_checkpoint <= last_evaluated_checkpoint:
            _idle_sleep(idle_sleep_seconds)
            continue
        last_evaluated_checkpoint = snapshot_checkpoint

        report = analyze_snapshot(snapshot, analytics_analyzer)
        metrics = to_analytics_metrics(snapshot)
        recurring_outputs = (
            analytics_function_runner.run_recurring(metrics)
            if analytics_function_runner is not None
            else {}
        )
        if output_writer is not None:
            output_writer.write_termination(report)
            output_writer.write_analytics("recurring", metrics, recurring_outputs)
        log_analytics_report(report, logger)
        database.record_analytics_summary(
            to_analytics_summary_record(report, final=False)
        )
        if report.termination_requested:
            publish_analytics_report(report, database)
        _idle_sleep(idle_sleep_seconds)


def _idle_sleep(seconds: float) -> None:
    """Yield between progress probes without defining evaluation cadence."""
    if seconds > 0:
        time.sleep(seconds)


def record_final_analytics_summary(
    *,
    database: AnalyticsDatabase,
    analytics_analyzer: AnalyticsAnalyzer,
    lifecycle_state,
    poll_seconds: float = FINAL_SUMMARY_POLL_SECONDS,
    stable_polls: int = FINAL_SUMMARY_STABLE_POLLS,
    logger: Any = None,
    analytics_function_runner: AnalyticsFunctionRunner | None = None,
    output_writer: AnalyticsJsonlWriter | None = None,
) -> None:
    """Record one last summary after workers have stopped changing action counts.

    This makes the newest analytics row the durable final report for the run.
    Preserve the controller's accepted reason and criteria even if a later
    analytics pass publishes a different signal. Final measurements are separate.

    See docs/algorithms/analytics.md#shutdown-and-final-report.
    """
    _require_non_negative_number("poll_seconds", poll_seconds)
    _require_positive_int("stable_polls", stable_polls)

    snapshot = wait_for_stable_analytics_snapshot(
        database,
        poll_seconds=poll_seconds,
        stable_polls=stable_polls,
    )
    report = analyze_snapshot(snapshot, analytics_analyzer)
    if getattr(lifecycle_state, "should_terminate", False):
        other_properties = dict(report.other_properties)
        other_properties["termination_requested"] = True
        other_properties["termination_reason"] = lifecycle_state.reason
        other_properties["termination_source"] = lifecycle_state.termination_source
        other_properties["final_termination_criteria"] = _logged_termination_criteria(report)
        lifecycle_criteria = _termination_criterion_results(
            getattr(lifecycle_state, "termination_criteria", None) or []
        )
        report = AnalyticsReport(
            metrics=report.metrics,
            termination_requested=True,
            termination_reason=lifecycle_state.reason,
            termination_criteria=lifecycle_criteria,
            other_properties=other_properties,
        )
    metrics = to_analytics_metrics(snapshot)
    final_outputs = (
        analytics_function_runner.run_final(metrics)
        if analytics_function_runner is not None
        else {}
    )
    report = AnalyticsReport(
        metrics=report.metrics,
        termination_requested=report.termination_requested,
        termination_reason=report.termination_reason,
        termination_criteria=report.termination_criteria,
        other_properties={
            **report.other_properties,
            "analytics_outputs": final_outputs,
        },
    )
    if output_writer is not None:
        output_writer.write_analytics("final", metrics, final_outputs)
    log_analytics_report(report, logger, final=True)
    database.record_analytics_summary(to_analytics_summary_record(report))


def wait_for_stable_analytics_snapshot(
    database: AnalyticsDatabase,
    *,
    poll_seconds: float,
    stable_polls: int,
) -> AnalyticsSnapshot:
    """Return a snapshot once the action count has stayed the same for a while.

    The repeated polls give in-flight worker transactions time to finish before
    analytics writes the final row.

    See docs/algorithms/analytics.md#shutdown-and-final-report.
    """
    _require_non_negative_number("poll_seconds", poll_seconds)
    _require_positive_int("stable_polls", stable_polls)

    previous_action_count: int | None = None
    stable_count = 0
    latest_snapshot: AnalyticsSnapshot | None = None
    while stable_count < stable_polls:
        latest_snapshot = database.fetch_analytics_snapshot()
        if latest_snapshot.action_count == previous_action_count:
            stable_count += 1
        else:
            previous_action_count = latest_snapshot.action_count
            stable_count = 0
        if stable_count < stable_polls and poll_seconds > 0:
            time.sleep(poll_seconds)

    if latest_snapshot is None:
        raise RuntimeError("final analytics snapshot was not read")
    return latest_snapshot


def analyze_snapshot(
    snapshot: AnalyticsSnapshot,
    analytics_analyzer: AnalyticsAnalyzer,
) -> AnalyticsReport:
    """Turn a database snapshot into an analytics report."""
    return analytics_analyzer.analyze(to_analytics_metrics(snapshot))


def publish_analytics_report(
    report: AnalyticsReport,
    database: AnalyticsDatabase,
) -> None:
    """Persist analytics output as a controller-readable stop signal."""
    database.record_analytics_stop_signal(to_analytics_stop_signal(report))


def log_analytics_report(
    report: AnalyticsReport,
    logger: Any,
    *,
    final: bool = False,
) -> None:
    """Write one structured analytics report to the process log."""
    if logger is None:
        return
    logger.info(
        "Analytics report: %s",
        json.dumps(
            analytics_report_log_payload(report, final=final),
            default=str,
        ),
    )


def analytics_report_log_payload(
    report: AnalyticsReport,
    *,
    final: bool = False,
) -> dict[str, Any]:
    """Build a lightweight recurring or full final analytics log payload."""
    metrics = report.metrics
    if not final:
        return {
            "final": False,
            "termination_requested": report.termination_requested,
            "termination_reason": report.termination_reason,
            "termination_criteria": _logged_termination_criteria(report),
        }

    return {
        "final": True,
        "metrics": {
            "action_count": metrics.action_count,
            "avg_activity_level": metrics.avg_activity_level,
            "avg_action_quality": metrics.avg_action_quality,
        },
        "termination_requested": report.termination_requested,
        "termination_reason": report.termination_reason,
        "termination_criteria": _logged_termination_criteria(report),
        "other_properties": dict(report.other_properties),
    }


def _logged_termination_criteria(
    report: AnalyticsReport,
) -> list[dict[str, Any]]:
    """Convert criterion results to JSON-ready log records."""
    return [
        {
            "met": criterion.met,
            "function_name": criterion.function_name,
            "reason": criterion.reason,
            "details": criterion.details,
        }
        for criterion in report.termination_criteria
    ]


def to_analytics_stop_signal(report: AnalyticsReport) -> AnalyticsStopSignal:
    """Convert a report into the lifecycle signal analytics is allowed to write."""
    return AnalyticsStopSignal(
        stop_requested=report.termination_requested,
        stop_reason=report.termination_reason if report.termination_requested else None,
        termination_criteria=[
            {
                "met": criterion.met,
                "function_name": criterion.function_name,
                "reason": criterion.reason,
                "details": criterion.details,
            }
            for criterion in report.termination_criteria
            if criterion.met
        ],
    )


def to_analytics_summary_record(
    report: AnalyticsReport,
    *,
    final: bool = True,
) -> AnalyticsSummaryRecord:
    """Convert a recurring check or final report into an analytics row."""
    metrics = report.metrics
    return AnalyticsSummaryRecord(
        user_count=metrics.user_count,
        action_count=metrics.action_count,
        avg_activity_level=metrics.avg_activity_level if final else None,
        avg_action_quality=metrics.avg_action_quality,
        termination_criteria=[
            {
                "met": criterion.met,
                "function_name": criterion.function_name,
                "reason": criterion.reason,
                "details": criterion.details,
            }
            for criterion in report.termination_criteria
            if criterion.met
        ],
        other_properties=report.other_properties if final else {},
    )


def to_analytics_metrics(snapshot: AnalyticsSnapshot) -> AnalyticsMetrics:
    """Convert a database snapshot into analyzer input metrics."""
    return AnalyticsMetrics(
        user_count=snapshot.user_count,
        action_count=snapshot.action_count,
        avg_activity_level=snapshot.avg_activity_level,
        avg_action_quality=snapshot.avg_action_quality,
        other_properties=snapshot.other_properties,
    )


def _termination_criterion_results(
    records: list[dict[str, Any]],
) -> list[TerminationCriterionResult]:
    """Convert lifecycle JSON criterion records back to typed results."""
    return [
        TerminationCriterionResult(
            met=bool(record.get("met", True)),
            function_name=str(record["function_name"]),
            reason=record.get("reason"),
            details=record.get("details"),
        )
        for record in records
    ]


def _require_non_negative_number(name: str, value) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")


def _require_positive_int(name: str, value) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be greater than 0")
