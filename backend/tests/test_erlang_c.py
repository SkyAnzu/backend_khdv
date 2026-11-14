import math
from math import factorial

import pytest
from pydantic import ValidationError

from backend.app.erlang_c import calculate_required_staff
from backend.app.models import ErlangCRequest


def request(
    calls_per_hour: float = 12,
    aht_seconds: float = 300,
    target_sla: float = 0.8,
    waiting_seconds: float = 20,
    max_agents: int = 10_000,
) -> ErlangCRequest:
    return ErlangCRequest.model_validate(
        {
            "calls_per_hour": calls_per_hour,
            "aht_seconds": aht_seconds,
            "target_sla": target_sla,
            "waiting_seconds": waiting_seconds,
            "max_agents": max_agents,
        }
    )


def reference_service_level(
    calls_per_hour: float,
    aht_seconds: float,
    waiting_seconds: float,
    agents: int,
) -> float:
    """Small-case factorial reference, independent of Erlang B recursion."""
    load = calls_per_hour * aht_seconds / 3600
    assert agents > load
    wait_term = load**agents / (
        factorial(agents) * (1 - load / agents)
    )
    normalization = sum(
        load**k / factorial(k) for k in range(agents)
    ) + wait_term
    probability_wait = wait_term / normalization
    return 1 - probability_wait * math.exp(
        -(agents - load) * waiting_seconds / aht_seconds
    )


@pytest.mark.parametrize(
    ("calls", "aht", "waiting", "target"),
    [
        (0.5, 1800, 15, 0.8),
        (12, 300, 20, 0.8),
        (12.5, 300, 0, 0.9),
        (24, 400, 45, 0.95),
        (60, 180, 30, 0.9),
    ],
)
def test_matches_independent_formula_and_returns_minimum(calls, aht, waiting, target):
    result = calculate_required_staff(
        request(calls, aht, target, waiting, max_agents=100)
    )
    expected = reference_service_level(
        calls, aht, waiting, result.required_staff
    )

    assert result.service_level == pytest.approx(expected, abs=1e-12)
    assert result.service_level >= target
    assert result.required_staff == math.floor(calls * aht / 3600) + 1 or (
        reference_service_level(
            calls, aht, waiting, result.required_staff - 1
        )
        < target
    )


def test_requested_example_needs_three_agents():
    result = calculate_required_staff(request())

    assert result.required_staff == 3
    assert result.service_level == pytest.approx(
        1 - (1 / 11) * math.exp(-2 * 20 / 300), abs=1e-12
    )


def test_zero_calls_returns_zero_staff_and_not_applicable_sla():
    result = calculate_required_staff(
        request(calls_per_hour=0, aht_seconds=0, waiting_seconds=0)
    )

    assert result.required_staff == 0
    assert result.service_level is None


def test_aht_must_be_positive_when_calls_are_positive():
    with pytest.raises(ValidationError, match="AHT"):
        request(calls_per_hour=1, aht_seconds=0)


def test_target_equal_to_calculated_sla_is_inclusive_but_next_float_is_not():
    boundary_request = request(
        calls_per_hour=0.5,
        aht_seconds=3600,
        target_sla=0.5,
        waiting_seconds=0,
    )
    baseline = calculate_required_staff(boundary_request)
    at_boundary = calculate_required_staff(
        request(
            calls_per_hour=0.5,
            aht_seconds=3600,
            target_sla=baseline.service_level,
            waiting_seconds=0,
        )
    )
    just_above = calculate_required_staff(
        request(
            calls_per_hour=0.5,
            aht_seconds=3600,
            target_sla=math.nextafter(baseline.service_level, 1.0),
            waiting_seconds=0,
        )
    )

    assert baseline.required_staff == 1
    assert baseline.service_level == 0.5
    assert at_boundary.required_staff == baseline.required_staff
    assert just_above.required_staff > baseline.required_staff


def test_rounding_near_one_does_not_accept_a_missed_target():
    target = math.nextafter(1.0, 0.0)
    waiting = 259_000
    one_agent_missed_probability = 0.5 * math.exp(-0.5 * waiting / 3600)
    result = calculate_required_staff(
        request(
            calls_per_hour=0.5,
            aht_seconds=3600,
            target_sla=target,
            waiting_seconds=waiting,
        )
    )

    assert one_agent_missed_probability > 1 - target
    assert result.required_staff == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("calls_per_hour", -1),
        ("calls_per_hour", math.nan),
        ("calls_per_hour", math.inf),
        ("calls_per_hour", True),
        ("calls_per_hour", "12"),
        ("aht_seconds", -1),
        ("aht_seconds", math.nan),
        ("aht_seconds", math.inf),
        ("aht_seconds", False),
        ("target_sla", 0),
        ("target_sla", 1),
        ("target_sla", math.nan),
        ("target_sla", math.inf),
        ("target_sla", True),
        ("waiting_seconds", -1),
        ("waiting_seconds", math.nan),
        ("waiting_seconds", math.inf),
        ("waiting_seconds", False),
        ("max_agents", 0),
        ("max_agents", True),
        ("max_agents", 10_001),
    ],
)
def test_request_rejects_invalid_numeric_values(field, value):
    values = request().model_dump()
    values[field] = value

    with pytest.raises(ValidationError):
        ErlangCRequest.model_validate(values)


def test_rejects_non_finite_offered_load():
    with pytest.raises(ValueError, match="offered load"):
        calculate_required_staff(
            request(calls_per_hour=1e308, aht_seconds=1e308)
        )


def test_max_agents_bounds_work_and_reports_unmet_target():
    with pytest.raises(ValueError, match="max_agents"):
        calculate_required_staff(request(max_agents=2))

    with pytest.raises(ValueError, match="max_agents"):
        calculate_required_staff(
            request(calls_per_hour=10_001, aht_seconds=3600)
        )


def test_large_stable_load_remains_numerically_finite():
    result = calculate_required_staff(
        request(
            calls_per_hour=999,
            aht_seconds=3600,
            target_sla=0.5,
            waiting_seconds=3600,
            max_agents=1000,
        )
    )

    assert result.required_staff == 1000
    assert math.isfinite(result.service_level)
    assert result.service_level >= 0.5
