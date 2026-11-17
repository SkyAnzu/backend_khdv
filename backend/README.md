# `backend/`

Package Python của backend scheduling prototype. Chạy lệnh từ repo root
`backend_khdv` để test import `backend.app` và khởi động API:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

Pipeline hiện dùng demand/forecast từng interval 30 phút, Erlang C theo queue
(`calls_per_hour = 2 * calls_per_interval`) và solver v2 với shifts có envelope
và breaks. Xem [README ở repo root](../README.md) để biết payload, import/export,
body limits, và các giới hạn prototype.
