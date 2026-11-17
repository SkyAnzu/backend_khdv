"""Spreadsheet-safe CSV rendering for interval pipeline results."""

import csv
import io
import unicodedata
from datetime import date, datetime

from pydantic import ValidationError

from .pipeline import PipelineResponse
from .solver_v2 import (
    MAX_EMPLOYEES,
    MAX_IDENTIFIER_LENGTH,
    MAX_SHIFT_CANDIDATES,
    ShiftAssignment,
    SolverV2Response,
)


HEADERS = (
    "Mã nhân viên",
    "Ngày",
    "Bắt đầu khoảng",
    "Kết thúc khoảng",
    "Mã nhóm",
    "Tên nhóm",
    "Bắt đầu ca",
    "Kết thúc ca",
    "Nghỉ trong ca",
    "Thời lượng ca (phút)",
)
_FORMULA_MARKERS = ("=", "+", "-", "@")


def _minutes(value: str) -> int:
    if value == "24:00":
        return 24 * 60
    if (
        not isinstance(value, str)
        or len(value) != 5
        or value[2] != ":"
        or any(char < "0" or char > "9" for char in value[:2] + value[3:])
    ):
        raise ValueError("malformed schedule time")
    hour, minute = int(value[:2]), int(value[3:])
    if hour > 23 or minute not in (0, 30):
        raise ValueError("malformed schedule time")
    return hour * 60 + minute


def _time_text(minutes: int) -> str:
    if not 0 <= minutes <= 24 * 60:
        raise ValueError("malformed schedule time")
    return "24:00" if minutes == 24 * 60 else f"{minutes // 60:02d}:{minutes % 60:02d}"


def _excel_safe(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("CSV text must contain valid Unicode") from None

    index = 0
    while index < len(value):
        normalized = unicodedata.normalize("NFKC", value[index])
        if not (
            normalized.isspace()
            or ord(value[index]) < 32
            or 127 <= ord(value[index]) < 160
        ):
            break
        index += 1
    has_leading_control = any(
        ord(char) < 32 or 127 <= ord(char) < 160 for char in value[:index]
    )
    marker = unicodedata.normalize("NFKC", value[index : index + 1])
    if has_leading_control or marker in _FORMULA_MARKERS:
        return "'" + value
    return value


def _validate_assignment(assignment: ShiftAssignment) -> tuple[int, int]:
    if not isinstance(assignment, ShiftAssignment):
        raise ValueError("malformed schedule assignment")
    try:
        assignment = ShiftAssignment.model_validate(assignment.model_dump())
    except ValidationError:
        raise ValueError("malformed schedule assignment") from None
    if (
        not isinstance(assignment.employee_id, str)
        or not assignment.employee_id.strip()
        or len(assignment.employee_id) > MAX_IDENTIFIER_LENGTH
        or isinstance(assignment.date, datetime)
        or not isinstance(assignment.date, date)
    ):
        raise ValueError("malformed schedule assignment")
    start = _minutes(assignment.start)
    end = _minutes(assignment.end)
    if start >= end or end - start > 24 * 60:
        raise ValueError("malformed schedule assignment")
    for item in assignment.breaks:
        if _minutes(item.start) < start or _minutes(item.end) > end:
            raise ValueError("malformed schedule assignment")
    return start, end - start


def pipeline_to_csv(result: PipelineResponse) -> bytes:
    """Render an existing PipelineResponse without rerunning its pipeline."""
    if not isinstance(result, PipelineResponse) or not isinstance(
        result.schedule, SolverV2Response
    ):
        raise ValueError("CSV export requires a PipelineResponse")
    if result.schedule.status not in ("OPTIMAL", "FEASIBLE"):
        raise ValueError("CSV export requires an OPTIMAL or FEASIBLE schedule")
    if not isinstance(result.schedule.assignments, list):
        raise ValueError("malformed schedule assignments")
    if len(result.schedule.assignments) > MAX_EMPLOYEES * MAX_SHIFT_CANDIDATES:
        raise ValueError("schedule assignments exceed solver v2 limits")

    group_names = {group.id: group.name for group in result.groups}
    rows = []
    for assignment in result.schedule.assignments:
        shift_start, shift_minutes = _validate_assignment(assignment)
        break_text = ";".join(
            f"{item.start}-{item.end}" for item in assignment.breaks
        )
        for slot in assignment.groups_by_slot:
            if slot.group_id not in group_names:
                raise ValueError("schedule assignment references an unknown group")
            slot_start = _minutes(slot.start)
            rows.append(
                (
                    assignment.date.isoformat(),
                    slot_start,
                    assignment.employee_id,
                    group_names[slot.group_id],
                    slot.group_id,
                    shift_start,
                    assignment.end,
                    break_text,
                    shift_minutes,
                )
            )
    rows.sort(key=lambda row: (row[0], row[1], row[2], row[5], row[4]))

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(HEADERS)
    for (
        day,
        slot_start,
        employee_id,
        group_name,
        group_id,
        shift_start,
        shift_end,
        break_text,
        shift_minutes,
    ) in rows:
        writer.writerow(
            (
                _excel_safe(employee_id),
                day,
                _time_text(slot_start),
                _time_text(slot_start + 30),
                _excel_safe(group_id),
                _excel_safe(group_name),
                _time_text(shift_start),
                shift_end,
                break_text,
                shift_minutes,
            )
        )
    return output.getvalue().encode("utf-8-sig")
