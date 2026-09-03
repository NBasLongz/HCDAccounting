"""FastAPI application for the PDF-to-Excel demo."""
from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from excel_export import export
from job_store import get_excel_path, get_job, recent_jobs, save_job
from parser import Receipt, Row, parse_pdf

BASE_DIR = Path(__file__).parent
# Keep uploads outside the watched source folder. Otherwise uvicorn --reload
# restarts whenever a user uploads a PDF.
JOB_STORAGE = Path(tempfile.gettempdir()) / "hcdaccounting" / "jobs"
JOB_STORAGE.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="HCDAccounting — Hoá đơn PDF → Excel", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
JOBS: dict[str, str] = {}


async def save_upload(upload: UploadFile, path: Path) -> None:
    path.write_bytes(await upload.read())


@app.get("/api/health")
@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "engine": "pdfplumber + openpyxl", "storage": "sqlite"}


@app.post("/api/process")
@app.post("/api/v1/receipts/upload")
async def process(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(400, "Chưa có file nào được tải lên")
    job_id = uuid.uuid4().hex
    job_dir = JOB_STORAGE / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    receipts, errors, source_files = [], [], []
    for source_index, upload in enumerate(files):
        filename = upload.filename or "file.pdf"
        if not filename.lower().endswith(".pdf"):
            errors.append(f"{filename}: không phải file PDF, đã bỏ qua")
            continue
        path = job_dir / f"{source_index:03d}.pdf"
        await save_upload(upload, path)
        try:
            parsed = parse_pdf(str(path))
            if not parsed or not any(receipt.rows for receipt in parsed):
                raise ValueError("không nhận ra nội dung hoá đơn Mira GoodFood")
            for receipt in parsed:
                receipt.display_name = filename if len(parsed) == 1 else f"{filename} — {receipt.receipt_id}"
                receipt.source_file_index = source_index
            receipts.extend(parsed)
            source_files.append({"index": source_index, "name": filename})
        except Exception as exc:
            errors.append(f"{filename}: không thể đọc PDF ({exc})")
    if not receipts:
        raise HTTPException(422, {"message": "Không trích xuất được hoá đơn nào.", "errors": errors})
    out_path = os.path.join(tempfile.gettempdir(), f"hcd_export_{job_id}.xlsx")
    export(receipts, out_path)
    result = build_result(job_id, receipts, errors, source_files)
    JOBS[job_id] = out_path
    save_job(job_id, result, out_path)
    return result


def build_result(job_id: str, receipts: list[Receipt], errors: list[str], source_files: list[dict]) -> dict:
    total_revenue = sum(row.amount or 0 for receipt in receipts for row in receipt.rows)
    return {"job_id": job_id, "status": "completed", "errors": errors, "source_files": source_files, "logs": [f"Đã nhận {len(source_files)} file", "Đang đọc text PDF", f"Đã trích xuất {len(receipts)} hoá đơn", "Đã tạo file Excel"], "stats": {"receipt_count": len(receipts), "item_count": sum(row.type == "item" for receipt in receipts for row in receipt.rows), "total_discount": sum(row.amount or 0 for receipt in receipts for row in receipt.rows if row.type == "discount"), "total_revenue": total_revenue}, "receipts": [{"display_name": receipt.display_name, "receipt_id": receipt.receipt_id, "source_file_index": getattr(receipt, "source_file_index", 0), "pdf_total": receipt.pdf_total, "parsed_total": sum(row.amount or 0 for row in receipt.rows), "is_valid": receipt.pdf_total is None or round(receipt.pdf_total) == round(sum(row.amount or 0 for row in receipt.rows)), "rows": [asdict(row) for row in receipt.rows]} for receipt in receipts]}


@app.get("/api/download/{job_id}")
@app.get("/api/v1/receipts/jobs/{job_id}/excel")
async def download(job_id: str):
    path = JOBS.get(job_id) or get_excel_path(job_id)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "File kết quả không còn tồn tại")
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename="HCDAccounting_export.xlsx")


@app.get("/api/v1/receipts/jobs")
async def history():
    return {"jobs": recent_jobs()}


@app.get("/api/v1/receipts/jobs/{job_id}")
async def job_status(job_id: str):
    result = get_job(job_id)
    if not result:
        raise HTTPException(404, "Không tìm thấy job")
    return {key: result[key] for key in ("job_id", "status", "created_at", "stats", "errors")}


@app.get("/api/v1/receipts/jobs/{job_id}/results")
@app.get("/api/v1/receipts/jobs/{job_id}/preview")
async def job_results(job_id: str):
    result = get_job(job_id)
    if not result:
        raise HTTPException(404, "Không tìm thấy job")
    return result


@app.get("/api/v1/receipts/jobs/{job_id}/pdf/{source_index}")
async def source_pdf(job_id: str, source_index: int):
    job = get_job(job_id)
    if not job or source_index not in {item["index"] for item in job.get("source_files", [])}:
        raise HTTPException(404, "Không tìm thấy PDF nguồn")
    path = JOB_STORAGE / job_id / f"{source_index:03d}.pdf"
    if not path.exists():
        raise HTTPException(404, "PDF nguồn không còn tồn tại")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.put("/api/v1/receipts/jobs/{job_id}/results")
async def update_job_results(job_id: str, payload: dict = Body(...)):
    old = get_job(job_id)
    if not old:
        raise HTTPException(404, "Không tìm thấy job")
    try:
        receipts = []
        for data in payload.get("receipts", []):
            receipt = Receipt(
                receipt_id=str(data["receipt_id"]),
                display_name=str(data["display_name"]),
                pdf_total=data.get("pdf_total"),
                rows=[Row(type=str(row["type"]), label=str(row["label"]), amount=(float(row["amount"]) if row.get("amount") is not None else None)) for row in data.get("rows", [])],
            )
            receipt.source_file_index = int(data.get("source_file_index", 0))
            receipts.append(receipt)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, f"Dữ liệu chỉnh sửa không hợp lệ: {exc}") from exc
    if not receipts:
        raise HTTPException(422, "Cần có ít nhất một hoá đơn")
    path = JOBS.get(job_id) or get_excel_path(job_id)
    if not path:
        raise HTTPException(404, "Không tìm thấy file Excel của job")
    export(receipts, path)
    result = build_result(job_id, receipts, old.get("errors", []), old.get("source_files", []))
    result["logs"].append("Đã lưu chỉnh sửa từ bảng web và tạo lại Excel")
    JOBS[job_id] = path
    save_job(job_id, result, path)
    return result


@app.websocket("/ws/jobs/{job_id}")
async def job_progress(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        result = get_job(job_id)
        await websocket.send_json({"job_id": job_id, "status": result["status"] if result else "not_found", "progress": 100 if result else 0})
    except WebSocketDisconnect:
        return
    finally:
        await websocket.close()


app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
