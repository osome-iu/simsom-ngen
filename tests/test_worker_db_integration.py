# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""Integration tests for worker DB behavior against Postgres."""

import os
import selectors
import shutil
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SIMSOM_ROOT = Path(__file__).resolve().parents[1] / "libs" / "simsom"
sys.path.insert(0, str(SIMSOM_ROOT))

from db.controller import PostgresControllerDatabase, SetupDbDataset  # noqa: E402
from db.core import DatabaseSettings, FollowerNetworkRecord, UserRecord  # noqa: E402
from db.policy import PostgresPolicyDatabase  # noqa: E402
from db.worker import (
    ActionViewRecord,
    ActionWriteRecord,
    ClaimedUserSessionRecord,
    PostgresWorkerDatabase,
    gaussian_around_user_quality,
    post_distribution_function,
)  # noqa: E402
from domain.post_label_semantics import (  # noqa: E402
    ILLEGAL_LABEL,
    INTERNAL_SURVIVAL_DURATION_MS_LABEL,
    LEGALITY_LABEL,
    SURVIVAL_TIME_LABEL,
)
from subcomponents.policy_annotations import (  # noqa: E402
    REMOVED_INTERVENTION,
    intervention_entry,
    user_duplicate_reaction_cleanup_intervention,
)
from subcomponents.user_executor import UserAction  # noqa: E402


TEST_POST_DISTRIBUTION_SELECTOR = "test_integration_post_distribution"


class _TransactionSession:
    """Bounded interactive psql session for controlled transaction interleavings."""

    def __init__(self, psql_bin, database):
        self.process = subprocess.Popen(
            [psql_bin, "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-d", database],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.pending = b""
        self.execute("SET statement_timeout = '15s';")
        self.pid = int(self.execute("SELECT pg_backend_pid();").strip())

    def execute(self, sql):
        marker = ("done_" + uuid.uuid4().hex).encode()
        self.process.stdin.write(sql.encode() + b"\nSELECT '" + marker + b"';\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + 20
        while marker + b"\n" not in self.pending:
            if not self.selector.select(max(0, deadline - time.monotonic())):
                raise TimeoutError(self.pending.decode())
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError(self.pending.decode())
            self.pending += chunk
        answer, self.pending = self.pending.split(marker + b"\n", 1)
        return answer.decode()

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.selector.close()


@post_distribution_function(TEST_POST_DISTRIBUTION_SELECTOR)
def test_integration_post_distribution(behavior, spec):
    """Generate a valid test property without depending on a shipped label."""
    return gaussian_around_user_quality(behavior, spec, stddev=0.12)


class WorkerPostgresIntegrationTests(unittest.TestCase):
    """Exercise worker writes with real Postgres rows."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.psql_bin = cls._resolve_binary(os.environ.get("SIMSOM_PSQL_BIN"), "psql")
        cls.createdb_bin = cls._resolve_binary(
            os.environ.get("SIMSOM_CREATEDB_BIN"),
            "createdb",
        )
        cls.dropdb_bin = cls._resolve_binary(
            os.environ.get("SIMSOM_DROPDB_BIN"),
            "dropdb",
        )
        cls.maintenance_db = os.environ.get("SIMSOM_MAINTENANCE_DB", "postgres")
        cls._check_server()

        cls.test_db = f"simsom_worker_test_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        subprocess.run([cls.createdb_bin, cls.test_db], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "test_db"):
            subprocess.run([cls.dropdb_bin, "--if-exists", cls.test_db], check=False)

    def setUp(self) -> None:
        self.schema = f"worker_test_{uuid.uuid4().hex[:8]}"
        self.settings = DatabaseSettings(
            dsn=self.test_db,
            schema=self.schema,
            clock_extension_name="simsom_clock",
        )
        self.controller = PostgresControllerDatabase(self.settings)
        self.controller.setup_schema()
        self.controller.populate_initial_state(
            SetupDbDataset(
                users=[
                    UserRecord(user_id=1, user_type="normal", activity_level=1.0),
                    UserRecord(user_id=2, user_type="normal", activity_level=1.0),
                ],
                follower_network=[
                    FollowerNetworkRecord(user_id=1, friend_id=2),
                    FollowerNetworkRecord(user_id=2, friend_id=1),
                ],
            )
        )
        self.controller.run_psql(f"""
            INSERT INTO {self.controller.schema_name("action")} (
                creator_user_id,
                timestamp,
                action_type
            )
            VALUES
                (1, 10, 'post'),
                (2, 20, 'post');
            """)

    def worker_database(self, **kwargs) -> PostgresWorkerDatabase:
        """Create a worker DB client with required recsys config."""
        kwargs.setdefault("action_insert_chunk_size", 1000)
        kwargs.setdefault(
            "user_types_config",
            {
                "normal": {
                    "num_nodes": 1,
                    "post_quality_distribution_function": {TEST_POST_DISTRIBUTION_SELECTOR: {}},
                    "post_appeal_distribution_function": {TEST_POST_DISTRIBUTION_SELECTOR: {}},
                }
            },
        )
        kwargs.setdefault(
            "recsys_config",
            SimpleNamespace(engagement_action_lookback_population_multiplier=10),
        )
        return PostgresWorkerDatabase(self.settings, **kwargs)

    def post_records(self, session_id, creator_user_id=1):
        return [ActionWriteRecord(
            session_id=session_id,
            action=UserAction.post(creator_user_id=creator_user_id, content_embedding=[0.3]),
            user_record=UserRecord(user_id=creator_user_id, user_type="normal",
                                   user_behavior={"quality": 0.7}),
        ) for _ in range(2)]

    def test_clock_ownership_precedes_identity_allocation_across_chunks(self):
        """A waiting production writer cannot reserve IDs before the owner commits."""
        owner = self.worker_database(action_insert_chunk_size=1)
        waiter = self.worker_database(action_insert_chunk_size=1)
        policy = PostgresPolicyDatabase(self.settings)
        policy.record_policy_cursor(policy.fetch_moderation_targets().next_cursor)
        table = self.controller.schema_name("action")
        sequence = self.controller.schema_name("action_action_id_seq")
        errors = []
        session = _TransactionSession(self.psql_bin, self.test_db)
        thread = None
        try:
            # Execute the real generated write, retaining only its final COMMIT.
            def hold_commit(sql):
                self.assertTrue(sql.rstrip().endswith("COMMIT;"))
                session.execute(sql.rstrip()[:-len("COMMIT;")])

            with patch.object(owner, "run_psql", side_effect=hold_commit):
                owner.write_actions(self.post_records("clock_owner"))
            last_owned_id = int(self.controller.execute_sql(f"SELECT last_value FROM {sequence};"))

            def write_waiter():
                try:
                    waiter.write_actions(self.post_records("clock_waiter"))
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=write_waiter)
            thread.start()
            deadline = time.monotonic() + 10
            while True:
                blocked = self.controller.fetch_json(f"""
                    SELECT to_json(EXISTS (
                        SELECT 1 FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND {session.pid} = ANY(pg_blocking_pids(pid))
                    ));
                """)
                if blocked:
                    break
                if time.monotonic() >= deadline:
                    self.fail("second writer did not wait for the clock owner")
                time.sleep(0.02)

            self.assertEqual(int(self.controller.execute_sql(
                f"SELECT last_value FROM {sequence};")), last_owned_id)
            invisible = policy.fetch_moderation_targets(policy.fetch_policy_cursor())
            self.assertEqual(list(invisible), [])
            self.assertIsNone(invisible.next_cursor)
            self.assertEqual(policy.fetch_policy_cursor().action_id, 2)
            session.execute("COMMIT;")
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            rows = self.controller.fetch_json(f"""
                SELECT json_agg(json_build_object('id', action_id, 'time', timestamp,
                    'session', other_properties->>'session_id') ORDER BY action_id)
                FROM {table} WHERE action_id > 2;
            """)
            self.assertEqual([r["session"] for r in rows],
                             ["clock_owner"] * 2 + ["clock_waiter"] * 2)
            self.assertEqual([r["time"] for r in rows], sorted(r["time"] for r in rows))
            visible = policy.fetch_moderation_targets(policy.fetch_policy_cursor())
            self.assertEqual([r.cursor_action_id for r in visible], [r["id"] for r in rows])
        finally:
            session.close()
            if thread is not None:
                thread.join(timeout=15)

    def test_failed_later_chunk_rolls_back_actions_and_clock(self):
        """A failed chunk releases the clock and rolls back the entire writer transaction."""
        worker = self.worker_database(action_insert_chunk_size=1)
        table = self.controller.schema_name("action")
        clock = self.controller.schema_name("simsom_clock_state")
        before = self.controller.fetch_json(f"SELECT row_to_json(c) FROM {clock} c;")
        records = self.post_records("rolled_back")[:1] + self.post_records("invalid", 999)[:1]
        with self.assertRaisesRegex(RuntimeError, "foreign key constraint"):
            worker.write_actions(records)
        self.assertEqual(self.controller.fetch_json(f"SELECT row_to_json(c) FROM {clock} c;"), before)
        self.assertEqual(int(self.controller.execute_sql(f"SELECT count(*) FROM {table};")), 2)
        worker.write_actions(self.post_records("after_rollback"))
        self.assertEqual(int(self.controller.execute_sql(f"SELECT count(*) FROM {table};")), 4)

    def test_concurrent_view_writes_update_rows_without_deadlock(self):
        """Concurrent workers should update shared view counters successfully."""
        worker_a = self.worker_database()
        worker_b = self.worker_database()
        start = threading.Barrier(3)
        errors: list[BaseException] = []

        def write_views(worker, records):
            start.wait()
            try:
                for _ in range(10):
                    worker.write_action_views(records)
            except BaseException as exc:
                errors.append(exc)

        thread_a = threading.Thread(
            target=write_views,
            args=(
                worker_a,
                [
                    ActionViewRecord("session_a", 1, 1),
                    ActionViewRecord("session_a", 1, 2),
                ],
            ),
        )
        thread_b = threading.Thread(
            target=write_views,
            args=(
                worker_b,
                [
                    ActionViewRecord("session_b", 2, 2),
                    ActionViewRecord("session_b", 2, 1),
                ],
            ),
        )

        thread_a.start()
        thread_b.start()
        start.wait()
        thread_a.join(timeout=30)
        thread_b.join(timeout=30)

        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())
        if errors:
            self.fail(f"concurrent view writes failed: {errors!r}")

        rows = self.controller.fetch_json(f"""
            SELECT json_agg(
                json_build_object(
                    'action_id', action_id,
                    'views_by_others_count', views_by_others_count
                )
                ORDER BY action_id
            )
            FROM {self.controller.schema_name("action")};
            """)

        self.assertEqual(
            rows,
            [
                {"action_id": 1, "views_by_others_count": 10},
                {"action_id": 2, "views_by_others_count": 10},
            ],
        )

    def test_view_writes_skip_views_by_action_creator(self):
        """Creator self-views should not increment views_by_others_count."""
        worker = self.worker_database()

        worker.write_action_views(
            [
                ActionViewRecord("session_self_1", 1, 1),
                ActionViewRecord("session_other_1", 2, 1),
                ActionViewRecord("session_other_2", 1, 2),
                ActionViewRecord("session_self_2", 2, 2),
            ]
        )

        rows = self.controller.fetch_json(f"""
            SELECT json_agg(
                json_build_object(
                    'action_id', action_id,
                    'views_by_others_count', views_by_others_count
                )
                ORDER BY action_id
            )
            FROM {self.controller.schema_name("action")};
            """)

        self.assertEqual(
            rows,
            [
                {"action_id": 1, "views_by_others_count": 1},
                {"action_id": 2, "views_by_others_count": 1},
            ],
        )

    def test_in_network_batch_union_preserves_per_user_visibility(self):
        """One shared in-network query should retain each viewer's visibility."""
        worker = self.worker_database()
        sessions = [
            ClaimedUserSessionRecord(
                user_id=1,
                session_id="session_1",
                user_record=UserRecord(user_id=1, user_type="normal"),
            ),
            ClaimedUserSessionRecord(
                user_id=2,
                session_id="session_2",
                user_record=UserRecord(user_id=2, user_type="normal"),
            ),
        ]

        inventory = worker.fetch_in_network_feed_inventory(sessions, limit=10)

        self.assertEqual(
            [action.action_id for action in inventory.actions],
            [2, 1],
        )
        self.assertEqual(
            [action.action_id for action in inventory.for_user(1)],
            [2],
        )
        self.assertEqual(
            [action.action_id for action in inventory.for_user(2)],
            [1],
        )

    def test_repost_sources_keep_distinct_viewer_sets(self):
        self.controller.run_psql(f"""
            INSERT INTO {self.controller.schema_name("action")}
                (creator_user_id, timestamp, action_type, parent_action_id)
            VALUES (2, 30, 'repost', 1), (1, 40, 'repost', 1), (2, 50, 'repost', 1);
        """)
        inventory = self.worker_database()._fetch_in_network_inventory_actions([1, 2], 10)
        self.assertEqual([a.other_properties["source_action_id"] for a in inventory.for_user(1)], [5, 3, 2])
        self.assertEqual([a.other_properties["source_action_id"] for a in inventory.for_user(2)], [4, 1])
        self.assertEqual(inventory.visible_to_user_ids[5], frozenset({1}))
        self.assertEqual(inventory.visible_to_user_ids[4], frozenset({2}))

    def test_inventory_limits_precede_source_and_parent_moderation(self):
        table = self.controller.schema_name("action")
        self.controller.run_psql(f"""
            INSERT INTO {table} (creator_user_id, timestamp, action_type, moderation_actions)
            VALUES (2, 30, 'post', '[{{"intervention_type":"removed","rule":"test","violation_type":"test"}}]');
        """)
        worker = self.worker_database()
        self.assertEqual(worker.fetch_out_network_feed_inventory(limit=1), [])
        self.assertEqual(worker._fetch_in_network_inventory_actions([1, 2], 1).actions, [])
        self.controller.run_psql(f"""
            UPDATE {table} SET moderation_actions = (SELECT moderation_actions FROM {table} WHERE action_id=3) WHERE action_id=1;
            UPDATE {table} SET action_type='repost', parent_action_id=1, moderation_actions=NULL WHERE action_id=3;
        """)
        self.assertEqual(worker.fetch_out_network_feed_inventory(limit=1), [])
        self.assertEqual(worker._fetch_in_network_inventory_actions([1, 2], 1).actions, [])
        # Likes do not consume slots, and multiple viewers of one source event
        # consume just one slot in the shared union.
        self.controller.run_psql(f"""
            UPDATE {table} SET moderation_actions=NULL WHERE action_id=1;
            INSERT INTO {table} (creator_user_id, timestamp, action_type, parent_action_id)
            VALUES (2, 40, 'like', 1);
            INSERT INTO {self.controller.schema_name("follower_network")} VALUES (2, 2);
        """)
        inventory = worker._fetch_in_network_inventory_actions([1, 1, 2], 1)
        self.assertEqual(len(inventory.actions), 1)
        self.assertEqual(inventory.visible_to_user_ids, {3: frozenset({1, 2})})
        self.assertEqual(worker.fetch_out_network_feed_inventory(limit=1)[0].other_properties["source_action_id"], 3)

    def test_engagement_window_does_not_backfill_posts_or_excluded_reactions(self):
        self.controller.run_psql(f"""
            INSERT INTO {self.controller.schema_name("action")}
                (creator_user_id, timestamp, action_type, parent_action_id, moderation_actions)
            VALUES (2, 30, 'like', 1, NULL), (2, 40, 'post', NULL, NULL),
                (2, 50, 'like', 1, '[{{"intervention_type":"removed","rule":"test","violation_type":"test"}}]');
        """)
        worker = self.worker_database(recsys_config=SimpleNamespace(
            engagement_action_lookback_population_multiplier=1))
        inventory = worker.fetch_out_network_feed_inventory(limit=10)
        parent = next(a for a in inventory if a.action_id == 1)
        # Two users => latest two total actions; older like 3 is outside the window.
        self.assertEqual(parent.other_properties["engagement_like"], 1)

    def test_repeated_repost_deletes_previous_matching_repost(self):
        """Repeated repost writes should annotate the older matching repost."""
        action_table = self.controller.schema_name("action")
        self.controller.run_psql(f"""
            INSERT INTO {action_table} (
                creator_user_id,
                timestamp,
                action_type,
                parent_action_id,
                moderation_actions,
                repeated_like_n_reshare_creator_count
            )
            VALUES (
                1,
                30,
                'repost',
                1,
                '[{{"intervention_type": "reviewed", "violation_type": "test_fixture", "rule": "test_fixture"}}]'::jsonb,
                4
            );
            """)

        worker = self.worker_database()
        worker.write_actions(
            [
                ActionWriteRecord(
                    session_id="repeat_repost",
                    action=UserAction.repost(
                        creator_user_id=1,
                        parent_action_id=1,
                    ),
                )
            ]
        )

        rows = self.controller.fetch_json(f"""
            SELECT json_agg(
                json_build_object(
                    'action_type', action_type,
                    'moderation_actions', moderation_actions,
                    'repeated_like_n_reshare_creator_count',
                        repeated_like_n_reshare_creator_count
                )
                ORDER BY action_id
            )
            FROM {action_table}
            WHERE action_type = 'repost';
            """)

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[0]["moderation_actions"],
            [
                {
                    "intervention_type": "reviewed",
                    "violation_type": "test_fixture",
                    "rule": "test_fixture",
                },
                user_duplicate_reaction_cleanup_intervention(),
            ],
        )
        self.assertEqual(rows[0]["repeated_like_n_reshare_creator_count"], 4)
        self.assertIsNone(rows[1]["moderation_actions"])
        self.assertEqual(rows[1]["repeated_like_n_reshare_creator_count"], 5)

    def test_repost_via_is_persisted_and_never_inherited(self):
        """A child's exposure survives the metadata join without copying parent via."""
        table = self.controller.schema_name("action")
        # Existing fixture posts deliberately carry stale metadata: inheritance
        # must not propagate it to either likes or directly exposed reposts.
        self.controller.run_psql(f"""
            UPDATE {table} SET other_properties =
                '{{"quality": 0.4, "appeal": 0.6, "via": 999}}'::jsonb;
        """)
        worker = self.worker_database()
        worker.write_actions([ActionWriteRecord(
            session_id="source_repost",
            action=UserAction.repost(creator_user_id=1, parent_action_id=1),
        )])
        source = int(self.controller.execute_sql(f"""
            SELECT action_id FROM {table}
            WHERE other_properties->>'session_id' = 'source_repost';
        """))
        worker.write_actions([
            ActionWriteRecord(session_id="via_repost", action=UserAction.repost(
                creator_user_id=2, parent_action_id=1, other_properties={"via": source})),
            ActionWriteRecord(session_id="direct_repost", action=UserAction.repost(
                creator_user_id=2, parent_action_id=2)),
            ActionWriteRecord(session_id="direct_like", action=UserAction.like(
                creator_user_id=2, parent_action_id=1)),
        ])
        rows = self.controller.fetch_json(f"""
            SELECT json_agg(json_build_object('session', other_properties->>'session_id',
                'properties', other_properties, 'parent', parent_action_id))
            FROM {table} WHERE action_type IN ('like', 'repost');
        """)
        rows = {row['session']: row for row in rows}
        self.assertEqual(rows['via_repost']['properties']['via'], source)
        self.assertEqual(rows['via_repost']['parent'], 1)
        for name in ('source_repost', 'direct_repost', 'direct_like'):
            self.assertNotIn('via', rows[name]['properties'])
        for row in rows.values():
            self.assertEqual(row['properties']['quality'], 0.4)
            self.assertEqual(row['properties']['appeal'], 0.6)

    def test_parented_non_content_actions_inherit_parent_metadata(self):
        """Likes and reposts should copy parent policy metadata at write time."""
        action_table = self.controller.schema_name("action")
        removed_intervention = intervention_entry(
            intervention_type=REMOVED_INTERVENTION,
            violation_type="below_quality_threshold",
            rule="below_quality_threshold",
        )
        child_intervention = intervention_entry(
            intervention_type="reviewed",
            violation_type="manual_review",
            rule="test_child_review",
        )
        parent_action_id = int(
            self.controller.execute_sql(f"""
                WITH inserted AS (
                    INSERT INTO {action_table} (
                        creator_user_id,
                        timestamp,
                        action_type,
                        content_embedding,
                        moderation_actions,
                        other_properties
                    )
                    VALUES (
                        1,
                        30,
                        'post',
                        '[0.3]'::jsonb,
                        '[{{
                            "intervention_type": "{removed_intervention["intervention_type"]}",
                            "violation_type": "{removed_intervention["violation_type"]}",
                            "rule": "{removed_intervention["rule"]}"
                        }}]'::jsonb,
                        '{{
                            "quality": 0.2,
                            "appeal": 0.4,
                            "custom_rank": "parent",
                            "session_id": "parent_session",
                            "source_action_id": 999,
                            "materialized_timestamp": 30,
                            "engagement_like": 7,
                            "post_labels": {{
                                "{LEGALITY_LABEL}": "{ILLEGAL_LABEL}",
                                "{SURVIVAL_TIME_LABEL}": 123
                            }}
                        }}'::jsonb
                    )
                    RETURNING action_id
                )
                SELECT action_id FROM inserted;
                """)
        )

        worker = self.worker_database()
        worker.write_actions(
            [
                ActionWriteRecord(
                    session_id="inherit_like",
                    action=UserAction.like(
                        creator_user_id=2,
                        parent_action_id=parent_action_id,
                        other_properties={
                            "child_marker": "like",
                            "source_debug": "child_like",
                        },
                    ),
                ),
                ActionWriteRecord(
                    session_id="inherit_repost",
                    action=UserAction.repost(
                        creator_user_id=2,
                        parent_action_id=parent_action_id,
                        moderation_actions=[child_intervention],
                        other_properties={
                            "child_marker": "repost",
                            "source_debug": "child_repost",
                        },
                    ),
                ),
            ]
        )

        rows = self.controller.fetch_json(f"""
            SELECT json_agg(
                json_build_object(
                    'action_type', action_type,
                    'moderation_actions', moderation_actions,
                    'other_properties', other_properties
                )
                ORDER BY action_type
            )
            FROM {action_table}
            WHERE other_properties->>'session_id' IN (
                'inherit_like',
                'inherit_repost'
            );
            """)

        self.assertEqual([row["action_type"] for row in rows], ["like", "repost"])
        for row in rows:
            properties = row["other_properties"]
            self.assertEqual(properties["quality"], 0.2)
            self.assertEqual(properties["appeal"], 0.4)
            self.assertEqual(properties["custom_rank"], "parent")
            self.assertEqual(
                properties["post_labels"],
                {
                    LEGALITY_LABEL: ILLEGAL_LABEL,
                    SURVIVAL_TIME_LABEL: 123,
                },
            )
            self.assertNotIn("source_action_id", properties)
            self.assertNotIn("materialized_timestamp", properties)
            self.assertNotIn("engagement_like", properties)

        like_row, repost_row = rows
        self.assertEqual(like_row["other_properties"]["session_id"], "inherit_like")
        self.assertEqual(
            like_row["other_properties"]["source_debug"],
            "child_like",
        )
        self.assertEqual(
            like_row["moderation_actions"],
            [removed_intervention],
        )
        self.assertEqual(
            repost_row["other_properties"]["session_id"],
            "inherit_repost",
        )
        self.assertEqual(
            repost_row["other_properties"]["source_debug"],
            "child_repost",
        )
        self.assertEqual(
            repost_row["moderation_actions"],
            [removed_intervention, child_intervention],
        )

    def test_out_network_inventory_excludes_feed_excluded_source_and_repost(self):
        """Feed-excluded content should stay out of inventory even through reposts."""
        action_table = self.controller.schema_name("action")
        removed_intervention = intervention_entry(
            intervention_type=REMOVED_INTERVENTION,
            violation_type="below_quality_threshold",
            rule="below_quality_threshold",
        )
        self.controller.run_psql(f"""
            UPDATE {action_table}
            SET moderation_actions = '[{{
                "intervention_type": "{removed_intervention["intervention_type"]}",
                "violation_type": "{removed_intervention["violation_type"]}",
                "rule": "{removed_intervention["rule"]}"
            }}]'::jsonb
            WHERE action_id = 1;

            INSERT INTO {action_table} (
                creator_user_id,
                timestamp,
                action_type,
                parent_action_id
            )
            VALUES (2, 30, 'repost', 1);
            """)

        worker = self.worker_database()
        inventory = worker.fetch_out_network_feed_inventory(
            limit=10,
        )

        self.assertEqual([action.action_id for action in inventory], [2])

    def test_out_network_inventory_adds_global_engagement_components(self):
        """Inventory rows should carry recent global engagement component counts."""
        action_table = self.controller.schema_name("action")
        self.controller.run_psql(f"""
            INSERT INTO {action_table} (
                creator_user_id,
                timestamp,
                action_type,
                parent_action_id,
                content_embedding,
                moderation_actions
            )
            VALUES
                (2, 30, 'like', 1, '[]'::jsonb, NULL),
                (2, 40, 'repost', 1, '[]'::jsonb, NULL),
                (2, 50, 'reply', 1, '[0.1]'::jsonb, NULL),
                (2, 60, 'quote', 1, '[0.2]'::jsonb, NULL),
                (2, 70, 'like', 1, '[]'::jsonb, '[{{"intervention_type": "removed", "violation_type": "test_fixture", "rule": "test_fixture"}}]'::jsonb);
            """)

        worker = self.worker_database()
        inventory = worker.fetch_out_network_feed_inventory(
            limit=10,
        )
        source_rows = [action for action in inventory if action.action_id == 1]

        self.assertTrue(source_rows)
        self.assertEqual(source_rows[0].other_properties["engagement_like"], 2)
        self.assertEqual(source_rows[0].other_properties["engagement_repost"], 2)
        self.assertEqual(source_rows[0].other_properties["engagement_reply"], 2)
        self.assertEqual(source_rows[0].other_properties["engagement_quote"], 2)

    def test_write_actions_materializes_survival_time_label(self):
        """Worker insert should turn internal survival duration into due timestamp."""
        worker = self.worker_database(
            user_types_config={
                "normal": {
                    "num_nodes": 1,
                    "post_quality_distribution_function": {TEST_POST_DISTRIBUTION_SELECTOR: {}},
                    "post_appeal_distribution_function": {TEST_POST_DISTRIBUTION_SELECTOR: {}},
                    "post_labeling_functions": {
                        "illegal_probability_n_survival_hours": {
                            "probability": 0.01, "expected_survival_hours": 144,
                        }
                    },
                }
            }
        )

        with patch(
            "subcomponents.post_labeling.random.random",
            return_value=0.0,
        ), patch(
            "subcomponents.post_labeling.random.expovariate",
            return_value=7.0,
        ):
            worker.write_actions(
                [
                    ActionWriteRecord(
                        session_id="survival_materialization",
                        action=UserAction.post(
                            creator_user_id=1,
                            content_embedding=[0.3],
                        ),
                        user_record=UserRecord(
                            user_id=1,
                            user_type="normal",
                            user_behavior={"quality": 0.7},
                        ),
                    )
                ]
            )

        row = self.controller.fetch_json(f"""
            SELECT json_build_object(
                'timestamp', timestamp,
                'other_properties', other_properties
            )
            FROM {self.controller.schema_name("action")}
            WHERE other_properties->>'session_id' = 'survival_materialization';
            """)

        labels = row["other_properties"]["post_labels"]
        self.assertEqual(labels[LEGALITY_LABEL], ILLEGAL_LABEL)
        self.assertEqual(labels[SURVIVAL_TIME_LABEL], row["timestamp"] + 7)
        self.assertNotIn(INTERNAL_SURVIVAL_DURATION_MS_LABEL, labels)

    def test_engagement_components_use_configured_recent_action_lookback(self):
        """Engagement counts should only use the configured recent action window."""
        action_table = self.controller.schema_name("action")
        self.controller.run_psql(f"""
            INSERT INTO {action_table} (
                creator_user_id,
                timestamp,
                action_type,
                parent_action_id
            )
            VALUES
                (2, 30, 'like', 1),
                (2, 40, 'like', 1),
                (2, 50, 'like', 1);
            """)

        worker = self.worker_database(
            recsys_config=SimpleNamespace(
                engagement_action_lookback_population_multiplier=1,
            ),
        )
        inventory = worker.fetch_out_network_feed_inventory(
            limit=10,
        )
        source_rows = [action for action in inventory if action.action_id == 1]

        self.assertTrue(source_rows)
        self.assertEqual(source_rows[0].other_properties["engagement_like"], 3)

    def test_concurrent_view_writes_and_parented_action_inserts_do_not_deadlock(self):
        """Views and parented action inserts should coordinate on parent action IDs."""
        viewer = self.worker_database()
        writer_a = self.worker_database()
        writer_b = self.worker_database()
        start = threading.Barrier(4)
        errors: list[BaseException] = []

        def write_views():
            start.wait()
            try:
                for _ in range(20):
                    viewer.write_action_views(
                        [
                            ActionViewRecord("viewer", 1, 1),
                            ActionViewRecord("viewer", 1, 2),
                        ]
                    )
            except BaseException as exc:
                errors.append(exc)

        def write_actions(worker, session_prefix, parent_ids):
            start.wait()
            try:
                for index in range(10):
                    worker.write_actions(
                        [
                            ActionWriteRecord(
                                session_id=f"{session_prefix}_{index}_a",
                                action=UserAction.repost(
                                    creator_user_id=1,
                                    parent_action_id=parent_ids[0],
                                ),
                            ),
                            ActionWriteRecord(
                                session_id=f"{session_prefix}_{index}_b",
                                action=UserAction.like(
                                    creator_user_id=2,
                                    parent_action_id=parent_ids[1],
                                ),
                            ),
                        ]
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=write_views),
            threading.Thread(target=write_actions, args=(writer_a, "writer_a", [1, 2])),
            threading.Thread(target=write_actions, args=(writer_b, "writer_b", [2, 1])),
        ]

        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=30)

        for thread in threads:
            self.assertFalse(thread.is_alive())
        if errors:
            self.fail(f"concurrent worker writes failed: {errors!r}")

        row = self.controller.fetch_json(f"""
            SELECT json_build_object(
                'action_count', COUNT(*),
                'total_views', SUM(views_by_others_count)
            )
            FROM {self.controller.schema_name("action")};
            """)

        self.assertEqual(row["action_count"], 42)
        self.assertEqual(row["total_views"], 20)

    @classmethod
    def _check_server(cls) -> None:
        try:
            completed = subprocess.run(
                [cls.psql_bin, "-d", cls.maintenance_db, "-tAc", "SELECT 1"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            raise unittest.SkipTest(
                f"local PostgreSQL server is not available: {exc}"
            ) from exc

        if completed.stdout.strip() != "1":
            raise unittest.SkipTest(
                "local PostgreSQL server did not return a healthy probe result"
            )

    @staticmethod
    def _resolve_binary(*candidates: str) -> str:
        for candidate in candidates:
            if not candidate:
                continue
            if os.path.sep in candidate:
                if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                    return candidate
                continue
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        raise unittest.SkipTest(
            f"required Postgres client not found in candidates: {candidates}"
        )


if __name__ == "__main__":
    unittest.main()
