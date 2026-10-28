/*
 * Copyright © 2026 The Trustees of Indiana University and contributors.
 * This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
 * You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
 * See the LICENSE file in the root of this repository for the full license terms.
 * https://creativecommons.org/licenses/by-nc/4.0/
 */

#include "postgres.h"
#include "fmgr.h"

#include <math.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

PG_MODULE_MAGIC;

PG_FUNCTION_INFO_V1(psql_normal_step);

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

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

Datum
psql_normal_step(PG_FUNCTION_ARGS)
{
    double mean = PG_GETARG_FLOAT8(0);
    double stddev = PG_GETARG_FLOAT8(1);
    int64 min_step = PG_GETARG_INT64(2);
    double u1;
    double u2;
    double z0;
    double sample;
    int64 step;

    if (stddev < 0.0)
        ereport(ERROR,
                (errmsg("stddev must be non-negative")));

    if (min_step < 1)
        ereport(ERROR,
                (errmsg("min_step must be at least 1")));

    seed_random_once();

    /*
     * Box-Muller transform for a normally distributed value.
     * u1 must be strictly greater than zero to avoid log(0).
     */
    do
    {
        u1 = drand48();
    } while (u1 <= 0.0);

    u2 = drand48();
    z0 = sqrt(-2.0 * log(u1)) * cos(2.0 * M_PI * u2);
    sample = mean + (stddev * z0);

    if (isnan(sample) || isinf(sample))
        ereport(ERROR,
                (errmsg("normal sample produced an invalid value")));

    if (sample < (double) min_step)
        sample = (double) min_step;

    if (sample > (double) PG_INT64_MAX)
        ereport(ERROR,
                (errmsg("sampled step exceeds bigint range")));

    step = (int64) llround(sample);

    if (step < min_step)
        step = min_step;

    PG_RETURN_INT64(step);
}
