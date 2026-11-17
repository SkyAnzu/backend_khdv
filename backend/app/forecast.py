"""Deterministic same-weekday/start/group interval call-volume baseline."""

from .models import ForecastRequest, ForecastResponse, IntervalForecast


def forecast_calls(request: ForecastRequest) -> ForecastResponse:
    """Average observations inside the explicit window by weekday/start/group."""
    history_by_key: dict[tuple[int, str, str], list[int]] = {}
    for point in request.history:
        key = (point.date.weekday(), point.start, point.group_id)
        history_by_key.setdefault(key, []).append(point.calls)

    forecasts = []
    for target in sorted(
        request.targets, key=lambda point: (point.date, point.start, point.group_id)
    ):
        samples = history_by_key[(target.date.weekday(), target.start, target.group_id)]
        forecasts.append(
            IntervalForecast(
                date=target.date,
                start=target.start,
                group_id=target.group_id,
                forecast_calls=sum(samples) / len(samples),
                sample_count=len(samples),
            )
        )
    return ForecastResponse(forecasts=forecasts)
