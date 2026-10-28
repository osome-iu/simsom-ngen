# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

FROM postgres:18-bookworm AS postgres-client-source


FROM python:3.12-slim-bookworm AS python-builder

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential libmpich-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.runtime.txt /tmp/requirements.runtime.txt
RUN pip install --no-cache-dir --no-binary=mpi4py -r /tmp/requirements.runtime.txt


FROM python:3.12-slim-bookworm AS simulator

COPY --from=postgres-client-source \
    /usr/local/share/keyrings/postgres.gpg.asc \
    /usr/local/share/keyrings/postgres.gpg.asc
COPY --from=postgres-client-source \
    /etc/apt/sources.list.d/pgdg.list \
    /etc/apt/sources.list.d/pgdg.list

RUN apt-get update \
    && apt-get install --yes --no-install-recommends mpich postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-builder /opt/venv /opt/venv
COPY libs /app/libs

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Relative output paths resolve to /output/output. Compose mounts that
# directory so logs, JSONL files, and the SQL dump survive container exit.
WORKDIR /output

ENTRYPOINT ["python", "/app/libs/simsom/run_simulation.py"]
