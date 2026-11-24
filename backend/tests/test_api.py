from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import main, pipeline, solver_v2
from backend.app.main import app
from backend.app.solver_v2 import SolverV2Response


client = TestClient(app)
SAMPLES = Path(__file__).parents[1] / "samples"


def load_sample(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def post_schedule(request):
    return client.post("/api/schedule-v2", json=request)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_retired_solver_preview_is_not_routed_or_documented():
    assert client.post("/api/solver-preview", json={}).status_code == 404
    assert "/api/solver-preview" not in client.get("/openapi.json").json()["paths"]


def test_schedule_v2_planned_calls_are_half_hour_rows_and_rate_doubles():
    response = post_schedule(load_sample("pipeline-planned-calls-request.json"))

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mode"] == "planned_calls"
    assert result["forecast"] is None
    assert result["groups"] == [{"id": "sales", "name": "Sales"}]
    assert [
        (item["start"], item["calls"], item["sample_count"])
        for item in result["interval_results"]
    ] == [("08:00", 0.5, None), ("08:30", 0.5, None)]
    assert all(
        item["erlang"]["required_staff"] == 1
        and item["assigned_staff"] == 1
        for item in result["interval_results"]
    )
    assert "requirements" not in result
    assert all("source_status" not in item for item in result["interval_results"])
    assert result["schedule"]["status"] in {"OPTIMAL", "FEASIBLE"}
    assert result["schedule"]["total_shift_minutes"] == 60
    assert result["schedule"]["assignments"][0]["end"] == "09:00"
    assert result["schedule"]["assignments"][0]["breaks"] == []
    assert [
        item["start"]
        for item in result["schedule"]["assignments"][0]["groups_by_slot"]
    ] == ["08:00", "08:30"]


def test_schedule_v2_history_uses_window_and_preserves_interval_forecast():
    response = post_schedule(load_sample("pipeline-history-request.json"))

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mode"] == "history"
    assert result["forecast"]["forecasts"] == [
        {
            "date": "2026-11-02",
            "start": "08:00",
            "group_id": "sales",
            "forecast_calls": 3.0,
            "sample_count": 2,
        },
        {
            "date": "2026-11-02",
            "start": "08:30",
            "group_id": "sales",
            "forecast_calls": 0.0,
            "sample_count": 2,
        },
    ]
    assert result["interval_results"][0]["calls"] == 3.0
    assert result["interval_results"][0]["sample_count"] == 2
    assert result["schedule"]["status"] in {"OPTIMAL", "FEASIBLE"}


def test_schedule_v2_zero_calls_need_no_aht_and_return_null_sla():
    request = load_sample("pipeline-planned-calls-request.json")
    request["planned_calls"] = [{**item, "calls": 0.0} for item in request["planned_calls"]]
    request["aht_by_group"] = []
    request["employees"] = []
    request["shifts"] = []

    response = post_schedule(request)

    assert response.status_code == 200, response.text
    result = response.json()
    assert [item["erlang"] for item in result["interval_results"]] == [
        {"required_staff": 0, "service_level": None},
        {"required_staff": 0, "service_level": None},
    ]
    assert result["schedule"]["status"] == "OPTIMAL"
    assert result["schedule"]["assignments"] == []


def test_schedule_v2_positive_calls_without_aht_are_a_domain_422():
    request = load_sample("pipeline-planned-calls-request.json")
    request["aht_by_group"] = []

    response = post_schedule(request)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "AHT seconds are required for groups with positive calls."
    }


def test_schedule_v2_preserves_infeasible_solver_status():
    request = load_sample("pipeline-planned-calls-request.json")
    request["employees"] = []

    response = post_schedule(request)

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["schedule"]["status"] == "INFEASIBLE"
    assert result["schedule"]["assignments"] == []
    assert result["schedule"]["stage_two_status"] is None
    assert all(item["assigned_staff"] is None for item in result["interval_results"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: request["planned_calls"].pop(),
        lambda request: request["planned_calls"].append(
            deepcopy(request["planned_calls"][0])
        ),
        lambda request: request["planned_calls"][0].update(start="08:15"),
        lambda request: request["employees"][0]["available_slots"][0].update(
            start="08:15"
        ),
        lambda request: request.update(operating_intervals=[]),
        lambda request: request["service_targets"].pop(),
    ],
)
def test_schedule_v2_rejects_malformed_inputs_as_sanitized_422(mutate):
    request = load_sample("pipeline-planned-calls-request.json")
    mutate(request)

    response = post_schedule(request)

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors
    assert all(error["msg"] == "Invalid value for this field." for error in errors)
    assert all("input" not in error and "ctx" not in error for error in errors)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: request["employees"].append(deepcopy(request["employees"][0])),
        lambda request: request["employees"][0].update(skills=["PRIVATE_UNKNOWN"]),
        lambda request: request["employees"][0]["available_slots"].append(
            {"date": "2026-11-02", "start": "07:30"}
        ),
        lambda request: request["employees"][0].update(
            unavailable_dates=["2026-11-02"]
        ),
        lambda request: request["shifts"][0].update(start="07:30"),
        lambda request: request["shifts"].append(deepcopy(request["shifts"][0])),
    ],
)
def test_schedule_v2_rejects_invalid_solver_configuration_safely(mutate):
    request = load_sample("pipeline-planned-calls-request.json")
    mutate(request)

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/schedule-v2", json=request
    )

    assert response.status_code == 422
    assert all(
        item["msg"] == "Invalid value for this field."
        for item in response.json()["detail"]
    )
    assert "PRIVATE_UNKNOWN" not in response.text
    assert "Traceback" not in response.text


def test_schedule_v2_rejects_history_outside_window_and_duplicate_rows():
    request = load_sample("pipeline-history-request.json")
    request["history"][0]["date"] = "2026-09-28"
    response = post_schedule(request)
    assert response.status_code == 422
    assert all(
        item["msg"] == "Invalid value for this field."
        for item in response.json()["detail"]
    )

    request = load_sample("pipeline-history-request.json")
    request["history"].append(deepcopy(request["history"][0]))
    response = post_schedule(request)
    assert response.status_code == 422


def test_schedule_v2_rejects_undeclared_history_groups_before_pipeline(monkeypatch):
    request = load_sample("pipeline-history-request.json")
    request["history"] = [{**row, "calls": 0} for row in request["history"]]
    sales_history = list(request["history"])
    request["history"].extend(
        {**row, "group_id": "queue999", "calls": 999}
        for row in sales_history
        if row["start"] == "08:00"
    )
    pipeline_called = False

    def unexpected_pipeline_call(_request):
        nonlocal pipeline_called
        pipeline_called = True
        raise RuntimeError("invalid history reached pipeline")

    monkeypatch.setattr(main, "run_pipeline", unexpected_pipeline_call)

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/schedule-v2", json=request
    )

    assert response.status_code == 422
    assert not pipeline_called
    assert all(
        error["msg"] == "Invalid value for this field."
        for error in response.json()["detail"]
    )
    assert "queue999" not in response.text


def test_schedule_v2_requires_two_distinct_matching_history_dates():
    request = load_sample("pipeline-history-request.json")
    request["history"] = [
        row for row in request["history"] if row["date"] == "2026-10-19"
    ]

    response = post_schedule(request)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "History has insufficient prior samples for one or more targets."
    }


def test_schedule_v2_rejects_legacy_hour_based_payload():
    response = post_schedule(
        {
            "mode": "planned_calls",
            "week_start": "2026-11-02",
            "groups": ["sales"],
            "operating_hours": [{"date": "2026-11-02", "hour": 8}],
        }
    )

    assert response.status_code == 422
    assert all(
        item["msg"] == "Invalid value for this field."
        for item in response.json()["detail"]
    )


def test_schedule_v2_rejects_more_than_employee_limit_before_solving():
    request = load_sample("pipeline-planned-calls-request.json")
    employee = request["employees"][0]
    request["employees"] += [
        {**employee, "id": f"e{index:03}"}
        for index in range(2, solver_v2.MAX_EMPLOYEES + 2)
    ]

    response = post_schedule(request)

    assert response.status_code == 422
    assert any(
        error["loc"] == ["body", "employees"]
        for error in response.json()["detail"]
    )


@pytest.mark.parametrize(
    ("status", "stage_two_status"),
    [
        ("FEASIBLE", "UNKNOWN"),
        ("UNKNOWN", None),
        ("MODEL_INVALID", None),
    ],
)
def test_schedule_v2_preserves_solver_status_phase_and_warnings(
    monkeypatch, status, stage_two_status
):
    solver_result = SolverV2Response(
        status=status,
        assignments=[],
        skill_mismatch_slots=None,
        total_shift_minutes=None,
        overstaff_slots=None,
        stage_two_status=stage_two_status,
        warnings=["synthetic solver warning"],
        message="synthetic solver status",
    )
    monkeypatch.setattr(pipeline, "solve_schedule_v2", lambda request: solver_result)

    response = post_schedule(load_sample("pipeline-planned-calls-request.json"))

    assert response.status_code == 200, response.text
    schedule = response.json()["schedule"]
    assert schedule["status"] == status
    assert schedule["stage_two_status"] == stage_two_status
    assert schedule["warnings"] == ["synthetic solver warning"]
    assert schedule["message"] == "synthetic solver status"


def test_schedule_v2_unexpected_internal_error_remains_500(monkeypatch):
    marker = "PRIVATE_INTERNAL_MARKER"

    def fail_pipeline(request):
        raise RuntimeError(marker)

    monkeypatch.setattr(main, "run_pipeline", fail_pipeline)
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/schedule-v2",
        json=load_sample("pipeline-planned-calls-request.json"),
    )

    assert response.status_code == 500
    assert marker not in response.text


def test_schedule_v2_maps_known_erlang_failure_to_domain_422():
    request = load_sample("pipeline-planned-calls-request.json")
    request["planned_calls"][0]["calls"] = 12.0
    request["aht_by_group"][0]["aht_seconds"] = 3600.0
    request["max_agents"] = 1

    response = post_schedule(request)

    assert response.status_code == 422
    assert "max_agents=1" in response.json()["detail"]
    assert "Traceback" not in response.text


def test_schedule_v2_catches_pre_model_assignment_budget_as_domain_422(
    monkeypatch,
):
    monkeypatch.setattr(solver_v2, "MAX_ASSIGNMENT_VARIABLES", 1)
    request = load_sample("pipeline-planned-calls-request.json")

    response = post_schedule(request)

    assert response.status_code == 422
    assert response.json() == {
        "detail": "The schedule exceeds the prototype assignment-variable limit."
    }


def test_schedule_v2_rejects_oversized_actual_body(monkeypatch):
    monkeypatch.setattr(main, "MAX_SCHEDULE_BODY_BYTES", 2, raising=False)
    response = client.post(
        "/api/schedule-v2",
        content=b"{} ",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert "size limit" in response.json()["detail"].lower()


def test_schedule_v2_counts_streamed_bytes(monkeypatch):
    monkeypatch.setattr(main, "MAX_SCHEDULE_BODY_BYTES", 4, raising=False)
    response = client.post(
        "/api/schedule-v2",
        content=iter((b'{"x', b'": 1}')),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_schedule_v2_rejects_lying_content_length(monkeypatch):
    monkeypatch.setattr(main, "MAX_SCHEDULE_BODY_BYTES", 4, raising=False)
    response = client.post(
        "/api/schedule-v2",
        content=b"{}   ",
        headers={
            "Content-Type": "application/json",
            "Content-Length": "1",
        },
    )
    assert response.status_code == 413


def test_schedule_v2_accepts_exact_body_limit(monkeypatch):
    monkeypatch.setattr(main, "MAX_SCHEDULE_BODY_BYTES", 2, raising=False)
    response = client.post(
        "/api/schedule-v2",
        content=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_schedule_v2_openapi_has_interval_schema_and_no_hour_compatibility():
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/api/schedule-v2"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["type"] == "object"
    assert {
        "groups",
        "operating_intervals",
        "service_targets",
        "aht_by_group",
        "shifts",
    } <= set(request_schema["properties"])
    assert "operating_hours" not in request_schema["properties"]
