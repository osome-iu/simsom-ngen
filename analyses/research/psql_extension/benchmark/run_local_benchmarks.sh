#!/usr/bin/env bash
# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
EXTENSION_DIR="${SCRIPT_DIR}/extension"
RESULTS_DIR="${RESULTS_DIR:-}"
SUMMARY_FILE="${SUMMARY_FILE:-}"
LOG_FILE="${LOG_FILE:-}"
PSQL_BIN="${PSQL_BIN:-}"
CREATEDB_BIN="${CREATEDB_BIN:-}"
PYTHON_BIN="${PYTHON_BIN:-}"
MAINTENANCE_DB="${MAINTENANCE_DB:-postgres}"
REAL_PG_CONFIG="${REAL_PG_CONFIG:-}"
BENCHMARK_VERSION="${BENCHMARK_VERSION:-all}"
RUNS="${RUNS:-3}"
TOTAL_ROWS="${TOTAL_ROWS:-10000}"
CLIENTS="${CLIENTS:-4}"
BATCH_SIZE="${BATCH_SIZE:-1}"
NUM_USERS="${NUM_USERS:-10000}"
V3_TOTAL_ROWS="${V3_TOTAL_ROWS:-1000000}"
V3_BATCH_SIZE_LIST="${V3_BATCH_SIZE_LIST:-1,10,100,1000,10000}"
V3_CLIENT_LIST="${V3_CLIENT_LIST:-4,8}"
DB_SMOKE="${DB_SMOKE:-bench_clock_smoke}"
DB_V1_NATIVE="${DB_V1_NATIVE:-bench_v1_native}"
DB_V1_SIMPLE_MATH="${DB_V1_SIMPLE_MATH:-bench_v1_simple_math}"
DB_V1_EXPONENTIAL="${DB_V1_EXPONENTIAL:-bench_v1_exponential}"
DB_V2_NATIVE="${DB_V2_NATIVE:-bench_v2_native}"
DB_V2_SIMPLE_MATH="${DB_V2_SIMPLE_MATH:-bench_v2_simple_math}"
DB_V2_EXPONENTIAL="${DB_V2_EXPONENTIAL:-bench_v2_exponential}"
DB_V3_EXPONENTIAL="${DB_V3_EXPONENTIAL:-bench_v3_exponential}"
RUN_TIMESTAMP="${RUN_TIMESTAMP:-$(date '+%Y%m%d_%H%M%S')}"

print_usage() {
    cat <<'EOF'
Usage: run_local_benchmarks.sh [options]

Options:
  --version V              Benchmark version: v1, v2, v3, or all
  --runs N                 Number of repeated runs per case
  --total-rows N           Total inserted rows per case in both v1 and v2
  --clients N              Number of concurrent clients for v2
  --batch-size N           Rows per statement for v2. Current harness supports only 1
  --num-users N           User count used for exponential clock calibration
  --v3-total-rows N        Total inserted rows for each v3 benchmark run
  --v3-batch-sizes S       Comma-separated batch sizes for v3
  --v3-clients S           Comma-separated client counts for v3
  --psql-bin PATH          Path to psql binary
  --createdb-bin PATH      Path to createdb binary
  --python-bin PATH        Path to python binary used for timing
  --pg-config PATH         Path to pg_config used for building the extension
  --maintenance-db DB      Database used to probe/create benchmark databases
  --results-dir DIR        Directory that stores timestamped benchmark logs
  --summary-file PATH      Explicit path for the run summary TSV
  --log-file PATH          Explicit path for the run log
  --help                   Show this help text
EOF
}

resolve_binary() {
    for candidate in "$@"; do
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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --version)
            BENCHMARK_VERSION="$2"
            shift 2
            ;;
        --runs)
            RUNS="$2"
            shift 2
            ;;
        --total-rows)
            TOTAL_ROWS="$2"
            shift 2
            ;;
        --clients)
            CLIENTS="$2"
            shift 2
            ;;
        --batch-size)
            BATCH_SIZE="$2"
            shift 2
            ;;
        --num-users)
            NUM_USERS="$2"
            shift 2
            ;;
        --v3-total-rows)
            V3_TOTAL_ROWS="$2"
            shift 2
            ;;
        --v3-batch-sizes)
            V3_BATCH_SIZE_LIST="$2"
            shift 2
            ;;
        --v3-clients)
            V3_CLIENT_LIST="$2"
            shift 2
            ;;
        --psql-bin)
            PSQL_BIN="$2"
            shift 2
            ;;
        --createdb-bin)
            CREATEDB_BIN="$2"
            shift 2
            ;;
        --python-bin)
            PYTHON_BIN="$2"
            shift 2
            ;;
        --pg-config)
            REAL_PG_CONFIG="$2"
            shift 2
            ;;
        --maintenance-db)
            MAINTENANCE_DB="$2"
            shift 2
            ;;
        --results-dir)
            RESULTS_DIR="$2"
            shift 2
            ;;
        --summary-file)
            SUMMARY_FILE="$2"
            shift 2
            ;;
        --log-file)
            LOG_FILE="$2"
            shift 2
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            print_usage >&2
            exit 1
            ;;
    esac
done

case "${BENCHMARK_VERSION}" in
    v1|v2|v3|all)
        ;;
    *)
        echo "Unsupported --version value: ${BENCHMARK_VERSION}" >&2
        exit 1
        ;;
esac

if [[ "${BATCH_SIZE}" != "1" ]]; then
    echo "This benchmark harness currently requires --batch-size 1" >&2
    exit 1
fi

if (( TOTAL_ROWS <= 0 )); then
    echo "--total-rows must be greater than zero" >&2
    exit 1
fi

if (( CLIENTS <= 0 )); then
    echo "--clients must be greater than zero" >&2
    exit 1
fi

if [[ "${BENCHMARK_VERSION}" != "v3" ]]; then
    if (( TOTAL_ROWS % CLIENTS != 0 )); then
        echo "--total-rows must be divisible by --clients so v1 and v2 can insert the same total rows per case" >&2
        exit 1
    fi
fi

ROWS_PER_CLIENT=$((TOTAL_ROWS / CLIENTS))

RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results}"
LOG_FILE="${LOG_FILE:-${RESULTS_DIR}/clock_benchmark_${RUN_TIMESTAMP}_${BENCHMARK_VERSION}.log}"
SUMMARY_FILE="${SUMMARY_FILE:-${RESULTS_DIR}/clock_benchmark_${RUN_TIMESTAMP}_${BENCHMARK_VERSION}.tsv}"

PSQL_BIN="$(
    resolve_binary \
        "${PSQL_BIN}" \
        "psql"
)"
CREATEDB_BIN="$(
    resolve_binary \
        "${CREATEDB_BIN}" \
        "createdb"
)"
PYTHON_BIN="$(
    resolve_binary \
        "${PYTHON_BIN}" \
        "python3" \
        "python"
)"

mkdir -p "${RESULTS_DIR}"

exec > >(tee "${LOG_FILE}") 2>&1

ensure_database() {
    local db_name="$1"

    if "${PSQL_BIN}" -d "${MAINTENANCE_DB}" -tAc \
        "SELECT 1 FROM pg_database WHERE datname = '${db_name}'" | grep -q '^1$'; then
        return 0
    fi

    "${CREATEDB_BIN}" "${db_name}"
}

now_ms() {
    "${PYTHON_BIN}" -c 'import time; print(int(time.time() * 1000))'
}

append_summary() {
    local version="$1"
    local case_name="$2"
    local run_number="$3"
    local clients="$4"
    local rows_per_client="$5"
    local batch_size="$6"
    local total_rows="$7"
    local elapsed_ms="$8"
    local rows_per_sec

    rows_per_sec="$("${PYTHON_BIN}" -c "total_rows=${total_rows}; elapsed_ms=${elapsed_ms}; print(0.0 if elapsed_ms == 0 else (total_rows * 1000.0) / elapsed_ms)")"

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%.3f\n' \
        "${version}" \
        "${case_name}" \
        "${run_number}" \
        "${clients}" \
        "${rows_per_client}" \
        "${batch_size}" \
        "${total_rows}" \
        "${elapsed_ms}" \
        "${rows_per_sec}" \
        >> "${SUMMARY_FILE}"

    printf 'SUMMARY version=%s case=%s run=%s elapsed_ms=%s rows_per_sec=%s\n' \
        "${version}" \
        "${case_name}" \
        "${run_number}" \
        "${elapsed_ms}" \
        "${rows_per_sec}"
}

run_psql_file() {
    local db_name="$1"
    local script_name="$2"
    shift 2

    "${PSQL_BIN}" -X -d "${db_name}" "$@" -f "${SCRIPT_DIR}/${script_name}"
}

run_smoke() {
    ensure_database "${DB_SMOKE}"

    echo
    echo "=== Smoke Test ==="
    run_psql_file "${DB_SMOKE}" "smoke.sql" -v num_users="${NUM_USERS}"
}

run_v1_case() {
    local case_name="$1"
    local db_name="$2"
    local setup_script="$3"
    local table_name="$4"

    ensure_database "${db_name}"

    for run_number in $(seq 1 "${RUNS}"); do
        local start_ms
        local end_ms
        local elapsed_ms

        echo
        echo "=== Version 1 ${case_name} run ${run_number}/${RUNS} ==="

        if [[ "${case_name}" == "exponential" ]]; then
            run_psql_file "${db_name}" "${setup_script}" -v num_users="${NUM_USERS}"
        else
            run_psql_file "${db_name}" "${setup_script}"
        fi

        start_ms="$(now_ms)"
        "${PSQL_BIN}" \
            -X \
            -q \
            -d "${db_name}" \
            -v ON_ERROR_STOP=1 \
            -v rows_per_client="${TOTAL_ROWS}" \
            -v worker_id=1 \
            -v table_name="${table_name}" \
            -f "${SCRIPT_DIR}/v2_worker.sql"
        end_ms="$(now_ms)"

        run_psql_file "${db_name}" "v2_validate.sql" -v table_name="${table_name}"

        elapsed_ms=$((end_ms - start_ms))
        append_summary "v1" "${case_name}" "${run_number}" 1 "${TOTAL_ROWS}" 1 "${TOTAL_ROWS}" "${elapsed_ms}"
    done
}

run_v2_case() {
    local case_name="$1"
    local db_name="$2"
    local setup_script="$3"
    local table_name="$4"
    local run_number

    ensure_database "${db_name}"

    for run_number in $(seq 1 "${RUNS}"); do
        local worker_id
        local pid
        local start_ms
        local end_ms
        local elapsed_ms
        local failed=0
        local pids=()
        local total_rows

        echo
        echo "=== Version 2 ${case_name} run ${run_number}/${RUNS} ==="

        if [[ "${case_name}" == "exponential" ]]; then
            run_psql_file "${db_name}" "${setup_script}" -v num_users="${NUM_USERS}"
        else
            run_psql_file "${db_name}" "${setup_script}"
        fi

        start_ms="$(now_ms)"
        for worker_id in $(seq 1 "${CLIENTS}"); do
            "${PSQL_BIN}" \
                -X \
                -q \
                -d "${db_name}" \
                -v ON_ERROR_STOP=1 \
                -v rows_per_client="${ROWS_PER_CLIENT}" \
                -v worker_id="${worker_id}" \
                -v table_name="${table_name}" \
                -f "${SCRIPT_DIR}/v2_worker.sql" &
            pids+=("$!")
        done

        for pid in "${pids[@]}"; do
            if ! wait "${pid}"; then
                failed=1
            fi
        done

        end_ms="$(now_ms)"

        if [[ "${failed}" -ne 0 ]]; then
            echo "Concurrent worker failure detected for case ${case_name}" >&2
            exit 1
        fi

        run_psql_file "${db_name}" "v2_validate.sql" -v table_name="${table_name}"

        elapsed_ms=$((end_ms - start_ms))
        total_rows=$((CLIENTS * ROWS_PER_CLIENT))
        append_summary "v2" "${case_name}" "${run_number}" "${CLIENTS}" "${ROWS_PER_CLIENT}" "${BATCH_SIZE}" "${total_rows}" "${elapsed_ms}"
    done
}

run_v3_case() {
    local db_name="$1"
    local setup_script="$2"
    local table_name="$3"
    local client_count
    local batch_size
    local run_number

    ensure_database "${db_name}"

    if [[ ! "${V3_TOTAL_ROWS}" =~ ^[0-9]+$ ]] || (( V3_TOTAL_ROWS <= 0 )); then
        echo "Invalid v3 total_rows value: ${V3_TOTAL_ROWS}" >&2
        exit 1
    fi

    IFS=',' read -r -a v3_client_values <<< "${V3_CLIENT_LIST}"
    IFS=',' read -r -a v3_batch_size_values <<< "${V3_BATCH_SIZE_LIST}"

    for client_count in "${v3_client_values[@]}"; do
        if [[ ! "${client_count}" =~ ^[0-9]+$ ]] || (( client_count <= 0 )); then
            echo "Invalid v3 client count: ${client_count}" >&2
            exit 1
        fi

        for batch_size in "${v3_batch_size_values[@]}"; do
            if [[ ! "${batch_size}" =~ ^[0-9]+$ ]] || (( batch_size <= 0 )); then
                echo "Invalid v3 batch size: ${batch_size}" >&2
                exit 1
            fi

            for run_number in $(seq 1 "${RUNS}"); do
                local worker_id
                local pid
                local start_ms
                local end_ms
                local elapsed_ms
                local failed=0
                local pids=()
                local base_rows_per_client=$((V3_TOTAL_ROWS / client_count))
                local remainder_rows=$((V3_TOTAL_ROWS % client_count))
                local worker_rows
                local case_name="exponential_clients_${client_count}_batch_${batch_size}"

                echo
                echo "=== Version 3 exponential clients=${client_count} batch_size=${batch_size} total_rows=${V3_TOTAL_ROWS} run ${run_number}/${RUNS} ==="

                run_psql_file "${db_name}" "${setup_script}" -v num_users="${NUM_USERS}"

                start_ms="$(now_ms)"
                for worker_id in $(seq 1 "${client_count}"); do
                    worker_rows="${base_rows_per_client}"
                    if (( worker_id <= remainder_rows )); then
                        worker_rows=$((worker_rows + 1))
                    fi

                    "${PSQL_BIN}" \
                        -X \
                        -q \
                        -d "${db_name}" \
                        -v ON_ERROR_STOP=1 \
                        -v rows_per_client="${worker_rows}" \
                        -v batch_size="${batch_size}" \
                        -v worker_id="${worker_id}" \
                        -v table_name="${table_name}" \
                        -f "${SCRIPT_DIR}/v2_worker.sql" &
                    pids+=("$!")
                done

                for pid in "${pids[@]}"; do
                    if ! wait "${pid}"; then
                        failed=1
                    fi
                done

                end_ms="$(now_ms)"

                if [[ "${failed}" -ne 0 ]]; then
                    echo "Concurrent worker failure detected for v3 clients=${client_count} batch_size=${batch_size}" >&2
                    exit 1
                fi

                run_psql_file "${db_name}" "v2_validate.sql" -v table_name="${table_name}"

                elapsed_ms=$((end_ms - start_ms))
                append_summary "v3" "${case_name}" "${run_number}" "${client_count}" "${base_rows_per_client}" "${batch_size}" "${V3_TOTAL_ROWS}" "${elapsed_ms}"
            done
        done
    done
}

print_summary_report() {
    echo
    echo "=== Summary TSV ==="
    cat "${SUMMARY_FILE}"

    echo
    echo "=== Aggregate Summary ==="
    echo "version\tcase\truns\tavg_elapsed_ms\tmin_elapsed_ms\tmax_elapsed_ms\tavg_rows_per_sec"
    awk -F'\t' '
        NR == 1 { next }
        {
            key = $1 FS $2
            count[key] += 1
            sum_ms[key] += $8
            sum_rps[key] += $9
            if (!(key in min_ms) || $8 < min_ms[key]) {
                min_ms[key] = $8
            }
            if (!(key in max_ms) || $8 > max_ms[key]) {
                max_ms[key] = $8
            }
        }
        END {
            for (key in count) {
                split(key, parts, FS)
                printf "%s\t%s\t%d\t%.3f\t%.0f\t%.0f\t%.3f\n",
                    parts[1],
                    parts[2],
                    count[key],
                    sum_ms[key] / count[key],
                    min_ms[key],
                    max_ms[key],
                    sum_rps[key] / count[key]
            }
        }
    ' "${SUMMARY_FILE}" | sort
}

echo "Benchmark log: ${LOG_FILE}"
echo "Benchmark summary: ${SUMMARY_FILE}"
echo "Benchmark version: ${BENCHMARK_VERSION}"
echo "Benchmark runs: ${RUNS}"
echo "Total rows per case: ${TOTAL_ROWS}"
echo "Version 2 clients: ${CLIENTS}"
echo "Version 2 rows_per_client: ${ROWS_PER_CLIENT}"
echo "Version 2 batch_size: ${BATCH_SIZE}"
echo "Version 3 total_rows: ${V3_TOTAL_ROWS}"
echo "Version 3 client sweep: ${V3_CLIENT_LIST}"
echo "Version 3 batch size sweep: ${V3_BATCH_SIZE_LIST}"
echo "Benchmark num_users: ${NUM_USERS}"
echo "Benchmark started at: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "psql binary: ${PSQL_BIN}"
echo "createdb binary: ${CREATEDB_BIN}"
echo "python binary: ${PYTHON_BIN}"
if [[ -n "${REAL_PG_CONFIG}" ]]; then
    echo "pg_config override: ${REAL_PG_CONFIG}"
fi

printf 'version\tcase\trun\tclients\trows_per_client\tbatch_size\ttotal_rows\telapsed_ms\trows_per_sec\n' > "${SUMMARY_FILE}"

make_args=(-C "${EXTENSION_DIR}")
if [[ -n "${REAL_PG_CONFIG}" ]]; then
    make_args+=("REAL_PG_CONFIG=${REAL_PG_CONFIG}")
fi

make "${make_args[@]}" clean
make "${make_args[@]}"
make "${make_args[@]}" install

run_smoke

case "${BENCHMARK_VERSION}" in
    v1)
        run_v1_case "native" "${DB_V1_NATIVE}" "v1_native.sql" "test_v1_native"
        run_v1_case "simple_math" "${DB_V1_SIMPLE_MATH}" "v1_simple_math.sql" "test_v1_simple_math"
        run_v1_case "exponential" "${DB_V1_EXPONENTIAL}" "v1_exponential.sql" "test_v1_exponential"
        ;;
    v2)
        run_v2_case "native" "${DB_V2_NATIVE}" "v2_native_setup.sql" "test_v2_native"
        run_v2_case "simple_math" "${DB_V2_SIMPLE_MATH}" "v2_simple_math_setup.sql" "test_v2_simple_math"
        run_v2_case "exponential" "${DB_V2_EXPONENTIAL}" "v2_exponential_setup.sql" "test_v2_exponential"
        ;;
    v3)
        run_v3_case "${DB_V3_EXPONENTIAL}" "v2_exponential_setup.sql" "test_v2_exponential"
        ;;
    all)
        run_v1_case "native" "${DB_V1_NATIVE}" "v1_native.sql" "test_v1_native"
        run_v1_case "simple_math" "${DB_V1_SIMPLE_MATH}" "v1_simple_math.sql" "test_v1_simple_math"
        run_v1_case "exponential" "${DB_V1_EXPONENTIAL}" "v1_exponential.sql" "test_v1_exponential"
        run_v2_case "native" "${DB_V2_NATIVE}" "v2_native_setup.sql" "test_v2_native"
        run_v2_case "simple_math" "${DB_V2_SIMPLE_MATH}" "v2_simple_math_setup.sql" "test_v2_simple_math"
        run_v2_case "exponential" "${DB_V2_EXPONENTIAL}" "v2_exponential_setup.sql" "test_v2_exponential"
        run_v3_case "${DB_V3_EXPONENTIAL}" "v2_exponential_setup.sql" "test_v2_exponential"
        ;;
esac

print_summary_report

echo "Benchmark finished at: $(date '+%Y-%m-%d %H:%M:%S %Z')"
