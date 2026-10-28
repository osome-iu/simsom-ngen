# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Policy-rule evaluation subcomponent.

Owned by the policy team. It converts review targets into policy decisions.
"""

from dataclasses import dataclass, field
import math
from domain.function_config import resolve_functions
from typing import Any, Callable, Protocol

from domain.content_semantics import QUALITY_PROPERTY_KEY
from domain.action_semantics import require_supported_action_type
from domain.post_label_semantics import (
    ILLEGAL_LABEL,
    LEGALITY_LABEL,
    SURVIVAL_TIME_LABEL,
    post_label_value,
)
from subcomponents.policy_annotations import (
    REMOVED_INTERVENTION,
    has_intervention,
    intervention_rule,
    intervention_entry,
    moderation_actions_payload,
    validate_intervention_entry,
)


@dataclass(frozen=True)
class ReviewTarget:
    subject_id: str
    action_type: str = "post"
    current_timestamp: int = 0
    moderation_actions: Any = None
    other_properties: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_supported_action_type(self.action_type)
        _require_non_negative_int("current_timestamp", self.current_timestamp)


@dataclass(frozen=True)
class PolicyRuleContext:
    """Inputs shared with each configured policy rule."""

    target: ReviewTarget
    current_timestamp: int


@dataclass(frozen=True)
class PolicyRuleMatch:
    """Describe one policy rule that matched a review target."""

    intervention_type: str
    violation_type: str
    rule: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str("intervention_type", self.intervention_type)
        _require_non_empty_str("violation_type", self.violation_type)
        _require_optional_non_empty_str("rule", self.rule)

    def with_rule(self, rule: str) -> "PolicyRuleMatch":
        """Return a concrete match tagged with the configured rule name."""
        _require_non_empty_str("rule", rule)
        if self.rule == rule:
            return self
        if self.rule is not None:
            raise ValueError(
                f"Policy rule {rule!r} returned mismatched rule {self.rule!r}"
            )
        return PolicyRuleMatch(
            intervention_type=self.intervention_type,
            violation_type=self.violation_type,
            rule=rule,
        )

    @property
    def intervention(self) -> dict[str, str]:
        """Return this match as one canonical intervention entry."""
        if self.rule is None:
            raise ValueError("PolicyRuleMatch.rule is required for serialization")
        return intervention_entry(
            intervention_type=self.intervention_type,
            violation_type=self.violation_type,
            rule=self.rule,
        )


@dataclass(frozen=True)
class PolicyDecision:
    subject_id: str
    interventions: tuple[dict[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_str("subject_id", self.subject_id)
        interventions = tuple(
            validate_intervention_entry(intervention, f"interventions[{index}]")
            for index, intervention in enumerate(self.interventions)
        )
        object.__setattr__(self, "interventions", interventions)

    @property
    def moderation_actions(self) -> list[dict[str, str]]:
        return moderation_actions_payload(self.interventions)


class PolicyEvaluator(Protocol):
    def evaluate(self, targets: list[ReviewTarget]) -> list[PolicyDecision]:
        """Return policy decisions for the provided review targets.

        See docs/algorithms/policy.md#rule-evaluation.
        """


PolicyRuleFunction = Callable[[PolicyRuleContext], PolicyRuleMatch | None]
POLICY_RULE_FUNCTIONS: dict[str, PolicyRuleFunction] = {}


def policy_rule(name: str, *, source: str = "policy"):
    """
    Register a config-visible policy rule and its intervention source.

    Source metadata lives on the rule declaration because persisted moderation
    entries store `rule`, not `source`.
    """

    def decorator(function: PolicyRuleFunction) -> PolicyRuleFunction:
        decorated = intervention_rule(name, source=source)(function)
        POLICY_RULE_FUNCTIONS[name] = decorated
        return decorated

    return decorator


def allowed_policy_rule_names() -> tuple[str, ...]:
    """Return the configured-policy rule names known by the registry."""
    return tuple(sorted(POLICY_RULE_FUNCTIONS))


def resolve_policy_rule_functions(function_names: Any, field_name: str) -> tuple[tuple[str, PolicyRuleFunction], ...]:
    """Bind policy rules; see docs/algorithms/policy.md#configured-rule-inputs."""
    return resolve_functions(function_names, POLICY_RULE_FUNCTIONS, field_name)


class DefaultPolicyEvaluator:
    """
    Configured policy-rule evaluator.

    Target eligibility is action-wide; configured rules own their metadata
    requirements.
    """

    def __init__(self, policy_config):
        """Resolve the configured policy rules once for later evaluations."""
        self.policy_rules = resolve_policy_rule_functions(
            _config_value(policy_config, "rule_functions"),
            "policy.rule_functions",
        )

    def evaluate(self, targets: list[ReviewTarget]) -> list[PolicyDecision]:
        """Run every configured rule and combine its matching decisions.

        See docs/algorithms/policy.md#rule-evaluation.
        """
        decisions: list[PolicyDecision] = []
        for target in targets:
            if not _survival_time_allows_policy(target):
                decisions.append(
                    PolicyDecision(
                        subject_id=target.subject_id,
                    )
                )
                continue
            context = PolicyRuleContext(
                target=target,
                current_timestamp=target.current_timestamp,
            )
            matches: list[PolicyRuleMatch] = []
            for function_name, rule in self.policy_rules:
                match = rule(context)
                if match is not None:
                    concrete_match = match.with_rule(function_name)
                    if not has_intervention(
                        target.moderation_actions,
                        concrete_match.intervention,
                    ):
                        matches.append(concrete_match)
            if not matches:
                decisions.append(
                    PolicyDecision(
                        subject_id=target.subject_id,
                    )
                )
                continue

            decisions.append(
                PolicyDecision(
                    subject_id=target.subject_id,
                    interventions=tuple(match.intervention for match in matches),
                )
            )
        return decisions


@policy_rule("below_quality_threshold", source="policy")
def below_quality_threshold(
    context: PolicyRuleContext,
    *,
    threshold: float,
) -> PolicyRuleMatch | None:
    """Apply the quality-threshold rule with an explicit configured threshold.

    See docs/algorithms/policy.md#rule-evaluation.
    """
    quality = _require_probability(
        _config_value(context.target.other_properties, QUALITY_PROPERTY_KEY),
        f"target.other_properties.{QUALITY_PROPERTY_KEY}",
    )
    if quality >= _require_probability(threshold, "threshold"):
        return None
    return PolicyRuleMatch(
        intervention_type=REMOVED_INTERVENTION,
        violation_type="below_quality_threshold",
    )


@policy_rule("illegal_content", source="policy")
def illegal_content(context: PolicyRuleContext) -> PolicyRuleMatch | None:
    """Match content labeled as illegal.

    See docs/algorithms/policy.md#rule-evaluation.
    """
    if (
        post_label_value(context.target.other_properties, LEGALITY_LABEL)
        != ILLEGAL_LABEL
    ):
        return None
    return PolicyRuleMatch(
        intervention_type=REMOVED_INTERVENTION,
        violation_type="illegal_content",
    )


def _config_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"Missing required config field: {key}")
        return value[key]
    if not hasattr(value, key):
        raise ValueError(f"Missing required config field: {key}")
    return getattr(value, key)


def _require_probability(value: Any, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 1.0:
        raise ValueError(f"{name} must satisfy 0 <= value <= 1")
    return number


def _survival_time_allows_policy(target: ReviewTarget) -> bool:
    due_time_value = post_label_value(target.other_properties, SURVIVAL_TIME_LABEL)
    if due_time_value is None:
        return True
    due_time = _require_non_negative_int(
        f"post_labels.{SURVIVAL_TIME_LABEL}",
        due_time_value,
    )
    return target.current_timestamp >= due_time


def _require_non_negative_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")
    return value


def _require_non_empty_str(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_optional_non_empty_str(name: str, value: Any) -> None:
    if value is None:
        return
    _require_non_empty_str(name, value)
