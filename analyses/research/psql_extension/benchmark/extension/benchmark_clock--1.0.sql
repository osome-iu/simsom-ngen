-- Copyright © 2026 The Trustees of Indiana University and contributors.
-- This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
-- You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
-- See the LICENSE file in the root of this repository for the full license terms.
-- https://creativecommons.org/licenses/by-nc/4.0/

CREATE TYPE benchmark_clock_allocation AS (
    action_id bigint,
    timestamp bigint
);

COMMENT ON TYPE benchmark_clock_allocation IS
'Atomic allocation pair for benchmark action rows.';

CREATE TABLE benchmark_clock_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    current_action_id bigint NOT NULL DEFAULT 0,
    current_tick_ms bigint NOT NULL DEFAULT 0,
    distribution text NOT NULL,
    fixed_step bigint NOT NULL DEFAULT 1,
    num_users bigint,
    lambda double precision NOT NULL DEFAULT 1,
    min_step_ms bigint NOT NULL DEFAULT 1,
    slope double precision,
    intercept double precision,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (current_action_id >= 0),
    CHECK (current_tick_ms >= 0),
    CHECK (fixed_step >= 1),
    CHECK (lambda > 0),
    CHECK (min_step_ms >= 1),
    CHECK (distribution IN ('fixed_step', 'exponential'))
);

COMMENT ON TABLE benchmark_clock_state IS
'Singleton benchmark clock state for DB-owned action_id and timestamp allocation.';

CREATE FUNCTION benchmark_clock_exponential_lambda(
    p_num_users bigint,
    p_slope double precision,
    p_intercept double precision
)
RETURNS double precision
AS 'MODULE_PATHNAME', 'benchmark_clock_exponential_lambda'
LANGUAGE C
STRICT
IMMUTABLE
PARALLEL SAFE;

COMMENT ON FUNCTION benchmark_clock_exponential_lambda(bigint, double precision, double precision) IS
'Computes the calibrated exponential lambda for the benchmark clock.';

CREATE FUNCTION benchmark_clock_sample_delta(
    p_distribution text,
    p_lambda double precision,
    p_min_step_ms bigint,
    p_fixed_step bigint
)
RETURNS bigint
AS 'MODULE_PATHNAME', 'benchmark_clock_sample_delta'
LANGUAGE C
STRICT
VOLATILE
PARALLEL RESTRICTED;

COMMENT ON FUNCTION benchmark_clock_sample_delta(text, double precision, bigint, bigint) IS
'Samples one benchmark delta in C for either fixed_step or exponential mode.';

CREATE FUNCTION benchmark_clock_next_allocation()
RETURNS benchmark_clock_allocation
AS 'MODULE_PATHNAME', 'benchmark_clock_next_allocation'
LANGUAGE C
VOLATILE
PARALLEL RESTRICTED;

COMMENT ON FUNCTION benchmark_clock_next_allocation() IS
'Atomically allocates the next action_id and timestamp pair.';

CREATE FUNCTION benchmark_clock_assign_action_fields()
RETURNS trigger
AS 'MODULE_PATHNAME', 'benchmark_clock_assign_action_fields'
LANGUAGE C
VOLATILE
PARALLEL RESTRICTED;

COMMENT ON FUNCTION benchmark_clock_assign_action_fields() IS
'Before-insert trigger that fills action_id and timestamp from one atomic benchmark allocation.';

CREATE FUNCTION benchmark_clock_configure_fixed_step(
    p_fixed_step bigint DEFAULT 1,
    p_start_action_id bigint DEFAULT 0,
    p_start_tick_ms bigint DEFAULT 0
)
RETURNS benchmark_clock_state
LANGUAGE sql
AS $$
    INSERT INTO benchmark_clock_state (
        singleton,
        current_action_id,
        current_tick_ms,
        distribution,
        fixed_step,
        num_users,
        lambda,
        min_step_ms,
        slope,
        intercept,
        updated_at
    )
    VALUES (
        true,
        p_start_action_id,
        p_start_tick_ms,
        'fixed_step',
        p_fixed_step,
        NULL,
        1,
        1,
        NULL,
        NULL,
        clock_timestamp()
    )
    ON CONFLICT (singleton) DO UPDATE
    SET current_action_id = EXCLUDED.current_action_id,
        current_tick_ms = EXCLUDED.current_tick_ms,
        distribution = EXCLUDED.distribution,
        fixed_step = EXCLUDED.fixed_step,
        num_users = EXCLUDED.num_users,
        lambda = EXCLUDED.lambda,
        min_step_ms = EXCLUDED.min_step_ms,
        slope = EXCLUDED.slope,
        intercept = EXCLUDED.intercept,
        updated_at = clock_timestamp()
    RETURNING *;
$$;

COMMENT ON FUNCTION benchmark_clock_configure_fixed_step(bigint, bigint, bigint) IS
'Creates or resets the singleton benchmark clock in fixed-step mode.';

CREATE FUNCTION benchmark_clock_configure_exponential(
    p_num_users bigint,
    p_start_action_id bigint DEFAULT 0,
    p_start_tick_ms bigint DEFAULT 0,
    p_min_step_ms bigint DEFAULT 1,
    p_slope double precision DEFAULT 9.586385781407468e-08,
    p_intercept double precision DEFAULT 3.907543315063294e-05
)
RETURNS benchmark_clock_state
LANGUAGE sql
AS $$
    INSERT INTO benchmark_clock_state (
        singleton,
        current_action_id,
        current_tick_ms,
        distribution,
        fixed_step,
        num_users,
        lambda,
        min_step_ms,
        slope,
        intercept,
        updated_at
    )
    VALUES (
        true,
        p_start_action_id,
        p_start_tick_ms,
        'exponential',
        1,
        p_num_users,
        benchmark_clock_exponential_lambda(p_num_users, p_slope, p_intercept),
        p_min_step_ms,
        p_slope,
        p_intercept,
        clock_timestamp()
    )
    ON CONFLICT (singleton) DO UPDATE
    SET current_action_id = EXCLUDED.current_action_id,
        current_tick_ms = EXCLUDED.current_tick_ms,
        distribution = EXCLUDED.distribution,
        fixed_step = EXCLUDED.fixed_step,
        num_users = EXCLUDED.num_users,
        lambda = EXCLUDED.lambda,
        min_step_ms = EXCLUDED.min_step_ms,
        slope = EXCLUDED.slope,
        intercept = EXCLUDED.intercept,
        updated_at = clock_timestamp()
    RETURNING *;
$$;

COMMENT ON FUNCTION benchmark_clock_configure_exponential(bigint, bigint, bigint, bigint, double precision, double precision) IS
'Creates or resets the singleton benchmark clock in exponential mode.';

CREATE FUNCTION benchmark_clock_reset(
    p_start_action_id bigint DEFAULT 0,
    p_start_tick_ms bigint DEFAULT 0
)
RETURNS benchmark_clock_state
LANGUAGE sql
AS $$
    UPDATE benchmark_clock_state
    SET current_action_id = p_start_action_id,
        current_tick_ms = p_start_tick_ms,
        updated_at = clock_timestamp()
    WHERE singleton = true
    RETURNING *;
$$;

COMMENT ON FUNCTION benchmark_clock_reset(bigint, bigint) IS
'Resets the current benchmark clock counters without changing the configured mode.';

CREATE FUNCTION benchmark_clock_current_action_id()
RETURNS bigint
LANGUAGE sql
STABLE
AS $$
    SELECT current_action_id
    FROM benchmark_clock_state
    WHERE singleton = true
$$;

COMMENT ON FUNCTION benchmark_clock_current_action_id() IS
'Returns the current benchmark action_id.';

CREATE FUNCTION benchmark_clock_current_tick_ms()
RETURNS bigint
LANGUAGE sql
STABLE
AS $$
    SELECT current_tick_ms
    FROM benchmark_clock_state
    WHERE singleton = true
$$;

COMMENT ON FUNCTION benchmark_clock_current_tick_ms() IS
'Returns the current benchmark timestamp tick.';
