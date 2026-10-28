-- Copyright © 2026 The Trustees of Indiana University and contributors.
-- This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
-- You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
-- See the LICENSE file in the root of this repository for the full license terms.
-- https://creativecommons.org/licenses/by-nc/4.0/

\set ON_ERROR_STOP on

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

DROP TABLE IF EXISTS test_v1_simple_math;

CREATE TABLE test_v1_simple_math (
    action_id bigint PRIMARY KEY,
    timestamp bigint NOT NULL,
    payload bigint NOT NULL
);

CREATE TRIGGER benchmark_clock_fill_action_fields
BEFORE INSERT ON test_v1_simple_math
FOR EACH ROW
EXECUTE FUNCTION benchmark_clock_assign_action_fields();
