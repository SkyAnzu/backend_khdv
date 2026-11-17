import pytest
from pydantic import ValidationError

from backend.app.forecast import forecast_calls
from backend.app.models import (
    CallIntervalObservation,
    ForecastRequest,
    ForecastTarget,
)


WINDOW = {"start_date": "2026-10-01", "end_date": "2026-10-19"}


def observation(day, start="08:00", group_id="sales", calls=0):
    return {
        "date": day,
        "start": start,
        "group_id": group_id,
        "calls": calls,
    }


def target(day, start="08:00", group_id="sales"):
    return {"date": day, "start": start, "group_id": group_id}


def request(history, targets, window=None):
    return ForecastRequest.model_validate(
        {
            "history_window": WINDOW if window is None else window,
            "history": history,
            "targets": targets,
        }
    )


def test_forecast_uses_matching_weekday_start_and_group():
    result = forecast_calls(
        request(
            [
                observation("2026-10-05", calls=8),
                observation("2026-10-12", calls=12),
                observation("2026-10-06", calls=100),
                observation("2026-10-05", start="08:30", calls=100),
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
            [
                observation("2026-10-05", calls=0),
                observation("2026-10-12", calls=4),
            ],
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
            [target("2026-10-19")],
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
def test_forecast_rejects_fewer_than_two_matching_dates(history):
    with pytest.raises(ValidationError):
        request(history, [target("2026-10-19")])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("date", "2026-10-5"),
        ("date", "2026-02-30"),
        ("start", "08:15"),
        ("start", "24:00"),
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
        CallIntervalObservation.model_validate(payload)


def test_observation_requires_calls_value():
    payload = observation("2026-10-05")
    del payload["calls"]

    with pytest.raises(ValidationError):
        CallIntervalObservation.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("date", "2026-10-5"),
        ("start", "24:00"),
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
    history = [
        observation("2026-10-05", calls=1),
        observation("2026-10-12", calls=2),
    ]
    targets = [target("2026-10-19")]
    if duplicate_kind == "history":
        history[1] = history[0].copy()
    else:
        targets.append(target("2026-10-19"))

    with pytest.raises(ValidationError):
        request(history, targets)


def test_request_rejects_history_outside_explicit_window_or_forecast_horizon():
    for invalid_history in (
        [
            observation("2026-09-28", calls=1),
            observation("2026-10-05", calls=2),
        ],
        [
            observation("2026-10-05", calls=1),
            observation("2026-10-19", calls=2),
        ],
    ):
        with pytest.raises(ValidationError):
            request(invalid_history, [target("2026-10-19")])


def test_forecast_keeps_fractional_mean():
    result = forecast_calls(
        request(
            [
                observation("2026-10-05", calls=2),
                observation("2026-10-12", calls=3),
            ],
            [target("2026-10-19")],
        )
    )

    assert result.forecasts[0].forecast_calls == 2.5


def test_forecast_results_are_sorted_independently_of_target_order():
    history = [
        observation(day, start="09:00", calls=calls)
        for day, calls in [("2026-10-05", 2), ("2026-10-12", 4)]
    ] + [
        observation(day, start="08:00", group_id="support", calls=calls)
        for day, calls in [("2026-10-05", 6), ("2026-10-12", 8)]
    ]
    result = forecast_calls(
        request(
            history,
            [
                target("2026-10-19", start="09:00"),
                target("2026-10-19", group_id="support"),
            ],
        )
    )

    assert [(item.start, item.group_id) for item in result.forecasts] == [
        ("08:00", "support"),
        ("09:00", "sales"),
    ]


def test_chronological_holdout_mae_uses_training_weeks_only():
    training = [
        observation("2026-10-05", start="08:00", calls=8),
        observation("2026-10-05", start="08:30", calls=10),
        observation("2026-10-12", start="08:00", calls=10),
        observation("2026-10-12", start="08:30", calls=12),
    ]
    holdout_actuals = {"08:00": 12, "08:30": 9}
    result = forecast_calls(
        request(
            training,
            [
                target("2026-10-19", start="08:30"),
                target("2026-10-19", start="08:00"),
            ],
        )
    )

    errors = [
        abs(holdout_actuals[item.start] - item.forecast_calls)
        for item in result.forecasts
    ]
    assert errors == [3, 2]
    assert sum(errors) / len(errors) == 2.5
