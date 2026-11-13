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

Dependency source duy nhất của prototype là `backend/requirements.txt`. Phạm vi
solver hiện tại chỉ là hai rule v1; các task A3+ chưa được claim hoàn tất.
