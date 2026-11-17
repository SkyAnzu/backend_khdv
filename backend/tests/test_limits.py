import importlib

import pytest
from fastapi.testclient import TestClient

from backend.app import limits, main


DEFAULT_BYTES = 2 * 1024 * 1024


def test_body_limit_defaults_are_independent(monkeypatch):
    with monkeypatch.context() as env:
        env.delenv("KHDV_MAX_UPLOAD_BYTES", raising=False)
        env.delenv("KHDV_MAX_SCHEDULE_BODY_BYTES", raising=False)
        loaded = importlib.reload(limits)
        assert loaded.MAX_UPLOAD_BYTES == DEFAULT_BYTES
        assert loaded.MAX_SCHEDULE_BODY_BYTES == DEFAULT_BYTES


def test_positive_environment_overrides_change_runtime_limits(monkeypatch):
    with monkeypatch.context() as env:
        env.setenv("KHDV_MAX_UPLOAD_BYTES", "23")
        env.setenv("KHDV_MAX_SCHEDULE_BODY_BYTES", "17")
        loaded = importlib.reload(limits)
        runtime = importlib.reload(main)
        assert runtime.MAX_UPLOAD_BYTES == loaded.MAX_UPLOAD_BYTES == 23
        assert runtime.MAX_SCHEDULE_BODY_BYTES == loaded.MAX_SCHEDULE_BODY_BYTES == 17
    importlib.reload(limits)
    importlib.reload(main)


def test_environment_overrides_change_actual_upload_and_schedule_caps(monkeypatch):
    try:
        with monkeypatch.context() as env:
            env.setenv("KHDV_MAX_UPLOAD_BYTES", "23")
            env.setenv("KHDV_MAX_SCHEDULE_BODY_BYTES", "17")
            importlib.reload(limits)
            runtime = importlib.reload(main)
            client = TestClient(runtime.app)

            schedule_boundary = client.post(
                "/api/schedule-v2",
                content=b"{}" + b" " * 15,
                headers={"Content-Type": "application/json"},
            )
            schedule_over = client.post(
                "/api/schedule-v2",
                content=b"{}" + b" " * 16,
                headers={"Content-Type": "application/json"},
            )
            upload_boundary = client.post(
                "/api/calls/import?file_format=csv&mode=history&group_id=sales",
                content=b"x" * 23,
                headers={"Content-Type": "application/octet-stream"},
            )
            upload_over = client.post(
                "/api/calls/import?file_format=csv&mode=history&group_id=sales",
                content=b"x" * 24,
                headers={"Content-Type": "application/octet-stream"},
            )

            assert schedule_boundary.status_code == 422
            assert schedule_over.status_code == 413
            assert upload_boundary.status_code == 422
            assert upload_over.status_code == 413
    finally:
        importlib.reload(limits)
        importlib.reload(main)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KHDV_MAX_UPLOAD_BYTES", "0"),
        ("KHDV_MAX_SCHEDULE_BODY_BYTES", "-1"),
        ("KHDV_MAX_UPLOAD_BYTES", "not-an-integer"),
    ],
)
def test_invalid_environment_overrides_fail_fast_without_echoing_value(
    monkeypatch, name, value
):
    try:
        with monkeypatch.context() as env:
            env.setenv(name, value)
            with pytest.raises(RuntimeError) as error:
                importlib.reload(limits)
            assert name in str(error.value)
            assert value not in str(error.value)
    finally:
        importlib.reload(limits)
        importlib.reload(main)


def test_upload_override_does_not_change_archive_or_row_limits():
    assert limits.MAX_UPLOAD_BYTES == DEFAULT_BYTES
    assert importlib.import_module("backend.app.import_calls").MAX_IMPORT_ROWS == 10_000
    assert importlib.import_module(
        "backend.app.import_calls"
    ).MAX_XLSX_UNCOMPRESSED_BYTES == 20 * 1024 * 1024
