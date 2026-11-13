from fastapi import FastAPI

from .models import ScheduleRequest, SolveResponse
from .solver import solve_schedule


app = FastAPI(
    title="MVP Xếp lịch nhân sự",
    version="0.1.0",
    description="API thử nghiệm cho solver CP-SAT tuần 1-2; chưa phải API lịch tuần 3.",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/solver-preview", response_model=SolveResponse)
def solver_preview(request: ScheduleRequest) -> SolveResponse:
    """Run the week 1-2 solver through a small test/demo endpoint."""
    return solve_schedule(request)
