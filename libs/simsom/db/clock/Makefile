# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

# REAL_PG_CONFIG may be passed in from the benchmark runner or environment.
# The wrapper falls back to a working local default and then to pg_config on PATH.
REAL_PG_CONFIG ?=
PG_CONFIG = $(CURDIR)/pg_config_wrapper.sh
# Some local PostgreSQL installs expose a stale macOS SDK path, so prefer the
# active SDK when PGXS expands compile and link flags.
SDKROOT ?= $(shell xcrun --show-sdk-path 2>/dev/null)
override PG_SYSROOT := $(SDKROOT)

EXTENSION = simsom_clock
MODULE_big = simsom_clock
OBJS = simsom_clock.o
DATA = simsom_clock--1.0.sql
DOCS = README.md
SHLIB_LINK += -lm
REGRESS = simsom_clock_basic
EXTRA_CLEAN = regression.diffs

PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)
