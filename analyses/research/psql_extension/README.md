<!--
Copyright © 2026 The Trustees of Indiana University and contributors.
This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
See the LICENSE file in the root of this repository for the full license terms.
https://creativecommons.org/licenses/by-nc/4.0/
-->

# NOTES

This whole thing was vibe-coded, so I don't even know if it works. - Ben

# psql_extension

Prototype PostgreSQL extension for a global simulated clock built with `PGXS`.

The extension creates named clocks whose values are stored as `bigint` simulated
milliseconds since the start of a simulation. Each call to `sim_clock_nextval`
advances a clock by a randomly sampled step from a normal distribution.

## What this prototype provides

- `sim_clock_create(...)` to create a named global clock
- `sim_clock_nextval(clock_name)` to advance and return the next simulated tick
- `sim_clock_peek(clock_name)` to inspect the current value
- `sim_clock_set_distribution(...)` to change mean/stddev/minimum step size
- `psql_normal_step(...)` as the underlying C helper that samples the next step

## Prerequisites

You need PostgreSQL server development packages so `pg_config` and the extension headers are available.

Examples:

```bash
# Debian/Ubuntu
sudo apt install postgresql-server-dev-all

# RHEL/CentOS/Fedora
sudo dnf install postgresql-devel
```

## Build

```bash
make
make install
```

## Enable in a database

```sql
create extension psql_extension;
```

## Example usage

Create a global clock:

```sql
select sim_clock_create(
    clock_name => 'global_social_clock',
    start_value => 0,
    mean_step => 250,
    stddev_step => 40,
    min_step => 1
);
```

Use it during inserts:

```sql
create table posts (
    post_id bigint generated always as identity primary key,
    simulated_ms bigint not null default sim_clock_nextval('global_social_clock'),
    user_id bigint not null,
    body text not null
);

insert into posts (user_id, body) values (42, 'hello world');
```

## Distribution notes

- `mean_step` is the average number of simulated milliseconds added per event
- `stddev_step` controls how spread out the step sizes are
- `min_step` clamps the sampled step so the clock always moves forward

This is a normal (gaussian) distribution prototype, implemented with the
Box-Muller transform in C.

## Prototype limitations

- This uses a single row update per generated value, so it proves the concept but
  is not the final high-throughput design for billions of inserts.
- The normal distribution is truncated at `min_step`, which slightly distorts the
  lower tail.
- The clock value is a logical simulation tick, not a wall-clock timestamp.

## Likely production direction

If the proof-of-concept is accepted, a production version should switch to a
sequence-like allocator with backend-local block caching to reduce contention and
WAL overhead.
