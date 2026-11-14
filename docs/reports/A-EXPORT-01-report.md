# A-EXPORT-01 — Báo cáo xuất lịch solver v2 sang CSV

## Kết quả

Thêm `backend.app.export.pipeline_to_csv(PipelineResponse)` để chuyển các
assignment đã có thành CSV, một dòng cho mỗi nhân viên/nhóm/slot 30 phút. Các
cột gồm mã nhân viên, ngày, giờ bắt đầu/kết thúc ca, giờ bắt đầu/kết thúc slot
và nhóm phục vụ. Ngày dùng `YYYY-MM-DD`, giờ dùng `HH:MM`; file có BOM UTF-8 để
phần mềm bảng tính đọc tiếng Việt.

Hàm chỉ render response hiện có, không chạy lại solver và không tự ghi file.
Chỉ `OPTIMAL`/`FEASIBLE` được xuất; lịch khả thi rỗng tạo CSV có hàng tiêu đề.
Response sai cấu trúc hoặc vượt giới hạn solver bị từ chối. ID nhân viên và
tên nhóm có thể được thêm dấu nháy đơn đầu giá trị để giảm nguy cơ formula
injection; khi đó dữ liệu CSV khác giá trị gốc có chủ ý.

Response hiện chỉ cung cấp tổng số phân công lệch kỹ năng/cảnh báo, không chỉ
ra phân công cụ thể nào lệch. Vì vậy CSV không tự gắn cờ khớp kỹ năng từng dòng.
Chưa có endpoint export, ghi file tự động hoặc frontend integration; hàm cũng
không xác minh độc lập độ phủ/ràng buộc hay nguồn gốc của response.

## Kiểm tra

- `.\.venv\Scripts\python.exe -m pytest backend\tests\test_export.py -q` —
  **36 passed**.
- `.\.venv\Scripts\python.exe -m pytest backend\tests -q` — **184 passed**.
- `git diff --check` và `git diff --cached --check` — không có lỗi whitespace.
- Test dùng request pipeline planned-calls tổng hợp, kiểm tra round-trip CSV,
  ngày/giờ, nhiều nhóm trong một ca, CSV formula injection, status solver,
  response sai cấu trúc và lịch khả thi không có assignment.

## Tự kiểm thử trên Windows

Từ root repo backend, chạy toàn bộ suite bằng
`.\.venv\Scripts\python.exe -m pytest backend\tests -q`. README có ví dụ dùng
request sample để tạo chủ động `lich_mau.csv` trong thư mục hiện hành; ví dụ
chỉ chạy pipeline một lần rồi xuất response đó. Dùng sample tổng hợp, không
dùng roster hoặc dữ liệu cuộc gọi thật.

Đây là hàm nội bộ prototype; chưa xác nhận API/frontend integration hoặc chốt
product contract. Kết quả chưa được user tự kiểm thử/nghiệm thu.
