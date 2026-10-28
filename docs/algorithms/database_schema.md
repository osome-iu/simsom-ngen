<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Implemented Database Schema

This document records the implemented Postgres schema used by the simulator.
Update it with every schema-shape change. For high-level simulation behavior,
use `docs/algorithms/simulation.md`; this file describes storage details.

The code-owned schema contract lives in `libs/simsom/db/core/schema.py`.

## Shared Schema

- All simulator tables are created inside the configured Postgres schema.
- The simulator is designed for up to `1,000,000,000` users. User identifiers
  use `INTEGER`, with a technical PostgreSQL maximum of `2,147,483,647`.
- The controller creates the schema during setup.
- Process-specific DB clients build queries against the shared table contract;
  they should not redefine shared table shapes.

## Tables

### `user`

Stores one simulator user.

| Column | Type | Notes |
| --- | --- | --- |
| `user_id` | `INTEGER` | Primary key. Must be between `1` and `2,147,483,647`, the PostgreSQL signed `INTEGER` maximum. |
| `vector_profile` | `JSONB` | Defaults to `[]`. |
| `activity_level` | `FLOAT(2)` | Defaults to `0`. |
| `user_type` | `TEXT` | Required profile/behavior category. |
| `user_behavior` | `JSONB` | Defaults to `{}`. Stores sampled behavior such as `p_actions` and user `quality`. |
| `moderation_actions` | `JSONB` | Optional moderation intervention list for user rows. The populated row determines whether the intervention is user-scoped or action-scoped. |
| `other_properties` | `JSONB` | Defaults to `{}`. Extra metadata outside core semantics. |

### `action`

Stores one social action.

| Column | Type | Notes |
| --- | --- | --- |
| `action_id` | `BIGINT` | Identity primary key. |
| `creator_user_id` | `INTEGER` | Required FK to `user.user_id`, cascade delete. |
| `timestamp` | `BIGINT` | Required simulator timestamp assigned by Postgres. |
| `action_type` | `TEXT` | Required canonical action type. |
| `parent_action_id` | `BIGINT` | Optional FK to `action.action_id`, set null on delete. |
| `content_embedding` | `JSONB` | Defaults to `[]`. |
| `moderation_actions` | `JSONB` | Optional moderation intervention list for action rows, stored as `[{"intervention_type": "...", "violation_type": "...", "rule": "..."}]`. Policy progress is tracked by `simulation_lifecycle.policy_cursor_action_id`; the `rule` value maps each intervention to a source such as `policy` or `user`. |
| `repeated_like_n_reshare_creator_count` | `BIGINT` | Defaults to `0`; must be non-negative. Worker-written repeated same-action-type count for the same creator and parent action. On repeated likes/reposts, the new row receives the prior maximum count plus one. |
| `views_by_others_count` | `BIGINT` | Defaults to `0`; counts views where the viewer is not the action creator; must be non-negative. |
| `other_properties` | `JSONB` | Defaults to `{}`. Stores session trace data, post-level `quality`/`appeal`, and optional `post_labels`. `like` and `repost` rows inherit descriptive and policy metadata from their parent action. `post_labels` is a namespaced map such as `{"legality": "illegal", "survival_time": 123456}`. |

#### Repost `via` metadata

`action.other_properties.via` is an optional positive BIGINT-valued JSON number
on reposts, identifying the source repost that exposed their parent content.
The default executor sets it from the selected feed candidate, and the writer
excludes it from parent-metadata inheritance. It is omitted for direct exposure
and non-repost actions. This is JSON metadata, not a physical foreign-key column.
See [provenance semantics](domain_activity.md#repost-exposure-provenance-via).

### `follower_network`

Stores directed friend links.

| Column | Type | Notes |
| --- | --- | --- |
| `user_id` | `INTEGER` | FK to `user.user_id`, cascade delete. |
| `friend_id` | `INTEGER` | FK to `user.user_id`, cascade delete. |

Primary key: (`user_id`, `friend_id`).

### `simulation_lifecycle`

Stores the singleton lifecycle state shared by controller, workers, policy, and
analytics.

| Column | Type | Notes |
| --- | --- | --- |
| `lifecycle_id` | `SMALLINT` | Primary key. Always `1`. |
| `started_at` | `TIMESTAMPTZ` | Controller-captured wall-clock start time for the current run. |
| `should_terminate` | `BOOLEAN` | Required. Defaults to `FALSE`. |
| `reason` | `TEXT` | Controller stop reason. |
| `termination_source` | `TEXT` | Source that recorded the authoritative termination, currently `controller`. |
| `requested_at` | `TIMESTAMPTZ` | First accepted stop request time. |
| `termination_criteria` | `JSONB` | Criteria observed and accepted by the controller with the stop reason. Frozen until the next run; defaults to `[]`. |
| `analytics_stop_requested` | `BOOLEAN` | Required. Defaults to `FALSE`. Analytics-owned stop signal. |
| `analytics_stop_reason` | `TEXT` | Optional analytics stop reason. |
| `analytics_stop_updated_at` | `TIMESTAMPTZ` | Last analytics stop-signal update time. |
| `analytics_termination_criteria` | `JSONB` | Criteria met in Analytics' latest termination request. Defaults to `[]`. |
| `policy_cursor_action_id` | `BIGINT` | Latest action ID processed by policy. Nullable before the first policy pass. |
| `policy_cursor_updated_at` | `TIMESTAMPTZ` | Last policy cursor update time. |
| `updated_at` | `TIMESTAMPTZ` | Required lifecycle update time. |

### `analytics_summary`

Stores lightweight analytics checks produced at configured simulation-progress
checkpoints and the final analytics report.

| Column | Type | Notes |
| --- | --- | --- |
| `analytics_summary_id` | `BIGINT` | Identity primary key. |
| `recorded_at` | `TIMESTAMPTZ` | Required. Defaults to `NOW()`. |
| `user_count` | `BIGINT` | Required; must be non-negative. |
| `action_count` | `BIGINT` | Required; must be non-negative. |
| `avg_activity_level` | `FLOAT` | Nullable; must be non-negative when present. |
| `avg_action_quality` | `FLOAT` | Nullable; must be in `[0, 1]` when present. |
| `termination_criteria` | `JSONB` | Criteria met for this report. Defaults to `[]`. |
| `other_properties` | `JSONB` | Defaults to `{}`. Analyzer-specific summary properties, including policy, network, runtime, timing metrics, and final analytics outputs. |

The final average quality for a run is the `avg_action_quality` value in the
last `analytics_summary` row for that run. Recurring rows leave
`avg_activity_level` null and keep full report properties empty.

Final analytics-function results are stored in the final row at
`other_properties.analytics_outputs`, keyed by configured function selector.
Recurring function results are not copied into recurring database rows; they are
stored in the analytics JSONL output file.

Recurring rows and termination-criterion evaluations share
`analytics.evaluation_interval`. The interval can use average actions per user
or simulator-clock hours. The final row is written independently after shutdown.

Runtime values are stored in `analytics_summary.other_properties.runtime`:

- `software_runtime_seconds`: wall-clock seconds since
  `simulation_lifecycle.started_at`.
- `simulated_runtime_seconds`: simulated elapsed seconds, computed from
  simulator millisecond timestamps.

Timing distribution values are stored in
`analytics_summary.other_properties.timing.inter_activity_ms`:

- `min`;
- `mean`;
- `p50`;
- `p90`;
- `p95`;
- `p99`;
- `max`.

Action type ratios are stored in
`analytics_summary.other_properties.action_type_ratios` as an object map keyed
by action type. Ratio values are strings formatted to three decimal places,
such as `"0.000"`.

Repost share values are stored in
`analytics_summary.other_properties.repost_stats`. Ratio values are strings
formatted to three decimal places:

- `repost_action_ratio`: reposts divided by total persisted actions;
- `repeated_repost_ratio`: second-and-later repost actions for the same
  creator and same `parent_action_id`, divided by reposts;
- `self_repost_ratio`: self reposts divided by reposts;
- `self_repeated_repost_ratio`: reposts that are both self reposts and
  second-and-later repost actions for the same creator and same
  `parent_action_id`, divided by reposts.

Policy intervention values are stored in
`analytics_summary.other_properties.policy_stats`:

- `policy_coverage`: policy-cursor-covered supported actions divided by all
  supported actions in the run, formatted to three decimal places.
  An action is counted as covered only when its `action_id` is not
  greater than `simulation_lifecycle.policy_cursor_action_id`.
- `policy_intervention_ratio`: cursor-covered supported actions with a policy
  intervention divided by cursor-covered supported actions in the run, formatted
  to three decimal places.
  Policy intervention counts use decorator-registered source metadata; only
  interventions whose `rule` maps to `policy` are counted.

Network summary values are stored in
`analytics_summary.other_properties.network_stats`:

- `avg_friends_per_user`: average outgoing friend links per user;
- `avg_followers_per_user`: average incoming friend links per user.

Policy cursor progress is separate from intervention metadata. Cursor-covered
actions without an intervention have no `moderation_actions` entries.

Post labels are generated before Policy reads an action. They are separate from
`moderation_actions`: a label describes the content, while a policy intervention
records what Policy decided to do. The optional per-user-type
`post_labeling_functions` mapping can add multiple namespaced labels to content
actions. Non-content child actions inherit all parent `post_labels`, including
survival timing. If post labeling is omitted or empty and no parent supplies
labels, the action has no `post_labels` property.

Intervention entries do not persist a separate `source` field. Shared
intervention semantics derives source from the persisted `rule` value through
decorator registration, so consumers can distinguish policy and user
interventions without duplicating ownership metadata on every action row.

`policy.target_batch_size` is a positive integer. It defaults to `500`. Each
pass selects at most this many actions in total. New actions and due older
actions share the limit, with alternating priority so both make progress. All
moderation writes from one pass use one transaction and update at most this
many action IDs. This setting is not a database column. A new row with a future
`survival_time` still advances the cursor, but its rules wait until that time.
Policy advances the cursor even when no intervention is written.
Delayed survival-time lookup uses the indexed
`other_properties.post_labels.survival_time` value; that label is the
evaluation limit for the action, not the intervention outcome itself.
There is no separate cache table for pending delayed interventions.
The required `policy.rule_functions` mapping selects the rules used for each
eligible action; every selected rule is evaluated.
Policy appends interventions with one bulk update per pass, then saves the
cursor. See [Policy](policy.md#intervention-storage) for retry behavior.

The lifecycle row remains the authoritative termination decision. Analytics
stores the proposed reason and met criteria first; the controller records the
authoritative `should_terminate`, `reason`, `termination_source`, `requested_at`,
and `termination_criteria` values atomically. Only the first request is accepted.
The criteria come from the same lifecycle snapshot as the accepted reason, not
from a later Analytics signal. Later Analytics updates cannot overwrite this
accepted decision. New-run initialization resets these fields together.
Message Passing Interface (MPI) shutdown messages coordinate process
completion, but do not create a
second termination state. The final analytics row is written only after
workers and policy have completed and the controller authorizes final analysis.

## Indexes

- `user_activity_level_idx` on `user(activity_level)`.
- `action_creator_idx` on `action(creator_user_id)`.
- `action_parent_idx` on `action(parent_action_id)`.
- `action_timestamp_idx` on `action(timestamp)`.
- `action_type_idx` on `action(action_type)`.
- `action_post_label_survival_time_idx` on numeric
  `action.other_properties.post_labels.survival_time` values.
- `analytics_summary_recorded_at_idx` on `analytics_summary(recorded_at)`.
- `follower_network_friend_idx` on `follower_network(friend_id)`.

## Fresh-Run Behavior

Setup drops and recreates the five run-owned tables from the current code
contract. It does not preserve old rows or store schema version numbers. The
controller then streams users and friend links, builds secondary indexes,
creates the singleton lifecycle row, and configures the database clock. The
load, index, lifecycle, and clock steps commit together.

Secondary indexes are deliberately absent during the two `COPY` streams. This
avoids maintaining each index once per inserted row. Primary keys, foreign
keys, checks, and follower-edge uniqueness remain enforced during loading.

## Timestamp Source

- Workers do not assign `action.timestamp`.
- `action.timestamp` uses `<clock_schema>.simsom_clock_nextval()`.
- Setup drops and recreates the `simsom_clock` extension before wiring this
  default, so stale extension functions and clock state are removed.
