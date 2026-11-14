# `backend/`

Đây là package Python của prototype A. Chạy lệnh từ **repo root**
`backend_khdv`, không chạy từ thư mục `backend/`, vì test và CLI import
`backend.app`.

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
.\.venv\Scripts\python.exe -m backend.app.demo backend\samples\feasible.json
.\.venv\Scripts\python.exe -m backend.app.demo backend\samples\infeasible.json
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Dependency source duy nhất của prototype là `backend/requirements.txt`. Solver
v1 giữ nguyên hai rule cũ; `app/solver_v2.py` là module tuần độc lập, chưa nối
vào API. Xem README tại repo root để biết input/output và giới hạn v2.
