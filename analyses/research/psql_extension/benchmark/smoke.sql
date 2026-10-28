-- Copyright © 2026 The Trustees of Indiana University and contributors.
-- This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
-- You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
-- See the LICENSE file in the root of this repository for the full license terms.
-- https://creativecommons.org/licenses/by-nc/4.0/

\set ON_ERROR_STOP on

\if :{?num_users}
\else
\set num_users 10000
\endif

DROP EXTENSION IF EXISTS benchmark_clock CASCADE;
CREATE EXTENSION benchmark_clock;

SELECT
    current_action_id,
    current_tick_ms,
    distribution,
    fixed_step
FROM benchmark_clock_configure_fixed_step(
    p_fixed_step => 1,
    p_start_action_id => 0,
    p_start_tick_ms => 0
);

DROP TABLE IF EXISTS smoke_fixed_rows;

CREATE TABLE smoke_fixed_rows (
    action_id bigint NOT NULL,
    timestamp bigint NOT NULL,
    payload bigint NOT NULL
);

CREATE TRIGGER benchmark_clock_fill_action_fields
BEFORE INSERT ON smoke_fixed_rows
FOR EACH ROW
EXECUTE FUNCTION benchmark_clock_assign_action_fields();

INSERT INTO smoke_fixed_rows (payload)
SELECT 0::bigint
FROM generate_series(1, 5);

SELECT
    count(*) AS row_count,
    bool_and(action_id = timestamp) AS action_and_timestamp_match
FROM smoke_fixed_rows;

SELECT
    current_action_id,
    current_tick_ms,
    distribution,
    lambda
FROM benchmark_clock_configure_exponential(
    p_num_users => :num_users,
    p_start_action_id => 0,
    p_start_tick_ms => 0
);

DROP TABLE IF EXISTS smoke_exponential_rows;

CREATE TABLE smoke_exponential_rows (
    action_id bigint NOT NULL,
    timestamp bigint NOT NULL,
    payload bigint NOT NULL
);

CREATE TRIGGER benchmark_clock_fill_action_fields_exp
BEFORE INSERT ON smoke_exponential_rows
FOR EACH ROW
EXECUTE FUNCTION benchmark_clock_assign_action_fields();

INSERT INTO smoke_exponential_rows (payload)
SELECT 0::bigint
FROM generate_series(1, 25);

SELECT action_id, timestamp
FROM smoke_exponential_rows
ORDER BY action_id
LIMIT 10;

SELECT
    count(*) AS row_count,
    bool_and(prev_action_id IS NULL OR action_id = prev_action_id + 1) AS action_ids_gapless,
    bool_and(prev_timestamp IS NULL OR timestamp > prev_timestamp) AS timestamps_strictly_increasing
FROM (
    SELECT
        action_id,
        lag(action_id) OVER (ORDER BY action_id) AS prev_action_id,
        timestamp,
        lag(timestamp) OVER (ORDER BY action_id) AS prev_timestamp
    FROM smoke_exponential_rows
) AS ordered_rows;

SELECT
    min(delta_ms) AS min_delta_ms,
    avg(delta_ms)::numeric(12, 3) AS avg_delta_ms,
    max(delta_ms) AS max_delta_ms
FROM (
    SELECT
        (timestamp - lag(timestamp) OVER (ORDER BY action_id)) AS delta_ms
    FROM smoke_exponential_rows
) AS deltas
WHERE delta_ms IS NOT NULL;
