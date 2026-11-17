from copy import deepcopy

import pytest
from ortools.sat.python import cp_model
from pydantic import ValidationError

from backend.app import solver_v2
from backend.app.solver_v2 import SolverV2Request, solve_schedule_v2


WEEK = "2026-11-02"
GROUPS = ["sales", "support"]


def make_payload(
    slots,
    shifts=None,
    demands=None,
    employees=None,
    groups=None,
    allow_skill_fallback=False,
):
    groups = groups or GROUPS
    demands = demands or {}
    slots = [{"date": day, "start": start} for day, start in slots]
    if shifts is None:
        shifts = [{"date": slot["date"], "start": slot["start"]} for slot in slots[:1]]
    if employees is None:
        employees = [
            {
                "id": "e1",
                "skills": groups,
                "available_slots": slots,
                "unavailable_dates": [],
                "max_work_minutes_per_week": 10_080,
            }
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
        "shift_starts": shifts,
        "allow_skill_fallback": allow_skill_fallback,
        "max_solve_seconds": 3,
    }


def slot_range(day=WEEK, starts=("08:00", "08:30", "09:00")):
    return [(day, start) for start in starts]


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
        "max_work_minutes_per_week": max_minutes,
    }
    if min_days_off is not None:
        result["min_days_off_per_week"] = min_days_off
    return result


def demands_for(slots, groups=("sales",), required=1):
    return {
        (day, start, group): required
        for day, start in slots
        for group in groups
    }


def test_one_employee_can_work_two_non_overlapping_shifts_in_one_day():
    slots = slot_range(starts=("08:00", "08:30", "09:00", "09:30", "10:00", "10:30"))
    payload = make_payload(
        slots,
        shifts=[{"date": WEEK, "start": "08:00"}, {"date": WEEK, "start": "09:30"}],
        demands=demands_for(slots),
        employees=[
            employee(
                available=[{"date": day, "start": start} for day, start in slots],
                max_minutes=180,
                min_days_off=6,
            )
        ],
        groups=["sales"],
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "OPTIMAL"
    assert len(result.assignments) == 2
    assert [(item.start, item.end) for item in result.assignments] == [
        ("08:00", "09:30"),
        ("09:30", "11:00"),
    ]
    assert result.total_employee_slots == 6
    assert all(len(item.groups_by_slot) == 3 for item in result.assignments)


def test_overlapping_shifts_are_not_given_to_the_same_employee():
    slots = slot_range(starts=("08:00", "08:30", "09:00", "09:30"))
    payload = make_payload(
        slots,
        shifts=[{"date": WEEK, "start": "08:00"}, {"date": WEEK, "start": "08:30"}],
        demands=demands_for(slots, groups=("sales",)),
        employees=[
            employee(
                available=[{"date": day, "start": start} for day, start in slots],
                max_minutes=180,
            )
        ],
        groups=["sales"],
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "INFEASIBLE"
    assert result.assignments == []


def test_weekly_work_limit_and_unavailable_dates_are_hard_constraints():
    slots = slot_range()
    demands = demands_for(slots, groups=("sales",))
    shift = [{"date": WEEK, "start": "08:00"}]
    available = [{"date": day, "start": start} for day, start in slots]

    over_limit = make_payload(
        slots,
        shifts=shift,
        demands=demands,
        employees=[employee(available=available, max_minutes=89)],
        groups=["sales"],
    )
    unavailable = make_payload(
        slots,
        shifts=shift,
        demands=demands,
        employees=[
            employee(
                available=[],
                unavailable=[WEEK],
            )
        ],
        groups=["sales"],
    )

    assert solve_schedule_v2(SolverV2Request.model_validate(over_limit)).status == "INFEASIBLE"
    assert solve_schedule_v2(SolverV2Request.model_validate(unavailable)).status == "INFEASIBLE"


def test_minimum_days_off_is_counted_over_the_declared_week():
    slots = [(f"2026-11-{day:02d}", start) for day in range(2, 9) for start in ("08:00", "08:30", "09:00")]
    shifts = [{"date": day, "start": "08:00"} for day in (f"2026-11-{n:02d}" for n in range(2, 9))]
    payload = make_payload(
        slots,
        shifts=shifts,
        demands=demands_for(slots, groups=("sales",)),
        employees=[
            employee(
                available=[{"date": day, "start": start} for day, start in slots],
                max_minutes=7 * 90,
                min_days_off=1,
            )
        ],
        groups=["sales"],
    )

    assert solve_schedule_v2(SolverV2Request.model_validate(payload)).status == "INFEASIBLE"


def test_skill_fallback_requires_opt_in_and_returns_a_warning():
    slots = slot_range()
    demands = demands_for(slots, groups=("support",))
    available = [{"date": day, "start": start} for day, start in slots]
    staff = employee(skills=("sales",), available=available)

    denied = make_payload(
        slots,
        demands=demands,
        employees=[staff],
        groups=GROUPS,
        allow_skill_fallback=False,
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


def test_skill_priority_is_optimized_before_total_work_slots():
    slots = slot_range()
    demands = demands_for(slots, groups=("sales",))
    demands[(WEEK, "08:30", "support")] = 1
    available = [{"date": day, "start": start} for day, start in slots]
    payload = make_payload(
        slots,
        demands=demands,
        employees=[
            employee("sales-worker", ("sales",), available),
            employee("support-worker", ("support",), available),
        ],
        groups=GROUPS,
        allow_skill_fallback=True,
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "OPTIMAL"
    assert result.skill_mismatch_slots == 0
    assert result.total_employee_slots == 6
    assert result.overstaff_slots == 2
    assert len(result.assignments) == 2
    assert any("vượt" in warning for warning in result.warnings)


def test_secondary_objective_reduces_total_employee_slots():
    slots = slot_range()
    payload = make_payload(
        slots,
        demands=demands_for(slots, groups=("sales",)),
        employees=[
            employee(
                employee_id,
                skills=("sales",),
                available=[{"date": day, "start": start} for day, start in slots],
            )
            for employee_id in ("e1", "e2")
        ],
        groups=["sales"],
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "OPTIMAL"
    assert result.skill_mismatch_slots == 0
    assert result.total_employee_slots == 3
    assert len(result.assignments) == 1


def test_multiskill_employee_cannot_cover_two_groups_in_one_slot():
    slots = slot_range()
    demands = demands_for(slots, groups=GROUPS)
    payload = make_payload(
        slots,
        demands=demands,
        employees=[
            employee(
                skills=GROUPS,
                available=[{"date": day, "start": start} for day, start in slots],
            )
        ],
        groups=GROUPS,
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "INFEASIBLE"
    assert result.assignments == []


def test_all_groups_must_be_explicit_even_when_the_demand_is_zero():
    payload = make_payload(slot_range())
    payload["requirements"].pop()
    with pytest.raises(ValidationError, match="every operating slot"):
        SolverV2Request.model_validate(payload)


def test_explicit_zero_demand_is_valid_and_needs_no_assignments():
    payload = make_payload(slot_range())

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "OPTIMAL"
    assert result.assignments == []
    assert result.total_employee_slots == 0


def test_demand_cannot_exceed_the_employee_input_bound():
    slots = slot_range()
    payload = make_payload(
        slots,
        demands={(WEEK, "08:00", "sales"): 51},
    )

    with pytest.raises(ValidationError):
        SolverV2Request.model_validate(payload)


def test_input_collection_limits_bound_solver_model_size():
    base = make_payload(slot_range())
    invalid_payloads = []

    too_many_groups = deepcopy(base)
    too_many_groups["groups"] = [f"group-{index}" for index in range(11)]
    invalid_payloads.append(too_many_groups)

    too_many_employees = deepcopy(base)
    too_many_employees["employees"] = [
        deepcopy(base["employees"][0]) for _ in range(51)
    ]
    invalid_payloads.append(too_many_employees)

    too_many_slots = deepcopy(base)
    too_many_slots["employees"][0]["available_slots"] = [
        deepcopy(base["operating_slots"][0]) for _ in range(337)
    ]
    invalid_payloads.append(too_many_slots)

    too_many_shifts = deepcopy(base)
    too_many_shifts["shift_starts"] = [
        {"date": WEEK, "start": "08:00"} for _ in range(201)
    ]
    invalid_payloads.append(too_many_shifts)

    for payload in invalid_payloads:
        with pytest.raises(ValidationError):
            SolverV2Request.model_validate(payload)


def test_empty_unavailability_is_explicit_but_missing_field_is_invalid():
    payload = make_payload(slot_range())
    assert SolverV2Request.model_validate(payload).employees[0].unavailable_dates == []

    del payload["employees"][0]["unavailable_dates"]
    with pytest.raises(ValidationError):
        SolverV2Request.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    ["available_slots", "max_work_minutes_per_week"],
)
def test_required_work_input_fields_cannot_be_omitted(field):
    payload = make_payload(slot_range())
    del payload["employees"][0][field]

    with pytest.raises(ValidationError):
        SolverV2Request.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("available_slots", None),
        ("max_work_minutes_per_week", None),
        ("unavailable_dates", None),
    ],
)
def test_required_work_constraints_reject_null(field, value):
    payload = make_payload(slot_range())
    payload["employees"][0][field] = value
    with pytest.raises(ValidationError):
        SolverV2Request.model_validate(payload)


def test_duplicate_ids_slots_and_shift_starts_are_rejected():
    payload = make_payload(slot_range())
    duplicate_employee = deepcopy(payload)
    duplicate_employee["employees"].append(deepcopy(duplicate_employee["employees"][0]))
    duplicate_slot = deepcopy(payload)
    duplicate_slot["operating_slots"].append(duplicate_slot["operating_slots"][0])
    duplicate_shift = deepcopy(payload)
    duplicate_shift["shift_starts"].append(duplicate_shift["shift_starts"][0])
    duplicate_demand = deepcopy(payload)
    duplicate_demand["requirements"].append(
        deepcopy(duplicate_demand["requirements"][0])
    )

    for invalid in (
        duplicate_employee,
        duplicate_slot,
        duplicate_shift,
        duplicate_demand,
    ):
        with pytest.raises(ValidationError):
            SolverV2Request.model_validate(invalid)


@pytest.mark.parametrize(
    "edit",
    [
        lambda data: data.update(week_start="2026-11-03"),
        lambda data: data.update(week_start="9999-12-27"),
        lambda data: data["operating_slots"][0].update(start="08:15"),
        lambda data: data.update(max_solve_seconds=31),
        lambda data: data["employees"][0].update(max_work_minutes_per_week=True),
    ],
)
def test_week_slot_and_numeric_limits_are_validated(edit):
    payload = make_payload(slot_range())
    edit(payload)
    with pytest.raises(ValidationError):
        SolverV2Request.model_validate(payload)


def test_a_shift_must_be_inside_operating_slots_and_not_cross_midnight():
    payload = make_payload(
        slot_range(starts=("08:00", "08:30")),
        shifts=[{"date": WEEK, "start": "08:00"}],
    )
    with pytest.raises(ValidationError, match="three operating slots"):
        SolverV2Request.model_validate(payload)


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
def test_solver_statuses_are_mapped_without_merging_model_invalid(solver_status, expected):
    assert solver_v2._map_solver_status(solver_status) == expected


def test_stage_one_feasible_is_returned_without_claiming_optimality(monkeypatch):
    real_solver = cp_model.CpSolver

    class FeasibleSolver:
        def __init__(self):
            self.inner = real_solver()

        @property
        def parameters(self):
            return self.inner.parameters

        def solve(self, model):
            self.inner.solve(model)
            return cp_model.FEASIBLE

        def value(self, variable):
            return self.inner.value(variable)

    monkeypatch.setattr(solver_v2.cp_model, "CpSolver", FeasibleSolver)
    slots = slot_range()
    payload = make_payload(
        slots,
        demands=demands_for(slots, groups=("sales",)),
        groups=["sales"],
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "FEASIBLE"
    assert result.assignments
    assert result.stage_two_status is None


def test_shared_time_budget_returns_first_stage_schedule_when_exhausted(monkeypatch):
    class Clock:
        readings = iter((0.0, 2.0))

        @classmethod
        def monotonic(cls):
            return next(cls.readings)

    monkeypatch.setattr(solver_v2, "time", Clock)
    slots = slot_range()
    payload = make_payload(
        slots,
        demands=demands_for(slots, groups=("sales",)),
        groups=["sales"],
    )
    payload["max_solve_seconds"] = 1

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "FEASIBLE"
    assert result.stage_two_status == "UNKNOWN"
    assert result.assignments


@pytest.mark.parametrize(
    "second_status",
    [
        cp_model.FEASIBLE,
        cp_model.UNKNOWN,
        cp_model.INFEASIBLE,
        cp_model.MODEL_INVALID,
    ],
)
def test_unproven_second_stage_keeps_or_returns_a_feasible_schedule(
    monkeypatch, second_status
):
    real_solver = cp_model.CpSolver
    statuses = iter((cp_model.OPTIMAL, second_status))

    class ScriptedSolver:
        def __init__(self):
            self.inner = real_solver()

        @property
        def parameters(self):
            return self.inner.parameters

        def solve(self, model):
            self.inner.solve(model)
            return next(statuses)

        def value(self, variable):
            return self.inner.value(variable)

    monkeypatch.setattr(solver_v2.cp_model, "CpSolver", ScriptedSolver)
    slots = slot_range()
    payload = make_payload(
        slots,
        demands=demands_for(slots, groups=("sales",)),
        groups=["sales"],
    )

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == "FEASIBLE"
    assert result.stage_two_status == solver_v2._map_solver_status(second_status)
    assert result.assignments


@pytest.mark.parametrize(
    ("first_plan", "second_plan", "expected_plan"),
    [
        (
            frozenset({("e1", "08:00")}),
            frozenset({("e1", "08:00")}),
            frozenset({("e1", "08:00")}),
        ),
        (
            frozenset({("e1", "08:00"), ("e2", "08:30")}),
            frozenset({("e2", "08:30")}),
            frozenset({("e2", "08:30")}),
        ),
        (
            frozenset({("e1", "08:00")}),
            frozenset({("e1", "08:00"), ("e2", "08:30")}),
            frozenset({("e1", "08:00")}),
        ),
    ],
    ids=("equal", "better", "worse"),
)
def test_feasible_second_stage_never_replaces_incumbent_with_more_slots(
    monkeypatch, first_plan, second_plan, expected_plan
):
    slots = slot_range(starts=("08:00", "08:30", "09:00", "09:30"))
    payload = make_payload(
        slots,
        shifts=[
            {"date": WEEK, "start": "08:00"},
            {"date": WEEK, "start": "08:30"},
        ],
        demands={
            (WEEK, "08:30", "sales"): 1,
            (WEEK, "09:00", "sales"): 1,
        },
        employees=[
            employee(
                employee_id,
                skills=("sales",),
                available=[{"date": day, "start": start} for day, start in slots],
                max_minutes=90,
            )
            for employee_id in ("e1", "e2")
        ],
        groups=["sales"],
    )
    real_solver = cp_model.CpSolver
    plans = iter((first_plan, second_plan))
    statuses = iter((cp_model.OPTIMAL, cp_model.FEASIBLE))

    class ScriptedSolver:
        def __init__(self):
            self.inner = real_solver()
            self.plan = frozenset()

        @property
        def parameters(self):
            return self.inner.parameters

        def solve(self, model):
            assert self.inner.solve(model) == cp_model.OPTIMAL
            self.plan = next(plans)
            return next(statuses)

        def value(self, variable):
            parts = variable.name.split("_")
            employee = payload["employees"][int(parts[1][1:])]["id"]
            shift = payload["shift_starts"][int(parts[2][1:])]["start"]
            selected = (employee, shift) in self.plan
            return int(selected and (parts[0] == "work" or parts[4] == "g0"))

    def assert_plan_is_valid(plan):
        employees = {item["id"]: item for item in payload["employees"]}
        required = {
            (item["date"], item["start"], item["group_id"]): item["required_staff"]
            for item in payload["requirements"]
            if item["required_staff"]
        }
        employee_slots, minutes, coverage = set(), {}, {}
        for employee_id, start in plan:
            worker = employees[employee_id]
            assert "sales" in worker["skills"]
            shift_minutes = int(start[:2]) * 60 + int(start[3:])
            shift_slots = [
                f"{(shift_minutes + offset) // 60:02d}:{(shift_minutes + offset) % 60:02d}"
                for offset in (0, 30, 60)
            ]
            available = {(slot["date"], slot["start"]) for slot in worker["available_slots"]}
            assert all((WEEK, slot) in available for slot in shift_slots)
            minutes[employee_id] = minutes.get(employee_id, 0) + 90
            for slot in shift_slots:
                key = (employee_id, WEEK, slot)
                assert key not in employee_slots
                employee_slots.add(key)
                key = (WEEK, slot, "sales")
                coverage[key] = coverage.get(key, 0) + 1

        assert all(minutes[id] <= employees[id]["max_work_minutes_per_week"] for id in minutes)
        assert all(coverage.get(key, 0) >= count for key, count in required.items())

    assert_plan_is_valid(first_plan)
    assert_plan_is_valid(second_plan)
    monkeypatch.setattr(solver_v2.cp_model, "CpSolver", ScriptedSolver)

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert {(item.employee_id, item.start) for item in result.assignments} == expected_plan
    assert result.status == "FEASIBLE"
    assert result.stage_two_status == "FEASIBLE"
    assert result.total_employee_slots == 3
    assert result.skill_mismatch_slots == 0
    assert result.overstaff_slots == 1
    assert result.warnings == ["Có 1 person-slots vượt nhu cầu theo nhóm/slot."]
    if len(second_plan) > len(first_plan):
        assert "giữ lịch bước một" in result.message
        assert "chưa được chứng minh" in result.message


@pytest.mark.parametrize(
    ("first_status", "expected"),
    [
        (cp_model.UNKNOWN, "UNKNOWN"),
        (cp_model.MODEL_INVALID, "MODEL_INVALID"),
    ],
)
def test_first_stage_unknown_or_invalid_is_reported_without_assignments(
    monkeypatch, first_status, expected
):
    real_solver = cp_model.CpSolver

    class ScriptedSolver:
        def __init__(self):
            self.inner = real_solver()

        @property
        def parameters(self):
            return self.inner.parameters

        def solve(self, model):
            return first_status

    monkeypatch.setattr(solver_v2.cp_model, "CpSolver", ScriptedSolver)
    payload = make_payload(slot_range())

    result = solve_schedule_v2(SolverV2Request.model_validate(payload))

    assert result.status == expected
    assert result.assignments == []
