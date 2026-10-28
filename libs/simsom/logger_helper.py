# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Logging utilities for the simulator.

Provides a custom logging handler that flushes after each log entry
and a logger factory function that reads configuration from CONFIG.
"""

import logging
import os
from pathlib import Path
from config_helper import CONFIG


class FlushOnLogHandler(logging.FileHandler):
    """
    Custom file handler that flushes after each log entry.
    
    Ensures log messages are written immediately to disk, which is
    important for debugging MPI processes that may crash.
    """
    
    def emit(self, record):
        """
        Emit a log record and flush the stream immediately.
        
        Args:
            record: LogRecord to be written
        """
        super().emit(record)
        self.flush()


def get_log_file_path(log_file: str = None) -> Path:
    """Return the configured log file path as an absolute path."""
    file_name = log_file or CONFIG.system.log.file
    log_dir = Path(CONFIG.system.log.path).expanduser()
    if not log_dir.is_absolute():
        log_dir = Path.cwd() / log_dir
    return (log_dir / file_name).resolve()


def get_setup_status_file_path() -> Path:
    """Return the controller-owned setup progress event-stream path."""
    return get_log_file_path().with_name("setup_status.jsonl")


def display_path(path: str | Path) -> str:
    """Return a path string suitable for logs, relative to the run directory."""
    path_value = Path(path).expanduser()
    if not path_value.is_absolute():
        return str(path_value)
    return os.path.relpath(path_value, Path.cwd())


def display_command(command: list[str]) -> str:
    """Return a shell-like command string with path arguments made relative."""
    import shlex

    return " ".join(shlex.quote(_display_command_part(part)) for part in command)


def _display_command_part(value: str) -> str:
    """Make one command argument relative when it is path-shaped."""
    if "/" not in value and "\\" not in value:
        return value
    return display_path(value)


def get_logger(
    name: str,
    log_file: str = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """
    Create or retrieve a logger with file output.
    
    Creates a logger that writes to a file specified in CONFIG or via parameter.
    The log directory is created if it doesn't exist.
    
    Args:
        name: Logger name (typically __name__ of the calling module)
        log_file: Optional override for log filename (defaults to CONFIG.system.log.file)
        level: Logging level (defaults to INFO)
        
    Returns:
        Configured Logger instance that flushes after each write
    """
    log_path = get_log_file_path(log_file)
    log_dir = log_path.parent
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    for existing_handler in list(logger.handlers):
        logger.removeHandler(existing_handler)
        existing_handler.close()
    handler = FlushOnLogHandler(str(log_path), mode="a", encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
