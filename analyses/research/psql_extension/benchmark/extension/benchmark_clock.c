/*
 * Copyright © 2026 The Trustees of Indiana University and contributors.
 * This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
 * You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
 * See the LICENSE file in the root of this repository for the full license terms.
 * https://creativecommons.org/licenses/by-nc/4.0/
 */

#include "postgres.h"
#include "fmgr.h"

#include "access/htup_details.h"
#include "catalog/namespace.h"
#include "commands/trigger.h"
#include "executor/spi.h"
#include "funcapi.h"
#include "utils/builtins.h"
#include "utils/lsyscache.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

PG_MODULE_MAGIC;

PG_FUNCTION_INFO_V1(benchmark_clock_exponential_lambda);
PG_FUNCTION_INFO_V1(benchmark_clock_sample_delta);
PG_FUNCTION_INFO_V1(benchmark_clock_next_allocation);
PG_FUNCTION_INFO_V1(benchmark_clock_assign_action_fields);

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
sample_fixed_step_delta(int64 fixed_step)
{
    if (fixed_step < 1)
        ereport(ERROR,
                (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                 errmsg("fixed_step must be at least 1")));

    return fixed_step;
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
sample_delta_for_distribution(const char *distribution,
                              double lambda,
                              int64 min_step_ms,
                              int64 fixed_step)
{
    if (strcmp(distribution, "fixed_step") == 0)
        return sample_fixed_step_delta(fixed_step);

    if (strcmp(distribution, "exponential") == 0)
        return sample_exponential_delta(lambda, min_step_ms);

    ereport(ERROR,
            (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
             errmsg("unsupported benchmark clock distribution \"%s\"", distribution)));
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
                 errmsg("could not resolve schema for benchmark_clock function")));

    return function_namespace_oid;
}

static void
allocate_next_pair(Oid function_namespace_oid, int64 *next_action_id, int64 *next_tick_ms)
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
    int64 current_action_id;
    int64 current_tick_ms;
    int64 min_step_ms;
    int64 fixed_step;
    int64 delta_ms;
    double lambda;
    char *distribution;
    Oid arg_types[2];
    Datum arg_values[2];
    char arg_nulls[2];

    schema_name = get_namespace_name(function_namespace_oid);
    if (schema_name == NULL)
        ereport(ERROR,
                (errcode(ERRCODE_UNDEFINED_SCHEMA),
                 errmsg("could not resolve schema name for benchmark_clock function")));

    qualified_state_name = quote_qualified_identifier(schema_name, "benchmark_clock_state");
    select_sql = psprintf(
        "SELECT current_action_id, current_tick_ms, distribution, lambda, min_step_ms, fixed_step "
        "FROM %s "
        "WHERE singleton = true "
        "FOR UPDATE",
        qualified_state_name
    );

    spi_status = SPI_connect();
    if (spi_status != SPI_OK_CONNECT)
        ereport(ERROR,
                (errcode(ERRCODE_INTERNAL_ERROR),
                 errmsg("SPI_connect failed for benchmark_clock allocation")));

    spi_status = SPI_execute(select_sql, false, 1);
    if (spi_status != SPI_OK_SELECT)
        ereport(ERROR,
                (errcode(ERRCODE_INTERNAL_ERROR),
                 errmsg("failed to read benchmark_clock_state")));

    if (SPI_processed != 1)
        ereport(ERROR,
                (errcode(ERRCODE_NO_DATA_FOUND),
                 errmsg("expected exactly one benchmark_clock_state row, found %lu",
                        (unsigned long) SPI_processed)));

    tuptable = SPI_tuptable;
    state_desc = tuptable->tupdesc;
    state_tuple = tuptable->vals[0];

    raw_value = SPI_getbinval(state_tuple, state_desc, 1, &is_null);
    if (is_null)
        ereport(ERROR,
                (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
                 errmsg("current_action_id must not be null")));
    current_action_id = DatumGetInt64(raw_value);

    raw_value = SPI_getbinval(state_tuple, state_desc, 2, &is_null);
    if (is_null)
        ereport(ERROR,
                (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
                 errmsg("current_tick_ms must not be null")));
    current_tick_ms = DatumGetInt64(raw_value);

    raw_value = SPI_getbinval(state_tuple, state_desc, 3, &is_null);
    if (is_null)
        ereport(ERROR,
                (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
                 errmsg("distribution must not be null")));
    distribution = TextDatumGetCString(raw_value);

    raw_value = SPI_getbinval(state_tuple, state_desc, 4, &is_null);
    if (is_null)
        ereport(ERROR,
                (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
                 errmsg("lambda must not be null")));
    lambda = DatumGetFloat8(raw_value);

    raw_value = SPI_getbinval(state_tuple, state_desc, 5, &is_null);
    if (is_null)
        ereport(ERROR,
                (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
                 errmsg("min_step_ms must not be null")));
    min_step_ms = DatumGetInt64(raw_value);

    raw_value = SPI_getbinval(state_tuple, state_desc, 6, &is_null);
    if (is_null)
        ereport(ERROR,
                (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED),
                 errmsg("fixed_step must not be null")));
    fixed_step = DatumGetInt64(raw_value);

    delta_ms = sample_delta_for_distribution(distribution, lambda, min_step_ms, fixed_step);

    if (current_action_id == PG_INT64_MAX)
        ereport(ERROR,
                (errcode(ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE),
                 errmsg("action_id allocation exceeded bigint range")));

    if (current_tick_ms > PG_INT64_MAX - delta_ms)
        ereport(ERROR,
                (errcode(ERRCODE_NUMERIC_VALUE_OUT_OF_RANGE),
                 errmsg("timestamp allocation exceeded bigint range")));

    *next_action_id = current_action_id + 1;
    *next_tick_ms = current_tick_ms + delta_ms;

    update_sql = psprintf(
        "UPDATE %s "
        "SET current_action_id = $1, "
        "    current_tick_ms = $2, "
        "    updated_at = clock_timestamp() "
        "WHERE singleton = true",
        qualified_state_name
    );

    arg_types[0] = INT8OID;
    arg_types[1] = INT8OID;
    arg_values[0] = Int64GetDatum(*next_action_id);
    arg_values[1] = Int64GetDatum(*next_tick_ms);
    arg_nulls[0] = ' ';
    arg_nulls[1] = ' ';

    spi_status = SPI_execute_with_args(
        update_sql,
        2,
        arg_types,
        arg_values,
        arg_nulls,
        false,
        0
    );
    if (spi_status != SPI_OK_UPDATE)
        ereport(ERROR,
                (errcode(ERRCODE_INTERNAL_ERROR),
                 errmsg("failed to update benchmark_clock_state")));

    if (SPI_processed != 1)
        ereport(ERROR,
                (errcode(ERRCODE_INTERNAL_ERROR),
                 errmsg("expected exactly one benchmark_clock_state row to update, updated %lu",
                        (unsigned long) SPI_processed)));

    spi_status = SPI_finish();
    if (spi_status != SPI_OK_FINISH)
        ereport(ERROR,
                (errcode(ERRCODE_INTERNAL_ERROR),
                 errmsg("SPI_finish failed for benchmark_clock allocation")));
}

Datum
benchmark_clock_exponential_lambda(PG_FUNCTION_ARGS)
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
benchmark_clock_sample_delta(PG_FUNCTION_ARGS)
{
    text *distribution_text = PG_GETARG_TEXT_PP(0);
    double lambda = PG_GETARG_FLOAT8(1);
    int64 min_step_ms = PG_GETARG_INT64(2);
    int64 fixed_step = PG_GETARG_INT64(3);
    char *distribution = text_to_cstring(distribution_text);
    int64 delta_ms;

    delta_ms = sample_delta_for_distribution(distribution, lambda, min_step_ms, fixed_step);
    PG_RETURN_INT64(delta_ms);
}

Datum
benchmark_clock_next_allocation(PG_FUNCTION_ARGS)
{
    Oid function_namespace_oid;
    int64 next_action_id;
    int64 next_tick_ms;
    TupleDesc result_desc;
    HeapTuple result_tuple;
    Datum result_values[2];
    bool result_nulls[2];

    function_namespace_oid = resolve_clock_schema_oid(fcinfo);
    allocate_next_pair(function_namespace_oid, &next_action_id, &next_tick_ms);

    if (get_call_result_type(fcinfo, NULL, &result_desc) != TYPEFUNC_COMPOSITE)
        ereport(ERROR,
                (errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
                 errmsg("benchmark_clock_next_allocation must return a composite type")));

    result_desc = BlessTupleDesc(result_desc);
    result_values[0] = Int64GetDatum(next_action_id);
    result_values[1] = Int64GetDatum(next_tick_ms);
    result_nulls[0] = false;
    result_nulls[1] = false;
    result_tuple = heap_form_tuple(result_desc, result_values, result_nulls);

    PG_RETURN_DATUM(HeapTupleGetDatum(result_tuple));
}

Datum
benchmark_clock_assign_action_fields(PG_FUNCTION_ARGS)
{
    TriggerData *trigdata;
    HeapTuple source_tuple;
    HeapTuple result_tuple;
    TupleDesc tuple_desc;
    int natts;
    int action_id_attno;
    int timestamp_attno;
    Datum *values;
    bool *nulls;
    bool *replaces;
    int64 next_action_id;
    int64 next_tick_ms;

    if (!CALLED_AS_TRIGGER(fcinfo))
        ereport(ERROR,
                (errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
                 errmsg("benchmark_clock_assign_action_fields must be called as a trigger")));

    trigdata = (TriggerData *) fcinfo->context;
    if (!TRIGGER_FIRED_FOR_ROW(trigdata->tg_event) ||
        !TRIGGER_FIRED_BEFORE(trigdata->tg_event) ||
        !TRIGGER_FIRED_BY_INSERT(trigdata->tg_event))
        ereport(ERROR,
                (errcode(ERRCODE_E_R_I_E_TRIGGER_PROTOCOL_VIOLATED),
                 errmsg("benchmark_clock_assign_action_fields must be fired before row insert")));

    source_tuple = trigdata->tg_trigtuple;
    tuple_desc = trigdata->tg_relation->rd_att;
    natts = tuple_desc->natts;
    action_id_attno = SPI_fnumber(tuple_desc, "action_id");
    timestamp_attno = SPI_fnumber(tuple_desc, "timestamp");

    if (action_id_attno <= 0 || timestamp_attno <= 0)
        ereport(ERROR,
                (errcode(ERRCODE_UNDEFINED_COLUMN),
                 errmsg("trigger target must define bigint action_id and timestamp columns")));

    values = palloc0(sizeof(Datum) * natts);
    nulls = palloc0(sizeof(bool) * natts);
    replaces = palloc0(sizeof(bool) * natts);

    allocate_next_pair(resolve_clock_schema_oid(fcinfo), &next_action_id, &next_tick_ms);

    values[action_id_attno - 1] = Int64GetDatum(next_action_id);
    replaces[action_id_attno - 1] = true;
    nulls[action_id_attno - 1] = false;

    values[timestamp_attno - 1] = Int64GetDatum(next_tick_ms);
    replaces[timestamp_attno - 1] = true;
    nulls[timestamp_attno - 1] = false;

    result_tuple = heap_modify_tuple(source_tuple, tuple_desc, values, nulls, replaces);

    PG_RETURN_POINTER(result_tuple);
}
