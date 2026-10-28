<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Simulation Algorithm

This document describes the simulation model at the algorithm level. It avoids
implementation-specific names except where an implementation detail is required
to make the algorithm unambiguous.

For high-level simulation behavior, this document is authoritative. Companion
algorithm docs describe component ownership and implementation-facing details.

## Full Run Flow

- The controller prepares the database schema.
- The controller creates the initial users.
- The controller creates or imports the directed friend network.
- Workers repeatedly select batches of users.
- Each worker cycle fetches one batch-level out-of-network inventory and one
  shared in-network inventory union for the selected users.
- Recommendation System (RecSys) builds a feed for each selected user.
- The user model records views and emits actions.
- Policy reads database state and writes moderation interventions.
- Analytics reads database state and may publish termination signals.
- Analytics records one summary row for each analytics pass.
- The controller records the authoritative lifecycle termination state after a
  termination signal.
- The controller dumps final state when configured and shuts down the run.

## Randomness And Reproducibility

- Any algorithm path that uses randomness is not reproducible unless every
  random source used by that path is explicitly controlled.
- This applies to Python `random`, NumPy random functions, PostgreSQL
  `random()`, and the database simulator-clock random increment.
- Distributed runs need per-process and database-side seed/control rules before
  random setup, worker selection, user decisions, post labels, survival
  durations, quality/appeal draws, feed sampling, or timestamps can be treated
  as reproducible.

## Controller Process Model

### Overview

The controller is the sole owner of simulation lifecycle decisions.

The controller operates in three phases:

1. Run setup
2. Termination monitoring
3. Shutdown

Analytics evaluates the configured termination criteria and requests termination when one or more criteria are met. The controller validates that request, writes the authoritative termination state, and coordinates an orderly shutdown of all processes.



### Run Setup

During run setup, the controller:

- prepares shared tables without secondary indexes;
- recreates and configures the simulator clock extension;
- plans compact user-type assignments;
- samples and streams bounded user-profile chunks directly to PostgreSQL;
- builds or imports the directed friend network;
- loads users and friend links into the database;
- builds secondary indexes after the initial rows are loaded;
- collects PostgreSQL planner statistics before runtime processes are released;
- initializes the shared lifecycle state to indicate that termination has not been requested.

Setup must be complete before workers, Policy, or Analytics are expected to make useful progress.

During setup, a controller-owned sidecar process appends structured progress
records to `setup_status.jsonl` in the configured log directory. A record is
written for each stage transition and integer-percent change, with a heartbeat
at least once every 10 seconds during active work. User generation reports its
preparation step and generated users; synthetic network generation reports each
phase, user type, function, source-user progress, and current edge counts;
database setup reports schema work, rows sent during initial-state loading, and
secondary-index construction plus planner-statistics collection.
The stream ends with `setup` marked `complete`, or with the current stage marked
`failed` and its error details.

Detailed setup records are written only to `setup_status.jsonl`. At the start
of each setup stage, `simsom.log` receives a compact `setup_stage_started` entry
that points to this file; it does not duplicate progress or heartbeat records.

The non-controller ranks do not produce waiting heartbeats. Analytics starts
after setup and does not read the setup progress stream as part of simulation
analytics.

Synthetic network generation uses sparse, compact execution paths without
changing the configured network models. Users and friend links are sent to
PostgreSQL as bounded `COPY` streams; the controller does not retain complete
user or edge-object lists. Random-walk adjacency, optional follower counts, and
weighted-sampling state use fixed-width NumPy arrays and temporary memory maps
when large. Fixed-degree models scale with user and edge counts;
density-based models still scale with their inherently quadratic requested edge
count. [`controller.md`](controller.md#friend-network-algorithms) specifies each
generator's exact target selection, edge-count formula, execution order,
complexity, memory requirements, optimization effects, and randomness effects.



### Termination Monitoring

During termination monitoring, the controller repeatedly reads the shared lifecycle state.

Analytics evaluates the configured termination criteria.

When one or more termination criteria are met, Analytics records:

- that termination has been requested;
- the termination reason;
- the criteria that were met.

The controller:

- reads the Analytics termination request;
- validates the request;
- writes the authoritative termination state exactly once;
- preserves the termination reason and the criteria reported by Analytics;
- sends a `STOP_REQUEST` message to every other process.

If termination has already been requested through the shared lifecycle state, the controller honors the existing request and uses its recorded reason. The controller must not overwrite an existing authoritative termination request with a later request.



### Shutdown Protocol

Each process receives `STOP_REQUEST` at a safe checkpoint.

A safe checkpoint is a point where the process can finish its current operation without leaving an incomplete database transaction or partially persisted state.

After receiving `STOP_REQUEST`, each process must finish only the work already in progress and must not begin new work.



### Worker Shutdown Behavior

MPI workers check for `STOP_REQUEST` between batches. A request arriving during
an already-selected batch waits for that batch to finish. Each worker:

- completes all selected active and lurker sessions in the current batch;
- persists the batch's view increments and generated actions;
- starts no new batch;
- closes its database connection;
- sends a `PROCESS_TERMINATED` acknowledgment to the controller;
- exits.

The acknowledgment confirms that the selected batch is complete and the database
connection is closed. Batch view and action writes need not share one transaction.



### Policy Shutdown Behavior

After receiving `STOP_REQUEST`, Policy:

- finishes the current policy pass and its database writes;
- starts no new policy pass;
- closes its database connection;
- sends a `PROCESS_TERMINATED` acknowledgment to the controller;
- exits.

Policy must not send `PROCESS_TERMINATED` until its current labeling operation and database transaction are complete.



### Analytics Shutdown Behavior

After receiving `STOP_REQUEST`, Analytics:

- finishes the current non-final analytics pass;
- stops starting new recurring analytics passes;
- sends an `ANALYTICS_READY_FOR_FINAL` acknowledgment to the controller;
- remains alive;
- keeps its database connection open;
- waits for final-analysis authorization.

`ANALYTICS_READY_FOR_FINAL` indicates that recurring Analytics work has stopped and that Analytics is ready to process the stable final database state.

Analytics must not begin final analysis until it receives `FINAL_ANALYSIS` from the controller.



### Controller Readiness Barrier

After sending `STOP_REQUEST`, the controller waits for:

- a `PROCESS_TERMINATED` acknowledgment from every worker;
- a `PROCESS_TERMINATED` acknowledgment from Policy;
- an `ANALYTICS_READY_FOR_FINAL` acknowledgment from Analytics.

The controller validates each acknowledgment and tracks which processes have responded.

The controller must not authorize final analysis until:

- every worker has terminated;
- Policy has terminated;
- Analytics has completed its current recurring pass;
- no worker or Policy process can perform additional database writes.

At this point, the database is considered stable for final analysis.



### Final Analysis

After all required acknowledgments have been received, the controller sends Analytics a `FINAL_ANALYSIS` message.

Upon receiving `FINAL_ANALYSIS`, Analytics:

- reads the stable final database state;
- performs the final analytics pass;
- builds the final analytics summary;
- persists the final analytics summary;
- sends `FINAL_ANALYTICS_COMPLETE` to the controller;
- closes its database connection;
- exits.

Analytics must send `FINAL_ANALYTICS_COMPLETE` only after the final summary has been successfully persisted.



### Controller Finalization

After receiving `FINAL_ANALYTICS_COMPLETE`, the controller:

- validates the acknowledgment;
- verifies that final Analytics processing completed successfully;
- optionally dumps the final database state when configured;
- closes its database connection;
- exits.

The optional database dump must occur only after the final analytics summary has been persisted.




### Authoritative Termination State

The controller owns the authoritative termination state.

Analytics may publish a termination request, but that request does not become authoritative until the controller validates and records it.

The authoritative termination state should include at least:

- whether termination has been requested;
- the termination reason;
- the criteria that were met;
- the source of the request;
- the time or simulation clock value at which the request was recorded.

The controller must write the authoritative termination state exactly once.

Repeated observations of the same Analytics request must not create duplicate state changes or duplicate shutdown sequences.



### Timeout Handling

The controller must apply timeout handling while waiting for acknowledgments.

Timeout handling must cover:

- missing worker `PROCESS_TERMINATED` acknowledgments;
- a missing Policy `PROCESS_TERMINATED` acknowledgment;
- a missing Analytics `ANALYTICS_READY_FOR_FINAL` acknowledgment;
- a missing `FINAL_ANALYTICS_COMPLETE` acknowledgment.

When a timeout occurs, the controller:

- identifies the process or acknowledgment that is missing;
- log the missing acknowledgment;
- preserve the original termination reason;
- stop waiting and fail the controller shutdown operation.

Retries, forced termination, and partial-dump policies are not part of the
current shutdown behavior.

A timeout must not be reported as the original reason for termination unless the timeout itself initiated termination.



### Lifecycle Sequence

```text
Controller                 Workers              Policy              Analytics
    |                          |                    |                    |
    |------ Run setup -------->|                    |                    |
    |------------------------------ Run setup ----->|                    |
    |-------------------------------------------------- Run setup ------>|
    |                          |                    |                    |
    |                          |                    |     Evaluate       |
    |                          |                    | termination criteria
    |                          |                    |                    |
    |<------------------------------------------------ Termination request
    |                          |                    |                    |
    | Validate request         |                    |                    |
    | Write authoritative      |                    |                    |
    | termination state        |                    |                    |
    |                          |                    |                    |
    |---- STOP_REQUEST ------->|                    |                    |
    |---------------------------- STOP_REQUEST ---->|                    |
    |-------------------------------------------------- STOP_REQUEST --->|
    |                          |                    |                    |
    |                     Finish current       Finish current       Finish current
    |                     operation and       labeling operation    recurring pass
    |                     transaction               |                    |
    |                          |                    |                    |
    |<-- PROCESS_TERMINATED ---|                    |                    |
    |<-------------------------- PROCESS_TERMINATED |                    |
    |<------------------------------------- ANALYTICS_READY_FOR_FINAL ---|
    |                          |                    |                    |
    | Confirm all workers      |                    |                    |
    | and Policy terminated    |                    |                    |
    |                          |                    |                    |
    |---------------------------------------------- FINAL_ANALYSIS ----->|
    |                          |                    |                    |
    |                          |                    |             Read stable state
    |                          |                    |             Persist final summary
    |                          |                    |                    |
    |<------------------------------------- FINAL_ANALYTICS_COMPLETE ----|
    |                          |                    |                    |
    | Optional database dump   |                    |                    |
    | Close database connection|                    |                    |
    | Exit                     |                    |                    |
```

## Function Registry And Configuration

- Decorator registries provide config-visible function selectors. A new
  config-visible function should be added by declaring the selector on the
  function or helper that owns the behavior.
- This practice applies to policy rules, intervention rules, post labeling,
  survival durations, post distributions, setup network/edge/activity
  functions, content embedding, scoring components, termination criteria, and
  analytics functions.
- Each selected function takes its tunable inputs explicitly from configuration.
  Use `{ "function_name": { "input_name": value } }`; numeric choices do not
  belong in wrapper names or hidden function defaults. See
  [function inputs and provenance](configuration.md#function-inputs). Shared
  semantic constants, such as JSON property names, remain code constants.
- Intervention entries persist `rule`, not `source`. Any rule or helper that
  produces an intervention must declare source metadata with the intervention
  registry, for example `source="policy"` or `source="user"`.

## User Type And Setup Model

- The simulator is designed for runs with up to `1,000,000,000` initialized
  users. This is the intended scale target for setup, network generation, and
  simulation execution when the selected network model and available storage
  can accommodate the generated edge set.
- Let `T` be the set of user types.
- `T` contains profile/behavior categories. A user type is not a community.
- The setup config provides `setup_db.user_types`.
- `user_types[t].num_nodes` is the exact number of initialized users with
  user type `t`.
- Total initialized users are derived as:
  - `N = sum(user_types[t].num_nodes for t in T)`.
- Each user receives one fixed `user_type` at initialization.
- A user's `user_type` remains fixed for the simulation run.
- Each user type has its own `lurker_ratio`, which controls how many
  initialized users of that type receive `activity_level = 0.0`. Per-type
  ratios accept `[0, 1]`; `1` is allowed when other types provide active users.
  Reject a population with no active users after rounding across all types.
- Active users have `activity_level > 0.0` produced from the configured
  activity-level function, which defaults to drawing from a power law
  distribution.
- Each initialized user receives a fixed `user_behavior.quality` in `[0, 1]` drawn
  from that user type's `user_quality_distribution`.
- Each active user receives the same fixed `planned_action_count` calculated from
  `round(recsys.feed_size * user_behavior.actions_per_session_ratio)`.
  Configurations that round this budget below one action are invalid. Lurkers
  intentionally receive zero; an active session can generate fewer actions than
  its positive budget when eligible targets are unavailable.

## Friend Network Model

- `setup_db.network_source` selects the source of initial friend links:
  - `"synthetic"` runs configured edge-generation functions;
  - `"file"` imports directed links from `setup_db.network_file`.
- Friend links are directed:
  - `user_id` has added `friend_id` as a friend;
  - the reverse link is not automatic.
- File-backed setup does not execute synthetic edge generation.
- Synthetic setup requires one within-type networking function and one
  cross-type networking function for each user type.
- The default within-type function is `random_walk`, configured with
  `seed_size = 3`, `avg_n_friend = 3`, and `probability_follow = 0.5`.
  With these inputs it starts with
  a fully connected directed three-user seed. Each later user selects one
  earlier user uniformly, draws the number of one-hop neighbor-closure links
  from `Binomial(2, 0.5)`, and fills unavailable or remaining slots uniformly
  from distinct earlier users. It creates exactly three outgoing links for
  every post-seed user and `3 * N_t - 3` total links for a type with
  `N_t >= 3` users.
- The random-walk name refers to this one-hop closure through the first target's
  outgoing neighbors; it is not a variable-length multi-hop traversal.
- Synthetic generation skips self-links and duplicate directed links.
- File import rejects self-links, unknown user IDs, and duplicate directed
  links.
- The exact algorithms, density rounding, cross-type follower weighting,
  complexity, memory formulas, and optimization effects are defined in
  [`controller.md`](controller.md#friend-network-algorithms).

## User Batch Algorithm

- Let `B` be the active-user batch size.
- Let `L` be the initialized-user-weighted effective lurker ratio derived from
  per-type lurker ratios, with `0 <= L < 1`. An individual type may have ratio
  `1`; the population-wide batch ratio must stay below `1` for the formula below.
- A user is active when `activity_level > 0.0`.
- Select `B` active users with probability proportional to `activity_level`.
- Each selected active user session gets `A(u) = planned_action_count`.
- Define `NZ` as the number of selected active users.
- Compute the desired number of extra zero-activity users:
  - `desired_zero_activity_users = NZ * L / (1 - L)`.
- Convert that value to an integer with the same probabilistic rounding rule.
- Draw that many users uniformly from users with `activity_level == 0.0`.
- Add them to the worker batch with `A(u) = 0`.
- The User Activity Model runs for every user in the final batch.

## Recommendation System (RecSys) Model

- RecSys builds a feed from separate in-network and out-of-network inventories.
- In-network content is authored by users the viewer follows.
- Out-of-network content is drawn from all recent actions, regardless of follow
  relationship.
- Self-authored actions are treated like any other candidate.
- `recsys.inventory_composition.size_multiplier` multiplies
  `recsys.feed_size * worker.batch_size` to determine the total inventory size.
- `recsys.inventory_composition.out_of_network_ratio` allocates the total to
  out-of-network candidates; the in-network limit receives the remainder.
- The out-of-network limit is the total multiplied by the ratio and rounded to
  the nearest integer. The two limits always sum to the total.
- Both inventories select the most recent candidate source actions up to their
  respective limits before any moderation filtering. Likes are not feed
  candidates and are excluded from these source pools.
- After limiting, apply moderation checks to the selected source actions and
  the parent content displayed by reposts. Excluded items leave gaps; do not
  fetch older actions to replace them or refill after deduplication.
- The in-network query builds one limited union for all users in the worker
  batch and records which users can see each returned action.
- Each user's in-network list is filtered from that shared union before feed
  construction.
- Preserve each source event's creator and viewer eligibility until the shared
  inventory has been filtered for the current user.
- If any eligible source event for a parent post is in-network, remove that
  parent's out-of-network representations.
- Display each parent post at most once, using the latest source event in the
  surviving set for recency. A discarded out-of-network event cannot supply
  recency to an in-network item.
- Out-of-network inventory is fetched once per worker cycle for the claimed
  batch.
- In-network inventory is fetched once per worker cycle for the claimed batch.
- Likes are skipped when building inventories and are not feed candidates.
- Reposts pull the reposted item into inventory, ranked by the repost event.
- Actions with feed-exclusion interventions are ignored when building
  inventories. The current feed-exclusion intervention values are `"delete"`
  and `"removed"`.
- Reposts of feed-excluded source actions are ignored, so those source actions
  cannot re-enter inventories through materialized reposts.
- RecSys combines the eligible inventories, deduplicates displayed action IDs,
  then ranks the combined candidates once using configured component functions.
- Each component returns a value in `[0, 1]`, where larger is better.
- Each component has separate `importance_in` and `importance_out` values, so
  in-network and out-of-network candidates can weight the same signal
  differently.
- Relevance compares the viewer's profile vector with the candidate content
  embedding using normalized cosine similarity.
- Engagement components currently count global child actions. The supported
  default functions are `global_like_count`, `global_repost_count`,
  `global_reply_count`, and `global_quote_count`.
- Engagement counts use only the latest
  `recsys.engagement_action_lookback_population_multiplier * initialized_user_count`
  action rows.
- Feed-excluded child actions do not contribute to engagement component counts.
- Weight controls ranking across both sources. Recency is inverse rank over the
  combined, deduplicated pool, ordered by surviving source action ID.
- In-network representations take precedence for a duplicate displayed action;
  within the surviving source, use the latest source event. Each displayed
  action appears at most once before scoring and feed truncation.
- Sort by descending component weight and retain at most `recsys.feed_size`
  items. Equal weights preserve combined input order; timestamps are not a
  separate tie-breaker.
- There is no post-ranking alternation or guaranteed in/out-network ratio.
  `importance_in` and `importance_out` retain their source-specific meanings.
- `feed_size` is a maximum, not a guarantee.


## User Activity Model

- Let `feed` be the RecSys output for user `u`.
- Let `A(u)` be the planned action count for this user session.
- Each feed item is either viewed or not viewed.
- Let `alpha = len(feed)`.
- The model assumes `E[V] = alpha / 2`; this is arbitrary and not yet
  empirically calibrated.
- Therefore `p_stop = 2 / alpha`.
- Draw a positive raw viewed count `V_raw` with density:
  - `P(V_raw = v) = p_stop * exp(-p_stop * v) = (2 / alpha) * exp(-2v / alpha)`,
    for `v > 0`.
- Convert the draw to an integer viewed count and cap it by feed length:
  - `viewed_count = min(ceil(V_raw), alpha)`.
- The viewed set is the first `viewed_count` feed items.
- Only views where `u` is not the action creator increment
  `views_by_others_count`.
- Parented actions are only emitted on viewed items.
- First choose action types for the `A(u)` planned actions using
  `user_behavior.p_actions`.
- Then handle each action type:
  - `post` emits parentless actions with content;
  - `like`, `repost`, `reply`, and `quote` sample targets from viewed items;
  - if no item was viewed, parented action types emit nothing.
- Calculate one weight for each viewed item using the configured component
  functions, then, for each action type with parented actions, sample that
  action type's targets without replacement using those fixed weights.
- If an action type requests more parented actions than there are viewed items,
  the extra requests produce no action.
- Different action types may target the same feed item.
- A single action type samples its own target set for that type.
- Actions with new content are enriched with post-level `quality` and `appeal`
  before being written.
- Actions without new content inherit descriptive and policy metadata from
  their parent action when written.
- Each user type may optionally configure an ordered mapping of post-labeling functions to inputs.
- New content means:
  - parentless `post` actions;
  - `reply` actions;
  - `quote` actions.
- For repeated `like` or `repost` actions by the same user on the same
  `parent_action_id`:
  - the worker writes the new action row;
  - the worker annotates prior matching action rows with
    `moderation_actions[*].intervention_type = "delete"`;
  - the new action's `repeated_like_n_reshare_creator_count` is the prior
    maximum matching count plus one.

Reposts created from content exposed by another repost record that surviving
source repost's ID as `other_properties.via`. The displayed content remains
`parent_action_id`; direct exposure and other action types omit `via`. This
field is not inherited and does not trace through a quote to its parent.
See [repost provenance](domain_activity.md#repost-exposure-provenance-via).

## Quality And Appeal Generation

- User quality is initialized once during setup:
  - draw from the user type's `user_quality_distribution`.
- Post quality is generated for each new content action with that user type's
  `post_quality_distribution_function`.
- Post appeal is generated for each new content action with that user type's
  `post_appeal_distribution_function`.
- Post labels are generated for each new content action by the optional
  `post_labeling_functions` mapping configured for the creator's user type.
- Likes and reposts inherit parent `quality`, `appeal`, `post_labels`, and
  `moderation_actions` instead of generating fresh content metadata. Parent
  row-local `other_properties` keys are not inherited: `session_id`, `via`,
  `source_*`, `materialized_*`, and `engagement_*`.
- Label results are stored in `other_properties.post_labels`, for example:
  `{"legality": "illegal", "survival_time": 123456}` for the delayed
  illegal-content survival wrapper, or `{"legality": "illegal"}` for a direct
  legality labeler. An omitted or empty result produces no labels.
- Survival-time labelers sample an internal duration before insert. Since
  Postgres assigns action timestamps, the worker insert calculates the public
  `survival_time` label as `timestamp + duration` inside the write transaction.

Configured standard deviations, the quality-variance calibration and the
uncalibrated reuse for appeal are documented under
[content function inputs](configuration.md#content-functions).

## Activity-Focused Policy

The implemented policy algorithm is documented in
`docs/algorithms/policy.md`. This section summarizes the behavior that affects
the full simulation flow.

- Policy runs as a separate asynchronous process.
- It evaluates action targets and writes moderation interventions
  according to the configured policy rules.
- The policy process keeps a persisted action-id cursor.
- `policy.target_batch_size` is a positive integer, defaulting to `500`. Each
  pass selects at most this many actions in total. New and due action pages
  share the limit. Policy alternates their priority so both make progress.
  All moderation writes for the pass use one transaction.
- Each policy pass fetches targets with two disjoint query branches:
  - new action rows with `action_id` greater than the persisted policy cursor;
    rows with a future `survival_time` wait for rule evaluation;
  - older action rows with `action_id` at or below the cursor whose
    survival-time label is due and has not already produced a feed-excluding
    intervention.
- Delayed survival-time rows are not copied into a separate cache table. The
  database schema defines a partial expression index on
  `other_properties.post_labels.survival_time`, and the policy query uses that
  label to find due rows.
- All five action types are eligible: `post`, `reply`, `quote`, `like`,
  and `repost`.
- The first pass starts with no cursor and selects the first new-action page;
  actions with a future `survival_time` are deferred.
- Each fetch also reports the largest action ID in its new-action page,
  including deferred actions. After a successful evaluation/write pass, the
  cursor advances to that ID, never moving backward. It is not limited to the
  IDs returned as eligible targets.
- Advancing past a deferred action does not skip its later evaluation: the
  older-action branch finds it once its `survival_time` is at or before the
  current simulator timestamp, provided no intervention already excludes it
  from feeds. Delayed older rows never move the cursor backward.
- If no eligible actions are returned, the policy process still persists any
  cursor progress, then sleeps for `policy.poll_interval_milliseconds` and
  tries again.
- If actions are returned, the configured policy rules are applied.
- `policy.rule_functions` selects the policy rules that run for each eligible
  action. Every selected rule is evaluated, and matching results are combined
  into the action's moderation intervention list.
- Labeler inputs are explicit in configuration. For example,
  `illegal_probability_n_survival_hours` requires `probability` and
  `expected_survival_hours`; see [units and evidence](policy.md#post-labeling-inputs).
- Post-labeling wrappers currently use Python's module-level `random`
  generator, so the general randomness reproducibility rule applies to label
  assignment and sampled survival durations.
- Rule functions own their metadata requirements. The quality-threshold rule
  requires numeric `other_properties.quality` satisfying `0 <= quality <= 1`;
  label-only rules do not require quality.
- The `below_quality_threshold` rule writes intervention fields when
  quality is below its configured `threshold` (default `0.3`):
  - `intervention_type = "removed"`;
  - `violation_type = "below_quality_threshold"`;
  - `rule = "below_quality_threshold"`.
- At or above that threshold, the quality rule writes no intervention for that
  action. Other configured policy rules, such as the illegal-content label
  rule, may still produce an intervention.
- `other_properties.post_labels.survival_time`, when present, is a limit on
  policy evaluation for that action. Policy rules are not evaluated for the
  action until the due time is at or before the current simulator timestamp.
  The configured rule set still determines whether an intervention is written
  and what intervention entry is used after that limit has passed.
- The `illegal_content` rule treats
  `other_properties.post_labels.legality = "illegal"` as illegal-content
  evidence and writes:
  - `intervention_type = "removed"`;
  - `violation_type = "illegal_content"`;
  - `rule = "illegal_content"`.
- For actions with survival time, the due-time check is deterministic after the
  timestamp has passed. The stochastic part is the labeler assignment and
  sampled survival duration; the rule set controls the intervention outcome.
- Interventions are stored as
  `moderation_actions = [{"intervention_type": "...", "violation_type": "...", "rule": "..."}]`.
- When more than one policy reason matches, each reason is stored as a separate
  intervention entry. Matching rules may produce the same or different
  `intervention_type` values.
- Policy appends interventions to the canonical `moderation_actions` list in
  one transaction per pass, then saves the cursor.
- `rule` is the persisted cause of an intervention. Shared intervention
  semantics uses decorator registrations to map rule names to sources such as
  `policy` and `user`, without adding a stored `source` field to every row.
- Rules registered through the policy-rule registry map to the `policy`
  intervention source. Repeated like/repost cleanup maps to the `user`
  intervention source, because it reflects repeated user actions; the worker is
  only the process that materializes those rows.
- Moderation interventions could be broader than feed inclusion/exclusion:
  - feed filtering reads `intervention_type` and excludes feed rows with
    `removed` or `delete`;
  - analytics policy counts read decorator-registered source metadata and count
    interventions whose `rule` maps to source `policy`.
- `intervention_type` is an open field inside each intervention entry; the
  default quality rule currently writes `"removed"` for below-threshold content.

## User-Focused Policy (TBD)

- User-focused policy may define visibility, suspension, or other user-level
  restrictions in the future.
- It is separate from activity-focused content labeling and is not part of the
  current simulation behavior.

## Analytics Model

- Analytics runs as a separate asynchronous process.
- `analytics.evaluation_interval` schedules recurring passes by either average
  persisted actions per initialized user or elapsed simulator hours.
- The same progress checkpoint runs recurring analytics functions and evaluates
  every configured termination criterion. Termination criteria are therefore
  checked by simulation progress, not by elapsed wall-clock time.
- A lightweight clock/action-ID watermark query detects candidate checkpoints;
  the exact aggregate query confirms them before any functions run.
- Recurring checks report only the lightweight metrics needed by termination
  criteria. Analytics writes the full report once after shutdown begins and the
  database is stable.
- Analytics evaluates every function in the ordered `termination.criteria_functions`
  mapping on each recurring check.
- Analytics may publish a controller-readable termination signal when one or
  more configured criterion functions are met.
- The controller freezes the first accepted stop reason and criteria together.
  Later Analytics passes cannot replace that attribution. Final measurements
  reflect the completed batches and are reported separately from the accepted
  stop criteria.
- The default criteria measure average actions per user and quality stability
  with an Exponential Moving Average.
- Analytics stays alive after the controller validates the termination signal,
  waits for final-analysis authorization, and then writes the final report
  after the other processes have completed.
- Recurring analytics functions receive lightweight termination metrics and
  write their outputs to the configured analytics JSONL file on each check.
- Final analytics functions receive the stable final metrics, write one final
  analytics JSONL record, and store their outputs in
  `analytics_summary.other_properties.analytics_outputs`.
- The final pass runs once after shutdown and is independent of the recurring
  progress interval.


## Related Docs

- `docs/algorithms/domain_activity.md`
- `docs/algorithms/controller.md`
- `docs/algorithms/analytics.md`
- `docs/algorithms/database.md`
- `docs/algorithms/database_schema.md`
- `docs/algorithms/worker.md`
- `docs/glossary.md`
