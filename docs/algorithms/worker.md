<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Worker Algorithm

Workers repeatedly choose user sessions, build feeds for them, execute user
behavior, and write views/actions back to the database.

`docs/algorithms/simulation.md` is the source of truth for the high-level
simulation algorithm. This document describes worker-owned responsibilities and
implementation-facing details.

## What It Does

- Checks whether the controller has sent a stop request.
- Selects a batch of user sessions.
- Fetches out-of-network feed inventory once per worker cycle.
- Fetches one shared in-network inventory union for the whole worker batch.
- Filters the shared in-network union by viewer in Python for each session.
- Removes each session's in-network action IDs from its out-of-network list
  before ranking.
- Converts database rows into RecSys and user-model inputs.
- Lets RecSys build one feed per selected user.
- Lets the user model return viewed action IDs and generated actions.
- Records aggregate views for the viewed action IDs.
- Writes generated actions back to Postgres.

## Main Steps

- Open the worker database connection.
- Read worker settings:
  - batch size;
  - zero-activity ratio derived from per-type setup settings;
  - sleep time when no users are available;
  - action write chunk size;
  - separate in-network and out-of-network inventory-size factors.
- Repeat until `STOP_REQUEST` is received:
  - select the next active users from the `user` table;
  - add zero-activity users required by the batch algorithm;
  - attach a unique session ID to each selected user;
  - if no users are available, wait for the configured idle interval (zero
    means retry immediately);
  - calculate each inventory limit as its source factor multiplied by
    `recsys.feed_size` and `worker.batch_size`;
  - fetch out-of-network inventory for the whole claimed batch;
  - fetch the shared in-network union for every claimed user;
  - map each user row into the worker-facing profile;
  - filter the shared in-network result for that user;
  - remove overlapping in-network action IDs from that user's out-of-network
    candidates;
  - map inventory rows into feed candidates;
  - ask RecSys to build the feed;
  - ask the user model for viewed IDs and generated actions;
  - convert viewed IDs into database view records;
  - convert generated actions into database writes;
  - write aggregate view increments;
  - write actions in batches.
- Close the database connection when the loop ends.

## Idle Polling And Performance

`worker.idle_sleep_milliseconds` is a finite nonnegative wall-clock delay,
defaulting to `0`. It applies only after a claim returns no sessions. A positive
value is divided by 1,000 for Python's seconds-based sleep; fractional
milliseconds are allowed. Successful claims proceed without this delay.

Zero provides a no-delay baseline for performance experiments. A positive
backoff may reduce repeated idle queries, CPU use and database contention,
possibly increasing overall throughput, but it also delays the next claim and
stop check. Measure throughput, CPU/database load and shutdown latency across
values rather than assuming longer waits improve performance. The setting does
not directly advance the simulator clock, though concurrent scheduling can
change which work happens first. There is no calibration or benchmark selecting
an optimal interval; zero is an operational baseline. See the related
[Policy setting](policy.md#polling-and-performance).

Configuration migration: replace `worker.idle_sleep_seconds` with
`worker.idle_sleep_milliseconds`; multiply an existing value by 1,000 to keep
the same delay (the former `0.25` seconds becomes `250` milliseconds). The old
key alone no longer satisfies the required configuration.

## Inventory Rules

- In-network content comes from creators the selected user follows.
- Out-of-network content comes from all recent actions, regardless of whether
  the selected user follows the source creator.
- Self-authored actions are treated like any other candidate.
- Likes are skipped when building inventories and are not feed candidates.
- Reposts pull the reposted item into inventory, ranked by the repost event.
- Actions with feed-exclusion interventions are skipped when building
  inventories. The current feed-exclusion intervention values are `"delete"`
  and `"removed"`.
- Reposts of feed-excluded source actions are skipped so those source actions do
  not re-enter inventories through materialized reposts.
- `recsys.inventory_composition.size_multiplier` is a positive integer that
  multiplies `recsys.feed_size * worker.batch_size` to form the total inventory.
- `recsys.inventory_composition.out_of_network_ratio` allocates that total to
  the out-of-network inventory; the in-network limit receives the remainder.
- The out-of-network limit is the total multiplied by the ratio and rounded to
  the nearest integer. The two limits always sum to the total.
- Both inventory queries select the newest candidate source events up to their
  respective limits before applying any source-action or repost-parent
  moderation filter. Likes are excluded from the candidate pools before
  limiting because they are not feed items.
- After limiting, filter feed-excluded source actions, resolve reposts to their
  parent content, and filter feed-excluded parents. Do not replace rejected
  events with older actions. Viewer filtering, overlap removal and content
  deduplication can further reduce the inventory without refilling it.
- The in-network query accepts all batch user IDs, joins their followed
  creators, deduplicates the source-event union, and records the viewers for
  each source event before applying the shared limit. Multiple viewers of the
  same source event do not consume separate inventory slots.
- Out-of-network inventory is fetched once per worker cycle.
- In-network inventory is fetched once per worker cycle.
- Per-user in-network filtering and in/out overlap removal happen in Python;
  ranking remains per user because component values can depend on that user.
- Viewer eligibility is keyed by source action ID, not materialized parent ID.
  Distinct reposts retain their own creator and eligible viewer sets.
- After filtering for a viewer, any eligible in-network event takes precedence
  over all out-of-network events for the same parent. Then deduplicate each
  surviving set by parent ID, keeping its largest source action ID.
- Both RecSys and user-target recency scores use inverse rank of these surviving
  source action IDs, newest first. Direct actions use their own IDs. Reposts use
  their repost event IDs; parent IDs still identify displayed content and targets.
  This does not reorder the final feed chronologically.
- RecSys receives unique candidates already tagged as `in_network` or `out_network`.
- Engagement first limits history to the latest configured number of total action
  rows, then filters eligible child reactions inside that window. Excluded rows
  are not replaced with older reactions; each component retains its +1 baseline.

## Selection And Feed Math

- Let `B` be `worker.batch_size`.
- Let `L` be the effective zero-activity ratio derived from per-type
  `lurker_ratio` settings.
- Active users have `activity_level > 0.0`.
- Zero-activity users have `activity_level == 0.0`.
- The worker first asks Postgres for at most `B` active users.
- Active users are sampled with probability proportional to `activity_level`.
- Each selected active user receives the fixed action budget calculated from
  `recsys.feed_size` and `user_behavior.actions_per_session_ratio`.
  Config validation requires `round(feed_size * min(0.5, ratio)) >= 1`, including
  when a small positive ratio would round to zero. Lurkers intentionally remain
  at zero. Actual generated actions can be fewer when eligible targets run out.
- Selected active users are counted as `NZ`.
- The worker computes `NZ * L / (1 - L)` and probabilistically rounds that value
  into the number of extra zero-activity users to add.
- Added zero-activity users receive planned action count `0`.
- On `STOP_REQUEST`, a worker finishes its current batch, writes its completed
  database operations, closes its connection, sends `PROCESS_TERMINATED`, and
  starts no new batch.
- Current selection is not exclusive:
  - there is no claim table yet;
  - another worker can select the same user.
- RecSys combined ranking and feed truncation are described in
  `docs/algorithms/simulation.md`.

## View Write Rules

- The user model decides which action IDs were viewed.
- The default user model samples a viewed feed prefix from the view-count
  distribution described in `docs/algorithms/simulation.md`.
- The worker records views for IDs returned in `viewed_action_ids`.
- Views are represented by `ActionViewRecord`.
- `write_action_views` increments `action.views_by_others_count` by grouped
  action ID after excluding views where the viewer is the action creator.
- Before incrementing counters, the worker takes transaction-scoped advisory
  locks for target action IDs in ascending order. This coordinates with action
  inserts that reference parent actions without taking row locks stronger than
  the actual update requires.
- View writes happen for active users and zero-activity users. Zero-activity
  users have a planned action count of zero.
- View writes do not create per-user exposure history.

## Action Write Rules

- Generated actions are written to the shared `action` table.
- Before writing actions with `parent_action_id`, the worker takes
  transaction-scoped advisory locks for those parent action IDs in ascending
  order. This prevents worker transactions from touching parent actions in
  conflicting orders while Postgres enforces the self-referential action
  foreign key.
- Planned action count `0` sessions are view-only.
- Generated actions must use canonical action types:
  - `post`;
  - `like`;
  - `repost`;
  - `reply`;
  - `quote`.
- `like`, `repost`, `reply`, and `quote` must point to a parent action.
- A sampled parented action is skipped when the user has no viewed feed item to
  use as the parent.
- `post` has no parent action ID.
- The worker records the session ID in `action.other_properties` for
  traceability.
- The worker enriches new content actions with post-level `quality` and
  `appeal` before writing them.
- The worker runs the optional `post_labeling_functions` mapping configured for
  the creator's user type and stores its namespaced results in
  `other_properties.post_labels`.
- Post-labeling wrappers currently use Python's module-level `random`
  generator, so the general randomness reproducibility rule in
  `docs/algorithms/simulation.md` applies to label assignment and sampled
  survival durations.
- Content actions are `post`, `reply`, and `quote`.
- Generated `post`, `reply`, and `quote` actions must carry content embeddings.
- Content embeddings come from the configured
  `user_behavior.content_embedding_generation_function`.
- `like` and `repost` do not generate fresh post-level quality, appeal, or post
  labels.
- `like` and `repost` inherit descriptive and policy metadata from their parent
  action at write time. That includes `quality`, `appeal`, all
  `other_properties.post_labels` fields, and `moderation_actions`.
- Parent `other_properties` keys that describe the parent row's local trace or
  materialized inventory state are not inherited: `session_id`, `source_*`,
  `materialized_*`, and `engagement_*`. The child keeps its own row-local
  properties, including its worker `session_id`.
- When the same user writes the same `like` or `repost` for the same
  `parent_action_id`, the worker writes the new row and annotates prior
  matching actions with
  `moderation_actions[*].intervention_type = "delete"`. The new row's
  `repeated_like_n_reshare_creator_count` equals the prior maximum matching
  count plus one.
- The worker preserves the ERD wording `moderation_actions`.
- The worker does not assign simulator timestamps.
- Postgres assigns `timestamp` through the shared column default.
- If a post-labeling wrapper samples an internal survival duration, the insert
  transaction materializes it as
  `post_labels.survival_time = action.timestamp + sampled_duration_ms`
- Generated actions are inserted in chunks of at most
  `worker.action_insert_chunk_size` rows.

## Important Limits

- Current session selection is not an exclusive database lease.
- Multiple workers may select the same user until a shared lease table is added.
- RecSys feed construction is owned by the RecSys component.
- User decision behavior is owned by the user-model component.
- Policy and analytics behavior are owned by their own components.

## Implemented In

- `libs/simsom/components/worker.py`
- `libs/simsom/db/worker.py`
- `libs/simsom/subcomponents/recsys.py`
- `libs/simsom/subcomponents/user_executor.py`

## Repost Exposure Provenance

The feed candidate's `source_action_id` and `source_action_type` retain the
surviving inventory event after viewer filtering and deduplication. The default
user executor uses them to set `other_properties.via` only on a new repost
exposed through a source repost. The worker persists this child-local value and
excludes the parent's `via` from inherited properties. The content parent and
view attribution remain unchanged. See the [definition and examples](domain_activity.md#repost-exposure-provenance-via).
