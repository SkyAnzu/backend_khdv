import pytest
from pydantic import ValidationError

from backend.app.forecast import forecast_calls
from backend.app.models import ForecastRequest, ForecastTarget, HourlyCallObservation


def observation(date: str, hour: int = 8, group_id: str = "sales", calls: int = 0):
    return {"date": date, "hour": hour, "group_id": group_id, "calls": calls}


def target(date: str, hour: int = 8, group_id: str = "sales"):
    return {"date": date, "hour": hour, "group_id": group_id}


def request(history, targets):
    return ForecastRequest.model_validate({"history": history, "targets": targets})


def test_forecast_uses_only_matching_weekday_hour_and_group():
    result = forecast_calls(
        request(
            [
                observation("2026-10-05", calls=8),
                observation("2026-10-12", calls=12),
                observation("2026-10-06", calls=100),
                observation("2026-10-05", hour=9, calls=100),
                observation("2026-10-05", group_id="support", calls=100),
            ],
            [target("2026-10-19")],
        )
    )

    assert result.forecasts[0].forecast_calls == 10
    assert result.forecasts[0].sample_count == 2


def test_zero_calls_is_an_observation_not_missing():
    result = forecast_calls(
        request(
            [observation("2026-10-05", calls=0), observation("2026-10-12", calls=4)],
            [target("2026-10-19")],
        )
    )

    assert result.forecasts[0].forecast_calls == 2
    assert result.forecasts[0].sample_count == 2


def test_group_ids_are_trimmed_before_matching():
    result = forecast_calls(
        request(
            [
                observation("2026-10-05", group_id=" sales ", calls=2),
                observation("2026-10-12", group_id="sales", calls=4),
            ],
            [target("2026-10-19", group_id="sales")],
        )
    )

    assert result.forecasts[0].forecast_calls == 3


@pytest.mark.parametrize(
    ("history", "targets"),
    [
        ([], [target("2026-10-19")]),
        (
            [observation("2026-10-05", calls=1), observation("2026-10-12", calls=2)],
            [],
        ),
    ],
    ids=["empty-history", "empty-targets"],
)
def test_request_rejects_empty_history_or_targets(history, targets):
    with pytest.raises(ValidationError):
        request(history, targets)


@pytest.mark.parametrize(
    "history",
    [
        [observation("2026-10-06", calls=7)],
        [observation("2026-10-05", calls=7)],
    ],
    ids=["no-matching-observation", "one-matching-observation"],
)
def test_forecast_rejects_fewer_than_two_matching_samples(history):
    with pytest.raises(ValidationError):
        request(history, [target("2026-10-19")])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("date", "2026-10-5"),
        ("date", "2026-02-30"),
        ("hour", -1),
        ("hour", 24),
        ("hour", True),
        ("group_id", "   "),
        ("calls", -1),
        ("calls", 2.5),
        ("calls", True),
    ],
)
def test_observation_rejects_invalid_values(field, value):
    payload = observation("2026-10-05", calls=4)
    payload[field] = value

    with pytest.raises(ValidationError):
        HourlyCallObservation.model_validate(payload)


def test_observation_requires_calls_value():
    payload = observation("2026-10-05")
    del payload["calls"]

    with pytest.raises(ValidationError):
        HourlyCallObservation.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("date", "2026-10-5"),
        ("hour", 24),
        ("group_id", " "),
    ],
)
def test_target_rejects_invalid_values(field, value):
    payload = target("2026-10-19")
    payload[field] = value

    with pytest.raises(ValidationError):
        ForecastTarget.model_validate(payload)


@pytest.mark.parametrize("duplicate_kind", ["history", "target"])
def test_request_rejects_duplicate_keys(duplicate_kind):
    history = [observation("2026-10-05", calls=1), observation("2026-10-12", calls=2)]
    targets = [target("2026-10-19")]
    if duplicate_kind == "history":
        history[1] = history[0].copy()
    else:
        targets.append(target("2026-10-19"))

    with pytest.raises(ValidationError):
        request(history, targets)


def test_request_rejects_history_on_or_after_forecast_horizon():
    with pytest.raises(ValidationError):
        request(
            [
                observation("2026-10-05", calls=1),
                observation("2026-10-12", calls=2),
                observation("2026-10-19", calls=3),
            ],
            [target("2026-10-19")],
        )


def test_forecast_keeps_fractional_mean():
    result = forecast_calls(
        request(
            [observation("2026-10-05", calls=2), observation("2026-10-12", calls=3)],
            [target("2026-10-19")],
        )
    )

    assert result.forecasts[0].forecast_calls == 2.5


def test_forecast_results_are_sorted_independently_of_target_order():
    history = [
        observation(day, hour=9, group_id="sales", calls=calls)
        for day, calls in [("2026-10-05", 2), ("2026-10-12", 4)]
    ] + [
        observation(day, hour=8, group_id="support", calls=calls)
        for day, calls in [("2026-10-05", 6), ("2026-10-12", 8)]
    ]
    result = forecast_calls(
        request(
            history,
            [target("2026-10-19", hour=9), target("2026-10-19", hour=8, group_id="support")],
        )
    )

    assert [(item.hour, item.group_id) for item in result.forecasts] == [
        (8, "support"),
        (9, "sales"),
    ]


def test_chronological_holdout_mae_uses_training_weeks_only():
    training = [
        observation("2026-10-05", hour=8, calls=8),
        observation("2026-10-05", hour=9, calls=10),
        observation("2026-10-12", hour=8, calls=10),
        observation("2026-10-12", hour=9, calls=12),
    ]
    holdout_actuals = {(8, "sales"): 12, (9, "sales"): 9}
    result = forecast_calls(
        request(
            training,
            [target("2026-10-19", hour=9), target("2026-10-19", hour=8)],
        )
    )

    errors = [
        abs(holdout_actuals[(item.hour, item.group_id)] - item.forecast_calls)
        for item in result.forecasts
    ]
    assert errors == [3, 2]
    assert sum(errors) / len(errors) == 2.5
