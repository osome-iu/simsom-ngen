# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Shared moderation intervention semantics."""

from collections.abc import Mapping, Sequence
from typing import Any

INTERVENTION_TYPE_PROPERTY = "intervention_type"
VIOLATION_TYPE_PROPERTY = "violation_type"
RULE_PROPERTY = "rule"
DELETE_INTERVENTION = "delete"
REMOVED_INTERVENTION = "removed"
FEED_EXCLUSION_INTERVENTION_TYPES = (DELETE_INTERVENTION, REMOVED_INTERVENTION)
_INTERVENTION_RULE_SOURCES: dict[str, str] = {}


def _require_non_empty_str(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _register_intervention_rule_source(rule: str, source: str) -> None:
    """Register which system source owns interventions written by a rule."""
    rule = _require_non_empty_str("rule", rule)
    source = _require_non_empty_str("source", source)
    existing = _INTERVENTION_RULE_SOURCES.get(rule)
    if existing is not None and existing != source:
        raise ValueError(
            f"intervention rule {rule!r} is already registered to source {existing!r}"
        )
    _INTERVENTION_RULE_SOURCES[rule] = source


def intervention_rule(name: str, *, source: str):
    """
    Register metadata for an intervention-producing rule or helper.

    Persisted moderation entries store the rule name, not the source. Declare
    source metadata on the same function/helper that owns the rule so analytics
    and other consumers can recover ownership from persisted entries without a
    separate JSON field.
    """
    rule_name = _require_non_empty_str("intervention rule name", name)
    source_name = _require_non_empty_str("intervention rule source", source)

    def decorator(function):
        _register_intervention_rule_source(rule_name, source_name)
        setattr(function, "_simsom_intervention_rule_name", rule_name)
        setattr(function, "_simsom_intervention_rule_source", source_name)
        return function

    return decorator


@intervention_rule("user_duplicate_reaction_cleanup", source="user")
def user_duplicate_reaction_cleanup_intervention() -> dict[str, str]:
    """Return the user-source intervention for prior repeated likes/reposts."""
    return intervention_entry(
        intervention_type=DELETE_INTERVENTION,
        violation_type="duplicate_reaction",
        rule="user_duplicate_reaction_cleanup",
    )


def intervention_entry(
    *,
    intervention_type: str,
    violation_type: str,
    rule: str,
) -> dict[str, str]:
    """Return one validated canonical intervention entry."""
    return validate_intervention_entry(
        {
            INTERVENTION_TYPE_PROPERTY: intervention_type,
            VIOLATION_TYPE_PROPERTY: violation_type,
            RULE_PROPERTY: rule,
        },
        "intervention",
    )


def moderation_actions_payload(
    interventions: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Return a canonical moderation-actions intervention list."""
    return [
        validate_intervention_entry(intervention, f"interventions[{index}]")
        for index, intervention in enumerate(interventions)
    ]


def validate_moderation_actions(
    moderation_actions: Any,
    name: str,
) -> list[dict[str, str]]:
    """Return canonical intervention entries from a moderation-actions value."""
    if moderation_actions is None:
        return []
    if not isinstance(moderation_actions, Sequence) or isinstance(
        moderation_actions,
        (str, bytes),
    ):
        raise TypeError(f"{name} must be a list")
    return [
        validate_intervention_entry(
            intervention,
            f"{name}[{index}]",
        )
        for index, intervention in enumerate(moderation_actions)
    ]


def validate_intervention_entry(
    intervention: Any,
    name: str,
) -> dict[str, str]:
    """Validate and copy one canonical intervention entry."""
    if not isinstance(intervention, Mapping):
        raise TypeError(f"{name} must be an object")
    return {
        INTERVENTION_TYPE_PROPERTY: _require_non_empty_str(
            f"{name}.{INTERVENTION_TYPE_PROPERTY}",
            intervention.get(INTERVENTION_TYPE_PROPERTY),
        ),
        VIOLATION_TYPE_PROPERTY: _require_non_empty_str(
            f"{name}.{VIOLATION_TYPE_PROPERTY}",
            intervention.get(VIOLATION_TYPE_PROPERTY),
        ),
        RULE_PROPERTY: _require_non_empty_str(
            f"{name}.{RULE_PROPERTY}",
            intervention.get(RULE_PROPERTY),
        ),
    }


def has_intervention(
    moderation_actions: Any,
    intervention: Mapping[str, Any],
) -> bool:
    """Return whether a matching intervention is already present."""
    target = validate_intervention_entry(intervention, "intervention")
    return any(
        _intervention_identity(existing) == _intervention_identity(target)
        for existing in validate_moderation_actions(
            moderation_actions,
            "moderation_actions",
        )
    )


def intervention_source(intervention: Mapping[str, Any]) -> str | None:
    """Return the source registered for one intervention entry, if known."""
    entry = validate_intervention_entry(intervention, "intervention")
    return _INTERVENTION_RULE_SOURCES.get(entry[RULE_PROPERTY])


def intervention_rules_for_source(source: str) -> tuple[str, ...]:
    """Return rule names whose interventions belong to the provided source."""
    source = _require_non_empty_str("source", source)
    return tuple(
        sorted(
            rule
            for rule, registered_source in _INTERVENTION_RULE_SOURCES.items()
            if registered_source == source
        )
    )


def feed_exclusion_moderation_filter_sql(table_alias: str) -> str:
    """Return SQL that keeps rows not excluded from feed inventories."""
    excluded = ", ".join(
        f"'{intervention}'" for intervention in FEED_EXCLUSION_INTERVENTION_TYPES
    )
    return (
        "NOT EXISTS ("
        "SELECT 1 FROM jsonb_array_elements("
        f"{_interventions_array_sql(table_alias)}"
        ") AS moderation_action(intervention) "
        "WHERE moderation_action.intervention->>'"
        f"{INTERVENTION_TYPE_PROPERTY}' IN ({excluded})"
        ")"
    )


def intervention_source_exists_sql(table_alias: str, source: str) -> str:
    """Return SQL that matches rows with any intervention from one source."""
    rules = intervention_rules_for_source(source)
    if not rules:
        return "FALSE"
    rule_literals = ", ".join(_sql_string_literal(rule) for rule in rules)
    return (
        "EXISTS ("
        "SELECT 1 FROM jsonb_array_elements("
        f"{_interventions_array_sql(table_alias)}"
        ") AS moderation_action(intervention) "
        "WHERE moderation_action.intervention->>'"
        f"{RULE_PROPERTY}' IN ({rule_literals})"
        ")"
    )


def is_feed_excluded_moderation_actions(moderation_actions: Any) -> bool:
    """Return whether moderation JSON excludes a row from feed inventories."""
    return any(
        intervention[INTERVENTION_TYPE_PROPERTY]
        in FEED_EXCLUSION_INTERVENTION_TYPES
        for intervention in validate_moderation_actions(
            moderation_actions,
            "moderation_actions",
        )
    )


def _interventions_array_sql(table_alias: str) -> str:
    column = f"{table_alias}.moderation_actions"
    return (
        "CASE "
        f"WHEN jsonb_typeof({column}) = 'array' "
        f"THEN {column} "
        "ELSE '[]'::jsonb "
        "END"
    )


def _intervention_identity(intervention: Mapping[str, str]) -> tuple[str, str, str]:
    return (
        intervention[INTERVENTION_TYPE_PROPERTY],
        intervention[VIOLATION_TYPE_PROPERTY],
        intervention[RULE_PROPERTY],
    )


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
