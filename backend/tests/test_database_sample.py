import csv
from datetime import date
import io
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.database_sample import (
    CALLS_SHA256,
    QUEUES_SHA256,
    DatabaseSampleImportError,
    parse_database_sample_bundle,
)


FIXTURES = Path(__file__).parent / "data" / "database_sample"
CALLS = (FIXTURES / "calls.csv").read_bytes()
QUEUES = (FIXTURES / "queues.csv").read_bytes()
SOURCE_START = "2025-02-03"
SOURCE_END = "2025-03-01"
client = TestClient(app)


def bundle(calls=CALLS, queues=QUEUES, extra=()):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("calls.csv", calls)
        archive.writestr("queues.csv", queues)
        for name, content in extra:
            archive.writestr(name, content)
    return output.getvalue()


def parse_bundle(*args, **kwargs):
    return parse_database_sample_bundle(*args, **kwargs).model_dump(mode="json")


def import_full(**kwargs):
    return parse_bundle(
        bundle(),
        week_start="2025-03-01",
        history_start_date=SOURCE_START,
        history_end_date=SOURCE_END,
        **kwargs,
    )


def post_bundle(
    data=None,
    *,
    mode="history",
    week_start="2025-03-01",
    history_start_date=SOURCE_START,
    history_end_date=SOURCE_END,
    group_ids=None,
):
    params = {
        "file_format": "database_sample_bundle",
        "mode": mode,
        "week_start": week_start,
        "history_start_date": history_start_date,
        "history_end_date": history_end_date,
    }
    if group_ids is not None:
        params["group_id"] = group_ids
    return client.post(
        "/api/calls/import",
        params=params,
        content=bundle() if data is None else data,
        headers={"Content-Type": "application/zip"},
    )


def test_full_pinned_import_has_expected_hashes_windows_and_separate_zero_counts():
    result = import_full()

    assert result["mode"] == "history"
    assert result["history_window"] == {
        "start_date": SOURCE_START,
        "end_date": SOURCE_END,
    }
    assert result["groups"] == [
        {"id": "1", "name": "Billing"},
        {"id": "2", "name": "Account changes"},
        {"id": "3", "name": "Sales"},
        {"id": "4", "name": "Technical support"},
        {"id": "5", "name": "Complaints"},
    ]
    assert len(result["rows"]) == 26 * 48 * 5 == 6_240
    assert set(result["rows"][0]) == {"date", "start", "group_id", "calls"}
    assert sum(row["calls"] > 0 for row in result["rows"]) == 699
    assert sum(row["calls"] == 0 for row in result["rows"]) == 5_541
    assert result["source"] == {
        "calls_sha256": CALLS_SHA256,
        "queues_sha256": QUEUES_SHA256,
        "hashes_verified": True,
        "source_window": {
            "start_date": SOURCE_START,
            "end_date_exclusive": SOURCE_END,
        },
        "derived_zero_counts": {
            "date_start_buckets": 804,
            "date_start_queue_cells": 5_541,
        },
    }
    assert len(result["aht_by_group"]) == 5
    assert all(
        item["cutoff_before"] == "2025-03-01"
        and item["sample_count"] > 0
        and item["aht_seconds"] > 0
        for item in result["aht_by_group"]
    )


def test_import_counts_empty_date_start_buckets_separately_from_empty_queue_cells():
    result = import_full()
    by_bucket = {}
    for row in result["rows"]:
        by_bucket.setdefault((row["date"], row["start"]), []).append(row["calls"])

    assert len(by_bucket) == 26 * 48 == 1_248
    assert sum(not any(calls) for calls in by_bucket.values()) == 804
    assert sum(not row["calls"] for row in result["rows"]) == 5_541

    forecast_keys = {
        (date.fromisoformat(row["date"]).weekday(), row["start"], row["group_id"])
        for row in result["rows"]
    }
    assert len(forecast_keys) == 7 * 48 * 5 == 1_680


def test_queue_filter_and_requested_window_keep_source_metadata_only():
    result = parse_bundle(
        bundle(),
        week_start="2025-03-01",
        history_start_date="2025-02-20",
        history_end_date="2025-02-25",
        group_ids=["3"],
    )

    assert result["groups"] == [{"id": "3", "name": "Sales"}]
    assert len(result["rows"]) == 5 * 48
    assert {row["group_id"] for row in result["rows"]} == {"3"}
    assert {row["date"] for row in result["rows"]} == {
        f"2025-02-{day:02d}" for day in range(20, 25)
    }
    assert len(result["aht_by_group"]) == 1
    assert result["aht_by_group"][0]["group_id"] == "3"
    assert result["source"]["derived_zero_counts"]["date_start_buckets"] <= 5 * 48


def test_history_window_filters_rows_but_aht_uses_all_prior_source_dates():
    calls_by_group = {}
    reader = csv.DictReader(io.StringIO(CALLS.decode("utf-8"), newline=""))
    for row in reader:
        if (
            row["queue_id"] == "3"
            and row["status"] == "answered"
            and row["handle_seconds"]
            and int(row["handle_seconds"]) > 0
        ):
            calls_by_group.setdefault("3", []).append(int(row["handle_seconds"]))
    expected_aht = sum(calls_by_group["3"]) / len(calls_by_group["3"])

    result = parse_bundle(
        bundle(),
        week_start="2025-03-01",
        history_start_date="2025-02-20",
        history_end_date="2025-02-25",
        group_ids=["3"],
    )

    aht = result["aht_by_group"][0]
    assert aht["sample_count"] == len(calls_by_group["3"])
    assert aht["aht_seconds"] == pytest.approx(expected_aht)
    assert aht["aht_seconds"] > 0


def test_raw_rows_on_or_after_week_start_are_excluded_not_file_errors():
    result = parse_bundle(
        bundle(),
        week_start="2025-02-20",
        history_start_date="2025-02-03",
        history_end_date="2025-02-20",
        group_ids=["3"],
    )

    assert all(row["date"] < "2025-02-20" for row in result["rows"])
    assert result["aht_by_group"][0]["cutoff_before"] == "2025-02-20"
    assert result["source"]["hashes_verified"] is True


@pytest.mark.parametrize(
    "group_ids",
    [
        ["unknown"],
        ["3", "3"],
        [],
        ["1"] * 11,
    ],
)
def test_group_filter_rejects_unknown_duplicate_or_invalid_selections(group_ids):
    with pytest.raises(DatabaseSampleImportError):
        import_full(group_ids=group_ids)


@pytest.mark.parametrize(
    ("week_start", "start", "end"),
    [
        ("2025-03-01", "2025-02-02", "2025-02-10"),
        ("2025-03-01", "2025-02-10", "2025-03-02"),
        ("2025-02-20", "2025-02-03", "2025-02-21"),
        ("2025-02-20", "2025-02-20", "2025-02-20"),
    ],
)
def test_bundle_rejects_invalid_or_unavailable_requested_windows(
    week_start, start, end
):
    with pytest.raises(DatabaseSampleImportError):
        parse_bundle(
            bundle(),
            week_start=week_start,
            history_start_date=start,
            history_end_date=end,
        )


@pytest.mark.parametrize(
    "data",
    [
        lambda: bundle(calls=CALLS + b"\n"),
        lambda: bundle(queues=QUEUES + b"\n"),
        lambda: bundle(extra=(("notes.txt", b"extra"),)),
        lambda: bundle(calls=CALLS, queues=b""),
        lambda: b"not a zip archive",
    ],
)
def test_import_requires_exact_pinned_members_without_sparse_fallback(data):
    with pytest.raises(DatabaseSampleImportError):
        parse_bundle(
            data(),
            week_start="2025-03-01",
            history_start_date=SOURCE_START,
            history_end_date=SOURCE_END,
        )


def test_import_api_returns_only_pinned_aggregate_envelope():
    response = post_bundle()

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mode"] == "history"
    assert len(result["rows"]) == 6_240
    assert result["source"]["hashes_verified"] is True
    assert result["source"]["derived_zero_counts"] == {
        "date_start_buckets": 804,
        "date_start_queue_cells": 5_541,
    }
    assert all(
        not {"caller_id", "agent_id", "handle_seconds", "sla_seconds"}
        & set(row)
        for row in result["rows"]
    )
    assert all(
        set(row) == {"date", "start", "group_id", "calls"}
        for row in result["rows"]
    )


def test_import_api_supports_optional_repeated_queue_filter():
    response = post_bundle(group_ids=["3", "1"])

    assert response.status_code == 200, response.text
    result = response.json()
    assert [group["id"] for group in result["groups"]] == ["1", "3"]
    assert {row["group_id"] for row in result["rows"]} == {"1", "3"}
    assert {row["group_id"] for row in result["aht_by_group"]} == {"1", "3"}


@pytest.mark.parametrize(
    "group_ids",
    [["unknown"], ["3", "3"]],
)
def test_import_api_safely_rejects_unknown_or_duplicate_queue_filters(group_ids):
    response = post_bundle(group_ids=group_ids)
    assert response.status_code == 422
    assert "group_id" in response.json()["detail"]
    assert "unknown" not in response.text


def test_import_api_rejects_incomplete_bundle_parameters_and_non_history_mode():
    response = client.post(
        "/api/calls/import",
        params={
            "file_format": "database_sample_bundle",
            "mode": "history",
        },
        content=bundle(),
        headers={"Content-Type": "application/zip"},
    )
    assert response.status_code == 422
    assert "week_start" in response.json()["detail"]

    response = post_bundle(mode="planned_calls")
    assert response.status_code == 422
    assert "history" in response.json()["detail"]


def test_import_api_rejects_altered_bundle_and_missing_archive_members():
    for data in (bundle(calls=CALLS + b"\n"), bundle(extra=(("extra.txt", b"x"),))):
        response = post_bundle(data)
        assert response.status_code == 422
        assert "hash" in response.json()["detail"] or "exactly" in response.json()["detail"]


def test_database_bundle_openapi_documents_zip_and_source_query_contract():
    operation = client.get("/openapi.json").json()["paths"]["/api/calls/import"]["post"]
    assert operation["requestBody"]["content"]["application/zip"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
    parameter_names = {item["name"] for item in operation["parameters"]}
    assert {
        "week_start",
        "history_start_date",
        "history_end_date",
        "group_id",
    } <= parameter_names
