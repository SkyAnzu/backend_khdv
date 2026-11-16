from copy import deepcopy
import math

import pytest
from pydantic import ValidationError

from backend.app import pipeline
from backend.app.pipeline import PipelineDomainError, PipelineRequest, run_pipeline
from backend.app.solver_v2 import SolverV2Response


WEEK = "2026-11-02"


def operating_hours(hours=(8, 9), day=WEEK):
    return [{"date": day, "hour": hour} for hour in hours]


def employee(employee_id, skills=("sales",), hours=(8, 9)):
    return {
        "id": employee_id,
        "skills": list(skills),
        "available_slots": [
            {"date": WEEK, "start": start}
            for hour in hours
            for start in (f"{hour:02d}:00", f"{hour:02d}:30")
        ],
        "unavailable_dates": [],
        "max_work_minutes_per_week": 90,
    }


def planned_calls(hours=(8, 9), groups=("sales",), volumes=None):
    volumes = volumes or {8: 12, 9: 0}
    return [
        {
            "date": WEEK,
            "hour": hour,
            "group_id": group,
            "calls_per_hour": volumes.get((hour, group), volumes.get(hour, 0)),
        }
        for hour in hours
        for group in groups
    ]


def payload(
    *,
    mode="planned_calls",
    hours=(8, 9),
    groups=("sales",),
    volumes=None,
    employees=None,
    shift_starts=None,
    allow_skill_fallback=False,
    max_agents=10_000,
):
    return {
        "mode": mode,
        "week_start": WEEK,
        "groups": list(groups),
        "operating_hours": operating_hours(hours),
        "planned_calls": planned_calls(hours, groups, volumes),
        "employees": employees
        if employees is not None
        else [employee(f"e{index}") for index in range(1, 4)],
        "shift_starts": shift_starts
        if shift_starts is not None
        else [{"date": WEEK, "start": "08:00"}],
        "allow_skill_fallback": allow_skill_fallback,
        "max_solve_seconds": 3,
        "aht_seconds": 300,
        "target_sla": 0.8,
        "waiting_seconds": 20,
        "max_agents": max_agents,
    }


def test_planned_calls_run_through_erlang_and_duplicate_hourly_need_to_two_slots():
    request = PipelineRequest.model_validate(payload())

    result = run_pipeline(request)

    assert result.mode == "planned_calls"
    assert result.forecast is None
    assert [
        (item.hour, item.erlang.required_staff) for item in result.hourly_results
    ] == [
        (8, 3),
        (9, 0),
    ]
    assert result.hourly_results[0].erlang.service_level == pytest.approx(0.9204, abs=1e-4)
    assert [
        (item.start, item.required_staff) for item in result.requirements
    ] == [
        ("08:00", 3),
        ("08:30", 3),
        ("09:00", 0),
        ("09:30", 0),
    ]
    assert result.schedule.status == "OPTIMAL"
    assert result.schedule.stage_two_status == "OPTIMAL"
    assert result.schedule.warnings == [
        "Có 3 person-slots vượt nhu cầu theo nhóm/slot."
    ]


def test_erlang_decimal_boundary_keeps_one_employee_schedule_feasible():
    request_payload = payload(
        volumes={8: 12, 9: 0},
        employees=[employee("e1")],
    )
    request_payload["aht_seconds"] = 60
    request_payload["target_sla"] = 0.8
    request_payload["waiting_seconds"] = 0

    result = run_pipeline(PipelineRequest.model_validate(request_payload))

    assert result.hourly_results[0].erlang.required_staff == 1
    assert result.hourly_results[0].erlang.service_level == 0.8
    assert result.requirements[0].required_staff == 1
    assert result.requirements[1].required_staff == 1
    assert result.schedule.status in {"OPTIMAL", "FEASIBLE"}
    assert len(result.schedule.assignments) == 1
    assert result.schedule.assignments[0].start == "08:00"
    assert len(result.schedule.assignments[0].groups_by_slot) == 3


def test_history_mode_preserves_fractional_forecast_and_sample_count_without_leakage():
    request_payload = payload(mode="history")
    del request_payload["planned_calls"]
    request_payload["history"] = [
        {"date": day, "hour": hour, "group_id": "sales", "calls": calls}
        for day, calls_by_hour in (
            ("2026-10-19", {8: 12, 9: 0}),
            ("2026-10-26", {8: 13, 9: 0}),
        )
        for hour, calls in calls_by_hour.items()
    ]

    result = run_pipeline(PipelineRequest.model_validate(request_payload))

    assert result.mode == "history"
    assert result.forecast.forecasts[0].forecast_calls == 12.5
    assert result.forecast.forecasts[0].sample_count == 2
    assert result.hourly_results[0].calls_per_hour == 12.5
    assert result.hourly_results[0].sample_count == 2


def test_zero_volume_has_no_service_level_and_can_return_empty_schedule():
    request_payload = payload(
        volumes={8: 0, 9: 0},
        employees=[],
        shift_starts=[],
    )

    result = run_pipeline(PipelineRequest.model_validate(request_payload))

    assert all(item.erlang.required_staff == 0 for item in result.hourly_results)
    assert all(item.erlang.service_level is None for item in result.hourly_results)
    assert result.schedule.status == "OPTIMAL"
    assert result.schedule.assignments == []


def test_missing_duplicate_and_extra_hourly_rows_are_rejected():
    missing = payload()
    missing["planned_calls"].pop()
    duplicate = payload()
    duplicate["planned_calls"].append(deepcopy(duplicate["planned_calls"][0]))
    extra = payload()
    extra["planned_calls"].append(
        {"date": WEEK, "hour": 10, "group_id": "sales", "calls_per_hour": 0}
    )

    for invalid in (missing, duplicate, extra):
        with pytest.raises(ValidationError):
            PipelineRequest.model_validate(invalid)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("calls_per_hour", True),
        ("calls_per_hour", "12"),
        ("calls_per_hour", -1),
        ("calls_per_hour", math.nan),
        ("date", "2026-11-2"),
        ("group_id", " "),
    ],
)
def test_planned_call_rows_reject_coercion_and_malformed_values(field, value):
    invalid = payload()
    invalid["planned_calls"][0][field] = value

    with pytest.raises(ValidationError):
        PipelineRequest.model_validate(invalid)


def test_request_rejects_missing_or_multiple_sources_and_direct_history_leakage():
    missing = payload()
    del missing["planned_calls"]
    both = payload()
    both["history"] = [
        {"date": "2026-10-19", "hour": 8, "group_id": "sales", "calls": 1}
    ]

    for invalid in (missing, both):
        with pytest.raises(ValidationError):
            PipelineRequest.model_validate(invalid)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda data: data["operating_hours"][0].update(hour=23), "23:30"),
        (lambda data: data["operating_hours"][0].update(date="2026-10-31"), "week"),
        (lambda data: data["operating_hours"][0].update(hour=True), "hour"),
        (lambda data: data["operating_hours"].append(data["operating_hours"][0]), "unique"),
        (lambda data: data["operating_hours"][1].update(hour=11), "contiguous"),
    ],
)
def test_operating_grid_rejects_unsupported_or_malformed_hours(change, message):
    invalid = payload()
    change(invalid)

    with pytest.raises(ValidationError, match=message):
        PipelineRequest.model_validate(invalid)


def test_history_without_matching_target_group_fails_with_safe_domain_error():
    request_payload = payload(mode="history")
    del request_payload["planned_calls"]
    request_payload["history"] = [
        {"date": day, "hour": hour, "group_id": "other", "calls": 2}
        for day in ("2026-10-19", "2026-10-26")
        for hour in (8, 9)
    ]

    with pytest.raises(PipelineDomainError) as error:
        run_pipeline(PipelineRequest.model_validate(request_payload))
    assert error.value.public_message == (
        "History has insufficient prior samples for one or more targets."
    )


def test_history_inside_scheduling_week_is_rejected_to_prevent_leakage():
    request_payload = payload(mode="history", hours=(8,), employees=[], shift_starts=[])
    del request_payload["planned_calls"]
    request_payload["operating_hours"] = operating_hours(hours=(8,), day="2026-11-03")
    request_payload["history"] = [
        {"date": day, "hour": 8, "group_id": "sales", "calls": 2}
        for day in ("2026-10-20", "2026-10-27", "2026-11-02")
    ]

    with pytest.raises(ValidationError, match="before week_start"):
        PipelineRequest.model_validate(request_payload)


def test_erlang_target_above_solver_cap_is_a_clear_input_error_not_infeasible():
    request_payload = payload(
        volumes={8: 60, 9: 0},
        employees=[],
        max_agents=100,
    )
    request_payload["aht_seconds"] = 3600
    request_payload["target_sla"] = 0.1
    request_payload["waiting_seconds"] = 0

    with pytest.raises(ValueError, match="exceeding.*50"):
        run_pipeline(PipelineRequest.model_validate(request_payload))


def test_erlang_search_limit_failure_is_not_mapped_to_solver_status():
    request_payload = payload(max_agents=2, employees=[])

    with pytest.raises(ValueError, match="max_agents=2"):
        run_pipeline(PipelineRequest.model_validate(request_payload))


def test_shift_start_and_availability_must_fit_generated_operating_slots():
    shift_outside = payload(shift_starts=[{"date": WEEK, "start": "07:30"}])
    availability_outside = payload()
    availability_outside["employees"][0]["available_slots"].append(
        {"date": WEEK, "start": "10:00"}
    )

    with pytest.raises(ValidationError, match="three operating slots"):
        run_pipeline(PipelineRequest.model_validate(shift_outside))
    with pytest.raises(ValidationError, match="must be operating slots"):
        run_pipeline(PipelineRequest.model_validate(availability_outside))


def test_infeasible_schedule_status_is_preserved():
    request_payload = payload(
        groups=("sales", "support"),
        volumes={
            (8, "sales"): 12,
            (9, "sales"): 0,
            (8, "support"): 0,
            (9, "support"): 0,
        },
        employees=[employee("e1", skills=("support",))],
        shift_starts=[{"date": WEEK, "start": "08:00"}],
    )

    result = run_pipeline(PipelineRequest.model_validate(request_payload))

    assert result.schedule.status == "INFEASIBLE"
    assert result.schedule.assignments == []
    assert result.schedule.stage_two_status is None


def test_unknown_solver_phase_and_warnings_are_returned_unchanged(monkeypatch):
    solver_result = SolverV2Response(
        status="FEASIBLE",
        assignments=[],
        skill_mismatch_slots=0,
        total_employee_slots=0,
        overstaff_slots=0,
        stage_two_status="UNKNOWN",
        warnings=["solver warning"],
        message="stage two did not finish",
    )
    monkeypatch.setattr(pipeline, "solve_schedule_v2", lambda request: solver_result)

    result = run_pipeline(PipelineRequest.model_validate(payload()))

    assert result.schedule == solver_result
    assert result.schedule.status == "FEASIBLE"
    assert result.schedule.stage_two_status == "UNKNOWN"
    assert result.schedule.warnings == ["solver warning"]


def test_skill_fallback_warning_is_preserved_from_solver():
    request_payload = payload(
        groups=("sales", "support"),
        volumes={
            (8, "sales"): 0.5,
            (9, "sales"): 0,
            (8, "support"): 0,
            (9, "support"): 0,
        },
        employees=[employee("e1", skills=("support",))],
        allow_skill_fallback=True,
    )

    result = run_pipeline(PipelineRequest.model_validate(request_payload))

    assert result.schedule.status == "OPTIMAL"
    assert result.schedule.skill_mismatch_slots == 2
    assert result.schedule.warnings == [
        "Skill fallback đã dùng: 2 person-slots lệch kỹ năng.",
        "Có 1 person-slots vượt nhu cầu theo nhóm/slot.",
    ]
    assert result.schedule.stage_two_status == "OPTIMAL"


def test_multiskill_employee_is_not_counted_for_two_groups_in_one_slot():
    groups = ("sales", "support")
    request_payload = payload(
        groups=groups,
        volumes={8: 0.5, 9: 0},
        employees=[
            employee("multi", skills=groups),
            employee("second", skills=groups),
        ],
    )

    result = run_pipeline(PipelineRequest.model_validate(request_payload))

    assert result.schedule.status == "OPTIMAL"
    for start in ("08:00", "08:30"):
        assigned = [
            (assignment.employee_id, slot.group_id)
            for assignment in result.schedule.assignments
            for slot in assignment.groups_by_slot
            if slot.start == start
        ]
        assert len(assigned) == 2
        assert {group for _, group in assigned} == set(groups)
        assert len({employee_id for employee_id, _ in assigned}) == 2
