"""Interval demand, Erlang C staffing, and weekly scheduling pipeline."""

from collections import defaultdict
from datetime import date, datetime, timedelta
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from .erlang_c import calculate_required_staff
from .forecast import forecast_calls
from .models import (
    CallIntervalObservation,
    ErlangCRequest,
    ErlangCResponse,
    ForecastRequest,
    ForecastResponse,
    ForecastTarget,
    HistoryWindow,
    IntervalForecast,
    MAX_ERLANG_C_AGENTS,
    PlannedCallInterval,
    validate_history_keys_unique,
)
from .solver_v2 import (
    MAX_EMPLOYEES,
    MAX_GROUPS,
    MAX_IDENTIFIER_LENGTH,
    MAX_OPERATING_SLOTS,
    MAX_SOLVE_SECONDS,
    MAX_SHIFT_CANDIDATES,
    EmployeeV2,
    OperatingInterval,
    OperatingSlot,
    ShiftAssignment,
    ShiftOption,
    SlotRequirement,
    SolverV2Request,
    SolverV2Response,
    SolverModelSizeError,
    solve_schedule_v2,
    validate_solver_configuration,
)


MAX_WEEK_DAYS = 7
MAX_HISTORY_ROWS = 100_000
MAX_INTERVAL_ROWS = MAX_OPERATING_SLOTS * MAX_GROUPS
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PipelineDomainError(ValueError):
    """A pipeline domain failure with distinct internal and public messages."""

    def __init__(self, internal_message: str, *, public_message: str):
        super().__init__(internal_message)
        self.public_message = public_message


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


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PipelineGroup(_StrictModel):
    id: StrictStr
    name: StrictStr

    @field_validator("id", "name")
    @classmethod
    def text_is_bounded(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > MAX_IDENTIFIER_LENGTH:
            raise ValueError("group id and name must be non-empty and bounded")
        return value


class PipelineServiceTarget(_StrictModel):
    group_id: StrictStr
    target_sla: StrictFloat = Field(gt=0, lt=1, allow_inf_nan=False)
    waiting_seconds: StrictFloat = Field(ge=0, allow_inf_nan=False)

    @field_validator("group_id")
    @classmethod
    def group_id_is_bounded(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > MAX_IDENTIFIER_LENGTH:
            raise ValueError("group id must be non-empty and bounded")
        return value


class _PipelineAHTValue(_StrictModel):
    group_id: StrictStr
    aht_seconds: StrictFloat = Field(gt=0, allow_inf_nan=False)

    @field_validator("group_id")
    @classmethod
    def group_id_is_bounded(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > MAX_IDENTIFIER_LENGTH:
            raise ValueError("group id must be non-empty and bounded")
        return value


class PipelineAHTSummary(_PipelineAHTValue):
    sample_count: StrictInt = Field(ge=1)
    cutoff_before: date

    @field_validator("cutoff_before", mode="before")
    @classmethod
    def cutoff_is_iso(cls, value: object) -> date:
        return _parse_date(value)


class PipelineAHTEstimate(_PipelineAHTValue):
    source: Literal["estimate"]


class PipelineRequest(_StrictModel):
    mode: Literal["history", "planned_calls"]
    week_start: date
    groups: list[PipelineGroup] = Field(min_length=1, max_length=MAX_GROUPS)
    operating_intervals: list[OperatingInterval] = Field(
        min_length=1, max_length=MAX_OPERATING_SLOTS
    )
    employees: list[EmployeeV2] = Field(max_length=MAX_EMPLOYEES)
    shifts: list[ShiftOption] = Field(max_length=MAX_SHIFT_CANDIDATES)
    service_targets: list[PipelineServiceTarget] = Field(
        min_length=1, max_length=MAX_GROUPS
    )
    aht_by_group: list[PipelineAHTSummary | PipelineAHTEstimate] = Field(max_length=MAX_GROUPS)
    allow_skill_fallback: StrictBool = False
    allow_understaffing: StrictBool = False
    max_solve_seconds: StrictInt = Field(default=10, ge=1, le=MAX_SOLVE_SECONDS)
    max_agents: StrictInt = Field(
        default=MAX_ERLANG_C_AGENTS,
        ge=1,
        le=MAX_ERLANG_C_AGENTS,
    )
    history_window: HistoryWindow | None = None
    history: list[CallIntervalObservation] | None = Field(
        default=None, max_length=MAX_HISTORY_ROWS
    )
    planned_calls: list[PlannedCallInterval] | None = Field(
        default=None, max_length=MAX_INTERVAL_ROWS
    )

    @field_validator("week_start", mode="before")
    @classmethod
    def week_start_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @model_validator(mode="after")
    def pipeline_inputs_are_consistent(self) -> "PipelineRequest":
        if self.week_start.weekday() != 0:
            raise ValueError("week_start must be a Monday")
        try:
            week_dates = {
                self.week_start + timedelta(days=offset)
                for offset in range(MAX_WEEK_DAYS)
            }
        except OverflowError as error:
            raise ValueError("declared week exceeds the supported date range") from error

        group_ids = [group.id for group in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("group ids must be unique")
        group_set = set(group_ids)

        service_group_ids = [item.group_id for item in self.service_targets]
        if len(service_group_ids) != len(set(service_group_ids)):
            raise ValueError("service targets must be unique by group")
        if set(service_group_ids) != group_set:
            raise ValueError("every declared group needs exactly one service target")

        aht_group_ids = [item.group_id for item in self.aht_by_group]
        if len(aht_group_ids) != len(set(aht_group_ids)):
            raise ValueError("AHT summaries must be unique by group")
        if not set(aht_group_ids) <= group_set:
            raise ValueError("AHT summaries must use declared groups")
        if any(
            isinstance(item, PipelineAHTSummary) and item.cutoff_before > self.week_start
            for item in self.aht_by_group
        ):
            raise ValueError("AHT cutoff_before must not be after week_start")

        operating_intervals = sorted(
            self.operating_intervals,
            key=lambda item: (item.date, item.start, item.end),
        )
        previous_end: dict[date, int] = {}
        operating_slots = []
        for interval in operating_intervals:
            if interval.date not in week_dates:
                raise ValueError("operating intervals must be within the declared week")
            start = _time_minutes(interval.start)
            end = _time_minutes(interval.end)
            if start < previous_end.get(interval.date, -1):
                raise ValueError("operating intervals must not overlap")
            previous_end[interval.date] = end
            for minute in range(start, end, 30):
                operating_slots.append(
                    OperatingSlot(
                        date=interval.date,
                        start="24:00" if minute == 1440 else f"{minute // 60:02d}:{minute % 60:02d}",
                    )
                )
                if len(operating_slots) > MAX_OPERATING_SLOTS:
                    raise ValueError(
                        f"operating slots exceed {MAX_OPERATING_SLOTS}"
                    )

        validate_solver_configuration(
            self.week_start,
            group_ids,
            operating_slots,
            self.employees,
            self.shifts,
        )

        if self.mode == "history":
            if not self.history or self.history_window is None:
                raise ValueError("history mode requires history and history_window")
            if self.planned_calls is not None:
                raise ValueError("history mode must not include planned_calls")
            if self.history_window.end_date > self.week_start:
                raise ValueError("history_window must end no later than week_start")
            if any(item.group_id not in group_set for item in self.history):
                raise ValueError("history must use declared groups")
            validate_history_keys_unique(self.history)
            if any(
                not self.history_window.start_date
                <= item.date
                < self.history_window.end_date
                or item.date >= self.week_start
                for item in self.history
            ):
                raise ValueError(
                    "history rows must be inside history_window and before week_start"
                )
            return self

        if self.history is not None or self.history_window is not None:
            raise ValueError("planned_calls mode must not include history fields")
        if not self.planned_calls:
            raise ValueError("planned_calls mode requires non-empty planned_calls")

        expected = {
            (slot.date, slot.start, group) for slot in operating_slots for group in group_ids
        }
        actual = [
            (row.date, row.start, row.group_id) for row in self.planned_calls
        ]
        if len(actual) != len(set(actual)):
            raise ValueError("planned_calls date/start/group rows must be unique")
        if any(group not in group_set for _, _, group in actual):
            raise ValueError("planned_calls must use declared groups")
        if set(actual) != expected:
            raise ValueError(
                "planned_calls must contain exactly one explicit row for every "
                "operating slot/group, including zero calls"
            )
        return self


def _time_minutes(value: str) -> int:
    if value == "24:00":
        return 24 * 60
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


class PipelineIntervalResult(_StrictModel):
    date: date
    start: StrictStr
    group_id: StrictStr
    calls: StrictFloat = Field(ge=0, allow_inf_nan=False)
    sample_count: StrictInt | None
    erlang: ErlangCResponse
    assigned_staff: StrictInt | None


class PipelineResponse(_StrictModel):
    mode: Literal["history", "planned_calls"]
    forecast: ForecastResponse | None
    groups: list[PipelineGroup]
    interval_results: list[PipelineIntervalResult]
    schedule: SolverV2Response


def _operating_slots(request: PipelineRequest) -> list[OperatingSlot]:
    slots = []
    for interval in request.operating_intervals:
        start = _time_minutes(interval.start)
        end = _time_minutes(interval.end)
        for minute in range(start, end, 30):
            slots.append(
                OperatingSlot(
                    date=interval.date,
                    start="24:00" if minute == 1440 else f"{minute // 60:02d}:{minute % 60:02d}",
                )
            )
    return sorted(slots, key=lambda item: (item.date, item.start))


def _staffed_by_slot(
    schedule: SolverV2Response,
) -> dict[tuple[date, str, str], int] | None:
    if schedule.status not in ("OPTIMAL", "FEASIBLE"):
        return None
    staffed: dict[tuple[date, str, str], int] = defaultdict(int)
    for assignment in schedule.assignments:
        for item in assignment.groups_by_slot:
            staffed[(assignment.date, item.start, item.group_id)] += 1
    return staffed


def run_pipeline(request: PipelineRequest) -> PipelineResponse:
    """Run validated interval demand through Erlang C and solver v2."""
    groups_by_id = {group.id: group for group in request.groups}
    service_by_group = {item.group_id: item for item in request.service_targets}
    aht_by_group = {item.group_id: item for item in request.aht_by_group}
    operating_slots = _operating_slots(request)

    forecast = None
    if request.mode == "history":
        targets = [
            ForecastTarget(date=slot.date, start=slot.start, group_id=group_id)
            for slot in operating_slots
            for group_id in groups_by_id
        ]
        observed_dates: dict[tuple[int, str, str], set[date]] = defaultdict(set)
        for point in request.history:
            observed_dates[
                (point.date.weekday(), point.start, point.group_id)
            ].add(point.date)
        if any(
            len(observed_dates[(target.date.weekday(), target.start, target.group_id)])
            < 2
            for target in targets
        ):
            raise PipelineDomainError(
                "history lacks two distinct prior dates for a forecast target",
                public_message=(
                    "History has insufficient prior samples for one or more targets."
                ),
            )
        forecast_request = ForecastRequest(
            history_window=request.history_window,
            history=request.history,
            targets=targets,
        )
        forecast = forecast_calls(forecast_request)
        demand_rows = [
            (item.date, item.start, item.group_id, item.forecast_calls, item.sample_count)
            for item in forecast.forecasts
        ]
    else:
        demand_rows = [
            (item.date, item.start, item.group_id, float(item.calls), None)
            for item in sorted(
                request.planned_calls,
                key=lambda row: (row.date, row.start, row.group_id),
            )
        ]

    interval_results = []
    requirements = []
    for day, start, group_id, calls, sample_count in demand_rows:
        aht_summary = aht_by_group.get(group_id)
        if calls > 0 and aht_summary is None:
            raise PipelineDomainError(
                f"AHT summary missing for positive-call group {group_id!r}",
                public_message=(
                    "AHT seconds are required for groups with positive calls."
                ),
            )
        service = service_by_group[group_id]
        aht_seconds = aht_summary.aht_seconds if aht_summary is not None else 0.0
        try:
            erlang = calculate_required_staff(
                ErlangCRequest(
                    calls_per_hour=2 * calls,
                    aht_seconds=aht_seconds,
                    target_sla=service.target_sla,
                    waiting_seconds=service.waiting_seconds,
                    max_agents=request.max_agents,
                )
            )
        except ValueError as error:
            raise PipelineDomainError(
                f"Erlang C failed for {day.isoformat()} {start} group "
                f"{group_id!r}: {error}",
                public_message=(
                    "Erlang C could not meet the staffing target within "
                    f"max_agents={request.max_agents}."
                ),
            ) from error
        if erlang.required_staff > MAX_EMPLOYEES:
            raise PipelineDomainError(
                f"Erlang C requires {erlang.required_staff} agents for "
                f"{day.isoformat()} {start} group {group_id!r}, exceeding "
                f"the solver limit of {MAX_EMPLOYEES}",
                public_message=(
                    "Erlang C staffing exceeds the solver v2 limit of "
                    f"{MAX_EMPLOYEES} employees per group/slot."
                ),
            )
        interval_results.append(
            PipelineIntervalResult(
                date=day,
                start=start,
                group_id=group_id,
                calls=float(calls),
                sample_count=sample_count,
                erlang=erlang,
                assigned_staff=None,
            )
        )
        requirements.append(
            SlotRequirement(
                date=day,
                start=start,
                group_id=group_id,
                required_staff=erlang.required_staff,
            )
        )

    solver_request = SolverV2Request.model_validate(
        {
            "week_start": request.week_start,
            "groups": list(groups_by_id),
            "operating_slots": operating_slots,
            "requirements": requirements,
            "employees": request.employees,
            "shifts": request.shifts,
            "allow_skill_fallback": request.allow_skill_fallback,
            "allow_understaffing": request.allow_understaffing,
            "max_solve_seconds": request.max_solve_seconds,
        }
    )
    try:
        schedule = solve_schedule_v2(solver_request)
    except SolverModelSizeError as error:
        raise PipelineDomainError(
            str(error),
            public_message="The schedule exceeds the prototype assignment-variable limit.",
        ) from None

    staffed = _staffed_by_slot(schedule)
    if staffed is not None:
        for result in interval_results:
            result.assigned_staff = staffed.get(
                (result.date, result.start, result.group_id), 0
            )

    return PipelineResponse(
        mode=request.mode,
        forecast=forecast,
        groups=list(request.groups),
        interval_results=interval_results,
        schedule=schedule,
    )
