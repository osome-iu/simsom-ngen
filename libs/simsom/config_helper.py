# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Configuration loader for the simulator.

Provides dynamic configuration loading from JSONC files with dot-notation access.
The global CONFIG instance is initialized on module import.
"""

import jsonc
import math
from domain.function_config import resolve_one, resolve_functions
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from domain.component_semantics import (
    component_function_from_config,
    validate_component_function,
)
from domain.analytics_semantics import AnalyticsEvaluationInterval
from domain.session_semantics import fixed_actions_per_active_session
from domain.termination_semantics import (
    criteria_function_set,
    allowed_termination_criterion_function_names,
)
from subcomponents.user_executor import CANONICAL_ACTION_TYPES
from subcomponents.user_executor import allowed_content_embedding_generation_function_names
from subcomponents.post_labeling import resolve_post_labeling_functions
from subcomponents.policy_rules import resolve_policy_rule_functions
from subcomponents.analytics_output import allowed_analytics_function_names
from subcomponents.setup_db import (
    DefaultSetupDbPlanner,
    WITHIN_TYPE_NETWORKING_PHASE,
    CROSS_TYPE_NETWORKING_PHASE,
)
from db.core import MAX_INTEGER_USER_ID

def _default_config_path() -> Path:
    """
    Get the default configuration file path.

    Returns:
        Path to default_simulator_config.jsonc in the config directory
    """
    return Path(__file__).resolve().parent / "config" / "default_simulator_config.jsonc"


REQUIRED_CONFIG_PATHS = (
    ("database",),
    ("database", "dsn"),
    ("database", "schema"),
    ("database", "clock_extension"),
    ("database", "clock_extension", "name"),
    ("system",),
    ("system", "log"),
    ("system", "log", "file"),
    ("system", "log", "path"),
    ("mpi",),
    ("mpi", "num_procs"),
    ("mpi", "processes"),
    ("mpi", "processes", "controller"),
    ("mpi", "processes", "controller", "rank"),
    ("mpi", "processes", "analytics"),
    ("mpi", "processes", "analytics", "rank"),
    ("mpi", "processes", "policy"),
    ("mpi", "processes", "policy", "rank"),
    ("mpi", "processes", "worker"),
    ("mpi", "processes", "worker", "start_rank"),
    ("controller",),
    ("controller", "dump_on_termination"),
    ("setup_db",),
    ("setup_db", "network_source"),
    ("setup_db", "network_file"),
    ("setup_db", "user_types"),
    ("worker",),
    ("worker", "batch_size"),
    ("worker", "idle_sleep_milliseconds"),
    ("worker", "action_insert_chunk_size"),
    ("recsys",),
    ("recsys", "feed_size"),
    ("recsys", "inventory_composition"),
    ("recsys", "inventory_composition", "out_of_network_ratio"),
    ("recsys", "inventory_composition", "size_multiplier"),
    ("recsys", "engagement_action_lookback_population_multiplier"),
    ("recsys", "feed_weight_components"),
    ("user_behavior",),
    ("user_behavior", "content_embedding_generation_function"),
    ("user_behavior", "actions_per_session_ratio"),
    ("user_behavior", "feed_weight_components"),
    ("user_behavior", "p_actions"),
    ("user_behavior", "vector_profile_generation_function"),
    ("policy",),
    ("policy", "enabled"),
    ("policy", "poll_interval_milliseconds"),
    ("policy", "rule_functions"),
    ("analytics",),
    ("analytics", "evaluation_interval"),
    ("analytics", "recurring_functions"),
    ("analytics", "final_functions"),
    ("analytics", "analytics_output_file"),
    ("termination",),
    ("termination", "termination_metrics_file"),
    ("termination", "criteria_functions"),
)


def _validate_required_config(cfg: Dict[str, Any]) -> tuple[str, ...]:
    """Validate inputs and return warnings for the controller's setup log.

    See docs/algorithms/configuration.md#validation.
    """
    for path in REQUIRED_CONFIG_PATHS:
        _require_config_path(cfg, path)
    _require_non_empty_string(cfg["database"]["dsn"], "database.dsn")
    _require_non_empty_string(cfg["database"]["schema"], "database.schema")
    _require_non_empty_string(
        cfg["database"]["clock_extension"]["name"],
        "database.clock_extension.name",
    )
    _require_non_empty_string(cfg["system"]["log"]["file"], "system.log.file")
    _require_non_empty_string(cfg["system"]["log"]["path"], "system.log.path")
    _require_boolean(
        cfg["controller"]["dump_on_termination"],
        "controller.dump_on_termination",
    )
    if "final_analytics_timeout_seconds" in cfg["controller"]:
        _require_positive_number(
            cfg["controller"]["final_analytics_timeout_seconds"],
            "controller.final_analytics_timeout_seconds",
        )
    _validate_mpi_config(cfg["mpi"])
    _validate_network_source(cfg["setup_db"]["network_source"])
    _validate_network_file(cfg["setup_db"]["network_file"])
    warnings = _validate_user_types_config(
        cfg["setup_db"]["user_types"],
        cfg["setup_db"]["network_source"],
    )
    _require_non_negative_integer(
        cfg["recsys"]["feed_size"],
        "recsys.feed_size",
    )
    _validate_inventory_composition(cfg["recsys"]["inventory_composition"])
    _require_positive_number(
        cfg["recsys"]["engagement_action_lookback_population_multiplier"],
        "recsys.engagement_action_lookback_population_multiplier",
    )
    _validate_weight_components(
        cfg["recsys"]["feed_weight_components"],
        "recsys.feed_weight_components",
    )
    _validate_weight_components(
        cfg["user_behavior"]["feed_weight_components"],
        "user_behavior.feed_weight_components",
    )
    _validate_content_embedding_generation_function(
        cfg["user_behavior"]["content_embedding_generation_function"],
        "user_behavior.content_embedding_generation_function",
    )
    _validate_user_action_settings(cfg["user_behavior"]["p_actions"])
    _validate_vector_profile_generation(
        cfg["user_behavior"]["vector_profile_generation_function"]
    )
    _require_probability(
        cfg["user_behavior"]["actions_per_session_ratio"],
        "user_behavior.actions_per_session_ratio",
    )
    fixed_actions_per_active_session(
        cfg["recsys"]["feed_size"], cfg["user_behavior"]["actions_per_session_ratio"]
    )
    _validate_worker_config(cfg["worker"])
    _validate_policy_config(cfg["policy"])
    _validate_analytics_config(cfg["analytics"])
    _validate_termination_config(cfg["termination"])
    return warnings


def _require_config_path(cfg: Dict[str, Any], path: tuple[str, ...]) -> None:
    current: Any = cfg
    for part in path:
        if not isinstance(current, dict) or part not in current:
            dotted_path = ".".join(path)
            raise ValueError(f"Missing required config field: {dotted_path}")
        current = current[part]


def _validate_network_source(value: Any) -> None:
    """Validate the configured source for initial follower-network rows."""
    allowed_sources = DefaultSetupDbPlanner.network_source_names()
    if not isinstance(value, str) or value not in allowed_sources:
        allowed = ", ".join(sorted(allowed_sources))
        raise ValueError(f"setup_db.network_source must be one of: {allowed}")


def _validate_mpi_config(value: Any) -> None:
    """Validate MPI process layout config before the runner builds a command."""
    if not isinstance(value, dict):
        raise ValueError("mpi must be an object")
    num_procs = value["num_procs"]
    _require_positive_integer(num_procs, "mpi.num_procs")
    processes = value["processes"]
    if not isinstance(processes, dict):
        raise ValueError("mpi.processes must be an object")
    for process_name in ("controller", "analytics", "policy"):
        process_config = processes[process_name]
        if not isinstance(process_config, dict):
            raise ValueError(f"mpi.processes.{process_name} must be an object")
        _require_non_negative_integer(
            process_config["rank"],
            f"mpi.processes.{process_name}.rank",
        )
    worker_config = processes["worker"]
    if not isinstance(worker_config, dict):
        raise ValueError("mpi.processes.worker must be an object")
    _require_non_negative_integer(
        worker_config["start_rank"],
        "mpi.processes.worker.start_rank",
    )


def _validate_worker_config(value: Any) -> None:
    """Validate worker process controls."""
    if not isinstance(value, dict):
        raise ValueError("worker must be an object")
    _require_positive_integer(value["batch_size"], "worker.batch_size")
    _require_non_negative_number(
        value["idle_sleep_milliseconds"],
        "worker.idle_sleep_milliseconds",
    )
    _require_positive_integer(
        value["action_insert_chunk_size"],
        "worker.action_insert_chunk_size",
    )


def _validate_network_file(value: Any) -> None:
    """Validate the configured network import path value."""
    if not isinstance(value, str) or not value:
        raise ValueError("setup_db.network_file must be a non-empty string")


def _validate_user_types_config(user_types: Any, network_source: str) -> tuple[str, ...]:
    """Validate populations without logging in every importing MPI process.

    See docs/algorithms/configuration.md#validation.
    """
    if not isinstance(user_types, dict) or not user_types:
        raise ValueError("setup_db.user_types must be a non-empty object")

    total_nodes = 0
    total_active = 0
    warnings = []
    for user_type, type_config in user_types.items():
        if not isinstance(type_config, dict):
            raise ValueError(f"setup_db.user_types.{user_type} must be an object")
        if "num_nodes" not in type_config:
            raise ValueError(
                f"Missing required config field: setup_db.user_types.{user_type}.num_nodes"
            )
        node_count = _require_non_negative_integer(
            type_config["num_nodes"],
            f"setup_db.user_types.{user_type}.num_nodes",
        )
        total_nodes += node_count
        if "lurker_ratio" not in type_config:
            raise ValueError(
                f"Missing required config field: setup_db.user_types.{user_type}.lurker_ratio"
            )
        lurker_ratio = _require_lurker_ratio(
            type_config["lurker_ratio"],
            f"setup_db.user_types.{user_type}.lurker_ratio",
        )
        active_count = node_count - round(node_count * lurker_ratio)
        total_active += active_count
        if node_count > 0 and active_count == 0:
            warnings.append(
                f"User type {user_type} has no active users after lurker rounding; "
                "other user types provide active users"
            )

        if node_count > 0:
            for field_name in (
                "activity_level_distribution",
                "user_quality_distribution",
                "post_quality_distribution_function",
                "post_appeal_distribution_function",
            ):
                if field_name not in type_config:
                    raise ValueError(
                        f"Missing required config field: setup_db.user_types.{user_type}.{field_name}"
                    )
            if network_source == "synthetic" and "edge_generation" not in type_config:
                raise ValueError(
                    "Missing required config field: "
                    f"setup_db.user_types.{user_type}.edge_generation"
                )

        if "edge_generation" in type_config:
            _validate_edge_generation(
                type_config["edge_generation"],
                f"setup_db.user_types.{user_type}.edge_generation",
            )
        if "activity_level_distribution" in type_config:
            _validate_activity_distribution(
                type_config["activity_level_distribution"],
                f"setup_db.user_types.{user_type}.activity_level_distribution",
            )
        if "user_quality_distribution" in type_config:
            _validate_two_cluster_distribution(
                type_config["user_quality_distribution"],
                f"setup_db.user_types.{user_type}.user_quality_distribution",
            )
        if "post_quality_distribution_function" in type_config:
            _validate_function_selector(
                type_config,
                "post_quality_distribution_function",
                f"setup_db.user_types.{user_type}",
                _post_distribution_function_names(),
            )
        if "post_appeal_distribution_function" in type_config:
            _validate_function_selector(
                type_config,
                "post_appeal_distribution_function",
                f"setup_db.user_types.{user_type}",
                _post_distribution_function_names(),
            )
        if "post_labeling_functions" in type_config:
            _validate_post_labeling_functions(
                type_config["post_labeling_functions"],
                f"setup_db.user_types.{user_type}.post_labeling_functions",
            )

    if total_nodes <= 0:
        raise ValueError("setup_db.user_types must configure at least one node")
    if total_nodes > MAX_INTEGER_USER_ID:
        raise ValueError(
            "setup_db.user_types total num_nodes exceeds the INTEGER user ID limit "
            f"of {MAX_INTEGER_USER_ID}"
        )
    if total_active == 0:
        raise ValueError("setup_db.user_types leaves no active users after lurker rounding")
    return tuple(warnings)


def _validate_edge_generation(value: Any, field_name: str) -> None:
    """Validate explicit edge-generation function inputs for one user type."""
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    _validate_function_selector(
        value,
        "within_type_networking_function",
        field_name,
        DefaultSetupDbPlanner.edge_generation_function_names(
            WITHIN_TYPE_NETWORKING_PHASE
        ),
    )
    _validate_function_selector(
        value,
        "cross_type_networking_function",
        field_name,
        DefaultSetupDbPlanner.edge_generation_function_names(
            CROSS_TYPE_NETWORKING_PHASE
        ),
    )


def _validate_activity_distribution(value: Any, field_name: str) -> None:
    """Validate activity inputs; see docs/algorithms/configuration.md#setup-functions."""
    DefaultSetupDbPlanner._activity_distribution_spec({"activity_level_distribution": value})


def _validate_two_cluster_distribution(value: Any, field_name: str) -> None:
    """Validate parameter-only two-cluster inverse distribution settings."""
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    for child_name in ("w", "lambda_slope"):
        if child_name not in value:
            raise ValueError(f"Missing required config field: {field_name}.{child_name}")
    _require_probability(value["w"], f"{field_name}.w")
    lambda_slope = _require_number(value["lambda_slope"], f"{field_name}.lambda_slope")
    if lambda_slope <= 0:
        raise ValueError(f"{field_name}.lambda_slope must be greater than 0")


def _validate_function_selector(value: dict[str, Any], selector_name: str, parent_field_name: str,
                                allowed_functions: tuple[str, ...]) -> str:
    """Validate model inputs; see docs/algorithms/configuration.md#function-inputs."""
    field_name = f"{parent_field_name}.{selector_name}"
    if selector_name not in value:
        raise ValueError(f"Missing required config field: {field_name}")
    if selector_name in ("within_type_networking_function", "cross_type_networking_function"):
        specs = DefaultSetupDbPlanner._edge_generation_registry()
        registry = {name: getattr(DefaultSetupDbPlanner, specs[name].method_name) for name in allowed_functions}
        name, _ = resolve_one(value[selector_name], registry, field_name, runtime_count=1,
                             runtime_keywords=("source_user_type", "users_by_type", "state", "progress"))
    else:
        from db.worker import POST_DISTRIBUTION_FUNCTIONS
        name, _ = resolve_one(value[selector_name], POST_DISTRIBUTION_FUNCTIONS, field_name, runtime_count=2)
    return name


def _post_distribution_function_names() -> tuple[str, ...]:
    from db.worker import allowed_post_distribution_function_names

    return allowed_post_distribution_function_names()


def _validate_content_embedding_generation_function(value: Any, field_name: str) -> str:
    """Validate embedding inputs; see docs/algorithms/configuration.md#content-functions."""
    from subcomponents.user_executor import CONTENT_EMBEDDING_GENERATION_FUNCTIONS
    name, _ = resolve_one(value, CONTENT_EMBEDDING_GENERATION_FUNCTIONS, field_name)
    return name


def _validate_post_labeling_functions(value: Any, field_name: str) -> None:
    """Validate optional post-labeling selectors through their registry."""
    resolve_post_labeling_functions(
        value,
        field_name,
    )


def _validate_inventory_composition(value: Any) -> None:
    """Validate the total inventory multiplier and out-network allocation."""
    if not isinstance(value, dict):
        raise ValueError("recsys.inventory_composition must be an object")
    ratio_name = "recsys.inventory_composition.out_of_network_ratio"
    multiplier_name = "recsys.inventory_composition.size_multiplier"
    if "out_of_network_ratio" not in value:
        raise ValueError(f"Missing required config field: {ratio_name}")
    if "size_multiplier" not in value:
        raise ValueError(f"Missing required config field: {multiplier_name}")
    _require_probability(value["out_of_network_ratio"], ratio_name)
    _require_positive_integer(value["size_multiplier"], multiplier_name)


def _validate_weight_components(
    value: Any,
    field_name: str,
) -> None:
    """Validate configured RecSys/user-model weight components."""
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} must be a non-empty list")
    for index, component_config in enumerate(value):
        item_name = f"{field_name}[{index}]"
        if not isinstance(component_config, dict):
            raise ValueError(f"{item_name} must be an object")
        for child_name in ("importance_in", "importance_out"):
            if child_name not in component_config:
                raise ValueError(
                    f"Missing required config field: {item_name}.{child_name}"
                )
            _require_non_negative_number(
                component_config[child_name],
                f"{item_name}.{child_name}",
            )
        try:
            selector, function_name = component_function_from_config(component_config)
            validate_component_function(selector, function_name)
        except ValueError as exc:
            raise ValueError(f"{item_name}: {exc}") from exc


def _validate_user_action_settings(value: Any) -> None:
    """Validate configured user action weights for all supported action types."""
    if not isinstance(value, dict):
        raise ValueError("user_behavior.p_actions must be an object")
    enabled = False
    positive_mean = False
    for action_type in CANONICAL_ACTION_TYPES:
        field_name = f"user_behavior.p_actions.{action_type}"
        if action_type not in value:
            raise ValueError(f"Missing required config field: {field_name}")
        distribution = value[action_type]
        if (
            not isinstance(distribution, list)
            or len(distribution) != 2
        ):
            raise ValueError(f"{field_name} must be [mean, std]")
        mean = _require_non_negative_number(distribution[0], f"{field_name}[0]")
        std = _require_non_negative_number(distribution[1], f"{field_name}[1]")
        enabled = enabled or mean > 0 or std > 0
        positive_mean = positive_mean or mean > 0
    if not enabled:
        raise ValueError("user_behavior.p_actions must enable at least one action type")
    if not positive_mean:
        raise ValueError(
            "user_behavior.p_actions must have a positive mean for at least one enabled action type"
        )


def _validate_vector_profile_generation(value: Any) -> None:
    """Validate the configured user vector representation generator."""
    DefaultSetupDbPlanner._vector_profile_generation_spec(value)


def _validate_policy_config(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("policy must be an object")
    _require_boolean(value["enabled"], "policy.enabled")
    if "target_batch_size" in value:
        _require_positive_integer(value["target_batch_size"], "policy.target_batch_size")
    _require_non_negative_number(
        value["poll_interval_milliseconds"],
        "policy.poll_interval_milliseconds",
    )
    resolve_policy_rule_functions(value["rule_functions"], "policy.rule_functions")


def _validate_analytics_config(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("analytics must be an object")
    AnalyticsEvaluationInterval.from_config(value["evaluation_interval"])
    for field_name in ("recurring_functions", "final_functions"):
        functions = value[field_name]
        from subcomponents.analytics_output import ANALYTICS_FUNCTIONS
        resolve_functions(functions, ANALYTICS_FUNCTIONS, f"analytics.{field_name}")
    _require_non_empty_string(
        value["analytics_output_file"],
        "analytics.analytics_output_file",
    )


def _validate_termination_config(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("termination must be an object")
    _require_non_empty_string(
        value["termination_metrics_file"],
        "termination.termination_metrics_file",
    )
    criteria_functions = criteria_function_set(
        value["criteria_functions"],
        allowed_functions=allowed_termination_criterion_function_names(),
        field_name="termination.criteria_functions",
    )
    if not criteria_functions:
        raise ValueError(
            "termination.criteria_functions must contain at least one criterion function"
        )


def _require_probability(value: Any, field_name: str) -> float:
    probability = _require_number(value, field_name)
    if probability < 0.0 or probability > 1.0:
        raise ValueError(f"{field_name} must satisfy 0 <= value <= 1")
    return probability


def _require_lurker_ratio(value: Any, field_name: str) -> float:
    """Allow an all-lurker type; population-wide activity is checked separately.

    See docs/algorithms/configuration.md#validation.
    """
    return _require_probability(value, field_name)


def _require_non_negative_number(value: Any, field_name: str) -> float:
    number = _require_number(value, field_name)
    if number < 0:
        raise ValueError(f"{field_name} must be greater than or equal to 0")
    return number


def _require_positive_number(value: Any, field_name: str) -> float:
    number = _require_number(value, field_name)
    if number <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return number


def _require_non_negative_integer(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be greater than or equal to 0")
    return value


def _require_positive_integer(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    if value < 1:
        raise ValueError(f"{field_name} must be greater than 0")
    return value


def _require_boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _require_number(value: Any, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{field_name} must be finite") from None
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number

class ConfigNamespace(SimpleNamespace):
    """Recursively convert dictionaries to objects for dot notation access."""
    def __init__(self, dictionary: Dict[str, Any]):
        super().__init__(**{k: self._to_namespace(v) for k, v in dictionary.items()})

    def _to_namespace(self, value: Any):
        if isinstance(value, dict):
            return ConfigNamespace(value)
        if isinstance(value, list):
            return [self._to_namespace(v) if isinstance(v, dict) else v for v in value]
        return value

class DynamicConfigLoader:
    """
    Dynamically load and validate JSON configuration with dot notation access.
    Caches per absolute config file path.
    """
    _config_cache: Dict[str, "DynamicConfigLoader"] = {}

    def __new__(cls, config_file: str | None = None):
        cfg_path = Path(config_file) if config_file else _default_config_path()
        key = str(cfg_path.resolve())
        if key in cls._config_cache:
            return cls._config_cache[key]
        instance = super().__new__(cls)
        cls._config_cache[key] = instance
        return instance

    def __init__(self, config_file: str | None = None):
        # Avoid triggering __getattr__ during init
        if object.__getattribute__(self, "__dict__").get("_initialized", False):
            return

        object.__setattr__(self, "config_file", str((Path(config_file) if config_file else _default_config_path()).resolve()))

        cfg = self._load_json_config()

        warnings = _validate_required_config(cfg)

        object.__setattr__(self, "config", cfg)
        object.__setattr__(self, "namespace", ConfigNamespace(cfg))
        object.__setattr__(self, "_validation_warnings", warnings)
        object.__setattr__(self, "_initialized", True)

    def load(self, config_file: str | None = None) -> None:
        """
        Load configuration in-place to keep existing references valid.
        """
        new_path = Path(config_file) if config_file else _default_config_path()
        object.__setattr__(self, "config_file", str(new_path.resolve()))

        cfg = self._load_json_config()
        warnings = _validate_required_config(cfg)

        object.__setattr__(self, "config", cfg)
        object.__setattr__(self, "namespace", ConfigNamespace(cfg))
        object.__setattr__(self, "_validation_warnings", warnings)

    @property
    def validation_warnings(self) -> tuple[str, ...]:
        """Return warnings from the last successfully loaded configuration.

        See docs/algorithms/configuration.md#validation. The controller emits
        these once at setup; config imports and reloads do not emit log records.
        """
        return self._validation_warnings

    def _load_json_config(self) -> Dict[str, Any]:
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                return jsonc.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Configuration file not found: {self.config_file}")
        except Exception as e:
            raise ValueError(f"Error parsing JSONC file {self.config_file}: {e}")

    def __getattr__(self, name: str) -> Any:
        # Guard against recursion during early init
        if name in ("_initialized", "namespace", "config", "config_file"):
            raise AttributeError(name)
        ns = object.__getattribute__(self, "__dict__").get("namespace")
        if ns is not None and hasattr(ns, name):
            return getattr(ns, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any):
        if name in ("_initialized", "namespace", "config", "config_file"):
            object.__setattr__(self, name, value)
        else:
            ns = object.__getattribute__(self, "__dict__").get("namespace")
            if ns is None:
                object.__setattr__(self, name, value)
            else:
                setattr(ns, name, value)

    def __repr__(self):
        return f"DynamicConfigLoader({self.config_file})"

# Global config instance - loaded once when module is imported
CONFIG = DynamicConfigLoader()

def init_config(config_file: str | None = None):
    """Initialize or update the global config instance.

    See docs/algorithms/configuration.md#validation.
    """
    if config_file:
        CONFIG.load(config_file)
    return CONFIG

def get_config(config_file: str | None = None):
    """
    Get the global configuration instance. Loads once and caches for reuse.
    If config_file is None, uses the default under project config/.
    """
    global CONFIG
    return init_config(config_file)
