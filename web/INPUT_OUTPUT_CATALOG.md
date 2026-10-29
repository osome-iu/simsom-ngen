# SimSoM Simulator Inputs and Outputs

**Scope:** The Python/MPI simulator only. React, Flask, Helios, browser
controls, API response shapes, and web downloads are intentionally excluded.

This catalog describes the simulator boundary as implemented in the current
repository: what can be supplied to it and what it writes or emits.

## Inputs

The simulator has three direct input mechanisms:

1. Command-line arguments.
2. A JSON or JSONC simulator configuration.
3. An optional follower-network file when file-based initialization is chosen.

PostgreSQL and MPI are runtime dependencies, but existing database rows are not
run inputs: the controller creates/resets the configured schema and builds a
new initial state at the start of a simulation.

### Command-line inputs

`libs/simsom/run_simulation.py` accepts:

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `-n`, `--num-procs` | Positive integer at least `worker.start_rank + 1` | `mpi.num_procs` | Total MPI process count |
| `--script` | Path/string | `libs/simsom/main.py` | Python MPI entry script |
| `--mpiexec` | Executable/string | `mpiexec` | MPI launcher |
| `--python-exec` | Executable/string | Current Python interpreter | Python used by each MPI rank |
| `--simulator_spec` | Path | `libs/simsom/config/default_simulator_config.jsonc` | Alternate JSON/JSONC configuration |

The MPI entry script, `libs/simsom/main.py`, directly accepts only
`--simulator_spec`. The runner rejects command strings containing its defined
unsafe shell fragments.

### Configuration inputs

#### Database and log destination

| Configuration path | Allowed value | Default |
|---|---|---|
| `database.dsn` | Non-empty PostgreSQL DSN, service, or database string | `postgres` |
| `database.schema` | Non-empty schema name | `simsom` |
| `database.clock_extension.name` | Non-empty extension name | `simsom_clock` |
| `system.log.file` | Non-empty filename | `simsom.log` |
| `system.log.path` | Non-empty absolute or working-directory-relative path | `logs` |

#### MPI process layout

| Configuration path | Allowed value | Default |
|---|---|---:|
| `mpi.num_procs` | Positive integer, at least `worker.start_rank + 1` | 8 |
| `mpi.processes.controller.rank` | Unique non-negative rank below the first worker | 0 |
| `mpi.processes.analytics.rank` | Unique non-negative rank below the first worker | 1 |
| `mpi.processes.policy.rank` | Unique non-negative rank below the first worker | 2 |
| `mpi.processes.worker.start_rank` | Non-negative first worker rank; all ranks from here upward are workers | 3 |

The three non-worker ranks must be unique. No configured non-worker rank may
overlap the worker range.

#### Controller

| Configuration path | Allowed value | Default |
|---|---|---|
| `controller.dump_on_termination` | Boolean | `true` |

#### Initial population and follower network

| Configuration path | Allowed value | Default |
|---|---|---|
| `setup_db.network_source` | `synthetic` or `file` | `synthetic` |
| `setup_db.network_file` | Non-empty path; used only for a `file` source | `./data/default_graph.gml` |
| `setup_db.user_types` | Non-empty object keyed by arbitrary user-type labels | `normal`, `bad` |

Every `setup_db.user_types.<type>` may contain:

| Configuration path under each type | Allowed value | Current choices/defaults |
|---|---|---|
| `num_nodes` | Non-negative integer; total across all types must be greater than zero | Normal 10,000; bad 0 |
| `lurker_ratio` | Number satisfying 0 ≤ value < 1 | 0.169 |
| `edge_generation.within_type_networking_function` | Registered within-type function | `density_uniform_sparse` or `no_edges`; default `density_uniform_sparse` |
| `edge_generation.cross_type_networking_function` | Registered cross-type function | `power_law_by_followers_sparse` or `no_edges`; normal defaults to power law, bad to no edges |
| `activity_level_distribution.activity_level_distribution_function` | Registered activity function | `power_law_activity_uncalibrated` |
| `user_quality_distribution.w` | Number from 0 through 1 | Normal 0.85; bad 0.15 |
| `user_quality_distribution.lambda_slope` | Number greater than zero | 0.119 |
| `post_quality_distribution_function` | Registered post-property generator | `gaussian_around_user_quality_stddev0_12` |
| `post_appeal_distribution_function` | Registered post-property generator | `gaussian_around_user_quality_stddev0_12` |

The named wrapper functions use code-owned constants rather than additional
configuration parameters:

- `density_uniform_sparse`: density 0.0003.
- `power_law_by_followers_sparse`: density 0.0003 and exponent 1.0.
- `power_law_activity_uncalibrated`: exponent 2.0, minimum activity 0.1,
  maximum activity 10.0.
- `gaussian_around_user_quality_stddev0_12`: standard deviation 0.12.

#### Worker behavior

| Configuration path | Allowed value | Default |
|---|---|---:|
| `worker.batch_size` | Positive integer | 30 |
| `worker.idle_sleep_seconds` | Non-negative number | 0.25 |
| `worker.action_insert_chunk_size` | Positive integer | 100 |

#### Recommendation system

| Configuration path | Allowed value | Default |
|---|---|---:|
| `recsys.feed_size` | Non-negative integer | 15 |
| `recsys.inventory_size.in_network` | Positive integer at least `feed_size` | 500 |
| `recsys.inventory_size.out_network` | Positive integer at least `feed_size` | 500 |
| `recsys.in_network_ratio` | Number from 0 through 1 | 0.8 |
| `recsys.engagement_action_lookback_population_multiplier` | Positive number | 10 |
| `recsys.feed_weight_components` | Non-empty component list described below | Seven default components |

Every feed-weight component has a non-negative `importance_in` and
`importance_out` plus exactly one selector/function pair:

| Selector | Possible function names |
|---|---|
| `appeal_component_function` | `post_appeal` |
| `recency_component_function` | `inverse_rank_recency` |
| `relevance_component_function` | `profile_vector_n_content_embedding_cosine_relevance` |
| `engagement_component_function` | `global_like_count`, `global_repost_count`, `global_reply_count`, `global_quote_count` |

The default recommendation list uses all seven functions with both weights set
to 1.0.

#### User action behavior

| Configuration path | Allowed value | Default |
|---|---|---|
| `user_behavior.content_embedding_generation_function` | `dummy_content_embedding` | `dummy_content_embedding` |
| `user_behavior.feed_weight_components` | Non-empty component list using the same selectors, functions, and weights listed above | Appeal, relevance, and four engagement components; all weights 1.0 |

`user_behavior.p_actions` contains a `[mean, standard deviation]` pair for each
supported action. Both numbers must be non-negative. These distributions are
sampled to construct each user's behavior; they are not fixed final action
percentages.

| Action configuration | Default mean | Default standard deviation |
|---|---:|---:|
| `user_behavior.p_actions.like` | 0.5948064801961054 | 0.3910461987769532 |
| `user_behavior.p_actions.post` | 0.13031231920499675 | 0.29161211125864744 |
| `user_behavior.p_actions.repost` | 0.06976972822176652 | 0.16523969585826476 |
| `user_behavior.p_actions.reply` | 0.05636377935996886 | 0.15503933993935257 |
| `user_behavior.p_actions.quote` | 0.008941192648479866 | 0.059042332347003214 |

#### Policy process

| Configuration path | Allowed value | Default |
|---|---|---:|
| `policy.enabled` | Boolean | `true` |
| `policy.batch_size` | `-1` for all unprocessed actions, or a positive integer | -1 |
| `policy.poll_interval_seconds` | Non-negative number | 2.0 |
| `policy.quality_threshold` | Number from 0 through 1 | 0.3 |

The current policy rule marks content whose generated quality is below the
threshold as `removed` for `below_quality_threshold`.

#### Analytics and termination

| Configuration path | Allowed value | Default |
|---|---|---|
| `analytics.poll_interval_seconds` | Non-negative number | 5.0 |
| `termination.stopping_criteria_functions` | List containing the registered function `max_avg_actions_per_user_3`, or an empty list | Function enabled |
| `termination.convergence_criteria_functions` | List containing `ema_avg_action_quality_threshold0_01_rho0_8`, or an empty list | Function enabled |

At least one stopping or convergence list must be non-empty. The current
ordinary rule stops at three average actions per initialized user. The current
convergence rule uses an average-quality EMA with rho 0.8 and stops when its
relative change is no more than 0.01.

### File-based follower-network input

When `setup_db.network_source` is `file`, the simulator reads directed
`user_id -> friend_id` edges. IDs must be positive configured simulator user
IDs. Unknown users, self-links, and duplicate directed edges are rejected.

| Extension | Accepted structure |
|---|---|
| `.csv`, `.tsv` | Header row. Source column may be `user_id`, `source`, `src`, or `from`; target may be `friend_id`, `target`, `dst`, or `to`. |
| `.json` | A list of edge objects/two-item arrays, or an object with an `edges` list. Objects accept the aliases above. |
| `.jsonl` | One edge object or two-item array per non-empty line. |
| `.gml` | Numeric `source` and `target` in each `edge [...]` block. IDs are used directly, without node-label or zero-based-ID remapping. |

### Implicit runtime inputs

The simulator requires:

- A reachable PostgreSQL server and authentication accepted by the configured
  DSN.
- PostgreSQL command-line programs, including `psql` and `pg_dump`.
- An MPI installation and `mpi4py`.
- The simulator clock extension build/install prerequisites.
- The Python packages imported by the simulator.
- Process environment such as `PATH`, locale, PostgreSQL service/authentication
  variables, and MPI variables.

Random draws affect user-type placement, lurker selection, activity, quality,
per-user action behavior, synthetic edges, viewed feed size, feed/action
selection, and content quality/appeal. There is no configurable random seed in
the current simulator.

## Outputs

The simulator has four output mechanisms:

1. Structured PostgreSQL state.
2. Periodic structured analytics in PostgreSQL.
3. A simulator log plus small stdout/stderr status messages.
4. An optional SQL dump at termination.

### PostgreSQL user output

The `user` table contains one row per generated user:

| Column | Output |
|---|---|
| `user_id` | Positive generated integer ID |
| `vector_profile` | Numeric profile vector; the current generator writes `[1.0]` |
| `activity_level` | Sampled activity in 0.1–10.0 for active users, or exactly 0 for configured lurkers |
| `user_type` | User-type label selected from the configuration |
| `user_behavior` | JSON containing sampled `p_actions` values and sampled `quality` |
| `moderation_actions` | Extensible user-moderation JSON; currently null for generated users |
| `other_properties` | Extensible JSON object; currently empty for generated users |

### PostgreSQL follower-network output

The `follower_network` table contains:

| Column | Output |
|---|---|
| `user_id` | Source user |
| `friend_id` | User followed/added by the source user |

These rows are either generated by the configured synthetic functions or
validated and copied from the network input file.

### PostgreSQL action output

The supported action types are `post`, `reply`, `quote`, `like`, and `repost`.
`post`, `reply`, and `quote` are content-bearing; `like` and `repost` are
non-content engagement actions.

The `action` table contains:

| Column | Output |
|---|---|
| `action_id` | Database-generated positive identity |
| `creator_user_id` | Acting user |
| `timestamp` | Simulator-clock integer tick assigned by PostgreSQL |
| `action_type` | One of the five supported types |
| `parent_action_id` | Referenced parent action, or null for a root post |
| `content_embedding` | Generated numeric vector for content actions; currently `[1.0]`; empty for non-content actions |
| `moderation_actions` | JSON policy/worker annotations |
| `repeated_like_n_reshare_creator_count` | Sequence count for repeated likes/reposts by the same actor against the same parent; zero for other cases |
| `views_by_others_count` | Aggregated feed views by users other than the creator |
| `other_properties` | JSON containing the generating `session_id`; content actions also contain generated `quality` and `appeal` |

Possible current moderation annotations are:

- Initial `policy_reviewed: false` on generated actions.
- `intervention_type: removed` with `violation_type:
  below_quality_threshold` when the policy removes low-quality content.
- `intervention_type: delete` with a null violation when an older duplicate
  like/repost is superseded. These deleted/removed rows remain stored but are
  excluded from feed inventories.

### PostgreSQL lifecycle output

The singleton `simulation_lifecycle` row contains:

| Column | Output |
|---|---|
| `lifecycle_id` | Constant singleton ID 1 |
| `started_at` | Run initialization time |
| `should_terminate` | Shared final termination flag |
| `reason` | Final controller termination reason |
| `requested_at` | Time termination was requested |
| `analytics_stop_requested` | Whether analytics requested a stop |
| `analytics_stop_reason` | Analytics stop reason |
| `analytics_stop_updated_at` | Time the analytics stop signal changed |
| `analytics_converged` | Whether analytics reported convergence |
| `analytics_convergence_reason` | Convergence explanation |
| `analytics_convergence_updated_at` | Time the convergence signal changed |
| `policy_cursor_action_id` | Highest action ID processed by policy |
| `policy_cursor_updated_at` | Time the policy cursor changed |
| `updated_at` | General lifecycle update time |

### PostgreSQL analytics output

The simulator periodically appends to `analytics_summary`:

| Column | Output |
|---|---|
| `analytics_summary_id` | Database-generated snapshot ID |
| `recorded_at` | Snapshot time |
| `user_count` | Total initialized users |
| `action_count` | Total stored actions |
| `avg_activity_level` | Average activity across users |
| `avg_action_quality` | Average quality across actions carrying quality |
| `converged` | Whether the configured convergence rule was met |
| `convergence_reason` | Explanation when converged |
| `other_properties` | Nested metrics below |

Every snapshot's `other_properties` may contain:

| Path | Output |
|---|---|
| `avg_actions_per_user` | Actions divided by initialized users |
| `action_type_ratios.<type>` | Fraction of actions for each action type present |
| `repost_stats.repost_action_ratio` | Reposts divided by all actions |
| `repost_stats.repeated_repost_ratio` | Repeated same-actor/same-parent reposts divided by reposts |
| `repost_stats.self_repost_ratio` | Reposts of one's own parent action divided by reposts |
| `repost_stats.self_repeated_repost_ratio` | Reposts that are both self and repeated divided by reposts |
| `policy_stats.policy_coverage` | Eligible actions reached by the policy cursor divided by eligible actions in the analytics window |
| `policy_stats.policy_intervention_ratio` | Intervened cursor-covered actions divided by cursor-covered eligible actions |
| `network_stats.avg_friends_per_user` | Average outgoing follower-network links |
| `network_stats.avg_followers_per_user` | Average incoming follower-network links |
| `timing.inter_activity_ms.min` | Minimum interval between ordered action timestamps |
| `timing.inter_activity_ms.mean` | Mean interval |
| `timing.inter_activity_ms.p50` | Median interval |
| `timing.inter_activity_ms.p90` | 90th-percentile interval |
| `timing.inter_activity_ms.p95` | 95th-percentile interval |
| `timing.inter_activity_ms.p99` | 99th-percentile interval |
| `timing.inter_activity_ms.max` | Maximum interval |
| `runtime.software_runtime_seconds` | Wall-clock seconds since lifecycle start |
| `runtime.simulated_runtime_seconds` | Difference between first and last simulated action timestamps, in seconds |
| `stopping_criteria_functions.stop_requested` | Whether a configured stop or convergence rule fired |
| `stopping_criteria_functions.stop_reason` | Applicable stop reason |
| `ema_avg_action_quality_threshold0_01_rho0_8.function` | Convergence function name |
| `ema_avg_action_quality_threshold0_01_rho0_8.threshold` | 0.01 |
| `ema_avg_action_quality_threshold0_01_rho0_8.rho` | 0.8 |
| `ema_avg_action_quality_threshold0_01_rho0_8.avg_action_quality` | Current average-quality input |
| `ema_avg_action_quality_threshold0_01_rho0_8.previous_ema` | EMA before an update, when available |
| `ema_avg_action_quality_threshold0_01_rho0_8.ema` | Current/updated EMA |
| `ema_avg_action_quality_threshold0_01_rho0_8.quality_diff` | Relative EMA change, or null while waiting |
| `ema_avg_action_quality_threshold0_01_rho0_8.status` | `waiting_for_quality`, `waiting_for_new_actions`, `updated`, or `converged` |

The ratios created by the action, repost, and policy SQL are stored as decimal
strings (or null); the other listed measurements are JSON numbers, strings,
booleans, or null as appropriate.

### PostgreSQL schema metadata output

The `schema_migration` table contains `version`, `description`, and
`applied_at`. The simulator also creates/updates database indexes, identity
sequences, the configured clock extension, and its clock function/state. These
are simulator artifacts, but not analytical results.

### Log and process output

The configured log file (default `logs/simsom.log`) receives timestamped INFO
and error records from all MPI roles, including:

- Configuration and launch command.
- Database/clock setup and initial user/follower counts.
- Controller, worker, analytics, and policy lifecycle messages.
- Periodic analytics reports as JSON-like structured log payloads.
- Stop/convergence reasons, final analytics, dump activity, and failures.

The runner also writes the log path and final success/failure status to
stdout/stderr. MPI/Python dependencies may emit their own diagnostics there.

### Optional SQL dump

When `controller.dump_on_termination` is true, the controller runs `pg_dump`
after the final analytics snapshot and writes:

```text
<configured-schema>_dump_YYYYMMDD_HHMMSS.sql
```

The file is created in the simulator's current working directory and contains
the configured PostgreSQL schema and its final state.

## Not simulator inputs or outputs

The simulator itself does not accept browser filters, graph layout controls,
API query limits, export-format choices, or React form state. It also does not
currently accept a random seed, free-text post content, images/video,
usernames, authentication data, run names, or comparison/history settings.
