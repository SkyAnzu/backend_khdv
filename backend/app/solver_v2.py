"""Dated 30-minute CP-SAT roster with declared shift envelopes and breaks."""

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


MAX_EMPLOYEES = 100
MAX_GROUPS = 10
MAX_OPERATING_SLOTS = 336
MAX_SHIFT_CANDIDATES = 200
MAX_WEEK_MINUTES = 10_080
MAX_SOLVE_SECONDS = 30
MAX_IDENTIFIER_LENGTH = 100
MAX_ASSIGNMENT_VARIABLES = 300_000
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
SolverStatus = Literal[
    "OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN", "MODEL_INVALID"
]


class SolverModelSizeError(ValueError):
    """A validated schedule exceeds the prototype assignment-variable budget."""


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


def _endpoint_minutes(value: str, *, allow_24: bool = True) -> int:
    if value == "24:00" and allow_24:
        return 24 * 60
    if not TIME_PATTERN.fullmatch(value):
        raise ValueError("time must use HH:MM")
    hour, minute = map(int, value.split(":"))
    if minute not in (0, 30):
        raise ValueError("time must align to the 30-minute grid")
    return hour * 60 + minute


def _slot_start(value: str) -> str:
    _endpoint_minutes(value, allow_24=False)
    return value


def _minutes(value: str) -> int:
    return _endpoint_minutes(value)


def _time_text(minutes: int) -> str:
    return "24:00" if minutes == 24 * 60 else f"{minutes // 60:02d}:{minutes % 60:02d}"


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


class OperatingInterval(_StrictModel):
    date: date
    start: StrictStr
    end: StrictStr

    @field_validator("date", mode="before")
    @classmethod
    def date_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("start")
    @classmethod
    def start_is_valid(cls, value: str) -> str:
        _endpoint_minutes(value, allow_24=False)
        return value

    @field_validator("end")
    @classmethod
    def end_is_valid(cls, value: str) -> str:
        _endpoint_minutes(value)
        return value

    @model_validator(mode="after")
    def interval_is_positive(self) -> "OperatingInterval":
        start, end = _minutes(self.start), _minutes(self.end)
        if start >= end:
            raise ValueError("operating interval must have positive duration")
        return self


class BreakInterval(_StrictModel):
    start: StrictStr
    end: StrictStr

    @field_validator("start")
    @classmethod
    def start_is_valid(cls, value: str) -> str:
        _endpoint_minutes(value, allow_24=False)
        return value

    @field_validator("end")
    @classmethod
    def end_is_valid(cls, value: str) -> str:
        _endpoint_minutes(value)
        return value

    @model_validator(mode="after")
    def interval_is_positive(self) -> "BreakInterval":
        if _minutes(self.start) >= _minutes(self.end):
            raise ValueError("break interval must have positive duration")
        return self


class ShiftOption(_StrictModel):
    date: date
    start: StrictStr
    end: StrictStr
    breaks: list[BreakInterval] = Field(default_factory=list, max_length=48)

    @field_validator("date", mode="before")
    @classmethod
    def date_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("start")
    @classmethod
    def start_is_valid(cls, value: str) -> str:
        _endpoint_minutes(value, allow_24=False)
        return value

    @field_validator("end")
    @classmethod
    def end_is_valid(cls, value: str) -> str:
        _endpoint_minutes(value)
        return value

    @model_validator(mode="after")
    def shift_and_breaks_are_valid(self) -> "ShiftOption":
        start, end = _minutes(self.start), _minutes(self.end)
        if start >= end or (end - start) % 30:
            raise ValueError("shift must have positive 30-minute duration")
        intervals = sorted(
            ( _minutes(item.start), _minutes(item.end)) for item in self.breaks
        )
        previous_end = start
        for break_start, break_end in intervals:
            if break_start < start or break_end > end or break_start < previous_end:
                raise ValueError("breaks must be non-overlapping inside the shift")
            previous_end = break_end
        if sum(end - start for start, end in intervals) >= end - start:
            raise ValueError("shift must contain at least one working slot")
        return self


class EmployeeV2(_StrictModel):
    id: StrictStr
    skills: list[StrictStr] = Field(max_length=MAX_GROUPS)
    available_slots: list[OperatingSlot] = Field(max_length=MAX_OPERATING_SLOTS)
    unavailable_dates: list[date] = Field(max_length=7)
    max_shift_minutes_per_week: StrictInt = Field(ge=0, le=MAX_WEEK_MINUTES)
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


def _interval_slot_keys(interval: OperatingInterval) -> tuple[tuple[date, str], ...]:
    start, end = _minutes(interval.start), _minutes(interval.end)
    return tuple(
        (interval.date, _time_text(value)) for value in range(start, end, 30)
    )


def _shift_envelope_slot_keys(shift: ShiftOption) -> tuple[tuple[date, str], ...]:
    start, end = _minutes(shift.start), _minutes(shift.end)
    return tuple(
        (shift.date, _time_text(value)) for value in range(start, end, 30)
    )


def _shift_working_slot_keys(shift: ShiftOption) -> tuple[tuple[date, str], ...]:
    breaks = {
        value
        for item in shift.breaks
        for value in range(_minutes(item.start), _minutes(item.end), 30)
    }
    return tuple(
        key
        for key in _shift_envelope_slot_keys(shift)
        if _minutes(key[1]) not in breaks
    )


def validate_solver_configuration(
    week_start: date,
    groups: list[str],
    operating_slots: list[OperatingSlot],
    employees: list[EmployeeV2],
    shifts: list[ShiftOption],
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
        if any(day in set(unavailable) for day, _ in availability):
            raise ValueError(
                f"employee {employee.id} cannot be available on an unavailable date"
            )

    shift_keys = [
        (
            shift.date,
            shift.start,
            shift.end,
            tuple((item.start, item.end) for item in shift.breaks),
        )
        for shift in shifts
    ]
    if len(shift_keys) != len(set(shift_keys)):
        raise ValueError("shift candidates must be unique")
    for shift in shifts:
        if shift.date not in week_dates:
            raise ValueError("shift candidates must be within the declared week")
        if not set(_shift_envelope_slot_keys(shift)) <= operating_slot_set:
            raise ValueError("each shift envelope must fit operating slots")
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
    shifts: list[ShiftOption] = Field(max_length=MAX_SHIFT_CANDIDATES)
    allow_skill_fallback: StrictBool = False
    allow_understaffing: StrictBool = False
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
            self.shifts,
        )
        requirement_keys = [
            (item.date, item.start, item.group_id) for item in self.requirements
        ]
        if len(requirement_keys) != len(set(requirement_keys)):
            raise ValueError("slot/group requirements must be unique")
        if any(day not in week_dates for day, _ in operating_slots):
            raise ValueError("operating slots must be within the declared week")
        if any(item.group_id not in groups for item in self.requirements):
            raise ValueError("requirements must use a declared group")
        expected = {
            (day, start, group) for day, start in operating_slots for group in groups
        }
        if set(requirement_keys) != expected:
            raise ValueError(
                "every operating slot needs an explicit requirement for every group, including zero"
            )
        return self


class ShiftSlotAssignment(_StrictModel):
    start: StrictStr
    group_id: StrictStr

    @field_validator("start")
    @classmethod
    def start_is_valid(cls, value: str) -> str:
        return _slot_start(value)

    @field_validator("group_id")
    @classmethod
    def group_id_is_valid(cls, value: str) -> str:
        return _identifier(value, "group id")


class ShiftAssignment(_StrictModel):
    employee_id: StrictStr
    date: date
    start: StrictStr
    end: StrictStr
    breaks: list[BreakInterval] = Field(max_length=48)
    groups_by_slot: list[ShiftSlotAssignment] = Field(min_length=1, max_length=48)

    @field_validator("date", mode="before")
    @classmethod
    def date_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("employee_id")
    @classmethod
    def employee_id_is_valid(cls, value: str) -> str:
        return _identifier(value, "employee id")

    @field_validator("start")
    @classmethod
    def start_is_valid(cls, value: str) -> str:
        _endpoint_minutes(value, allow_24=False)
        return value

    @field_validator("end")
    @classmethod
    def end_is_valid(cls, value: str) -> str:
        _endpoint_minutes(value)
        return value

    @model_validator(mode="after")
    def output_slots_match_shift(self) -> "ShiftAssignment":
        shift = ShiftOption(
            date=self.date,
            start=self.start,
            end=self.end,
            breaks=self.breaks,
        )
        expected_starts = [start for _, start in _shift_working_slot_keys(shift)]
        actual_starts = [item.start for item in self.groups_by_slot]
        if actual_starts != expected_starts:
            raise ValueError("assignment slots must exactly cover working shift slots")
        return self


class SolverV2Response(_StrictModel):
    status: SolverStatus
    assignments: list[ShiftAssignment] = Field(
        max_length=MAX_EMPLOYEES * MAX_SHIFT_CANDIDATES
    )
    skill_mismatch_slots: StrictInt | None
    total_shift_minutes: StrictInt | None
    overstaff_slots: StrictInt | None
    understaff_slots: StrictInt | None = None
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


def _shift_minutes(shift: ShiftOption) -> int:
    return _minutes(shift.end) - _minutes(shift.start)


def _candidate_data(
    request: SolverV2Request,
) -> tuple[
    list[tuple[tuple[date, str], ...]],
    list[tuple[tuple[date, str], ...]],
    list[list[str]],
    list[tuple[int, int]],
]:
    envelope_slots = [_shift_envelope_slot_keys(shift) for shift in request.shifts]
    working_slots = [_shift_working_slot_keys(shift) for shift in request.shifts]
    eligible_groups = [
        request.groups
        if request.allow_skill_fallback
        else [group for group in request.groups if group in employee.skills]
        for employee in request.employees
    ]
    eligible_pairs = []
    estimated_variables = 0
    for employee_index, employee in enumerate(request.employees):
        groups = eligible_groups[employee_index]
        if not groups:
            continue
        available = {(slot.date, slot.start) for slot in employee.available_slots}
        unavailable_dates = set(employee.unavailable_dates)
        for shift_index, shift in enumerate(request.shifts):
            if (
                shift.date in unavailable_dates
                or not set(envelope_slots[shift_index]) <= available
            ):
                continue
            estimated_variables += len(working_slots[shift_index]) * len(groups)
            if estimated_variables > MAX_ASSIGNMENT_VARIABLES:
                raise SolverModelSizeError(
                    "assignment-variable budget exceeded by estimated model size"
                )
            eligible_pairs.append((employee_index, shift_index))
    return envelope_slots, working_slots, eligible_groups, eligible_pairs


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
    working_slots: list[tuple[tuple[date, str], ...]],
    eligible_groups: list[list[str]],
) -> list[ShiftAssignment]:
    assignments = []
    group_indices = {group: index for index, group in enumerate(request.groups)}
    for (employee_index, shift_index), work_var in work_vars.items():
        if not solver.value(work_var):
            continue
        employee = request.employees[employee_index]
        shift = request.shifts[shift_index]
        groups_by_slot = []
        for slot_index, (_, slot_start) in enumerate(working_slots[shift_index]):
            group_id = next(
                (
                    group
                    for group in eligible_groups[employee_index]
                    if solver.value(
                        assignment_vars[
                            (
                                employee_index,
                                shift_index,
                                slot_index,
                                group_indices[group],
                            )
                        ]
                    )
                ),
                None,
            )
            if group_id is None:
                raise RuntimeError("solver returned an unassigned working slot")
            groups_by_slot.append(
                ShiftSlotAssignment(start=slot_start, group_id=group_id)
            )
        assignments.append(
            ShiftAssignment(
                employee_id=employee.id,
                date=shift.date,
                start=shift.start,
                end=shift.end,
                breaks=shift.breaks,
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
    total_minutes = 0
    for assignment in assignments:
        total_minutes += _minutes(assignment.end) - _minutes(assignment.start)
        for slot_assignment in assignment.groups_by_slot:
            group = slot_assignment.group_id
            if group not in skills[assignment.employee_id]:
                mismatch_slots += 1
            key = (assignment.date, slot_assignment.start, group)
            assigned[key] = assigned.get(key, 0) + 1
    overstaff_slots = sum(
        max(0, count - demand.get(key, 0)) for key, count in assigned.items()
    )
    understaff_slots = sum(
        max(0, req - assigned.get(key, 0)) for key, req in demand.items()
    )
    warnings = []
    if understaff_slots:
        warnings.append(
            f"Còn thiếu {understaff_slots} lượt người (khung 30 phút) so với nhu cầu."
        )
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
        total_shift_minutes=total_minutes,
        overstaff_slots=overstaff_slots,
        understaff_slots=understaff_slots,
        stage_two_status=stage_two_status,
        warnings=warnings,
        message=message,
    )


def _empty_response(status: SolverStatus, message: str) -> SolverV2Response:
    return SolverV2Response(
        status=status,
        assignments=[],
        skill_mismatch_slots=None,
        total_shift_minutes=None,
        overstaff_slots=None,
        understaff_slots=None,
        warnings=[],
        message=message,
    )


def solve_schedule_v2(request: SolverV2Request) -> SolverV2Response:
    """Optimize skills first, then elapsed shift minutes including breaks."""
    envelope_slots, working_slots, eligible_groups, eligible_pairs = _candidate_data(
        request
    )
    model = cp_model.CpModel()
    work_vars: dict[tuple[int, int], cp_model.IntVar] = {}
    assignment_vars: dict[tuple[int, int, int, int], cp_model.IntVar] = {}
    employee_envelope_vars: dict[tuple[int, date, str], list[cp_model.IntVar]] = {}
    coverage_vars: dict[tuple[date, str, str], list[cp_model.IntVar]] = {}
    mismatch_vars: list[cp_model.IntVar] = []
    group_indices = {group: index for index, group in enumerate(request.groups)}

    for employee_index, shift_index in eligible_pairs:
        employee = request.employees[employee_index]
        shift = request.shifts[shift_index]
        work = model.new_bool_var(f"work_e{employee_index}_s{shift_index}")
        work_vars[(employee_index, shift_index)] = work
        for day, start in envelope_slots[shift_index]:
            employee_envelope_vars.setdefault(
                (employee_index, day, start), []
            ).append(work)

        for slot_index, (day, start) in enumerate(working_slots[shift_index]):
            slot_vars = []
            for group in eligible_groups[employee_index]:
                group_index = group_indices[group]
                assigned = model.new_bool_var(
                    f"assign_e{employee_index}_s{shift_index}_"
                    f"t{slot_index}_g{group_index}"
                )
                assignment_vars[
                    (employee_index, shift_index, slot_index, group_index)
                ] = assigned
                coverage_vars.setdefault((day, start, group), []).append(assigned)
                slot_vars.append(assigned)
                if group not in employee.skills:
                    mismatch_vars.append(assigned)
            model.add(sum(slot_vars) == work)

    for variables in employee_envelope_vars.values():
        model.add_at_most_one(variables)

    for employee_index, employee in enumerate(request.employees):
        model.add(
            sum(
                _shift_minutes(request.shifts[shift_index]) * variable
                for (index, shift_index), variable in work_vars.items()
                if index == employee_index
            )
            <= employee.max_shift_minutes_per_week
        )
        worked_days = []
        for day in _week_dates(request.week_start):
            day_work = [
                variable
                for (index, shift_index), variable in work_vars.items()
                if index == employee_index and request.shifts[shift_index].date == day
            ]
            worked = model.new_bool_var(f"worked_e{employee_index}_{day.isoformat()}")
            model.add(sum(day_work) >= worked)
            model.add(sum(day_work) <= len(day_work) * worked)
            worked_days.append(worked)
        if employee.min_days_off_per_week is not None:
            model.add(sum(worked_days) <= 7 - employee.min_days_off_per_week)

    understaff_vars: list[cp_model.IntVar] = []
    for req_index, requirement in enumerate(request.requirements):
        slot_coverage = sum(
            coverage_vars.get(
                (requirement.date, requirement.start, requirement.group_id), []
            )
        )
        if request.allow_understaffing and requirement.required_staff > 0:
            slack = model.new_int_var(
                0, requirement.required_staff, f"understaff_{req_index}"
            )
            model.add(slot_coverage + slack >= requirement.required_staff)
            understaff_vars.append(slack)
        else:
            model.add(slot_coverage >= requirement.required_staff)

    if understaff_vars:
        understaff_weight = len(mismatch_vars) + 1
        model.minimize(understaff_weight * sum(understaff_vars) + sum(mismatch_vars))
    else:
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
        request, first_solver, work_vars, assignment_vars, working_slots, eligible_groups
    )
    if first_status == "FEASIBLE":
        return _response_with_schedule(
            request,
            first_assignments,
            "FEASIBLE",
            "Có lịch hợp lệ; mục tiêu ưu tiên độ phủ/kỹ năng chưa được chứng minh tối ưu.",
        )

    if understaff_vars:
        best_understaff = sum(
            first_solver.value(variable) for variable in understaff_vars
        )
        model.add(sum(understaff_vars) == best_understaff)
    best_mismatches = sum(first_solver.value(variable) for variable in mismatch_vars)
    if mismatch_vars:
        model.add(sum(mismatch_vars) == best_mismatches)
    model.minimize(
        sum(
            _shift_minutes(request.shifts[shift_index]) * variable
            for (_, shift_index), variable in work_vars.items()
        )
    )
    remaining = request.max_solve_seconds - (time.monotonic() - solve_start)
    if remaining <= 0:
        return _response_with_schedule(
            request,
            first_assignments,
            "FEASIBLE",
            "Đã chứng minh mức lệch kỹ năng tối thiểu; hết thời gian trước khi "
            "tối ưu tổng phút ca.",
            "UNKNOWN",
        )

    second_solver = _new_solver(remaining)
    second_status = _map_solver_status(second_solver.solve(model))
    if second_status in ("OPTIMAL", "FEASIBLE"):
        candidate_assignments = _assignments_from_solver(
            request,
            second_solver,
            work_vars,
            assignment_vars,
            working_slots,
            eligible_groups,
        )
        candidate = _response_with_schedule(
            request,
            candidate_assignments,
            second_status,
            "Có lịch hợp lệ; tối ưu tổng phút ca chưa được chứng minh.",
            second_status,
        )
        incumbent = _response_with_schedule(
            request,
            first_assignments,
            "FEASIBLE",
            "Đã chứng minh mức lệch kỹ năng tối thiểu; giữ lịch bước một vì ứng "
            "viên bước hai dùng nhiều phút ca hơn. Tối ưu tổng phút ca chưa được "
            "chứng minh.",
            second_status,
        )
        if (
            second_status == "FEASIBLE"
            and candidate.total_shift_minutes > incumbent.total_shift_minutes
        ):
            return incumbent
        if second_status == "OPTIMAL":
            candidate.message = (
                "Đã xếp tối đa nhân sự hiện có, sau đó giảm lệch kỹ năng và tổng phút ca."
                if candidate.understaff_slots
                else "Đã chứng minh tối ưu tuần tự: trước hết lệch kỹ năng, sau đó "
                "tổng phút ca."
            )
        return candidate

    return _response_with_schedule(
        request,
        first_assignments,
        "FEASIBLE",
        "Có lịch hợp lệ từ bước ưu tiên kỹ năng; bước tối ưu tổng phút ca trả "
        f"{second_status}.",
        second_status,
    )
