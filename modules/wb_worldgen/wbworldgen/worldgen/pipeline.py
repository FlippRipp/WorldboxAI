"""Pure pipeline helpers: dependency ordering + chain-context construction.

Kept as free functions so the facade (which owns the step registry state) and
tests can reuse them without a stateful orchestrator object.
"""


def resolve_order(steps: dict) -> list[str]:
    """Topologically order step ids by their ``after`` dependency.

    Raises ValueError on a circular or missing dependency.
    """
    resolved: list[str] = []
    remaining = set(steps.keys())

    while remaining:
        placed = False
        for step_id in list(remaining):
            step = steps[step_id]
            if step.after is None or step.after in resolved:
                resolved.append(step_id)
                remaining.remove(step_id)
                placed = True
        if not placed and remaining:
            unresolved = ", ".join(remaining)
            raise ValueError(f"Circular or missing dependency in world steps: {unresolved}")

    return resolved


def build_chain_context(ordered_ids: list[str], world_state: dict, up_to_step_id: str,
                        steps: dict = None) -> dict:
    """Collect prior approved-step data up to (but excluding) ``up_to_step_id``.

    When ``steps`` is given, each step's ``context_view`` shapes what downstream
    prompts see (so bulky UI/procedural payloads stay out of LLM context).
    """
    context = {}
    for sid in ordered_ids:
        if sid == up_to_step_id:
            break
        data = world_state.get("steps", {}).get(sid, {}).get("data")
        if data:
            step = (steps or {}).get(sid)
            if step is not None and hasattr(step, "context_view"):
                try:
                    data = step.context_view(data)
                except Exception:
                    pass  # fall back to the raw data
            context[sid] = data
    return context
