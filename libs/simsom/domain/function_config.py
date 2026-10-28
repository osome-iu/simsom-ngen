# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Named function inputs; see docs/algorithms/configuration.md#function-inputs."""

from functools import partial
import inspect
import math
from types import SimpleNamespace
from typing import Any, Callable


def function_entries(value: Any, field_name: str) -> dict[str, dict[str, Any]]:
    """Read ordered inputs; see docs/algorithms/configuration.md#function-inputs."""
    if isinstance(value, SimpleNamespace):
        value = vars(value)
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object mapping function names to inputs")
    result = {}
    for name, inputs in value.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{field_name} requires non-empty function names")
        if isinstance(inputs, SimpleNamespace):
            inputs = vars(inputs)
        if not isinstance(inputs, dict):
            raise ValueError(f"{field_name}.{name} must be an input object")
        result[name] = inputs
    return result


def bind_function(
    name: str, inputs: dict[str, Any], function: Callable, field_name: str,
    *, runtime_count: int = 1, runtime_keywords: tuple[str, ...] = (),
) -> Callable:
    """Require every model input; see docs/algorithms/configuration.md#validation.

    Runtime context is supplied by the caller. Model parameters, including
    Python parameters with defaults, must be explicit in configuration.
    """
    parameters = list(inspect.signature(function).parameters.values())
    annotations = inspect.get_annotations(function, eval_str=True)
    runtime_names = {p.name for p in parameters[:runtime_count]} | set(runtime_keywords)
    expected = {p.name: p for p in parameters if p.name not in runtime_names}
    missing = expected.keys() - inputs.keys()
    unexpected = inputs.keys() - expected.keys()
    if missing:
        raise ValueError(f"{field_name}.{name} missing required inputs: {', '.join(sorted(missing))}")
    if unexpected:
        raise ValueError(f"{field_name}.{name} unknown inputs: {', '.join(sorted(unexpected))}")
    for key, value in inputs.items():
        annotation = annotations.get(key, expected[key].annotation)
        if annotation is dict and not isinstance(value, (dict, SimpleNamespace)):
            raise ValueError(f"{field_name}.{name}.{key} must be an object")
        if annotation in (float, int):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field_name}.{name}.{key} must be numeric")
            if annotation is int and not isinstance(value, int):
                raise ValueError(f"{field_name}.{name}.{key} must be an integer")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{field_name}.{name}.{key} must be finite")
    return partial(function, **inputs)


def resolve_functions(
    value: Any, registry: dict[str, Callable], field_name: str,
    *, runtime_count: int = 1, runtime_keywords: tuple[str, ...] = (),
) -> tuple[tuple[str, Callable], ...]:
    """Bind a configured group; see docs/algorithms/configuration.md#function-inputs."""
    result = []
    for name, inputs in function_entries(value, field_name).items():
        if name not in registry:
            raise ValueError(f"{field_name}: {name!r} must be one of: {', '.join(sorted(registry))}")
        result.append((name, bind_function(
            name, inputs, registry[name], field_name,
            runtime_count=runtime_count, runtime_keywords=runtime_keywords,
        )))
    return tuple(result)


def resolve_one(value: Any, registry: dict[str, Callable], field_name: str, **kwargs):
    """Require one algorithm choice; see docs/algorithms/configuration.md#function-inputs."""
    result = resolve_functions(value, registry, field_name, **kwargs)
    if len(result) != 1:
        raise ValueError(f"{field_name} must configure exactly one function")
    return result[0]
