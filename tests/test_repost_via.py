# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Repost exposure provenance follows the selected inventory source."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libs" / "simsom"))
from components.worker import to_feed_candidates
from db.core import ActionRow, UserRecord
from db.worker import FeedInventoryContext, ClaimedUserSessionRecord
from subcomponents.recsys import FeedCandidate, RankedFeed, UserProfile
from subcomponents.user_executor import DefaultUserExecutor, UserAction


class RepostViaTests(unittest.TestCase):
    def emit(self, item, action_type="repost"):
        executor = DefaultUserExecutor({
            "content_embedding_generation_function": {"dummy_content_embedding": {}},
            "feed_weight_components": [{
                "recency_component_function": {"inverse_rank_recency": {}},
                "importance_in": 1.0, "importance_out": 1.0,
            }],
        })
        user = UserProfile(user_id=3, user_type="normal", planned_action_count=1,
                           user_behavior={"p_actions": {
                               kind: float(kind == action_type)
                               for kind in ("post", "like", "repost", "reply", "quote")
                           }})
        result = executor.execute_session(user, RankedFeed(items=[item]))
        self.assertEqual(result.viewed_action_ids, [item.action_id])
        return result.actions[0]

    def test_repost_keeps_content_parent_and_records_exposing_repost(self):
        action = self.emit(FeedCandidate(
            action_id=10, creator_user_id=1, action_type="post",
            source_action_id=20, source_action_type="repost",
            other_properties={"via": 7},
        ))
        self.assertEqual(action.parent_action_id, 10)
        self.assertEqual(action.other_properties, {"via": 20})

    def test_direct_content_and_non_repost_reactions_have_no_via(self):
        direct = FeedCandidate(action_id=10, creator_user_id=1, action_type="post",
                               source_action_id=10, source_action_type="post")
        self.assertNotIn("via", self.emit(direct).other_properties)
        exposed = FeedCandidate(action_id=10, creator_user_id=1, action_type="post",
                                source_action_id=20, source_action_type="repost")
        for kind in ("post", "like", "reply", "quote"):
            with self.subTest(kind=kind):
                self.assertNotIn("via", self.emit(exposed, kind).other_properties)

    def test_quote_exposure_does_not_trace_to_the_quotes_parent(self):
        for source_type, source_id in (("quote", 10), ("repost", 20)):
            with self.subTest(source=source_type):
                action = self.emit(FeedCandidate(
                    action_id=10, creator_user_id=1, action_type="quote", parent_action_id=5,
                    source_action_id=source_id, source_action_type=source_type,
                ))
                self.assertEqual(action.parent_action_id, 10)
                self.assertEqual(action.other_properties,
                                 {"via": 20} if source_type == "repost" else {})

    def test_inventory_deduplication_selects_latest_in_network_exposure(self):
        def row(source):
            return ActionRow(action_id=10, creator_user_id=1, timestamp=source,
                             action_type="post", other_properties={
                                 "source_action_id": source, "source_action_type": "repost",
                             })
        inventory = FeedInventoryContext(in_network_actions=[row(20), row(30)],
                                         out_network_actions=[row(40)])
        candidates = to_feed_candidates(inventory, ClaimedUserSessionRecord(
            user_id=3, session_id="via", user_record=UserRecord(user_id=3, user_type="normal"),
            planned_action_count=1))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(self.emit(candidates[0]).other_properties, {"via": 30})

    def test_stored_source_metadata_uses_same_exposure(self):
        action = self.emit(FeedCandidate(action_id=10, creator_user_id=1, action_type="post",
                                         other_properties={"source_action_type": "repost",
                                                           "source_action_id": 20}))
        self.assertEqual(action.other_properties, {"via": 20})

    def test_invalid_or_non_repost_via_is_rejected(self):
        for via in (None, True, 0, -1, 1.5, "20", 2**63, 10):
            with self.subTest(via=via), self.assertRaisesRegex(ValueError, "via"):
                UserAction.repost(creator_user_id=3, parent_action_id=10,
                                  other_properties={"via": via})
        with self.assertRaisesRegex(ValueError, "only valid for repost"):
            UserAction.like(creator_user_id=3, parent_action_id=10, other_properties={"via": 20})
