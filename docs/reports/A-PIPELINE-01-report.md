# A-PIPELINE-01 — Báo cáo nối pipeline nội bộ

## Kết quả

Thêm `backend.app.pipeline` với hai mode: lịch sử → forecast → Erlang C → solver
v2, hoặc calls dự kiến → Erlang C → solver v2. Ma trận input được kiểm tra đủ
theo operating hour và nhóm; mỗi nhu cầu giờ được giữ nguyên ở hai slot 30 phút.
Response giữ forecast/sample count nếu có, service level Erlang C, requirements
từng slot và nguyên trạng status, phase status, warnings của solver.

Tham số Erlang C dùng chung cho các nhóm. Module từ chối input thiếu/trùng/sai,
history không trước tuần, giờ 23 không được solver hỗ trợ và staffing vượt giới
hạn 50 của solver; không thêm endpoint/API, database, import file hoặc phụ thuộc.
README có ví dụ tổng hợp hoàn chỉnh và lệnh chạy trên Windows.

## Kiểm tra

- Chạy lại khi cập nhật report (2026-11-14):
  `.\.venv\Scripts\python.exe -m pytest backend\tests\test_pipeline.py -q` —
  **25 passed in 0.42s**.
- Cùng lần xác minh:
  `.\.venv\Scripts\python.exe -m pytest backend\tests -q` —
  **131 passed in 0.65s**.
- Ví dụ README chạy với dữ liệu tổng hợp; solver trả `OPTIMAL`, nhu cầu 1 người
  cho 0.5 calls/giờ và `service_level=0.9609121154958824`.
- `git diff --check` sau khi cập nhật report — không có lỗi whitespace.

## Giới hạn và tự kiểm thử

`service_level` là ước lượng Erlang C riêng theo nhóm, không phải cam kết SLA
cho một pool đa kỹ năng. Không có API, frontend hoặc dữ liệu calls thực; task
đang chờ user tự kiểm thử và phản hồi, chưa được nghiệm thu.

Trên Windows, từ root repo backend, tạo môi trường/cài dependency theo README
nếu cần, sau đó chạy:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
```
