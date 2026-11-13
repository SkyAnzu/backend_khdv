from fastapi.testclient import TestClient

from backend.app.main import app


client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_solver_preview_accepts_json_and_returns_result():
    response = client.post(
        "/api/solver-preview",
        json={
            "employees": [{"id": "e01", "name": "An"}],
            "shift_templates": [
                {"id": "s1", "name": "Ca sáng", "start": "08:00", "end": "12:00"}
            ],
            "requirements": [
                {"date": "2026-11-02", "start": "08:00", "end": "12:00", "required_staff": 1}
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] in {"FEASIBLE", "OPTIMAL"}
    assert response.json()["assignments"][0]["employee_id"] == "e01"
