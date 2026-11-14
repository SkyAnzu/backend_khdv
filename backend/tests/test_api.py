from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import pipeline
from backend.app.main import app
from backend.app.solver_v2 import SolverV2Response


client = TestClient(app)
SAMPLES = Path(__file__).parents[1] / "samples"


def load_sample(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_solver_preview_accepts_json_and_returns_result():
    response = client.post(
        "/api/solver-preview",
        json={
            "employees": [{"id": "e01", "name": "An"}],
            "shift_templates": [
                {"id": "s1", "name": "Ca sáng", "start": "08:00", "end": "12:00"}
            ],
            "requirements": [
                {"date": "2026-11-02", "start": "08:00", "end": "12:00", "required_staff": 1}
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] in {"FEASIBLE", "OPTIMAL"}
    assert response.json()["assignments"][0]["employee_id"] == "e01"


def test_schedule_v2_runs_planned_calls_sample_and_returns_full_pipeline_result():
    response = client.post(
        "/api/schedule-v2",
        json=load_sample("pipeline-planned-calls-request.json"),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mode"] == "planned_calls"
    assert result["forecast"] is None
    assert result["hourly_results"][0] == {
        "date": "2026-11-02",
        "hour": 8,
        "group_id": "sales",
        "calls_per_hour": 0.5,
        "sample_count": None,
        "erlang": {
            "required_staff": 1,
            "service_level": pytest.approx(0.9609, abs=0.0001),
        },
    }
    assert [
        (item["start"], item["required_staff"]) for item in result["requirements"]
    ] == [("08:00", 1), ("08:30", 1), ("09:00", 0), ("09:30", 0)]
    assert result["schedule"]["status"] in {"OPTIMAL", "FEASIBLE"}
    assert result["schedule"]["stage_two_status"] in {"OPTIMAL", "FEASIBLE"}
    assert result["schedule"]["warnings"] == [
        "Có 1 person-slots vượt nhu cầu theo nhóm/slot."
    ]
    assert result["schedule"]["assignments"][0]["employee_id"] == "e01"
    assert len(result["schedule"]["assignments"][0]["groups_by_slot"]) == 3


def test_schedule_v2_runs_history_sample_and_preserves_forecast():
    response = client.post(
        "/api/schedule-v2",
        json=load_sample("pipeline-history-request.json"),
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["mode"] == "history"
    assert result["forecast"]["forecasts"][0] == {
        "date": "2026-11-02",
        "hour": 8,
        "group_id": "sales",
        "forecast_calls": 2.5,
        "sample_count": 2,
    }
    assert result["hourly_results"][0]["calls_per_hour"] == 2.5
    assert result["hourly_results"][0]["sample_count"] == 2
    assert result["schedule"]["status"] in {"OPTIMAL", "FEASIBLE"}


def test_schedule_v2_zero_demand_returns_null_service_level_and_empty_schedule():
    request = load_sample("pipeline-planned-calls-request.json")
    request["planned_calls"] = [
        {**item, "calls_per_hour": 0.0} for item in request["planned_calls"]
    ]
    request["employees"] = []
    request["shift_starts"] = []

    response = client.post("/api/schedule-v2", json=request)

    assert response.status_code == 200, response.text
    result = response.json()
    assert [item["erlang"] for item in result["hourly_results"]] == [
        {"required_staff": 0, "service_level": None},
        {"required_staff": 0, "service_level": None},
    ]
    assert result["schedule"]["status"] == "OPTIMAL"
    assert result["schedule"]["assignments"] == []


def test_schedule_v2_preserves_infeasible_status_instead_of_returning_http_error():
    request = load_sample("pipeline-planned-calls-request.json")
    request["employees"] = []

    response = client.post("/api/schedule-v2", json=request)

    assert response.status_code == 200, response.text
    assert response.json()["schedule"]["status"] == "INFEASIBLE"
    assert response.json()["schedule"]["assignments"] == []
    assert response.json()["schedule"]["stage_two_status"] is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: request["planned_calls"].pop(),
        lambda request: request["planned_calls"].append(
            deepcopy(request["planned_calls"][0])
        ),
        lambda request: request["planned_calls"].append(
            {
                "date": "2026-11-02",
                "hour": 10,
                "group_id": "sales",
                "calls_per_hour": 0.0,
            }
        ),
        lambda request: request["employees"][0]["available_slots"][0].update(
            start="8:00"
        ),
    ],
)
def test_schedule_v2_rejects_malformed_payloads_with_safe_422(mutate):
    request = load_sample("pipeline-planned-calls-request.json")
    mutate(request)

    response = client.post("/api/schedule-v2", json=request)

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors
    assert all(error["msg"] == "Invalid value for this field." for error in errors)
    assert all("input" not in error and "ctx" not in error for error in errors)


def test_schedule_v2_rejects_more_than_50_employees_before_solving():
    request = load_sample("pipeline-planned-calls-request.json")
    employee = request["employees"][0]
    request["employees"] += [
        {**employee, "id": f"e{index:02}"}
        for index in range(2, 52)
    ]

    response = client.post("/api/schedule-v2", json=request)

    assert response.status_code == 422
    assert any(error["loc"] == ["body", "employees"] for error in response.json()["detail"])


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
        total_employee_slots=None,
        overstaff_slots=None,
        stage_two_status=stage_two_status,
        warnings=["synthetic solver warning"],
        message="synthetic solver status",
    )
    monkeypatch.setattr(pipeline, "solve_schedule_v2", lambda request: solver_result)

    response = client.post(
        "/api/schedule-v2",
        json=load_sample("pipeline-planned-calls-request.json"),
    )

    assert response.status_code == 200, response.text
    schedule = response.json()["schedule"]
    assert schedule["status"] == status
    assert schedule["stage_two_status"] == stage_two_status
    assert schedule["warnings"] == ["synthetic solver warning"]
    assert schedule["message"] == "synthetic solver status"


def test_schedule_v2_returns_domain_errors_as_422_without_traceback():
    request = load_sample("pipeline-planned-calls-request.json")
    request["planned_calls"][0]["calls_per_hour"] = 12.0
    request["max_agents"] = 1

    response = client.post("/api/schedule-v2", json=request)

    assert response.status_code == 422
    assert "max_agents=1" in response.json()["detail"]
    assert "Traceback" not in response.text


def test_solver_preview_validation_errors_remain_fastapi_422():
    response = client.post("/api/solver-preview", json={})

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
