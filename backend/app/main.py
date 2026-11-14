import threading

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .import_calls import (
    MAX_FILE_BYTES,
    CallImportError,
    CallImportFormat,
    CallImportMode,
    CallImportRecord,
    parse_calls_import,
)
from .models import ScheduleRequest, SolveResponse
from .pipeline import (
    PipelineDomainError,
    PipelineRequest,
    PipelineResponse,
    run_pipeline,
)
from .solver import solve_schedule
from .solver_v2 import MAX_GROUPS


app = FastAPI(
    title="MVP Xếp lịch nhân sự",
    version="0.1.0",
    description=(
        "API local prototype. /api/solver-preview giữ solver v1; "
        "/api/schedule-v2 là endpoint thử nghiệm pipeline, chưa phải contract "
        "sản phẩm đã chốt. /api/calls/import chỉ parse rows CSV/XLSX, không chạy "
        "pipeline hoặc lưu file."
    ),
)

_XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
# ponytail: one parse slot per process; use a bounded worker pool if throughput needs it.
_CALL_IMPORT_SEMAPHORE = threading.BoundedSemaphore(1)


class _CallImportBusy(Exception):
    pass


def _parse_calls_with_limit(
    data: bytes,
    *,
    file_format: CallImportFormat,
    mode: CallImportMode,
    group_ids: list[str],
) -> list[CallImportRecord]:
    if not _CALL_IMPORT_SEMAPHORE.acquire(blocking=False):
        raise _CallImportBusy
    try:
        return parse_calls_import(
            data,
            file_format=file_format,
            mode=mode,
            group_ids=group_ids,
        )
    finally:
        _CALL_IMPORT_SEMAPHORE.release()


def _validate_import_headers(request: Request, file_format: CallImportFormat) -> None:
    content_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    expected_type = "text/csv" if file_format == "csv" else _XLSX_CONTENT_TYPE
    if content_type not in ("application/octet-stream", expected_type):
        raise HTTPException(status_code=415, detail="Unsupported import media type.")

    content_encoding = request.headers.get("content-encoding", "").strip().lower()
    if content_encoding not in ("", "identity"):
        raise HTTPException(
            status_code=415,
            detail="Compressed request bodies are not supported.",
        )


async def _read_bounded_import_body(request: Request) -> bytes:
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            declared_size = int(declared_length, 10)
        except ValueError:
            declared_size = -1
        if declared_size > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Import file exceeds the 2 MiB size limit.",
            )

    content = bytearray()
    async for chunk in request.stream():
        if len(chunk) > MAX_FILE_BYTES - len(content):
            raise HTTPException(
                status_code=413,
                detail="Import file exceeds the 2 MiB size limit.",
            )
        content.extend(chunk)
    return bytes(content)


@app.exception_handler(RequestValidationError)
async def handle_request_validation(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    if request.url.path not in {"/api/schedule-v2", "/api/calls/import"}:
        return await request_validation_exception_handler(request, error)
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "loc": list(item["loc"]),
                    "msg": "Invalid value for this field.",
                    "type": item["type"],
                }
                for item in error.errors()
            ]
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/solver-preview", response_model=SolveResponse)
def solver_preview(request: ScheduleRequest) -> SolveResponse:
    """Run the week 1-2 solver through a small test/demo endpoint."""
    return solve_schedule(request)


@app.post("/api/schedule-v2", response_model=PipelineResponse)
def schedule_v2(request: PipelineRequest) -> PipelineResponse:
    """Run the provisional weekly pipeline API with the existing request model."""
    try:
        return run_pipeline(request)
    except PipelineDomainError as error:
        raise HTTPException(status_code=422, detail=error.public_message) from None


@app.post(
    "/api/calls/import",
    summary="Parse calls from a raw CSV or XLSX request body",
    description=(
        "Local prototype endpoint. Send the file bytes directly, not multipart "
        "form data. The selected file_format is explicit; no filename inference "
        "or storage is performed."
    ),
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "Raw file bytes; do not send multipart/form-data.",
            "content": {
                media_type: {"schema": {"type": "string", "format": "binary"}}
                for media_type in (
                    "application/octet-stream",
                    "text/csv",
                    _XLSX_CONTENT_TYPE,
                )
            },
        }
    },
)
async def import_calls_route(
    request: Request,
    file_format: CallImportFormat = Query(
        ..., description="Explicit parser selection; no filename inference."
    ),
    mode: CallImportMode = Query(...),
    group_id: list[str] = Query(
        ...,
        min_length=1,
        max_length=MAX_GROUPS,
        description="Repeat once per selected group; IDs must be unique.",
    ),
) -> dict[str, object]:
    """Parse bounded CSV/XLSX bytes into existing pipeline row models."""
    _validate_import_headers(request, file_format)
    data = await _read_bounded_import_body(request)
    try:
        records = await run_in_threadpool(
            _parse_calls_with_limit,
            data,
            file_format=file_format,
            mode=mode,
            group_ids=group_id,
        )
    except _CallImportBusy:
        raise HTTPException(
            status_code=429,
            detail="Another import is being parsed in this process.",
            headers={"Retry-After": "1"},
        ) from None
    except CallImportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    return {
        "mode": mode,
        "rows": [record.model_dump(mode="json") for record in records],
    }
