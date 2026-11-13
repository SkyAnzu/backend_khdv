"""Deterministic same-weekday/hour/group call-volume baseline."""

from .models import ForecastRequest, ForecastResponse, HourlyForecast


def forecast_calls(request: ForecastRequest) -> ForecastResponse:
    """Average prior observations with the same weekday, hour, and group."""
    history_by_key: dict[tuple[int, int, str], list[int]] = {}
    for point in request.history:
        key = (point.date.weekday(), point.hour, point.group_id)
        history_by_key.setdefault(key, []).append(point.calls)

    forecasts = []
    for target in sorted(
        request.targets, key=lambda point: (point.date, point.hour, point.group_id)
    ):
        samples = history_by_key[
            (target.date.weekday(), target.hour, target.group_id)
        ]
        forecasts.append(
            HourlyForecast(
                date=target.date,
                hour=target.hour,
                group_id=target.group_id,
                forecast_calls=sum(samples) / len(samples),
                sample_count=len(samples),
            )
        )
    return ForecastResponse(forecasts=forecasts)
