#!/usr/bin/env python3
# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

"""
User-state lurker ratio pipeline for raw Bluesky Firehose archives.

Terminology
- seen_users (seen_users in the report): unique users seen in the data,
  excluding app.bsky.actor.profile create/delete records
- non_lurkers: seen users with at least one qualifying create of post/repost/quote/reply/like
- lurkers: seen_users - non_lurkers

Exact metrics
1) Full-period cumulative
   seen_users(t)  = count of unique authors with first_seen_day <= t
   non_lurkers(t)   = count of unique authors with first_non_lurker_day <= t
   lurkers(t)       = seen_users(t) - non_lurkers(t)

2) Sliding window, trailing W days ending at day t
   seen_users_W(t)  = unique authors with any seen day in [t-W+1, t]
   non_lurkers_W(t)   = unique authors with any non-lurker day in [t-W+1, t]
   lurkers_W(t)       = seen_users_W(t) - non_lurkers_W(t)

Design
- recursive file discovery with symlink support
- stable sharding by author id
- disk spill of per-file shard parts
- external merge per shard
- exact shard-local user-state reduction
- stage logging with timing and throughput

Defaults
- --data-path ./data/raw
- --output-dir ./data/output
- --full-day-min-hours 22
- --max-workers os.cpu_count()
- --windows 1,7,30,60
"""

from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import hashlib
import heapq
import json
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------- logging ----------

def log(msg: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)

# ---------- json loader ----------

try:
    import orjson as _jsonlib
    def loads_line(line: bytes):
        return _jsonlib.loads(line)
except Exception:
    def loads_line(line: bytes):
        return json.loads(line.decode("utf-8", errors="replace"))

# ---------- constants ----------

NON_LURKING_TYPES = {
    "app.bsky.feed.post",
    "app.bsky.feed.repost",
    "app.bsky.feed.like"
}

# ---------- helpers ----------

def normalize_day_bound(value: Optional[str], end: bool = False) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 7:
        year, month = [int(x) for x in text.split("-")]
        day = calendar.monthrange(year, month)[1] if end else 1
        return f"{year:04d}-{month:02d}-{day:02d}"
    if len(text) == 10:
        return text
    raise ValueError(f"Invalid day bound: {value}")

def parse_windows(text: str) -> tuple[int, ...]:
    vals = []
    for part in str(text).split(","):
        part = part.strip()
        if part:
            vals.append(int(part))
    vals = tuple(sorted(set(v for v in vals if v > 0)))
    if not vals:
        raise ValueError("At least one positive window is required.")
    return vals

def ensure_dir(path: Path, force: bool = False) -> None:
    if force and path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)

def stable_author_shard(author: str, shards: int) -> int:
    digest = hashlib.blake2b(author.encode("utf-8", errors="replace"), digest_size=8).digest()
    return int.from_bytes(digest, "little", signed=False) % shards

def is_non_lurking_record(rec: dict) -> bool:
    raw_type = rec.get("raw_type") or rec.get("type") or rec.get("$type")
    return rec.get("action") == "create" and raw_type in NON_LURKING_TYPES

def counts_for_seen(rec: dict) -> bool:
    raw_type = rec.get("raw_type") or rec.get("type") or rec.get("$type")
    action = rec.get("action")
    return not (raw_type == "app.bsky.actor.profile" and action in {"create", "delete"})

def extract_day_and_hour(rec: dict):
    ts = rec.get("commit_time_str") or rec.get("timestamp")
    if not isinstance(ts, str) or len(ts) < 13:
        return None, None
    day = ts[:10]
    try:
        hour = int(ts[11:13])
    except Exception:
        hour = None
    return day, hour

def open_text_write(path: Path, compression: str):
    if compression == "gz":
        return gzip.open(path, "wt", encoding="utf-8", newline="")
    return open(path, "w", encoding="utf-8", newline="")

def open_text_read(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", newline="")
    return open(path, "r", encoding="utf-8", newline="")

def discover_raw_files(data_path: Path, start_day: Optional[str], end_day: Optional[str]) -> list[Path]:
    if not data_path.exists():
        raise ValueError(f"Path does not exist: {data_path}")

    files: list[Path] = []
    for root, _dirs, filenames in os.walk(data_path, followlinks=True):
        for name in filenames:
            lower = name.lower()
            if lower.startswith("."):
                continue
            if lower.endswith(".json") and not lower.endswith(".json.gz"):
                continue
            if not lower.endswith(".gz"):
                continue

            file_day = None
            if len(name) >= 10 and name[4] == "-" and name[7] == "-":
                file_day = name[:10]

            if file_day:
                if start_day and file_day < start_day:
                    continue
                if end_day and file_day > end_day:
                    continue

            files.append(Path(root) / name)

    files.sort()
    print(f"[discover] found {len(files):,} files under {data_path}", flush=True)
    if files:
        print(f"[discover] sample: {files[0]}", flush=True)
    return files

def interval_union(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    intervals.sort()
    out = [intervals[0]]
    for s, e in intervals[1:]:
        ps, pe = out[-1]
        if s <= pe + 1:
            out[-1] = (ps, max(pe, e))
        else:
            out.append((s, e))
    return out

def add_interval(diff: list[int], start: int, end: int, weight: int = 1) -> None:
    if start > end:
        return
    diff[start] += weight
    if end + 1 < len(diff):
        diff[end + 1] -= weight

def prefix_from_diff(diff: list[int]) -> list[int]:
    out = []
    running = 0
    for v in diff[:-1]:
        running += v
        out.append(running)
    return out

def apply_first_week_variant(
    seen_days: list[int],
    non_lurker_days: list[int],
    mode: str,
) -> tuple[list[int], list[int]]:
    if not seen_days:
        return [], []
    burn_end = seen_days[0] + 6
    non_lurker_set = set(non_lurker_days)
    if mode == "baseline":
        seen_out = list(seen_days)
        non_lurker_out = list(non_lurker_days)
    elif mode == "ignore_first_week_lurking":
        seen_out = [
            idx for idx in seen_days
            if idx > burn_end or idx in non_lurker_set
        ]
        non_lurker_out = list(non_lurker_days)
    elif mode == "ignore_first_week_all_activity":
        seen_out = [idx for idx in seen_days if idx > burn_end]
        non_lurker_out = [idx for idx in non_lurker_days if idx > burn_end]
    else:
        raise ValueError(f"Unknown variant mode: {mode}")
    seen_out = sorted(dict.fromkeys(seen_out))
    non_lurker_out = sorted(idx for idx in dict.fromkeys(non_lurker_out) if idx in set(seen_out))
    return seen_out, non_lurker_out
# ---------- stage 1: parse + spill ----------

def process_raw_file(
    path_str: str,
    stage1_dir_str: str,
    shards: int,
    flush_entries: int,
    analysis_start_day: Optional[str],
    analysis_end_day: Optional[str],
    spill_compression: str,
    worker_log_every: int,
) -> dict:
    path = Path(path_str)
    stage1_dir = Path(stage1_dir_str)
    file_tag = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]

    shard_maps: list[dict[tuple[str, str], list[int]]] = [dict() for _ in range(shards)]
    day_hour_masks: dict[str, int] = {}

    spill_seq = 0
    total_lines = 0
    kept_lines = 0
    bad_lines = 0
    spills = 0
    approx_entries = 0

    def flush() -> None:
        nonlocal spill_seq, spills, approx_entries
        for shard_id, shard_map in enumerate(shard_maps):
            if not shard_map:
                continue
            shard_dir = stage1_dir / f"shard={shard_id:04d}"
            shard_dir.mkdir(parents=True, exist_ok=True)
            suffix = ".tsv.gz" if spill_compression == "gz" else ".tsv"
            out_path = shard_dir / f"part_{file_tag}_{spill_seq:06d}{suffix}"

            items = sorted(shard_map.items(), key=lambda kv: kv[0])
            with open_text_write(out_path, spill_compression) as handle:
                for (author, day), (seen_flag, non_lurker_flag) in items:
                    handle.write(f"{author}\t{day}\t{seen_flag}\t{non_lurker_flag}\n")

            shard_map.clear()
            spills += 1

        spill_seq += 1
        approx_entries = 0

    with gzip.open(path, "rb") as handle:
        for raw_line in handle:
            total_lines += 1
            if worker_log_every and total_lines % worker_log_every == 0:
                print(f"[worker] {path.name}: {total_lines:,} lines", flush=True)

            line = raw_line.strip()
            if not line:
                continue

            try:
                rec = loads_line(line)
            except Exception:
                bad_lines += 1
                continue

            author = rec.get("author")
            if not isinstance(author, str) or not author:
                continue

            day, hour = extract_day_and_hour(rec)
            if day is None:
                continue
            if analysis_start_day and day < analysis_start_day:
                continue
            if analysis_end_day and day > analysis_end_day:
                continue

            kept_lines += 1
            if hour is not None and 0 <= hour <= 23:
                day_hour_masks[day] = day_hour_masks.get(day, 0) | (1 << hour)

            if not counts_for_seen(rec):
                continue

            shard_id = stable_author_shard(author, shards)
            key = (author, day)
            nl = 1 if is_non_lurking_record(rec) else 0
            state = shard_maps[shard_id].get(key)
            if state is None:
                shard_maps[shard_id][key] = [1, nl]
                approx_entries += 1
            else:
                state[0] = 1
                if nl:
                    state[1] = 1

            if approx_entries >= flush_entries:
                flush()

    if approx_entries > 0:
        flush()

    coverage_path = stage1_dir / "_coverage" / f"coverage_{file_tag}.json"
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    coverage_path.write_text(json.dumps(day_hour_masks, separators=(",", ":")))

    return {
        "path": str(path),
        "lines": total_lines,
        "kept_lines": kept_lines,
        "bad_lines": bad_lines,
        "spills": spills,
    }

# ---------- stage 1b: coverage ----------

def reduce_coverage(stage1_dir: Path, full_day_min_hours: float, reports_dir: Path) -> list[str]:
    coverage_dir = stage1_dir / "_coverage"
    files = sorted(coverage_dir.glob("coverage_*.json")) if coverage_dir.exists() else []
    merged: dict[str, int] = {}

    stage_start = time.time()
    log(f"Stage 1b/4 started: reducing coverage from {len(files):,} manifests")

    for idx, path in enumerate(files, start=1):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        for day, mask in data.items():
            merged[day] = merged.get(day, 0) | int(mask)

        if idx % 500 == 0 or idx == len(files):
            elapsed = time.time() - stage_start
            speed = idx / elapsed if elapsed > 0 else 0.0
            log(f"Stage 1b progress: {idx:,}/{len(files):,} manifests ({speed:,.2f} files/sec)")

    threshold = int(full_day_min_hours)
    full_days = [day for day, mask in merged.items() if int(mask).bit_count() >= threshold]
    full_days.sort()

    coverage_csv = reports_dir / "day_coverage.csv"
    with open(coverage_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["day", "hours_present", "is_full_day"])
        for day in sorted(merged):
            hours_present = int(merged[day]).bit_count()
            writer.writerow([day, hours_present, int(hours_present >= threshold)])

    elapsed = time.time() - stage_start
    speed = len(files) / elapsed if elapsed > 0 else 0.0
    log(f"Stage 1b/4 done in {elapsed:.2f}s")
    log(f"Stage 1b speed: {speed:,.2f} files/sec")
    return full_days

# ---------- stage 2: merge parts ----------

class PartIterator:
    def __init__(self, path: Path):
        self.handle = open_text_read(path)
        self.current = self._advance()

    def _advance(self):
        line = self.handle.readline()
        if not line:
            self.handle.close()
            return None
        author, day, seen_flag, non_lurker_flag = line.rstrip("\n").split("\t")
        return (author, day, int(seen_flag), int(non_lurker_flag))

    def pop(self):
        value = self.current
        self.current = self._advance()
        return value

def merge_one_shard(shard_dir: Path, merged_dir: Path) -> dict:
    part_paths = sorted(
        p for p in shard_dir.glob("part_*")
        if p.is_file() and (str(p).endswith(".tsv") or str(p).endswith(".tsv.gz"))
    )
    merged_path = merged_dir / f"{shard_dir.name}.tsv"
    merged_dir.mkdir(parents=True, exist_ok=True)

    if not part_paths:
        merged_path.write_text("")
        return {"shard": shard_dir.name, "rows": 0, "parts": 0}

    iters = [PartIterator(p) for p in part_paths]
    heap = []
    for idx, it in enumerate(iters):
        if it.current is not None:
            a, d, act, nl = it.current
            heapq.heappush(heap, (a, d, idx, act, nl))

    out_rows = 0
    with open(merged_path, "w", encoding="utf-8", newline="") as out:
        current_key = None
        current_seen = 0
        current_non_lurker = 0

        while heap:
            author, day, idx, seen_flag, non_lurker_flag = heapq.heappop(heap)
            key = (author, day)

            if current_key is None:
                current_key = key
                current_seen = seen_flag
                current_non_lurker = non_lurker_flag
            elif key == current_key:
                current_seen = max(current_seen, seen_flag)
                current_non_lurker = max(current_non_lurker, non_lurker_flag)
            else:
                out.write(f"{current_key[0]}\t{current_key[1]}\t{current_seen}\t{current_non_lurker}\n")
                out_rows += 1
                current_key = key
                current_seen = seen_flag
                current_non_lurker = non_lurker_flag

            iters[idx].pop()
            if iters[idx].current is not None:
                a2, d2, act2, nl2 = iters[idx].current
                heapq.heappush(heap, (a2, d2, idx, act2, nl2))

        if current_key is not None:
            out.write(f"{current_key[0]}\t{current_key[1]}\t{current_seen}\t{current_non_lurker}\n")
            out_rows += 1

    return {"shard": shard_dir.name, "rows": out_rows, "parts": len(part_paths)}

# ---------- stage 3: exact user-state metrics ----------

def compute_metrics_for_shard(
    shard_path_str: str,
    full_days: list[str],
    day_to_idx: dict[str, int],
    windows: tuple[int, ...],
    alignment: str,
    user_state_dir_str: Optional[str],
) -> dict:
    shard_path = Path(shard_path_str)
    user_state_path = None
    user_state_handle = None
    user_state_writer = None

    variant_specs = {
        "baseline": {"prefix": ""},
        "ignore_first_week_lurking": {"prefix": "ignore_first_week_lurking_"},
        "ignore_first_week_all_activity": {"prefix": "ignore_first_week_all_activity_"},
    }

    if user_state_dir_str is not None:
        user_state_dir = Path(user_state_dir_str)
        user_state_dir.mkdir(parents=True, exist_ok=True)

        user_state_path = user_state_dir / f"{shard_path.stem}.csv"

        user_state_handle = open(user_state_path, "w", newline="", encoding="utf-8")
        user_state_writer = csv.writer(user_state_handle)

        header = [
            "author",
            "first_seen_day",
            "first_non_lurker_day",
            "last_seen_day",
            "last_non_lurker_day",
            "seen_day_count",
            "non_lurker_day_count",
        ]
        for variant_name, spec in variant_specs.items():
            if variant_name == "baseline":
                continue
            prefix = spec["prefix"]
            header.extend([
                f"first_seen_day_{prefix[:-1]}",
                f"first_non_lurker_day_{prefix[:-1]}",
                f"last_seen_day_{prefix[:-1]}",
                f"last_non_lurker_day_{prefix[:-1]}",
                f"seen_day_count_{prefix[:-1]}",
                f"non_lurker_day_count_{prefix[:-1]}",
            ])
        user_state_writer.writerow(header)

    day_count = len(full_days)

    seen_diffs = {variant: {w: [0] * (day_count + 1) for w in windows} for variant in variant_specs}
    non_lurker_diffs = {variant: {w: [0] * (day_count + 1) for w in windows} for variant in variant_specs}
    first_seen_events = {variant: [0] * day_count for variant in variant_specs}
    first_non_lurker_events = {variant: [0] * day_count for variant in variant_specs}
    users_processed = 0

    def build_variant_days(seen_days: list[int], non_lurker_days: list[int]) -> dict[str, tuple[list[int], list[int]]]:
        first_seen = seen_days[0]
        cutoff = first_seen + 7
        non_lurker_set = set(non_lurker_days)

        baseline_seen = list(seen_days)
        baseline_non = list(non_lurker_days)

        ignore_lurking_seen = [idx for idx in seen_days if idx >= cutoff or idx in non_lurker_set]
        ignore_lurking_non = list(non_lurker_days)

        ignore_all_seen = [idx for idx in seen_days if idx >= cutoff]
        ignore_all_non = [idx for idx in non_lurker_days if idx >= cutoff]

        return {
            "baseline": (baseline_seen, baseline_non),
            "ignore_first_week_lurking": (ignore_lurking_seen, ignore_lurking_non),
            "ignore_first_week_all_activity": (ignore_all_seen, ignore_all_non),
        }

    def flush_author(author: str, seen_days: list[int], non_lurker_days: list[int]) -> None:
        nonlocal users_processed
        if not seen_days:
            return
        users_processed += 1

        variant_days = build_variant_days(seen_days, non_lurker_days)

        for variant_name, (variant_seen_days, variant_non_lurker_days) in variant_days.items():
            if variant_seen_days:
                first_seen_events[variant_name][variant_seen_days[0]] += 1
            if variant_non_lurker_days:
                first_non_lurker_events[variant_name][variant_non_lurker_days[0]] += 1

            seen_by_w = {w: [] for w in windows}
            non_lurker_by_w = {w: [] for w in windows}

            for idx in variant_seen_days:
                for w in windows:
                    if alignment == "trailing":
                        start = max(0, idx - w + 1)
                        end = idx
                    else:
                        start = idx
                        end = min(day_count - 1, idx + w - 1)
                    seen_by_w[w].append((start, end))

            for idx in variant_non_lurker_days:
                for w in windows:
                    if alignment == "trailing":
                        start = max(0, idx - w + 1)
                        end = idx
                    else:
                        start = idx
                        end = min(day_count - 1, idx + w - 1)
                    non_lurker_by_w[w].append((start, end))

            for w in windows:
                for s, e in interval_union(seen_by_w[w]):
                    add_interval(seen_diffs[variant_name][w], s, e, 1)
                for s, e in interval_union(non_lurker_by_w[w]):
                    add_interval(non_lurker_diffs[variant_name][w], s, e, 1)

        if user_state_writer is not None:
            row = [
                author,
                full_days[seen_days[0]],
                full_days[non_lurker_days[0]] if non_lurker_days else "",
                full_days[seen_days[-1]],
                full_days[non_lurker_days[-1]] if non_lurker_days else "",
                len(seen_days),
                len(non_lurker_days),
            ]

            for variant_name in ("ignore_first_week_lurking", "ignore_first_week_all_activity"):
                variant_seen_days, variant_non_lurker_days = variant_days[variant_name]
                row.extend([
                    full_days[variant_seen_days[0]] if variant_seen_days else "",
                    full_days[variant_non_lurker_days[0]] if variant_non_lurker_days else "",
                    full_days[variant_seen_days[-1]] if variant_seen_days else "",
                    full_days[variant_non_lurker_days[-1]] if variant_non_lurker_days else "",
                    len(variant_seen_days),
                    len(variant_non_lurker_days),
                ])
            user_state_writer.writerow(row)

    prev_author = None
    seen_days: list[int] = []
    non_lurker_days: list[int] = []
    last_seen_idx = None
    last_non_lurker_idx = None

    with open(shard_path, "r", encoding="utf-8", newline="") as handle:
        for line in handle:
            author, day, seen_flag, non_lurker_flag = line.rstrip("\n").split("\t")
            idx = day_to_idx.get(day)
            if idx is None:
                continue

            if prev_author is None:
                prev_author = author

            if author != prev_author:
                flush_author(prev_author, seen_days, non_lurker_days)
                prev_author = author
                seen_days = []
                non_lurker_days = []
                last_seen_idx = None
                last_non_lurker_idx = None

            if int(seen_flag) > 0 and idx != last_seen_idx:
                seen_days.append(idx)
                last_seen_idx = idx
            if int(non_lurker_flag) > 0 and idx != last_non_lurker_idx:
                non_lurker_days.append(idx)
                last_non_lurker_idx = idx

    if prev_author is not None:
        flush_author(prev_author, seen_days, non_lurker_days)

    if user_state_handle is not None:
        user_state_handle.close()

    return {
        "seen_diffs": seen_diffs,
        "non_lurker_diffs": non_lurker_diffs,
        "first_seen_events": first_seen_events,
        "first_non_lurker_events": first_non_lurker_events,
        "users_processed": users_processed,
        "user_state_path": str(user_state_path) if user_state_path else None,
    }

# ---------- stage 4: write reports ----------

def write_reports(
    reports_dir: Path,
    full_days: list[str],
    windows: tuple[int, ...],
    seen_series: dict[int, list[int]],
    non_lurker_series: dict[int, list[int]],
    first_seen_events: list[int],
    first_non_lurker_events: list[int],
    prefix: str = "",
) -> None:
    daily_csv = reports_dir / f"{prefix}lurker_ratio_daily.csv"
    monthly_csv = reports_dir / f"{prefix}lurker_ratio_monthly.csv"
    cumulative_csv = reports_dir / f"{prefix}full_period_cumulative.csv"

    with open(daily_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["day", "window_size", "seen_users", "lurkers", "non_lurkers", "lurker_ratio"])
        for w in windows:
            seen = seen_series[w]
            non_lurkers = non_lurker_series[w]
            for day, seen_users, non_lurkers_count in zip(full_days, seen, non_lurkers):
                lurkers = max(seen_users - non_lurkers_count, 0)
                ratio = (lurkers / seen_users) if seen_users else 0.0
                writer.writerow([day, w, seen_users, lurkers, non_lurkers_count, f"{ratio:.12f}"])

    monthly = {}
    for w in windows:
        seen = seen_series[w]
        non_lurkers = non_lurker_series[w]
        for day, seen_users, non_lurkers_count in zip(full_days, seen, non_lurkers):
            month = day[:7] + "-01"
            lurkers = max(seen_users - non_lurkers_count, 0)
            key = (month, w)
            bucket = monthly.setdefault(key, {"seen_sum": 0.0, "lurkers_sum": 0.0, "non_lurkers_sum": 0.0, "days": 0})
            bucket["seen_sum"] += seen_users
            bucket["lurkers_sum"] += lurkers
            bucket["non_lurkers_sum"] += non_lurkers_count
            bucket["days"] += 1

    with open(monthly_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["month", "window_size", "avg_seen_users", "avg_lurkers", "avg_non_lurkers", "lurker_ratio"])
        for month, w in sorted(monthly):
            bucket = monthly[(month, w)]
            days = max(int(bucket["days"]), 1)
            avg_seen = bucket["seen_sum"] / days
            avg_lurkers = bucket["lurkers_sum"] / days
            avg_non_lurkers = bucket["non_lurkers_sum"] / days
            ratio = (avg_lurkers / avg_seen) if avg_seen else 0.0
            writer.writerow([month, w, f"{avg_seen:.6f}", f"{avg_lurkers:.6f}", f"{avg_non_lurkers:.6f}", f"{ratio:.12f}"])

    running_seen = 0
    running_non_lurkers = 0
    with open(cumulative_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["day", "seen_users", "non_lurkers", "lurkers", "lurker_ratio"])
        for i, day in enumerate(full_days):
            running_seen += first_seen_events[i]
            running_non_lurkers += first_non_lurker_events[i]
            lurkers = max(running_seen - running_non_lurkers, 0)
            ratio = (lurkers / running_seen) if running_seen else 0.0
            writer.writerow([day, running_seen, running_non_lurkers, lurkers, f"{ratio:.12f}"])

# ---------- main ----------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="TB-scale lurker-ratio pipeline with exact user-state metrics and logging."
    )
    parser.add_argument("--data-path", type=Path, default=Path("./data/raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("./data/output"))
    parser.add_argument("--tmp-dir", type=Path, default=None)
    parser.add_argument("--analysis-start-day", type=str, default=None)
    parser.add_argument("--analysis-end-day", type=str, default=None)
    parser.add_argument("--windows", type=str, default="1,7,30,60")
    parser.add_argument("--alignment", choices=["trailing", "leading"], default="trailing")
    parser.add_argument("--shards", type=int, default=1024)
    parser.add_argument("--max-workers", type=int, default=max(1, (os.cpu_count() or 1)))
    parser.add_argument("--flush-entries", type=int, default=500000)
    parser.add_argument("--full-day-min-hours", type=float, default=22.0)
    parser.add_argument("--spill-compression", choices=["none", "gz"], default="none")
    parser.add_argument("--worker-log-every", type=int, default=5_000_000)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    global_start = time.time()

    analysis_start_day = normalize_day_bound(args.analysis_start_day, end=False)
    analysis_end_day = normalize_day_bound(args.analysis_end_day, end=True)
    windows = parse_windows(args.windows)

    output_dir = Path(args.output_dir)
    tmp_dir = Path(args.tmp_dir) if args.tmp_dir else output_dir / "tmp"
    reports_dir = output_dir / "reports"
    stage1_dir = tmp_dir / "stage1_spills"
    merged_dir = tmp_dir / "stage2_merged_shards"
    user_state_dir = tmp_dir / "stage3_user_state"
    ensure_dir(user_state_dir)

    ensure_dir(output_dir, force=args.force)
    ensure_dir(tmp_dir, force=args.force)
    ensure_dir(reports_dir)
    ensure_dir(stage1_dir)
    ensure_dir(merged_dir)

    raw_files = discover_raw_files(Path(args.data_path), analysis_start_day, analysis_end_day)
    if not raw_files:
        print(f"No raw files found under {args.data_path}", file=sys.stderr)
        return 1

    log(f"Discovered {len(raw_files):,} raw files")
    log(f"Resolved data path: {Path(args.data_path).resolve()}")
    log(f"Using {args.max_workers} worker processes")
    log(f"Output dir: {output_dir}")
    log(f"Windows: {windows}")
    log(f"Full-day threshold: {args.full_day_min_hours} hours")
    log(f"Spill compression: {args.spill_compression}")

    # Stage 1
    stage_start = time.time()
    log("Stage 1/4 started: parsing + spilling")
    worker_results = []
    with ProcessPoolExecutor(max_workers=args.max_workers) as pool:
        futures = [
            pool.submit(
                process_raw_file,
                str(path),
                str(stage1_dir),
                int(args.shards),
                int(args.flush_entries),
                analysis_start_day,
                analysis_end_day,
                str(args.spill_compression),
                int(args.worker_log_every),
            )
            for path in raw_files
        ]
        for idx, fut in enumerate(as_completed(futures), start=1):
            worker_results.append(fut.result())
            if idx % 100 == 0 or idx == len(futures):
                elapsed = time.time() - stage_start
                speed = idx / elapsed if elapsed > 0 else 0.0
                log(f"Stage 1 progress: {idx:,}/{len(futures):,} files ({speed:,.2f} files/sec)")

    stage_elapsed = time.time() - stage_start
    total_lines = sum(r["lines"] for r in worker_results)
    kept_lines = sum(r["kept_lines"] for r in worker_results)
    bad_lines = sum(r["bad_lines"] for r in worker_results)
    spill_files = sum(r["spills"] for r in worker_results)
    lines_per_sec = total_lines / stage_elapsed if stage_elapsed > 0 else 0.0
    log(f"Stage 1/4 done in {stage_elapsed:.2f}s")
    log(f"Stage 1 lines: total={total_lines:,}, kept={kept_lines:,}, bad={bad_lines:,}, spill_files={spill_files:,}")
    log(f"Stage 1 speed: {lines_per_sec:,.0f} lines/sec")

    # Stage 1b
    full_days = reduce_coverage(stage1_dir, float(args.full_day_min_hours), reports_dir)
    if not full_days:
        print("No days survived the hourly coverage filter.", file=sys.stderr)
        return 2
    log(f"Kept {len(full_days):,} full days after coverage filter")

    # Stage 2
    stage_start = time.time()
    log("Stage 2/4 started: merging shard parts")
    shard_dirs = [stage1_dir / f"shard={i:04d}" for i in range(int(args.shards))]
    shard_summaries = []
    for idx, shard_dir in enumerate(shard_dirs, start=1):
        shard_summaries.append(merge_one_shard(shard_dir, merged_dir))
        if idx % 100 == 0 or idx == len(shard_dirs):
            elapsed = time.time() - stage_start
            speed = idx / elapsed if elapsed > 0 else 0.0
            log(f"Stage 2 progress: {idx:,}/{len(shard_dirs):,} shards ({speed:,.2f} shards/sec)")

    stage_elapsed = time.time() - stage_start
    merged_rows = sum(s["rows"] for s in shard_summaries)
    merged_parts = sum(s["parts"] for s in shard_summaries)
    shard_speed = len(shard_dirs) / stage_elapsed if stage_elapsed > 0 else 0.0
    log(f"Stage 2/4 done in {stage_elapsed:.2f}s")
    log(f"Stage 2 merged rows: {merged_rows:,}, input parts: {merged_parts:,}")
    log(f"Stage 2 speed: {shard_speed:,.2f} shards/sec")

    # Stage 3
    stage_start = time.time()
    log("Stage 3/4 started: computing exact user-state metrics")
    merged_shards = sorted(merged_dir.glob("shard=*.tsv"))
    nonempty_merged_shards = [p for p in merged_shards if p.stat().st_size > 0]
    day_to_idx = {day: idx for idx, day in enumerate(full_days)}

    variant_modes = ["baseline", "ignore_first_week_lurking", "ignore_first_week_all_activity"]
    seen_diffs = {mode: {w: [0] * (len(full_days) + 1) for w in windows} for mode in variant_modes}
    non_lurker_diffs = {mode: {w: [0] * (len(full_days) + 1) for w in windows} for mode in variant_modes}
    first_seen_events = {mode: [0] * len(full_days) for mode in variant_modes}
    first_non_lurker_events = {mode: [0] * len(full_days) for mode in variant_modes}
    total_users_processed = 0

    with ProcessPoolExecutor(max_workers=args.max_workers) as pool:
        futures = [
            pool.submit(
                compute_metrics_for_shard,
                str(path),
                full_days,
                day_to_idx,
                windows,
                str(args.alignment),
                str(user_state_dir),
            )
            for path in nonempty_merged_shards
        ]
        for idx, fut in enumerate(as_completed(futures), start=1):
            result = fut.result()

            for mode in variant_modes:
                for w in windows:
                    sa = result["seen_diffs"][mode][w]
                    sn = result["non_lurker_diffs"][mode][w]
                    ba = seen_diffs[mode][w]
                    bn = non_lurker_diffs[mode][w]
                    for i in range(len(ba)):
                        ba[i] += sa[i]
                        bn[i] += sn[i]

                sfa = result["first_seen_events"][mode]
                sfn = result["first_non_lurker_events"][mode]
                for i in range(len(full_days)):

                    first_seen_events[mode][i] += sfa[i]
                    first_non_lurker_events[mode][i] += sfn[i]
            total_users_processed += int(result["users_processed"])

            if idx % 100 == 0 or idx == len(futures):
                elapsed = time.time() - stage_start
                speed = idx / elapsed if elapsed > 0 else 0.0
                log(f"Stage 3 progress: {idx:,}/{len(futures):,} shards ({speed:,.2f} shards/sec)")

    stage_elapsed = time.time() - stage_start
    metric_speed = len(nonempty_merged_shards) / stage_elapsed if stage_elapsed > 0 else 0.0
    log(f"Stage 3/4 done in {stage_elapsed:.2f}s")
    log(f"Stage 3 speed: {metric_speed:,.2f} shards/sec")
    log(f"Stage 3 users processed: {total_users_processed:,}")

    seen_series = {
        mode: {w: prefix_from_diff(seen_diffs[mode][w]) for w in windows}
        for mode in variant_modes
    }
    non_lurker_series = {
        mode: {w: prefix_from_diff(non_lurker_diffs[mode][w]) for w in windows}
        for mode in variant_modes
    }
    # Stage 4
    stage_start = time.time()
    log("Stage 4/4 started: step a and b")
    log("Stage 4a/4 started: writing reports")
    write_reports(
        reports_dir,
        full_days,
        windows,
        seen_series["baseline"],
        non_lurker_series["baseline"],
        first_seen_events["baseline"],
        first_non_lurker_events["baseline"],
        prefix="",
    )
    write_reports(
        reports_dir,
        full_days,
        windows,
        seen_series["ignore_first_week_lurking"],
        non_lurker_series["ignore_first_week_lurking"],
        first_seen_events["ignore_first_week_lurking"],
        first_non_lurker_events["ignore_first_week_lurking"],
        prefix="ignore_first_week_lurking_",
    )
    write_reports(
        reports_dir,
        full_days,
        windows,
        seen_series["ignore_first_week_all_activity"],
        non_lurker_series["ignore_first_week_all_activity"],
        first_seen_events["ignore_first_week_all_activity"],
        first_non_lurker_events["ignore_first_week_all_activity"],
        prefix="ignore_first_week_all_activity_",
    )
    stage_elapsed = time.time() - stage_start


    def concat_user_state(user_state_dir: Path, output_csv: Path) -> int:
        paths = sorted(user_state_dir.glob("*.csv"))
        count = 0

        with open(output_csv, "w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out)
            writer.writerow([
                "author",
                "first_seen_day",
                "first_non_lurker_day",
                "last_seen_day",
                "last_non_lurker_day",
                "seen_day_count",
                "non_lurker_day_count",
                "first_seen_day_ignore_first_week_lurking",
                "first_non_lurker_day_ignore_first_week_lurking",
                "last_seen_day_ignore_first_week_lurking",
                "last_non_lurker_day_ignore_first_week_lurking",
                "seen_day_count_ignore_first_week_lurking",
                "non_lurker_day_count_ignore_first_week_lurking",
                "first_seen_day_ignore_first_week_all_activity",
                "first_non_lurker_day_ignore_first_week_all_activity",
                "last_seen_day_ignore_first_week_all_activity",
                "last_non_lurker_day_ignore_first_week_all_activity",
                "seen_day_count_ignore_first_week_all_activity",
                "non_lurker_day_count_ignore_first_week_all_activity",
            ])

            for path in paths:
                with open(path, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    for row in reader:
                        writer.writerow(row)
                        count += 1

        return count

    log("Stage 4b/4 started: concatenating user_state")

    user_state_csv = reports_dir / "user_state.csv"
    rows = concat_user_state(user_state_dir, user_state_csv)

    log(f"user_state rows: {rows}")

    log(f"Stage 4/4 done in {stage_elapsed:.2f}s")

    manifest = {
        "data_path": str(args.data_path),
        "output_dir": str(output_dir),
        "tmp_dir": str(tmp_dir),
        "analysis_start_day": analysis_start_day,
        "analysis_end_day": analysis_end_day,
        "windows": list(windows),
        "alignment": args.alignment,
        "shards": int(args.shards),
        "max_workers": int(args.max_workers),
        "flush_entries": int(args.flush_entries),
        "full_day_min_hours": float(args.full_day_min_hours),
        "spill_compression": str(args.spill_compression),
        "raw_file_count": len(raw_files),
        "full_day_count": len(full_days),
        "stage1": {
            "total_lines": total_lines,
            "kept_lines": kept_lines,
            "bad_lines": bad_lines,
            "spill_files": spill_files,
        },
        "stage2": {
            "merged_shards": len(merged_shards),
            "merged_rows": merged_rows,
            "parts": merged_parts,
        },
        "stage3": {
            "users_processed": total_users_processed,
        },
        "outputs": {
            "daily_csv": str(reports_dir / "lurker_ratio_daily.csv"),
            "monthly_csv": str(reports_dir / "lurker_ratio_monthly.csv"),
            "coverage_csv": str(reports_dir / "day_coverage.csv"),
            "full_period_cumulative_csv": str(reports_dir / "full_period_cumulative.csv"),
            "user_state_csv": str(reports_dir / "user_state.csv"),
            "ignore_first_week_lurking_daily_csv": str(reports_dir / "ignore_first_week_lurking_lurker_ratio_daily.csv"),
            "ignore_first_week_lurking_monthly_csv": str(reports_dir / "ignore_first_week_lurking_lurker_ratio_monthly.csv"),
            "ignore_first_week_lurking_full_period_cumulative_csv": str(reports_dir / "ignore_first_week_lurking_full_period_cumulative.csv"),
            "ignore_first_week_all_activity_daily_csv": str(reports_dir / "ignore_first_week_all_activity_lurker_ratio_daily.csv"),
            "ignore_first_week_all_activity_monthly_csv": str(reports_dir / "ignore_first_week_all_activity_lurker_ratio_monthly.csv"),
            "ignore_first_week_all_activity_full_period_cumulative_csv": str(reports_dir / "ignore_first_week_all_activity_full_period_cumulative.csv"),
        },
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    total_elapsed = time.time() - global_start
    log(f"TOTAL runtime: {total_elapsed:.2f}s ({total_elapsed/60:.2f} min)")
    log(f"Daily report:   {reports_dir / 'lurker_ratio_daily.csv'}")
    log(f"Monthly report: {reports_dir / 'lurker_ratio_monthly.csv'}")
    log(f"Cumulative:     {reports_dir / 'full_period_cumulative.csv'}")
    log(f"Ignore-1st-week-lurking daily: {reports_dir / 'ignore_first_week_lurking_lurker_ratio_daily.csv'}")
    log(f"Ignore-1st-week-all-activity daily: {reports_dir / 'ignore_first_week_all_activity_lurker_ratio_daily.csv'}")
    log(f"Manifest:       {output_dir / 'manifest.json'}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
