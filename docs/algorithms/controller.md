<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# Controller Algorithm

The controller prepares the run, watches for the stop signal, and shuts the
simulation down cleanly.

`docs/algorithms/simulation.md` is the source of truth for the high-level
simulation algorithm. This document describes controller-owned responsibilities
and implementation-facing details.

## What It Does

- Plans the starting users and friend network.
- Recreates the run-owned database tables from the current schema definition.
- Loads the starting users and friend network into the database.
- Resets the shared stop flag before the run begins.
- Records the authoritative termination request after a lifecycle stop signal.
- Optionally saves a database dump at the end.
- Coordinates stop messages and completion acknowledgments with the other
  Message Passing Interface (MPI) ranks.

## Main Steps

- Initialize the configured file logger and emit the collected configuration
  warnings once. The launcher and other MPI ranks validate without emitting
  them; see [validation diagnostics](configuration.md#validation).
- Open the controller database connection.
- Prepare the database:
  - create the configured schema when it is missing;
  - drop and recreate SimSoM's run-owned tables with current constraints;
  - omit secondary indexes during initial loading;
  - reinstall the clock extension and configure the `timestamp` default.
- Plan users:
  - validate each user type and its exact count;
  - store shuffled type assignments as compact integer codes, or as an implicit
    range when only one type has users;
  - support a designed population scale of up to `1,000,000,000` users;
  - reject a population larger than `2,147,483,647` users, the PostgreSQL
    signed `INTEGER` maximum used for user identifiers.
- Load the initial state in one transaction:
  - sample profiles in bounded NumPy chunks and immediately send users through
    PostgreSQL `COPY`;
  - create directed friend links from synthetic generation or an imported
    network file;
  - send friend links through a separate PostgreSQL `COPY` stream;
  - build secondary indexes after all initial rows have been sent;
  - collect PostgreSQL planner statistics for users, friend links, and the
    action table;
  - reset lifecycle state to "not stopping";
  - configure and reset the database clock from the loaded user count.
- Wait for termination:
  - repeatedly read the shared lifecycle state;
  - treat analytics stop signals as the metric-based termination source;
  - write the stop request when a stop signal is present;
  - return the stop reason.
- Finish the run:
  - send `STOP_REQUEST` to workers, policy, and analytics;
  - wait for workers and policy to send `PROCESS_TERMINATED`;
  - wait for Analytics to send `ANALYTICS_READY_FOR_FINAL`;
  - send `FINAL_ANALYSIS` to Analytics;
  - wait for `FINAL_ANALYTICS_COMPLETE`;
  - dump the configured database schema when enabled. Exports omit source
    ownership and privilege statements so another PostgreSQL role can restore
    the data;
  - close the database connection.

`controller.final_analytics_timeout_seconds` limits each shutdown wait: first
for worker/policy termination and Analytics readiness, then again after
`FINAL_ANALYSIS` for final Analytics completion. It defaults to 60 seconds when
omitted and must be positive. If either phase times out, the controller does
not write the optional final database dump.

## Setup Progress

The controller recreates `setup_status.jsonl` beside the configured simulator
log for each run. The runner prints both paths when a run starts. Each line is
one complete, immediately flushed JSON record, so the file can be read as
history or followed with `tail -f output/logs/setup_status.jsonl` under the
default configuration.

Records are appended when a stage starts or completes, when its integer
percentage changes, and at least once every 10 seconds while a setup stage is
running. The heartbeat is scheduled every nine seconds to leave timing margin
under load. Every record includes `stage`, `status`, `update_type`, `sequence`,
`updated_at`, `elapsed_seconds`, and `stage_elapsed_seconds`. `update_type`
distinguishes stage starts, progress changes, heartbeats, completions, and
failures. Progress-bearing stages also include `progress_current`,
`progress_total`, `progress_percent`, and `progress_unit`.

Generated user and network loops send the current counter whenever a whole
percentage changes and otherwise about once per second at bounded count
checkpoints. Heartbeats therefore show the latest sampled count and a
percentage rounded to six decimal places, including progress well below one
percent on very large runs.

The reported stages are:

- `setup` for the run-level start, completion, or failure;
- `user_planning` for compact user-type assignment and shuffling;
- `user_generation` with generated and total user counts;
- `network_generation` for each phase, user type, and configured function,
  including current phase and total edge counts;
- `network_import` for a file-backed initial network;
- `database_schema` while schema and clock objects are prepared;
- `database_load` for the materialized small-data API; the production stream
  instead adds `rows_sent` to the current user or network stage;
- `database_indexes` while secondary indexes are built and PostgreSQL planner
  statistics are collected over the loaded rows.

One controller-owned sidecar process is the sole writer for the detailed setup
stream. `simsom.log` receives only one `setup_stage_started` entry when each
stage invocation begins; that entry includes the `setup_status.jsonl` path and
stage identifiers such as network phase, user type, and function. Progress,
heartbeat, completion, and failure events are not duplicated into
`simsom.log`.

Owning the heartbeat outside the controller interpreter keeps updates flowing
while setup is inside long native operations or under memory pressure. Worker,
Policy, and Analytics ranks wait on a nonblocking setup barrier without
emitting periodic waiting messages; they sleep between bounded barrier checks
instead of continuously occupying CPU. Analytics begins its normal database
reads after setup and is not used as a setup progress poller. Setup work loops
only send counters at bounded checkpoints, keeping progress overhead small.

User planning reports `user_type_assignment_and_shuffle`. User generation
reports `profile_generation_and_database_load` because each bounded profile
chunk is consumed directly by the database stream. Network stages likewise
include `rows_sent` while generated chunks enter PostgreSQL. A final partial
edge chunk can be sent just after its network stage completes; in that case the
following `database_indexes` start record contains the final cumulative
`rows_sent` value. This avoids reopening a completed stage or creating
competing generation and database-load records for the same streaming work.

## Bounded User Pipeline

The production controller uses a one-pass user stream. The older materialized
`SetupDbDataset` API remains available for small tests and direct callers, but
the simulator run path does not build a list containing every `UserRecord`.

The production path works as follows:

1. Read and validate every configured user type and exact `num_nodes` count.
2. If only one type has users, represent its IDs as the range `1..N` and store
   no assignment array.
3. Otherwise, allocate one integer code per user using the smallest of 8, 16,
   or 32 bits that can represent all configured type names.
4. Fill each 100,000-user assignment chunk by drawing its exact per-type counts
   from the remaining population with a multivariate hypergeometric draw, then
   shuffle that chunk. Across all chunks this is the same uniform random
   permutation of the configured type multiset as a full-array shuffle.
5. Build per-type user-ID indexes only for synthetic network generation. IDs
   use the smallest unsigned width that can hold the maximum configured ID:
   8, 16, 32, or 64 bits. One billion IDs use 32 bits. The PostgreSQL
   `INTEGER` storage limit is reached before 64-bit user IDs are needed.
6. Keep each compact array in memory when it is at most 256 MiB. Larger arrays
   use an automatically deleted temporary memory-mapped file, bounding Python
   heap use while retaining random access.
7. Process at most 100,000 users at a time. NumPy samples action settings,
   quality, active-user activity values, and configured vector representations
   in arrays instead of making one native random-number call per scalar.
8. Preserve each type's exact rounded lurker count with remaining-count
   hypergeometric sampling. This chooses a uniform subset chunk by chunk and
   avoids a set containing every lurker user ID.
9. Convert only the current chunk to `UserRecord` rows. The database writer
   consumes at most 10,000 rows at a time and sends them through one PostgreSQL
   `COPY` stream.
10. Discard each row chunk after it is sent. Keep only compact type membership
   needed by the following network stage.

This changes storage and execution, not the configured user model. Type totals,
the uniform type permutation, exact lurker totals, and all configured profile
distributions are preserved. Vectorized calls consume random numbers in a
different order than the old scalar loop. Random simulation output was already
not reproducible, so this is not a promised result-level compatibility change.
No Numba, Cython, compiler, or new runtime dependency is added; NumPy performs
the bulk random draws and inverse-distribution math in its compiled kernels.

## User Generation Math

- Let `N = sum(setup_db.user_types[*].num_nodes)`.
- The controller creates one user for each integer index `i` from `0` to
  `N - 1`.
- The user ID is generated as `user_id = i + 1`.
- User type is assigned from exact configured `num_nodes` counts and stored as
  `user.user_type`.
- `user_behavior.vector_profile_generation_function` generates one finite,
  non-empty vector per user chunk. The shipped constant and uniform functions
  are minimal examples with no empirical calibration; replace the selector and
  inputs with the calibrated vector-representation generator when that model is
  implemented.
- Each user type's `lurker_ratio` controls how many initialized users of that
  type receive `activity_level = 0.0`. It accepts `[0, 1]`, including an
  entirely lurker type. Reject only when the whole population has no active
  users after per-type rounding; inactive individual types receive a warning.
- Lurker identity is defined by `activity_level == 0.0`; there is no separate
  stored boolean.
- Active users receive `activity_level > 0.0`.
- Active activity level is sampled through the selected user type's
  `activity_level_distribution`.
- `power_law_activity` takes exponent and bounds explicitly from
  config. The min and max bounds are uncalibrated; see
  [setup function inputs and evidence](configuration.md#setup-functions).
- User quality is sampled during setup from the selected user type's
  `user_quality_distribution`.
- User quality sampling uses the fixed `two_cluster_inverse` distribution
  method on a `[0, 100]` scale; config provides only its parameters.
- For that distribution, `w` is the probability of drawing from the
  high-value cluster and `lambda_slope` controls truncated exponential
  steepness.
- The active default uses the quality calibration notebook's COVID-data
  scenario: `w = 0.85` and `lambda_slope = 0.1190`.
- Setup normalizes the draw to `[0, 1]` and stores only the scalar as
  `user_behavior.quality`.
- Setup does not persist the source distribution in `user_behavior`.
- Post-level quality and appeal are generated when new content actions are
  written. Contentless likes and reposts inherit parent metadata, as described
  in `docs/algorithms/simulation.md`.

## Action Settings Math

- The user behavior config provides global action settings:
  - each action type maps to `[mean, standard_deviation]`;
  - example: `"post": [mean, standard_deviation]`.
- Config keys should use canonical action types:
  - `post`;
  - `like`;
  - `repost`;
  - `reply`;
  - `quote`.
- `[0,0]` explicitly disables an action type; it is not sampled and remains zero.
- For every enabled type `a`, sample `Normal(mean_a, standard_deviation_a)` and
  clip negative draws to zero. If all enabled draws for a user are zero, use
  the configured means for that vector, then normalize enabled weights together.
- Store the resulting fixed probabilities under `user_behavior.p_actions`.
- Reject an all-disabled configuration. See the
  [calibration report, exact parameters and cutoff](domain_activity.md#action-probability-calibration).
- Because this uses NumPy runtime sampling, these per-user action settings are
  random unless explicit randomness control is added.

## Friend Network Algorithms

`setup_db.network_source` selects either synthetic generation or file import.
Every stored link is directed: `(user_id, friend_id)` means `user_id` follows
`friend_id`; it does not imply the reverse link.

### Synthetic Execution Order And State

Synthetic setup requires a within-type and cross-type function for every user
type. Generation uses this exact order:

1. Run every user type's `within_type_networking_function` in configured
   user-type order.
2. Run every user type's `cross_type_networking_function` in the same order.
3. Within one function, process source users in ascending simulator user-ID
   order.
4. Record each yielded edge in shared generation state before requesting the
   next edge from the function.

The shared state always holds the cumulative edge count. It also holds one
`uint64` incoming-follower count per initialized user when any runnable
decorated function declares `requires_follower_counts=True`. Consequently, a
follower-weighted function sees all edges emitted by earlier phases, earlier
user types, and earlier source users. The follower-count array is not allocated
when the selected functions do not need it.

Density-based wrappers use the same target-count rule. For `C` valid candidates
and density `d`, define `x = C * d`. The requested edge count is:

```text
k = min(C, floor(x) + Bernoulli(x - floor(x)))
```

This is probabilistic rounding: it rounds up with probability equal to the
fractional part of `x`. For `0 <= d <= 1`, `E[k] = C * d`. A wrapper may instead
provide an integer `target_edge_count`, in which case `k` is that value capped
at `C`. Every implemented sampler chooses targets without replacement, so one
source cannot produce the same directed edge twice.

### `no_edges`

`no_edges` emits an empty iterator. It is valid for both network phases and
allocates no rule-specific network state.

### `density_uniform_sparse`

This within-type function requires `density` in configuration. The historical
0.0003 preset has no empirical calibration recorded. For a source type containing
`N_t` users, each source has `C = N_t - 1` candidates:

1. Exclude the source user's own local index.
2. Calculate `k` with the density target-count rule above.
3. Uniformly sample `k` distinct indexes from the remaining same-type users.
4. Emit one directed edge from the source to each selected target.

For `d <= 1`, the expected phase size is `d * N_t * (N_t - 1)`. The sparse path
uses rejection sampling over integer indexes and retains only the selected
indexes. When the requested sample is dense, it materializes the valid indexes
and uses uniform sampling without replacement because repeated rejection would
be slower. Both paths implement the same uniform target distribution.

Expected sparse runtime is `O(N_t + E_t)` and temporary selection memory is
`O(k)` per source, in addition to the compact user-ID index. The model's output
can still be quadratic: at `d = 0.0003`, one type with one million users has
about 300 million expected edges, and one type with one billion users has about
300 trillion expected edges.

### `random_walk`

The within-type function requires `seed_size`, `avg_n_friend`, and
`probability_follow` in configuration. The shipped values are 3, 3, and 0.5;
these are model choices with no empirical calibration recorded.
`avg_n_friend` is the fixed number of links created by each post-seed user; it
is not an average sampled at runtime. The configured seed size must be at least
the link count. The implementation is based on the *Random Walk Model* in
Section 5.5.3 of https://doi.org/10.1017/9781108653947.

For a source type with `N_t` users, let `s = min(N_t, seed_size)`, `m` be
`avg_n_friend`, and `p` be `probability_follow`. The helper performs these exact
steps:

1. If `N_t < 2`, emit no edges.
2. Create a fully connected directed seed among local positions `0..s-1`.
   Every ordered seed pair is emitted except self-pairs.
3. For each later source position `i`, choose one first target uniformly from
   the earlier positions `0..i-1`, emit that edge, and mark the target selected.
4. Draw `h ~ Binomial(m - 1, p)`. This is the number of the remaining link
   slots that attempt closure.
5. Read the first target's stored outgoing targets. Keep only earlier users and
   exclude any target already selected by the new source. Uniformly sample
   `min(h, available_closure_targets)` of these targets without replacement.
6. Fill every unfilled link slot by uniformly sampling distinct users from all
   earlier positions, excluding targets already selected by this source.
7. Emit the closure targets followed by the uniform fallback targets, and store
   all `m` target positions as this source's outgoing adjacency row.

The binomial draw is distributionally identical to performing `m - 1`
independent Bernoulli closure decisions because only the number of successful
decisions is used. A successful closure attempt that has no available neighbor
also becomes a uniform fallback. In this implementation, "random walk" is a
one-hop closure through the first target's outgoing neighbors; it does not
traverse a variable-length, multi-hop path.

The exact edge count is:

```text
0                                      when N_t < 2
N_t * (N_t - 1)                        when 2 <= N_t <= seed_size
seed_size * (seed_size - 1)
  + (N_t - seed_size) * avg_n_friend   when N_t > seed_size
```

For the default configuration this is `3 * N_t - 3` when `N_t >= 3`: 2,999,997
edges for one million users and 2,999,999,997 edges for one billion users.

The adjacency matrix has shape
`(N_t, max(avg_n_friend, s - 1))`. It stores one-based local target positions,
using zero as the empty sentinel and the smallest unsigned integer width that
can represent `N_t`. One type with one billion users therefore uses a
three-column `uint32` matrix of about 12 GiB. Only one user type's random-walk
matrix exists at a time. This is why one billion users is a design target
rather than a promise that every network configuration will fit on every
machine.

With fixed configured parameters, runtime and output are `O(N_t)`. More generally,
generation is `O(N_t * max(m, s))` and matrix memory is
`N_t * max(m, s - 1) * index_width_bytes`. The matrix is memory-mapped to a
temporary file above 256 MiB. This bounds Python heap use, but the file still
requires disk space and its operating-system page cache may appear as resident
memory under monitoring tools.

### `power_law_by_followers_sparse`

This cross-type function requires `density` and `power_law_exponent` in
configuration. The historical presets 0.0003 and 1.0 have no empirical
calibration recorded. For one source type, its candidate set is every
initialized user outside that type, concatenated in configured type order. Let
`F_j` be candidate `j`'s current incoming-follower count and `a` be the
exponent. Its weight is:

```text
w_j = (F_j + 1) ** a
```

The `+1` gives users with no followers a positive selection probability. For
each source user, the wrapper calculates `k` from the density target-count rule
and draws `k` candidates sequentially without replacement. At each draw,
candidate `j` has probability `w_j / sum(available weights)`. After the source's
selected edges are emitted, their follower counts and weights are increased
before the next source is sampled. Thus later source users preferentially
attach to targets that accumulated followers in all earlier generation work.
For `S` source users, `C` outside-type candidates, and `d <= 1`, the expected
phase size is `d * S * C`; it is quadratic when both groups grow proportionally.

A mutable Fenwick tree stores cumulative weights. Constructing candidate
weights and the tree costs `O(C)` per source type; drawing and updating `k`
targets costs `O(k log C)` per source. This replaces rebuilding a candidate
list and renormalizing all `C` weights for every source. Rule-specific memory is
the compact candidate-ID sequence plus two `float64` arrays of length `C` for
weights and the tree. The shared follower-count state adds one `uint64` value
per initialized user. Compact arrays above 256 MiB use temporary memory-mapped
files.

### File Import

File-backed setup does not execute synthetic functions. CSV, TSV, and JSON
Lines are parsed and yielded one edge at a time; JSON and GML use their current
whole-document parsers. Every endpoint must be an initialized user ID, and
self-links are rejected during parsing. The
`follower_network(user_id, friend_id)` primary key rejects duplicate directed
edges and rolls back the complete initial-state transaction on failure.

## Network Generation Optimizations And Effects

The production network path streams edges directly to PostgreSQL. It does not
retain every `FollowerNetworkRecord` or a Python set of all generated edges.
One PostgreSQL `COPY` command receives bounded chunks of at most 50,000 rows in
the same initial-state transaction as user loading.

| Optimization | Time or memory effect | Effect on model/output |
| --- | --- | --- |
| Stream user and edge rows through `COPY` | Bounds Python row-object memory; avoids per-row insert round trips. | Row content and setup transaction semantics are unchanged. |
| Build secondary indexes after initial `COPY` | Avoids maintaining those indexes for every loaded row. | Final indexes and query behavior are unchanged; constraints kept during load still validate rows. |
| Run PostgreSQL `ANALYZE` before releasing runtime processes | Gives runtime queries current row-count and column-distribution estimates instead of waiting for autovacuum. | Adds a bounded setup scan and can improve query plans; simulator data and model probabilities are unchanged. |
| Store random-walk local positions in a fixed-width matrix | Replaces Python adjacency sets and repeated user-ID prefix copies with contiguous 8/16/32/64-bit values. | Global IDs and target probabilities are unchanged after local positions are translated back. |
| Draw the random-walk closure count with one binomial call | Moves `m - 1` equivalent Bernoulli trials into NumPy and reduces Python-loop work. | Preserves the closure-count distribution, but consumes random values differently. |
| Use sparse index rejection sampling | Avoids constructing an `O(C)` candidate list when `k` is small. | Preserves uniform sampling without replacement, but may produce a different random realization. |
| Switch to a dense sampling path when rejection would be inefficient | Avoids excessive rejected draws when most candidates are requested or blocked. | Preserves uniform sampling without replacement. |
| Use a Fenwick tree for follower-weighted targets | Changes repeated `O(C)` normalization into `O(log C)` selection and update operations. | Preserves sequential weighted sampling without replacement. |
| Allocate follower counts from decorator metadata | Removes an otherwise unnecessary `8 * N` bytes when no selected rule needs incoming degree. | No effect on rules that do not read follower counts. |
| Use temporary memory maps above 256 MiB | Bounds Python heap allocation and permits arrays larger than available process heap. | Values are unchanged; disk I/O and page faults can make execution slower than RAM. |
| Omit a global synthetic-edge set | Avoids memory proportional to all emitted edges. | Safe for current functions because phase target scopes do not overlap and every per-source sampler is distinct; the database primary key remains the final guard. |

A global duplicate set is unnecessary for the current synthetic registry.
Within-type targets belong to the source's type, cross-type targets are outside
it, every function runs once per source type and phase, and per-source targets
are sampled without replacement. A future function that can revisit the same
source-target scope must own compact duplicate state or introduce an equivalent
guarantee.

These optimizations do not change the configured graph model, but they do not
make runs reproducible. Chunked type assignment, vectorized random draws,
binomial closure counts, and index-based samplers consume randomness differently
from scalar or materialized implementations. They preserve the stated
distributions and invariants, not the exact edge sequence for a previous seed
or implementation. SimSoM currently uses process-global random generators
without a distributed reproducibility contract.

Large runs remain bounded by requested output and persistent storage. A
fixed-degree random-walk network grows linearly. A fixed-density network grows
quadratically. Memory mapping prevents one large array from exhausting Python's
heap, but it does not eliminate temporary disk, database table, database index,
load-time, or simulation-time costs.

## Important Rules

- The controller does not send users or friend links through MPI.
- Postgres is the shared source of users, actions, friend links, lifecycle
  state, and simulator time.
- Workers, policy, and analytics read from the database after the controller
  prepares it.
- Analytics may publish stop signals. The controller records the authoritative
  first termination request with the observed reason and criteria atomically.
  Later requests cannot replace that decision. Workers may complete their selected
  batches afterward; final metrics are reported separately from the stop criteria.
- User loading, friend-link loading, secondary-index creation, lifecycle reset,
  and clock configuration share one transaction.
  They become durable together only when it commits.
- Users and friend links use PostgreSQL `COPY`. Reported counts are rows sent to
  the one `psql` process.

## Implemented In

- `libs/simsom/components/controller.py`
- `libs/simsom/db/controller.py`
- `libs/simsom/subcomponents/setup_db.py`
- `libs/simsom/db/core/schema.py`
