"""Time Tracker -- estimates in-world time elapsed per turn and informs the Storyteller of the current date/time."""

TIME_OF_DAY_BANDS = [
    (0, 5, "the dead of night"),
    (5, 8, "early morning"),
    (8, 12, "morning"),
    (12, 14, "midday"),
    (14, 18, "afternoon"),
    (18, 21, "evening"),
    (21, 24, "night"),
]


def _config(state: dict) -> dict:
    return state.get("module_configs", {}).get("wb_time_tracker", {})


def _module_data(state: dict) -> dict:
    return state.get("module_data", {}).get("wb_time_tracker", {})


def _default_clock(config: dict) -> dict:
    return {
        "year": config.get("starting_year", 1),
        "month": config.get("starting_month", 1),
        "day": config.get("starting_day", 1),
        "hour": config.get("starting_hour", 8),
        "minute": 0,
        "total_minutes_elapsed": 0,
    }


def _get_clock(state: dict) -> dict:
    config = _config(state)
    data = _module_data(state)
    clock = data.get("clock")
    if not clock:
        return _default_clock(config)
    return dict(clock)


def _time_of_day(hour: int) -> str:
    for start, end, label in TIME_OF_DAY_BANDS:
        if start <= hour < end:
            return label
    return "night"


def _format_clock(clock: dict) -> str:
    return (
        f"Year {clock['year']}, Month {clock['month']}, Day {clock['day']} -- "
        f"{clock['hour']:02d}:{clock['minute']:02d} ({_time_of_day(clock['hour'])})"
    )


async def on_render_prompt_block(block: dict, state: dict, sdk) -> dict | None:
    block_id = block.get("id", "")
    if block_id != "time_context":
        return None

    clock = _get_clock(state)
    content = f"""<time_context>
Current in-world date and time: {_format_clock(clock)}.
Use this to inform lighting, NPC schedules, weather, and pacing in the narration. Do not state the exact clock numbers to the player unless they check a timepiece or ask; instead reflect the time of day naturally (e.g. through light, activity, or atmosphere).
</time_context>"""

    return {"content": content}


async def on_mutate_state(mutation: dict, state: dict, sdk) -> dict | None:
    config = _config(state)
    data = dict(_module_data(state))
    clock = _get_clock(state)

    minutes_elapsed = mutation.get("time_elapsed_minutes", 0)
    try:
        minutes_elapsed = int(minutes_elapsed)
    except (TypeError, ValueError):
        minutes_elapsed = 0
    if minutes_elapsed < 0:
        minutes_elapsed = 0

    days_per_month = config.get("days_per_month", 30)
    months_per_year = config.get("months_per_year", 12)

    total_minutes = clock["hour"] * 60 + clock["minute"] + minutes_elapsed
    extra_days, remaining_minutes = divmod(total_minutes, 24 * 60)
    clock["hour"], clock["minute"] = divmod(remaining_minutes, 60)

    day = clock["day"] + extra_days
    month = clock["month"]
    year = clock["year"]

    while day > days_per_month:
        day -= days_per_month
        month += 1
        if month > months_per_year:
            month -= months_per_year
            year += 1

    clock["day"] = day
    clock["month"] = month
    clock["year"] = year
    clock["total_minutes_elapsed"] = data.get("clock", {}).get("total_minutes_elapsed", 0) + minutes_elapsed

    data["clock"] = clock
    data["last_elapsed_minutes"] = minutes_elapsed

    print(f"[Time Tracker] +{minutes_elapsed}m -> {_format_clock(clock)}")

    return {"module_data": {"wb_time_tracker": data}}


async def on_command_time(args: list[str], state: dict, sdk) -> dict:
    clock = _get_clock(state)
    data = _module_data(state)
    message = (
        f"[Time] {_format_clock(clock)}\n"
        f"Total time elapsed since the story began: {data.get('clock', {}).get('total_minutes_elapsed', 0)} minutes"
    )
    return {"message": message, "signal": "end_turn"}
