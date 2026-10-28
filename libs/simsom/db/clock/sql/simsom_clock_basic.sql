-- Copyright © 2026 The Trustees of Indiana University and contributors.
-- This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
-- You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
-- See the LICENSE file in the root of this repository for the full license terms.
-- https://creativecommons.org/licenses/by-nc/4.0/

CREATE EXTENSION simsom_clock;

SELECT round(
    simsom_clock_exponential_lambda(
        10000,
        9.586385781407468e-08,
        3.907543315063294e-05
    )::numeric,
    12
);

SELECT current_tick_ms
FROM simsom_clock_configure_exponential(
    p_num_users => 10000,
    p_origin_timestamp => timestamp '2020-01-01 00:00:00'
);

CREATE TEMP TABLE simsom_clock_basic_rows (
    action_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    timestamp bigint NOT NULL DEFAULT simsom_clock_nextval(),
    payload bigint NOT NULL
);

INSERT INTO simsom_clock_basic_rows (payload)
SELECT 0::bigint
FROM generate_series(1, 25);

SELECT count(*) FROM simsom_clock_basic_rows;

SELECT bool_and(prev_action_id IS NULL OR action_id = prev_action_id + 1)
FROM (
    SELECT
        action_id,
        lag(action_id) OVER (ORDER BY action_id) AS prev_action_id
    FROM simsom_clock_basic_rows
) AS ordered_rows;

SELECT bool_and(prev_timestamp IS NULL OR timestamp > prev_timestamp)
FROM (
    SELECT
        timestamp,
        lag(timestamp) OVER (ORDER BY action_id) AS prev_timestamp
    FROM simsom_clock_basic_rows
) AS ordered_rows;

SELECT min(delta_ms) >= 1
FROM (
    SELECT
        (timestamp - lag(timestamp) OVER (ORDER BY action_id)) AS delta_ms
    FROM simsom_clock_basic_rows
) AS deltas
WHERE delta_ms IS NOT NULL;

SELECT simsom_clock_current_tick_ms() > 0;

SELECT simsom_clock_tick_to_timestamp(0, timestamp '2020-01-01 00:00:00');

CREATE TEMP TABLE simsom_clock_trigger_rows (
    action_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    timestamp bigint NOT NULL,
    payload bigint NOT NULL
);

CREATE TRIGGER simsom_clock_trigger_rows_stamp
BEFORE INSERT ON simsom_clock_trigger_rows
FOR EACH ROW
EXECUTE FUNCTION simsom_clock_stamp_timestamp();

INSERT INTO simsom_clock_trigger_rows (timestamp, payload)
SELECT -1, 0::bigint
FROM generate_series(1, 10);

SELECT count(*) FROM simsom_clock_trigger_rows;

SELECT bool_and(timestamp > 0) FROM simsom_clock_trigger_rows;

SELECT bool_and(prev_timestamp IS NULL OR timestamp > prev_timestamp)
FROM (
    SELECT
        timestamp,
        lag(timestamp) OVER (ORDER BY action_id) AS prev_timestamp
    FROM simsom_clock_trigger_rows
) AS ordered_rows;
