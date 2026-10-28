# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Unit tests for the local simulation runner."""

import io
import subprocess
import sys
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

import run_simulation  # noqa: E402


class RunSimulationTests(unittest.TestCase):
    """Validate terminal-facing runner messages without launching MPI."""

    def test_runner_prints_log_file_and_completion_message(self):
        """Successful runs should tell the user where logs are stored."""
        stdout = io.StringIO()

        with _patched_runner(stdout) as run:
            run_simulation.main()

        output = stdout.getvalue().splitlines()
        display_path = run_simulation.display_path(Path("/tmp/simsom-test.log"))
        setup_status_path = run_simulation.display_path(
            Path("/tmp/setup_status.jsonl")
        )
        self.assertEqual(output[0], f"SimSoM log file: {display_path}")
        self.assertEqual(output[1], f"SimSoM setup status: {setup_status_path}")
        self.assertTrue(output[2].startswith("SimSoM termination metrics: "))
        self.assertTrue(output[3].startswith("SimSoM analytics output: "))
        self.assertEqual(
            output[4:9],
            _run_status_lines(
                f"SimSoM simulation is running. Follow progress in: {display_path}"
            ),
        )
        self.assertEqual(
            output[9:13],
            _run_status_lines(
                f"SimSoM simulation finished successfully. Log file: {display_path}",
                leading_blank=False,
            ),
        )
        run.assert_called_once()

    def test_runner_prints_failure_message(self):
        """Failed MPI subprocesses should still point the user to the log file."""
        stdout = io.StringIO()

        with _patched_runner(stdout) as run:
            run.return_value.wait.return_value = 7
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                run_simulation.main()

            self.assertEqual(raised.exception.returncode, 7)

        output = stdout.getvalue().splitlines()
        display_path = run_simulation.display_path(Path("/tmp/simsom-test.log"))
        setup_status_path = run_simulation.display_path(
            Path("/tmp/setup_status.jsonl")
        )
        self.assertEqual(output[0], f"SimSoM log file: {display_path}")
        self.assertEqual(output[1], f"SimSoM setup status: {setup_status_path}")
        self.assertTrue(output[2].startswith("SimSoM termination metrics: "))
        self.assertTrue(output[3].startswith("SimSoM analytics output: "))
        self.assertEqual(
            output[4:9],
            _run_status_lines(
                f"SimSoM simulation is running. Follow progress in: {display_path}"
            ),
        )
        self.assertEqual(
            output[9],
            f"SimSoM simulation failed with exit code 7. Log file: {display_path}",
        )

    def test_runner_reports_manual_interrupt_without_traceback(self):
        """Manual interruption should stop MPI and exit with status 130."""
        stdout = io.StringIO()

        with _patched_runner(stdout) as run, patch(
            "run_simulation._gracefully_stop_process"
        ) as stop_process:
            run.return_value.wait.side_effect = KeyboardInterrupt
            with self.assertRaises(SystemExit) as raised:
                run_simulation.main()

        self.assertEqual(raised.exception.code, 130)
        stop_process.assert_called_once_with(run.return_value)
        display_path = run_simulation.display_path(Path("/tmp/simsom-test.log"))
        setup_status_path = run_simulation.display_path(
            Path("/tmp/setup_status.jsonl")
        )
        output = stdout.getvalue().splitlines()
        self.assertEqual(output[0], f"SimSoM log file: {display_path}")
        self.assertEqual(output[1], f"SimSoM setup status: {setup_status_path}")
        self.assertTrue(output[2].startswith("SimSoM termination metrics: "))
        self.assertTrue(output[3].startswith("SimSoM analytics output: "))
        self.assertEqual(
            output[4:9],
            _run_status_lines(
                f"SimSoM simulation is running. Follow progress in: {display_path}"
            ),
        )
        self.assertEqual(
            output[9],
            f"SimSoM simulation interrupted by user. Log file: {display_path}",
        )

    def test_runner_reports_missing_mpi_launcher(self):
        """A missing MPI launcher should produce an actionable terminal error."""
        stdout = io.StringIO()

        with _patched_runner(stdout) as run:
            run.side_effect = FileNotFoundError(2, "No such file or directory", "mpiexec")
            with self.assertRaises(SystemExit) as raised:
                run_simulation.main()

        self.assertEqual(raised.exception.code, 1)
        display_path = run_simulation.display_path(Path("/tmp/simsom-test.log"))
        setup_status_path = run_simulation.display_path(
            Path("/tmp/setup_status.jsonl")
        )
        output = stdout.getvalue().splitlines()
        self.assertEqual(output[0], f"SimSoM log file: {display_path}")
        self.assertEqual(output[1], f"SimSoM setup status: {setup_status_path}")
        self.assertTrue(output[2].startswith("SimSoM termination metrics: "))
        self.assertTrue(output[3].startswith("SimSoM analytics output: "))
        self.assertEqual(
            output[4:9],
            _run_status_lines(
                f"SimSoM simulation is running. Follow progress in: {display_path}"
            ),
        )
        self.assertEqual(
            output[9],
            "SimSoM simulation could not start MPI: "
            "[Errno 2] No such file or directory: 'mpiexec'. "
            f"Log file: {display_path}",
        )


class _DummyLogger:
    """Minimal logger used so runner tests do not write to the real log file."""

    def info(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


def _run_status_lines(message: str, *, leading_blank: bool = True) -> list[str]:
    lines = [run_simulation.RUN_STATUS_DIVIDER, message, run_simulation.RUN_STATUS_DIVIDER, ""]
    return [""] + lines if leading_blank else lines


@contextmanager
def _patched_runner(stdout: io.StringIO):
    """Patch runner dependencies that would otherwise touch the real system."""
    with patch.object(
        sys,
        "argv",
        [
            "run_simulation.py",
            "--num-procs",
            "4",
            "--mpiexec",
            "mpiexec",
            "--python-exec",
            "python",
            "--script",
            "main.py",
        ],
    ), patch("run_simulation.get_logger", return_value=_DummyLogger()), patch(
        "run_simulation.get_log_file_path",
        return_value=Path("/tmp/simsom-test.log"),
    ), patch(
        "run_simulation.get_setup_status_file_path",
        return_value=Path("/tmp/setup_status.jsonl"),
    ), patch("run_simulation.subprocess.Popen") as run, redirect_stdout(stdout):
        run.return_value.wait.return_value = 0
        yield run


if __name__ == "__main__":
    unittest.main()
