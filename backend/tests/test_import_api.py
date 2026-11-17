import csv
from datetime import date
import io
import threading
import zipfile

from fastapi.testclient import TestClient
from openpyxl import Workbook
import pytest

from backend.app import import_calls, main
from backend.app.import_calls import CallImportError
from backend.app.main import app
from backend.app.models import CallIntervalObservation


client = TestClient(app)
HEADER = ("date", "start", "group_id", "calls")
XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


def csv_bytes(*rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerows(rows)
    return stream.getvalue().encode()


def query(mode="planned_calls", file_format="csv", groups=("sales",)):
    return [
        ("file_format", file_format),
        ("mode", mode),
        *(("group_id", group) for group in groups),
    ]


def post_import(
    data, *, mode="planned_calls", file_format="csv", groups=("sales",), headers=None
):
    return client.post(
        "/api/calls/import",
        params=query(mode, file_format, groups),
        content=data,
        headers=headers or {"Content-Type": "application/octet-stream"},
    )


def xlsx_bytes(rows, *, formula=None):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "calls"
    for row in rows:
        sheet.append(row)
    if formula is not None:
        sheet[formula[0]] = formula[1]
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def add_zip_entries(data, entries, *, compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(
        output, "w", compression=compression
    ) as target:
        for item in source.infolist():
            target.writestr(item.filename, source.read(item))
        for name, content in entries:
            target.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize(
    ("mode", "row", "expected_row"),
    [
        (
            "history",
            ("2026-10-05", "08:00", "sales", 0),
            {"date": "2026-10-05", "start": "08:00", "group_id": "sales", "calls": 0},
        ),
        (
            "planned_calls",
            ("2026-11-02", "08:00", "sales", 0),
            {
                "date": "2026-11-02",
                "start": "08:00",
                "group_id": "sales",
                "calls": 0.0,
            },
        ),
    ],
)
def test_import_csv_returns_mode_specific_rows_and_preserves_zero(
    mode, row, expected_row
):
    response = post_import(
        csv_bytes(row), mode=mode, headers={"Content-Type": "text/csv"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"mode": mode, "rows": [expected_row]}


def test_import_xlsx_returns_planned_rows_with_iso_dates():
    response = post_import(
        xlsx_bytes([HEADER, [date(2026, 11, 2), "08:00", "sales", 2.5]]),
        file_format="xlsx",
        headers={"Content-Type": XLSX_CONTENT_TYPE},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "mode": "planned_calls",
        "rows": [
            {
                "date": "2026-11-02",
                "start": "08:00",
                "group_id": "sales",
                "calls": 2.5,
            }
        ],
    }


@pytest.mark.parametrize(
    ("content_type", "file_format"),
    [
        ("multipart/form-data; boundary=sample", "csv"),
        ("application/zip", "xlsx"),
        (XLSX_CONTENT_TYPE, "csv"),
        ("text/csv", "xlsx"),
    ],
)
def test_import_rejects_unsupported_or_mismatched_media_types(
    content_type, file_format
):
    response = post_import(
        b"data",
        file_format=file_format,
        headers={"Content-Type": content_type},
    )

    assert response.status_code == 415
    assert "multipart" not in response.text


def test_import_rejects_unsupported_content_encoding():
    response = post_import(
        csv_bytes(("2026-11-02", "08:00", "sales", 1)),
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Encoding": "gzip",
        },
    )

    assert response.status_code == 415


def test_import_rejects_more_than_ten_repeated_groups_without_echoing_values():
    marker = "PRIVATE_GROUP_MARKER"
    response = post_import(
        csv_bytes(("2026-11-02", "08:00", "sales", 1)),
        groups=(marker,) * 11,
    )

    assert response.status_code == 422
    assert marker not in response.text
    assert all(
        item["msg"] == "Invalid value for this field."
        for item in response.json()["detail"]
    )


def test_import_rejects_duplicate_groups_safely():
    response = post_import(
        csv_bytes(("2026-11-02", "08:00", "sales", 1)),
        groups=("sales", "sales"),
    )

    assert response.status_code == 422
    assert "unique" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    "data",
    [
        b"PRIVATE_HEADER_MARKER,start,group_id,calls\n2026-11-02,08:00,sales,1\n",
        b"date,start,group_id,calls\n2026-11-02,8,sales,\n",
        b"date,start,group_id,calls\n2026-11-02,8,PRIVATE_GROUP_MARKER,1\n",
        b"\xffPRIVATE_INVALID_CSV",
    ],
)
def test_import_returns_safe_422_for_bad_csv(data):
    response = post_import(data)

    assert response.status_code == 422
    assert "PRIVATE_" not in response.text
    assert "Traceback" not in response.text


def test_import_rejects_formula_workbook_without_echoing_cell_contents():
    marker = "=PRIVATE_FORMULA_MARKER"
    response = post_import(
        xlsx_bytes(
            [HEADER, ["2026-11-02", "08:00", "sales", 1]],
            formula=("D2", marker),
        ),
        file_format="xlsx",
        headers={"Content-Type": XLSX_CONTENT_TYPE},
    )

    assert response.status_code == 422
    assert "formula" in response.json()["detail"].lower()
    assert marker not in response.text


def test_import_rejects_xlsx_xml_limit_before_openpyxl(monkeypatch):
    xml_part = b"<root>" + b"<item/>" * 4_000 + b"</root>"
    data = add_zip_entries(
        xlsx_bytes([HEADER, ["2026-11-02", "08:00", "sales", 1]]),
        [
            (f"docProps/extra{index}.xml", xml_part)
            for index in range(38)
        ],
        compression=zipfile.ZIP_STORED,
    )

    def unexpected_openpyxl_call(*args, **kwargs):
        pytest.fail("XLSX XML limit was not enforced before openpyxl.")

    monkeypatch.setattr(import_calls, "load_workbook", unexpected_openpyxl_call)
    response = post_import(
        data, file_format="xlsx", headers={"Content-Type": XLSX_CONTENT_TYPE}
    )

    assert response.status_code == 422
    assert "element count" in response.json()["detail"].lower()


def test_import_uses_actual_stream_size_not_untrusted_small_content_length(monkeypatch):
    parser_calls = 0

    def unexpected_parser_call(*args, **kwargs):
        nonlocal parser_calls
        parser_calls += 1
        return []

    monkeypatch.setattr(main, "parse_calls_import", unexpected_parser_call)
    response = post_import(
        b"x" * (main.MAX_UPLOAD_BYTES + 1),
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": "1",
        },
    )

    assert response.status_code == 413
    assert parser_calls == 0


def test_import_uses_valid_oversized_content_length_as_an_early_reject(monkeypatch):
    parser_calls = 0

    def unexpected_parser_call(*args, **kwargs):
        nonlocal parser_calls
        parser_calls += 1
        return []

    monkeypatch.setattr(main, "parse_calls_import", unexpected_parser_call)
    response = post_import(
        csv_bytes(("2026-11-02", "08:00", "sales", 1)),
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(main.MAX_UPLOAD_BYTES + 1),
        },
    )

    assert response.status_code == 413
    assert parser_calls == 0


def test_import_ignores_malformed_content_length_and_still_parses_stream():
    response = post_import(
        csv_bytes(("2026-11-02", "08:00", "sales", 1)),
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": "not-a-number",
        },
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "params",
    [
        "file_format=private_format&mode=history&group_id=sales",
        "file_format=csv&mode=private_mode&group_id=sales",
        "file_format=csv&mode=history",
    ],
)
def test_import_sanitizes_missing_or_invalid_query_values(params):
    response = client.post(
        f"/api/calls/import?{params}",
        content=b"PRIVATE_BODY_MARKER",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 422
    assert "private_" not in response.text.lower()
    assert "PRIVATE_BODY_MARKER" not in response.text
    if params.endswith("mode=history"):
        assert response.json() == {
            "detail": "group_id is required for CSV/XLSX."
        }
    else:
        assert all(
            item["msg"] == "Invalid value for this field."
            for item in response.json()["detail"]
        )


def test_import_maps_only_call_import_errors_to_422(monkeypatch):
    marker = "safe parser validation"

    def reject_import(*args, **kwargs):
        raise CallImportError(marker)

    monkeypatch.setattr(main, "parse_calls_import", reject_import)

    response = post_import(csv_bytes(("2026-11-02", "08:00", "sales", 1)))

    assert response.status_code == 422
    assert response.json() == {"detail": marker}


def test_import_does_not_disguise_unexpected_parser_errors_as_422(monkeypatch):
    marker = "PRIVATE_INTERNAL_FAILURE"

    def fail_import(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(main, "parse_calls_import", fail_import)

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/calls/import",
        params=query(),
        content=csv_bytes(("2026-11-02", "08:00", "sales", 1)),
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 500
    assert marker not in response.text
    assert main._CALL_IMPORT_SEMAPHORE.acquire(blocking=False)
    main._CALL_IMPORT_SEMAPHORE.release()


@pytest.mark.parametrize("raises", [False, True])
def test_import_parse_semaphore_is_released_after_success_or_parser_error(
    monkeypatch, raises
):
    def parser(*args, **kwargs):
        if raises:
            raise CallImportError("invalid file")
        return [
            CallIntervalObservation(
                date=date(2026, 10, 5), start="08:00", group_id="sales", calls=0
            )
        ]

    monkeypatch.setattr(main, "parse_calls_import", parser)
    response = post_import(
        csv_bytes(("2026-10-05", "08:00", "sales", 0)),
        mode="history",
    )

    assert response.status_code == (422 if raises else 200)
    assert main._CALL_IMPORT_SEMAPHORE.acquire(blocking=False)
    main._CALL_IMPORT_SEMAPHORE.release()


def test_import_returns_429_when_process_parse_slot_is_busy(monkeypatch):
    parser_calls = 0

    def unexpected_parser_call(*args, **kwargs):
        nonlocal parser_calls
        parser_calls += 1
        return []

    monkeypatch.setattr(main, "parse_calls_import", unexpected_parser_call)
    assert main._CALL_IMPORT_SEMAPHORE.acquire(blocking=False)
    try:
        response = post_import(csv_bytes(("2026-11-02", "08:00", "sales", 1)))
    finally:
        main._CALL_IMPORT_SEMAPHORE.release()

    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert parser_calls == 0


def test_import_runs_parser_off_the_async_request_thread(monkeypatch):
    event_loop_thread = []
    parser_thread = []
    original_run_in_threadpool = main.run_in_threadpool
    original_parser = main.parse_calls_import

    async def record_threadpool_thread(func, *args, **kwargs):
        event_loop_thread.append(threading.get_ident())
        return await original_run_in_threadpool(func, *args, **kwargs)

    def record_parser_thread(*args, **kwargs):
        parser_thread.append(threading.get_ident())
        return original_parser(*args, **kwargs)

    monkeypatch.setattr(main, "run_in_threadpool", record_threadpool_thread)
    monkeypatch.setattr(main, "parse_calls_import", record_parser_thread)
    response = post_import(csv_bytes(("2026-11-02", "08:00", "sales", 1)))

    assert response.status_code == 200
    assert event_loop_thread and parser_thread
    assert parser_thread[0] != event_loop_thread[0]


def test_import_openapi_documents_a_binary_body_and_repeated_group_query():
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/calls/import"]["post"]

    media = operation["requestBody"]["content"]
    assert set(media) == {
        "application/octet-stream",
        "text/csv",
        XLSX_CONTENT_TYPE,
        "application/zip",
    }
    assert all(
        item["schema"] == {"type": "string", "format": "binary"}
        for item in media.values()
    )
    group = next(item for item in operation["parameters"] if item["name"] == "group_id")
    assert group["in"] == "query"
    assert any(
        item.get("maxItems") == 10
        for item in group["schema"]["anyOf"]
    )
