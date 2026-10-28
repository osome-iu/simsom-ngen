# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
MPI protocol message identifiers.

These tokens are internal process-control protocol, not simulation parameters.
"""

from enum import StrEnum
import time


class MpiMessage(StrEnum):
    STOP_REQUEST = "stop_request"
    PROCESS_TERMINATED = "process_terminated"
    ANALYTICS_READY_FOR_FINAL = "analytics_ready_for_final"
    FINAL_ANALYSIS = "final_analysis"
    FINAL_ANALYTICS_COMPLETE = "final_analytics_complete"


def send_control_message(comm_world, destination: int, message: MpiMessage, **payload):
    """Send one structured lifecycle message to another MPI rank."""
    body = {"message": message.value, **payload}
    return comm_world.isend(body, dest=destination)


def receive_control_message(comm_world):
    """Receive one pending lifecycle message, or return ``None``."""
    if not comm_world.iprobe():
        return None
    message = comm_world.recv()
    if not isinstance(message, dict) or message.get("message") not in {
        item.value for item in MpiMessage
    }:
        raise ValueError("received invalid MPI lifecycle message")
    return MpiMessage(message["message"]), message


def wait_for_control_message(comm_world, expected: MpiMessage, timeout_seconds: float = 60.0):
    """Wait for one expected lifecycle message and fail on timeout."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        message = receive_control_message(comm_world)
        if message is not None:
            if message[0] != expected:
                raise RuntimeError(
                    f"expected {expected.value}, received {message[0].value}"
                )
            return message[1]
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {expected.value}")
