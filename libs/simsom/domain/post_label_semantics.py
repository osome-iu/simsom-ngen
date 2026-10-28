# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Shared storage and validation rules for generated post labels."""

from collections.abc import Mapping
from typing import Any


POST_LABELS_PROPERTY = "post_labels"
LEGALITY_LABEL = "legality"
LEGAL_LABEL = "legal"
ILLEGAL_LABEL = "illegal"
SURVIVAL_TIME_LABEL = "survival_time"
INTERNAL_SURVIVAL_DURATION_MS_LABEL = "_survival_duration_ms"
PostLabelValue = str | int | float


def merge_post_labels(
    other_properties: dict[str, Any],
    labels: Mapping[str, PostLabelValue],
    *,
    source_name: str,
) -> None:
    """Merge one labeler's namespaced labels into action properties."""
    validated_labels = validate_post_labels(labels, source_name)
    if not validated_labels:
        return

    existing = other_properties.get(POST_LABELS_PROPERTY)
    if existing is None:
        existing_labels: dict[str, str] = {}
    elif isinstance(existing, dict):
        existing_labels = dict(existing)
        validate_post_labels(existing_labels, "existing post labels")
    else:
        raise TypeError(
            f"other_properties.{POST_LABELS_PROPERTY} must be an object"
        )

    duplicate_names = sorted(set(existing_labels).intersection(validated_labels))
    if duplicate_names:
        names = ", ".join(duplicate_names)
        raise ValueError(f"post label keys already exist: {names}")

    existing_labels.update(validated_labels)
    other_properties[POST_LABELS_PROPERTY] = existing_labels


def post_label_value(
    other_properties: Mapping[str, Any],
    label_name: str,
) -> PostLabelValue | None:
    """Return one stored post-label value, or None when it is absent."""
    labels = other_properties.get(POST_LABELS_PROPERTY)
    if labels is None:
        return None
    validate_post_labels(labels, "candidate.other_properties.post_labels")
    return labels.get(label_name)


def validate_post_labels(
    labels: Any,
    name: str,
) -> dict[str, PostLabelValue]:
    """Validate and copy a namespaced post-label mapping."""
    if not isinstance(labels, Mapping):
        raise TypeError(f"{name} must be an object")

    validated: dict[str, PostLabelValue] = {}
    for label_name, label_value in labels.items():
        if not isinstance(label_name, str) or not label_name.strip():
            raise ValueError(f"{name} keys must be non-empty strings")
        if isinstance(label_value, str):
            if not label_value.strip():
                raise ValueError(f"{name}.{label_name} must be a non-empty string")
        elif isinstance(label_value, (int, float)):
            if isinstance(label_value, bool):
                raise TypeError(f"{name}.{label_name} must not be bool")
        else:
            raise TypeError(
                f"{name}.{label_name} must be str, int, or float; "
                f"got {type(label_value).__name__}"
            )
        validated[label_name] = label_value
    return validated
