from copy import deepcopy
from datetime import date, timedelta
import math

import pytest
from pydantic import ValidationError

from backend.app import pipeline
from backend.app.models import ErlangCResponse
from backend.app.pipeline import PipelineDomainError, PipelineRequest, run_pipeline
from backend.app.solver_v2 import SolverV2Response


WEEK = "2026-11-02"
GROUPS = [{"id": "sales", "name": "Sales"}, {"id": "support", "name": "Support"}]
SLOTS = ("08:00", "08:30")


def operating_intervals():
    return [{"date": WEEK, "start": "08:00", "end": "09:00"}]


def employee(employee_id="e1", skills=("sales", "support"), slots=SLOTS):
    return {
        "id": employee_id,
        "skills": list(skills),
        "available_slots": [{"date": WEEK, "start": start} for start in slots],
        "unavailable_dates": [],
        "max_shift_minutes_per_week": 600,
    }


def planned_rows(calls=None, groups=None, slots=SLOTS):
    calls = calls or {}
    groups = groups or ["sales", "support"]
    return [
        {
            "date": WEEK,
            "start": start,
            "group_id": group,
            "calls": calls.get((start, group), 0.0),
        }
        for start in slots
        for group in groups
    ]


def full_week_planned_payload():
    group_ids = [f"group-{index}" for index in range(10)]
    week_start = date.fromisoformat(WEEK)
    operating = [
        {
            "date": (week_start + timedelta(days=day)).isoformat(),
            "start": "00:00",
            "end": "24:00",
        }
        for day in range(7)
    ]
    request = payload(
        groups=[{"id": group, "name": group} for group in group_ids],
        operating=operating,
        targets=service_targets(group_ids),
        aht=[],
    )
    request["employees"] = []
    request["shifts"] = []
    request["planned_calls"] = [
        {
            "date": interval["date"],
            "start": f"{minute // 60:02d}:{minute % 60:02d}",
            "group_id": group,
            "calls": 0.0,
        }
        for interval in operating
        for minute in range(0, 24 * 60, 30)
        for group in group_ids
    ]
    return request


def aht_rows(groups=("sales", "support"), cutoff=WEEK):
    return [
        {
            "group_id": group,
            "aht_seconds": 60.0,
            "sample_count": 20,
            "cutoff_before": cutoff,
        }
        for group in groups
    ]


def service_targets(groups=("sales", "support")):
    return [
        {"group_id": group, "target_sla": 0.8, "waiting_seconds": 20.0}
        for group in groups
    ]


def payload(
    *,
    mode="planned_calls",
    groups=None,
    operating=None,
    calls=None,
    employees=None,
    shifts=None,
    targets=None,
    aht=None,
):
    group_list = deepcopy(GROUPS if groups is None else groups)
    group_ids = [item["id"] for item in group_list]
    slots = SLOTS
    request = {
        "mode": mode,
        "week_start": WEEK,
        "groups": group_list,
        "operating_intervals": operating or operating_intervals(),
        "employees": employees
        if employees is not None
        else [employee(slots=slots)],
        "shifts": shifts
        if shifts is not None
        else [{"date": WEEK, "start": "08:00", "end": "09:00", "breaks": []}],
        "service_targets": targets
        if targets is not None
        else service_targets(group_ids),
        "aht_by_group": aht if aht is not None else aht_rows(group_ids),
        "allow_skill_fallback": False,
        "max_solve_seconds": 3,
        "max_agents": 10_000,
    }
    if mode == "planned_calls":
        request["planned_calls"] = planned_rows(calls, group_ids, slots)
    else:
        request["history_window"] = {
            "start_date": "2026-10-01",
            "end_date": WEEK,
        }
        request["history"] = [
            {
                "date": day,
                "start": start,
                "group_id": group,
                "calls": values.get((start, group), 0),
            }
            for day, values in (
                ("2026-10-19", {("08:00", "sales"): 2}),
                ("2026-10-26", {("08:00", "sales"): 4}),
            )
            for start in slots
            for group in group_ids
        ]
    return request


def test_planned_calls_are_per_interval_and_erlang_rate_is_doubled(monkeypatch):
    captured = []

    def erlang(request):
        captured.append(request)
        return ErlangCResponse(required_staff=1, service_level=0.8)

    monkeypatch.setattr(pipeline, "calculate_required_staff", erlang)
    request = PipelineRequest.model_validate(
        payload(calls={("08:00", "sales"): 1.5})
    )

    result = run_pipeline(request)

    assert result.mode == "planned_calls"
    assert result.forecast is None
    assert result.interval_results[0].calls == 1.5
    assert result.interval_results[0].erlang.required_staff == 1
    assert captured[0].calls_per_hour == 3.0
    assert captured[0].aht_seconds == 60.0
    assert [item.start for item in result.interval_results] == [
        "08:00",
        "08:00",
        "08:30",
        "08:30",
    ]


def test_history_forecast_uses_explicit_window_and_distinct_prior_dates():
    request = PipelineRequest.model_validate(payload(mode="history"))

    result = run_pipeline(request)

    assert result.mode == "history"
    assert result.forecast.forecasts[0].forecast_calls == 3
    assert result.forecast.forecasts[0].sample_count == 2
    assert result.interval_results[0].calls == 3
    assert "source_status" not in result.interval_results[0].model_dump()


def test_history_rejects_rows_outside_window_or_on_cutoff():
    for day in ("2026-09-28", WEEK):
        request = payload(mode="history")
        request["history"][0]["date"] = day
        with pytest.raises(ValidationError):
            PipelineRequest.model_validate(request)


def test_history_rejects_undeclared_group_rows():
    request = payload(
        mode="history",
        groups=[{"id": "sales", "name": "Sales"}],
        operating=[{"date": WEEK, "start": "08:00", "end": "08:30"}],
    )
    request["employees"] = [employee(skills=("sales",), slots=("08:00",))]
    request["shifts"] = [
        {"date": WEEK, "start": "08:00", "end": "08:30", "breaks": []}
    ]
    request["history"] = [
        {"date": "2026-10-19", "start": "08:00", "group_id": "sales", "calls": 0},
        {"date": "2026-10-26", "start": "08:00", "group_id": "sales", "calls": 0},
        {"date": "2026-10-19", "start": "08:00", "group_id": "queue999", "calls": 999},
        {"date": "2026-10-26", "start": "08:00", "group_id": "queue999", "calls": 999},
    ]

    with pytest.raises(ValidationError):
        PipelineRequest.model_validate(request)


def test_missing_history_rows_are_not_zero_filled():
    request = payload(mode="history")
    request["history"] = [
        item for item in request["history"] if item["group_id"] == "sales"
    ]
    with pytest.raises(PipelineDomainError) as error:
        run_pipeline(PipelineRequest.model_validate(request))
    assert error.value.public_message == (
        "History has insufficient prior samples for one or more targets."
    )


def test_duplicate_history_rows_and_off_grid_rows_are_rejected():
    duplicate = payload(mode="history")
    duplicate["history"].append(deepcopy(duplicate["history"][0]))
    off_grid = payload(mode="history")
    off_grid["history"][0]["start"] = "08:15"
    for request in (duplicate, off_grid):
        with pytest.raises(ValidationError):
            PipelineRequest.model_validate(request)


def test_planned_rows_must_cover_every_slot_and_group_including_zero():
    for mutation in (
        lambda request: request["planned_calls"].pop(),
        lambda request: request["planned_calls"].append(
            deepcopy(request["planned_calls"][0])
        ),
    ):
        invalid = payload()
        mutation(invalid)
        with pytest.raises(ValidationError):
            PipelineRequest.model_validate(invalid)


def test_planned_rows_accept_full_3360_cell_week_through_24_00():
    request = full_week_planned_payload()

    validated = PipelineRequest.model_validate(request)

    assert len(validated.planned_calls) == 3_360
    assert validated.operating_intervals[0].end == "24:00"
    assert validated.planned_calls[-1].start == "23:30"
    assert validated.aht_by_group == []


def test_planned_rows_reject_3361_cells():
    request = full_week_planned_payload()
    request["planned_calls"].append(deepcopy(request["planned_calls"][0]))

    with pytest.raises(ValidationError):
        PipelineRequest.model_validate(request)


def test_explicit_zero_is_valid_and_needs_no_aht():
    request = payload(calls={})
    request["aht_by_group"] = []
    request["employees"] = []
    request["shifts"] = []

    result = run_pipeline(PipelineRequest.model_validate(request))
    assert all(item.erlang.required_staff == 0 for item in result.interval_results)
    assert all(item.erlang.service_level is None for item in result.interval_results)
    assert result.schedule.total_shift_minutes == 0


def test_positive_calls_without_group_aht_are_domain_error():
    request = payload(calls={("08:00", "sales"): 1.0})
    request["aht_by_group"] = [
        item for item in request["aht_by_group"] if item["group_id"] != "sales"
    ]
    with pytest.raises(PipelineDomainError) as error:
        run_pipeline(PipelineRequest.model_validate(request))
    assert error.value.public_message == (
        "AHT seconds are required for groups with positive calls."
    )


@pytest.mark.parametrize(
    "edit",
    [
        lambda request: request["service_targets"].pop(),
        lambda request: request["service_targets"].append(
            deepcopy(request["service_targets"][0])
        ),
        lambda request: request["groups"][0].update(id=" "),
        lambda request: request["planned_calls"][0].update(calls=math.nan),
        lambda request: request["planned_calls"][0].update(calls=True),
    ],
)
def test_service_targets_groups_and_call_values_are_validated(edit):
    request = payload()
    edit(request)
    with pytest.raises(ValidationError):
        PipelineRequest.model_validate(request)


@pytest.mark.parametrize(
    "summary",
    [
        {"group_id": "sales", "aht_seconds": 0.0, "sample_count": 2, "cutoff_before": WEEK},
        {"group_id": "sales", "aht_seconds": 60.0, "sample_count": 0, "cutoff_before": WEEK},
        {"group_id": "sales", "aht_seconds": 60.0, "sample_count": 2, "cutoff_before": "2026-11-03"},
    ],
)
def test_aht_summary_values_and_cutoff_are_validated(summary):
    request = payload(calls={("08:00", "sales"): 1.0})
    request["aht_by_group"] = [summary]
    with pytest.raises(ValidationError):
        PipelineRequest.model_validate(request)


def test_disjoint_operating_intervals_remain_disjoint():
    day = WEEK
    operating = [
        {"date": day, "start": "08:00", "end": "09:00"},
        {"date": day, "start": "13:00", "end": "14:00"},
    ]
    slots = ("08:00", "08:30", "13:00", "13:30")
    request = payload(
        operating=operating,
        employees=[
            employee(
                slots=slots,
            )
        ],
        shifts=[{"date": day, "start": "08:00", "end": "09:00", "breaks": []}],
    )
    request["planned_calls"] = planned_rows(slots=slots)

    result = run_pipeline(PipelineRequest.model_validate(request))
    assert [item.start for item in result.interval_results] == [
        "08:00",
        "08:00",
        "08:30",
        "08:30",
        "13:00",
        "13:00",
        "13:30",
        "13:30",
    ]


def test_stage_variable_budget_is_sanitized_as_known_domain_error(monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_ASSIGNMENT_VARIABLES", 0, raising=False)
    monkeypatch.setattr(
        pipeline,
        "solve_schedule_v2",
        lambda request: pytest.fail("budget should reject before the solver call"),
    )
    request = PipelineRequest.model_validate(payload())
    monkeypatch.setattr(
        pipeline,
        "solve_schedule_v2",
        lambda request: (_ for _ in ()).throw(
            pipeline.SolverModelSizeError("private variable budget detail")
        ),
    )

    with pytest.raises(PipelineDomainError) as error:
        run_pipeline(request)
    assert error.value.public_message == (
        "The schedule exceeds the prototype assignment-variable limit."
    )
    assert "private" not in error.value.public_message


def test_unexpected_solver_validation_error_is_not_rewritten(monkeypatch):
    marker = "unexpected internal model validation"
    monkeypatch.setattr(
        pipeline,
        "solve_schedule_v2",
        lambda request: (_ for _ in ()).throw(ValidationError.from_exception_data(
            "Internal", [{"type": "value_error", "loc": (), "input": None, "ctx": {"error": ValueError(marker)}}]
        )),
    )
    with pytest.raises(ValidationError):
        run_pipeline(PipelineRequest.model_validate(payload()))


def test_infeasible_schedule_status_is_preserved():
    request = payload(
        calls={("08:00", "sales"): 1.0},
        employees=[],
        shifts=[],
    )
    result = run_pipeline(PipelineRequest.model_validate(request))
    assert result.schedule.status == "INFEASIBLE"
    assert result.schedule.assignments == []
    assert result.schedule.stage_two_status is None


def test_unknown_solver_phase_and_warnings_are_returned_unchanged(monkeypatch):
    solver_result = SolverV2Response(
        status="FEASIBLE",
        assignments=[],
        skill_mismatch_slots=0,
        total_shift_minutes=0,
        overstaff_slots=0,
        stage_two_status="UNKNOWN",
        warnings=["synthetic solver warning"],
        message="synthetic solver status",
    )
    monkeypatch.setattr(pipeline, "solve_schedule_v2", lambda request: solver_result)

    result = run_pipeline(PipelineRequest.model_validate(payload()))

    assert result.schedule == solver_result
    assert result.schedule.status == "FEASIBLE"
    assert result.schedule.stage_two_status == "UNKNOWN"
    assert result.schedule.warnings == ["synthetic solver warning"]
