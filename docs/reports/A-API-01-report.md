# A-API-01 — Báo cáo API local cho pipeline

## Kết quả

Thêm `POST /api/schedule-v2`, dùng trực tiếp `PipelineRequest` và
`PipelineResponse` của pipeline hiện có. Hai mode `history` và `planned_calls`
trả forecast nếu có, kết quả Erlang C theo giờ, requirements theo slot và toàn
bộ response solver gồm `OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, `UNKNOWN`,
`MODEL_INVALID`, `stage_two_status` và `warnings`. `INFEASIBLE` là kết quả solver
trong HTTP 200; request validation và lỗi miền tính toán trả HTTP 422. Lỗi
validation endpoint mới không echo input; lỗi lập trình không bị đổi thành 422.

`GET /health` và `POST /api/solver-preview` tiếp tục hoạt động. Thêm hai cặp
request/response JSON tổng hợp trong `backend/samples/pipeline-*.json`. README
hướng dẫn chạy Uvicorn, thử Swagger và gửi sample bằng PowerShell.
`/api/schedule-v2` là prototype local, chưa phải contract sản phẩm/frontend hoặc
deployment đã chốt.

## Kiểm tra

- `.\.venv\Scripts\python.exe -m pytest backend\tests\test_api.py -q` —
  **18 passed**.
- `.\.venv\Scripts\python.exe -m pytest backend\tests -q` — **147 passed**.
- `git diff --cached --check` — không có lỗi whitespace.
- TestClient xác nhận hai mode, zero demand, `INFEASIBLE`, passthrough cả status
  còn lại, validation/domain error 422 và tương thích validation của route cũ.
- Regression kiểm tra lỗi `ForecastRequest` thiếu mẫu trả 422 với thông báo an
  toàn phân biệt được thiếu mẫu, không lộ dữ liệu đầu vào; lỗi Erlang C giữ
  ngữ cảnh giới hạn an toàn nhưng không phản hồi group id.
- Live loopback smoke: `GET /health` trả `ok`; hai POST bằng
  `Invoke-RestMethod -Method Post -ContentType application/json -InFile`
  trả `OPTIMAL`; planned sample trả 0.5 calls/giờ và một assignment, history
  sample trả forecast 2.5 với hai mẫu.
- Hai file response sample được so khớp với response thực và xác thực dưới
  `PipelineResponse`.

## Giới hạn và tự kiểm thử

Endpoint chỉ dành cho thử nghiệm local; chưa có xác thực, lưu trữ dữ liệu,
upload file, frontend integration hoặc public deployment. Dùng sample/nhân viên
giả, không gửi roster hoặc calls thật lên server công khai.

Trên Windows, từ root repo backend, chạy
`.\.venv\Scripts\python.exe -m pytest backend\tests -q`. Làm theo mục **Chạy API
local** trong README để mở Swagger hoặc gửi hai request sample. Kết quả chưa
được user tự kiểm thử/nghiệm thu.
