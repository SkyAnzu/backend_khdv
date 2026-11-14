# Backend scheduling prototype

Prototype backend A được tách độc lập từ `BTL/backend/`. Repo này **chưa** là
backend contract v1.0.0 trên remote khác. Solver cũ trong
`backend/app/solver.py` vẫn là prototype tuần 1–2 với hai quy tắc; module solver
tuần v2 ở `backend/app/solver_v2.py` được pipeline gọi qua API prototype
`POST /api/schedule-v2`.

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

Module nội bộ nhận số calls/giờ, AHT và mục tiêu SLA; không có route Erlang C
độc lập. Pipeline API prototype gọi module này. Ví dụ 12 calls/giờ, AHT 300
giây, SLA 0.8 trong 20 giây cần 3 nhân viên:

```powershell
.\.venv\Scripts\python.exe -c "from backend.app.models import ErlangCRequest; from backend.app.erlang_c import calculate_required_staff; request=ErlangCRequest.model_validate({'calls_per_hour':12,'aht_seconds':300,'target_sla':0.8,'waiting_seconds':20}); print(calculate_required_staff(request).model_dump_json())"
```

Mặc định phép tìm kiếm bị giới hạn ở 10.000 nhân viên để chặn công việc quá lớn;
không đạt target trong giới hạn sẽ ném `ValueError`, không phải trạng thái solver.

## Pipeline nội bộ: forecast/Erlang C → solver tuần v2

`backend.app.pipeline.run_pipeline(PipelineRequest)` ghép hai nguồn nhu cầu với
Erlang C và `solve_schedule_v2()`. `POST /api/schedule-v2` gọi pipeline này như
một API local prototype; endpoint chưa phải contract sản phẩm/frontend đã chốt.
`/api/solver-preview` không đổi. AHT và ngưỡng chờ tính bằng giây,
SLA là tỷ lệ từ 0 đến 1; các tham số Erlang C dùng chung cho mọi nhóm trong
request. Kết quả `service_level` là ước lượng Erlang C theo nhóm, không phải cam
kết SLA cho pool nhân viên đa kỹ năng.

Ví dụ tổng hợp, đầy đủ ma trận theo giờ/nhóm và hai slot nửa giờ:

```python
import json

from backend.app.pipeline import PipelineRequest, run_pipeline

day = "2026-11-02"
half_hours = ("08:00", "08:30", "09:00", "09:30")
payload = {
    "mode": "planned_calls",
    "week_start": day,
    "groups": ["sales"],
    "operating_hours": [
        {"date": day, "hour": 8},
        {"date": day, "hour": 9},
    ],
    "planned_calls": [
        {"date": day, "hour": 8, "group_id": "sales", "calls_per_hour": 0.5},
        {"date": day, "hour": 9, "group_id": "sales", "calls_per_hour": 0},
    ],
    "aht_seconds": 300,
    "target_sla": 0.8,
    "waiting_seconds": 20,
    "employees": [{
        "id": "e01",
        "skills": ["sales"],
        "available_slots": [
            {"date": day, "start": start} for start in half_hours
        ],
        "unavailable_dates": [],
        "max_work_minutes_per_week": 90,
    }],
    "shift_starts": [{"date": day, "start": "08:00"}],
}
result = run_pipeline(PipelineRequest.model_validate(payload))
print(json.dumps(result.model_dump(mode="json"), ensure_ascii=True))
```

Để chạy nhánh lịch sử, đặt `mode` thành `"history"`, bỏ `planned_calls` và thêm
ít nhất hai quan sát trước tuần đích cho cùng weekday/giờ/nhóm của mọi ô vận
hành. Dùng `PipelineRequest.model_validate(payload)` cho cả hai mode; không được
đưa cả hai nguồn cùng lúc. Calls dự kiến phải khai báo đúng một dòng cho mỗi
`operating_hours × groups`, kể cả zero. Mỗi nhu cầu Erlang C được giữ nguyên ở
hai slot 30 phút tương ứng. Operating hours phải liên tục trong từng ngày; giờ
23 bị từ chối rõ ràng vì slot 23:30 không được solver v2 hỗ trợ. Nhân viên,
availability, ngày vắng, giới hạn phút và giờ bắt đầu ca dùng model solver v2
hiện có; giờ/ca không nằm trong operating grid bị từ chối.

Erlang C mặc định tìm tối đa 10.000 agents; khi kết quả vượt giới hạn 50 người
cho mỗi nhóm/slot của solver v2, pipeline ném `ValueError` thay vì kẹp số hoặc
trả `INFEASIBLE`. Lỗi không đủ `max_agents` cũng là `ValueError`. Kết quả gồm
forecast (chỉ mode history), calls và service level theo giờ, requirements từng
slot, cùng response solver nguyên trạng gồm status, `stage_two_status` và
warnings. Kiểm tra cả pipeline bằng pytest ở mục cài đặt.

## Solver tuần v2 (module Python và pipeline API prototype)

`backend.app.solver_v2.solve_schedule_v2()` là module mới, độc lập với solver
v1. Không có route gọi solver v2 trực tiếp; pipeline API prototype ở
`POST /api/schedule-v2` dùng solver v2. `POST /api/solver-preview` và các model
v1 không đổi.
Lưới lập lịch gồm slot 30 phút và ca cố định 90 phút. Kỳ lập lịch là đúng 7 ngày
Monday–Sunday; mỗi ca phải có ba slot liên tục cùng ngày. Người dùng khai báo
các `shift_starts` có thể dùng, solver không tự tạo giờ bắt đầu.

Input bắt buộc khai báo:

- `groups`, `operating_slots` và một `requirements` cho **mọi cặp slot/nhóm**.
  `required_staff` là số nguyên 0–50; `0` là nhu cầu bằng không, còn thiếu dòng
  là input không hợp lệ.
- `employees` có `skills`, `available_slots` (list slot tường minh, là tập con
  của `operating_slots`; cả ba slot của ca phải khả dụng),
  `unavailable_dates` (phải có field; `[]` nghĩa là không có ngày vắng),
  `max_work_minutes_per_week` (phút, từ 0 đến 10.080). Có thể khai báo
  `min_days_off_per_week` (0–7) nếu cần ràng buộc số ngày nghỉ tối thiểu.
- `allow_skill_fallback` (mặc định `false`) và `max_solve_seconds` (1–30 giây,
  mặc định 10).

Ví dụ gọi trực tiếp từ Python tại repo root:

```python
import json

from backend.app.solver_v2 import SolverV2Request, solve_schedule_v2

payload = {
    "week_start": "2026-11-02",
    "groups": ["sales"],
    "operating_slots": [
        {"date": "2026-11-02", "start": start}
        for start in ("08:00", "08:30", "09:00")
    ],
    "requirements": [
        {
            "date": "2026-11-02",
            "start": start,
            "group_id": "sales",
            "required_staff": 1,
        }
        for start in ("08:00", "08:30", "09:00")
    ],
    "employees": [{
        "id": "e01",
        "skills": ["sales"],
        "available_slots": [
            {"date": "2026-11-02", "start": start}
            for start in ("08:00", "08:30", "09:00")
        ],
        "unavailable_dates": [],
        "max_work_minutes_per_week": 90,
        "min_days_off_per_week": 6,
    }],
    "shift_starts": [{"date": "2026-11-02", "start": "08:00"}],
    "allow_skill_fallback": False,
    "max_solve_seconds": 10,
}
result = solve_schedule_v2(SolverV2Request.model_validate(payload))
print(json.dumps(result.model_dump(mode="json"), ensure_ascii=True))
```

Mỗi assignment trả nhân viên, ngày, giờ bắt đầu/kết thúc và nhóm được phục vụ
ở từng slot. CP-SAT tối ưu tuần tự: trước số person-slots lệch kỹ năng, sau đó
tổng person-slots; không dùng trọng số ghép hoặc dữ liệu lương. `warnings` báo
fallback kỹ năng và các person-slots vượt nhu cầu. `OPTIMAL` chỉ có nghĩa cả hai
bước tối ưu đã được chứng minh; `FEASIBLE` vẫn là lịch thỏa ràng buộc nhưng một
bước tối ưu chưa được chứng minh. `stage_two_status` cho biết status riêng của
bước giảm tổng slot nếu bước đó không kết thúc bình thường; `MODEL_INVALID` được
tách riêng khỏi `INFEASIBLE`.

Giới hạn cứng để chặn model quá lớn: 50 nhân viên, 10 nhóm, 336 operating slots,
200 ca bắt đầu, 3.360 dòng nhu cầu và tối đa 30 giây solve. Không hỗ trợ nghỉ
giữa ca, ca qua ngày hoặc múi giờ/DST. Chạy test v2 cùng toàn bộ backend bằng
lệnh pytest ở phần trên.

## Chạy API local

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

- `GET http://127.0.0.1:8000/health`
- `POST http://127.0.0.1:8000/api/solver-preview` — solver prototype v1 cũ.
- `POST http://127.0.0.1:8000/api/schedule-v2` — pipeline API prototype, contract
  tạm thời để thử nghiệm, chưa chốt với frontend.
- Swagger: `http://127.0.0.1:8000/docs`

Trong Swagger, chọn `POST /api/schedule-v2` → **Try it out** rồi gửi nội dung
`backend\samples\pipeline-planned-calls-request.json` hoặc
`backend\samples\pipeline-history-request.json`. Hai request chỉ dùng dữ liệu
tổng hợp; response tương ứng được lưu ở `pipeline-planned-calls-response.json`
và `pipeline-history-response.json` trong cùng thư mục.
PowerShell cũng có thể gửi trực tiếp sample:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/schedule-v2 -ContentType "application/json" -InFile "backend\samples\pipeline-planned-calls-request.json"
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/schedule-v2 -ContentType "application/json" -InFile "backend\samples\pipeline-history-request.json"
```

Response trả `mode`, forecast (null ở mode planned), kết quả Erlang theo giờ,
requirements 30 phút và response solver gồm nguyên trạng status, phase status
và warnings. `INFEASIBLE`, `UNKNOWN` và `MODEL_INVALID` là kết quả HTTP 200 của
solver; validation và lỗi miền tính toán trả HTTP 422. Request validation của
endpoint mới không lặp giá trị input trong lỗi; `/api/solver-preview` vẫn giữ
hành vi cũ. Hàm route là đồng bộ để FastAPI chạy nó trong threadpool; giới hạn
solver tối đa 30 giây vẫn giữ nguyên.

Response trong file `*-response.json` là mẫu thực đã nhận từ request tổng hợp;
status và assignment có thể khác nếu solver dừng ở `FEASIBLE`, nhưng schema và
ý nghĩa status được giữ nguyên.

## Xuất kết quả pipeline đã có sang CSV

`backend.app.export.pipeline_to_csv(result)` tạo bytes CSV từ các assignment
đang có trong `PipelineResponse`; hàm này không gọi lại pipeline hoặc solver và
không xác minh độc lập độ phủ, ràng buộc hay nguồn gốc của response. Chỉ dùng
với kết quả đáng tin cậy do pipeline vừa trả về. Ví dụ dùng request tổng hợp đã
có trong repo và ghi file có chủ ý tại thư mục hiện hành:

```python
import json
from pathlib import Path

from backend.app.export import pipeline_to_csv
from backend.app.pipeline import PipelineRequest, run_pipeline

payload = json.loads(
    Path("backend/samples/pipeline-planned-calls-request.json").read_text(
        encoding="utf-8"
    )
)
result = run_pipeline(PipelineRequest.model_validate(payload))
Path("lich_mau.csv").write_bytes(pipeline_to_csv(result))
```

Mỗi dòng là một phân công nhân viên/nhóm trong một slot 30 phút; thông tin ca
90 phút được lặp lại ở ba dòng. `Bắt đầu khoảng` và `Kết thúc khoảng` là biên
của slot, còn `Bắt đầu ca` và `Kết thúc ca` là biên của cả ca. Ngày dùng
`YYYY-MM-DD`, giờ dùng `HH:MM`. File có BOM UTF-8 và được tạo bằng `csv.writer`
để Excel đọc tiếng Việt và giữ nguyên dấu phẩy/nháy/xuống dòng trong ô.

Chỉ `OPTIMAL` hoặc `FEASIBLE` được xuất; status không có lịch hợp lệ bị từ chối.
Lịch khả thi không có phân công vẫn cho file chỉ có hàng tiêu đề. Mã nhân viên
và tên nhóm bắt đầu bằng `=`, `+`, `-`, `@`, hoặc bằng control/whitespace nguy
cơ được thêm dấu nháy đơn ở đầu để giảm nguy cơ formula injection; giá trị CSV
có thể hiển thị thêm dấu này. Response solver chỉ có số lần lệch kỹ
năng/cảnh báo ở mức tổng hợp, không chỉ rõ phân công nào lệch, nên CSV không
gắn cờ khớp kỹ năng giả cho từng dòng. Chưa có route export riêng hoặc frontend
integration. Export từ chối response vượt giới hạn hiện có: tối đa 10.000 ca
(50 nhân viên × 200 giờ bắt đầu), tương đương 30.000 dòng slot.

Prototype chưa có auth, database, public deployment hoặc contract sản phẩm đã
chốt. Chỉ chạy trên loopback với sample/dữ liệu đã khử định danh; không đưa
secret vào repo và không chạy tunnel với dữ liệu thật.
