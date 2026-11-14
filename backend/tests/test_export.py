import csv
import io
import json
from datetime import date
from pathlib import Path

import pytest

from backend.app.export import pipeline_to_csv
from backend.app.pipeline import PipelineRequest, PipelineResponse, run_pipeline
from backend.app.solver_v2 import (
    MAX_EMPLOYEES,
    MAX_SHIFT_STARTS,
    SlotGroupAssignment,
)


SAMPLES = Path(__file__).parents[1] / "samples"
HEADER = [
    "Mã nhân viên",
    "Ngày",
    "Bắt đầu ca",
    "Kết thúc ca",
    "Bắt đầu khoảng",
    "Kết thúc khoảng",
    "Nhóm phục vụ",
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


def test_csv_contains_one_row_per_actual_solver_group_slot(sample_response):
    assert sample_response.schedule.status in {"OPTIMAL", "FEASIBLE"}

    rows = csv_rows(pipeline_to_csv(sample_response))

    assert rows[0] == HEADER
    assert rows[1:] == [
        ["e01", "2026-11-02", "08:00", "09:30", "08:00", "08:30", "sales"],
        ["e01", "2026-11-02", "08:00", "09:30", "08:30", "09:00", "sales"],
        ["e01", "2026-11-02", "08:00", "09:30", "09:00", "09:30", "sales"],
    ]


def test_csv_allows_feasible_schedule_when_second_phase_is_unknown(sample_response):
    schedule = sample_response.schedule.model_copy(
        update={"status": "FEASIBLE", "stage_two_status": "UNKNOWN"}
    )
    response = sample_response.model_copy(update={"schedule": schedule})

    assert len(csv_rows(pipeline_to_csv(response))) == 4


def test_csv_splits_shift_across_its_actual_group_assignments():
    payload = json.loads(
        (SAMPLES / "pipeline-planned-calls-request.json").read_text(
            encoding="utf-8"
        )
    )
    payload["groups"] = ["sales", "support"]
    payload["employees"] = [
        {
            **payload["employees"][0],
            "id": employee_id,
            "skills": ["sales", "support"],
        }
        for employee_id in ("e01", "e02")
    ]
    payload["shift_starts"] = [
        {"date": "2026-11-02", "start": start} for start in ("08:00", "08:30")
    ]
    payload["planned_calls"] = [
        {
            "date": "2026-11-02",
            "hour": hour,
            "group_id": group,
            "calls_per_hour": calls,
        }
        for hour, group, calls in (
            (8, "sales", 0.5),
            (8, "support", 0.0),
            (9, "sales", 0.0),
            (9, "support", 0.5),
        )
    ]
    response = run_pipeline(PipelineRequest.model_validate(payload))
    assert response.schedule.status in {"OPTIMAL", "FEASIBLE"}

    rows = csv_rows(pipeline_to_csv(response))

    assert len(rows) - 1 == sum(
        len(assignment.groups_by_slot)
        for assignment in response.schedule.assignments
    )
    assert {"sales", "support"} <= {row[6] for row in rows[1:]}
    assert any(
        len({slot.group_id for slot in assignment.groups_by_slot}) > 1
        for assignment in response.schedule.assignments
    )


def test_csv_preserves_unicode_quotes_commas_and_newlines(sample_response):
    assignment = sample_response.schedule.assignments[0]
    group_slot = assignment.groups_by_slot[0].model_copy(
        update={"group_id": 'Nhóm, "B"\nca'}
    )
    updated_assignment = assignment.model_copy(
        update={
            "employee_id": 'Mã, "e"\n01',
            "groups_by_slot": [group_slot, *assignment.groups_by_slot[1:]],
        }
    )
    schedule = sample_response.schedule.model_copy(
        update={"assignments": [updated_assignment]}
    )
    response = sample_response.model_copy(update={"schedule": schedule})

    rows = csv_rows(pipeline_to_csv(response))

    assert len(rows) == 4
    assert rows[1][0] == 'Mã, "e"\n01'
    assert rows[1][6] == 'Nhóm, "B"\nca'


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
@pytest.mark.parametrize("field", ["employee_id", "group_id"])
def test_csv_prefixes_dangerous_untrusted_text_but_preserves_plain_values(
    sample_response, value, expected, field
):
    assignment = sample_response.schedule.assignments[0]
    if field == "employee_id":
        assignment = assignment.model_copy(update={"employee_id": value})
        column = 0
    else:
        first_slot = assignment.groups_by_slot[0].model_copy(
            update={"group_id": value}
        )
        assignment = assignment.model_copy(
            update={
                "groups_by_slot": [first_slot, *assignment.groups_by_slot[1:]]
            }
        )
        column = 6
    schedule = sample_response.schedule.model_copy(
        update={"assignments": [assignment]}
    )
    response = sample_response.model_copy(update={"schedule": schedule})

    rows = csv_rows(pipeline_to_csv(response))

    assert rows[1][column] == expected


@pytest.mark.parametrize(
    "value",
    [
        "＝1+1",
        "＋1+1",
        "－1+1",
        "＠SUM(A1:A2)",
        " ＝1+1",
        " 　＋1+1",
    ],
)
@pytest.mark.parametrize("field", ["employee_id", "group_id"])
def test_csv_detects_nfkc_formula_markers_without_normalizing_text(
    sample_response, value, field
):
    assignment = sample_response.schedule.assignments[0]
    if field == "employee_id":
        assignment = assignment.model_copy(update={"employee_id": value})
        column = 0
    else:
        first_slot = assignment.groups_by_slot[0].model_copy(
            update={"group_id": value}
        )
        assignment = assignment.model_copy(
            update={
                "groups_by_slot": [first_slot, *assignment.groups_by_slot[1:]]
            }
        )
        column = 6
    schedule = sample_response.schedule.model_copy(
        update={"assignments": [assignment]}
    )
    response = sample_response.model_copy(update={"schedule": schedule})

    rows = csv_rows(pipeline_to_csv(response))

    assert rows[1][column] == "'" + value


@pytest.mark.parametrize("field", ["employee_id", "group_id"])
def test_csv_rejects_unpaired_surrogates_without_echoing_text(sample_response, field):
    invalid_unicode = chr(0xD800)
    if field == "employee_id":
        path = ["schedule", "assignments", 0, "employee_id"]
    else:
        path = ["schedule", "assignments", 0, "groups_by_slot", 0, "group_id"]
    payload = sample_response.model_dump(mode="python")
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = invalid_unicode
    response = PipelineResponse.model_validate(payload)
    malformed = PipelineResponse.model_construct(**response.__dict__)

    for candidate in (response, malformed):
        with pytest.raises(ValueError, match="valid Unicode") as error:
            pipeline_to_csv(candidate)
        assert invalid_unicode not in str(error.value)


@pytest.mark.parametrize("status", ["INFEASIBLE", "UNKNOWN", "MODEL_INVALID"])
def test_csv_rejects_results_without_a_proven_feasible_schedule(
    sample_response, status
):
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
    payload["planned_calls"] = [
        {**item, "calls_per_hour": 0.0} for item in payload["planned_calls"]
    ]
    payload["employees"] = []
    payload["shift_starts"] = []
    response = run_pipeline(PipelineRequest.model_validate(payload))
    assert response.schedule.assignments == []
    schedule = response.schedule.model_copy(
        update={"status": "FEASIBLE", "stage_two_status": "UNKNOWN"}
    )
    response = response.model_copy(update={"schedule": schedule})
    assert response.schedule.status == "FEASIBLE"

    assert csv_rows(pipeline_to_csv(response)) == [HEADER]


@pytest.mark.parametrize(
    "changes",
    [
        {"end": "09:00"},
        {"groups_by_slot": []},
        {"groups_by_slot": None},
        {
            "groups_by_slot": [
                {"start": "08:15", "group_id": "sales"},
                {"start": "08:30", "group_id": "sales"},
                {"start": "09:00", "group_id": "sales"},
            ]
        },
    ],
)
def test_csv_rejects_malformed_shift_assignments(sample_response, changes):
    assignment = sample_response.schedule.assignments[0].model_copy(update=changes)
    schedule = sample_response.schedule.model_copy(
        update={"assignments": [assignment]}
    )
    response = sample_response.model_copy(update={"schedule": schedule})

    with pytest.raises(ValueError):
        pipeline_to_csv(response)


def test_csv_rejects_non_ascii_time_digits(sample_response):
    assignment = sample_response.schedule.assignments[0]
    first_slot = assignment.groups_by_slot[0].model_copy(update={"start": "٠٨:00"})
    assignment = assignment.model_copy(
        update={"groups_by_slot": [first_slot, *assignment.groups_by_slot[1:]]}
    )
    schedule = sample_response.schedule.model_copy(
        update={"assignments": [assignment]}
    )
    response = sample_response.model_copy(update={"schedule": schedule})

    with pytest.raises(ValueError):
        pipeline_to_csv(response)


@pytest.mark.parametrize("field", ["schedule", "assignments"])
def test_csv_rejects_malformed_response_containers(sample_response, field):
    if field == "schedule":
        response = sample_response.model_copy(update={"schedule": None})
    else:
        schedule = sample_response.schedule.model_copy(update={"assignments": None})
        response = sample_response.model_copy(update={"schedule": schedule})

    with pytest.raises(ValueError):
        pipeline_to_csv(response)


def test_csv_rejects_assignments_beyond_solver_input_limits(sample_response):
    assignment = sample_response.schedule.assignments[0]
    schedule = sample_response.schedule.model_copy(
        update={
            "assignments": [
                assignment
            ]
            * (MAX_EMPLOYEES * MAX_SHIFT_STARTS + 1)
        }
    )
    response = sample_response.model_copy(update={"schedule": schedule})

    with pytest.raises(ValueError):
        pipeline_to_csv(response)


def test_csv_orders_assignments_by_date_then_employee(sample_response):
    original = sample_response.schedule.assignments[0]
    next_day = original.model_copy(
        update={"employee_id": "e02", "date": date(2026, 11, 3)}
    )
    schedule = sample_response.schedule.model_copy(
        update={"assignments": [next_day, original]}
    )
    response = sample_response.model_copy(update={"schedule": schedule})

    rows = csv_rows(pipeline_to_csv(response))

    assert [(row[1], row[0]) for row in rows[1::3]] == [
        ("2026-11-02", "e01"),
        ("2026-11-03", "e02"),
    ]


def test_csv_does_not_extend_last_slot_past_the_same_day(sample_response):
    assignment = sample_response.schedule.assignments[0].model_copy(
        update={
            "start": "22:00",
            "end": "23:30",
            "groups_by_slot": [
                SlotGroupAssignment(start="22:00", group_id="sales"),
                SlotGroupAssignment(start="22:30", group_id="sales"),
                SlotGroupAssignment(start="23:00", group_id="sales"),
            ],
        }
    )
    schedule = sample_response.schedule.model_copy(
        update={"assignments": [assignment]}
    )
    response = sample_response.model_copy(update={"schedule": schedule})

    rows = csv_rows(pipeline_to_csv(response))

    assert rows[-1][4:6] == ["23:00", "23:30"]
