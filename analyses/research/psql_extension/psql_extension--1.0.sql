-- Copyright © 2026 The Trustees of Indiana University and contributors.
-- This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
-- You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
-- See the LICENSE file in the root of this repository for the full license terms.
-- https://creativecommons.org/licenses/by-nc/4.0/

CREATE TABLE psql_extension_clocks (
    name text PRIMARY KEY,
    current_value bigint NOT NULL,
    mean_step double precision NOT NULL,
    stddev_step double precision NOT NULL,
    min_step bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (min_step >= 1),
    CHECK (stddev_step >= 0)
);

COMMENT ON TABLE psql_extension_clocks IS
'Prototype global simulated clocks for psql_extension.';

CREATE FUNCTION psql_normal_step(
    p_mean_step double precision,
    p_stddev_step double precision,
    p_min_step bigint
)
RETURNS bigint
AS 'MODULE_PATHNAME', 'psql_normal_step'
LANGUAGE C
VOLATILE
STRICT
PARALLEL RESTRICTED;

COMMENT ON FUNCTION psql_normal_step(double precision, double precision, bigint) IS
'Samples a bigint step from a normal distribution and clamps it to min_step.';

CREATE FUNCTION sim_clock_create(
    p_clock_name text,
    p_start_value bigint DEFAULT 0,
    p_mean_step double precision DEFAULT 1000,
    p_stddev_step double precision DEFAULT 250,
    p_min_step bigint DEFAULT 1
)
RETURNS psql_extension_clocks
LANGUAGE plpgsql
AS $$
DECLARE
    created_clock psql_extension_clocks;
BEGIN
    INSERT INTO psql_extension_clocks (
        name,
        current_value,
        mean_step,
        stddev_step,
        min_step
    )
    VALUES (
        p_clock_name,
        p_start_value,
        p_mean_step,
        p_stddev_step,
        p_min_step
    )
    RETURNING * INTO created_clock;

    RETURN created_clock;
END;
$$;

COMMENT ON FUNCTION sim_clock_create(text, bigint, double precision, double precision, bigint) IS
'Creates a named global simulated clock.';

CREATE FUNCTION sim_clock_set_distribution(
    p_clock_name text,
    p_mean_step double precision,
    p_stddev_step double precision,
    p_min_step bigint DEFAULT 1
)
RETURNS psql_extension_clocks
LANGUAGE plpgsql
AS $$
DECLARE
    updated_clock psql_extension_clocks;
BEGIN
    UPDATE psql_extension_clocks
    SET mean_step = p_mean_step,
        stddev_step = p_stddev_step,
        min_step = p_min_step,
        updated_at = now()
    WHERE name = p_clock_name
    RETURNING * INTO updated_clock;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'clock "%" does not exist', p_clock_name;
    END IF;

    RETURN updated_clock;
END;
$$;

COMMENT ON FUNCTION sim_clock_set_distribution(text, double precision, double precision, bigint) IS
'Updates the normal-distribution parameters for a named clock.';

CREATE FUNCTION sim_clock_nextval(p_clock_name text)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    next_value bigint;
BEGIN
    UPDATE psql_extension_clocks
    SET current_value = current_value
            + psql_normal_step(mean_step, stddev_step, min_step),
        updated_at = now()
    WHERE name = p_clock_name
    RETURNING current_value INTO next_value;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'clock "%" does not exist', p_clock_name;
    END IF;

    RETURN next_value;
END;
$$;

COMMENT ON FUNCTION sim_clock_nextval(text) IS
'Advances a named global simulated clock and returns the new bigint tick value.';

CREATE FUNCTION sim_clock_peek(p_clock_name text)
RETURNS bigint
LANGUAGE sql
STABLE
AS $$
    SELECT current_value
    FROM psql_extension_clocks
    WHERE name = $1
$$;

COMMENT ON FUNCTION sim_clock_peek(text) IS
'Returns the current bigint value of a named simulated clock without advancing it.';
