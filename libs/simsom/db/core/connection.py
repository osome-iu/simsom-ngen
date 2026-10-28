# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Shared DB connection boundary.

This module should stay small and stable so process-specific DB work can happen
in separate PRs without everyone touching the same file.
"""

from contextlib import suppress
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Iterable, Protocol

from db.core.schema import qualified_name, sql_text
from db.core.settings import DatabaseSettings


class DatabaseClient(Protocol):
    def close(self) -> None:
        """Release DB resources held by this client."""


class PostgresClientBase:
    DEFAULT_PSQL_EXEC = "psql"
    DEFAULT_PG_DUMP_EXEC = "pg_dump"

    def __init__(self, settings: DatabaseSettings):
        self.settings = settings

    def close(self) -> None:
        return None

    def require_dsn(self) -> str:
        if not self.settings.dsn:
            raise ValueError(
                "database.dsn must be configured before running Postgres-backed SimSoM components"
            )
        return self.settings.dsn

    def run_psql(self, sql: str, *, capture_output: bool = False) -> str | None:
        """
        Run SQL without streaming normal psql command tags to the terminal.

        INSERT/UPDATE/COMMIT output is useful for debugging SQL by hand, but it
        drowns out simulator logs during normal runs. We always capture psql
        output, return it only for explicit reads, and include it in exceptions
        when a command fails.
        """
        completed = subprocess.run(
            self._psql_command(capture_output=capture_output),
            input=sql,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            details = _command_failure_details(
                "psql",
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
            raise RuntimeError(f"psql command failed: {details}")

        if not capture_output:
            return None
        return completed.stdout.strip()

    def run_psql_stream(self, sql_parts: Iterable[str]) -> None:
        """Stream SQL through one psql session without assembling it in memory."""
        process = subprocess.Popen(
            self._psql_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if process.stdin is None or process.stderr is None:
            process.kill()
            process.wait()
            raise RuntimeError("psql stream did not expose required pipes")
        broken_pipe = False
        try:
            for sql in sql_parts:
                process.stdin.write(sql)
                process.stdin.write("\n")
                process.stdin.flush()
        except BrokenPipeError:
            broken_pipe = True
        except BaseException:
            with suppress(BrokenPipeError):
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
            process.wait()
            process.stderr.close()
            raise
        finally:
            if not process.stdin.closed:
                with suppress(BrokenPipeError):
                    process.stdin.close()
        stderr = process.stderr.read()
        return_code = process.wait()
        process.stderr.close()
        if return_code != 0 or broken_pipe:
            details = _command_failure_details("psql", stdout=None, stderr=stderr)
            raise RuntimeError(f"psql command failed: {details}")

    def execute_sql(self, sql: str) -> str:
        """Run SQL and return psql's captured unaligned output."""
        return self.run_psql(sql, capture_output=True) or ""

    def fetch_json(self, sql: str) -> Any:
        """Run SQL that returns a single JSON value and decode it."""
        output = self.execute_sql(sql)
        if not output:
            return None
        return json.loads(output)

    def schema_name(self, name: str) -> str:
        """Return a schema-qualified identifier for this client's schema."""
        return qualified_name(self.settings.schema, name)

    def qualified_name(self, schema: str, name: str) -> str:
        """Return a schema-qualified identifier."""
        return qualified_name(schema, name)

    def json_literal(self, value: Any) -> str:
        """Return a SQL JSON literal for generated SQL payloads."""
        return sql_text(json.dumps(value, separators=(",", ":")))

    def run_psql_file(self, path: Path) -> None:
        sql_path = Path(path)
        if not sql_path.exists():
            raise FileNotFoundError(f"SQL file does not exist: {sql_path}")

        command = [
            self._require_command(self.DEFAULT_PSQL_EXEC),
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "--dbname",
            self.require_dsn(),
            "-f",
            str(sql_path),
        ]
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            details = _command_failure_details(
                "psql",
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
            raise RuntimeError(f"psql file execution failed: {details}")

    def run_pg_dump(self, output_path: Path, *, schema: str | None = None) -> Path:
        """Export a portable schema/data dump without source ownership grants."""
        dump_path = Path(output_path)
        dump_path.parent.mkdir(parents=True, exist_ok=True)

        command = [
            self._require_command(self.DEFAULT_PG_DUMP_EXEC),
            "--dbname",
            self.require_dsn(),
            "--file",
            str(dump_path),
            "--no-owner",
            "--no-privileges",
        ]
        if schema:
            command.extend(["--schema", schema])

        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            details = _command_failure_details(
                "pg_dump",
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
            raise RuntimeError(f"pg_dump command failed: {details}")

        return dump_path

    @staticmethod
    def _require_command(command_name: str) -> str:
        resolved = shutil.which(command_name)
        if resolved is None:
            raise RuntimeError(
                f"Required system command is not available on PATH: {command_name}"
            )
        return resolved

    def _psql_command(self, *, capture_output: bool = False) -> list[str]:
        """Return the common psql command for this database client."""
        command = [
            self._require_command(self.DEFAULT_PSQL_EXEC),
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "--dbname",
            self.require_dsn(),
        ]
        if capture_output:
            command.extend(["-t", "-A"])
        return command


def _command_failure_details(
    command_name: str,
    *,
    stdout: str | None,
    stderr: str | None,
) -> str:
    """Return labeled stdout/stderr text for a failed command."""
    stderr_text = stderr.strip() if stderr else ""
    stdout_text = stdout.strip() if stdout else ""
    parts = []
    if stderr_text:
        parts.append(f"stderr:\n{stderr_text}")
    if stdout_text:
        parts.append(f"stdout:\n{stdout_text}")
    if parts:
        return "\n".join(parts)
    return f"unknown {command_name} failure"
