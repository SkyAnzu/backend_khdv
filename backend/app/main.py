import threading
from datetime import date

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .export import pipeline_to_csv
from .database_sample import (
    DatabaseSampleImportError,
    DatabaseSampleImportResponse,
    parse_database_sample_bundle,
)
from .import_calls import (
    CallImportError,
    CallImportFormat,
    CallImportMode,
    CallImportRecord,
    parse_calls_import,
)
from .limits import (
    MAX_EXPORT_BODY_BYTES,
    MAX_SCHEDULE_BODY_BYTES,
    MAX_UPLOAD_BYTES,
)
from .pipeline import (
    PipelineDomainError,
    PipelineRequest,
    PipelineResponse,
    run_pipeline,
)
from .solver_v2 import MAX_GROUPS


app = FastAPI(
    title="MVP Xếp lịch nhân sự",
    version="0.1.0",
    description=(
        "API local prototype. /api/schedule-v2 là endpoint thử nghiệm pipeline, "
        "chưa phải contract "
        "sản phẩm đã chốt. /api/schedule-v2/export chỉ render CSV từ response "
        "caller gửi, không chạy pipeline/solver hay xác minh lịch. "
        "/api/calls/import chỉ parse rows CSV/XLSX, không chạy pipeline hoặc lưu file."
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


def _parse_database_bundle_with_limit(
    data: bytes,
    *,
    week_start: date,
    history_start_date: date,
    history_end_date: date,
    group_ids: list[str] | None,
) -> DatabaseSampleImportResponse:
    if not _CALL_IMPORT_SEMAPHORE.acquire(blocking=False):
        raise _CallImportBusy
    try:
        return parse_database_sample_bundle(
            data,
            week_start=week_start,
            history_start_date=history_start_date,
            history_end_date=history_end_date,
            group_ids=group_ids,
        )
    finally:
        _CALL_IMPORT_SEMAPHORE.release()


def _validate_import_headers(request: Request, file_format: CallImportFormat) -> None:
    content_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    expected_type = {
        "csv": "text/csv",
        "xlsx": _XLSX_CONTENT_TYPE,
        "database_sample_bundle": "application/zip",
    }[file_format]
    if content_type not in ("application/octet-stream", expected_type):
        raise HTTPException(status_code=415, detail="Unsupported import media type.")

    content_encoding = request.headers.get("content-encoding", "").strip().lower()
    if content_encoding not in ("", "identity"):
        raise HTTPException(
            status_code=415,
            detail="Compressed request bodies are not supported.",
        )


async def _read_bounded_body(
    request: Request, *, body_name: str, max_bytes: int
) -> bytes:
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            declared_size = int(declared_length, 10)
        except ValueError:
            declared_size = -1
        if declared_size > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{body_name} exceeds the configured size limit.",
            )

    content = bytearray()
    async for chunk in request.stream():
        if len(chunk) > max_bytes - len(content):
            raise HTTPException(
                status_code=413,
                detail=f"{body_name} exceeds the configured size limit.",
            )
        content.extend(chunk)
    return bytes(content)


def _validate_export_headers(request: Request) -> None:
    content_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="Unsupported export media type.")

    content_encoding = request.headers.get("content-encoding", "").strip().lower()
    if content_encoding not in ("", "identity"):
        raise HTTPException(
            status_code=415,
            detail="Compressed request bodies are not supported.",
        )


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


@app.post(
    "/api/schedule-v2",
    response_model=PipelineResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": PipelineRequest.model_json_schema()
                }
            },
        }
    },
)
async def schedule_v2(request: Request) -> PipelineResponse:
    """Run the provisional weekly pipeline API with the existing request model."""
    data = await _read_bounded_body(
        request,
        body_name="Schedule request",
        max_bytes=MAX_SCHEDULE_BODY_BYTES,
    )
    try:
        pipeline_request = PipelineRequest.model_validate_json(data)
    except ValidationError as error:
        raise RequestValidationError(
            [
                {**item, "loc": ("body", *item["loc"])}
                for item in error.errors(
                    include_input=False, include_context=False
                )
            ]
        ) from None
    try:
        return run_pipeline(pipeline_request)
    except PipelineDomainError as error:
        raise HTTPException(status_code=422, detail=error.public_message) from None


@app.post(
    "/api/calls/import",
    summary="Parse calls from a raw CSV or XLSX request body",
    description=(
        "Local prototype endpoint. Send CSV/XLSX bytes or an exact pinned "
        "Database Sample ZIP directly, not multipart form data. No filename "
        "inference or storage is performed."
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
                    "application/zip",
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
    group_id: list[str] | None = Query(
        default=None,
        max_length=MAX_GROUPS,
        description=(
            "Repeat once per selected queue for a database_sample_bundle; "
            "omit to select every source queue. Required for CSV/XLSX."
        ),
    ),
    week_start: date | None = Query(
        default=None, description="Required for database_sample_bundle."
    ),
    history_start_date: date | None = Query(
        default=None, description="Inclusive source-history start date."
    ),
    history_end_date: date | None = Query(
        default=None, description="Exclusive source-history end date."
    ),
) -> dict[str, object]:
    """Parse generic interval rows or the exact pinned source bundle."""
    _validate_import_headers(request, file_format)
    data = await _read_bounded_body(
        request, body_name="Import file", max_bytes=MAX_UPLOAD_BYTES
    )
    if file_format == "database_sample_bundle":
        if mode != "history":
            raise HTTPException(
                status_code=422, detail="Database Sample bundle mode must be history."
            )
        if week_start is None or history_start_date is None or history_end_date is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "week_start, history_start_date and history_end_date are required."
                ),
            )
        try:
            result = await run_in_threadpool(
                _parse_database_bundle_with_limit,
                data,
                week_start=week_start,
                history_start_date=history_start_date,
                history_end_date=history_end_date,
                group_ids=group_id,
            )
        except _CallImportBusy:
            raise HTTPException(
                status_code=429,
                detail="Another import is being parsed in this process.",
                headers={"Retry-After": "1"},
            ) from None
        except DatabaseSampleImportError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        return result.model_dump(mode="json")

    if group_id is None:
        raise HTTPException(status_code=422, detail="group_id is required for CSV/XLSX.")
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


@app.post(
    "/api/schedule-v2/export",
    summary="Export an existing schedule response as CSV",
    description=(
        "Local prototype endpoint. Send a PipelineResponse as JSON. It renders "
        "the caller-provided schedule without rerunning the pipeline or solver "
        "and does not independently verify its provenance, coverage, or constraints."
    ),
    response_class=Response,
    responses={
        200: {
            "description": "UTF-8 CSV attachment.",
            "content": {
                "text/csv": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "A PipelineResponse JSON object from an existing result.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/PipelineResponse"}
                }
            },
        }
    },
)
async def export_schedule(request: Request) -> Response:
    """Render a bounded caller-provided pipeline response as a CSV attachment."""
    _validate_export_headers(request)
    data = await _read_bounded_body(
        request, body_name="Export response", max_bytes=MAX_EXPORT_BODY_BYTES
    )
    try:
        result = await run_in_threadpool(PipelineResponse.model_validate_json, data)
    except ValidationError:
        raise HTTPException(
            status_code=422, detail="Invalid pipeline response."
        ) from None

    try:
        content = await run_in_threadpool(pipeline_to_csv, result)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None

    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="lich-lam-viec.csv"'
        },
    )
