"""Mock step generation — returns fixture data for dev/demo without an LLM."""

from backend.engine.worldgen.fixtures.mock_data import MOCK_GENERATORS


class MockStepGenerator:
    """Returns hardcoded fixture data, dispatched by step id."""

    def generate(self, step, world_state: dict, user_prompt: str, user_note: str = "") -> dict:
        handler = MOCK_GENERATORS.get(step.id)
        if handler:
            return handler(user_prompt, user_note)
        return {
            "_mock": True,
            "step": step.id,
            "prompt": user_prompt[:100],
            "note": user_note[:100],
        }
