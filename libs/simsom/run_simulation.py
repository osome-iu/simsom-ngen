# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Script to run the MPI simulation with configurable number of processes.
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from logger_helper import (
    display_command,
    display_path,
    get_logger,
    get_log_file_path,
    get_setup_status_file_path,
)
from config_helper import CONFIG, init_config
from mpi_layout import MpiLayout
from subcomponents.analytics_output import resolve_analytics_output_path


DEFAULT_MPIEXEC = "mpiexec"
DEFAULT_PYTHON_EXEC = sys.executable
DEFAULT_SCRIPT = str(Path(__file__).with_name("main.py"))
GRACEFUL_INTERRUPT_TIMEOUT_SECONDS = 10.0
FORCED_INTERRUPT_TIMEOUT_SECONDS = 5.0
RUN_STATUS_DIVIDER = "=" * 72


def main():
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument(
        "--simulator_spec",
        type=str,
        default=None,
        help="Config file path passed to the simulator",
    )
    pre_args, _ = pre_parser.parse_known_args()
    try:
        init_config(pre_args.simulator_spec)
    except (ValueError, TypeError) as exc:
        logging.getLogger(__name__).error("Invalid simulator configuration: %s", exc)
        raise SystemExit(2) from None

    logger = get_logger(__name__ + ".Runner")
    log_path = get_log_file_path()
    display_log_path = display_path(log_path)
    display_setup_status_path = display_path(get_setup_status_file_path())
    termination_metrics_path = resolve_analytics_output_path(
        CONFIG.system.log.path,
        CONFIG.termination.termination_metrics_file,
    )
    analytics_output_path = resolve_analytics_output_path(
        CONFIG.system.log.path,
        CONFIG.analytics.analytics_output_file,
    )

    logger.info("=============== Starting SimSoM simulation... ===============")
    logger.info("Using config: %s", display_path(CONFIG.config_file))
    print(f"SimSoM log file: {display_log_path}", flush=True)
    print(f"SimSoM setup status: {display_setup_status_path}", flush=True)
    print(
        f"SimSoM termination metrics: {display_path(termination_metrics_path)}",
        flush=True,
    )
    print(
        f"SimSoM analytics output: {display_path(analytics_output_path)}",
        flush=True,
    )
    _print_run_status(
        f"SimSoM simulation is running. Follow progress in: {display_log_path}",
    )
    mpi_layout = MpiLayout.from_config(CONFIG.mpi)
    default_num_procs = _default_num_procs(CONFIG.mpi, mpi_layout)

    parser = argparse.ArgumentParser(description="Run the MPI simulation.")

    parser.add_argument(
        "-n",
        "--num-procs",
        type=int,
        default=default_num_procs,
        help=f"Number of MPI processes (minimum: {mpi_layout.minimum_size})",
    )

    parser.add_argument(
        "--script",
        type=str,
        default=DEFAULT_SCRIPT,
        help="Simulation entry script",
    )

    parser.add_argument(
        "--mpiexec",
        type=str,
        default=DEFAULT_MPIEXEC,
        help="MPI execution command",
    )

    parser.add_argument(
        "--python-exec",
        type=str,
        default=DEFAULT_PYTHON_EXEC,
        help="Python interpreter command",
    )

    parser.add_argument(
        "--simulator_spec",
        type=str,
        default=pre_args.simulator_spec,
        help="Config file path passed to the simulator",
    )

    args = parser.parse_args()

    if args.num_procs < mpi_layout.minimum_size:
        raise ValueError(
            f"num_procs must be at least {mpi_layout.minimum_size} for the configured MPI layout"
        )

    # Safety check: prevent dangerous commands
    for val in [args.script, args.mpiexec, args.python_exec]:
        if any(x in str(val) for x in ["rm -rf", "rm -r", "rm ", "sudo", ">", "|", ";"]):
            logger.error(f"Unsafe command detected in argument: {val}")
            raise ValueError("Unsafe command detected in arguments. Aborting.")

    command = [args.mpiexec, "-n", str(args.num_procs), args.python_exec, args.script]
    if args.simulator_spec:
        command.extend(["--simulator_spec", args.simulator_spec])

    logger.info("Running command: %s", display_command(command))
    try:
        return_code = _run_mpi_command(command)
        if _is_interrupt_return_code(return_code):
            _print_interrupted(logger, display_log_path)
            raise SystemExit(130)
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, command)
    except subprocess.CalledProcessError as exc:
        message = (
            f"SimSoM simulation failed with exit code {exc.returncode}. "
            f"Log file: {display_log_path}"
        )
        logger.error(message)
        print(message, flush=True)
        raise
    except KeyboardInterrupt:
        _print_interrupted(logger, display_log_path)
        raise SystemExit(130)
    except OSError as exc:
        message = (
            f"SimSoM simulation could not start MPI: {exc}. "
            f"Log file: {display_log_path}"
        )
        logger.error(message)
        print(message, flush=True)
        raise SystemExit(1) from exc

    message = f"SimSoM simulation finished successfully. Log file: {display_log_path}"
    logger.info(message)
    _print_run_status(message, leading_blank=False)


def _print_run_status(message: str, *, leading_blank: bool = True) -> None:
    """Print a terminal-safe banner for a major simulation lifecycle event."""
    if leading_blank:
        print()
    print(RUN_STATUS_DIVIDER)
    print(message)
    print(RUN_STATUS_DIVIDER)
    print(flush=True)


def _run_mpi_command(command: list[str]) -> int:
    """Run MPI in the appropriate process group and return its exit code."""
    # The web API already owns an isolated process group around this runner.
    # Keep MPI in that group so a web stop request reaches the whole tree.
    inherit_process_group = os.environ.get("SIMSOM_INHERIT_MPI_PROCESS_GROUP") == "1"
    process = subprocess.Popen(
        command,
        start_new_session=not inherit_process_group,
    )
    try:
        return process.wait()
    except KeyboardInterrupt:
        _gracefully_stop_process(process)
        raise


def _gracefully_stop_process(process: subprocess.Popen) -> None:
    """Interrupt the MPI group, then force it down if it does not exit."""
    _send_process_signal(process, signal.SIGINT)
    try:
        process.wait(timeout=GRACEFUL_INTERRUPT_TIMEOUT_SECONDS)
        return
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        _send_process_signal(process, signal.SIGTERM)

    try:
        process.wait(timeout=FORCED_INTERRUPT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _send_process_signal(process, signal.SIGKILL)
        process.wait()


def _send_process_signal(process: subprocess.Popen, signum: int) -> None:
    """Send a signal to the MPI process group without masking exit races."""
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signum)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    process.send_signal(signum)


def _is_interrupt_return_code(return_code: int) -> bool:
    """Recognize the standard exit codes produced by an interrupted process."""
    return return_code in {130, -signal.SIGINT}


def _print_interrupted(logger, display_log_path: str) -> None:
    """Log and print a concise manual-interruption message."""
    message = (
        "SimSoM simulation interrupted by user. "
        f"Log file: {display_log_path}"
    )
    logger.warning(message)
    print(message, flush=True)


def _default_num_procs(mpi_config, mpi_layout: MpiLayout) -> int:
    configured_num_procs = mpi_config.num_procs
    if not isinstance(configured_num_procs, int) or isinstance(configured_num_procs, bool):
        raise ValueError("mpi.num_procs must be an integer")

    if configured_num_procs < mpi_layout.minimum_size:
        raise ValueError(
            f"mpi.num_procs must be at least {mpi_layout.minimum_size} for the configured MPI layout"
        )

    return configured_num_procs


if __name__ == "__main__":
    main()
