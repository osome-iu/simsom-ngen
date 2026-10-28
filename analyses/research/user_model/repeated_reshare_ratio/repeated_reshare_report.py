#!/usr/bin/env python3
# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
import argparse
import json
import subprocess
import re
import os
import time
from datetime import date

import pandas as pd

REPEAT_KINDS = ("repost", "like")


@dataclass
class Config:
    base_data_dir: Path
    schema_version: int = 3
    analysis_start_day: Optional[str] = None
    analysis_end_day: Optional[str] = None
    # "cumulative" | "windows" | "both"
    series_mode: str = "cumulative"
    enabled_kinds: Iterable[str] = REPEAT_KINDS
    compute_sliding_series: bool = False
    sliding_window_days: Optional[Iterable[int]] = (1, 2, 7, 10, 30)
    include_full_period_window: bool = True
    analysis_commit_actions: Iterable[str] = ("create",)
    denominator_commit_actions: Iterable[str] = ("create",)
    raw_dir_override: Optional[Path] = None
    parquet_dir_override: Optional[Path] = None
    intermediate_dir_override: Optional[Path] = None
    use_cached_tables: bool = True
    write_intermediate: bool = True
    write_window_tables: bool = True
    use_state_cache: bool = True
    buffer_size: int = 50_000
    parallel_mode: str = "processes"
    max_workers: int = 0
    force_rebuild: bool = False
    spark_master: Optional[str] = None
    spark_app_name: str = "repeated_reshare_ratio"
    spark_sql_shuffle_partitions: int = 2000
    spark_shuffle_compress: bool = False
    spark_io_compression_codec: str = "snappy"
    spark_driver_memory: str = "24g"
    spark_executor_memory: str = "24g"
    spark_local_dir: Optional[Path] = None
    log_progress: bool = True
    progress_every_batches: int = 25
    progress_every_days: int = 20

    raw_dir: Path = field(init=False)
    parquet_dir: Path = field(init=False)
    intermediate_dir: Path = field(init=False)
    tables_dir: Path = field(init=False)
    parquet_glob: str = field(init=False)

    def __post_init__(self) -> None:
        self.raw_dir = self.raw_dir_override or (self.base_data_dir / "raw")
        self.parquet_dir = self.parquet_dir_override or (self.base_data_dir / "parquet")
        self.intermediate_dir = self.intermediate_dir_override or (self.base_data_dir / "intermediate")
        self.tables_dir = self.intermediate_dir / "tables"
        self.parquet_glob = str(self.parquet_dir / "*.parquet")
        kinds = tuple(dict.fromkeys(str(k) for k in self.enabled_kinds))
        invalid = [k for k in kinds if k not in REPEAT_KINDS]
        if invalid:
            raise ValueError(f"Invalid enabled_kinds: {invalid}. Allowed: {REPEAT_KINDS}")
        if not kinds:
            raise ValueError("enabled_kinds cannot be empty")
        self.enabled_kinds = kinds

    @property
    def cache_tag(self) -> str:
        start = self.analysis_start_day or "start"
        end = self.analysis_end_day or "end"
        commits = "-".join(sorted(str(x) for x in self.analysis_commit_actions))
        kinds = "-".join(sorted(str(x) for x in self.enabled_kinds))
        return f"range_{start}_{end}_commit_{commits}_kinds_{kinds}"

    def cache_path(self, name: str) -> Path:
        return self.tables_dir / f"{name}_{self.cache_tag}.parquet"


# ----------------------------- Spark extraction -----------------------------

def _spark_session(cfg: Config):
    _validate_java_runtime_for_spark()
    try:
        from pyspark.sql import SparkSession
    except Exception as exc:
        raise RuntimeError("PySpark is required but not installed in this environment.") from exc

    builder = SparkSession.builder.appName(cfg.spark_app_name)
    if cfg.spark_master:
        builder = builder.master(cfg.spark_master)
    builder = (
        builder
        .config("spark.sql.adaptive.enabled", "true")
        .config("spark.sql.adaptive.coalescePartitions.enabled", "true")
        .config("spark.sql.adaptive.skewJoin.enabled", "true")
        .config("spark.sql.shuffle.partitions", str(int(cfg.spark_sql_shuffle_partitions)))
        .config("spark.shuffle.compress", "true" if cfg.spark_shuffle_compress else "false")
        .config("spark.io.compression.codec", str(cfg.spark_io_compression_codec))
        .config("spark.driver.memory", str(cfg.spark_driver_memory))
        .config("spark.executor.memory", str(cfg.spark_executor_memory))
    )
    if cfg.spark_local_dir is not None:
        Path(cfg.spark_local_dir).mkdir(parents=True, exist_ok=True)
        builder = builder.config("spark.local.dir", str(cfg.spark_local_dir))
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    return spark


def _detect_java_major() -> Optional[int]:
    try:
        proc = subprocess.run(
            ["java", "-version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    out = (proc.stderr or "") + "\n" + (proc.stdout or "")
    m = re.search(r'version\\s+"(\\d+)(?:\\.(\\d+))?', out)
    if not m:
        return None
    first = int(m.group(1))
    if first == 1 and m.group(2):
        return int(m.group(2))
    return first


def _validate_java_runtime_for_spark() -> None:
    # Spark 4.x requires Java 17+.
    java_major = _detect_java_major()
    if java_major is not None and java_major < 17:
        raise RuntimeError(
            "Detected Java "
            f"{java_major}, but PySpark/Spark 4.x requires Java 17+.\n"
            "Fix options:\n"
            "1) Install Java 17 and set JAVA_HOME.\n"
            "2) Or downgrade PySpark to 3.5.x (works with Java 11)."
        )


def _ensure_parquet_exists(cfg: Config) -> None:
    if not cfg.parquet_dir.exists() or not list(cfg.parquet_dir.glob("*.parquet")):
        raise FileNotFoundError(
            f"No parquet files found at {cfg.parquet_dir}. "
            "Prepare parquet first before running this report."
        )


def _log(cfg: Config, message: str) -> None:
    if cfg.log_progress:
        print(f"[repeat-report] {message}", flush=True)


def _parse_date_from_name(name: str) -> Optional[date]:
    m = re.search(r"(\\d{4}-\\d{2}-\\d{2})", name)
    if not m:
        return None
    try:
        return pd.to_datetime(m.group(1), errors="raise").date()
    except Exception:
        return None


def _candidate_parquet_files(cfg: Config) -> list[Path]:
    files = sorted(cfg.parquet_dir.glob("*.parquet"))
    if not files:
        return files
    if not cfg.analysis_start_day and not cfg.analysis_end_day:
        return files

    start = pd.to_datetime(cfg.analysis_start_day, errors="coerce").date() if cfg.analysis_start_day else None
    end = pd.to_datetime(cfg.analysis_end_day, errors="coerce").date() if cfg.analysis_end_day else None

    selected: list[Path] = []
    unknown: list[Path] = []
    for p in files:
        d = _parse_date_from_name(p.name)
        if d is None:
            # Keep unknown names to avoid accidental drops.
            unknown.append(p)
            continue
        if start and d < start:
            continue
        if end and d > end:
            continue
        selected.append(p)
    return selected + unknown


def ensure_parquet(cfg: Config) -> None:
    prep = Path(__file__).resolve().parent / "prepare_parquet.py"
    args = [
        os.sys.executable,
        str(prep),
        "--raw-dir",
        str(cfg.raw_dir),
        "--parquet-dir",
        str(cfg.parquet_dir),
        "--schema-version",
        str(cfg.schema_version),
        "--buffer-size",
        str(cfg.buffer_size),
        "--parallel-mode",
        str(cfg.parallel_mode),
    ]
    if cfg.max_workers > 0:
        args += ["--max-workers", str(cfg.max_workers)]
    if cfg.force_rebuild:
        args.append("--force")
    subprocess.run(args, check=True)


def _build_relation_df(cfg: Config, spark):
    from pyspark.sql import functions as F

    # Read directory directly to avoid noisy metadata warnings on glob paths.
    df = spark.read.option("recursiveFileLookup","true").option("pathGlobFilter","*.parquet").parquet(str(cfg.parquet_dir))

    required = {"uri", "action", "commit_action", "commit_time", "subject_uri"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Missing required parquet columns: {missing}")

    day_col = F.to_date(F.from_unixtime(F.col("commit_time")))
    author_col = F.regexp_extract(F.col("uri"), r"^at://([^/]+)/", 1)

    # Minimal relation for this analysis:
    # only create events with explicit subject_uri are needed.
    relation = (
        df
        .withColumn("author", author_col)
        .withColumn("day", day_col)
        .filter(F.col("action").isin(*tuple(cfg.enabled_kinds)))
        .filter(F.col("commit_action") == F.lit("create"))
        .filter(F.col("commit_time").isNotNull())
        .filter(F.col("uri").isNotNull())
        .filter(F.col("subject_uri").isNotNull())
        .filter(F.col("author").isNotNull() & (F.length(F.col("author")) > 0))
        .select(
            F.col("action").alias("kind"),
            "author",
            F.col("subject_uri").alias("target_uri"),
            "commit_action",
            "commit_time",
            "day",
        )
    )

    if cfg.analysis_start_day:
        relation = relation.filter(F.col("day") >= F.lit(cfg.analysis_start_day))
    if cfg.analysis_end_day:
        relation = relation.filter(F.col("day") <= F.lit(cfg.analysis_end_day))

    return relation


def _materialize_daily_tables(cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, int]:
    """
    Spark-first materialization:
      - Read prepared parquet with Spark
      - Build per-day (author, target) pair counts
      - Compute:
          * day index
          * daily new total pairs (first seen day)
          * daily new repeat pairs (day pair becomes repeat: cumulative >=2 for first time)
          * daily new repeat users (day an author first has any repeat pair)
          * summary totals per kind
      - Cache all intermediate tables to parquet so reruns do not rescan raw data.
    """
    cfg.tables_dir.mkdir(parents=True, exist_ok=True)

    # Cached tables (skip Spark work when present)
    day_index_path = cfg.cache_path("day_index")
    daily_repeat_users_path = cfg.cache_path("daily_new_repeat_users")
    daily_total_pairs_path = cfg.cache_path("daily_new_total_pairs")
    daily_repeat_pairs_path = cfg.cache_path("daily_new_repeat_pairs")
    summary_pairs_path = cfg.cache_path("summary_pairs")
    total_users_path = cfg.cache_path("total_users")

    if (
        cfg.use_cached_tables
        and not cfg.force_rebuild
        and day_index_path.exists()
        and daily_repeat_users_path.exists()
        and daily_total_pairs_path.exists()
        and daily_repeat_pairs_path.exists()
        and summary_pairs_path.exists()
        and total_users_path.exists()
    ):
        _log(cfg, "using cached parquet tables (skipping Spark scan)")
        day_index = pd.read_parquet(day_index_path)
        daily_new_repeat_users = pd.read_parquet(daily_repeat_users_path)
        daily_new_total_pairs = pd.read_parquet(daily_total_pairs_path)
        daily_new_repeat_pairs = pd.read_parquet(daily_repeat_pairs_path)
        summary_pairs = pd.read_parquet(summary_pairs_path)
        total_users = int(pd.read_parquet(total_users_path)["total_users"].iloc[0])
        return (
            day_index,
            daily_new_repeat_users,
            daily_new_total_pairs,
            daily_new_repeat_pairs,
            summary_pairs,
            total_users,
        )

    started = time.perf_counter()
    _log(cfg, "spark materialization started")

    spark = _spark_session(cfg)
    relation = _build_relation_df(cfg, spark).select("kind", "author", "target_uri", "day")

    from pyspark.sql import functions as F
    from pyspark.sql.window import Window

    # (1) day index
    days_df = relation.select("day").distinct().orderBy("day")
    w_day = Window.orderBy(F.col("day"))
    day_index_sdf = days_df.withColumn("day_index", F.row_number().over(w_day) - F.lit(1))
    day_index_sdf.write.mode("overwrite").parquet(str(day_index_path))

    # (2) per-day pair counts
    pair_day = (
        relation.groupBy("kind", "author", "target_uri", "day")
        .agg(F.count(F.lit(1)).alias("day_cnt"))
    )
    # Reused multiple times; persisting avoids recomputing shuffles.
    from pyspark import StorageLevel
    pair_day = pair_day.persist(StorageLevel.DISK_ONLY)

    # Per pair totals + first day (used for new_total_pairs + summaries)
    pair_tot = (
        pair_day.groupBy("kind", "author", "target_uri")
        .agg(
            F.sum("day_cnt").alias("total_cnt"),
            F.min("day").alias("first_day"),
        )
    )
    pair_tot = pair_tot.persist(StorageLevel.DISK_ONLY)

    # (3) daily new total pairs (first seen day)
    daily_new_total_pairs_sdf = (
        pair_tot.groupBy("kind", F.col("first_day").alias("day"))
        .agg(F.count(F.lit(1)).alias("new_total_pairs"))
    )

    # (4) day a pair becomes repeat (cum >=2 for first time)
    w_pair = Window.partitionBy("kind", "author", "target_uri").orderBy(F.col("day")).rowsBetween(Window.unboundedPreceding, Window.currentRow)
    pair_cum = pair_day.withColumn("cum_cnt", F.sum("day_cnt").over(w_pair))
    first_repeat_day = (
        pair_cum.filter(F.col("cum_cnt") >= F.lit(2))
        .groupBy("kind", "author", "target_uri")
        .agg(F.min("day").alias("day"))
    )
    daily_new_repeat_pairs_sdf = (
        first_repeat_day.groupBy("kind", "day")
        .agg(F.count(F.lit(1)).alias("new_repeat_pairs"))
    )

    # (5) daily new repeat users: first day each author has any repeat pair
    author_first_repeat = (
        first_repeat_day.groupBy("kind", "author")
        .agg(F.min("day").alias("day"))
    )
    daily_new_repeat_users_sdf = (
        author_first_repeat.groupBy("kind", "day")
        .agg(F.count(F.lit(1)).alias("new_repeat_users"))
    )

    # (6) summary totals per kind
    summary_pairs_sdf = (
        pair_tot.groupBy("kind")
        .agg(
            F.count(F.lit(1)).alias("pairs"),
            F.sum(F.when(F.col("total_cnt") >= F.lit(2), F.lit(1)).otherwise(F.lit(0))).cast("long").alias("repeat_pairs"),
            F.countDistinct("author").alias("users_with_kind"),
            F.countDistinct(F.when(F.col("total_cnt") >= F.lit(2), F.col("author"))).alias("repeat_users"),
        )
    )

    # (7) total users across all enabled kinds in this analysis slice
    total_users = int(relation.select("author").distinct().count())
    total_users_sdf = spark.createDataFrame([(total_users,)], ["total_users"])

    # Write cached parquet tables (overwrite for idempotence)
    daily_new_total_pairs_sdf.write.mode("overwrite").parquet(str(daily_total_pairs_path))
    daily_new_repeat_pairs_sdf.write.mode("overwrite").parquet(str(daily_repeat_pairs_path))
    daily_new_repeat_users_sdf.write.mode("overwrite").parquet(str(daily_repeat_users_path))
    summary_pairs_sdf.write.mode("overwrite").parquet(str(summary_pairs_path))
    total_users_sdf.write.mode("overwrite").parquet(str(total_users_path))

    # Free persisted Spark intermediates (they may have spilled to disk)
    try:
        pair_tot.unpersist()
        pair_day.unpersist()
    except Exception:
        pass

    # Convert small cached tables to pandas for downstream plotting/fit
    day_index = pd.read_parquet(day_index_path).sort_values("day").reset_index(drop=True)
    daily_new_repeat_users = pd.read_parquet(daily_repeat_users_path)
    daily_new_total_pairs = pd.read_parquet(daily_total_pairs_path)
    daily_new_repeat_pairs = pd.read_parquet(daily_repeat_pairs_path)
    summary_pairs = pd.read_parquet(summary_pairs_path)

    elapsed = time.perf_counter() - started
    _log(cfg, f"spark materialization complete in {elapsed/60.0:.1f} min (total_users={total_users:,})")

    return (
        day_index,
        daily_new_repeat_users,
        daily_new_total_pairs,
        daily_new_repeat_pairs,
        summary_pairs,
        total_users,
    )


# ---------------------------- Pandas aggregation ----------------------------

def _coerce_day(df: pd.DataFrame, col: str) -> pd.DataFrame:
    if df.empty or col not in df.columns:
        return df
    out = df.copy()
    out[col] = pd.to_datetime(out[col], errors="coerce").dt.floor("D")
    out = out[out[col].notna()]
    return out


def _rolling_window_days(cfg: Config, n_days: int) -> list[int]:
    windows = sorted({int(x) for x in (cfg.sliding_window_days or []) if int(x) > 0 and int(x) <= n_days})
    if cfg.include_full_period_window and n_days > 0:
        windows = sorted(set(windows + [n_days]))
    return windows


def _cumulative_repeat_users(
    day_index: pd.DataFrame,
    daily_new_repeat_users: pd.DataFrame,
    total_users: int,
    kinds: Iterable[str],
) -> pd.DataFrame:
    if day_index.empty:
        return pd.DataFrame(columns=["kind", "day", "day_index", "repeat_users", "total_users", "ratio"])

    base = _coerce_day(day_index, "day")
    daily = _coerce_day(daily_new_repeat_users, "day")

    rows = []
    for kind in kinds:
        out = base[["day", "day_index"]].copy()
        out["kind"] = kind
        part = daily[daily["kind"] == kind][["day", "new_repeat_users"]] if not daily.empty else pd.DataFrame()
        if part.empty:
            out["new_repeat_users"] = 0
        else:
            part = part.groupby("day", as_index=False)["new_repeat_users"].sum()
            out = out.merge(part, on="day", how="left")
            out["new_repeat_users"] = out["new_repeat_users"].fillna(0)
        out["repeat_users"] = out["new_repeat_users"].astype(int).cumsum()
        out["total_users"] = int(total_users)
        out["ratio"] = out["repeat_users"] / float(total_users) if total_users else 0.0
        rows.append(out[["kind", "day", "day_index", "repeat_users", "total_users", "ratio"]])
    return pd.concat(rows, ignore_index=True)


def _cumulative_repeat_pairs(
    day_index: pd.DataFrame,
    daily_new_total_pairs: pd.DataFrame,
    daily_new_repeat_pairs: pd.DataFrame,
    kinds: Iterable[str],
) -> pd.DataFrame:
    if day_index.empty:
        return pd.DataFrame(columns=["kind", "day", "day_index", "repeat_pairs", "total_pairs", "ratio"])

    base = _coerce_day(day_index, "day")
    total = _coerce_day(daily_new_total_pairs, "day")
    repeat = _coerce_day(daily_new_repeat_pairs, "day")

    rows = []
    for kind in kinds:
        out = base[["day", "day_index"]].copy()
        out["kind"] = kind

        t = total[total["kind"] == kind][["day", "new_total_pairs"]] if not total.empty else pd.DataFrame()
        r = repeat[repeat["kind"] == kind][["day", "new_repeat_pairs"]] if not repeat.empty else pd.DataFrame()
        if t.empty:
            out["new_total_pairs"] = 0
        else:
            out = out.merge(t.groupby("day", as_index=False)["new_total_pairs"].sum(), on="day", how="left")
            out["new_total_pairs"] = out["new_total_pairs"].fillna(0)
        if r.empty:
            out["new_repeat_pairs"] = 0
        else:
            out = out.merge(r.groupby("day", as_index=False)["new_repeat_pairs"].sum(), on="day", how="left")
            out["new_repeat_pairs"] = out["new_repeat_pairs"].fillna(0)

        out["total_pairs"] = out["new_total_pairs"].astype(int).cumsum()
        out["repeat_pairs"] = out["new_repeat_pairs"].astype(int).cumsum()
        out["ratio"] = 0.0
        m = out["total_pairs"] > 0
        out.loc[m, "ratio"] = out.loc[m, "repeat_pairs"] / out.loc[m, "total_pairs"]
        rows.append(out[["kind", "day", "day_index", "repeat_pairs", "total_pairs", "ratio"]])
    return pd.concat(rows, ignore_index=True)


def _sliding_repeat_users(cfg: Config, day_index: pd.DataFrame, daily_new_repeat_users: pd.DataFrame, total_users: int) -> pd.DataFrame:
    if day_index.empty:
        return pd.DataFrame()
    base = _coerce_day(day_index, "day")
    daily = _coerce_day(daily_new_repeat_users, "day")
    windows = _rolling_window_days(cfg, len(base))
    if not windows:
        return pd.DataFrame()

    rows = []
    for kind in cfg.enabled_kinds:
        part = daily[daily["kind"] == kind][["day", "new_repeat_users"]] if not daily.empty else pd.DataFrame()
        merged = base[["day", "day_index"]].copy()
        if part.empty:
            merged["new_repeat_users"] = 0
        else:
            merged = merged.merge(part.groupby("day", as_index=False)["new_repeat_users"].sum(), on="day", how="left")
            merged["new_repeat_users"] = merged["new_repeat_users"].fillna(0)
        for w in windows:
            out = merged[["day", "day_index"]].copy()
            out["kind"] = kind
            out["window_days"] = int(w)
            out["repeat_users"] = merged["new_repeat_users"].rolling(window=w, min_periods=1).sum().astype(int)
            out["total_users"] = int(total_users)
            out["ratio"] = out["repeat_users"] / float(total_users) if total_users else 0.0
            rows.append(out)
    return pd.concat(rows, ignore_index=True)


def _sliding_repeat_pairs(cfg: Config, day_index: pd.DataFrame, daily_new_total_pairs: pd.DataFrame, daily_new_repeat_pairs: pd.DataFrame) -> pd.DataFrame:
    if day_index.empty:
        return pd.DataFrame()
    base = _coerce_day(day_index, "day")
    total = _coerce_day(daily_new_total_pairs, "day")
    repeat = _coerce_day(daily_new_repeat_pairs, "day")
    windows = _rolling_window_days(cfg, len(base))
    if not windows:
        return pd.DataFrame()

    rows = []
    for kind in cfg.enabled_kinds:
        merged = base[["day", "day_index"]].copy()

        t = total[total["kind"] == kind][["day", "new_total_pairs"]] if not total.empty else pd.DataFrame()
        r = repeat[repeat["kind"] == kind][["day", "new_repeat_pairs"]] if not repeat.empty else pd.DataFrame()
        if t.empty:
            merged["new_total_pairs"] = 0
        else:
            merged = merged.merge(t.groupby("day", as_index=False)["new_total_pairs"].sum(), on="day", how="left")
            merged["new_total_pairs"] = merged["new_total_pairs"].fillna(0)
        if r.empty:
            merged["new_repeat_pairs"] = 0
        else:
            merged = merged.merge(r.groupby("day", as_index=False)["new_repeat_pairs"].sum(), on="day", how="left")
            merged["new_repeat_pairs"] = merged["new_repeat_pairs"].fillna(0)

        for w in windows:
            out = merged[["day", "day_index"]].copy()
            out["kind"] = kind
            out["window_days"] = int(w)
            out["total_pairs"] = merged["new_total_pairs"].rolling(window=w, min_periods=1).sum().astype(int)
            out["repeat_pairs"] = merged["new_repeat_pairs"].rolling(window=w, min_periods=1).sum().astype(int)
            out["ratio"] = 0.0
            m = out["total_pairs"] > 0
            out.loc[m, "ratio"] = out.loc[m, "repeat_pairs"] / out.loc[m, "total_pairs"]
            rows.append(out)
    return pd.concat(rows, ignore_index=True)


def fit_convergence_exponential(series: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if series is None or series.empty:
        return pd.DataFrame(), pd.DataFrame()

    import numpy as np

    rows = []
    fit_rows = []
    for kind, group in series.groupby("kind"):
        g = group.sort_values("day_index")
        x = g["day_index"].to_numpy()
        y = g["ratio"].to_numpy()
        if len(x) < 3:
            continue
        a = float(y[-1])
        eps = 1e-8
        mask = (a - y) > eps
        if int(mask.sum()) < 2:
            continue
        try:
            coeff = np.polyfit(x[mask].astype(float), np.log(a - y[mask].astype(float)), 1)
            c = -float(coeff[0])
            b = float(np.exp(coeff[1]))
        except Exception:
            continue
        fitted = a - b * np.exp(-c * x)
        ss_res = float(((y - fitted) ** 2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum()) if len(y) > 1 else 0.0
        r2 = 1.0 - ss_res / ss_tot if ss_tot else 0.0
        rows.append({"kind": kind, "a": a, "b": b, "c": c, "r2": r2})
        fit_rows.append(pd.DataFrame({"kind": kind, "day_index": x, "fitted_ratio": fitted}))

    return pd.DataFrame(rows), (pd.concat(fit_rows, ignore_index=True) if fit_rows else pd.DataFrame())


def _finalize_summary(summary_pairs: pd.DataFrame, total_users: int, kinds: Iterable[str]) -> pd.DataFrame:
    needed = pd.DataFrame({"kind": list(kinds)})
    if summary_pairs is None or summary_pairs.empty:
        summary = needed.copy()
        summary["pairs"] = 0
        summary["repeat_pairs"] = 0
        summary["users_with_kind"] = 0
        summary["repeat_users"] = 0
    else:
        summary = needed.merge(summary_pairs, on="kind", how="left").fillna(0)

    for col in ["pairs", "repeat_pairs", "users_with_kind", "repeat_users"]:
        summary[col] = summary[col].astype(int)

    summary["total_users"] = int(total_users)
    summary["repeat_users_pct_of_total"] = summary["repeat_users"] / float(total_users) if total_users else 0.0
    summary["repeat_users_pct_of_kind"] = 0.0
    m = summary["users_with_kind"] > 0
    summary.loc[m, "repeat_users_pct_of_kind"] = summary.loc[m, "repeat_users"] / summary.loc[m, "users_with_kind"]
    summary["repeat_pairs_pct"] = 0.0
    p = summary["pairs"] > 0
    summary.loc[p, "repeat_pairs_pct"] = summary.loc[p, "repeat_pairs"] / summary.loc[p, "pairs"]
    return summary.sort_values("kind").reset_index(drop=True)


def _compute_total_users(summary: pd.DataFrame) -> int:
    if summary is None or summary.empty:
        return 0
    return int(summary["users_with_kind"].max()) if len(summary) else 0


def compute_repeat_summary(cfg: Config) -> tuple[pd.DataFrame, dict]:
    # First step (raw -> parquet) can use schema-versioned caching via prepare_parquet.py.
    _log(cfg, "prepare parquet step started")
    ensure_parquet(cfg)
    _log(cfg, "prepare parquet step finished")
    day_index, daily_repeat_users, daily_total_pairs, daily_repeat_pairs, summary_pairs, total_users = _materialize_daily_tables(cfg)
    _log(cfg, "building cumulative/window series")

    kinds = tuple(cfg.enabled_kinds)
    summary = _finalize_summary(summary_pairs, total_users, kinds)

    mode = str(cfg.series_mode or "cumulative").strip().lower()
    do_cumulative = mode in {"cumulative", "both"}
    do_windows = mode in {"windows", "both"} or bool(cfg.compute_sliding_series)

    cumulative_repeat_series = pd.DataFrame()
    cumulative_repeat_pairs_series = pd.DataFrame()
    if do_cumulative:
        cumulative_repeat_series = _cumulative_repeat_users(day_index, daily_repeat_users, total_users, kinds)
        cumulative_repeat_pairs_series = _cumulative_repeat_pairs(day_index, daily_total_pairs, daily_repeat_pairs, kinds)

    fit_params, fit_series = fit_convergence_exponential(cumulative_repeat_series)

    sliding_repeat_series = pd.DataFrame()
    sliding_repeat_pairs_series = pd.DataFrame()
    if do_windows:
        sliding_repeat_series = _sliding_repeat_users(cfg, day_index, daily_repeat_users, total_users)
        sliding_repeat_pairs_series = _sliding_repeat_pairs(cfg, day_index, daily_total_pairs, daily_repeat_pairs)

    report = {
        "summary": summary,
        "notes": {"total_users": total_users},
        "cumulative_repeat_series": cumulative_repeat_series,
        "cumulative_repeat_pairs_series": cumulative_repeat_pairs_series,
        "cumulative_fit_params": fit_params,
        "cumulative_fit_series": fit_series,
        "sliding_repeat_series": sliding_repeat_series,
        "sliding_repeat_pairs_series": sliding_repeat_pairs_series,
    }

    if cfg.write_intermediate:
        cfg.tables_dir.mkdir(parents=True, exist_ok=True)
        summary.to_parquet(cfg.cache_path("repeat_summary"), index=False)
        if do_cumulative:
            cumulative_repeat_series.to_parquet(cfg.cache_path("cumulative_repeat_series"), index=False)
            cumulative_repeat_pairs_series.to_parquet(cfg.cache_path("cumulative_repeat_pairs_series"), index=False)
        fit_params.to_parquet(cfg.cache_path("cumulative_fit_params"), index=False)
        if do_windows:
            sliding_repeat_series.to_parquet(cfg.cache_path("sliding_repeat_series"), index=False)
            sliding_repeat_pairs_series.to_parquet(cfg.cache_path("sliding_repeat_pairs_series"), index=False)
            if cfg.write_window_tables and not sliding_repeat_series.empty:
                for window_days in sorted(sliding_repeat_series["window_days"].dropna().astype(int).unique().tolist()):
                    sliding_repeat_series[
                        sliding_repeat_series["window_days"].astype(int) == int(window_days)
                    ].to_parquet(
                        cfg.cache_path(f"sliding_repeat_series_{int(window_days)}d"),
                        index=False,
                    )
            if cfg.write_window_tables and not sliding_repeat_pairs_series.empty:
                for window_days in sorted(sliding_repeat_pairs_series["window_days"].dropna().astype(int).unique().tolist()):
                    sliding_repeat_pairs_series[
                        sliding_repeat_pairs_series["window_days"].astype(int) == int(window_days)
                    ].to_parquet(
                        cfg.cache_path(f"sliding_repeat_pairs_series_{int(window_days)}d"),
                        index=False,
                    )

    return summary, report


def generate_report(cfg: Optional[Config] = None) -> dict:
    if cfg is None:
        cfg = Config(base_data_dir=Path("./data"))
    _, report = compute_repeat_summary(cfg)
    return report


def plot_report(
    report: dict,
    cfg: Optional[Config] = None,
    *,
    show: bool = True,
    save_dir: Optional[Path] = None,
) -> dict:
    import matplotlib.pyplot as plt

    output = {"saved_paths": []}
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)

    def _finalize(fig, name: str) -> None:
        if save_dir is not None:
            path = save_dir / f"{name}.png"
            fig.savefig(path, dpi=140, bbox_inches="tight")
            output["saved_paths"].append(str(path))
        if show:
            plt.show()
        else:
            plt.close(fig)

    summary = report.get("summary")
    if isinstance(summary, pd.DataFrame) and not summary.empty:
        print("Whole-period ratios by kind:")
        print(summary[["kind", "total_users", "repeat_users_pct_of_total", "repeat_pairs_pct"]])

    cumulative = report.get("cumulative_repeat_series")
    if isinstance(cumulative, pd.DataFrame) and not cumulative.empty:
        for kind, g in cumulative.groupby("kind"):
            gg = g.sort_values("day_index")
            fig = plt.figure(figsize=(10, 4))
            plt.plot(gg["day_index"], gg["ratio"], label="cumulative")
            plt.title(f"Cumulative repeat ratio (user-based) - {kind}")
            plt.xlabel("day index")
            plt.ylabel("ratio of users")
            plt.figtext(
                0.5,
                -0.02,
                "Ratio = repeated users up to day t / total_users",
                ha="center",
                fontsize=9,
            )
            plt.legend()
            plt.grid(alpha=0.2)
            _finalize(fig, f"cumulative_users_{kind}")

    cumulative_pairs = report.get("cumulative_repeat_pairs_series")
    if isinstance(cumulative_pairs, pd.DataFrame) and not cumulative_pairs.empty:
        for kind, g in cumulative_pairs.groupby("kind"):
            gg = g.sort_values("day_index")
            fig = plt.figure(figsize=(10, 4))
            plt.plot(gg["day_index"], gg["ratio"], label="cumulative")
            plt.title(f"Cumulative repeat pairs pct (activity-based) - {kind}")
            plt.xlabel("day index")
            plt.ylabel("ratio of pairs")
            plt.figtext(
                0.5,
                -0.02,
                "Ratio = repeated pairs up to day t / total pairs up to day t",
                ha="center",
                fontsize=9,
            )
            plt.legend()
            plt.grid(alpha=0.2)
            _finalize(fig, f"cumulative_pairs_{kind}")

    do_sliding = bool(cfg.compute_sliding_series) if cfg is not None else True
    if do_sliding:
        sliding = report.get("sliding_repeat_series")
        if isinstance(sliding, pd.DataFrame) and not sliding.empty:
            max_day_index = sliding["day_index"].max() if "day_index" in sliding.columns else None
            for kind, g in sliding.groupby("kind"):
                fig = plt.figure(figsize=(10, 4))
                for window, gg in g.groupby("window_days"):
                    gg = gg.sort_values("day_index")
                    label = f"{int(window)}d"
                    if max_day_index is not None and int(window) == int(max_day_index) + 1:
                        label = "all"
                    plt.plot(gg["day_index"], gg["ratio"], label=label)
                plt.title(f"Sliding repeat ratio (user-based) - {kind}")
                plt.xlabel("day index")
                plt.ylabel("ratio of users")
                plt.figtext(
                    0.5,
                    -0.02,
                    "Ratio = repeated users in trailing window / total_users",
                    ha="center",
                    fontsize=9,
                )
                plt.legend()
                plt.grid(alpha=0.2)
                _finalize(fig, f"sliding_users_{kind}")

        sliding_pairs = report.get("sliding_repeat_pairs_series")
        if isinstance(sliding_pairs, pd.DataFrame) and not sliding_pairs.empty:
            max_day_index = sliding_pairs["day_index"].max() if "day_index" in sliding_pairs.columns else None
            for kind, g in sliding_pairs.groupby("kind"):
                fig = plt.figure(figsize=(10, 4))
                for window, gg in g.groupby("window_days"):
                    gg = gg.sort_values("day_index")
                    label = f"{int(window)}d"
                    if max_day_index is not None and int(window) == int(max_day_index) + 1:
                        label = "all"
                    plt.plot(gg["day_index"], gg["ratio"], label=label)
                plt.title(f"Sliding repeat pairs pct (activity-based) - {kind}")
                plt.xlabel("day index")
                plt.ylabel("ratio of pairs")
                plt.figtext(
                    0.5,
                    -0.02,
                    "Ratio = repeated pairs in trailing window / total pairs in trailing window",
                    ha="center",
                    fontsize=9,
                )
                plt.legend()
                plt.grid(alpha=0.2)
                _finalize(fig, f"sliding_pairs_{kind}")
    else:
        print("Sliding series disabled (compute_sliding_series=False).")

    return output


def generate_report_with_plots(
    cfg: Optional[Config] = None,
    *,
    show: bool = True,
    save_dir: Optional[Path] = None,
) -> dict:
    if cfg is None:
        cfg = Config(base_data_dir=Path("./data"))
    report = generate_report(cfg)
    plot_report(report, cfg, show=show, save_dir=save_dir)
    return report


def config_from_json(path: Path) -> Config:
    payload = json.loads(path.read_text())
    payload["base_data_dir"] = Path(payload["base_data_dir"])
    for key in ["raw_dir_override", "parquet_dir_override", "intermediate_dir_override"]:
        if payload.get(key) is not None:
            payload[key] = Path(payload[key])
    return Config(**payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Repeated reshare/like ratio report (PySpark)")
    parser.add_argument("--config-json", type=Path, default=None)
    args = parser.parse_args()

    if args.config_json:
        cfg = config_from_json(args.config_json)
    else:
        cfg = Config(base_data_dir=Path("./data"))

    report = generate_report(cfg)
    print(report["summary"])
    print(report["cumulative_fit_params"])


if __name__ == "__main__":
    main()
