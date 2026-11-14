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
injection; NFKC chỉ dùng để nhận diện marker full-width sau whitespace/control,
còn nội dung gốc được giữ nguyên. Text có lone surrogate bị từ chối bằng lỗi
`ValueError` chung, không đưa lại giá trị input.

[OWASP CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection)
ghi nhận full-width marker có thể được hiểu là công thức ở một số locale; đồng
thời các cách escape thông thường có thể mất tác dụng khi Microsoft Excel lưu
rồi mở lại file. Vì vậy dấu nháy đơn ở đây chỉ giảm rủi ro, không phải bảo đảm
an toàn với mọi bảng tính/locale. Không có Microsoft Excel hoặc locale cụ thể
nào được chạy kiểm thử trực tiếp.

Response hiện chỉ cung cấp tổng số phân công lệch kỹ năng/cảnh báo, không chỉ
ra phân công cụ thể nào lệch. Vì vậy CSV không tự gắn cờ khớp kỹ năng từng dòng.
Chưa có endpoint export, ghi file tự động hoặc frontend integration; hàm cũng
không xác minh độc lập độ phủ/ràng buộc hay nguồn gốc của response.

## Kiểm tra

- `.\.venv\Scripts\python.exe -m pytest backend\tests\test_export.py -q` —
  **50 passed**.
- `.\.venv\Scripts\python.exe -m pytest -q --ignore=backend/tests/test_import_calls.py`
  — **198 passed**; đây là kết quả có thể tái lập sau khi chỉ các file export
  trong commit này được checkout.
- `.\.venv\Scripts\python.exe -m pytest backend\tests -q` trên working tree
  hiện tại — **251 passed**, bao gồm **53 test** từ
  `backend/tests/test_import_calls.py` chưa commit. Con số này bao gồm thay đổi
  XLSX import đang chờ duyệt, không phải baseline sạch của commit export.
- `git diff --check` và `git diff --cached --check` — không có lỗi whitespace.
- Test dùng request pipeline planned-calls tổng hợp, kiểm tra round-trip CSV,
  ngày/giờ, nhiều nhóm trong một ca, CSV formula injection, status solver,
  marker full-width/NBSP, surrogate sai Unicode, response sai cấu trúc và lịch
  khả thi không có assignment.

## Tự kiểm thử trên Windows

Từ root repo backend, chạy toàn bộ suite bằng
`.\.venv\Scripts\python.exe -m pytest backend\tests -q`. README có ví dụ dùng
request sample để tạo chủ động `lich_mau.csv` trong thư mục hiện hành; ví dụ
chỉ chạy pipeline một lần rồi xuất response đó. Dùng sample tổng hợp, không
dùng roster hoặc dữ liệu cuộc gọi thật.

Đây là hàm nội bộ prototype; chưa xác nhận API/frontend integration hoặc chốt
product contract. Kết quả chưa được user tự kiểm thử/nghiệm thu.
