"""Validated input/output models for the week 1-2 scheduling prototype."""

from datetime import date, datetime
import re

from pydantic import (
    BaseModel,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)


TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_ERLANG_C_AGENTS = 10_000


def validate_time(value: str) -> str:
    if not TIME_PATTERN.fullmatch(value):
        raise ValueError("time must use HH:MM in the same day")
    return value


def validate_non_empty(value: str, label: str) -> str:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")
    return value


class Employee(BaseModel):
    id: str
    name: str

    @field_validator("id")
    @classmethod
    def id_is_non_empty(cls, value: str) -> str:
        return validate_non_empty(value, "employee id")

    @field_validator("name")
    @classmethod
    def name_is_non_empty(cls, value: str) -> str:
        return validate_non_empty(value, "employee name")


class ShiftTemplate(BaseModel):
    id: str
    name: str
    start: str
    end: str

    @field_validator("id")
    @classmethod
    def id_is_non_empty(cls, value: str) -> str:
        return validate_non_empty(value, "shift id")

    @field_validator("name")
    @classmethod
    def name_is_non_empty(cls, value: str) -> str:
        return validate_non_empty(value, "shift name")

    @field_validator("start", "end")
    @classmethod
    def time_is_valid(cls, value: str) -> str:
        return validate_time(value)

    @model_validator(mode="after")
    def end_is_after_start(self) -> "ShiftTemplate":
        if self.start >= self.end:
            raise ValueError("shift end must be after shift start")
        return self


class StaffingRequirement(BaseModel):
    date: date
    start: str
    end: str
    required_staff: int = Field(ge=0)

    @field_validator("date", mode="before")
    @classmethod
    def date_is_iso(cls, value: object) -> object:
        if isinstance(value, date):
            return value
        if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
            raise ValueError("date must use ISO format YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("date must be a valid calendar date") from error
        return value

    @field_validator("start", "end")
    @classmethod
    def time_is_valid(cls, value: str) -> str:
        return validate_time(value)

    @model_validator(mode="after")
    def interval_is_valid(self) -> "StaffingRequirement":
        if self.start >= self.end:
            raise ValueError("requirement end must be after requirement start")
        return self


class ScheduleRequest(BaseModel):
    employees: list[Employee] = Field(min_length=1)
    shift_templates: list[ShiftTemplate] = Field(min_length=1)
    requirements: list[StaffingRequirement] = Field(min_length=1)

    @model_validator(mode="after")
    def ids_are_unique(self) -> "ScheduleRequest":
        employee_ids = [employee.id for employee in self.employees]
        shift_ids = [shift.id for shift in self.shift_templates]
        if len(employee_ids) != len(set(employee_ids)):
            raise ValueError("employee ids must be unique")
        if len(shift_ids) != len(set(shift_ids)):
            raise ValueError("shift ids must be unique")
        return self


class _HourlyCallKey(BaseModel):
    date: date
    hour: StrictInt = Field(ge=0, le=23)
    group_id: str

    @field_validator("date", mode="before")
    @classmethod
    def date_is_iso(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("date must use ISO format YYYY-MM-DD")
        if isinstance(value, date):
            return value
        if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
            raise ValueError("date must use ISO format YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("date must be a valid calendar date") from error
        return value

    @field_validator("group_id")
    @classmethod
    def group_id_is_non_empty(cls, value: str) -> str:
        return validate_non_empty(value.strip(), "group id")


class HourlyCallObservation(_HourlyCallKey):
    calls: StrictInt = Field(ge=0)

    @field_validator("calls")
    @classmethod
    def calls_fit_finite_forecast(cls, value: int) -> int:
        try:
            float(value)
        except OverflowError as error:
            raise ValueError("calls is too large for a finite forecast") from error
        return value


class ForecastTarget(_HourlyCallKey):
    pass


class ForecastRequest(BaseModel):
    history: list[HourlyCallObservation] = Field(min_length=1)
    targets: list[ForecastTarget] = Field(min_length=1)

    @model_validator(mode="after")
    def forecast_inputs_are_valid(self) -> "ForecastRequest":
        history_keys = [
            (point.date, point.hour, point.group_id) for point in self.history
        ]
        if len(history_keys) != len(set(history_keys)):
            raise ValueError("history date/hour/group keys must be unique")

        target_keys = [
            (point.date, point.hour, point.group_id) for point in self.targets
        ]
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("forecast target keys must be unique")

        first_target_date = min(point.date for point in self.targets)
        if any(point.date >= first_target_date for point in self.history):
            raise ValueError("history dates must be before the forecast horizon")

        sample_counts: dict[tuple[int, int, str], int] = {}
        for point in self.history:
            key = (point.date.weekday(), point.hour, point.group_id)
            sample_counts[key] = sample_counts.get(key, 0) + 1

        if any(
            sample_counts.get(
                (point.date.weekday(), point.hour, point.group_id), 0
            )
            < 2
            for point in self.targets
        ):
            raise ValueError(
                "each target needs at least two prior same weekday/hour/group samples"
            )
        return self


class HourlyForecast(_HourlyCallKey):
    forecast_calls: float = Field(ge=0, allow_inf_nan=False)
    sample_count: StrictInt = Field(ge=2)


class ForecastResponse(BaseModel):
    forecasts: list[HourlyForecast] = Field(min_length=1)


class ErlangCRequest(BaseModel):
    calls_per_hour: StrictFloat = Field(ge=0, allow_inf_nan=False)
    aht_seconds: StrictFloat = Field(ge=0, allow_inf_nan=False)
    target_sla: StrictFloat = Field(gt=0, lt=1, allow_inf_nan=False)
    waiting_seconds: StrictFloat = Field(ge=0, allow_inf_nan=False)
    max_agents: StrictInt = Field(
        default=MAX_ERLANG_C_AGENTS,
        ge=1,
        le=MAX_ERLANG_C_AGENTS,
    )

    @model_validator(mode="after")
    def aht_is_positive_for_nonzero_calls(self) -> "ErlangCRequest":
        if self.calls_per_hour > 0 and self.aht_seconds <= 0:
            raise ValueError("AHT seconds must be positive when calls are nonzero")
        return self


class ErlangCResponse(BaseModel):
    required_staff: StrictInt = Field(ge=0)
    service_level: StrictFloat | None = Field(
        ge=0,
        le=1,
        allow_inf_nan=False,
    )


class Assignment(BaseModel):
    employee_id: str
    date: date
    shift_id: str
    shift_name: str
    start: str
    end: str


class SolveResponse(BaseModel):
    status: str
    assignments: list[Assignment]
    message: str
