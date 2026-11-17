# Backend scheduling prototype

Repo backend A là prototype độc lập, chưa phải product/API contract và chưa tích
hợp frontend. Luồng hiện tại là `PipelineRequest` → forecast (history mode) →
Erlang C theo interval → solver tuần v2.

## Cài đặt và kiểm tra trên Windows

Chạy từ **root repo `backend_khdv`** để Python import được `backend.app`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.\.venv\Scripts\python.exe -m pytest backend\tests -q
```

## Hợp đồng pipeline hiện tại

Lưới demand và lịch là **30 phút**. Mỗi row history/planned có đúng bốn trường:
`date,start,group_id,calls`; `start` là `HH:00` hoặc `HH:30`. Khoảng là nửa mở
`[start,end)`, không tự làm tròn hoặc lấp row bị thiếu. Planned mode phải gửi
đúng một row cho từng cặp operating slot/nhóm, kể cả `calls: 0`.

- History forecast lấy trung bình các quan sát cùng weekday/start/group trong
  `history_window`; mỗi target cần ít nhất hai ngày quan sát riêng. Lịch sử
  không được bù zero; mọi history row phải thuộc một group đã khai báo.
- Erlang C chạy độc lập cho từng queue/interval với
  `calls_per_hour = 2 * calls_in_30_minutes`; AHT vẫn tính bằng giây và không
  chia đôi. SLA fraction và `waiting_seconds` được cấu hình riêng theo queue.
  Positive calls cần AHT hợp lệ; zero calls trả 0 nhân viên và SLA `null`, không
  cần AHT.
- `groups` dùng `{id,name}`; `operating_intervals` là các range ngày có thể rời
  nhau. `shifts` là occurrences cùng ngày `{date,start,end,breaks}`. Break không
  tạo coverage nhưng toàn thời lượng elapsed của ca (gồm break) tính vào cap
  tuần và mục tiêu phút. Không có overnight shift; `24:00` chỉ là endpoint loại
  trừ.
- Solver giữ hard skill mặc định, fallback tường minh có warning và
  `min_days_off_per_week` tùy chọn. Không thêm daily cap, labor/rest defaults,
  wages, fairness hoặc headcount objective. `INFEASIBLE`, `UNKNOWN` và
  `MODEL_INVALID` được trả như status solver, không phải HTTP validation errors.
- Response có `mode`, `forecast`, `groups`, `interval_results` và `schedule`;
  không có top-level `requirements` hay provenance theo từng row. Lịch lỗi
  validation/domain trả 422 đã làm sạch; lỗi nội bộ bất ngờ vẫn là 500.

Các request samples trong `backend/samples/pipeline-*-request.json` dùng
employee/roster và calls **TEST-only**, không phải nguồn dữ liệu vận hành hay
default nhân viên/giờ mở cửa. Sample response được lưu để minh họa schema/status;
assignment có thể thay đổi khi chạy lại solver.

## Giới hạn và cấu hình body

Prototype giữ giới hạn 50 nhân viên, 10 nhóm, 336 operating slots, 3.360 planned
rows, 200 shift candidates, 100.000 history rows và tối đa 30 giây solve-phase
budget. Cap 3.360 là `336 operating slots × 10 groups`, dùng chung cho planned
JSON và generic planned CSV/XLSX; generic history import vẫn tối đa 10.000 rows.
Đây là các technical ceilings của input, không phải capacity/performance
benchmark. Trước khi tạo `CpModel`, solver ước lượng số assignment variables đủ
điều kiện và từ chối cấu hình vượt `MAX_ASSIGNMENT_VARIABLES=300_000`;
input employee/demand không bị cắt.

Hai giới hạn body độc lập, mỗi giới hạn mặc định 2 MiB, phải là positive integer
và được đọc lúc process khởi động:

```powershell
$env:KHDV_MAX_UPLOAD_BYTES = "4194304"
$env:KHDV_MAX_SCHEDULE_BODY_BYTES = "4194304"
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Đổi `$env:` cần khởi động lại backend để nạp cấu hình mới. Upload cap không thay
đổi các giới hạn độc lập của ZIP expansion/ratio/entry/row hoặc model; export
vẫn dùng body cap hiện có 2 MiB.

## API local

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000
```

- `GET /health`
- `POST /api/schedule-v2` — nhận typed JSON request; có thể thử bằng hai sample
  request TEST-only trong `backend/samples/`.
- `POST /api/schedule-v2/export` — nhận `PipelineResponse` đã có, không chạy lại
  solver; chỉ xuất status `OPTIMAL`/`FEASIBLE`.
- `POST /api/calls/import` — nhận raw bytes; không multipart, không ghi file,
  không chạy lịch.
- Swagger: `http://127.0.0.1:8000/docs`

## Import calls CSV/XLSX

Generic CSV/XLSX dùng đúng header:

```csv
date,start,group_id,calls
2026-11-02,08:00,sales,0.5
2026-11-02,08:30,sales,0
```

`backend/samples/calls-history.csv` và `calls-planned.csv` cũng là fixture
**TEST-only**, không phải số liệu vận hành.

Gọi với `file_format=csv|xlsx`, `mode=history|planned_calls` và một hay nhiều
`group_id` query values:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/calls/import?file_format=csv&mode=planned_calls&group_id=sales' -ContentType 'text/csv' -InFile 'backend\samples\calls-planned.csv'
```

Trả về `{mode,rows}` với bốn trường, không tự sinh missing rows. Planned import
dùng chung cap 3.360 rows với schedule JSON; history import giữ cap 10.000 rows.
CSV/XLSX vẫn được bảo vệ bằng parser bounds hiện có (bao gồm file 2 MiB mặc
định, ZIP/XML caps và formula/XML checks); XLSX chỉ chấp nhận sheet `calls`.

### Import bundle Database Sample

Chế độ `file_format=database_sample_bundle` nhận `application/zip` raw body gồm
chính xác hai file nguyên bản `calls.csv` và `queues.csv`. Adapter kiểm tra SHA-256
của cả hai file, giữ queue ID/name, lọc history window, tính AHT từ answered
positive `handle_seconds` trước `week_start` và chỉ tạo zero rows trong requested
window có source coverage được pin. Queue filtering là optional; nếu bỏ `group_id` sẽ dùng
tất cả queue. Cần truyền `mode=history`, `week_start`, `history_start_date` và
`history_end_date` (exclusive); không dùng source SLA, employee fields, timezone
hay giờ vận hành.

Response có `mode`, `history_window`, `groups`, plain four-field `rows`,
`aht_by_group` và import-only `source` diagnostics. Với toàn bộ cửa sổ source
[2025-02-03, 2025-03-01) và cả năm queue, có 6.240 cells: 699 nonzero và 5.541
derived-zero cells; riêng 804 date/start buckets không có call ở queue nào.
Diagnostics không theo từng row và không được schedule endpoint xác minh lại.

Raw fixture attribution, source URL và hashes nằm tại
`backend/tests/data/database_sample/README.md`; parser tests dùng nguyên file
source. Không xem dataset này là đại diện cho vận hành thực.

## CSV export

Một dòng output là một employee/group assignment trong một working interval;
break slots không được xuất như coverage. Các cột gồm ngày, interval start/end,
group ID/name, shift start/end, break envelope và `Thời lượng ca (phút)`.
`shift_minutes` là elapsed duration gồm break và được lặp lại trên từng dòng,
nên không cộng cột này theo dòng để tính tổng. CSV có UTF-8 BOM, dùng
`csv.writer` và formula-injection guard.

## Kiểm thử

Chạy toàn bộ suite bằng:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
```

`TestClient` kiểm tra API trong tiến trình; suite không thay thế browser test,
frontend integration, user acceptance, auth hoặc deployment verification.
