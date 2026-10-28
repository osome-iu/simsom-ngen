<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# SimSoM Architecture

This document describes the current simulator package in `libs/simsom/` with
architecture version v3. The implemented database schema is documented in
`docs/algorithms/database_schema.md`; diagrams in `docs/logical_architecture/`
are visual companions.

The intent is:

- Postgres owns shared state and simulator timestamps.
- Message Passing Interface (MPI) owns process lifecycle only.
- `components/` orchestrate process loops.
- `db/` defines process-specific persistence seams.
- `subcomponents/` define pure behavior contracts owned by feature teams.

The package is organized around process ownership so multiple PR owners can
build in parallel without all editing the same files.

## Package Shape

```text
docs/
  architecture/
    ARCHITECTURE.md
    simsom_2025-08-05.jpg
    architecture.png

libs/simsom/
  main.py
  run_simulation.py
  mpi_layout.py
  mpi_protocol.py
  config_helper.py
  logger_helper.py
  config/
    default_simulator_config.jsonc
  components/
    runtime.py
    controller.py
    worker.py
    analytics.py
    policy.py
  db/
    core/
      __init__.py
      settings.py
      connection.py
      lifecycle.py
      schema.py
      data_transfer_objects.py
    controller.py
    worker.py
    analytics.py
    policy.py
    clock/
      README.md
  subcomponents/
    setup_db.py
    recsys.py
    user_executor.py
    analytics_report.py
    policy_rules.py
```

## Active Modules

- `main.py`: MPI entry point and rank dispatch only.
- `run_simulation.py`: local runner for launching the MPI entry point.
- `mpi_layout.py`: code-owned process roles and rank placement validation.
- `mpi_protocol.py`: code-owned MPI control messages.
- `config_helper.py`: JSONC config loader with dot-notation access.
- `logger_helper.py`: process-safe logging setup.
- `components/controller.py`: controller process orchestration.
- `components/worker.py`: worker process orchestration.
- `components/analytics.py`: analytics process orchestration.
- `components/policy.py`: policy process orchestration.
- `components/runtime.py`: shared process context object.
- `db/core/__init__.py`: shared DB core import seam.
- `db/core/settings.py`: shared DB settings from config.
- `db/core/connection.py`: shared DB connection/client base.
- `db/core/lifecycle.py`: shared termination-state boundary.
- `db/core/schema.py`: shared Postgres schema contract, index contract, and
  idempotent bootstrap SQL.
- `db/core/data_transfer_objects.py`: shared row shapes for `user`, `action`,
  and `follower_network`.
- `db/controller.py`: controller-only DB contract for setup, initial-state
  population, dump, and lifecycle coordination.
- `db/worker.py`: worker-only DB contract for claiming user sessions,
  reading feed candidates, writing aggregate action views, and writing actions.
- `db/policy.py`: policy-only DB contract for review targets and moderation
  action writes.
- `db/analytics.py`: analytics-only DB contract for termination metrics, final
  report writes, lifecycle-state reads, and termination-signal writes.
- `db/clock/README.md`: PostgreSQL clock extension design and integration
  notes.
- `subcomponents/setup_db.py`: DB-setup dataset planning contract, including
  setup-time action probability handling.
- `subcomponents/recsys.py`: RecSys feed-building contract, including
  `UserProfile` and `FeedCandidate` boundary checks.
- `subcomponents/user_executor.py`: user execution contract, including
  `UserSessionResult`, viewed-action IDs, `UserAction` constructors, and
  action payload checks.
- `subcomponents/analytics_report.py`: analytics report-building contract.
- `subcomponents/policy_rules.py`: policy decision contract.

Shared infra note:

- `main.py`, `run_simulation.py`, `mpi_layout.py`, `mpi_protocol.py`,
  `config_helper.py`, and `logger_helper.py`
  are shared runtime files. Keep PRs out of them unless a boundary or startup
  contract actually changes.

## Suggested Pull Request Ownership

This is based on the high-level interaction logic of our latest V3 architecture design. The purpose is to prevent overlapping code changes across PRs. As team members work on their respective parts, new ideas may emerge, leading to changes in ownership or further decomposition into smaller components as needed.

- Each component will have a single owner.
- Each person may own one or more components and is responsible for both development and PR decisions.


1. Database/schema owner
   Own `db/core/`, plus shared schema work.
   This owner defines the shared Postgres contract and reviews changes that
   affect more than one process. Shared table names, columns, foreign keys,
   indexes, lifecycle SQL, and bootstrap SQL belong in `db/core/schema.py`,
   not process-specific DB modules.

2. Controller owner
   Own `components/controller.py`, `db/controller.py`, and
   `subcomponents/setup_db.py`.
   This includes schema setup, initial-state planning, initial-state
   population,
   dump/export, and lifecycle coordination. The controller invokes the shared
   schema contract from `db/core/schema.py` and owns populating concrete
   `SetupDbDataset` rows into that contract.

3. Worker owner
   Own `components/worker.py` and `db/worker.py`.
   This includes user-batch claiming, feed-candidate reads,
   DB-to-behavior mapping, aggregate view writes, and action writes. Worker
   queries must build on the shared `user`, `action`, `follower_network`, and
   lifecycle contracts from `db/core/` instead of redefining table shape
   locally.

4. RecSys owner
   Own `subcomponents/recsys.py`.
   This includes feed-candidate interpretation, ranking, deduping, and feed
   assembly independent of MPI and Structured Query Language (SQL).

5. User owner
   Own `subcomponents/user_executor.py`.
   This includes simulated view/action decision logic and the contract for
   viewed action IDs plus generated actions.

6. Analytics owner
   Own `components/analytics.py`, `db/analytics.py`, and
   `subcomponents/analytics_report.py`.
   This includes termination checks, final report generation, summary logging,
   and stop-signal publishing.

7. Policy owner
   Own `components/policy.py`, `db/policy.py`, and
   `subcomponents/policy_rules.py`.
   This includes review-target handling, moderation/policy logic, and
   moderation-action writes.

8. DB-clock owner
   Own `db/clock/` and the PostgreSQL clock extension, control files, SQL
   install scripts, and `timestamp` defaults.
   Python process code must not assign simulator timestamps.

9. Domain/activity owner
   Own cross-cutting simulation semantics that do not belong to a single
   process. This owner defines canonical action types, activity-level meaning,
   and cross-owner behavior assumptions across setup_db, RecSys, worker, and
   user execution. Executable checks live on the component boundary objects
   that developers already use, such as `UserAction`, `UserProfile`,
   `FeedCandidate`, `UserSessionResult`, `ActionViewRecord`, and
   `ActionWriteRecord`.

## Coordination Rules

- `components/worker.py` may import `db/worker.py`, shared DB DTOs, and worker
  subcomponents. It should not import `db/policy.py` or `db/analytics.py`.
- Shared DB imports should prefer `from db.core import ...` rather than pulling
  from multiple separate files.
- Shared table DTOs stay aligned with the DB diagram. Worker execution payloads
  should use `user_id` and `session_id` rather than introducing alias ids.
- The worker layer may process multiple sessions for the same `user_id`.
  Do not add per-user locking in orchestration code; write/aggregation paths
  are responsible for consolidating generated state.
- `components/policy.py` may import `db/policy.py` and `subcomponents/policy_rules.py`.
- `components/analytics.py` may import `db/analytics.py` and
  `subcomponents/analytics_report.py`.
- `components/controller.py` may import `db/controller.py` and
  `subcomponents/setup_db.py`.
- `subcomponents/` should stay free of MPI concerns.
- Cross-owner behavior semantics should be enforced at component boundaries
  instead of retyping action or activity constants locally.
- `db/core/data_transfer_objects.py` should only hold shared table shapes, not
  process-specific query payloads.
- Process-specific write/read payloads belong in their owning DB module.
- If a PR needs edits across multiple process DB modules, the boundary is
  probably wrong.

## Shared Postgres Schema Contract

`docs/algorithms/database_schema.md` is the maintained plain-English reference
for the implemented shared table names, column names, column types, indexes,
and reset behavior. `libs/simsom/db/core/schema.py` is the code implementation
of that schema contract, including shared indexes, lifecycle SQL, and
idempotent bootstrap SQL. Process-specific DB modules can compose queries
against these contracts, but they should not redefine shared table layouts.

The ERD in `docs/logical_architecture/Entity_Relationship_Diagram.drawio.svg`
is the visual companion to the implemented schema reference.

The plain-English database algorithm is documented in
`docs/algorithms/database.md`.

The cross-component simulation algorithm is documented in
`docs/algorithms/simulation.md`.

The shared domain/activity semantics are documented in
`docs/algorithms/domain_activity.md`.

### Tables

- `user`
  - Primary key: `user_id`.
  - Columns: `vector_profile`, `activity_level`, `user_type`,
    `user_behavior`, `moderation_actions`, `other_properties`.
  - `moderation_actions` carries moderation interventions when present.
  - Used by setup/controller for initial users and by workers for user
    profiles.
- `action`
  - Primary key: `action_id`.
  - Foreign keys: `creator_user_id -> user.user_id`,
    `parent_action_id -> action.action_id`.
  - Columns: `timestamp`, `action_type`, `content_embedding`,
    `moderation_actions`, `repeated_like_n_reshare_creator_count`,
    `views_by_others_count`, `other_properties`.
  - `moderation_actions` carries moderation interventions when present.
  - Simulator timestamps are assigned by the database-owned `timestamp`
    default when workers insert actions without a timestamp.
- `follower_network`
  - Primary key: `(user_id, friend_id)`.
  - Foreign keys: `user_id -> user.user_id`,
    `friend_id -> user.user_id`.
  - Represents a directed friend/follow edge: `user_id` has added
    `friend_id`.
- `simulation_lifecycle`
  - Singleton row with `lifecycle_id = 1`.
  - Shared termination state: `should_terminate`, `reason`, `requested_at`,
    analytics stop request and the list of criteria that were met, `updated_at`.
- `analytics_summary`
  - One lightweight status row for each recurring check and one full report
    after termination.
  - Stores counts, average activity, average action quality, termination
    criteria, and analyzer-specific properties on the final report.
### Shared Indexes

- `user_activity_level_idx` on `user(activity_level)`.
- `action_creator_idx` on `action(creator_user_id)`.
- `action_parent_idx` on `action(parent_action_id)`.
- `action_timestamp_idx` on `action(timestamp)`.
- `action_type_idx` on `action(action_type)`.
- `action_post_label_survival_time_idx` on numeric
  `action.other_properties.post_labels.survival_time` values.
- `analytics_summary_recorded_at_idx` on `analytics_summary(recorded_at)`.
- `follower_network_friend_idx` on `follower_network(friend_id)`.

### Setup Approach

The setup path always starts fresh. The controller creates the configured
schema when needed, drops and recreates SimSoM's run-owned tables from the
current contract, and reinstalls the clock-extension timestamp default. It does
not keep schema version rows or perform incremental migrations.

Initial-state population is a separate transaction. It streams users and friend
links through PostgreSQL `COPY`, builds secondary indexes only after both
streams finish, initializes lifecycle state, and configures the DB-owned clock.
The controller's setup barrier prevents other ranks from reading partial state.

Schema-shape changes should update
`docs/algorithms/database_schema.md`, `db/core/schema.py`, and any affected
process-specific DB query modules together.

## Intended Runtime Flow

### Controller

1. Setup schema and DB-owned clock hooks.
2. Plan the initial users and follower network.
3. Populate initial state in Postgres.
4. Monitor the analytics termination request.
5. Record the authoritative termination state.
6. Send stop requests and coordinate process acknowledgments.
7. Dump DB state if configured.

### Worker

1. Claim a batch of user sessions.
2. Fetch feed inventories once for the claimed worker cycle.
3. Build a ranked feed through RecSys.
4. Execute user behavior.
5. Write actions to Postgres.

The implemented worker algorithm is documented in `docs/algorithms/worker.md`.

### Policy

1. Fetch eligible supported actions from Postgres after the policy cursor, plus
   due delayed-survival targets.
2. Evaluate configured policy rules.
3. Write matching moderation interventions back to Postgres.
4. Advance the policy cursor after the pass.

The implemented policy algorithm is documented in `docs/algorithms/policy.md`.

### Analytics

1. Poll aggregate DB state.
2. Evaluate all configured termination criteria.
3. Publish a termination request for the controller to consume.
4. After the controller coordinates shutdown and authorizes final analysis,
   write the final analytics summary.
5. Acknowledge completion after the final summary is persisted.
