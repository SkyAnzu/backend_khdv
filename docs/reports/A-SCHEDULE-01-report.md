# A-SCHEDULE-01 — Báo cáo solver tuần v2

## Kết quả

Đã thêm module Python nội bộ `backend.app.solver_v2` với validation input theo
tuần Monday–Sunday, operating slots 30 phút, ca 90 phút, availability tường
minh, ngày vắng, giới hạn phút/tuần và số ngày nghỉ tối thiểu tùy chọn. Nhu cầu
phải khai báo đủ từng cặp slot/nhóm; số 0 khác với thiếu dòng. Mỗi nhân viên
chỉ phục vụ một nhóm trong một slot, có thể làm nhiều ca/ngày nếu không chồng
nhau và các ràng buộc cho phép.

CP-SAT tối ưu tuần tự person-slots lệch kỹ năng rồi tổng person-slots, không
dùng weighted objective hoặc chi phí lương. Skill fallback chỉ hoạt động khi
được bật và tạo warning; output cũng báo person-slots vượt nhu cầu. Status
`OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, `UNKNOWN` và `MODEL_INVALID` được phân biệt;
`stage_two_status` giữ kết quả riêng của bước tối ưu thứ hai khi cần.

Solver v1 và `/api/solver-preview` không đổi; v2 chưa có route và chưa tích hợp
forecast, Erlang C hay frontend.

## Kiểm tra

- `.\.venv\Scripts\python.exe -m pytest backend\tests\test_solver_v2.py -q` —
  **38 passed**.
- `.\.venv\Scripts\python.exe -m pytest backend\tests -q` — **106 passed**,
  gồm test solver v1 và API.
- Ví dụ gọi trực tiếp trong README được chạy từ PowerShell; fixture một nhân
  viên trả `OPTIMAL`, một ca 90 phút và 3 employee-slots.

## Giới hạn và tự kiểm tra

Chưa hỗ trợ nghỉ giữa ca, ca qua ngày, múi giờ/DST, fairness, chi phí/lương hay
tích hợp product flow/API. Để tự chạy trên Windows, mở PowerShell tại root repo
backend và chạy:

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests\test_solver_v2.py -q
.\.venv\Scripts\python.exe -m pytest backend\tests -q
```

Kết quả trên là kiểm tra bằng fixture trong workspace triển khai, không thay
cho user test hoặc nghiệm thu.
