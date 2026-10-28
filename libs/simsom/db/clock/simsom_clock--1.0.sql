-- Copyright © 2026 The Trustees of Indiana University and contributors.
-- This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
-- You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
-- See the LICENSE file in the root of this repository for the full license terms.
-- https://creativecommons.org/licenses/by-nc/4.0/

-- One singleton row is enough because only one hot simulator timestamp stream
-- is being modeled here. Every allocation serializes through this row.
CREATE TABLE simsom_clock_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    origin_timestamp timestamp without time zone NOT NULL,
    current_tick_ms bigint NOT NULL DEFAULT 0,
    distribution text NOT NULL,
    num_users bigint NOT NULL,
    lambda double precision NOT NULL,
    min_step_ms bigint NOT NULL DEFAULT 1,
    slope double precision NOT NULL,
    intercept double precision NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (current_tick_ms >= 0),
    CHECK (min_step_ms >= 1),
    CHECK (lambda > 0),
    CHECK (distribution IN ('exponential'))
);

COMMENT ON TABLE simsom_clock_state IS
'Singleton strict-order simulator clock state for timestamp allocation.';

COMMENT ON COLUMN simsom_clock_state.current_tick_ms IS
'Current logical simulator timestamp in milliseconds since origin_timestamp.';

COMMENT ON COLUMN simsom_clock_state.distribution IS
'Distribution identifier used by the C sampler dispatch.';

CREATE FUNCTION simsom_clock_exponential_lambda(
    p_num_users bigint,
    p_slope double precision,
    p_intercept double precision
)
RETURNS double precision
AS 'MODULE_PATHNAME', 'simsom_clock_exponential_lambda'
LANGUAGE C
STRICT
IMMUTABLE
PARALLEL SAFE;

COMMENT ON FUNCTION simsom_clock_exponential_lambda(bigint, double precision, double precision) IS
'Computes the calibrated exponential lambda from the Python clock manager parameters.';

CREATE FUNCTION simsom_clock_sample_delta(
    p_distribution text,
    p_lambda double precision,
    p_min_step_ms bigint
)
RETURNS bigint
AS 'MODULE_PATHNAME', 'simsom_clock_sample_delta'
LANGUAGE C
STRICT
VOLATILE
PARALLEL RESTRICTED;

COMMENT ON FUNCTION simsom_clock_sample_delta(text, double precision, bigint) IS
'Samples one logical millisecond delta in C for the configured clock distribution.';

CREATE FUNCTION simsom_clock_nextval()
RETURNS bigint
AS 'MODULE_PATHNAME', 'simsom_clock_nextval'
LANGUAGE C
VOLATILE
PARALLEL RESTRICTED;

COMMENT ON FUNCTION simsom_clock_nextval() IS
'Allocates the next simulator timestamp tick in milliseconds.';

CREATE FUNCTION simsom_clock_stamp_timestamp()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    -- Keep simulator timestamp ownership in Postgres even if callers try to
    -- provide their own value.
    NEW.timestamp := simsom_clock_nextval();
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION simsom_clock_stamp_timestamp() IS
'BEFORE INSERT trigger that stamps NEW.timestamp from the shared simulator clock.';

CREATE FUNCTION simsom_clock_configure_exponential(
    p_num_users bigint,
    p_origin_timestamp timestamp without time zone DEFAULT timestamp '2020-01-01 00:00:00',
    p_min_step_ms bigint DEFAULT 1,
    p_slope double precision DEFAULT 9.586385781407468e-08,
    p_intercept double precision DEFAULT 3.907543315063294e-05
)
RETURNS simsom_clock_state
LANGUAGE sql
AS $$
    INSERT INTO simsom_clock_state (
        singleton,
        origin_timestamp,
        current_tick_ms,
        distribution,
        num_users,
        lambda,
        min_step_ms,
        slope,
        intercept,
        updated_at
    )
    VALUES (
        true,
        p_origin_timestamp,
        0,
        'exponential',
        p_num_users,
        simsom_clock_exponential_lambda(p_num_users, p_slope, p_intercept),
        p_min_step_ms,
        p_slope,
        p_intercept,
        clock_timestamp()
    )
    ON CONFLICT (singleton) DO UPDATE
    SET origin_timestamp = EXCLUDED.origin_timestamp,
        current_tick_ms = 0,
        distribution = EXCLUDED.distribution,
        num_users = EXCLUDED.num_users,
        lambda = EXCLUDED.lambda,
        min_step_ms = EXCLUDED.min_step_ms,
        slope = EXCLUDED.slope,
        intercept = EXCLUDED.intercept,
        updated_at = clock_timestamp()
    RETURNING *;
$$;

COMMENT ON FUNCTION simsom_clock_configure_exponential(bigint, timestamp without time zone, bigint, double precision, double precision) IS
'Creates or resets the singleton clock so it matches the Python ExponentialClockManager calibration.';

CREATE FUNCTION simsom_clock_reset(
    p_origin_timestamp timestamp without time zone DEFAULT NULL
)
RETURNS simsom_clock_state
LANGUAGE sql
AS $$
    UPDATE simsom_clock_state
    SET origin_timestamp = COALESCE(p_origin_timestamp, origin_timestamp),
        current_tick_ms = 0,
        updated_at = clock_timestamp()
    WHERE singleton = true
    RETURNING *;
$$;

COMMENT ON FUNCTION simsom_clock_reset(timestamp without time zone) IS
'Resets the logical clock back to zero milliseconds since origin_timestamp.';

CREATE FUNCTION simsom_clock_current_tick_ms()
RETURNS bigint
LANGUAGE sql
STABLE
AS $$
    SELECT current_tick_ms
    FROM simsom_clock_state
    WHERE singleton = true
$$;

COMMENT ON FUNCTION simsom_clock_current_tick_ms() IS
'Returns the current logical simulator timestamp in milliseconds.';

CREATE FUNCTION simsom_clock_tick_to_timestamp(
    p_tick_ms bigint,
    p_origin_timestamp timestamp without time zone DEFAULT NULL
)
RETURNS timestamp without time zone
LANGUAGE sql
STABLE
AS $$
    SELECT
        COALESCE(
            p_origin_timestamp,
            (SELECT origin_timestamp
             FROM simsom_clock_state
             WHERE singleton = true)
        ) + ((p_tick_ms::double precision / 1000.0) * interval '1 second');
$$;

COMMENT ON FUNCTION simsom_clock_tick_to_timestamp(bigint, timestamp without time zone) IS
'Converts a logical bigint millisecond tick into a SQL timestamp using the configured origin.';

CREATE FUNCTION simsom_clock_current_timestamp()
RETURNS timestamp without time zone
LANGUAGE sql
STABLE
AS $$
    SELECT simsom_clock_tick_to_timestamp(current_tick_ms, origin_timestamp)
    FROM simsom_clock_state
    WHERE singleton = true
$$;

COMMENT ON FUNCTION simsom_clock_current_timestamp() IS
'Returns the current allocated simulator timestamp as a SQL timestamp.';
