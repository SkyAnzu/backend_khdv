# Backend scheduling prototype

Prototype backend A được tách độc lập từ `BTL/backend/`. Repo này **chưa** là
backend contract v1.0.0 trên remote khác: code hiện tại là prototype tuần 1–2,
chỉ có hai quy tắc solver được ghi trong `backend/app/solver.py`.

## Cài đặt và kiểm tra trên Windows

Chạy các lệnh từ **root repo này** (`backend_khdv`), để Python giữ được import
`backend.app`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.\.venv\Scripts\python.exe -m pytest backend\tests -q
```

Luôn chạy test trong workspace hiện tại; số lượng test thay đổi khi thêm module.
Khi clone repo này phải chạy lại, không coi kết quả cũ là của máy mới.

## Chạy hai sample

```powershell
.\.venv\Scripts\python.exe -m backend.app.demo backend\samples\feasible.json
.\.venv\Scripts\python.exe -m backend.app.demo backend\samples\infeasible.json
```

Sample khả thi phải trả `OPTIMAL` hoặc `FEASIBLE`; sample không khả thi phải trả
`INFEASIBLE`.

## Tính Erlang C trực tiếp

Module nội bộ nhận số calls/giờ, AHT và mục tiêu SLA; không có route HTTP. Ví dụ
12 calls/giờ, AHT 300 giây, SLA 0.8 trong 20 giây cần 3 nhân viên:

```powershell
.\.venv\Scripts\python.exe -c "from backend.app.models import ErlangCRequest; from backend.app.erlang_c import calculate_required_staff; request=ErlangCRequest.model_validate({'calls_per_hour':12,'aht_seconds':300,'target_sla':0.8,'waiting_seconds':20}); print(calculate_required_staff(request).model_dump_json())"
```

Mặc định phép tìm kiếm bị giới hạn ở 10.000 nhân viên để chặn công việc quá lớn;
không đạt target trong giới hạn sẽ ném `ValueError`, không phải trạng thái solver.

## Chạy API local

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

- `GET http://127.0.0.1:8000/health`
- `POST http://127.0.0.1:8000/api/solver-preview`
- Swagger: `http://127.0.0.1:8000/docs`

Chưa có auth, database, public deployment hoặc API lịch nghiệp vụ tuần 3 trong
prototype này. Không đưa secret vào repo; không chạy tunnel với dữ liệu thật.
