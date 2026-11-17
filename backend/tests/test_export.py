import csv
import io
import json
from datetime import date
from pathlib import Path

import pytest

from backend.app.export import HEADERS, pipeline_to_csv
from backend.app.pipeline import (
    PipelineGroup,
    PipelineRequest,
    PipelineResponse,
    run_pipeline,
)
from backend.app.solver_v2 import (
    MAX_EMPLOYEES,
    MAX_SHIFT_CANDIDATES,
    BreakInterval,
    ShiftSlotAssignment,
)


SAMPLES = Path(__file__).parents[1] / "samples"
EXPECTED_HEADERS = [
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
]


@pytest.fixture(scope="module")
def sample_response():
    payload = json.loads(
        (SAMPLES / "pipeline-planned-calls-request.json").read_text(
            encoding="utf-8"
        )
    )
    return run_pipeline(PipelineRequest.model_validate(payload))


def csv_rows(content):
    assert content.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig"), newline="")))


def test_csv_exports_actual_working_slots_and_truthful_elapsed_minutes(sample_response):
    assert sample_response.schedule.status in {"OPTIMAL", "FEASIBLE"}

    rows = csv_rows(pipeline_to_csv(sample_response))

    assert rows[0] == EXPECTED_HEADERS == list(HEADERS)
    assert rows[1:] == [
        [
            "e01",
            "2026-11-02",
            "08:00",
            "08:30",
            "sales",
            "Sales",
            "08:00",
            "09:00",
            "",
            "60",
        ],
        [
            "e01",
            "2026-11-02",
            "08:30",
            "09:00",
            "sales",
            "Sales",
            "08:00",
            "09:00",
            "",
            "60",
        ],
    ]


def test_csv_keeps_feasible_schedule_with_unknown_second_phase(sample_response):
    schedule = sample_response.schedule.model_copy(
        update={"status": "FEASIBLE", "stage_two_status": "UNKNOWN"}
    )
    response = sample_response.model_copy(update={"schedule": schedule})
    assert len(csv_rows(pipeline_to_csv(response))) == 3


def test_csv_includes_break_envelope_but_excludes_break_coverage(sample_response):
    assignment = sample_response.schedule.assignments[0].model_copy(
        update={
            "end": "09:30",
            "breaks": [BreakInterval(start="08:30", end="09:00")],
            "groups_by_slot": [
                ShiftSlotAssignment(start="08:00", group_id="sales"),
                ShiftSlotAssignment(start="09:00", group_id="sales"),
            ],
        }
    )
    schedule = sample_response.schedule.model_copy(
        update={
            "assignments": [assignment],
            "total_shift_minutes": 90,
        }
    )
    response = sample_response.model_copy(update={"schedule": schedule})

    rows = csv_rows(pipeline_to_csv(response))

    assert rows[1][2:10] == [
        "08:00",
        "08:30",
        "sales",
        "Sales",
        "08:00",
        "09:30",
        "08:30-09:00",
        "90",
    ]
    assert rows[2][2:10] == [
        "09:00",
        "09:30",
        "sales",
        "Sales",
        "08:00",
        "09:30",
        "08:30-09:00",
        "90",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=1+1", "'=1+1"),
        ("+1+1", "'+1+1"),
        ("-1+1", "'-1+1"),
        ("@SUM(A1:A2)", "'@SUM(A1:A2)"),
        (" \t=1+1", "' \t=1+1"),
        ("\r\n=1+1", "'\r\n=1+1"),
        ("\tplain", "'\tplain"),
        ("\x01plain", "'\x01plain"),
        ("  plain", "  plain"),
    ],
)
@pytest.mark.parametrize("field", ["employee_id", "group_name"])
def test_csv_prefixes_dangerous_text_and_preserves_plain_values(
    sample_response, value, expected, field
):
    assignment = sample_response.schedule.assignments[0]
    response = sample_response
    if field == "employee_id":
        assignment = assignment.model_copy(update={"employee_id": value})
        column = 0
        schedule = sample_response.schedule.model_copy(update={"assignments": [assignment]})
        response = sample_response.model_copy(update={"schedule": schedule})
    else:
        response = sample_response.model_copy(
            update={
                "groups": [
                    sample_response.groups[0].model_copy(update={"name": value})
                ]
            }
        )
        column = 5

    assert csv_rows(pipeline_to_csv(response))[1][column] == expected


def test_csv_prefixes_formula_like_group_id(sample_response):
    assignment = sample_response.schedule.assignments[0]
    slots = [
        item.model_copy(update={"group_id": "=1+1"})
        for item in assignment.groups_by_slot
    ]
    assignment = assignment.model_copy(update={"groups_by_slot": slots})
    schedule = sample_response.schedule.model_copy(update={"assignments": [assignment]})
    response = sample_response.model_copy(
        update={
            "schedule": schedule,
            "groups": [sample_response.groups[0].model_copy(update={"id": "=1+1"})],
        }
    )
    assert csv_rows(pipeline_to_csv(response))[1][4] == "'=1+1"


@pytest.mark.parametrize(
    "value",
    ["＝1+1", "＋1+1", "－1+1", "＠SUM(A1:A2)", " ＝1+1", " 　＋1+1"],
)
@pytest.mark.parametrize("field", ["employee_id", "group_name"])
def test_csv_detects_nfkc_formula_markers_without_normalizing_text(
    sample_response, value, field
):
    if field == "employee_id":
        assignment = sample_response.schedule.assignments[0].model_copy(
            update={"employee_id": value}
        )
        schedule = sample_response.schedule.model_copy(update={"assignments": [assignment]})
        response = sample_response.model_copy(update={"schedule": schedule})
        column = 0
    else:
        response = sample_response.model_copy(
            update={
                "groups": [
                    sample_response.groups[0].model_copy(update={"name": value})
                ]
            }
        )
        column = 5

    assert csv_rows(pipeline_to_csv(response))[1][column] == "'" + value


@pytest.mark.parametrize("field", ["employee_id", "group_name"])
def test_csv_rejects_unpaired_surrogates_without_echoing_text(sample_response, field):
    if field == "employee_id":
        invalid_unicode = chr(0xD800)
        assignment = sample_response.schedule.assignments[0].model_copy(
            update={"employee_id": invalid_unicode}
        )
        schedule = sample_response.schedule.model_copy(update={"assignments": [assignment]})
        response = sample_response.model_copy(update={"schedule": schedule})
    else:
        invalid_unicode = chr(0xD800)
        group = PipelineGroup.model_construct(id="sales", name=invalid_unicode)
        response = sample_response.model_copy(update={"groups": [group]})

    with pytest.raises(ValueError, match="valid Unicode") as error:
        pipeline_to_csv(response)
    assert invalid_unicode not in str(error.value)


@pytest.mark.parametrize("status", ["INFEASIBLE", "UNKNOWN", "MODEL_INVALID"])
def test_csv_rejects_results_without_a_proven_feasible_schedule(sample_response, status):
    schedule = sample_response.schedule.model_copy(update={"status": status})
    response = sample_response.model_copy(update={"schedule": schedule})
    with pytest.raises(ValueError):
        pipeline_to_csv(response)


def test_feasible_empty_schedule_exports_header_only():
    payload = json.loads(
        (SAMPLES / "pipeline-planned-calls-request.json").read_text(
            encoding="utf-8"
        )
    )
    payload["planned_calls"] = [{**item, "calls": 0.0} for item in payload["planned_calls"]]
    payload["employees"] = []
    payload["shifts"] = []
    response = run_pipeline(PipelineRequest.model_validate(payload))
    schedule = response.schedule.model_copy(
        update={"status": "FEASIBLE", "stage_two_status": "UNKNOWN"}
    )
    response = response.model_copy(update={"schedule": schedule})
    assert csv_rows(pipeline_to_csv(response)) == [EXPECTED_HEADERS]


@pytest.mark.parametrize(
    "changes",
    [
        {"end": "09:30"},
        {"groups_by_slot": []},
        {"groups_by_slot": None},
        {
            "groups_by_slot": [
                ShiftSlotAssignment.model_construct(
                    start="08:15", group_id="sales"
                ),
                ShiftSlotAssignment.model_construct(
                    start="08:30", group_id="sales"
                ),
            ]
        },
    ],
)
def test_csv_rejects_malformed_shift_assignments(sample_response, changes):
    assignment = sample_response.schedule.assignments[0].model_copy(update=changes)
    schedule = sample_response.schedule.model_copy(update={"assignments": [assignment]})
    response = sample_response.model_copy(update={"schedule": schedule})
    with pytest.raises(ValueError):
        pipeline_to_csv(response)


def test_csv_supports_24_00_as_exclusive_end(sample_response):
    assignment = sample_response.schedule.assignments[0].model_copy(
        update={
            "start": "23:00",
            "end": "24:00",
            "groups_by_slot": [
                ShiftSlotAssignment(start="23:00", group_id="sales"),
                ShiftSlotAssignment(start="23:30", group_id="sales"),
            ],
        }
    )
    schedule = sample_response.schedule.model_copy(update={"assignments": [assignment]})
    response = sample_response.model_copy(update={"schedule": schedule})
    rows = csv_rows(pipeline_to_csv(response))
    assert rows[-1][2:4] == ["23:30", "24:00"]


def test_csv_rejects_unknown_group_references(sample_response):
    assignment = sample_response.schedule.assignments[0].model_copy(
        update={
            "groups_by_slot": [
                ShiftSlotAssignment(start="08:00", group_id="unknown"),
                sample_response.schedule.assignments[0].groups_by_slot[1],
            ]
        }
    )
    schedule = sample_response.schedule.model_copy(update={"assignments": [assignment]})
    response = sample_response.model_copy(update={"schedule": schedule})
    with pytest.raises(ValueError, match="unknown group"):
        pipeline_to_csv(response)


def test_csv_rejects_assignments_beyond_solver_input_limits(sample_response):
    assignment = sample_response.schedule.assignments[0]
    schedule = sample_response.schedule.model_copy(
        update={
            "assignments": [
                assignment
            ]
            * (MAX_EMPLOYEES * MAX_SHIFT_CANDIDATES + 1)
        }
    )
    response = sample_response.model_copy(update={"schedule": schedule})
    with pytest.raises(ValueError):
        pipeline_to_csv(response)


def test_csv_orders_rows_by_date_then_slot_and_employee(sample_response):
    original = sample_response.schedule.assignments[0]
    next_day = original.model_copy(
        update={"employee_id": "e02", "date": date(2026, 11, 3)}
    )
    schedule = sample_response.schedule.model_copy(
        update={"assignments": [next_day, original]}
    )
    response = sample_response.model_copy(update={"schedule": schedule})
    rows = csv_rows(pipeline_to_csv(response))
    assert [(row[1], row[0]) for row in rows[1::2]] == [
        ("2026-11-02", "e01"),
        ("2026-11-03", "e02"),
    ]
