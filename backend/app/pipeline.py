"""Internal end-to-end call-volume, Erlang C, and weekly scheduling pipeline."""

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
    ErlangCRequest,
    ErlangCResponse,
    ForecastRequest,
    ForecastResponse,
    ForecastTarget,
    HourlyCallObservation,
    MAX_ERLANG_C_AGENTS,
)
from .solver_v2 import (
    MAX_EMPLOYEES,
    MAX_GROUPS,
    MAX_IDENTIFIER_LENGTH,
    MAX_OPERATING_SLOTS,
    MAX_SOLVE_SECONDS,
    MAX_SHIFT_STARTS,
    EmployeeV2,
    OperatingSlot,
    ShiftStart,
    SlotRequirement,
    SolverV2Request,
    SolverV2Response,
    solve_schedule_v2,
)


MAX_WEEK_DAYS = 7
MAX_HOURLY_SLOTS = min(MAX_WEEK_DAYS * 23, MAX_OPERATING_SLOTS // 2)
MAX_HISTORY_ROWS = 100_000
MAX_HOURLY_CALL_ROWS = MAX_HOURLY_SLOTS * MAX_GROUPS
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


class PipelineOperatingHour(_StrictModel):
    date: date
    hour: StrictInt

    @field_validator("date", mode="before")
    @classmethod
    def date_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("hour")
    @classmethod
    def hour_is_supported(cls, value: int) -> int:
        if value == 23:
            raise ValueError(
                "hour 23 is unsupported because solver v2 cannot represent "
                "the required 23:30 slot"
            )
        if not 0 <= value <= 22:
            raise ValueError("hour must be an integer from 0 through 22")
        return value


class PipelineHistoryObservation(HourlyCallObservation):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("date", mode="before")
    @classmethod
    def date_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("group_id")
    @classmethod
    def group_id_is_bounded(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("group id must not be empty")
        if len(value) > MAX_IDENTIFIER_LENGTH:
            raise ValueError(
                f"group id must be at most {MAX_IDENTIFIER_LENGTH} characters"
            )
        return value


class PipelinePlannedCalls(PipelineOperatingHour):
    group_id: StrictStr
    calls_per_hour: StrictFloat = Field(ge=0, allow_inf_nan=False)

    @field_validator("group_id")
    @classmethod
    def group_id_is_normalized(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("group id must not be empty")
        if len(value) > MAX_IDENTIFIER_LENGTH:
            raise ValueError(
                f"group id must be at most {MAX_IDENTIFIER_LENGTH} characters"
            )
        return value


class PipelineRequest(_StrictModel):
    mode: Literal["history", "planned_calls"]
    week_start: date
    groups: list[StrictStr] = Field(min_length=1, max_length=MAX_GROUPS)
    operating_hours: list[PipelineOperatingHour] = Field(
        min_length=1, max_length=MAX_HOURLY_SLOTS
    )
    employees: list[EmployeeV2] = Field(max_length=MAX_EMPLOYEES)
    shift_starts: list[ShiftStart] = Field(max_length=MAX_SHIFT_STARTS)
    allow_skill_fallback: StrictBool = False
    max_solve_seconds: StrictInt = Field(default=10, ge=1, le=MAX_SOLVE_SECONDS)
    aht_seconds: StrictFloat = Field(ge=0, allow_inf_nan=False)
    target_sla: StrictFloat = Field(gt=0, lt=1, allow_inf_nan=False)
    waiting_seconds: StrictFloat = Field(ge=0, allow_inf_nan=False)
    max_agents: StrictInt = Field(
        default=MAX_ERLANG_C_AGENTS,
        ge=1,
        le=MAX_ERLANG_C_AGENTS,
    )
    history: list[PipelineHistoryObservation] | None = Field(
        default=None, max_length=MAX_HISTORY_ROWS
    )
    planned_calls: list[PipelinePlannedCalls] | None = Field(
        default=None, max_length=MAX_HOURLY_CALL_ROWS
    )

    @field_validator("week_start", mode="before")
    @classmethod
    def week_start_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("groups")
    @classmethod
    def groups_are_normalized(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            value = value.strip()
            if not value:
                raise ValueError("group id must not be empty")
            if len(value) > MAX_IDENTIFIER_LENGTH:
                raise ValueError(
                    f"group id must be at most {MAX_IDENTIFIER_LENGTH} characters"
                )
            normalized.append(value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("group ids must be unique")
        return normalized

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

        hour_keys = [(item.date, item.hour) for item in self.operating_hours]
        if len(hour_keys) != len(set(hour_keys)):
            raise ValueError("operating hours must be unique")
        if any(day not in week_dates for day, _ in hour_keys):
            raise ValueError("operating hours must be within the declared week")

        hours_by_day: dict[date, list[int]] = {}
        for day, hour in hour_keys:
            hours_by_day.setdefault(day, []).append(hour)
        for hours in hours_by_day.values():
            ordered_hours = sorted(hours)
            if ordered_hours != list(range(ordered_hours[0], ordered_hours[-1] + 1)):
                raise ValueError("operating hours must be contiguous within each date")

        if self.mode == "history":
            if not self.history:
                raise ValueError("history mode requires non-empty history")
            if self.planned_calls is not None:
                raise ValueError("history mode must not include planned_calls")
            if any(point.date >= self.week_start for point in self.history):
                raise ValueError(
                    "history dates must be before week_start to prevent forecast leakage"
                )
            return self

        if self.history is not None:
            raise ValueError("planned_calls mode must not include history")
        if not self.planned_calls:
            raise ValueError("planned_calls mode requires non-empty planned_calls")

        expected = {
            (item.date, item.hour, group)
            for item in self.operating_hours
            for group in self.groups
        }
        actual_keys = [
            (item.date, item.hour, item.group_id) for item in self.planned_calls
        ]
        if len(actual_keys) != len(set(actual_keys)):
            raise ValueError("planned_calls date/hour/group rows must be unique")
        if any(day not in week_dates for day, _, _ in actual_keys):
            raise ValueError("planned_calls must be within the declared week")
        if any(group not in self.groups for _, _, group in actual_keys):
            raise ValueError("planned_calls must use declared groups")
        if set(actual_keys) != expected:
            missing = len(expected - set(actual_keys))
            extra = len(set(actual_keys) - expected)
            raise ValueError(
                "planned_calls must contain exactly one row for every "
                f"operating hour/group (missing={missing}, extra={extra}); "
                "zero calls must be explicit"
            )
        return self


class PipelineHourlyResult(_StrictModel):
    date: date
    hour: StrictInt
    group_id: StrictStr
    calls_per_hour: StrictFloat
    sample_count: StrictInt | None
    erlang: ErlangCResponse


class PipelineResponse(_StrictModel):
    mode: Literal["history", "planned_calls"]
    forecast: ForecastResponse | None
    hourly_results: list[PipelineHourlyResult]
    requirements: list[SlotRequirement]
    schedule: SolverV2Response


def run_pipeline(request: PipelineRequest) -> PipelineResponse:
    """Run the selected hourly demand source through Erlang C and solver v2."""
    forecast = None
    if request.mode == "history":
        targets = [
            ForecastTarget(date=item.date, hour=item.hour, group_id=group)
            for item in request.operating_hours
            for group in request.groups
        ]
        forecast = forecast_calls(
            ForecastRequest(history=request.history, targets=targets)
        )
        hourly_calls = [
            (
                item.date,
                item.hour,
                item.group_id,
                item.forecast_calls,
                item.sample_count,
            )
            for item in forecast.forecasts
        ]
    else:
        hourly_calls = [
            (
                item.date,
                item.hour,
                item.group_id,
                float(item.calls_per_hour),
                None,
            )
            for item in sorted(
                request.planned_calls,
                key=lambda row: (row.date, row.hour, row.group_id),
            )
        ]

    hourly_results = []
    for day, hour, group_id, calls, sample_count in hourly_calls:
        try:
            erlang = calculate_required_staff(
                ErlangCRequest(
                    calls_per_hour=calls,
                    aht_seconds=request.aht_seconds,
                    target_sla=request.target_sla,
                    waiting_seconds=request.waiting_seconds,
                    max_agents=request.max_agents,
                )
            )
        except ValueError as error:
            raise ValueError(
                f"Erlang C failed for {day.isoformat()} {hour:02d}:00 "
                f"group {group_id!r}: {error}"
            ) from error
        if erlang.required_staff > MAX_EMPLOYEES:
            raise ValueError(
                f"Erlang C requires {erlang.required_staff} agents for "
                f"{day.isoformat()} {hour:02d}:00 group {group_id!r}, "
                f"exceeding solver v2's per-slot limit of {MAX_EMPLOYEES}"
            )
        hourly_results.append(
            PipelineHourlyResult(
                date=day,
                hour=hour,
                group_id=group_id,
                calls_per_hour=calls,
                sample_count=sample_count,
                erlang=erlang,
            )
        )

    operating_slots = [
        OperatingSlot(date=item.date, start=start)
        for item in sorted(request.operating_hours, key=lambda row: (row.date, row.hour))
        for start in (f"{item.hour:02d}:00", f"{item.hour:02d}:30")
    ]
    requirements = [
        SlotRequirement(
            date=result.date,
            start=start,
            group_id=result.group_id,
            required_staff=result.erlang.required_staff,
        )
        for result in hourly_results
        for start in (f"{result.hour:02d}:00", f"{result.hour:02d}:30")
    ]
    solver_request = SolverV2Request.model_validate(
        {
            "week_start": request.week_start,
            "groups": request.groups,
            "operating_slots": operating_slots,
            "requirements": requirements,
            "employees": request.employees,
            "shift_starts": request.shift_starts,
            "allow_skill_fallback": request.allow_skill_fallback,
            "max_solve_seconds": request.max_solve_seconds,
        }
    )
    schedule = solve_schedule_v2(solver_request)
    return PipelineResponse(
        mode=request.mode,
        forecast=forecast,
        hourly_results=hourly_results,
        requirements=requirements,
        schedule=schedule,
    )
