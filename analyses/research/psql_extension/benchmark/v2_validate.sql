-- Copyright © 2026 The Trustees of Indiana University and contributors.
-- This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
-- You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
-- See the LICENSE file in the root of this repository for the full license terms.
-- https://creativecommons.org/licenses/by-nc/4.0/

\set ON_ERROR_STOP on

\if :{?table_name}
\else
\echo 'table_name is required'
\quit 1
\endif

SELECT
    count(*) AS row_count,
    min(action_id) AS min_action_id,
    max(action_id) AS max_action_id,
    min(timestamp) AS min_timestamp,
    max(timestamp) AS max_timestamp,
    bool_and(prev_action_id IS NULL OR action_id = prev_action_id + 1) AS action_ids_gapless,
    bool_and(prev_timestamp IS NULL OR timestamp > prev_timestamp) AS timestamps_strictly_increasing,
    min(delta_ms) AS min_delta_ms,
    max(delta_ms) AS max_delta_ms,
    bool_and(action_id = timestamp) AS action_and_timestamp_match
FROM (
    SELECT
        action_id,
        lag(action_id) OVER (ORDER BY action_id) AS prev_action_id,
        timestamp,
        lag(timestamp) OVER (ORDER BY action_id) AS prev_timestamp,
        (timestamp - lag(timestamp) OVER (ORDER BY action_id)) AS delta_ms
    FROM :table_name
) AS ordered_rows;
