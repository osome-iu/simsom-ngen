<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Domain And Activity Rules

This document defines shared meanings that setup, RecSys, worker, user
execution, policy, and analytics depend on.

`docs/algorithms/simulation.md` is the source of truth for the high-level
simulation algorithm. This document defines the shared domain vocabulary used by
that algorithm and by implementation-facing component docs.

## Shared User Meaning

- A simulator user is identified by `user_id`.
- `user_id` is stable for one run.
- Setup creates integer IDs starting at `1`.
- `vector_profile` is an embedding-like list reserved for RecSys or behavior
  models.
- `other_properties` is extra metadata and is not part of core simulation
  semantics.

## Activity Level Meaning

- `activity_level` is a non-negative float.
- `activity_level == 0.0` means zero-activity identity.
- `activity_level > 0.0` means active for worker selection.
- There is no separate lurker boolean.
- Setup assigns exact `0.0` to the configured share of zero-activity users.
- Active activity-level sampling must return values greater than `0.0`.
- Active users are sampled for a worker batch with probability proportional to
  `activity_level`.
- Each selected active user session receives the fixed action budget calculated
  from the configured feed size and action-per-session ratio. The rounded
  budget must be at least one; a configuration that produces zero is rejected.
- Zero-activity users receive planned action count `0`; their sessions can
  contribute views but emit no actions.

The activity sampler requires exponent, minimum and maximum in configuration.
See [setup inputs and provenance](configuration.md#setup-functions).

## Session Meaning

- A session is one worker execution slot for one selected simulator user.
- `user_id` identifies the persisted user.
- `session_id` identifies one claimed work item for tracing and debugging.
- Multiple sessions for the same `user_id` may exist at the same time.
- Current session selection is not an exclusive database lease.
- A session carries:
  - the selected user record;
  - the generated `session_id`;
  - `planned_action_count`.
- The worker maps each session into the worker-facing profile before RecSys
  and user execution.
- User execution returns viewed action IDs and generated actions in one result.
- The default user model samples a viewed feed prefix from the view-count
  distribution described in `docs/algorithms/simulation.md`.
- The worker records one aggregate view for each action ID returned by user
  execution.
- Generated actions are wrapped with the same `session_id`.
- The worker stores that trace ID under `action.other_properties.session_id`
  when writing generated actions.
- `session_id` is not a replacement for `user_id`.

## Action Settings Meaning

- Per-user action settings live under:
  - `user_behavior.p_actions`.
- `p_actions` maps action types to fixed per-user probabilities.
- The source config stores action settings under:
  - `user_behavior.p_actions`.
- Setup samples from `user_behavior.p_actions` and stores per-user copies under
  `user_behavior.p_actions`.
- Config keys should use canonical action types:
  - `post`;
  - `like`;
  - `repost`;
  - `reply`;
  - `quote`.
- User execution reads canonical keys from `user_behavior.p_actions`.
- User execution emits canonical action types.
- A configured `[mean=0, std=0]` disables that action type. Exclude it before
  sampling, flooring and normalization; its final probability stays zero.
- For each user, draw one normal weight per enabled type and clip negative draws
  to zero. If every enabled draw for a user is zero, replace that user's vector
  with the configured means, then normalize only enabled weights. Keep the
  resulting preferences across sessions. Disabled types remain at probability
  zero. Configuration validation rejects enabled action sets with no positive
  mean before sampling; reject an all-disabled configuration as well.
- Reject nonfinite numeric configuration and scoring results. Valid zeros and
  field-specific empty values retain their existing meanings.

## Action Probability Calibration

Calibration steps and the saved report are in
[`analyses/calibration/activity/event_distribution.ipynb`](../../analyses/calibration/activity/event_distribution.ipynb).
The report uses the May 1, 2025 Bluesky sample, retains recognized create actions
and users with **fewer than 500** such actions, and computes each user's action
shares. The configured means/stds are the equally weighted means and population
standard deviations (`ddof=0`) across those users. The cutoff selects the
calibration sample; it does not limit a simulation user's activity or sessions.

| Action | Mean | Standard deviation |
|---|---:|---:|
| like | 0.6915118327290447 | 0.36454652573865576 |
| post | 0.15068090071198412 | 0.3124205094831243 |
| repost | 0.0815232983022833 | 0.18122663495827296 |
| reply | 0.065723841705132 | 0.16981072168932315 |
| quote | 0.010560126551555762 | 0.06587685283794088 |

Normal draws approximate the empirical shares; flooring and normalization
change the resulting distribution. These numbers are the report's saved
results, not a fresh calibration run. The raw source data are not included in
this repository.

## Canonical Action Types

- `post`
  - Original top-level content created by a user.
  - Does not require `parent_action_id`.
- `like`
  - Positive reaction to an existing action.
  - Requires `parent_action_id`.
- `repost`
  - Reshare of an existing action without new authored text.
  - Requires `parent_action_id`.
- `reply`
  - Response to an existing action.
  - Requires `parent_action_id`.
  - Requires `content_embedding`.
- `quote`
  - Reshare of an existing action with new authored text.
  - Requires `parent_action_id`.
  - Requires `content_embedding`.

## Parent Action Meaning

- `parent_action_id` points to the action being reacted to, reshared, replied
  to, or quoted.
- `like`, `repost`, `reply`, and `quote` require `parent_action_id`.
- `post` does not require `parent_action_id`.
- If a parented action type is sampled but no viewed item is available, that
  sampled action emits nothing.
- The database stores the parent relationship as:
  - `action.parent_action_id -> action.action_id`.

## Repost Exposure Provenance (`via`)

A generated repost optionally stores `other_properties.via` as a positive
BIGINT action ID identifying the **source repost** through which the user saw
the displayed content. `parent_action_id` continues to point to the displayed
content, not to the exposing repost. Omit `via` when content was encountered
directly; likes, replies, quotes and original posts do not carry this field.

For example, Ben creates post P, Nick reposts P as R1, and Fil sees P through
R1. Fil's new repost R2 has `parent_action_id = P` and `via = R1`. A later user
seeing P through R2 records `via = R2`; they do not copy R2's existing `via`.
Inventory deduplication chooses the exposure: in-network sources take priority,
then the newest source action in the surviving set supplies the provenance.

If the displayed content is a quote Q of P, a repost of Q still has parent Q.
When Q was exposed by repost R, record `via = R`; do not add an extra link to P
or infer a route through Q's parent. A direct view of Q yields no `via`.

This is row-local provenance, excluded from inherited metadata. It does not
change ranking, quality, policy eligibility, duplicate cleanup, or view counters.
Views remain credited to displayed content. The default executor selects the
source from a persisted feed candidate; direct database imports remain
responsible for reference integrity because this JSON field is not a foreign
key. No empirical calibration is involved. 

## Views And Actions

- Each feed item is either viewed or not viewed.
- Emitted actions do not imply views.
- Views are returned separately from generated actions.
- The worker writes aggregate non-creator views to `views_by_others_count`.
- First choose action types for the planned action count using
  `user_behavior.p_actions`.
- Then sample targets for each parented action type from viewed items.
- Different action types may target the same viewed item.
- `post` is parentless and can emit when no item was viewed.

## Weight Components

- Weight calculations use expandable configured component lists.
- Each component entry has:
  - exactly one component function selector;
  - `importance_in`;
  - `importance_out`.
- The general form is:
  - `W(i) = product(component_j(i) ** importance_j)`.
- Components must return values in `[0, 1]`, where larger means better.
- `importance_in` is used for in-network feed items.
- `importance_out` is used for out-of-network feed items.
- RecSys uses `recsys.feed_weight_components` to rank feed items from
  inventories.
- The user model uses `user_behavior.feed_weight_components` to sample parent
  targets from viewed items.
- The default RecSys weights include recency; the default user-behavior weights
  omit it. Recency remains an available component for either list.
- Current supported components are:
  - `appeal_component_function = {"post_appeal": {}}`;
  - `recency_component_function = {"inverse_rank_recency": {}}`;
  - `relevance_component_function = {"profile_vector_n_content_embedding_cosine_relevance": {}}`;
  - `engagement_component_function = {"global_like_count": {}}`;
  - `engagement_component_function = {"global_repost_count": {}}`;
  - `engagement_component_function = {"global_reply_count": {}}`;
  - `engagement_component_function = {"global_quote_count": {}}`.
- Engagement components currently count global child actions. Future component
  function names may add friends-scoped engagement calculations.
- Engagement counts use only the latest
  `recsys.engagement_action_lookback_population_multiplier * initialized_user_count`
  action rows.
- Quality is not part of default weight calculations; it is stored for analysis.
- Generated content embeddings come from
  `user_behavior.content_embedding_generation_function`.

The component formulas and default importance values are model choices; no
empirical calibration is recorded for those weights. See
[configurable scoring and provenance](configuration.md#policy-and-analytics-functions).

## User Type, Quality, And Appeal Meaning

- Setup assigns each simulator user one fixed user type at the beginning of a
  simulation run.
- A user type is a profile/behavior category, not a community.
- The selected user type is stored as `user.user_type`.
- User type counts come from `setup_db.user_types[*].num_nodes`.
- Total initialized users are derived from the sum of configured type counts.
- User quality uses the fixed `two_cluster_inverse` distribution function on a
  `[0, 100]` scale during setup.
- Config provides its parameters:
  - `w` is the probability of drawing from the high-value cluster;
  - `1 - w` is the probability of drawing from the low-value cluster;
  - `lambda_slope` controls truncated exponential steepness.
- The active default uses the quality calibration notebook's COVID-data
  scenario: `w = 0.85` and `lambda_slope = 0.1190`.
- Setup normalizes each user quality draw to `[0, 1]` and stores it as
  `user_behavior.quality`.
- Actions with newly generated content generate post-level `quality` and
  `appeal` using the creator user type's configured post distribution
  functions.
- Each user type may optionally configure `post_labeling_functions`. Each
  selected function returns namespaced labels that are stored under
  `action.other_properties.post_labels`.
- Non-content actions inherit descriptive and policy metadata from their parent
  action instead of generating fresh values. Inherited metadata includes
  `quality`, `appeal`, all `post_labels`, and `moderation_actions`.
- Parent row-local `other_properties` keys are not inherited: `session_id`, `via`,
  `source_*`, `materialized_*`, and `engagement_*`.
- `gaussian_around_user_quality` samples a Gaussian centered on
  `user_behavior.quality` using the explicit `stddev` input, then clamps to
  `[0,1]`. The default is `0.12`; see
  [quality calibration and the appeal assumption](configuration.md#content-functions).
- Quality is intended for policy and analysis on any action carrying that
  metadata.
- Appeal may be used as a model feature.

Example:

```json
{
  "user_types": {
    "normal": {
      "num_nodes": 10000,
      "user_quality_distribution": {"w": 0.85, "lambda_slope": 0.1190},
      "post_quality_distribution_function": {"gaussian_around_user_quality": {"stddev": 0.12}},
      "post_appeal_distribution_function": {"gaussian_around_user_quality": {"stddev": 0.12}},
      "post_labeling_functions": {"illegal_probability_n_survival_hours": {"probability": 0.01, "expected_survival_hours": 144}}
    }
  }
}
```

## Moderation Actions Meaning

- Use the ERD wording `moderation_actions`.
- Do not introduce `annotations` as an alias for moderation behavior.
- Policy review progress is tracked by the persisted policy cursor, not by
  writing per-action review metadata.
- Interventions are stored only when an action is actually intervened on:
  `moderation_actions = [{"intervention_type": "...", "violation_type": "...", "rule": "..."}]`.
- The default quality-threshold policy rule uses
  `intervention_type = "removed"` inside an intervention entry when a content
  item falls below the configured threshold.
- `violation_type` stores the reason, for example `"below_quality_threshold"`
  from a configured threshold wrapper, and `rule` stores the selector that
  produced the intervention.
- Shared intervention semantics maps each `rule` to an intervention source,
  such as `policy` or `user`. Source is declared through the intervention
  decorator instead of being repeated as a stored JSON field.
- Consumers choose the dimension they need: RecSys/feed filtering uses
  `intervention_type`, while analytics policy counts use decorator-registered
  intervention source metadata.
- Repeated `like` and `repost` actions are written as new action rows. The
  worker records repeated attempts in
  `repeated_like_n_reshare_creator_count` and annotates prior matching rows
  with a user-source `delete` intervention. The worker owns the batching/write
  mechanism; the intervention source is the repeated user action.
- Setup, RecSys, worker, and user execution should preserve moderation values
  unless they explicitly own a moderation decision.

## Cross-Owner Assumptions

- Setup owner:
  - creates initial users and sampled `user_behavior.p_actions`;
  - does not implement RecSys or user behavior.
- RecSys owner:
  - samples feed items from in/out inventories;
  - may use action type, parent, counters, content embeddings, moderation state,
    activity level, and `other_properties` as features;
  - does not redefine action type meanings.
- User-model owner:
  - reads the worker-facing profile and ranked feed;
  - decides which feed items were viewed;
  - emits action values with canonical action types;
  - supplies required parent IDs for actions that need them.
- Worker owner:
  - maps database rows into RecSys and user-model contracts;
  - maps viewed IDs into DB view records;
  - writes aggregate action views back to the shared `action` table;
  - writes generated actions back to the shared `action` table;
  - stores session trace data in `other_properties.session_id`.
- DB/schema owner:
  - owns storage shape and indexes;
  - does not own behavior interpretation beyond shared schema constraints.
