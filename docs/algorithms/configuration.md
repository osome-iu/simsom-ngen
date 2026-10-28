<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Configurable Model Functions

## Function Inputs

Decorators identify algorithms. Model inputs belong in the simulation config,
so parameter sweeps do not require adding or editing Python wrappers. A function
name maps directly to its inputs; there are no generic `function` or `parameters`
layers:

```json
"post_labeling_functions": {
    "illegal_probability_n_survival_hours": {
        "probability": 0.01,
        "expected_survival_hours": 144
    }
}
```

A single-choice field contains exactly one named function. A group such as
`post_labeling_functions`, `policy.rule_functions`, or
`termination.criteria_functions` can contain multiple named functions, evaluated
in insertion order. `{}` means an empty optional group. Every selected function
has an input object, including functions with no tunable inputs:

```json
"cross_type_networking_function": {"no_edges": {}}
```

`{"no_edges": {}}` selects one function with no configurable inputs; `{}` selects
no functions and is valid only for optional groups. Bare names and lists of names
are rejected. Migrate `"no_edges"` to `{"no_edges": {}}`, and a group such as
`["illegal_content"]` to `{"illegal_content": {}}`.

`user_quality_distribution` already identifies a fixed algorithm and retains
its flat `{ "w": ..., "lambda_slope": ... }` input object. Scoring components
retain their existing selector and explicit `importance_in` / `importance_out`
fields. Their shipped algorithms use empty input objects, for example
`"appeal_component_function": {"post_appeal": {}}`. A custom
component uses the same named input object in its selector field, for example
`"appeal_component_function": {"custom_appeal": {"scale": 0.5}}`.

## Validation

Every declared model input is required, including inputs for which a Python
function might have a default. Missing or unknown inputs fail validation.
Numeric inputs must be finite; valid zero values remain meaningful according
to the field's domain. Runtime context (the action, user, metrics, setup state
or progress callback) is supplied by the simulator, not by configuration.

Validation collects warnings without logging during module imports or config
reloads in the launcher and MPI ranks. The controller emits each warning once
per simulation at setup, after initializing the configured file logger. Each
warning appears once on stderr and once in `simsom.log`, without worker repeats.

A per-type `lurker_ratio` accepts the inclusive range `[0, 1]`: `1` means every
member of that type is a lurker. This is allowed for both populated and empty
types. Values outside that range still fail validation.

A user type with `num_nodes: 0` warns that its population is configured to zero
and no users will be created. A populated type whose rounded lurker count leaves
no active users gets a separate lurker-rounding warning. Either is allowed when
other types provide active users; a wholly inactive population still fails
validation and exits before setup. These are configuration diagnostics, with
no calibration requirement.

Custom functions with tunable inputs declare keyword-only parameters after
their runtime arguments. Register their names through the existing decorator.
Do not encode probability, standard deviation, threshold or other parameter
values in registered function names. Old numeric wrapper names are removed;
configs using them must migrate to their named-function input objects.

## Setup Functions

| Configuration location / function | Required inputs | Explanation and evidence |
|---|---|---|
| `edge_generation.within_type_networking_function.random_walk` | `seed_size`, `avg_n_friend`, `probability_follow` | [Random-walk algorithm](controller.md#random_walk). Defaults 3, 3 and 0.5 are model choices; no empirical calibration is recorded for these values. |
| `edge_generation.within_type_networking_function.density_uniform_sparse` | `density` | [Uniform network algorithm](controller.md#density_uniform_sparse). The historical 0.0003 preset is uncalibrated. |
| `edge_generation.cross_type_networking_function.power_law_by_followers_sparse` | `density`, `power_law_exponent` | [Cross-type algorithm](controller.md#power_law_by_followers_sparse). The historical 0.0003 and 1.0 presets are uncalibrated. |
| Either network phase: `no_edges` | None | [No-edge algorithm](controller.md#no_edges); deterministic, no calibration needed. |
| `activity_level_distribution.power_law_activity` | `power_law_exponent`, `minimum_activity_level`, `maximum_activity_level` | [Activity meaning](domain_activity.md#activity-level-meaning). Default min and max of 1 and 167 are calibrated from empirical data. |
| `user_quality_distribution` | `w`, `lambda_slope` | [User generation](controller.md#user-generation-math); calibration steps/report: [quality parameters notebook](../../analyses/calibration/quality_params/empirical_parameters.ipynb)|
| `user_behavior.p_actions` | `[mean, std]` for every action type | [Initialization and calibrated report](domain_activity.md#action-probability-calibration). Explicit `[0,0]` disables a type; negative draws are clipped to zero. |
| `user_behavior.vector_profile_generation_function.constant_vector_profile` | `dimension`, `value` | Minimal wiring example that assigns the same vector to every user; not calibrated. |
| `user_behavior.vector_profile_generation_function.pca_7param_social_media_archive_profile` | `dimension` (10), `pc1.component1` and `pc1.component2` (`mu`, `sigma`, `weight`), `pc2_to_pc10` (`mu`, `sigma`) | Archive-calibrated profile model described in the [profile and post representation report](../../analyses/calibration/user/profile_post_representation/profile_post_representation_report.ipynb); PC1 is a Gaussian mixture and PC2-PC10 are independent shared Gaussians. |
| `user_behavior.vector_profile_generation_function.uniform_random_vector_profile` | `dimension`, `minimum`, `maximum` | Minimal bounded random-vector example; not calibrated. |

The normal-user lurker ratio comes from the
[lurker calibration report](../../analyses/calibration/user/lurker_ratio/README.md).
The bad-user zero-lurker preset has no empirical calibration. After existing
rounding, a wholly inactive population is an error and exits before setup;
individual inactive types produce warnings and are allowed when other types
provide active users.

## Content Functions

- `post_quality_distribution_function.gaussian_around_user_quality` and
  `post_appeal_distribution_function.gaussian_around_user_quality` require
  `stddev`. The [generation algorithm](simulation.md#quality-and-appeal-generation)
  clips each Gaussian draw to `[0,1]`. Quality's default 0.12 follows the
  [quality-variance notebook](../../analyses/calibration/quality_variance/variance.ipynb).
  Reusing that value for appeal is a modeling assumption, not a separate appeal
  calibration.
- `dummy_content_embedding` has no model inputs. It returns a placeholder
  vector and is uncalibrated; see [user activity](simulation.md#user-activity-model).
- `social_media_archive_content_embedding` takes the author's profile vector
  and configured `gen_gamma` parameters (`a`, `c`, `loc`, `scale`) from the
  [profile and post representation calibration report](../../analyses/calibration/user/profile_post_representation/profile_post_representation_report.ipynb).
  It samples cosine distances in `[0,2]` and preserves the author's vector
  norm. Setup creates users and network edges; runtime session action counts
  follow `actions_per_session_ratio`.
- Post labeling takes explicit probability and survival hours; see
  [labeling inputs and their evidence](policy.md#post-labeling-inputs).

## Policy And Analytics Functions

- [Policy rule inputs](policy.md#configured-rule-inputs) specify the quality
  threshold directly; legality is an input-free rule on stored labels.
- [Termination inputs](analytics.md#termination-inputs) specify the action
  threshold and EMA threshold/rho directly. These are experiment/convergence
  controls, not empirically calibrated population parameters.
- [Analytics output functions](analytics.md#configurable-outputs) accept named
  input objects for any custom keyword-only parameters. Empty groups are valid.
- The [scoring contract](domain_activity.md#weight-components) retains explicit
  importance weights. The shipped importance presets and inverse-rank recency
  are model choices with no empirical weight calibration recorded here.

## Documentation And Provenance

Important and config-visible functions link in their docstrings to specific
algorithm sections. Those sections explain behavior, units, bounds and default
choices, then link to calibration steps/reports, source code or GitHub rationale
when available. State explicitly when a parameter has no calibration; the
existence of a notebook alone does not prove it produced a particular preset.
Record reference-based presets separately from locally estimated parameters.
For the database event clock, see [clock calibration](database.md#timestamp-rule)
and the [calibration folder](../../analyses/calibration/clock/exponential/README.md).

## Combined Feed Ranking

Looked at the X algo and decided to combine eligible in/out-network candidates before ranking. Component
`importance_in` and `importance_out` select the exponents for each candidate's
source. The resulting scores determine the feed order and source mixture.
Inventory size factors limit retrieval before moderation.
See [the RecSys algorithm](simulation.md#recommendation-system-recsys-model)
for deduplication, global recency and truncation. These scoring controls are
model choices with no empirical calibration asserted.
