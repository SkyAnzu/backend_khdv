from copy import deepcopy

import pytest
from ortools.sat.python import cp_model
from pydantic import ValidationError

from backend.app import solver_v2
from backend.app.solver_v2 import (
    BreakInterval,
    ShiftAssignment,
    ShiftOption,
    ShiftSlotAssignment,
    SolverV2Request,
    solve_schedule_v2,
)


WEEK = "2026-11-02"
GROUPS = ["sales", "support"]


def time_after(start, minutes):
    total = int(start[:2]) * 60 + int(start[3:]) + minutes
    return f"{total // 60:02d}:{total % 60:02d}" if total < 1440 else "24:00"


def shift(start="08:00", end="09:30", breaks=(), day=WEEK):
    return {
        "date": day,
        "start": start,
        "end": end,
        "breaks": list(breaks),
    }


def employee(
    employee_id="e1",
    skills=("sales",),
    available=None,
    unavailable=(),
    max_minutes=10_080,
    min_days_off=None,
):
    result = {
        "id": employee_id,
        "skills": list(skills),
        "available_slots": available or [],
        "unavailable_dates": list(unavailable),
        "max_shift_minutes_per_week": max_minutes,
    }
    if min_days_off is not None:
        result["min_days_off_per_week"] = min_days_off
    return result


def slot_range(day=WEEK, starts=("08:00", "08:30", "09:00")):
    return [(day, start) for start in starts]


def make_payload(
    slots,
    shifts=None,
    demands=None,
    employees=None,
    groups=None,
    allow_skill_fallback=False,
):
    groups = groups or ["sales", "support"]
    demands = demands or {}
    slots = [{"date": day, "start": start} for day, start in slots]
    if shifts is None:
        first = slots[0]["start"]
        shifts = [shift(first, time_after(first, 90), day=slots[0]["date"])]
    if employees is None:
        employees = [
            employee(
                skills=groups,
                available=slots,
            )
        ]
    return {
        "week_start": WEEK,
        "groups": groups,
        "operating_slots": slots,
        "requirements": [
            {
                "date": slot["date"],
                "start": slot["start"],
                "group_id": group,
                "required_staff": demands.get(
                    (slot["date"], slot["start"], group), 0
                ),
            }
            for slot in slots
            for group in groups
        ],
        "employees": employees,
        "shifts": shifts,
        "allow_skill_fallback": allow_skill_fallback,
        "max_solve_seconds": 3,
    }


def requirements_for(slots, groups=("sales",), required=1):
    return {
        (day, start, group): required
        for day, start in slots
        for group in groups
    }


def test_one_employee_can_work_two_adjacent_non_overlapping_shifts():
    slots = slot_range(
        starts=("08:00", "08:30", "09:00", "09:30", "10:00", "10:30")
    )
    payload = make_payload(
        slots,
        shifts=[shift("08:00", "09:30"), shift("09:30", "11:00")],
        demands=requirements_for(slots, groups=("sales",)),
        employees=[
            employee(
                skills=("sales",),
                available=[{"date": day, "start": start} for day, start in slots],
                max_minutes=180,
                min_days_off=6,
            )
        ],
        groups=["sales"],
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "OPTIMAL"
    assert [(item.start, item.end) for item in result.assignments] == [
        ("08:00", "09:30"),
        ("09:30", "11:00"),
    ]
    assert result.total_shift_minutes == 180
    assert [len(item.groups_by_slot) for item in result.assignments] == [3, 3]


def test_overlapping_shift_envelopes_cannot_be_assigned_to_one_employee():
    slots = slot_range(starts=("08:00", "08:30", "09:00", "09:30"))
    payload = make_payload(
        slots,
        shifts=[shift("08:00", "09:30"), shift("08:30", "10:00")],
        demands=requirements_for(slots, groups=("sales",)),
        employees=[
            employee(
                skills=("sales",),
                available=[{"date": day, "start": start} for day, start in slots],
            )
        ],
        groups=["sales"],
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "INFEASIBLE"
    assert result.assignments == []


def test_weekly_cap_counts_elapsed_shift_duration_including_break():
    slots = slot_range()
    payload = make_payload(
        slots,
        demands=requirements_for(slots, groups=("sales",)),
        employees=[
            employee(
                skills=("sales",),
                available=[{"date": day, "start": start} for day, start in slots],
                max_minutes=89,
            )
        ],
        groups=["sales"],
    )

    assert solve_schedule_v2(SolverV2Request.model_validate(payload)).status == "INFEASIBLE"


def test_minimum_days_off_is_counted_over_declared_week():
    slots = [
        (f"2026-11-{day:02d}", start)
        for day in range(2, 9)
        for start in ("08:00", "08:30", "09:00")
    ]
    shifts = [
        shift("08:00", "09:30", day=f"2026-11-{day:02d}")
        for day in range(2, 9)
    ]
    payload = make_payload(
        slots,
        shifts=shifts,
        demands=requirements_for(slots, groups=("sales",)),
        employees=[
            employee(
                skills=("sales",),
                available=[{"date": day, "start": start} for day, start in slots],
                max_minutes=630,
                min_days_off=1,
            )
        ],
        groups=["sales"],
    )

    assert solve_schedule_v2(SolverV2Request.model_validate(payload)).status == "INFEASIBLE"


def test_skill_fallback_requires_opt_in_and_returns_warning():
    slots = slot_range()
    demands = requirements_for(slots, groups=("support",))
    staff = employee(
        skills=("sales",),
        available=[{"date": day, "start": start} for day, start in slots],
    )
    denied = make_payload(
        slots,
        demands=demands,
        employees=[staff],
        groups=GROUPS,
    )
    allowed = make_payload(
        slots,
        demands=demands,
        employees=[staff],
        groups=GROUPS,
        allow_skill_fallback=True,
    )

    assert solve_schedule_v2(SolverV2Request.model_validate(denied)).status == "INFEASIBLE"
    result = solve_schedule_v2(SolverV2Request.model_validate(allowed))
    assert result.status == "OPTIMAL"
    assert result.skill_mismatch_slots == 3
    assert any("kỹ năng" in warning for warning in result.warnings)


def test_skill_match_objective_precedes_elapsed_shift_duration():
    slots = slot_range()
    payload = make_payload(
        slots,
        shifts=[shift("08:00", "09:30")],
        demands={(WEEK, "08:00", "support"): 1},
        employees=[
            employee(
                "sales-worker",
                ("sales",),
                [{"date": day, "start": start} for day, start in slots],
            ),
            employee(
                "support-worker",
                ("support",),
                [{"date": day, "start": start} for day, start in slots],
            ),
        ],
        groups=GROUPS,
        allow_skill_fallback=True,
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "OPTIMAL"
    assert result.skill_mismatch_slots == 0
    assert {item.employee_id for item in result.assignments} == {"support-worker"}


def test_secondary_objective_chooses_shorter_elapsed_shift():
    slots = slot_range(starts=("08:00", "08:30", "09:00"))
    payload = make_payload(
        slots,
        shifts=[shift("08:00", "09:30"), shift("08:00", "08:30")],
        demands={(WEEK, "08:00", "sales"): 1},
        employees=[
            employee(
                skills=("sales",),
                available=[{"date": day, "start": start} for day, start in slots],
            )
        ],
        groups=["sales"],
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "OPTIMAL"
    assert [(item.start, item.end) for item in result.assignments] == [
        ("08:00", "08:30")
    ]
    assert result.total_shift_minutes == 30


def test_multiskill_employee_cannot_cover_two_groups_in_one_slot():
    slots = slot_range()
    payload = make_payload(
        slots,
        demands=requirements_for(slots, groups=GROUPS),
        employees=[
            employee(
                skills=GROUPS,
                available=[{"date": day, "start": start} for day, start in slots],
            )
        ],
        groups=GROUPS,
    )

    assert solve_schedule_v2(SolverV2Request.model_validate(payload)).status == "INFEASIBLE"


def test_zero_demand_needs_no_assignments_and_reports_zero_minutes():
    result = solve_schedule_v2(
        SolverV2Request.model_validate(make_payload(slot_range()))
    )

    assert result.status == "OPTIMAL"
    assert result.assignments == []
    assert result.total_shift_minutes == 0


def test_demand_cannot_exceed_employee_bound():
    slots = slot_range()
    payload = make_payload(
        slots,
        demands={(WEEK, "08:00", "sales"): solver_v2.MAX_EMPLOYEES},
        groups=["sales"],
    )
    SolverV2Request.model_validate(payload)
    payload["requirements"][0]["required_staff"] += 1

    with pytest.raises(ValidationError):
        SolverV2Request.model_validate(payload)


def test_input_collection_limits_bound_request_size():
    base = make_payload(slot_range())
    invalid = []

    too_many_groups = deepcopy(base)
    too_many_groups["groups"] = [f"group-{index}" for index in range(11)]
    invalid.append(too_many_groups)
    too_many_employees = deepcopy(base)
    too_many_employees["employees"] = [
        {**base["employees"][0], "id": f"e{index}"}
        for index in range(solver_v2.MAX_EMPLOYEES + 1)
    ]
    too_many_slots = deepcopy(base)
    too_many_slots["employees"][0]["available_slots"] = [
        {"date": WEEK, "start": "08:00"} for _ in range(337)
    ]
    invalid.append(too_many_slots)
    too_many_shifts = deepcopy(base)
    too_many_shifts["shifts"] = [
        shift("08:00", "09:30") for _ in range(201)
    ]
    invalid.append(too_many_shifts)

    for item in invalid:
        with pytest.raises(ValidationError):
            SolverV2Request.model_validate(item)

    with pytest.raises(ValidationError) as error:
        SolverV2Request.model_validate(too_many_employees)
    assert error.value.errors()[0]["loc"] == ("employees",)


@pytest.mark.parametrize("field", ["available_slots", "max_shift_minutes_per_week"])
def test_required_employee_fields_cannot_be_omitted(field):
    payload = make_payload(slot_range())
    del payload["employees"][0][field]

    with pytest.raises(ValidationError):
        SolverV2Request.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("available_slots", None),
        ("max_shift_minutes_per_week", None),
        ("unavailable_dates", None),
    ],
)
def test_employee_constraints_reject_null(field, value):
    payload = make_payload(slot_range())
    payload["employees"][0][field] = value
    with pytest.raises(ValidationError):
        SolverV2Request.model_validate(payload)


def test_duplicate_ids_slots_shifts_and_requirements_are_rejected():
    base = make_payload(slot_range())
    mutations = []
    item = deepcopy(base)
    item["employees"].append(deepcopy(item["employees"][0]))
    mutations.append(item)
    item = deepcopy(base)
    item["operating_slots"].append(deepcopy(item["operating_slots"][0]))
    mutations.append(item)
    item = deepcopy(base)
    item["shifts"].append(deepcopy(item["shifts"][0]))
    mutations.append(item)
    item = deepcopy(base)
    item["requirements"].append(deepcopy(item["requirements"][0]))
    mutations.append(item)

    for invalid in mutations:
        with pytest.raises(ValidationError):
            SolverV2Request.model_validate(invalid)


@pytest.mark.parametrize(
    "edit",
    [
        lambda data: data.update(week_start="2026-11-03"),
        lambda data: data.update(week_start="9999-12-27"),
        lambda data: data["operating_slots"][0].update(start="08:15"),
        lambda data: data.update(max_solve_seconds=31),
        lambda data: data["employees"][0].update(max_shift_minutes_per_week=True),
    ],
)
def test_week_slot_and_numeric_limits_are_validated(edit):
    payload = make_payload(slot_range())
    edit(payload)
    with pytest.raises(ValidationError):
        SolverV2Request.model_validate(payload)


def test_shift_envelope_must_fit_operating_slots():
    payload = make_payload(
        slot_range(starts=("08:00", "08:30")),
        shifts=[shift("08:00", "09:30")],
    )
    with pytest.raises(ValidationError, match="operating slots"):
        SolverV2Request.model_validate(payload)


def test_break_slots_do_not_cover_and_full_envelope_requires_availability():
    slots = slot_range(starts=("08:00", "08:30", "09:00"))
    shift_with_break = shift(
        "08:00", "09:30", breaks=[{"start": "08:30", "end": "09:00"}]
    )
    demand_during_break = make_payload(
        slots,
        shifts=[shift_with_break],
        demands={(WEEK, "08:30", "sales"): 1},
        groups=["sales"],
    )
    unavailable_break = make_payload(
        slots,
        shifts=[shift_with_break],
        demands={(WEEK, "08:00", "sales"): 1},
        employees=[
            employee(
                skills=("sales",),
                available=[
                    {"date": WEEK, "start": "08:00"},
                    {"date": WEEK, "start": "09:00"},
                ],
            )
        ],
        groups=["sales"],
    )

    assert (
        solve_schedule_v2(SolverV2Request.model_validate(demand_during_break)).status
        == "INFEASIBLE"
    )
    assert (
        solve_schedule_v2(SolverV2Request.model_validate(unavailable_break)).status
        == "INFEASIBLE"
    )


@pytest.mark.parametrize(
    "breaks",
    [
        [{"start": "08:00", "end": "08:00"}],
        [{"start": "08:15", "end": "08:45"}],
        [{"start": "07:30", "end": "08:00"}],
        [
            {"start": "08:30", "end": "09:00"},
            {"start": "08:30", "end": "09:00"},
        ],
        [{"start": "08:00", "end": "09:30"}],
    ],
)
def test_invalid_breaks_empty_shifts_and_all_break_shifts_are_rejected(breaks):
    payload = make_payload(
        slot_range(),
        shifts=[shift("08:00", "09:30", breaks=breaks)],
    )
    with pytest.raises(ValidationError):
        SolverV2Request.model_validate(payload)


def test_empty_break_list_and_24_00_exclusive_end_are_valid():
    slots = [("2026-11-02", "23:30")]
    payload = make_payload(
        slots,
        shifts=[shift("23:30", "24:00", breaks=[])],
        demands=requirements_for(slots, groups=("sales",)),
        employees=[
            employee(
                skills=("sales",),
                available=[{"date": day, "start": start} for day, start in slots],
            )
        ],
        groups=["sales"],
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))
    assert result.status == "OPTIMAL"
    assert result.assignments[0].end == "24:00"
    assert result.total_shift_minutes == 30


@pytest.mark.parametrize(
    ("solver_status", "expected"),
    [
        (cp_model.OPTIMAL, "OPTIMAL"),
        (cp_model.FEASIBLE, "FEASIBLE"),
        (cp_model.INFEASIBLE, "INFEASIBLE"),
        (cp_model.UNKNOWN, "UNKNOWN"),
        (cp_model.MODEL_INVALID, "MODEL_INVALID"),
    ],
)
def test_solver_statuses_are_mapped_without_merging_model_invalid(
    solver_status, expected
):
    assert solver_v2._map_solver_status(solver_status) == expected


def test_stage_one_feasible_returns_without_claiming_duration_optimality(monkeypatch):
    slots = slot_range()
    request = SolverV2Request.model_validate(make_payload(slots))

    class FeasibleSolver:
        parameters = type("Parameters", (), {})()

        def solve(self, model):
            return cp_model.FEASIBLE

        def value(self, variable):
            return 0

    monkeypatch.setattr(solver_v2, "_new_solver", lambda seconds: FeasibleSolver())
    monkeypatch.setattr(
        solver_v2,
        "_assignments_from_solver",
        lambda *args: [
            ShiftAssignment(
                employee_id="e1",
                date=WEEK,
                start="08:00",
                end="09:30",
                breaks=[],
                groups_by_slot=[
                    ShiftSlotAssignment(start=s, group_id="sales")
                    for s in ("08:00", "08:30", "09:00")
                ],
            )
        ],
    )

    result = solve_schedule_v2(request)
    assert result.status == "FEASIBLE"
    assert result.stage_two_status is None
    assert result.total_shift_minutes == 90
    assert "chưa được chứng minh" in result.message


def test_stage_two_candidate_cost_includes_breaks_and_keeps_shorter_incumbent(
    monkeypatch,
):
    slots = slot_range(starts=("08:00", "08:30", "09:00", "09:30"))
    request = SolverV2Request.model_validate(
        make_payload(
            slots,
            shifts=[
                shift("08:00", "09:30"),
                shift(
                    "08:00",
                    "10:00",
                    breaks=[{"start": "08:30", "end": "09:00"}],
                ),
            ],
            groups=["sales"],
        )
    )
    assignments = [
        ShiftAssignment(
            employee_id="e1",
            date=WEEK,
            start="08:00",
            end="09:30",
            breaks=[],
            groups_by_slot=[
                ShiftSlotAssignment(start=s, group_id="sales")
                for s in ("08:00", "08:30", "09:00")
            ],
        ),
        ShiftAssignment(
            employee_id="e1",
            date=WEEK,
            start="08:00",
            end="10:00",
            breaks=[BreakInterval(start="08:30", end="09:00")],
            groups_by_slot=[
                ShiftSlotAssignment(start=s, group_id="sales")
                for s in ("08:00", "09:00", "09:30")
            ],
        ),
    ]
    statuses = iter((cp_model.OPTIMAL, cp_model.FEASIBLE))
    plans = iter(assignments)

    class ScriptedSolver:
        def solve(self, model):
            return next(statuses)

        def value(self, variable):
            return 0

    monkeypatch.setattr(solver_v2, "_new_solver", lambda seconds: ScriptedSolver())
    monkeypatch.setattr(
        solver_v2, "_assignments_from_solver", lambda *args: [next(plans)]
    )

    result = solve_schedule_v2(request)

    assert result.status == "FEASIBLE"
    assert result.stage_two_status == "FEASIBLE"
    assert result.total_shift_minutes == 90
    assert result.assignments[0].end == "09:30"
    assert "giữ lịch bước một" in result.message
    assert "chưa được chứng minh" in result.message


@pytest.mark.parametrize("candidate_minutes", [60, 90])
def test_stage_two_accepts_equal_or_lower_elapsed_cost(monkeypatch, candidate_minutes):
    slots = slot_range()
    request = SolverV2Request.model_validate(make_payload(slots))
    incumbent = ShiftAssignment(
        employee_id="e1",
        date=WEEK,
        start="08:00",
        end="09:30",
        breaks=[],
        groups_by_slot=[
            ShiftSlotAssignment(start=s, group_id="sales")
            for s in ("08:00", "08:30", "09:00")
        ],
    )
    end = time_after("08:00", candidate_minutes)
    candidate = ShiftAssignment(
        employee_id="e1",
        date=WEEK,
        start="08:00",
        end=end,
        breaks=[],
        groups_by_slot=[
            ShiftSlotAssignment(start=s, group_id="sales")
            for s in ("08:00", "08:30", "09:00")[: candidate_minutes // 30]
        ],
    )
    statuses = iter((cp_model.OPTIMAL, cp_model.FEASIBLE))
    plans = iter((incumbent, candidate))

    class ScriptedSolver:
        def solve(self, model):
            return next(statuses)

        def value(self, variable):
            return 0

    monkeypatch.setattr(solver_v2, "_new_solver", lambda seconds: ScriptedSolver())
    monkeypatch.setattr(
        solver_v2, "_assignments_from_solver", lambda *args: [next(plans)]
    )

    result = solve_schedule_v2(request)
    assert result.status == "FEASIBLE"
    assert result.stage_two_status == "FEASIBLE"
    assert result.total_shift_minutes == candidate_minutes


def test_assignment_variable_budget_rejects_before_cp_model(monkeypatch):
    request = SolverV2Request.model_validate(make_payload(slot_range()))
    monkeypatch.setattr(solver_v2, "MAX_ASSIGNMENT_VARIABLES", 1)

    def unexpected_model():
        pytest.fail("assignment budget must be checked before CpModel construction")

    monkeypatch.setattr(solver_v2.cp_model, "CpModel", unexpected_model)
    with pytest.raises(ValueError, match="assignment-variable budget"):
        solve_schedule_v2(request)


@pytest.mark.parametrize(
    ("first_status", "second_status", "expected", "phase"),
    [
        (cp_model.UNKNOWN, None, "UNKNOWN", None),
        (cp_model.MODEL_INVALID, None, "MODEL_INVALID", None),
        (cp_model.OPTIMAL, cp_model.UNKNOWN, "FEASIBLE", "UNKNOWN"),
        (cp_model.OPTIMAL, cp_model.INFEASIBLE, "FEASIBLE", "INFEASIBLE"),
        (cp_model.OPTIMAL, cp_model.MODEL_INVALID, "FEASIBLE", "MODEL_INVALID"),
    ],
)
def test_solver_status_and_stage_two_status_are_preserved(
    monkeypatch, first_status, second_status, expected, phase
):
    request = SolverV2Request.model_validate(make_payload(slot_range()))
    statuses = iter(
        [first_status] if second_status is None else [first_status, second_status]
    )

    class ScriptedSolver:
        def solve(self, model):
            return next(statuses)

        def value(self, variable):
            return 0

    monkeypatch.setattr(solver_v2, "_new_solver", lambda seconds: ScriptedSolver())
    if first_status == cp_model.OPTIMAL:
        monkeypatch.setattr(solver_v2, "_assignments_from_solver", lambda *args: [])

    result = solve_schedule_v2(request)
    assert result.status == expected
    assert result.stage_two_status == phase


def test_shared_solve_budget_returns_first_stage_plan_when_exhausted(monkeypatch):
    class Clock:
        readings = iter((0.0, 2.0))

        @classmethod
        def monotonic(cls):
            return next(cls.readings)

    request_payload = make_payload(slot_range())
    request_payload["max_solve_seconds"] = 1
    request = SolverV2Request.model_validate(request_payload)
    monkeypatch.setattr(solver_v2, "time", Clock)

    class OptimalSolver:
        def solve(self, model):
            return cp_model.OPTIMAL

        def value(self, variable):
            return 0

    monkeypatch.setattr(solver_v2, "_new_solver", lambda seconds: OptimalSolver())
    result = solve_schedule_v2(request)
    assert result.status == "FEASIBLE"
    assert result.stage_two_status == "UNKNOWN"
    assert "hết thời gian" in result.message
