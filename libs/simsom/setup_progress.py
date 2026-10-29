# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Controller-owned setup progress events with an independent heartbeat writer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
from typing import Any


class SetupProgressReporter:
    """Send setup state to a small process that owns the JSONL heartbeat stream."""

    def __init__(
        self,
        path: Path,
        *,
        logger: Any | None = None,
        heartbeat_seconds: float = 9.0,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be greater than zero")
        self.path = Path(path)
        self.display_path = os.path.relpath(self.path.resolve(), Path.cwd())
        self.logger = logger
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(self.path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o644))
        self._state: dict[str, Any] | None = None
        self._last_progress_percent: int | None = None
        self._closed = False
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--writer",
            str(self.path),
            "--heartbeat-seconds",
            str(self.heartbeat_seconds),
        ]
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def begin(
        self,
        stage: str,
        *,
        progress_total: int | None = None,
        progress_unit: str | None = None,
        **details: Any,
    ) -> None:
        """Start one setup stage and publish its initial state immediately."""
        state = {
            "stage": stage,
            "status": "running",
            **details,
        }
        if progress_total is not None:
            state.update(
                progress_current=0,
                progress_total=progress_total,
                progress_percent=0,
            )
        if progress_unit is not None:
            state["progress_unit"] = progress_unit
        self._state = state
        self._last_progress_percent = 0 if progress_total is not None else None
        self._send("stage_started", reset_stage=True)
        if self.logger is not None:
            stage_event = {
                "event": "setup_stage_started",
                "stage": stage,
                "setup_status_file": self.display_path,
                **details,
            }
            if progress_total is not None:
                stage_event["progress_total"] = progress_total
            if progress_unit is not None:
                stage_event["progress_unit"] = progress_unit
            self.logger.info(
                "%s",
                json.dumps(
                    stage_event,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )

    @property
    def is_running(self) -> bool:
        """Return whether the current stage can receive progress updates."""
        return self._state is not None and self._state.get("status") == "running"

    def update(
        self,
        *,
        progress_current: int | None = None,
        progress_total: int | None = None,
        **details: Any,
    ) -> None:
        """Refresh heartbeat state and publish integer percentage changes."""
        if self._state is None or self._state["status"] != "running":
            raise RuntimeError("setup progress update requires a running stage")
        self._state.update(details)
        publish = None
        if progress_current is not None:
            total = (
                progress_total
                if progress_total is not None
                else self._state.get("progress_total")
            )
            if not isinstance(total, int) or total < 0:
                raise ValueError("progress_total must be a non-negative integer")
            if not isinstance(progress_current, int) or not 0 <= progress_current <= total:
                raise ValueError("progress_current must be between zero and progress_total")
            percent = (
                100.0
                if total == 0
                else round(progress_current * 100.0 / total, 6)
            )
            whole_percent = 100 if total == 0 else progress_current * 100 // total
            self._state.update(
                progress_current=progress_current,
                progress_total=total,
                progress_percent=percent,
            )
            if whole_percent != self._last_progress_percent:
                publish = "progress"
                self._last_progress_percent = whole_percent
                if self.logger is not None:
                    self.logger.info(
                        "%s",
                        json.dumps(
                            {"event": "setup_progress", **self._state},
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    )
        self._send(publish)

    def complete(self, **details: Any) -> None:
        """Mark the current setup stage complete and publish immediately."""
        if self._state is None:
            raise RuntimeError("setup progress completion requires an active stage")
        self._state.update(details)
        self._state["status"] = "complete"
        if "progress_total" in self._state:
            total = self._state["progress_total"]
            self._state.update(progress_current=total, progress_percent=100)
        self._send("stage_completed")

    def fail(self, error: BaseException) -> None:
        """Publish setup failure while preserving the stage that failed."""
        if self._state is None:
            self._state = {"stage": "setup"}
        self._state.update(
            status="failed",
            error_type=type(error).__name__,
            error=str(error),
        )
        self._send("stage_failed")

    def close(self) -> None:
        """Stop the writer after it has consumed all submitted setup state."""
        if self._closed:
            return
        try:
            if self._process.poll() is None:
                self._write_command({"command": "close"})
        finally:
            self._closed = True
            if self._process.stdin is not None:
                self._process.stdin.close()
            return_code = self._process.wait()
        if return_code != 0:
            raise RuntimeError(
                f"setup progress writer exited with status {return_code}"
            )

    def _send(
        self,
        publish: str | None,
        *,
        reset_stage: bool = False,
    ) -> None:
        if self._state is None:
            return
        self._write_command(
            {
                "command": "state",
                "state": self._state,
                "publish": publish,
                "reset_stage": reset_stage,
            }
        )

    def _write_command(self, command: dict[str, Any]) -> None:
        if self._closed and command.get("command") != "close":
            raise RuntimeError("setup progress reporter is closed")
        if self._process.poll() is not None:
            raise RuntimeError(
                "setup progress writer stopped before setup completed: "
                f"status={self._process.returncode}"
            )
        if self._process.stdin is None:
            raise RuntimeError("setup progress writer input is unavailable")
        try:
            self._process.stdin.write(
                json.dumps(command, separators=(",", ":"), sort_keys=True) + "\n"
            )
            self._process.stdin.flush()
        except BrokenPipeError as error:
            return_code = self._process.wait()
            raise RuntimeError(
                f"setup progress writer stopped unexpectedly: status={return_code}"
            ) from error


def _run_writer(
    path: Path,
    *,
    heartbeat_seconds: float,
) -> None:
    """Own setup output files and publish heartbeats independent of controller work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    status_fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)

    selector = selectors.DefaultSelector()
    input_fd = sys.stdin.fileno()
    selector.register(input_fd, selectors.EVENT_READ)
    input_buffer = b""
    run_started_at = time.monotonic()
    stage_started_at = run_started_at
    last_published_at = run_started_at
    state: dict[str, Any] | None = None
    sequence = 0

    def publish(now: float, update_type: str) -> None:
        nonlocal last_published_at, sequence
        if state is None:
            return
        sequence += 1
        timestamp = datetime.now(timezone.utc)
        snapshot = {
            **state,
            "update_type": update_type,
            "sequence": sequence,
            "updated_at": timestamp.isoformat(),
            "elapsed_seconds": round(now - run_started_at, 1),
            "stage_elapsed_seconds": round(now - stage_started_at, 1),
        }
        _write_json_line(status_fd, snapshot)
        last_published_at = now

    try:
        while True:
            running = state is not None and state.get("status") == "running"
            timeout = None
            if running:
                timeout = max(
                    0.0,
                    heartbeat_seconds - (time.monotonic() - last_published_at),
                )
            events = selector.select(timeout)
            now = time.monotonic()
            if not events:
                if running:
                    publish(now, "heartbeat")
                continue

            chunk = os.read(input_fd, 65_536)
            if not chunk:
                break
            input_buffer += chunk
            should_close = False
            while b"\n" in input_buffer:
                encoded_command, input_buffer = input_buffer.split(b"\n", 1)
                command = json.loads(encoded_command)
                if command.get("command") == "close":
                    should_close = True
                    break
                if command.get("command") != "state":
                    raise ValueError("unsupported setup progress writer command")
                now = time.monotonic()
                state = command["state"]
                if command.get("reset_stage"):
                    stage_started_at = now
                update_type = command.get("publish")
                if update_type is not None:
                    publish(now, update_type)
                elif (
                    state.get("status") == "running"
                    and now - last_published_at >= heartbeat_seconds
                ):
                    publish(now, "heartbeat")
            if should_close:
                break
    finally:
        selector.close()
        os.close(status_fd)


def _write_json_line(file_descriptor: int, value: dict[str, Any]) -> None:
    """Write one compact JSON record with one system call."""
    encoded = (
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_bytes(file_descriptor, encoded)


def _write_bytes(file_descriptor: int, value: bytes) -> None:
    """Write one complete append-only record or fail instead of truncating it."""
    if os.write(file_descriptor, value) != len(value):
        raise OSError("setup progress record was only partially written")


def _writer_main() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    parser = argparse.ArgumentParser()
    parser.add_argument("--writer", type=Path, required=True)
    parser.add_argument("--heartbeat-seconds", type=float, required=True)
    args = parser.parse_args()
    _run_writer(
        args.writer,
        heartbeat_seconds=args.heartbeat_seconds,
    )


if __name__ == "__main__":
    _writer_main()
