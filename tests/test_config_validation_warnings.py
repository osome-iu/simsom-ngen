# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Configuration warnings belong to the controller's setup log, once per run."""

import copy
from contextlib import ExitStack, redirect_stderr
import io
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from config_helper import CONFIG, DynamicConfigLoader, _validate_required_config
from components.controller import run_controller


ALL_LURKERS = (
    "User type inactive has no active users after lurker rounding; "
    "other user types provide active users"
)


class ConfigValidationWarningTests(unittest.TestCase):
    def config(self):
        config = copy.deepcopy(CONFIG.config)
        types = config["setup_db"]["user_types"]
        types["normal"].update(num_nodes=10, lurker_ratio=0)
        types["bad"]["num_nodes"] = 0
        return config

    def test_zero_population_is_an_allowed_inactive_template(self):
        with self.assertNoLogs(level="WARNING"):
            warnings = _validate_required_config(self.config())
        self.assertEqual(warnings, ())

    def test_reload_keeps_zero_population_template_silent(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.jsonc"
            path.write_text(json.dumps(config))
            with self.assertNoLogs(level="WARNING"):
                loader = DynamicConfigLoader(str(path))
                loader.load(str(path))
                self.assertEqual(loader.validation_warnings, ())
                config["setup_db"]["user_types"]["bad"].update(num_nodes=1, lurker_ratio=0)
                path.write_text(json.dumps(config))
                loader.load(str(path))
                self.assertEqual(loader.validation_warnings, ())

    def test_controller_records_each_warning_once_in_the_configured_log(self):
        config = self.config()
        types = config["setup_db"]["user_types"]
        types["inactive"] = {**types["normal"], "num_nodes": 3, "lurker_ratio": 1.0}
        with tempfile.TemporaryDirectory() as directory:
            config["system"]["log"]["path"] = directory
            path = Path(directory) / "config.jsonc"
            path.write_text(json.dumps(config))
            with self.assertNoLogs(level="WARNING"):
                loader = DynamicConfigLoader(str(path))
                loader.load(str(path))
            self.assertEqual(loader.validation_warnings, (ALL_LURKERS,))
            stderr = io.StringIO()
            with ExitStack() as stack:
                stack.enter_context(redirect_stderr(stderr))
                stack.enter_context(patch("logger_helper.CONFIG", loader))
                for name in (
                    "create_controller_database_client", "SetupProgressReporter",
                    "DefaultSetupDbPlanner", "plan_initial_state_stream",
                    "release_processes_after_setup", "wait_for_termination",
                    "coordinate_process_shutdown",
                ):
                    stack.enter_context(patch("components.controller." + name))
                stack.enter_context(patch(
                    "components.controller.setup_database",
                    return_value=SimpleNamespace(users=13, follower_edges=0),
                ))
                run_controller(SimpleNamespace(config=loader))
            log = (Path(directory) / config["system"]["log"]["file"]).read_text()
            self.assertEqual(log.count("[WARNING]"), 1)
            for message in (ALL_LURKERS,):
                self.assertEqual(log.count(message), 1)
                self.assertEqual(stderr.getvalue().count(message), 1)
            logger = logging.getLogger("components.controller.Controller")
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
