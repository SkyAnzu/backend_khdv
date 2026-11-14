from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .models import ScheduleRequest, SolveResponse
from .pipeline import (
    PipelineDomainError,
    PipelineRequest,
    PipelineResponse,
    run_pipeline,
)
from .solver import solve_schedule


app = FastAPI(
    title="MVP Xếp lịch nhân sự",
    version="0.1.0",
    description=(
        "API local prototype. /api/solver-preview giữ solver v1; "
        "/api/schedule-v2 là endpoint thử nghiệm pipeline, chưa phải contract "
        "sản phẩm đã chốt."
    ),
)


@app.exception_handler(RequestValidationError)
async def handle_request_validation(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    if request.url.path != "/api/schedule-v2":
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
