"""Validated shared input/output models for interval forecasts and Erlang C."""

from datetime import date, datetime
import re

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
START_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
MAX_ERLANG_C_AGENTS = 10_000
MAX_HISTORY_ROWS = 100_000
MAX_FORECAST_TARGETS = 3_360


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


def _validate_group_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("group id must not be empty")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HistoryWindow(_StrictModel):
    start_date: date
    end_date: date

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def dates_are_iso(cls, value: object) -> date:
        return _parse_date(value)

    @model_validator(mode="after")
    def window_is_non_empty(self) -> "HistoryWindow":
        if self.start_date >= self.end_date:
            raise ValueError("history_window must be a non-empty half-open range")
        return self


class _CallIntervalKey(_StrictModel):
    date: date
    start: StrictStr
    group_id: StrictStr

    @field_validator("date", mode="before")
    @classmethod
    def date_is_iso(cls, value: object) -> date:
        return _parse_date(value)

    @field_validator("start")
    @classmethod
    def start_is_aligned(cls, value: str) -> str:
        if not START_PATTERN.fullmatch(value):
            raise ValueError("start must use HH:MM")
        hour, minute = map(int, value.split(":"))
        if minute not in (0, 30):
            raise ValueError("start must align to the 30-minute grid")
        return value

    @field_validator("group_id")
    @classmethod
    def group_id_is_normalized(cls, value: str) -> str:
        return _validate_group_id(value)


class CallIntervalObservation(_CallIntervalKey):
    calls: StrictInt = Field(ge=0)


class PlannedCallInterval(_CallIntervalKey):
    calls: StrictFloat = Field(ge=0, allow_inf_nan=False)


class ForecastTarget(_CallIntervalKey):
    pass


def validate_history_keys_unique(history: list[CallIntervalObservation]) -> None:
    keys = [(point.date, point.start, point.group_id) for point in history]
    if len(keys) != len(set(keys)):
        raise ValueError("history date/start/group keys must be unique")


class ForecastRequest(_StrictModel):
    history_window: HistoryWindow
    history: list[CallIntervalObservation] = Field(
        min_length=1, max_length=MAX_HISTORY_ROWS
    )
    targets: list[ForecastTarget] = Field(
        min_length=1, max_length=MAX_FORECAST_TARGETS
    )

    @model_validator(mode="after")
    def forecast_inputs_are_valid(self) -> "ForecastRequest":
        validate_history_keys_unique(self.history)
        target_keys = [
            (item.date, item.start, item.group_id) for item in self.targets
        ]
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("forecast target keys must be unique")
        if any(
            not self.history_window.start_date
            <= item.date
            < self.history_window.end_date
            for item in self.history
        ):
            raise ValueError("history rows must be inside history_window")
        first_target_date = min(item.date for item in self.targets)
        if any(item.date >= first_target_date for item in self.history):
            raise ValueError("history dates must be before the forecast horizon")

        sample_dates: dict[tuple[int, str, str], set[date]] = {}
        for point in self.history:
            key = (point.date.weekday(), point.start, point.group_id)
            sample_dates.setdefault(key, set()).add(point.date)
        if any(
            len(
                sample_dates.get(
                    (point.date.weekday(), point.start, point.group_id), set()
                )
            )
            < 2
            for point in self.targets
        ):
            raise ValueError(
                "each target needs at least two distinct prior "
                "same weekday/start/group samples"
            )
        return self


class IntervalForecast(_CallIntervalKey):
    forecast_calls: StrictFloat = Field(ge=0, allow_inf_nan=False)
    sample_count: StrictInt = Field(ge=2)


class ForecastResponse(_StrictModel):
    forecasts: list[IntervalForecast] = Field(min_length=1)


def validate_aht_for_calls(calls_per_hour: float, aht_seconds: float) -> None:
    if calls_per_hour > 0 and aht_seconds <= 0:
        raise ValueError("AHT seconds must be positive when calls are nonzero")


class ErlangCRequest(_StrictModel):
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
        validate_aht_for_calls(self.calls_per_hour, self.aht_seconds)
        return self


class ErlangCResponse(_StrictModel):
    required_staff: StrictInt = Field(ge=0)
    service_level: StrictFloat | None = Field(
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
