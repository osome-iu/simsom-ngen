# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Policy process

The policy process reads latest eligible supported-action targets through a
persisted cursor, evaluates policy rules, and writes moderation actions back to
Postgres.
"""

import time

from db.policy import (
    DEFAULT_POLICY_TARGET_BATCH_SIZE,
    PolicyCursor,
    PolicyModerationAction,
    PolicyTargetRecord,
    create_policy_database_client,
)
from logger_helper import get_logger
from mpi_layout import Role
from mpi_protocol import MpiMessage, receive_control_message, send_control_message
from subcomponents.policy_rules import (
    DefaultPolicyEvaluator,
    PolicyDecision,
    PolicyEvaluator,
    ReviewTarget,
)

from components.runtime import ProcessContext


def run_policy(context: ProcessContext) -> None:
    """Create policy dependencies and process action targets until shutdown.

    See docs/algorithms/policy.md#main-steps.
    """
    logger = get_logger(__name__ + ".Policy")
    if not _require_bool("policy.enabled", context.config.policy.enabled):
        logger.info("Policy process disabled")
        _send_process_terminated(context)
        return

    database = create_policy_database_client(
        context.config.database,
        target_batch_size=getattr(
            context.config.policy,
            "target_batch_size",
            DEFAULT_POLICY_TARGET_BATCH_SIZE,
        ),
    )
    policy_evaluator = DefaultPolicyEvaluator(context.config.policy)

    completed = False
    try:
        logger.info("Policy process starting")
        run_policy_loop(
            database=database,
            policy_evaluator=policy_evaluator,
            poll_interval_milliseconds=context.config.policy.poll_interval_milliseconds,
            comm_world=context.comm_world,
        )
        completed = True
    finally:
        database.close()
        if completed:
            _send_process_terminated(context)


def run_policy_loop(
    *,
    database,
    policy_evaluator: PolicyEvaluator,
    poll_interval_milliseconds: float,
    comm_world=None,
) -> None:
    """Review new supported actions until the controller sends a stop request.

    See docs/algorithms/policy.md#main-steps.
    Backoff rationale: docs/algorithms/policy.md#polling-and-performance.
    """
    _require_non_negative_number(
        "poll_interval_milliseconds",
        poll_interval_milliseconds,
    )

    cursor: PolicyCursor | None = database.fetch_policy_cursor()
    # Zero is the no-delay baseline. Back off passes with no interventions
    # to test contention versus review latency; productive passes keep going.
    while comm_world is not None or not database.should_terminate():
        if _stop_requested(comm_world):
            return
        targets = database.fetch_moderation_targets(cursor=cursor)
        next_cursor = policy_cursor_after(targets, cursor=cursor)
        if not targets:
            if next_cursor is not None and next_cursor != cursor:
                database.record_policy_cursor(next_cursor)
                cursor = next_cursor
            if poll_interval_milliseconds > 0:
                time.sleep(poll_interval_milliseconds / 1000.0)
            continue
        actions = evaluate_policy(targets, policy_evaluator)
        if actions:
            database.write_moderation_actions(actions)
        # Save progress only after writes succeed. A restart may revisit
        # committed actions; evaluation skips identical stored interventions.
        database.record_policy_cursor(next_cursor)
        cursor = next_cursor
        if not actions and poll_interval_milliseconds > 0:
            time.sleep(poll_interval_milliseconds / 1000.0)


def evaluate_policy(
    targets: list[PolicyTargetRecord],
    policy_evaluator: PolicyEvaluator,
) -> list[PolicyModerationAction]:
    """Evaluate targets and convert matching policy decisions into writes."""
    decisions = policy_evaluator.evaluate(to_review_targets(targets))
    return to_policy_moderation_actions(decisions)


def policy_cursor_after(
    targets: list[PolicyTargetRecord],
    *,
    cursor: PolicyCursor | None = None,
) -> PolicyCursor | None:
    """Return the high-water cursor after the fetched action targets.

    See docs/algorithms/policy.md#cursor-and-values-read-for-each-pass.
    """
    batch_cursor = getattr(targets, "next_cursor", None)
    action_ids = [target.cursor_action_id for target in targets]
    if batch_cursor is not None:
        action_ids.append(batch_cursor.action_id)
    if cursor is not None:
        action_ids.append(cursor.action_id)
    if not action_ids:
        return None
    return PolicyCursor(action_id=max(action_ids))


def to_review_targets(targets: list[PolicyTargetRecord]) -> list[ReviewTarget]:
    """Convert database policy rows into evaluator input objects."""
    return [
        ReviewTarget(
            subject_id=target.subject_id,
            action_type=target.action_type,
            current_timestamp=target.current_timestamp,
            moderation_actions=target.moderation_actions,
            other_properties=target.other_properties,
        )
        for target in targets
    ]


def _require_non_negative_number(name: str, value) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")


def _require_bool(name: str, value) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool, got {type(value).__name__}")
    return value


def to_policy_moderation_actions(
    decisions: list[PolicyDecision],
) -> list[PolicyModerationAction]:
    """Convert decisions to DB moderation writes."""
    return [
        PolicyModerationAction(
            subject_id=decision.subject_id,
            interventions=decision.interventions,
        )
        for decision in decisions
        if decision.interventions
    ]


def _stop_requested(comm_world) -> bool:
    """Return whether the controller has sent a stop request."""
    if comm_world is None:
        return False
    message = receive_control_message(comm_world)
    return message is not None and message[0] == MpiMessage.STOP_REQUEST


def _send_process_terminated(context) -> None:
    """Acknowledge shutdown when this process is running under MPI."""
    if not hasattr(context, "comm_world") or not hasattr(context, "mpi_layout"):
        return
    send_control_message(
        context.comm_world,
        context.mpi_layout.rank(Role.CONTROLLER),
        MpiMessage.PROCESS_TERMINATED,
        rank=context.rank,
        role=context.role.value,
    ).wait()
