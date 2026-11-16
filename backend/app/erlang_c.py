"""Hourly Erlang C staffing estimate."""

import math
from decimal import Decimal, localcontext

from .models import ErlangCRequest, ErlangCResponse, MAX_ERLANG_C_AGENTS


def calculate_required_staff(request: ErlangCRequest) -> ErlangCResponse:
    """Return the minimum agents meeting the requested hourly service level."""
    if request.calls_per_hour == 0:
        return ErlangCResponse(required_staff=0, service_level=None)

    offered_load = (request.calls_per_hour / 3600) * request.aht_seconds
    if not math.isfinite(offered_load):
        raise ValueError("offered load must be finite")

    with localcontext() as context:
        context.prec = 60
        load = (
            Decimal(str(request.calls_per_hour))
            * Decimal(str(request.aht_seconds))
            / Decimal(3600)
        )
        target_sla = Decimal(str(request.target_sla))
        aht_seconds = Decimal(str(request.aht_seconds))
        waiting_seconds = Decimal(str(request.waiting_seconds))

    first_stable_count = int(load) + 1
    if first_stable_count > request.max_agents:
        raise ValueError(
            f"offered load requires at least {first_stable_count} agents, "
            f"exceeding max_agents={request.max_agents}"
        )

    with localcontext() as context:
        # Inputs are binary floats; str() uses their shortest round-trip decimal.
        # Decimal arithmetic avoids losing the SLA boundary to 1 - target_sla.
        context.prec = 60
        erlang_b = Decimal(1)
        for agents in range(1, request.max_agents + 1):
            load_times_blocking = load * erlang_b
            erlang_b = load_times_blocking / (agents + load_times_blocking)
            if agents < first_stable_count:
                continue

            # Erlang C form avoids subtracting offered_load/agents near stability.
            probability_wait = (agents * erlang_b) / (
                agents - load + load * erlang_b
            )
            wait_exponent = (
                -(agents - load) * waiting_seconds / aht_seconds
            )
            missed_probability = probability_wait * wait_exponent.exp()
            if missed_probability <= Decimal(1) - target_sla:
                return ErlangCResponse(
                    required_staff=agents,
                    service_level=float(Decimal(1) - missed_probability),
                )

    raise ValueError(
        f"target SLA not met within max_agents={request.max_agents} "
        f"(maximum supported: {MAX_ERLANG_C_AGENTS})"
    )
