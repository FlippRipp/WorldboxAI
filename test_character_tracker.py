import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def _load_backend():
    path = Path(__file__).parent / "modules" / "wb_character_tracker" / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_character_tracker_backend", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_sdk(reply: str, captured: dict):
    async def generate(prompt, model_preference="balanced", max_tokens=None):
        captured["prompt"] = prompt
        return reply

    return SimpleNamespace(llm=SimpleNamespace(generate=generate, _current_module=""))


def _state(history):
    return {
        "turn": 3,
        "history": history,
        "characters": {
            "default_player": {
                "name": "Aria",
                "gender": "female",
                "race": "human",
                "full_appearance": "Tall, dark braid, green eyes.",
                "personality": "Cautious and curious.",
            }
        },
        "module_configs": {},
        "module_data": {},
    }


def test_latest_scene_always_reaches_the_prompt():
    # Long earlier scenes used to push this turn's scene past the truncation
    # cap, so the change-detection LLM never saw the scene it was asked about.
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk("{}", captured)
    sentinel = "Aria's body reshapes itself; where she stood, a man now stands."
    history = ["Old scene. " * 300, "Older scene. " * 300, ("Filler prose. " * 200) + sentinel]

    asyncio.run(backend.on_librarian(_state(history), sdk))

    assert sentinel in captured["prompt"]
    assert "THIS TURN'S SCENE" in captured["prompt"]


def test_gender_change_is_recorded():
    backend = _load_backend()
    captured = {}
    reply = json.dumps({
        "gender": "male",
        "name": "Aric",
        "full_appearance": "Broad-shouldered, short dark hair, green eyes.",
        "change_note": "Aria was transformed into a man named Aric.",
    })
    sdk = _make_sdk(reply, captured)

    result = asyncio.run(backend.on_librarian(_state(["The ritual completes."]), sdk))

    assert "Gender: female" in captured["prompt"]
    update = result["character_update"]
    assert update["gender"] == "male"
    assert update["name"] == "Aric"
