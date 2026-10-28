# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Unit tests for policy annotation contracts and default quality rules."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from components.policy import (  # noqa: E402
    policy_cursor_after,
    run_policy,
    run_policy_loop,
    to_policy_moderation_actions,
)
from db.core import DatabaseSettings  # noqa: E402
from db.policy import (  # noqa: E402
    PolicyCursor,
    PolicyModerationAction,
    PolicyTargetBatch,
    PolicyTargetRecord,
    PostgresPolicyDatabase,
)
from domain.post_label_semantics import (  # noqa: E402
    ILLEGAL_LABEL,
    LEGALITY_LABEL,
    SURVIVAL_TIME_LABEL,
)
from subcomponents.policy_annotations import (  # noqa: E402
    REMOVED_INTERVENTION,
    intervention_rule,
    intervention_entry,
    intervention_rules_for_source,
    intervention_source,
    user_duplicate_reaction_cleanup_intervention,
)
from subcomponents.policy_rules import (  # noqa: E402
    DefaultPolicyEvaluator,
    PolicyDecision,
    PolicyRuleContext,
    PolicyRuleMatch,
    ReviewTarget,
    below_quality_threshold,
    policy_rule,
)


TEST_POLICY_RULES = [
    "test_below_quality_threshold",
    "illegal_content",
]


def _policy_config():
    """Build explicit policy settings for a policy-contract test."""
    return {
        "rule_functions": {name: {} for name in TEST_POLICY_RULES},
    }


@policy_rule("test_policy_rule")
def test_policy_rule(context: PolicyRuleContext) -> PolicyRuleMatch:
    """Return a test match to verify configured rule dispatch."""
    del context
    return PolicyRuleMatch(
        intervention_type="reviewed",
        violation_type="test_rule_matched",
    )


@policy_rule("test_below_quality_threshold")
def test_below_quality_threshold(
    context: PolicyRuleContext,
) -> PolicyRuleMatch | None:
    """Use an explicit test threshold without depending on shipped config."""
    return below_quality_threshold(context, threshold=0.25)


class RecordingPolicyDatabase(PostgresPolicyDatabase):
    """Policy DB test double that records SQL instead of executing psql."""

    def __init__(self, outputs=None, *, target_batch_size=500):
        super().__init__(
            DatabaseSettings(schema="unit_test"),
            target_batch_size=target_batch_size,
        )
        self.calls = []
        self.outputs = list(outputs or [])

    def run_psql(self, sql: str, *, capture_output: bool = False):
        self.calls.append((sql, capture_output))
        if capture_output:
            if self.outputs:
                return self.outputs.pop(0)
            return "[]"
        return None


class DummyLogger:
    """Logger test double that avoids writing to the real simulator log."""

    def info(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass


class PolicyContractTests(unittest.TestCase):
    """Validate policy annotations and configured policy-rule decisions."""

    def test_policy_decisions_allow_no_intervention_without_write_payload(self):
        """Non-intervention decisions should not produce moderation writes."""
        decision = PolicyDecision(
            subject_id="1",
        )

        self.assertEqual(decision.moderation_actions, [])
        self.assertEqual(to_policy_moderation_actions([decision]), [])

    def test_policy_dtos_validate_intervention_entries(self):
        """Policy outputs should reject malformed canonical interventions."""
        with self.assertRaisesRegex(ValueError, "intervention_type"):
            PolicyDecision(
                subject_id="1",
                interventions=(
                    {
                        "intervention_type": "",
                        "violation_type": "low_quality",
                        "rule": "test",
                    },
                ),
            )

        with self.assertRaisesRegex(ValueError, "interventions"):
            PolicyModerationAction(subject_id="1", interventions=())

        with self.assertRaisesRegex(TypeError, "interventions"):
            PolicyModerationAction(subject_id="1", interventions="removed")

        with self.assertRaisesRegex(ValueError, "rule"):
            PolicyDecision(
                subject_id="1",
                interventions=(
                    {
                        "intervention_type": REMOVED_INTERVENTION,
                        "violation_type": "low_quality",
                        "rule": "",
                    },
                ),
            )

    def test_low_quality_action_receives_quality_threshold_intervention(self):
        """Actions below the selected threshold should receive an intervention."""
        evaluator = DefaultPolicyEvaluator(
            SimpleNamespace(
                rule_functions={name: {} for name in TEST_POLICY_RULES},
            )
        )

        decisions = evaluator.evaluate(
            [
                ReviewTarget(
                    subject_id="42",
                    other_properties={"quality": 0.2},
                )
            ]
        )

        self.assertEqual(len(decisions), 1)
        self.assertEqual(
            decisions[0].moderation_actions,
            [
                {
                    "intervention_type": REMOVED_INTERVENTION,
                    "violation_type": "below_quality_threshold",
                    "rule": "test_below_quality_threshold",
                }
            ],
        )

    def test_quality_at_or_above_threshold_emits_no_intervention(self):
        """Actions at or above the threshold should advance cursor only."""
        evaluator = DefaultPolicyEvaluator(_policy_config())

        decisions = evaluator.evaluate(
            [
                ReviewTarget(
                    subject_id="42",
                    current_timestamp=100,
                    other_properties={"quality": 0.25},
                )
            ]
        )

        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].moderation_actions, [])
        self.assertEqual(to_policy_moderation_actions(decisions), [])

    def test_future_survival_time_blocks_configured_policy_rules(self):
        """Future survival time should block all configured policy rules."""
        evaluator = DefaultPolicyEvaluator(_policy_config())

        decisions = evaluator.evaluate(
            [
                ReviewTarget(
                    subject_id="42",
                    current_timestamp=99,
                    other_properties={
                        "quality": 0.2,
                        "post_labels": {
                            LEGALITY_LABEL: ILLEGAL_LABEL,
                            SURVIVAL_TIME_LABEL: 100,
                        },
                    },
                )
            ]
        )

        self.assertEqual(decisions[0].moderation_actions, [])

    def test_due_illegal_label_receives_illegal_content_intervention(self):
        """A due illegal label should produce an illegal-content intervention."""
        evaluator = DefaultPolicyEvaluator(_policy_config())

        decisions = evaluator.evaluate(
            [
                ReviewTarget(
                    subject_id="42",
                    current_timestamp=100,
                    other_properties={
                        "quality": 0.8,
                        "post_labels": {
                            LEGALITY_LABEL: ILLEGAL_LABEL,
                            SURVIVAL_TIME_LABEL: 100,
                        },
                    },
                )
            ]
        )

        self.assertEqual(
            decisions[0].moderation_actions,
            [
                {
                    "intervention_type": REMOVED_INTERVENTION,
                    "violation_type": "illegal_content",
                    "rule": "illegal_content",
                }
            ],
        )

    def test_missing_illegal_label_emits_no_illegal_content_intervention(self):
        """Absent illegal labels should not trigger illegal-content removal."""
        evaluator = DefaultPolicyEvaluator(_policy_config())

        for properties in (
            {
                "quality": 0.8,
                "post_labels": {},
            },
            {"quality": 0.8},
        ):
            with self.subTest(properties=properties):
                decisions = evaluator.evaluate(
                    [
                        ReviewTarget(
                            subject_id="42",
                            current_timestamp=100,
                            other_properties=properties,
                        )
                    ]
                )
                self.assertEqual(decisions[0].moderation_actions, [])

    def test_label_only_policy_rule_does_not_require_quality(self):
        """A rule should only require the metadata it consumes."""
        evaluator = DefaultPolicyEvaluator({"rule_functions": {"illegal_content": {}}})

        decisions = evaluator.evaluate(
            [
                ReviewTarget(
                    subject_id="42",
                    action_type="repost",
                    current_timestamp=100,
                    other_properties={
                        "post_labels": {
                            LEGALITY_LABEL: ILLEGAL_LABEL,
                        },
                    },
                )
            ]
        )

        self.assertEqual(
            decisions[0].moderation_actions,
            [
                {
                    "intervention_type": REMOVED_INTERVENTION,
                    "violation_type": "illegal_content",
                    "rule": "illegal_content",
                }
            ],
        )

    def test_quality_and_expired_survival_label_interventions_are_preserved(self):
        """Multiple matching rules should produce multiple intervention entries."""
        evaluator = DefaultPolicyEvaluator(_policy_config())

        decision = evaluator.evaluate(
            [
                ReviewTarget(
                    subject_id="42",
                    current_timestamp=100,
                    other_properties={
                        "quality": 0.2,
                        "post_labels": {
                            LEGALITY_LABEL: ILLEGAL_LABEL,
                            SURVIVAL_TIME_LABEL: 90,
                        },
                    },
                )
            ]
        )[0]

        self.assertEqual(
            decision.interventions,
            (
                {
                    "intervention_type": REMOVED_INTERVENTION,
                    "violation_type": "below_quality_threshold",
                    "rule": "test_below_quality_threshold",
                },
                {
                    "intervention_type": REMOVED_INTERVENTION,
                    "violation_type": "illegal_content",
                    "rule": "illegal_content",
                },
            ),
        )

    def test_existing_intervention_is_not_duplicated(self):
        """Policy should skip an already-present identical intervention entry."""
        evaluator = DefaultPolicyEvaluator(_policy_config())
        existing_intervention = intervention_entry(
            intervention_type=REMOVED_INTERVENTION,
            violation_type="illegal_content",
            rule="illegal_content",
        )

        decision = evaluator.evaluate(
            [
                ReviewTarget(
                    subject_id="42",
                    current_timestamp=100,
                    moderation_actions=[existing_intervention],
                    other_properties={
                        "quality": 0.8,
                        "post_labels": {
                            LEGALITY_LABEL: ILLEGAL_LABEL,
                            SURVIVAL_TIME_LABEL: 90,
                        },
                    },
                )
            ]
        )[0]

        self.assertEqual(decision.moderation_actions, [])

    def test_evaluator_runs_every_configured_policy_rule(self):
        """Every selected registry rule should contribute its matching result."""
        evaluator = DefaultPolicyEvaluator(
            {
                "rule_functions": {"test_policy_rule": {}},
            }
        )

        decision = evaluator.evaluate(
            [ReviewTarget(subject_id="42", other_properties={"quality": 0.8})]
        )[0]

        self.assertEqual(
            decision.interventions,
            (
                {
                    "intervention_type": "reviewed",
                    "violation_type": "test_rule_matched",
                    "rule": "test_policy_rule",
                },
            ),
        )

    def test_quality_rule_requires_numeric_unit_interval_quality(self):
        """Quality-threshold rules should require the quality metadata they use."""
        evaluator = DefaultPolicyEvaluator(_policy_config())

        with self.assertRaisesRegex(ValueError, "quality"):
            evaluator.evaluate(
                [
                    ReviewTarget(
                        subject_id="42",
                        action_type="reply",
                        other_properties={},
                    )
                ]
            )

        with self.assertRaisesRegex(TypeError, "quality"):
            evaluator.evaluate(
                [
                    ReviewTarget(
                        subject_id="42",
                        action_type="quote",
                        other_properties={"quality": "low"},
                    )
                ]
            )

    def test_policy_mapping_serializes_structured_moderation_actions(self):
        """Component mapping should carry canonical moderation annotations."""
        intervention = intervention_entry(
            intervention_type=REMOVED_INTERVENTION,
            violation_type="below_quality_threshold",
            rule="below_quality_threshold",
        )
        actions = to_policy_moderation_actions(
            [
                PolicyDecision(
                    subject_id="42",
                    interventions=(intervention,),
                )
            ]
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].interventions, (intervention,))
        self.assertEqual(
            actions[0].moderation_actions,
            [intervention],
        )

    def test_intervention_source_registry_maps_rules_to_sources(self):
        """Rule names should provide intervention ownership without row bloat."""
        user_rule = "test_user_intervention_rule"

        @intervention_rule(user_rule, source="user")
        def test_user_intervention():
            return None

        self.assertEqual(
            intervention_source(
                intervention_entry(
                    intervention_type=REMOVED_INTERVENTION,
                    violation_type="below_quality_threshold",
                    rule="test_below_quality_threshold",
                )
            ),
            "policy",
        )
        self.assertEqual(
            intervention_source(user_duplicate_reaction_cleanup_intervention()),
            "user",
        )
        self.assertIn(user_rule, intervention_rules_for_source("user"))

    def test_intervention_rule_rejects_conflicting_source_registration(self):
        """A persisted rule name should not map to two different sources."""

        @intervention_rule("test_conflicting_intervention_rule", source="policy")
        def first_intervention():
            return None

        with self.assertRaisesRegex(ValueError, "already registered"):

            @intervention_rule("test_conflicting_intervention_rule", source="user")
            def second_intervention():
                return None

    def test_policy_database_fetches_supported_action_targets(self):
        """Policy target rows should decode into policy target records."""
        database = RecordingPolicyDatabase(
            outputs=[
                """
                {
                    "targets": [
                        {
                            "subject_id": "42",
                            "cursor_action_id": 42,
                            "action_type": "reply",
                            "current_timestamp": 99,
                            "moderation_actions": [],
                            "other_properties": {"quality": 0.2}
                        }
                    ],
                    "next_cursor_action_id": 99
                }
                """
            ]
        )

        targets = database.fetch_moderation_targets()

        self.assertEqual(
            targets,
            [
                PolicyTargetRecord(
                    subject_id="42",
                    cursor_action_id=42,
                    action_type="reply",
                    current_timestamp=99,
                    moderation_actions=[],
                    other_properties={"quality": 0.2},
                )
            ],
        )
        self.assertEqual(targets.next_cursor, PolicyCursor(action_id=99))

    def test_policy_database_fetch_splits_new_and_due_target_branches(self):
        """Policy fetch SQL should give new and due rows separate planner paths."""
        database = RecordingPolicyDatabase(target_batch_size=2)

        database.fetch_moderation_targets(cursor=PolicyCursor(action_id=42))

        sql = database.calls[-1][0]
        self.assertIn("UNION ALL", sql)
        self.assertEqual(sql.count("LIMIT 2"), 3)
        self.assertIn("review_window AS", sql)
        self.assertIn("action_row.action_id > 42", sql)
        self.assertIn("action_row.action_id <= 42", sql)
        self.assertIn("post_labels,survival_time", sql)
        self.assertIn('FROM "unit_test"."simsom_clock_state"', sql)
        self.assertIn("SELECT current_tick_ms", sql)
        self.assertNotIn("SELECT MAX(timestamp)", sql)

    def test_policy_database_write_uses_one_bulk_update(self):
        """A small policy batch should use one bulk update."""
        database = RecordingPolicyDatabase()

        database.write_moderation_actions(
            [
                PolicyModerationAction(
                    subject_id="1",
                    interventions=(
                        intervention_entry(
                            intervention_type=REMOVED_INTERVENTION,
                            violation_type="below_quality_threshold",
                            rule="test_below_quality_threshold",
                        ),
                    ),
                ),
                PolicyModerationAction(
                    subject_id="2",
                    interventions=(
                        intervention_entry(
                            intervention_type=REMOVED_INTERVENTION,
                            violation_type="illegal_content",
                            rule="illegal_content",
                        ),
                    ),
                ),
            ]
        )

        sql = database.calls[-1][0]
        self.assertIn("WITH moderation_payload(action_id, interventions) AS", sql)
        self.assertIn("FROM moderation_payload", sql)
        self.assertEqual(
            sql.count('UPDATE "unit_test"."action" AS action_row'),
            1,
        )

    def test_policy_database_write_bounds_transaction_lock_count(self):
        database = RecordingPolicyDatabase(target_batch_size=2)
        intervention = intervention_entry(
            intervention_type=REMOVED_INTERVENTION,
            violation_type="below_quality_threshold",
            rule="test_below_quality_threshold",
        )

        actions = [
            PolicyModerationAction(subject_id=str(action_id), interventions=(intervention,))
            for action_id in range(1, 4)
        ]
        database.write_moderation_actions(actions[:2])

        self.assertEqual(len(database.calls), 1)
        sql = database.calls[0][0]
        self.assertEqual(sql.count("pg_advisory_xact_lock"), 1)
        self.assertTrue(sql.startswith("BEGIN;") and sql.rstrip().endswith("COMMIT;"))

        with self.assertRaisesRegex(ValueError, "policy.target_batch_size"):
            database.write_moderation_actions(actions)
        self.assertEqual(len(database.calls), 1)

    def test_policy_cursor_advances_to_latest_target(self):
        """Cursor should advance to the newest fetched action coordinate."""
        cursor = policy_cursor_after(
            [
                PolicyTargetRecord(
                    subject_id="10",
                    cursor_action_id=10,
                    other_properties={"quality": 0.9},
                ),
                PolicyTargetRecord(
                    subject_id="11",
                    cursor_action_id=11,
                    other_properties={"quality": 0.8},
                ),
            ]
        )

        self.assertEqual(cursor, PolicyCursor(action_id=11))

    def test_policy_cursor_does_not_move_backward_for_expired_old_targets(self):
        """Revisiting older delayed rows should not lower the policy cursor."""
        cursor = policy_cursor_after(
            [
                PolicyTargetRecord(
                    subject_id="10",
                    cursor_action_id=10,
                    other_properties={"quality": 0.9},
                ),
            ],
            cursor=PolicyCursor(action_id=42),
        )

        self.assertEqual(cursor, PolicyCursor(action_id=42))

    def test_policy_loop_records_cursor_when_new_rows_are_deferred(self):
        """Future-survival rows should not be rescanned as new rows every poll."""

        class DeferredNewRowsDatabase:
            def __init__(self):
                self.fetch_cursors = []
                self.recorded_cursors = []

            def fetch_policy_cursor(self):
                return None

            def should_terminate(self):
                return len(self.fetch_cursors) >= 1

            def fetch_moderation_targets(self, cursor=None):
                self.fetch_cursors.append(cursor)
                return PolicyTargetBatch(
                    [],
                    next_cursor=PolicyCursor(action_id=42),
                )

            def write_moderation_actions(self, actions):
                raise AssertionError("write_moderation_actions should not be called")

            def record_policy_cursor(self, cursor):
                self.recorded_cursors.append(cursor)

        database = DeferredNewRowsDatabase()

        run_policy_loop(
            database=database,
            policy_evaluator=DefaultPolicyEvaluator(_policy_config()),
            poll_interval_milliseconds=0.0,
        )

        self.assertEqual(database.fetch_cursors, [None])
        self.assertEqual(database.recorded_cursors, [PolicyCursor(action_id=42)])

    def test_policy_loop_evaluates_and_writes_one_batch(self):
        """Policy loop should fetch, evaluate, write, then stop on lifecycle."""

        class OneBatchDatabase:
            def __init__(self, quality):
                self.quality = quality
                self.should_terminate_calls = 0
                self.written = []
                self.recorded_cursors = []

            def fetch_policy_cursor(self):
                return None

            def should_terminate(self):
                self.should_terminate_calls += 1
                return self.should_terminate_calls > 1

            def fetch_moderation_targets(self, cursor=None):
                return [
                    PolicyTargetRecord(
                        subject_id="42",
                        cursor_action_id=42,
                        other_properties={"quality": self.quality},
                    )
                ]

            def write_moderation_actions(self, actions):
                self.written.extend(actions)

            def record_policy_cursor(self, cursor):
                self.recorded_cursors.append(cursor)

        for quality, removes in [(0.2, True), (0.9, False)]:
            with self.subTest(quality=quality):
                database = OneBatchDatabase(quality)
                with patch("components.policy.time.sleep") as sleep:
                    run_policy_loop(
                        database=database,
                        policy_evaluator=DefaultPolicyEvaluator(_policy_config()),
                        poll_interval_milliseconds=7.5,
                    )
                self.assertEqual(len(database.written), int(removes))
                if removes:
                    sleep.assert_not_called()
                    self.assertEqual(
                        database.written[0].interventions[0]["intervention_type"],
                        REMOVED_INTERVENTION,
                    )
                else:
                    sleep.assert_called_once_with(0.0075)
                self.assertEqual(database.recorded_cursors, [PolicyCursor(action_id=42)])

    def test_policy_loop_uses_cursor_for_next_latest_fetch(self):
        """Policy loop should request targets newer than the latest fetched target."""

        class CursorDatabase:
            def __init__(self):
                self.fetch_cursors = []
                self.written = []
                self.recorded_cursors = []

            def fetch_policy_cursor(self):
                return None

            def should_terminate(self):
                return len(self.fetch_cursors) >= 2

            def fetch_moderation_targets(self, cursor=None):
                self.fetch_cursors.append(cursor)
                if len(self.fetch_cursors) == 1:
                    return [
                        PolicyTargetRecord(
                            subject_id="42",
                            cursor_action_id=42,
                            other_properties={"quality": 0.8},
                        )
                    ]
                return []

            def write_moderation_actions(self, actions):
                self.written.extend(actions)

            def record_policy_cursor(self, cursor):
                self.recorded_cursors.append(cursor)

        database = CursorDatabase()

        run_policy_loop(
            database=database,
            policy_evaluator=DefaultPolicyEvaluator(_policy_config()),
            poll_interval_milliseconds=0.0,
        )

        self.assertEqual(
            database.fetch_cursors,
            [None, PolicyCursor(action_id=42)],
        )
        self.assertEqual(database.written, [])
        self.assertEqual(database.recorded_cursors, [PolicyCursor(action_id=42)])

    def test_policy_loop_fetches_one_page_per_pass(self):
        """Policy delegates page boundaries to its database client."""

        class EmptyDatabase:
            def __init__(self):
                self.fetch_calls = 0

            def fetch_policy_cursor(self):
                return None

            def should_terminate(self):
                return self.fetch_calls > 0

            def fetch_moderation_targets(self, cursor=None):
                self.fetch_calls += 1
                return []

            def write_moderation_actions(self, actions):
                raise AssertionError("write_moderation_actions should not be called")

            def record_policy_cursor(self, cursor):
                raise AssertionError("record_policy_cursor should not be called")

        database = EmptyDatabase()

        with patch("components.policy.time.sleep") as sleep:
            run_policy_loop(
                database=database,
                policy_evaluator=DefaultPolicyEvaluator(_policy_config()),
                poll_interval_milliseconds=0.0,
            )
        sleep.assert_not_called()
        self.assertEqual(database.fetch_calls, 1)

    def test_policy_loop_uses_millisecond_poll_interval(self):
        """A configured millisecond interval should be converted to seconds."""

        class EmptyThenStopDatabase:
            def __init__(self):
                self.fetches = 0

            def fetch_policy_cursor(self):
                return None

            def should_terminate(self):
                return self.fetches >= 1

            def fetch_moderation_targets(self, cursor=None):
                self.fetches += 1
                return []

        database = EmptyThenStopDatabase()
        with patch("components.policy.time.sleep") as sleep:
            run_policy_loop(
                database=database,
                policy_evaluator=DefaultPolicyEvaluator(_policy_config()),
                poll_interval_milliseconds=7.0,
            )

        sleep.assert_called_once_with(0.007)

    def test_policy_loop_stops_before_starting_another_pass(self):
        """A controller stop message prevents a new policy fetch."""

        class StopCommunicator:
            def __init__(self):
                self.pending = True

            def iprobe(self):
                return self.pending

            def recv(self):
                self.pending = False
                return {"message": "stop_request"}

        class Database:
            def __init__(self):
                self.target_fetches = 0

            def fetch_policy_cursor(self):
                return None

            def fetch_moderation_targets(self, cursor=None):
                self.target_fetches += 1
                return []

        database = Database()
        run_policy_loop(
            database=database,
            policy_evaluator=DefaultPolicyEvaluator(_policy_config()),
            poll_interval_milliseconds=0,
            comm_world=StopCommunicator(),
        )

        self.assertEqual(database.target_fetches, 0)

    def test_run_policy_respects_disabled_config(self):
        """Disabled policy config should not open a policy database client."""
        context = SimpleNamespace(
            config=SimpleNamespace(
                policy=SimpleNamespace(enabled=False),
                database=SimpleNamespace(),
            )
        )

        with patch(
            "components.policy.create_policy_database_client",
        ) as create_database, patch(
            "components.policy.get_logger",
            return_value=DummyLogger(),
        ):
            run_policy(context)

        create_database.assert_not_called()


if __name__ == "__main__":
    unittest.main()
