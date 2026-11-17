import csv
import io
import json
from pathlib import Path
import threading

from fastapi.testclient import TestClient
import pytest

from backend.app import main
from backend.app.export import HEADERS, pipeline_to_csv
from backend.app.main import app
from backend.app.pipeline import PipelineResponse


client = TestClient(app)
SAMPLES = Path(__file__).parents[1] / "samples"
CONTENT_DISPOSITION = 'attachment; filename="lich-lam-viec.csv"'


def sample_response_bytes():
    return (SAMPLES / "pipeline-planned-calls-response.json").read_bytes()


def post_export(content=None, *, headers=None):
    return client.post(
        "/api/schedule-v2/export",
        content=sample_response_bytes() if content is None else content,
        headers=(
            {"Content-Type": "application/json"} if headers is None else headers
        ),
    )


def test_export_api_renders_the_supplied_sample_without_rerunning_pipeline(
    monkeypatch,
):
    def unexpected_pipeline_call(*args, **kwargs):
        pytest.fail("Export must not run the pipeline or solver.")

    monkeypatch.setattr(main, "run_pipeline", unexpected_pipeline_call)
    result = PipelineResponse.model_validate_json(sample_response_bytes())

    response = post_export()

    assert response.status_code == 200, response.text
    assert response.content == pipeline_to_csv(result)
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == CONTENT_DISPOSITION
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == list(HEADERS)
    assert rows[1][0] == "e01"
    assert rows[1][1] == "2026-11-02"
    assert rows[1][4] == "sales"


def test_export_api_accepts_a_feasible_empty_schedule():
    payload = json.loads(sample_response_bytes())
    payload["schedule"]["status"] = "FEASIBLE"
    payload["schedule"]["assignments"] = []

    response = post_export(json.dumps(payload).encode())

    assert response.status_code == 200, response.text
    assert response.content == pipeline_to_csv(
        PipelineResponse.model_validate_json(json.dumps(payload).encode())
    )
    assert list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")))) == [
        list(HEADERS)
    ]


@pytest.mark.parametrize("status", ["INFEASIBLE", "UNKNOWN", "MODEL_INVALID"])
def test_export_api_rejects_status_without_a_feasible_schedule(status):
    payload = json.loads(sample_response_bytes())
    payload["schedule"]["status"] = status

    response = post_export(json.dumps(payload).encode())

    assert response.status_code == 422
    assert "OPTIMAL or FEASIBLE" in response.json()["detail"]


def test_export_api_sanitizes_invalid_response_without_echoing_unknown_keys():
    marker = "PRIVATE-EXPORT-MARKER"
    payload = json.loads(sample_response_bytes())
    payload["schedule"][marker] = marker

    response = post_export(json.dumps(payload).encode())

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid pipeline response."}
    assert marker not in response.text
    assert "loc" not in response.text
    assert "input" not in response.text


def test_export_api_sanitizes_malformed_schedule_time():
    marker = "PRIVATE-TIME-MARKER"
    payload = json.loads(sample_response_bytes())
    payload["schedule"]["assignments"][0]["groups_by_slot"][0]["start"] = marker

    response = post_export(json.dumps(payload).encode())

    assert response.status_code == 422
    assert marker not in response.text
    assert response.json() == {"detail": "Invalid pipeline response."}


def test_export_api_rejects_invalid_json_without_echoing_body():
    marker = b"PRIVATE-INVALID-JSON-MARKER"

    response = post_export(marker)

    assert response.status_code == 422
    assert marker.decode() not in response.text
    assert response.json() == {"detail": "Invalid pipeline response."}


@pytest.mark.parametrize(
    "content_type",
    ["", "text/csv", "multipart/form-data; boundary=private"],
)
def test_export_api_requires_application_json(content_type):
    headers = {} if not content_type else {"Content-Type": content_type}

    response = post_export(headers=headers)

    assert response.status_code == 415
    assert "private" not in response.text


def test_export_api_rejects_non_identity_content_encoding():
    response = post_export(
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }
    )

    assert response.status_code == 415


def test_export_api_counts_actual_body_bytes_even_with_small_content_length(
    monkeypatch,
):
    def unexpected_pipeline_call(*args, **kwargs):
        pytest.fail("Oversized body must be rejected before validation/export.")

    monkeypatch.setattr(main, "run_pipeline", unexpected_pipeline_call)
    response = post_export(
        b"x" * (main.MAX_EXPORT_BODY_BYTES + 1),
        headers={
            "Content-Type": "application/json",
            "Content-Length": "1",
        },
    )

    assert response.status_code == 413


def test_export_api_rejects_declared_oversized_body_early():
    response = post_export(
        b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(main.MAX_EXPORT_BODY_BYTES + 1),
        },
    )

    assert response.status_code == 413


def test_export_api_passes_nfkc_formula_injection_through_existing_csv_guard():
    payload = json.loads(sample_response_bytes())
    payload["schedule"]["assignments"][0]["employee_id"] = "＝1+1"

    response = post_export(json.dumps(payload).encode())

    assert response.status_code == 200, response.text
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[1][0] == "'＝1+1"


def test_export_api_rejects_unpaired_surrogate_safely():
    payload = json.loads(sample_response_bytes())
    payload["schedule"]["assignments"][0]["employee_id"] = chr(0xD800)

    response = post_export(json.dumps(payload).encode())

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid pipeline response."}
    assert chr(0xD800) not in response.text


def test_export_api_does_not_turn_unexpected_exporter_error_into_422(monkeypatch):
    marker = "PRIVATE-EXPORTER-FAILURE"

    def fail_export(*args, **kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(main, "pipeline_to_csv", fail_export, raising=False)
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/schedule-v2/export",
        content=sample_response_bytes(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 500
    assert marker not in response.text


def test_export_api_runs_validation_and_renderer_in_threadpool(monkeypatch):
    event_loop_thread = []
    worker_threads = []
    original_run_in_threadpool = main.run_in_threadpool

    async def record_threadpool(func, *args, **kwargs):
        event_loop_thread.append(threading.get_ident())

        def record_call(*call_args, **call_kwargs):
            worker_threads.append(threading.get_ident())
            return func(*call_args, **call_kwargs)

        return await original_run_in_threadpool(record_call, *args, **kwargs)

    monkeypatch.setattr(main, "run_in_threadpool", record_threadpool)
    response = post_export()

    assert response.status_code == 200
    assert event_loop_thread and len(worker_threads) == 2
    assert all(worker != event_loop_thread[0] for worker in worker_threads)


def test_export_api_openapi_documents_json_input_and_csv_download():
    operation = client.get("/openapi.json").json()["paths"][
        "/api/schedule-v2/export"
    ]["post"]

    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PipelineResponse"
    }
    assert operation["responses"]["200"]["content"]["text/csv"]["schema"] == {
        "type": "string",
        "format": "binary",
    }
