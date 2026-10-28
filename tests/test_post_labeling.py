# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Tests for shared post-label storage and the initial legality labeler."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from domain.post_label_semantics import (  # noqa: E402
    ILLEGAL_LABEL,
    INTERNAL_SURVIVAL_DURATION_MS_LABEL,
    LEGALITY_LABEL,
    SURVIVAL_TIME_LABEL,
    merge_post_labels,
    post_label_value,
)
from subcomponents.post_labeling import (  # noqa: E402
    PostLabelingContext,
    exponential_survival_duration_ms,
    illegal_probability_n_survival_hours,
    legal_or_illegal_probability,
)


class PostLabelingTests(unittest.TestCase):
    """Validate label output and the shared action-property contract."""

    def test_basic_labeler_returns_namespaced_legality_label(self):
        """The initial labeler should return a legality mapping."""
        context = PostLabelingContext(
            action=SimpleNamespace(action_type="post"),
            user_record=SimpleNamespace(user_type="normal"),
            other_properties={},
        )

        with patch(
            "subcomponents.post_labeling.random.random",
            return_value=0.2,
        ):
            labels = legal_or_illegal_probability(context, probability=0.5)

        self.assertEqual(labels, {LEGALITY_LABEL: ILLEGAL_LABEL})

    def test_survival_labeler_returns_no_label_when_unassigned(self):
        """The survival labeler should stay sparse when content is unassigned."""
        context = PostLabelingContext(
            action=SimpleNamespace(action_type="post"),
            user_record=SimpleNamespace(user_type="normal"),
            other_properties={},
        )

        with patch("subcomponents.post_labeling.random.random", return_value=0.5):
            labels = illegal_probability_n_survival_hours(context, probability=0.01, expected_survival_hours=144)

        self.assertEqual(labels, {})

    def test_survival_labeler_returns_illegal_with_internal_duration(self):
        """Assigned illegal content should carry a duration for insert materialization."""
        context = PostLabelingContext(
            action=SimpleNamespace(action_type="post"),
            user_record=SimpleNamespace(user_type="normal"),
            other_properties={},
        )

        with patch(
            "subcomponents.post_labeling.random.random",
            return_value=0.0,
        ), patch(
            "subcomponents.post_labeling.random.expovariate",
            return_value=42.2,
        ):
            labels = illegal_probability_n_survival_hours(context, probability=0.01, expected_survival_hours=144)

        self.assertEqual(
            labels,
            {
                LEGALITY_LABEL: ILLEGAL_LABEL,
                INTERNAL_SURVIVAL_DURATION_MS_LABEL: 42,
            },
        )

    def test_exponential_survival_duration_is_positive(self):
        """Survival duration wrappers should never produce non-positive values."""
        with patch(
            "subcomponents.post_labeling.random.expovariate",
            return_value=0.0,
        ):
            self.assertEqual(exponential_survival_duration_ms(mean_ms=10), 1)

    def test_label_merge_and_policy_lookup_share_the_same_shape(self):
        """Worker storage and Policy lookup should use one shared contract."""
        properties = {"quality": 0.8}
        merge_post_labels(
            properties,
            {SURVIVAL_TIME_LABEL: 123},
            source_name="test labeler",
        )

        self.assertEqual(
            properties,
            {
                "quality": 0.8,
                "post_labels": {"survival_time": 123},
            },
        )
        self.assertEqual(post_label_value(properties, SURVIVAL_TIME_LABEL), 123)

    def test_duplicate_label_names_raise_instead_of_overwriting(self):
        """Two labelers may not silently replace the same label namespace."""
        properties = {"post_labels": {"legality": "legal"}}

        with self.assertRaisesRegex(ValueError, "already exist"):
            merge_post_labels(
                properties,
                {LEGALITY_LABEL: "illegal"},
                source_name="second labeler",
            )

    def test_malformed_stored_labels_raise(self):
        """A malformed persisted label object is a data error."""
        with self.assertRaisesRegex(TypeError, "post_labels"):
            post_label_value({"post_labels": "illegal"}, LEGALITY_LABEL)


if __name__ == "__main__":
    unittest.main()
