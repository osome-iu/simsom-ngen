-- Copyright © 2026 The Trustees of Indiana University and contributors.
-- This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
-- You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
-- See the LICENSE file in the root of this repository for the full license terms.
-- https://creativecommons.org/licenses/by-nc/4.0/

\set ON_ERROR_STOP on
\set QUIET 1

\if :{?rows_per_client}
\else
\set rows_per_client 100
\endif

\if :{?worker_id}
\else
\set worker_id 1
\endif

\if :{?batch_size}
\else
\set batch_size 1
\endif

\if :{?table_name}
\else
\echo 'table_name is required'
\quit 1
\endif

WITH payload_rows AS (
    SELECT
        gs AS seq_no,
        ((:worker_id::bigint * 1000000) + gs)::bigint AS payload
    FROM generate_series(1, :rows_per_client) AS gs
),
batched_payload_rows AS (
    SELECT
        ((seq_no - 1) / :batch_size) AS batch_no,
        string_agg('(' || payload || ')', ',' ORDER BY seq_no) AS values_sql
    FROM payload_rows
    GROUP BY 1
)
SELECT
    'INSERT INTO '
    || quote_ident(:'table_name')
    || ' (payload) VALUES '
    || values_sql
    || ';'
FROM batched_payload_rows
ORDER BY batch_no
\gexec
