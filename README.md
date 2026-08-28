# Hoá đơn PDF → Excel

Web demo chuyển hoá đơn Mira GoodFood dạng PDF thành một file Excel có công thức `SUM` ở dòng Tổng. Giao diện dark/light có kéo-thả, tìm kiếm, workspace Excel chỉnh trực tiếp, preview PDF nguồn cạnh bên và log xử lý.

## Chạy demo

1. Cài Python 3.11+.
2. Mở PowerShell trong thư mục này và chạy:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Mở http://127.0.0.1:8000. Kéo PDF vào vùng upload, bấm **Chuyển đổi**, kiểm tra preview và tải Excel.

## API demo

- `GET /api/v1/health`: kiểm tra server và parser engine.
- `POST /api/v1/receipts/upload`: nhận một hay nhiều trường `files` kiểu PDF.
- `GET /api/v1/receipts/jobs/{job_id}`: lấy trạng thái và thống kê job.
- `GET /api/v1/receipts/jobs/{job_id}/results`: lấy dữ liệu đã parse cho preview.
- `GET /api/v1/receipts/jobs/{job_id}/excel`: tải file Excel.
- `GET /api/v1/receipts/jobs/{job_id}/pdf/{source_index}`: xem PDF nguồn trong web.
- `PUT /api/v1/receipts/jobs/{job_id}/results`: lưu các ô đã chỉnh sửa và tạo lại Excel.
- `WS /ws/jobs/{job_id}`: trả trạng thái job hiện tại cho client.

Lịch sử job demo được lưu trong SQLite cục bộ (`receipt_jobs.sqlite3`). File Excel vẫn được lưu tạm trên máy chủ; khi cần triển khai nhiều người dùng, thay thư mục tạm bằng S3/MinIO và chuyển job dài sang Celery + Redis.

Trong tab **Excel trực tiếp**, bấm vào tên món hoặc số tiền để chỉnh. Bấm **Lưu chỉnh sửa** trước khi dùng nút xuất Excel; PDF ở khung bên phải tự đổi theo hoá đơn được chọn.

## Lưu ý demo

Parser đang tối ưu cho layout hoá đơn Mira GoodFood (mã đơn kiểu `GM-xxx`) có text chọn được trong PDF. PDF ảnh scan cần thêm OCR trước khi parse.

### Lỗi `DLL load failed ... charset_normalizer.cd`

Một số máy Windows có App Control chặn DLL tăng tốc của `charset-normalizer`. Dừng server, rồi chạy lại trong môi trường ảo đã kích hoạt:

```powershell
pip install --force-reinstall --no-cache-dir --no-binary=charset-normalizer charset-normalizer
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
```

Lệnh đầu cài bản Python thuần nên không còn cần nạp file `.pyd` bị policy chặn.
