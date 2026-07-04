import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def _load_backend():
    path = Path(__file__).parent / "modules" / "wb_core_rpg" / "backend.py"
    spec = importlib.util.spec_from_file_location("wb_core_rpg_backend", path)
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
        "module_configs": {},
        "module_data": {"wb_core_rpg": {}},
    }


def test_grant_early_in_a_long_scene_reaches_the_prompt():
    # The old prompt kept only the last 2500 chars of the last 3 scenes joined,
    # so a boon granted early in a long newest scene was cut out and missed.
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk(json.dumps({"added": [], "removed": [], "altered": []}), captured)
    sentinel = "The hearth-goddess presses a coal into your palm: Emberkiss is yours."
    long_scene = sentinel + (" The evening wears on uneventfully." * 90)
    history = ["Old scene. " * 200, "Older scene. " * 200, long_scene]

    asyncio.run(backend.on_librarian(_state(history), sdk))

    assert sentinel in captured["prompt"]
    assert "THIS TURN'S SCENE" in captured["prompt"]


def test_external_grant_adds_the_skill():
    backend = _load_backend()
    captured = {}
    reply = json.dumps({
        "added": [{
            "name": "Emberkiss",
            "rating": 4,
            "description": "A boon from the hearth-goddess: kindle or snuff small flames by touch.",
            "trigger_words": ["fire", "flame"],
            "type": "active",
        }],
        "removed": [],
        "altered": [],
    })
    sdk = _make_sdk(reply, captured)

    result = asyncio.run(backend.on_librarian(_state(["The ritual completes."]), sdk))

    skills = result["module_data"]["wb_core_rpg"]["skills"]
    assert "emberkiss" in skills
    assert skills["emberkiss"]["rating"] == 4
