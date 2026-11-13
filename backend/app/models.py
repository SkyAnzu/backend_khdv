"""Validated input/output models for the week 1-2 scheduling prototype."""

from datetime import date
import re

from pydantic import BaseModel, Field, field_validator, model_validator


TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
