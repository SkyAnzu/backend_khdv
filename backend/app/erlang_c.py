"""Hourly Erlang C staffing estimate."""

import math

from .models import ErlangCRequest, ErlangCResponse, MAX_ERLANG_C_AGENTS


def calculate_required_staff(request: ErlangCRequest) -> ErlangCResponse:
    """Return the minimum agents meeting the requested hourly service level."""
    if request.calls_per_hour == 0:
        return ErlangCResponse(required_staff=0, service_level=None)

    offered_load = (request.calls_per_hour / 3600) * request.aht_seconds
    if not math.isfinite(offered_load):
        raise ValueError("offered load must be finite")

    first_stable_count = math.floor(offered_load) + 1
    if first_stable_count > request.max_agents:
        raise ValueError(
            f"offered load requires at least {first_stable_count} agents, "
            f"exceeding max_agents={request.max_agents}"
        )

    erlang_b = 1.0
    for agents in range(1, request.max_agents + 1):
        load_times_blocking = offered_load * erlang_b
        erlang_b = load_times_blocking / (agents + load_times_blocking)
        if agents < first_stable_count:
            continue

        # This is Erlang C rewritten to avoid subtracting offered_load/agents
        # from one when agents is only slightly above the offered load.
        probability_wait = (agents * erlang_b) / (
            agents - offered_load + offered_load * erlang_b
        )
        wait_exponent = (
            -(agents - offered_load)
            * request.waiting_seconds
            / request.aht_seconds
        )
        wait_factor = math.exp(wait_exponent)
        missed_probability = probability_wait * wait_factor
        service_level = 1 - missed_probability
        if missed_probability <= 1 - request.target_sla:
            return ErlangCResponse(
                required_staff=agents,
                service_level=service_level,
            )

    raise ValueError(
        f"target SLA not met within max_agents={request.max_agents} "
        f"(maximum supported: {MAX_ERLANG_C_AGENTS})"
    )
