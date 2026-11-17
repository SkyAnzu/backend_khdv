import csv
from datetime import date
import io
import json
from pathlib import Path
import zipfile

from fastapi.testclient import TestClient

from backend.app.main import app


client = TestClient(app)
FIXTURES = Path(__file__).parent / "data" / "database_sample"
SAMPLES = Path(__file__).parent.parent / "samples"


def database_bundle():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("calls.csv", (FIXTURES / "calls.csv").read_bytes())
        archive.writestr("queues.csv", (FIXTURES / "queues.csv").read_bytes())
    return output.getvalue()


def schedule_export(request):
    response = client.post("/api/schedule-v2", json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    exported = client.post(
        "/api/schedule-v2/export",
        json=result,
        headers={"Content-Type": "application/json"},
    )
    assert exported.status_code == 200, exported.text
    rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
    return result, rows


def test_generic_interval_csv_import_to_schedule_and_break_aware_export():
    imported = client.post(
        "/api/calls/import",
        params={
            "file_format": "csv",
            "mode": "planned_calls",
            "group_id": ["sales", "support"],
        },
        content=(
            "date,start,group_id,calls\n"
            "2026-11-02,08:00,sales,1\n"
            "2026-11-02,08:00,support,0\n"
            "2026-11-02,08:30,sales,0\n"
            "2026-11-02,08:30,support,0\n"
            "2026-11-02,09:00,sales,0\n"
            "2026-11-02,09:00,support,1\n"
            "2026-11-02,09:30,sales,1\n"
            "2026-11-02,09:30,support,0\n"
            "2026-11-03,13:00,sales,0\n"
            "2026-11-03,13:00,support,1\n"
            "2026-11-03,13:30,sales,1\n"
            "2026-11-03,13:30,support,0\n"
            "2026-11-03,14:00,sales,0\n"
            "2026-11-03,14:00,support,1\n"
        ).encode(),
        headers={"Content-Type": "text/csv"},
    )
    assert imported.status_code == 200, imported.text
    rows = imported.json()["rows"]

    groups = [{"id": "sales", "name": "Sales"}, {"id": "support", "name": "Support"}]
    operating = [
        {"date": "2026-11-02", "start": "08:00", "end": "10:00"},
        {"date": "2026-11-03", "start": "13:00", "end": "14:30"},
    ]
    slots = [
        (day, start)
        for day, starts in (
            ("2026-11-02", ("08:00", "08:30", "09:00", "09:30")),
            ("2026-11-03", ("13:00", "13:30", "14:00")),
        )
        for start in starts
    ]
    request = {
        "mode": "planned_calls",
        "week_start": "2026-11-02",
        "groups": groups,
        "operating_intervals": operating,
        "employees": [
            {
                "id": f"TEST-e{index:02d}",
                "skills": ["sales", "support"],
                "available_slots": [
                    {"date": day, "start": start} for day, start in slots
                ],
                "unavailable_dates": [],
                "max_shift_minutes_per_week": 600,
            }
            for index in range(1, 11)
        ],
        "shifts": [
            {
                "date": "2026-11-02",
                "start": "08:00",
                "end": "10:00",
                "breaks": [{"start": "08:30", "end": "09:00"}],
            },
            {
                "date": "2026-11-03",
                "start": "13:00",
                "end": "14:30",
                "breaks": [],
            },
        ],
        "service_targets": [
            {"group_id": group, "target_sla": 0.8, "waiting_seconds": 20.0}
            for group in ("sales", "support")
        ],
        "aht_by_group": [
            {
                "group_id": group,
                "aht_seconds": 60.0,
                "sample_count": 10,
                "cutoff_before": "2026-11-02",
            }
            for group in ("sales", "support")
        ],
        "allow_skill_fallback": False,
        "max_solve_seconds": 3,
        "planned_calls": rows,
    }
    result, csv_rows = schedule_export(request)

    assert result["schedule"]["status"] in {"OPTIMAL", "FEASIBLE"}
    assert {item["date"] for item in result["interval_results"]} == {
        "2026-11-02",
        "2026-11-03",
    }
    assert result["schedule"]["total_shift_minutes"] > 0
    assert all(item["employee_id"].startswith("TEST-") for item in result["schedule"]["assignments"])
    assert csv_rows[0] == [
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
    assert not any(
        row[1] == "2026-11-02" and row[2] == "08:30"
        for row in csv_rows[1:]
    )
    assert any(row[8] == "08:30-09:00" and row[9] == "120" for row in csv_rows[1:])
    assert any(row[9] == "90" for row in csv_rows[1:])


def test_pinned_bundle_import_to_history_schedule_and_export():
    imported = client.post(
        "/api/calls/import",
        params={
            "file_format": "database_sample_bundle",
            "mode": "history",
            "week_start": "2025-02-24",
            "history_start_date": "2025-02-03",
            "history_end_date": "2025-02-24",
            "group_id": ["1", "3"],
        },
        content=database_bundle(),
        headers={"Content-Type": "application/zip"},
    )
    assert imported.status_code == 200, imported.text
    envelope = imported.json()
    groups = envelope["groups"]
    group_ids = [group["id"] for group in groups]
    target_weekdays = {0, 1}
    history = [
        row
        for row in envelope["rows"]
        if date.fromisoformat(row["date"]).weekday() in target_weekdays
    ]
    week_start = "2025-02-24"
    operating = [
        {"date": week_start, "start": "16:30", "end": "18:00"},
        {"date": "2025-02-25", "start": "16:00", "end": "18:00"},
    ]
    slots = [
        (day, start)
        for day, starts in (
            (week_start, ("16:30", "17:00", "17:30")),
            ("2025-02-25", ("16:00", "16:30", "17:00", "17:30")),
        )
        for start in starts
    ]
    request = {
        "mode": "history",
        "week_start": week_start,
        "history_window": envelope["history_window"],
        "groups": groups,
        "operating_intervals": operating,
        "employees": [
            {
                "id": f"TEST-source-e{index:02d}",
                "skills": group_ids,
                "available_slots": [
                    {"date": day, "start": start} for day, start in slots
                ],
                "unavailable_dates": [],
                "max_shift_minutes_per_week": 600,
            }
            for index in range(1, 11)
        ],
        "shifts": [
            {
                "date": week_start,
                "start": "16:30",
                "end": "18:00",
                "breaks": [{"start": "17:00", "end": "17:30"}],
            },
            {
                "date": "2025-02-25",
                "start": "16:00",
                "end": "18:00",
                "breaks": [],
            },
        ],
        "service_targets": [
            {"group_id": group, "target_sla": 0.5, "waiting_seconds": 20.0}
            for group in group_ids
        ],
        "aht_by_group": envelope["aht_by_group"],
        "allow_skill_fallback": False,
        "max_solve_seconds": 3,
        "history": history,
    }
    result, csv_rows = schedule_export(request)

    assert result["forecast"] is not None
    assert result["forecast"]["forecasts"]
    assert {item["date"] for item in result["interval_results"]} == {
        week_start,
        "2025-02-25",
    }
    assert result["schedule"]["status"] in {"OPTIMAL", "FEASIBLE"}
    assert all(
        item["employee_id"].startswith("TEST-source-")
        for item in result["schedule"]["assignments"]
    )
    assert any(row[8] == "17:00-17:30" and row[9] == "90" for row in csv_rows[1:])
    assert any(row[9] == "120" for row in csv_rows[1:])
    assert not any(
        row[1] == week_start and row[2] == "17:00" for row in csv_rows[1:]
    )


def test_explicit_infeasible_availability_preserves_solver_status_and_null_staffing():
    request = json.loads(
        (SAMPLES / "pipeline-planned-calls-request.json").read_text(encoding="utf-8")
    )
    request["employees"][0]["available_slots"] = []

    response = client.post("/api/schedule-v2", json=request)

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["schedule"]["status"] == "INFEASIBLE"
    assert result["schedule"]["assignments"] == []
    assert all(item["assigned_staff"] is None for item in result["interval_results"])
