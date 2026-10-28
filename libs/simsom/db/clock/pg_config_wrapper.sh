#!/usr/bin/env bash
# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

set -euo pipefail

# PGXS needs the full server pg_config. Resolve it from an explicit override or
# from PATH so the same wrapper works across host and container layouts.
resolve_pg_config() {
    local candidate

    for candidate in "${REAL_PG_CONFIG:-}" "pg_config"; do
        if [[ -z "${candidate}" ]]; then
            continue
        fi

        if [[ "${candidate}" == */* ]]; then
            if [[ -x "${candidate}" ]]; then
                printf '%s\n' "${candidate}"
                return 0
            fi
            continue
        fi

        if resolved="$(command -v "${candidate}" 2>/dev/null)"; then
            printf '%s\n' "${resolved}"
            return 0
        fi
    done

    return 1
}

REAL_PG_CONFIG="$(resolve_pg_config)"

if [[ "${1:-}" == "--cppflags" ]]; then
    sdk_root="$(xcrun --show-sdk-path 2>/dev/null || true)"
    cppflags="$("${REAL_PG_CONFIG}" --cppflags)"

    if [[ -n "${sdk_root}" ]]; then
        # Replace the stale CommandLineTools SDK path with the active Xcode SDK.
        cppflags="${cppflags//\/Library\/Developer\/CommandLineTools\/SDKs\/MacOSX26.sdk/${sdk_root}}"
    fi

    printf '%s\n' "${cppflags}"
    exit 0
fi

exec "${REAL_PG_CONFIG}" "$@"
