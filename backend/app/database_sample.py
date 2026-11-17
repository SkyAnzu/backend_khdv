"""Pinned, bounded importer for the Database Sample calls/queues bundle."""

from collections import defaultdict
import csv
from datetime import date, datetime, time, timedelta
import hashlib
import io
from typing import Literal
import zipfile

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from .limits import MAX_UPLOAD_BYTES
from .models import CallIntervalObservation
from .pipeline import PipelineAHTSummary, PipelineGroup
from .solver_v2 import MAX_GROUPS, MAX_IDENTIFIER_LENGTH


CALLS_SHA256 = "aa80c8f88f3c2b7ec66e5b2eb440be5733447cb49ad4021da7b0baffdf9f71fd"
QUEUES_SHA256 = "c1d69085348c1eccd61c7e7a8dde199a354042be626f2f2f7eb448b6ca4a39d5"
SOURCE_START = date(2025, 2, 3)
SOURCE_END = date(2025, 3, 1)
MAX_BUNDLE_ENTRIES = 2
MAX_BUNDLE_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_BUNDLE_ENTRY_BYTES = 10 * 1024 * 1024
MAX_BUNDLE_COMPRESSION_RATIO = 100
MAX_BUNDLE_ROWS = 10_000
MAX_BUNDLE_CELLS = 26 * 48 * MAX_GROUPS
CALLS_HEADER = (
    "id",
    "queue_id",
    "caller_id",
    "started_at",
    "status",
    "base_wait_seconds",
    "wait_seconds",
    "abandon_after_seconds",
    "agent_id",
    "base_handle_seconds",
    "queue_handle_seconds",
    "handle_seconds",
    "resolved_first_contact",
)
QUEUES_HEADER = (
    "id",
    "queue_set",
    "queue_name",
    "sla_seconds",
    "wait_factor",
    "handle_factor",
)


class DatabaseSampleImportError(ValueError):
    """A safe validation error for pinned Database Sample bundles."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DatabaseHistoryWindow(_StrictModel):
    start_date: date
    end_date: date


class DatabaseSourceWindow(_StrictModel):
    start_date: date
    end_date_exclusive: date


class DatabaseZeroCounts(_StrictModel):
    date_start_buckets: StrictInt = Field(ge=0)
    date_start_queue_cells: StrictInt = Field(ge=0)


class DatabaseSampleSource(_StrictModel):
    calls_sha256: StrictStr
    queues_sha256: StrictStr
    hashes_verified: StrictBool
    source_window: DatabaseSourceWindow
    derived_zero_counts: DatabaseZeroCounts


class DatabaseSampleImportResponse(_StrictModel):
    mode: Literal["history"]
    history_window: DatabaseHistoryWindow
    groups: list[PipelineGroup]
    rows: list[CallIntervalObservation] = Field(max_length=MAX_BUNDLE_CELLS)
    aht_by_group: list[PipelineAHTSummary] = Field(max_length=MAX_GROUPS)
    source: DatabaseSampleSource


def _parse_date(value: date | str, name: str) -> date:
    if isinstance(value, datetime):
        raise DatabaseSampleImportError(f"{name} must be a date.")
    if isinstance(value, date):
        return value
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise DatabaseSampleImportError(f"{name} must be an ISO date.") from None
    if parsed.isoformat() != value:
        raise DatabaseSampleImportError(f"{name} must be an ISO date.")
    return parsed


def _read_archive(data: bytes) -> tuple[bytes, bytes]:
    if not isinstance(data, bytes):
        raise DatabaseSampleImportError("Database Sample bundle must be ZIP bytes.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DatabaseSampleImportError("Database Sample bundle exceeds the configured size limit.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) != MAX_BUNDLE_ENTRIES:
                raise DatabaseSampleImportError(
                    "Bundle must contain exactly calls.csv and queues.csv."
                )
            by_name = {}
            total_size = 0
            for info in infos:
                if info.filename in by_name or info.filename not in {
                    "calls.csv",
                    "queues.csv",
                }:
                    raise DatabaseSampleImportError(
                        "Bundle must contain exactly calls.csv and queues.csv."
                    )
                if info.is_dir() or info.flag_bits & 0x1:
                    raise DatabaseSampleImportError(
                        "Bundle contains an unsupported archive member."
                    )
                if info.compress_type not in (
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                ):
                    raise DatabaseSampleImportError(
                        "Bundle uses an unsupported compression method."
                    )
                if info.file_size > MAX_BUNDLE_ENTRY_BYTES:
                    raise DatabaseSampleImportError(
                        "Bundle member exceeds the expanded-size limit."
                    )
                total_size += info.file_size
                if total_size > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                    raise DatabaseSampleImportError(
                        "Bundle exceeds the expanded-size limit."
                    )
                if info.file_size and (
                    not info.compress_size
                    or info.file_size
                    > info.compress_size * MAX_BUNDLE_COMPRESSION_RATIO
                ):
                    raise DatabaseSampleImportError(
                        "Bundle member exceeds the compression-ratio limit."
                    )
                by_name[info.filename] = info
            if set(by_name) != {"calls.csv", "queues.csv"}:
                raise DatabaseSampleImportError(
                    "Bundle must contain exactly calls.csv and queues.csv."
                )

            contents = []
            for name in ("calls.csv", "queues.csv"):
                info = by_name[name]
                content = bytearray()
                with archive.open(info) as member:
                    while chunk := member.read(64 * 1024):
                        content.extend(chunk)
                        if len(content) > info.file_size or len(content) > MAX_BUNDLE_ENTRY_BYTES:
                            raise DatabaseSampleImportError(
                                "Bundle member exceeds the expanded-size limit."
                            )
                if len(content) != info.file_size:
                    raise DatabaseSampleImportError(
                        "Bundle member has an invalid expanded size."
                    )
                contents.append(bytes(content))
            return contents[0], contents[1]
    except DatabaseSampleImportError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        raise DatabaseSampleImportError("Database Sample bundle is invalid or unsafe.") from None


def _csv_rows(content: bytes, expected_header: tuple[str, ...], label: str):
    try:
        text = content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if tuple(reader.fieldnames or ()) != expected_header:
            raise DatabaseSampleImportError(f"Pinned {label} CSV header is invalid.")
        rows = list(reader)
    except DatabaseSampleImportError:
        raise
    except (UnicodeDecodeError, csv.Error):
        raise DatabaseSampleImportError(f"Pinned {label} CSV is invalid.") from None
    if len(rows) > MAX_BUNDLE_ROWS:
        raise DatabaseSampleImportError(f"Pinned {label} CSV exceeds its row limit.")
    return rows


def _selected_groups(group_ids: list[str] | None, queues: list[dict[str, str]]):
    known = {item["id"] for item in queues}
    if group_ids is None:
        selected = set(known)
    else:
        if (
            isinstance(group_ids, (str, bytes))
            or not isinstance(group_ids, list)
            or not 1 <= len(group_ids) <= MAX_GROUPS
        ):
            raise DatabaseSampleImportError("group_id must select 1 to 10 queues.")
        selected = set()
        for group_id in group_ids:
            if not isinstance(group_id, str):
                raise DatabaseSampleImportError("group_id must be a queue ID.")
            if not group_id or len(group_id) > MAX_IDENTIFIER_LENGTH:
                raise DatabaseSampleImportError("group_id is invalid.")
            if group_id in selected:
                raise DatabaseSampleImportError("group_id values must be unique.")
            if group_id not in known:
                raise DatabaseSampleImportError("group_id is not a source queue.")
            selected.add(group_id)
    return [item for item in queues if item["id"] in selected]


def parse_database_sample_bundle(
    data: bytes,
    *,
    week_start: date | str,
    history_start_date: date | str,
    history_end_date: date | str,
    group_ids: list[str] | None = None,
) -> DatabaseSampleImportResponse:
    """Validate pinned source bytes before generating bounded aggregate rows."""
    week = _parse_date(week_start, "week_start")
    start = _parse_date(history_start_date, "history_start_date")
    end = _parse_date(history_end_date, "history_end_date")
    if (
        start < SOURCE_START
        or end > SOURCE_END
        or start >= end
        or end > week
    ):
        raise DatabaseSampleImportError(
            "Requested history window must be non-empty, inside the pinned source window, "
            "and end no later than week_start."
        )

    calls_bytes, queues_bytes = _read_archive(data)
    if (
        hashlib.sha256(calls_bytes).hexdigest() != CALLS_SHA256
        or hashlib.sha256(queues_bytes).hexdigest() != QUEUES_SHA256
    ):
        raise DatabaseSampleImportError(
            "Bundle member hashes do not match the supported Database Sample version."
        )

    queue_rows = _csv_rows(queues_bytes, QUEUES_HEADER, "queues")
    queues = []
    for row in queue_rows:
        group_id = row["id"]
        name = row["queue_name"]
        if (
            not group_id
            or len(group_id) > MAX_IDENTIFIER_LENGTH
            or not name
            or len(name) > MAX_IDENTIFIER_LENGTH
        ):
            raise DatabaseSampleImportError("Pinned queue metadata is invalid.")
        queues.append({"id": group_id, "name": name})
    selected = _selected_groups(group_ids, queues)
    selected_ids = {item["id"] for item in selected}

    call_rows = _csv_rows(calls_bytes, CALLS_HEADER, "calls")
    counts: dict[tuple[date, str, str], int] = defaultdict(int)
    handle_seconds: dict[str, list[int]] = {group_id: [] for group_id in selected_ids}
    for row in call_rows:
        try:
            timestamp = datetime.fromisoformat(row["started_at"])
            if timestamp.tzinfo is not None:
                raise ValueError
            group_id = row["queue_id"]
            if group_id not in {item["id"] for item in queues}:
                raise ValueError
            if row["status"] == "answered" and row["handle_seconds"]:
                duration = int(row["handle_seconds"])
                if duration > 0 and timestamp.date() < week and group_id in selected_ids:
                    handle_seconds[group_id].append(duration)
            if start <= timestamp.date() < end and timestamp.date() < week and group_id in selected_ids:
                bucket_minute = timestamp.minute // 30 * 30
                bucket = time(hour=timestamp.hour, minute=bucket_minute).strftime("%H:%M")
                counts[(timestamp.date(), bucket, group_id)] += 1
        except (TypeError, ValueError, OverflowError):
            raise DatabaseSampleImportError(
                "Pinned call event data is invalid."
            ) from None

    days = (end - start).days
    if days * 48 * len(selected) > MAX_BUNDLE_CELLS:
        raise DatabaseSampleImportError("Requested bundle output exceeds the row limit.")
    rows = []
    empty_cells = 0
    empty_buckets = 0
    for day_offset in range(days):
        day = start + timedelta(days=day_offset)
        for slot_index in range(48):
            minute = slot_index * 30
            slot = f"{minute // 60:02d}:{minute % 60:02d}"
            bucket_empty = True
            for group in selected:
                calls = counts.get((day, slot, group["id"]), 0)
                if calls:
                    bucket_empty = False
                else:
                    empty_cells += 1
                rows.append(
                    {
                        "date": day.isoformat(),
                        "start": slot,
                        "group_id": group["id"],
                        "calls": calls,
                    }
                )
            if bucket_empty:
                empty_buckets += 1

    aht_by_group = []
    for group in selected:
        samples = handle_seconds[group["id"]]
        if not samples:
            raise DatabaseSampleImportError(
                "Pinned source has no eligible answered handle-time samples for a selected queue."
            )
        aht_by_group.append(
            {
                "group_id": group["id"],
                "aht_seconds": float(sum(samples) / len(samples)),
                "sample_count": len(samples),
                "cutoff_before": week.isoformat(),
            }
        )
    return DatabaseSampleImportResponse(
        mode="history",
        history_window={"start_date": start, "end_date": end},
        groups=selected,
        rows=rows,
        aht_by_group=aht_by_group,
        source={
            "calls_sha256": CALLS_SHA256,
            "queues_sha256": QUEUES_SHA256,
            "hashes_verified": True,
            "source_window": {
                "start_date": SOURCE_START,
                "end_date_exclusive": SOURCE_END,
            },
            "derived_zero_counts": {
                "date_start_buckets": empty_buckets,
                "date_start_queue_cells": empty_cells,
            },
        },
    )
