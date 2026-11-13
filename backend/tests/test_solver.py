import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.models import ScheduleRequest
from backend.app.solver import solve_schedule


ROOT = Path(__file__).parents[1]


def load_sample(name: str) -> ScheduleRequest:
    path = ROOT / "samples" / name
    return ScheduleRequest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_feasible_has_coverage_and_at_most_one_shift_per_employee_per_day():
    request = load_sample("feasible.json")
    result = solve_schedule(request)

    assert result.status in {"FEASIBLE", "OPTIMAL"}
    by_employee_day: dict[tuple[str, date], int] = {}
    for assignment in result.assignments:
        key = (assignment.employee_id, assignment.date)
        by_employee_day[key] = by_employee_day.get(key, 0) + 1
    assert all(count <= 1 for count in by_employee_day.values())

    for requirement in request.requirements:
        covered = sum(
            assignment.date == requirement.date
            and assignment.start <= requirement.start
            and assignment.end >= requirement.end
            for assignment in result.assignments
        )
        assert covered >= requirement.required_staff


def test_infeasible_is_reported_without_assignments():
    result = solve_schedule(load_sample("infeasible.json"))
    assert result.status == "INFEASIBLE"
    assert result.assignments == []


@pytest.mark.parametrize(
    "payload",
    [
        {"employees": [], "shift_templates": [], "requirements": []},
        {
            "employees": [{"id": "e", "name": "A"}, {"id": "e", "name": "B"}],
            "shift_templates": [{"id": "s", "name": "S", "start": "08:00", "end": "09:00"}],
            "requirements": [{"date": "2026-11-02", "start": "08:00", "end": "09:00", "required_staff": 0}],
        },
        {
            "employees": [{"id": "e", "name": "A"}],
            "shift_templates": [{"id": "s", "name": "S", "start": "09:00", "end": "08:00"}],
            "requirements": [{"date": "2026-11-02", "start": "08:00", "end": "09:00", "required_staff": 0}],
        },
        {
            "employees": [{"id": "e", "name": "A"}],
            "shift_templates": [{"id": "s", "name": "S", "start": "08:00", "end": "09:00"}],
            "requirements": [{"date": "02/11/2026", "start": "08:00", "end": "09:00", "required_staff": 0}],
        },
    ],
)
def test_boundary_validation(payload):
    with pytest.raises(ValidationError):
        ScheduleRequest.model_validate(payload)
