-- Copyright © 2026 The Trustees of Indiana University and contributors.
-- This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
-- You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
-- See the LICENSE file in the root of this repository for the full license terms.
-- https://creativecommons.org/licenses/by-nc/4.0/

\set ON_ERROR_STOP on

DROP TABLE IF EXISTS test_v1_native;

CREATE TABLE test_v1_native (
    action_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    timestamp bigint GENERATED ALWAYS AS (action_id) STORED,
    payload bigint NOT NULL
);
