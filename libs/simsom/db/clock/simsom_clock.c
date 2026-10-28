/*
 * Copyright © 2026 The Trustees of Indiana University and contributors.
 * This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
 * You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
 * See the LICENSE file in the root of this repository for the full license terms.
 * https://creativecommons.org/licenses/by-nc/4.0/
 */

#include "postgres.h"
#include "fmgr.h"

#include "catalog/namespace.h"
#include "executor/spi.h"
#include "utils/builtins.h"
#include "utils/lsyscache.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

PG_MODULE_MAGIC;

PG_FUNCTION_INFO_V1(simsom_clock_exponential_lambda);
PG_FUNCTION_INFO_V1(simsom_clock_sample_delta);
PG_FUNCTION_INFO_V1(simsom_clock_nextval);

static bool random_seeded = false;

static void
seed_random_once(void)
{
    long seed;

    if (random_seeded)
        return;

    seed = (long) time(NULL) ^ ((long) getpid() << 16);
    srand48(seed);
    random_seeded = true;
}

static double
sample_open_unit_interval(void)
{
    double sample;

    seed_random_once();

    do
    {
        sample = drand48();
    } while (sample <= 0.0 || sample >= 1.0);

    return sample;
}

static int64
sample_exponential_delta(double lambda, int64 min_step_ms)
{
    double uniform_sample;
    double exponential_sample;
    int64 delta_ms;

    if (lambda <= 0.0)
        ereport(ERROR,
                (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                 errmsg("lambda must be greater than zero")));

    if (min_step_ms < 1)
        ereport(ERROR,
                (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                 errmsg("min_step_ms must be at least 1")));

    uniform_sample = sample_open_unit_interval();
    exponential_sample = -log(uniform_sample) / lambda;

    if (isnan(exponential_sample) || isinf(exponential_sample))
        ereport(ERROR,
                (errcode(ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE),
                 errmsg("exponential sample produced an invalid value")));

    if (exponential_sample > (double) PG_INT64_MAX)
        ereport(ERROR,
                (errcode(ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE),
                 errmsg("sampled delta exceeds bigint range")));

    delta_ms = (int64) exponential_sample;

    if (delta_ms < min_step_ms)
        delta_ms = min_step_ms;

    return delta_ms;
}

static int64
sample_delta_for_distribution(const char *distribution, double lambda, int64 min_step_ms)
{
    if (strcmp(distribution, "exponential") == 0)
        return sample_exponential_delta(lambda, min_step_ms);

    ereport(ERROR,
            (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
             errmsg("unsupported clock distribution \"%s\"", distribution)));
    return 0;
}

static Oid
resolve_clock_schema_oid(FunctionCallInfo fcinfo)
{
    Oid function_namespace_oid;

    function_namespace_oid = get_func_namespace(fcinfo->flinfo->fn_oid);
    if (!OidIsValid(function_namespace_oid))
        ereport(ERROR,
                (errcode(ERRCODE_UNDEFINED_SCHEMA),
                 errmsg("could not resolve schema for simsom_clock function")));

    return function_namespace_oid;
}

static int64
allocate_next_tick(Oid function_namespace_oid)
{
    char *schema_name;
    char *qualified_state_name;
    char *select_sql;
    char *update_sql;
    int spi_status;
    SPITupleTable *tuptable;
    TupleDesc state_desc;
    HeapTuple state_tuple;
    Datum raw_value;
    bool is_null;
    int64 current_tick_ms;
    int64 min_step_ms;
    int64 delta_ms;
    int64 next_tick_ms;
    double lambda;
    char *distribution;
    Oid arg_types[1];
    Datum arg_values[1];
    char arg_nulls[1];

    schema_name = get_namespace_name(function_namespace_oid);
    if (schema_name == NULL)
        ereport(ERROR,
                (errcode(ERRCODE_UNDEFINED_SCHEMA),
                 errmsg("could not resolve schema name for simsom_clock function")));

    qualified_state_name = quote_qualified_identifier(schema_name, "simsom_clock_state");
    select_sql = psprintf(
        "SELECT current_tick_ms, distribution, lambda, min_step_ms "
        "FROM %s "
        "WHERE singleton = true "
        "FOR UPDATE",
        qualified_state_name
    );

    spi_status = SPI_connect();
    if (spi_status != SPI_OK_CONNECT)
        ereport(ERROR,
                (errcode(ERRCODE_INTERNAL_ERROR),
                 errmsg("SPI_connect failed for simsom_clock allocation")));

    spi_status = SPI_execute(select_sql, false, 1);
    if (spi_status != SPI_OK_SELECT)
        ereport(ERROR,
                (errcode(ERRCODE_INTERNAL_ERROR),
                 errmsg("failed to read simsom_clock_state")));

    if (SPI_processed != 1)
        ereport(ERROR,
                (errcode(ERRCODE_NO_DATA_FOUND),
                 errmsg("expected exactly one simsom_clock_state row, found %lu",
                        (unsigned long) SPI_processed)));

    tuptable = SPI_tuptable;
    state_desc = tuptable->tupdesc;
    state_tuple = tuptable->vals[0];

    raw_value = SPI_getbinval(state_tuple, state_desc, 1, &is_null);
    if (is_null)
        ereport(ERROR,
                (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
                 errmsg("current_tick_ms must not be null")));
    current_tick_ms = DatumGetInt64(raw_value);

    raw_value = SPI_getbinval(state_tuple, state_desc, 2, &is_null);
    if (is_null)
        ereport(ERROR,
                (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
                 errmsg("distribution must not be null")));
    distribution = TextDatumGetCString(raw_value);

    raw_value = SPI_getbinval(state_tuple, state_desc, 3, &is_null);
    if (is_null)
        ereport(ERROR,
                (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
                 errmsg("lambda must not be null")));
    lambda = DatumGetFloat8(raw_value);

    raw_value = SPI_getbinval(state_tuple, state_desc, 4, &is_null);
    if (is_null)
        ereport(ERROR,
                (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
                 errmsg("min_step_ms must not be null")));
    min_step_ms = DatumGetInt64(raw_value);

    delta_ms = sample_delta_for_distribution(distribution, lambda, min_step_ms);

    if (current_tick_ms > PG_INT64_MAX - delta_ms)
        ereport(ERROR,
                (errcode(ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE),
                 errmsg("timestamp allocation exceeded bigint range")));

    next_tick_ms = current_tick_ms + delta_ms;

    update_sql = psprintf(
        "UPDATE %s "
        "SET current_tick_ms = $1, "
        "    updated_at = clock_timestamp() "
        "WHERE singleton = true",
        qualified_state_name
    );

    arg_types[0] = INT8OID;
    arg_values[0] = Int64GetDatum(next_tick_ms);
    arg_nulls[0] = ' ';

    spi_status = SPI_execute_with_args(
        update_sql,
        1,
        arg_types,
        arg_values,
        arg_nulls,
        false,
        0
    );
    if (spi_status != SPI_OK_UPDATE)
        ereport(ERROR,
                (errcode(ERRCODE_INTERNAL_ERROR),
                 errmsg("failed to update simsom_clock_state")));

    if (SPI_processed != 1)
        ereport(ERROR,
                (errcode(ERRCODE_INTERNAL_ERROR),
                 errmsg("expected exactly one simsom_clock_state row to update, updated %lu",
                        (unsigned long) SPI_processed)));

    spi_status = SPI_finish();
    if (spi_status != SPI_OK_FINISH)
        ereport(ERROR,
                (errcode(ERRCODE_INTERNAL_ERROR),
                 errmsg("SPI_finish failed for simsom_clock allocation")));

    return next_tick_ms;
}

Datum
simsom_clock_exponential_lambda(PG_FUNCTION_ARGS)
{
    int64 num_users = PG_GETARG_INT64(0);
    double slope = PG_GETARG_FLOAT8(1);
    double intercept = PG_GETARG_FLOAT8(2);
    double lambda;

    if (num_users <= 0)
        ereport(ERROR,
                (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                 errmsg("num_users must be greater than zero")));

    lambda = slope * (double) num_users + intercept;

    if (lambda <= 0.0 || isnan(lambda) || isinf(lambda))
        ereport(ERROR,
                (errcode(ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE),
                 errmsg("computed lambda must be finite and greater than zero")));

    PG_RETURN_FLOAT8(lambda);
}

Datum
simsom_clock_sample_delta(PG_FUNCTION_ARGS)
{
    text *distribution_text = PG_GETARG_TEXT_PP(0);
    double lambda = PG_GETARG_FLOAT8(1);
    int64 min_step_ms = PG_GETARG_INT64(2);
    char *distribution = text_to_cstring(distribution_text);
    int64 delta_ms;

    delta_ms = sample_delta_for_distribution(distribution, lambda, min_step_ms);
    PG_RETURN_INT64(delta_ms);
}

Datum
simsom_clock_nextval(PG_FUNCTION_ARGS)
{
    int64 next_tick_ms;

    next_tick_ms = allocate_next_tick(resolve_clock_schema_oid(fcinfo));
    PG_RETURN_INT64(next_tick_ms);
}
