# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Unit tests for worker process orchestration helpers."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from components.worker import (  # noqa: E402
    _effective_lurker_ratio,
    build_feed,
    fixed_actions_per_active_session,
    inventory_limit_for_batch,
    inventory_limits_for_batch,
    run_worker_loop,
    to_action_view_records,
    to_feed_candidates,
    to_user_profile,
)
from db.core import ActionRow, UserRecord  # noqa: E402
from db.worker import (  # noqa: E402
    BatchInNetworkInventory,
    ClaimedUserSessionRecord,
    FeedInventoryContext,
)
from subcomponents.user_executor import (  # noqa: E402
    UserAction,
    UserSessionResult,
)
from subcomponents.recsys import IN_NETWORK, OUT_NETWORK, RankedFeed  # noqa: E402


class FakeWorkerDatabase:
    """Worker DB fake that returns one session and one feed inventory."""

    def __init__(self, sessions=None, terminate_after_checks=3):
        """Initialize call logs for worker-loop assertions."""
        self.termination_checks = 0
        self.terminate_after_checks = terminate_after_checks
        self.sessions = sessions
        self.claim_calls = []
        self.out_inventory_calls = []
        self.in_inventory_calls = []
        self.view_records = []
        self.written_actions = []

    def should_terminate(self) -> bool:
        """Stop after one full worker-loop pass."""
        self.termination_checks += 1
        return self.termination_checks > self.terminate_after_checks

    def claim_user_batch(
        self,
        batch_size: int,
        *,
        lurker_ratio: float,
        planned_action_count: int,
    ):
        """Return one typed claimed session."""
        self.claim_calls.append((batch_size, lurker_ratio, planned_action_count))
        if self.sessions is not None:
            return self.sessions
        return [
            ClaimedUserSessionRecord(
                user_id=1,
                session_id="session_1",
                user_record=UserRecord(
                    user_id=1,
                    user_type="normal",
                    activity_level=0.8,
                    user_behavior={"p_actions": {"post": 1.0}},
                    other_properties={},
                ),
                planned_action_count=1,
            )
        ]

    def fetch_out_network_feed_inventory(self, *, limit):
        """Return one batch-level global inventory."""
        self.out_inventory_calls.append(limit)
        return []

    def fetch_in_network_feed_inventory(self, claimed_sessions, *, limit):
        """Return one shared in-network inventory for the batch."""
        self.in_inventory_calls.append(
            ([session.user_id for session in claimed_sessions], limit)
        )
        action = ActionRow(
            action_id=42,
            creator_user_id=99,
            timestamp=10,
            action_type="post",
        )
        return BatchInNetworkInventory(
            actions=[action],
            visible_to_user_ids={
                42: frozenset(session.user_id for session in claimed_sessions)
            },
        )

    def write_action_views(self, views):
        """Record aggregate view counter updates."""
        self.view_records.extend(views)

    def write_actions(self, actions):
        """Record generated action writes."""
        self.written_actions.extend(actions)


class InvalidClaimDatabase(FakeWorkerDatabase):
    """Worker DB fake that returns an invalid claim payload."""

    def claim_user_batch(
        self,
        batch_size: int,
        *,
        lurker_ratio: float,
        planned_action_count: int,
    ):
        """Return an invalid claim so output validation can fail."""
        self.claim_calls.append((batch_size, lurker_ratio, planned_action_count))
        return [object()]


class FakeFeedBuilder:
    """Feed builder fake that returns a typed ranked feed."""

    def __init__(self):
        """Initialize call logs for feed-builder assertions."""
        self.calls = []

    def build_feed(self, user, candidates):
        """Return candidates unchanged inside RankedFeed."""
        self.calls.append((user, candidates))
        return RankedFeed(items=candidates)


class InvalidFeedBuilder:
    """Feed builder fake that returns the wrong output type."""

    def build_feed(self, user, candidates):
        """Return an invalid feed so output validation can fail."""
        return object()


class FakeUserExecutor:
    """User executor fake that returns a typed session result."""

    def __init__(self):
        """Initialize call logs for executor assertions."""
        self.calls = []

    def execute_session(self, user, feed):
        """Return views for every session and actions when planned."""
        self.calls.append((user, feed))
        actions = []
        if user.planned_action_count > 0:
            actions = [
                UserAction.post(
                    creator_user_id=user.user_id,
                    content_embedding=[0.1],
                    moderation_actions=[
                        {
                            "intervention_type": "queued",
                            "violation_type": "test_fixture",
                            "rule": "test_fixture",
                        }
                    ],
                    other_properties={"source": "unit"},
                )
            ]
        return UserSessionResult(
            viewed_action_ids=[item.action_id for item in feed.items],
            actions=actions,
        )


class WorkerLoopTests(unittest.TestCase):
    """Validate the worker loop and helper output validation."""

    def test_fixed_active_session_action_budget_uses_configured_feed_size(self):
        """Active sessions use one fixed budget derived from feed size."""
        self.assertEqual(fixed_actions_per_active_session(15, 0.20), 3)
        self.assertEqual(fixed_actions_per_active_session(15, 0.50), 8)
        self.assertEqual(fixed_actions_per_active_session(15, 0.80), 12)
        self.assertEqual(fixed_actions_per_active_session(15, 1.00), 15)
        for feed_size, ratio in ((15, 0.0), (20, 0.01), (1, 0.5), (0, 1.0)):
            with self.subTest(feed_size=feed_size, ratio=ratio):
                with self.assertRaisesRegex(ValueError, "at least one action"):
                    fixed_actions_per_active_session(feed_size, ratio)

    def test_reposts_preserve_viewers_before_deduplication_and_recency(self):
        from domain.action_semantics import action_source_id
        from subcomponents.recsys import DefaultFeedBuilder
        from subcomponents.user_executor import _recency_scores

        def repost(source_id, creator):
            return ActionRow(
                action_id=1, creator_user_id=99, action_type="post",
                other_properties={"source_action_id": source_id,
                                  "source_creator_user_id": creator,
                                  "source_action_type": "repost"},
            )
        first, other_viewer, latest, outside = (
            repost(10, 2), repost(20, 3), repost(30, 4), repost(40, 5)
        )
        shared = BatchInNetworkInventory(
            actions=[first, other_viewer, latest],
            visible_to_user_ids={10: frozenset({1}), 20: frozenset({2}),
                                 30: frozenset({1})},
        )
        self.assertEqual([action_source_id(a) for a in shared.for_user(1)], [10, 30])
        self.assertEqual([action_source_id(a) for a in shared.for_user(2)], [20])
        for viewer, expected_id, expected_network in (
            (1, 30, IN_NETWORK), (2, 20, IN_NETWORK), (3, 40, OUT_NETWORK)
        ):
            with self.subTest(viewer=viewer):
                candidates = to_feed_candidates(
                    FeedInventoryContext(
                        in_network_actions=shared.for_user(viewer),
                        out_network_actions=[outside, first,
                            ActionRow(action_id=25, creator_user_id=6, action_type="post")],
                    ),
                    ClaimedUserSessionRecord(user_id=viewer, session_id=viewer,
                        user_record=UserRecord(user_id=viewer, user_type="normal")),
                )
                self.assertEqual(len(candidates), 2)
                parent = next(c for c in candidates if c.action_id == 1)
                self.assertEqual(parent.source_action_id, expected_id)
                self.assertEqual(parent.network_source, expected_network)
                self.assertEqual(parent.source_creator_user_id, {1: 4, 2: 3, 3: 5}[viewer])
                expected_order = [1, 25] if expected_id > 25 else [25, 1]
                self.assertEqual([c.action_id for c in DefaultFeedBuilder._newest_first(candidates)], expected_order)
                self.assertEqual(_recency_scores(candidates)[expected_order[0]], 1.0)
                self.assertEqual(_recency_scores(candidates)[expected_order[1]], 0.5)

    def test_total_inventory_limit_uses_composition_inputs(self):
        """The multiplier determines the total pool before allocation."""
        self.assertEqual(
            inventory_limit_for_batch(
                multiplier=2,
                feed_size=5,
                batch_size=3,
            ),
            30,
        )
        self.assertEqual(
            inventory_limits_for_batch(
                multiplier=2,
                out_of_network_ratio=0.5,
                feed_size=5,
                batch_size=3,
            ),
            (15, 15),
        )
        self.assertEqual(
            inventory_limits_for_batch(
                multiplier=1,
                out_of_network_ratio=1.0,
                feed_size=5,
                batch_size=3,
            ),
            (0, 15),
        )
        self.assertEqual(
            inventory_limits_for_batch(
                multiplier=1,
                out_of_network_ratio=0.0,
                feed_size=5,
                batch_size=3,
            ),
            (15, 0),
        )
        self.assertEqual(
            inventory_limits_for_batch(
                multiplier=1,
                out_of_network_ratio=0.5,
                feed_size=1,
                batch_size=1,
            ),
            (1, 0),
        )

    def test_worker_loop_claims_builds_executes_and_writes(self):
        """Worker loop should claim, build, execute, and write one batch."""
        database = FakeWorkerDatabase()
        feed_builder = FakeFeedBuilder()
        user_executor = FakeUserExecutor()

        run_worker_loop(
            database=database,
            batch_size=2,
            lurker_ratio=0.25,
            planned_action_count=1,
            in_network_inventory_limit=5,
            out_network_inventory_limit=7,
            idle_sleep_milliseconds=0,
            feed_builder=feed_builder,
            user_executor=user_executor,
        )

        self.assertEqual(database.claim_calls, [(2, 0.25, 1)])
        self.assertEqual(database.out_inventory_calls, [7])
        self.assertEqual(database.in_inventory_calls, [([1], 5)])
        self.assertEqual(len(feed_builder.calls), 1)
        self.assertEqual(feed_builder.calls[0][1][0].network_source, IN_NETWORK)
        self.assertEqual(len(user_executor.calls), 1)
        user = user_executor.calls[0][0]
        self.assertEqual(user.planned_action_count, 1)
        self.assertFalse(hasattr(user, "lurker"))
        self.assertEqual(user.user_behavior, {"p_actions": {"post": 1.0}})
        self.assertEqual(len(database.view_records), 1)
        self.assertEqual(database.view_records[0].session_id, "session_1")
        self.assertEqual(database.view_records[0].viewer_user_id, 1)
        self.assertEqual(database.view_records[0].action_id, 42)
        self.assertEqual(len(database.written_actions), 1)
        written = database.written_actions[0]
        self.assertEqual(written.session_id, "session_1")
        self.assertEqual(written.user_record.user_id, 1)
        self.assertEqual(written.action.creator_user_id, 1)
        self.assertEqual(
            written.action.moderation_actions,
            [
                {
                    "intervention_type": "queued",
                    "violation_type": "test_fixture",
                    "rule": "test_fixture",
                }
            ],
        )

    def test_worker_idle_backoff_only_delays_empty_claims(self):
        """Milliseconds convert to seconds; zero and productive batches do not sleep."""
        for sessions, milliseconds, expected_sleep in [
            ([], 7.5, 0.0075),
            ([], 0, None),
            (None, 7.5, None),
        ]:
            with self.subTest(sessions=sessions, milliseconds=milliseconds):
                database = FakeWorkerDatabase(
                    sessions=sessions,
                    terminate_after_checks=1 if sessions == [] else 3,
                )
                with patch("components.worker.time.sleep") as sleep:
                    run_worker_loop(
                        database=database,
                        batch_size=2,
                        lurker_ratio=0.25,
                        planned_action_count=1,
                        in_network_inventory_limit=5,
                        out_network_inventory_limit=7,
                        idle_sleep_milliseconds=milliseconds,
                        feed_builder=FakeFeedBuilder(),
                        user_executor=FakeUserExecutor(),
                    )
                self.assertEqual(len(database.claim_calls), 1)
                if expected_sleep is None:
                    sleep.assert_not_called()
                else:
                    sleep.assert_called_once_with(expected_sleep)
                self.assertEqual(len(database.written_actions), int(sessions is None))

    def test_worker_loop_skips_writes_when_stop_requested_before_commit(self):
        """Generated writes should not commit after termination is requested."""
        database = FakeWorkerDatabase(terminate_after_checks=1)

        run_worker_loop(
            database=database,
            batch_size=2,
            lurker_ratio=0.25,
            planned_action_count=1,
            in_network_inventory_limit=5,
            out_network_inventory_limit=7,
            idle_sleep_milliseconds=0,
            feed_builder=FakeFeedBuilder(),
            user_executor=FakeUserExecutor(),
        )

        self.assertEqual(database.view_records, [])
        self.assertEqual(database.written_actions, [])

    def test_worker_loop_stops_before_claiming_another_batch(self):
        """A controller stop message prevents a new user batch claim."""

        class StopCommunicator:
            def __init__(self):
                self.pending = True

            def iprobe(self):
                return self.pending

            def recv(self):
                self.pending = False
                return {"message": "stop_request"}

        database = FakeWorkerDatabase()
        run_worker_loop(
            database=database,
            batch_size=2,
            lurker_ratio=0.25,
            planned_action_count=1,
            in_network_inventory_limit=5,
            out_network_inventory_limit=7,
            idle_sleep_milliseconds=0,
            feed_builder=FakeFeedBuilder(),
            user_executor=FakeUserExecutor(),
            comm_world=StopCommunicator(),
        )

        self.assertEqual(database.claim_calls, [])

    def test_worker_loop_persists_zero_activity_view_only_result(self):
        """Zero-activity users should build feeds and persist returned views."""
        database = FakeWorkerDatabase(
            sessions=[
                ClaimedUserSessionRecord(
                    user_id=2,
                    session_id="zero_activity_session",
                    user_record=UserRecord(
                        user_id=2,
                        user_type="normal",
                        activity_level=0.0,
                        user_behavior={"p_actions": {"post": 1.0}},
                        other_properties={},
                    ),
                    planned_action_count=0,
                )
            ]
        )
        feed_builder = FakeFeedBuilder()
        user_executor = FakeUserExecutor()

        run_worker_loop(
            database=database,
            batch_size=2,
            lurker_ratio=0.25,
            planned_action_count=1,
            in_network_inventory_limit=5,
            out_network_inventory_limit=7,
            idle_sleep_milliseconds=0,
            feed_builder=feed_builder,
            user_executor=user_executor,
        )

        self.assertEqual(len(feed_builder.calls), 1)
        self.assertFalse(hasattr(feed_builder.calls[0][0], "lurker"))
        self.assertEqual(len(database.view_records), 1)
        self.assertEqual(database.view_records[0].session_id, "zero_activity_session")
        self.assertEqual(len(user_executor.calls), 1)
        self.assertEqual(user_executor.calls[0][0].activity_level, 0.0)
        self.assertEqual(user_executor.calls[0][0].planned_action_count, 0)
        self.assertEqual(database.written_actions, [])

    def test_worker_loop_reuses_batch_out_inventory_per_session(self):
        """Both inventories are fetched once and filtered per user in Python."""

        class MultiSessionDatabase(FakeWorkerDatabase):
            def __init__(self):
                super().__init__(
                    sessions=[
                        ClaimedUserSessionRecord(
                            user_id=1,
                            session_id="session_1",
                            user_record=UserRecord(
                                user_id=1,
                                user_type="normal",
                                activity_level=1.0,
                                user_behavior={"p_actions": {"post": 1.0}},
                                other_properties={},
                            ),
                            planned_action_count=1,
                        ),
                        ClaimedUserSessionRecord(
                            user_id=2,
                            session_id="session_2",
                            user_record=UserRecord(
                                user_id=2,
                                user_type="normal",
                                activity_level=1.0,
                                user_behavior={"p_actions": {"post": 1.0}},
                                other_properties={},
                            ),
                            planned_action_count=1,
                        ),
                    ]
                )

            def fetch_out_network_feed_inventory(self, *, limit):
                self.out_inventory_calls.append(limit)
                return [
                    ActionRow(
                        action_id=42,
                        creator_user_id=100,
                        timestamp=8,
                        action_type="post",
                    ),
                    ActionRow(
                        action_id=44,
                        creator_user_id=100,
                        timestamp=8,
                        action_type="post",
                    )
                ]

            def fetch_in_network_feed_inventory(self, claimed_sessions, *, limit):
                self.in_inventory_calls.append(
                    ([session.user_id for session in claimed_sessions], limit)
                )
                actions = [
                    ActionRow(
                        action_id=42,
                        creator_user_id=99,
                        timestamp=10,
                        action_type="post",
                    ),
                    ActionRow(
                        action_id=43,
                        creator_user_id=100,
                        timestamp=9,
                        action_type="post",
                    ),
                ]
                return BatchInNetworkInventory(
                    actions=actions,
                    visible_to_user_ids={
                        42: frozenset({1}),
                        43: frozenset({2}),
                    },
                )

        database = MultiSessionDatabase()
        feed_builder = FakeFeedBuilder()

        run_worker_loop(
            database=database,
            batch_size=2,
            lurker_ratio=0.25,
            planned_action_count=1,
            in_network_inventory_limit=5,
            out_network_inventory_limit=7,
            idle_sleep_milliseconds=0,
            feed_builder=feed_builder,
            user_executor=FakeUserExecutor(),
        )

        self.assertEqual(database.out_inventory_calls, [7])
        self.assertEqual(
            database.in_inventory_calls,
            [([1, 2], 5)],
        )
        session_1_candidates = feed_builder.calls[0][1]
        session_2_candidates = feed_builder.calls[1][1]
        self.assertEqual(
            [(candidate.action_id, candidate.network_source) for candidate in session_1_candidates],
            [(42, IN_NETWORK), (44, OUT_NETWORK)],
        )
        self.assertEqual(
            [(candidate.action_id, candidate.network_source) for candidate in session_2_candidates],
            [(43, IN_NETWORK), (42, OUT_NETWORK), (44, OUT_NETWORK)],
        )

    def test_worker_loop_rejects_invalid_claim_output(self):
        """Worker loop should reject invalid DB claim output."""
        database = InvalidClaimDatabase()

        with self.assertRaises(TypeError):
            run_worker_loop(
                database=database,
                batch_size=2,
                lurker_ratio=0.25,
                planned_action_count=1,
                in_network_inventory_limit=5,
                out_network_inventory_limit=7,
                idle_sleep_milliseconds=0,
                feed_builder=FakeFeedBuilder(),
                user_executor=FakeUserExecutor(),
            )

    def test_to_action_view_records_maps_viewed_action_ids(self):
        """Worker should map selected viewed IDs into DB view records."""
        claimed_session = FakeWorkerDatabase().claim_user_batch(
            1,
            lurker_ratio=0.0,
            planned_action_count=1,
        )[0]

        records = to_action_view_records([42, 43], claimed_session)

        self.assertEqual([record.action_id for record in records], [42, 43])
        self.assertEqual([record.viewer_user_id for record in records], [1, 1])

    def test_to_feed_candidates_preserves_self_authored_source_metadata(self):
        """Self-authored materialized candidates should pass through normally."""
        claimed_session = FakeWorkerDatabase().claim_user_batch(
            1,
            lurker_ratio=0.0,
            planned_action_count=1,
        )[0]
        context = FeedInventoryContext(
            in_network_actions=[
                ActionRow(
                    action_id=42,
                    creator_user_id=claimed_session.user_id,
                    timestamp=99,
                    action_type="post",
                    other_properties={
                        "source_action_id": 100,
                        "source_creator_user_id": 5,
                        "source_action_type": "repost",
                        "source_timestamp": 101,
                    },
                )
            ],
        )

        candidates = to_feed_candidates(context, claimed_session)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].creator_user_id, claimed_session.user_id)
        self.assertEqual(candidates[0].source_action_id, 100)
        self.assertEqual(candidates[0].source_creator_user_id, 5)
        self.assertEqual(candidates[0].source_action_type, "repost")
        self.assertEqual(candidates[0].source_timestamp, 101)

    def test_overall_lurker_ratio_allows_a_fully_lurking_user_type(self):
        types = {
            "normal": {"num_nodes": 2, "lurker_ratio": 0},
            "bad": {"num_nodes": 3, "lurker_ratio": 1},
        }
        self.assertAlmostEqual(_effective_lurker_ratio(types), 0.6)
        types["bad"]["num_nodes"] = 0
        self.assertEqual(_effective_lurker_ratio(types), 0)
        types["bad"]["lurker_ratio"] = 1.01
        with self.assertRaisesRegex(ValueError, "user_types.bad.lurker_ratio"):
            _effective_lurker_ratio(types)

    def test_worker_loop_rejects_invalid_lurker_ratio(self):
        """Worker loop should reject invalid lurker ratios before DB claims."""
        database = FakeWorkerDatabase()

        with self.assertRaisesRegex(ValueError, "lurker_ratio"):
            run_worker_loop(
                database=database,
                batch_size=2,
                lurker_ratio=1.0,
                planned_action_count=1,
                in_network_inventory_limit=5,
                out_network_inventory_limit=7,
                idle_sleep_milliseconds=0,
                feed_builder=FakeFeedBuilder(),
                user_executor=FakeUserExecutor(),
            )

        self.assertEqual(database.claim_calls, [])

    def test_worker_loop_rejects_invalid_inventory_limits(self):
        """Worker loop should reject malformed inventory limits before claims."""
        database = FakeWorkerDatabase()

        with self.assertRaisesRegex(ValueError, "in_network_inventory_limit"):
            run_worker_loop(
                database=database,
                batch_size=2,
                lurker_ratio=0.0,
                planned_action_count=1,
                in_network_inventory_limit=-1,
                out_network_inventory_limit=7,
                idle_sleep_milliseconds=0,
                feed_builder=FakeFeedBuilder(),
                user_executor=FakeUserExecutor(),
            )

        self.assertEqual(database.claim_calls, [])

    def test_build_feed_rejects_invalid_feed_output(self):
        """build_feed should reject non-RankedFeed outputs."""
        claimed_session = FakeWorkerDatabase().claim_user_batch(
            1,
            lurker_ratio=0.0,
            planned_action_count=1,
        )[0]
        user = to_user_profile(claimed_session)

        with self.assertRaises(TypeError):
            build_feed(
                user=user,
                candidates=[],
                feed_builder=InvalidFeedBuilder(),
            )


if __name__ == "__main__":
    unittest.main()
