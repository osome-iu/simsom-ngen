#!/usr/bin/env python3
# Copyright © 2026 The Trustees of Indiana University and contributors.
# This work is licensed under the Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0).
# You may use, share, and adapt this work for non-commercial purposes, subject to the terms of the license and appropriate attribution.
# See the LICENSE file in the root of this repository for the full license terms.
# https://creativecommons.org/licenses/by-nc/4.0/

from __future__ import annotations

import argparse
import gzip
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

TARGET_TYPES = {
    "app.bsky.feed.post",
    "app.bsky.feed.repost",
    "app.bsky.feed.like",
}

try:
    import orjson as _jsonlib
    JSONDecodeError = _jsonlib.JSONDecodeError

    def loads_line(line: bytes):
        return _jsonlib.loads(line)

except Exception:
    JSONDecodeError = json.JSONDecodeError

    def loads_line(line: bytes):
        return json.loads(line.decode("utf-8", errors="replace"))


def get_rtype(rec: dict) -> str | None:
    return rec.get("type") or rec.get("$type")


def get_day(rec: dict) -> str | None:
    val = rec.get("commit_time_str")
    if isinstance(val, str) and len(val) >= 10:
        return val[:10]
    return None


def extract_embed_post_uri(rec: dict) -> str | None:
    embed = rec.get("embed")
    if not isinstance(embed, dict):
        return None

    record = embed.get("record")
    if isinstance(record, dict):
        inner = record.get("record") if isinstance(record.get("record"), dict) else record
        uri = inner.get("uri") if isinstance(inner, dict) else None
        if isinstance(uri, str) and "/app.bsky.feed.post/" in uri:
            return uri

    return None


def classify_post(rec: dict) -> str:
    if rec.get("reply"):
        return "reply"

    embed_post_uri = extract_embed_post_uri(rec)
    if embed_post_uri:
        return "quote"

    embed = rec.get("embed")
    if isinstance(embed, dict):
        etype = embed.get("$type", "")
        if etype in ("app.bsky.embed.record", "app.bsky.embed.recordWithMedia"):
            return "quote"
        if "record" in embed:
            return "quote"

    return "original post"


def classify_action(rec: dict, rtype: str | None = None) -> str | None:
    rtype = rtype or get_rtype(rec)
    if rtype == "app.bsky.feed.like":
        return "like"
    if rtype == "app.bsky.feed.repost":
        return "repost"
    if rtype == "app.bsky.feed.post":
        return classify_post(rec)
    return None


def extract_subject_uri(rec: dict) -> str | None:
    subject = rec.get("subject")
    if not isinstance(subject, dict):
        return None
    uri = subject.get("uri")
    if isinstance(uri, str):
        return uri
    return None


def _arrow_type(type_name: str):
    import pyarrow as pa

    if type_name == "float":
        return pa.float64()
    if type_name == "int":
        return pa.int64()
    return pa.string()


def file_fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime}


def _relpath(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except Exception:
        return os.path.relpath(str(path))


def load_raw_manifest(raw_manifest_file: Path) -> dict:
    if not raw_manifest_file.exists():
        return {"files": {}}
    try:
        return json.loads(raw_manifest_file.read_text())
    except Exception:
        return {"files": {}}


def process_file(path_str: str, settings: dict) -> dict:
    path = Path(path_str)
    parquet_dir = Path(settings["parquet_dir"])
    bad_json_dir = Path(settings["bad_json_dir"])
    buffer_size = int(settings["buffer_size"])
    schema = settings["schema"]

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except Exception as exc:
        return {"file": path.name, "rows": 0, "bad": 0, "error": f"pyarrow missing: {exc}"}

    parquet_dir.mkdir(parents=True, exist_ok=True)
    bad_json_dir.mkdir(parents=True, exist_ok=True)

    out_name = path.name.replace(".jsonl.gz", "").replace(".json.gz", "") + ".parquet"
    out_path = parquet_dir / out_name

    arrow_schema = pa.schema([(k, _arrow_type(v)) for k, v in schema.items()])
    writer = pq.ParquetWriter(out_path, arrow_schema)

    buffer = []
    rows_written = 0
    bad_lines = 0
    bad_handle = None
    bad_path = bad_json_dir / f"{path.name}.bad.jsonl"

    with gzip.open(path, "rb") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = loads_line(line)
            except JSONDecodeError:
                bad_lines += 1
                if bad_handle is None:
                    bad_handle = open(bad_path, "wb")
                bad_handle.write(line + b"\n")
                continue

            rtype = get_rtype(rec)
            if rtype not in TARGET_TYPES:
                continue

            action = classify_action(rec, rtype)
            if not action:
                continue

            embed_post_uri = extract_embed_post_uri(rec)
            subject_uri = None
            if rtype in ("app.bsky.feed.repost", "app.bsky.feed.like"):
                subject_uri = extract_subject_uri(rec)

            row = {
                "author": rec.get("author") or "unknown",
                "uri": rec.get("uri"),
                "action": action,
                "type": rtype,
                "commit_action": rec.get("action"),
                "commit_time_str": rec.get("commit_time_str"),
                "commit_time": rec.get("commit_time"),
                "day": get_day(rec),
                "file": path.name,
                "subject_uri": subject_uri,
                "embed_post_uri": embed_post_uri,
            }
            buffer.append(row)

            if len(buffer) >= buffer_size:
                table = pa.Table.from_pylist(buffer, schema=arrow_schema)
                writer.write_table(table)
                rows_written += len(buffer)
                buffer = []

    if buffer:
        table = pa.Table.from_pylist(buffer, schema=arrow_schema)
        writer.write_table(table)
        rows_written += len(buffer)

    writer.close()

    if bad_handle is not None:
        bad_handle.close()

    return {"file": path.name, "rows": rows_written, "bad": bad_lines}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Parse Bluesky JSONL .gz files to parquet (one per file)."
    )
    parser.add_argument("--raw-dir", required=True, type=Path, help="Raw data folder (data/raw)")
    parser.add_argument("--parquet-dir", required=True, type=Path, help="Parquet output folder (data/parquet)")
    parser.add_argument("--schema-version", type=int, default=1)
    parser.add_argument("--buffer-size", type=int, default=50_000)
    parser.add_argument("--max-workers", type=int, default=0, help="0 = auto (min(cpu_count, file_count))")
    parser.add_argument("--parallel-mode", choices=["processes", "off"], default="processes")
    parser.add_argument("--force", action="store_true", help="Force rebuild all parquet files")
    args = parser.parse_args()

    raw_dir = args.raw_dir
    parquet_dir = args.parquet_dir
    parquet_dir.mkdir(parents=True, exist_ok=True)

    parquet_ready_file = parquet_dir / f"_PARQUET_READY_v{args.schema_version}"
    stats_ready_file = parquet_dir / f"_STATS_READY_v{args.schema_version}"
    raw_manifest_file = parquet_dir / "raw_manifest.json"
    schema_file = parquet_dir / f"schema_v{args.schema_version}.json"
    bad_json_dir = parquet_dir / "bad_json"

    files = list(raw_dir.glob("*.json.gz"))
    files += [p for p in raw_dir.glob("*.jsonl.gz") if p not in files]
    files = sorted(files)

    schema = {
        "author": "string",
        "uri": "string",
        "action": "string",
        "type": "string",
        "commit_action": "string",
        "commit_time_str": "string",
        "commit_time": "float",
        "day": "string",
        "file": "string",
        "subject_uri": "string",
        "embed_post_uri": "string",
    }
    schema_file.write_text(json.dumps(schema, indent=2))

    raw_manifest = load_raw_manifest(raw_manifest_file)
    files_to_build = files if args.force else [
        p for p in files if raw_manifest.get("files", {}).get(p.name) != file_fingerprint(p)
    ]

    if not files_to_build:
        print("No new or changed raw files. Nothing to do.")
        return 0
    if args.max_workers <= 0:
        args.max_workers = max(1, min((os.cpu_count() or 2), len(files_to_build)))

    settings = {
        "parquet_dir": str(parquet_dir),
        "bad_json_dir": str(bad_json_dir),
        "buffer_size": args.buffer_size,
        "schema": schema,
    }

    results = []
    if args.parallel_mode == "processes" and len(files_to_build) > 1:
        with ProcessPoolExecutor(max_workers=args.max_workers) as ex:
            futures = [ex.submit(process_file, str(p), settings) for p in files_to_build]
            for fut in as_completed(futures):
                results.append(fut.result())
    else:
        for p in files_to_build:
            results.append(process_file(str(p), settings))

    errors = [r for r in results if r.get("error")]
    if errors:
        raise RuntimeError(f"Parquet build errors: {errors[:3]}")

    total_rows = sum(r.get("rows", 0) for r in results)
    total_bad = sum(r.get("bad", 0) for r in results)
    print(f"Wrote parquet rows: {total_rows:,}")
    print(f"Bad JSON lines: {total_bad:,} (see {_relpath(bad_json_dir)})")

    raw_manifest = {"files": {}}
    for p in files:
        raw_manifest["files"][p.name] = file_fingerprint(p)
    raw_manifest_file.write_text(json.dumps(raw_manifest, indent=2))

    parquet_ready_file.write_text("ok")
    stats_ready_file.write_text("pending")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
