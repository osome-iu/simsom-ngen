# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Controller process

Responsibilities:
- Create the Postgres schema, install/enable DB-owned clock integration, and
  populate initial state.
- Own lifecycle coordination.
- Dump final DB state.
- Coordinate MPI stop messages and completion acknowledgments after termination.
"""

from datetime import datetime, timezone
import sys
import time
from typing import Any
from mpi4py import MPI

from db.controller import (
    ControllerDatabase,
    InitialStateCounts,
    SetupDbDataset,
    SetupDbStream,
    create_controller_database_client,
)
from logger_helper import get_logger, get_setup_status_file_path
from db.core import CLOCK_CONFIGURE_FUNCTION, CLOCK_NEXTVAL_FUNCTION
from mpi_protocol import MpiMessage, send_control_message
from mpi_layout import Role
from subcomponents.setup_db import DefaultSetupDbPlanner, SetupDbPlanner
from setup_progress import SetupProgressReporter

from components.runtime import ProcessContext

CONTROLLER_TERMINATION_POLL_SECONDS = 1.0
# Each shutdown wait gets its own deadline. The configured value may override
# this default without affecting recurring analytics checkpoints.
FINAL_ANALYTICS_TIMEOUT_SECONDS = 60.0


def run_controller(context: ProcessContext) -> None:
    """Run setup, wait for a stop request, then close the run cleanly.

    See docs/algorithms/controller.md#main-steps.
    """
    run_started_at = datetime.now(timezone.utc)
    logger = get_logger(__name__ + ".Controller")
    # Every MPI rank validates config, but only setup owns warning output.
    # Emit after the file logger exists so warnings are retained in simsom.log.
    for warning in context.config.validation_warnings:
        logger.warning("%s", warning)
        print(f"Configuration warning: {warning}", file=sys.stderr, flush=True)
    database = create_controller_database_client(context.config.database)
    setup_progress = SetupProgressReporter(
        get_setup_status_file_path(),
        logger=logger,
    )
    setup_db_planner = DefaultSetupDbPlanner(
        context.config.setup_db,
        context.config.user_behavior,
        setup_progress=setup_progress,
    )

    try:
        try:
            setup_progress.begin("setup")
            initial_state = plan_initial_state_stream(setup_db_planner)
            counts = setup_database(
                database,
                initial_state,
                started_at=run_started_at,
                logger=logger,
                database_config=context.config.database,
                setup_progress=setup_progress,
            )
            setup_progress.begin(
                "setup",
                users=counts.users,
                follower_edges=counts.follower_edges,
            )
            setup_progress.complete()
        except BaseException as error:
            setup_progress.fail(error)
            raise
        finally:
            setup_progress.close()

        release_processes_after_setup(context)

        reason = wait_for_termination(database)
        logger.info("Controller observed termination request: %s", reason)
        coordinate_process_shutdown(
            context,
            reason=reason,
            policy_enabled=_require_bool("policy.enabled", context.config.policy.enabled),
            timeout_seconds=getattr(
                context.config.controller,
                "final_analytics_timeout_seconds",
                FINAL_ANALYTICS_TIMEOUT_SECONDS,
            ),
            logger=logger,
        )
        logger.info("Controller observed final analytics summary")

        if _require_bool(
            "controller.dump_on_termination",
            context.config.controller.dump_on_termination,
        ):
            logger.info("Controller dumping DB state before MPI termination")
            database.dump()

    finally:
        database.close()


def setup_database(
    database: ControllerDatabase,
    dataset: SetupDbDataset | SetupDbStream,
    *,
    database_config: Any,
    started_at: datetime | None = None,
    logger: Any | None = None,
    setup_progress: SetupProgressReporter | None = None,
) -> InitialStateCounts:
    """Create/reset DB state and write the typed initial dataset.

    See docs/algorithms/controller.md#bounded-user-pipeline.
    """
    if not isinstance(dataset, (SetupDbDataset, SetupDbStream)):
        raise TypeError(
            "dataset must be SetupDbDataset or SetupDbStream, "
            f"got {type(dataset).__name__}"
        )

    if setup_progress is not None:
        setup_progress.begin("database_schema")
    _log_clock_bootstrap_start(logger, database_config)
    database.setup_schema()
    _log_clock_bootstrap_complete(logger, database_config)
    if setup_progress is not None:
        setup_progress.complete()

    user_count = (
        len(dataset.users) if isinstance(dataset, SetupDbDataset) else dataset.user_count
    )
    total_rows = (
        len(dataset.users) + len(dataset.follower_network)
        if isinstance(dataset, SetupDbDataset)
        else None
    )
    if setup_progress is not None and isinstance(dataset, SetupDbDataset):
        setup_progress.begin(
            "database_load",
            progress_total=total_rows,
            progress_unit="rows_sent",
            table="user",
            users=user_count,
            follower_edges=len(dataset.follower_network),
        )
    last_table = None

    def report_load(table: str, current: int, total: int | None) -> None:
        nonlocal last_table
        if setup_progress is None:
            return
        if table == "user_complete":
            setup_progress.complete(users_generated=user_count, rows_sent=current)
            last_table = table
            return
        if table in {"user", "follower_network"}:
            if isinstance(dataset, SetupDbDataset):
                setup_progress.update(
                    progress_current=current,
                    progress_total=total,
                    rows_sent=current,
                    table=table,
                )
            elif setup_progress.is_running:
                setup_progress.update(rows_sent=current, table=table)
        elif table == "indexes":
            setup_progress.begin(
                "database_indexes",
                table=table,
                rows_sent=current,
                step="secondary_indexes_and_planner_statistics",
            )
        last_table = table

    _log_clock_run_configuration_start(logger, database_config, user_count)
    counts = database.populate_initial_state(
        dataset,
        started_at=started_at,
        progress=report_load if setup_progress is not None else None,
    )
    _log_clock_run_configuration_complete(logger, database_config, user_count)
    if setup_progress is not None:
        setup_progress.complete(table="complete")
    return counts


def release_processes_after_setup(context: ProcessContext) -> None:
    """Release non-controller ranks after setup has created usable DB state."""
    context.comm_world.Ibarrier().Wait()


def plan_initial_state(setup_db_planner: SetupDbPlanner) -> SetupDbDataset:
    """Ask the setup planner for typed initial users and friend links.

    See docs/algorithms/controller.md#user-generation-math.
    """
    dataset = setup_db_planner.build_dataset()
    if not isinstance(dataset, SetupDbDataset):
        raise TypeError(
            "setup_db_planner.build_dataset() must return SetupDbDataset, "
            f"got {type(dataset).__name__}"
        )
    return dataset


def plan_initial_state_stream(setup_db_planner: SetupDbPlanner) -> SetupDbStream:
    """Ask the setup planner for a bounded-memory one-pass initial state."""
    stream = setup_db_planner.build_stream()
    if not isinstance(stream, SetupDbStream):
        raise TypeError(
            "setup_db_planner.build_stream() must return SetupDbStream, "
            f"got {type(stream).__name__}"
        )
    return stream


def wait_for_termination(
    database: ControllerDatabase,
    *,
    poll_seconds: float = CONTROLLER_TERMINATION_POLL_SECONDS,
) -> str:
    """Wait until lifecycle state contains a stop request, then return its reason.

    Analytics owns metric-based stop signals. The controller observes those
    signals and records the final termination request once.

    See docs/algorithms/simulation.md#authoritative-termination-state.
    """
    _require_non_negative_number("poll_seconds", poll_seconds)
    while True:
        state = database.get_termination_state()
        if state.should_terminate:
            return state.reason or "termination requested through shared lifecycle state"
        reason = controller_termination_reason(lifecycle_state=state)
        if reason is not None:
            database.request_termination(
                reason, termination_criteria=state.analytics_termination_criteria
            )
            # A concurrent request may have won; always return the accepted reason.
            continue
        time.sleep(poll_seconds)


def controller_termination_reason(
    *,
    lifecycle_state: Any | None = None,
) -> str | None:
    """Return the lifecycle-published stop reason, or `None` to keep running."""
    if lifecycle_state is not None and getattr(
        lifecycle_state,
        "analytics_stop_requested",
        False,
    ):
        _validate_analytics_termination_signal(lifecycle_state)
        signal_reason = getattr(lifecycle_state, "analytics_stop_reason", None)
        if signal_reason:
            return signal_reason
        return "analytics stop signal"
    return None


def _validate_analytics_termination_signal(lifecycle_state: Any) -> None:
    """Validate the generic criterion records sent by Analytics."""
    criteria = getattr(lifecycle_state, "analytics_termination_criteria", [])
    if not isinstance(criteria, list):
        raise TypeError("analytics_termination_criteria must be a list")
    if not criteria:
        raise ValueError("analytics_termination_criteria must not be empty")
    for index, criterion in enumerate(criteria):
        if not isinstance(criterion, dict):
            raise TypeError(
                f"analytics_termination_criteria[{index}] must be a dict"
            )
        if criterion.get("met") is not True:
            raise ValueError(
                f"analytics_termination_criteria[{index}].met must be true"
            )
        function_name = criterion.get("function_name")
        if not isinstance(function_name, str) or not function_name:
            raise ValueError(
                f"analytics_termination_criteria[{index}].function_name "
                "must be a non-empty string"
            )
        reason = criterion.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise TypeError(
                f"analytics_termination_criteria[{index}].reason must be a string or null"
            )


def broadcast_termination(
    context: ProcessContext,
    reason: str,
    *,
    policy_enabled: bool = True,
) -> None:
    """Send a structured stop request to every non-controller rank."""
    if not isinstance(reason, str):
        raise TypeError(f"reason must be str, got {type(reason).__name__}")

    requests = [
        send_control_message(
            context.comm_world,
            rank,
            MpiMessage.STOP_REQUEST,
            reason=reason,
        )
        for rank in range(context.size)
        if rank != context.rank
        and (policy_enabled or rank != context.mpi_layout.rank(Role.POLICY))
    ]
    for request in requests:
        request.wait()


def coordinate_process_shutdown(
    context: ProcessContext,
    *,
    reason: str,
    policy_enabled: bool,
    timeout_seconds: float,
    logger: Any | None = None,
) -> None:
    """Stop other ranks and use a fresh timeout for each shutdown phase."""
    broadcast_termination(context, reason, policy_enabled=policy_enabled)
    expected_processes = set(context.mpi_layout.worker_ranks(context.size))
    if policy_enabled:
        expected_processes.add(context.mpi_layout.rank(Role.POLICY))
    analytics_rank = context.mpi_layout.rank(Role.ANALYTICS)
    terminated: set[int] = set()
    analytics_ready = False
    deadline = time.monotonic() + timeout_seconds
    while terminated != expected_processes or not analytics_ready:
        if time.monotonic() >= deadline:
            missing_processes = sorted(expected_processes - terminated)
            _log_shutdown_timeout(
                logger,
                "process termination and analytics readiness",
                missing_processes=missing_processes,
                analytics_ready=analytics_ready,
            )
            raise TimeoutError(
                "timed out waiting for worker/policy termination and analytics readiness"
            )
        status = MPI.Status()
        if not context.comm_world.iprobe(source=MPI.ANY_SOURCE, status=status):
            time.sleep(0.01)
            continue
        source = status.Get_source()
        message = context.comm_world.recv(source=source)
        if not isinstance(message, dict):
            raise ValueError("received invalid shutdown message")
        kind = MpiMessage(message.get("message"))
        if kind == MpiMessage.PROCESS_TERMINATED and source in expected_processes:
            _validate_shutdown_ack(
                message,
                source=source,
                expected_role=(
                    Role.POLICY.value
                    if source == context.mpi_layout.rank(Role.POLICY)
                    else Role.WORKER.value
                ),
                message_name=MpiMessage.PROCESS_TERMINATED,
            )
            terminated.add(source)
        elif kind == MpiMessage.ANALYTICS_READY_FOR_FINAL and source == analytics_rank:
            _validate_shutdown_ack(
                message,
                source=source,
                expected_role=Role.ANALYTICS.value,
                message_name=MpiMessage.ANALYTICS_READY_FOR_FINAL,
            )
            analytics_ready = True

    send_control_message(
        context.comm_world,
        analytics_rank,
        MpiMessage.FINAL_ANALYSIS,
    ).wait()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = MPI.Status()
        if not context.comm_world.iprobe(source=analytics_rank, status=status):
            time.sleep(0.01)
            continue
        message = context.comm_world.recv(source=analytics_rank)
        if not isinstance(message, dict):
            raise ValueError("received invalid final analytics message")
        kind = MpiMessage(message.get("message"))
        if kind != MpiMessage.FINAL_ANALYTICS_COMPLETE:
            raise RuntimeError(
                "expected final_analytics_complete, "
                f"received {kind.value}"
            )
        _validate_shutdown_ack(
            message,
            source=analytics_rank,
            expected_role=Role.ANALYTICS.value,
            message_name=MpiMessage.FINAL_ANALYTICS_COMPLETE,
        )
        return
    _log_shutdown_timeout(
        logger,
        "final analytics completion",
        missing_processes=[],
        analytics_ready=True,
    )
    raise TimeoutError("timed out waiting for final analytics completion")


def _log_shutdown_timeout(
    logger: Any | None,
    phase: str,
    *,
    missing_processes: list[int],
    analytics_ready: bool,
) -> None:
    """Log which shutdown acknowledgments were still missing at timeout."""
    if logger is None:
        return
    logger.error(
        "Controller shutdown timeout during %s: missing_processes=%s "
        "analytics_ready=%s",
        phase,
        missing_processes,
        analytics_ready,
    )


def _validate_shutdown_ack(
    message: dict[str, Any],
    *,
    source: int,
    expected_role: str,
    message_name: MpiMessage,
) -> None:
    """Validate the rank and role carried by one shutdown acknowledgment."""
    if message.get("rank") != source:
        raise ValueError(
            f"{message_name.value} rank does not match MPI source {source}"
        )
    if message.get("role") != expected_role:
        raise ValueError(
            f"{message_name.value} from rank {source} must identify role "
            f"{expected_role!r}"
        )


def _log_clock_bootstrap_start(logger: Any | None, database_config: Any) -> None:
    """Log whether schema setup will install the simulator clock extension."""
    if logger is None:
        return
    clock = _clock_log_context(database_config)
    logger.info(
        "Clock extension bootstrap starting: extension=%s schema=%s "
        "default_function=%s.%s",
        clock["name"],
        clock["schema"],
        clock["schema"],
        CLOCK_NEXTVAL_FUNCTION,
    )


def _log_clock_bootstrap_complete(
    logger: Any | None,
    database_config: Any,
) -> None:
    """Log that schema setup has recreated and wired the clock extension."""
    if logger is None:
        return
    clock = _clock_log_context(database_config)
    logger.info(
        "Clock extension bootstrap complete: extension=%s recreated; "
        "action.timestamp default wired to %s.%s",
        clock["name"],
        clock["schema"],
        CLOCK_NEXTVAL_FUNCTION,
    )


def _log_clock_run_configuration_start(
    logger: Any | None,
    database_config: Any,
    user_count: int,
) -> None:
    """Log the run-specific clock configuration before initial-state load."""
    if logger is None:
        return
    clock = _clock_log_context(database_config)
    logger.info(
        "Clock extension run configuration starting: "
        "function=%s user_count=%d tick_reset_ms=0 min_step_ms=1",
        CLOCK_CONFIGURE_FUNCTION,
        user_count,
    )


def _log_clock_run_configuration_complete(
    logger: Any | None,
    database_config: Any,
    user_count: int,
) -> None:
    """Log that run-specific simulator clock state has been reset."""
    if logger is None:
        return
    clock = _clock_log_context(database_config)
    logger.info(
        "Clock extension run configuration complete: "
        "function=%s user_count=%d current_tick_ms=0",
        CLOCK_CONFIGURE_FUNCTION,
        user_count,
    )


def _clock_log_context(database_config: Any) -> dict[str, Any]:
    """Return clock config fields used only for setup logging."""
    clock_config = _config_value(database_config, "clock_extension")
    return {
        "name": _config_value(clock_config, "name"),
        "schema": _config_value(database_config, "schema"),
    }


def _require_non_negative_number(name: str, value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")
    return float(value)


def _require_positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be greater than 0")
    return value


def _require_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool, got {type(value).__name__}")
    return value


def _config_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"Missing required config field: {key}")
        return value[key]
    if not hasattr(value, key):
        raise ValueError(f"Missing required config field: {key}")
    return getattr(value, key)
