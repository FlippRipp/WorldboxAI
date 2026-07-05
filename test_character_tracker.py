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


def _state(history, last_input_text=""):
    return {
        "turn": 3,
        "history": history,
        "last_input_text": last_input_text,
        "characters": {
            "default_player": {
                "name": "Aria",
                "gender": "female",
                "race": "human",
                "full_appearance": "Tall, dark braid, green eyes. Aria's scar marks her left cheek.",
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


def test_player_action_reaches_the_prompt():
    # A rename is usually declared in the player's own input ("call me X"),
    # which never appears in history (storyteller outputs only) — the tracker
    # must see the turn's input too.
    backend = _load_backend()
    captured = {}
    sdk = _make_sdk("{}", captured)
    state = _state(["The innkeeper nods slowly."], last_input_text='I lean in and whisper: "Call me Nyx from now on."')

    asyncio.run(backend.on_librarian(state, sdk))

    assert "Call me Nyx" in captured["prompt"]
    assert "PLAYER'S ACTION THIS TURN" in captured["prompt"]


def test_rename_sweeps_old_name_from_other_fields():
    # If the LLM reports only the new name, the old name must still be swept
    # out of the record's other text fields deterministically.
    backend = _load_backend()
    captured = {}
    reply = json.dumps({"name": "Nyx", "change_note": "Aria now goes by Nyx."})
    sdk = _make_sdk(reply, captured)

    result = asyncio.run(backend.on_librarian(_state(["'Nyx it is,' the innkeeper says."]), sdk))

    update = result["character_update"]
    assert update["name"] == "Nyx"
    assert update["full_appearance"] == "Tall, dark braid, green eyes. Nyx's scar marks her left cheek."
    # Fields that never mentioned the old name are left untouched.
    assert "personality" not in update


def test_manual_update_appearance_rewrites_from_full_story():
    backend = _load_backend()
    captured = {}
    reply = json.dumps({
        "full_appearance": "Weathered and scarred, her braid now streaked with grey.",
        "short_appearance": "A weathered woman with a grey-streaked braid.",
        "change_note": "The record now shows the toll of the campaign.",
    })
    sdk = _make_sdk(reply, captured)
    state = _state(["She fights through the winter.", "The siege leaves its mark."])

    result = asyncio.run(backend.on_command_character(["update", "appearance"], state, sdk))

    # The whole story (not just the latest scene) is in the prompt.
    assert "She fights through the winter." in captured["prompt"]
    assert "The siege leaves its mark." in captured["prompt"]
    update = result["character_update"]
    assert update["full_appearance"].startswith("Weathered")
    assert update["short_appearance"].startswith("A weathered")
    assert "personality" not in update
    log = result["module_data"]["wb_character_tracker"]["evolution_log"]
    assert log[-1]["note"].startswith("Manual update")
    assert "Weathered" in result["message"]


def test_manual_update_personality_ignores_unrequested_fields():
    # Even if the LLM returns appearance fields, a personality-only update
    # must not touch them.
    backend = _load_backend()
    reply = json.dumps({
        "personality": "Hardened but loyal, slower to trust.",
        "full_appearance": "Should be ignored.",
        "change_note": "Personality reflects the betrayal.",
    })
    sdk = _make_sdk(reply, {})

    result = asyncio.run(backend.on_command_character(["update", "personality"], _state(["A friend betrays her."]), sdk))

    update = result["character_update"]
    assert update == {"personality": "Hardened but loyal, slower to trust."}


def test_manual_update_both_targets_all_descriptive_fields():
    backend = _load_backend()
    reply = json.dumps({
        "full_appearance": "New look.",
        "short_appearance": "New look, short.",
        "personality": "New temperament.",
        "change_note": "Full refresh.",
    })
    sdk = _make_sdk(reply, {})

    result = asyncio.run(backend.on_command_character(["update", "both"], _state(["Things happen."]), sdk))

    assert set(result["character_update"]) == {"full_appearance", "short_appearance", "personality"}


def test_manual_update_without_story_or_with_bad_target_is_a_noop():
    backend = _load_backend()
    sdk = _make_sdk("{}", {})

    no_story = asyncio.run(backend.on_command_character(["update", "both"], _state([]), sdk))
    assert "character_update" not in no_story
    assert "no story" in no_story["message"].lower()

    bad_target = asyncio.run(backend.on_command_character(["update", "hat"], _state(["Scene."]), sdk))
    assert "character_update" not in bad_target
    assert "Usage" in bad_target["message"]


def test_manual_update_with_unusable_llm_reply_reports_failure():
    backend = _load_backend()
    sdk = _make_sdk("I cannot do that.", {})

    result = asyncio.run(backend.on_command_character(["update", "appearance"], _state(["Scene."]), sdk))

    assert "character_update" not in result
    assert "try again" in result["message"].lower()


def test_rename_does_not_overwrite_llm_rewritten_fields():
    # When the LLM already rewrote a field for the rename, the deterministic
    # sweep must not clobber it.
    backend = _load_backend()
    reply = json.dumps({
        "name": "Nyx",
        "full_appearance": "Tall, dark braid, green eyes. A fresh brand covers Nyx's old scar.",
        "change_note": "Aria took the name Nyx and was branded.",
    })
    sdk = _make_sdk(reply, {})

    result = asyncio.run(backend.on_librarian(_state(["The brand sizzles."]), sdk))

    update = result["character_update"]
    assert update["full_appearance"] == "Tall, dark braid, green eyes. A fresh brand covers Nyx's old scar."
