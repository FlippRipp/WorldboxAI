import asyncio
from backend.engine.state import WorldState
from backend.engine.graph import EngineGraph, VETO_MAX_RETRIES
from backend.engine.registry import ModuleRegistry
from backend.sdk.mock_sdk import ValidationVeto


def test_check_veto_retry_allowed():
    """When retries < max, _check_veto returns 'rewrite'."""
    registry = ModuleRegistry("modules")
    engine = EngineGraph(registry)

    state = WorldState(
        active_save_id="test", input_text="test", turn=0, history=[],
        chat_messages=[], module_data={}, module_configs={}, prompt_pipeline=[],
        current_context=[], needs_rewrite=True, veto_retries=0, veto_reason="reason",
    )

    result = engine._check_veto(state)
    assert result == "rewrite"
    assert state.get("needs_rewrite") is True


def test_check_veto_max_retries_exhausted():
    """After VETO_MAX_RETRIES, _check_veto returns 'librarian' and clears state."""
    registry = ModuleRegistry("modules")
    engine = EngineGraph(registry)

    state = WorldState(
        active_save_id="test", input_text="test", turn=0, history=[],
        chat_messages=[], module_data={}, module_configs={}, prompt_pipeline=[],
        current_context=[], needs_rewrite=True, veto_retries=VETO_MAX_RETRIES,
        veto_reason="reason",
    )

    result = engine._check_veto(state)
    assert result == "librarian"
    assert state.get("needs_rewrite") is False
    assert state.get("veto_reason") is None


def test_check_veto_no_rewrite_needed():
    """When needs_rewrite=False, _check_veto returns 'librarian'."""
    registry = ModuleRegistry("modules")
    engine = EngineGraph(registry)

    state = WorldState(
        active_save_id="test", input_text="test", turn=0, history=[],
        chat_messages=[], module_data={}, module_configs={}, prompt_pipeline=[],
        current_context=[], needs_rewrite=False, veto_retries=0, veto_reason=None,
    )

    result = engine._check_veto(state)
    assert result == "librarian"


def test_healthy_character_no_veto():
    """When HP > 0, on_validate_output should not raise."""
    char_data = {
        "stats": {"strength": 10}, "hp": 85, "max_hp": 85, "level": 1, "xp": 0,
    }
    state = {"module_data": {"wb_core_rpg": char_data}}
    story = "You swing your sword and strike true!"

    async def _run():
        from modules.wb_core_rpg.backend import on_validate_output
        sdk = _make_sdk()
        await on_validate_output(story, state, sdk)

    asyncio.run(_run())


def test_unconscious_veto_raises():
    """When HP=0 and physical action words present, veto is raised."""
    char_data = {
        "stats": {"strength": 10}, "hp": 0, "max_hp": 85, "level": 1, "xp": 0,
    }
    state = {"module_data": {"wb_core_rpg": char_data}}
    story = "Despite your wounds, you swing your blade and charge the enemy."

    raised = False

    async def _run():
        nonlocal raised
        from modules.wb_core_rpg.backend import on_validate_output
        sdk = _make_sdk()
        try:
            await on_validate_output(story, state, sdk)
        except ValidationVeto:
            raised = True

    asyncio.run(_run())
    assert raised


def test_unconscious_mental_narration_allowed():
    """When HP=0 but no physical action words, veto does NOT raise."""
    char_data = {
        "stats": {"strength": 10}, "hp": 0, "max_hp": 85, "level": 1, "xp": 0,
    }
    state = {"module_data": {"wb_core_rpg": char_data}}
    story = "You drift in darkness, fragments of memory swirling around you..."

    async def _run():
        from modules.wb_core_rpg.backend import on_validate_output
        sdk = _make_sdk()
        await on_validate_output(story, state, sdk)

    asyncio.run(_run())


def test_no_char_data_no_veto():
    """When module has no character data, veto is skipped."""
    state = {"module_data": {}}
    story = "You swing your sword!"

    async def _run():
        from modules.wb_core_rpg.backend import on_validate_output
        sdk = _make_sdk()
        await on_validate_output(story, state, sdk)

    asyncio.run(_run())


def _make_sdk():
    class Sdk:
        ValidationVeto = ValidationVeto
    return Sdk()


if __name__ == "__main__":
    test_check_veto_retry_allowed()
    test_check_veto_max_retries_exhausted()
    test_check_veto_no_rewrite_needed()
    test_healthy_character_no_veto()
    test_unconscious_veto_raises()
    test_unconscious_mental_narration_allowed()
    test_no_char_data_no_veto()
    print("All validation veto tests passed!")
