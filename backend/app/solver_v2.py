"""Independent weekly 30-minute/90-minute CP-SAT scheduler prototype."""

from datetime import date, datetime, timedelta
import re
import time
from typing import Literal

from ortools.sat.python import cp_model
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


MAX_EMPLOYEES = 50
MAX_GROUPS = 10
MAX_OPERATING_SLOTS = 336
MAX_SHIFT_STARTS = 200
MAX_WEEK_MINUTES = 10_080
MAX_SOLVE_SECONDS = 30
MAX_IDENTIFIER_LENGTH = 100
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SolverStatus = Literal[
    "OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN", "MODEL_INVALID"
]


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        raise ValueError("date must use ISO format YYYY-MM-DD")
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ValueError("date must use ISO format YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("date must be a valid calendar date") from error


def _identifier(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{label} must be at most {MAX_IDENTIFIER_LENGTH} characters")
    return value


def _slot_start(value: str) -> str:
    if not TIME_PATTERN.fullmatch(value):
        raise ValueError("slot start must use HH:MM")
    minutes = _minutes(value)
    if minutes % 30 or minutes >= 23 * 60 + 30:
        raise ValueError(
            "slot start must be aligned to 30 minutes and stay within the same day"
        )
    return value


def _minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _time_text(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _week_dates(week_start: date) -> tuple[date, ...]:
    return tuple(week_start + timedelta(days=offset) for offset in range(7))


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OperatingSlot(_StrictModel):
    date: date
    start: StrictStr

    @field_validator("date", mode="before")
    @classmethod
    def date_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("start")
    @classmethod
    def start_is_valid(cls, value: str) -> str:
        return _slot_start(value)


class ShiftStart(_StrictModel):
    date: date
    start: StrictStr

    @field_validator("date", mode="before")
    @classmethod
    def date_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("start")
    @classmethod
    def start_is_valid(cls, value: str) -> str:
        return _slot_start(value)


class EmployeeV2(_StrictModel):
    id: StrictStr
    skills: list[StrictStr] = Field(max_length=MAX_GROUPS)
    available_slots: list[OperatingSlot] = Field(max_length=MAX_OPERATING_SLOTS)
    unavailable_dates: list[date] = Field(max_length=7)
    max_work_minutes_per_week: StrictInt = Field(ge=0, le=MAX_WEEK_MINUTES)
    min_days_off_per_week: StrictInt | None = Field(default=None, ge=0, le=7)

    @field_validator("id")
    @classmethod
    def id_is_valid(cls, value: str) -> str:
        return _identifier(value, "employee id")

    @field_validator("skills")
    @classmethod
    def skills_are_normalized(cls, values: list[str]) -> list[str]:
        return [_identifier(value, "skill id") for value in values]

    @field_validator("unavailable_dates", mode="before")
    @classmethod
    def unavailable_dates_are_iso(cls, values: object) -> object:
        if isinstance(values, list):
            return [_parse_date(value) for value in values]
        return values


def validate_solver_configuration(
    week_start: date,
    groups: list[str],
    operating_slots: list[OperatingSlot],
    employees: list[EmployeeV2],
    shift_starts: list[ShiftStart],
) -> tuple[set[date], set[str], set[tuple[date, str]]]:
    if week_start.weekday() != 0:
        raise ValueError("week_start must be a Monday")
    try:
        week_dates = set(_week_dates(week_start))
    except OverflowError as error:
        raise ValueError("declared week exceeds the supported date range") from error
    group_set = set(groups)
    if len(group_set) != len(groups):
        raise ValueError("group ids must be unique")

    slot_keys = [(slot.date, slot.start) for slot in operating_slots]
    if len(slot_keys) != len(set(slot_keys)):
        raise ValueError("operating slots must be unique")
    if any(day not in week_dates for day, _ in slot_keys):
        raise ValueError("operating slots must be within the declared week")
    operating_slot_set = set(slot_keys)

    employee_ids = [employee.id for employee in employees]
    if len(employee_ids) != len(set(employee_ids)):
        raise ValueError("employee ids must be unique")
    for employee in employees:
        if len(employee.skills) != len(set(employee.skills)):
            raise ValueError(f"skills for employee {employee.id} must be unique")
        if not set(employee.skills) <= group_set:
            raise ValueError(
                f"skills for employee {employee.id} must use declared groups"
            )
        availability = [(slot.date, slot.start) for slot in employee.available_slots]
        if len(availability) != len(set(availability)):
            raise ValueError(
                f"available slots for employee {employee.id} must be unique"
            )
        if not set(availability) <= operating_slot_set:
            raise ValueError(
                f"available slots for employee {employee.id} must be operating slots"
            )
        unavailable = employee.unavailable_dates
        if len(unavailable) != len(set(unavailable)):
            raise ValueError(
                f"unavailable dates for employee {employee.id} must be unique"
            )
        if any(day not in week_dates for day in unavailable):
            raise ValueError(
                f"unavailable dates for employee {employee.id} must be within the week"
            )
        unavailable_set = set(unavailable)
        if any(day in unavailable_set for day, _ in availability):
            raise ValueError(
                f"employee {employee.id} cannot be available on an unavailable date"
            )

    shift_keys = [(shift.date, shift.start) for shift in shift_starts]
    if len(shift_keys) != len(set(shift_keys)):
        raise ValueError("shift starts must be unique")
    for day, start in shift_keys:
        if day not in week_dates:
            raise ValueError("shift starts must be within the declared week")
        start_minute = _minutes(start)
        if start_minute > 22 * 60:
            raise ValueError("90-minute shifts must end on the same day")
        if any(
            (day, _time_text(start_minute + offset)) not in operating_slot_set
            for offset in (0, 30, 60)
        ):
            raise ValueError("each shift must contain three operating slots")
    return week_dates, group_set, operating_slot_set


class SlotRequirement(_StrictModel):
    date: date
    start: StrictStr
    group_id: StrictStr
    required_staff: StrictInt = Field(ge=0, le=MAX_EMPLOYEES)

    @field_validator("date", mode="before")
    @classmethod
    def date_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("start")
    @classmethod
    def start_is_valid(cls, value: str) -> str:
        return _slot_start(value)

    @field_validator("group_id")
    @classmethod
    def group_id_is_valid(cls, value: str) -> str:
        return _identifier(value, "group id")


class SolverV2Request(_StrictModel):
    week_start: date
    groups: list[StrictStr] = Field(min_length=1, max_length=MAX_GROUPS)
    operating_slots: list[OperatingSlot] = Field(
        min_length=1, max_length=MAX_OPERATING_SLOTS
    )
    requirements: list[SlotRequirement] = Field(
        min_length=1, max_length=MAX_OPERATING_SLOTS * MAX_GROUPS
    )
    employees: list[EmployeeV2] = Field(max_length=MAX_EMPLOYEES)
    shift_starts: list[ShiftStart] = Field(max_length=MAX_SHIFT_STARTS)
    allow_skill_fallback: StrictBool = False
    max_solve_seconds: StrictInt = Field(default=10, ge=1, le=MAX_SOLVE_SECONDS)

    @field_validator("week_start", mode="before")
    @classmethod
    def week_start_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("groups")
    @classmethod
    def groups_are_normalized(cls, values: list[str]) -> list[str]:
        return [_identifier(value, "group id") for value in values]

    @model_validator(mode="after")
    def schedule_inputs_are_consistent(self) -> "SolverV2Request":
        week_dates, groups, operating_slots = validate_solver_configuration(
            self.week_start,
            self.groups,
            self.operating_slots,
            self.employees,
            self.shift_starts,
        )

        requirement_keys = [
            (item.date, item.start, item.group_id) for item in self.requirements
        ]
        if len(requirement_keys) != len(set(requirement_keys)):
            raise ValueError("slot/group requirements must be unique")
        if any(item.group_id not in groups for item in self.requirements):
            raise ValueError("requirements must use a declared group")
        expected_requirements = {
            (day, start, group) for day, start in operating_slots for group in groups
        }
        if set(requirement_keys) != expected_requirements:
            raise ValueError(
                "every operating slot needs an explicit requirement for every group, including zero"
            )
        return self


class SlotGroupAssignment(_StrictModel):
    start: StrictStr
    group_id: StrictStr


class ShiftAssignment(_StrictModel):
    employee_id: StrictStr
    date: date
    start: StrictStr
    end: StrictStr
    groups_by_slot: list[SlotGroupAssignment] = Field(min_length=3, max_length=3)


class SolverV2Response(_StrictModel):
    status: SolverStatus
    assignments: list[ShiftAssignment]
    skill_mismatch_slots: StrictInt | None
    total_employee_slots: StrictInt | None
    overstaff_slots: StrictInt | None
    stage_two_status: SolverStatus | None = None
    warnings: list[StrictStr]
    message: StrictStr


def _map_solver_status(status: object) -> SolverStatus:
    if status == cp_model.OPTIMAL:
        return "OPTIMAL"
    if status == cp_model.FEASIBLE:
        return "FEASIBLE"
    if status == cp_model.INFEASIBLE:
        return "INFEASIBLE"
    if status == cp_model.MODEL_INVALID:
        return "MODEL_INVALID"
    return "UNKNOWN"


def _shift_slot_keys(shift: ShiftStart) -> tuple[tuple[date, str], ...]:
    start = _minutes(shift.start)
    return tuple(
        (shift.date, _time_text(start + offset)) for offset in (0, 30, 60)
    )


def _new_solver(seconds: float) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_time_in_seconds = seconds
    return solver


def _assignments_from_solver(
    request: SolverV2Request,
    solver: cp_model.CpSolver,
    work_vars: dict[tuple[int, int], cp_model.IntVar],
    assignment_vars: dict[tuple[int, int, int, int], cp_model.IntVar],
) -> list[ShiftAssignment]:
    assignments = []
    for employee_index, employee in enumerate(request.employees):
        for shift_index, shift in enumerate(request.shift_starts):
            work_var = work_vars.get((employee_index, shift_index))
            if work_var is None or not solver.value(work_var):
                continue
            groups_by_slot = []
            for slot_index, (_, slot_start) in enumerate(_shift_slot_keys(shift)):
                group_id = None
                for group_index, group in enumerate(request.groups):
                    variable = assignment_vars.get(
                        (employee_index, shift_index, slot_index, group_index)
                    )
                    if variable is not None and solver.value(variable):
                        group_id = group
                        break
                if group_id is None:
                    raise RuntimeError("solver returned a shift without a group assignment")
                groups_by_slot.append(
                    SlotGroupAssignment(
                        start=slot_start,
                        group_id=group_id,
                    )
                )
            assignments.append(
                ShiftAssignment(
                    employee_id=employee.id,
                    date=shift.date,
                    start=shift.start,
                    end=_time_text(_minutes(shift.start) + 90),
                    groups_by_slot=groups_by_slot,
                )
            )
    return sorted(
        assignments, key=lambda item: (item.date, item.start, item.employee_id)
    )


def _response_with_schedule(
    request: SolverV2Request,
    assignments: list[ShiftAssignment],
    status: SolverStatus,
    message: str,
    stage_two_status: SolverStatus | None = None,
) -> SolverV2Response:
    skills = {employee.id: set(employee.skills) for employee in request.employees}
    demand = {
        (item.date, item.start, item.group_id): item.required_staff
        for item in request.requirements
    }
    assigned: dict[tuple[date, str, str], int] = {}
    mismatch_slots = 0
    for assignment in assignments:
        for group_assignment in assignment.groups_by_slot:
            group = group_assignment.group_id
            if group not in skills[assignment.employee_id]:
                mismatch_slots += 1
            key = (assignment.date, group_assignment.start, group)
            assigned[key] = assigned.get(key, 0) + 1
    overstaff_slots = sum(
        max(0, count - demand.get(key, 0)) for key, count in assigned.items()
    )
    warnings = []
    if mismatch_slots:
        warnings.append(
            f"Skill fallback đã dùng: {mismatch_slots} person-slots lệch kỹ năng."
        )
    if overstaff_slots:
        warnings.append(
            f"Có {overstaff_slots} person-slots vượt nhu cầu theo nhóm/slot."
        )
    return SolverV2Response(
        status=status,
        assignments=assignments,
        skill_mismatch_slots=mismatch_slots,
        total_employee_slots=sum(len(item.groups_by_slot) for item in assignments),
        overstaff_slots=overstaff_slots,
        stage_two_status=stage_two_status,
        warnings=warnings,
        message=message,
    )


def _empty_response(status: SolverStatus, message: str) -> SolverV2Response:
    return SolverV2Response(
        status=status,
        assignments=[],
        skill_mismatch_slots=None,
        total_employee_slots=None,
        overstaff_slots=None,
        warnings=[],
        message=message,
    )


def solve_schedule_v2(request: SolverV2Request) -> SolverV2Response:
    """Find a valid weekly roster, then optimize skill matches before work slots."""
    model = cp_model.CpModel()
    work_vars: dict[tuple[int, int], cp_model.IntVar] = {}
    assignment_vars: dict[tuple[int, int, int, int], cp_model.IntVar] = {}
    employee_slot_vars: dict[tuple[int, date, str], list[cp_model.IntVar]] = {}
    coverage_vars: dict[tuple[date, str, str], list[cp_model.IntVar]] = {}
    mismatch_vars: list[cp_model.IntVar] = []

    for employee_index, employee in enumerate(request.employees):
        available = {(slot.date, slot.start) for slot in employee.available_slots}
        unavailable_dates = set(employee.unavailable_dates)
        for shift_index, shift in enumerate(request.shift_starts):
            shift_slots = _shift_slot_keys(shift)
            if shift.date in unavailable_dates or not set(shift_slots) <= available:
                continue
            work = model.new_bool_var(f"work_e{employee_index}_s{shift_index}")
            work_vars[(employee_index, shift_index)] = work
            for slot_index, (day, start) in enumerate(shift_slots):
                slot_vars = []
                for group_index, group in enumerate(request.groups):
                    if not request.allow_skill_fallback and group not in employee.skills:
                        continue
                    assigned = model.new_bool_var(
                        f"assign_e{employee_index}_s{shift_index}_t{slot_index}_g{group_index}"
                    )
                    assignment_vars[
                        (employee_index, shift_index, slot_index, group_index)
                    ] = assigned
                    employee_slot_vars.setdefault(
                        (employee_index, day, start), []
                    ).append(assigned)
                    coverage_vars.setdefault((day, start, group), []).append(
                        assigned
                    )
                    slot_vars.append(assigned)
                    if group not in employee.skills:
                        mismatch_vars.append(assigned)
                model.add(sum(slot_vars) == work)

    for employee_index, employee in enumerate(request.employees):
        employee_work = [
            variable
            for (index, _), variable in work_vars.items()
            if index == employee_index
        ]
        model.add(90 * sum(employee_work) <= employee.max_work_minutes_per_week)
        worked_days = []
        for day in _week_dates(request.week_start):
            day_work = [
                variable
                for (index, shift_index), variable in work_vars.items()
                if index == employee_index
                and request.shift_starts[shift_index].date == day
            ]
            worked = model.new_bool_var(f"worked_e{employee_index}_{day.isoformat()}")
            model.add(sum(day_work) >= worked)
            model.add(sum(day_work) <= len(day_work) * worked)
            worked_days.append(worked)
        if employee.min_days_off_per_week is not None:
            model.add(sum(worked_days) <= 7 - employee.min_days_off_per_week)

    for variables in employee_slot_vars.values():
        model.add(sum(variables) <= 1)
    for requirement in request.requirements:
        model.add(
            sum(
                coverage_vars.get(
                    (requirement.date, requirement.start, requirement.group_id), []
                )
            )
            >= requirement.required_staff
        )

    model.minimize(sum(mismatch_vars))
    solve_start = time.monotonic()
    first_solver = _new_solver(request.max_solve_seconds)
    first_status = _map_solver_status(first_solver.solve(model))
    if first_status in ("INFEASIBLE", "UNKNOWN", "MODEL_INVALID"):
        messages = {
            "INFEASIBLE": "Không tồn tại lịch thỏa các ràng buộc v2.",
            "UNKNOWN": "Solver chưa tìm được lịch hoặc kết luận trong giới hạn thời gian.",
            "MODEL_INVALID": "CP-SAT từ chối model v2 vì model không hợp lệ.",
        }
        return _empty_response(first_status, messages[first_status])

    first_assignments = _assignments_from_solver(
        request, first_solver, work_vars, assignment_vars
    )
    if first_status == "FEASIBLE":
        return _response_with_schedule(
            request,
            first_assignments,
            "FEASIBLE",
            "Có lịch hợp lệ; mục tiêu ưu tiên kỹ năng chưa được chứng minh tối ưu.",
        )

    best_mismatches = sum(first_solver.value(variable) for variable in mismatch_vars)
    if mismatch_vars:
        model.add(sum(mismatch_vars) == best_mismatches)
    model.minimize(3 * sum(work_vars.values()))
    remaining = request.max_solve_seconds - (time.monotonic() - solve_start)
    if remaining <= 0:
        return _response_with_schedule(
            request,
            first_assignments,
            "FEASIBLE",
            "Mức lệch kỹ năng tối thiểu đã được chứng minh; hết thời gian trước khi tối ưu tổng slot làm.",
            "UNKNOWN",
        )

    second_solver = _new_solver(remaining)
    second_status = _map_solver_status(second_solver.solve(model))
    if second_status in ("OPTIMAL", "FEASIBLE"):
        assignments = _assignments_from_solver(
            request, second_solver, work_vars, assignment_vars
        )
        candidate_slots = sum(len(item.groups_by_slot) for item in assignments)
        incumbent_slots = sum(
            len(item.groups_by_slot) for item in first_assignments
        )
        if second_status == "FEASIBLE" and candidate_slots > incumbent_slots:
            return _response_with_schedule(
                request,
                first_assignments,
                "FEASIBLE",
                "Đã chứng minh mức lệch kỹ năng tối thiểu; giữ lịch bước một vì ứng viên bước hai dùng nhiều slot làm hơn. Tối ưu tổng slot làm chưa được chứng minh.",
                second_status,
            )
        message = (
            "Đã chứng minh tối ưu tuần tự: trước hết lệch kỹ năng, sau đó tổng slot làm."
            if second_status == "OPTIMAL"
            else "Có lịch hợp lệ; tối ưu tổng slot làm chưa được chứng minh."
        )
        return _response_with_schedule(
            request, assignments, second_status, message, second_status
        )

    return _response_with_schedule(
        request,
        first_assignments,
        "FEASIBLE",
        f"Có lịch hợp lệ từ bước ưu tiên kỹ năng; bước tối ưu tổng slot làm trả {second_status}.",
        second_status,
    )
