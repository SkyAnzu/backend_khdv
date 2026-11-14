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
- `POST http://127.0.0.1:8000/api/calls/import` — parse body CSV/XLSX thành rows;
  không chạy pipeline hoặc lưu file.
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
`/api/schedule-v2` và `/api/calls/import` không lặp giá trị input trong lỗi;
`/api/solver-preview` vẫn giữ hành vi cũ. Hàm schedule route là đồng bộ để
FastAPI chạy nó trong threadpool; giới hạn solver tối đa 30 giây vẫn giữ nguyên.

Response trong file `*-response.json` là mẫu thực đã nhận từ request tổng hợp;
status và assignment có thể khác nếu solver dừng ở `FEASIBLE`, nhưng schema và
ý nghĩa status được giữ nguyên.

## Parse calls CSV/XLSX qua API local

`POST /api/calls/import` nhận **raw bytes** làm HTTP body, không phải
`multipart/form-data`. Chọn rõ `file_format=csv|xlsx` và `mode=history|planned_calls`;
truyền mỗi nhóm bằng một query `group_id` lặp lại (1–10 nhóm duy nhất). Không
suy luận format từ tên file, không nhận path và không ghi file. `Content-Type`
phải khớp format (`text/csv` hoặc MIME XLSX bên dưới); cũng có thể dùng
`application/octet-stream` vì `file_format` đã được chỉ định. Content-Encoding
nén không được hỗ trợ.

Ví dụ dùng sample planned calls; response giữ đúng model theo mode và ngày ISO:

```powershell
curl.exe --request POST "http://127.0.0.1:8000/api/calls/import?file_format=csv&mode=planned_calls&group_id=sales" --header "Content-Type: text/csv" --data-binary "@backend/samples/calls-planned.csv"

Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/calls/import?file_format=csv&mode=planned_calls&group_id=sales' -ContentType "text/csv" -InFile "backend\samples\calls-planned.csv"
```

Response dự kiến:

```json
{
  "mode": "planned_calls",
  "rows": [
    {
      "date": "2026-11-02",
      "hour": 8,
      "group_id": "sales",
      "calls_per_hour": 2.5
    },
    {
      "date": "2026-11-02",
      "hour": 9,
      "group_id": "sales",
      "calls_per_hour": 0.0
    }
  ]
}
```

Với nhiều nhóm, lặp query, ví dụ
`...?file_format=csv&mode=history&group_id=sales&group_id=support`. Response
history dùng `calls` số nguyên; response planned dùng `calls_per_hour` số hữu
hạn, có thể thập phân. Giá trị zero được giữ nguyên, không tự điền record thiếu.
Trong Swagger, endpoint ghi body dạng binary; gửi nội dung file trực tiếp thay vì
form upload. XLSX dùng MIME
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`.

Server stream và chặn body thực lớn hơn 2 MiB (Content-Length chỉ là early reject,
không thay cho giới hạn stream); các row/XLSX caps vẫn do parser hiện có áp dụng.
Chỉ một parse chạy đồng thời trong mỗi process; khi bận endpoint trả HTTP 429 và
`Retry-After: 1`. Đây là guard cục bộ theo process, không phải hàng đợi hay giới
hạn xuyên nhiều worker. API trả rows để caller ghép vào `history` hoặc
`planned_calls` của `PipelineRequest`; endpoint không tự tạo request lịch, không
kiểm tra nhân viên/coverage, không chạy solver và chưa tích hợp frontend. Chỉ
chạy local với dữ liệu tổng hợp/đã khử định danh; API prototype chưa có auth hoặc
public deployment.

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

## Đọc calls theo giờ từ CSV/XLSX — parser Python và API local

`backend.app.import_calls.parse_calls_import(data: bytes, *, file_format,
mode, group_ids)` nhận bytes trong bộ nhớ và trả về list model pipeline hiện có.
`POST /api/calls/import` bọc parser này với body raw bytes và trả `{mode, rows}`:
`PipelineHistoryObservation` khi `mode="history"` hoặc `PipelinePlannedCalls`
khi `mode="planned_calls"`. Không nhận đường dẫn, không ghi file và không tạo
multipart upload; form web và pipeline JSON hiện tại không đổi. Kết quả chỉ là
các dòng calls, chưa phải một `PipelineRequest` hoàn chỉnh.

CSV và XLSX dùng cùng đúng bốn cột theo thứ tự:

```csv
date,hour,group_id,calls
2026-11-02,8,sales,2.5
```

`date` dùng `YYYY-MM-DD` (Excel cũng nhận ô ngày lúc 00:00), `hour` là số nguyên
0–22; giờ 23 bị từ chối vì solver hiện không hỗ trợ slot 23:30. `group_id` phải
nằm trong danh sách `group_ids` đã chọn. Với `history`, `calls` là số nguyên
không âm; với `planned_calls`, đó là số hữu hạn không âm và có thể thập phân
(dấu chấm thập phân). Số 0 hợp lệ, nhưng parser không tự điền dòng/cột thiếu.
Dòng trùng theo ngày/giờ/nhóm, header sai, nhóm lạ và giá trị sai bị từ chối;
lỗi chỉ nêu định dạng và dòng/hàng, không lặp nội dung ô.

Giới hạn: file tối đa 2 MiB, tối đa 10.000 dòng history hoặc 1.610 dòng
planned calls. XLSX chỉ nhận gói `.xlsx` có một sheet tên `calls`; worksheet
được tìm qua workbook relationship trong `xl/worksheets/`, target không an toàn
và worksheet part thừa bị từ chối. Tất cả `.xml`/`.rels` được preflight bằng XML
streaming trước bất kỳ eager XML parsing nào của `openpyxl`. Từng part tối đa
2 MiB; tổng XML tối đa 150.000 elements, mỗi eager part 4.096 elements,
worksheet 120.000 và shared strings 50.000; độ sâu tối đa 64, tối đa 64
attributes/element và 100.000 attributes tổng. Shared strings còn giới hạn
20.000 mục/200.000 ký tự và từ chối rich text runs. Manifest phải chọn đúng một
`xl/workbook.xml`; nếu có shared strings, manifest và relationship phải cùng
chọn `xl/sharedStrings.xml`. Content types, workbook XML và relationship
metadata tối đa 1 MiB. Parser cũng từ chối công thức, macro, external links, ZIP
mã hóa/entry nguy hiểm, hơn 100 entry, tổng giải nén theo metadata hơn 20 MiB,
một entry hơn 10 MiB hoặc tỷ lệ nén trên 100. `openpyxl` 3.1.5 đọc workbook
read-only, không lấy cached formula result; `defusedxml` 0.7.1 bật bảo vệ XML.
XLSX test được tạo trong bộ nhớ, không thực thi công thức.

Trước `iterparse`, byte guard yêu cầu UTF-8 (cho phép BOM đầu part, từ chối NUL
và XML declaration encoding khác UTF-8), tối đa 256 literal `xmlns` markers mỗi
part và tối đa 8 KiB cho mỗi markup token được quét quote-aware (tag, comment,
CDATA hoặc PI). Marker được đếm thô trên bytes, kể cả trong comment/text, nên có
thể từ chối nhầm part không khai báo namespace. Đây là guard allocation có phạm
vi hẹp, không phải XML validator hay cam kết mọi XML độc hại đều bị chặn; DTD,
entity và external entity vẫn bị `defusedxml` cấm. Giới hạn 2 MiB/part và sức
chứa worksheet thông thường không đổi.

Giới hạn ZIP: parser kiểm tra `ZipInfo` và số byte `zipfile` trả về, nhưng
không tự giải mã/đếm độc lập toàn bộ raw DEFLATE stream khi metadata khai báo
sai kích thước. Kiểm tra source Python 3.12 cho thấy
`zipfile.ZipExtFile._read1` cắt output theo `ZipInfo.file_size` và dừng khi đạt
giới hạn khai báo; vì vậy output parser nhận được còn bị chặn bởi các size caps.
Đây là bằng chứng cho giới hạn output trả về, không phải xác minh kích thước
thực/EOF của raw stream; metadata discrepancy vẫn là giới hạn integrity còn lại.

Hai CSV tổng hợp có sẵn: `backend/samples/calls-history.csv` và
`backend/samples/calls-planned.csv`. Ví dụ đọc sample bằng helper:

```python
from pathlib import Path

from backend.app.import_calls import parse_calls_import

history = parse_calls_import(
    Path("backend/samples/calls-history.csv").read_bytes(),
    file_format="csv",
    mode="history",
    group_ids=["sales"],
)
planned = parse_calls_import(
    Path("backend/samples/calls-planned.csv").read_bytes(),
    file_format="csv",
    mode="planned_calls",
    group_ids=["sales"],
)
excel = parse_calls_import(
    Path("du_lieu.xlsx").read_bytes(),
    file_format="xlsx",
    mode="planned_calls",
    group_ids=["sales"],
)
```

Ở ví dụ XLSX, file do người gọi đọc thành bytes; bên trong phải có đúng một sheet
`calls` với cùng header. `du_lieu.xlsx` là tên file do người dùng tự cung cấp,
không phải file sample của repo.

Parser kiểm tra kiểu, ngày/giờ, group, số dòng và khóa trùng; quy tắc phủ đủ
`operating_hours × groups`, tuần đích và các trường nhân viên/ca vẫn do
`PipelineRequest` hiện có kiểm tra khi caller ghép request sau này. Route import
là API local-only cho rows; chưa có UI/form upload hoặc tích hợp frontend.

Prototype chưa có auth, database, public deployment hoặc contract sản phẩm đã
chốt. Chỉ chạy trên loopback với sample/dữ liệu đã khử định danh; không đưa
secret vào repo và không chạy tunnel với dữ liệu thật.
