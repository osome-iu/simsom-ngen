# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Shared process runtime context.
"""

from dataclasses import dataclass
from typing import Any

from mpi4py import MPI

from mpi_layout import MpiLayout, Role


@dataclass(frozen=True)
class ProcessContext:
    """
    Immutable runtime envelope passed to one process component.

    MPI rank/role data stays here so component loops can receive one typed
    context object instead of reaching into global MPI state or config loaders.
    """

    comm_world: MPI.Intracomm
    rank: int
    size: int
    role: Role
    mpi_layout: MpiLayout
    config: Any
