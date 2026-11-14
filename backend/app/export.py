"""Spreadsheet-safe CSV rendering for existing pipeline results."""

import csv
import io
from datetime import date, datetime

from .pipeline import PipelineResponse
from .solver_v2 import (
    MAX_EMPLOYEES,
    MAX_IDENTIFIER_LENGTH,
    MAX_SHIFT_STARTS,
    ShiftAssignment,
    SlotGroupAssignment,
    SolverV2Response,
)


HEADERS = (
    "Mã nhân viên",
    "Ngày",
    "Bắt đầu ca",
    "Kết thúc ca",
    "Bắt đầu khoảng",
    "Kết thúc khoảng",
    "Nhóm phục vụ",
)
_FORMULA_MARKERS = ("=", "+", "-", "@")


def _minutes(value: str) -> int:
    if (
        not isinstance(value, str)
        or len(value) != 5
        or value[2] != ":"
        or any(char < "0" or char > "9" for char in value[:2] + value[3:])
    ):
        raise ValueError("malformed schedule time")
    hour, minute = int(value[:2]), int(value[3:])
    if hour > 23 or minute > 59:
        raise ValueError("malformed schedule time")
    return hour * 60 + minute


def _time_text(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _excel_safe(value: str) -> str:
    index = 0
    while index < len(value) and (
        value[index].isspace()
        or ord(value[index]) < 32
        or 127 <= ord(value[index]) < 160
    ):
        index += 1
    has_leading_control = any(
        ord(char) < 32 or 127 <= ord(char) < 160 for char in value[:index]
    )
    if has_leading_control or value[index : index + 1] in _FORMULA_MARKERS:
        return "'" + value
    return value


def _validate_assignment(assignment: ShiftAssignment) -> int:
    if not isinstance(assignment, ShiftAssignment):
        raise ValueError("malformed schedule assignment")
    if (
        not isinstance(assignment.employee_id, str)
        or not assignment.employee_id.strip()
        or len(assignment.employee_id) > MAX_IDENTIFIER_LENGTH
        or isinstance(assignment.date, datetime)
        or not isinstance(assignment.date, date)
        or not isinstance(assignment.groups_by_slot, list)
        or len(assignment.groups_by_slot) != 3
    ):
        raise ValueError("malformed schedule assignment")

    shift_start = _minutes(assignment.start)
    if shift_start % 30 or shift_start > 22 * 60:
        raise ValueError("malformed schedule assignment")
    if _minutes(assignment.end) != shift_start + 90:
        raise ValueError("malformed schedule assignment")
    for offset, slot in enumerate(assignment.groups_by_slot):
        if not isinstance(slot, SlotGroupAssignment):
            raise ValueError("malformed schedule assignment")
        if (
            _minutes(slot.start) != shift_start + 30 * offset
            or not isinstance(slot.group_id, str)
            or not slot.group_id.strip()
            or len(slot.group_id) > MAX_IDENTIFIER_LENGTH
        ):
            raise ValueError("malformed schedule assignment")
    return shift_start


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
    if len(result.schedule.assignments) > MAX_EMPLOYEES * MAX_SHIFT_STARTS:
        raise ValueError("schedule assignments exceed solver v2 limits")

    rows = []
    for assignment in result.schedule.assignments:
        shift_start = _validate_assignment(assignment)
        for slot in assignment.groups_by_slot:
            slot_start = _minutes(slot.start)
            rows.append(
                (
                    assignment.date.isoformat(),
                    slot_start,
                    assignment.employee_id,
                    shift_start,
                    slot.group_id,
                )
            )
    rows.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]))

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(HEADERS)
    for day, slot_start, employee_id, shift_start, group_id in rows:
        writer.writerow(
            (
                _excel_safe(employee_id),
                day,
                _time_text(shift_start),
                _time_text(shift_start + 90),
                _time_text(slot_start),
                _time_text(slot_start + 30),
                _excel_safe(group_id),
            )
        )
    return output.getvalue().encode("utf-8-sig")
