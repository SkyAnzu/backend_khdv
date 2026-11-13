"""Small, deterministic-enough CP-SAT model for the week 1-2 prototype."""

from datetime import date

from ortools.sat.python import cp_model

from .models import Assignment, ScheduleRequest, SolveResponse


def _dates(request: ScheduleRequest) -> list[date]:
    return sorted({requirement.date for requirement in request.requirements})


def solve_schedule(request: ScheduleRequest) -> SolveResponse:
    """Solve only the two agreed v1 rules.

    Rules:
    1. An employee has at most one shift on each date.
    2. Every requirement interval has enough assigned employees.

    All variables and coefficients are integer/bool CP-SAT values. There is
    deliberately no objective, availability, skill, hour, cost, or time rule.
    """
    model = cp_model.CpModel()
    dates = _dates(request)
    variables: dict[tuple[str, date, str], cp_model.IntVar] = {}

    for employee in request.employees:
        for day in dates:
            day_vars = []
            for shift in request.shift_templates:
                variable = model.new_bool_var(
                    f"work_{employee.id}_{day.isoformat()}_{shift.id}"
                )
                variables[(employee.id, day, shift.id)] = variable
                day_vars.append(variable)
            # A worker can receive zero or one fixed shift on a date.
            model.add(sum(day_vars) <= 1)

    for requirement in request.requirements:
        covering = [
            variables[(employee.id, requirement.date, shift.id)]
            for employee in request.employees
            for shift in request.shift_templates
            if shift.start <= requirement.start and shift.end >= requirement.end
        ]
        # required_staff is validated as a non-negative integer at the boundary.
        model.add(sum(covering) >= requirement.required_staff)

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    status = solver.solve(model)

    if status == cp_model.INFEASIBLE:
        return SolveResponse(
            status="INFEASIBLE",
            assignments=[],
            message="Không tồn tại lịch thỏa hai quy tắc v1.",
        )
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return SolveResponse(
            status="UNKNOWN",
            assignments=[],
            message="Solver không xác định được nghiệm trong lần chạy này.",
        )

    assignments = [
        Assignment(
            employee_id=employee.id,
            date=day,
            shift_id=shift.id,
            shift_name=shift.name,
            start=shift.start,
            end=shift.end,
        )
        for employee in request.employees
        for day in dates
        for shift in request.shift_templates
        if solver.value(variables[(employee.id, day, shift.id)])
    ]
    return SolveResponse(
        status="OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
        assignments=assignments,
        message="Đã tìm thấy lịch thỏa hai quy tắc v1.",
    )
