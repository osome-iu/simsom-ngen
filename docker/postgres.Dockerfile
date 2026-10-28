# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

FROM postgres:18-bookworm AS clock-builder

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential postgresql-server-dev-18 \
    && rm -rf /var/lib/apt/lists/*

COPY libs/simsom/db/clock /build/simsom_clock
RUN make -C /build/simsom_clock clean \
    && make -C /build/simsom_clock \
        REAL_PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config \
    && make -C /build/simsom_clock \
        REAL_PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config \
        DESTDIR=/extension-root install


FROM postgres:18-bookworm

# Install only the compiled extension artifacts in the runtime database image.
COPY --from=clock-builder /extension-root/ /
