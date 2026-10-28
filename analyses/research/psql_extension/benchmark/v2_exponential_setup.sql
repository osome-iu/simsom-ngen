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

SELECT benchmark_clock_configure_exponential(
    p_num_users => :num_users,
    p_start_action_id => 0,
    p_start_tick_ms => 0
);

DROP TABLE IF EXISTS test_v2_exponential;

CREATE TABLE test_v2_exponential (
    action_id bigint PRIMARY KEY,
    timestamp bigint NOT NULL,
    payload bigint NOT NULL
);

CREATE TRIGGER benchmark_clock_fill_action_fields
BEFORE INSERT ON test_v2_exponential
FOR EACH ROW
EXECUTE FUNCTION benchmark_clock_assign_action_fields();
