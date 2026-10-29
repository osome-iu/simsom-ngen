"""Local API for launching SimSoM and reading its analytics."""

from __future__ import annotations

import argparse
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from http import HTTPStatus
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any
from uuid import uuid4

from flask import Flask, jsonify, request
import jsonc
from werkzeug.exceptions import BadRequest
from werkzeug.serving import BaseWSGIServer, make_server


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SIMULATOR_RUNNER = REPOSITORY_ROOT / "libs" / "simsom" / "run_simulation.py"
DEFAULT_CONFIG = (
    REPOSITORY_ROOT / "libs" / "simsom" / "config" / "default_simulator_config.jsonc"
)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
OUTPUT_TAIL_LINES = 120
SIMULATOR_LOG_FILE = REPOSITORY_ROOT / "logs" / "simsom.log"
SETUP_STATUS_FILE = REPOSITORY_ROOT / "logs" / "setup_status.jsonl"
DEFAULT_LOG_TAIL_LINES = 200
MAX_LOG_TAIL_LINES = 1_000
DEFAULT_NETWORK_EDGE_LIMIT = 100_000
MAX_NETWORK_EDGE_LIMIT = 100_000
DEFAULT_NETWORK_NODE_LIMIT = 50_000
MAX_NETWORK_NODE_LIMIT = 50_000
MAX_REQUEST_BODY_BYTES = 16_384
DATABASE_PREFLIGHT_TIMEOUT_SECONDS = 5
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ACTION_TYPES = ("like", "post", "repost", "reply", "quote")
ANALYTICS_EVALUATION_UNITS = (
    "average_actions_per_user",
    "simulated_hours",
)
STOPPING_CRITERIA = ("max_avg_actions_per_user",)
CONVERGENCE_CRITERIA = (
    "exponential_moving_average_action_quality",
)
EDITABLE_CONFIG_PATHS = {
    ("controller", "dump_on_termination"),
    ("setup_db", "user_types", "normal", "num_nodes"),
    ("setup_db", "user_types", "normal", "lurker_ratio"),
    ("setup_db", "user_types", "normal", "user_quality_distribution", "w"),
    ("setup_db", "user_types", "normal", "user_quality_distribution", "lambda_slope"),
    ("setup_db", "user_types", "bad", "num_nodes"),
    ("setup_db", "user_types", "bad", "lurker_ratio"),
    ("setup_db", "user_types", "bad", "user_quality_distribution", "w"),
    ("setup_db", "user_types", "bad", "user_quality_distribution", "lambda_slope"),
    ("recsys", "feed_size"),
    ("recsys", "inventory_composition", "out_of_network_ratio"),
    ("recsys", "inventory_composition", "size_multiplier"),
    ("recsys", "engagement_action_lookback_population_multiplier"),
    ("worker", "batch_size"),
    ("worker", "idle_sleep_milliseconds"),
    ("worker", "action_insert_chunk_size"),
    ("policy", "enabled"),
    ("policy", "poll_interval_milliseconds"),
    ("policy", "rule_functions", "below_quality_threshold", "threshold"),
    ("analytics", "evaluation_interval", "unit"),
    ("analytics", "evaluation_interval", "value"),
    ("termination", "criteria_functions"),
    *(("user_behavior", "p_actions", action_type) for action_type in ACTION_TYPES),
}
ATOMIC_EDITABLE_CONFIG_PATHS = {
    ("termination", "criteria_functions"),
}


class DatabaseNotReadyError(RuntimeError):
    """Raised when a simulation cannot safely start against PostgreSQL."""


def smallest_fitting_k_core(
    graph: Any,
    *,
    edge_limit: int,
    node_limit: int,
) -> tuple[frozenset[int], int]:
    """Return the lowest k-core that satisfies both visualization limits."""
    if graph.vcount() == 0:
        return frozenset(), 1

    coreness = graph.coreness(mode="all")
    maximum_core = max(coreness, default=0)

    def fits(level: int) -> bool:
        vertex_ids = [
            vertex_id
            for vertex_id, core_number in enumerate(coreness)
            if core_number >= level
        ]
        if len(vertex_ids) > node_limit:
            return False
        if not vertex_ids:
            return True
        return graph.induced_subgraph(vertex_ids).ecount() <= edge_limit

    low = 1
    high = maximum_core + 1
    while low < high:
        middle = (low + high) // 2
        if fits(middle):
            high = middle
        else:
            low = middle + 1

    selected = frozenset(
        int(graph.vs[vertex_id]["name"])
        for vertex_id, core_number in enumerate(coreness)
        if core_number >= low
    )
    return selected, low


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_log_tail(
    line_limit: int = DEFAULT_LOG_TAIL_LINES,
    *,
    log_path: Path | None = None,
) -> dict[str, Any]:
    """Read the newest simulator log lines without loading the whole file."""
    if isinstance(line_limit, bool) or not isinstance(line_limit, int):
        raise ValueError("lines must be an integer")
    if line_limit < 1 or line_limit > MAX_LOG_TAIL_LINES:
        raise ValueError(f"lines must be between 1 and {MAX_LOG_TAIL_LINES}")

    path = log_path or SIMULATOR_LOG_FILE
    display_path = str(path.relative_to(REPOSITORY_ROOT)) if path.is_relative_to(REPOSITORY_ROOT) else path.name
    if not path.exists():
        return {
            "available": True,
            "path": display_path,
            "lines": [],
            "sizeBytes": 0,
            "modifiedAt": None,
        }

    try:
        stat = path.stat()
        with path.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            position = log_file.tell()
            chunks: list[bytes] = []
            newline_count = 0
            while position > 0 and newline_count <= line_limit:
                read_size = min(8_192, position)
                position -= read_size
                log_file.seek(position)
                chunk = log_file.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
        text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
        lines = text.splitlines()[-line_limit:]
        return {
            "available": True,
            "path": display_path,
            "lines": lines,
            "sizeBytes": stat.st_size,
            "modifiedAt": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }
    except OSError as exc:
        return {
            "available": False,
            "path": display_path,
            "lines": [],
            "message": str(exc),
        }


def read_setup_status(*, status_path: Path | None = None) -> dict[str, Any]:
    """Read the latest setup-progress snapshot from the JSONL heartbeat file."""
    path = status_path or SETUP_STATUS_FILE
    display_path = (
        str(path.relative_to(REPOSITORY_ROOT))
        if path.is_relative_to(REPOSITORY_ROOT)
        else path.name
    )
    if not path.exists():
        return {"available": False, "path": display_path}

    try:
        with path.open("rb") as status_file:
            status_file.seek(0, os.SEEK_END)
            position = status_file.tell()
            if position == 0:
                return {"available": False, "path": display_path}
            status_file.seek(max(0, position - 16_384))
            lines = status_file.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if line.strip():
                return {"available": True, "path": display_path, **json.loads(line)}
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "path": display_path, "message": str(exc)}
    return {"available": False, "path": display_path}


def build_simulator_command(
    python_executable: str,
    *,
    num_procs: int | None = None,
    config_path: Path | None = None,
) -> list[str]:
    """Build the fixed simulator command accepted by the local API."""
    if num_procs is not None:
        if isinstance(num_procs, bool) or not isinstance(num_procs, int):
            raise ValueError("numProcs must be an integer")
        if num_procs < 4 or num_procs > 64:
            raise ValueError("numProcs must be between 4 and 64")

    command = [python_executable, str(SIMULATOR_RUNNER)]
    if num_procs is not None:
        command.extend(["--num-procs", str(num_procs)])
    if config_path is not None:
        command.extend(["--simulator_spec", str(config_path)])
    return command


def load_default_config() -> dict[str, Any]:
    """Load a fresh copy of the canonical simulator configuration."""
    with DEFAULT_CONFIG.open(encoding="utf-8") as config_file:
        config = jsonc.load(config_file)
    if not isinstance(config, dict):
        raise RuntimeError("Default simulator config must contain a JSON object")
    return config


def editable_config_defaults() -> dict[str, Any]:
    """Return only fields intentionally exposed by the advanced form."""
    default = load_default_config()
    editable: dict[str, Any] = {}
    for path in sorted(EDITABLE_CONFIG_PATHS):
        _set_path(editable, path, deepcopy(_get_path(default, path)))
    return editable


def build_run_config(overrides: dict[str, Any]) -> dict[str, Any]:
    """Validate an editable partial config and merge it over canonical defaults."""
    if not isinstance(overrides, dict):
        raise ValueError("config must be a JSON object")
    leaf_paths = set(_leaf_paths(overrides))
    unknown_paths = leaf_paths - EDITABLE_CONFIG_PATHS
    if unknown_paths:
        display_path = ".".join(sorted(unknown_paths)[0])
        raise ValueError(f"Config field is not editable: {display_path}")

    config = load_default_config()
    _deep_merge(config, overrides)
    _validate_editable_config(config)
    return config


def _write_temporary_config(config: dict[str, Any]) -> Path:
    descriptor, file_name = tempfile.mkstemp(prefix="simsom-web-", suffix=".json")
    with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
        json.dump(config, config_file, indent=2)
        config_file.write("\n")
    return Path(file_name)


def _leaf_paths(value: Any, prefix: tuple[str, ...] = ()):
    if prefix in ATOMIC_EDITABLE_CONFIG_PATHS:
        yield prefix
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                yield prefix + (str(key),)
                continue
            yield from _leaf_paths(child, prefix + (key,))
        return
    yield prefix


def _deep_merge(
    target: dict[str, Any],
    overrides: dict[str, Any],
    prefix: tuple[str, ...] = (),
) -> None:
    for key, value in overrides.items():
        path = prefix + (key,)
        if path in ATOMIC_EDITABLE_CONFIG_PATHS:
            target[key] = deepcopy(value)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value, path)
        else:
            target[key] = deepcopy(value)


def _get_path(value: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = value
    for key in path:
        current = current[key]
    return current


def _set_path(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = target
    for key in path[:-1]:
        current = current.setdefault(key, {})
    current[path[-1]] = value


def _validate_editable_config(config: dict[str, Any]) -> None:
    user_types = config["setup_db"]["user_types"]
    for user_type in ("normal", "bad"):
        user = user_types[user_type]
        _integer(user["num_nodes"], f"{user_type} user count", minimum=0, maximum=1_000_000)
        _number(user["lurker_ratio"], f"{user_type} lurker ratio", minimum=0, maximum=0.999999)
        distribution = user["user_quality_distribution"]
        _number(distribution["w"], f"{user_type} quality weight", minimum=0, maximum=1)
        _number(distribution["lambda_slope"], f"{user_type} quality slope", minimum=0.000001)
    if sum(user_types[name]["num_nodes"] for name in ("normal", "bad")) <= 0:
        raise ValueError("At least one user must be configured")

    recsys = config["recsys"]
    _integer(recsys["feed_size"], "Feed size", minimum=0, maximum=100_000)
    inventory = recsys["inventory_composition"]
    _integer(
        inventory["size_multiplier"],
        "Inventory size multiplier",
        minimum=1,
    )
    _number(
        inventory["out_of_network_ratio"],
        "Out-of-network inventory ratio",
        minimum=0,
        maximum=1,
    )
    _number(
        recsys["engagement_action_lookback_population_multiplier"],
        "Engagement lookback multiplier",
        minimum=0.000001,
    )

    worker = config["worker"]
    _integer(worker["batch_size"], "Worker batch size", minimum=1, maximum=1_000_000)
    _integer(
        worker["action_insert_chunk_size"],
        "Action insert chunk size",
        minimum=1,
        maximum=1_000_000,
    )
    _number(
        worker["idle_sleep_milliseconds"],
        "Worker idle sleep milliseconds",
        minimum=0,
    )

    for action_type in ACTION_TYPES:
        distribution = config["user_behavior"]["p_actions"][action_type]
        if not isinstance(distribution, list) or len(distribution) != 2:
            raise ValueError(f"{action_type} action distribution must be [mean, standard deviation]")
        _number(distribution[0], f"{action_type} mean", minimum=0)
        _number(distribution[1], f"{action_type} standard deviation", minimum=0)

    policy = config["policy"]
    _boolean(policy["enabled"], "Policy enabled")
    _number(policy["poll_interval_milliseconds"], "Policy poll interval", minimum=0)
    _number(
        policy["rule_functions"]["below_quality_threshold"]["threshold"],
        "Policy quality threshold",
        minimum=0,
        maximum=1,
    )
    evaluation_interval = config["analytics"]["evaluation_interval"]
    unit = evaluation_interval["unit"]
    if unit not in ANALYTICS_EVALUATION_UNITS:
        allowed = ", ".join(ANALYTICS_EVALUATION_UNITS)
        raise ValueError(f"Analytics evaluation unit must be one of: {allowed}")
    _number(
        evaluation_interval["value"],
        "Analytics evaluation interval",
        minimum=0,
        minimum_inclusive=False,
    )
    _boolean(config["controller"]["dump_on_termination"], "Dump on termination")

    criteria = _criterion_mapping(
        config["termination"]["criteria_functions"],
        "Termination criteria",
        STOPPING_CRITERIA + CONVERGENCE_CRITERIA,
    )
    if not criteria:
        raise ValueError("At least one termination criterion is required")


def _number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    minimum_inclusive: bool = True,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if minimum is not None and (
        number < minimum or (number == minimum and not minimum_inclusive)
    ):
        comparison = "greater than" if not minimum_inclusive else "at least"
        raise ValueError(f"{label} must be {comparison} {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} must be at most {maximum}")
    return number


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    _number(value, label, minimum=minimum, maximum=maximum)
    return value


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _criterion_mapping(
    value: Any,
    label: str,
    allowed: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    unsupported = set(value) - set(allowed)
    if unsupported:
        raise ValueError(f"{label} contains an unsupported value")
    expected_inputs = {
        "max_avg_actions_per_user": {"threshold"},
        "exponential_moving_average_action_quality": {"threshold", "rho"},
    }
    for criterion, inputs in value.items():
        if not isinstance(inputs, dict):
            raise ValueError(f"{label}.{criterion} must be an object")
        if set(inputs) != expected_inputs[criterion]:
            raise ValueError(f"{label}.{criterion} has invalid inputs")
        _number(
            inputs["threshold"],
            f"{label}.{criterion}.threshold",
            minimum=0,
            minimum_inclusive=False,
        )
        if criterion == "exponential_moving_average_action_quality":
            _number(
                inputs["rho"],
                f"{label}.{criterion}.rho",
                minimum=0,
                maximum=1,
                minimum_inclusive=False,
            )
    return value


def _postgres_path_environment() -> dict[str, str]:
    """Expose this Python environment's MPI tools and prefer PostgreSQL 18."""
    environment = os.environ.copy()
    path_entries = [str(Path(sys.executable).resolve().parent)]
    postgres_bin = Path("/opt/homebrew/opt/postgresql@18/bin")
    if postgres_bin.is_dir():
        path_entries.insert(0, str(postgres_bin))
    current_path = environment.get("PATH", "")
    if current_path:
        path_entries.append(current_path)
    environment["PATH"] = os.pathsep.join(path_entries)
    # run_simulation.py is already launched in its own process group by the
    # API. Keep its nested mpiexec process in that same group for cancellation.
    environment["SIMSOM_INHERIT_MPI_PROCESS_GROUP"] = "1"
    return environment


class RunManager:
    """Own at most one simulator subprocess and expose a JSON-friendly status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._state: dict[str, Any] = {
            "id": None,
            "status": "idle",
            "startedAt": None,
            "finishedAt": None,
            "returnCode": None,
            "command": [],
        }
        self._output: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)

    def start(
        self,
        *,
        num_procs: int | None = None,
        config_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        config_path: Path | None = None
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("A simulation is already running")

            run_config = (
                build_run_config(config_overrides)
                if config_overrides is not None
                else load_default_config()
            )
            if num_procs is not None:
                run_config["mpi"]["num_procs"] = num_procs
            if config_overrides is not None:
                config_path = _write_temporary_config(run_config)
            command = build_simulator_command(
                sys.executable,
                num_procs=num_procs,
                config_path=config_path,
            )
            self._output.clear()
            run_id = uuid4().hex
            self._state = {
                "id": run_id,
                "status": "running",
                "startedAt": utc_now(),
                "finishedAt": None,
                "returnCode": None,
                "command": command,
                "usesCustomConfig": config_path is not None,
                "config": run_config,
            }
            try:
                self._process = subprocess.Popen(
                    command,
                    cwd=REPOSITORY_ROOT,
                    env=_postgres_path_environment(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
            except Exception:
                if config_path is not None:
                    config_path.unlink(missing_ok=True)
                raise

        threading.Thread(
            target=self._monitor,
            args=(run_id, self._process, config_path),
            name=f"simsom-run-{run_id[:8]}",
            daemon=True,
        ).start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise RuntimeError("No simulation is currently running")
            self._state["status"] = "stopping"
            # mpiexec is a process supervisor; terminating only it can leave
            # MPI workers running. The simulator has its own process group so
            # cancellation reaches the supervisor and every worker.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["output"] = list(self._output)
            setup = read_setup_status()
            # The status file is reused between runs. Do not let a completed
            # snapshot from the previous run make a new run look finished.
            if setup.get("updated_at") and state.get("startedAt"):
                try:
                    if datetime.fromisoformat(setup["updated_at"]) < datetime.fromisoformat(state["startedAt"]):
                        setup = {"available": False, "path": setup.get("path", "logs/setup_status.jsonl")}
                except (TypeError, ValueError):
                    pass
            state["setup"] = setup
            return state

    def _monitor(
        self,
        run_id: str,
        process: subprocess.Popen[str],
        config_path: Path | None,
    ) -> None:
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    with self._lock:
                        self._output.append(line.rstrip())

            return_code = process.wait()
            with self._lock:
                if self._state.get("id") != run_id:
                    return
                self._state.update(
                    {
                        "status": "completed" if return_code == 0 else "failed",
                        "finishedAt": utc_now(),
                        "returnCode": return_code,
                    }
                )
        finally:
            if config_path is not None:
                config_path.unlink(missing_ok=True)


class AnalyticsRepository:
    """Read the simulator's durable analytics series with the psql CLI."""

    def __init__(self) -> None:
        self.dsn = os.environ.get("SIMSOM_DATABASE_DSN", "postgres")
        self.schema = os.environ.get("SIMSOM_DATABASE_SCHEMA", "simsom")
        self.clock_extension = load_default_config()["database"]["clock_extension"][
            "name"
        ]
        self._network_core_cache: dict[
            tuple[int, int, str, int, int], tuple[frozenset[int], int]
        ] = {}
        if not IDENTIFIER.fullmatch(self.schema):
            raise ValueError("SIMSOM_DATABASE_SCHEMA must be a PostgreSQL identifier")
        if not IDENTIFIER.fullmatch(self.clock_extension):
            raise ValueError(
                "The configured clock extension must be a PostgreSQL identifier"
            )

    def readiness(self) -> dict[str, Any]:
        """Check the database prerequisites required before launching MPI."""
        try:
            psql = self._resolve_psql()
        except (OSError, RuntimeError) as exc:
            return {
                "ready": False,
                "status": "psql_unavailable",
                "message": (
                    "PostgreSQL client `psql` was not found. Install the client "
                    "tools for your PostgreSQL version or set SIMSOM_PSQL_BIN to "
                    "the correct psql executable. "
                    f"Details: {exc}"
                ),
            }

        sql = f"""
        SELECT json_build_object(
            'database', current_database(),
            'serverVersion', current_setting('server_version'),
            'currentUser', current_user,
            'isSuperuser', (
                SELECT rolsuper FROM pg_roles WHERE rolname = current_user
            ),
            'clockExtension', '{self.clock_extension}',
            'clockExtensionAvailable', EXISTS (
                SELECT 1
                FROM pg_available_extensions
                WHERE name = '{self.clock_extension}'
            )
        )::text;
        """
        try:
            completed = subprocess.run(
                [
                    psql,
                    "-X",
                    "-q",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-tA",
                    "--dbname",
                    self.dsn,
                    "--command",
                    sql,
                ],
                cwd=REPOSITORY_ROOT,
                env=_postgres_path_environment(),
                capture_output=True,
                text=True,
                check=False,
                timeout=DATABASE_PREFLIGHT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "ready": False,
                "status": "database_unreachable",
                "message": (
                    "PostgreSQL is not ready for SimSoM. Start the server and verify "
                    f"the configured database connection. Details: {exc}"
                ),
            }

        if completed.returncode != 0:
            details = completed.stderr.strip() or "psql could not connect"
            return {
                "ready": False,
                "status": "database_unreachable",
                "message": (
                    "PostgreSQL is not ready for SimSoM. On a Homebrew macOS "
                    "installation, run `brew services start postgresql@18` (or "
                    "replace it with your installed formula, such as "
                    "`postgresql@14`), then "
                    "verify the connection with `pg_isready -d postgres`. "
                    f"psql reported: {details}"
                ),
            }

        try:
            details = json.loads(completed.stdout.strip())
        except (json.JSONDecodeError, TypeError) as exc:
            return {
                "ready": False,
                "status": "preflight_failed",
                "message": f"PostgreSQL returned an invalid preflight response: {exc}",
            }

        if not details.get("clockExtensionAvailable"):
            return {
                "ready": False,
                "status": "clock_extension_unavailable",
                **details,
                "message": (
                    f"PostgreSQL is running, but `{self.clock_extension}` is not "
                    "installed for this server. Build and install the extension "
                    "with the matching pg_config as described in web/README.md."
                ),
            }

        if not details.get("isSuperuser"):
            return {
                "ready": False,
                "status": "insufficient_database_privileges",
                **details,
                "message": (
                    f"PostgreSQL role `{details.get('currentUser')}` is not a "
                    "superuser. SimSoM requires a database role that can drop and "
                    f"recreate the `{self.clock_extension}` extension at startup."
                ),
            }

        return {"ready": True, "status": "ready", **details}

    def require_ready(self) -> dict[str, Any]:
        """Raise an actionable API error when database prerequisites are missing."""
        readiness = self.readiness()
        if not readiness["ready"]:
            raise DatabaseNotReadyError(readiness["message"])
        return readiness

    def fetch(self) -> dict[str, Any]:
        return self._fetch_json(
            self._results_sql(),
            empty_payload={"series": [], "lifecycle": None},
        )

    def fetch_network(
        self,
        *,
        edge_limit: int = DEFAULT_NETWORK_EDGE_LIMIT,
        node_limit: int = DEFAULT_NETWORK_NODE_LIMIT,
    ) -> dict[str, Any]:
        """Return users and post interactions in Helios-friendly node/edge rows."""
        if isinstance(edge_limit, bool) or not isinstance(edge_limit, int):
            raise ValueError("edgeLimit must be an integer")
        if edge_limit < 1 or edge_limit > MAX_NETWORK_EDGE_LIMIT:
            raise ValueError(
                f"edgeLimit must be between 1 and {MAX_NETWORK_EDGE_LIMIT}"
            )
        if isinstance(node_limit, bool) or not isinstance(node_limit, int):
            raise ValueError("nodeLimit must be an integer")
        if node_limit < 1 or node_limit > MAX_NETWORK_NODE_LIMIT:
            raise ValueError(
                f"nodeLimit must be between 1 and {MAX_NETWORK_NODE_LIMIT}"
            )
        stats_payload = self._fetch_json(
            self._network_stats_sql(),
            empty_payload={"meta": {}, "userIds": []},
            timeout_seconds=60,
        )
        if not stats_payload.get("available"):
            return {"nodes": [], "edges": [], **stats_payload}

        meta = stats_payload.get("meta") or {}
        user_count = int(meta.get("userCount") or 0)
        interaction_count = int(meta.get("interactionCount") or 0)
        truncated = user_count > node_limit or interaction_count > edge_limit
        selected_node_ids: frozenset[int] | None = None
        k_core_level = 0

        if truncated:
            cache_key = (
                user_count,
                interaction_count,
                str(meta.get("runStartedAt") or meta.get("networkVersion") or ""),
                edge_limit,
                node_limit,
            )
            cached = self._network_core_cache.get(cache_key)
            if cached is None:
                try:
                    selected_node_ids, k_core_level = self._python_k_core(
                        [int(user_id) for user_id in stats_payload.get("userIds", [])],
                        edge_limit=edge_limit,
                        node_limit=node_limit,
                    )
                except (
                    ImportError,
                    OSError,
                    RuntimeError,
                    ValueError,
                    subprocess.SubprocessError,
                ) as exc:
                    return {
                        "available": False,
                        "message": f"Network filtering failed: {exc}",
                        "nodes": [],
                        "edges": [],
                        "meta": meta,
                    }
                self._network_core_cache = {cache_key: (selected_node_ids, k_core_level)}
            else:
                selected_node_ids, k_core_level = cached

        payload = self._fetch_json(
            self._network_rows_sql(edge_limit, selected_node_ids),
            empty_payload={"nodes": [], "edges": []},
            timeout_seconds=120 if truncated else 60,
        )
        if not payload.get("available"):
            payload["meta"] = meta
            return payload

        nodes = payload.get("nodes") or []
        edges = payload.get("edges") or []
        payload["meta"] = {
            **meta,
            "returnedNodeCount": len(nodes),
            "returnedEdgeCount": len(edges),
            "edgeLimit": edge_limit,
            "nodeLimit": node_limit,
            "truncated": truncated,
            "kCoreLevel": k_core_level,
            "kCoreApplied": truncated,
        }
        return payload

    def _fetch_json(
        self,
        sql: str,
        *,
        empty_payload: dict[str, Any],
        timeout_seconds: float = 15,
    ) -> dict[str, Any]:
        try:
            psql = self._resolve_psql()
            completed = subprocess.run(
                [
                    psql,
                    "-X",
                    "-q",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-tA",
                    "--dbname",
                    self.dsn,
                    "--command",
                    sql,
                ],
                cwd=REPOSITORY_ROOT,
                env=_postgres_path_environment(),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"available": False, "message": str(exc), **empty_payload}

        if completed.returncode != 0:
            message = completed.stderr.strip() or "Analytics are not available yet"
            return {"available": False, "message": message, **empty_payload}

        output = completed.stdout.strip()
        if not output:
            return {"available": True, **empty_payload}
        payload = json.loads(output)
        payload["available"] = True
        return payload

    @staticmethod
    def _resolve_psql() -> str:
        configured = os.environ.get("SIMSOM_PSQL_BIN")
        if configured:
            return configured
        homebrew_psql = Path("/opt/homebrew/opt/postgresql@18/bin/psql")
        if homebrew_psql.is_file():
            return str(homebrew_psql)
        resolved = shutil.which("psql")
        if resolved is None:
            raise RuntimeError("psql is not available on PATH")
        return resolved

    def _results_sql(self) -> str:
        schema = f'"{self.schema}"'
        summary = f'{schema}."analytics_summary"'
        lifecycle = f'{schema}."simulation_lifecycle"'
        return f"""
        SELECT json_build_object(
            'series', COALESCE(
                (
                    SELECT json_agg(row_to_json(summary_row) ORDER BY summary_row.id)
                    FROM (
                        SELECT
                            analytics_summary_id AS id,
                            recorded_at AS "recordedAt",
                            user_count AS "userCount",
                            action_count AS "actionCount",
                            avg_activity_level AS "avgActivityLevel",
                            avg_action_quality AS "avgActionQuality",
                            EXISTS (
                                SELECT 1
                                FROM jsonb_array_elements(termination_criteria) AS criterion
                                WHERE criterion->>'function_name' =
                                    'exponential_moving_average_action_quality'
                                  AND COALESCE((criterion->>'met')::BOOLEAN, FALSE)
                            ) AS converged,
                            (
                                SELECT criterion->>'reason'
                                FROM jsonb_array_elements(termination_criteria) AS criterion
                                WHERE criterion->>'function_name' =
                                    'exponential_moving_average_action_quality'
                                LIMIT 1
                            ) AS "convergenceReason",
                            other_properties AS "otherProperties"
                        FROM {summary}
                        ORDER BY analytics_summary_id
                    ) AS summary_row
                ),
                '[]'::json
            ),
            'lifecycle', (
                SELECT row_to_json(lifecycle_row)
                FROM (
                    SELECT
                        started_at AS "startedAt",
                        should_terminate AS "shouldTerminate",
                        reason,
                        requested_at AS "requestedAt",
                        analytics_stop_requested AS "analyticsStopRequested",
                        analytics_stop_reason AS "analyticsStopReason",
                        NULL::BOOLEAN AS "analyticsConverged",
                        NULL::TEXT AS "analyticsConvergenceReason"
                    FROM {lifecycle}
                    WHERE lifecycle_id = 1
                ) AS lifecycle_row
            )
        )::text;
        """

    def _network_stats_sql(self) -> str:
        schema = f'"{self.schema}"'
        user_table = f'{schema}."user"'
        action_table = f'{schema}."action"'
        lifecycle_table = f'{schema}."simulation_lifecycle"'
        return f"""
        WITH action_counts AS (
            SELECT
                COUNT(*)::BIGINT AS action_count,
                COUNT(*) FILTER (WHERE parent_action_id IS NULL)::BIGINT AS post_count,
                COUNT(*) FILTER (WHERE parent_action_id IS NOT NULL)::BIGINT AS interaction_count,
                MAX(timestamp) AS network_version
            FROM {action_table}
        )
        SELECT json_build_object(
            'meta', json_build_object(
                'userCount', (SELECT COUNT(*) FROM {user_table}),
                'actionCount', action_count,
                'originalPostCount', post_count,
                'interactionCount', interaction_count,
                'networkVersion', network_version,
                'runStartedAt', (
                    SELECT started_at FROM {lifecycle_table} WHERE lifecycle_id = 1
                )
            ),
            'userIds', COALESCE(
                (SELECT json_agg(user_id ORDER BY user_id) FROM {user_table}),
                '[]'::json
            )
        )::text
        FROM action_counts;
        """

    def _network_edge_list_sql(self) -> str:
        schema = f'"{self.schema}"'
        action_table = f'{schema}."action"'
        return f"""
        COPY (
            SELECT action.creator_user_id, parent.creator_user_id
            FROM {action_table} AS action
            JOIN {action_table} AS parent
                ON parent.action_id = action.parent_action_id
            WHERE action.parent_action_id IS NOT NULL
        ) TO STDOUT WITH (FORMAT CSV, DELIMITER ' ');
        """

    def _python_k_core(
        self,
        user_ids: list[int],
        *,
        edge_limit: int,
        node_limit: int,
    ) -> tuple[frozenset[int], int]:
        try:
            import igraph
        except ImportError as exc:
            raise ImportError(
                "python-igraph is required for large-network k-core filtering"
            ) from exc

        with tempfile.NamedTemporaryFile(
            mode="w+", encoding="utf-8", suffix=".ncol"
        ) as edge_file:
            try:
                completed = subprocess.run(
                    [
                        self._resolve_psql(),
                        "-X",
                        "-q",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-tA",
                        "--dbname",
                        self.dsn,
                        "--command",
                        self._network_edge_list_sql(),
                    ],
                    cwd=REPOSITORY_ROOT,
                    env=_postgres_path_environment(),
                    stdout=edge_file,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=120,
                )
            except (OSError, subprocess.SubprocessError):
                raise
            if completed.returncode != 0:
                raise RuntimeError(
                    completed.stderr.strip() or "Could not export network topology"
                )

            edge_file.flush()
            if edge_file.tell() > 0:
                graph = igraph.Graph.Read_Ncol(
                    edge_file.name,
                    names=True,
                    directed=False,
                )
            else:
                graph = igraph.Graph()

        existing_names = set(graph.vs["name"]) if "name" in graph.vs.attributes() else set()
        missing_names = [str(user_id) for user_id in user_ids if str(user_id) not in existing_names]
        if missing_names:
            graph.add_vertices(missing_names)

        return smallest_fitting_k_core(
            graph,
            edge_limit=edge_limit,
            node_limit=node_limit,
        )

    def _network_rows_sql(
        self,
        edge_limit: int,
        selected_node_ids: frozenset[int] | None,
    ) -> str:
        schema = f'"{self.schema}"'
        user_table = f'{schema}."user"'
        action_table = f'{schema}."action"'
        if selected_node_ids is None:
            node_predicate = "TRUE"
            edge_predicate = "TRUE"
        elif selected_node_ids:
            ids = ",".join(str(int(user_id)) for user_id in sorted(selected_node_ids))
            id_array = f"ARRAY[{ids}]::BIGINT[]"
            node_predicate = f"users.user_id = ANY({id_array})"
            edge_predicate = (
                f"action.creator_user_id = ANY({id_array}) "
                f"AND parent.creator_user_id = ANY({id_array})"
            )
        else:
            node_predicate = "FALSE"
            edge_predicate = "FALSE"

        return f"""
        WITH interaction_rows AS MATERIALIZED (
            SELECT
                action.action_id AS id,
                action.creator_user_id AS source,
                parent.creator_user_id AS target,
                action.action_type AS "actionType",
                action.timestamp,
                CASE
                    WHEN action.action_type IN ('post', 'reply', 'quote')
                        THEN action.action_id
                    ELSE parent.action_id
                END AS "contentActionId",
                COALESCE(
                    (action.other_properties->>'quality')::FLOAT,
                    (parent.other_properties->>'quality')::FLOAT
                ) AS quality,
                COALESCE(
                    (action.other_properties->>'appeal')::FLOAT,
                    (parent.other_properties->>'appeal')::FLOAT
                ) AS appeal,
                CASE
                    WHEN action.action_type IN ('post', 'reply', 'quote')
                        THEN jsonb_array_length(action.content_embedding)
                    ELSE jsonb_array_length(parent.content_embedding)
                END AS "contentEmbeddingDimensions",
                CASE
                    WHEN action.action_type IN ('post', 'reply', 'quote')
                        THEN action.views_by_others_count
                    ELSE parent.views_by_others_count
                END AS "contentViewCount",
                CASE
                    WHEN action.action_type IN ('post', 'reply', 'quote')
                        THEN action.repeated_like_n_reshare_creator_count
                    ELSE parent.repeated_like_n_reshare_creator_count
                END AS "contentRepeatActorCount",
                action.moderation_actions AS "moderationActions",
                CASE
                    WHEN action.action_type IN ('post', 'reply', 'quote')
                        THEN action.moderation_actions
                    ELSE parent.moderation_actions
                END AS "contentModerationActions",
                action.other_properties->>'session_id' AS "sessionId",
                action.parent_action_id AS "parentActionId"
            FROM {action_table} AS action
            JOIN {action_table} AS parent
                ON parent.action_id = action.parent_action_id
            WHERE {edge_predicate}
            ORDER BY action.action_id DESC
            LIMIT {edge_limit}
        )
        SELECT json_build_object(
            'nodes', COALESCE(
                (
                    SELECT json_agg(row_to_json(node_row) ORDER BY node_row.id)
                    FROM (
                        SELECT
                            users.user_id AS id,
                            'User ' || users.user_id AS label,
                            users.user_type AS "userType",
                            users.activity_level AS "activityLevel",
                            (users.user_behavior->>'quality')::FLOAT AS quality
                        FROM {user_table} AS users
                        WHERE {node_predicate}
                        ORDER BY users.user_id
                    ) AS node_row
                ),
                '[]'::json
            ),
            'edges', COALESCE(
                (
                    SELECT json_agg(row_to_json(edge_row) ORDER BY edge_row.id)
                    FROM (
                        SELECT * FROM interaction_rows ORDER BY id
                    ) AS edge_row
                ),
                '[]'::json
            )
        )::text;
        """


def create_app(
    *,
    run_manager: RunManager | None = None,
    analytics_repository: AnalyticsRepository | None = None,
) -> Flask:
    """Create the Flask application while allowing isolated test dependencies."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BODY_BYTES
    runs = run_manager or RunManager()
    analytics = analytics_repository or AnalyticsRepository()

    @app.after_request
    def add_cors_headers(response):
        response.headers["Access-Control-Allow-Origin"] = "http://localhost:5173"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    @app.errorhandler(ValueError)
    def invalid_request(error):
        return jsonify(error=str(error)), HTTPStatus.BAD_REQUEST

    @app.errorhandler(DatabaseNotReadyError)
    def database_not_ready(error):
        return jsonify(error=str(error)), HTTPStatus.SERVICE_UNAVAILABLE

    @app.errorhandler(RuntimeError)
    def conflicting_request(error):
        return jsonify(error=str(error)), HTTPStatus.CONFLICT

    @app.errorhandler(404)
    def route_not_found(_error):
        return jsonify(error="Route not found"), HTTPStatus.NOT_FOUND

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify(error="Request body is too large"), HTTPStatus.REQUEST_ENTITY_TOO_LARGE

    @app.get("/api/health")
    def health():
        database = analytics.readiness()
        return jsonify(ok=True, ready=database["ready"], database=database)

    @app.get("/api/runs/current")
    def current_run():
        return jsonify(runs.status())

    @app.get("/api/config/defaults")
    def config_defaults():
        return jsonify(config=editable_config_defaults())

    @app.get("/api/results")
    def results():
        return jsonify(analytics.fetch())

    @app.get("/api/logs/tail")
    def log_tail():
        line_limit = int(request.args.get("lines", DEFAULT_LOG_TAIL_LINES))
        return jsonify(read_log_tail(line_limit))

    @app.get("/api/network")
    def network():
        edge_limit = int(request.args.get("edgeLimit", DEFAULT_NETWORK_EDGE_LIMIT))
        node_limit = int(request.args.get("nodeLimit", DEFAULT_NETWORK_NODE_LIMIT))
        return jsonify(
            analytics.fetch_network(edge_limit=edge_limit, node_limit=node_limit)
        )

    @app.post("/api/runs")
    def start_run():
        body = _request_json_object()
        analytics.require_ready()
        state = runs.start(
            num_procs=body.get("numProcs"),
            config_overrides=body.get("config"),
        )
        return jsonify(state), HTTPStatus.ACCEPTED

    @app.post("/api/runs/current/stop")
    def stop_run():
        return jsonify(runs.stop()), HTTPStatus.ACCEPTED

    return app


def _request_json_object() -> dict[str, Any]:
    if not request.get_data(cache=True):
        return {}
    try:
        body = request.get_json(force=True)
    except BadRequest as exc:
        raise ValueError("Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    return body


def create_server(host: str, port: int) -> BaseWSGIServer:
    """Create Flask's threaded local WSGI server."""
    return make_server(host, port, create_app(), threaded=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local SimSoM web API")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    args = parser.parse_args()

    server = create_server(args.host, args.port)
    print(f"SimSoM API listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
