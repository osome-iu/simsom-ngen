# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
Worker process

Workers claim user sessions from Postgres, build feeds from Postgres actions,
execute user behavior, and write resulting views/actions back to Postgres. The
database owns simulator timestamps, so workers do not assign clock values in
Python.
"""

import math
import time
from typing import Any, TYPE_CHECKING

from db.core import ActionRow
from domain.action_semantics import action_source_id
from domain.session_semantics import fixed_actions_per_active_session
from db.worker import (
    ActionViewRecord,
    ActionWriteRecord,
    BatchInNetworkInventory,
    ClaimedUserSessionRecord,
    FeedInventoryContext,
    WorkerDatabase,
    create_worker_database_client,
)
from logger_helper import get_logger
from mpi_protocol import MpiMessage, receive_control_message, send_control_message
from mpi_layout import Role
from subcomponents.user_executor import (
    UserAction,
    UserSessionResult,
    UserExecutor,
    DefaultUserExecutor,
    execute_user_session,
)
from subcomponents.recsys import (
    UserProfile,
    DefaultFeedBuilder,
    FeedBuilder,
    FeedCandidate,
    IN_NETWORK,
    OUT_NETWORK,
    RankedFeed,
)

if TYPE_CHECKING:
    from components.runtime import ProcessContext


def run_worker(context: "ProcessContext") -> None:
    """Create worker dependencies and process user sessions until stopping.

    See docs/algorithms/worker.md#main-steps.
    """
    logger = get_logger(__name__ + ".Worker")
    database = create_worker_database_client(
        context.config.database,
        context.config.worker,
        context.config.setup_db.user_types,
        context.config.recsys,
    )
    batch_size = context.config.worker.batch_size
    lurker_ratio = _effective_lurker_ratio(context.config.setup_db.user_types)
    idle_sleep_milliseconds = context.config.worker.idle_sleep_milliseconds
    inventory_composition = context.config.recsys.inventory_composition
    in_network_inventory_limit, out_network_inventory_limit = inventory_limits_for_batch(
        multiplier=_config_value(inventory_composition, "size_multiplier"),
        out_of_network_ratio=_config_value(
            inventory_composition,
            "out_of_network_ratio",
        ),
        feed_size=context.config.recsys.feed_size,
        batch_size=batch_size,
    )
    planned_action_count = fixed_actions_per_active_session(
        context.config.recsys.feed_size,
        context.config.user_behavior.actions_per_session_ratio,
    )
    feed_builder = DefaultFeedBuilder(context.config.recsys)
    user_executor = DefaultUserExecutor(context.config.user_behavior)

    completed = False
    try:
        logger.info(f"Worker rank {context.rank} starting with batch_size={batch_size}")
        run_worker_loop(
            database=database,
            batch_size=batch_size,
            lurker_ratio=lurker_ratio,
            planned_action_count=planned_action_count,
            in_network_inventory_limit=in_network_inventory_limit,
            out_network_inventory_limit=out_network_inventory_limit,
            idle_sleep_milliseconds=idle_sleep_milliseconds,
            feed_builder=feed_builder,
            user_executor=user_executor,
            comm_world=context.comm_world,
        )
        completed = True
    finally:
        database.close()
        if completed:
            send_control_message(
                context.comm_world,
                context.mpi_layout.rank(Role.CONTROLLER),
                MpiMessage.PROCESS_TERMINATED,
                rank=context.rank,
                role=context.role.value,
            ).wait()


def run_worker_loop(
    *,
    database: WorkerDatabase,
    batch_size: int,
    lurker_ratio: float,
    planned_action_count: int,
    in_network_inventory_limit: int,
    out_network_inventory_limit: int,
    idle_sleep_milliseconds: float,
    feed_builder: FeedBuilder,
    user_executor: UserExecutor,
    comm_world: Any | None = None,
) -> None:
    """Run worker batches until the controller sends a stop request.

    Without an MPI communicator, the lifecycle row remains the stop source.
    Under MPI, the current batch writes complete before the next stop check.

    See docs/algorithms/simulation.md#worker-shutdown-behavior.
    Idle backoff: docs/algorithms/worker.md#idle-polling-and-performance.
    """
    _require_non_negative_int("batch_size", batch_size)
    _require_lurker_ratio("lurker_ratio", lurker_ratio)
    _require_positive_int("planned_action_count", planned_action_count)
    _require_non_negative_int(
        "in_network_inventory_limit",
        in_network_inventory_limit,
    )
    _require_non_negative_int(
        "out_network_inventory_limit",
        out_network_inventory_limit,
    )
    _require_non_negative_number("idle_sleep_milliseconds", idle_sleep_milliseconds)

    while comm_world is not None or not database.should_terminate():
        if _stop_requested(comm_world):
            return
        claimed_sessions = database.claim_user_batch(
            batch_size,
            lurker_ratio=lurker_ratio,
            planned_action_count=planned_action_count,
        )
        _require_list(
            "claimed_sessions",
            claimed_sessions,
            ClaimedUserSessionRecord,
        )
        if not claimed_sessions:
            # Back off empty claims to test query contention; zero is the
            # no-delay baseline. This does not pace productive batches.
            if idle_sleep_milliseconds > 0:
                time.sleep(idle_sleep_milliseconds / 1000.0)
            continue

        out_network_inventory = database.fetch_out_network_feed_inventory(
            limit=out_network_inventory_limit,
        )
        _require_list("out_network_inventory", out_network_inventory, ActionRow)
        in_network_inventory = database.fetch_in_network_feed_inventory(
            claimed_sessions,
            limit=in_network_inventory_limit,
        )
        _require_type(
            "in_network_inventory",
            in_network_inventory,
            BatchInNetworkInventory,
        )

        action_view_records: list[ActionViewRecord] = []
        action_records: list[ActionWriteRecord] = []
        for claimed_session in claimed_sessions:
            user = to_user_profile(claimed_session)
            user_in_network_inventory = in_network_inventory.for_user(
                claimed_session.user_id
            )
            inventory_context = FeedInventoryContext(
                in_network_actions=user_in_network_inventory,
                out_network_actions=out_network_inventory,
            )
            feed = build_feed(
                user=user,
                candidates=to_feed_candidates(inventory_context, claimed_session),
                feed_builder=feed_builder,
            )
            session_result = execute_user(
                user=user,
                feed=feed,
                user_executor=user_executor,
            )
            action_view_records.extend(
                to_action_view_records(
                    session_result.viewed_action_ids,
                    claimed_session,
                )
            )
            action_records.extend(
                to_action_records(session_result.actions, claimed_session)
            )

        if comm_world is None and database.should_terminate():
            return
        if action_view_records:
            database.write_action_views(action_view_records)
        if comm_world is None and database.should_terminate():
            return
        if action_records:
            database.write_actions(action_records)


def _stop_requested(comm_world: Any | None) -> bool:
    """Return whether the controller has sent a stop request."""
    if comm_world is None:
        return False
    message = receive_control_message(comm_world)
    return message is not None and message[0] == MpiMessage.STOP_REQUEST


def build_feed(
    user: UserProfile,
    candidates: list[FeedCandidate],
    feed_builder: FeedBuilder,
) -> RankedFeed:
    """Build a typed ranked feed from typed feed candidates."""
    _require_type("user", user, UserProfile)
    _require_list("candidates", candidates, FeedCandidate)
    feed = feed_builder.build_feed(user, candidates)
    _require_type("ranked feed", feed, RankedFeed)
    return feed


def execute_user(
    user: UserProfile,
    feed: RankedFeed,
    user_executor: UserExecutor,
) -> UserSessionResult:
    """Execute user behavior and return its typed session result."""
    _require_type("user", user, UserProfile)
    _require_type("feed", feed, RankedFeed)
    return execute_user_session(user, feed, user_executor)


def to_user_profile(claimed_session: ClaimedUserSessionRecord) -> UserProfile:
    """Convert one claimed session into a typed user profile."""
    _require_type(
        "claimed_session",
        claimed_session,
        ClaimedUserSessionRecord,
    )
    user = claimed_session.user_record
    return UserProfile(
        user_id=claimed_session.user_id,
        session_id=claimed_session.session_id,
        activity_level=user.activity_level,
        planned_action_count=claimed_session.planned_action_count,
        user_type=user.user_type,
        vector_profile=user.vector_profile,
        user_behavior=user.user_behavior,
        other_properties=user.other_properties,
    )


def to_feed_candidates(
    inventory_context: FeedInventoryContext,
    claimed_session: ClaimedUserSessionRecord,
) -> list[FeedCandidate]:
    """Convert this session's inventories into feed candidates.

    See docs/algorithms/worker.md#inventory-rules.
    """
    _require_type("inventory_context", inventory_context, FeedInventoryContext)
    _require_type(
        "claimed_session",
        claimed_session,
        ClaimedUserSessionRecord,
    )
    in_network_action_ids = {
        action.action_id for action in inventory_context.in_network_actions
    }
    candidates = []
    for network_source, inventory in (
        (
            IN_NETWORK,
            inventory_context.in_network_actions,
        ),
        (
            OUT_NETWORK,
            inventory_context.out_network_actions,
        ),
    ):
        filtered_inventory = (
            inventory
            if network_source == IN_NETWORK
            else [
                action
                for action in inventory
                if action.action_id not in in_network_action_ids
            ]
        )
        # Viewer eligibility and in-network precedence are settled before
        # choosing the latest surviving source event for each parent content ID.
        representatives = {}
        for action in filtered_inventory:
            previous = representatives.get(action.action_id)
            if previous is None or action_source_id(action) > action_source_id(previous):
                representatives[action.action_id] = action
        candidates.extend(
            FeedCandidate(
                action_id=action.action_id,
                creator_user_id=action.creator_user_id,
                action_type=action.action_type,
                parent_action_id=action.parent_action_id,
                content_embedding=action.content_embedding,
                moderation_actions=action.moderation_actions,
                repeated_like_n_reshare_creator_count=(
                    action.repeated_like_n_reshare_creator_count
                ),
                views_by_others_count=action.views_by_others_count,
                other_properties=action.other_properties,
                timestamp=action.timestamp,
                network_source=network_source,
                source_action_id=_optional_int_from_properties(
                    action.other_properties,
                    "source_action_id",
                ),
                source_creator_user_id=_optional_int_from_properties(
                    action.other_properties,
                    "source_creator_user_id",
                ),
                source_action_type=action.other_properties.get("source_action_type"),
                source_timestamp=_optional_int_from_properties(
                    action.other_properties,
                    "source_timestamp",
                ),
            )
            for action in representatives.values()
        )
    _require_list("feed candidates", candidates, FeedCandidate)
    return candidates


def to_action_records(
    actions: list[UserAction],
    claimed_session: ClaimedUserSessionRecord,
) -> list[ActionWriteRecord]:
    """Convert generated user actions into typed DB write records."""
    _require_list("actions", actions, UserAction)
    _require_type(
        "claimed_session",
        claimed_session,
        ClaimedUserSessionRecord,
    )
    records = [
        ActionWriteRecord(
            session_id=claimed_session.session_id,
            action=action,
            user_record=claimed_session.user_record,
        )
        for action in actions
    ]
    _require_list("action records", records, ActionWriteRecord)
    return records


def to_action_view_records(
    viewed_action_ids: list[int],
    claimed_session: ClaimedUserSessionRecord,
) -> list[ActionViewRecord]:
    """Convert user-selected viewed action IDs into DB view records."""
    _require_list("viewed_action_ids", viewed_action_ids, int)
    _require_type(
        "claimed_session",
        claimed_session,
        ClaimedUserSessionRecord,
    )

    records = []
    for action_id in viewed_action_ids:
        records.append(
            ActionViewRecord(
                session_id=claimed_session.session_id,
                viewer_user_id=claimed_session.user_id,
                action_id=action_id,
            )
        )
    _require_list("action view records", records, ActionViewRecord)
    return records


def _require_type(name: str, value: Any, expected_type: type) -> None:
    if not isinstance(value, expected_type):
        raise TypeError(
            f"{name} must be {expected_type.__name__}, got {type(value).__name__}"
        )


def _require_non_negative_int(name: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")


def _require_probability(name: str, value: Any) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float")
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must satisfy 0 <= value <= 1")


def _require_optional_non_negative_int(name: str, value: Any) -> None:
    if value is None:
        return
    _require_non_negative_int(name, value)


def inventory_limit_for_batch(
    *,
    multiplier: int,
    feed_size: int,
    batch_size: int,
) -> int:
    """Calculate the total inventory limit for a worker batch.

    See docs/algorithms/worker.md#inventory-rules.
    """
    _require_positive_int("recsys.inventory_composition.size_multiplier", multiplier)
    _require_non_negative_int("recsys.feed_size", feed_size)
    _require_non_negative_int("worker.batch_size", batch_size)
    return multiplier * feed_size * batch_size


def inventory_limits_for_batch(
    *,
    multiplier: int,
    out_of_network_ratio: float,
    feed_size: int,
    batch_size: int,
) -> tuple[int, int]:
    """Allocate the total inventory between networks by configured ratio.

    The multiplier sets the total pool. The out-of-network allocation is
    rounded to the nearest action and the in-network allocation receives the
    remainder, so both limits always sum to the total.

    See docs/algorithms/worker.md#inventory-rules.
    """
    total = inventory_limit_for_batch(
        multiplier=multiplier,
        feed_size=feed_size,
        batch_size=batch_size,
    )
    _require_probability(
        "recsys.inventory_composition.out_of_network_ratio",
        out_of_network_ratio,
    )
    out_network = round(total * out_of_network_ratio)
    return total - out_network, out_network


def _require_non_negative_number(name: str, value: Any) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be int or float, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")


def _require_probability(name: str, value: Any) -> None:
    _require_non_negative_number(name, value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value > 1:
        raise ValueError(f"{name} must be less than or equal to 1")


def _require_lurker_ratio(
    name: str, value: Any, *, allow_all_lurkers: bool = False,
) -> None:
    """Allow 1 per type, but keep the overall batch ratio below 1.

    See docs/algorithms/simulation.md#user-batch-algorithm.
    """
    _require_non_negative_number(name, value)
    if value > 1.0 or (value == 1.0 and not allow_all_lurkers):
        boundary = "<=" if allow_all_lurkers else "<"
        raise ValueError(f"{name} must satisfy 0 <= value {boundary} 1")


def _effective_lurker_ratio(user_types: Any) -> float:
    """Return the initialized-user-weighted lurker ratio from user type config.

    See docs/algorithms/simulation.md#user-batch-algorithm.
    """
    type_items = vars(user_types).items() if hasattr(user_types, "__dict__") else user_types.items()
    total_nodes = 0
    expected_lurkers = 0.0
    for user_type, raw_type_config in type_items:
        type_config = vars(raw_type_config) if hasattr(raw_type_config, "__dict__") else raw_type_config
        num_nodes = _config_value(type_config, "num_nodes")
        _require_non_negative_int(f"setup_db.user_types.{user_type}.num_nodes", num_nodes)
        lurker_ratio = _config_value(type_config, "lurker_ratio")
        _require_lurker_ratio(
            f"setup_db.user_types.{user_type}.lurker_ratio", lurker_ratio,
            allow_all_lurkers=True,
        )
        total_nodes += num_nodes
        expected_lurkers += num_nodes * float(lurker_ratio)
    if total_nodes <= 0:
        return 0.0
    return expected_lurkers / total_nodes


def _require_positive_int(name: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be greater than 0")


def _config_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key not in value:
            raise ValueError(f"Missing required config field: {key}")
        return value[key]
    if not hasattr(value, key):
        raise ValueError(f"Missing required config field: {key}")
    return getattr(value, key)


def _optional_int_from_properties(properties: dict[str, Any], key: str) -> int | None:
    value = properties.get(key)
    if value is None:
        return None
    return int(value)


def _require_list(name: str, value: Any, item_type: type) -> None:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be list, got {type(value).__name__}")
    for index, item in enumerate(value):
        if not isinstance(item, item_type):
            raise TypeError(
                f"{name}[{index}] must be {item_type.__name__}, "
                f"got {type(item).__name__}"
            )
