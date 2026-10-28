<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Database Algorithm

The database is the shared state holder for the simulation. It stores users,
actions, friend links, lifecycle state, and simulator timestamps.

`docs/algorithms/simulation.md` is the source of truth for the high-level
simulation algorithm. This document describes the shared storage contract that
supports that algorithm.

## What It Does

- Gives every process one shared place to read and write simulation state.
- Keeps the table layout consistent across controller, worker, analytics, and
  policy code.
- Assigns simulator timestamps inside Postgres when workers write actions.
- Stores the lifecycle termination request used by the controller.

## Main Tables

- `user`
  - Stores one simulator user.
  - Holds profile data, activity level, user type, behavior settings,
    moderation actions, and other properties.
- `action`
  - Stores one social action, such as a post, repost, reply, quote, or like.
  - Holds creator, parent action, action type, content embedding, moderation
    actions, counters, `timestamp`, and other properties. Content actions
    generate quality, appeal, and post labels inside `other_properties`;
    non-content actions may inherit that metadata from their parent.
- `follower_network`
  - Stores directed friend links.
  - Means one user has added another user as a friend.
  - Does not assume friendship goes both directions.
- `simulation_lifecycle`
  - Stores the shared stop state.
  - Lets the controller record its termination decision.
  - Lets analytics publish a termination request and the criteria that were met.
  - Stores policy's latest processed action ID.
  - Stores the authoritative request while the Message Passing Interface (MPI)
    carries shutdown messages.
- `termination.criteria_functions` is an ordered mapping of termination
  functions to their explicit inputs. Analytics evaluates every listed function and records the ones
  that are met in the lifecycle row.
- Policy uses `policy_cursor_action_id` as its new-action review boundary;
  there is no policy batch-size limit. Older actions with due survival-time
  labels can be revisited through the same target query; new rows with pending
  future survival-time labels advance the cursor but wait for policy-rule
  evaluation until due.
- `policy.rule_functions` maps required policy-rule selectors to input objects. Each
  selected rule is resolved from the policy-rule registry and evaluated for
  every fetched supported action.
- Post labels describe generated content properties, while moderation actions
  record interventions. The configured post-labeling functions store
  namespaced values in `action.other_properties.post_labels`; interventions
  are stored directly in `action.moderation_actions` as a JSON list.
- Each intervention entry stores `intervention_type`, `violation_type`, and
  `rule`. Shared intervention semantics derives the intervention source from
  decorator registration for the persisted `rule`, so owner-specific consumers
  do not need a separate stored `source` field.
- `analytics_summary`
  - Stores lightweight checks produced at configured simulation-progress
    checkpoints and the final full report.
  - Holds aggregate counts, average activity, average action quality,
    termination criteria, policy statistics, network averages, and
    analyzer-specific properties.
  - The final row stores final analytics-function results at
    `other_properties.analytics_outputs`.
- The last row for a run provides the final average quality.

## Database Provisioning

SimSoM requires PostgreSQL with the `simsom_clock` extension. You can manage
PostgreSQL yourself, locally or on a remote host, or run it in Docker. Docker
is optional; both deployment methods use the same database setup for each run.

- For a manual deployment, build and install the extension files on the
  PostgreSQL server using the Makefile in `libs/simsom/db/clock/` and build
  tools matching that server's PostgreSQL installation.
- For a Docker deployment, the database image must include the extension
  files. They are installed when building the image.
- In either case, configure `database.dsn` and authentication so the simulator
  can connect to the intended database without an interactive password prompt.

Extension-file installation happens before running the simulator. At startup,
the controller creates the extension object inside the configured database and
initializes the simulation schema and clock. It does not compile or install
extension files on the server. See the [README](../../README.md#usage) for setup
instructions.

## Setup Steps

- Create the configured schema if it does not exist.
- Drop and recreate SimSoM's run-owned tables from the current code contract.
- Create only primary keys, uniqueness constraints, checks, and foreign keys
  before loading initial rows.
- Remove any existing `simsom_clock` extension from the database, including its
  extension-owned functions and state table.
- Create a fresh `simsom_clock` extension object in the configured simulator
  schema, using the extension files already installed on the server.
- Set the `action.timestamp` default to the clock extension allocator.
- Do not store a schema version. Setup always creates the only supported current
  schema, so there is no incremental migration history to identify.

## Initial Load Steps

- Sample starting users in bounded NumPy chunks and stream them through
  PostgreSQL `COPY` without retaining all user row objects.
- Generate or import friend links as a one-pass stream and send them through a
  second PostgreSQL `COPY` command without retaining the complete graph.
- Build secondary user, action, analytics, and friend-link indexes after all
  initial rows have been sent.
- Run `ANALYZE` on the freshly populated user and friend-link tables and on the
  initially empty action table before releasing runtime processes. This gives
  PostgreSQL current planner statistics immediately instead of waiting for
  autovacuum during the simulation.
- Reset the lifecycle state to "not stopping."
- Configure and reset the database-owned clock extension.

Row loading, index creation, planner-statistics collection, lifecycle
initialization, and clock configuration use one transaction. Schema recreation
happens immediately before that transaction. Other MPI ranks remain behind the
setup barrier, so they cannot observe partial setup state.

The simulator is designed for runs with up to `1,000,000,000` users. User IDs
and every user foreign key use PostgreSQL `INTEGER`. Setup rejects a population
above `2,147,483,647`, the signed `INTEGER` maximum, before allocating compact
local state. Action IDs, timestamps, parent action IDs,
persisted action counters, and analytics aggregate counts use PostgreSQL
`BIGINT`; these values can grow independently of the number of users.

## Timestamp Rule

- Python code does not assign simulator timestamps.
- Workers insert actions without a timestamp.
- The database fills `action.timestamp` when the action is inserted.
- `timestamp` is a `bigint` logical simulator timestamp in milliseconds
  since the configured clock origin; it is not wall-clock time.
- The clock extension is required and cannot be disabled.
- `action.timestamp` defaults to `<clock_schema>.simsom_clock_nextval()`.
- Setup recreates the extension before use, so stale functions or previous
  clock state do not carry into a new run.
- `num_users = max(number of users, 1)`.
- Controller setup calls `simsom_clock_configure_exponential(...)`.
- The singleton `simsom_clock_state` row stores the current tick, distribution,
  calibrated lambda, and minimum step.
- Each allocation locks that row, samples a positive exponential delta, adds it
  to `current_tick_ms`, and returns the new value.
- Action writers acquire the same clock-row lock **before allocating action
  IDs**, after acquiring their parent-action advisory locks. They retain clock
  ownership across every insert chunk until commit or rollback. Another writer
  cannot reserve an earlier ID and commit it after Policy has advanced past it.
  IDs and timestamps advance under one owner; their increments need not match.
- Rollbacks may leave unused identity IDs. Policy must not interpret ID gaps as
  pending actions. External action writers must follow the same lock protocol.
- The implemented clock-extension delta is:
  - `lambda = 9.586385781407468e-08 * num_users + 3.907543315063294e-05`;
  - `u` is sampled uniformly from `(0, 1)`;
  - `delta = max(1, floor(-ln(u) / lambda))`.
- Clock configuration resets the extension-backed timestamp back to `0`.
- This means every inserted action advances simulator time by at least `1`.
- Calibration steps, fitted parameters and reports are in
  [the exponential-clock analysis folder](../../analyses/calibration/clock/exponential/README.md).
  The one-millisecond minimum is an implementation bound, not a calibrated value.

## Ownership Rules

- Shared schema changes belong in `libs/simsom/db/core/`.
- Controller-only database behavior belongs in `libs/simsom/db/controller.py`.
- Worker-only database behavior belongs in `libs/simsom/db/worker.py`.
- Analytics-only database behavior belongs in `libs/simsom/db/analytics.py`.
- Policy-only database behavior belongs in `libs/simsom/db/policy.py`.
- Process-specific database modules should not redefine shared table shapes.
- Writes that touch existing action rows coordinate with transaction-scoped
  advisory locks by action ID. This applies to view-counter updates, child
  action inserts that reference `parent_action_id`, and policy moderation
  updates.

## Implemented Now

- Shared table names, columns, relationships, indexes, lifecycle SQL, and setup
  SQL.
- Shared row objects for users, actions, and friend links.
- Controller database setup and initial-state loading.
- Worker session selection, feed reads, action writes, and duplicate
  like/repost delete interventions.
- Policy target reads, cursor writes for review progress, and moderation-action
  writes for interventions.
- Analytics lightweight termination-metric reads and final report writes,
  including `avg_action_quality`, software
  wall-clock runtime, simulated elapsed time, inter-activity timing distribution,
  policy coverage, and directed friend-network averages.
- Shared lifecycle reads, analytics termination requests, and controller-owned
  termination requests.
- MPI shutdown coordination uses `STOP_REQUEST`, process-completion
  acknowledgments, Analytics final-analysis authorization, and final-report
  completion acknowledgment. The controller only dumps the database after the
  final acknowledgment.
- Database-owned timestamp assignment for inserted actions.
- PostgreSQL clock extension source and Makefile build/install targets under
  `libs/simsom/db/clock/`.

## Not Built Yet

- Additional analytics export formats beyond the configured JSONL files.
- Incremental schema migration; each simulation starts from fresh run tables.
- A worker lease table, if worker sessions need stricter ownership.

Database exports omit source ownership and privilege statements. The role that
restores an export owns the restored objects unless ownership is assigned
separately.

## Table Details

- `docs/algorithms/database_schema.md` lists the current tables and columns.
- `docs/algorithms/analytics.md` describes recurring termination checks and the
  final summary write.
- Update that file whenever a table or column changes.
