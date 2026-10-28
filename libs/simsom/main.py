# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
MPI entry point.

Process roles are intentionally thin dispatchers. Component owners should work
in the dedicated modules under `components/` and the shared DB contract under
`db/` instead of adding role-specific behavior here.
"""

import sys
import logging
import time
from mpi4py import MPI
import argparse
from logger_helper import get_logger
from config_helper import CONFIG, init_config
from mpi_layout import MpiLayout, Role
from components.runtime import ProcessContext


def main() -> int:

    # Load configurations
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--simulator_spec",
        type=str,
        default=None,
        help="File that contains configuration for the simulation",
    )
    args = parser.parse_args()
    try:
        init_config(args.simulator_spec)
    except (ValueError, TypeError) as exc:
        logging.getLogger(__name__).error("Invalid simulator configuration: %s", exc)
        return 2
    logger = get_logger(__name__ + ".Main")

    mpi_layout = MpiLayout.from_config(CONFIG.mpi)

    # Initialize MPI
    comm_world = MPI.COMM_WORLD
    size = comm_world.Get_size()
    rank = comm_world.Get_rank()

    # Check minimum number of processes
    min_size = mpi_layout.minimum_size
    if size < min_size:
        if rank == 0:
            msg = f"This program requires at least {min_size} processes"
            logger.error(msg)
            print(msg, flush=True)
        sys.exit(1)

    try:
        if rank == mpi_layout.rank(Role.CONTROLLER):
            from components.controller import run_controller

            run_controller(_build_context(comm_world, rank, size, Role.CONTROLLER, mpi_layout))

        else:
            _wait_for_controller_setup(comm_world)

        if rank == mpi_layout.rank(Role.ANALYTICS):
            from components.analytics import run_analytics

            run_analytics(_build_context(comm_world, rank, size, Role.ANALYTICS, mpi_layout))

        elif rank == mpi_layout.rank(Role.POLICY):
            from components.policy import run_policy

            run_policy(_build_context(comm_world, rank, size, Role.POLICY, mpi_layout))

        elif mpi_layout.is_worker(rank):
            from components.worker import run_worker

            run_worker(_build_context(comm_world, rank, size, Role.WORKER, mpi_layout))
    except KeyboardInterrupt:
        logger.warning("SimSoM process interrupted by user: rank=%d", rank)
        if rank == mpi_layout.rank(Role.CONTROLLER):
            print("SimSoM simulation interrupted by user.", flush=True)
        return 130

    return 0


def _build_context(
    comm_world: MPI.Intracomm,
    rank: int,
    size: int,
    role: Role,
    mpi_layout: MpiLayout,
) -> ProcessContext:
    return ProcessContext(
        comm_world=comm_world,
        rank=rank,
        size=size,
        role=role,
        mpi_layout=mpi_layout,
        config=CONFIG,
    )


def _wait_for_controller_setup(comm_world: MPI.Intracomm) -> None:
    """Wait for setup without continuously occupying a CPU core."""
    setup_barrier = comm_world.Ibarrier()
    while not setup_barrier.Test():
        time.sleep(0.1)


if __name__ == "__main__":

    sys.exit(main())
